// Device/library/pipeline/first-render timing in a fresh process; not whole-app launch timing.
// xcrun clang -fobjc-arc -O1 tools/bench_metal_load.m -framework Foundation -framework Metal -framework IOKit -o /tmp/bml
// /tmp/bml [salt=1.0] [--async]; NM_TEST_PLUGIN and NM_TEST_ICD select isolated candidates.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOKit/IOKitLib.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <dlfcn.h>
#include <math.h>
#include <time.h>
static double ms(uint64_t a, uint64_t b) { return (b - a) / 1e6; }
static uint64_t now(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }
static void fail(NSString *s) { fprintf(stderr, "FAIL: %s\n", s.UTF8String); exit(2); }
int main(int argc, char **argv) { @autoreleasepool {
    const char *salt = "1.0"; BOOL async = NO;
    for (int i = 1; i < argc; i++) { if (!strcmp(argv[i], "--async")) async = YES; else salt = argv[i]; }
    char *end = NULL; double scale = strtod(salt, &end);
    if (!end || *end || !isfinite(scale) || scale <= 0 || scale > 2) fail(@"salt must be a finite number in (0,2]");
    const char *plugin = getenv("NM_TEST_PLUGIN"), *icd = getenv("NM_TEST_ICD");
    uint64_t t0 = now();
    if ((plugin || icd) && !dlopen(plugin ?: "/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver", RTLD_NOW | RTLD_LOCAL)) fail(@(dlerror()));
    if (icd) setenv("NVMTL_ICD_LIB", icd, 1); // After the release constructor forgets inherited knobs.
    id<MTLDevice> d = nil;
    if (plugin) {
        io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
        if (!svc) fail(@"no NVAccel service");
        d = ((id(*)(id, SEL, mach_port_t))objc_msgSend)([NSClassFromString(@"NVMTLDevice") alloc], NSSelectorFromString(@"initWithAcceleratorPort:"), svc);
        IOObjectRelease(svc);
    } else d = MTLCreateSystemDefaultDevice();
    if (!d) fail(@"no Metal device");
    if (plugin && strcmp(class_getImageName(object_getClass(d)), plugin)) fail(@"requested plugin did not create the device");
    if (icd && !dlopen(icd, RTLD_LAZY | RTLD_NOLOAD)) fail(@"requested ICD is not mapped");
    uint64_t t1 = now();
    NSString *src = [NSString stringWithFormat:@"#include <metal_stdlib>\nusing namespace metal;\nstruct V{float4 p[[position]];float2 uv;};\n"
        "vertex V vs(uint i[[vertex_id]]){V o;float2 q=float2((i<<1)&2,i&2);o.p=float4(q*2-1,0,1);o.uv=q;return o;}\n"
        "fragment float4 fs(V v[[stage_in]],texture2d<float> t[[texture(0)]],sampler s[[sampler(0)]]){return t.sample(s,v.uv)*%.17g;}\n", scale];
    __block NSError *error = nil; __block id<MTLLibrary> library = nil;
    uint64_t submitted;
    if (async) {
        dispatch_semaphore_t done = dispatch_semaphore_create(0);
        [d newLibraryWithSource:src options:nil completionHandler:^(id<MTLLibrary> lib, NSError *e) { library = lib; error = e; dispatch_semaphore_signal(done); }];
        submitted = now();
        if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC))) fail(@"library callback timed out");
    } else { library = [d newLibraryWithSource:src options:nil error:&error]; submitted = now(); }
    uint64_t t2 = now();
    if (!library) fail(error.description ?: @"library creation failed");
    MTLRenderPipelineDescriptor *p = [MTLRenderPipelineDescriptor new];
    p.vertexFunction = [library newFunctionWithName:@"vs"]; p.fragmentFunction = [library newFunctionWithName:@"fs"];
    p.colorAttachments[0].pixelFormat = MTLPixelFormatRGBA8Unorm;
    id<MTLRenderPipelineState> pipeline = [d newRenderPipelineStateWithDescriptor:p error:&error];
    uint64_t t3 = now();
    if (!pipeline) fail(error.description ?: @"pipeline creation failed");
    MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:1 height:1 mipmapped:NO];
    td.storageMode = MTLStorageModeShared; td.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> texture = [d newTextureWithDescriptor:td];
    const uint8_t rgba[4] = {64, 96, 128, 255};
    [texture replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0 withBytes:rgba bytesPerRow:4];
    td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm width:4 height:4 mipmapped:NO];
    td.storageMode = MTLStorageModePrivate; td.usage = MTLTextureUsageRenderTarget;
    id<MTLTexture> target = [d newTextureWithDescriptor:td];
    id<MTLBuffer> readback = [d newBufferWithLength:1024 options:MTLResourceStorageModeShared];
    MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new]; sd.minFilter = sd.magFilter = MTLSamplerMinMagFilterNearest;
    id<MTLSamplerState> sampler = [d newSamplerStateWithDescriptor:sd];
    id<MTLCommandQueue> queue = [d newCommandQueue];
    if (!texture || !target || !readback || !sampler || !queue) fail(@"render allocation failed");
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = target; pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore; pass.colorAttachments[0].clearColor = MTLClearColorMake(0,0,0,0);
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [cb renderCommandEncoderWithDescriptor:pass];
    [encoder setRenderPipelineState:pipeline]; [encoder setFragmentTexture:texture atIndex:0]; [encoder setFragmentSamplerState:sampler atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [encoder endEncoding];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    [blit copyFromTexture:target sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(4,4,1)
                 toBuffer:readback destinationOffset:0 destinationBytesPerRow:256 destinationBytesPerImage:1024];
    [blit endEncoding]; [cb commit]; [cb waitUntilCompleted];
    if (cb.status != MTLCommandBufferStatusCompleted) fail(cb.error.description ?: @"render command failed");
    uint64_t t4 = now(); const uint8_t *pixels = readback.contents;
    for (unsigned y = 0; y < 4; y++) for (unsigned x = 0; x < 4; x++) for (unsigned c = 0; c < 4; c++)
        if (abs(pixels[y * 256 + x * 4 + c] - (int)lround(fmin(255, rgba[c] * scale))) > 1) fail(@"first-frame pixel mismatch");
    NSDictionary *result = @{@"device":d.name, @"async":@(async), @"device_ms":@(ms(t0,t1)), @"library_submit_ms":@(ms(t1,submitted)),
        @"library_total_ms":@(ms(t1,t2)), @"pipeline_ms":@(ms(t2,t3)), @"render_ms":@(ms(t3,t4)), @"first_render_ms":@(ms(t0,t4)), @"verified":@YES};
    NSData *json = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingSortedKeys error:nil];
    fwrite(json.bytes, 1, json.length, stdout); putchar('\n'); return 0;
} }
