#!/usr/bin/env python3
"""The fallback vblank period must reach the real rate of a high-refresh panel.

When NVKMS refuses the hardware vblank, vblPeriodAbs() paces the software timer that delivers vblanks
to WindowServer. Match the 1 ms minimum already used by flipWaitUs, including 540/600 Hz panels
that a 2 ms floor incorrectly dropped to 60 Hz. The real vblPeriodAbs() body is lifted and exercised;
negative controls restore both the 2 ms and 4 ms floors."""
import pathlib
import re
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root / 'kexts/NVRMFB/fb/nvrm-fb.cpp').read_text()

start = source.index('uint64_t NVRMNVDAFramebuffer::vblPeriodAbs()')
body_open = source.index('{', start) + 1
# Lift the function body up to its closing brace (single-level, no nested braces inside).
depth = 1
i = body_open
while depth:
    if source[i] == '{': depth += 1
    elif source[i] == '}': depth -= 1
    i += 1
body = source[body_open:i-1]

assert 'q >= 1000000ull' in body, 'the fallback and flip wait must agree on the minimum panel period'

fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
struct NvKmsKapiDisplayModeTimings { uint32_t pixelClockHz=0,hTotal=0,vTotal=0; };
// On x86 the mach timebase is 1:1 with ns, so model the conversion as identity for the test.
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs) { *abs = ns; }
struct FB {
    struct { NvKmsKapiDisplayModeTimings timings; } fMode;
    uint64_t vblPeriodAbs() {
VBL_BODY
    }
};
static FB at(uint32_t w, uint32_t h, uint32_t hz) {
    FB fb; auto &t = fb.fMode.timings;
    t.hTotal = w + 160; t.vTotal = h + 90;
    t.pixelClockHz = (uint32_t)((uint64_t)t.hTotal * t.vTotal * hz); // q == 1e9/hz exactly
    return fb;
}
static uint64_t nsFor(uint32_t hz) { return 1000000000ull / hz; }
int main() {
    // Real high-refresh panels report their own rate, not the 60 Hz default.
    assert(at(2560,1440,300).vblPeriodAbs() == nsFor(300));
    assert(at(2560,1440,280).vblPeriodAbs() == nsFor(280));
    assert(at(1920,1080,360).vblPeriodAbs() == nsFor(360));
    // Rates that already worked are unchanged.
    assert(at(2560,1440,240).vblPeriodAbs() == nsFor(240));
    assert(at(2560,1440,165).vblPeriodAbs() == nsFor(165));
    assert(at(1920,1080,60).vblPeriodAbs()  == nsFor(60));
    assert(at(1920,1080,540).vblPeriodAbs() == nsFor(540));
    assert(at(1920,1080,600).vblPeriodAbs() == nsFor(600));
    assert(at(1920,1080,1000).vblPeriodAbs() == nsFor(1000));
    // Out of the plausible panel range still falls back to 60 Hz (16666667 ns).
    assert(at(1920,1080,1001).vblPeriodAbs() == 16666667ull); // below the supported 1 ms floor
    assert(at(1920,1080,10).vblPeriodAbs()  == 16666667ull); // over 50 ms
    { FB z; assert(z.vblPeriodAbs() == 16666667ull); }       // no mode yet
    printf("OK\n");
    return 0;
}
'''

def build_and_run(vbl_body):
    src = fixture.replace('VBL_BODY', vbl_body)
    with tempfile.TemporaryDirectory() as d:
        cpp = pathlib.Path(d) / 'tc.cpp'
        binp = pathlib.Path(d) / 'tc'
        cpp.write_text(src)
        subprocess.run(['c++', '-std=c++17', '-O0', str(cpp), '-o', str(binp)], check=True)
        return subprocess.run([str(binp)], capture_output=True, text=True)

r = build_and_run(body)
assert r.returncode == 0 and r.stdout.strip() == 'OK', \
    f'fixed vblPeriodAbs failed: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}'
print('fixed source: PASS (60 through 1000 Hz; invalid/missing timings fall back)')

for floor in ('2000000ull', '4000000ull'):
    control = body.replace('q >= 1000000ull', 'q >= ' + floor)
    assert control != body, 'could not build the negative control'
    r = build_and_run(control)
    assert r.returncode != 0, 'negative control passed -- high refresh regression undetected'
    print(f'negative control ({floor} floor): FAILS as required')
print('PASS')
