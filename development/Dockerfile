FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    git git-lfs gnupg ca-certificates curl flex bison build-essential zip \
    zlib1g-dev libc6-dev-i386 x11proto-core-dev libx11-dev lib32z1-dev \
    libgl1-mesa-dev libxml2-utils xsltproc unzip fontconfig python3 \
    python-is-python3 bc rsync libssl-dev libncurses-dev libtinfo6 lz4 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
CMD ["bash"]
