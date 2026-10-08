"""Tests for refresh_map: staleness reporting and the default-mode chain.

No real table is read or written. Cursors are stubs; w1_run.main (or, for
the refusal test, every database call inside it), export_map_data.main and
subprocess.run are patched.
"""
import contextlib
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
import psycopg2.errors  # noqa: E402

import refresh_map  # noqa: E402

UTC = datetime.timezone.utc
RUN_DATE = datetime.datetime(2026, 10, 8, 8, 49, tzinfo=UTC)


class StubCur:
    """Answers the kinds of query refresh_map makes, by SQL text."""

    def __init__(self, run=(25, RUN_DATE), inputs=None, loaded=None,
                 export_inputs=None):
        self.run = run
        self.export_inputs = export_inputs or []
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
            t = next(t for t in self.inputs + self.export_inputs if t in s)
            self._rows = [(self.loaded.get(t),)]
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def patch_inputs():
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(
        refresh_map, "input_tables",
        lambda cur: [(t, True) for t in cur.inputs]))
    stack.enter_context(mock.patch.object(
        refresh_map, "export_only_tables",
        lambda cur: [(t, True) for t in cur.export_inputs]))
    return stack


class Staleness(unittest.TestCase):
    def setUp(self):
        p = patch_inputs()
        p.__enter__()
        self.addCleanup(p.close)

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
                 mock.patch.object(
                     refresh_map, "head_published",
                     return_value=((published, RUN_DATE), None)), \
                 mock.patch.object(refresh_map, "local_exported",
                                   return_value=None), \
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
        # Modelled as it really happens: the real w1_run.main runs, step 03
        # raises the one-run-per-day RaiseException inside run_steps, main
        # catches it and returns 1. Nothing reaches a database: the contract
        # check, the snapshot, the connection and the sequence step are
        # patched.
        refusal = psycopg2.errors.RaiseException(
            "A completed run already exists for today")
        cur = StubCur(loaded={"la_population": RUN_DATE
                              + datetime.timedelta(hours=1)})
        conn = mock.Mock()
        conn.cursor.return_value = cur
        ex = mock.Mock()
        out, err = io.StringIO(), io.StringIO()
        w1 = refresh_map.w1_run
        with mock.patch.object(refresh_map, "get_readonly_conn",
                               return_value=conn), \
             patch_inputs(), \
             mock.patch.object(w1.w1_contract_check, "check",
                               return_value=([], [], [])), \
             mock.patch.object(w1, "_contract_snapshot", return_value=None), \
             mock.patch.object(w1, "get_conn"), \
             mock.patch.object(w1, "align_run_sequence"), \
             mock.patch.object(w1, "run_steps", side_effect=refusal), \
             mock.patch.object(refresh_map.export_map_data, "main", ex), \
             redirect_stdout(out), redirect_stderr(err):
            rc = refresh_map.main([])
        self.assertEqual(rc, 1)
        ex.assert_not_called()
        self.assertIn("A completed run already exists for today",
                      err.getvalue())
        self.assertIn("W1 did not run; stopping before the export.",
                      err.getvalue())

    def test_export_stop_fails_non_zero(self):
        def stop():
            sys.exit("HARD STOP: the export failed 1 check(s); "
                     "no file was written.")
        cur = StubCur()
        conn = mock.Mock()
        conn.cursor.return_value = cur
        summary = mock.Mock(return_value=[])
        err = io.StringIO()
        with mock.patch.object(refresh_map, "get_readonly_conn",
                               return_value=conn), \
             patch_inputs(), \
             mock.patch.object(refresh_map.export_map_data, "main", stop), \
             mock.patch.object(refresh_map, "export_summary", summary), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = refresh_map.main([])
        self.assertEqual(rc, 1)
        summary.assert_not_called()
        self.assertIn("HARD STOP", err.getvalue())
        self.assertIn("The export stopped; no map file was written",
                      err.getvalue())


class Summary(unittest.TestCase):
    def test_changed_cells_counts_per_field(self):
        old = [{"lad24cd": "A", "x": 1, "y": 2}, {"lad24cd": "B", "x": 1, "y": 2}]
        new = [{"lad24cd": "A", "x": 5, "y": 2}, {"lad24cd": "B", "x": 6, "y": 2}]
        self.assertEqual(refresh_map.changed_cells(old, new), {"x": 2})

    def test_changed_cells_ignores_unchanged_and_missing_areas(self):
        old = [{"lad24cd": "A", "x": 1.0}]
        new = [{"lad24cd": "A", "x": 1.0}, {"lad24cd": "Z", "x": 9}]
        self.assertEqual(refresh_map.changed_cells(old, new), {})


GEN = datetime.datetime(2026, 10, 7, 17, 44, tzinfo=UTC)


class Published(unittest.TestCase):
    def test_local_exported_reads_string_run_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            p.write_text(json.dumps({"run_id": "23",
                                     "generated_at": GEN.isoformat()}))
            self.assertEqual(refresh_map.local_exported(p), (23, GEN))
            self.assertIsNone(refresh_map.local_exported(Path(d) / "no.json"))

    def test_head_published_parses_git_show(self):
        out = json.dumps({"run_id": "23", "generated_at": GEN.isoformat()})
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=0, stdout=out)) as sp:
            self.assertEqual(refresh_map.head_published(), ((23, GEN), None))
        self.assertEqual(sp.call_args[0][0][:2], ["git", "show"])

    def test_git_missing_is_an_error(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            info, err = refresh_map.head_published()
        self.assertIsNone(info)
        self.assertIn("git is unavailable", err)

    def test_head_file_missing_is_an_error(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(
                returncode=128, stdout="")):
            info, err = refresh_map.head_published()
        self.assertIsNone(info)
        self.assertIn("not readable at git HEAD", err)


class ExportOnlyAndPushState(unittest.TestCase):
    def setUp(self):
        p = patch_inputs()
        p.__enter__()
        self.addCleanup(p.close)

    def test_export_only_table_loaded_after_published_is_behind(self):
        cur = StubCur(export_inputs=["la_house_prices"],
                      loaded={"la_house_prices": GEN + datetime.timedelta(days=1)})
        msgs = refresh_map.staleness(cur, 25, None, GEN)
        self.assertEqual(len(msgs), 1)
        self.assertIn("la_house_prices", msgs[0])

    def test_export_only_table_loaded_before_published_is_fresh(self):
        cur = StubCur(export_inputs=["la_house_prices"],
                      loaded={"la_house_prices": GEN - datetime.timedelta(days=1)})
        self.assertEqual(refresh_map.staleness(cur, 25, None, GEN), [])

    def test_head_fresh_and_nothing_newer_is_fresh(self):
        cur = StubCur(export_inputs=["la_house_prices"])
        self.assertEqual(refresh_map.staleness(cur, 25, (25, GEN), GEN), [])

    def test_local_ahead_of_head_is_exported_but_not_pushed(self):
        msgs = refresh_map.staleness(StubCur(), 23, (25, GEN), GEN)
        self.assertTrue(any("exported but not pushed: run 25 is exported "
                            "locally; the live map still shows run 23" in m
                            for m in msgs))

    def _check(self, head, local=None):
        cur = StubCur()
        conn = mock.Mock()
        conn.cursor.return_value = cur
        out = io.StringIO()
        with mock.patch.object(refresh_map, "get_readonly_conn",
                               return_value=conn), \
             mock.patch.object(refresh_map, "head_published",
                               return_value=head), \
             mock.patch.object(refresh_map, "local_exported",
                               return_value=local), \
             redirect_stdout(out):
            return refresh_map.main(["--check"]), out.getvalue()

    def test_check_exit_1_when_local_ahead_of_head(self):
        rc, out = self._check(((25, GEN), None),
                              (25, GEN + datetime.timedelta(hours=1)))
        self.assertEqual(rc, 1)
        self.assertIn("exported but not pushed", out)

    def test_check_exit_1_when_git_missing(self):
        rc, out = self._check((None, "git is unavailable (x); cannot read"))
        self.assertEqual(rc, 1)
        self.assertIn("git is unavailable", out)

    def test_check_exit_0_when_head_fresh(self):
        rc, out = self._check(((25, GEN), None), (25, GEN))
        self.assertEqual(rc, 0)

    def test_export_only_load_does_not_trigger_w1_in_default_mode(self):
        cur = StubCur(export_inputs=["la_house_prices"],
                      loaded={"la_house_prices": RUN_DATE
                              + datetime.timedelta(days=5)})
        conn = mock.Mock()
        conn.cursor.return_value = cur
        w1 = mock.Mock(return_value=0)
        with mock.patch.object(refresh_map, "get_readonly_conn",
                               return_value=conn), \
             mock.patch.object(refresh_map.w1_run, "main", w1), \
             mock.patch.object(refresh_map.export_map_data, "main"), \
             mock.patch.object(refresh_map, "export_summary", return_value=[]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_map.main([]), 0)
        w1.assert_not_called()


class ExportDiscovery(unittest.TestCase):
    def test_export_tables_discovered_from_export_source(self):
        # The real export source and real W1 SQL, over a stubbed table list.
        cur = mock.Mock()
        cur.fetchall.return_value = [("la_house_prices", True),
                                     ("la_population", True),
                                     ("staging_la_signals", False),
                                     ("la_boundaries", True)]
        names = [t for t, _ in refresh_map.export_only_tables(cur)]
        self.assertIn("la_house_prices", names)
        self.assertNotIn("la_population", names)   # a W1 input
        self.assertNotIn("staging_la_signals", names)
        self.assertNotIn("la_boundaries", names)


if __name__ == "__main__":
    unittest.main()
