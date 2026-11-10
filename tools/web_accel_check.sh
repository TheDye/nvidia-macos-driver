#!/bin/bash
# Read-only mapping evidence, not a proof of rendering or video-decoder use.
# Usage: tools/web_accel_check.sh [--processes-only]
# Functional Chrome-only pixels/compute/media test (Node >=22, disposable profile):
# tools/web_accel_check.sh --functional --chrome /path/Chrome --media-dir /tmp/fixtures --output /tmp/chrome.json
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
if [ "${1:-}" = --functional ]; then
  exec python3 "$HERE/test_angle_chrome_family.py" "$@"
fi
PROCESS_ONLY=0
if [ "$#" -gt 1 ]; then echo "usage: $0 [--processes-only]" >&2; exit 2; fi
if [ "$#" = 1 ]; then
  [ "$1" = --processes-only ] || { echo "usage: $0 [--processes-only]" >&2; exit 2; }
  PROCESS_ONLY=1
fi
check() {
  local found=0 p m libraries state pids status
  if pids=$(pgrep -f "$2"); then
    :
  else
    status=$?
    if [ "$status" != 1 ]; then
      printf "  %-28s UNKNOWN (process enumeration failed: %s)\n" "$1" "$status"
      return
    fi
  fi
  for p in $pids; do
    found=1
    if ! m=$(vmmap "$p" 2>&1); then
      printf "  %-28s pid %-6s UNKNOWN (vmmap failed; permissions or process exit)\n" "$1" "$p"
      continue
    fi
    if [ -z "$m" ]; then
      printf "  %-28s pid %-6s UNKNOWN (empty vmmap result)\n" "$1" "$p"
      continue
    fi
    libraries=$(printf "%s\n" "$m" | grep -oE 'NVMTLDriver|libvulkan_nouveau|libnvdec_[a-z0-9_]+|libnvenc_[a-z0-9_]+' | sort -u | tr '\n' ' ')
    case "$m" in
      *NVMTLDriver*|*libvulkan_nouveau*) state="PLUGIN MAPPED";;
      *libnvdec_*|*libnvenc_*) state="CODEC ONLY";;
      *) state="NOT OBSERVED";;
    esac
    printf "  %-28s pid %-6s %s (%s)\n" "$1" "$p" "$state" "${libraries:-no NullMoth mapping in this snapshot}"
  done
  [ "$found" = 1 ] || printf "  %-28s not running\n" "$1"
}
echo "GPU process mapping evidence:"
check "Safari (WebKit GPU)" "com.apple.WebKit.GPU"
# Chromium helper suffixes change (e.g. Aperitif in Chrome 157); identify the GPU role from argv too.
check "Chrome GPU" "Google Chrome Helper \(GPU\)|Google Chrome Helper.*--type=gpu-process"
check "Edge GPU" "Microsoft Edge Helper \(GPU\)|Microsoft Edge Helper.*--type=gpu-process"
check "Brave GPU" "Brave Browser Helper \(GPU\)|Brave Browser Helper.*--type=gpu-process"
check "Firefox GPU" "firefox.*-isForBrowser|plugin-container.*gpu"
check "Video decoder (VT XPC)" "VTDecoderXPCService"
echo "Mappings show loaded code only: neither mapping presence nor absence proves active GPU rendering or decoding."
[ "$PROCESS_ONLY" = 0 ] || exit 0
echo
echo "Chrome/WebKit ANGLE policy log this boot:"
/usr/bin/log show --last 1d --predicate 'eventMessage CONTAINS "NullMoth: angle"' --style compact 2>/dev/null | tail -3 | sed 's/^/  /'
echo
echo "VideoToolbox hardware-decode capability queries (not decode-session measurements):"
BIN=$(mktemp "${TMPDIR:-/tmp}/vt_hw_decode_check.XXXXXX") || exit 1
trap 'rm -f "$BIN"' EXIT HUP INT TERM
if xcrun clang -fobjc-arc -O1 -o "$BIN" "$HERE/vt_hw_decode_check.m" -framework VideoToolbox -framework CoreMedia -framework CoreVideo -framework AVFoundation -framework Foundation; then
  "$BIN" | sed 's/^/  /'
else
  echo "  UNKNOWN: decoder probe could not build; check Xcode command line tools."
  exit 1
fi
