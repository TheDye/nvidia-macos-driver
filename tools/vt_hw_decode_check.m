// vt_hw_decode_check: which video codecs VideoToolbox reports as hardware-decodable on this Mac, which is what
// Safari, Chrome and YouTube's codec choice depend on. NullMoth registers H.264 and HEVC through NVDEC;
// VP9 and AV1 have no decoder registered (CPU decode).
//
// Compare the first answer with a later sample. VT may cache capabilities before discovering the plugin, so neither
// discovery-time registration nor a later sample guarantees a fresh answer. --plugin explicitly dlopens a candidate
// BEFORE the first query; it does not install it or interpose on VideoToolbox. Decoder dylibs must be adjacent.
//
// build: xcrun clang -fobjc-arc -O1 -o vt_hw_decode_check tools/vt_hw_decode_check.m -framework VideoToolbox -framework CoreMedia -framework AVFoundation -framework Foundation -framework CoreVideo
// run:   ./vt_hw_decode_check [h264|hevc|vp9|av1] [--plugin /path/NVMTLDriver] [--settle-ms 250]
// --decode red-blue.mp4 requires a real hardware session, decodes all frames and checks NV12 pixels
// against a red left half / blue right half fixture. Emits JSON; capability queries alone are not proof.
// NVMTL_EAGER_NVDEC=1 --plugin ... explicitly registers an unsandboxed probe (not browser behavior).
#import <VideoToolbox/VideoToolbox.h>
#import <AVFoundation/AVFoundation.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <dlfcn.h>

typedef struct { unsigned frames, matching, errors; int left[3], right[3]; } DecodeEvidence;
static void decoded(void *ref, void *source, OSStatus status, VTDecodeInfoFlags flags,
                    CVImageBufferRef image, CMTime pts, CMTime duration) {
    DecodeEvidence *e = ref;
    if (status || !image) { e->errors++; return; }
    e->frames++;
    if (CVPixelBufferGetPixelFormatType(image) != kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
        CVPixelBufferGetPlaneCount(image) != 2 || CVPixelBufferLockBaseAddress(image, kCVPixelBufferLock_ReadOnly)) {
        e->errors++; return;
    }
    size_t w = CVPixelBufferGetWidth(image), h = CVPixelBufferGetHeight(image);
    const uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(image, 0), *uv = CVPixelBufferGetBaseAddressOfPlane(image, 1);
    size_t ys = CVPixelBufferGetBytesPerRowOfPlane(image, 0), uvs = CVPixelBufferGetBytesPerRowOfPlane(image, 1);
    size_t x1 = (w / 4) & ~1u, x2 = (3 * w / 4) & ~1u;
    e->left[0] = y[(h / 2) * ys + x1]; e->right[0] = y[(h / 2) * ys + x2];
    e->left[1] = uv[(h / 4) * uvs + x1]; e->left[2] = uv[(h / 4) * uvs + x1 + 1];
    e->right[1] = uv[(h / 4) * uvs + x2]; e->right[2] = uv[(h / 4) * uvs + x2 + 1];
    const int expected[6] = {81, 90, 240, 41, 240, 110};
    int actual[6] = {e->left[0], e->left[1], e->left[2], e->right[0], e->right[1], e->right[2]};
    bool match = true;
    for (int i = 0; i < 6; i++) if (abs(actual[i] - expected[i]) > 12) match = false;
    e->matching += match;
    CVPixelBufferUnlockBaseAddress(image, kCVPixelBufferLock_ReadOnly);
}
static int decode_file(const char *path) {
    @autoreleasepool {
        NSError *error = nil;
        AVURLAsset *asset = [AVURLAsset URLAssetWithURL:[NSURL fileURLWithPath:@(path)] options:nil];
        __block AVAssetTrack *track = nil;
        dispatch_semaphore_t loaded = dispatch_semaphore_create(0);
        [asset loadTracksWithMediaType:AVMediaTypeVideo completionHandler:^(NSArray<AVAssetTrack *> *tracks, NSError *loadError) {
            track = tracks.firstObject; dispatch_semaphore_signal(loaded);
        }];
        if (dispatch_semaphore_wait(loaded, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC))) {
            fprintf(stderr, "video track load timed out\n"); return 2;
        }
        AVAssetReader *reader = track ? [[AVAssetReader alloc] initWithAsset:asset error:&error] : nil;
        AVAssetReaderTrackOutput *output = track ? [[AVAssetReaderTrackOutput alloc] initWithTrack:track outputSettings:nil] : nil;
        if (!reader || !output || ![reader canAddOutput:output]) { fprintf(stderr, "cannot read video track: %s\n", error.description.UTF8String ?: "missing track"); return 2; }
        [reader addOutput:output];
        if (![reader startReading]) { fprintf(stderr, "reader start failed\n"); return 2; }
        CMSampleBufferRef sample = [output copyNextSampleBuffer];
        while (sample && !CMSampleBufferGetFormatDescription(sample)) {
            CFRelease(sample); sample = [output copyNextSampleBuffer];
        }
        if (!sample) { fprintf(stderr, "no compressed samples with a format description\n"); return 2; }
        DecodeEvidence evidence = {0};
        VTDecompressionOutputCallbackRecord cb = {decoded, &evidence};
        NSDictionary *spec = @{(__bridge NSString *)kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder: @YES,
                               (__bridge NSString *)kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder: @YES};
        NSDictionary *attrs = @{(__bridge NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)};
        VTDecompressionSessionRef session = NULL;
        CMFormatDescriptionRef format = (CMFormatDescriptionRef)CFRetain(CMSampleBufferGetFormatDescription(sample));
        CMVideoDimensions dimensions = CMVideoFormatDescriptionGetDimensions(format);
        OSStatus status = VTDecompressionSessionCreate(kCFAllocatorDefault, format,
            (__bridge CFDictionaryRef)spec, (__bridge CFDictionaryRef)attrs, &cb, &session);
        CFTypeRef hardware = NULL;
        OSStatus propertyStatus = session ? VTSessionCopyProperty(session, kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder, NULL, &hardware) : status;
        bool usingHardware = hardware && CFEqual(hardware, kCFBooleanTrue);
        if (hardware) CFRelease(hardware);
        unsigned submitted = 0;
        while (sample) {
            if (!status && CMSampleBufferGetFormatDescription(sample) && CMSampleBufferGetNumSamples(sample)) {
                OSStatus st = VTDecompressionSessionDecodeFrame(session, sample, kVTDecodeFrame_EnableAsynchronousDecompression, NULL, NULL);
                submitted++; if (st) { evidence.errors++; status = st; }
            }
            CFRelease(sample); sample = status ? NULL : [output copyNextSampleBuffer];
        }
        OSStatus finish = 0, wait = 0;
        if (session) { finish = VTDecompressionSessionFinishDelayedFrames(session); wait = VTDecompressionSessionWaitForAsynchronousFrames(session); VTDecompressionSessionInvalidate(session); CFRelease(session); }
        bool pass = !status && !finish && !wait && !propertyStatus && usingHardware && submitted && reader.status == AVAssetReaderStatusCompleted && evidence.frames == submitted && evidence.matching == evidence.frames && !evidence.errors;
        NSDictionary *result = @{ @"pass": @(pass), @"sessionStatus": @(status), @"propertyStatus": @(propertyStatus),
            @"usingHardware": @(usingHardware), @"submitted": @(submitted), @"frames": @(evidence.frames), @"matchingFrames": @(evidence.matching),
            @"errors": @(evidence.errors), @"finishStatus": @(finish), @"waitStatus": @(wait), @"readerStatus": @(reader.status),
            @"mediaType": @(CMFormatDescriptionGetMediaType(format)), @"codec": @(CMFormatDescriptionGetMediaSubType(format)),
            @"width": @(dimensions.width), @"height": @(dimensions.height),
            @"leftYUV": @[@(evidence.left[0]), @(evidence.left[1]), @(evidence.left[2])],
            @"rightYUV": @[@(evidence.right[0]), @(evidence.right[1]), @(evidence.right[2])] };
        NSData *json = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted error:nil];
        fwrite(json.bytes, 1, json.length, stdout); puts("");
        CFRelease(format);
        return pass ? 0 : 1;
    }
}
int main(int argc, char **argv) {
    struct { CMVideoCodecType c; const char *key, *name, *use; } codecs[] = {
        { kCMVideoCodecType_H264, "h264", "H.264", "YouTube with h264ify, most sites" },
        { kCMVideoCodecType_HEVC, "hevc", "HEVC",  "Safari/QuickTime, Apple content" },
        { kCMVideoCodecType_VP9,  "vp9",  "VP9",   "YouTube default up to 4K" },
        { kCMVideoCodecType_AV1,  "av1",  "AV1",   "YouTube, Netflix, Twitch (newer)" },
    };
    const size_t n = sizeof codecs / sizeof codecs[0];
    const char *firstKey = "h264", *plugin = NULL, *decode = NULL;
    unsigned settleMs = 250;
    for (int a = 1; a < argc; a++) {
        if (!strcmp(argv[a], "--decode") && a + 1 < argc) decode = argv[++a];
        else if (!strcmp(argv[a], "--plugin") && a + 1 < argc) plugin = argv[++a];
        else if (!strcmp(argv[a], "--settle-ms") && a + 1 < argc) {
            char *end = NULL;
            long value = strtol(argv[++a], &end, 10);
            if (!*argv[a] || *end || value < 0 || value > 10000) {
                fprintf(stderr, "--settle-ms must be 0..10000\n"); return 2;
            }
            settleMs = (unsigned)value;
        } else if (argv[a][0] != '-') firstKey = argv[a];
        else { fprintf(stderr, "unknown or incomplete option: %s\n", argv[a]); return 2; }
    }
    size_t fi = n;
    for (size_t i = 0; i < n; i++) if (!strcmp(codecs[i].key, firstKey)) fi = i;
    if (fi == n) { fprintf(stderr, "unknown codec: %s\n", firstKey); return 2; }
    if (plugin) {
        if (!dlopen(plugin, RTLD_NOW | RTLD_LOCAL)) {
            fprintf(stderr, "dlopen %s failed: %s\n", plugin, dlerror()); return 2;
        }
        if (!decode) printf("Explicit dlopen before first VT query: %s\n", plugin);
    }
    if (decode) return decode_file(decode);
    const Boolean first = VTIsHardwareDecodeSupported(codecs[fi].c);
    if (settleMs) usleep(settleMs * 1000);
    printf("Later sample after %u ms (VT may retain earlier cached answers).\n", settleMs);

    printf("%-6s %-10s %-10s %s\n", "codec", "first", "settled", "used by");
    int hw = 0;
    for (size_t i = 0; i < n; i++) {
        const Boolean settled = VTIsHardwareDecodeSupported(codecs[i].c);
        hw += settled;
        printf("%-6s %-10s %-10s %s\n", codecs[i].name, i == fi ? (first ? "hardware" : "CPU only") : "-",
               settled ? "hardware" : "CPU only", codecs[i].use);
    }
    if (!first && VTIsHardwareDecodeSupported(codecs[fi].c))
        printf("\nChanged answer: %s gained hardware support after the first query.\n",
               codecs[fi].name);
    return hw ? 0 : 1;
}
