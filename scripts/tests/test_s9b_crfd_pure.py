"""Pure tests for s9b_crfd_editions: pages, links, streaming, parsing,
identity, records, stop rules. No database, no network; data files (a zip
holding a CSV, or a plain CSV) are built in the test."""
import csv
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s9b_crfd_editions as m  # noqa: E402

FILES = "https://files.digital.nhs.uk/"
HEADER = ["REPORTING_PERIOD_START", "REPORTING_PERIOD_END", "STATUS",
          "BREAKDOWN", "PRIMARY_LEVEL", "PRIMARY_LEVEL_DESCRIPTION",
          "SECONDARY_LEVEL", "SECONDARY_LEVEL_DESCRIPTION", "MEASURE_ID",
          "MEASURE_NAME", "MEASURE_VALUE"]
MNAME = ("Days of delayed discharge, for patients clinically ready for "
         "discharge, in the Reporting Period")
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
        "Nov", "Dec")


def url(name, tag="AA/123456"):
    """A files.digital.nhs.uk link to `name` (encoded as the pages do)."""
    return f"{FILES}{tag}/{quote(name)}"


def perf_name(period, v=1):
    d = date.fromisoformat(period)
    return (f"MHSDS Data_{_MON[d.month - 1]}Perf_{d.year}"
            + (f" v{v}" if v > 1 else "") + ".zip")


def final_name(period, v=1):
    d = date.fromisoformat(period)
    return (f"MHSDS Data_{_MON[d.month - 1]}Final__{d.year}"
            + (f" v{v}" if v > 1 else "") + ".zip")


def html_of(*hrefs):
    return "<html>" + "".join(f'<a href="{h}">x</a>' for h in hrefs) + "</html>"


def _day(d: date, form: str) -> str:
    if form == "iso":
        return d.isoformat()
    if form == "slash":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    return f"{d.day:02d}-{d.month:02d}-{d.year}"


def data_lines(period, values, *, status="Performance", form="slash",
               noise=True, measure_name=MNAME, end=None, start=None):
    """CSV rows (lists, header first) of an MHSDS data file. values:
    [(code, value text or int)] for the MHS26 LA breakdown."""
    d = date.fromisoformat(period)
    s = _day(start or d, form)
    import calendar
    e = _day(end or date(d.year, d.month,
                         calendar.monthrange(d.year, d.month)[1]), form)
    out = [list(HEADER)]

    def row(breakdown, code, desc, sec, measure, value, st=status):
        out.append([s, e, st, breakdown, code, desc, sec, sec, measure,
                    measure_name if measure == m.MEASURE else "Other",
                    str(value)])
    if noise:
        row("England", "England", "England", "NONE", m.MEASURE, 999)
        row(m.BREAKDOWN, "E06000001", "x", "NONE", "MHS01", 5)
        row(m.BREAKDOWN + "; Delayed discharge reason", "E06000001", "x",
            "A1", m.MEASURE, 7)
        row(m.BREAKDOWN, "E06000001", "x", "A1", m.MEASURE, 8)
        row(m.BREAKDOWN, "E10000003", "Cambridgeshire", "NONE", m.MEASURE,
            40)
        row(m.BREAKDOWN, "UNKNOWN", "UNKNOWN", "NONE", m.MEASURE, 15)
    for code, value in values:
        row(m.BREAKDOWN, code, f"Area {code}", "NONE", m.MEASURE, value)
    return out


def write_data_file(path, period, values, *, as_zip=True, member=None,
                    **kw):
    """Write the file (zip with the data CSV and a small metadata CSV, or a
    plain CSV) and return its path."""
    path = Path(path)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(
        data_lines(period, values, **kw))
    text = buf.getvalue()
    if as_zip:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr((member or path.stem) + "/", "")
            z.writestr(f"{member or path.stem}/{path.stem}.csv",
                       text.encode("utf-8-sig"))
            z.writestr("metadata.csv", "a,b\n1,2\n")
    else:
        path.write_bytes(text.encode("utf-8-sig"))
    return path


CODES = ["E06000001", "E07000008", "E08000012", "E09000001"]


class Pages(unittest.TestCase):
    def test_publication_slugs_both_forms(self):
        self.assertEqual(m.publication_slugs("2023-04-01"),
                         ["performance-april-2023",
                          "performance-april-provisional-may-2023"])
        self.assertEqual(m.publication_slugs("2026-08-01"),
                         ["performance-august-2026",
                          "performance-august-provisional-september-2026"])
        self.assertEqual(m.publication_slugs("2025-12-01")[1],
                         "performance-december-provisional-january-2025")

    def test_months_between(self):
        self.assertEqual(m.months_between("2025-11-01", "2026-02-01"),
                         ["2025-11-01", "2025-12-01", "2026-01-01",
                          "2026-02-01"])


class Links(unittest.TestCase):
    PAGE = [
        "MHSDS Data_AprPerf_2026 v2.zip",
        "MHSDS Data_AprFinal__2025.zip",
        "MHSDS Data_AprPrf_2023 v3.csv",
        "MHSDS Data_MarPerf_2026_v2.zip",
        "MHSDS Monthly Performance July 2025 MHSDS Data File.zip",
    ]
    EXCLUDED = [
        "MHSDS Data_Rstr_Apr2026_Prf.zip",
        "MHSDS Data_Rstr_Jul2025_Final.zip",
        "MHSDS Data_OAPs_AprPerf_2026.zip",
        "MHSDS Data_4WW_AprPerf_2026.zip",
        "MHSDS ASCOF_AprPerf_2026.zip",
        "ASCOF_AugPerf_2026.zip",
        "MHSDS Time_Series_data_Apr_2016_Apr_2026_Perf v2.zip",
        "MHSDS OAPs_Time_Series_Apr_2023_Apr_2026_Perf.zip",
        "DQ_coverage_AprPerf_2026.zip",
        "DQ_vodim_AprPerf_2026.csv",
        "MHSDS_DQ_prov_comms_Apr_26.csv",
        "MHSDS Data_MayP_2023 v3.csv",
        "Restated April 2026 data based on May 2026 data cut v2.zip",
        "MHSDS Monthly Performance July 2025 Out of Area Placements.zip",
        "MHSDS Monthly Performance July 2025 Restrictive Interventions.zip",
        "MHSDS Monthly Performance May 2025 to July 2025 Access and Waiting "
        "Times Reference Tables.zip",
        "MHSDS Combined_Backlog_Metrics_Perf_Aug_2026.zip",
        "Pre-Release Access List.pdf",
    ]

    def test_find_data_links_keeps_only_the_data_file(self):
        hrefs = [url(n, f"{i:02d}/ABCDEF") for i, n in
                 enumerate(self.PAGE + self.EXCLUDED)]
        page = html_of(*hrefs) + html_of(hrefs[0].replace("&", "&amp;"))
        got = [m._name(u) for u in m.find_data_links(page)]
        self.assertEqual(got, self.PAGE)

    def test_classify_and_period(self):
        cases = {
            "MHSDS Data_AprPerf_2026 v2.zip": ("2026-04-01", "Performance", 2),
            "MHSDS Data_AprFinal__2025.zip": ("2025-04-01", "Final", 1),
            "MHSDS Data_AprPrf_2023 v3.csv": ("2023-04-01", "Performance", 3),
            "MHSDS Data_MarPerf_2026_v2.zip": ("2026-03-01", "Performance",
                                               2),
            "MHSDS Data_JanPrf_2024.zip": ("2024-01-01", "Performance", 1),
            "MHSDS Monthly Performance July 2025 MHSDS Data File.zip":
                ("2025-07-01", "Performance", 1),
        }
        for name, (p, kind, v) in cases.items():
            self.assertEqual(m.period_from_link(url(name)), p, name)
            self.assertEqual(m.classify_link(url(name)), (kind, v), name)
        with self.assertRaises(ValueError):
            m.classify_link(url("MHSDS Data_Rstr_Apr2026_Prf.zip"))
        self.assertIsNone(m.period_from_link("x.pdf"))

    def test_final_over_performance_then_version(self):
        p = "2025-04-01"
        perf3 = url(perf_name(p, 3))
        final = url(final_name(p))
        final2 = url(final_name(p, 2))
        self.assertEqual(m.choose_link([perf3, final]), final)
        self.assertEqual(m.choose_link([final, final2, perf3]), final2)
        self.assertEqual(m.choose_link([url(perf_name(p)),
                                        url(perf_name(p, 2))]),
                         url(perf_name(p, 2)))
        self.assertGreater(m.link_rank(final), m.link_rank(perf3))

    def test_tie_raises_naming_both(self):
        a = url(perf_name("2026-04-01"), "AA/111111")
        b = url(perf_name("2026-04-01"), "BB/222222")
        with self.assertRaises(ValueError) as cm:
            m.choose_link([a, b])
        self.assertIn(a, str(cm.exception))
        self.assertIn(b, str(cm.exception))
        with self.assertRaises(ValueError):
            m.choose_link([])

    def test_link_older(self):
        p = "2025-04-01"
        self.assertTrue(m.link_older(url(perf_name(p, 3)),
                                     url(final_name(p))))
        self.assertTrue(m.link_older(url(perf_name(p)),
                                     url(perf_name(p, 2))))
        self.assertFalse(m.link_older(url(final_name(p)),
                                      url(perf_name(p))))
        self.assertFalse(m.link_older(url(perf_name(p)), None))
        self.assertFalse(m.link_older(url(perf_name(p)), "local.zip"))

    def test_transition(self):
        p = "2025-04-01"
        self.assertEqual(m.transition(url(perf_name(p)), url(final_name(p))),
                         ("Performance", "Final"))
        self.assertEqual(m.transition(None, url(perf_name(p))),
                         (None, "Performance"))


class Streaming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_zip_read_without_zipfile_read(self):
        f = write_data_file(self.dir / "MHSDS Data_AprPerf_2026.zip",
                            "2026-04-01", [(c, 10) for c in CODES])
        with mock.patch.object(zipfile.ZipFile, "read",
                               side_effect=AssertionError("ZipFile.read")):
            rows, meta = m.parse_file(f)
        self.assertEqual(len(rows), 4)
        # the largest CSV member is the data, not metadata.csv
        self.assertEqual(meta["mhs26_rows"], 4 + 5)

    def test_plain_csv(self):
        f = write_data_file(self.dir / "MHSDS Data_AprPrf_2023 v3.csv",
                            "2023-04-01", [(c, 10) for c in CODES],
                            as_zip=False, form="iso")
        rows, meta = m.parse_file(f)
        self.assertEqual({r["lad24cd"] for r in rows}, set(CODES))
        self.assertEqual(meta["periods"], ["2023-04-01"])

    def test_iter_csv_rows_is_lazy(self):
        f = write_data_file(self.dir / "a.zip", "2026-04-01",
                            [(c, 10) for c in CODES])
        it = m.iter_csv_rows(f)
        first = next(it)
        it.close()
        self.assertEqual(first["MEASURE_ID"], m.MEASURE)

    def test_missing_column_or_short_row_raises(self):
        p = self.dir / "bad.csv"
        p.write_text("REPORTING_PERIOD_START,STATUS\n2026-04-01,x\n")
        with self.assertRaises(ValueError) as cm:
            m.parse_file(p)
        self.assertIn("header missing", str(cm.exception))
        lines = data_lines("2026-04-01", [(c, 1) for c in CODES])
        lines.append(lines[-1][:5])
        p = self.dir / "short.csv"
        with open(p, "w", newline="") as f:
            csv.writer(f).writerows(lines)
        with self.assertRaises(ValueError) as cm:
            m.parse_file(p)
        self.assertIn("fields", str(cm.exception))

    def test_zip_without_csv_raises(self):
        p = self.dir / "empty.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("readme.txt", "x")
        with self.assertRaises(ValueError):
            m.parse_file(p)


class Parse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.n = 0

    def parse(self, period, values, **kw):
        self.n += 1
        f = write_data_file(self.dir / f"f{self.n}.zip", period, values, **kw)
        return m.parse_file(f)

    def test_three_date_forms(self):
        for form in ("iso", "slash", "dash"):
            rows, meta = self.parse("2025-04-01", [(c, 5) for c in CODES],
                                    form=form)
            self.assertEqual(meta["periods"], ["2025-04-01"], form)
            self.assertEqual(meta["period_ends"], ["2025-04-30"], form)
        self.assertEqual(m._parse_day("01/04/2024"), "2024-04-01")
        self.assertEqual(m._parse_day("01-04-2025"), "2025-04-01")
        self.assertEqual(m._parse_day("2023-04-01"), "2023-04-01")
        # also seen (June 2024 Final): a two-digit year, no leading zeros
        self.assertEqual(m._parse_day("01/06/24"), "2024-06-01")
        self.assertEqual(m._parse_day("1/6/2024"), "2024-06-01")
        for bad in ("April 2025", "2025-13-01", "31/02/2025", "1/6/224"):
            with self.assertRaises(ValueError, msg=bad):
                m._parse_day(bad)

    def test_status_with_trailing_space(self):
        rows, meta = self.parse("2026-05-01", [(c, 5) for c in CODES],
                                status="Performance ")
        self.assertEqual(meta["statuses"], ["Performance"])
        self.assertEqual(meta["statuses_raw"], ["Performance "])
        self.assertEqual(m.check_identity("2026-05-01", "Performance", meta,
                                          perf_name("2026-05-01", 2)), [])

    def test_e10_and_unknown_dropped_and_counted(self):
        rows, meta = self.parse("2026-04-01", [(c, 5) for c in CODES])
        self.assertEqual(meta["dropped"], {"E10": 1, "UNKNOWN": 1})
        self.assertEqual(sorted(r["lad24cd"] for r in rows), sorted(CODES))

    def test_welsh_or_other_code_halts(self):
        with self.assertRaises(ValueError) as cm:
            self.parse("2026-04-01", [(c, 5) for c in CODES]
                       + [("W06000001", 5)])
        self.assertIn("W06000001", str(cm.exception))

    def test_markers_null_not_zero_and_unknown_marker_halts(self):
        rows, _ = self.parse("2026-04-01", [("E06000001", "*"),
                                            ("E07000008", ""),
                                            ("E08000012", "0"),
                                            ("E09000001", 35)])
        got = {r["lad24cd"]: r["measure_value"] for r in rows}
        self.assertEqual(got, {"E06000001": None, "E07000008": None,
                               "E08000012": 0, "E09000001": 35})
        self.assertIsNone(got["E06000001"])
        for bad in ("abc", "12.5", "1,200", "-", ".."):
            with self.assertRaises(ValueError, msg=bad) as cm:
                self.parse("2026-04-01", [("E06000001", bad)])
            self.assertIn("MEASURE_VALUE", str(cm.exception))

    def test_breakdown_exact_and_labels(self):
        rows, meta = self.parse("2026-04-01", [("E06000001", 5)])
        # the noise rows (England, another measure, the delayed-discharge
        # reason breakdown, SECONDARY_LEVEL A1) are not kept
        self.assertEqual(rows, [{"lad24cd": "E06000001",
                                 "la_name": "Area E06000001",
                                 "measure_name": MNAME,
                                 "measure_value": 5}])
        self.assertEqual(meta["measure_names"], [MNAME])

    def test_identity_mismatches(self):
        p = "2026-04-01"
        rows, meta = self.parse(p, [(c, 5) for c in CODES])
        self.assertEqual(m.check_identity(p, "Performance", meta,
                                          perf_name(p)), [])
        # another month
        probs = m.check_identity("2026-05-01", "Performance", meta,
                                 perf_name("2026-05-01"))
        self.assertTrue(any("REPORTING_PERIOD_START" in x for x in probs))
        # the link is Final but the file says Performance
        probs = m.check_identity(p, "Final", meta, final_name(p))
        self.assertTrue(any("STATUS" in x for x in probs))
        # the link name names another month or kind
        probs = m.check_identity(p, "Performance", meta,
                                 perf_name("2026-03-01"))
        self.assertTrue(any("names 2026-03-01" in x for x in probs))
        probs = m.check_identity(p, "Performance", meta, final_name(p))
        self.assertTrue(any("is a Final file" in x for x in probs))
        # two statuses, or a wrong period end
        _, meta2 = self.parse(p, [(c, 5) for c in CODES], status="Final")
        meta2 = dict(meta2, statuses=["Final", "Performance"])
        self.assertTrue(m.check_identity(p, "Final", meta2, final_name(p)))
        _, meta3 = self.parse(p, [(c, 5) for c in CODES],
                              end=date(2026, 6, 30))
        probs = m.check_identity(p, "Performance", meta3, perf_name(p))
        self.assertTrue(any("REPORTING_PERIOD_END" in x for x in probs))
        self.assertTrue(m.check_identity(p, "Performance",
                                         dict(meta, mhs26_rows=0),
                                         perf_name(p)))

    def test_build_records_recodes_and_duplicates(self):
        rows = [{"lad24cd": "E08000038", "la_name": "Barnsley",
                 "measure_name": MNAME, "measure_value": 5},
                {"lad24cd": "E06000001", "la_name": "x",
                 "measure_name": MNAME, "measure_value": None}]
        recs = m.build_records(rows, {"E08000038": "E08000016"},
                               "2026-04-01")
        self.assertEqual([r["lad24cd"] for r in recs],
                         ["E06000001", "E08000016"])
        self.assertTrue(all(r["measure_id"] == "MHS26"
                            and r["reporting_period"] == "2026-04-01"
                            for r in recs))
        both = rows + [{"lad24cd": "E08000016", "la_name": "Barnsley",
                        "measure_name": MNAME, "measure_value": 6}]
        with self.assertRaises(ValueError) as cm:
            m.build_records(both, {"E08000038": "E08000016"}, "2026-04-01")
        self.assertIn("duplicate", str(cm.exception))

    def test_content_sha256(self):
        p = self.dir / "x.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p)[:16], "ba7816bf8f01cfea")


def _summary(per_period):
    return {"per_period": per_period}


def _d(**kw):
    d = {"new": False, "transition": ("Performance", "Final"), "areas": 296,
         "total_old": 1000, "total_new": 1100, "changed": 150,
         "null_areas": 9, "number_areas": 16, "big_areas": 36,
         "zero_null": 0, "one_side": 0}
    d.update(kw)
    return d


class StopRules(unittest.TestCase):
    P = "2025-04-01"

    def test_final_with_150_areas_changed_passes(self):
        self.assertEqual(m.stop_problems(_summary({self.P: _d()})), {})

    def test_same_changes_on_a_reissue_stop(self):
        s = _summary({self.P: _d()})
        stops = m.stop_problems(s, {self.P: ("Performance", "Performance")})
        self.assertIn("NULL replaces a number in 9 areas", stops[self.P])
        self.assertIn("revised above 50% in 36 areas", stops[self.P])
        # a Final reissued (Final to Final) is held to the same rules
        self.assertIn(self.P, m.stop_problems(
            s, {self.P: ("Final", "Final")}))
        self.assertEqual(m.stop_problems(
            s, {self.P: ("Performance", "Performance")},
            acknowledged={self.P}), {})

    def test_reissue_limits(self):
        t = {self.P: ("Performance", "Performance")}
        ok = _d(null_areas=5, big_areas=10)
        self.assertEqual(m.stop_problems(_summary({self.P: ok}), t), {})
        self.assertIn(self.P, m.stop_problems(
            _summary({self.P: _d(null_areas=6, big_areas=0)}), t))
        self.assertIn(self.P, m.stop_problems(
            _summary({self.P: _d(null_areas=0, big_areas=11)}), t))

    def test_final_national_total_over_50pct_stops(self):
        stops = m.stop_problems(_summary({self.P: _d(total_new=1501)}))
        self.assertIn("national total", stops[self.P])
        self.assertEqual(m.stop_problems(
            _summary({self.P: _d(total_new=1500)})), {})
        self.assertIn(self.P, m.stop_problems(
            _summary({self.P: _d(total_new=400)})))

    def test_zero_null_always_stops_and_new_months_never(self):
        self.assertIn("rule 1.10", m.stop_problems(
            _summary({self.P: _d(zero_null=1)}))[self.P])
        self.assertEqual(m.stop_problems(_summary(
            {self.P: _d(new=True, null_areas=99, big_areas=99)})), {})


class Module(unittest.TestCase):
    def test_no_private_recode_dict(self):
        import ast
        src = Path(m.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(m, "HARD_RECODES"))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant)}
                self.assertFalse(keys & {"E08000038", "E08000039"})
        self.assertEqual(__import__("geography").DATASET_FORM["9b"][0],
                         "mixed")


if __name__ == "__main__":
    unittest.main()
