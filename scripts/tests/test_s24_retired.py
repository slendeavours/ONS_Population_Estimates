"""The two retired S24 scripts (scripts/historical/s24_*.py) stop at once,
also when run directly (scripts/ is then not on sys.path), in the scrubbed
sandbox of retired_sandbox.py (invalid host, no .env, no _db) and without
opening a connection."""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retired_sandbox import run_retired  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
HIST = SCRIPTS / "historical"
NAMES = ["s24_rsh_register_build.py", "s24_rsh_register_verify.py"]
GUARD = "sys.exit(2)"


class RetiredS24(unittest.TestCase):
    def _run(self, name):
        # always the scrubbed sandbox (retired_sandbox.py)
        return run_retired(HIST / name)

    def test_run_directly_prints_retired_and_exits_2(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name)
                self.assertEqual(r.returncode, 2)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s24_rsh_register_editions", r.stderr)
                self.assertIn("2026-08-14", r.stderr)
                self.assertIn("run-log id 96" if "build" in name
                              else "run-log id 101", r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertNotIn("ModuleNotFoundError", r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_retired_even_with_broken_environment(self):
        for name in NAMES:
            with self.subTest(name):
                r = self._run(name)
                self.assertEqual(r.returncode, 2)
                self.assertIn("RETIRED", r.stderr)
                self.assertIn("s24_rsh_register_editions", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertNotIn("could not translate host", r.stderr)
                self.assertNotIn("HARD STOP", r.stderr)

    def test_each_names_its_replacement(self):
        want = {
            "s24_rsh_register_build.py": "scripts/s24_rsh_register_editions.py",
            "s24_rsh_register_verify.py":
                "scripts/s24_rsh_register_editions_verify.py",
        }
        for name, repl in want.items():
            with self.subTest(name):
                r = self._run(name)
                self.assertIn("use " + repl, r.stderr)

    def test_wording_names_what_each_step_did(self):
        want = {
            "s24_rsh_register_build.py": [
                "ON CONFLICT", "DO UPDATE", "loaded_at", "no preview",
                "id 101", "293 of the 308", "15 August rows",
                "same-named raw download", "code that is not in the repo",
                "21 minutes before"],
            "s24_rsh_register_verify.py": [
                "seven-gate", "rolled back", "wrote nothing", "604", "633",
                "29 NULL grades", "one file with a mixture", "rule 1.5"],
        }
        for name, bits in want.items():
            r = self._run(name)
            for b in bits:
                with self.subTest(name=name, bit=b):
                    self.assertIn(b, r.stderr)

    def test_guard_comes_before_any_import_that_can_fail(self):
        for name in NAMES:
            src = (HIST / name).read_text(encoding="utf-8")
            guard = src.index(GUARD)
            for mod in ("openpyxl", "pandas", "psycopg2", "_db", "argparse",
                        "s24_rsh_register_"):
                for stmt in ("import " + mod, "from " + mod):
                    line_start = "\n" + stmt
                    if line_start in src:
                        with self.subTest(name=name, mod=mod):
                            self.assertLess(guard, src.index(line_start))

    def test_only_sys_is_imported_before_the_guard(self):
        for name in NAMES:
            src = (HIST / name).read_text(encoding="utf-8")
            head = src[:src.index(GUARD)]
            imports = [ln for ln in head.splitlines()
                       if ln.startswith(("import ", "from "))]
            with self.subTest(name):
                self.assertEqual(imports, ["import sys"])

    def test_old_scripts_no_longer_in_scripts_folder(self):
        for name in NAMES:
            self.assertFalse((SCRIPTS / name).exists(), name)
            self.assertTrue((HIST / name).exists(), name)

    def test_nothing_live_imports_or_runs_the_old_scripts(self):
        pat = re.compile(r"^\s*(import|from)\s+s24_rsh_register_(build|verify)"
                         r"\b|python\s+\S*s24_rsh_register_(build|verify)\.py",
                         re.M)
        live = [p for p in SCRIPTS.glob("*.py")]
        live += [p for p in (SCRIPTS / "tests").glob("*.py")
                 if p.name != Path(__file__).name]
        for p in live:
            with self.subTest(p.name):
                self.assertIsNone(pat.search(p.read_text(encoding="utf-8")),
                                  p.name)


if __name__ == "__main__":
    unittest.main()
