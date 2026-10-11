"""Pure tests for s5_imd_editions: discovery, the File 10 reader, cells,
records, the rank and range checks, geography, ranks, ledger and planning,
the stop conditions and the download rules.

No database and no network: every .xlsx file is written by the tests
(openpyxl, then docProps/core.xml rewritten so the modified date is the
test's), and the content API replies and downloads are stubbed. The fixture
writers are reused by test_s5_imd_loader.
"""
import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s5_imd_editions as m  # noqa: E402

UTC = timezone.utc
MODIFIED = datetime(2025, 11, 14, 12, 48, 6, tzinfo=UTC)
# the fixture's authorities, on the old Barnsley/Sheffield codes (File 10's
# form)
CODES = ["E06000001", "E06000002", "E08000016", "E08000019", "E09000033"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000016": "Barnsley", "E08000019": "Sheffield",
         "E08000038": "Barnsley", "E08000039": "Sheffield",
         "E09000033": "Westminster", "E07000999": "Nowhere"}
# per column, the values of CODES in order (each column most deprived first,
# so the ranks are 1..5); two published zeros tie in the proportion column
# (both rank 4) and one in extent
COLUMN_VALUES = {
    "imd_average_rank": [25000.12, 24000.5, 23000, 22000.25, 21000.75],
    "imd_rank_of_average_rank": [1, 2, 3, 4, 5],
    "imd_average_score": [40.123, 35.5, 30.0, 25.25, 20.001],
    "imd_rank_of_average_score": [1, 2, 3, 4, 5],
    "imd_proportion_most_deprived_10": [0.5, 0.4211, 0.1, 0, 0],
    "imd_rank_of_proportion": [1, 2, 3, 4, 4],
    "imd_extent": [0.6, 0.5259, 0.2, 0.1, 0],
    "imd_rank_of_extent": [1, 2, 3, 4, 5],
    "imd_local_concentration": [33672.1, 33623.27, 30000.0, 29000.5, 28000],
    "imd_rank_of_local_concentration": [1, 2, 3, 4, 5],
}
HEADER_2025 = list(m.FILE10_HEADERS["IoD2025"])
HEADER_2030 = [h.replace("IMD25 - ", "IMD30 - ") for h in HEADER_2025]
TITLE = "English indices of deprivation {}"
BASE = "/government/statistics/english-indices-of-deprivation-{}"
ATT = "File 10: Local Authority District summaries - lower tier"
SHA_HELD = ("6c38a2fd3998e0caf76a3ef8da331613"
            "d52d6d0320065ccef346103fd9770a7e")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def value(code, col):
    i = CODES.index(code) if code in CODES else 4
    return COLUMN_VALUES[col][i]


def imd_rows(codes=None, overrides=None, names=None):
    """The IMD data rows: [code, name, ten values] per code."""
    overrides = overrides or {}
    out = []
    for c in codes or CODES:
        vals = [overrides.get((c, col), value(c, col)) for col in m.VALUES]
        out.append([c, (names or NAMES).get(c, "Somewhere")] + vals)
    return out


def core_xml(modified=MODIFIED, *, raw=None):
    if raw is not None:
        return raw
    mod = ("" if modified is None else
           '<dcterms:modified xsi:type="dcterms:W3CDTF">'
           f'{modified.strftime("%Y-%m-%dT%H:%M:%SZ")}</dcterms:modified>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
            'package/2006/metadata/core-properties" xmlns:dc="http://purl.org'
            '/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://'
            'www.w3.org/2001/XMLSchema-instance"><cp:lastModifiedBy>Test'
            f'</cp:lastModifiedBy>{mod}</cp:coreProperties>')


def write_file10(path, *, vintage="IoD2025", modified=MODIFIED, codes=None,
                 overrides=None, header=None, sheets=None, rows=None,
                 extra_rows=(), core=None, names=None):
    """A File 10 workbook: the eleven sheets (IMD filled, the others with a
    header cell), docProps/core.xml carrying `modified` (None: no modified
    element; core: the whole XML)."""
    import openpyxl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    hdr = header or (HEADER_2030 if vintage == "IoD2030" else HEADER_2025)
    for s in sheets or m.SHEETS:
        ws = wb.create_sheet(s)
        if s == "IMD":
            ws.append(hdr)
            for r in (rows if rows is not None
                      else imd_rows(codes, overrides, names)):
                ws.append(r)
            for r in extra_rows:
                ws.append(r)
        elif s != "Notes":
            ws.append(["Local Authority District code (2024)"])
    tmp = path.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    xml = core_xml(modified, raw=core)
    with zipfile.ZipFile(tmp) as zin, \
            zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "docProps/core.xml":
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    tmp.unlink()
    return path


def collection(*years, extra=(), title="English indices of deprivation"):
    docs = [{"title": TITLE.format(y), "base_path": BASE.format(y)}
            for y in years]
    docs += [{"title": t, "base_path": f"/government/publications/x-{i}"}
             for i, t in enumerate(extra)]
    return {"title": title, "links": {"documents": docs}}


def release_page(year, attachments=None, *, title=None, history=()):
    if attachments is None:
        attachments = [
            (TITLE.format(year) + ": statistical release",
             BASE.format(year) + "/release"),
            (ATT, f"https://assets.example/media/m{year}/File_10_-_IoD{year}"
             "_Local_Authority_District_Summaries__lower-tier__v2.xlsx")]
    return {"title": title or TITLE.format(year),
            "first_published_at": f"{year}-10-30T00:00:00+00:00",
            "public_updated_at": f"{year}-11-17T09:20:39+00:00",
            "details": {"attachments": [{"title": t, "url": u}
                                        for t, u in attachments],
                        "change_history": [
                            {"public_timestamp": ts, "note": n}
                            for ts, n in (history or [(
                                f"{year}-10-30T00:00:00Z",
                                "First published.")])]}}


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0
        p = mock.patch.object(m, "EXPECTED_AREAS", len(CODES))
        p.start()
        self.addCleanup(p.stop)

    def write(self, **kw):
        self.n += 1
        return write_file10(self.root / f"f{self.n}.xlsx", **kw)

    def read(self, **kw):
        return m.read_file10(self.write(**kw))

    def raises(self, *needles, **kw):
        path = self.write(**kw)
        with self.assertRaises(ValueError) as e:
            m.read_file10(path)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):

    def test_newest_release_is_chosen(self):
        c = collection(2019, 2025, 2015, extra=(
            "English indices of deprivation 2019: mapping resources",
            "English indices of deprivation: privacy notice"))
        self.assertEqual(m.latest_release(c),
                         (TITLE.format(2025), BASE.format(2025)))

    def test_none_halts_listing_titles(self):
        c = collection(extra=("Something else",))
        with self.assertRaises(ValueError) as e:
            m.latest_release(c)
        self.assertIn("Something else", str(e.exception))

    def test_two_pages_for_one_year_raise(self):
        c = collection(2025, 2025)
        with self.assertRaises(ValueError) as e:
            m.latest_release(c)
        self.assertIn("2 documents", str(e.exception))

    def test_a_renamed_series_with_a_later_year_halts_naming_both(self):
        c = collection(2025, extra=("English Indices of Deprivation 2030",))
        with self.assertRaises(ValueError) as e:
            m.latest_release(c)
        msg = str(e.exception)
        self.assertIn("English Indices of Deprivation 2030", msg)
        self.assertIn("2025", msg)

    def test_release_for_year(self):
        c = collection(2019, 2025)
        self.assertEqual(m.release_for_year(c, 2019),
                         (TITLE.format(2019), BASE.format(2019)))
        with self.assertRaises(ValueError):
            m.release_for_year(c, 2030)

    def test_collection_title_checked(self):
        m.check_collection(collection(2025))
        with self.assertRaises(ValueError) as e:
            m.check_collection(collection(2025, title="Deprivation"))
        self.assertIn("Deprivation", str(e.exception))

    def test_file10_attachment_exactly_one(self):
        a = m.file10_attachment(release_page(2025))
        self.assertEqual(a["title"], ATT)
        self.assertTrue(a["url"].endswith(".xlsx"))
        for atts in ([], [(ATT, "a.xlsx"), (ATT + " (v1)", "b.xlsx")],
                     [("File 11: Local Authority District summaries - upper "
                       "tier", "c.xlsx")]):
            with self.subTest(atts=atts), self.assertRaises(ValueError) as e:
                m.file10_attachment(release_page(2025, atts))
            for t, _ in atts:
                self.assertIn(t, str(e.exception))
        with self.assertRaises(ValueError) as e:
            m.file10_attachment(release_page(2025, [(ATT, "x.csv")]))
        self.assertIn(".xlsx", str(e.exception))

    def test_change_history(self):
        page = release_page(2025, history=[
            ("2025-11-17T09:20:39Z", "Please ensure you are using the "
             "updated 'v2' of these workbooks."),
            ("2025-10-30T00:00:00Z", "First published.")])
        self.assertEqual(m.change_history(page)[0][0], "2025-11-17")
        self.assertIn("v2", m.change_history(page)[0][1])

    def test_nothing_newer_says_which_release_it_saw(self):
        lines = m.discovery_note(collection(2019, 2025), ["IoD2025"])
        self.assertTrue(any("English indices of deprivation 2025" in x
                            for x in lines), lines)
        self.assertEqual(m.discovery_note(collection(2030), ["IoD2025"]), [])


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

class ReadFile(Tmp):

    def test_identity_rows_and_rank(self):
        f = self.read()
        self.assertEqual(f["vintage"], "IoD2025")
        self.assertEqual(f["sheets"], list(m.SHEETS))
        self.assertEqual(f["modified"], MODIFIED)
        self.assertEqual(f["rank"], MODIFIED)
        self.assertEqual(len(f["rows"]), len(CODES))
        self.assertEqual(f["header"], HEADER_2025)
        self.assertEqual(f["rows"][0]["code"], "E06000001")

    def test_headers_are_held_verbatim_with_trailing_spaces(self):
        h = m.FILE10_HEADERS["IoD2025"]
        self.assertEqual(len(h), 12)
        self.assertEqual(h[0], "Local Authority District code (2024)")
        self.assertEqual(h[2], "IMD - Average rank ")
        self.assertEqual(h[11], "IMD25 - Rank of local concentration ")
        # trailing spaces ignored when reading
        f = self.read(header=[x.rstrip() for x in HEADER_2025])
        self.assertEqual(f["vintage"], "IoD2025")

    def test_a_missing_or_extra_sheet_raises(self):
        self.raises("Crime", sheets=[s for s in m.SHEETS if s != "Crime"])
        self.raises("Extra", sheets=list(m.SHEETS) + ["Extra"])

    def test_a_renamed_header_raises(self):
        h = list(HEADER_2025)
        h[8] = "IMD25 - Extent of deprivation"
        self.raises("IMD25 - Extent of deprivation", header=h)

    def test_another_vintage_halts_until_its_header_is_declared(self):
        msg = self.raises("IMD30 - Extent", vintage="IoD2030")
        self.assertIn("deliberately", msg)
        with mock.patch.dict(m.FILE10_HEADERS, {"IoD2030": tuple(HEADER_2030)}):
            self.assertEqual(self.read(vintage="IoD2030")["vintage"],
                             "IoD2030")

    def test_a_295_row_file_raises(self):
        self.raises("4 authorities", codes=CODES[:-1])

    def test_a_blank_or_text_cell_raises(self):
        self.raises("blank", overrides={("E06000002", "imd_extent"): None})
        self.raises("text", overrides={("E06000002", "imd_extent"): "0.5"})
        self.raises("text", overrides={("E06000002", "imd_rank_of_extent"):
                                       "[x]"})

    def test_bad_rows_raise(self):
        self.raises("E92000001", extra_rows=[["E92000001", "England"] + [1] * 10])
        self.raises("twice", codes=CODES + ["E06000001"])
        self.raises("after", extra_rows=[[None] * 12,
                                         ["E07000999", "Nowhere"] + [1] * 10])
        self.raises("name", names={"E06000002": None})

    def test_no_docprops_modified_raises(self):
        self.raises("modified", modified=None)
        self.raises("modified", core="<not xml")


class Cells(unittest.TestCase):

    def test_num_cell(self):
        self.assertEqual(m.num_cell(3, "w"), 3)
        self.assertEqual(m.num_cell(0, "w"), 0)
        self.assertEqual(m.num_cell(0.4211, "w"), 0.4211)
        for bad in (None, "", "  ", "3", "[x]", True, float("nan"),
                    float("inf"), object()):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as e:
                m.num_cell(bad, "IMD row 2 imd_extent")
            self.assertIn("IMD row 2 imd_extent", str(e.exception))


class Records(Tmp):

    def resolve(self, c):
        return c

    def test_build_records_at_the_live_scales_and_zeros_stay_zero(self):
        recs = m.build_records(self.read(), self.resolve)
        self.assertEqual([r["lad24cd"] for r in recs], sorted(CODES))
        r = {x["lad24cd"]: x for x in recs}
        self.assertEqual(r["E06000001"]["vintage"], "IoD2025")
        self.assertEqual(r["E06000001"]["la_name"], "Hartlepool")
        self.assertEqual(r["E06000001"]["imd_average_rank"], Decimal("25000.12"))
        self.assertEqual(str(r["E08000016"]["imd_average_rank"]), "23000.00")
        self.assertEqual(r["E06000002"]["imd_proportion_most_deprived_10"],
                         Decimal("0.4211"))
        z = r["E08000019"]["imd_proportion_most_deprived_10"]
        self.assertIsNotNone(z)
        self.assertEqual(z, 0)
        self.assertEqual(str(z), "0.0000")
        self.assertEqual(r["E09000033"]["imd_extent"], 0)
        self.assertIsInstance(r["E06000001"]["imd_rank_of_extent"], int)

    def test_more_decimals_than_the_column_holds_raises(self):
        f = self.read(overrides={("E06000002", "imd_average_rank"): 24000.555})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, self.resolve)
        self.assertIn("round", str(e.exception))
        f = self.read(overrides={("E06000002", "imd_rank_of_average_rank"):
                                 2.5})
        with self.assertRaises(ValueError):
            m.build_records(f, self.resolve)

    def test_unresolved_or_duplicate_key_raises(self):
        f = self.read()
        with self.assertRaises(ValueError):
            m.build_records(f, {c: c for c in CODES[1:]})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, lambda c: "E06000001")
        self.assertIn("duplicate", str(e.exception))

    def test_rows_content_sha_is_key_name_and_ten_measures(self):
        recs = m.build_records(self.read(), self.resolve)
        a = m.rows_content_sha(recs)
        self.assertEqual(a, m.rows_content_sha(list(reversed(recs))))
        # where it was read or extra keys never change it
        self.assertEqual(a, m.rows_content_sha(
            [dict(r, source_file="x", vintage="IoD2030") for r in recs]))
        b = [dict(r) for r in recs]
        b[0]["la_name"] = "Other"
        self.assertNotEqual(a, m.rows_content_sha(b))
        c = [dict(r) for r in recs]
        c[0]["imd_rank_of_extent"] += 1
        self.assertNotEqual(a, m.rows_content_sha(c))
        # a stored numeric(8,4) zero renders as the record's zero
        d = [dict(r) for r in recs]
        d[-1]["imd_extent"] = Decimal("0")
        self.assertEqual(a, m.rows_content_sha(d))
        first = sorted(recs, key=lambda r: r["lad24cd"])[0]
        line = "|".join([first["lad24cd"], first["la_name"]]
                        + [m._fmt(c, first[c]) for c in m.VALUES])
        self.assertTrue(line.startswith("E06000001|Hartlepool|25000.12|1|"
                                        "40.123|1|0.5000|1|0.6000|1|"
                                        "33672.10|1"), line)


class Checks(Tmp):

    def recs(self, **kw):
        return m.build_records(self.read(**kw), lambda c: c)

    def test_the_fixture_passes_with_its_tie(self):
        lines = m.checks(self.recs())
        self.assertTrue(lines)

    def test_a_rank_column_that_is_not_a_ranking_raises(self):
        for rank_col, vals in (
                ("imd_rank_of_average_rank", [1, 2, 3, 4, 4]),   # duplicate
                ("imd_rank_of_average_rank", [1, 2, 3, 4, 6]),   # gap
                ("imd_rank_of_proportion", [1, 2, 3, 5, 5]),     # tie wrong
                ("imd_rank_of_extent", [0, 2, 3, 4, 5])):        # out of range
            ov = {(c, rank_col): v for c, v in zip(CODES, vals)}
            with self.subTest(col=rank_col, vals=vals), \
                    self.assertRaises(ValueError) as e:
                m.checks(self.recs(overrides=ov))
            self.assertIn(rank_col, str(e.exception))

    def test_a_rank_against_its_measure_order_raises(self):
        ov = {("E06000001", "imd_rank_of_average_score"): 2,
              ("E06000002", "imd_rank_of_average_score"): 1}
        with self.assertRaises(ValueError) as e:
            m.checks(self.recs(overrides=ov))
        self.assertIn("imd_rank_of_average_score", str(e.exception))

    def test_equal_measures_may_rank_apart(self):
        # the publisher ranks unrounded values: two scores equal at 3 dp may
        # hold different ranks
        ov = {("E06000002", "imd_average_score"): 40.123}
        m.checks(self.recs(overrides=ov))

    def test_proportion_and_extent_outside_zero_one_raise(self):
        for col, v in (("imd_extent", 1.5),
                       ("imd_proportion_most_deprived_10", -0.1)):
            ov = {("E06000001", col): v}
            with self.subTest(col=col), self.assertRaises(ValueError) as e:
                m.checks(self.recs(overrides=ov))
            self.assertIn(col, str(e.exception))


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


class Geography(unittest.TestCase):

    def test_old_codes_resolve_to_themselves(self):
        rmap, problems = m.resolve_codes(_Cur(CODES), set(CODES))
        self.assertEqual(problems, [])
        self.assertEqual(rmap, {c: c for c in CODES})

    def test_e08000038_against_old_halts(self):
        _, problems = m.resolve_codes(_Cur(CODES), {"E06000001", "E08000038"})
        self.assertTrue(any("E08000038" in p and "'old'" in p
                            for p in problems), problems)

    def test_an_unknown_code_is_unexplained(self):
        _, problems = m.resolve_codes(_Cur(CODES), {"E06000001", "E07000999"})
        self.assertTrue(any("UNEXPLAINED E07000999" in p for p in problems))

    def test_the_declaration_is_old_with_the_file_evidence(self):
        import geography
        form, ev = geography.DATASET_FORM["5"]
        self.assertEqual(form, "old")
        self.assertIn("data/raw/s5_imd", ev)
        self.assertIn("v2", ev)
        self.assertIn("2026-10-10", ev)


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

R1 = datetime(2025, 10, 28, 9, 0, 0, tzinfo=UTC)
R2 = MODIFIED


class Ranks(Tmp):

    def test_rank_from_docprops_never_the_file_name(self):
        a = self.write(modified=R1)
        b = self.root / ("File_10_-_IoD2025_Local_Authority_District_"
                         "Summaries__lower-tier__v2.xlsx")
        b.write_bytes(a.read_bytes())
        self.assertEqual(m.release_rank(m.read_file10(a)), R1)
        # 'v2' in the name is not evidence: same bytes, same rank
        self.assertEqual(m.release_rank(m.read_file10(b)), R1)
        self.assertIsNone(m.release_rank(b.name))
        self.assertIsNone(m.release_rank(None))
        self.assertEqual(m.release_rank(m.read_file10(self.write())), R2)

    def test_rank_from_an_edition_or_ledger_source(self):
        f = self.read()
        src = m.edition_source(f, {"file_name": "x.xlsx", "where": "u"})
        self.assertEqual(m.release_rank(src), R2)
        self.assertIn("docProps modified 2025-11-14T12:48:06Z", src)
        lsrc = m.ledger_source("https://a/x.xlsx", "IoD2025", R2)
        self.assertEqual(m.release_rank(lsrc), R2)
        self.assertEqual(m.parse_ledger_source(lsrc),
                         ("https://a/x.xlsx", "IoD2025", R2))
        self.assertIsNone(m.parse_ledger_source("https://a/x.xlsx"))

    def test_content_sha256(self):
        p = self.root / "b.bin"
        p.write_bytes(b"abc")
        self.assertEqual(m.content_sha256(p),
                         hashlib.sha256(b"abc").hexdigest())


class Ledger(unittest.TestCase):

    URL = "https://a/x.xlsx"

    def test_ledger_skip_needs_the_pair_for_the_period(self):
        src = m.ledger_source(self.URL, "IoD2025", R2)
        checked = {"IoD2025": {(src, "s")}}
        self.assertEqual(m.ledger_complete(checked, self.URL, "s",
                                           {"IoD2025": R2}), ["IoD2025"])
        self.assertIsNone(m.ledger_complete(checked, self.URL, "other",
                                            {"IoD2025": R2}))
        self.assertIsNone(m.ledger_complete(checked, "https://b/x.xlsx", "s",
                                            {"IoD2025": R2}))
        # recorded under another period only: read again
        self.assertIsNone(m.ledger_complete({"IoD2030": {(src, "s")}},
                                            self.URL, "s", {"IoD2025": R2}))
        self.assertIsNone(m.ledger_complete(checked, self.URL, "s",
                                            {"IoD2025": R2},
                                            stranded=["IoD2025"]))

    def test_a_period_held_from_a_newer_file_is_read_and_planned_older(self):
        src = m.ledger_source(self.URL, "IoD2025", R1)
        checked = {"IoD2025": {(src, "s")}}
        self.assertIsNone(m.ledger_complete(checked, self.URL, "s",
                                            {"IoD2025": R2}))

    def test_plan(self):
        src = m.ledger_source(self.URL, "IoD2025", R2)
        self.assertEqual(m.plan_vintage("IoD2025", R2, {}, {}, src, "s",
                                        recheck=None, allow_older=False),
                         (["IoD2025"], [], {}))
        self.assertEqual(m.plan_vintage("IoD2025", R2, {"IoD2025": R1}, {},
                                        src, "s", recheck=None,
                                        allow_older=False),
                         ([], ["IoD2025"], {}))
        new, rev, skip = m.plan_vintage("IoD2025", R1, {"IoD2025": R2}, {},
                                        src, "s", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["IoD2025"].startswith("older"))
        _, rev, _ = m.plan_vintage("IoD2025", R1, {"IoD2025": R2}, {}, src,
                                   "s", recheck=None, allow_older=True)
        self.assertEqual(rev, ["IoD2025"])
        new, rev, skip = m.plan_vintage("IoD2025", R2, {"IoD2025": R2},
                                        {"IoD2025": {(src, "s")}}, src, "s",
                                        recheck=None, allow_older=False)
        self.assertTrue(skip["IoD2025"].startswith("checked"))
        _, rev, _ = m.plan_vintage("IoD2025", R2, {"IoD2025": R2},
                                   {"IoD2025": {(src, "s")}}, src, "s",
                                   recheck="IoD2025", allow_older=False)
        self.assertEqual(rev, ["IoD2025"])
        with self.assertRaises(ValueError):
            m.plan_vintage("IoD2025", R2, {"IoD2025": R2}, {}, src, "s",
                           recheck="IoD2030", allow_older=False)

    def test_an_earlier_vintage_than_the_newest_held_is_older(self):
        src = m.ledger_source(self.URL, "IoD2019", R1)
        new, rev, skip = m.plan_vintage("IoD2019", R1, {"IoD2025": R2}, {},
                                        src, "s", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip["IoD2019"].startswith("older"))
        new, _, _ = m.plan_vintage("IoD2019", R1, {"IoD2025": R2}, {}, src,
                                   "s", recheck=None, allow_older=True)
        self.assertEqual(new, ["IoD2019"])


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def recs(codes, **vals):
    out = []
    for c in codes:
        r = {"lad24cd": c, "vintage": "IoD2025", "la_name": NAMES.get(c, "x")}
        r.update({col: vals.get(col, 1) for col in m.VALUES})
        out.append(r)
    return out


class Stops(unittest.TestCase):

    def test_a_revision_missing_or_adding_an_authority_is_partial(self):
        tip = recs(CODES)
        self.assertEqual(m.period_problems(recs(CODES), tip, kind="revised"),
                         [])
        p = m.period_problems(recs(CODES[:-1]), tip, kind="revised")
        self.assertTrue(p and p[0].startswith(m.PARTIAL), p)
        p = m.period_problems(recs(CODES + ["E07000999"]), tip,
                              kind="revised")
        self.assertTrue(p and p[0].startswith(m.PARTIAL), p)

    def test_a_new_vintage_into_an_empty_live_table_must_be_the_live_one(self):
        with mock.patch.object(m, "EXPECTED_AREAS", len(CODES)):
            self.assertEqual(m.period_problems(recs(CODES), None, kind="new",
                                               vintage="IoD2025",
                                               live_empty=True), [])
            p = m.period_problems(recs(CODES), None, kind="new",
                                  vintage="IoD2030", live_empty=True)
        self.assertTrue(any("IoD2025" in x and "IoD2030" in x for x in p), p)

    def test_differs(self):
        a = recs(CODES)
        self.assertFalse(m._differs(a, recs(CODES)))
        b = recs(CODES)
        b[0]["la_name"] = "Other"
        self.assertTrue(m._differs(a, b))
        self.assertTrue(m._differs(a, recs(CODES[:-1])))


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
        return write_file10(self.root / "src" / f"b{self.n}.xlsx",
                            **kw).read_bytes()

    def test_redirect_records_the_final_url(self):
        body = self.body()
        s = _Session(body, "https://assets.example/media/2/final.xlsx")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, final = m.fetch("https://assets.example/media/1/old.xlsx",
                                  self.root / "raw", session=s)
        self.assertEqual(final, "https://assets.example/media/2/final.xlsx")
        self.assertEqual(path.name, "final.xlsx")
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())

    def test_same_name_different_content_saved_beside_never_overwritten(self):
        raw = self.root / "raw"
        raw.mkdir()
        old = raw / "f.xlsx"
        old.write_bytes(self.body())
        before = old.read_bytes()
        new_body = self.body(modified=R1)
        s = _Session(new_body, "https://assets.example/media/3/f.xlsx")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet():
            path, _ = m.fetch("https://assets.example/media/3/f.xlsx", raw,
                              session=s)
        sha8 = hashlib.sha256(new_body).hexdigest()[:8]
        self.assertEqual(path.name, f"f-{sha8}.xlsx")
        self.assertEqual(path.read_bytes(), new_body)
        self.assertEqual(old.read_bytes(), before)

    def test_an_existing_same_named_file_does_not_skip_the_download(self):
        raw = self.root / "raw"
        raw.mkdir()
        body = self.body()
        (raw / "f.xlsx").write_bytes(body)
        s = _Session(body, "https://assets.example/media/3/f.xlsx")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, _ = m.fetch("https://assets.example/media/3/f.xlsx", raw,
                              session=s)
        self.assertEqual(s.calls, ["https://assets.example/media/3/f.xlsx"])
        self.assertEqual(path, raw / "f.xlsx")
        self.assertIn("same content", out.getvalue())

    def test_size_floor_format_and_sheet_checks_keep_nothing(self):
        raw = self.root / "raw"
        url = "https://assets.example/media/3/f.xlsx"
        s = _Session(b"x" * 50, url)
        with self.assertRaises(SystemExit), quiet():
            m.fetch(url, raw, session=s)
        s = _Session(b"x" * 5000, url)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit), quiet():
            m.fetch(url, raw, session=s)
        s = _Session(self.body(sheets=["Notes", "Income"]), url)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit) as e, quiet():
            m.fetch(url, raw, session=s)
        self.assertIn("IMD", str(e.exception.code))
        s = _Session(self.body(), "https://assets.example/media/3/f.ods")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), \
                self.assertRaises(SystemExit), quiet():
            m.fetch("https://assets.example/media/3/f.ods", raw, session=s)
        self.assertEqual(sorted(p.name for p in raw.iterdir())
                         if raw.exists() else [], [])


class Module(unittest.TestCase):

    def test_no_zero_coercion_commit_or_recode_dict(self):
        import check_loaders
        reasons = check_loaders.check_loader(Path(m.__file__),
                                             {"revises_back_series": True})
        self.assertFalse([r for r in reasons if "zero coercion" in r
                          or "commit" in r or "recode dict" in r
                          or "editions_core" in r], reasons)

    def test_the_held_file_constants(self):
        self.assertEqual(m.LEGACY_FILE["sha256"], SHA_HELD)
        self.assertEqual(m.LEGACY_RANK, MODIFIED)
        self.assertEqual(m.LEGACY_LIVE, (296, "60f16360106cf7e2"
                                              "5cba3f4283f4bae8"))
        self.assertTrue(m.LEGACY_FILE["url"].startswith(
            "https://assets.publishing.service.gov.uk/media/"))

    def test_spec_and_profile(self):
        s = m.SPEC
        self.assertEqual((s.live_table, s.editions_table, s.period_col,
                          s.key_cols), ("la_imd_2025", "la_imd_2025_editions",
                                        "vintage", ("lad24cd",)))
        self.assertTrue(s.live_single_period)
        self.assertFalse(s.refresh_key_changes)
        self.assertEqual(dict(s.refresh_from)["source"], "release_label")
        self.assertNotIn("source", s.compare_cols)
        self.assertEqual(set(s.compare_cols), set(m.VALUES) | {"la_name"})
        self.assertEqual(len(m.VALUES), 10)
        p = m.PROFILE
        self.assertTrue(p.file_checks)
        self.assertEqual((p.run_agent, p.run_source, p.savepoint),
                         ("s5_imd_editions", "5", "s5_period"))

    def test_the_live_alter_is_the_planned_statement(self):
        self.assertEqual(
            m.live_alter_sql(m.SPEC),
            "ALTER TABLE public.la_imd_2025 ADD COLUMN vintage text NOT NULL "
            "DEFAULT 'IoD2025' CHECK (vintage = 'IoD2025')")


if __name__ == "__main__":
    unittest.main()
