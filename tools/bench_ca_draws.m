// bench_ca_draws: a CoreAnimation-shaped load for measuring the plugin's CPU cost per draw and per frame.
// Each frame clears a 1920x1080 target and draws <draws> blended quads; every draw binds a different pair of
// textures and alternates two samplers, which is what WindowServer does while compositing windows, shadows and
// the cursor. Runs entirely in this process: it never touches WindowServer or the installed driver files.
//
// build: xcrun clang -fobjc-arc -O2 -o bench_ca_draws tools/bench_ca_draws.m -framework Metal -framework Foundation -framework IOKit
// run:   ./bench_ca_draws <path to NVMTLDriver> [frames=600] [draws=120]
//        Compare two plugins by running both, alternating, several times; single runs are noisy.
//        NVMTL_ENC78_CENSUS=1 adds the plugin's descriptor-set census on stderr.
//        <plugin> --resset [iterations=5000000] measures only production resource-set adds, no device/GPU.
//        Compare original/candidate with separate explicit-plugin processes; alternate run order.
//        This synthetic CPU test does not establish WindowServer or application performance.
#import <Metal/Metal.h>
#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <objc/message.h>
#include <IOKit/IOKitLib.h>
#include <sys/resource.h>
#include <math.h>

// Read back the final frame outside the timing interval. Every visible quad has a
// known opaque colour; checking each centre catches stale texture/table snapshots.
static int verify_pixels(id<MTLDevice> d, id<MTLCommandQueue> q, id<MTLTexture> rt, int frame, int draws) {
    const NSUInteger stride = 1920 * 4;
    id<MTLBuffer> readback = [d newBufferWithLength:stride * 1080 options:MTLResourceStorageModeShared];
    if (!readback) return 11;
    id<MTLCommandBuffer> cb = [q commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:rt sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
                 sourceSize:MTLSizeMake(1920,1080,1) toBuffer:readback destinationOffset:0
                 destinationBytesPerRow:stride destinationBytesPerImage:stride*1080];
    [blit endEncoding]; [cb commit]; [cb waitUntilCompleted];
    if (cb.status != MTLCommandBufferStatusCompleted) return 12;
    const uint8_t *pixels = readback.contents;
    unsigned checked = 0;
    for (int k = 0; k < draws; k++) {
        float x = -0.925f + (k % 12) * 0.16f, y = -0.915f + (k / 12) * 0.18f;
        if (x <= -1 || x >= 1 || y <= -1 || y >= 1) continue;
        unsigned px = (unsigned)((x + 1) * 960), py = (unsigned)((1 - y) * 540);
        uint32_t a = 0xff000000u | (uint32_t)(((k + frame) % 48) * 5000);
        uint32_t b = 0xff000000u | (uint32_t)(((k * 7 + frame) % 48) * 5000);
        for (unsigned channel = 0; channel < 4; channel++) {
            int expected = (int)lround((((a >> (channel*8)) & 255) + ((b >> (channel*8)) & 255)) * 0.5);
            int actual = pixels[py*stride + px*4 + channel];
            if (abs(actual - expected) > 1) {
                fprintf(stderr,"READBACK FAIL draw=%d pixel=%u,%u channel=%u expected=%d actual=%d\n",k,px,py,channel,expected,actual);
                return 13;
            }
        }
        checked++;
    }
    // The rightmost columns are outside all quads and must retain the clear colour.
    for (unsigned channel = 0; channel < 4; channel++)
        if (pixels[100*stride + 1919*4 + channel] != 0) {
            fprintf(stderr,"READBACK FAIL clear channel=%u expected=0 actual=%u\n",channel,pixels[100*stride + 1919*4 + channel]);
            return 14;
        }
    if (!checked) return 15;
    printf("READBACK verified=%u clear=1 tolerance=1\n",checked);
    return 0;
}

static double cpu_s(void) {
    struct rusage r; getrusage(RUSAGE_SELF, &r);
    return r.ru_utime.tv_sec + r.ru_stime.tv_sec + (r.ru_utime.tv_usec + r.ru_stime.tv_usec) / 1e6;
}

static int bench_resset(int argc, char **argv) {
    Class cls = NSClassFromString(@"NVMTLResSet");
    if (!cls) { fprintf(stderr, "plugin has no NVMTLResSet\n"); return 9; }
    const unsigned long n = argc > 3 ? strtoul(argv[3], NULL, 10) : 5000000;
    if (!n) return 2;
    // 48 identities repeatedly overflow the eight-entry MRU; prepopulate to isolate existing-member misses.
    NSMutableArray *pool = [NSMutableArray new];
    NSMutableSet *set = [cls new];
    for (unsigned i=0; i<48; i++) { id o=[NSObject new]; [pool addObject:o]; [set addObject:o]; }
    double cpu[2]={0}, wall[2]={0};
    // Interleave small hit/miss chunks in alternating order to reduce clock/thermal drift between controls.
    for (unsigned long base=0, chunk=0; base<n; chunk++) {
        unsigned long count=MIN(n-base, 100000ul);
        for (unsigned pass=0; pass<2; pass++) {
            unsigned mru=(pass+chunk)&1;
            double c0=cpu_s(), w0=CFAbsoluteTimeGetCurrent();
            for (unsigned long i=0; i<count; i++) [set addObject:pool[i % (mru ? 4 : 48)]];
            cpu[mru]+=cpu_s()-c0; wall[mru]+=CFAbsoluteTimeGetCurrent()-w0;
        }
        base+=count;
    }
    for (unsigned mru=0; mru<2; mru++) {
        printf("RESSET plugin=%s CF=%s mode=%s iterations=%lu cpu_ns/add=%.3f wall_ns/add=%.3f count=%lu\n",
               argv[1], getenv("NVMTL_RESSET_CF") ?: "unset", mru ? "mru-hit" : "mru-miss", n,
               cpu[mru]*1e9/n, wall[mru]*1e9/n, (unsigned long)set.count);
        if (set.count != 48) return 10;
    }
    return 0;
}

int main(int argc, char **argv) { @autoreleasepool {
    if (argc < 2) { fprintf(stderr, "usage: %s <NVMTLDriver> [frames] [draws]\n", argv[0]); return 2; }
    if (!dlopen(argv[1], RTLD_NOW | RTLD_LOCAL)) { fprintf(stderr, "%s\n", dlerror()); return 3; }
    if (argc > 2 && !strcmp(argv[2], "--resset")) return bench_resset(argc, argv);
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
    if (!svc) { fprintf(stderr, "no NVAccel service\n"); return 4; }
    id<MTLDevice> d = ((id(*)(id, SEL, mach_port_t))objc_msgSend)([NSClassFromString(@"NVMTLDevice") alloc],
                                                                     NSSelectorFromString(@"initWithAcceleratorPort:"), svc);
    IOObjectRelease(svc);
    if (!d) { fprintf(stderr, "NVMTLDevice init failed\n"); return 5; }

    NSString *src = @"#include <metal_stdlib>\nusing namespace metal;\n"
        "struct V { float4 p [[position]]; float2 t; };\n"
        "vertex V vs(uint i [[vertex_id]], constant float4 &r [[buffer(0)]]) {\n"
        "  float2 c = float2(i & 1, i >> 1); V o; o.p = float4(mix(r.xy, r.zw, c), 0, 1); o.t = c; return o; }\n"
        "fragment float4 fs(V v [[stage_in]], texture2d<float> a [[texture(0)]], texture2d<float> b [[texture(1)]],\n"
        "                   sampler s [[sampler(0)]]) { return a.sample(s, v.t) * 0.5 + b.sample(s, v.t) * 0.5; }\n";
    NSError *e = nil;
    id<MTLLibrary> lib = [d newLibraryWithSource:src options:nil error:&e];
    if (!lib) { NSLog(@"library: %@", e); return 6; }
    MTLRenderPipelineDescriptor *pd = [MTLRenderPipelineDescriptor new];
    pd.vertexFunction = [lib newFunctionWithName:@"vs"]; pd.fragmentFunction = [lib newFunctionWithName:@"fs"];
    pd.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
    pd.colorAttachments[0].blendingEnabled = YES;
    pd.colorAttachments[0].sourceRGBBlendFactor = MTLBlendFactorSourceAlpha;
    pd.colorAttachments[0].destinationRGBBlendFactor = MTLBlendFactorOneMinusSourceAlpha;
    id<MTLRenderPipelineState> ps = [d newRenderPipelineStateWithDescriptor:pd error:&e];
    if (!ps) { NSLog(@"pipeline: %@", e); return 7; }

    enum { NT = 48 };
    NSMutableArray<id<MTLTexture>> *tx = [NSMutableArray new];
    for (int i = 0; i < NT; i++) {
        MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                     width:256 height:256 mipmapped:NO];
        td.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> t = [d newTextureWithDescriptor:td];
        uint32_t row[256]; for (int k = 0; k < 256; k++) row[k] = 0xff000000u | (uint32_t)(i * 5000);
        for (int y = 0; y < 256; y++) [t replaceRegion:MTLRegionMake2D(0, y, 256, 1) mipmapLevel:0 withBytes:row bytesPerRow:1024];
        [tx addObject:t];
    }
    // BENCH_PIXEL_SAMPLER=1: pixel-coordinate samplers, plus one new sampler state per frame, as CoreDisplay makes on every
    // display-pipe commit (exercises the pixel-sampler specialization key and newSamplerStateWithDescriptor:).
    const int pixel = getenv("BENCH_PIXEL_SAMPLER") && !strcmp(getenv("BENCH_PIXEL_SAMPLER"), "1");
    MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
    sd.normalizedCoordinates = !pixel;
    sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterLinear;  id<MTLSamplerState> linear = [d newSamplerStateWithDescriptor:sd];
    sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterNearest; id<MTLSamplerState> nearest = [d newSamplerStateWithDescriptor:sd];
    MTLTextureDescriptor *rd = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                  width:1920 height:1080 mipmapped:NO];
    rd.usage = MTLTextureUsageRenderTarget; rd.storageMode = MTLStorageModePrivate;
    id<MTLTexture> rt = [d newTextureWithDescriptor:rd];
    id<MTLCommandQueue> q = [d newCommandQueue];

    const int frames = argc > 2 ? atoi(argv[2]) : 600, draws = argc > 3 ? atoi(argv[3]) : 120;
    if (frames <= 0 || draws <= 0) return 2;
    for (int pass = 0; pass < 2; pass++) {          // pass 0 warms pipelines and caches
        const int n = pass ? frames : 60;
        double c0 = cpu_s(), w0 = CFAbsoluteTimeGetCurrent();
        id<MTLCommandBuffer> last = nil;
        for (int f = 0; f < n; f++) { @autoreleasepool {
            MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor new];
            rp.colorAttachments[0].texture = rt;
            rp.colorAttachments[0].clearColor = MTLClearColorMake(0,0,0,0);
            rp.colorAttachments[0].loadAction = MTLLoadActionClear;
            rp.colorAttachments[0].storeAction = MTLStoreActionStore;
            id<MTLCommandBuffer> cb = [q commandBuffer];
            id<MTLRenderCommandEncoder> en = [cb renderCommandEncoderWithDescriptor:rp];
            [en setRenderPipelineState:ps];
            if (pixel) { id<MTLSamplerState> perFrame = [d newSamplerStateWithDescriptor:sd]; if (!perFrame) return 9; [en setFragmentSamplerState:perFrame atIndex:0]; }
            for (int k = 0; k < draws; k++) {
                float r[4] = { -1 + (k % 12) * 0.16f, -1 + (k / 12) * 0.18f, -0.85f + (k % 12) * 0.16f, -0.83f + (k / 12) * 0.18f };
                [en setVertexBytes:r length:sizeof r atIndex:0];
                [en setFragmentTexture:tx[(k + f) % NT] atIndex:0];
                [en setFragmentTexture:tx[(k * 7 + f) % NT] atIndex:1];
                [en setFragmentSamplerState:(k & 1) ? linear : nearest atIndex:0];
                [en drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
            }
            [en endEncoding]; [cb commit];
            if (last) {
                [last waitUntilCompleted];     // keep one frame in flight, like a compositor
                if (last.status != MTLCommandBufferStatusCompleted) return 8;
            }
            last = cb;
        } }
        [last waitUntilCompleted];
        if (pass) {
            const double c = cpu_s() - c0, w = CFAbsoluteTimeGetCurrent() - w0;
            printf("RESULT plugin=%s pixel=%d CF=%s frames=%d draws/frame=%d cpu_ms/frame=%.6f us/draw=%.4f wall_ms/frame=%.6f wall_us/draw=%.4f fps=%.1f status=%ld\n",
                   argv[1], pixel, getenv("NVMTL_RESSET_CF") ?: "unset", frames, draws, c * 1000 / frames,
                   c * 1e6 / ((double)frames * draws), w * 1000 / frames, w * 1e6 / ((double)frames * draws), frames / w, (long)last.status);
            if (last.status != MTLCommandBufferStatusCompleted) return 8;
            int verified = verify_pixels(d,q,rt,frames-1,draws);
            if (verified) return verified;
        }
    }
    return 0;
} }
