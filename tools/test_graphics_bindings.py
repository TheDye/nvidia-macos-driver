#!/usr/bin/env python3
"""Exercise production descriptor allocation and graphics binding elision.

--descriptors-only runs the allocation regression without Vulkan/macOS headers.
"""
import pathlib
import sys
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root/'plugin/nvmtl_vk.c').read_text()
def section(start, end):
    i = source.index(start)
    return source[i:source.index(end, i)]

sets = section('static void nvmtl_bind_graphics_sets(', '\nvoid nvmtl_vk_cmd_bind_set(')
pipeline = section('void nvmtl_vk_cmd_bind_pipeline(', '\nint nvmtl_vk_cmd_set_topology(')
uniform_pick = section('static nvk_pipeline *nvmtl_uniform_cbuf_pick(', '\nstatic VkDescriptorType binding_type(')
assert 'memset(out, 0, sizeof *out);' in section('int nvmtl_vk_cmd_begin(', '\nstatic pthread_mutex_t g_rpv_lock')
assert source.count('pvkCmdBindPipeline(c->cb, VK_PIPELINE_BIND_POINT_GRAPHICS,') == 1
assert 'if (p->pipe) nvmtl_vk_cmd_bind_pipeline(c, p);' in source

# Exercise the production allocation half independently of Vulkan/macOS headers.
# Updates are deliberately outside this fixture: no claim about GPU execution.
allocation = section('static VkDescriptorSet nvmtl_snapshot_set_md(nvk_cmdbuf *c, uint32_t which, uint32_t stex_hi, const uint64_t *mask, const uint64_t *tb,\n                                             VkDescriptorSet from, const uint64_t *copym, const VkDescriptorBufferInfo *uniform)\n{', '\n    VkWriteDescriptorSet w[')
allocation += '\n    return set;\n}\n'
allocation_fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef void *VkDescriptorSet, *VkDescriptorPool, *VkDescriptorSetLayout;
typedef int VkResult;
typedef struct { int unused; } VkDescriptorBufferInfo;
typedef struct { int sType; void *pNext; VkDescriptorPool descriptorPool;
    unsigned descriptorSetCount; const VkDescriptorSetLayout *pSetLayouts; } VkDescriptorSetAllocateInfo;
typedef struct { void *pool, *set; } nvk_setref;
typedef struct { nvk_setref *sets; unsigned nsets, capsets, skipped;
    void *dcache[32], *dcachepool; unsigned ndcache, dcache_gl_uniform; } nvk_cmdbuf;
#define VK_NULL_HANDLE NULL
#define VK_SUCCESS 0
#define VK_ERROR_OUT_OF_POOL_MEMORY (-1)
#define VK_ERROR_OUT_OF_HOST_MEMORY (-2)
#define VK_ERROR_FRAGMENTED_POOL (-3)
#define VK_ERROR_DEVICE_LOST (-4)
#define VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO 1
#define NVMTL_DESC_BATCH_MAX 32
#define nvlog(...) ((void)0)
enum { E78C_DCACHE, E78C_POOLALLOC, E78C_POOLOPEN, E78C_SETFAIL };
#define nvmtl_enc78_count(k, n) ((void)0)
static unsigned locked, opens, calls, serial, fail_grow;
static int failure, init_failure, pool_error=VK_ERROR_OUT_OF_POOL_MEMORY;
static unsigned available[64], g_ndescPools = 1;
static void *g_descPools[64] = {(void *)1};
static void *g_dev, *g_setLayout = (void *)2, *g_glUniformLayout = (void *)3;
static int g_desc_lock;
static void pthread_mutex_lock(int *m) { assert(!locked); locked=1; }
static void pthread_mutex_unlock(int *m) { assert(locked); locked=0; }
static unsigned nvmtl_desc_batch(void) { return 32; }
static int ensure_descriptors_locked(void) { assert(locked); return init_failure; }
static int nvmtl_desc_pool_open(void) {
    assert(locked); opens++;
    if (g_ndescPools==64) return -1;
    unsigned i=g_ndescPools++; g_descPools[i]=(void *)(uintptr_t)(i+1); available[i]=64; return 0;
}
static int pvkAllocateDescriptorSets(void *dev, const VkDescriptorSetAllocateInfo *ai, VkDescriptorSet *out) {
    assert(locked); calls++;
    if (failure) return failure;
    unsigned i=(unsigned)(uintptr_t)ai->descriptorPool-1;
    if (available[i]<ai->descriptorSetCount) return pool_error;
    available[i]-=ai->descriptorSetCount;
    for (unsigned k=0;k<ai->descriptorSetCount;k++) out[k]=(void *)(uintptr_t)(++serial);
    return VK_SUCCESS;
}
static int pvkFreeDescriptorSets(void *dev, void *pool, unsigned n, void *const *sets) {
    assert(locked); available[(uintptr_t)pool-1]+=n; return VK_SUCCESS;
}
static void *checked_realloc(void *p, size_t n) {
    assert(!locked && "host list growth must not hold the process-wide descriptor lock");
    return fail_grow ? NULL : realloc(p,n);
}
#define realloc checked_realloc
''' + allocation + r'''
#undef realloc
static void reset(void) {
    assert(!locked); memset(available,0,sizeof available); g_ndescPools=1;
    opens=calls=serial=fail_grow=0; failure=init_failure=0; pool_error=VK_ERROR_OUT_OF_POOL_MEMORY;
}
static void *snapshot(nvk_cmdbuf *c, int gl) {
    VkDescriptorBufferInfo u={0};
    return nvmtl_snapshot_set_md(c,0,0,NULL,NULL,NULL,NULL,gl ? &u : NULL);
}
int main(void) {
    nvk_setref refs[128]; nvk_cmdbuf c={.sets=refs,.capsets=128};
    reset(); available[0]=1;
    assert(snapshot(&c,0));
    assert(opens==0 && "a batch miss must try a single set before opening another pool");
    assert(c.nsets==1 && !c.ndcache && c.sets[0].pool==(void *)1);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; available[0]=64;
    assert(snapshot(&c,0)); assert(c.ndcache==31 && calls==1);
    for(unsigned i=0;i<31;i++) assert(snapshot(&c,0));
    assert(c.nsets==32 && !c.ndcache && calls==1);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128};
    assert(snapshot(&c,0) && opens==1 && c.ndcache==31); // full pool: keep batching
    assert(snapshot(&c,1) && c.dcache_gl_uniform==1 && c.ndcache==31); // layout switch
    reset(); c=(nvk_cmdbuf){0}; available[0]=64;
    assert(snapshot(&c,0)); assert(c.nsets==1); free(c.sets); // growth is outside lock
    reset(); c=(nvk_cmdbuf){0}; available[0]=64; fail_grow=1;
    assert(!snapshot(&c,0) && c.skipped==1 && !calls && !locked);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; failure=VK_ERROR_DEVICE_LOST;
    assert(!snapshot(&c,0) && c.skipped==1 && calls==1 && !opens && !locked);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; init_failure=1;
    assert(!snapshot(&c,0) && c.skipped==1 && !calls && !locked);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; available[0]=1; pool_error=VK_ERROR_FRAGMENTED_POOL;
    assert(snapshot(&c,0) && !opens && c.nsets==1); // fragmentation also permits single retry
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; failure=VK_ERROR_FRAGMENTED_POOL;
    assert(!snapshot(&c,0) && c.skipped==1 && opens==1 && calls==4 && !locked);
    reset(); c=(nvk_cmdbuf){.sets=refs,.capsets=128}; g_ndescPools=64;
    for(unsigned i=0;i<64;i++) g_descPools[i]=(void *)(uintptr_t)(i+1);
    assert(!snapshot(&c,0) && c.skipped==1 && c.nsets==0 && !c.ndcache && !locked);
    // At the pool limit, a single free set in an older pool must still be found.
    c.skipped=0; available[0]=1;
    assert(snapshot(&c,0) && c.nsets==1 && !c.skipped && c.sets[0].pool==(void *)1);
    reset(); c=(nvk_cmdbuf){0}; available[0]=64;
    assert(snapshot(&c,0)); unsigned cached=c.ndcache, before=calls;
    c.capsets=c.nsets; fail_grow=1;
    assert(!snapshot(&c,1) && c.ndcache==cached && calls==before && !c.dcache_gl_uniform && !locked);
    free(c.sets); // failed growth did not consume/free the old layout's cached descriptors
    puts("PASS: descriptor batch exhaustion, cache, layout switch, growth, fragmentation, pool limit and failures");
}
'''
with tempfile.TemporaryDirectory(prefix='nvmtl-descriptor-allocation-') as tmp:
    path=pathlib.Path(tmp)
    (path/'test.c').write_text(allocation_fixture)
    subprocess.run(['xcrun','clang','-O2',str(path/'test.c'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)
if '--descriptors-only' in sys.argv:
    sys.exit(0)

fixture = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <mach/mach.h>
#include <vulkan/vulkan.h>
#include "nvmtl_vk.h"
static unsigned descriptor_calls, pipeline_calls;
enum { NVMTL_SLOT_BUF = 1 };
typedef struct { unsigned char kind; void *h; size_t off, size; uint64_t address; } nvmtl_slot;
typedef struct { nvmtl_slot s[2][321]; } nvmtl_bindtable;
static VkDeviceSize g_cbuf_alignment = 256;
#define syslog(...) ((void)0)
static void pvkCmdBindDescriptorSets(VkCommandBuffer cb,VkPipelineBindPoint point,VkPipelineLayout layout,
        unsigned first,unsigned count,const VkDescriptorSet *sets,unsigned ndyn,const unsigned *offsets) {
    assert(point==VK_PIPELINE_BIND_POINT_GRAPHICS && first==0 && count<=3 && ndyn<=4);
    descriptor_calls++;
}
static void pvkCmdBindPipeline(VkCommandBuffer cb,VkPipelineBindPoint point,VkPipeline pipeline) {
    assert(point==VK_PIPELINE_BIND_POINT_GRAPHICS); pipeline_calls++;
}
/* depth bounds (2.7.2) are re-sent after a bind only for a pipeline with db_dyn set; these pipelines have it off */
static void pvkCmdSetDepthBoundsTestEnable(VkCommandBuffer cb,VkBool32 on) { (void)cb; (void)on; }
static void pvkCmdSetDepthBounds(VkCommandBuffer cb,float lo,float hi) { (void)cb; (void)lo; (void)hi; }
''' + sets + '\n' + uniform_pick + '\n' + pipeline + r'''
int main(void) {
    nvk_cmdbuf c={0};
    VkDescriptorSet sets[3]={(VkDescriptorSet)10,(VkDescriptorSet)20,(VkDescriptorSet)30};
    unsigned offsets[2]={0,256}; VkPipelineLayout layout=(VkPipelineLayout)40;
    nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==1);
    for(unsigned i=0;i<10000;i++) nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets);
    assert(descriptor_calls==1);
    offsets[0]=256; nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==2);
    offsets[1]=512; nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==3);
    sets[1]=(VkDescriptorSet)21; nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==4);
    layout=(VkPipelineLayout)41; nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==5);
    nvmtl_bind_graphics_sets(&c,layout,3,sets,2,offsets); assert(descriptor_calls==6);
    sets[2]=(VkDescriptorSet)31; nvmtl_bind_graphics_sets(&c,layout,3,sets,2,offsets); assert(descriptor_calls==7);
    nvmtl_bind_graphics_sets(&c,layout,3,sets,0,NULL); assert(descriptor_calls==8);
    offsets[0]=768; nvmtl_bind_graphics_sets(&c,layout,3,sets,0,offsets); assert(descriptor_calls==8);
    nvmtl_bind_graphics_sets(&c,layout,3,sets,2,offsets); assert(descriptor_calls==9);
    nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==10);
    nvk_pipeline a={.pipe=(void *)50},alias={.pipe=(void *)50},b={.pipe=(void *)51};
    nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(pipeline_calls==1);
    for(unsigned i=0;i<10000;i++) nvmtl_vk_cmd_bind_pipeline(&c,&a);
    nvmtl_vk_cmd_bind_pipeline(&c,&alias); assert(pipeline_calls==1);
    nvmtl_vk_cmd_bind_pipeline(&c,&b); assert(pipeline_calls==2);
    nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(pipeline_calls==3);
    // Fresh command recording cannot reuse state from a recycled pool or an earlier event segment.
    memset(&c,0,sizeof c);
    nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(pipeline_calls==4);
    nvmtl_bind_graphics_sets(&c,layout,2,sets,2,offsets); assert(descriptor_calls==11);
    nvk_cmdbuf independent={0};
    nvmtl_bind_graphics_sets(&independent,layout,2,sets,2,offsets); assert(descriptor_calls==12);
    unsigned uniform_offsets[4]={0,256,512,768};
    nvmtl_bind_graphics_sets(&independent,layout,2,sets,4,uniform_offsets); assert(descriptor_calls==13);
    nvmtl_bind_graphics_sets(&independent,layout,2,sets,4,uniform_offsets); assert(descriptor_calls==13);
    for(unsigned i=0;i<4;i++) {
        uniform_offsets[i]+=256;
        nvmtl_bind_graphics_sets(&independent,layout,2,sets,4,uniform_offsets);
        assert(descriptor_calls==14+i);
    }
    nvmtl_bindtable table={0}; c.table=&table;
    nvk_pipeline optimized={.pipe=(void *)60,.gl_uniform_slot={8,0},.gl_uniform_range={80,0}};
    a.gl_uniform_alt=&optimized;
    nvmtl_slot *slot=&table.s[0][7];
    *slot=(nvmtl_slot){.kind=NVMTL_SLOT_BUF,.h=(void *)1,.off=256,.size=4096};
    assert(nvmtl_uniform_cbuf_pick(&c,&a)==&optimized);
    slot->off=4; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&a);
    slot->off=4096; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&a);
    slot->off=0; slot->size=79; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&a);
    slot->size=4096; slot->h=NULL; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&a);
    slot->h=(void *)1; slot->off=(size_t)UINT32_MAX+1;
    slot->size=slot->off+4096; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&a);
    slot->off=0; assert(nvmtl_uniform_cbuf_pick(&c,&a)==&optimized);
    nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(c.graphics_pipeline==optimized.pipe);
    slot->off=4; nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(c.graphics_pipeline==a.pipe);
    slot->off=0; nvmtl_vk_cmd_bind_pipeline(&c,&a); assert(c.graphics_pipeline==optimized.pipe);
    puts("PASS: identical bindings skipped; resource sets, heap, layout, both offsets/counts, pipeline changes, new recordings and independent buffers rebind");
}
'''
with tempfile.TemporaryDirectory(prefix='nvmtl-graphics-bindings-') as tmp:
    path=pathlib.Path(tmp)
    (path/'test.c').write_text(fixture)
    subprocess.run(['xcrun','clang','-O2',str(path/'test.c'),'-I'+str(root/'plugin'),
                    '-I'+str(pathlib.Path.home()/'Vulkan-Headers/include'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)
