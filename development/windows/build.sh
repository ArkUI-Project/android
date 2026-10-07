#!/usr/bin/env bash
set -eo pipefail

cd "$ARKUI_SOURCE"
export USE_CCACHE=1
export CCACHE_EXEC=/usr/bin/ccache
export CCACHE_DIR="$HOME/.cache/arkui-ccache"
ccache -M "$ARKUI_CCACHE_SIZE"
ccache -o compression=true

source build/envsetup.sh
breakfast "$ARKUI_DEVICE" "$ARKUI_VARIANT"

if [[ ${1:-} == check ]]; then
  get_build_var TARGET_PRODUCT
  get_build_var TARGET_DEVICE
  get_build_var PLATFORM_SDK_VERSION
  exit 0
fi

if [[ ${1:-} == modules ]]; then
  shift
  m -j"$ARKUI_BUILD_JOBS" "$@"
  exit 0
fi

m -j"$ARKUI_BUILD_JOBS"
m -j"$ARKUI_BUILD_JOBS" emu_img_zip
printf '%s\n' "$ANDROID_PRODUCT_OUT" > "$ARKUI_PRODUCT_OUT_FILE"
