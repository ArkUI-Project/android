#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  printf 'Run this script on an x86_64 Linux build workstation.\n' >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y git git-lfs repo gnupg flex bison build-essential \
  zip curl zlib1g-dev libc6-dev-i386 x11proto-core-dev libx11-dev \
  lib32z1-dev libgl1-mesa-dev libxml2-utils xsltproc unzip fontconfig \
  python3 python-is-python3 bc rsync libssl-dev libncurses-dev lz4
git lfs install --skip-repo

printf 'Linux source and build dependencies installed.\n'
