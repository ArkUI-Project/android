#!/usr/bin/env bash
set -e
[[ $1 == -s && $2 == emulator-5554 ]]
shift 2
printf '%s\n' "$*" >> "$TEST_ADB_LOG"
case "$*" in
  'shell getprop sys.boot_completed') printf '1\n' ;;
  'shell cat /proc/sys/kernel/random/boot_id')
    if [[ -f $TEST_ADB_LOG.boot ]]; then
      printf 'new-boot\n'
    else
      printf 'first-boot\n'
    fi ;;
  reboot) [[ ${TEST_STUCK_BOOT:-} == 1 ]] || touch "$TEST_ADB_LOG.boot" ;;
  'shell getprop ro.lineage.version')
    if [[ ${TEST_WRONG_ROM:-} == 1 ]]; then
      printf 'wrong-rom\n'
    else
      printf '23.2-local-test\n'
    fi ;;
  'remount -R') printf 'Remount succeeded\nRebooting device\n'; exit 1 ;;
  'remount') [[ ${TEST_REMOUNT_FAIL:-} != 1 ]] ;;
  sync*)
    [[ $ANDROID_PRODUCT_OUT == "$ARKUI_SOURCE/out/target/product/emu64x" ]]
    if [[ ${TEST_SYNC_ERROR:-} == 1 ]]; then
      printf 'adb: error: failed to lstat fixture\n'
    else
      printf 'Sync completed\n'
    fi ;;
esac
