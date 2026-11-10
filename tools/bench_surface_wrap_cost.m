// bench_surface_wrap_cost: CPU cost of wrapping IOSurfaces in fresh MTLTextures (what Core Animation does in WindowServer),
// with a GPU clear through every 30th wrapper and a pixel read back through a new wrapper and through the CPU.
// xcrun clang -fobjc-arc -O1 tools/bench_surface_wrap_cost.m -framework Foundation -framework Metal -framework IOSurface -o /tmp/bsw
// /tmp/bsw [wrappers]; A/B: NVMTL_PROCESS_ENV=1 NVMTL_NO_VRAM_IMPORT_CACHE=1 /tmp/bsw
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOSurface/IOSurface.h>
#include <time.h>
static void clear(id<MTLCommandQueue> q, id<MTLTexture> x, double r, double g) {
    id<MTLCommandBuffer> cb = [q commandBuffer];
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.colorAttachments[0].texture = x; rp.colorAttachments[0].loadAction = MTLLoadActionClear;
    rp.colorAttachments[0].storeAction = MTLStoreActionStore; rp.colorAttachments[0].clearColor = MTLClearColorMake(r, g, 0, 1);
    [[cb renderCommandEncoderWithDescriptor:rp] endEncoding]; [cb commit]; [cb waitUntilCompleted];
    if (cb.error) { printf("command buffer error\n"); exit(1); }
}
int main(int argc, char **argv) { @autoreleasepool {
    id<MTLDevice> d = MTLCreateSystemDefaultDevice(); printf("%s\n", d.name.UTF8String);
    // BSW_SURFACES=n (default 8, at most 256) cycles the wrappers over n surfaces: more live surfaces than the import cache
    // holds shows its eviction cost (a desktop has many windows, layers and browser tiles).
    enum { NSMAX = 256 }; IOSurfaceRef s[NSMAX];
    const char *ns_env = getenv("BSW_SURFACES"); int NS = ns_env ? atoi(ns_env) : 8;
    if (NS < 1 || NS > NSMAX) { printf("BSW_SURFACES must be 1..%d\n", NSMAX); return 2; }
    const int SW = NS > 16 ? 512 : 1920, SH = NS > 16 ? 512 : 1080;
    for (int i = 0; i < NS; i++)
        s[i] = IOSurfaceCreate((__bridge CFDictionaryRef)@{ (id)kIOSurfaceWidth: @(SW), (id)kIOSurfaceHeight: @(SH),
            (id)kIOSurfaceBytesPerElement: @4, (id)kIOSurfaceBytesPerRow: @(SW * 4), (id)kIOSurfacePixelFormat: @((unsigned)'BGRA') });
    MTLTextureDescriptor *t = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:SW height:SH mipmapped:NO];
    t.usage = MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget; t.storageMode = MTLStorageModeManaged;
    id<MTLCommandQueue> q = [d newCommandQueue];
    const int N = argc > 1 ? atoi(argv[1]) : 3000;
    for (int pass = 0; pass < 2; pass++) {   // the first pass warms up; the second is reported
        struct timespec c0, c1; clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &c0); uint64_t w0 = clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);
        for (int i = 0; i < N; i++) @autoreleasepool {
            id<MTLTexture> x = [d newTextureWithDescriptor:t iosurface:s[i % NS] plane:0];
            if (!x) { printf("nil texture\n"); return 1; }
            if (i % 30 == 0) clear(q, x, 0, (i % 255) / 255.0);
        }
        clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &c1); uint64_t w1 = clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);
        const double cpu = (c1.tv_sec - c0.tv_sec) * 1e6 + (c1.tv_nsec - c0.tv_nsec) / 1e3;
        if (pass) printf("wrappers %d: %.1f us CPU/wrapper, %.1f us wall/wrapper\n", N, cpu / N, (w1 - w0) / 1e3 / N);
    }
    id<MTLTexture> x = [d newTextureWithDescriptor:t iosurface:s[0] plane:0]; clear(q, x, 1, 0); x = nil;
    id<MTLTexture> y = [d newTextureWithDescriptor:t iosurface:s[0] plane:0];
    uint32_t px = 0; [y getBytes:&px bytesPerRow:SW * 4 fromRegion:MTLRegionMake2D(5, 5, 1, 1) mipmapLevel:0];
    IOSurfaceLock(s[0], kIOSurfaceLockReadOnly, NULL); uint32_t cpx = ((uint32_t *)IOSurfaceGetBaseAddress(s[0]))[5 * SW + 5];
    IOSurfaceUnlock(s[0], kIOSurfaceLockReadOnly, NULL);
    const int ok = px == 0xffff0000u && cpx == 0xffff0000u;
    printf("pixel via new wrapper %08x, via CPU %08x -> %s\n", px, cpx, ok ? "OK" : "MISMATCH");
    return ok ? 0 : 1;
} }
