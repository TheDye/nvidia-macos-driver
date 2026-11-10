#!/usr/bin/env python3
"""Which processes hand video decode to VTDecoderXPCService instead of registering the decoder in-process. Only Apple's
own sandboxed apps do (Safari's WebKit GPU process, QuickTime Player, Preview); a sandboxed Chromium or Electron GPU
process (Discord) must keep its in-process hardware decoder, or its videos do not play. The plugin's own functions are
compiled into two small programs, one carrying an Apple bundle identifier and one Discord's, and run inside and outside
a sandbox."""
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
src = (root / 'plugin/NVMTLDevice.m').read_text()
fails = 0


def check(name, ok, detail=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + (f' ({detail})' if detail else ''))
    fails += 0 if ok else 1


def function(signature):
    i = src.index(signature)
    return src[i:src.index('\n}\n', i) + 3]


if platform.system() != 'Darwin' or not shutil.which('clang') or not pathlib.Path('/usr/bin/sandbox-exec').exists():
    print('skip: needs macOS with clang and sandbox-exec')
    sys.exit(0)

body = ''.join(function(s) for s in ('static bool nvmtl_is_sandboxed(void)',
                                       'static bool nvmtl_is_decode_service(const char *me)',
                                       'static bool nvmtl_decode_is_delegated(const char *me)'))
harness = ('#include <CoreFoundation/CoreFoundation.h>\n#include <dispatch/dispatch.h>\n#include <dlfcn.h>\n'
           '#include <stdbool.h>\n#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <unistd.h>\n'
           + body + 'int main(void) { printf("%d\\n", nvmtl_decode_is_delegated(getprogname())); return 0; }\n')
plist = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleIdentifier</key>'
         '<string>{}</string></dict></plist>')

with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    (d / 'h.c').write_text(harness)
    progs = {}
    for name, ident in (('apple', 'com.apple.Preview'), ('discord', 'com.hnc.Discord')):
        (d / f'{name}.plist').write_text(plist.format(ident))
        r = subprocess.run(['clang', '-o', str(d / name), str(d / 'h.c'), '-framework', 'CoreFoundation',
                            '-Wl,-sectcreate,__TEXT,__info_plist,' + str(d / f'{name}.plist')],
                           capture_output=True, text=True, timeout=120)
        check(f'the {name} probe builds from the plugin source', r.returncode == 0, r.stderr.strip()[-300:])
        progs[name] = d / name

    def run(prog, sandboxed):
        cmd = ['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)', str(prog)] if sandboxed else [str(prog)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()

    if all(p.exists() for p in progs.values()):
        check('a sandboxed Apple app delegates decode to VTDecoderXPCService', run(progs['apple'], True) == '1')
        check('a sandboxed Discord keeps its in-process hardware decoder', run(progs['discord'], True) == '0')
        check('control: an unsandboxed Apple app registers in-process', run(progs['apple'], False) == '0')
        check('control: an unsandboxed Discord registers in-process', run(progs['discord'], False) == '0')
# Compile the real registrar/constructor bodies with deterministic loader and I/O stubs.
# Each scenario gets a new process, hence fresh dispatch_once tokens (no system interposition).
registration = ''.join(function(s) for s in (
    'static void nvmtl_nvdec_register(void)', 'static void nvmtl_nvdec_hevc_register(void)',
    '__attribute__((constructor)) static void nvmtl_nvdec_in_decode_service(void)'))
registration = registration.replace('__attribute__((constructor)) ', '')
stubs = r'''
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#include <dispatch/dispatch.h>
#include <dlfcn.h>
#include <libgen.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int ready, registrations, loads, scans, queued, sandboxed, delegated;
static int g_video_blackwell = -1;
static const char *process = "probe";
static const char *mock_progname(void) { return process; }
static bool nvmtl_is_sandboxed(void) { return sandboxed; }
static bool nvmtl_decode_is_delegated(const char *me) { return delegated; }
static bool nvmtl_video_is_blackwell(void) { return false; }
static bool nvmtl_gpu_profile_string(CFStringRef k, char *out, size_t n) { return false; }
static int32_t mock_register(uint32_t c, CFDictionaryRef info, void *factory) { registrations++; return 0; }
static void *mock_dlsym(void *h, const char *s) {
    if (!strcmp(s, "VTRegisterVideoDecoderWithInfo")) return ready ? (void *)mock_register : NULL;
    return (void *)1;
}
static int mock_dladdr(const void *p, Dl_info *di) { di->dli_fname = "/test/NVMTLDriver"; return 1; }
static void *mock_dlopen(const char *p, int flags) { loads++; return (void *)1; }
static kern_return_t mock_matching(mach_port_t p, CFDictionaryRef match, io_iterator_t *it) {
    scans++; CFRelease(match); *it = 1; return KERN_SUCCESS;
}
static io_object_t mock_next(io_iterator_t it) { static int n; return n++ ? 0 : 1; }
static kern_return_t mock_release(io_object_t o) { return KERN_SUCCESS; }
static CFTypeRef mock_property(io_registry_entry_t o, CFStringRef key, CFAllocatorRef a, IOOptionBits opts) {
    uint32_t value = CFEqual(key, CFSTR("vendor-id")) ? 0x10de :
                     CFEqual(key, CFSTR("class-code")) ? 0x030000 : 0x2684;
    return CFDataCreate(a, (const UInt8 *)&value, 4);
}
static void mock_async(dispatch_queue_t q, dispatch_block_t block) { queued++; block(); }
#define getprogname mock_progname
#define dlsym mock_dlsym
#define dladdr mock_dladdr
#define dlopen mock_dlopen
#define IOServiceGetMatchingServices mock_matching
#define IOIteratorNext mock_next
#define IOObjectRelease mock_release
#define IORegistryEntryCreateCFProperty mock_property
#define dispatch_async mock_async
#define nvlog(...) ((void)0)
'''
main = r'''
int main(int argc, char **argv) {
    @autoreleasepool {
        const char *mode = argv[1];
        if (!strcmp(mode, "retry")) {
            nvmtl_nvdec_register(); nvmtl_nvdec_hevc_register();
            if (loads || registrations) return 1;
            ready = 1;
            nvmtl_nvdec_register(); nvmtl_nvdec_hevc_register();
            nvmtl_nvdec_register(); nvmtl_nvdec_hevc_register();
            printf("registrations=%d loads=%d\n", registrations, loads);
            return registrations != 2 || loads != 2;
        }
        if (strcmp(mode, "default") && strcmp(mode, "eager-off")) setenv("NVMTL_EAGER_NVDEC", "1", 1);
        if (!strcmp(mode, "eager-off")) setenv("NVMTL_EAGER_NVDEC", "0", 1);
        ready = strcmp(mode, "unavailable") != 0;
        sandboxed = !strcmp(mode, "apple") || !strcmp(mode, "discord") || !strcmp(mode, "service");
        delegated = !strcmp(mode, "apple");
        if (!strcmp(mode, "window")) process = "WindowServer";
        if (!strcmp(mode, "service")) process = "VTDecoderXPCService";
        if (!strcmp(mode, "disabled")) setenv("NVMTL_NO_NVDEC", "1", 1);
        if (!strcmp(mode, "hevc-disabled")) setenv("NVMTL_NO_NVDEC_HEVC", "1", 1);
        nvmtl_nvdec_in_decode_service();
        int expected = !strcmp(mode, "eager") || !strcmp(mode, "service") ? 2 :
                       !strcmp(mode, "hevc-disabled") ? 1 : 0;
        printf("registrations=%d loads=%d scans=%d queued=%d\n", registrations, loads, scans, queued);
        if (registrations != expected || loads != expected) return 1;
        if (!expected && scans) return 1;
        if (queued != (!strcmp(mode, "service"))) return 1;
        if (!strcmp(mode, "apple") || !strcmp(mode, "window")) {
            nvmtl_nvdec_register(); nvmtl_nvdec_hevc_register();
            return loads != 0;
        }
        if (!strcmp(mode, "discord")) {
            nvmtl_nvdec_register(); nvmtl_nvdec_hevc_register();
            return registrations != 2;
        }
        return 0;
    }
}
'''
with tempfile.TemporaryDirectory(prefix='nvmtl-decode-tests-') as d:
    d = pathlib.Path(d)
    (d / 'registration.m').write_text(stubs + function('static bool nvmtl_is_decode_service(const char *me)')
                                     + registration + main)
    r = subprocess.run(['clang', '-fobjc-arc', '-fblocks', '-o', str(d / 'registration'),
                        str(d / 'registration.m'), '-framework', 'Foundation', '-framework', 'IOKit'],
                       capture_output=True, text=True, timeout=120)
    check('extracted decoder initialization helpers build', r.returncode == 0, r.stderr.strip()[-1000:])
    if r.returncode == 0:
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith('NVMTL_NO_NVDEC') and k != 'NVMTL_EAGER_NVDEC'}
        for mode in ('retry', 'default', 'eager-off', 'eager', 'unavailable', 'apple', 'discord', 'window', 'service', 'disabled', 'hevc-disabled'):
            r = subprocess.run([str(d / 'registration'), mode], capture_output=True, text=True, env=env, timeout=30)
            check('decoder initialization: ' + mode, r.returncode == 0, (r.stdout + r.stderr).strip()[-500:])
# Actual hardware decode is opt-in because registration/capability stubs cannot certify it.
# Fixtures: ffmpeg color=red:size=128x96:rate=30:duration=3 + blue right half,
# encoded with libx264 / libx265 (hvc1), yuv420p as h264.mp4 / hevc.mp4.
# NVMTL_DECODE_FIXTURES=/tmp/fixtures NVMTL_DECODE_PLUGIN=/.../NVMTLDriver python3 tools/test_decode_delegation.py
import os
import json
with tempfile.TemporaryDirectory(prefix='nvmtl-real-decode-') as d:
    exe = pathlib.Path(d) / 'vt_hw_decode_check'
    r = subprocess.run(['clang', '-fobjc-arc', '-fblocks', '-O1', '-o', str(exe),
                        str(root / 'tools/vt_hw_decode_check.m'), '-framework', 'VideoToolbox',
                        '-framework', 'CoreMedia', '-framework', 'CoreVideo',
                        '-framework', 'AVFoundation', '-framework', 'Foundation'],
                       capture_output=True, text=True, timeout=120)
    check('real VT session/pixel probe builds', r.returncode == 0, r.stderr.strip()[-1000:])
    if r.returncode == 0:
        r = subprocess.run([str(exe), '--decode'], capture_output=True, text=True, timeout=10)
        check('incomplete decode option is rejected', r.returncode == 2 and 'incomplete' in r.stderr)
        fixtures = os.environ.get('NVMTL_DECODE_FIXTURES')
        plugin = os.environ.get('NVMTL_DECODE_PLUGIN')
        if fixtures and plugin:
            env = dict(os.environ, NVMTL_PROCESS_ENV='1', NVMTL_EAGER_NVDEC='1')
            for codec in ('h264', 'hevc'):
                r = subprocess.run([str(exe), '--plugin', plugin, '--decode',
                                    str(pathlib.Path(fixtures) / (codec + '.mp4'))],
                                   env=env, capture_output=True, text=True, timeout=60)
                try:
                    evidence = json.loads(r.stdout)
                except ValueError:
                    evidence = {}
                check(codec + ' real hardware frames and red/blue pixels',
                      r.returncode == 0 and evidence.get('pass') is True and evidence.get('usingHardware') is True
                      and evidence.get('frames', 0) > 0 and evidence.get('matchingFrames') == evidence.get('frames'),
                      (r.stdout + r.stderr).strip()[-1500:])
        else:
            print('skip: actual hardware frame tests need NVMTL_DECODE_FIXTURES and NVMTL_DECODE_PLUGIN (capabilities are not proof)')
sys.exit(1 if fails else 0)
