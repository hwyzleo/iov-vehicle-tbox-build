# TboxBuildInfo.cmake - Build info generation for TBOX services
#
# Generates a build info header with git commit, build profile,
# and timestamp for artifact traceability.

# tbox_generate_build_info(<output-dir>)
#
# Generates tbox_build_info.h in <output-dir> with:
#   TBOX_BUILD_GIT_COMMIT  - current git commit hash
#   TBOX_BUILD_GIT_DIRTY   - 1 if working tree has uncommitted changes
#   TBOX_BUILD_PROFILE     - build profile (Debug/Release)
#   TBOX_BUILD_TIMESTAMP   - UTC build timestamp
function(tbox_generate_build_info _output_dir)
    # Git commit
    execute_process(
        COMMAND git rev-parse HEAD
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
        OUTPUT_VARIABLE _tbox_git_commit
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(NOT _tbox_git_commit)
        set(_tbox_git_commit "unknown")
    endif()

    # Check for dirty working tree
    execute_process(
        COMMAND git status --porcelain
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
        OUTPUT_VARIABLE _tbox_git_status
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(_tbox_git_status)
        set(_tbox_git_dirty 1)
    else()
        set(_tbox_git_dirty 0)
    endif()

    string(TIMESTAMP _tbox_build_ts UTC)

    set(_tbox_build_info_file "${_output_dir}/tbox_build_info.h")
    configure_file(
        "${CMAKE_CURRENT_LIST_DIR}/tbox_build_info.h.in"
        "${_tbox_build_info_file}"
        @ONLY
    )
endfunction()
