"""CR-005 deploy tests: BUILD-owned ordering drop-in diff plan (BUILD-REQ-049).

Covers install/replace, stale removal from the *previous deployed* artifact
manifest, missing-target no-ops, hash verification, dry-run visibility and
rollback of newly created drop-ins.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from tbox_build.deploy import Deployer, OrderingDropInPlan
from tbox_build.manifest import Project
from tbox_build.ordering import (
    ORDERING_DROPIN_FILENAME,
    dropin_rel_path,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _artifact(rel: str, content: str) -> dict:
    return {
        "path": f"rootfs/{rel}",
        "sha256": _sha(content),
        "build_owned": True,
        "after_ordering": ["tbox-prov.service"],
    }


def _build_package(tmp_path: Path, dropins: dict[str, str]) -> Path:
    """Package with install-root drop-ins + a target artifact manifest."""
    payload = tmp_path / "install-root"
    artifacts = []
    for rel, content in dropins.items():
        f = payload / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        artifacts.append(_artifact(rel, content))
    manifests = tmp_path / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "artifact-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )
    pkg = tmp_path / "pkg.tar.gz"
    with tarfile.open(pkg, "w:gz") as tar:
        for p in sorted(payload.rglob("*")):
            tar.add(p, arcname=str(p.relative_to(tmp_path)))
        tar.add(manifests / "artifact-manifest.json",
                arcname="manifests/artifact-manifest.json")
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    pkg.with_suffix(".sha256").write_text(f"{digest}  {pkg.name}\n", encoding="utf-8")
    return pkg


def _write_previous_manifest(tmp_path: Path, dropins: dict[str, str]) -> Path:
    artifacts = [_artifact(rel, content) for rel, content in dropins.items()]
    path = tmp_path / "previous-artifact-manifest.json"
    path.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    return path


def _deployer(project_root: Path, **kw) -> Deployer:
    return Deployer(Project(project_root), target_host="orin.local",
                    target_user="tbox", identity="/k/id", **kw)


def _step(report, name: str):
    return next(s for s in report.steps if s.name == name)


DROPIN_B = dropin_rel_path("tbox-b.service")
CONTENT_B = "[Unit]\nAfter=tbox-prov.service\n"


class TestOrderingDropInPlan:
    def test_install_dropin_planned(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        report = _deployer(project_root).deploy(pkg, execute=False)
        assert report.status == "success (dry-run)"
        step = _step(report, "ordering-dropins")
        joined = "\n".join(step.commands)
        assert "install -D -m 0644" in joined
        assert f"/etc/systemd/system/tbox-b.service.d/{ORDERING_DROPIN_FILENAME}" in joined
        assert report.ordering_plan["install"][0]["rel_path"] == DROPIN_B
        assert report.ordering_plan["install"][0]["sha256"] == _sha(CONTENT_B)

    def test_base_rsync_excludes_dropins(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        report = _deployer(project_root).deploy(pkg, execute=False)
        install = _step(report, "install")
        joined = "\n".join(install.commands)
        assert "--exclude=" in joined
        assert f"etc/systemd/system/*.d/{ORDERING_DROPIN_FILENAME}" in joined

    def test_stale_dropin_removed_from_previous_manifest(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        stale_rel = dropin_rel_path("tbox-old.service")
        previous = _write_previous_manifest(tmp_path, {stale_rel: "[Unit]\nAfter=x\n"})
        report = _deployer(project_root, previous_manifest=previous).deploy(
            pkg, execute=False
        )
        step = _step(report, "ordering-dropins")
        joined = "\n".join(step.commands)
        assert "rm -f -- /etc/systemd/system/tbox-old.service.d/10-tbox-order.conf" in joined
        assert "if [ -e /etc/systemd/system/tbox-old.service.d/10-tbox-order.conf ]" in joined
        # The still-targeted drop-in is not removed.
        assert report.ordering_plan["remove"][0]["rel_path"] == stale_rel

    def test_no_previous_manifest_warns_and_never_guesses(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        report = _deployer(project_root).deploy(pkg, execute=False)
        step = _step(report, "ordering-dropins")
        assert "previous artifact manifest not provided" in step.message
        assert any("previous artifact manifest not provided" in w
                   for w in report.ordering_plan["warnings"])
        assert report.ordering_plan["previous_manifest_available"] is False
        assert "rm -f" not in "\n".join(step.commands)

    def test_unloadable_previous_manifest_warns(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        report = _deployer(
            project_root, previous_manifest=tmp_path / "missing.json"
        ).deploy(pkg, execute=False)
        assert any("could not be loaded" in w
                   for w in report.ordering_plan["warnings"])

    def test_hash_mismatch_not_installed(self, tmp_path, project_root):
        # Build a package whose manifest declares a bogus hash for the drop-in.
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        payload = tmp_path / "install-root"
        manifests = tmp_path / "manifests"
        data = json.loads((manifests / "artifact-manifest.json").read_text())
        data["artifacts"][0]["sha256"] = "0" * 64
        (manifests / "artifact-manifest.json").write_text(json.dumps(data))
        pkg2 = tmp_path / "pkg2.tar.gz"
        with tarfile.open(pkg2, "w:gz") as tar:
            for p in sorted(payload.rglob("*")):
                tar.add(p, arcname=str(p.relative_to(tmp_path)))
            tar.add(manifests / "artifact-manifest.json",
                    arcname="manifests/artifact-manifest.json")
        digest = hashlib.sha256(pkg2.read_bytes()).hexdigest()
        pkg2.with_suffix(".sha256").write_text(f"{digest}  {pkg2.name}\n")
        report = _deployer(project_root).deploy(pkg2, execute=False)
        assert report.ordering_plan["install"] == []
        assert any("hash mismatch" in w for w in report.ordering_plan["warnings"])

    def test_ordering_step_before_daemon_reload(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        report = _deployer(project_root).deploy(pkg, execute=False)
        names = [s.name for s in report.steps]
        assert names.index("install") < names.index("ordering-dropins")
        assert names.index("ordering-dropins") < names.index("daemon-reload")

    def test_no_wildcard_directory_delete(self, tmp_path, project_root):
        pkg = _build_package(tmp_path, {DROPIN_B: CONTENT_B})
        previous = _write_previous_manifest(
            tmp_path, {dropin_rel_path("tbox-old.service"): "[Unit]\nAfter=x\n"}
        )
        report = _deployer(project_root, previous_manifest=previous).deploy(
            pkg, execute=False
        )
        commands = "\n".join(_step(report, "ordering-dropins").commands)
        # Only the fixed file name may be removed; the .d directory is never
        # wildcard-deleted.
        assert "rm -rf" not in commands
        assert "*.d" not in commands
        assert "10-tbox-order.conf" in commands


class TestScriptsAndRollback:
    def test_remove_script_guards_existence(self):
        script = Deployer._remove_dropin_script("/etc/systemd/system/x.d/10-tbox-order.conf")
        assert script.startswith("if [ -e ")
        assert "rm -f -- " in script
        assert "|| true" not in script  # failures must stay visible

    def test_install_script_quotes_paths(self):
        script = Deployer._install_dropin_script(
            "/tmp/stage/rootfs/etc/systemd/system/x.d/10-tbox-order.conf",
            "/etc/systemd/system/x.d/10-tbox-order.conf",
        )
        assert script.startswith("install -D -m 0644 ")
        assert "/etc/systemd/system/x.d/10-tbox-order.conf" in script
        # shell metacharacters in a path are quoted to a single word
        quoted = Deployer._install_dropin_script("src", "/etc/a b/c")
        assert "'/etc/a b/c'" in quoted

    def test_rollback_removes_only_newly_created_dropins(self, project_root, monkeypatch):
        d = _deployer(project_root)
        d._ordering_previous = {
            DROPIN_B: False,  # newly created -> must be removed
            dropin_rel_path("tbox-prov.service"): True,  # pre-existing -> kept
        }
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd
            return 0, ""

        monkeypatch.setattr(d, "_run", fake_run)
        step = d._rollback()
        assert step.status == "success"
        cmd = captured["cmd"]
        assert "tbox-b.service.d/10-tbox-order.conf" in cmd
        assert "tbox-prov.service.d/10-tbox-order.conf" not in cmd

    def test_empty_plan_summary(self):
        plan = OrderingDropInPlan()
        assert plan.empty
        assert "0 install/replace" in plan.summary()
