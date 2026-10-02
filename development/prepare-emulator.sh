#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

mkdir -p "$ANDROID_AVD_HOME"
android --no-metrics --sdk "$ANDROID_HOME" sdk install \
  system-images/android-36/default/arm64-v8a
if [[ ! -f "$ANDROID_AVD_HOME/ArkUI_API_36_arm64.avd/config.ini" ]]; then
  printf 'no\n' | avdmanager create avd --name ArkUI_API_36_arm64 \
    --package 'system-images;android-36;default;arm64-v8a' \
    --device medium_phone
fi
