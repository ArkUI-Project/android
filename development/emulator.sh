#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
exec emulator -avd ArkUI_API_36_arm64 -memory 2048 \
  -no-snapshot-load -writable-system "$@"
