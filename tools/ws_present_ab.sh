#!/bin/bash
# ws_present_ab.sh: report per-head handled transactions/s (not physical frames), flip-call time,
# late/timeouts/failures and WindowServer CPU. Run as root (sudo); the workload runs as the console user.
# usage: sudo tools/ws_present_ab.sh [ungated-values...]   e.g. "0 1 0 1" for an interleaved A/B (restored afterwards)
set -eu
for v in "$@"; do
  case "$v" in 0|1) ;; *) echo "ungated values must be 0 or 1" >&2; exit 2;; esac
done
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd); WL=${WL:-/tmp/ws_anim_workload}
was=$(sysctl -n debug.nvrmfb_flip_ungated) || { echo "ungated control unavailable; no workload or changes" >&2; exit 2; }
case "$was" in 0|1) ;; *) echo "unrecognized original ungated value; refusing changes" >&2; exit 2;; esac
[ -x "$WL" ] || xcrun clang -fobjc-arc -O1 "$HERE/ws_anim_workload.m" -framework Cocoa -framework QuartzCore -o "$WL"
U=$(stat -f %Su /dev/console); UID_=$(id -u "$U")
changed=0; workload=
cleanup() {
  local result=$?
  trap - EXIT
  # Workloads are bounded to 13 s; drain before restoring the original mode.
  if [ -n "$workload" ]; then wait "$workload" || result=1; fi
  if [ "$changed" = 1 ]; then
    sysctl -w debug.nvrmfb_flip_ungated="$was" >/dev/null || { echo "ERROR: failed to restore ungated=$was" >&2; result=1; }
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
one() {
  if [ -n "$1" ]; then
    changed=1
    sysctl -w debug.nvrmfb_flip_ungated="$1" >/dev/null
  fi
  local current
  current=$(sysctl -n debug.nvrmfb_flip_ungated)
  [ "$current" = "${1:-$was}" ] || { echo "ungated changed unexpectedly" >&2; return 2; }
  echo "ungated=$current; software callbacks and aggregate updates are not physical FPS"
  launchctl asuser "$UID_" sudo -u "$U" "$WL" 13 &
  workload=$!
  sleep 2
  kill -0 "$workload" 2>/dev/null || { echo "workload exited before measurement" >&2; return 2; }
  # Reuse the replay-tested monotonic/CPU parser; absent counters remain unavailable, never zero.
  bash "$HERE/ws_frame_cost.sh" 10
  wait "$workload"; workload=
  current=$(sysctl -n debug.nvrmfb_flip_ungated)
  [ "$current" = "${1:-$was}" ] || { echo "ungated changed during measurement; discard this window" >&2; return 2; }
}
if [ $# -eq 0 ]; then one ""; else for v in "$@"; do one "$v"; done; fi
