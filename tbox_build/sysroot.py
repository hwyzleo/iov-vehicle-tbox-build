"""Sysroot management for TBOX Build.

Provides tools to import, verify and maintain the Orin sysroot:

  * fix_absolute_symlinks - rewrite absolute symlinks to relative paths
  * generate_manifest      - file manifest with structure digest
  * verify_sysroot         - check architecture and key libraries
  * import_sysroot         - full import pipeline (fix + manifest + verify)

The sysroot is a read-only, versioned input.  Changes to its content
trigger a full rebuild of all services.
"""

from __future__ import annotations

import hashlib
import json
import os
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .elfcheck import parse_elf, EM_AARCH64, _ELFCLASS64, classify_file
from .manifest import Project


class SysrootManager:
    """Manages the orin-r35.3.1 sysroot."""

    def __init__(self, project: Project):
        self.project = project
        self.sysroot_path = project.sysroot_path
        self.manifest_path = project.sysroot_path.parent / "orin-r35.3.1.manifest.json"
        self.sysroot_yaml_path = project.manifests_dir / "orin-r35.3.1.yaml"

    # -- absolute symlink fixing ------------------------------------------

    def find_absolute_symlinks(self) -> list[dict[str, str]]:
        """Find all absolute symlinks in the sysroot."""
        result: list[dict[str, str]] = []
        for root, _dirs, files in os.walk(self.sysroot_path):
            for name in files:
                full = Path(root, name)
                if full.is_symlink():
                    target = os.readlink(full)
                    if target.startswith("/"):
                        rel = str(full.relative_to(self.sysroot_path))
                        result.append({"path": rel, "target": target})
        # Also check symlinks that os.walk might list as dirs
        for root, dirs, _files in os.walk(self.sysroot_path):
            for name in dirs:
                full = Path(root, name)
                if full.is_symlink():
                    target = os.readlink(full)
                    if target.startswith("/"):
                        rel = str(full.relative_to(self.sysroot_path))
                        entry = {"path": rel, "target": target}
                        if entry not in result:
                            result.append(entry)
        return result

    def fix_absolute_symlinks(self, dry_run: bool = False) -> dict[str, Any]:
        """Rewrite absolute symlinks to be relative to the sysroot root."""
        abs_links = self.find_absolute_symlinks()
        fixed = 0
        skipped = 0
        details: list[dict[str, str]] = []

        for link in abs_links:
            link_path = self.sysroot_path / link["path"]
            abs_target = link["target"]

            # Compute the target path relative to sysroot root
            # e.g. /lib/aarch64-linux-gnu/libc.so.6 -> lib/aarch64-linux-gnu/libc.so.6
            target_in_sysroot = abs_target.lstrip("/")

            # Verify the target exists within the sysroot
            target_full = self.sysroot_path / target_in_sysroot
            if not target_full.exists() and not target_full.is_symlink():
                skipped += 1
                details.append({
                    "path": link["path"],
                    "old_target": abs_target,
                    "action": "skipped (target not in sysroot)",
                })
                continue

            # Compute relative path from the symlink's directory
            link_dir = link_path.parent
            rel_target = os.path.relpath(target_full, link_dir)

            if not dry_run:
                link_path.unlink()
                link_path.symlink_to(rel_target)

            fixed += 1
            details.append({
                "path": link["path"],
                "old_target": abs_target,
                "new_target": rel_target,
                "action": "fixed" if not dry_run else "would-fix",
            })

        return {
            "status": "success",
            "absolute_symlinks_found": len(abs_links),
            "fixed": fixed,
            "skipped": skipped,
            "dry_run": dry_run,
            "details": details[:20],  # limit output
        }

    # -- file manifest ----------------------------------------------------

    def generate_manifest(self) -> dict[str, Any]:
        """Generate a file manifest with structure digest."""
        files: list[dict[str, Any]] = []
        total_size = 0

        for root, _dirs, names in os.walk(self.sysroot_path):
            for name in names:
                full = Path(root, name)
                rel = str(full.relative_to(self.sysroot_path))
                if full.is_symlink():
                    target = os.readlink(full)
                    files.append({"path": rel, "type": "symlink", "target": target, "size": 0})
                elif full.is_file():
                    size = full.stat().st_size
                    total_size += size
                    files.append({"path": rel, "type": "file", "size": size})

        files.sort(key=lambda f: f["path"])

        # Structure digest: SHA-256 of the sorted file list with sizes
        hasher = hashlib.sha256()
        for f in files:
            hasher.update(f["path"].encode())
            hasher.update(str(f.get("size", 0)).encode())
            if f["type"] == "symlink":
                hasher.update(f["target"].encode())
        digest = hasher.hexdigest()

        manifest = {
            "sysroot_id": "orin-r35.3.1",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "file_count": len(files),
            "total_size": total_size,
            "structure_sha256": digest,
            "files": files,
        }

        # Save manifest
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return {
            "status": "success",
            "manifest_path": str(self.manifest_path),
            "file_count": len(files),
            "total_size": total_size,
            "structure_sha256": digest,
        }

    # -- verification -----------------------------------------------------

    def verify_sysroot(self) -> dict[str, Any]:
        """Verify architecture and key libraries in the sysroot."""
        checks: list[dict[str, Any]] = []

        # 1. Check key ELF files are aarch64
        key_libs = [
            "lib/aarch64-linux-gnu/libc.so.6",
            "lib/aarch64-linux-gnu/libstdc++.so.6",
            "usr/lib/aarch64-linux-gnu/libc.so.6",
            "usr/lib/aarch64-linux-gnu/libstdc++.so.6",
            "lib/ld-linux-aarch64.so.1",
        ]
        for lib_rel in key_libs:
            lib_path = self.sysroot_path / lib_rel
            # Resolve symlinks
            try:
                real = lib_path.resolve()
                if not real.exists():
                    # Try as direct file
                    if lib_path.exists():
                        real = lib_path
                    else:
                        checks.append({"check": f"exists:{lib_rel}", "status": "skip",
                                       "message": "not found (may be a symlink chain)"})
                        continue
                cls = classify_file(real)
                if cls.file_type == "elf" and cls.elf_info and cls.elf_info.is_elf:
                    if cls.elf_info.elf_machine == EM_AARCH64 and cls.elf_info.elf_class == _ELFCLASS64:
                        checks.append({"check": f"arch:{lib_rel}", "status": "pass",
                                       "message": f"aarch64/64-bit ELF"})
                    else:
                        checks.append({"check": f"arch:{lib_rel}", "status": "fail",
                                       "message": f"machine={cls.elf_info.machine_name}, "
                                                  f"class={cls.elf_info.elf_class}"})
                else:
                    checks.append({"check": f"arch:{lib_rel}", "status": "skip",
                                   "message": f"not an ELF file ({cls.file_type})"})
            except Exception as exc:
                checks.append({"check": f"arch:{lib_rel}", "status": "error",
                               "message": str(exc)})

        # 2. Check crt files exist
        crt_files = [
            "usr/lib/aarch64-linux-gnu/crt1.o",
            "usr/lib/aarch64-linux-gnu/crti.o",
            "usr/lib/aarch64-linux-gnu/crtn.o",
            "usr/lib/gcc/aarch64-linux-gnu/9/crtbegin.o",
            "usr/lib/gcc/aarch64-linux-gnu/9/crtend.o",
        ]
        for crt_rel in crt_files:
            crt_path = self.sysroot_path / crt_rel
            if crt_path.exists():
                checks.append({"check": f"crt:{crt_rel}", "status": "pass"})
            else:
                checks.append({"check": f"crt:{crt_rel}", "status": "fail",
                               "message": "not found"})

        # 3. Check headers
        headers = ["usr/include/stdio.h", "usr/include/stdlib.h"]
        for hdr_rel in headers:
            hdr_path = self.sysroot_path / hdr_rel
            if hdr_path.exists():
                checks.append({"check": f"header:{hdr_rel}", "status": "pass"})
            else:
                checks.append({"check": f"header:{hdr_rel}", "status": "fail",
                               "message": "not found"})

        # 4. Check absolute symlinks remaining
        abs_links = self.find_absolute_symlinks()
        checks.append({
            "check": "absolute-symlinks",
            "status": "pass" if len(abs_links) == 0 else "warn",
            "message": f"{len(abs_links)} absolute symlink(s) remaining",
        })

        failed = sum(1 for c in checks if c["status"] == "fail")
        return {
            "status": "failed" if failed > 0 else "success",
            "checks": checks,
            "failed_count": failed,
        }

    # -- full import ------------------------------------------------------

    def import_sysroot(self, dry_run: bool = False) -> dict[str, Any]:
        """Full sysroot import: fix symlinks, generate manifest, verify."""
        print("=== Sysroot import ===")

        # 1. Fix absolute symlinks
        print("  Fixing absolute symlinks...")
        fix_report = self.fix_absolute_symlinks(dry_run=dry_run)
        print(f"    Found: {fix_report['absolute_symlinks_found']}, "
              f"Fixed: {fix_report['fixed']}, Skipped: {fix_report['skipped']}")

        # 2. Generate file manifest
        if not dry_run:
            print("  Generating file manifest...")
            manifest_report = self.generate_manifest()
            print(f"    {manifest_report['file_count']} files, "
                  f"digest: {manifest_report['structure_sha256'][:16]}...")
        else:
            manifest_report = {"status": "skipped", "dry_run": True}

        # 3. Verify
        print("  Verifying sysroot...")
        verify_report = self.verify_sysroot()
        passed = sum(1 for c in verify_report["checks"] if c["status"] == "pass")
        failed = verify_report["failed_count"]
        print(f"    {passed} passed, {failed} failed")

        # 4. Update sysroot manifest YAML
        if not dry_run:
            self._update_sysroot_yaml(fix_report, manifest_report, verify_report)

        return {
            "status": "failed" if failed > 0 else "success",
            "fix_report": fix_report,
            "manifest_report": manifest_report,
            "verify_report": verify_report,
            "dry_run": dry_run,
        }

    def _update_sysroot_yaml(
        self,
        fix_report: dict,
        manifest_report: dict,
        verify_report: dict,
    ) -> None:
        """Update the sysroot manifest YAML with import results."""
        with open(self.sysroot_yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        sr = data.setdefault("sysroot", {})
        sr["digest"] = manifest_report.get("structure_sha256", "")
        sr["file_manifest"] = str(self.manifest_path.relative_to(self.project.root))
        sr["import_status"] = "verified" if verify_report["failed_count"] == 0 else "partial"
        sr["import_date"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        fixes = data.setdefault("fixes_applied", {})
        fixes["absolute_symlinks_rewritten"] = fix_report.get("fixed", 0)

        with open(self.sysroot_yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
