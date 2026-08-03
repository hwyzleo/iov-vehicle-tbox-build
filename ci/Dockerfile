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

# Record installed package versions for manifest
RUN dpkg -l > /tmp/installed-packages.txt && \
    gcc-9 --version | head -1 > /tmp/toolchain-version.txt && \
    cmake --version | head -1 >> /tmp/toolchain-version.txt && \
    ninja --version >> /tmp/toolchain-version.txt

# Set up workspace
ENV TBOX_ROOT=/workspace
WORKDIR /workspace

# Default: run build
CMD ["bash"]
