#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) != 0 || $(uname -m) != x86_64 ]]; then
  printf 'Run through arkui.ps1 setup on x86_64 WSL Ubuntu.\n' >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  bc bison build-essential ca-certificates ccache curl erofs-utils flex \
  fontconfig g++-multilib gcc-multilib git git-lfs gnupg gperf imagemagick \
  lib32readline-dev lib32z1-dev libc6-dev-i386 libdw-dev libelf-dev \
  libgl1-mesa-dev libgnutls28-dev libncurses-dev libsdl2-dev libssl-dev \
  libx11-dev libxml2-dev libxml2-utils lz4 lzop ninja-build pngcrush \
  protobuf-compiler python3 python3-protobuf python-is-python3 ripgrep \
  rsync schedtool squashfs-tools unzip xsltproc xxd zip zlib1g-dev
