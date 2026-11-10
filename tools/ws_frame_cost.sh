#!/bin/bash
# CPU per completed output update (all heads), never per flip attempt or handled transaction.
# Usage: tools/ws_frame_cost.sh [seconds=10] [--sample] [--json] [--save FILE]
#        tools/ws_frame_cost.sh --replay FILE --json
exec python3 - "$@" <<'PY'
import argparse
import json
import math
import pathlib
import subprocess
import sys
import tempfile
import time

COUNTERS = {'flips': 'debug.nvaccel_iop_flips', 'copies': 'debug.nvaccel_iop_copies',
            'attempts': 'debug.nvrmfb_flip_n', 'late': 'debug.nvaccel_iop_late',
            'drop': 'debug.nvaccel_iop_async_drop', 'fail': 'debug.nvaccel_iop_fail',
            'latch_timeouts': 'debug.nvrmfb_flip_latch_timeouts', 'flip_us_sum': 'debug.nvrmfb_flip_us_sum'}
COUNTERS.update({'handled_h%d' % i: 'debug.nvaccel_iop_ok_h%d' % i for i in range(5)})

def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10)

def cpu_seconds(value):
    value = value.strip()
    days = 0
    if '-' in value:
        day, value = value.split('-', 1)
        days = int(day)
    result = 0.0
    for part in value.split(':'):
        result = result * 60 + float(part)
    return result + days * 86400

def snapshot(pid):
    result = command(['ps', '-o', 'time=', '-p', str(pid)])
    if result.returncode or not result.stdout.strip():
        raise ValueError('WindowServer CPU time unavailable')
    row = {'pid': pid, 'cpu_s': cpu_seconds(result.stdout), 'monotonic_s': time.monotonic(), 'counters': {}}
    for key, oid in COUNTERS.items():
        result = command(['sysctl', '-n', oid])
        try:
            row['counters'][key] = int(result.stdout.strip()) if result.returncode == 0 else None
        except ValueError:
            row['counters'][key] = None
    return row

def metrics(first, last):
    if first['pid'] != last['pid']:
        raise ValueError('WindowServer changed during measurement')
    elapsed = last['monotonic_s'] - first['monotonic_s']
    cpu = last['cpu_s'] - first['cpu_s']
    if elapsed <= 0 or cpu < 0:
        raise ValueError('invalid elapsed/CPU time or WindowServer restarted')
    out = {'elapsed_s': elapsed, 'cpu_s': cpu, 'cpu_percent_one_core': 100 * cpu / elapsed}
    for key in COUNTERS:
        a, b = first['counters'].get(key), last['counters'].get(key)
        if a is None or b is None:
            out[key] = None
        elif b < a:
            raise ValueError('counter reset: ' + key)
        else:
            out[key] = b - a
    flips, copies = out['flips'], out['copies']
    total = flips + copies if flips is not None and copies is not None else None
    out['completed_updates'] = total
    out['updates_per_s'] = total / elapsed if total is not None else None
    out['cpu_ms_per_update'] = 1000 * cpu / total if total else None
    out['zero_copy_per_s'] = flips / elapsed if flips is not None else None
    out['cpu_ms_per_zero_copy_update'] = 1000 * cpu / flips if flips else None
    out['flip_call_mean_us'] = out['flip_us_sum'] / out['attempts'] if out['flip_us_sum'] is not None and out['attempts'] else None
    return out

def main():
    parser = argparse.ArgumentParser(description='Aggregate WindowServer CPU per completed output update; not a per-display frame-time measurement.')
    parser.add_argument('seconds', nargs='?', type=float, default=10)
    parser.add_argument('--sample', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--save', type=pathlib.Path)
    parser.add_argument('--replay', type=pathlib.Path)
    parser.add_argument('--pid', type=int)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 120:
        parser.error('seconds must be 1..120')
    if args.replay:
        data = json.loads(args.replay.read_text())
    else:
        if args.pid is None:
            result = command(['pgrep', '-x', 'WindowServer'])
            pids = result.stdout.split()
            if result.returncode or len(pids) != 1:
                raise ValueError('WindowServer not uniquely readable; use --pid')
            args.pid = int(pids[0])
        print('Measuring %.1f s; keep the same workload active.' % args.seconds, file=sys.stderr)
        data = {'before': snapshot(args.pid)}
        if args.sample:
            with tempfile.NamedTemporaryFile(prefix='ws-sample-', suffix='.txt', delete=False) as output:
                sample_path = output.name
            result = subprocess.run(['/usr/bin/sample', str(args.pid), str(math.ceil(args.seconds)), '-file', sample_path],
                                    capture_output=True, text=True, timeout=args.seconds + 30)
            if result.returncode:
                raise ValueError('WindowServer sample failed (permissions may require sudo): ' + result.stderr.strip())
            data['sample'] = sample_path
        else:
            time.sleep(args.seconds)
        data['after'] = snapshot(args.pid)
        if args.save:
            args.save.write_text(json.dumps(data, indent=2))
    out = metrics(data['before'], data['after'])
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        print('WindowServer CPU %.2f%% of one core over %.3f s' % (out['cpu_percent_one_core'], out['elapsed_s']))
        print('Successful zero-copy updates: %s; CPU-completed copies: %s; flip attempts: %s' %
              (out['flips'], out['copies'], out['attempts']))
        if out['cpu_ms_per_update'] is not None:
            print('Aggregate %.3f completed output updates/s; %.3f ms CPU/update' % (out['updates_per_s'], out['cpu_ms_per_update']))
        else:
            print('CPU per completed output update: UNKNOWN (missing copy counter or no completed updates).')
        if out['cpu_ms_per_zero_copy_update'] is not None:
            print('Zero-copy-only ratio %.3f ms CPU/update (not the total-frame cost if copies occur).' % out['cpu_ms_per_zero_copy_update'])
        print('Per-head handled transactions (includes queued/kept frames): ' + ', '.join(str(out['handled_h%d' % i]) for i in range(5)))
        print('Late %s; dropped %s; failed %s; latch timeout steps %s (not synonymous with failures).' %
              (out['late'], out['drop'], out['fail'], out['latch_timeouts']))
        print('Mean flip-call time: %s us (UNKNOWN if missing counters or zero attempts).' % out['flip_call_mean_us'])
        print('Output counts aggregate all heads, not unique compositor frames or frame-time percentiles; None means unavailable.')
        if 'sample' in data:
            print('Full sample: ' + data['sample'])
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print('measurement unavailable: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
PY
