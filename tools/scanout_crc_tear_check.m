// Sample display-engine CRCs for scanout anomalies; this does not certify absence of tearing or panel flicker.
// A borderless window covers the selected screen. Set debug.nvaccel_crc_head to its corresponding head separately.
// Learn CRCs of held greys, then animate them while reading the CRC. Reads can block and miss many refreshes.
// Unknown CRCs require investigation (including overlays/cursor). A stale frame can retain a learned CRC;
// neither sampled CRCs nor compositor callbacks prove every physical refresh or detect panel-side flicker.
// Keep the mouse still during the run (the cursor is part of the CRC). Greys differ by 2/255: no visible flashing.
// xcrun clang -fobjc-arc -O1 tools/scanout_crc_tear_check.m -framework Cocoa -framework QuartzCore -o /tmp/crc_check && /tmp/crc_check [seconds]
#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>
#include <sys/sysctl.h>
#include <stdatomic.h>
enum { NG = 6, MAXV = 4096 };
static unsigned crc(void) { int v = 0; size_t n = sizeof v; return sysctlbyname("debug.nvaccel_crc", &v, &n, NULL, 0) ? 0 : (unsigned)v; }
// CRC collection can itself stall NVKMS. Stop rather than manufacture hundreds of display timeouts while claiming a clean test.
static int64_t counter(const char *name) {
    uint64_t v=0; size_t n=sizeof v;
    if (sysctlbyname(name,&v,&n,NULL,0) || (n!=4 && n!=8) || v>INT64_MAX) return -1;
    return (int64_t)v;
}
static int64_t counter_step(int64_t before, int64_t after) { return before<0 || after<before ? -1 : after-before; }
static void pump(double s) { [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:s]]; }
static unsigned vals[MAXV]; static unsigned long cnt[MAXV]; static int nv;
static _Atomic int running;
static void note(unsigned v) { for (int i = 0; i < nv; i++) if (vals[i] == v) { cnt[i]++; return; } if (nv < MAXV) { vals[nv] = v; cnt[nv++] = 1; } }
int main(int argc, char **argv) { @autoreleasepool {
    double secs = argc > 1 ? atof(argv[1]) : 5;
    if (!isfinite(secs) || secs < 1 || secs > 120) { fprintf(stderr, "seconds must be 1..120\n"); return 2; }
    const int64_t timeoutsBefore=counter("debug.nvrmfb_flip_latch_timeouts");
    const int64_t surfaceBefore=counter("debug.nvaccel_surface_gpu_timeouts");
    if (!crc()) { printf("debug.nvaccel_crc unavailable (0)\n"); return 2; }
    int head = 0; size_t hn = sizeof head; sysctlbyname("debug.nvaccel_crc_head", &head, &hn, NULL, 0);
    [NSApplication sharedApplication]; [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    NSScreen *s = NSScreen.mainScreen; const char *want = getenv("WL_SCREEN");
    for (NSScreen *c in NSScreen.screens)
        if (want ? strstr(c.localizedName.UTF8String, want) != NULL : c.maximumFramesPerSecond > s.maximumFramesPerSecond) s = c;
    NSWindow *w = [[NSWindow alloc] initWithContentRect:s.frame styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO screen:s];
    [w setFrame:s.frame display:YES]; w.level = NSScreenSaverWindowLevel; w.opaque = YES; w.hasShadow = NO;
    NSView *v = [[NSView alloc] initWithFrame:(NSRect){ NSZeroPoint, s.frame.size }]; v.wantsLayer = YES; w.contentView = v;
    [w orderFrontRegardless];
    // the cursor goes to another screen so it is not in the frame
    for (NSScreen *c in NSScreen.screens) if (c != s) { CGWarpMouseCursorPosition(CGPointMake(NSMidX(c.frame), 50)); break; }
    CGColorRef *g = calloc(NG, sizeof *g); for (int i = 0; i < NG; i++) g[i] = CGColorCreateGenericGray((96 + 2 * i) / 255.0, 1);
    printf("screen %s %.0f Hz, CRC head %d\n", s.localizedName.UTF8String, (double)s.maximumFramesPerSecond, head);
    unsigned learned[NG];
    for (int k = 0; k < NG; k++) {
        [CATransaction begin]; [CATransaction setDisableActions:YES]; v.layer.backgroundColor = g[k]; [CATransaction commit];
        pump(0.25);
        nv = 0; for (int j = 0; j < 40; j++) {
            note(crc()); usleep(2000);
            int64_t dt=counter_step(timeoutsBefore,counter("debug.nvrmfb_flip_latch_timeouts"));
            int64_t ds=counter_step(surfaceBefore,counter("debug.nvaccel_surface_gpu_timeouts"));
            if (dt>0 || ds>0) {
                printf("INCONCLUSIVE: timeout counters advanced during CRC probing (flip %lld, surface %lld); aborting intrusive measurement\n",(long long)dt,(long long)ds);
                [w orderOut:nil]; return 2;
            }
        }
        int best = 0; for (int i = 1; i < nv; i++) if (cnt[i] > cnt[best]) best = i;
        learned[k] = vals[best];
        printf("grey %d: crc %08x (%lu of 40 reads agree)\n", k, learned[k], cnt[best]);
    }
    for (int a = 0; a < NG; a++) for (int b = a + 1; b < NG; b++) if (learned[a] == learned[b]) { printf("two greys share a CRC - inconclusive\n"); return 2; }
    nv = 0; memset(cnt, 0, sizeof cnt); atomic_store(&running, 1);
    __block unsigned long reads = 0;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0), ^{
        unsigned last = 0;
        while (atomic_load(&running)) { unsigned c = crc(); reads++; if (c != last) { note(c); last = c; } usleep(150); }
        dispatch_semaphore_signal(done);
    });
    __block int k = 0; __block unsigned long shown = 0;
    CADisplayLink *dl = [v displayLinkWithTarget:[NSBlockOperation blockOperationWithBlock:^{
        [CATransaction begin]; [CATransaction setDisableActions:YES]; k = (k + 1) % NG; v.layer.backgroundColor = g[k]; [CATransaction commit]; shown++;
    }] selector:@selector(main)];
    [dl addToRunLoop:[NSRunLoop mainRunLoop] forMode:NSRunLoopCommonModes];
    pump(secs); [dl invalidate]; atomic_store(&running, 0); dispatch_semaphore_wait(done, DISPATCH_TIME_FOREVER);
    unsigned long known = 0, unknown = 0; int distinctUnknown = 0;
    for (int i = 0; i < nv; i++) {
        int hit = 0; for (int j = 0; j < NG; j++) if (vals[i] == learned[j]) hit = 1;
        if (hit) known += cnt[i]; else { unknown += cnt[i]; distinctUnknown++; if (distinctUnknown <= 8) printf("  unknown crc %08x seen %lu time(s)\n", vals[i], cnt[i]); }
    }
    printf("animated %.1f s: %lu frames committed (%.0f/s), %lu CRC reads, %lu frame changes observed: %lu complete greys, %lu unknown (%d distinct)\n",
           secs, shown, shown / secs, reads, known + unknown, known, unknown, distinctUnknown);
    printf("CRC sample rate %.1f/s; callbacks are not confirmed physical frames. Head-to-screen mapping must be checked.\n", reads / secs);
    int64_t timeoutDelta=counter_step(timeoutsBefore,counter("debug.nvrmfb_flip_latch_timeouts"));
    int64_t surfaceDelta=counter_step(surfaceBefore,counter("debug.nvaccel_surface_gpu_timeouts"));
    printf("During CRC probing: flip timeout delta %lld; surface timeout delta %lld (-1 = unavailable/reset)\n",(long long)timeoutDelta,(long long)surfaceDelta);
    const int intrusive=timeoutDelta>0 || surfaceDelta>0;
    printf("%s\n", intrusive ? "INCONCLUSIVE: timeout counters advanced during intrusive CRC collection; do not use this run as latency/smoothness qualification" : unknown ? "ANOMALY: unlearned CRCs observed; inspect tearing, overlays and cursor before attributing cause" :
           known < 2 ? "INCONCLUSIVE: too few CRC changes observed" : "NO ANOMALY OBSERVED in sampled CRCs; unsampled refreshes and panel-side flicker remain untested");
    [w orderOut:nil];
    return unknown ? 1 : intrusive || known < 2 ? 2 : 0;
} }
