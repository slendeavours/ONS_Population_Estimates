"""The retired S21 build (scripts/s21_statistical_neighbours_build.py) stops
at once, also when run directly and with a broken environment, without
opening a connection.

Every test that executes the script runs a copy of it in the scrubbed sandbox
of retired_sandbox.py: alone in a temporary directory (no _db, no repo .env
anywhere on its path), python -E -s, an environment built from nothing (PATH
and SystemRoot, dummy credentials, host invalid.invalid, port 1). If the
guard were ever lost, the script would fail at its first import or
connection, not repeat the 2026-10-11 incident against the real database.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from retired_sandbox import run_retired  # noqa: E402

NAME = "s21_statistical_neighbours_build.py"
NEW_LOADER = "scripts/s21_neighbours_editions.py"


class RetiredS21(unittest.TestCase):
    def _run(self, import_module=False):
        return run_retired(SCRIPTS / NAME, import_module=import_module)

    def test_run_directly_prints_retired_and_exits_2(self):
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertRegex(r.stderr, r"RETIRED 2026-10-1\d: use python "
                         + re.escape(NEW_LOADER) + r" \(preview by default\)")
        self.assertEqual(r.stdout, "")
        for bad in ("Traceback", "ModuleNotFoundError", "could not translate host",
                    "HARD STOP", "ERROR:", "HALT"):
            self.assertNotIn(bad, r.stderr)

    def test_import_also_stops(self):
        r = self._run(import_module=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("RETIRED", r.stderr)
        self.assertNotIn("Traceback", r.stderr.replace("SystemExit", ""))

    def test_wording_states_the_history(self):
        r = self._run()
        for bit in ("id 66", "2026-07-24", "1,480", "59ae151", "2026-08-20",
                    "TRUNCATE", "no preview", "staging_la_signals",
                    "renumber", "E08000038", "E08000016", "pre-recode",
                    "wrong way round", "LIVE_RULE"):
            with self.subTest(bit=bit):
                self.assertIn(bit, r.stderr)

    def test_guard_is_the_first_executable_code(self):
        """After the docstring: 'import sys', the message assignment, the
        print to stderr and sys.exit(2), and nothing else. No other import,
        no function, no call before the exit."""
        src = (SCRIPTS / NAME).read_text(encoding="utf-8")
        tree = ast.parse(src)
        body = tree.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        self.assertEqual(len(body), 4, ast.dump(tree)[:300])
        self.assertIsInstance(body[0], ast.Import)
        self.assertEqual([a.name for a in body[0].names], ["sys"])
        self.assertIsInstance(body[1], ast.Assign)
        self.assertIsInstance(body[1].value, ast.Constant)
        self.assertEqual(ast.unparse(body[2]),
                         "print(_RETIRED, file=sys.stderr)")
        self.assertEqual(ast.unparse(body[3]), "sys.exit(2)")
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(len(imports), 1)

    def test_no_retired_body_left_in_the_file(self):
        src = (SCRIPTS / NAME).read_text(encoding="utf-8")
        self.assertLess(len(src.splitlines()), 40)
        for gone in ("INSERT INTO", "TRUNCATE la_", "get_conn", "CODE_REMAP =",
                     "execute_batch", "def main"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def test_nothing_live_imports_or_runs_the_old_script(self):
        pat = re.compile(r"^\s*(import|from)\s+s21_statistical_neighbours_build"
                         r"\b|python\s+\S*s21_statistical_neighbours_build",
                         re.M)
        live = [p for p in SCRIPTS.glob("*.py") if p.name != NAME]
        live += [p for p in (SCRIPTS / "tests").glob("*.py")
                 if p.name != Path(__file__).name]
        for p in live:
            with self.subTest(p.name):
                self.assertIsNone(pat.search(p.read_text(encoding="utf-8")),
                                  p.name)


if __name__ == "__main__":
    unittest.main()
