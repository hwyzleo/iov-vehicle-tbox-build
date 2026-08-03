# TboxDependency.cmake - Dependency lookup helpers for TBOX services
#
# Enforces that target dependencies are found only in the sysroot or
# target dependency staging, never in host paths.

# tbox_find_package(<package> [REQUIRED] [COMPONENTS ...])
#
# Wrapper around find_package that respects the toolchain's FIND_ROOT_PATH
# settings. Fails with a clear message if the package is not found in the
# target sysroot/staging.
macro(tbox_find_package _package)
    find_package(${_package} ${ARGN})
    if(NOT ${_package}_FOUND AND NOT ${_package}_FOUND STREQUAL "")
        message(FATAL_ERROR
            "TBOX dependency '${_package}' not found in target sysroot or "
            "dependency staging. Ensure it is built and installed to the "
            "staging prefix, or declared in dependencies/lock.yaml.")
    endif()
endmacro()

# tbox_find_library(<var> <library>)
#
# Finds a library in the target sysroot only.
macro(tbox_find_library _var _library)
    find_library(${_var} ${_library}
        REQUIRED
        NO_DEFAULT_PATH
        HINTS ${CMAKE_FIND_ROOT_PATH}
        PATH_SUFFIXES lib aarch64-linux-gnu lib/aarch64-linux-gnu
    )
endmacro()
