#!/usr/bin/env python3
"""Qualified NVK generated-placeholder NULL binding, NOT a generic weak-import fix.

Flat weak imports can bind exports in the host or dependencies; SELF cannot. Even
Mesa-style names can be interposed. --accept-no-interposition explicitly accepts
that semantic change: generated missing entrypoints must mean NULL, never a host
implementation. --nvk-generated-dir must name the corresponding Mesa build tree;
its four generated source/header pairs supply exact weak-placeholder names. This
is provenance validation, not proof of the future process's symbol namespace.

Only flat weak undefined symbols in that list, absent from BOTH our export trie
and symbol table, can change. Generic imports, chained/threaded fixups, malformed
or unsupported streams are refused before writing. Already-SELF binds cannot be
retrospectively certified as safe. Re-sign output with codesign --force --sign -.

The build's AUTO policy accepts only its exact qualified raw SHA. Under that
contract, weak hidden Mesa-generated placeholders are module-private: host and
dependency definitions are intentionally ignored, matching upstream ELF intent.
Qualification is on macOS 26 only; minos metadata does not certify macOS 15 dyld.

usage: nvk_flat_bind_fix.py --nvk-generated-dir BUILD --accept-no-interposition IN OUT
       nvk_flat_bind_fix.py --verify-rewrite [--expected-opcode-offset N] --nvk-generated-dir BUILD --accept-no-interposition IN OUT
       nvk_flat_bind_fix.py --check DYLIB  (report flat binds; exit 1 if any)
"""
import argparse
import pathlib
import re
import struct
import sys

LC_DYLD_INFO, LC_DYLD_INFO_ONLY, LC_DYLD_CHAINED_FIXUPS = 0x22, 0x80000022, 0x80000034
LC_SYMTAB = 0x2
FLAT_IMM, SELF_IMM = 0x0E, 0x00
WEAK_IMPORT = 0x1


def fail(message):
    raise SystemExit(message)


def region(b, off, size, label):
    if off < 0 or size < 0 or off > len(b) or size > len(b) - off:
        fail('out-of-bounds ' + label)
    return b[off:off + size]


def uleb(b, i, end=None, signed=False):
    end = len(b) if end is None else end
    v = 0
    for n in range(10):
        if i >= end:
            fail('truncated LEB')
        c = b[i]; i += 1
        if n == 9 and (c not in (0, 0x7F) if signed else c > 1):
            fail('overflowing LEB')
        v |= (c & 0x7F) << (7 * n)
        if not c & 0x80:
            if signed and c & 0x40:
                v -= 1 << (7 * (n + 1))
            return v, i
    fail('overflowing LEB')


def cstr(b, i, end=None):
    end = len(b) if end is None else end
    j = b.find(b'\0', i, end)
    if i < 0 or j < 0:
        fail('unterminated string')
    return b[i:j].decode('latin-1'), j + 1


def load_commands(b):
    region(b, 0, 32, 'Mach-O header')
    magic, cpu, _, kind, ncmds, cmdsize, _, _ = struct.unpack_from('<IiiIIIII', b)
    if magic != 0xFEEDFACF or cpu not in (0x1000007, 0x100000C) or kind != 6:
        fail('not a supported thin 64-bit little-endian dylib')
    region(b, 32, cmdsize, 'load commands')
    end = 32 + cmdsize
    if ncmds > cmdsize // 8:
        fail('invalid load command count')
    off, out = 32, []
    exact = {LC_DYLD_INFO: 48, LC_DYLD_INFO_ONLY: 48, LC_SYMTAB: 24,
             0x80000033: 16, LC_DYLD_CHAINED_FIXUPS: 16, 0x26: 16, 0x29: 16, 0x1D: 16}
    for _ in range(ncmds):
        if off + 8 > end:
            fail('truncated load command')
        cmd, size = struct.unpack_from('<II', b, off)
        if size < 8 or size % 8 or size > end - off or (cmd in exact and size != exact[cmd]):
            fail('invalid load command size')
        if cmd == 0x19:
            if size < 72:
                fail('short segment command')
            _, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ', b, off + 24)
            nsects = struct.unpack_from('<I', b, off + 64)[0]
            if size != 72 + nsects * 80 or filesize > vmsize:
                fail('invalid segment command')
            region(b, fileoff, filesize, 'segment')
        out.append((cmd, off, size)); off += size
    if off != end:
        fail('load commands do not fill sizeofcmds')
    return out


def symbols(b, cmds):
    records = {}
    tables = [o for c, o, _ in cmds if c == LC_SYMTAB]
    if len(tables) != 1:
        fail('expected one symbol table')
    symoff, nsyms, stroff, strsize = struct.unpack_from('<IIII', b, tables[0] + 8)
    region(b, symoff, nsyms * 16, 'symbol table')
    region(b, stroff, strsize, 'string table')
    for k in range(nsyms):
        strx, ntype, _, desc, _ = struct.unpack_from('<IBBHQ', b, symoff + 16 * k)
        if strx >= strsize:
            fail('symbol string index out of range')
        name, _ = cstr(b, stroff + strx, stroff + strsize)
        if not ntype & 0xE0:  # ignore debug/stab entries
            records.setdefault(name, []).append((ntype, desc))
    return records


def trie_exports(b, off, size):
    trie = region(b, off, size, 'export trie')
    if not size:
        return set()
    names, seen, todo = set(), set(), [(0, '')]
    while todo:
        i, prefix = todo.pop()
        if i in seen or i >= size or len(prefix) > size:
            fail('cyclic/shared or invalid export trie node')
        seen.add(i)
        terminal, i = uleb(trie, i)
        terminal_end = i + terminal
        if terminal_end >= size:
            fail('truncated export trie terminal')
        if terminal:
            flags, p = uleb(trie, i, terminal_end)
            if flags & ~0x1F or flags & 3 == 3:
                fail('unsupported export flags')
            _, p = uleb(trie, p, terminal_end)
            if flags & 8:  # reexport ordinal + imported name
                _, p = cstr(trie, p, terminal_end)
            elif flags & 16:  # stub and resolver
                _, p = uleb(trie, p, terminal_end)
            if p != terminal_end:
                fail('invalid export terminal')
            names.add(prefix)
        i = terminal_end
        children = trie[i]; i += 1
        for _ in range(children):
            edge, i = cstr(trie, i)
            if not edge:
                fail('empty export trie edge')
            child, i = uleb(trie, i)
            todo.append((child, prefix + edge))
    return names


def exports(b, cmds):
    """Union of dyld export trie (including reexports) and defined externals."""
    names = {name for name, entries in symbols(b, cmds).items()
             if any(t & 1 and t & 0x0E for t, _ in entries)}
    tries = []
    for c, o, _ in cmds:
        if c in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY):
            tries.append(struct.unpack_from('<II', b, o + 40))
        elif c == 0x80000033:  # LC_DYLD_EXPORTS_TRIE
            tries.append(struct.unpack_from('<II', b, o + 8))
    nonempty = {t for t in tries if t[1]}
    if len(nonempty) > 1:
        fail('conflicting export tries')
    for off, size in nonempty:
        names.update(trie_exports(b, off, size))
    return names


def scan(b, start, size, lazy, segments=None, weak=False, slots=None):
    """Parse pointer binds; optional slots records every (ordinal, name, flags,
    segment, offset, addend), including repeated binds, for native equivalence tests.
    Threaded binding is intentionally unsupported. Policy/rewrites are unchanged.
    """
    region(b, start, size, 'bind stream')
    i, end = start, start + size
    special_at, ordinal, sym, flags, segment, address, addend = None, -3 if weak else None, None, 0, None, 0, 0
    binds, done = [], False
    while i < end:
        op = b[i]; hi, imm = op & 0xF0, op & 0x0F; at = i; i += 1
        done = False
        if hi == 0:
            if imm:
                fail('invalid DONE opcode')
            done = True
            if not lazy:
                if any(b[i:end]):
                    fail('nonzero bytes after DONE')
                return binds
            special_at, ordinal, sym, flags, segment, address, addend = None, None, None, 0, None, 0, 0
            continue
        if hi == 0x10:
            ordinal, special_at = imm, None
        elif hi == 0x20:
            ordinal, i = uleb(b, i, end); special_at = None
        elif hi == 0x30:
            ordinal, special_at = (imm | ~0xF) if imm else 0, at
        elif hi == 0x40:
            if imm & ~(1 | (8 if weak else 0)):
                fail('unsupported symbol flags')
            flags = imm; sym, i = cstr(b, i, end)
        elif hi == 0x50:
            if imm != 1:
                fail('only pointer bind type is supported')
        elif hi == 0x60:
            if imm:
                fail('invalid addend opcode')
            addend, i = uleb(b, i, end, signed=True)
        elif hi == 0x70:
            segment = imm; address, i = uleb(b, i, end)
            if segments is not None and segment >= len(segments):
                fail('invalid bind segment')
        elif hi == 0x80:
            if imm or segment is None:
                fail('invalid address opcode/state')
            delta, i = uleb(b, i, end)
            # ld64 encodes backward moves as uint64 ULEBs; match dyld's wrap.
            address = (address + delta) & ((1 << 64) - 1)
        elif hi in (0x90, 0xA0, 0xB0, 0xC0):
            count, skip = 1, 0
            if hi in (0x90, 0xA0, 0xC0) and imm:
                fail('invalid bind opcode immediate')
            if hi == 0xA0:
                skip, i = uleb(b, i, end)
            elif hi == 0xB0:
                skip = imm * 8
            elif hi == 0xC0:
                count, i = uleb(b, i, end); skip, i = uleb(b, i, end)
            if ordinal is None or not sym or segment is None:
                fail('incomplete bind state (including lazy DONE reset)')
            if count:
                last = address + (count - 1) * (8 + skip)
                if last > (1 << 64) - 8 or (segments is not None and last + 8 > segments[segment]):
                    fail('bind target out of segment bounds: segment %d offset 0x%x' % (segment, last))
                # A nonzero addend would not yield NULL for a missing weak symbol.
                binds.append((special_at, ordinal, sym, flags if not addend else 0))
                if slots is not None:
                    if count > 1000000:
                        fail('slot inventory exceeds bounded expansion limit')
                    slots.extend((ordinal, sym, flags, segment, address + j * (8 + skip), addend)
                                 for j in range(count))
            address = (address + count * (8 + skip)) & ((1 << 64) - 1)
        elif hi == 0xD0:
            fail('threaded binds: not handled')
        else:
            fail('unknown bind opcode 0x%02x at 0x%x' % (op, at))
    if size and not done:
        fail('bind stream missing DONE')
    return binds


def generated_placeholders(directory):
    """Exact declarations, not prefix-only matching; caller asserts matching build provenance."""
    names = set()
    pairs = (('src/nouveau/vulkan/nvk_entrypoints', 'nvk_'),
             ('src/vulkan/runtime/vk_common_entrypoints', 'vk_common_'),
             ('src/vulkan/runtime/vk_cmd_enqueue_entrypoints', 'vk_cmd_enqueue_'),
             ('src/vulkan/wsi/wsi_common_entrypoints', 'wsi_'))
    marker = "This file generated from vk_entrypoints_gen.py, don't edit directly."
    for base, prefix in pairs:
        p = pathlib.Path(directory) / base
        source, header = p.with_suffix('.c').read_text(), p.with_suffix('.h').read_text()
        if (marker not in source or marker not in header or
                '#define VK_ENTRY_USE_WEAK 1' not in source or
                '#include "' + p.name + '.h"' not in source):
            fail('unrecognized generated placeholder provenance: ' + str(p))
        declarations = re.findall(r'^\s*VKAPI_ATTR\s+\w+\s+VKAPI_CALL\s+(' + re.escape(prefix) +
                                  r'[A-Z][A-Za-z0-9_]*)\([^;\n]*\)\s+VK_ENTRY_WEAK\s+VK_ENTRY_HIDDEN;', header, re.M)
        if not declarations:
            fail('no generated weak declarations: ' + str(p))
        if prefix == 'vk_cmd_enqueue_':
            declarations += re.findall(
                r'^\s*VKAPI_ATTR\s+\w+\s+VKAPI_CALL\s+(vk_cmd_enqueue_unless_primary_[A-Z][A-Za-z0-9_]*)'
                r'\([^;\n]*\)\s+VK_ENTRY_WEAK\s+VK_ENTRY_HIDDEN;', header, re.M)
        table_refs = set(re.findall(r'\.\w+\s*=\s*(\w+)\s*,', source))
        if not set(declarations) <= table_refs:
            fail('weak declarations missing generated dispatch-table references: ' + str(p))
        names.update('_' + name for name in declarations)
    return names


def plan(b, allowed=None):
    cmds = load_commands(b)
    if any(c == LC_DYLD_CHAINED_FIXUPS for c, _, _ in cmds):
        fail('chained fixups: not handled')
    if allowed is not None:
        flags = struct.unpack_from('<I', b, 24)[0]
        # SELF can search reexported dependencies too; absent own exports is not
        # sufficient for such images. Legacy umbrella/sub-library rules likewise.
        if not flags & 0x80 or flags & 0x100:
            fail('rewrite requires two-level namespace without MH_FORCE_FLAT')
        if any(c in (0x8000001F, 0x12, 0x13, 0x14, 0x15) for c, _, _ in cmds):
            fail('reexport/umbrella libraries: not handled')
    info = [off for c, off, _ in cmds if c in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY)]
    if len(info) != 1:
        fail('expected one LC_DYLD_INFO')
    fields = struct.unpack_from('<10I', b, info[0] + 8)
    ranges = [(fields[i], fields[i + 1]) for i in range(0, 10, 2) if fields[i + 1]]
    for c, o, _ in cmds:
        if c == LC_SYMTAB:
            so, ns, st, ss = struct.unpack_from('<4I', b, o + 8)
            ranges += [(so, ns * 16), (st, ss)]
        elif c in (0x80000033, 0x26, 0x29, 0x1D):
            payload = struct.unpack_from('<2I', b, o + 8)
            # Only the two export-trie command forms may name the same bytes.
            if c != 0x80000033 or payload != (fields[8], fields[9]):
                ranges.append(payload)
    payload_start = 32 + struct.unpack_from('<I', b, 20)[0]
    previous_end = payload_start
    for off, size in sorted(ranges):
        if not size:
            continue
        region(b, off, size, 'linkedit payload')
        if off < previous_end:
            fail('overlapping linkedit/load-command regions')
        previous_end = off + size
    mine, records = exports(b, cmds), symbols(b, cmds)
    # Dyld may bind zero-fill (__bss) slots beyond a segment's file-backed bytes.
    segments = [struct.unpack_from('<Q', b, o + 32)[0] for c, o, _ in cmds if c == 0x19]
    flips, flat_syms, refused = set(), set(), set()
    for off, size, lazy, weak in ((fields[2], fields[3], False, False),
                                  (fields[4], fields[5], False, True),
                                  (fields[6], fields[7], True, False)):
        for special_at, ordinal, sym, flags in scan(b, off, size, lazy, segments, weak):
            if ordinal != -2:
                continue
            flat_syms.add(sym)
            entries = records.get(sym, [])
            valid_symbol = (len(entries) == 1 and entries[0][0] == 1 and
                            entries[0][1] & 0x40 and entries[0][1] >> 8 == 0xFE)
            if (weak or special_at is None or not flags & WEAK_IMPORT or sym in mine or
                    not valid_symbol or allowed is None or sym not in allowed):
                refused.add(sym)
            else:
                flips.add(special_at)
    # One opcode can govern several symbols: no partial plan may escape refusal.
    return (set() if refused else flips), flat_syms, sorted(refused)


def verify_rewrite(original, candidate, allowed, expected_offset=None):
    """Independently reparse both images and certify ONLY planned 0x3e->0x30 edits.

    Run before codesign, whose unrelated signature edits must never be exempted.
    This verifies bytes/provenance, not generic host-symbol equivalence.
    """
    if allowed is None:
        fail('rewrite verification requires generated provenance')
    flips, flat, refused = plan(original, allowed)
    if refused or not flat or not flips:
        fail('cannot verify rewrite outside generated flat-placeholder contract')
    if expected_offset is not None and flips != {expected_offset}:
        fail('qualified build binding opcode location/count mismatch')
    if len(original) != len(candidate):
        fail('rewrite changed image size')
    changed = {i for i, (a, b) in enumerate(zip(original, candidate)) if a != b}
    if changed != flips or any(original[i] != 0x3E or candidate[i] != 0x30 for i in flips):
        fail('rewrite changed bytes outside exact planned binding opcodes')
    if plan(candidate, allowed)[1]:
        fail('verified rewrite still has flat imports')
    print('verified %d generated imports, opcode diff: %s (0x3e->0x30 only, before signing)' %
          (len(flat), ','.join(str(i) for i in sorted(flips))))
    return flips


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', metavar='DYLIB')
    parser.add_argument('--verify-rewrite', action='store_true')
    parser.add_argument('--expected-opcode-offset', type=lambda value: int(value, 0))
    parser.add_argument('--nvk-generated-dir', metavar='BUILD')
    parser.add_argument('--accept-no-interposition', action='store_true',
                        help='accept intentional loss of host/dependency interposition for generated missing entrypoints')
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args(argv[1:])
    if args.check:
        if (args.paths or args.nvk_generated_dir or args.accept_no_interposition or
                args.verify_rewrite or args.expected_opcode_offset is not None):
            parser.error('--check takes only a dylib')
        _, flat, _ = plan(pathlib.Path(args.check).read_bytes())
        print('%d flat-namespace imports (not a semantic safety certificate)' % len(flat))
        return 1 if flat else 0
    if len(args.paths) != 2:
        parser.error('expected input and output paths')
    if bool(args.nvk_generated_dir) != args.accept_no_interposition:
        parser.error('rewrite requires both --nvk-generated-dir and --accept-no-interposition')
    if args.expected_opcode_offset is not None and not args.verify_rewrite:
        parser.error('--expected-opcode-offset requires --verify-rewrite')
    allowed = generated_placeholders(args.nvk_generated_dir) if args.nvk_generated_dir else None
    if args.verify_rewrite:
        verify_rewrite(pathlib.Path(args.paths[0]).read_bytes(), pathlib.Path(args.paths[1]).read_bytes(),
                       allowed, args.expected_opcode_offset)
        return 0
    b = bytearray(pathlib.Path(args.paths[0]).read_bytes())
    flips, flat, refused = plan(bytes(b), allowed)
    if refused:
        fail('refused: %d flat import(s) outside verified generated NULL-placeholder contract, e.g. %s' %
             (len(refused), refused[:5]))
    for at in flips:
        if b[at] != 0x30 | FLAT_IMM:
            fail('invalid planned opcode')
        b[at] = 0x30 | SELF_IMM
    left = plan(bytes(b), allowed)[1]
    if left:
        fail('flat imports remain; output not written')
    pathlib.Path(args.paths[1]).write_bytes(b)
    print('%d generated flat weak imports rebound to self (%d opcodes); no-interposition contract accepted' %
          (len(flat), len(flips)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
