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
# Search order (CR-002): downstream SDK staging -> TARGET dependency staging
# -> sysroot. Each is only prepended when the variable is defined AND
# non-empty, to avoid an empty value collapsing to "/usr" (host pollution).
#
# IMPORTANT (CMake 3.16 + CMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY):
# A find-root entry must be the DESTDIR *root* that CONTAINS /usr — i.e.
# ${TBOX_*_STAGING}, NOT ${TBOX_*_STAGING}/usr. Recipes install with
# DESTDIR=${TBOX_DEP_STAGING} and CMAKE_INSTALL_PREFIX=/usr, so package
# configs land at ${TBOX_DEP_STAGING}/usr/lib/cmake/<pkg>/. Under ONLY mode,
# CMake re-roots the /usr system prefix (and the absolute CMAKE_PREFIX_PATH
# entry below, which sits under this root) onto each find-root. With the root
# set to the DESTDIR, /usr re-roots to ${TBOX_DEP_STAGING}/usr and configs are
# found; if the root were ${TBOX_DEP_STAGING}/usr, CMake 3.16 would search
# ${TBOX_DEP_STAGING}/usr/usr/... and find_package(<pkg> CONFIG) would fail
# (this is exactly the vsomeip/CommonAPI cross-compile discovery failure).
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
if(DEFINED ENV{TBOX_SDK_STAGING} AND NOT "$ENV{TBOX_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "$ENV{TBOX_SDK_STAGING}")
elseif(DEFINED TBOX_SDK_STAGING AND NOT "${TBOX_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "${TBOX_SDK_STAGING}")
endif()
if(DEFINED ENV{TBOX_DEP_STAGING} AND NOT "$ENV{TBOX_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "$ENV{TBOX_DEP_STAGING}")
elseif(DEFINED TBOX_DEP_STAGING AND NOT "${TBOX_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "${TBOX_DEP_STAGING}")
endif()

# CMAKE_PREFIX_PATH mirrors the find-root order so find_package(CONFIG)
# resolves to the same staging roots.
set(CMAKE_PREFIX_PATH "")
if(DEFINED ENV{TBOX_SDK_STAGING} AND NOT "$ENV{TBOX_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "$ENV{TBOX_SDK_STAGING}/usr")
elseif(DEFINED TBOX_SDK_STAGING AND NOT "${TBOX_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "${TBOX_SDK_STAGING}/usr")
endif()
if(DEFINED ENV{TBOX_DEP_STAGING} AND NOT "$ENV{TBOX_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "$ENV{TBOX_DEP_STAGING}/usr")
elseif(DEFINED TBOX_DEP_STAGING AND NOT "${TBOX_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "${TBOX_DEP_STAGING}/usr")
endif()

# --- Multi-SDK staging （TBOX-MQTT-DSN-CR-011 §6.1: 多级 SDk roots） ---
# TBOX_SDK_STAGING_DIRS 是一个 ':' 分隔的路径列表（unix），用于注入多个
# 上游 service dependency SDK。遍历每个 <dir>/usr，按声明顺序加入查找路径。
# 设计 ordem：下游优先（SEC→PROV→framework），最先声明的最先搜索。
if(DEFINED ENV{TBOX_SDK_STAGING_DIRS} AND NOT "$ENV{TBOX_SDK_STAGING_DIRS}" STREQUAL "")
    string(REPLACE ":" ";" _tbox_sdk_dirs "$ENV{TBOX_SDK_STAGING_DIRS}")
elseif(DEFINED TBOX_SDK_STAGING_DIRS AND NOT "${TBOX_SDK_STAGING_DIRS}" STREQUAL "")
    set(_tbox_sdk_dirs "${TBOX_SDK_STAGING_DIRS}")
else()
    set(_tbox_sdk_dirs "")
endif()
foreach(_tbox_sdk_root IN LISTS _tbox_sdk_dirs)
    if(IS_DIRECTORY "${_tbox_sdk_root}/usr")
        # find-root = DESTDIR root (contains /usr); prefix-path = the /usr prefix
        # (sits under the root, so it is searched as-is under PACKAGE=ONLY).
        list(PREPEND CMAKE_FIND_ROOT_PATH "${_tbox_sdk_root}")
        list(PREPEND CMAKE_PREFIX_PATH "${_tbox_sdk_root}/usr")
    endif()
endforeach()
unset(_tbox_sdk_dirs)
unset(_tbox_sdk_root)

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
message(STATUS "  Prefix path:      ${CMAKE_PREFIX_PATH}")
message(STATUS "  C standard:       ${CMAKE_C_STANDARD}")
message(STATUS "  C++ standard:     ${CMAKE_CXX_STANDARD}")
