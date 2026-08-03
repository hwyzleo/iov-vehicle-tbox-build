# orin-aarch64.cmake - TBOX Build toolchain for NVIDIA Orin (Linux aarch64)
#
# Loaded by CMake when configuring with an Orin preset or when the
# orchestrator passes -DCMAKE_TOOLCHAIN_FILE=<this file>.
#
# Projects MUST NOT set compilers or sysroot in their own CMakeLists.txt.
# All target-environment decisions are made here and in the platform manifest.
#
# Required variables (set via -D or environment):
#   TBOX_SYSROOT       - Path to the orin-r35.3.1 sysroot
#   TBOX_DEP_STAGING   - (optional) Path to target dependency staging prefix
#
# When building inside the linux/arm64 container the compiler (gcc-9/g++-9)
# is a native aarch64 compiler; the sysroot ensures linking against the
# exact Orin rootfs libraries rather than the container's own.

# --- Target system identification ---
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

# --- Compilers ---
# In the linux/arm64 build container these are native compilers.
# For x86_64-host cross-compilation, set TBOX_CROSS_CC / TBOX_CROSS_CXX.
if(DEFINED ENV{TBOX_CROSS_CC})
    set(CMAKE_C_COMPILER "$ENV{TBOX_CROSS_CC}")
elseif(DEFINED TBOX_CROSS_CC)
    set(CMAKE_C_COMPILER "${TBOX_CROSS_CC}")
else()
    set(CMAKE_C_COMPILER gcc-9)
endif()

if(DEFINED ENV{TBOX_CROSS_CXX})
    set(CMAKE_CXX_COMPILER "$ENV{TBOX_CROSS_CXX}")
elseif(DEFINED TBOX_CROSS_CXX)
    set(CMAKE_CXX_COMPILER "${TBOX_CROSS_CXX}")
else()
    set(CMAKE_CXX_COMPILER g++-9)
endif()

# --- Sysroot ---
if(DEFINED ENV{TBOX_SYSROOT})
    set(_tbox_sysroot "$ENV{TBOX_SYSROOT}")
elseif(DEFINED TBOX_SYSROOT)
    set(_tbox_sysroot "${TBOX_SYSROOT}")
else()
    message(FATAL_ERROR
        "TBOX_SYSROOT is not set. Pass -DTBOX_SYSROOT=<path> or set the "
        "TBOX_SYSROOT environment variable to the orin-r35.3.1 sysroot path.")
endif()

if(NOT IS_DIRECTORY "${_tbox_sysroot}")
    message(FATAL_ERROR "TBOX_SYSROOT does not exist or is not a directory: ${_tbox_sysroot}")
endif()

set(CMAKE_SYSROOT "${_tbox_sysroot}")

# --- Find root path ---
# Search order: sysroot first, then target dependency staging.
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
if(DEFINED ENV{TBOX_DEP_STAGING})
    list(APPEND CMAKE_FIND_ROOT_PATH "$ENV{TBOX_DEP_STAGING}")
elseif(DEFINED TBOX_DEP_STAGING)
    list(APPEND CMAKE_FIND_ROOT_PATH "${TBOX_DEP_STAGING}")
endif()

# --- Find mode ---
# Programs (generators, code generators): search HOST paths only.
# Libraries, headers, packages: search TARGET (sysroot/staging) only.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# --- C/C++ standards ---
set(CMAKE_C_STANDARD 11 CACHE STRING "C standard")
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard")
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

# --- ABI: use GCC 9 default libstdc++ C++11 ABI ---
# Projects MUST NOT override _GLIBCXX_USE_CXX11_ABI.

# --- Security and diagnostic compile flags (defaults) ---
set(TBOX_WARN_FLAGS "-Wall -Wextra -Wformat -Wformat-security")
set(TBOX_SECURITY_FLAGS "-fstack-protector-strong -D_FORTIFY_SOURCE=2")
set(TBOX_ORIN_C_FLAGS_INIT "${TBOX_WARN_FLAGS} ${TBOX_SECURITY_FLAGS}")
set(TBOX_ORIN_CXX_FLAGS_INIT "${TBOX_WARN_FLAGS} ${TBOX_SECURITY_FLAGS}")

# Only set _INIT if not already defined by the caller/preset
if(NOT DEFINED CMAKE_C_FLAGS_INIT)
    set(CMAKE_C_FLAGS_INIT "${TBOX_ORIN_C_FLAGS_INIT}")
endif()
if(NOT DEFINED CMAKE_CXX_FLAGS_INIT)
    set(CMAKE_CXX_FLAGS_INIT "${TBOX_ORIN_CXX_FLAGS_INIT}")
endif()

# --- RPATH policy ---
# Build-tree RPATH is allowed during development but MUST be stripped
# from release artifacts. The orchestrator runs elfcheck to enforce this.
# Release: no RPATH in final artifact (use default linker search path).
# Origin-relative RPATH ($ORIGIN) is allowed for libraries.
if(NOT DEFINED CMAKE_INSTALL_RPATH)
    set(CMAKE_INSTALL_RPATH "")
endif()
if(NOT DEFINED CMAKE_BUILD_RPATH_USE_ORIGIN)
    set(CMAKE_BUILD_RPATH_USE_ORIGIN TRUE)
endif()

# --- Diagnostic message (visible in configure output) ---
message(STATUS "TBOX Orin toolchain:")
message(STATUS "  C compiler:       ${CMAKE_C_COMPILER}")
message(STATUS "  C++ compiler:     ${CMAKE_CXX_COMPILER}")
message(STATUS "  Sysroot:          ${CMAKE_SYSROOT}")
message(STATUS "  Find root path:   ${CMAKE_FIND_ROOT_PATH}")
message(STATUS "  C standard:       ${CMAKE_C_STANDARD}")
message(STATUS "  C++ standard:     ${CMAKE_CXX_STANDARD}")
