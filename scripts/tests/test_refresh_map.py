"""Tests for refresh_map: staleness reporting and the default-mode chain.

No real table is read or written. Cursors are stubs; w1_run.main,
export_map_data.main and subprocess.run are patched.
"""
import datetime
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import refresh_map  # noqa: E402

UTC = datetime.timezone.utc
RUN_DATE = datetime.datetime(2026, 10, 8, 8, 49, tzinfo=UTC)


class StubCur:
    """Answers the kinds of query refresh_map makes, by SQL text."""

    def __init__(self, run=(25, RUN_DATE), inputs=None, loaded=None):
        self.run = run
        self.inputs = inputs if inputs is not None else ["la_population"]
        self.loaded = loaded or {}
        self.sql = []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        s = " ".join(sql.split()).lower()
        if s.startswith(("insert", "update", "delete", "create", "drop",
                         "alter", "truncate")):
            raise AssertionError(f"a write was attempted: {sql}")
        if "from staging_runs" in s:
            self._rows = [self.run] if self.run else []
        elif "max(loaded_at)" in s:
            t = next(t for t in self.inputs if t in s)
            self._rows = [(self.loaded.get(t),)]
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def patch_inputs():
    return mock.patch.object(refresh_map, "input_tables",
                             lambda cur: [(t, True) for t in cur.inputs])


class Staleness(unittest.TestCase):
    def setUp(self):
        p = patch_inputs()
        p.start()
        self.addCleanup(p.stop)

    def test_fresh_when_published_equals_latest(self):
        self.assertEqual(refresh_map.staleness(StubCur(), 25), [])

    def test_stale_when_published_older(self):
        msgs = refresh_map.staleness(StubCur(), 23)
        self.assertEqual(len(msgs), 1)
        self.assertIn("23", msgs[0])
        self.assertIn("25", msgs[0])

    def test_stale_when_no_published_file(self):
        msgs = refresh_map.staleness(StubCur(), None)
        self.assertTrue(any("no published" in m.lower() for m in msgs))

    def test_stale_when_no_complete_run(self):
        msgs = refresh_map.staleness(StubCur(run=None), 25)
        self.assertTrue(any("no completed" in m.lower() for m in msgs))

    def test_stale_when_input_loaded_after_run(self):
        later = RUN_DATE + datetime.timedelta(hours=3)
        cur = StubCur(inputs=["la_population", "marac_cases"],
                      loaded={"marac_cases": later,
                              "la_population": RUN_DATE
                              - datetime.timedelta(days=1)})
        msgs = refresh_map.staleness(cur, 25)
        self.assertEqual(len(msgs), 1)
        self.assertIn("marac_cases", msgs[0])
        self.assertNotIn("la_population", msgs[0])


class Discovery(unittest.TestCase):
    def test_table_tokens_from_sql(self):
        found = refresh_map.table_tokens(
            "SELECT 1 FROM la_population p JOIN marac_cases m ON 1=1 "
            "WHERE x = 'la_s114_notices' AND y = other_thing",
            {"la_population", "marac_cases", "la_s114_notices", "other"})
        self.assertEqual(found, {"la_population", "marac_cases",
                                 "la_s114_notices"})

    def test_excluded_names(self):
        self.assertTrue(refresh_map.is_excluded("staging_la_signals"))
        self.assertTrue(refresh_map.is_excluded("staging_signal_contract"))
        self.assertTrue(refresh_map.is_excluded("la_boundaries"))
        self.assertFalse(refresh_map.is_excluded("la_population"))


def _boom(*a, **k):
    raise AssertionError("--check wrote or ran something")


class Modes(unittest.TestCase):
    def test_check_mode_exit_code_and_writes_nothing(self):
        for published, code in ((25, 0), (23, 1)):
            cur = StubCur()
            conn = mock.Mock()
            conn.cursor.return_value = cur
            with mock.patch.object(refresh_map, "get_readonly_conn",
                                   return_value=conn), \
                 mock.patch.object(refresh_map, "published_run",
                                   return_value=published), \
                 patch_inputs(), \
                 mock.patch.object(refresh_map.w1_run, "main", _boom), \
                 mock.patch.object(refresh_map.export_map_data, "main", _boom), \
                 mock.patch.object(refresh_map.w1_contract_check, "check", _boom), \
                 mock.patch.object(Path, "write_text", _boom), \
                 mock.patch("subprocess.run", _boom), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(refresh_map.main(["--check"]), code)
            conn.commit.assert_not_called()

    def _default(self, *, complete=True, stale=False, w1_rc=0):
        cur = StubCur(run=(25, RUN_DATE) if complete else None,
                      loaded={"la_population": RUN_DATE
                              + datetime.timedelta(hours=1)} if stale else {})
        conn = mock.Mock()
        conn.cursor.return_value = cur
        calls = []
        sp = mock.Mock(side_effect=lambda cmd, **k: calls.append(("sp", cmd))
                       or mock.Mock(returncode=0, stdout="{}"))
        w1 = mock.Mock(side_effect=lambda argv=None: calls.append(("w1", argv))
                       or w1_rc)
        ex = mock.Mock(side_effect=lambda: calls.append(("export", None)))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(refresh_map, "get_readonly_conn",
                               return_value=conn), \
             patch_inputs(), \
             mock.patch.object(refresh_map.w1_run, "main", w1), \
             mock.patch.object(refresh_map.export_map_data, "main", ex), \
             mock.patch.object(refresh_map, "export_summary",
                               return_value=["run 25 exported"]), \
             mock.patch("subprocess.run", sp), \
             redirect_stdout(out), redirect_stderr(err):
            rc = refresh_map.main([])
        return rc, calls, out.getvalue()

    def test_default_mode_never_pushes(self):
        for kw in ({}, {"stale": True}, {"complete": False}):
            rc, calls, out = self._default(**kw)
            self.assertEqual(rc, 0)
            self.assertIn("Not pushed. Review, then: python scripts/push.py",
                          out)
            for kind, arg in calls:
                self.assertIn(kind, ("w1", "export"))
                self.assertNotIn("push", str(arg))

    def test_current_run_is_not_rerun(self):
        rc, calls, out = self._default()
        self.assertEqual([c[0] for c in calls], ["export"])
        self.assertIn("latest run 25 is current", out)

    def test_stale_inputs_run_w1_then_export(self):
        rc, calls, out = self._default(stale=True)
        self.assertEqual([c[0] for c in calls], ["w1", "export"])
        self.assertEqual(calls[0][1], [])

    def test_w1_refusal_stops_non_zero(self):
        rc, calls, out = self._default(stale=True, w1_rc=1)
        self.assertEqual(rc, 1)
        self.assertEqual([c[0] for c in calls], ["w1"])


class Summary(unittest.TestCase):
    def test_changed_cells_counts_per_field(self):
        old = [{"lad24cd": "A", "x": 1, "y": 2}, {"lad24cd": "B", "x": 1, "y": 2}]
        new = [{"lad24cd": "A", "x": 5, "y": 2}, {"lad24cd": "B", "x": 6, "y": 2}]
        self.assertEqual(refresh_map.changed_cells(old, new), {"x": 2})

    def test_changed_cells_ignores_unchanged_and_missing_areas(self):
        old = [{"lad24cd": "A", "x": 1.0}]
        new = [{"lad24cd": "A", "x": 1.0}, {"lad24cd": "Z", "x": 9}]
        self.assertEqual(refresh_map.changed_cells(old, new), {})


class Published(unittest.TestCase):
    def test_published_run_reads_string_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text(json.dumps({"run_id": "23"}))
            self.assertEqual(refresh_map.published_run(p), 23)
            self.assertIsNone(refresh_map.published_run(Path(d) / "no.json"))


if __name__ == "__main__":
    unittest.main()
