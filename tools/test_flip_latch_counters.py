#!/usr/bin/env python3
"""The flip completion wait is measured in refresh periods, returns only once the flip is on the panel, and reports.

A committed flip latches at the next vblank, so the wait runs in steps of one period and a quarter (from the mode's
raster and pixel clock); a step that misses counts as a late flip and the wait goes on, because the caller completes
the transaction (WindowServer then reuses the surface the flip replaced) as soon as submitConfig returns. Only a flip
still pending 100 ms after its submit faults the head. A prior flip that is merely late costs one present on the frame
path (IN_PROGRESS, the frame stays on the panel). The real wait regions are lifted from nvrm-fb.cpp and exercised.
Two negative controls must fail: the constant 100 ms region (no late count) and the region that let a late frame go
before its flip landed (the surface it replaced could still be scanned out while WindowServer reused it)."""
import pathlib
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root / 'kexts/NVRMFB/fb/nvrm-fb.cpp').read_text()


def lift_body(name):
    """The body of a single-level member function, by its definition line."""
    start = source.index(name)
    body_open = source.index('{', start) + 1
    depth, i = 1, body_open
    while depth:
        if source[i] == '{': depth += 1
        elif source[i] == '}': depth -= 1
        i += 1
    return source[body_open:i - 1]


# the latch region of submitConfig: from the gate to the fault at its end
start = source.index('if (bootstrap || !gFlipLatch) return NV_TRUE;')
end = source.index('fSubmissionFaulted = true; return NV_FALSE;', start)
end = source.index('\n', end)
region = source[start:end]
# the prior-completion wait that precedes a new submit (the if block's body, closing brace included)
sc = source.index('NvBool NVRMNVDAFramebuffer::submitConfig(')
p0 = source.index('if (fSubmissionSeen) {', sc) + len('if (fSubmissionSeen) {')
p1 = source.index('    const bool tracked', p0)
prior_region = source[p0:p1]
# the event-woken wait, lifted whole so the test runs the shipped loop
w0 = source.index('static IOLock *sFlipWaitLock;')
w1 = source.index('\n}\n', source.index('bool NVRMNVDAFramebuffer::waitFlipDone(uint64_t limitUs)')) + 3
waiter = source[w0:w1]
statics = waiter[:waiter.index('void NVRMNVDAFramebuffer::flipWake')]
body = waiter[waiter.index('bool NVRMNVDAFramebuffer::waitFlipDone'):].replace('NVRMNVDAFramebuffer::', '')
vbl_body = lift_body('uint64_t NVRMNVDAFramebuffer::vblPeriodAbs()')
wait_us_body = lift_body('uint64_t NVRMNVDAFramebuffer::flipWaitUs()')
prior_body = lift_body('int NVRMNVDAFramebuffer::priorFlipState()')
stuck_line = source[source.index('static const uint64_t kFlipStuckUs'):]
stuck_line = stuck_line[:stuck_line.index('\n')]

assert 'gFlipLatchWaits' in region and 'gFlipLatchTimeouts' in region, \
    'the completion wait no longer updates the flip-latch counters'
assert 'waitFlipDone(flipWaitUs())' in region, \
    'the latch wait is bounded by a constant again instead of the refresh period'

# the region a first draft of this change had: the frame path let go after one period, before its flip landed
early_control = region.replace('    for (;;) {', '    if (frame) return NV_TRUE;\n    for (;;) {', 1)
assert early_control != region

control = r'''    if (bootstrap || !gFlipLatch) return NV_TRUE;
    OSIncrementAtomic((volatile SInt32 *)&gFlipLatchWaits);
    const uint64_t latch0 = mach_absolute_time();
    if (waitFlipDone(100000)) {
        uint64_t ns = 0; absolutetime_to_nanoseconds(mach_absolute_time() - latch0, &ns);
        const int us = (int)(ns / 1000);
        if (us > gFlipLatchMaxUs) gFlipLatchMaxUs = us;
        return NV_TRUE;
    }
    OSIncrementAtomic((volatile SInt32 *)&gFlipLatchTimeouts);
    FBLOG("presentation blocked: submitted completion unavailable on head %u", fHead);
    fSubmissionFaulted = true; return NV_FALSE;'''

fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
using NvU32=uint32_t; using SInt32=int32_t; using NvBool=bool;
enum { NV_TRUE=1, NV_FALSE=0, NVKMS_KAPI_LAYER_PRIMARY_IDX=0 };
enum NvKmsFlipResult { NV_KMS_FLIP_RESULT_SUCCESS = 0, NV_KMS_FLIP_RESULT_INVALID_PARAMS, NV_KMS_FLIP_RESULT_IN_PROGRESS };
#define FBLOG(...) ((void)0)
static int64_t gClock = 0;   // nanoseconds, the absolute-time unit here
static uint64_t mach_absolute_time() { gClock += 1000; return (uint64_t)gClock; } // +1 us per call
static void IOSleep(unsigned) { gClock += 1000000; }
static SInt32 OSIncrementAtomic(volatile SInt32 *p) { return (*p)++; }
static SInt32 OSAddAtomic(SInt32 v, volatile SInt32 *p) { SInt32 o = *p; *p += v; return o; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = t; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs) { *abs = ns; }
enum { kMicrosecondScale = 1000, kMillisecondScale = 1000000, THREAD_UNINT = 0 };
using event_t = void *;
struct IOLock { int held; };
static IOLock gLockObj;
static IOLock *IOLockAlloc() { return &gLockObj; }
static void IOLockFree(IOLock *) {}
static bool OSCompareAndSwapPtr(void *o, void *n, void *volatile *p) { if (*p != o) return false; *p = n; return true; }
static void IOLockLock(IOLock *l) { l->held = 1; }
static void IOLockUnlock(IOLock *l) { l->held = 0; }
static void clock_interval_to_deadline(uint64_t v, uint64_t scale, uint64_t *out) { *out = (uint64_t)gClock + v * scale; }
static int gSleeps = 0; static int64_t gLongestSleep = 0;
static int IOLockSleepDeadline(IOLock *l, event_t, uint64_t deadline, int) {
    assert(l->held && "the flip wait must sleep holding its lock");
    int64_t d = (int64_t)deadline - gClock; if (d > gLongestSleep) gLongestSleep = d;
    gSleeps++; if ((int64_t)deadline > gClock) gClock = (int64_t)deadline; return 0; }
STATICS

static char gPresentMsg[8];
#define PRESENT_STATS_LOG() ((void)0)
static bool presentStatsNote(unsigned, uint64_t, bool, bool, bool, uint32_t) { return false; }   // exercised by test_present_stats.py
static int gFlipLatch = 1;
static int gFlipLatchWaits = 0, gFlipLatchTimeouts = 0, gFlipLatchMaxUs = 0, gFlipLatchLate = 0;
static int gFlipLatchDeferred = 0;
STUCK_LINE
enum Mode { COMPLETE, STUCK, FAIL, LATE };
struct Kms {
    Mode mode = COMPLETE; int polls = 0, lateFor = 0;
    bool getFlipPendingStatus(void *, unsigned, int, NvBool *pending) {
        if (mode == FAIL) return false;
        polls++;
        *pending = (mode == STUCK) || (mode == LATE && polls <= lateFor);
        return true;
    }
};
struct NvKmsKapiDisplayModeTimings { uint32_t pixelClockHz=0,hTotal=0,vTotal=0; };
struct Rep { NvKmsFlipResult flipResult = NV_KMS_FLIP_RESULT_SUCCESS; };
struct FB {
    Kms *fKms=nullptr; void *fDev=this; unsigned fHead=0; bool fSubmissionFaulted=false, fSubmissionSeen=false;
    volatile SInt32 fStopping = 0;
    bool fFlipPowerInvalid = false, fLandingOwed = false;
    void *fSubmissionDevice = nullptr;
    uint64_t fSubmitted = 0, fCompleted = 0, fLandingEpoch = 1;
    void invalidateLanding() { INVALIDATE_BODY }
    bool rmGpuReady() { return true; }
    uint64_t fFlipSubmitAbs = 0; unsigned fLatchLateLogs = 0;
    int faultLogs = 0; const char *faultWhy = "";
    void flipFaultLog(const char *why) { faultLogs++; faultWhy = why; }
    enum { kPriorDone = 0, kPriorLate = 1, kPriorStuck = 2 };
    struct { NvKmsKapiDisplayModeTimings timings; } fMode;
    void mode(uint32_t pclk, uint32_t ht, uint32_t vt) { fMode.timings.pixelClockHz = pclk; fMode.timings.hTotal = ht; fMode.timings.vTotal = vt; }
    uint64_t vblPeriodAbs() {
VBL_BODY
    }
    uint64_t flipWaitUs() {
WAIT_US_BODY
    }
    int priorFlipState() {
PRIOR_BODY
    }
WAITER
    // the prior-completion wait, as submitConfig runs it before a new submit
    NvBool priorWait(Rep *rep, bool frame) {
        if (fSubmissionSeen) {
PRIOR_REGION
        return NV_TRUE;
    }
    // the latch wait after an accepted submit
    NvBool waitComplete(bool bootstrap, bool frame) {
        const uint64_t submit0 = mach_absolute_time();
WAIT_REGION
        return NV_TRUE; // control's path when it falls through (never reached in fixed region)
    }
};
static void reset() { gFlipLatchWaits = gFlipLatchTimeouts = gFlipLatchMaxUs = gFlipLatchLate = gFlipLatchDeferred = 0; gFlipLatch = 1; gSleeps = 0; gLongestSleep = 0; }
int main() {
    // Deferred (debug.nvrmfb_flip_latch=2, #300): the submit returns without waiting; the next submit verifies the flip.
    { Kms k; k.mode=COMPLETE; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset(); gFlipLatch = 2;
      assert(f.waitComplete(false, true) == NV_TRUE);
      assert(gFlipLatchDeferred == 1 && gFlipLatchWaits == 0 && gSleeps == 0 && "deferred mode must not wait for the flip"); }
    // A completed flip: one wait recorded, no timeout, a duration captured.
    { Kms k; k.mode=COMPLETE; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      assert(f.waitComplete(false, true) == NV_TRUE);
      assert(gFlipLatchWaits == 1 && "a completed flip must count as one latch wait");
      assert(gFlipLatchTimeouts == 0);
      assert(gFlipLatchMaxUs >= 1 && "the wait duration must be recorded"); }
    // The bound follows the mode's raster and pixel clock, not a constant.
    { FB f;
      f.mode(148500000, 2200, 1125); const uint64_t hz60 = f.flipWaitUs();
      f.mode(405000000, 2200, 1115); const uint64_t hz165 = f.flipWaitUs();
      f.mode(237600000, 4400, 2250); const uint64_t hz24 = f.flipWaitUs();
      f.mode(0, 0, 0);               const uint64_t none = f.flipWaitUs();
      printf("flip wait: 60 Hz %llu us, 165 Hz %llu us, 24 Hz %llu us, no timings %llu us\n",
             (unsigned long long)hz60, (unsigned long long)hz165, (unsigned long long)hz24, (unsigned long long)none);
      assert(hz60 >= 20500 && hz60 <= 21500 && "60 Hz: one period and a quarter is about 20.8 ms");
      assert(hz165 >= 7000 && hz165 <= 8200 && "165 Hz: about 7.6 ms, a 60 Hz constant would hold the frame three periods");
      assert(hz24 >= 50000 && hz24 <= 54000 && "24 Hz: about 52 ms, a 100 ms bound was never the panel's period either");
      assert(none == hz60 && "without timings the 60 Hz default period paces the wait"); }
    // 540 Hz: the bound is about 2.3 ms, not the fallback timer's 2 ms floor (which would make it 20.8 ms).
    { FB f; f.mode(1199880000, 2000, 1111); const uint64_t hz540 = f.flipWaitUs();
      printf("flip wait: 540 Hz %llu us\n", (unsigned long long)hz540);
      assert(hz540 >= 2200 && hz540 <= 2400 && "540 Hz: one period and a quarter is about 2.3 ms"); }
    // A flip that lands well past one period on the frame path: counted late once, and submitConfig returns only after
    // it is on the panel (the surface it replaces may be scanned out until then, and the caller completes the transaction).
    { Kms k; k.mode=LATE; k.lateFor = 200; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      const int64_t t0 = gClock;
      assert(f.waitComplete(false, true) == NV_TRUE && "a late flip is not a fault");
      const int64_t took = gClock - t0;
      printf("late flip on the frame path: returned after %lld us, %d polls (landed at poll %d)\n", (long long)(took / 1000), k.polls, k.lateFor + 1);
      assert(k.polls > k.lateFor && "the frame path returned while its flip was still pending: the old surface may still be on the panel");
      assert(gFlipLatchTimeouts == 1 && !f.fSubmissionFaulted && "a late flip counts once as late");
      assert(f.faultLogs == 0 && "a late flip that lands is not a head fault and leaves no fault line");
      assert(gLongestSleep <= 250000 && "a sleep longer than 250 us between pending checks"); }
    // A flip that never lands faults at the 100 ms stuck limit, on the frame path and on a mode set alike.
    for (int frame = 0; frame < 2; frame++) {
      Kms k; k.mode=STUCK; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      const int64_t t0 = gClock;
      assert(f.waitComplete(false, frame != 0) == NV_FALSE);
      const int64_t took = gClock - t0;
      printf("stuck flip (%s): faulted after %lld us\n", frame ? "frame" : "mode set", (long long)(took / 1000));
      assert(gFlipLatchTimeouts == 1 && f.fSubmissionFaulted);
      assert(f.faultLogs == 1 && f.faultWhy[0] == 's' && "the fault leaves exactly one line, naming the submitted flip");
      assert(took >= 100000000 && took < 125000000 && "a stuck flip faults at the stuck limit, not before"); }
    // A prior flip still landing: this present is skipped as IN_PROGRESS within one period, the head is not faulted.
    { Kms k; k.mode=STUCK; FB f; f.fKms=&k; f.mode(405000000, 2200, 1115); reset();
      f.fSubmissionSeen = true; f.fFlipSubmitAbs = mach_absolute_time(); Rep rep;
      const int64_t t0 = gClock;
      assert(f.priorWait(&rep, true) == NV_FALSE);
      const int64_t took = gClock - t0;
      printf("late prior flip at 165 Hz: present skipped after %lld us\n", (long long)(took / 1000));
      assert(rep.flipResult == NV_KMS_FLIP_RESULT_IN_PROGRESS && "a late prior flip answers IN_PROGRESS, not a fault");
      assert(!f.fSubmissionFaulted && gFlipLatchLate == 1);
      assert(took < 9000000 && "WindowServer's present is held at most one period and a quarter"); }
    // A prior flip still pending 100 ms after its submit is a stuck display: the head faults as before.
    { Kms k; k.mode=STUCK; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      f.fSubmissionSeen = true; f.fFlipSubmitAbs = mach_absolute_time() - 150000000ull; Rep rep;
      assert(f.priorWait(&rep, true) == NV_FALSE);
      assert(f.fSubmissionFaulted && gFlipLatchLate == 0 && "a flip pending 150 ms after its submit faults, it is not merely late");
      assert(f.faultLogs == 1 && f.faultWhy[0] == 'p' && "the fault leaves exactly one line, naming the prior flip"); }
    // A prior flip that lands late is waited for by a mode set (no present to skip) and then accepted.
    { Kms k; k.mode=LATE; k.lateFor = 200; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      f.fSubmissionSeen = true; f.fFlipSubmitAbs = mach_absolute_time(); Rep rep;
      assert(f.priorWait(&rep, false) == NV_TRUE && !f.fSubmissionFaulted && "a mode set waits a late flip out");
      assert(gFlipLatchLate == 0 && "a mode set's wait is not a skipped present"); }
    // Knob off: the wait is bypassed and no counter moves.
    { Kms k; k.mode=STUCK; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset(); gFlipLatch=0;
      assert(f.waitComplete(false, true) == NV_TRUE);
      assert(gFlipLatchWaits == 0 && gFlipLatchTimeouts == 0 && !f.fSubmissionFaulted); }
    // Bootstrap commit: no wait, no counters.
    { Kms k; k.mode=STUCK; FB f; f.fKms=&k; f.mode(148500000, 2200, 1125); reset();
      assert(f.waitComplete(true, false) == NV_TRUE);
      assert(gFlipLatchWaits == 0); }
    printf("OK\n");
    return 0;
}
'''


def build_and_run(region_text):
    src = (fixture.replace('WAIT_REGION', region_text).replace('PRIOR_REGION', prior_region)
           .replace('STATICS', statics).replace('WAITER', body).replace('VBL_BODY', vbl_body)
           .replace('WAIT_US_BODY', wait_us_body).replace('PRIOR_BODY', prior_body).replace('STUCK_LINE', stuck_line)
           .replace('INVALIDATE_BODY', lift_body('void NVRMNVDAFramebuffer::invalidateLanding()')))
    with tempfile.TemporaryDirectory() as d:
        cpp = pathlib.Path(d) / 'tc.cpp'; binp = pathlib.Path(d) / 'tc'
        cpp.write_text(src)
        subprocess.run(['c++', '-std=c++17', '-O0', str(cpp), '-o', str(binp)], check=True)
        return subprocess.run([str(binp)], capture_output=True, text=True)


r = build_and_run(region)
assert r.returncode == 0 and r.stdout.strip().endswith('OK'), \
    f'fixed wait failed: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}'
for line in r.stdout.strip().splitlines()[:-1]:
    print(line)
print('fixed source: PASS (one-period steps from the mode; returns only once the flip landed; a late prior flip costs a present; 100 ms faults)')

r = build_and_run(control)
assert r.returncode != 0, 'negative control passed -- the test does not detect the constant 100 ms hold'
print('negative control (constant 100 ms region): FAILS as required')
r = build_and_run(early_control)
assert r.returncode != 0, 'negative control passed -- the test does not detect a frame completed before its flip landed'
print('negative control (frame let go before its flip landed): FAILS as required')
# The fault line carries what a log reader needs to tell a stuck head from a late one: the time since the submit and the
# latch counters. A line that names only the head cannot say whether the 100 ms limit was reached.
helper = source[source.index('void NVRMNVDAFramebuffer::flipFaultLog'):]
helper = helper[:helper.index('\n}\n')]
for needle in ('us since its submit', 'latch waits %d timeouts %d late %d max %d us', 'gFlipLatchTimeouts', 'gFlipLatchMaxUs'):
    assert needle in helper, f'the head-fault line lost {needle!r}'
assert source.count('presentation blocked') == 1, 'a head fault logs outside flipFaultLog'
print('head-fault line: carries time since submit and the latch counters')
# Run the actual platform pure branch -> whole flipToMemory -> whole submitConfig, not a model of the gate.
# A real second thread presents through the other head while the first sleeps. Deterministic hooks also force
# notifier/STOP/power races. The old production path holds the recursive gate and starves the second thread.
sc_body = lift_body('NvBool NVRMNVDAFramebuffer::submitConfig(')
flip_body = lift_body('bool NVRMNVDAFramebuffer::flipToMemory(')
ungated_body = lift_body('bool NVRMNVDAFramebuffer::waitLandedUngated(')
platform_start = source.index('    if (functionName && functionName->isEqualTo("NVRMGpuPower"))')
platform_power = source[platform_start:source.index('    if (functionName && functionName->isEqualTo(NVRM_VRAM_FN_SCANOUT))', platform_start)]
platform_start = source.index('    if (functionName && functionName->isEqualTo("nvFlipToSurfacePure"))')
platform_pure = source[platform_start:source.index('    if (functionName && functionName->isEqualTo(NVRM_FLIP_FN))', platform_start)]
stop_start = source.index('void NVRMNVDAFramebuffer::stop(IOService *provider)')
stop_prefix = source[source.index('{', stop_start) + 1:source.index('    if (fAudioReady)', stop_start)]

# These are lifetime assumptions of the private wait, checked against the shipped teardown, not silently modelled.
stop_body = lift_body('void NVRMNVDAFramebuffer::stop(IOService *provider)')
assert not any(x in stop_body for x in ('destroySurface(', 'freeMemory(', 'freeDevice(')), 'STOP must preserve scanout pins'
assert 'freeDevice(' not in source, 'a new device teardown needs gate + landing-epoch invalidation review'
rm = (root / 'kexts/NVRM/NVRM.cpp').read_text()
suspend = rm[rm.index('void NVRM::gpuSuspend()'):rm.index('void NVRM::gpuResume()')]
assert suspend.index('notifyFramebuffers(0)') < suspend.index('nvkms_xnu_suspend('), 'invalidate before suspend resets notifier'
assert 'static int gFlipUngated = 1;' in source, 'ungated landing wait is the validated default (165 Hz + 60 Hz hardware A/B)'
assert 'home ? nullptr : &token' in platform_pure and 'pure && mem ? defer : nullptr' in flip_body
assert 'defer && frame && !bootstrap' in sc_body, 'bootstrap and mode sets must retain gated waits'

extra = r'''
#include <cstring>
#include <atomic>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <chrono>
using UInt64=uint64_t; using SInt64=int64_t; using NvU16=uint16_t; using NvU8=uint8_t;
using IOReturn=int;
enum { kIOReturnSuccess=0, kIOReturnBusy=1, kIOReturnIOError=2, kIOReturnBadArgument=3, kIOReturnNoMemory=4 };
thread_local int gGateDepth=0;
int gGatedSleeps=0, gUngatedSleeps=0;
static std::recursive_mutex gDisplayMutex;
void (*gSleepHook)() = nullptr;
struct NVRMDisplayGate {
    NVRMDisplayGate() { gDisplayMutex.lock(); ++gGateDepth; }
    ~NVRMDisplayGate() { --gGateDepth; gDisplayMutex.unlock(); }
    bool valid() { return true; }
};
struct NVRMCallReference { template<class T> explicit NVRMCallReference(T *) {} };
struct OSSymbol { const char *s; bool isEqualTo(const char *t) const { return !strcmp(s,t); } };
struct NvKmsKapiMemory { int refs=0; };
struct NvKmsKapiSurface { NvKmsKapiMemory *pin=nullptr; };
struct NvKmsKapiLayerConfig {
    NvKmsKapiSurface *surface=nullptr; unsigned srcWidth=0,srcHeight=0,srcX=0,srcY=0,dstWidth=0,dstHeight=0;
    int dstX=0,dstY=0; struct { int compMode=0; } compParams; NvU8 minPresentInterval=0; bool tearing=false;
};
struct NvKmsKapiLayerRequestedConfig { NvKmsKapiLayerConfig config;
    struct { bool surfaceChanged=false,srcXYChanged=false,srcWHChanged=false,dstXYChanged=false,dstWHChanged=false,cscChanged=false; } flags; };
struct NvKmsKapiHeadRequestedConfig {
    struct { bool bActive=false; unsigned numDisplays=0,displays[8]={}; struct ModeData { NvKmsKapiDisplayModeTimings timings; } mode; } modeSetConfig;
    struct { bool activeChanged=false,displaysChanged=false,modeChanged=false,olutFpNormScaleChanged=false; } flags;
    NvKmsKapiLayerRequestedConfig layerRequestedConfig[2];
    struct { NvKmsKapiSurface *surface=nullptr; struct { bool surfaceChanged=false; } flags; } cursorRequestedConfig;
};
struct NvKmsKapiRequestedModeSetConfig { unsigned headsMask=0; NvKmsKapiHeadRequestedConfig headRequestedConfig[8]; };
struct NvKmsKapiModeSetReplyConfig { NvKmsFlipResult flipResult=NV_KMS_FLIP_RESULT_SUCCESS; };
struct NvKmsKapiCreateSurfaceParams { struct { NvKmsKapiMemory *memory=nullptr; unsigned offset=0,pitch=0; } planes[1]; unsigned width=0,height=0; int format=0; };
enum { NVKMS_KAPI_LAYER_MAX=2, NVKMS_COMPOSITION_BLENDING_MODE_OPAQUE=0, NvKmsSurfaceMemoryFormatX8R8G8B8=0, kFlipEpochLimit=100 };
static UInt64 readFlipEpoch() { return 1; }
static void nvrmHeadConfigBaseline(NvKmsKapiHeadRequestedConfig *) {}
static void *IOMalloc(size_t n) { return calloc(1,n); } static void IOFree(void *p,size_t) { free(p); }
static void nvu_zero(void *p,size_t n) { memset(p,0,n); }
static SInt64 OSAddAtomic64(SInt64 v,SInt64 *p) { SInt64 o=*p; *p+=v; return o; }
static bool OSCompareAndSwap(int o,int n,volatile SInt32 *p) { if (*p!=o) return false; *p=n; return true; }
using IOInterruptState=int; using IOSimpleLock=int;
static IOInterruptState IOSimpleLockLockDisableInterrupt(IOSimpleLock *) { return 0; }
static void IOSimpleLockUnlockEnableInterrupt(IOSimpleLock *, IOInterruptState) {}
static struct { UInt64 size(NvKmsKapiMemory *m,UInt64 c) { return m && c==7 ? 4096 : 0; } } gAllocations;
static int gFlipUngated=1, gFlipInterval=1, gFlipLean=1;
static SInt64 gFlipUsSum=0,gFlipN=0,gFlipUsMax=0;
static const unsigned NVRM_FLIP_ABI_VERSION=1;
struct NVRMFlipRequest { unsigned version=1, flags=0; void *kapiMemory=nullptr; unsigned width=8,height=8,pitch=32; int flipResult=-1; UInt64 allocationCookie=7; };
'''

kms_extra = r'''
    int applies=0, destroys=0, frees=0; bool reject=false,stallAfterApply=false;
    NvBool applyModeSetConfig(void *, NvKmsKapiRequestedModeSetConfig *, NvKmsKapiModeSetReplyConfig *r,NvBool commit) {
        assert(gGateDepth==1); if (!commit) return true;
        ++applies; polls=0; // production KAPI advances/resets the live notifier even on rejection
        if (stallAfterApply) mode=STUCK;
        r->flipResult=reject ? NV_KMS_FLIP_RESULT_INVALID_PARAMS : NV_KMS_FLIP_RESULT_SUCCESS;
        return !reject;
    }
    NvKmsKapiMemory *dupMemory(void *,void *,NvKmsKapiMemory *m) { ++m->refs; return m; }
    NvKmsKapiSurface *createSurface(void *,NvKmsKapiCreateSurfaceParams *p) { auto *s=new NvKmsKapiSurface; s->pin=p->planes[0].memory; return s; }
    void destroySurface(void *,NvKmsKapiSurface *s) { ++destroys; delete s; }
    void freeMemory(void *,NvKmsKapiMemory *m) { ++frees; --m->refs; }
'''

fb_extra = r'''
    struct NVFlipToken { uint64_t epoch=0,sequence=0,submitAbs=0,boundUs=0; };
    struct NVFlipEntry { NvKmsKapiMemory *mem=nullptr; NvKmsKapiSurface *surf=nullptr; unsigned w=0,h=0,pitch=0; UInt64 epoch=0,cookie=0; NvKmsKapiMemory *pin=nullptr; };
    NVFlipEntry fFlipCache[16]; IOSimpleLock *fFlipCacheLock=nullptr;
    unsigned fFlipCacheN=0,fFlipCount=0,fFlipRejects=0,fRejected=0,fHomePure=0;
    unsigned fW=8,fH=8,fPitch=32,fRasterW=8,fRasterH=8,fDisplay=0,fIndex=0;
    bool fModeSet=true,fFirstPrimaryLogged=false,fHeadSlept=false,fSysSlept=false;
    uint64_t fEnableAbs=0; NvKmsKapiSurface *fSurf=nullptr,*fFrontSurf=nullptr;
    volatile SInt32 fServerFresh=0; unsigned fTransform=0,fCurTransform=0;
    struct { void (*proc)(void *,void *)=nullptr; void *target=nullptr,*ref=nullptr; } fConnect;
    void *fGpuWake=nullptr;
    int retains=0; void retain() { ++retains; }
    void scheduleCall(void *,unsigned) {}
    void setProperty(const char *,unsigned long long,unsigned) {}
    NvBool submitConfig(NvKmsKapiRequestedModeSetConfig *cfg,NvKmsKapiModeSetReplyConfig *rep,NvBool commit,
                        bool bootstrap=false,bool cursorOnly=false,bool frame=false,NVFlipToken *defer=nullptr) { SUBMIT_BODY }
    bool flipToMemory(NvKmsKapiMemory *mem,unsigned w,unsigned h,unsigned pitch,int *rcOut,bool pure=false,UInt64 cookie=0,NVFlipToken *defer=nullptr) { FLIP_BODY }
    bool waitLandedUngated(const NVFlipToken &token) { UNGATED_BODY }
    IOReturn platform(const OSSymbol *functionName,void *param1) { POWER_BRANCH PURE_BRANCH return kIOReturnBadArgument; }
    void stop() { STOP_PREFIX }
'''

regression_main = r'''
static FB *gTarget=nullptr,*gOther=nullptr;
static Kms *gTargetKms=nullptr;
static NVRMFlipRequest gOtherReq;
static OSSymbol gPure{"nvFlipToSurfacePure"}, gPower{"NVRMGpuPower"};
static int gAction=0,gProgress=0;
static void interleave() {
    gSleepHook=nullptr;
    assert(gGateDepth==0 && "the mixed head must run while the first waiter sleeps with ZERO outer gate");
    if (gAction==0) { assert(gOther->platform(&gPure,&gOtherReq)==kIOReturnSuccess); ++gProgress; }
    if (gAction==1 || gAction==2) {
        NVRMDisplayGate gate;
        gTargetKms->mode=COMPLETE;
        assert(gTarget->waitFlipDone(0)); // another submit's priorFlipState observes completion under the gate
        gTargetKms->stallAfterApply=true; gTargetKms->reject=(gAction==2);
        NvKmsKapiRequestedModeSetConfig c; NvKmsKapiModeSetReplyConfig r;
        c.headRequestedConfig[0].modeSetConfig.bActive=true;
        NvKmsKapiSurface s; c.headRequestedConfig[0].layerRequestedConfig[0].config.surface=&s;
        FB::NVFlipToken next;
        auto ok=gTarget->submitConfig(&c,&r,true,false,false,true,&next);
        assert(ok==(gAction==1)); // next accepted pending notifier OR rejected never-written notifier
    }
    if (gAction==3) gTarget->stop();
    if (gAction==4) assert(gTarget->platform(&gPower,(void *)0)==kIOReturnSuccess);
    if (gAction==5) { NVRMDisplayGate gate; gTarget->fDev=(void *)0x1234; } // stale live fDev must not be polled
    if (gAction==6) { NVRMDisplayGate gate; gTarget->fSubmissionFaulted=true; }
    if (gAction==7) { // complete, then STOP: STOP is not landing even if the completed watermark covers the token
        { NVRMDisplayGate gate; gTargetKms->mode=COMPLETE; assert(gTarget->waitFlipDone(0)); }
        gTarget->stop();
    }
    if (gAction==8) { // suspend -> resume; the new device's empty notifier cannot resolve an old hole
        gTarget->platform(&gPower,(void *)0); gTarget->platform(&gPower,(void *)1); gTargetKms->mode=COMPLETE;
    }
    if (gAction==9) { // landed after the last poll: observe before invalidation, do not permanently fault
        gTargetKms->mode=COMPLETE;
        gTarget->platform(&gPower,(void *)0);
        assert(!gTarget->fSubmissionFaulted);
    }
}
static void setup(FB &f,Kms &k,NvKmsKapiMemory &m,NVRMFlipRequest &r) {
    f.fKms=&k; f.mode(405000000,2200,1115); r.kapiMemory=&m;
    k.mode=LATE; k.lateFor=8;
    gGateDepth=gGatedSleeps=gUngatedSleeps=0; gFlipUngated=1; gFlipLatch=1;
    gFlipLatchWaits=gFlipLatchTimeouts=gFlipLatchMaxUs=0;
    gSleepHook=nullptr;
}
static std::mutex gProgressMutex;
static std::condition_variable gProgressCV;
static void threadedPresent() {
    gSleepHook=nullptr;
    bool done=false;
    std::thread peer([&] {
        assert(gOther->platform(&gPure,&gOtherReq)==kIOReturnSuccess);
        { std::lock_guard<std::mutex> lock(gProgressMutex); done=true; }
        gProgressCV.notify_one();
    });
    std::unique_lock<std::mutex> lock(gProgressMutex);
    const bool progressed=gProgressCV.wait_for(lock,std::chrono::seconds(1),[&] { return done; });
    assert(progressed && "other head's lifted production call blocked behind the first head's outer gate");
    lock.unlock(); peer.join();
    assert(gGateDepth==0 && "pure waiter slept holding an outer recursive gate");
    ++gProgress;
}
int main() {
    // Actual mutex + two threads: a real other-head platform submission must finish BEFORE this waiter resumes.
    {
        FB f,other; Kms k,ko; NvKmsKapiMemory m,mo; NVRMFlipRequest r,ro;
        setup(f,k,m,r); setup(other,ko,mo,ro); ko.mode=COMPLETE;
        gOther=&other; gOtherReq=ro; gProgress=0; gSleepHook=threadedPresent;
        assert(f.platform(&gPure,&r)==kIOReturnSuccess);
        assert(gProgress==1 && gGatedSleeps==0 && gUngatedSleeps>0);
    }
    for (int action=0;action<=9;++action) {
        FB f,other; Kms k,ko; NvKmsKapiMemory m,mo; NVRMFlipRequest r,ro;
        setup(f,k,m,r); setup(other,ko,mo,ro); ko.mode=COMPLETE;
        gTarget=&f; gOther=&other; gTargetKms=&k; gOtherReq=ro; gAction=action; gProgress=0;
        gSleepHook=interleave;
        auto rr=f.platform(&gPure,&r);
        const bool good=action<=2;
        assert((rr==kIOReturnSuccess)==good);
        assert(r.flipResult==(good ? (int)NV_KMS_FLIP_RESULT_SUCCESS : -1));
        assert(gGateDepth==0 && gGatedSleeps==0 && gUngatedSleeps>=1);
        if (action==0) assert(gProgress==1 && "the other actual platform present stalled behind this head");
        if (action==1 || action==2) assert(f.fCompleted==1 && "completed token was lost when the notifier advanced");
        if (action==3 || action==4 || action==8) assert(f.fSubmissionFaulted && "an invalidated hole must not be bridged");
        if (!good) assert(m.refs==1 && k.destroys==0 && k.frees==0 && "failure released the FB pin while scanout may still use it");
    }
    // Default-off bootstrap/deferred submits may be physically landed but never observed, or still pending.
    // Suspend must not turn either legacy path into a permanent experimental-handoff fault.
    for (int latch : {0, 2}) for (bool pending : {false, true}) {
        FB f; Kms k; NvKmsKapiMemory m; NVRMFlipRequest r; setup(f,k,m,r);
        gFlipUngated=0; gFlipLatch=latch;
        assert(f.platform(&gPure,&r)==kIOReturnSuccess);
        k.mode=pending ? STUCK : COMPLETE;
        assert(f.platform(&gPower,(void *)0)==kIOReturnSuccess);
        assert(!f.fSubmissionFaulted && "legacy deferred submit permanently faulted at standby");
        f.platform(&gPower,(void *)1); k.mode=COMPLETE; gFlipLatch=1;
        assert(f.platform(&gPure,&r)==kIOReturnSuccess);
    }
    // Disabled, HOME and modeset-like paths stay gated; old debug latch modes never request the new handoff.
    for (int arm=0;arm<5;++arm) {
        FB f; Kms k; NvKmsKapiMemory m; NVRMFlipRequest r; setup(f,k,m,r);
        NvKmsKapiSurface home; f.fSurf=&home;
        if (arm==0) gFlipUngated=0;
        if (arm==1) r.flags=1;
        if (arm==2) { int rc; assert(f.flipToMemory(&m,8,8,32,&rc,false,7)); }
        else { if (arm>=3) gFlipLatch=arm==3 ? 0 : 2; assert(f.platform(&gPure,&r)==kIOReturnSuccess); }
        assert(gUngatedSleeps==0);
        assert((gGatedSleeps>0)==(arm<3));
    }
    // Late and stuck waits retain existing counters and pinning, never turn timeout into success or BUSY.
    for (int stuck=0;stuck<2;++stuck) {
        FB f; Kms k; NvKmsKapiMemory m; NVRMFlipRequest r; setup(f,k,m,r);
        k.mode=stuck ? STUCK : LATE; k.lateFor=100;
        auto t=mach_absolute_time(); auto rr=f.platform(&gPure,&r);
        assert(rr==(stuck ? kIOReturnIOError : kIOReturnSuccess));
        assert(gFlipLatchWaits==1 && gFlipLatchTimeouts==1 && gGatedSleeps==0);
        if (stuck) {
            assert(f.fSubmissionFaulted && f.fCompleted==0 && m.refs==1 && k.frees==0 && k.destroys==0);
            assert((mach_absolute_time()-t)/1000>=100000);
            assert(f.platform(&gPure,&r)==kIOReturnIOError && k.applies==1);
        } else assert(f.fCompleted==1 && gFlipLatchMaxUs>7570);
    }
    // An unresolved prior submission never gets a second token; wrap fails before KAPI can reset a notifier.
    for (int wrap=0;wrap<2;++wrap) {
        FB f; Kms k; NvKmsKapiMemory m; NVRMFlipRequest r; setup(f,k,m,r);
        f.fSubmitted=wrap ? ~0ull : 1; f.fCompleted=wrap ? ~0ull : 0;
        f.fSubmissionSeen=!wrap; f.fSubmissionDevice=f.fDev; f.fFlipSubmitAbs=mach_absolute_time(); k.mode=STUCK;
        assert(f.platform(&gPure,&r)==(wrap ? kIOReturnIOError : kIOReturnBusy));
        assert(k.applies==0);
    }
    puts("OK lifted platform -> flipToMemory -> submitConfig: mixed-head progress, notifier advance/rejection, STOP/power/device/fault, gated HOME/modes, late/stuck pins");
}
'''


def run_production_handoff(sc_text=sc_body, waiter_text=body):
    # Reuse the real one-period helpers and OS clock from the earlier fixture, but replace KAPI with a resettable notifier.
    text = fixture[:fixture.index('static void reset()')]
    text = text.replace('#include <cstdio>', '#include <cstdio>\n#include <cstdlib>\n#include <cstring>')
    # The extra types need the timing/result types, but must precede Kms.
    timing = 'struct NvKmsKapiDisplayModeTimings { uint32_t pixelClockHz=0,hTotal=0,vTotal=0; };'
    text = text.replace(timing, '').replace('enum Mode {', timing + '\n' + extra + '\nenum Mode {')
    text = text.replace('    Mode mode = COMPLETE;', kms_extra + '\n    Mode mode = COMPLETE;')
    text = text.replace('        if (mode == FAIL)', '        assert(gGateDepth==1);\n        if (mode == FAIL)')
    text = text.replace('    int faultLogs = 0;', fb_extra + '\n    int faultLogs = 0;')
    text = text.replace('struct { NvKmsKapiDisplayModeTimings timings; } fMode;', 'NvKmsKapiHeadRequestedConfig::modeSetConfig_not_a_type fMode;')
    text = text.replace('NvKmsKapiHeadRequestedConfig::modeSetConfig_not_a_type', 'decltype(NvKmsKapiHeadRequestedConfig().modeSetConfig.mode)')
    # Every real sleep tries to schedule the other head; it is blocked if a recursive display gate remains held.
    text = text.replace('    gSleeps++;', '    if (gGateDepth) ++gGatedSleeps; else ++gUngatedSleeps; if (gSleepHook) gSleepHook();\n    gSleeps++;')
    # Sleep stubs precede the extra types: declarations only, definitions arrive with extra.
    text = text.replace('static int gSleeps = 0;', 'extern thread_local int gGateDepth; extern int gGatedSleeps,gUngatedSleeps; extern void (*gSleepHook)();\nstatic int gSleeps = 0;')
    for key, value in [('SUBMIT_BODY', sc_text), ('FLIP_BODY', flip_body), ('UNGATED_BODY', ungated_body),
                       ('POWER_BRANCH', platform_power), ('PURE_BRANCH', platform_pure), ('STOP_PREFIX', stop_prefix),
                       ('INVALIDATE_BODY', lift_body('void NVRMNVDAFramebuffer::invalidateLanding()')),
                       ('WAIT_REGION', region), ('PRIOR_REGION', prior_region), ('STATICS', statics), ('WAITER', waiter_text),
                       ('VBL_BODY', vbl_body), ('WAIT_US_BODY', wait_us_body), ('PRIOR_BODY', prior_body), ('STUCK_LINE', stuck_line)]:
        text = text.replace(key, value)
    text += regression_main
    with tempfile.TemporaryDirectory() as d:
        cpp = pathlib.Path(d) / 'handoff.cpp'; exe = pathlib.Path(d) / 'handoff'
        cpp.write_text(text)
        c = subprocess.run(['c++', '-std=c++17', '-O0', '-pthread', '-Wno-everything', str(cpp), '-o', str(exe)], capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True)


r = run_production_handoff()
assert r.returncode == 0, f'lifted production handoff failed: {r.stdout}\n{r.stderr}'
print(r.stdout.strip())
# Restore the exact old gated production submit flow: no output token, so flipToMemory waits under its real gate.
old_submit = sc_body.replace('if (defer && frame && !bootstrap && gFlipUngated && gFlipLatch == 1)', 'if (false)')
r = run_production_handoff(old_submit)
assert r.returncode != 0, 'old production gated path passed: the mixed-head regression is blind'
print('negative control (old actual gated submit path): FAILS as required')
no_publication = body.replace('if (fSubmissionSeen) fCompleted = fSubmitted;', '')
assert no_publication != body
r = run_production_handoff(waiter_text=no_publication)
assert r.returncode != 0, 'live-notifier-only wait passed after another submit reset the notifier'
print('negative control (no completion publication before notifier reset): FAILS as required')
print('PASS')
