# ci/Dockerfile - TBOX Build container for NVIDIA Orin cross-compilation
#
# Provides a reproducible build environment with GCC 9, CMake 3.16,
# Ninja 1.10 and the Orin sysroot.  Used for both CI and local
# development validation on Apple Silicon Mac.
#
# Build:
#   docker build -t tbox-build:0.1-alpha -f ci/Dockerfile .
#
# Run:
#   docker run --rm -v "$PWD":/workspace -v "$PWD/sysroots":/workspace/sysroots \
#     tbox-build:0.1-alpha ./scripts/build.sh --set tbox-orin-minimal

# Base image: Ubuntu 20.04.6 Focal, linux/arm64
# Digest verified on Apple Silicon Mac per platform manifest.
FROM docker.m.daocloud.io/library/ubuntu:20.04@sha256:722ea796ac2d57eeb3627c58a582fc1acc58be51faf815e1bce1682ae5c092f7

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-9 \
    g++-9 \
    cmake \
    ninja-build \
    python3 \
    python3-pip \
    python3-yaml \
    python3-jsonschema \
    python3-setuptools \
    pkg-config \
    binutils \
    make \
    file \
    openssh-client \
    rsync \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set gcc-9/g++-9 as default compilers
RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-9 100 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-9 100 && \
    update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-9 100 && \
    update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-9 100

# ---------------------------------------------------------------------------
# HOST protoc (Protocol Buffers compiler) — 受控输入
#
# TSP / SOMEIP 都用 find_program(PROTOC_EXECUTABLE NAMES protoc REQUIRED) 从
# 宿主 PATH 取 protoc（toolchain 设 CMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER），
# protoc 只生成构建中间文件、不进入 runtime package —— 与 CommonAPI generator
# 的 HOST 定位一致，故不登记在 dependencies/lock.yaml。
#
# 不用 apt 的 protobuf-compiler：focal 只有 3.6.1.3，无法编译两个服务 proto3
# 里的 field presence（optional，需 protoc >= 3.15）。
#
# 版本必须与 dependencies/lock.yaml 的 TARGET protobuf 一致（3.21.12）：
# 生成代码带 #if PROTOBUF_VERSION < 3021000 的版本门禁。
# ---------------------------------------------------------------------------
ARG PROTOC_VERSION=21.12
ARG PROTOC_SHA256=2dd17f75d66a682640b136e31848da9fb2eefe68d55303baf8b32617374f6711
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip \
    && curl -fsSL -o /tmp/protoc.zip \
        "https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/protoc-${PROTOC_VERSION}-linux-aarch_64.zip" \
    && echo "${PROTOC_SHA256}  /tmp/protoc.zip" | sha256sum -c - \
    && unzip -q /tmp/protoc.zip -d /opt/protoc \
    && ln -sf /opt/protoc/bin/protoc /usr/local/bin/protoc \
    && rm -f /tmp/protoc.zip \
    && apt-get purge -y unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && protoc --version

# Record installed package versions for manifest
RUN dpkg -l > /tmp/installed-packages.txt && \
    gcc-9 --version | head -1 > /tmp/toolchain-version.txt && \
    cmake --version | head -1 >> /tmp/toolchain-version.txt && \
    ninja --version >> /tmp/toolchain-version.txt && \
    protoc --version >> /tmp/toolchain-version.txt

# Set up workspace
ENV TBOX_ROOT=/workspace
WORKDIR /workspace

# Default: run build
CMD ["bash"]
