# examples/minimal/CMakeLists.txt
# Minimal aarch64 example for TBOX BUILD v0.1-alpha self-verification.
#
# Defines two targets matching manifests/services.yaml:
#   tbox-hello-lib  - shared library exporting greet()
#   tbox-hello-cli  - executable consuming tbox-hello-lib
#
# Both targets follow the TBOX CMake接入契约:
#   * No compiler or sysroot overrides (toolchain/preset injects them)
#   * GNUInstallDirs for standard directories
#   * Named install components matching service manifest
#   * DESTDIR staging support

cmake_minimum_required(VERSION 3.16)
project(tbox-hello VERSION 0.1.0 LANGUAGES C CXX)

# Locate TBOX CMake modules
if(DEFINED ENV{TBOX_ROOT})
    set(TBOX_ROOT "$ENV{TBOX_ROOT}")
elseif(DEFINED TBOX_ROOT)
    # cache variable from preset
else()
    get_filename_component(TBOX_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
endif()
list(APPEND CMAKE_MODULE_PATH "${TBOX_ROOT}/cmake/modules")

include(GNUInstallDirs)
include(TboxInstall)

# ---------------------------------------------------------------------------
# tbox-hello-lib: shared library
# ---------------------------------------------------------------------------
add_library(tbox-hello-lib SHARED
    src/greet.cpp
)
target_include_directories(tbox-hello-lib PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}/tbox>
)
set_target_properties(tbox-hello-lib PROPERTIES
    VERSION ${PROJECT_VERSION}
    SOVERSION 0
    POSITION_INDEPENDENT_CODE ON
)

# ---------------------------------------------------------------------------
# tbox-hello-cli: executable
# ---------------------------------------------------------------------------
add_executable(tbox-hello-cli
    src/hello_cli.cpp
)
target_link_libraries(tbox-hello-cli PRIVATE tbox-hello-lib)

# ---------------------------------------------------------------------------
# Install rules with components matching manifests/services.yaml
# ---------------------------------------------------------------------------
# Library: shared lib -> tbox-hello-lib-runtime
#          headers    -> tbox-hello-lib-development
tbox_install_target(tbox-hello-lib
    RUNTIME_COMPONENT tbox-hello-lib-runtime
    DEVELOPMENT_COMPONENT tbox-hello-lib-development
)
tbox_install_header(src/greet.hpp .
    COMPONENT tbox-hello-lib-development
)

# CLI: executable + systemd unit + config -> tbox-hello-cli-runtime
tbox_install_target(tbox-hello-cli
    RUNTIME_COMPONENT tbox-hello-cli-runtime
)
tbox_install_systemd_unit(systemd/tbox-hello.service
    COMPONENT tbox-hello-cli-runtime
)
tbox_install_config(config/hello.conf
    COMPONENT tbox-hello-cli-runtime
)
