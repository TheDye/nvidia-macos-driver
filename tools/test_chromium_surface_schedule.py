#!/usr/bin/env python3
"""Chromium-family processes publish surface writes before reporting scheduled (upstream PR #481);
everything else keeps policy 3, and explicit 0..3 overrides still win. Also checks the production call site."""
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
src = (ROOT / 'plugin/NVMTLEventSync.m').read_text()
assert '#include "nvmtl_surface_schedule.h"' in src
assert 'nvmtl_surface_schedule_after(getenv("NVMTL_SCHEDULE_AFTER_SURFACE_WRITES"), getprogname())' in src
assert "after = e && *e ? e[0] - '0' : 3" not in src
C = r'''
#include "nvmtl_surface_schedule.h"
#include <stdio.h>
int main(void) {
    const char *browsers[] = {"Google Chrome", "Google Chrome Helper (GPU)", "Google Chrome Helper (Aperitif)",
        "Chromium", "Chromium Helper (Renderer)", "Microsoft Edge", "Microsoft Edge Helper (GPU)",
        "Brave Browser Helper (GPU)", "Vivaldi Helper (GPU)"};
    const char *others[] = {0, "", "WindowServer", "QQ", "Electron", "Safari", "EdgeView", "com.apple.WebKit.GPU"};
    for (unsigned i = 0; i < sizeof browsers / sizeof *browsers; i++)
        if (nvmtl_surface_schedule_after(0, browsers[i]) != 1 || nvmtl_surface_schedule_after("", browsers[i]) != 1) return 1;
    for (unsigned i = 0; i < sizeof others / sizeof *others; i++)
        if (nvmtl_surface_schedule_after(0, others[i]) != 3) return 2;
    for (int mode = 0; mode <= 3; mode++) {
        const char s[] = {(char)('0' + mode), 0};
        if (nvmtl_surface_schedule_after(s, "Google Chrome Helper (GPU)") != mode || nvmtl_surface_schedule_after(s, "WindowServer") != mode) return 3;
    }
    /* malformed overrides fall back to the default instead of a negative or huge mode */
    if (nvmtl_surface_schedule_after("x", "WindowServer") != 3 || nvmtl_surface_schedule_after("12", "Google Chrome") != 1) return 4;
    return 0;
}
'''
with tempfile.TemporaryDirectory() as d:
    c = pathlib.Path(d) / 't.c'; c.write_text(C)
    exe = pathlib.Path(d) / 't'
    subprocess.run(['xcrun', 'clang', '-Wall', '-Werror', '-I', str(ROOT / 'plugin'), str(c), '-o', str(exe)], check=True)
    r = subprocess.run([str(exe)])
    assert r.returncode == 0, f'policy case {r.returncode} failed'
print('PASS: Chromium surface publication policy, other-process scope, explicit and malformed overrides')
