#!/usr/bin/env bash
set -euo pipefail

workspace=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
"$workspace/manifest/development/mount-source.sh"
docker build --platform linux/amd64 -t arkui-build:23.2 \
  "$workspace/manifest/development"

terminal=(-i)
if [[ -t 0 && -t 1 ]]; then terminal=(-it); fi
if [[ $# -eq 0 ]]; then set -- bash; fi
docker run --rm "${terminal[@]}" --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp/arkui-home \
  --mount "type=bind,source=$workspace/source,target=/src" \
  --workdir /src arkui-build:23.2 \
  bash -c 'mkdir -p "$HOME"; exec "$@"' bash "$@"
