# TboxLinkHardening.cmake - per-target link-hardening exemption helper (CR-004 D2 §5.4)
#
# TBOX_LINK_HARDENING applies -Wl,--no-undefined globally (see
# cmake/toolchains/orin-aarch64.cmake). This helper exempts a NAMED target only:
#
#   include(TboxLinkHardening)
#   tbox_link_hardening_exempt(tbox_prov "yaml-cpp unresolved data symbols; see CR-004")
#
# Behaviour:
#   * When TBOX_LINK_HARDENING is ON, appends -Wl,-z,undefs (the explicit
#     inverse of -z,defs) to that target's PRIVATE LINK_OPTIONS, which appear
#     after the global flags and therefore cancel --no-undefined for this
#     target only.
#   * Records the exemption in the global property
#     TBOX_LINK_HARDENING_EXEMPTIONS so a build report can enumerate them.
#
# Governance (CR-004 §5.4 / BUILD-REQ-046): exemptions MUST also be recorded
# in manifests/link-exemptions.yaml with target, owner, reason, symbol_class,
# risk, scope and removal_condition. Repository-wide silent overrides are
# forbidden; only named targets may call this helper.

function(tbox_link_hardening_exempt _tbox_lh_target)
    # 注意：参数名不可叫 TARGET —— `if(TARGET)` 会被当成普通变量取值，
    # 而不是 `if(TARGET <name>)` 目标存在性操作符，守卫会完全失效
    # （CR-004 评审 P1-5：传入不存在的目标时既不报错，还会被记入全局属性）。
    if(NOT _tbox_lh_target)
        message(FATAL_ERROR "tbox_link_hardening_exempt requires a target name")
    endif()
    if(NOT TARGET "${_tbox_lh_target}")
        message(FATAL_ERROR
            "tbox_link_hardening_exempt: '${_tbox_lh_target}' is not a target "
            "defined in this project; exemptions may only name real targets")
    endif()
    set_property(GLOBAL APPEND PROPERTY
        TBOX_LINK_HARDENING_EXEMPTIONS "${_tbox_lh_target}")
    if(TBOX_LINK_HARDENING)
        if(CMAKE_VERSION VERSION_LESS 3.13)
            message(FATAL_ERROR
                "tbox_link_hardening_exempt needs CMake >= 3.13 (target_link_options)")
        endif()
        # -z undefs is the explicit inverse of -z defs / --no-undefined; on the
        # target's own LINK_OPTIONS it restores the default permissive link for
        # this target without touching any other target.
        target_link_options(${_tbox_lh_target} PRIVATE "-Wl,-z,undefs")
        message(STATUS "TBOX link hardening: exempted target ${_tbox_lh_target} "
            "(reason recorded in manifests/link-exemptions.yaml)")
    endif()
endfunction()
