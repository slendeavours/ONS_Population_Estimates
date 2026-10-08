"""Tests for the export's hard stops (export_map_data.validate_export).

No real table is read or written. Rows and features are stubs; for main()
the connection, the contract backstop and the layer periods are patched, and
Path.write_text is replaced so no file can be written.
"""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export_map_data as em  # noqa: E402

SQUARE = {"type": "Polygon",
          "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def codes(n=296):
    return [f"E0{i:07d}" for i in range(n)]


def good(n=296):
    signals = [{"lad24cd": c, "la_name": f"Area {c}", "ta_households_current": 5}
               for c in codes(n)]
    features = [{"type": "Feature", "geometry": copy.deepcopy(SQUARE),
                 "properties": dict(s)} for s in signals]
    return signals, features


class ValidateExport(unittest.TestCase):
    def test_296_good_features_pass(self):
        self.assertEqual(em.validate_export(*good()), [])

    def test_multipolygon_passes(self):
        signals, features = good()
        features[0]["geometry"] = {"type": "MultiPolygon",
                                   "coordinates": [SQUARE["coordinates"]]}
        self.assertEqual(em.validate_export(signals, features), [])

    def test_295_features_fail(self):
        problems = em.validate_export(*good(295))
        self.assertTrue(any("295 features, 296 expected" in p
                            for p in problems), problems)

    def test_duplicate_lad24cd_fails(self):
        signals, features = good()
        features[1]["properties"]["lad24cd"] = features[0]["properties"]["lad24cd"]
        problems = em.validate_export(signals, features)
        self.assertTrue(any("repeated" in p and "E00000000" in p
                            for p in problems), problems)

    def test_null_lad24cd_fails(self):
        signals, features = good()
        features[3]["properties"]["lad24cd"] = None
        problems = em.validate_export(signals, features)
        self.assertTrue(any("feature 3: lad24cd is NULL" in p
                            for p in problems), problems)

    def test_null_name_fails(self):
        signals, features = good()
        features[3]["properties"]["la_name"] = None
        problems = em.validate_export(signals, features)
        self.assertTrue(any("la_name is NULL" in p for p in problems), problems)

    def test_null_geometry_fails(self):
        signals, features = good()
        features[5]["geometry"] = None
        problems = em.validate_export(signals, features)
        self.assertTrue(any("E00000005: geometry is NULL" in p
                            for p in problems), problems)

    def test_point_geometry_fails(self):
        signals, features = good()
        features[7]["geometry"] = {"type": "Point", "coordinates": [0, 0]}
        problems = em.validate_export(signals, features)
        self.assertTrue(any("'Point'" in p for p in problems), problems)

    def test_signals_and_features_mismatch_fails(self):
        signals, features = good()
        signals[0] = dict(signals[0], lad24cd="E99999999")
        problems = em.validate_export(signals, features)
        self.assertTrue(any("in the signals but not the features: E99999999"
                            in p for p in problems), problems)
        self.assertTrue(any("in the features but not the signals: E00000000"
                            in p for p in problems), problems)

    def test_signals_row_count_mismatch_fails(self):
        signals, features = good()
        problems = em.validate_export(signals[:-1], features)
        self.assertTrue(any("295 signals rows but 296 features" in p
                            for p in problems), problems)

    def test_null_signal_values_still_pass(self):
        # docs/RULES.md rule 1: a blank is unknown, not zero, and is exported.
        signals, features = good()
        for s, f in zip(signals, features):
            s["ta_households_current"] = None
            f["properties"]["ta_households_current"] = None
            f["properties"]["avg_price_all"] = None
        self.assertEqual(em.validate_export(signals, features), [])


class StubCur:
    """Answers export_map_data.main()'s queries by SQL text. Reads only."""

    def __init__(self, n=296, point_at=None):
        self.cols = (["run_id", "lad24cd", "la_name", "ta_households_current"]
                     + [c for c in em.EXPECTED_COLUMNS
                        if c not in ("hb_sa_claimants_latest",
                                     "avg_price_all", "annual_change_pct")])
        self.rows = []
        for c in codes(n):
            r = {k: None for k in self.cols}
            r.update(run_id=25, lad24cd=c, la_name=f"Area {c}",
                     hb_sa_claimants_latest=None, avg_price_all=None,
                     annual_change_pct=None)
            self.rows.append(r)
        self.geom = [{"lad24cd": c, "geojson": copy.deepcopy(SQUARE)}
                     for c in codes(n)]
        if point_at is not None:
            self.geom[point_at]["geojson"] = {"type": "Point",
                                              "coordinates": [0, 0]}
        self._one = self._all = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        assert s.startswith("select"), f"a write was attempted: {sql}"
        if "max(run_id)" in s:
            self._one = {"r": 25}
        elif "information_schema.columns" in s:
            self._all = [{"column_name": c} for c in self.cols]
        elif "from staging_la_signals s" in s:
            self._all = self.rows
        elif "to_char(max(period)" in s:
            self._one = {"p": "2026-07"}
        elif "from la_boundaries" in s:
            self._all = self.geom
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def close(self):
        pass


class MainWritesOnlyAfterChecks(unittest.TestCase):
    def _main(self, cur):
        conn = mock.Mock()
        conn.cursor.return_value = cur
        writer = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(em, "_contract_backstop"), \
             mock.patch.object(em, "layer_periods", return_value={}), \
             mock.patch.object(em.psycopg2, "connect", return_value=conn), \
             mock.patch.object(Path, "write_text", writer), \
             mock.patch.object(Path, "stat",
                               return_value=mock.Mock(st_size=1)), \
             redirect_stdout(out):
            try:
                em.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, writer, out.getvalue()

    def test_failed_check_writes_no_file(self):
        for cur in (StubCur(n=295), StubCur(point_at=4)):
            code, writer, out = self._main(cur)
            self.assertIn("no file was written", str(code))
            self.assertIn("STOP", out)
            writer.assert_not_called()

    def test_good_export_writes_three_files(self):
        code, writer, out = self._main(StubCur())
        self.assertEqual(code, 0, out)
        self.assertEqual(writer.call_count, 3)
        self.assertIn("Checks passed before writing", out)


if __name__ == "__main__":
    unittest.main()
