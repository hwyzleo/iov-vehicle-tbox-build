# TBOX Build - Installed-package consumer (CR-002 §10.4 verification scaffold)
#
# This is a standalone CMake project that consumes the framework SDK staging
# ONLY via find_package(TboxFramework CONFIG REQUIRED) and the TBoxFramework::
# component targets. It MUST NOT use add_subdirectory(framework) or reference
# framework source/build trees.
#
# Status: SCAFFOLD. Compilation is gated on FW CR-010 landing the per-component
# TBoxFramework:: exports (config/store/log/ipc/hash/application). Until then
# this project demonstrates the BUILD-side consumption contract and is exercised
# once the framework SDK staging is populated.
#
# BUILD injects:
#   TBOX_SDK_STAGING = <BUILD_ROOT>/out/orin/release/sdk/framework
#   TBOX_DEP_STAGING = <BUILD_ROOT>/out/orin/release/deps
# The orin-aarch64.cmake toolchain prepends both to CMAKE_PREFIX_PATH /
# CMAKE_FIND_ROOT_PATH, so find_package resolves to the staging prefixes only.

cmake_minimum_required(VERSION 3.16)
project(tbox_framework_consumer LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Framework SDK package (headers + static libs + CMake package config).
find_package(TboxFramework CONFIG REQUIRED)

# yaml-cpp is consumed transitively; if TboxFrameworkConfig.cmake declares
# find_dependency(yaml-cpp), it resolves through the same TBOX_DEP_STAGING.
# The consumer does NOT find_package(yaml-cpp) directly.

add_executable(tbox_framework_consumer main.cpp)

# Link the public component targets exported by FW CR-010.
target_link_libraries(tbox_framework_consumer PRIVATE
  TBoxFramework::config
  TBoxFramework::store
  TBoxFramework::log
  TBoxFramework::ipc
)

# Install the consumer binary into the rootfs staging for ELF/NEEDED checks.
install(TARGETS tbox_framework_consumer
  RUNTIME DESTINATION bin
          COMPONENT consumer-runtime
)
