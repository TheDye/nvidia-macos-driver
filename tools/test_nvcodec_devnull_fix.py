#!/usr/bin/env python3
"""The codec dylibs must open their /dev/null connection token read-only (Chrome's GPU sandbox grants only
file-read-data on /dev/null; O_RDWR made every in-process NVDEC/NVENC open fail and Chrome fell back to software).
Checks the packaged payload, the rewrite on a scratch copy of an unfixed input when one is available, and refusal."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / 'tools/nvcodec_devnull_fix.py'
PKG = ROOT / 'dist/pkgroot-install/pkgroot/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS'
OLD = bytes.fromhex('be02000001')


def run(*a):
    return subprocess.run([sys.executable, str(TOOL), *map(str, a)], capture_output=True, text=True)


codecs = sorted(PKG.glob('libnvdec_*.dylib')) + sorted(PKG.glob('libnvenc_*.dylib'))
assert len(codecs) == 9, codecs
r = run('--check', *codecs)
assert r.returncode == 0 and r.stdout.count('already read-only') == 9, r.stdout + r.stderr
print('ok   all 9 packaged codec dylibs open /dev/null read-only')

with tempfile.TemporaryDirectory() as d:
    # Reverse the fix on a scratch copy, then require the tool to find and repair exactly those two sites.
    src = pathlib.Path(d) / 'libnvdec_hevc.dylib'
    shutil.copy(PKG / 'libnvdec_hevc.dylib', src)
    data = bytearray(src.read_bytes())
    r = run('--check', src)
    assert 'already read-only' in r.stdout
    dis = subprocess.run(['otool', '-tV', '-p', '_nvrm_xnu_open', str(src)], capture_output=True, text=True).stdout
    addrs = [int(l.split()[0], 16) for l in dis.splitlines() if '$0x1000000, %esi' in l]
    for a in addrs[:2]:
        data[a:a + 5] = OLD
    src.write_bytes(bytes(data))
    subprocess.run(['codesign', '-f', '-s', '-', str(src)], check=True, capture_output=True)
    assert run('--check', src).returncode == 1
    r = run(src)
    assert r.returncode == 0 and 'fixed' in r.stdout, r.stdout + r.stderr
    assert 'already read-only' in run('--check', src).stdout
    subprocess.run(['codesign', '--verify', '--strict', str(src)], check=True)
    print('ok   an O_RDWR input is rewritten at exactly its two sites, re-signed and verified')

    # A binary without the expected function is refused, never edited.
    other = pathlib.Path(d) / 'NVMTLDriver'
    shutil.copy(PKG / 'NVMTLDriver', other)
    before = other.read_bytes()
    r = run(other)
    assert r.returncode == 2 and 'STOP' in r.stderr and other.read_bytes() == before, r.stderr
    print('ok   an unrelated binary is refused unchanged')
print('PASS: codec /dev/null token is read-only')
