# TboxDependency.cmake - Dependency lookup helpers for TBOX services
#
# Enforces that target dependencies are found only in the controlled
# staging roots (downstream SDK staging -> TARGET dependency staging ->
# sysroot), never in host paths. The toolchain (orin-aarch64.cmake)
# populates CMAKE_FIND_ROOT_PATH and CMAKE_PREFIX_PATH in that order and
# sets the find modes to ONLY for library/include/package.
#
# Required environment (set by the orchestrator):
#   TBOX_DEP_STAGING  - TARGET dependency staging prefix (always set for orin)
#   TBOX_SDK_STAGING  - upstream service SDK prefix (empty when building a
#                       leaf library such as framework)

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
            "staging prefix, or declared in dependencies/lock.yaml. "
            "CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}")
    endif()
endmacro()

# tbox_assert_no_host_dependency()
#
# Sanity check: ensure no target library/include path resolves to the
# container /usr, /usr/local or other host-only locations. Call after all
# find_package/find_library calls in a service CMakeLists.
function(tbox_assert_no_host_dependency)
    foreach(_lib ${ARGN})
        get_target_property(_loc ${_lib} IMPORTED_LOCATION)
        if(_loc)
            if(_loc MATCHES "^/usr/local" OR _loc MATCHES "^/usr/lib(?!/cmake)")
                # /usr under the sysroot/staging is legitimate (logical prefix);
                # only flag paths that are NOT under a TBOX staging root.
                if(NOT _loc MATCHES "TBOX_DEP_STAGING"
                   AND NOT _loc MATCHES "TBOX_SDK_STAGING"
                   AND NOT _loc MATCHES "orin-r35.3.1")
                    message(WARNING
                        "Target dependency '${_lib}' resolves to a potential "
                        "host path: ${_loc}")
                endif()
            endif()
        endif()
    endforeach()
endfunction()

# tbox_find_library(<var> <library>)
#
# Finds a library in the target sysroot/staging only.
macro(tbox_find_library _var _library)
    find_library(${_var} ${_library}
        REQUIRED
        NO_DEFAULT_PATH
        HINTS ${CMAKE_FIND_ROOT_PATH}
        PATH_SUFFIXES lib aarch64-linux-gnu lib/aarch64-linux-gnu
    )
endmacro()
