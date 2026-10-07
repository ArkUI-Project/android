# Loaded by an interactive WSL bash session.
if [[ -f "$HOME/.bashrc" ]]; then
  source "$HOME/.bashrc"
fi
export USE_CCACHE=1
export CCACHE_EXEC=/usr/bin/ccache
export CCACHE_DIR="$HOME/.cache/arkui-ccache"
export PATH="$HOME/.local/bin:$PATH"
cd "$ARKUI_SOURCE" || return
if [[ -f build/envsetup.sh && -f vendor/lineage/build/envsetup.sh ]]; then
  source build/envsetup.sh
  breakfast "$ARKUI_DEVICE" "$ARKUI_VARIANT"
else
  printf 'Source sync is incomplete. Run arkui.ps1 sync from PowerShell.\n'
fi
