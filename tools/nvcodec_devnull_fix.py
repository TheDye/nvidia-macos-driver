#!/usr/bin/env python3
"""Make the NVDEC/NVENC codec dylibs open their /dev/null connection token read-only, as NVK already does.

nvrm_xnu_open() binds each IOKit connection to a placeholder descriptor from open("/dev/null"). NVK opens it
O_RDONLY|O_CLOEXEC; the codec dylibs were built from an older copy that opens it O_RDWR|O_CLOEXEC. Chrome's GPU
sandbox (common.sb) grants only file-read-data on /dev/null, so inside Chrome the codec open failed silently
("nvd264: open /dev/nvidiactl failed") and VideoToolbox fell back to Apple's software decoder in VTDecoderXPCService.
The descriptor is never read or written; only its number is bound to the connection (selector 3).

The rewrite is exact: inside _nvrm_xnu_open only, `mov esi, 0x01000002` directly after `lea rdi, "/dev/null"` and
before `call _open` becomes `mov esi, 0x01000000`. Each input must contain exactly the expected sites; anything else
is refused. Usage: nvcodec_devnull_fix.py [--check] file.dylib...   (--check: report, do not write)
"""
import re
import subprocess
import sys

OLD = bytes.fromhex('be02000001')   # mov esi, 0x01000002 (O_RDWR | O_CLOEXEC)
NEW = bytes.fromhex('be00000001')   # mov esi, 0x01000000 (O_RDONLY | O_CLOEXEC)


def fail(msg):
    print('STOP: ' + msg, file=sys.stderr)
    sys.exit(2)


def text_mapping(path):
    out = subprocess.run(['otool', '-l', path], capture_output=True, text=True, check=True).stdout
    for m in re.finditer(r'segname __TEXT\n\s+vmaddr (0x[0-9a-f]+)\n\s+vmsize (0x[0-9a-f]+)\n\s+fileoff (\d+)', out):
        return int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))
    fail(f'{path}: no __TEXT segment')


def sites(path):
    dis = subprocess.run(['otool', '-tV', '-p', '_nvrm_xnu_open', path], capture_output=True, text=True, check=True).stdout
    lines = [l for l in dis.splitlines() if re.match(r'^[0-9a-f]{8,16}\s', l)]
    # otool -p prints from the symbol to the end of the section: stop at the next symbol
    syms = subprocess.run(['nm', '-n', path], capture_output=True, text=True, check=True).stdout.split('\n')
    addrs = sorted({int(s.split()[0], 16) for s in syms if re.match(r'^[0-9a-f]{16} [tT] ', s)})
    start = next((int(s.split()[0], 16) for s in syms if s.endswith(' _nvrm_xnu_open')), None)
    if start is None:
        fail(f'{path}: no _nvrm_xnu_open')
    end = next((a for a in addrs if a > start), 1 << 64)
    body = [l for l in lines if start <= int(l.split()[0], 16) < end]
    found, readonly = [], 0
    for i in range(1, len(body) - 2):
        if '"/dev/null"' not in body[i - 1]:
            continue
        if 'symbol stub for: _open' not in body[i + 2]:
            continue
        addr = int(body[i].split()[0], 16)
        if '$0x1000002, %esi' in body[i]:
            found.append(addr)
        elif '$0x1000000, %esi' in body[i]:
            readonly += 1
    return found, readonly


def main(argv):
    check = '--check' in argv
    files = [a for a in argv if a != '--check']
    if not files:
        fail('no input')
    bad = 0
    for path in files:
        found, readonly = sites(path)
        if not found and readonly == 2:
            print(f'ok   {path}: already read-only (2 sites)')
            continue
        if len(found) != 2 or readonly:
            fail(f'{path}: expected exactly 2 O_RDWR /dev/null sites in _nvrm_xnu_open, found {len(found)} (+{readonly} read-only)')
        vm, size, fo = text_mapping(path)
        data = bytearray(open(path, 'rb').read())
        for a in found:
            if not vm <= a < vm + size:
                fail(f'{path}: site 0x{a:x} outside __TEXT')
            off = a - vm + fo
            if data[off:off + 5] != OLD:
                fail(f'{path}: bytes at 0x{a:x} are {data[off:off + 5].hex()}, not {OLD.hex()}')
            data[off:off + 5] = NEW
        if check:
            print(f'NEED {path}: 2 O_RDWR /dev/null sites')
            bad += 1
            continue
        info = subprocess.run(['codesign', '-dv', path], capture_output=True, text=True).stderr
        ident = re.search(r'^Identifier=(.+)$', info, re.M)
        tmp = path + '.devnull-fix.tmp'
        open(tmp, 'wb').write(data)
        sign = ['codesign', '-f', '-s', '-'] + (['--identifier', ident.group(1)] if ident else []) + [tmp]
        subprocess.run(sign, check=True, capture_output=True)
        after, ro = sites(tmp)
        if after or ro != 2:
            fail(f'{tmp}: verification failed')
        subprocess.run(['/bin/mv', '-f', tmp, path], check=True)
        print(f'fixed {path}')
    return 1 if (check and bad) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
