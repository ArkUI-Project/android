#!/usr/bin/env bash
set -euo pipefail

workspace=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
image="$workspace/volumes/ArkUI-Source.sparsebundle"
source_dir="$workspace/source"

if [[ $(uname -s) != Darwin ]]; then
  mkdir -p "$source_dir"
  exit 0
fi

mkdir -p "$workspace/volumes" "$source_dir"
if ! /sbin/mount | /usr/bin/grep -Fq " on $source_dir ("; then
  if [[ ! -d "$image" ]]; then
    hdiutil create -size 700g -type SPARSEBUNDLE \
      -fs 'Case-sensitive APFS' -volname ArkUI-Source "$image"
  fi
  hdiutil attach -nobrowse -mountpoint "$source_dir" "$image"
fi

printf 'Source workspace: %s\n' "$source_dir"
