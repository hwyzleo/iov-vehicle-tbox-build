# TboxInstall.cmake - Standard install helpers for TBOX services
#
# Enforces the TBOX CMake接入契约:
#   * Use GNUInstallDirs for standard directories
#   * Install with named components (runtime vs development)
#   * Export targets with tbox:: namespace
#   * Support CMAKE_INSTALL_PREFIX and DESTDIR staging

include(GNUInstallDirs)

# tbox_install_target(<target>
#   [RUNTIME_COMPONENT <component>]   (default: <target>-runtime)
#   [DEVELOPMENT_COMPONENT <comp>]    (default: <target>-development)
#   [EXPORT <export-name>])
#
# Installs a target using standard GNUInstallDirs destinations.
# Executables and shared libraries go to RUNTIME_COMPONENT.
# Static libraries and export files go to DEVELOPMENT_COMPONENT.
function(tbox_install_target _target)
    set(_one_value RUNTIME_COMPONENT DEVELOPMENT_COMPONENT EXPORT)
    cmake_parse_arguments(_TBOX_INSTALL "" "${_one_value}" "" ${ARGN})

    if(NOT _TBOX_INSTALL_RUNTIME_COMPONENT)
        set(_TBOX_INSTALL_RUNTIME_COMPONENT "${_target}-runtime")
    endif()
    if(NOT _TBOX_INSTALL_DEVELOPMENT_COMPONENT)
        set(_TBOX_INSTALL_DEVELOPMENT_COMPONENT "${_target}-development")
    endif()

    install(TARGETS ${_target}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
            COMPONENT ${_TBOX_INSTALL_RUNTIME_COMPONENT}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
            COMPONENT ${_TBOX_INSTALL_RUNTIME_COMPONENT}
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
            COMPONENT ${_TBOX_INSTALL_DEVELOPMENT_COMPONENT}
    )

    if(_TBOX_INSTALL_EXPORT)
        install(EXPORT ${_TBOX_INSTALL_EXPORT}
            DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/${_TBOX_INSTALL_EXPORT}
            NAMESPACE tbox::
            FILE ${_TBOX_INSTALL_EXPORT}-targets.cmake
            COMPONENT ${_TBOX_INSTALL_DEVELOPMENT_COMPONENT}
        )
    endif()
endfunction()

# tbox_install_header(<file> <destination> [COMPONENT <comp>])
#
# Installs a public header file to include/tbox/<destination>.
function(tbox_install_header _file _dest)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_TBOX_HDR "" "${_one_value}" "" ${ARGN})
    if(NOT _TBOX_HDR_COMPONENT)
        set(_TBOX_HDR_COMPONENT "${PROJECT_NAME}-development")
    endif()
    install(FILES ${_file}
        DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/tbox/${_dest}
        COMPONENT ${_TBOX_HDR_COMPONENT}
    )
endfunction()

# tbox_install_systemd_unit(<unit-file> [COMPONENT <comp>])
#
# Installs a systemd unit file to the systemd system directory.
function(tbox_install_systemd_unit _unit_file)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_TBOX_UNIT "" "${_one_value}" "" ${ARGN})
    if(NOT _TBOX_UNIT_COMPONENT)
        set(_TBOX_UNIT_COMPONENT "${PROJECT_NAME}-runtime")
    endif()
    install(FILES ${_unit_file}
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/systemd/system
        COMPONENT ${_TBOX_UNIT_COMPONENT}
    )
endfunction()

# tbox_install_config(<config-file> [COMPONENT <comp>])
#
# Installs a default configuration file to etc/tbox/.
function(tbox_install_config _config_file)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_TBOX_CFG "" "${_one_value}" "" ${ARGN})
    if(NOT _TBOX_CFG_COMPONENT)
        set(_TBOX_CFG_COMPONENT "${PROJECT_NAME}-runtime")
    endif()
    install(FILES ${_config_file}
        DESTINATION ${CMAKE_INSTALL_SYSCONFDIR}/tbox
        COMPONENT ${_TBOX_CFG_COMPONENT}
    )
endfunction()
