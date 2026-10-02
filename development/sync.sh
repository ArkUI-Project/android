#!/usr/bin/env bash
set -euo pipefail

workspace=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
"$workspace/manifest/development/mount-source.sh"
mkdir -p "$workspace/logs"
cd "$workspace/source"

if [[ ! -d .repo ]]; then
  repo init -u https://github.com/ArkUI-Project/android.git \
    -b lineage-23.2 --git-lfs --partial-clone --clone-filter=blob:none \
    --no-clone-bundle --platform=linux
fi

repo sync -c -j"${ARKUI_SYNC_JOBS:-4}" --no-tags "$@" 2>&1 | tee -a "$workspace/logs/sync.log"

repo forall android frameworks/base packages/apps/Settings packages/apps/Launcher3 \
  vendor/lineage build/make build/soong -c '
  git config remote.upstream.url "https://github.com/LineageOS/${REPO_PROJECT##*/}.git"
  git config remote.upstream.fetch "+refs/heads/*:refs/remotes/upstream/*"
  git config "remote.$REPO_REMOTE.pushurl" "git@github.com:$REPO_PROJECT.git"
  git config remote.pushDefault "$REPO_REMOTE"
'
repo manifest -r -o "$workspace/logs/source-lock.xml"
