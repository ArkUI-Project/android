#!/usr/bin/env bash
set -euo pipefail

workspace=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
"$workspace/manifest/development/mount-source.sh"
docker build --platform linux/amd64 -t arkui-build:23.2 \
  --build-arg ARKUI_UID="$(id -u)" --build-arg ARKUI_GID="$(id -g)" \
  "$workspace/manifest/development"

terminal=(-i)
if [[ -t 0 && -t 1 ]]; then terminal=(-it); fi
if [[ $# -eq 0 ]]; then set -- bash; fi
docker run --rm "${terminal[@]}" --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  --env HOME=/home/arkui \
  --mount "type=bind,source=$workspace/source,target=/src" \
  --workdir /src arkui-build:23.2 "$@"
