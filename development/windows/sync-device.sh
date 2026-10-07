#!/usr/bin/env bash
set -eo pipefail

usage() {
  printf '%s\n' 'Usage: ./sync-device.sh [--dry-run] [all|system|system_ext|product|vendor|odm|oem|data]'
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  usage
  exit 0
fi

if [[ -z ${ARKUI_ADB:-} ]]; then
  script_dir=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
  exec python3 "$script_dir/arkui.py" --linux-worker sync-device "$@"
fi

sync_only=false
dry_run=false
partition=all
if [[ ${1:-} == --adb-sync ]]; then
  sync_only=true
  shift
else
  if [[ ${1:-} == --dry-run ]]; then
    dry_run=true
    shift
  fi
  if (( $# > 1 )); then
    usage >&2
    exit 2
  fi
  partition=${1:-all}
  case "$partition" in
    all|system|system_ext|product|vendor|odm|oem|data) ;;
    *) usage >&2; exit 2 ;;
  esac
fi

cd "$ARKUI_SOURCE"
exec 9>.repo/arkui-device-sync.lock
if ! flock -n 9; then
  printf '%s\n' 'Another device synchronization is running.' >&2
  exit 1
fi

source build/envsetup.sh
breakfast "$ARKUI_DEVICE" "$ARKUI_VARIANT"
export ANDROID_PRODUCT_OUT
ANDROID_PRODUCT_OUT=$(get_abs_build_var PRODUCT_OUT)
if [[ ! -d $ANDROID_PRODUCT_OUT/system || $ANDROID_PRODUCT_OUT != "$ARKUI_SOURCE"/out/* ]]; then
  printf 'Invalid or missing product output: %s\n' "$ANDROID_PRODUCT_OUT" >&2
  exit 1
fi
printf 'Native ADB product output: %s\nDevice: %s\n' "$ANDROID_PRODUCT_OUT" "$ANDROID_SERIAL"

adb() {
  timeout "${ARKUI_BOOT_TIMEOUT}s" "$ARKUI_ADB" -s "$ANDROID_SERIAL" "$@"
}

wait_boot() {
  local previous_boot=${1:-}
  local deadline=$((SECONDS + ARKUI_BOOT_TIMEOUT))
  adb wait-for-device
  while (( SECONDS < deadline )); do
    if [[ $(timeout 10s "$ARKUI_ADB" -s "$ANDROID_SERIAL" shell getprop sys.boot_completed 2>/dev/null || true) == 1 ]]; then
      if [[ -z $previous_boot ]]; then
        return
      fi
      local boot
      boot=$(timeout 10s "$ARKUI_ADB" -s "$ANDROID_SERIAL" shell cat /proc/sys/kernel/random/boot_id 2>/dev/null | tr -d '\r' || true)
      if [[ -n $boot && $boot != "$previous_boot" ]]; then
        return
      fi
    fi
    sleep 2
  done
  printf '%s\n' 'Device boot timed out. Keep the VM running and inspect logcat.' >&2
  return 1
}

wait_boot
version=$(adb shell getprop ro.lineage.version | tr -d '\r')
if [[ $version != "$ARKUI_EXPECTED_VERSION" ]]; then
  printf 'Wrong ROM on %s: %s (expected %s). No files transferred.\n' \
    "$ANDROID_SERIAL" "$version" "$ARKUI_EXPECTED_VERSION" >&2
  exit 1
fi

if ! $sync_only; then
  adb root
  adb wait-for-device
  # remount -R can return 1 when it succeeds but schedules a verity/overlay reboot.
  if ! adb remount -R; then
    printf '%s\n' 'Initial remount requested a reboot or failed; checking again after device recovery.'
  fi
  sleep 2
  wait_boot
  adb root
  adb wait-for-device
  adb remount
  if $dry_run; then
    set -- -l "$partition"
  else
    set -- "$partition"
  fi
fi

sync_output=$(mktemp)
trap 'rm -f -- "$sync_output"' EXIT
set +e
adb sync "$@" 2>&1 | tee "$sync_output"
sync_status=${PIPESTATUS[0]}
set -e
if (( sync_status != 0 )) || grep -Eq '^adb: error:|^error:' "$sync_output"; then
  printf '%s\n' 'ADB sync failed. No success or final reboot was recorded.' >&2
  exit 1
fi

if ! $sync_only; then
  if $dry_run; then
    printf '%s\n' 'Dry run complete. No files transferred.'
  else
    previous_boot=$(adb shell cat /proc/sys/kernel/random/boot_id | tr -d '\r')
    if [[ -z $previous_boot ]]; then
      printf '%s\n' 'Cannot read boot ID; refusing to report an unverified reboot.' >&2
      exit 1
    fi
    adb reboot
    wait_boot "$previous_boot"
    printf 'Device sync complete; boot verified: %s\n' "$(adb shell getprop ro.lineage.version | tr -d '\r')"
  fi
fi
