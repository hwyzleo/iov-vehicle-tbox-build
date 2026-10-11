"""BUILD-owned systemd ordering drop-in materialization (CR-005, BUILD-REQ-049).

BUILD compiles the cross-service ordering contract already declared in each
service's ``runtime.after`` into its own fixed-named systemd drop-in. The
service repository keeps owning the original unit file; BUILD never rewrites,
copies or merges it.

Output is fixed::

    <install-root>/etc/systemd/system/<unit>.d/10-tbox-order.conf

The drop-in only ever declares ``[Unit] After=...``: no ``Requires=`` /
``Wants=`` and no failure-propagation semantics. A service whose ``after`` is
empty produces no file; removing a drop-in left behind by a previous release
is the deployment diff plan's responsibility (CR-005 §6).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BuildFailure
from .manifest import ServiceManifest
from .staging import sha256_file

# Fixed BUILD-owned file name and directory. The name is a stable contract:
# any ``<unit>.d/10-tbox-order.conf`` under the systemd unit tree is
# BUILD-owned and may be replaced/removed by the deploy diff plan.
ORDERING_DROPIN_FILENAME = "10-tbox-order.conf"
ORDERING_DROPIN_DIR_REL = "etc/systemd/system"

# Valid systemd unit name. The unit name becomes part of the output path, so
# a hostile name must never be able to escape the fixed directory or inject
# shell/path metacharacters.
_UNIT_NAME_RE = re.compile(
    r"^[A-Za-z0-9@._-]+\.(service|socket|timer|target|path|mount|slice)$"
)


@dataclass(frozen=True)
class OrderingDropIn:
    """A materialized BUILD-owned ordering drop-in."""

    rel_path: str  # relative to install-root
    target_path: str  # logical on-device path (/etc/systemd/system/...)
    unit: str
    source_service: str
    after: tuple[str, ...]  # normalized (deduplicated + lexicographically sorted)
    mode: int
    sha256: str


def dropin_rel_path(unit: str) -> str:
    """Return the install-root-relative path of *unit*'s ordering drop-in."""
    return f"{ORDERING_DROPIN_DIR_REL}/{unit}.d/{ORDERING_DROPIN_FILENAME}"


def render_ordering_dropin(after: list[str] | tuple[str, ...]) -> str:
    """Render the drop-in body: deduplicated, lexicographically sorted.

    The result is UTF-8 with a single trailing newline and only the
    ``[Unit]`` / ``After=`` lines mandated by CR-005 §5.2.
    """
    ordered = sorted({a for a in after if a})
    return "[Unit]\nAfter=" + " ".join(ordered) + "\n"


def _invalid_unit_name(name: str) -> str | None:
    if not _UNIT_NAME_RE.match(name):
        return (
            f"'{name}' is not a valid systemd unit name "
            f"(expected [A-Za-z0-9@._-]+ with a .service/.socket/.timer/"
            f".target/.path/.mount/.slice suffix)"
        )
    return None


def _validate_dropin_content(content: str, rel_path: str) -> None:
    """Assert the generated content obeys the CR-005 §5.2 content contract.

    Only ``[Unit]`` and ``After=`` lines (plus blank lines) are allowed. This
    is a defence-in-depth check on the generation itself so that a future
    change cannot silently start emitting ``Requires=``/``Wants=`` or any
    other unit semantics.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "[Unit]":
            continue
        if line.startswith("After="):
            continue
        raise BuildFailure(
            f"generated ordering drop-in {rel_path} contains a forbidden "
            f"directive: {raw!r} (only [Unit] and After= are allowed)"
        )


def materialize_systemd_ordering(
    install_root: Path,
    service_manifest: ServiceManifest,
) -> list[OrderingDropIn]:
    """Generate BUILD-owned ordering drop-ins into *install_root*.

    For every service with a non-empty ``runtime.after``, one drop-in is
    written per declared ``runtime.systemd_units`` member. Multiple units of
    one service share the same service-level ``after`` list (unit-level
    differences would need a separate schema extension, CR-005 §5.2).

    Raises :class:`BuildFailure` when:
      * a unit name or ``after`` entry is not a valid systemd unit name;
      * two services claim the same drop-in path;
      * a file already exists at the target path in staging (BUILD must not
        overwrite a service-repository or platform-overlay file, §5.3).

    Returns the list of materialized records (deterministic order: service
    declaration order, then unit declaration order).
    """
    install_root = Path(install_root)
    records: list[OrderingDropIn] = []
    claimed: dict[str, str] = {}  # rel_path -> owning service id

    for svc in service_manifest:
        after = [a for a in svc.runtime.after]
        if not after:
            continue
        for entry in after:
            err = _invalid_unit_name(entry)
            if err is not None:
                raise BuildFailure(
                    f"Service '{svc.id}' runtime.after entry {err}"
                )
        normalized = tuple(sorted(set(after)))
        content = render_ordering_dropin(normalized)

        for unit in svc.runtime.systemd_units:
            err = _invalid_unit_name(unit)
            if err is not None:
                raise BuildFailure(
                    f"Service '{svc.id}' systemd unit {err}"
                )
            rel = dropin_rel_path(unit)
            if not rel.startswith(ORDERING_DROPIN_DIR_REL + "/"):
                raise BuildFailure(
                    f"refusing to materialize ordering drop-in outside "
                    f"{ORDERING_DROPIN_DIR_REL}: {rel}"
                )
            _validate_dropin_content(content, rel)

            prior_owner = claimed.get(rel)
            if prior_owner is not None:
                raise BuildFailure(
                    f"ordering drop-in path {rel} is claimed by both "
                    f"'{prior_owner}' and '{svc.id}'"
                )

            dst = install_root / rel
            if dst.exists() or dst.is_symlink():
                raise BuildFailure(
                    f"BUILD-owned ordering drop-in already exists in staging: "
                    f"{rel} (BUILD must not overwrite a service-repository or "
                    f"platform-overlay file)"
                )

            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            os.chmod(dst, 0o644)

            records.append(OrderingDropIn(
                rel_path=rel,
                target_path="/" + rel,
                unit=unit,
                source_service=svc.id,
                after=normalized,
                mode=0o644,
                sha256=sha256_file(dst),
            ))
            claimed[rel] = svc.id

    return records
