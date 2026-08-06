"""Deploy framework for TBOX Build.

Automated deployment of a release package to an Orin device over SSH:

    pre-check -> extract -> upload -> backup -> install -> ldconfig
    -> daemon-reload -> enable/restart (dependency order) -> smoke
    (with automatic rollback to the pre-deploy backup on failure)

The pipeline is *dry-run by default*: it prints the exact ssh/rsync/systemctl
command plan without touching the device. Pass ``execute=True`` (CLI:
``--execute``) with a ``--host`` to perform the real deployment. Nothing about
a specific device is hard-coded; host/user/identity/port/sudo are provided at
invocation time.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import TboxBuildError
from .manifest import Project, ConfigDeploymentManifest
from .graph import DependencyGraph
from .staging import sha256_file


@dataclass
class DeployStep:
    name: str
    status: str = "pending"  # pending, success, failed, skipped, planned
    message: str = ""
    commands: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class DeployReport:
    package_path: str = ""
    target_host: str = ""
    steps: list[DeployStep] = field(default_factory=list)
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    config_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Configuration deployment planner (CR-003 §7.2)
# ---------------------------------------------------------------------------


@dataclass
class ConfigDeployAction:
    """A single config-file deployment decision."""

    target_path: str  # /etc/tbox/common.yaml
    action: str  # replace | preserve | create_if_missing | preserve-default
    reason: str
    payload_sha256: str | None = None
    device_sha256: str | None = None
    authority_violation: bool = False


@dataclass
class ConfigDeployPlan:
    """Aggregated config deployment plan for a payload."""

    platform: str
    actions: list[ConfigDeployAction] = field(default_factory=list)
    replace_count: int = 0
    preserve_count: int = 0
    create_count: int = 0
    violations: int = 0

    @property
    def passed(self) -> bool:
        return self.violations == 0

    def summary(self) -> str:
        return (
            f"{self.replace_count} replace, {self.preserve_count} preserve, "
            f"{self.create_count} create_if_missing, {self.violations} violation(s)"
        )


class ConfigDeployPlanner:
    """Computes a create/replace/preserve plan for config files (CR-003 §7.2).

    Given a payload (the extracted install-root) and the config-deployment
    manifest, determines the action for every ``/etc/tbox/**`` file in the
    payload. A simulated device root may be supplied to model the device's
    existing config state (used by tests and dry-run planning).
    """

    def __init__(self, cdm: ConfigDeploymentManifest, platform: str):
        self.cdm = cdm
        self.platform = platform

    def plan(
        self,
        payload_root: Path,
        device_root: Path | None = None,
    ) -> ConfigDeployPlan:
        plan = ConfigDeployPlan(platform=self.platform)
        tbox_etc = payload_root / "etc" / "tbox"
        if not tbox_etc.is_dir():
            return plan
        for path in sorted(tbox_etc.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(payload_root).as_posix()
            target_path = "/" + rel
            payload_sha = sha256_file(path)
            device_sha: str | None = None
            if device_root is not None:
                dev_file = device_root / rel
                if dev_file.is_file():
                    device_sha = sha256_file(dev_file)
            rule = self.cdm.match(self.platform, target_path)
            if rule is None:
                # Unmatched /etc/tbox/** -> default preserve (§7.1)
                plan.actions.append(ConfigDeployAction(
                    target_path=target_path,
                    action="preserve",
                    reason="unmatched path defaults to preserve",
                    payload_sha256=payload_sha,
                    device_sha256=device_sha,
                ))
                plan.preserve_count += 1
            elif rule.category == "device-managed":
                if rule.deploy_policy == "create_if_missing":
                    # Package may provide a skeleton; installed only if the
                    # device lacks the file (§7.2 step 5).
                    exists = device_sha is not None
                    plan.actions.append(ConfigDeployAction(
                        target_path=target_path,
                        action="create_if_missing",
                        reason=("device already has file" if exists
                                else "device lacks file; will initialize"),
                        payload_sha256=payload_sha,
                        device_sha256=device_sha,
                    ))
                    plan.create_count += 1
                else:
                    # device-managed + preserve/replace: the package must not
                    # contain this file (§7.1: device-managed files are not
                    # packaged; §7.2 step 6 verifies no authority breach).
                    plan.actions.append(ConfigDeployAction(
                        target_path=target_path,
                        action="preserve",
                        reason=(f"device-managed ({rule.owner}); package must not "
                                f"contain this file"),
                        payload_sha256=payload_sha,
                        device_sha256=device_sha,
                        authority_violation=True,
                    ))
                    plan.preserve_count += 1
                    plan.violations += 1
            elif rule.deploy_policy == "replace":
                plan.actions.append(ConfigDeployAction(
                    target_path=target_path,
                    action="replace",
                    reason="release-managed replace",
                    payload_sha256=payload_sha,
                    device_sha256=device_sha,
                ))
                plan.replace_count += 1
            elif rule.deploy_policy == "create_if_missing":
                exists = device_sha is not None
                plan.actions.append(ConfigDeployAction(
                    target_path=target_path,
                    action="create_if_missing",
                    reason=("device already has file" if exists
                            else "device lacks file; will initialize"),
                    payload_sha256=payload_sha,
                    device_sha256=device_sha,
                ))
                plan.create_count += 1
            elif rule.deploy_policy == "preserve":
                plan.actions.append(ConfigDeployAction(
                    target_path=target_path,
                    action="preserve",
                    reason="release-managed preserve",
                    payload_sha256=payload_sha,
                    device_sha256=device_sha,
                ))
                plan.preserve_count += 1
        return plan


class Deployer:
    """Deploys a release package to a target device over SSH."""

    def __init__(
        self,
        project: Project,
        target_host: str | None = None,
        target_user: str = "tbox",
        identity: str | None = None,
        port: int = 22,
        use_sudo: bool = True,
        ask_pass: bool = False,
        ask_sudo_pass: bool = False,
        rollback_on_failure: bool = True,
        smoke_settle_seconds: int = 3,
        platform: str = "orin",
    ):
        self.project = project
        self.target_host = target_host
        self.target_user = target_user
        self.identity = identity
        self.port = port
        self.use_sudo = use_sudo
        # ask_pass: authenticate the SSH session interactively (password or key
        # passphrase) ONCE via a multiplexed master connection, reused by every
        # step. ask_sudo_pass: prompt once for the remote sudo password and feed
        # it to `sudo -S` over stdin (never on the command line).
        self.ask_pass = ask_pass
        self.ask_sudo_pass = ask_sudo_pass
        self.rollback_on_failure = rollback_on_failure
        self.smoke_settle_seconds = smoke_settle_seconds
        self.staging_platform = platform
        self._units: list[str] = []
        self._config_plan: ConfigDeployPlan | None = None
        self._control_path: str | None = None
        self._sudo_pw: str | None = None
        self._stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.remote_stage = f"/tmp/tbox-deploy-{self._stamp}"
        self.backup_path = f"/var/tbox/backups/tbox-{self._stamp}.tar.gz"

    # -- ssh / rsync helpers ----------------------------------------------

    def _ssh_opts(self) -> list[str]:
        opts = ["-o", "StrictHostKeyChecking=accept-new"]
        # Per-command connections never prompt: either a key is used, or they
        # reuse the already-authenticated master socket (ControlPath).
        opts += ["-o", "BatchMode=yes"]
        if self._control_path:
            opts += ["-o", f"ControlPath={self._control_path}"]
        if self.identity:
            opts += ["-i", self.identity]
        if self.port and self.port != 22:
            opts += ["-p", str(self.port)]
        return opts

    def _target(self) -> str:
        return f"{self.target_user}@{self.target_host}"

    def _ssh_cmd(self, remote_command: str) -> list[str]:
        return ["ssh", *self._ssh_opts(), self._target(), remote_command]

    def _rsync_cmd(self, local_src: str, remote_dst: str) -> list[str]:
        ssh = "ssh " + " ".join(self._ssh_opts())
        return ["rsync", "-a", "--delete", "-e", ssh,
                local_src, f"{self._target()}:{remote_dst}"]

    def _sudo(self, cmd: str) -> str:
        if not self.use_sudo:
            return cmd
        # -S reads the password from stdin; -p '' suppresses the prompt text.
        return f"sudo -S -p '' {cmd}" if self.ask_sudo_pass else f"sudo {cmd}"

    def _sudo_sh(self, script: str) -> str:
        """Run a whole (possibly compound, with &&/||) script under one sudo.

        Without this, only the first command of an ``a && b`` chain would be
        privileged; ``b`` would run as the login user.
        """
        if not self.use_sudo:
            return f"sh -c {shlex.quote(script)}"
        prefix = "sudo -S -p '' " if self.ask_sudo_pass else "sudo "
        return prefix + "sh -c " + shlex.quote(script)

    # -- ssh connection multiplexing (single authentication) --------------

    def _connect(self) -> bool:
        """Open a multiplexed SSH master connection (authenticate once).

        Runs interactively so the user is prompted at most once for a password
        or key passphrase; all subsequent ssh/rsync calls reuse the socket.
        """
        opts = ["-o", "StrictHostKeyChecking=accept-new",
                "-o", "ControlMaster=yes", "-o", "ControlPersist=600",
                "-o", f"ControlPath={self._control_path}"]
        if not self.ask_pass:
            # key/agent auth only (no interactive prompt) unless --ask-pass
            opts += ["-o", "BatchMode=yes"]
        if self.identity:
            opts += ["-i", self.identity]
        if self.port and self.port != 22:
            opts += ["-p", str(self.port)]
        cmd = ["ssh", "-N", "-f", *opts, self._target()]
        # Inherit the terminal so the password/passphrase prompt is visible.
        return subprocess.run(cmd).returncode == 0

    def _disconnect(self) -> None:
        if not self._control_path:
            return
        subprocess.run(
            ["ssh", "-o", f"ControlPath={self._control_path}", "-O", "exit",
             self._target()],
            capture_output=True, text=True,
        )

    def _ssh_str(self, remote_command: str) -> str:
        return " ".join(shlex.quote(c) for c in self._ssh_cmd(remote_command))

    def _run_smoke(self, units: list[str]) -> DeployStep:
        """Per-unit health check with diagnostics.

        Reports every unit's state (not just the first failure) and, for any
        unit that is not active, captures a short ``systemctl status`` tail so
        the report explains *what* failed instead of a bare exit code.
        """
        step = DeployStep(name="smoke")
        if self.smoke_settle_seconds > 0:
            time.sleep(self.smoke_settle_seconds)  # let Type=simple units settle
        states: list[str] = []
        failing: list[str] = []
        for unit in units:
            rc, out = self._run(self._ssh_str(f"systemctl is-active {unit}"))
            state = out.strip() or ("active" if rc == 0 else "unknown")
            states.append(f"{unit}={state}")
            if rc != 0:
                failing.append(unit)
        step.message = "unit states: " + ", ".join(states)
        if failing:
            diags = []
            for unit in failing:
                _, d = self._run(self._ssh_str(
                    f"systemctl --no-pager --full status {unit} 2>&1 | tail -n 25"))
                diags.append(f"----- {unit} -----\n{d.strip()}")
            step.status = "failed"
            step.message += "\nnot active: " + ", ".join(failing) + "\n" + "\n".join(diags)
        else:
            step.status = "success"
        return step

    # -- restart order ----------------------------------------------------

    def _ordered_units(self, payload_root: Path) -> list[str]:
        """Return systemd units present in the payload, in dependency order."""
        unit_dir = payload_root / "usr" / "lib" / "systemd" / "system"
        present = {p.name for p in unit_dir.glob("*.service")} if unit_dir.is_dir() else set()
        if not present:
            return []
        try:
            sm = self.project.load_service_manifest()
            graph = DependencyGraph(sm)
            svc_units = {
                sid: list(svc.runtime.systemd_units)
                for sid, svc in sm.services.items()
            }
            ordered: list[str] = []
            for sid in graph.build_order():
                for unit in svc_units.get(sid, []):
                    if unit in present and unit not in ordered:
                        ordered.append(unit)
            # append any present units not mapped to a service (stable)
            for unit in sorted(present):
                if unit not in ordered:
                    ordered.append(unit)
            return ordered
        except Exception:
            return sorted(present)

    # -- plan -------------------------------------------------------------

    def build_plan(self, package_path: Path, payload_root: Path) -> list[DeployStep]:
        """Build the ordered deploy step plan (command lists only).

        Config files under ``/etc/tbox/**`` are deployed according to the
        config-deployment manifest (CR-003 §7.2): replace files are backed up
        and overwritten, preserve files are skipped, create_if_missing files
        are installed only when absent. The base rsync excludes ``etc/tbox/**``
        so config files are never blindly overwritten.
        """
        units = self._ordered_units(payload_root)
        self._units = units
        steps: list[DeployStep] = []

        # Compute the config deployment plan (CR-003 §7.2 steps 1-3).
        cdm = self.project.load_config_deployment_manifest()
        planner = ConfigDeployPlanner(cdm, self.staging_platform)
        config_plan = planner.plan(payload_root)
        self._config_plan = config_plan

        steps.append(DeployStep("upload", commands=[
            " ".join(shlex.quote(c) for c in
                     self._ssh_cmd(f"mkdir -p {self.remote_stage}/rootfs")),
            " ".join(shlex.quote(c) for c in
                     self._rsync_cmd(f"{payload_root}/", f"{self.remote_stage}/rootfs/")),
        ]))
        steps.append(DeployStep("backup", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo_sh(
                f"mkdir -p /var/tbox/backups && "
                f"tar -C / -czf {self.backup_path} "
                f"$(cd {self.remote_stage}/rootfs && find . -type f -o -type l | sed 's#^\\./##') "
                f"2>/dev/null || true"))),
        ]))
        # config-plan: report the create/replace/preserve decisions (no remote
        # command; the decisions drive the config-install step below).
        steps.append(DeployStep("config-plan", commands=[],
                                message=f"{config_plan.summary()}"))
        for act in config_plan.actions:
            steps[-1].commands.append(
                f"# {act.target_path}: {act.action} ({act.reason})")
        # install: base rsync excludes etc/tbox/** so configs are policy-driven
        steps.append(DeployStep("install", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(
                self._sudo(f"rsync -a --exclude=etc/tbox/** "
                           f"{self.remote_stage}/rootfs/ /"))),
        ]))
        # config-install: per-file policy-aware install (CR-003 §7.2 steps 4-7)
        config_cmds: list[str] = []
        for act in config_plan.actions:
            rel = act.target_path.lstrip("/")
            src = f"{self.remote_stage}/rootfs/{rel}"
            if act.action == "replace":
                config_cmds.append(" ".join(shlex.quote(c) for c in self._ssh_cmd(
                    self._sudo(f"install -m 644 {src} {act.target_path}"))))
            elif act.action == "create_if_missing":
                config_cmds.append(" ".join(shlex.quote(c) for c in self._ssh_cmd(
                    self._sudo_sh(
                        f"test -f {act.target_path} || "
                        f"install -m 644 {src} {act.target_path}"))))
            # preserve: no command (skipped)
        steps.append(DeployStep("config-install", commands=config_cmds,
                                message=f"{config_plan.replace_count} replace, "
                                        f"{config_plan.create_count} create_if_missing, "
                                        f"{config_plan.preserve_count} preserve (skipped)"))
        steps.append(DeployStep("ldconfig", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo("ldconfig"))),
        ]))
        steps.append(DeployStep("daemon-reload", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(self._sudo("systemctl daemon-reload"))),
        ]))
        restart_cmds = []
        for unit in units:
            restart_cmds.append(" ".join(shlex.quote(c) for c in self._ssh_cmd(
                self._sudo_sh(f"systemctl enable --now {unit} && systemctl restart {unit}"))))
        steps.append(DeployStep("restart", commands=restart_cmds,
                                message=f"units (dependency order): {', '.join(units) or '(none)'}"))
        smoke_cmds = [" ".join(shlex.quote(c) for c in self._ssh_cmd(
            f"systemctl is-active {unit}")) for unit in units]
        steps.append(DeployStep("smoke", commands=smoke_cmds,
                                message="per-unit is-active + status tail on failure"))
        steps.append(DeployStep("cleanup", commands=[
            " ".join(shlex.quote(c) for c in self._ssh_cmd(f"rm -rf {self.remote_stage}")),
        ]))
        return steps

    # -- pre-check --------------------------------------------------------

    def _pre_check(self, package_path: Path) -> DeployStep:
        step = DeployStep(name="pre-check")
        start = time.time()
        if not package_path.is_file():
            step.status = "failed"
            step.message = f"Package not found: {package_path}"
            step.duration_seconds = time.time() - start
            return step
        checksum_file = package_path.with_suffix(".sha256")
        if checksum_file.is_file():
            expected = checksum_file.read_text().split()[0]
            actual = sha256_file(package_path)
            if expected != actual:
                step.status = "failed"
                step.message = f"Checksum mismatch: expected {expected}, got {actual}"
                step.duration_seconds = time.time() - start
                return step
        step.status = "success"
        step.message = f"Package verified ({package_path.name})"
        step.duration_seconds = time.time() - start
        return step

    def _extract(self, package_path: Path, dest: Path) -> Path:
        """Extract the package and return the install-root (payload) dir."""
        with tarfile.open(package_path, "r:gz") as tar:
            # Safe extraction (reject path traversal / unsafe members) where
            # supported (Python >= 3.12); fall back for older runtimes.
            try:
                tar.extractall(dest, filter="data")
            except TypeError:
                tar.extractall(dest)
        # package stores paths under install-root/
        candidates = list(dest.rglob("install-root"))
        for c in candidates:
            if c.is_dir():
                return c
        raise TboxBuildError(f"Package has no install-root: {package_path}")

    # -- run --------------------------------------------------------------

    def _run(self, cmd: str) -> tuple[int, str]:
        stdin_data = None
        if self.ask_sudo_pass and self._sudo_pw is not None and "sudo -S" in cmd:
            stdin_data = self._sudo_pw + "\n"
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, input=stdin_data,
        )
        return proc.returncode, (proc.stdout + proc.stderr)

    def deploy(self, package_path: Path, execute: bool = False) -> DeployReport:
        """Execute (or plan) the deploy pipeline.

        Dry-run by default; set *execute* True (and a target host) to run.
        """
        report = DeployReport(
            package_path=str(package_path),
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        pre = self._pre_check(package_path)
        report.steps.append(pre)
        if pre.status == "failed":
            report.status = "failed"
            report.errors.append(pre.message)
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        do_execute = execute and bool(self.target_host)

        with tempfile.TemporaryDirectory(prefix="tbox-deploy-") as tmp:
            payload_root = self._extract(package_path, Path(tmp))

            if do_execute:
                # One authentication for the whole run: open a multiplexed SSH
                # master (prompts once for password/passphrase if needed), and
                # collect the sudo password once if requested.
                self._control_path = str(Path(tmp) / "cm.sock")
                if self.ask_sudo_pass:
                    import getpass
                    self._sudo_pw = getpass.getpass(
                        f"[{self.target_host}] sudo password: ")
                connect = DeployStep(name="connect")
                if not self._connect():
                    connect.status = "failed"
                    connect.message = "SSH authentication/connection failed"
                    report.steps.append(connect)
                    report.status = "failed"
                    report.errors.append("connect: SSH authentication/connection failed")
                    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    return report
                connect.status = "success"
                connect.message = f"authenticated to {self._target()} (multiplexed)"
                report.steps.append(connect)

            plan = self.build_plan(package_path, payload_root)

            # Record the config deployment plan in the report (CR-003 §7.2).
            if self._config_plan is not None:
                report.config_plan = {
                    "platform": self._config_plan.platform,
                    "summary": self._config_plan.summary(),
                    "replace_count": self._config_plan.replace_count,
                    "preserve_count": self._config_plan.preserve_count,
                    "create_count": self._config_plan.create_count,
                    "violations": self._config_plan.violations,
                    "actions": [asdict(a) for a in self._config_plan.actions],
                }
                # Authority violations: package contains device-managed files.
                if self._config_plan.violations > 0:
                    report.status = "failed"
                    for a in self._config_plan.actions:
                        if a.authority_violation:
                            report.errors.append(
                                f"config-plan: package contains device-managed "
                                f"file {a.target_path} (must not be packaged)"
                            )
                    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    return report

            if not do_execute:
                for step in plan:
                    step.status = "planned"
                    if not step.message:
                        step.message = f"{len(step.commands)} command(s) (dry-run)"
                    report.steps.append(step)
                if execute and not self.target_host:
                    report.errors.append("--execute requires --host")
                report.status = "success (dry-run)"
                report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                return report

            backed_up = False
            try:
                for step in plan:
                    if step.name == "smoke":
                        # Rich per-unit diagnostics instead of a bare rc check.
                        smoke = self._run_smoke(self._units)
                        report.steps.append(smoke)
                        if smoke.status == "failed":
                            report.status = "failed"
                            report.errors.append(f"smoke: {smoke.message}")
                            if self.rollback_on_failure and backed_up:
                                report.steps.append(self._rollback())
                            else:
                                report.steps.append(DeployStep(
                                    name="rollback", status="skipped",
                                    message="skipped (--no-rollback); deploy left "
                                            "on device for inspection"))
                            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            return report
                        continue
                    if step.name == "config-plan":
                        # Decision report only; no remote command to execute.
                        step.status = "success"
                        report.steps.append(step)
                        continue
                    start = time.time()
                    failed = False
                    for cmd in step.commands:
                        rc, out = self._run(cmd)
                        if rc != 0:
                            step.message = (step.message + " " if step.message else "") + \
                                f"command failed (rc={rc}): {out.strip()[:400]}"
                            failed = True
                            break
                    step.duration_seconds = time.time() - start
                    step.status = "failed" if failed else "success"
                    report.steps.append(step)
                    if step.name == "backup" and not failed:
                        backed_up = True
                    if failed:
                        report.status = "failed"
                        report.errors.append(f"{step.name}: {step.message}")
                        if (self.rollback_on_failure and backed_up
                                and step.name in ("install", "config-install",
                                                  "ldconfig",
                                                  "daemon-reload", "restart")):
                            report.steps.append(self._rollback())
                        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        return report
            finally:
                self._disconnect()

        report.status = "success"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report

    def _rollback(self) -> DeployStep:
        step = DeployStep(name="rollback")
        start = time.time()
        cmds = [
            self._ssh_cmd(self._sudo_sh(
                f"tar -C / -xzf {self.backup_path} && ldconfig && systemctl daemon-reload")),
        ]
        step.commands = [" ".join(shlex.quote(c) for c in cmds[0])]
        rc, out = self._run(step.commands[0])
        step.status = "success" if rc == 0 else "failed"
        step.message = f"restored from {self.backup_path}" if rc == 0 else out.strip()[:400]
        step.duration_seconds = time.time() - start
        return step

    def rollback(self, backup_path: Path | str) -> DeployReport:
        """Manually roll back to a specific on-device backup archive."""
        report = DeployReport(
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.backup_path = str(backup_path)
        if not self.target_host:
            report.steps.append(DeployStep(
                name="rollback", status="planned",
                message="requires --host",
            ))
            report.status = "success (dry-run)"
        else:
            report.steps.append(self._rollback())
            report.status = "success" if report.steps[-1].status == "success" else "failed"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report
