#!/bin/bash
# Build the current checkout without installing. Usage: build_plugin.sh [output-directory]
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
HERE=$(cd "$(dirname "$0")/.." && pwd)
S=${NVMTL_PLUGIN_SOURCE:-$HERE/plugin}
if [ "$#" -gt 1 ]; then echo "usage: $0 [output-directory]" >&2; exit 2; fi
O=${1:-${NVMTL_PLUGIN_OUTPUT:-$(mktemp -d "${EMPRYO_TMP:-${TMPDIR:-/tmp}}/nvmtl-plugin.XXXXXX")}}
mkdir -p "$O"
O=$(cd "$O" && pwd)
L=$O/.build.lock
mkdir "$L" 2>/dev/null || { echo "STOP: build already owns $L" >&2; exit 3; }
trap 'rmdir "$L"' EXIT
cd "$S"
C=$(git rev-parse --short HEAD 2>/dev/null || printf 'working-tree')
RDEF=(); [ -z "${RELEASE:-}" ] || RDEF=(-DNVMTL_RELEASE=1)
xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -bundle -fobjc-arc -O1 "${RDEF[@]}" -Wno-protocol -Wno-incomplete-implementation -Werror=unguarded-availability-new \
  -I"${VULKAN_HEADERS:-$HOME/Vulkan-Headers/include}" -o "$O/NVMTLDriver" NVMTLDevice.m nvmtl_dlss.c dlss/elfhost.c dlss/lnxlibc.c \
  -framework Foundation -framework Metal -framework IOSurface -framework IOKit -framework CoreFoundation -framework MetalFX -liconv 2>"$O/NVMTLDriver.err" \
  || { echo "STOP: compile failed; see $O/NVMTLDriver.err"; grep -A2 ': error:' "$O/NVMTLDriver.err" | head -30; exit 5; }
codesign -f -s - "$O/NVMTLDriver"
codesign --verify --strict "$O/NVMTLDriver"
minos=$(vtool -show-build "$O/NVMTLDriver" | awk '/minos/{print $2; exit}')
[ "$minos" = 15.0 ] || { echo "STOP: minos $minos, not 15.0"; exit 6; }
# Preserve the existing live-feature checks for checkout and explicit-output builds.
miss=0
while IFS='|' read -r feat pat; do
  [ -n "$feat" ] || continue
  n=$(strings -a "$O/NVMTLDriver" | grep -c -- "$pat" || true)
  # FPS now deliberately remains available to NVMTL_PROCESS_ENV=1 processes;
  # only the request-file passtime instrument is compiled out of releases.
  case "$feat" in "passtime instrument")
    if [ -n "${RELEASE:-}" ]; then
      [ "$n" -eq 0 ] || { echo "LEAKED diagnostic $feat ($pat)"; miss=$((miss+1)); }
      continue
    fi;;
  esac
  [ "$n" -ge 1 ] || { echo "MISSING $feat ($pat)"; miss=$((miss+1)); }
done <<'M'
vendor compiler lane (NVIDIAShared)|NVIDIAShared
SASS dispatch on NVK (sasspipe)|nvmtl_nvk_dispatch_sass_v1
vendor variant (Apple MTLCompiler handshake)|vendor variant
bindless-all heap slots|bindless-all - textures reach shaders
dirty-only bindings|NVMTL_NO_DIRTYONLY
fullclear (Apple whole-attachment clear)|begin_render: fullclear
per-stage binding mask (gfx-bmask)|gmask
fps file|nvmtl-fps-
passtime instrument|nvmtl-passtime-request
library key memo (xlate)|library key memo: hit
res2-evict (Apple order: newest wins the card)|on the allocating thread
DLSS behind the MetalFX temporal scaler|served by DLSS
M
[ "$miss" -eq 0 ] || { echo "STOP: missing live feature; do NOT stage"; exit 6; }
echo "plugin-$C sha256 $(shasum -a 256 "$O/NVMTLDriver" | cut -d' ' -f1) minos $minos warnings $(grep -c ': warning:' "$O/NVMTLDriver.err" || true)"
echo "$O/NVMTLDriver"
