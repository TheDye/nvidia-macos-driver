#!/usr/bin/env python3
"""Exercise production OpenGL descriptor reuse, encoder defaults and resource-cache lifetime without a GPU."""

import pathlib
import re
import subprocess
import tempfile
import os
import sys
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Runnable alternating real-GPU A/B; explicit plugin, no install or WindowServer changes.
# python3 tools/test_opengl_perf.py --snapshot-ab /path/to/candidate [frames=180] [draws=120] [baseline-path]
if '--snapshot-ab' in sys.argv:
    i = sys.argv.index('--snapshot-ab')
    args = sys.argv[i + 1:]
    plugin = args[0]
    frames = args[1] if len(args) > 1 else '180'
    draws = args[2] if len(args) > 2 else '120'
    baseline = args[3] if len(args) > 3 else '/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver'
    arms = [('installed', baseline, 0)] + [(f'candidate{mode}', plugin, mode) for mode in range(3)]
    samples = {label: [] for label, _, _ in arms}
    with tempfile.TemporaryDirectory(prefix='nvmtl-snapshot-ab-') as directory:
        bench = str(pathlib.Path(directory) / 'bench_ca_draws')
        subprocess.run(['xcrun', 'clang', '-arch', 'x86_64', '-fobjc-arc', '-O2', str(ROOT / 'tools/bench_ca_draws.m'),
                        '-framework', 'Metal', '-framework', 'Foundation', '-framework', 'IOKit', '-o', bench], check=True)
        # Eight blocks: each arm occurs twice in every position; reverse the second
        # Latin square to balance ordering. No allocation logging in timed runs.
        orders = [list(range(shift, 4)) + list(range(shift)) for shift in range(4)]
        orders += [list(reversed(order)) for order in orders[:]]
        for block, order in enumerate(orders):
            for arm in order:
                label, path, mode = arms[arm]
                env = dict(os.environ, NVMTL_PROCESS_ENV='1', NVMTL_SNAPSHOT_REUSE=str(mode))
                env.pop('NVMTL_PERF_LOG', None)
                env.pop('NVMTL_ENC78_CENSUS', None)
                run = subprocess.run([bench, path, frames, draws], env=env, text=True, capture_output=True)
                assert run.returncode == 0, run.stdout + run.stderr
                result = re.search(r'cpu_ms/frame=([0-9.]+) us/draw=([0-9.]+) wall_ms/frame=([0-9.]+) wall_us/draw=([0-9.]+).*status=(\d+)', run.stdout)
                assert result and result[5] == '4' and 'READBACK verified=' in run.stdout, run.stdout + run.stderr
                samples[label].append(tuple(map(float, result.group(1, 2, 3, 4))))
                print(f'SNAPSHOT_AB block={block} arm={label} cpu_ms/frame={result[1]} cpu_us/draw={result[2]} wall_ms/frame={result[3]} wall_us/draw={result[4]} readback=PASS', flush=True)
        for label, values in samples.items():
            medians = [statistics.median(column) for column in zip(*values)]
            print(f'SNAPSHOT_MEDIAN arm={label} n={len(values)} cpu_ms/frame={medians[0]:.6f} cpu_us/draw={medians[1]:.4f} wall_ms/frame={medians[2]:.6f} wall_us/draw={medians[3]:.4f} cpu_range={min(v[1] for v in values):.4f}..{max(v[1] for v in values):.4f}', flush=True)
    sys.exit(0)
# The asynchronous library API must not compile on its caller or retain mutable inputs unsnapshotted.
device_source = (ROOT / 'plugin/NVMTLDevice.m').read_text()
async_start = device_source.index('- (void)newLibraryWithSource:(NSString *)src options:(MTLCompileOptions *)opt completionHandler:')
async_end = device_source.index('\n- (id)targetDeviceArchitecture', async_start)
async_method = device_source[async_start:async_end]
async_fixture = r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdatomic.h>
static dispatch_semaphore_t entered, allow, returned, callback;
static _Atomic int active, peak, callbacks;
static _Atomic BOOL inputsOK = YES;
static NSOperationQueue *nvmtl_pipeline_compile_queue(void) {
    static NSOperationQueue *q; static dispatch_once_t once;
    dispatch_once(&once, ^{ q = [NSOperationQueue new]; q.maxConcurrentOperationCount = 2; });
    return q;
}
@interface SlowDevice : NSObject
@end
@implementation SlowDevice
- (id<MTLLibrary>)newLibraryWithSource:(NSString *)src options:(MTLCompileOptions *)opt error:(NSError **)err {
    int n = atomic_fetch_add(&active, 1) + 1, old = atomic_load(&peak);
    while (old < n && !atomic_compare_exchange_weak(&peak, &old, n)) {}
    dispatch_semaphore_signal(entered);
    dispatch_semaphore_wait(allow, DISPATCH_TIME_FOREVER);
    if (src && ![src isEqualToString:@"original"]) inputsOK = NO;
    if (opt && ![opt.preprocessorMacros[@"TEST"] isEqual:@1]) inputsOK = NO;
    atomic_fetch_sub(&active, 1);
    if (!src) { if (err) *err = [NSError errorWithDomain:@"fixture" code:7 userInfo:nil]; return nil; }
    return (id<MTLLibrary>)(id)@"library";
}
''' + async_method + r'''
@end
int main(void) { @autoreleasepool {
    entered = dispatch_semaphore_create(0); allow = dispatch_semaphore_create(0);
    returned = dispatch_semaphore_create(0); callback = dispatch_semaphore_create(0);
    SlowDevice *d = [SlowDevice new]; NSMutableString *source = [@"original" mutableCopy];
    MTLCompileOptions *options = [MTLCompileOptions new]; options.preprocessorMacros = @{@"TEST":@1};
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        for (int i = 0; i < 6; i++) [d newLibraryWithSource:source options:options completionHandler:^(id<MTLLibrary> lib, NSError *err) {
            if (!lib || err) inputsOK = NO;
            atomic_fetch_add(&callbacks, 1); dispatch_semaphore_signal(callback);
        }];
        dispatch_semaphore_signal(returned);
    });
    BOOL nonblocking = dispatch_semaphore_wait(returned, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC)) == 0;
    if (nonblocking) { [source setString:@"mutated"]; options.preprocessorMacros = @{@"TEST":@0}; }
    for (int i = 0; i < 8; i++) dispatch_semaphore_signal(allow);
    if (!nonblocking) { fprintf(stderr, "FAIL: async library method blocks its caller on compilation\n"); return 1; }
    for (int i = 0; i < 6; i++) if (dispatch_semaphore_wait(callback, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC))) return 2;
    if (!inputsOK || atomic_load(&callbacks) != 6 || atomic_load(&peak) > 2) return 3;
    [d newLibraryWithSource:nil options:nil completionHandler:^(id<MTLLibrary> lib, NSError *err) {
        if (lib || err.code != 7) inputsOK = NO;
        dispatch_semaphore_signal(callback);
    }];
    if (dispatch_semaphore_wait(callback, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) || !inputsOK) return 4;
    puts("PASS: async library returns before compilation; source/options snapshots, bounded workers, callbacks and errors");
    return 0;
} }
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'async.m').write_text(async_fixture)
    subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-Wno-nullability-completeness', str(path / 'async.m'),
                    '-framework', 'Foundation', '-framework', 'Metal', '-o', str(path / 'async')], check=True)
    subprocess.run([str(path / 'async')], check=True, timeout=15)
# Specialization and mesh asynchronous wrappers must not execute expensive work on the caller either.
object_source = (ROOT / 'plugin/NVMTLObjects.m').read_text()
mesh_source = (ROOT / 'plugin/NVMTLLibraryLoad.m').read_text()
function_start = object_source.index('- (void)newFunctionWithName:(NSString *)name constantValues:')
function_end = object_source.index('- (id<MTLFunction>)newFunctionWithDescriptor:', function_start)
descriptor_start = object_source.index('- (void)newFunctionWithDescriptor:')
descriptor_end = object_source.index('- (NSArray<NSString *> *)externFunctionNames', descriptor_start)
mesh_start = mesh_source.index('- (void)newRenderPipelineStateWithMeshDescriptor:')
mesh_end = mesh_source.index('\n@end', mesh_start)
wrappers_fixture = r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdatomic.h>
static dispatch_semaphore_t allow, returned, done;
static _Atomic int wrong, callbacks;
static NSOperationQueue *nvmtl_pipeline_compile_queue(void) {
    static NSOperationQueue *q;static dispatch_once_t once;dispatch_once(&once,^{q=[NSOperationQueue new];q.maxConcurrentOperationCount=2;});return q;
}
@interface Values : NSObject<NSCopying>
@property int value;
@end
@implementation Values
- (id)copyWithZone:(NSZone *)zone { Values *v=[Values new];v.value=self.value;return v; }
@end
@interface Descriptor : NSObject<NSCopying>
@property(copy) NSString *name;
@property(strong) Values *constantValues;
@end
@implementation Descriptor
- (id)copyWithZone:(NSZone *)zone {Descriptor *d=[Descriptor new];d.name=self.name;d.constantValues=[self.constantValues copy];return d;}
@end
@interface SlowLibrary : NSObject
@end
@implementation SlowLibrary
- (id<MTLFunction>)newFunctionWithName:(NSString *)name constantValues:(MTLFunctionConstantValues *)cv error:(NSError **)error {
    if(!name){if(error)*error=[NSError errorWithDomain:@"fixture" code:7 userInfo:nil];return nil;}
    dispatch_semaphore_wait(allow,DISPATCH_TIME_FOREVER);
    if(![name isEqual:@"original"]||((Values *)(id)cv).value!=7)atomic_fetch_add(&wrong,1);
    return (id<MTLFunction>)(id)@"function";
}
- (id<MTLFunction>)newFunctionWithDescriptor:(id)desc error:(NSError **)error {
    return [self newFunctionWithName:[desc name] constantValues:(id)[desc constantValues] error:error];
}
''' + object_source[function_start:function_end] + object_source[descriptor_start:descriptor_end] + r'''
@end
@interface SlowMesh : NSObject
@end
@implementation SlowMesh
- (id<MTLRenderPipelineState>)newRenderPipelineStateWithMeshDescriptor:(MTLMeshRenderPipelineDescriptor *)md options:(MTLPipelineOption)o reflection:(MTLRenderPipelineReflection **)r error:(NSError **)e {
    dispatch_semaphore_wait(allow,DISPATCH_TIME_FOREVER);
    if(![md.label isEqual:@"original"]||o!=MTLPipelineOptionArgumentInfo)atomic_fetch_add(&wrong,1);
    if(r)*r=(MTLRenderPipelineReflection *)(id)@"reflection";return (id<MTLRenderPipelineState>)(id)@"pipeline";
}
''' + mesh_source[mesh_start:mesh_end] + r'''
@end
int main(void){@autoreleasepool{
    allow=dispatch_semaphore_create(0);returned=dispatch_semaphore_create(0);done=dispatch_semaphore_create(0);
    SlowLibrary *lib=[SlowLibrary new];SlowMesh *mesh=[SlowMesh new];NSMutableString *name=[@"original" mutableCopy];
    Values *cv=[Values new];cv.value=7;Descriptor *desc=[Descriptor new];desc.name=@"original";desc.constantValues=cv;
    MTLMeshRenderPipelineDescriptor *md=[MTLMeshRenderPipelineDescriptor new];md.label=@"original";
    void (^handler)(id<MTLFunction>,NSError *)=^(id<MTLFunction>f,NSError*e){if(!f||e)atomic_fetch_add(&wrong,1);atomic_fetch_add(&callbacks,1);dispatch_semaphore_signal(done);};
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^{
        [lib newFunctionWithName:name constantValues:(id)cv completionHandler:handler];
        [lib newFunctionWithDescriptor:desc completionHandler:handler];
        [mesh newRenderPipelineStateWithMeshDescriptor:md options:MTLPipelineOptionArgumentInfo completionHandler:^(id<MTLRenderPipelineState>p,MTLRenderPipelineReflection*r,NSError*e){if(!p||!r||e)atomic_fetch_add(&wrong,1);atomic_fetch_add(&callbacks,1);dispatch_semaphore_signal(done);}];
        dispatch_semaphore_signal(returned);
    });
    BOOL fast=dispatch_semaphore_wait(returned,dispatch_time(DISPATCH_TIME_NOW,2*NSEC_PER_SEC))==0;
    if(fast){[name setString:@"mutated"];cv.value=99;desc.name=@"mutated";md.label=@"mutated";}
    for(int i=0;i<3;i++)dispatch_semaphore_signal(allow);
    if(!fast){fprintf(stderr,"FAIL: asynchronous specialization/mesh compile blocks its caller\n");return 1;}
    for(int i=0;i<3;i++)if(dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)))return 2;
    if(atomic_load(&wrong)||atomic_load(&callbacks)!=3)return 3;
    [lib newFunctionWithName:nil constantValues:nil completionHandler:^(id<MTLFunction>f,NSError*e){if(f||e.code!=7)atomic_fetch_add(&wrong,1);dispatch_semaphore_signal(done);}];
    if(dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC))||atomic_load(&wrong))return 4;
    puts("PASS: async function/name/descriptor/mesh wrappers return promptly, snapshot inputs and deliver result/error/reflection");return 0;
}}
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'wrappers.m').write_text(wrappers_fixture)
    subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-Wno-deprecated-declarations', str(path / 'wrappers.m'),
                    '-framework', 'Foundation', '-framework', 'Metal', '-o', str(path / 'wrappers')], check=True)
    subprocess.run([str(path / 'wrappers')], check=True, timeout=15)
# Parallel pipeline requests for aliases of one AIR blob must share one decode, without serializing other blobs.
load_source = (ROOT / 'plugin/NVMTLLibraryLoad.m').read_text()
lazy_start = load_source.index('static _Atomic unsigned long gLazyLibs')
lazy_end = load_source.index('static NSDictionary *nvmtl_index_from_record', lazy_start)
lazy_fixture = r'''
#import <Foundation/Foundation.h>
#include <os/lock.h>
#include <stdatomic.h>
#include <unistd.h>
#define nvlog(...) ((void)0)
static _Atomic int decodeCalls[2], failNext, blockDecode, unexpectedValue;
static dispatch_semaphore_t entered, allow;
static NSString *nvmtl_air_to_text(NSData *air) {
    unsigned i = ((const uint8_t *)air.bytes)[0] == 'A' ? 0 : 1;
    atomic_fetch_add(&decodeCalls[i], 1);
    if (atomic_load(&blockDecode)) { dispatch_semaphore_signal(entered); dispatch_semaphore_wait(allow, DISPATCH_TIME_FOREVER); usleep(30000); }
    if (atomic_exchange(&failNext, 0)) return nil;
    return [[NSString alloc] initWithData:air encoding:NSUTF8StringEncoding];
}
''' + load_source[lazy_start:lazy_end] + r'''
static NVMTLLazyAirs *cache(void) {
    return [[NVMTLLazyAirs alloc] initWithSlots:@{@"x":@0,@"alias":@0,@"y":@1}
                                        blobs:@[[@"A" dataUsingEncoding:NSUTF8StringEncoding],[@"B" dataUsingEncoding:NSUTF8StringEncoding]]];
}
int main(void) { @autoreleasepool {
    entered=dispatch_semaphore_create(0); allow=dispatch_semaphore_create(0); atomic_store(&blockDecode,1);
    NVMTLLazyAirs *c=cache(); dispatch_group_t g=dispatch_group_create();
    for(int i=0;i<8;i++) { dispatch_group_enter(g); dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0), ^{
        if(![[c objectForKey:i&1?@"alias":@"x"] isEqual:@"A"]) abort(); dispatch_group_leave(g);
    }); }
    if(dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,2*NSEC_PER_SEC)))return 2;
    usleep(60000); for(int i=0;i<8;i++)dispatch_semaphore_signal(allow);
    if(dispatch_group_wait(g,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)))return 3;
    if(atomic_load(&decodeCalls[0])!=1){fprintf(stderr,"FAIL: one AIR blob decoded %d times for parallel aliases\n",atomic_load(&decodeCalls[0]));return 1;}
    if([c objectForKey:@"absent"]||![[c copy] isEqual:c]||c.count!=3)return 4;
    c=cache();atomic_store(&decodeCalls[0],0);atomic_store(&failNext,1);
    entered=dispatch_semaphore_create(0);allow=dispatch_semaphore_create(0);
    for(int i=0;i<8;i++){dispatch_group_enter(g);dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0), ^{if([c objectForKey:@"x"])atomic_fetch_add(&unexpectedValue,1);dispatch_group_leave(g);});}
    if(dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,2*NSEC_PER_SEC)))return 7;
    usleep(60000);for(int i=0;i<8;i++)dispatch_semaphore_signal(allow);
    if(dispatch_group_wait(g,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC))||atomic_load(&decodeCalls[0])!=1||atomic_load(&unexpectedValue)){
        fprintf(stderr,"FAIL: failed decode was retried by waiters instead of shared (%d attempts)\n",atomic_load(&decodeCalls[0]));return 8;
    }
    atomic_store(&blockDecode,0);if(![[c objectForKey:@"x"] isEqual:@"A"]||atomic_load(&decodeCalls[0])!=2)return 9;
    atomic_store(&blockDecode,1);c=cache(); atomic_store(&decodeCalls[0],0);atomic_store(&decodeCalls[1],0);
    entered=dispatch_semaphore_create(0);allow=dispatch_semaphore_create(0);
    for(int i=0;i<2;i++){dispatch_group_enter(g);dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0), ^{[c objectForKey:i?@"y":@"x"];dispatch_group_leave(g);});}
    BOOL parallel=YES;for(int i=0;i<2;i++)if(dispatch_semaphore_wait(entered,dispatch_time(DISPATCH_TIME_NOW,2*NSEC_PER_SEC)))parallel=NO;
    for(int i=0;i<2;i++)dispatch_semaphore_signal(allow);
    if(dispatch_group_wait(g,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC))||!parallel)return 5;
    atomic_store(&blockDecode,0);atomic_store(&failNext,1);atomic_store(&decodeCalls[0],0);c=cache();
    if([c objectForKey:@"x"]||![[c objectForKey:@"alias"] isEqual:@"A"]||![[c objectForKey:@"x"] isEqual:@"A"]||atomic_load(&decodeCalls[0])!=2)return 6;
    puts("PASS: lazy AIR aliases coalesce one decode; independent blobs progress; failures retry; cached hits persist");return 0;
} }
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'lazy.m').write_text(lazy_fixture)
    subprocess.run(['xcrun', 'clang', '-fobjc-arc', str(path / 'lazy.m'), '-framework', 'Foundation', '-o', str(path / 'lazy')], check=True)
    subprocess.run([str(path / 'lazy')], check=True, timeout=15)
# IOSurface publication/busy keys for the hot two planes must not allocate formatted strings per bind.
key_source = (ROOT / 'plugin/NVMTLDevice.m').read_text()
key_bodies = []
for key_name in ('nvmtl_vram_tag_key', 'nvmtl_busy_key'):
    begin = key_source.index('static CFStringRef ' + key_name + '(')
    end = key_source.index('\n', begin)
    # Existing compact definitions and expanded bodies both end at a closing brace.
    if key_source[begin:end].count('{') != key_source[begin:end].count('}'):
        end = key_source.index('\n}', begin) + 2
    key_bodies.append(key_source[begin:end])
key_fixture = r'''
#import <Foundation/Foundation.h>
#import <IOSurface/IOSurface.h>
#include <stdarg.h>
#include <assert.h>
static unsigned formats;
static CFStringRef counted(CFAllocatorRef a,CFDictionaryRef o,CFStringRef f,...) {
    formats++;va_list ap;va_start(ap,f);CFStringRef s=CFStringCreateWithFormatAndArguments(a,o,f,ap);va_end(ap);return s;
}
#define CFStringCreateWithFormat counted
''' + '\n'.join(key_bodies) + r'''
int main(void){@autoreleasepool{
    NSUInteger planes[]={0,1,2,7,NSUIntegerMax};
    for(unsigned family=0;family<2;family++)for(unsigned i=0;i<5;i++){
        unsigned before=formats;CFStringRef key=family?nvmtl_busy_key(planes[i]):nvmtl_vram_tag_key(planes[i]);
        assert(formats-before==(planes[i]<2?0:1));char text[128],expected[128];assert(CFStringGetCString(key,text,sizeof text,kCFStringEncodingUTF8));
        snprintf(expected,sizeof expected,family?"NVMTLBusyAt:p%lu":"NVMTLVramAt:p%lu",(unsigned long)planes[i]);assert(!strcmp(text,expected));CFRelease(key);
    }
    IOSurfaceRef surface=IOSurfaceCreate((__bridge CFDictionaryRef)@{(id)kIOSurfaceWidth:@1,(id)kIOSurfaceHeight:@1,(id)kIOSurfaceBytesPerElement:@4});assert(surface);
    for(unsigned p=0;p<2;p++){
        CFStringRef key=nvmtl_vram_tag_key(p);IOSurfaceSetValue(surface,key,CFSTR("roundtrip"));CFRelease(key);
        CFStringRef legacy=counted(NULL,NULL,CFSTR("NVMTLVramAt:p%lu"),(unsigned long)p);CFTypeRef value=IOSurfaceCopyValue(surface,legacy);
        assert(value&&CFEqual(value,CFSTR("roundtrip")));CFRelease(value);CFRelease(legacy);
    }
    CFRelease(surface);puts("PASS: hot IOSurface keys avoid formatting, preserve owned returns and legacy property identity; other planes stay distinct");return 0;
}}
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'keys.m').write_text(key_fixture)
    subprocess.run(['xcrun', 'clang', '-fobjc-arc', str(path / 'keys.m'), '-framework', 'Foundation',
                    '-framework', 'IOSurface', '-o', str(path / 'keys')], check=True)
    subprocess.run([str(path / 'keys')], check=True, timeout=15)
vk = (ROOT / 'plugin/nvmtl_vk.c').read_text()
objects = (ROOT / 'plugin/NVMTLObjects.m').read_text()
header = (ROOT / 'plugin/NVMTLObjects.h').read_text()
# The packaged translator moved beside NVMTLDriver; rebuilt plugins must still load it.
loader = (ROOT / 'plugin/NVMTLLibraryLoad.m').read_text()
loader_begin = loader.index('static inline const char *nvmtl_pick_ll(')
loader_end = loader.index('\ntypedef ', loader_begin)
loader_fixture = r'''
#include <assert.h>
#include <string.h>
#define R_OK 4
static const char *available;
static int probe_access(const char *path, int mode) { return strcmp(path, available); }
#define access probe_access
''' + loader[loader_begin:loader_end] + r'''
int main(void) {
    available="/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/libnvmtl_translate.dylib";
    assert(!strcmp(NVMTL_XLATE_LIB,available));
    available="/Library/GPUBundles/nvmtl/libnvmtl_translate.dylib";
    assert(!strcmp(NVMTL_XLATE_LIB,available));
    return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    (path / 'loader.c').write_text(loader_fixture)
    subprocess.run(['xcrun', 'clang', str(path / 'loader.c'), '-o', str(path / 'loader')], check=True)
    subprocess.run([str(path / 'loader')], check=True)
print('PASS: production translator selection supports current bundle and legacy layouts')
# Both plugin builders must link their direct IOKit calls and remain loadable on Sequoia.
for builder in ('build_plugin.sh', 'build_plugin_release.sh'):
    command = (ROOT / 'build' / builder).read_text()
    assert '-framework IOKit' in command, f'{builder}: unresolved direct IOKit calls'
    assert '-framework CoreFoundation' in command, f'{builder}: missing CoreFoundation linkage'
    assert '-mmacosx-version-min=15.0' in command, f'{builder}: plugin must load on macOS 15'


def section(text, start, end):
    return text[text.index(start):text.index(end, text.index(start))]


history_types = section(vk, '#define NVMTL_ADDRESS_HISTORY', 'static int nvmtl_address_snapshot(')
history_helpers = section(vk, 'static nvmtl_descriptor_history *nvmtl_descriptor_history_get(', 'static VkDescriptorSet nvmtl_snapshot_set_c(')
cache = history_helpers + section(vk, 'static VkDescriptorSet nvmtl_snapshot_set_c(', '\nint nvmtl_vk_bind_buffer(')
macros = section(vk, '#define NVMTL_NBUF ', '#define NVMTL_DISPATCH_PC_BYTES')
# The slot type and the handle check come from the plugin: the GL uniform path reads its slot through
# nvmtl_slot_checked, so a hand copy here would test a slot read the plugin no longer does.
slots = '#define nvlog(...) ((void)0)\n' + section(vk, 'enum { NVMTL_SLOT_NONE', '#define NVMTL_EMB_KEYMAX')
fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mach/mach.h>
#include <vulkan/vulkan.h>
#include "nvmtl_vk.h"
''' + macros + r'''
#define NVMTL_E78_HAS(m,i) (!(m) || (((m)[(i)>>6] >> ((i)&63)) & 1))
#define NVMTL_TABLES_PER_PAGE 256
enum { NVMTL_E78_H3, E78C_SETREUSE, E78C_SETNEW };
''' + slots + r'''
static nvk_buffer g_dummyBuf={.buf=(void *)1,.size=256};
typedef struct {
    nvmtl_slot s[2][NVMTL_NBIND];
    uint8_t dirty[2]; VkDescriptorSet last[2]; unsigned last_idx[2]; uint32_t last_hi[2];
    uint64_t lastw[2][4], chg[2][4], lasttb[2][4]; uint32_t lastdyn[2]; void *lasttab[2];
    uint32_t lastgl[2]; VkBuffer lastubuf[2]; VkDeviceSize lasturange[2];
} nvmtl_bindtable;
typedef struct nvmtl_address_page { nvk_buffer buffer; struct nvmtl_address_page *next; } nvmtl_address_page;
''' + history_types + r'''
static int reuse_mode;
static int nvmtl_snapshot_reuse(void) { return reuse_mode; }
static int enabled=1, dynamic=1, snapshots, allocations, copies, fail_snapshot;
static int nvmtl_enc78_on(int flag) { return enabled; }
static int nvmtl_table_ubo(void) { return dynamic; }
static int nvmtl_dirty_only_on(void) { return 1; }
static void nvmtl_enc78_count(int key, int count) {}
static int nvmtl_address_snapshot(nvk_cmdbuf *c, uint32_t which, VkDescriptorBufferInfo *out) {
    snapshots++;
    nvmtl_address_page *page=c->address_pages;
    if (!(c->address_count % NVMTL_TABLES_PER_PAGE)) page->buffer.buf=(void *)((uintptr_t)page->buffer.buf+1);
    *out=(VkDescriptorBufferInfo){(VkBuffer)page->buffer.buf,(c->address_count++ % NVMTL_TABLES_PER_PAGE)*256,256};
    return 0;
}
static VkDescriptorSet nvmtl_snapshot_set_md(nvk_cmdbuf *c, uint32_t which, uint32_t hi,
        const uint64_t *mask, const uint64_t *tb, VkDescriptorSet from, const uint64_t *cm,
        const VkDescriptorBufferInfo *uniform) {
    if (fail_snapshot) return VK_NULL_HANDLE;
    allocations++; copies += from != VK_NULL_HANDLE;
    c->dyn_out[which]=0; c->tab_out[which]=NULL;
    if (NVMTL_E78_HAS(mask,NVMTL_ADDRESS_BINDING)) {
        VkDescriptorBufferInfo ai; assert(!nvmtl_address_snapshot(c,which,&ai));
        c->dyn_out[which]=dynamic ? ai.offset : 0; c->tab_out[which]=dynamic ? (void *)ai.buffer : NULL;
    }
    VkDescriptorSet set=(VkDescriptorSet)(uintptr_t)(100+allocations);
    c->sets[c->nsets++].set=(void *)set;
    return set;
}
static VkDescriptorSet nvmtl_snapshot_set_t(nvk_cmdbuf *c, uint32_t which, uint32_t hi, const uint64_t *tb) {
    return nvmtl_snapshot_set_md(c,which,hi,NULL,tb,VK_NULL_HANDLE,NULL,NULL);
}
'''
# The mask helpers start at nvmtl_spirv_bmask_x's forward declaration: nvmtl_spirv_bmask calls it, and it logs whether
# the framebuffer-fetch set is skipped, so a no-op nvlog stands in for the plugin's logger here.
mask = '#define nvlog(...) ((void)0)\n' + section(vk, 'static int nvmtl_spirv_bmask_x(const void *p, size_t n, uint64_t out[4], int skip_fetch);', '\nenum { NVMTL_SLOT_NONE')
checks = r'''
int main(void) {
    // Bindless heap binding 0 belongs to set 2, not to the stage's buffer 0. Decoration order is arbitrary.
    uint32_t spv[]={0x07230203,0x00010500,0,10,0,
        (4u<<16)|71,7,33,0, (4u<<16)|71,8,33,640,
        (4u<<16)|71,7,34,2, (4u<<16)|71,8,34,0};
    uint64_t used[4]; assert(nvmtl_spirv_bmask(spv,sizeof spv,used));
    assert(!used[0] && used[NVMTL_ADDRESS_BINDING>>6]==(1ull<<(NVMTL_ADDRESS_BINDING&63)));
    spv[16]=0; assert(nvmtl_spirv_bmask(spv,sizeof spv,used) && (used[0]&1));
    spv[5]=0; assert(!nvmtl_spirv_bmask(spv,sizeof spv,used));
    nvmtl_bindtable table={0}; nvmtl_address_page page={.buffer.buf=(void *)20}; nvk_setref sets[64]={0};
    nvk_cmdbuf c={.table=&table,.sets=sets,.address_pages=&page,.address_count=1};
    nvk_pipeline p={.bmask_ok=1};
    p.bmask[NVMTL_ADDRESS_BINDING>>6] |= 1ull<<(NVMTL_ADDRESS_BINDING&63);
    VkDescriptorSet original=nvmtl_snapshot_set_c(&c,0,&p);
    assert(allocations==1 && snapshots==1);
    assert(nvmtl_snapshot_set_c(&c,0,&p)==original && allocations==1 && snapshots==1);
    for (unsigned i=0; i<100; i++) {
        table.dirty[0]=1; table.chg[0][0]=1;
        assert(nvmtl_snapshot_set_c(&c,0,&p)==original && allocations==1);
    }
    assert(snapshots==101 && c.dyn_out[0]==101*256);
    // Rolling to another address-table page must write a new descriptor, not reuse the old page's set.
    c.address_count=256; table.dirty[0]=1; table.chg[0][0]=1;
    VkDescriptorSet rolled=nvmtl_snapshot_set_c(&c,0,&p); assert(rolled!=original && allocations==2);
    // The next pipeline reads a texel buffer where the prior one read an image.
    p.tbany=1; p.tbm[0][0]=1; p.bmask[0] |= 1ull<<NVMTL_NBUF;
    VkDescriptorSet texel=nvmtl_snapshot_set_c(&c,0,&p); assert(texel!=rolled && allocations==3);
    assert(nvmtl_snapshot_set_c(&c,0,&p)==texel && allocations==3);
    table.dirty[0]=1; table.chg[0][0]=1;
    assert(nvmtl_snapshot_set_c(&c,0,&p)==texel && allocations==3);
    p.tbany=0;
    VkDescriptorSet image=nvmtl_snapshot_set_c(&c,0,&p); assert(image!=texel && allocations==4);
    // Image/sampler changes and direct buffer descriptors cannot take the offset-only path.
    table.dirty[0]=1; table.chg[0][0]=1ull<<NVMTL_NBUF;
    VkDescriptorSet changed=nvmtl_snapshot_set_c(&c,0,&p); assert(changed!=image && allocations==5);
    p.bmask[0] |= 1;
    VkDescriptorSet direct=nvmtl_snapshot_set_c(&c,0,&p); assert(direct!=changed && allocations==6);
    table.dirty[0]=1; table.chg[0][0]=1;
    assert(nvmtl_snapshot_set_c(&c,0,&p)!=direct && allocations==7);
    // A descriptor from a retired/recycled slot must not be returned as live.
    sets[table.last_idx[0]].set=(void *)999;
    assert(nvmtl_snapshot_set_c(&c,0,&p)!=(VkDescriptorSet)(uintptr_t)999 && allocations==8);
    // A growing pipeline mask can copy compatible descriptors but must allocate its new bindings.
    p.bmask[1] |= 1;
    nvmtl_snapshot_set_c(&c,0,&p); assert(allocations==9 && copies>=1);
    enabled=0; nvmtl_snapshot_set_c(&c,0,&p); assert(allocations==10);
    enabled=1; p.bmask[0]&=~1ull; p.gl_uniform_layout=1;
    p.gl_uniform_slot[0]=8; p.gl_uniform_range[0]=80;
    table.s[0][7]=(nvmtl_slot){.kind=NVMTL_SLOT_BUF,.h=(void *)0x770000,.size=65536};
    c.address_count=1;
    VkDescriptorSet uniform=nvmtl_snapshot_set_c(&c,0,&p); assert(allocations==11);
    for(unsigned i=1;i<16;i++) {
        table.s[0][7].off=i*256; table.dirty[0]=1; table.chg[0][0]=1ull<<7;
        assert(nvmtl_snapshot_set_c(&c,0,&p)==uniform && allocations==11);
        assert(c.gl_uniform_dyn[0]==i*256);
    }
    p.gl_uniform_range[0]=96;
    assert(nvmtl_snapshot_set_c(&c,0,&p)!=uniform && allocations==12);
    table.s[0][7].h=(void *)0x780000;
    nvmtl_snapshot_set_c(&c,0,&p); assert(allocations==13);
    p.gl_uniform_layout=0;
    nvmtl_snapshot_set_c(&c,0,&p); assert(allocations==14);
    // A handle that cannot be a live object is not handed on (the dummy is bound instead).
    nvmtl_slot bad={.kind=NVMTL_SLOT_BUF,.h=(void *)0xa3,.size=65536};
    assert(nvmtl_slot_checked(&bad,0,7)==NULL && nvmtl_slot_checked(&table.s[0][7],0,7)==&table.s[0][7]);
    puts("PASS: unchanged/offset-only reuse, table-page rollover, texel/image masks, direct buffers, recycled sets, growing masks, opt-out");
    // Production history + snapshot dispatch, not a hand model. A-B-A must return A.
    reuse_mode=2; dynamic=1;
    table=(nvmtl_bindtable){0}; c=(nvk_cmdbuf){.table=&table,.sets=sets,.address_pages=&page,.address_count=1};
    p=(nvk_pipeline){.gmask_ok=1};
    p.gmask[1][0]=1ull<<NVMTL_NBUF; p.gmask[1][2]=1ull<<32;
    table.s[1][NVMTL_NBUF]=(nvmtl_slot){.kind=NVMTL_SLOT_TEX,.h=(void *)0x10000};
    table.s[1][NVMTL_NBUF+NVMTL_NTEX]=(nvmtl_slot){.kind=NVMTL_SLOT_SAMP,.h=(void *)0x20000};
    VkDescriptorSet a=nvmtl_snapshot_set_c(&c,1,&p);
    table.dirty[1]=1; table.chg[1][0]=1ull<<NVMTL_NBUF; table.s[1][NVMTL_NBUF].h=(void *)0x20000;
    table.s[1][NVMTL_NBUF+NVMTL_NTEX].h=(void *)0x10000;
    VkDescriptorSet b=nvmtl_snapshot_set_c(&c,1,&p); assert(a!=b); // same XOR/sum, unequal exact state
    table.dirty[1]=1; table.chg[1][0]=1ull<<NVMTL_NBUF; table.s[1][NVMTL_NBUF].h=(void *)0x10000;
    table.s[1][NVMTL_NBUF+NVMTL_NTEX].h=(void *)0x20000;
    assert(nvmtl_snapshot_set_c(&c,1,&p)==a && c.nsets==2);
    p.gmask[1][0]|=1ull<<(NVMTL_NBUF+1);
    VkDescriptorSet grown=nvmtl_snapshot_set_c(&c,1,&p); assert(grown!=a && c.nsets==3);
    sets[table.last_idx[1]].set=(void *)999;
    assert(nvmtl_snapshot_set_c(&c,1,&p)!=grown && c.nsets==4);
    table.dirty[1]=1; table.chg[1][0]=1ull<<NVMTL_NBUF; table.s[1][NVMTL_NBUF].h=(void *)0x30000;
    unsigned recorded=c.nsets; fail_snapshot=1;
    assert(!nvmtl_snapshot_set_c(&c,1,&p) && c.nsets==recorded);
    fail_snapshot=0; assert(nvmtl_snapshot_set_c(&c,1,&p) && c.nsets==recorded+1);
    free(c.address_history); free(c.descriptor_history);
    nvmtl_bindtable other={0}; nvk_setref other_sets[64]={0};
    c=(nvk_cmdbuf){.table=&other,.sets=other_sets};
    assert(nvmtl_snapshot_set_c(&c,1,&p)!=a && c.nsets==1); // cross-CB never searches prior history
    free(c.address_history); free(c.descriptor_history);
    puts("PASS: production FS A-B-A, colliding aggregate handles, mask growth, recycled sets, failed allocation and cross-CB isolation");
}
'''

# Independently extract the actual table writer, with allocation failures and retained pages.
table_fixture = fixture[:fixture.index('static int enabled=')] + r'''
static unsigned serial, fail_page;
static int g_pl_lock, g_pl;
#define pthread_mutex_lock(x) ((void)0)
#define pthread_mutex_unlock(x) ((void)0)
#define nvmtl_pl_made(x) ((void)0)
#define NVMTL_LV_PAGE 0
#define nvmtl_live_relabel(b,k) ((void)0)
#define NVMTL_TABLE_BYTES ((2*NVMTL_NBUF+NVMTL_NTEX)*sizeof(uint64_t))
static nvmtl_address_page *nvmtl_keep_page_pop(void) { return NULL; }
uint64_t nvmtl_vk_buffer_address(nvk_buffer *b) { return (uintptr_t)b->buf+0x10000000; }
int nvmtl_vk_buffer_create(size_t n,int host,nvk_buffer *b) {
    if(fail_page) return -1;
    b->map=calloc(1,n); b->buf=(void *)(uintptr_t)(++serial*4096); return !b->map;
}
void nvmtl_vk_buffer_destroy(nvk_buffer *b) { free(b->map); }
''' + section(vk, 'static int nvmtl_address_snapshot(', '\nstatic void nvmtl_spirv_retarget_set(') + r'''
int main(void) {
    reuse_mode=2; nvmtl_bindtable t={0}; nvk_cmdbuf c={.table=&t}; VkDescriptorBufferInfo a,b,again;
    t.s[1][0]=(nvmtl_slot){.kind=NVMTL_SLOT_BUF,.address=0x1000,.off=8};
    t.s[1][NVMTL_NBUF]=(nvmtl_slot){.kind=NVMTL_SLOT_TEX,.h=(void *)0x10000,.size=16};
    assert(!nvmtl_address_snapshot(&c,1,&a));
    t.s[1][0].address+=16; t.s[1][NVMTL_NBUF].size-=16; // equal sum, unequal logical words
    assert(!nvmtl_address_snapshot(&c,1,&b) && b.offset!=a.offset);
    t.s[1][0].address-=16; t.s[1][NVMTL_NBUF].size+=16;
    assert(!nvmtl_address_snapshot(&c,1,&again) && again.offset==a.offset && c.address_count==2);
    uint64_t *words=c.address_pages ? ((nvmtl_address_page *)c.address_pages)->buffer.map : NULL;
    assert(words[NVMTL_NBUF]==nvmtl_vk_buffer_address(&((nvmtl_address_page *)c.address_pages)->buffer));
    // At the page boundary an old immutable table remains valid, without allocating a new page.
    c.address_count=NVMTL_TABLES_PER_PAGE;
    fail_page=1; assert(!nvmtl_address_snapshot(&c,1,&again) && again.buffer==a.buffer && again.offset==a.offset);
    t.s[1][0].off++; assert(nvmtl_address_snapshot(&c,1,&b) && c.address_count==NVMTL_TABLES_PER_PAGE);
    fail_page=0; assert(!nvmtl_address_snapshot(&c,1,&b) && b.buffer!=a.buffer);
    t.s[1][0].off--; assert(!nvmtl_address_snapshot(&c,1,&again) && again.buffer==a.buffer);
    nvk_cmdbuf other={.table=&t}; assert(!nvmtl_address_snapshot(&other,1,&again) && again.buffer!=a.buffer);
    for(unsigned pass=0;pass<2;pass++) {
        nvk_cmdbuf *cb=pass ? &other : &c; free(cb->address_history);
        nvmtl_address_page *page=cb->address_pages;
        while(page) { nvmtl_address_page *next=page->next; nvmtl_vk_buffer_destroy(&page->buffer); free(page); page=next; }
    }
    puts("PASS: production immutable table A-B-A, exact logical equality, address/offset/size changes, page boundary, failed allocation, cross-CB");
}
'''

iface = re.search(r'@interface NVMTLResSet : NSMutableSet .*?@end', header, re.S).group(0)
impl = section(objects, '@implementation NVMTLResSet', '\n@end') + '\n@end\n'
# the identity-hashed set the class allocates sits just above it in NVMTLObjects.m
impl = objects[objects.index('static NSMutableSet *nvmtl_identity_set('):objects.index('@implementation NVMTLResSet')] + impl
resources = r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <os/lock.h>
#include <stdatomic.h>
#include <objc/runtime.h>
#include <dispatch/dispatch.h>
static _Atomic unsigned long gRes2Tick;
static _Atomic long refs;
static id lastReleased;
static void nvmtl_resref(id o,int d) { refs+=d; if(d<0) lastReleased=o; }
''' + iface + '\n' + impl + r'''
@interface EqualResource : NSObject { @public unsigned key; } @end
@implementation EqualResource
- (NSUInteger)hash { return key; }
- (BOOL)isEqual:(id)o { return [o isKindOfClass:[EqualResource class]] && ((EqualResource *)o)->key==key; }
@end
static id watched;
static unsigned lookupMessages, addMessages;
static IMP originalMember, originalAdd;
static id countedMember(id self, SEL cmd, id o) {
    if (self==watched) lookupMessages++;
    return ((id(*)(id,SEL,id))originalMember)(self,cmd,o);
}
static void countedAdd(id self, SEL cmd, id o) {
    if (self==watched) addMessages++;
    ((void(*)(id,SEL,id))originalAdd)(self,cmd,o);
}
int main(void) { @autoreleasepool {
    NVMTLResSet *set=[NVMTLResSet new]; EqualResource *a=[EqualResource new], *alias=[EqualResource new];
    watched=set->_s;
    Class backing=object_getClass(watched);
    originalMember=method_setImplementation(class_getInstanceMethod(backing,@selector(member:)), (IMP)countedMember);
    originalAdd=method_setImplementation(class_getInstanceMethod(backing,@selector(addObject:)), (IMP)countedAdd);
    // Metal objects are equal only to themselves, so the set dedupes by identity: the same object twice is one
    // member, and an equal-but-distinct object is a second member (it never stands in for the first)
    a->key=alias->key=1; [set addObject:a]; [set addObject:a];
    assert(set.count==1 && refs==1);
    for(unsigned i=0;i<8;i++) assert(!set->_mru[i] || set->_mru[i]==(__bridge const void *)a);
    [set removeObject:alias]; assert(set.count==1 && refs==1);
    [set removeObject:a]; assert(!set.count && !refs && lastReleased==a);
    for(unsigned i=0;i<8;i++) assert(!set->_mru[i]);
    lastReleased=nil;
    lookupMessages=addMessages=0; // measure only addObject's miss path, not removeObject's unchanged lookup
    NSMutableArray *pool=[NSMutableArray new];
    for(unsigned i=0;i<32;i++) { id o=[NSObject new]; [pool addObject:o]; [set addObject:o]; }
    // Revisit entries displaced from the eight-entry MRU: membership must not add residency twice.
    for(id o in pool) [set addObject:o];
    id absent=nil; [set addObject:absent]; assert(set.count==32 && refs==32);
    assert(lookupMessages && addMessages); // production keeps its original ObjC bridge after an inconclusive CF A/B
    watched=nil;
    NVMTLResSet *taken=[set nvmtlTakeAll]; assert(!set.count && taken.count==32 && refs==32);
    for(unsigned i=0;i<8;i++) assert(!set->_mru[i]);
    [set addObject:a]; assert(set.count==1 && refs==33);
    [set removeAllObjects]; assert(!set.count && refs==32);
    [taken removeAllObjects]; assert(!refs);
    NVMTLResSet *copy=[set mutableCopy]; assert(!copy.count && !refs);
    [set addObject:a]; copy=[set mutableCopy]; assert(copy.count==1 && refs==2);
    [copy addObject:a]; assert(refs==2); [copy removeAllObjects]; [set removeAllObjects]; assert(!refs);
    // Adds and removals remain serialized; test shared misses, MRU hits and invalidation under contention.
    dispatch_apply(4000, dispatch_get_global_queue(QOS_CLASS_DEFAULT,0), ^(size_t i) {
        [set addObject:pool[i%32]];
    });
    assert(set.count==32 && refs==32);
    for(id o in pool) [set removeObject:o]; assert(!set.count && !refs);
    for(unsigned i=0;i<8;i++) assert(!set->_mru[i]);
    fprintf(stderr,"resource backing messages: member=%u add=%u\n",lookupMessages,addMessages);
    puts("PASS: production resource-set miss/MRU paths, identity, copy/seal/clear invalidation, concurrent adds and residency balance");
} }
'''

defaults = section(objects, '    const float defaultBlend[4]', '    e->_visBuf')
state = r'''
#include <assert.h>
#include <stdio.h>
typedef struct { unsigned restart_off; } Commands;
typedef struct { Commands _c; } Owner;
static int provoking=1; static float bias=7,slope=8,clamp=9;
static void nvmtl_vk_cmd_set_blend_color(Commands *c,const float *v) {}
static void nvmtl_vk_cmd_set_line_width(Commands *c,float v) {}
static int nvmtl_vk_cmd_set_polygon_mode(Commands *c,int v) { return 0; }
static int nvmtl_vk_cmd_set_depth_clip(Commands *c,int v) { return 0; }
static void nvmtl_vk_cmd_set_provoking_last(Commands *c,int v) { provoking=v; }
static void nvmtl_vk_cmd_set_depth_bias(Commands *c,float b,float s,float cl) { bias=b;slope=s;clamp=cl; }
static void next_encoder(Owner *owner) {
#define _c owner->_c
''' + defaults + r'''
#undef _c
}
int main(void) {
    Owner owner={._c.restart_off=1}; next_encoder(&owner);
    assert(!owner._c.restart_off && !provoking && bias==0 && slope==0 && clamp==0);
    puts("PASS: depth bias, provoking vertex and strip restart defaults reset between render encoders");
}
'''

bias = section(vk, 'void nvmtl_vk_cmd_set_depth_bias(', '\nvoid nvmtl_vk_cmd_set_line_width(')
# the array grows as dynamic states are added (24 -> 26 with depth bounds), so the anchor names no size
pipeline_states = section(vk, '    VkDynamicState dyn[', '    // Metal has no depth-bias enable switch.')
bias_check = r'''
#include <assert.h>
#include <stdio.h>
#include <mach/mach.h>
#include <vulkan/vulkan.h>
#include "nvmtl_vk.h"
static int enabled; static float constant_value,slope_value,clamp_value;
static void pvkCmdSetDepthBiasEnable(void *c,int value) { enabled=value; }
static void pvkCmdSetDepthBias(void *c,float b,float cl,float s) { constant_value=b;clamp_value=cl;slope_value=s; }
''' + bias + r'''
static int g_dyn3,g_provoking,g_clip,g_wide,g_sample_locations,g_depth_bounds;
static unsigned states(int hasDepth,nvk_pipeline *out,unsigned samples) {
''' + pipeline_states + r'''
    assert(dsci.dynamicStateCount<=sizeof dyn/sizeof dyn[0]);
    for(unsigned i=0;i<dsci.dynamicStateCount;i++) for(unsigned j=0;j<i;j++) assert(dyn[i]!=dyn[j]);
    return dsci.dynamicStateCount;
}
int main(void) {
    nvk_cmdbuf c={0};
    nvmtl_vk_cmd_set_depth_bias(&c,0,0,0); assert(!enabled);
    nvmtl_vk_cmd_set_depth_bias(&c,1,2,3); assert(enabled && constant_value==1 && slope_value==2 && clamp_value==3);
    nvmtl_vk_cmd_set_depth_bias(&c,0,2,0); assert(enabled);
    nvmtl_vk_cmd_set_depth_bias(&c,0,0,0); assert(!enabled);
    for(unsigned flags=0;flags<256;flags++) {
        g_dyn3=flags&1;g_provoking=flags&2;g_clip=flags&4;g_wide=flags&8;g_sample_locations=flags&16;g_depth_bounds=!!(flags&128);
        nvk_pipeline p={.has_stencil=!!(flags&32),.vin_dyn=!!(flags&64)};
        states(0,&p,1);states(1,&p,4);
    }
    g_dyn3=g_provoking=g_clip=g_wide=g_sample_locations=g_depth_bounds=1;
    /* 16 depth/stencil + depth-bias enable + 2 depth bounds + polygon, provoking, clip, stride, width, sample locations */
    nvk_pipeline p={.has_stencil=1,.vin_dyn=1}; assert(states(1,&p,4)==25);
    puts("PASS: depth bias is disabled at zero; all 256 dynamic-state combinations fit without duplicates");
}
'''

# Compile production helpers and setter regions, not a hand-written model of their formatting.
def braced_region(text, start):
    begin = text.index(start)
    opening = text.index('{', begin)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[begin:end]


format_helpers = braced_region(objects, 'static BOOL nvmtl_pixel_sampler_update(')
for symbol in ('static NSString *nvmtl_sampler_index_key(', 'static BOOL nvmtl_texture_shape_first('):
    if symbol in objects:  # The pre-fix source must compile too, to demonstrate the regression.
        format_helpers += '\n' + braced_region(objects, symbol)
sampler_methods = '\n'.join(braced_region(objects, signature) for signature in (
    '- (void)setFragmentSamplerState:(id<MTLSamplerState>)s atIndex:',
    '- (void)setVertexSamplerState:(id<MTLSamplerState>)s atIndex:',
    '- (void)setFragmentSamplerState:(id<MTLSamplerState>)ss lodMinClamp:',
    '- (void)setVertexSamplerState:(id<MTLSamplerState>)ss lodMinClamp:'))
texture_diagnostic = braced_region(objects, 'if (!nt->_stex && (nt.usage & MTLTextureUsageShaderWrite))')
formatting = r'''
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <assert.h>
#include <dispatch/dispatch.h>
#include <os/lock.h>
#include <stdatomic.h>
#include <stdio.h>
static _Atomic unsigned number_strings, formatted_strings, logs;
@interface NSNumber (CountStrings)
- (NSString *)countedStringValue;
@end
@implementation NSNumber (CountStrings)
- (NSString *)countedStringValue {
    atomic_fetch_add(&number_strings, 1);
    return [self countedStringValue];
}
@end
@interface NSString (CountFormats)
+ (instancetype)countedStringWithFormat:(NSString *)format, ...;
@end
@implementation NSString (CountFormats)
+ (instancetype)countedStringWithFormat:(NSString *)format, ... {
    atomic_fetch_add(&formatted_strings, 1);
    va_list args; va_start(args, format);
    NSString *result = [[NSString alloc] initWithFormat:format arguments:args];
    va_end(args); return result;
}
@end
#define nvlog(...) ((void)atomic_fetch_add(&logs, 1))
#define nvtrace(...) ((void)0)
#define NVMTL_SET_VERTEX 0
#define NVMTL_SET_FRAGMENT 1
@protocol MTLSamplerState @end
@interface NVMTLSamplerState : NSObject <MTLSamplerState> {
@public int _s; NSDictionary *_pixelState;
}
@end
@implementation NVMTLSamplerState @end
@interface Commands : NSObject { @public int _c; NSMutableSet *_resources; } @end
@implementation Commands @end
#define NVMTLResSet NSMutableSet
static unsigned binds[2]; static BOOL fail_bind;
static int nvmtl_vk_bind_sampler(int *c, unsigned stage, uint32_t i, int *sampler) {
    assert(i < 16); binds[stage]++; return fail_bind;
}
''' + format_helpers + r'''
@interface Encoder : NSObject {
@public Commands *_cb;
    NSMutableDictionary *_fragmentSamplers, *_vertexSamplers;
    id _pixelSamplerKey;
}
@end
@implementation Encoder
''' + sampler_methods + r'''
@end
#define MTLTextureUsageShaderWrite 2
struct Image { unsigned storage; };
@interface NVMTLTexture : NSObject {
@public void *_stex; struct Image image;
}
@property NSUInteger pixelFormat, usage, textureType;
- (struct Image *)nvi;
@end
@implementation NVMTLTexture
- (struct Image *)nvi { return &image; }
@end
static void diagnostic(NVMTLTexture *nt, NSUInteger i) {
''' + texture_diagnostic + r'''
}
static void check_samplers(void) {
    Encoder *e=[Encoder new]; e->_cb=[Commands new];
    NVMTLSamplerState *s=[NVMTLSamplerState new];
    s->_pixelState=@{@"lod_min_clamp":@0.0f, @"lod_max_clamp":@100.0f};
    atomic_store(&number_strings, 0); atomic_store(&formatted_strings, 0);
    for (unsigned repeat=0; repeat<100; repeat++) for (NSUInteger i=0; i<16; i++) {
        [e setFragmentSamplerState:s atIndex:i]; [e setVertexSamplerState:s atIndex:i];
    }
    assert(binds[0]==1600 && binds[1]==1600);
    assert(e->_fragmentSamplers.count==16 && e->_vertexSamplers.count==16);
    for (NSUInteger i=0; i<16; i++) {
        // snprintf in the test is deliberately outside the instrumented Foundation formatting paths.
        char decimal[8]; snprintf(decimal, sizeof decimal, "%lu", (unsigned long)i);
        NSString *key=[NSString stringWithUTF8String:decimal];
        assert([e->_fragmentSamplers[key] isEqual:s->_pixelState]);
        assert([e->_vertexSamplers[key] isEqual:s->_pixelState]);
        e->_pixelSamplerKey=@"cached";
        [e setFragmentSamplerState:s atIndex:i]; [e setVertexSamplerState:s atIndex:i];
        assert(e->_pixelSamplerKey); // unchanged states preserve the cached key
        [e setFragmentSamplerState:s lodMinClamp:1 lodMaxClamp:2 atIndex:i];
        [e setVertexSamplerState:s lodMinClamp:3 lodMaxClamp:4 atIndex:i];
        assert(!e->_pixelSamplerKey);
        assert([e->_fragmentSamplers[key][@"lod_min_clamp"] floatValue]==1);
        assert([e->_vertexSamplers[key][@"lod_max_clamp"] floatValue]==4);
        [e setFragmentSamplerState:nil atIndex:i]; [e setVertexSamplerState:nil atIndex:i];
        assert(!e->_fragmentSamplers[key] && !e->_vertexSamplers[key]);
    }
    fprintf(stderr, "sampler Foundation conversions: number=%u format=%u\n",
            atomic_load(&number_strings), atomic_load(&formatted_strings));
    assert(!atomic_load(&number_strings) && !atomic_load(&formatted_strings));
    // Preserve bind failure handling and the LOD setter's existing decimal-string fallback contract.
    fail_bind=YES; [e setFragmentSamplerState:s atIndex:0];
    assert(!e->_fragmentSamplers[@"0"]); fail_bind=NO;
    e->_fragmentSamplers[@"16"]=s->_pixelState;
    [e setFragmentSamplerState:s lodMinClamp:5 lodMaxClamp:6 atIndex:16];
    assert([e->_fragmentSamplers[@"16"][@"lod_min_clamp"] floatValue]==5);
    puts("PASS: production base/LOD sampler setters preserve string keys, nil, cache invalidation and bind failures without valid-index formatting");
}
static void check_textures(void) {
    NVMTLTexture *t=[NVMTLTexture new]; t.pixelFormat=80; t.textureType=2;
    atomic_store(&formatted_strings, 0); atomic_store(&logs, 0);
    diagnostic(t, 0); assert(!atomic_load(&logs));
    t.usage=MTLTextureUsageShaderWrite; t->_stex=(void *)1;
    diagnostic(t, 0); assert(!atomic_load(&logs)); t->_stex=NULL;
    dispatch_apply(2000, dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^(size_t i) {
        @autoreleasepool { diagnostic(t, i%16); }
    });
    assert(atomic_load(&logs)==1); // index is not part of the shape; concurrent repeats log once
    t.pixelFormat++; diagnostic(t, 0);
    t.usage|=4; diagnostic(t, 0);
    t.textureType++; diagnostic(t, 0);
    assert(atomic_load(&logs)==4); // all three shape fields participate
    for (NSUInteger i=0; i<4096; i++) { t.pixelFormat=1000+i; diagnostic(t, 0); }
    unsigned capped=atomic_load(&logs);
    fprintf(stderr, "texture Foundation formats=%u unique-shape logs=%u\n",
            atomic_load(&formatted_strings), capped);
    assert(!atomic_load(&formatted_strings)); assert(capped>=4 && capped<=128);
    for (NSUInteger i=0; i<4096; i++) { t.pixelFormat=1000+i; diagnostic(t, 0); }
    t.pixelFormat=80; t.usage=MTLTextureUsageShaderWrite; t.textureType=2; diagnostic(t, 0);
    assert(atomic_load(&logs)==capped); // saturation neither evicts old triples nor logs new ones
    puts("PASS: production texture diagnostic gates, primitive triple deduplication, concurrency and bounded saturation without formatting");
}
int main(int argc, char **argv) { @autoreleasepool {
    method_exchangeImplementations(class_getInstanceMethod([NSNumber class], @selector(stringValue)),
                                  class_getInstanceMethod([NSNumber class], @selector(countedStringValue)));
    method_exchangeImplementations(class_getClassMethod([NSString class], @selector(stringWithFormat:)),
                                  class_getClassMethod([NSString class], @selector(countedStringWithFormat:)));
    if (argc>1 && !strcmp(argv[1], "samplers")) check_samplers(); else check_textures();
} }
'''

with tempfile.TemporaryDirectory() as d:
    path = pathlib.Path(d)
    includes = ['-I' + str(ROOT / 'plugin'), '-I' + str(pathlib.Path.home() / 'Vulkan-Headers/include')]
    for name, code, objc in [('descriptors', fixture + mask + cache + checks, False), ('tables', table_fixture, False), ('resources', resources, True),
                             ('defaults', state, False), ('bias', bias_check, False), ('formatting', formatting, True)]:
        src = path / (name + ('.m' if objc else '.c'))
        src.write_text(code)
        executable = path / name
        cmd = ['xcrun', 'clang', '-O2', '-Werror', *includes, str(src), '-o', str(executable)]
        if objc:
            cmd += ['-fobjc-arc', '-framework', 'Foundation']
        subprocess.run(cmd, check=True)
        if name == 'formatting':
            results = [subprocess.run([str(executable), mode]) for mode in ('samplers', 'textures')]
            assert all(result.returncode == 0 for result in results), 'production hot-path formatting regression'
        else:
            subprocess.run([str(executable)], check=True)

# WindowServer hot paths (2026-11-10 sample): a release plugin must not describe the pixel-sampler dictionary for a log
# line it does not write, and a sampler set already serialized in one encoder must not be serialized again in the next.
_dev = (ROOT / 'plugin/NVMTLDevice.m').read_text()
_i = _dev.index('nvlog("pixel sampler: %s",s->_pixelState.description.UTF8String);')
assert 'if (nvmtl_process_env_on())' in _dev[_i - 300:_i], 'pixel sampler description formatted in release processes'
_obj = (ROOT / 'plugin/NVMTLObjects.m').read_text()
_m = _obj[_obj.index('- (NVMTLRenderPipelineState *)nvmtlSamplerPipeline {'):]
_m = _m[:_m.index('NSData *key=_pixelSamplerKey;')]
assert 'globalKeys' in _m and _m.index('globalKeys') < _m.index('dataWithJSONObject'), 'process-wide pixel sampler key list missing'
assert _m.count('os_unfair_lock_lock(&globalLock)') == 2 and _m.count('os_unfair_lock_unlock(&globalLock)') == 2, 'global key list not locked'
assert 'globalKeys.count == 32' in _m, 'global key list must stay bounded'
print('ok: WindowServer sampler hot paths')
