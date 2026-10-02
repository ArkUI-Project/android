#!/usr/bin/env bash
# Source this file from the workspace shell.
if [[ -n "${ZSH_VERSION:-}" ]]; then
  arkui_env_file="${(%):-%x}"
else
  arkui_env_file="${BASH_SOURCE[0]}"
fi
arkui_workspace=$(cd "$(dirname "$arkui_env_file")/../.." && pwd)
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_AVD_HOME="$arkui_workspace/emulator/avd"
export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
unset arkui_workspace arkui_env_file
