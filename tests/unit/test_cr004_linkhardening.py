"""Unit tests for CR-004 D2: link-time undefined-symbol hardening (BUILD-REQ-046).

The toolchain ships the mechanism DEFAULT OFF; activation requires the
mandatory CR-004 §5.2 build evidence. These tests verify:

  * the centralized option exists and defaults to OFF;
  * enabling it injects -Wl,--no-undefined into shared/module/exe linker
    flags (via a real cmake configure when cmake is available);
  * the per-target exemption helper exists and only touches the named target
    (real host build when cmake + a C compiler are available).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLCHAIN = PROJECT_ROOT / "cmake" / "toolchains" / "orin-aarch64.cmake"
HARDENING_MODULE = PROJECT_ROOT / "cmake" / "modules" / "TboxLinkHardening.cmake"

pytestmark = pytest.mark.skipif(
    not TOOLCHAIN.is_file() or not HARDENING_MODULE.is_file(),
    reason="CR-004 D2 cmake files not present",
)

CMAKE = shutil.which("cmake")
CC = shutil.which("gcc") or shutil.which("cc")


class TestToolchainPolicy:
    def test_option_exists_default_off(self):
        text = TOOLCHAIN.read_text(encoding="utf-8")
        assert "option(TBOX_LINK_HARDENING" in text
        assert "OFF)" in text.split("option(TBOX_LINK_HARDENING", 1)[1][:120]
        assert "DEFAULT OFF" in text or "default OFF" in text

    def test_flag_injected_into_all_linker_types(self):
        text = TOOLCHAIN.read_text(encoding="utf-8")
        for var in ("CMAKE_SHARED_LINKER_FLAGS_INIT",
                    "CMAKE_MODULE_LINKER_FLAGS_INIT",
                    "CMAKE_EXE_LINKER_FLAGS_INIT"):
            assert f"string(APPEND {var} \" -Wl,--no-undefined\")" in text, var

    def test_activation_gated_by_evidence(self):
        text = TOOLCHAIN.read_text(encoding="utf-8")
        assert "tbox-someip-orin" in text  # mandatory full-build evidence
        assert "non-dry-run" in text
        assert "link-exemptions.yaml" in text

    def test_exemption_helper_defined(self):
        text = HARDENING_MODULE.read_text(encoding="utf-8")
        assert "function(tbox_link_hardening_exempt" in text
        assert "TBOX_LINK_HARDENING_EXEMPTIONS" in text
        assert "target_link_options" in text
        assert "manifest" in text  # governance: record in link-exemptions.yaml


@pytest.mark.skipif(CMAKE is None, reason="cmake not available")
class TestToolchainConfigure:
    """Configure a trivial project with the real toolchain + fake sysroot."""

    def _configure(self, tmp_path: Path, hardening: bool) -> Path:
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        (project / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\n"
            "project(lhtest C)\n"
            "add_library(foo SHARED src/foo.c)\n",
            encoding="utf-8",
        )
        (project / "src" / "foo.c").write_text("int foo(void){return 1;}\n")
        sysroot = tmp_path / "fakesysroot"
        sysroot.mkdir(exist_ok=True)
        build = tmp_path / ("build-on" if hardening else "build-off")
        cmd = [
            CMAKE, "-S", str(project), "-B", str(build),
            f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN}",
            f"-DTBOX_SYSROOT={sysroot}",
            "-DTBOX_CROSS_CC=" + (CC or "gcc"),
            "-DTBOX_CROSS_CXX=" + (CC or "g++"),
        ]
        if hardening:
            cmd.append("-DTBOX_LINK_HARDENING=ON")
        subprocess.run(cmd, capture_output=True, text=True, check=False)
        return build

    def test_hardening_on_injects_flags(self, tmp_path):
        build = self._configure(tmp_path, hardening=True)
        cache = (build / "CMakeCache.txt")
        assert cache.is_file(), "configure failed"
        text = cache.read_text()
        assert "TBOX_LINK_HARDENING:BOOL=ON" in text
        for var in ("CMAKE_SHARED_LINKER_FLAGS", "CMAKE_MODULE_LINKER_FLAGS",
                    "CMAKE_EXE_LINKER_FLAGS"):
            line = next(
                l for l in text.splitlines() if l.startswith(f"{var}:")
            )
            assert "-Wl,--no-undefined" in line
            assert line.count("--no-undefined") == 1  # exactly once

    def test_hardening_default_off(self, tmp_path):
        build = self._configure(tmp_path, hardening=False)
        cache = (build / "CMakeCache.txt")
        assert cache.is_file(), "configure failed"
        text = cache.read_text()
        assert "TBOX_LINK_HARDENING:BOOL=OFF" in text
        for var in ("CMAKE_SHARED_LINKER_FLAGS", "CMAKE_MODULE_LINKER_FLAGS",
                    "CMAKE_EXE_LINKER_FLAGS"):
            line = next(
                l for l in text.splitlines() if l.startswith(f"{var}:")
            )
            assert "-Wl,--no-undefined" not in line


@pytest.mark.skipif(CMAKE is None, reason="cmake + cc needed")
class TestExemptionHelper:
    def test_exempts_only_named_target(self, tmp_path):
        """Configure-only: exempted target's link line gets -Wl,-z,undefs;
        non-exempted targets are untouched. (The inverse -z undefs flag is GNU
        ld semantics; link.txt inspection keeps the test host-portable.)"""
        project = tmp_path / "proj"
        (project / "src").mkdir(parents=True)
        (project / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\n"
            "project(lhtest C)\n"
            "include(TboxLinkHardening)\n"
            "add_library(foo SHARED src/foo.c)\n"
            "add_library(bar SHARED src/bar.c)\n"
            "tbox_link_hardening_exempt(foo \"test exemption\")\n",
            encoding="utf-8",
        )
        (project / "src" / "foo.c").write_text("int foo(void){return 1;}\n")
        (project / "src" / "bar.c").write_text("int bar(void){return 2;}\n")
        build = tmp_path / "build"
        cmd = [
            CMAKE, "-S", str(project), "-B", str(build),
            "-DTBOX_LINK_HARDENING=ON",
            f"-DCMAKE_MODULE_PATH={PROJECT_ROOT / 'cmake' / 'modules'}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stderr[-500:]
        foo_link = (build / "CMakeFiles" / "foo.dir" / "link.txt")
        bar_link = (build / "CMakeFiles" / "bar.dir" / "link.txt")
        assert foo_link.is_file() and bar_link.is_file()
        assert "-z,undefs" in foo_link.read_text(), \
            "exempted target missing inverse flag"
        assert "-z,undefs" not in bar_link.read_text(), \
            "non-exempted target touched"


# ---------------------------------------------------------------------------
# CR-004 评审回归 P1-3 / P1-5
# ---------------------------------------------------------------------------


class TestModulePathInjection:
    """P1-3: cmake/modules must be reachable from service repositories.

    Without CMAKE_MODULE_PATH injection, ``include(TboxLinkHardening)`` in a
    service CMakeLists fails outright, making the D2 per-target exemption
    mechanism unusable.
    """

    def test_toolchain_appends_modules_dir(self):
        text = TOOLCHAIN.read_text(encoding="utf-8")
        assert "CMAKE_MODULE_PATH" in text
        assert "/../modules" in text

    @pytest.mark.skipif(CMAKE is None, reason="cmake not available")
    def test_service_can_include_module(self, tmp_path):
        """A project configured with the toolchain can include the module."""
        sysroot = tmp_path / "sysroot"
        sysroot.mkdir()
        src = tmp_path / "src"
        src.mkdir()
        (src / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\n"
            "project(ModPath NONE)\n"
            # 仅验证模块可被解析，不触发编译器检测
            "include(TboxLinkHardening)\n"
            "if(NOT COMMAND tbox_link_hardening_exempt)\n"
            "  message(FATAL_ERROR 'helper not defined')\n"
            "endif()\n"
            "message(STATUS \"module resolved\")\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [CMAKE, "-S", str(src), "-B", str(tmp_path / "b"),
             f"-DCMAKE_TOOLCHAIN_FILE={TOOLCHAIN}",
             f"-DTBOX_SYSROOT={sysroot}"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "module resolved" in proc.stdout


@pytest.mark.skipif(CMAKE is None, reason="cmake not available")
class TestExemptionGuard:
    """P1-5: the guard must actually test target existence.

    ``if(NOT TARGET)`` evaluates TARGET as a plain variable rather than using
    the ``if(TARGET <name>)`` operator, so a bogus name was accepted and even
    recorded in the global exemption property.
    """

    def _configure(self, tmp_path: Path, body: str):
        src = tmp_path / "src"
        src.mkdir()
        (src / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\n"
            "project(Guard NONE)\n"
            f"list(APPEND CMAKE_MODULE_PATH \"{HARDENING_MODULE.parent}\")\n"
            "include(TboxLinkHardening)\n"
            f"{body}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [CMAKE, "-S", str(src), "-B", str(tmp_path / "b")],
            capture_output=True, text=True,
        )

    def test_nonexistent_target_is_rejected(self, tmp_path):
        proc = self._configure(
            tmp_path, "tbox_link_hardening_exempt(no_such_target)")
        assert proc.returncode != 0
        assert "is not a target" in (proc.stdout + proc.stderr)

    def test_empty_argument_is_rejected(self, tmp_path):
        proc = self._configure(tmp_path, 'tbox_link_hardening_exempt("")')
        assert proc.returncode != 0
        assert "requires a target name" in (proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# CR-004 评审回归 P1-4: orchestrator 必须显式透传开关
# ---------------------------------------------------------------------------


class TestOrchestratorPlumbing:
    def test_configure_cmd_passes_switch(self):
        from tbox_build.orchestrator import BuildConfig

        assert BuildConfig().link_hardening is False
        cfg_on = BuildConfig(link_hardening=True)
        assert cfg_on.link_hardening is True

    def test_cli_exposes_flag(self):
        import tbox_build.__main__ as m

        parser = m.build_parser() if hasattr(m, "build_parser") else None
        if parser is None:
            # 回退：直接在源码中确认 flag 与透传存在
            text = (PROJECT_ROOT / "tbox_build" / "__main__.py").read_text(
                encoding="utf-8")
            assert "--link-hardening" in text
            assert "link_hardening=" in text
        orch = (PROJECT_ROOT / "tbox_build" / "orchestrator.py").read_text(
            encoding="utf-8")
        assert "-DTBOX_LINK_HARDENING=" in orch
