"""Tests for check_loaders (the loader conformance checker).

Sample loader texts are written to a temp directory. No database is needed:
registry rows are passed as plain dicts.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_loaders as cl  # noqa: E402

CLEAN = '''
import argparse
import editions_core as core

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    total = sum(parts) or 0  # not a source value
    if args.commit:
        conn.commit()
'''

ROW = {"revises_back_series": False, "build_script_path": ""}


class CheckLoader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make(self, text, stem="sx_load", verify=True):
        p = self.dir / f"{stem}.py"
        p.write_text(text, encoding="utf-8")
        if verify:
            (self.dir / f"{stem}_verify.py").write_text("# verify\n")
        return p

    def test_clean_loader_passes(self):
        self.assertEqual(cl.check_loader(self.make(CLEAN), ROW), [])

    def test_zero_coercion_flagged(self):
        for bad in ("v = row['x'] or 0", "v = \"js.x || 0\"",
                    "cur.execute('select coalesce(a, 0) from t')",
                    "v = 1 if ok else 0", "v = d.get('k', 0)"):
            p = self.make(CLEAN + "\n    " + bad + "\n")
            out = cl.check_loader(p, ROW)
            self.assertTrue(any("zero" in r.lower() for r in out), bad)

    def test_allow_listed_derived_sum_not_flagged(self):
        p = self.make(CLEAN + "\n    t = (a or 0) + (b or 0)  # not a source value\n")
        self.assertEqual(cl.check_loader(p, ROW), [])

    def test_missing_commit_flagged(self):
        text = CLEAN.replace('"--commit"', '"--go"').replace("args.commit", "args.go")
        out = cl.check_loader(self.make(text), ROW)
        self.assertTrue(any("--commit" in r for r in out))

    def test_unconditional_commit_flagged(self):
        text = CLEAN + "\n    conn.commit()\n"
        out = cl.check_loader(self.make(text), ROW)
        self.assertTrue(any("without" in r for r in out))

    def test_revising_source_without_core_flagged(self):
        text = CLEAN.replace("import editions_core as core\n", "")
        row = {"revises_back_series": True, "build_script_path": "x"}
        out = cl.check_loader(self.make(text), row)
        self.assertTrue(any("revises" in r for r in out))

    def test_non_revising_without_core_or_declaration_flagged(self):
        text = CLEAN.replace("import editions_core as core\n", "")
        out = cl.check_loader(self.make(text), ROW)
        self.assertTrue(any("editions_core" in r for r in out))

    def test_no_editions_declaration_accepted(self):
        text = CLEAN.replace("import editions_core as core\n",
                             'NO_EDITIONS = "annual snapshot, never revised"\n')
        self.assertEqual(cl.check_loader(self.make(text), ROW), [])

    def test_no_editions_declaration_does_not_excuse_revising(self):
        text = CLEAN.replace("import editions_core as core\n",
                             'NO_EDITIONS = "reason"\n')
        row = {"revises_back_series": True, "build_script_path": "x"}
        self.assertTrue(cl.check_loader(self.make(text), row))

    def test_missing_verify_flagged(self):
        out = cl.check_loader(self.make(CLEAN, verify=False), ROW)
        self.assertTrue(any("verify" in r for r in out))

    def test_verify_prefix_form_accepted(self):
        p = self.make(CLEAN, verify=False)
        (self.dir / "verify_sx_load.py").write_text("#")
        self.assertEqual(cl.check_loader(p, ROW), [])


if __name__ == "__main__":
    unittest.main()
