"""Pure tests for s10_rough_sleeping_editions: discovery, the snapshot file
reader, LA cells, reconciliation, records, geography, ranks, ledger and
planning, the stop conditions and the download rules.

No database and no network: every .ods file is written by the tests (pandas
+ odfpy, the autumn 2025 layout and an autumn 2024-style layout), and the
content API replies and downloads are stubbed. The fixture writers are
reused by test_s10_rough_sleeping_loader.
"""
import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s10_rough_sleeping_editions as m  # noqa: E402

# the fixture's authorities, published on the new Barnsley/Sheffield codes
# (the autumn 2025 file's form)
CODES = ["E06000001", "E06000002", "E08000038", "E08000039", "E09000033"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000038": "Barnsley", "E08000039": "Sheffield",
         "E08000016": "Barnsley", "E08000019": "Sheffield",
         "E09000033": "Westminster", "E07000999": "Nowhere"}
REGION = {"E06000001": ("E12000001", "North East"),
          "E06000002": ("E12000001", "North East"),
          "E08000038": ("E12000003", "Yorkshire and The Humber"),
          "E08000039": ("E12000003", "Yorkshire and The Humber"),
          "E08000016": ("E12000003", "Yorkshire and The Humber"),
          "E08000019": ("E12000003", "Yorkshire and The Humber"),
          "E09000033": ("E12000007", "London"),
          "E07000999": ("E12000001", "North East")}
LONDON = "E12000007"
SHORTHAND = ("This worksheet contains one table. Some shorthand is used in "
             "this table, [x] = Not Available. [z] = Not Applicable. [n] =  "
             "No data available as the authority was created through "
             "reorganisation. Some cells may refer to notes, which can be "
             "found on the notes worksheet. Notes 1-3.")
TITLE = "Rough sleeping snapshot in England: autumn {}"
BASE = "/government/statistics/rough-sleeping-snapshot-in-england-autumn-{}"
TABLES = "Rough sleeping snapshot in England: autumn {} - tables"
SHA_2025 = ("7b7ed536d0d51f837552be0207f76d0f"
            "62838ce4e72409370f6482543c710c89")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def value(code, year):
    """A plausible published count; Hartlepool publishes 0 in 2024 and
    2025 (a published zero)."""
    if code == "E06000001" and year >= 2024:
        return 0
    if code == "E09000033":
        return 300 + 5 * (year - 2010)
    i = CODES.index(code) if code in CODES else 7
    return 3 * (i + 1) + (year - 2010) % 5


def _ordinal(d) -> str:
    suf = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd",
           31: "st"}.get(d.day, "th")
    return f"{d.day}{suf} {d:%B %Y}"


def table_rows(year=2025, *, codes=None, values=None, england=None,
               regions=None, rest=None, title_year=None, header=None,
               shorthand=SHORTHAND, extra=None, first=2010):
    """The Table_1_Total rows of the autumn 2025 layout. values: {(code,
    year): cell} overrides; england / regions / rest: {year: cell} (regions
    {(region code, year): cell}) overrides of the computed aggregates."""
    codes = list(codes or CODES)
    years = list(range(first, year + 1))
    values = values or {}

    def v(c, y):
        return values[(c, y)] if (c, y) in values else value(c, y)

    def total(cs, y):
        return sum(x for x in (v(c, y) for c in cs)
                   if isinstance(x, (int, float)) and not isinstance(x, bool))

    w = 4 + len(years)
    pad = [None] * (w - 1)
    ty = title_year or year
    rows = [[f"Table 1: Estimated number of people sleeping rough, by local "
             f"authority district and region,  2010 - {ty}"] + pad,
            ["Return to Contents"] + pad]
    if shorthand is not None:
        rows.append([shorthand] + pad)
    rows.append(["This publication covers England only."] + pad)
    rows.append(header or (["Local Authority Code", "Local Authority Name",
                            "Region Code", "Region Name"] + years))
    eng = [england[y] if england and y in england else total(codes, y)
           for y in years]
    rows.append(["[z]", "[z]", "E92000001", "England"] + eng)
    regs = sorted({REGION[c] for c in codes}, key=lambda r: (r[0] != LONDON,
                                                            r[1]))
    lon = [c for c in codes if REGION[c][0] == LONDON]
    if lon:
        rows.append(["[z]", "[z]", LONDON, "London"]
                    + [regions[(LONDON, y)] if regions and (LONDON, y) in regions
                       else total(lon, y) for y in years])
    rows.append(["[z]", "[z]", "[z]", "Rest of England"]
                + [rest[y] if rest and y in rest else
                   total([c for c in codes if c not in lon], y)
                   for y in years])
    for rc, rn in regs:
        if rc == LONDON:
            continue
        cs = [c for c in codes if REGION[c][0] == rc]
        rows.append(["[z]", "[z]", rc, rn]
                    + [regions[(rc, y)] if regions and (rc, y) in regions
                       else total(cs, y) for y in years])
    for c in sorted(codes, key=lambda c: NAMES[c]):
        rows.append([c, NAMES[c], REGION[c][0], REGION[c][1]]
                    + [v(c, y) for y in years])
    for r in extra or ():
        rows.append(list(r) + [None] * (w - len(r)))
    return rows


def cover_rows(year=2025, *, published=date(2026, 2, 26), title_year=None,
               first=2010, last=None, date_cell="same", pub_line=True,
               next_release="Winter 2026/2027"):
    rows = [f"Annual rough sleeping snapshot in England: autumn "
            f"{title_year or year}"]
    if date_cell == "same":
        rows.append(datetime(published.year, published.month, published.day))
    elif date_cell is not None:
        rows.append(date_cell)
    rows += [None,
             "Statistical release about the annual single night snapshot of "
             "the number of people sleeping rough in local authorities across "
             "England",
             f"Autumn {first} to autumn {last or year}",
             "See full release here", None, "Introduction", "...", None,
             "Source: MHCLG Rough Sleeping Snapshot"]
    if pub_line:
        rows.append(f"Publication Date: {_ordinal(published)}")
    if next_release is not None:
        rows.append(f"Next Release: {next_release}")
    return [[r] for r in rows]


def write_file(path, year=2025, *, published=date(2026, 2, 26), cover=None,
               table=None, sheets_extra=True, **kw):
    """An autumn 2025-layout snapshot .ods: Cover, Contents, Notes,
    Table_1_Total (kw go to table_rows; cover a cover_rows kwargs dict)."""
    import pandas as pd
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = table if table is not None else table_rows(year, **kw)
    with pd.ExcelWriter(path, engine="odf") as w:
        if cover is not False:
            pd.DataFrame(cover_rows(year, published=published,
                                    **(cover or {})), dtype=object).to_excel(
                w, sheet_name="Cover", header=False, index=False)
        if sheets_extra:
            pd.DataFrame([["Table of contents"]]).to_excel(
                w, sheet_name="Contents", header=False, index=False)
            pd.DataFrame([["Notes"]]).to_excel(w, sheet_name="Notes",
                                               header=False, index=False)
        pd.DataFrame(rows, dtype=object).to_excel(
            w, sheet_name="Table_1_Total", header=False, index=False)
    return path


def write_2024_layout(path, year=2024, codes=None):
    """The autumn 2024 layout: no Cover sheet; Table_1_Total's header row
    shifted (the column headed 'Local authority' holds the codes, 'Local
    authority ONS code' the names) and old Barnsley/Sheffield codes."""
    import pandas as pd
    codes = codes or ["E06000001", "E06000002", "E08000016", "E08000019",
                      "E09000033"]
    years = list(range(2010, year + 1))
    w = 4 + len(years)
    pad = [None] * (w - 1)
    rows = [[f"Table 1: Estimated number of people sleeping rough, by local "
             f"authority district and region,  2010 - {year}"] + pad,
            [f"England, autumn 2010-{year}"] + pad,
            ["This table contains notes, which can be found in the Notes "
             "worksheet (1-3)."] + pad, [None] * w,
            ["Local authority", "Local authority ONS code", "Region",
             "Region ONS code"] + years,
            ["---", "---", "England", "E92000001"]
            + [sum(value(c, y) for c in codes) for y in years]]
    for c in codes:
        rows.append([c, NAMES[c], REGION[c][1], REGION[c][0]]
                    + [value(c, y) for y in years])
    rows += [["Source:", "MHCLG Annual Rough Sleeping Snapshot"] + pad[1:],
             ["Last Update:", "27 February 2025"] + pad[1:]]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="odf") as wr:
        pd.DataFrame([["Contents"]]).to_excel(wr, sheet_name="Contents",
                                              header=False, index=False)
        pd.DataFrame(rows, dtype=object).to_excel(
            wr, sheet_name="Table_1_Total", header=False, index=False)
    return path


def collection(*years, extra=(), title="Homelessness statistics"):
    docs = [{"title": TITLE.format(y), "base_path": BASE.format(y)}
            for y in years]
    docs += [{"title": t, "base_path": "/government/publications/x-" +
              str(i)} for i, t in enumerate(extra)]
    return {"title": title, "links": {"documents": docs}}


def release_page(year, attachments=None, *, title=None, history=()):
    if attachments is None:
        attachments = [
            (TITLE.format(year), BASE.format(year) + "/html"),
            (TABLES.format(year), f"https://assets.example/media/m{year}/"
             f"Rough_sleeping_snapshot_in_England__autumn_{year}.ods")]
    return {"title": title or TITLE.format(year),
            "first_published_at": f"{year + 1}-02-26T09:30:00+00:00",
            "public_updated_at": f"{year + 1}-02-26T09:30:00+00:00",
            "details": {"attachments": [{"title": t, "url": u}
                                        for t, u in attachments],
                        "change_history": [
                            {"public_timestamp": ts, "note": n}
                            for ts, n in (history or [(
                                f"{year + 1}-02-26T09:30:00Z",
                                "First published.")])]}}


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0

    def write(self, year=2025, **kw):
        self.n += 1
        return write_file(self.root / f"f{self.n}.ods", year, **kw)

    def read(self, year=2025, **kw):
        return m.read_file(self.write(year, **kw))

    def raises(self, *needles, **kw):
        path = self.write(**kw)
        with self.assertRaises(ValueError) as e:
            m.read_file(path)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):

    def test_newest_release_is_chosen(self):
        c = collection(2023, 2025, 2024, extra=(
            "Rough sleeping data framework, April to June 2026",
            "Rough sleeping in England: autumn 2018"))
        self.assertEqual(m.latest_release(c), (2025, BASE.format(2025)))
        self.assertEqual(m.release_for_year(c, 2024), (2024, BASE.format(2024)))

    def test_none_halts_listing_titles(self):
        c = collection(extra=("Statutory homelessness in England",))
        with self.assertRaises(ValueError) as e:
            m.latest_release(c)
        self.assertIn("Statutory homelessness in England", str(e.exception))

    def test_two_pages_for_one_year_raise_listing_titles(self):
        c = collection(2025, 2025)
        with self.assertRaises(ValueError) as e:
            m.latest_release(c)
        self.assertIn(TITLE.format(2025), str(e.exception))
        with self.assertRaises(ValueError):
            m.release_for_year(c, 2025)

    def test_a_renamed_series_with_a_later_year_halts_naming_it(self):
        for t in ("Rough sleeping in England: autumn 2026",
                  "Rough sleeping snapshot in England, autumn 2026",
                  "Annual rough sleeping snapshot: England"):
            with self.subTest(t=t):
                c = collection(2024, 2025, extra=(t,))
                with self.assertRaises(ValueError) as e:
                    m.latest_release(c)
                self.assertIn(t, str(e.exception))
                self.assertIn("autumn 2025", str(e.exception))

    def test_unrelated_and_older_titles_do_not_halt(self):
        c = collection(2025, extra=(
            "Rough sleeping data framework, April to June 2026",
            "Rough sleeping in England: autumn 2018",
            "Tables on rough sleeping"))
        self.assertEqual(m.latest_release(c)[0], 2025)

    def test_release_for_year_none(self):
        with self.assertRaises(ValueError) as e:
            m.release_for_year(collection(2024, 2025), 2026)
        self.assertIn("0 documents", str(e.exception))

    def test_collection_title_checked(self):
        m.check_collection(collection(2025))
        with self.assertRaises(ValueError) as e:
            m.check_collection(collection(2025, title="Homelessness data"))
        self.assertIn("Homelessness data", str(e.exception))

    def test_tables_attachment_and_the_accessible_copy(self):
        page = release_page(2024, [
            (TITLE.format(2024), BASE.format(2024) + "/html"),
            (TABLES.format(2024), "https://assets.example/a/t.ods"),
            (TABLES.format(2024) + " (accessible)",
             "https://assets.example/a/t_accessible.ods"),
            ("Rough sleeping snapshot in England: autumn 2024 - infographic",
             "https://assets.example/a/i.pdf")])
        att = m.tables_attachment(page)
        self.assertEqual(att["url"], "https://assets.example/a/t.ods")
        self.assertEqual(att["year"], 2024)
        self.assertEqual(att["ignored"], [TABLES.format(2024)
                                          + " (accessible)"])

    def test_no_or_two_tables_attachments_raise_listing_titles(self):
        none = release_page(2025, [
            (TITLE.format(2025), "x"),
            (TABLES.format(2025) + " (accessible)", "https://a/t.ods")])
        with self.assertRaises(ValueError) as e:
            m.tables_attachment(none)
        self.assertIn("(accessible)", str(e.exception))
        two = release_page(2025, [(TABLES.format(2025), "https://a/1.ods"),
                                  (TABLES.format(2025), "https://a/2.ods")])
        with self.assertRaises(ValueError) as e:
            m.tables_attachment(two)
        self.assertIn("2 attachments", str(e.exception))
        xlsx = release_page(2025, [(TABLES.format(2025), "https://a/1.xlsx")])
        with self.assertRaises(ValueError) as e:
            m.tables_attachment(xlsx)
        self.assertIn(".ods", str(e.exception))

    def test_change_history(self):
        page = release_page(2024, history=[
            ("2025-07-15T15:50:06Z", "Added accessible version of tables. "),
            ("2025-02-27T00:00:00Z", "First published.")])
        self.assertEqual(m.change_history(page), [
            ("2025-07-15", "Added accessible version of tables."),
            ("2025-02-27", "First published.")])

    def test_nothing_newer_says_which_release_it_saw(self):
        lines = m.discovery_note(collection(2024, 2025), ["2025"],
                                 today=date(2026, 10, 10))
        self.assertEqual(len(lines), 1)
        self.assertIn("autumn 2025", lines[0])
        self.assertIn("no newer release", lines[0])
        self.assertEqual(m.discovery_note(collection(2025), ["2024"],
                                          today=date(2026, 10, 10)), [])

    def test_past_the_expected_date_with_nothing_newer_warns(self):
        lines = m.discovery_note(collection(2025), ["2025"],
                                 today=date(2027, 3, 2))
        self.assertTrue(any(x.startswith("WARNING") and "autumn 2026" in x
                            for x in lines), lines)
        lines = m.discovery_note(collection(2025), ["2025"],
                                 today=date(2027, 2, 27))
        self.assertFalse(any(x.startswith("WARNING") for x in lines))


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

class ReadFile(Tmp):

    def test_identity_rows_and_markers(self):
        f = self.read()
        self.assertEqual(f["year"], 2025)
        self.assertEqual(f["published"], date(2026, 2, 26))
        self.assertEqual(f["rank"], (date(2026, 2, 26), 2025))
        self.assertEqual(f["first_year"], 2010)
        self.assertEqual(f["next_release"], "Winter 2026/2027")
        self.assertEqual(f["years"], list(range(2010, 2026)))
        self.assertEqual(sorted(f["las"]), sorted(CODES))
        self.assertEqual(f["markers"], {
            "x": "Not Available", "z": "Not Applicable",
            "n": "No data available as the authority was created through "
                 "reorganisation"})
        self.assertEqual(sorted(f["regions"]),
                         ["E12000001", "E12000003", LONDON])
        self.assertEqual(m.england_total(f, 2025),
                         sum(value(c, 2025) for c in CODES))

    def test_cover_year_differs_from_the_table_title_year(self):
        self.raises("2024", "2025", title_year=2024)
        self.raises("Cover", cover={"title_year": 2024})

    def test_cover_range_not_starting_2010(self):
        self.raises("2011", "2010", cover={"first": 2011})
        self.raises("2024", cover={"last": 2024})

    def test_cover_without_the_publication_line_or_next_release(self):
        self.raises("Publication Date", cover={"pub_line": False})
        self.raises("Next Release", cover={"next_release": None})

    def test_a_cover_date_cell_must_equal_the_publication_line(self):
        self.raises("2026-02-27", cover={"date_cell": datetime(2026, 2, 27)})
        f = self.read(cover={"date_cell": None})          # absent: fine
        self.assertEqual(f["published"], date(2026, 2, 26))

    def test_unknown_or_missing_header_raises(self):
        hdr = (["Local Authority Code", "Local Authority Name", "Region Code",
                "Region"] + list(range(2010, 2026)))
        self.raises("unknown ['Region']", "missing ['Region Name']",
                    header=hdr)
        hdr = (["Local Authority Code", "Local Authority Name", "Region Code",
                "Region Name"] + list(range(2010, 2025)))
        self.raises("2025", header=hdr)

    def test_the_2024_style_shifted_header_raises_naming_it(self):
        path = write_2024_layout(self.root / "a2024.ods")
        with self.assertRaises(ValueError) as e:
            m.read_file(path)
        msg = str(e.exception)
        self.assertIn("'Local authority'", msg)
        self.assertIn("'Local authority ONS code'", msg)
        self.assertIn("Cover", msg)

    def test_missing_shorthand_line_raises(self):
        self.raises("[x]", shorthand=None)

    def test_missing_sheets_raise(self):
        import pandas as pd
        p = self.root / "bare.ods"
        with pd.ExcelWriter(p, engine="odf") as w:
            pd.DataFrame([["x"]]).to_excel(w, sheet_name="Cover",
                                           header=False, index=False)
        with self.assertRaises(ValueError) as e:
            m.read_file(p)
        self.assertIn("Table_1_Total", str(e.exception))

    def test_an_unexpected_row_or_a_duplicate_code_raises(self):
        self.raises("unexpected row", extra=[["Total", "x", "y", "z", 1]])
        self.raises("E06000001", "twice",
                    extra=[["E06000001", "Hartlepool", "E12000001",
                            "North East"] + [1] * 16])

    def test_data_after_an_empty_row_raises(self):
        self.raises("empty row", extra=[[None], ["Source:", "x"]])


class Cells(Tmp):

    def test_la_cell(self):
        self.assertEqual(m.la_cell(4.0, "w"), 4)
        self.assertEqual(m.la_cell(0, "w"), 0)        # a published zero
        self.assertEqual(m.la_cell(0.0, "w"), 0)
        markers = {"x": "Not Available"}
        for v, needle in (("[x]", "Not Available"), (None, "blank"),
                          ("", "blank"), (float("nan"), "blank"),
                          ("12 approx", "12 approx"), (-1, "negative"),
                          (2.5, "non-integer"), (True, "True")):
            with self.subTest(v=v), self.assertRaises(ValueError) as e:
                m.la_cell(v, "Table_1_Total row 9 E06000002 2025", markers)
            self.assertIn("E06000002 2025", str(e.exception))
            self.assertIn(needle, str(e.exception))

    def test_a_marker_in_a_loaded_year_halts_naming_the_cell_and_meaning(self):
        f = self.read(values={("E06000002", 2025): "[x]"})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, 2025, lambda c: c)
        self.assertIn("E06000002", str(e.exception))
        self.assertIn("[x]", str(e.exception))
        self.assertIn("Not Available", str(e.exception))

    def test_bad_cells_halt_and_a_published_zero_stays_zero(self):
        for bad in (None, "text", -2, 1.5):
            with self.subTest(bad=bad):
                f = self.read(values={("E06000002", 2024): bad})
                with self.assertRaises(ValueError) as e:
                    m.build_records(f, 2025, lambda c: c)
                self.assertIn("E06000002", str(e.exception))
        f = self.read()
        recs = {r["lad24cd"]: r for r in m.build_records(f, 2025, lambda c: c)}
        self.assertEqual(recs["E06000001"]["rough_sleeping"], 0)
        self.assertEqual(recs["E06000001"]["rough_sleeping_prev_year"], 0)

    def test_a_marker_outside_the_loaded_years_does_not_halt(self):
        f = self.read(values={("E06000002", 2012): "[x]"})
        recs = m.build_records(f, 2025, lambda c: c)
        self.assertEqual(len(recs), len(CODES))
        self.assertEqual(m.reconcile(f, [2024, 2025]), [])
        self.assertTrue(any("2012" in p for p in m.reconcile(f)))


class Reconcile(Tmp):

    def test_the_fixture_reconciles(self):
        self.assertEqual(m.reconcile(self.read()), [])

    def test_off_by_one_halts(self):
        eng = value("E09000033", 2025) + sum(value(c, 2025) for c in CODES
                                             if c != "E09000033")
        for kw, needle in (({"england": {2025: eng + 1}}, "England"),
                           ({"regions": {("E12000003", 2024): 1}},
                            "E12000003"),
                           ({"regions": {(LONDON, 2025): 1}}, LONDON),
                           ({"rest": {2025: 1}}, "Rest of England")):
            with self.subTest(kw=kw):
                probs = m.reconcile(self.read(**kw))
                self.assertTrue(probs)
                self.assertTrue(any(needle in p for p in probs), probs)


class Records(Tmp):

    def test_build_records(self):
        f = self.read()
        recs = m.build_records(f, 2025, {c: c for c in CODES})
        self.assertEqual([r["lad24cd"] for r in recs], sorted(CODES))
        r = {x["lad24cd"]: x for x in recs}["E09000033"]
        self.assertEqual(r, {"lad24cd": "E09000033", "snapshot_year": "2025",
                             "rough_sleeping": 375,
                             "rough_sleeping_prev_year": 370})
        recs = m.build_records(f, 2024, lambda c: c)
        r = {x["lad24cd"]: x for x in recs}["E09000033"]
        self.assertEqual((r["snapshot_year"], r["rough_sleeping"],
                          r["rough_sleeping_prev_year"]), ("2024", 370, 365))

    def test_a_year_the_file_cannot_state_raises(self):
        f = self.read()
        for y in (2010, 2026):
            with self.subTest(y=y), self.assertRaises(ValueError):
                m.build_records(f, y, lambda c: c)

    def test_duplicate_key_halts(self):
        f = self.read()
        with self.assertRaises(ValueError) as e:
            m.build_records(f, 2025, lambda c: "E06000001")
        self.assertIn("duplicate", str(e.exception))

    def test_periods_stated_includes_a_held_back_year(self):
        f = self.read()
        self.assertEqual(m.periods_stated(f, []), [2025])
        self.assertEqual(m.periods_stated(f, [2025]), [2025])
        self.assertEqual(m.periods_stated(f, [2024, 2025]), [2024, 2025])
        self.assertEqual(m.periods_stated(f, ["2023", 2026, 2010]),
                         [2023, 2025])

    def test_rows_content_sha_is_key_and_two_values(self):
        f = self.read()
        recs = m.build_records(f, 2025, lambda c: c)
        sha = m.rows_content_sha(recs)
        lines = "\n".join(f"{r['lad24cd']}|{r['rough_sleeping']}|"
                          f"{r['rough_sleeping_prev_year']}"
                          for r in sorted(recs, key=lambda r: r["lad24cd"]))
        self.assertEqual(sha, hashlib.sha256(lines.encode()).hexdigest())
        self.assertEqual(m.rows_content_sha(list(reversed(recs))), sha)
        extra = [dict(r, other="x") for r in recs]
        self.assertEqual(m.rows_content_sha(extra), sha)
        bumped = [dict(r) for r in recs]
        bumped[0]["rough_sleeping_prev_year"] += 1
        self.assertNotEqual(m.rows_content_sha(bumped), sha)


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

class _Cur:
    """A cursor answering la_boundaries and la_code_lookup reads."""

    def __init__(self, bounds):
        self.bounds = bounds
        self.result = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        if "change_type = 'recode'" in s:
            self.result = [("E08000038", "E08000016"),
                           ("E08000039", "E08000019")]
        elif "FROM public.la_boundaries" in s or "FROM la_boundaries" in s:
            self.result = [(c,) for c in self.bounds]
        else:
            raise AssertionError(f"unexpected SQL {s}")

    def fetchall(self):
        return list(self.result)


BOUNDS = ["E06000001", "E06000002", "E08000016", "E08000019", "E09000033"]


class Geography(unittest.TestCase):

    def test_new_codes_resolve_to_the_canonical_ones(self):
        rmap, problems = m.resolve_codes(_Cur(BOUNDS), set(CODES), 2025)
        self.assertEqual(problems, [])
        self.assertEqual(rmap["E08000038"], "E08000016")
        self.assertEqual(rmap["E08000039"], "E08000019")
        self.assertEqual(rmap["E06000001"], "E06000001")

    def test_an_old_code_halts_against_the_new_declaration(self):
        _, problems = m.resolve_codes(_Cur(BOUNDS),
                                      {"E06000001", "E08000016"}, 2024)
        self.assertTrue(any("E08000016" in p and "'new'" in p
                            for p in problems), problems)

    def test_an_unknown_code_is_unexplained(self):
        _, problems = m.resolve_codes(_Cur(BOUNDS),
                                      {"E06000001", "E07000999"}, 2025)
        self.assertTrue(any("UNEXPLAINED E07000999" in p for p in problems),
                        problems)

    def test_the_declaration_is_new_with_the_file_evidence(self):
        import geography
        form, ev = geography.DATASET_FORM["10"]
        self.assertEqual(form, "new")
        self.assertIn("2010-2025", ev)
        self.assertIn("autumn 2024", ev)


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

class Ranks(Tmp):

    def test_rank_from_the_file_never_the_name(self):
        a = self.write()
        b = self.root / "Rough_sleeping_snapshot_in_England__autumn_2030.ods"
        b.write_bytes(a.read_bytes())
        self.assertEqual(m.release_rank(m.read_file(a)),
                         (date(2026, 2, 26), 2025))
        self.assertEqual(m.release_rank(m.read_file(b)),
                         (date(2026, 2, 26), 2025))
        c = self.write(published=date(2026, 3, 5))
        self.assertEqual(m.release_rank(m.read_file(c)),
                         (date(2026, 3, 5), 2025))
        self.assertIsNone(m.release_rank(
            "Rough_sleeping_snapshot_in_England__autumn_2030.ods"))
        self.assertIsNone(m.release_rank(None))

    def test_rank_from_an_edition_or_ledger_source(self):
        f = m.read_file(self.write())
        src = m.edition_source(f, {"file_name": "x.ods", "where": "u"})
        self.assertEqual(m.release_rank(src), (date(2026, 2, 26), 2025))
        lsrc = m.ledger_source("https://a/x.ods", f["rank"], [2024, 2025])
        self.assertEqual(m.release_rank(lsrc), (date(2026, 2, 26), 2025))
        self.assertEqual(m.parse_ledger_source(lsrc),
                         ("https://a/x.ods", (date(2026, 2, 26), 2025),
                          ["2024", "2025"]))
        self.assertIsNone(m.parse_ledger_source("https://a/x.ods"))

    def test_content_sha256(self):
        p = self.root / "b.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p),
                         hashlib.sha256(b"abc").hexdigest())


R25 = (date(2026, 2, 26), 2025)
R26 = (date(2027, 2, 25), 2026)


class Ledger(unittest.TestCase):

    URL = "https://a/x.ods"

    def test_ledger_skip_needs_the_pair_for_every_period(self):
        src = m.ledger_source(self.URL, R26, [2025, 2026])
        both = {"2025": {(src, "s")}, "2026": {(src, "s")}}
        self.assertEqual(m.ledger_complete(both, self.URL, "s",
                                           {"2025": R26, "2026": R26}),
                         ["2025", "2026"])
        one = {"2026": {(src, "s")}}
        self.assertIsNone(m.ledger_complete(one, self.URL, "s",
                                            {"2025": R25, "2026": R26}))
        self.assertIsNone(m.ledger_complete(both, self.URL, "other",
                                            {"2025": R26, "2026": R26}))
        self.assertIsNone(m.ledger_complete(both, "https://b/x.ods", "s",
                                            {"2025": R26, "2026": R26}))
        # a stranded period always makes the file be read
        self.assertIsNone(m.ledger_complete(both, self.URL, "s",
                                            {"2025": R26, "2026": R26},
                                            stranded=["2025"]))

    def test_a_period_held_from_a_newer_file_is_not_needed(self):
        src = m.ledger_source(self.URL, R25, [2024, 2025])
        checked = {"2025": {(src, "s")}}
        ranks = {"2024": (date(2027, 1, 1), 2026), "2025": R25}
        self.assertEqual(m.ledger_complete(checked, self.URL, "s", ranks),
                         ["2025"])
        self.assertIsNone(m.ledger_complete(checked, self.URL, "s", ranks,
                                            allow_older=True))

    def test_plan_periods(self):
        src = m.ledger_source(self.URL, R26, [2025, 2026])
        new, rev, skip = m.plan_periods(["2025", "2026"], R26, {"2025": R25},
                                        {}, src, "s", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev, skip), (["2026"], ["2025"], {}))
        # older: the held tip comes from a newer file
        new, rev, skip = m.plan_periods(["2025"], R25,
                                        {"2025": (date(2026, 3, 1), 2025)},
                                        {}, src, "s", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["2025"].startswith("older"))
        new, rev, skip = m.plan_periods(["2025"], R25,
                                        {"2025": (date(2026, 3, 1), 2025)},
                                        {}, src, "s", recheck=None,
                                        allow_older=True)
        self.assertEqual(rev, ["2025"])
        # checked: the ledger records (source, sha) for the period
        new, rev, skip = m.plan_periods(["2025"], R26, {"2025": R26},
                                        {"2025": {(src, "s")}}, src, "s",
                                        recheck=None, allow_older=False)
        self.assertEqual(skip, {"2025": "checked: this file is already in "
                                        "the ledger"})
        new, rev, skip = m.plan_periods(["2025"], R26, {"2025": R26},
                                        {"2025": {(src, "s")}}, src, "s",
                                        recheck="2025", allow_older=False)
        self.assertEqual(rev, ["2025"])
        with self.assertRaises(ValueError):
            m.plan_periods(["2025"], R26, {"2025": R26}, {}, src, "s",
                           recheck="2024", allow_older=False)

    def test_a_new_period_older_than_the_newest_held_is_skipped(self):
        src = m.ledger_source(self.URL, (date(2025, 2, 27), 2024), [2024])
        new, rev, skip = m.plan_periods(["2024"], (date(2025, 2, 27), 2024),
                                        {"2025": R25}, {}, src, "s",
                                        recheck=None, allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["2024"].startswith("older"))
        new, _, _ = m.plan_periods(["2024"], (date(2025, 2, 27), 2024),
                                   {"2025": R25}, {}, src, "s",
                                   recheck=None, allow_older=True)
        self.assertEqual(new, ["2024"])

    def test_tip_ranks_take_a_newer_ledger_file(self):
        src = m.ledger_source(self.URL, R26, [2025, 2026])
        tips = {"2025": {"rank": R25}, "2024": {"rank": None}}
        self.assertEqual(m.tip_ranks(tips, {"2025": {(src, "s")}}),
                         {"2025": R26, "2024": None})


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def recs(values, year="2025"):
    """[{lad24cd, snapshot_year, rough_sleeping, rough_sleeping_prev_year}]
    from {code: (rs, prev)}."""
    return [{"lad24cd": c, "snapshot_year": year, "rough_sleeping": a,
             "rough_sleeping_prev_year": b} for c, (a, b) in values.items()]


def area(i):
    return f"E07{i:06d}"


class Stops(unittest.TestCase):

    N = 20

    def setUp(self):
        p = mock.patch.object(m, "EXPECTED_AREAS", self.N)
        p.start()
        self.addCleanup(p.stop)
        self.base = {area(i): (100, 100) for i in range(self.N)}

    def test_constants_and_calibration_note(self):
        self.assertEqual((m.NEW_TOTAL_PCT, m.NEW_AREA_ABS, m.REV_TOTAL_PCT,
                          m.REV_MAX_AREAS), (30, 150, 1, 10))
        doc = m.__doc__
        for needle in ("27.0%", "2020", "-37.0%", "Westminster"):
            self.assertIn(needle, doc)

    def test_fewer_authorities_is_a_partial_file_both_kinds(self):
        few = dict(list(self.base.items())[:-1])
        for kind, tip, prev in (("new", None, recs(self.base)),
                                ("revised", recs(self.base), None)):
            with self.subTest(kind=kind):
                p = m.period_problems(recs(few), tip, prev, kind=kind)
                self.assertTrue(any(x.startswith(m.PARTIAL) for x in p), p)
        self.assertEqual(m.period_problems(recs(self.base), None, None,
                                           kind="new"), [])

    def test_new_national_total_both_sides(self):
        prev = recs(self.base, "2024")                   # total 2,000
        up = dict(self.base)
        for i in range(6):                               # +600 = 30%: fine
            up[area(i)] = (200, 100)
        self.assertEqual(m.period_problems(recs(up), None, prev, kind="new"),
                         [])
        up[area(6)] = (101, 100)                         # 30.05%: stops
        p = m.period_problems(recs(up), None, prev, kind="new")
        self.assertTrue(any("national" in x for x in p), p)
        self.assertFalse(any(x.startswith(m.PARTIAL) for x in p))
        down = dict(self.base)
        for i in range(7):
            down[area(i)] = (14, 100)                    # -602: stops
        p = m.period_problems(recs(down), None, prev, kind="new")
        self.assertTrue(any("national" in x for x in p), p)

    def test_new_area_move_both_sides(self):
        base = {**self.base, area(0): (1000, 1000)}
        prev = recs(base, "2024")
        mv = {**base, area(0): (1150, 1000)}             # exactly 150: fine
        self.assertEqual(m.period_problems(recs(mv), None, prev, kind="new"),
                         [])
        mv = {**base, area(0): (849, 1000)}              # 151 down: stops
        p = m.period_problems(recs(mv), None, prev, kind="new")
        self.assertTrue(any(area(0) in x and "150" in x for x in p), p)

    def test_revised_any_change_needs_acknowledge(self):
        tip = recs(self.base)
        self.assertEqual(m.period_problems(recs(self.base), tip, None,
                                           kind="revised"), [])
        ch = {**self.base, area(0): (101, 100)}
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertEqual(len(p), 1, p)
        self.assertIn("never happened", p[0])
        self.assertIn(area(0), p[0])
        self.assertFalse(p[0].startswith(m.PARTIAL))

    def test_revised_national_total_both_sides(self):
        tip = recs(self.base)                            # total 2,000
        ch = {**self.base, area(0): (120, 100)}          # +20 = 1%: no
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertFalse(any("national" in x for x in p), p)
        ch = {**self.base, area(0): (121, 100)}          # 1.05%: yes
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertTrue(any("national rough_sleeping" in x for x in p), p)
        ch = {**self.base, area(0): (100, 79)}           # prev column
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertTrue(any("national rough_sleeping_prev_year" in x
                            for x in p), p)

    def test_revised_authorities_changed_both_sides(self):
        tip = recs(self.base)
        ch = dict(self.base)
        for i in range(10):
            ch[area(i)] = (101, 99)                      # 10 change: no
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertFalse(any("limit 10" in x for x in p), p)
        ch[area(10)] = (101, 99)                         # 11: yes
        p = m.period_problems(recs(ch), tip, None, kind="revised")
        self.assertTrue(any("11 authorities" in x and "limit 10" in x
                            for x in p), p)

    def test_revised_tip_authority_missing_is_partial(self):
        tip = recs({**self.base, "E07999999": (1, 1)})
        p = m.period_problems(recs(self.base), tip, None, kind="revised")
        self.assertTrue(any(x.startswith(m.PARTIAL) and "E07999999" in x
                            for x in p), p)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body, url):
        self.content = body
        self.url = url
        self.text = body.decode("latin-1")

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body, final):
        self.body, self.final, self.calls = body, final, []

    def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(self.body, self.final)


class Download(Tmp):

    def body(self, **kw):
        self.n += 1
        return write_file(self.root / "src" / f"b{self.n}.ods",
                          **kw).read_bytes()

    def test_redirect_records_the_final_url(self):
        body = self.body()
        s = _Session(body, "https://assets.example/media/2/final.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, final = m.fetch("https://assets.example/media/1/old.ods",
                                  self.root / "raw", session=s)
        self.assertEqual(final, "https://assets.example/media/2/final.ods")
        self.assertEqual(path.name, "final.ods")
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())

    def test_same_name_different_content_saved_beside_never_overwritten(self):
        raw = self.root / "raw"
        raw.mkdir()
        old = raw / "f.ods"
        old.write_bytes(self.body())
        before = old.read_bytes()
        new_body = self.body(published=date(2026, 3, 5))
        s = _Session(new_body, "https://assets.example/media/3/f.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet():
            path, _ = m.fetch("https://assets.example/media/3/f.ods", raw,
                              session=s)
        sha8 = hashlib.sha256(new_body).hexdigest()[:8]
        self.assertEqual(path.name, f"f-{sha8}.ods")
        self.assertEqual(path.read_bytes(), new_body)
        self.assertEqual(old.read_bytes(), before)

    def test_an_existing_same_named_file_does_not_skip_the_download(self):
        raw = self.root / "raw"
        raw.mkdir()
        body = self.body()
        (raw / "f.ods").write_bytes(body)
        s = _Session(body, "https://assets.example/media/3/f.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, _ = m.fetch("https://assets.example/media/3/f.ods", raw,
                              session=s)
        self.assertEqual(s.calls, ["https://assets.example/media/3/f.ods"])
        self.assertEqual(path, raw / "f.ods")
        self.assertIn("same content", out.getvalue())

    def test_size_floor_format_and_sheet_checks_keep_nothing(self):
        raw = self.root / "raw"
        s = _Session(b"x" * 50, "https://assets.example/media/3/f.ods")
        with self.assertRaises(SystemExit), quiet():
            m.fetch("https://assets.example/media/3/f.ods", raw, session=s)
        s = _Session(b"x" * 5000, "https://assets.example/media/3/f.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit), quiet():
            m.fetch("https://assets.example/media/3/f.ods", raw, session=s)
        no_table = write_2024_layout(self.root / "src" / "n.ods")
        import pandas as pd
        with pd.ExcelWriter(no_table, engine="odf") as w:
            pd.DataFrame([["x"]]).to_excel(w, sheet_name="Cover",
                                           header=False, index=False)
        s = _Session(no_table.read_bytes(),
                     "https://assets.example/media/3/f.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit) as e, quiet():
            m.fetch("https://assets.example/media/3/f.ods", raw, session=s)
        self.assertIn("Table_1_Total", str(e.exception.code))
        s = _Session(self.body(), "https://assets.example/media/3/f.xlsx")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit), quiet():
            m.fetch("https://assets.example/media/3/f.xlsx", raw, session=s)
        self.assertEqual(sorted(p.name for p in raw.iterdir())
                         if raw.exists() else [], [])


class Module(unittest.TestCase):

    def test_no_zero_coercion_commit_or_recode_dict(self):
        import check_loaders
        reasons = check_loaders.check_loader(Path(m.__file__),
                                             {"revises_back_series": False})
        self.assertFalse([r for r in reasons if "zero coercion" in r
                          or "commit" in r or "recode dict" in r], reasons)

    def test_the_held_file_constants(self):
        self.assertEqual(m.LEGACY_FILE["sha256"], SHA_2025)
        self.assertEqual(m.LEGACY_RANK, (date(2026, 2, 26), 2025))
        self.assertEqual(m.LEGACY_LIVE[0], 296)

    def test_spec_and_profile(self):
        s = m.SPEC
        self.assertEqual((s.live_table, s.editions_table, s.period_col,
                          s.key_cols), ("la_rough_sleeping",
                                        "la_rough_sleeping_editions",
                                        "snapshot_year", ("lad24cd",)))
        self.assertEqual(s.value_cols, (
            ("rough_sleeping", "integer NOT NULL"),
            ("rough_sleeping_prev_year", "integer NOT NULL")))
        p = m.PROFILE
        self.assertTrue(p.file_checks)
        self.assertEqual((p.run_agent, p.run_source, p.savepoint),
                         ("s10_rough_sleeping_editions", "10", "s10_period"))


if __name__ == "__main__":
    unittest.main()
