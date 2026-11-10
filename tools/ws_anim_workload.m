// Display-paced compositor workload; callback cadence is not proof of physical presentation.
// xcrun clang -fobjc-arc -O1 tools/ws_anim_workload.m -framework Cocoa -framework QuartzCore -o /tmp/ws_anim_workload
#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>
static double intervals[65536];
static unsigned nintervals;
static int compare(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }
@interface V : NSView
@property double t0, last;
@property NSPoint origin;
@property unsigned long frames;
@end
@implementation V
- (void)tick:(CADisplayLink *)link {
    double now = CACurrentMediaTime(), t = now - self.t0;
    if (self.last && nintervals < sizeof intervals / sizeof *intervals) intervals[nintervals++] = (now - self.last) * 1000;
    self.last = now; self.frames++;
    [CATransaction begin]; [CATransaction setDisableActions:YES];
    self.layer.backgroundColor = [NSColor colorWithHue:fmod(t * 0.5, 1) saturation:0.8 brightness:0.9 alpha:1].CGColor;
    [self.window setFrameOrigin:NSMakePoint(self.origin.x + 200 * (1 + sin(t * 2)), self.origin.y)];
    [CATransaction commit];
}
@end
int main(int argc, char **argv) { @autoreleasepool {
    double secs = argc > 1 ? atof(argv[1]) : 10;
    if (!isfinite(secs) || secs < 1 || secs > 120) { fprintf(stderr, "seconds must be 1..120\n"); return 2; }
    [NSApplication sharedApplication]; [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    NSScreen *s = NSScreen.mainScreen; const char *want = getenv("WL_SCREEN");
    for (NSScreen *c in NSScreen.screens)
        if (want ? strstr(c.localizedName.UTF8String, want) != NULL : c.maximumFramesPerSecond > s.maximumFramesPerSecond) s = c;
    if (!s) { fprintf(stderr, "no display\n"); return 2; }
    NSRect r = NSMakeRect(NSMinX(s.frame) + 100, NSMinY(s.frame) + 100, 800, 600);
    NSWindow *w = [[NSWindow alloc] initWithContentRect:r styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
    V *v = [[V alloc] initWithFrame:NSMakeRect(0, 0, 800, 600)]; v.wantsLayer = YES; v.t0 = CACurrentMediaTime(); v.origin = r.origin;
    w.contentView = v; w.level = NSFloatingWindowLevel; [w orderFrontRegardless];
    CGDirectDisplayID display = [s.deviceDescription[@"NSScreenNumber"] unsignedIntValue];
    CGDisplayModeRef mode = CGDisplayCopyDisplayMode(display);
    double hz = mode ? CGDisplayModeGetRefreshRate(mode) : 0; if (mode) CGDisplayModeRelease(mode);
    CADisplayLink *link = [v displayLinkWithTarget:v selector:@selector(tick:)];
    [link addToRunLoop:[NSRunLoop mainRunLoop] forMode:NSRunLoopCommonModes];
    printf("screen %s mode %.3f Hz, maximum %ld Hz\n", s.localizedName.UTF8String, hz, (long)s.maximumFramesPerSecond); fflush(stdout);
    [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:secs]];
    [link invalidate];
    double elapsed = CACurrentMediaTime() - v.t0;
    qsort(intervals, nintervals, sizeof *intervals, compare);
    printf("display callbacks %lu (%.1f/s), interval p50 %.3f ms p95 %.3f ms max %.3f ms (software cadence, not scanout)\n", v.frames, v.frames / elapsed,
           nintervals ? intervals[nintervals / 2] : 0, nintervals ? intervals[(nintervals - 1) * 95 / 100] : 0, nintervals ? intervals[nintervals - 1] : 0);
    [w orderOut:nil];
    return v.frames > 1 ? 0 : 2;
} }
