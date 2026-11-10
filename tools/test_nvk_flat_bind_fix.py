#!/usr/bin/env python3
"""Fail-closed parser tests and native old-vs-SELF dlopen interposition regression.

Host/dependency exports DO satisfy flat weak imports, including Mesa-style names.
Only the explicit generated-placeholder/no-interposition contract permits SELF.
All generated fixtures and native outputs live in TemporaryDirectory, not the repo.
"""
import importlib.util
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
root = pathlib.Path(__file__).resolve().parents[1]
tool = root / 'tools/nvk_flat_bind_fix.py'
spec = importlib.util.spec_from_file_location('fix', tool)
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)
NAME = '_nvk_CmdBeginQuery'
STREAM = b'\x3e\x41' + NAME.encode() + b'\0\x51\x70\0\x90\0'
GENERATED = (
    ('src/nouveau/vulkan/nvk_entrypoints', 'nvk_'),
    ('src/vulkan/runtime/vk_common_entrypoints', 'vk_common_'),
    ('src/vulkan/runtime/vk_cmd_enqueue_entrypoints', 'vk_cmd_enqueue_'),
    ('src/vulkan/wsi/wsi_common_entrypoints', 'wsi_'),
)


def generated(d):
    for base, prefix in GENERATED:
        p = d / base
        p.parent.mkdir(parents=True, exist_ok=True)
        marker = '/* This file generated from vk_entrypoints_gen.py, don\'t edit directly. */\n'
        p.with_suffix('.c').write_text(marker + '#define VK_ENTRY_USE_WEAK 1\n#include "' + p.name + '.h"\n' +
                                      'const struct table entries = { .CmdBeginQuery = ' + prefix + 'CmdBeginQuery, };\n')
        p.with_suffix('.h').write_text(marker + 'VKAPI_ATTR void VKAPI_CALL ' + prefix +
                                      'CmdBeginQuery(void) VK_ENTRY_WEAK VK_ENTRY_HIDDEN;\n')
    return ['--nvk-generated-dir', str(d), '--accept-no-interposition']


def image(stream=STREAM, lazy=False, trie=b'', separate_trie=False, ntype=1, desc=0xFE40):
    """Minimal thin dylib, one file-backed segment, one undefined weak symbol."""
    dataoff = 512
    stroff = dataoff + len(stream) + 16
    strings = b'\0' + NAME.encode() + b'\0'
    trieoff = stroff + len(strings)
    size = trieoff + len(trie)
    seg = struct.pack('<II16sQQQQiiII', 0x19, 72, b'__DATA', 0, size, 0, size, 3, 3, 0, 0)
    fields = [0x80000022, 48, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    fields[8 if lazy else 4:10 if lazy else 6] = [dataoff, len(stream)]
    if not separate_trie:
        fields[10:12] = [trieoff, len(trie)]
    cmds = seg + struct.pack('<12I', *fields)
    cmds += struct.pack('<6I', 2, 24, dataoff + len(stream), 1, stroff, len(strings))
    if separate_trie:
        cmds += struct.pack('<4I', 0x80000033, 16, trieoff, len(trie))
    header = struct.pack('<IiiIIIII', 0xFEEDFACF, 0x1000007, 3, 6, 4 if separate_trie else 3, len(cmds), 0x80, 0)
    return bytearray(header + cmds + bytes(dataoff - len(header + cmds)) + stream +
                     struct.pack('<IBBHQ', 1, ntype, 0, desc, 0) + strings + trie)


def export_trie():
    edge = NAME.encode() + b'\0'
    return b'\0\x01' + edge + bytes([3 + len(edge)]) + b'\x02\0\0\0'


class ParserTests(unittest.TestCase):
    def rejected(self, data):
        with self.assertRaises(SystemExit):
            fix.plan(bytes(data))

    def test_generic_flat_refused(self):
        flips, flat, refused = fix.plan(bytes(image()))
        self.assertEqual(flat, {NAME})
        self.assertTrue(refused)
        self.assertFalse(flips)

    def test_load_command_bounds(self):
        for field, value in ((20, 8), (36, 0), (36, 7), (36, 0xFFFFFFF8), (16, 200)):
            with self.subTest(field=field, value=value):
                b = image(); struct.pack_into('<I', b, field, value)
                self.rejected(b)
        self.rejected(b'\xcf\xfa\xed\xfe')

    def test_command_specific_bounds(self):
        b = image(); struct.pack_into('<I', b, 32, 0x80000022)
        self.rejected(b)  # duplicate dyld info / wrong command size
        b = image(); struct.pack_into('<I', b, 104 + 16, len(b) - 1)
        self.rejected(b)
        b = image(); struct.pack_into('<I', b, 152 + 20, 1)
        self.rejected(b)  # string table cannot contain the symbol name
        b = image(); struct.pack_into('<II', b, 104 + 8, 512, len(STREAM))
        self.rejected(b)  # rebase stream must not alias the modified bind stream

    def test_stream_bounds_and_opcodes(self):
        for stream in (b'\x20\x80', b'\x41unterminated', b'\xd0\x01\xd1\0',
                       b'\x3e\x41' + NAME.encode() + b'\0\x90\0',
                       STREAM[:-1], STREAM + b'\x90', b'\x20' + b'\xff' * 11 + b'\0',
                       STREAM.replace(b'\0\x51\x70', b'\0\x52\x70'),
                       STREAM.replace(b'\x70\0', b'\x7f\0')):
            with self.subTest(stream=stream):
                self.rejected(image(stream))

    def test_shared_opcode_addends_and_zero_repeat(self):
        # One nonweak use poisons the entire shared SET_DYLIB_SPECIAL opcode.
        mixed = STREAM[:-1] + b'\x40' + NAME.encode() + b'\0\x90\0'
        self.assertEqual(fix.plan(bytes(image(mixed)), {NAME})[0], set())
        added = STREAM.replace(b'\0\x51\x70', b'\0\x51\x60\x01\x70')
        self.assertTrue(fix.plan(bytes(image(added)), {NAME})[2])
        # A zero-count bind does not actually import the selected symbol.
        zero = STREAM[:-2] + b'\xc0\0\0\0'
        self.assertEqual(fix.plan(bytes(image(zero)), {NAME})[1], set())

    def test_reexport_and_force_flat_rejected(self):
        for flags in (0, 0x180):
            b = image(); struct.pack_into('<I', b, 24, flags)
            with self.assertRaises(SystemExit):
                fix.plan(bytes(b), {NAME})
        # Add an otherwise unused LC_REEXPORT_DYLIB before payload padding.
        b = image(); struct.pack_into('<I', b, 16, 4)
        struct.pack_into('<I', b, 20, 72 + 48 + 24 + 24)
        b[176:200] = struct.pack('<6I', 0x8000001F, 24, 24, 0, 0, 0)
        with self.assertRaises(SystemExit):
            fix.plan(bytes(b), {NAME})

    def test_uint64_backward_address_and_zero_fill(self):
        # ld64 uses uint64(-8) to return to an earlier slot, not a signed LEB.
        stream = STREAM[:-1] + b'\x80' + b'\xf8' + b'\xff' * 8 + b'\x01\x90\0'
        self.assertEqual(fix.plan(bytes(image(stream)))[1], {NAME})
        # A valid bind can target zero-fill memory beyond the file-backed bytes.
        b = image(STREAM.replace(b'\x70\0', b'\x70\xff\x7f'))
        struct.pack_into('<Q', b, 32 + 32, 32768)
        self.assertEqual(fix.plan(bytes(b))[1], {NAME})

    def test_slot_inventory_preserves_repeats_and_addends(self):
        slots = []
        stream = STREAM[:-2] + b'\xc0\x03\x08\0'
        fix.scan(stream, 0, len(stream), False, [64], slots=slots)
        self.assertEqual(slots, [(-2, NAME, 1, 0, offset, 0) for offset in (0, 16, 32)])
        added = STREAM.replace(b'\0\x51\x70', b'\0\x51\x60\x7f\x70')
        slots = []
        fix.scan(added, 0, len(added), False, [64], slots=slots)
        self.assertEqual(slots, [(-2, NAME, 1, 0, 0, -1)])

    def test_exact_rewrite_verifier(self):
        original = bytes(image()); candidate = bytearray(original); candidate[512] = 0x30
        self.assertEqual(fix.verify_rewrite(original, bytes(candidate), {NAME}, 512), {512})
        for data, allowed, offset in ((original, {NAME}, 512), (bytes(candidate) + b'x', {NAME}, 512),
                                      (bytes(candidate), None, 512), (bytes(candidate), {NAME}, 513)):
            with self.subTest(offset=offset, allowed=allowed), self.assertRaises(SystemExit):
                fix.verify_rewrite(original, data, allowed, offset)
        candidate[28] ^= 1  # unrelated header byte must NOT be exempted
        with self.assertRaises(SystemExit):
            fix.verify_rewrite(original, bytes(candidate), {NAME})
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td); args = generated(d)
            (d / 'in').write_bytes(original); (d / 'out').write_bytes(bytes(candidate))
            opts = [sys.executable, str(tool), '--verify-rewrite', '--expected-opcode-offset', '512', *args,
                    str(d / 'in'), str(d / 'out')]
            r = subprocess.run(opts, capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            candidate[28] ^= 1; (d / 'out').write_bytes(candidate)
            r = subprocess.run(opts, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((d / 'out').read_bytes(), bytes(candidate))

    def test_lazy_done_resets_state(self):
        self.rejected(image(STREAM + b'\x70\x08\x90\0', lazy=True))
        # Complete lazy records are accepted; no state may leak across DONE.
        self.assertEqual(fix.plan(bytes(image(STREAM + STREAM, lazy=True)))[1], {NAME})

    def test_trie_exports_and_symtab_are_both_checked(self):
        # The symtab deliberately says undefined while the dyld trie exports it.
        for separate in (False, True):
            b = image(trie=export_trie(), separate_trie=separate)
            self.assertIn(NAME, fix.exports(bytes(b), fix.load_commands(b)))
        b = image(ntype=0x0F)
        self.assertIn(NAME, fix.exports(bytes(b), fix.load_commands(b)))
        for trie in (b'\0\x01x\0\0', b'\x7f\0', b'\0\x01x\0\xff'):
            self.rejected(image(trie=trie))

    def test_no_output_written_on_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            (d / 'in').write_bytes(image())
            (d / 'out').write_bytes(b'keep')
            r = subprocess.run([sys.executable, str(tool), str(d / 'in'), str(d / 'out')], capture_output=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual((d / 'out').read_bytes(), b'keep')

    def test_provenance_contract(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td); args = generated(d)
            (d / 'in').write_bytes(image())
            def run(*opts):
                return subprocess.run([sys.executable, str(tool), *opts, str(d / 'in'), str(d / 'out')], capture_output=True)
            self.assertNotEqual(run(*args[:-1]).returncode, 0)
            ok = run(*args)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            out = (d / 'out').read_bytes()
            self.assertEqual([i for i, (a, b) in enumerate(zip((d / 'in').read_bytes(), out)) if a != b], [512])
            for data in (image(ntype=0x0F), image(desc=0xFE00), image(desc=0xFF40),
                         image(trie=export_trie()), image(separate_trie=True, trie=export_trie())):
                (d / 'in').write_bytes(data)
                self.assertNotEqual(run(*args).returncode, 0)
            (d / 'in').write_bytes(image())
            p = d / 'src/nouveau/vulkan/nvk_entrypoints.h'
            p.write_text(p.read_text().replace('VK_ENTRY_WEAK', 'NOT_WEAK'))
            self.assertNotEqual(run(*args).returncode, 0)


@unittest.skipUnless(sys.platform == 'darwin', 'native Mach-O dlopen regression requires macOS')
class NativeTests(unittest.TestCase):
    def test_host_dependency_and_null_contract(self):
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            def run(*a, **kwargs):
                return subprocess.run(a, cwd=d, capture_output=True, text=True, **kwargs)
            def cc(*a):
                r = run('xcrun', 'clang', *a)
                self.assertEqual(r.returncode, 0, r.stderr)
            lib = '''extern int nvk_CmdBeginQuery(void) __attribute__((weak_import));
int (*const table[])(void) = { nvk_CmdBeginQuery };
int count_present(void) { return table[0] != 0; }
'''
            host = '''#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#ifdef EXPORT
int nvk_CmdBeginQuery(void) { return 7; }
#endif
int main(int argc, char **argv) {
 if (argc > 2 && !dlopen(argv[2], RTLD_NOW | RTLD_GLOBAL)) return 2;
 const char *flags = getenv("NVK_TEST_DLOPEN_FLAGS");
 void *h = dlopen(argv[1], flags ? atoi(flags) : RTLD_NOW | RTLD_LOCAL);
 if (!h) { puts(dlerror()); return 3; }
 int (*count)(void) = (int (*)(void))dlsym(h, "count_present");
 printf("%d\\n", count()); return 0;
}
'''
            (d / 'lib.c').write_text(lib); (d / 'host.c').write_text(host)
            (d / 'dep.c').write_text('int nvk_CmdBeginQuery(void) { return 7; }')
            cc('-dynamiclib', '-Wl,-undefined,dynamic_lookup', '-Wl,-no_fixup_chains', 'lib.c', '-o', 'raw.dylib')
            cc('host.c', '-o', 'host'); cc('-DEXPORT', 'host.c', '-Wl,-export_dynamic', '-o', 'export-host')
            cc('-dynamiclib', 'dep.c', '-o', 'dep.dylib')
            # Reproduce the OLD transform directly, independent of the new safety policy.
            raw = (d / 'raw.dylib').read_bytes()
            cmds = fix.load_commands(raw)
            info = next(o for c, o, _ in cmds if c in (0x22, 0x80000022))
            off, size = struct.unpack_from('<II', raw, info + 16)
            old = bytearray(raw)
            for at, ordinal, _, _ in fix.scan(raw, off, size, False):
                if ordinal == -2:
                    old[at] = 0x30
            (d / 'old-self.dylib').write_bytes(old)
            r = run('codesign', '--force', '--sign', '-', 'old-self.dylib')
            self.assertEqual(r.returncode, 0, r.stderr)
            for flags in (os.RTLD_NOW | os.RTLD_LOCAL, os.RTLD_LAZY | os.RTLD_LOCAL, os.RTLD_NOW | os.RTLD_LOCAL | 0x100):
                for host_name, dep, expected in (('host', [], '0'), ('export-host', [], '1'), ('host', [str(d / 'dep.dylib')], '1')):
                    env = dict(os.environ, NVK_TEST_DLOPEN_FLAGS=str(flags))
                    before = run(str(d / host_name), str(d / 'raw.dylib'), *dep, env=env)
                    after = run(str(d / host_name), str(d / 'old-self.dylib'), *dep, env=env)
                    self.assertEqual((before.returncode, before.stdout.strip()), (0, expected), before.stderr)
                    self.assertEqual((after.returncode, after.stdout.strip()), (0, '0'), after.stderr)
                    print('dlopen flags=%d %s%s: flat=%s old-SELF=%s' % (flags, host_name, '+dependency' if dep else '', before.stdout.strip(), after.stdout.strip()))
            # Even a matching nvk_ name must be refused without the explicit contract.
            r = run(sys.executable, str(tool), 'raw.dylib', 'refused.dylib')
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((d / 'refused.dylib').exists())
            args = generated(d / 'generated')
            r = run(sys.executable, str(tool), *args, 'raw.dylib', 'fixed.dylib')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((d / 'fixed.dylib').read_bytes(), bytes(old))
            check = run(sys.executable, str(tool), '--check', 'fixed.dylib')
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual(run('codesign', '--force', '--sign', '-', 'fixed.dylib').returncode, 0)
            for host_name, dep in (('host', []), ('export-host', []), ('host', [str(d / 'dep.dylib')])):
                actual = run(str(d / host_name), str(d / 'fixed.dylib'), *dep)
                self.assertEqual((actual.returncode, actual.stdout.strip()), (0, '0'), actual.stderr)
            # A generic runtime weak import is never admitted by the generated allowlist.
            (d / 'lib.c').write_text(lib.replace('nvk_CmdBeginQuery', 'optional_runtime'))
            cc('-dynamiclib', '-Wl,-undefined,dynamic_lookup', '-Wl,-no_fixup_chains', 'lib.c', '-o', 'generic.dylib')
            self.assertNotEqual(run(sys.executable, str(tool), *args, 'generic.dylib', 'generic-fixed.dylib').returncode, 0)
            # Own definition is exported only through the trie (strip symtab below).
            (d / 'lib.c').write_text(lib.replace('extern int nvk_CmdBeginQuery(void) __attribute__((weak_import));', 'int nvk_CmdBeginQuery(void) { return 1; }'))
            cc('-dynamiclib', '-Wl,-flat_namespace', '-Wl,-no_fixup_chains', 'lib.c', '-o', 'own.dylib')
            own = bytearray((d / 'own.dylib').read_bytes())
            for c, o, _ in fix.load_commands(own):
                if c == 2:
                    struct.pack_into('<I', own, o + 12, 0)
            (d / 'own.dylib').write_bytes(own)
            self.assertNotEqual(run(sys.executable, str(tool), *args, 'own.dylib', 'own-fixed.dylib').returncode, 0)


# Explicit opt-in: this exercises the real GPU/ICD, never an installed replacement.
# NVMTL_NVK_QUALIFY_BUILD=$HOME/nvmtl-build/rebase263/mesa/build263 \
#   python3 tools/test_nvk_flat_bind_fix.py ActualICDTests
QUALIFY_BUILD = os.environ.get('NVMTL_NVK_QUALIFY_BUILD')

ACTUAL_PROBE = r'''
#define VK_NO_PROTOTYPES 1
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr, "failed line %d: %s\n", __LINE__, #x); return 2; } } while (0)
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static int slots(const char *path, const char *manifest, int flags) {
 double start = now(); void *h = dlopen(path, flags); double ms = (now() - start) * 1000;
 if (!h) { fprintf(stderr, "%s\n", dlerror()); return 2; }
 fprintf(stderr, "dlopen_ms=%.3f flags=%d\n", ms, flags);
 Dl_info own; REQUIRE(dladdr(dlsym(h, "vk_icdGetInstanceProcAddr"), &own));
 FILE *f = fopen(manifest, "r"); REQUIRE(f);
 unsigned long long offset; char name[512]; unsigned count = 0, collisions = 0;
 while (fscanf(f, "%llx %511s", &offset, name) == 2) {
  void *value = *(void **)((char *)own.dli_fbase + offset);
  if (!value) printf("%llx %s NULL\n", offset, name);
  else { Dl_info info; REQUIRE(dladdr(value, &info));
   printf("%llx %s %s+%llx\n", offset, name,
          info.dli_fbase == own.dli_fbase ? "ICD" : info.dli_fname,
          (unsigned long long)((char *)value - (char *)info.dli_fbase)); }
  /* FIRST limits dlsym to each image, so a transitive hit is not misattributed.
   * Check the executable plus every loaded dependency/framework, not just LC_LOAD_DYLIB.
   * This proves the current namespace only, never arbitrary future host processes. */
  void *main = dlopen(NULL, RTLD_NOW | RTLD_FIRST);
  if (dlsym(main, name + 1)) { fprintf(stderr, "collision %s executable\n", name); collisions++; }
  dlclose(main);
  for (uint32_t i = 0; i < _dyld_image_count(); i++) {
   const char *image = _dyld_get_image_name(i);
   void *dep = dlopen(image, RTLD_NOW | RTLD_NOLOAD | RTLD_FIRST);
   if (!dep) continue;
   if (dlsym(dep, name + 1)) { fprintf(stderr, "collision %s %s\n", name, image); collisions++; }
   dlclose(dep);
  }
  count++;
 }
 fclose(f); fprintf(stderr, "slots=%u collisions=%u images=%u\n", count, collisions, _dyld_image_count());
 return collisions ? 3 : 0;
}
static int has_ext(VkExtensionProperties *ext, uint32_t n, const char *name) {
 for (uint32_t i = 0; i < n; i++) if (!strcmp(ext[i].extensionName, name)) return 1;
 return 0;
}
static int loader(const char *path, uint32_t api, int enable) {
 void *h = dlopen(path, RTLD_NOW | RTLD_LOCAL); REQUIRE(h);
 PFN_vkGetInstanceProcAddr gipa = (PFN_vkGetInstanceProcAddr)dlsym(h, "vkGetInstanceProcAddr"); REQUIRE(gipa);
 PFN_vkCreateInstance create = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance"); REQUIRE(create);
 PFN_vkEnumerateInstanceExtensionProperties iext = (PFN_vkEnumerateInstanceExtensionProperties)gipa(NULL, "vkEnumerateInstanceExtensionProperties"); REQUIRE(iext);
 uint32_t n = 0; REQUIRE(iext(NULL, &n, NULL) == VK_SUCCESS);
 VkExtensionProperties *ext = calloc(n, sizeof(*ext)); REQUIRE(ext);
 REQUIRE(iext(NULL, &n, ext) == VK_SUCCESS);
 const char *iprop = VK_KHR_GET_PHYSICAL_DEVICE_PROPERTIES_2_EXTENSION_NAME;
 REQUIRE(has_ext(ext, n, iprop)); free(ext);
 VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = api };
 VkInstanceCreateInfo ci = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app,
                            .enabledExtensionCount = enable, .ppEnabledExtensionNames = &iprop };
 VkInstance inst; REQUIRE(create(&ci, NULL, &inst) == VK_SUCCESS);
 #define IP(type, name) type name = (type)gipa(inst, #name); REQUIRE(name)
 IP(PFN_vkEnumeratePhysicalDevices, vkEnumeratePhysicalDevices);
 IP(PFN_vkGetPhysicalDeviceProperties, vkGetPhysicalDeviceProperties);
 IP(PFN_vkGetPhysicalDeviceQueueFamilyProperties, vkGetPhysicalDeviceQueueFamilyProperties);
 IP(PFN_vkEnumerateDeviceExtensionProperties, vkEnumerateDeviceExtensionProperties);
 IP(PFN_vkCreateDevice, vkCreateDevice);
 IP(PFN_vkGetDeviceProcAddr, vkGetDeviceProcAddr);
 IP(PFN_vkDestroyInstance, vkDestroyInstance);
 REQUIRE(vkEnumeratePhysicalDevices(inst, &n, NULL) == VK_SUCCESS && n);
 VkPhysicalDevice *devices = calloc(n, sizeof(*devices)); REQUIRE(devices);
 REQUIRE(vkEnumeratePhysicalDevices(inst, &n, devices) == VK_SUCCESS);
 VkPhysicalDevice physical = NULL; VkPhysicalDeviceProperties props;
 for (uint32_t i = 0; i < n; i++) { vkGetPhysicalDeviceProperties(devices[i], &props); if (props.vendorID == 0x10de) { physical = devices[i]; break; } }
 free(devices); REQUIRE(physical); REQUIRE(props.apiVersion >= api);
 printf("api=%u.%u ext=%d gpu=%s\n", VK_API_VERSION_MAJOR(api), VK_API_VERSION_MINOR(api), enable, props.deviceName);
 /* Do not ask core 1.1 commands of a 1.0 instance. KHR alias is queried only when enabled. */
 if (api >= VK_API_VERSION_1_1) {
  IP(PFN_vkGetPhysicalDeviceProperties2, vkGetPhysicalDeviceProperties2);
  VkPhysicalDeviceProperties2 p = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 };
  vkGetPhysicalDeviceProperties2(physical, &p); REQUIRE(p.properties.vendorID == props.vendorID);
  puts("core vkGetPhysicalDeviceProperties2=present/called");
 }
 if (enable) {
  IP(PFN_vkGetPhysicalDeviceProperties2KHR, vkGetPhysicalDeviceProperties2KHR);
  VkPhysicalDeviceProperties2 p = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 };
  vkGetPhysicalDeviceProperties2KHR(physical, &p); REQUIRE(p.properties.vendorID == props.vendorID);
  puts("extension vkGetPhysicalDeviceProperties2KHR=present/called");
 }
 REQUIRE(!gipa(inst, "vkNullMothNonexistentCommand"));
 vkGetPhysicalDeviceQueueFamilyProperties(physical, &n, NULL); REQUIRE(n);
 VkQueueFamilyProperties *families = calloc(n, sizeof(*families)); REQUIRE(families);
 vkGetPhysicalDeviceQueueFamilyProperties(physical, &n, families);
 uint32_t family = 0; while (family < n && !(families[family].queueFlags & VK_QUEUE_GRAPHICS_BIT)) family++;
 free(families); REQUIRE(family < n);
 REQUIRE(vkEnumerateDeviceExtensionProperties(physical, NULL, &n, NULL) == VK_SUCCESS);
 ext = calloc(n, sizeof(*ext)); REQUIRE(ext);
 REQUIRE(vkEnumerateDeviceExtensionProperties(physical, NULL, &n, ext) == VK_SUCCESS);
 const char *maintenance = VK_KHR_MAINTENANCE_1_EXTENSION_NAME;
 REQUIRE(has_ext(ext, n, maintenance));
 /* An actually unsupported extension, not merely an unenabled supported one. */
 const char *unsupported_ext[] = { "VK_NV_cuda_kernel_launch", "VK_FUCHSIA_buffer_collection", "VK_ANDROID_external_memory_android_hardware_buffer" };
 const char *unsupported_cmd[] = { "vkCreateCudaModuleNV", "vkCreateBufferCollectionFUCHSIA", "vkGetAndroidHardwareBufferPropertiesANDROID" };
 int unsupported = -1;
 for (int i = 0; i < 3; i++) if (!has_ext(ext, n, unsupported_ext[i])) { unsupported = i; break; }
 REQUIRE(unsupported >= 0); free(ext);
 float priority = 1;
 VkDeviceQueueCreateInfo qi = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority };
 VkDeviceCreateInfo di = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qi,
                          .enabledExtensionCount = enable, .ppEnabledExtensionNames = &maintenance };
 VkDevice device; REQUIRE(vkCreateDevice(physical, &di, NULL, &device) == VK_SUCCESS);
 #define DP(type, name) type name = (type)vkGetDeviceProcAddr(device, #name); REQUIRE(name)
 DP(PFN_vkGetDeviceQueue, vkGetDeviceQueue); DP(PFN_vkDeviceWaitIdle, vkDeviceWaitIdle);
 DP(PFN_vkCreateCommandPool, vkCreateCommandPool); DP(PFN_vkDestroyCommandPool, vkDestroyCommandPool);
 DP(PFN_vkDestroyDevice, vkDestroyDevice);
 VkQueue queue; vkGetDeviceQueue(device, family, 0, &queue); REQUIRE(queue);
 REQUIRE(vkDeviceWaitIdle(device) == VK_SUCCESS); puts("core queue/idle=present/called");
 VkCommandPoolCreateInfo pi = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = family };
 VkCommandPool pool; REQUIRE(vkCreateCommandPool(device, &pi, NULL, &pool) == VK_SUCCESS);
 if (api >= VK_API_VERSION_1_1) {
  DP(PFN_vkTrimCommandPool, vkTrimCommandPool); vkTrimCommandPool(device, pool, 0);
  puts("core vkTrimCommandPool=present/called");
 }
 PFN_vkTrimCommandPoolKHR trim = (PFN_vkTrimCommandPoolKHR)vkGetDeviceProcAddr(device, "vkTrimCommandPoolKHR");
 REQUIRE(!!trim == !!enable);
 if (trim) trim(device, pool, 0);
 printf("extension vkTrimCommandPoolKHR=%s\n", trim ? "present/called" : "NULL (disabled)");
 REQUIRE(!vkGetDeviceProcAddr(device, unsupported_cmd[unsupported]));
 printf("unsupported %s=NULL\n", unsupported_cmd[unsupported]);
 REQUIRE(!vkGetDeviceProcAddr(device, "vkNullMothNonexistentCommand"));
 vkDestroyCommandPool(device, pool, NULL); vkDestroyDevice(device, NULL); vkDestroyInstance(inst, NULL);
 return 0;
}
int main(int argc, char **argv) {
 if (argc != 5) return 2;
 if (!strcmp(argv[1], "slots")) return slots(argv[2], argv[3], atoi(argv[4]));
 return loader(argv[2], (uint32_t)strtoul(argv[3], NULL, 10), atoi(argv[4]));
}
'''


@unittest.skipUnless(sys.platform == 'darwin' and QUALIFY_BUILD, 'opt-in actual ICD qualification requires NVMTL_NVK_QUALIFY_BUILD on macOS')
class ActualICDTests(unittest.TestCase):
    def test_all_slots_and_loader_contract(self):
        build = pathlib.Path(QUALIFY_BUILD).resolve()
        raw = build / 'src/nouveau/vulkan/libvulkan_nouveau.dylib'
        loader = pathlib.Path(os.environ.get('NVMTL_NVK_QUALIFY_LOADER', '/Library/GPUBundles/nvmtl/libvulkan.dylib'))
        b = raw.read_bytes(); allowed = fix.generated_placeholders(build)
        flips, flat, refused = fix.plan(b, allowed)
        self.assertFalse(refused); self.assertEqual(len(flat), 2940)
        cmds = fix.load_commands(b)
        segments = [struct.unpack_from('<QQ', b, o + 24) for c, o, _ in cmds if c == 0x19]
        self.assertEqual(segments[0][0], 0)  # native probe uses image-header-relative offsets
        info = next(o for c, o, _ in cmds if c in (fix.LC_DYLD_INFO, fix.LC_DYLD_INFO_ONLY))
        fields = struct.unpack_from('<10I', b, info + 8)
        slots = []
        for offset, size, lazy, weak in ((fields[2], fields[3], False, False), (fields[4], fields[5], False, True), (fields[6], fields[7], True, False)):
            fix.scan(b, offset, size, lazy, [s[1] for s in segments], weak, slots=slots)
        selected = [s for s in slots if s[0] == -2]
        self.assertEqual({s[1] for s in selected}, flat)
        self.assertTrue(all(s[2] & 1 and s[5] == 0 for s in selected))
        self.assertEqual(len({(s[3], s[4]) for s in selected}), len(selected))
        with tempfile.TemporaryDirectory(prefix='nvk-qualification-') as td:
            d = pathlib.Path(td)
            def run(*args, **kwargs):
                return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=120, **kwargs)
            fast = d / 'self.dylib'
            result = run(sys.executable, tool, '--nvk-generated-dir', build, '--accept-no-interposition', raw, fast)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            transformed = fast.read_bytes()
            self.assertEqual(len(transformed), len(b)); self.assertEqual(len(flips), 1)
            self.assertEqual([i for i, (a, c) in enumerate(zip(b, transformed)) if a != c], sorted(flips))
            result = run('codesign', '--force', '--sign', '-', fast)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run('codesign', '--verify', '--strict', fast)
            self.assertEqual(result.returncode, 0, result.stderr)
            published = os.environ.get('NVMTL_NVK_QUALIFY_CANDIDATE')
            if published:
                # Read-only qualification of the actual stripped/signed build output.
                fast = pathlib.Path(published).resolve()
                self.assertNotEqual(fast, raw)
                self.assertFalse(fix.plan(fast.read_bytes())[1])
                result = run('codesign', '--verify', '--strict', fast)
                self.assertEqual(result.returncode, 0, result.stderr)
                print('published candidate:', fast)
            labels = {raw: 'raw', fast: 'SELF'}
            manifest = d / 'slots'
            manifest.write_text(''.join('%x %s\n' % (segments[s[3]][0] + s[4], s[1]) for s in selected))
            source = d / 'probe.c'; source.write_text(ACTUAL_PROBE)
            probe = d / 'probe'
            result = run('xcrun', 'clang', '-O2', '-I' + str(build.parent / 'include'), source, '-o', probe)
            self.assertEqual(result.returncode, 0, result.stderr)
            inventories = []
            # RTLD_LAZY and FIRST do not narrow nonlazy flat weak lookup; retain
            # this measured boundary rather than changing plugin flags speculatively.
            for lib in (raw, fast):
                for flags in (os.RTLD_NOW | os.RTLD_LOCAL, os.RTLD_LAZY | os.RTLD_LOCAL, os.RTLD_NOW | os.RTLD_LOCAL | 0x100):
                    result = run(probe, 'slots', lib, manifest, flags)
                    self.assertEqual(result.returncode, 0, result.stdout[-1000:] + result.stderr)
                    self.assertEqual(len(result.stdout.splitlines()), len(selected))
                    inventories.append(result.stdout)
                    print(labels[lib], result.stderr.strip())
            self.assertTrue(all(inventory == inventories[0] for inventory in inventories))
            # Exact target identity (image + ASLR-independent offset), including NULL,
            # is compared for EVERY affected slot, not a sample or aggregate count.
            print('equivalent slots=%d symbols=%d all_NULL=%s' %
                  (len(selected), len(flat), all(line.endswith(' NULL') for line in inventories[0].splitlines())))
            empty = d / 'empty'; empty.write_text('')
            timings = {}
            for repeat in range(7):
                order = (raw, fast) if repeat % 2 == 0 else (fast, raw)
                modes = (6, 5, 262) if repeat % 2 == 0 else (262, 5, 6)
                for lib in order:
                    for flags in modes:
                        result = run(probe, 'slots', lib, empty, flags)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        ms = float(result.stderr.split('dlopen_ms=')[1].split()[0])
                        timings.setdefault((labels[lib], flags), []).append(ms)
            for (name, flags), samples in sorted(timings.items()):
                print('dlopen benchmark %s flags=%d n=7 median=%.3f min=%.3f max=%.3f ms' %
                      (name, flags, sorted(samples)[3], min(samples), max(samples)))
            outputs = {}
            for lib in (raw, fast):
                icd = d / 'icd.json'
                icd.write_text(json.dumps({'file_format_version': '1.0.0', 'ICD': {'library_path': str(lib), 'api_version': '1.4.0'}}))
                env = dict(os.environ, VK_DRIVER_FILES=str(icd), VK_ICD_FILENAMES=str(icd),
                           VK_INSTANCE_LAYERS='', VK_LOADER_LAYERS_DISABLE='*')
                for minor in (0, 1, 3):
                    for enabled in (0, 1):
                        result = run(probe, 'loader', loader, (1 << 22) | (minor << 12), enabled, env=env)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        key = (minor, enabled)
                        if lib == raw: outputs[key] = result.stdout
                        else: self.assertEqual(result.stdout, outputs[key])
                        print(labels[lib], result.stdout.strip().replace('\n', '; '))


class BuildSafetyTests(unittest.TestCase):
    def test_failed_publish_preserves_previous_verified_output(self):
        script = (root / 'build/build263.sh').read_text()
        fragment = script[script.index('mkdir -p "$O";'):]
        trap = next(line for line in script.splitlines() if line.startswith('trap '))
        known = 'ec60a89dcb54fb65e9edcdb2ca649b4db226170c7a0aa5d1991e8d89da9d0779'
        setup = '''set -euo pipefail
check_symbols(){ [ "$FAIL_AT" != symbols ]; }
check_runtime(){ [ "$FAIL_AT" != minos ]; }
cp(){ [ "$FAIL_AT" != copy ] && command cp "$@"; }
strip(){ [ "$FAIL_AT" != strip ]; }
shasum(){ [ "$FAIL_AT" != hash ] || return 9; if [ "$3" = "$B/src/nouveau/vulkan/libvulkan_nouveau.dylib" ]; then echo "$TEST_RAW_SHA  $3"; else command shasum "$@"; fi; }
python3(){
 if [ "$2" = --verify-rewrite ]; then [ "$FAIL_AT" != diff ] || return 9; echo OPCODE_VERIFIED; return; fi
 case "$FAIL_AT" in fixup|provenance|structural) return 9 ;; esac
 echo SELF_APPLIED; command cp "$5" "$6"
}
codesign(){ if [ "$1" = --force ]; then [ "$FAIL_AT" != sign ]; else [ "$FAIL_AT" != verify ]; fi; }
otool(){ [ "$FAIL_AT" != deps ]; }
mv(){ if [ "$2" = "$O/libvulkan_nouveau.dylib" ] && [ "$FAIL_AT" = publish ]; then return 9; fi; command mv "$@"; }
'''
        # An unset selector MUST take AUTO; explicit1 remains a requalification
        # route for unknown inputs, but no structural/provenance/diff bypass.
        modes = [(None, known, True), ('auto', known, True), ('auto', 'a' * 64, False),
                 ('0', known, False), ('1', known, True), ('1', 'a' * 64, True), ('invalid', known, False)]
        for opt, raw_sha, rewrite in modes:
            phases = ['hash', 'copy', 'strip', 'sign', 'symbols', 'minos', 'verify', 'deps', 'publish', 'none']
            if rewrite: phases += ['fixup', 'provenance', 'structural', 'diff']
            for phase in phases:
                with self.subTest(optimization=opt, raw_sha=raw_sha[:8], failure=phase), tempfile.TemporaryDirectory() as temporary:
                    d = pathlib.Path(temporary); build = d / 'build'; out = d / 'out'; lock = d / 'lock'
                    src = build / 'src/nouveau/vulkan/libvulkan_nouveau.dylib'
                    src.parent.mkdir(parents=True); out.mkdir(); lock.mkdir()
                    (lock / 'owner').write_text('fixture'); src.write_bytes(b'new-unverified')
                    previous = out / src.name; previous.write_bytes(b'previous-good')
                    env = dict(PATH='/usr/bin:/bin', B=str(build), O=str(out), R='unused', L=str(lock),
                               FAIL_AT=phase, TEST_RAW_SHA=raw_sha)
                    if opt is not None: env['NVMTL_NVK_SELF_BIND'] = opt
                    r = subprocess.run(['bash', '-c', setup + trap + '\n' + fragment], env=env,
                                       capture_output=True, text=True, timeout=10)
                    success = phase == 'none' and opt != 'invalid'
                    self.assertEqual(r.returncode == 0, success, r.stdout + r.stderr)
                    self.assertEqual(previous.read_bytes(), b'new-unverified' if success else b'previous-good')
                    self.assertEqual(list(out.iterdir()), [previous]); self.assertFalse(lock.exists())
                    if success:
                        self.assertEqual('SELF_APPLIED' in r.stdout, rewrite, r.stdout)
                        self.assertEqual('OPCODE_VERIFIED' in r.stdout, rewrite, r.stdout)
                        if opt == 'auto' and raw_sha != known: self.assertIn('WARNING', r.stdout + r.stderr)
                        if opt == '1': self.assertIn('experimental', r.stdout + r.stderr)
                        if rewrite: self.assertIn('macOS 26 only', r.stdout + r.stderr)

    def test_runtime_deployment_gate(self):
        script = (root / 'build/build263.sh').read_text()
        start = script.index('check_runtime() {')
        end = script.index('\n}\n', start) + 3
        function = script[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            d = pathlib.Path(temporary)
            for name, text in {'lipo': '#!/bin/sh\necho "$TEST_ARCH"\n',
                               'otool': '#!/bin/sh\nprintf "    minos %s\\n" "$TEST_MINOS"\n'}.items():
                p = d / name; p.write_text(text); p.chmod(0o755)
            for arch, version, ok in [('x86_64','15.0',True), ('x86_64','15.5',True),
                                       ('x86_64','15.8',False), ('x86_64','26.0',False), ('arm64','15.5',False)]:
                env = dict(PATH=str(d)+':/usr/bin:/bin', TEST_ARCH=arch, TEST_MINOS=version)
                r = subprocess.run(['bash','-c','set -euo pipefail\n'+function+'\ncheck_runtime fixture'],
                                   env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
