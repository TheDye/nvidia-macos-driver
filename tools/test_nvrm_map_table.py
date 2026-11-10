"""NVRM's kernel-mapping table holds what a resizable BAR1 needs, and its lookups stay as cheap as before.

Field logs from an RTX 3050 and an RTX 5070 showed 'high water 1024/1024' after about 20 minutes: every later
surface had no physical address and was refused. The table is now 8192 entries; every slot that is filled
raises the high-water mark (the alias path did not), and the lookups scan only up to it."""
import os, re, subprocess, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = "kexts/NVRM/os-xnu.cpp"
# These are mutations of CURRENT source, not reconstructions of historical code.
INSERT = re.compile(r"nv_xnu_map_publish\(&nv_xnu_maps\[i\],[^;]*\);|"
                    r"nv_xnu_maps\[i\] = \{ (?!NULL, NULL, NULL)[^}]*\};")
HIGH_WATER_UPDATE = re.compile(
    r"\s*if \(\(unsigned\)i \+ 1 > nv_xnu_maps_high\) "
    r"nv_xnu_maps_high = \(unsigned\)i \+ 1;")
LOOKUPS = ("nv_xnu_phys_for_kernel_va", "os_unmap_kernel_space", "nv_xnu_kmap_overlap")


def problems(text):
    out = []
    m = re.search(r"#define NV_XNU_MAX_MAPS (\d+)", text)
    if not m or int(m.group(1)) < 8192:
        out.append("table smaller than 8192 entries")
    # Recognise both the current release-publication helper and older aggregate inserts.
    # A read of the high-water mark is not an update (normal insertion also logs it).
    inserts = list(INSERT.finditer(text))
    if not inserts:
        out.append("no table inserts found")
    for ins in inserts:
        if not HIGH_WATER_UPDATE.match(text, ins.end()):
            out.append("an insert does not raise the high-water mark: " + ins.group(0)[:60])
    # the three lookups must stop at the high-water mark. Anchor on the DEFINITION, which starts a line
    # with its return type: searching for the bare name finds the first mention of it anywhere, and a
    # comment above the table that names these functions made this read the wrong body and report a
    # lookup as unbounded when it was not.
    for fn in LOOKUPS:
        d = re.search(r"^[A-Za-z_][^\n/]*\b%s\s*\(" % re.escape(fn), text, re.M)
        if not d:
            out.append(fn + " has no definition in this file")
            continue
        body = text[d.start():]
        body = body[:body.find("\n}\n")]
        if "i < nv_xnu_maps_high" not in body:
            out.append(fn + " scans the whole table")
    return out


class MapTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, SRC)) as f:
            cls.source = f.read()

    def test_table_is_large_and_lookups_are_bounded(self):
        self.assertEqual(problems(self.source), [])

    def test_negative_control_small_table(self):
        self.assertEqual(problems(self.source), [])
        mutated, count = re.subn(r"#define NV_XNU_MAX_MAPS \d+",
                                 "#define NV_XNU_MAX_MAPS 1024", self.source)
        self.assertEqual(count, 1)
        self.assertNotEqual(mutated, self.source)
        self.assertEqual(problems(mutated), ["table smaller than 8192 entries"])

    def test_negative_control_each_insert_without_high_water_update(self):
        self.assertEqual(problems(self.source), [])
        inserts = list(INSERT.finditer(self.source))
        self.assertEqual(len(inserts), 2, "exercise both alias and normal insert paths")
        for index, ins in enumerate(inserts):
            with self.subTest(insert=index, source=ins.group(0)):
                update = HIGH_WATER_UPDATE.match(self.source, ins.end())
                self.assertIsNotNone(update, "insert must have an adjacent high-water update")
                mutated = self.source[:update.start()] + self.source[update.end():]
                self.assertNotEqual(mutated, self.source)
                self.assertEqual(problems(mutated), [
                    "an insert does not raise the high-water mark: " + ins.group(0)[:60]])

    def test_negative_control_each_unbounded_lookup(self):
        self.assertEqual(problems(self.source), [])
        for fn in LOOKUPS:
            with self.subTest(lookup=fn):
                definition = re.search(r"^[A-Za-z_][^\n/]*\b%s\s*\(" % re.escape(fn),
                                       self.source, re.M)
                self.assertIsNotNone(definition)
                end = self.source.find("\n}\n", definition.start())
                self.assertNotEqual(end, -1)
                body = self.source[definition.start():end]
                changed, count = re.subn(r"i < nv_xnu_maps_high", "i < NV_XNU_MAX_MAPS", body)
                self.assertEqual(count, 1, "mutate exactly this lookup's bound")
                mutated = self.source[:definition.start()] + changed + self.source[end:]
                self.assertNotEqual(mutated, self.source)
                self.assertEqual(problems(mutated), [fn + " scans the whole table"])

    def test_optional_authentic_historical_source(self):
        # Offline only. Optionally point at a real pre-fix public commit and bare repo
        # with NVRM_MAP_TABLE_HISTORY_REF / NVRM_MAP_TABLE_HISTORY_GIT_DIR.
        # Never present a different public revision as the unavailable private 042bebe.
        history = os.environ.get("NVRM_MAP_TABLE_HISTORY_GIT_DIR")
        revision = os.environ.get("NVRM_MAP_TABLE_HISTORY_REF", "042bebe")
        explicit = history is not None or "NVRM_MAP_TABLE_HISTORY_REF" in os.environ
        command = ["git", "--git-dir=" + history] if history else ["git", "-C", ROOT]
        try:
            result = subprocess.run(command + ["show", revision + ":" + SRC],
                                    capture_output=True, text=True)
        except FileNotFoundError:
            if explicit:
                self.fail("explicit historical verification requires git")
            self.skipTest("optional authentic history unavailable: git missing; mutation controls required")
        if result.returncode:
            reason = "authentic " + revision + " unavailable locally: " + result.stderr.strip()
            if explicit:
                self.fail(reason)
            self.skipTest("optional " + reason + "; mutation controls required")
        found = problems(result.stdout)
        self.assertIn("table smaller than 8192 entries", found)
        self.assertTrue(any("does not raise" in p for p in found), found)


if __name__ == "__main__":
    unittest.main()
