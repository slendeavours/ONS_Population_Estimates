"""Pure tests for s21_neighbours_editions: discovery (the ONS /data JSON),
the workbook reader and its identity checks, cells, records, LIVE_RULE,
ranks, ledger and planning, the stop conditions and the download rules.

No database and no network: every .xlsx file is written by the tests
(openpyxl, then docProps/core.xml rewritten so the modified date is the
test's), and the ONS replies and downloads are stubbed. The fixture writers
are reused by test_s21_neighbours_loader. Nothing here imports or runs the
old build (scripts/s21_statistical_neighbours_build.py).
"""
import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s21_neighbours_editions as m  # noqa: E402

UTC = timezone.utc
MODIFIED = datetime(2026, 3, 17, 14, 25, 14, tzinfo=UTC)
PUBLISHED = "18 March 2026"
P = "2026-03-18"
# English authorities of the fixture (Barnsley and Sheffield on the new
# codes, the publisher's form for source 21), one Welsh and one Scottish
# subject (read and counted, never loaded)
ENGLISH = ["E06000001", "E06000002", "E08000038", "E08000039", "E09000033"]
SUBJECTS = ENGLISH + ["W06000022", "S12000033"]
# every neighbour code is a real la_boundaries authority (or its new
# Barnsley/Sheffield form), so the loader tests resolve them for real
POOL = ["E06000003", "E06000004", "E06000005", "E06000006", "E06000007",
        "E06000008", "E06000009", "E06000010", "E08000001", "E08000002",
        "E08000003", "E08000004", "E08000005", "E08000006", "E08000007",
        "E08000008", "E08000009", "E08000038", "E08000039", "E09000001",
        "E09000002", "E07000008", "E06000001", "E06000002", "E09000033"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000038": "Barnsley", "E08000039": "Sheffield",
         "E09000033": "Westminster", "W06000022": "Newport",
         "S12000033": "Aberdeen City", "N09000001": "Antrim and Newtownabbey"}
# per subject: {publisher rank: code}; E06000001's neighbour 3 is Welsh (so
# its neighbour 6 is promoted to live rank 5); E06000002 has a Scottish
# neighbour 1 and a Northern Irish neighbour 20
OVERRIDES = {"E06000001": {3: "W06000022"},
             "E06000002": {1: "S12000033", 20: "N09000001"}}
COVER_TITLE = ("Clustering similar local authorities and statistical nearest "
               "neighbours in the UK")
SHEET_TITLE = "Table 7a: LTLA global statistical nearest neighbours"
NOTE2 = ("Models are based on the latest local authority and combined "
         "authority boundaries, as at March 2026. Where source data used "
         "older boundaries, an aggregation or proportional imputation method "
         "has been used to convert the data to the latest boundaries.")
NOTE12 = ('Missing values in our tables are marked with "[x]". Values which '
          'are not missing in our underlying data, but are suppressed in this '
          'publication for disclosure control are marked with "[c]". ')
FILE = "2026clusteringsimilarlocalauthoritiesandnearestneighbours.xlsx"
EDITION_URI = m.LANDING + "/{}"


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def name(code):
    return NAMES.get(code, f"Area {code}")


def neighbours(subject, overrides=None):
    """The subject's 20 neighbour codes in publisher rank order."""
    codes = [c for c in POOL if c != subject][:20]
    for rank, code in ((overrides if overrides is not None
                        else OVERRIDES).get(subject, {})).items():
        codes[rank - 1] = code
    return codes


def table_rows(subjects=None, overrides=None):
    out = []
    for s in subjects or SUBJECTS:
        row = [s, name(s)]
        for c in neighbours(s, overrides):
            row += [c, name(c)]
        out.append(row)
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


def write_s21(path, *, published=PUBLISHED, modified=MODIFIED, subjects=None,
              overrides=None, cells=None, header=None, cover_title=COVER_TITLE,
              sheet_title=SHEET_TITLE, contents_title=SHEET_TITLE,
              note2=NOTE2, note12=NOTE12, rows=None, extra_rows=(),
              sheets=None, cover_extra=(), core=None):
    """A clustering workbook with the sheets the loader reads: Cover sheet,
    Contents, Notes and 7.a (title, notes line, one-table line, header,
    data). cells: {(subject, column index): value} raw overrides of the
    data rows. docProps/core.xml carries `modified` (None: no modified
    element; core: the whole XML)."""
    import openpyxl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for s in sheets or ("Cover sheet", "Contents", "Notes", "7.a"):
        ws = wb.create_sheet(s)
        if s == "Cover sheet":
            ws.append([cover_title])
            ws.append(["Publication date"])
            if published is not None:
                ws.append(["The data tables in this spreadsheet were "
                           f"originally published at 9.30am {published}."])
            for x in cover_extra:
                ws.append([x])
            ws.append(["Statistical contact:"])
        elif s == "Contents":
            ws.append(["Table of contents"])
            ws.append(["Worksheet number/name", "Worksheet title"])
            ws.append(["1", "Table 1: LTLA cluster allocation by model"])
            ws.append(["7.a", contents_title])
        elif s == "Notes":
            ws.append(["Table footnotes", "Back to contents"])
            ws.append(["Note number", "Note text"])
            ws.append(["1", "These statistics are labelled as ‘official "
                       "statistics in development’."])
            if note2 is not None:
                ws.append(["2", note2])
            if note12 is not None:
                ws.append(["12", note12])
        elif s == "7.a":
            ws.append([sheet_title, None, None, "Back to contents"])
            ws.append(["Notes that apply to this table: [note 1, 2, 3, 5, "
                       "17, 18]"])
            ws.append(["This worksheet contains one table."])
            ws.append(list(header or m.HEADER))
            data = rows if rows is not None else table_rows(subjects,
                                                            overrides)
            for r in data:
                r = list(r)
                for (subj, col), v in (cells or {}).items():
                    if r and r[0] == subj:
                        r[col] = v
                ws.append(r)
            for r in extra_rows:
                ws.append(list(r))
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


def landing(*editions, title=None, extra_uris=()):
    uris = [EDITION_URI.format(e) for e in editions] + list(extra_uris)
    return {"type": "dataset_landing_page", "uri": m.LANDING,
            "datasets": [{"uri": u} for u in uris],
            "description": {"title": title or m.LANDING_TITLE,
                            "releaseDate": "2026-03-18T00:00:00.000Z",
                            "nextRelease": ""}}


def edition_page(edition="2026", files=(FILE,),
                 release="2026-03-18T00:00:00.000Z"):
    return {"downloads": [{"file": f} for f in files],
            "supplementaryFiles": [], "versions": [], "type": "dataset",
            "uri": EDITION_URI.format(edition),
            "description": {"releaseDate": release, "edition": edition}}


def resolve_all(code):
    """A stand-in for the geography step: the Barnsley/Sheffield recode,
    every other English code itself."""
    return {"E08000038": "E08000016", "E08000039": "E08000019"}.get(code,
                                                                   code)


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0
        p = mock.patch.object(m, "EXPECTED_SUBJECTS", len(ENGLISH))
        p.start()
        self.addCleanup(p.stop)

    def write(self, **kw):
        self.n += 1
        return write_s21(self.root / f"f{self.n}.xlsx", **kw)

    def read(self, **kw):
        return m.read_file(self.write(**kw))

    def raises(self, *needles, **kw):
        path = self.write(**kw)
        with self.assertRaises(ValueError) as e:
            m.read_file(path)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)

    def records(self, **kw):
        return m.build_records(self.read(**kw), resolve_all, {})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):

    def test_newest_edition_is_chosen(self):
        lp = landing("2026", "2027", "2024")
        self.assertEqual(m.editions_listed(lp),
                         [("2027", EDITION_URI.format("2027")),
                          ("2026", EDITION_URI.format("2026")),
                          ("2024", EDITION_URI.format("2024"))])
        self.assertEqual(m.latest_edition(lp),
                         ("2027", EDITION_URI.format("2027")))

    def test_a_newer_edition_than_held_is_named(self):
        lines = m.discovery_note(landing("2026", "2027"), [P])
        text = "\n".join(lines)
        self.assertIn("NEWER EDITION", text)
        self.assertIn("2027", text)
        self.assertIn("editions only", text)
        self.assertIn("never", text)
        lines = m.discovery_note(landing("2026"), [P])
        text = "\n".join(lines)
        self.assertIn("2026", text)
        self.assertIn("no newer edition", text)
        self.assertNotIn("NEWER EDITION", text)

    def test_none_or_an_unparseable_edition_halts_naming_it(self):
        with self.assertRaises(ValueError) as e:
            m.editions_listed(landing())
        self.assertIn("no edition", str(e.exception))
        odd = m.LANDING + "/2026revised"
        with self.assertRaises(ValueError) as e:
            m.editions_listed(landing("2026", extra_uris=(odd,)))
        self.assertIn(odd, str(e.exception))
        self.assertIn("2026", str(e.exception))

    def test_landing_title_is_checked(self):
        m.check_landing(landing("2026"))
        with self.assertRaises(ValueError) as e:
            m.check_landing(landing("2026", title="Something else"))
        self.assertIn("Something else", str(e.exception))

    def test_edition_download_exactly_one(self):
        self.assertEqual(m.edition_download(edition_page()),
                         (FILE, "2026-03-18T00:00:00.000Z"))
        for files in ((), (FILE, "b.xlsx")):
            with self.subTest(files=files), \
                    self.assertRaises(ValueError) as e:
                m.edition_download(edition_page(files=files))
            self.assertIn("exactly one", str(e.exception))
        with self.assertRaises(ValueError) as e:
            m.edition_download(edition_page(files=("x.csv",)))
        self.assertIn(".xlsx", str(e.exception))

    def test_edition_for_year(self):
        lp = landing("2024", "2026")
        self.assertEqual(m.edition_for_year(lp, 2026),
                         ("2026", EDITION_URI.format("2026")))
        with self.assertRaises(ValueError):
            m.edition_for_year(lp, 2025)

    def test_api_release_date_corroborates_the_cover(self):
        f = {"published": date(2026, 3, 18), "year": 2026}
        self.assertEqual(m.identity_problems(
            f, "2026", "2026-03-18T00:00:00.000Z"), [])
        # the evening before in UTC (a British Summer Time midnight)
        self.assertEqual(m.identity_problems(
            {"published": date(2026, 7, 1), "year": 2026}, "2026",
            "2026-06-30T23:00:00.000Z"), [])
        bad = m.identity_problems(f, "2026", "2026-03-11T00:00:00.000Z")
        self.assertTrue(bad and "2026-03-11" in bad[0], bad)
        bad = m.identity_problems(f, "2027", None)
        self.assertTrue(bad and "2027" in bad[0], bad)


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

class Cells(unittest.TestCase):

    def test_a_code_is_nine_characters(self):
        for v in ("E06000001", "W06000022", "S12000033", "N09000001"):
            self.assertEqual(m.code_cell(v, "here"), v)

    def test_marker_blank_and_malformed_halt(self):
        with self.assertRaises(ValueError) as e:
            m.code_cell("[x]", "row 9")
        self.assertIn("note 12", str(e.exception))
        self.assertIn("row 9", str(e.exception))
        for v in (None, "", "  "):
            with self.subTest(v=v), self.assertRaises(ValueError) as e:
                m.code_cell(v, "row 9")
            self.assertIn("blank", str(e.exception))
        for v in ("E0600001", "X06000001", "E06000001 ", "e06000001", 6,
                  "[c]", "E060000011"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                m.code_cell(v, "row 9")


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

class Read(Tmp):

    def test_reads_identity_and_counts_subjects_by_nation(self):
        f = self.read()
        self.assertEqual(f["release_date"], P)
        self.assertEqual(f["published"], date(2026, 3, 18))
        self.assertEqual(f["rank"], date(2026, 3, 18))
        self.assertEqual(f["title"], COVER_TITLE)
        self.assertEqual(f["contents_title"], SHEET_TITLE)
        self.assertEqual(f["sheet_title"], SHEET_TITLE)
        self.assertEqual(tuple(f["header"]), m.HEADER)
        self.assertEqual(f["note2"], NOTE2)
        self.assertEqual(f["note12"], NOTE12)
        self.assertEqual(f["boundaries"], "March 2026")
        self.assertEqual(f["modified"], MODIFIED)
        self.assertEqual(f["nations"], {"England": 5, "Wales": 1,
                                        "Scotland": 1})
        self.assertEqual(len(f["subjects"]), 7)
        s = f["subjects"][0]
        self.assertEqual((s["code"], s["name"]), ("E06000001", "Hartlepool"))
        self.assertEqual(len(s["neighbours"]), 20)
        self.assertEqual(s["neighbours"][2], (3, "W06000022", "Newport"))

    def test_the_header_is_exact(self):
        self.assertEqual(len(m.HEADER), 42)
        self.assertEqual(m.HEADER[:4], ("Local Authority Code",
                                        "Local Authority Name",
                                        "Neighbour 1: code",
                                        "Neighbour 1: name"))
        self.assertEqual(m.HEADER[-1], "Neighbour 20: name")

    def test_cover_date_mismatches_halt(self):
        self.raises("originally published", published=None)
        self.raises("originally published",
                    cover_extra=("The data tables in this spreadsheet were "
                                 "originally published at 9.30am 19 March "
                                 "2026.",))
        self.raises("31 February 2026", published="31 February 2026")
        self.raises("Cover sheet", cover_title="Clustering similar areas")

    def test_sheet_title_contents_and_header_mismatches_halt(self):
        self.raises("Table 7b", sheet_title="Table 7b: LTLA economic "
                    "statistical nearest neighbours")
        self.raises("Contents", contents_title="Table 7a: something else")
        hdr = list(m.HEADER)
        hdr[5] = "Neighbour 2: Name"
        msg = self.raises("Neighbour 2: Name", header=hdr)
        self.assertIn("Neighbour 2: name", msg)
        self.raises("Neighbour 20: name", header=list(m.HEADER[:-2]))
        self.raises("7.a", sheets=("Cover sheet", "Contents", "Notes"))

    def test_notes_and_docprops_are_required(self):
        self.raises("note 2", note2=None)
        self.raises("note 12", note12=None)
        self.raises("note 12", note12="Missing values are left blank.")
        self.raises("docProps", modified=None)

    def test_a_blank_pair_x_marker_or_malformed_code_halts(self):
        # Hartlepool's neighbour 4 (columns 8 and 9)
        msg = self.raises("blank", "E06000001",
                          cells={("E06000001", 8): None,
                                 ("E06000001", 9): None})
        self.assertIn("Neighbour 4: code", msg)
        self.raises("note 12", "Neighbour 4: code",
                    cells={("E06000001", 8): "[x]"})
        self.raises("E0600000X", cells={("E06000001", 8): "E0600000X"})
        self.raises("Neighbour 4: name", cells={("E06000001", 9): None})
        self.raises("note 12", cells={("E06000001", 9): "[x]"})
        self.raises("Local Authority Code",
                    cells={("E06000002", 0): "[x]"})

    def test_rows_after_a_blank_row_or_beyond_the_table_halt(self):
        self.raises("empty row", extra_rows=([None] * 42,
                                             table_rows(["E06000001"])[0]))
        bad = table_rows(["E06000001"])[0] + ["stray"]
        self.raises("beyond", rows=[bad])
        self.raises("twice", rows=table_rows(["E06000001", "E06000001"]))

    def test_rank_is_from_the_cover_never_the_name(self):
        path = write_s21(self.root / "2027clusteringsimilar.xlsx",
                         published="18 March 2026")
        self.assertEqual(m.read_file(path)["rank"], date(2026, 3, 18))
        self.assertEqual(m.release_rank(m.read_file(path)),
                         date(2026, 3, 18))
        self.assertIsNone(m.release_rank("2027clusteringsimilar.xlsx"))
        self.assertIsNone(m.release_rank(
            "https://www.ons.gov.uk/file?uri=/x/2027/2027.xlsx"))
        self.assertEqual(m.release_rank(m.edition_source(
            {"release_date": P, "rank": date(2026, 3, 18)},
            {"file_name": "2027.xlsx", "where": "data/raw/x.xlsx"})),
            date(2026, 3, 18))


# ---------------------------------------------------------------------------
# Records and the live rule
# ---------------------------------------------------------------------------

def by_key(records):
    return {(r["lad24cd"], r["statistical_neighbour_lad24cd"]): r
            for r in records}


class Records(Tmp):

    def test_england_subjects_only_twenty_rows_each(self):
        recs = self.records()
        self.assertEqual(len(recs), 5 * 20)
        self.assertEqual({r["lad24cd"] for r in recs},
                         {"E06000001", "E06000002", "E08000016", "E08000019",
                          "E09000033"})
        self.assertEqual({r["release_date"] for r in recs}, {P})
        for subj in {r["lad24cd"] for r in recs}:
            ranks = sorted(r["publisher_rank"] for r in recs
                           if r["lad24cd"] == subj)
            self.assertEqual(ranks, list(range(1, 21)))

    def test_new_barnsley_code_as_subject_and_neighbour_resolves(self):
        k = by_key(self.records())
        self.assertNotIn("E08000038", {a for a, _ in k})
        r = k[("E06000001", "E08000016")]
        self.assertEqual(r["neighbour_code_published"], "E08000038")
        self.assertEqual(r["neighbour_nation"], "England")
        self.assertEqual(r["neighbour_name"], "Barnsley")
        self.assertTrue(any(a == "E08000016" for a, _ in k))
        # resolution goes through the caller's resolver, nothing private
        seen = []
        m.build_records(self.read(), lambda c: seen.append(c)
                        or resolve_all(c), {})
        self.assertIn("E08000038", seen)
        self.assertTrue(all(c.startswith("E") for c in seen), seen)

    def test_a_welsh_neighbour_is_verbatim_with_england_rank_null(self):
        r = by_key(self.records())[("E06000001", "W06000022")]
        self.assertEqual(r["neighbour_code_published"], "W06000022")
        self.assertEqual(r["neighbour_nation"], "Wales")
        self.assertEqual(r["publisher_rank"], 3)
        self.assertIsNone(r["england_rank"])
        self.assertIsNone(r["neighbour_rank"])
        self.assertIn("england_rank=not_an_english_authority",
                      r["null_reasons"])
        self.assertIn("neighbour_rank=outside_live_rule", r["null_reasons"])
        k = by_key(self.records())
        s = k[("E06000002", "S12000033")]
        n = k[("E06000002", "N09000001")]
        self.assertEqual((s["neighbour_nation"], n["neighbour_nation"]),
                         ("Scotland", "Northern Ireland"))
        self.assertEqual((s["publisher_rank"], n["publisher_rank"]), (1, 20))
        # every NULL carries its reason; nothing else is NULL
        for x in self.records():
            nulls = sorted(c for c in ("england_rank", "neighbour_rank")
                           if x[c] is None)
            want = sorted(p.split("=")[0] for p in (x["null_reasons"] or ""
                                                     ).split("; ") if p)
            self.assertEqual(nulls, want, x)

    def test_live_rule_promotes_neighbour_6_and_numbers_1_to_5(self):
        recs = self.records()
        live = [x for x in m.live_rows(recs) if x["lad24cd"] == "E06000001"]
        k = by_key(recs)
        got = sorted((x["neighbour_rank"],
                      k[("E06000001", x["statistical_neighbour_lad24cd"])]
                      ["publisher_rank"]) for x in live)
        self.assertEqual(got, [(1, 1), (2, 2), (3, 4), (4, 5), (5, 6)])
        self.assertEqual(len(m.live_rows(recs)), 5 * 5)
        for x in m.live_rows(recs):
            self.assertEqual(set(x), {"lad24cd", "statistical_neighbour_lad24cd",
                                      "release_date", "neighbour_rank"})
        # the stored live rank is LIVE_RULE's, england_rank the order
        self.assertEqual(k[("E06000001", "E06000008")]["neighbour_rank"], 5)
        self.assertEqual(k[("E06000001", "E06000008")]["england_rank"], 5)
        self.assertEqual(k[("E06000001", "E06000009")]["england_rank"], 6)
        self.assertIsNone(k[("E06000001", "E06000009")]["neighbour_rank"])
        # Middlesbrough's neighbour 1 is Scottish: publisher 2..6 are live
        mb = sorted(k[("E06000002", x["statistical_neighbour_lad24cd"])]
                    ["publisher_rank"] for x in m.live_rows(recs)
                    if x["lad24cd"] == "E06000002")
        self.assertEqual(mb, [2, 3, 4, 5, 6])

    def test_live_rule_is_one_named_constant(self):
        self.assertIn("English", m.LIVE_RULE.name)
        self.assertEqual(m.LIVE_RULE.english_prefixes,
                         ("E06", "E07", "E08", "E09"))
        self.assertEqual((m.LIVE_RULE.take, m.LIVE_RULE.renumber,
                          m.LIVE_RULE.within_publisher_rank), (5, True, None))
        recs = self.records()
        # the alternative (Decisions item 1) is a change of the constant
        alt = m.LiveRule(name="the publisher's top five, English only, "
                         "ranks as published",
                         english_prefixes=("E06", "E07", "E08", "E09"),
                         within_publisher_rank=5, take=5, renumber=False)
        with mock.patch.object(m, "LIVE_RULE", alt):
            live = [x for x in m.live_rows(recs)
                    if x["lad24cd"] == "E06000001"]
        self.assertEqual(sorted(x["neighbour_rank"] for x in live),
                         [1, 2, 4, 5])
        # without the English filter the Welsh neighbour would be live
        wide = m.LiveRule(name="x", english_prefixes=("E", "W", "S", "N"),
                          within_publisher_rank=None, take=5, renumber=True)
        with mock.patch.object(m, "LIVE_RULE", wide):
            live = {x["statistical_neighbour_lad24cd"]
                    for x in m.live_rows(recs) if x["lad24cd"] == "E06000001"}
        self.assertIn("W06000022", live)

    def test_rule_counts(self):
        c = m.rule_counts(self.records())
        # Hartlepool: publisher 4, 5, 6 renumbered 3, 4, 5; Middlesbrough:
        # publisher 2..6 renumbered 1..5
        self.assertEqual(c["live_pairs"], 25)
        self.assertEqual(c["subjects"], 5)
        self.assertEqual(c["rank_differs"], 3 + 5)
        self.assertEqual(c["subjects_affected"], 2)
        self.assertEqual(c["subjects_top_non_english"], 2)
        self.assertEqual(c["from_beyond_top"], 2)
        self.assertEqual(c["furthest"], 6)

    def test_a_subject_listing_itself_or_a_neighbour_twice_halts(self):
        f = self.read(overrides={"E06000001": {4: "E06000001"}})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, resolve_all, {})
        self.assertIn("itself", str(e.exception))
        f = self.read(overrides={"E06000001": {4: "E06000003"}})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, resolve_all, {})
        self.assertIn("twice", str(e.exception))
        # the old and new Barnsley codes in one list reach one lad24cd
        f = self.read(overrides={"E06000001": {4: "E08000016"}})
        with self.assertRaises(ValueError) as e:
            m.build_records(f, resolve_all, {})
        self.assertIn("twice", str(e.exception))

    def test_an_unresolved_english_code_halts(self):
        f = self.read()
        with self.assertRaises(ValueError) as e:
            m.build_records(f, {"E06000001": "E06000001"}, {})
        self.assertIn("resolves to no lad24cd", str(e.exception))

    def test_content_sha_ignores_provenance(self):
        f = self.read()
        a = m.build_records(f, resolve_all, {})
        b = m.build_records(f, resolve_all, {"file_name": "other.xlsx"})
        self.assertEqual(b[0]["file_name"], "other.xlsx")
        self.assertEqual(m.rows_content_sha(a), m.rows_content_sha(b))
        c = [dict(x) for x in a]
        c[0]["neighbour_name"] = "Renamed"
        self.assertNotEqual(m.rows_content_sha(a), m.rows_content_sha(c))
        self.assertEqual(m.rows_content_sha(list(reversed(a))),
                         m.rows_content_sha(a))


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

class Problems(Tmp):

    def test_fewer_english_subjects_is_rejected_both_kinds(self):
        recs = self.records(subjects=ENGLISH[:4] + ["W06000022"])
        for kind, tip in (("new", None), ("revised", self.records())):
            with self.subTest(kind=kind):
                bad = m.period_problems(recs, tip, kind=kind)
                self.assertTrue(any(b.startswith(m.PARTIAL) and
                                    "4 English subjects" in b for b in bad),
                                bad)

    def test_a_reissue_changing_a_pair_needs_acknowledge(self):
        tip = self.records()
        self.assertEqual(m.period_problems(tip, tip, kind="revised"), [])
        new = self.records(overrides=dict(OVERRIDES,
                                          E09000033={2: "E07000008"}))
        bad = m.period_problems(new, tip, kind="revised")
        self.assertEqual(len(bad), 1, bad)
        self.assertFalse(bad[0].startswith(m.PARTIAL))
        self.assertIn("E09000033", bad[0])
        self.assertIn("--acknowledge", m.ACK_HINT)
        # a renamed neighbour is a change too
        ren = [dict(x) for x in tip]
        ren[0]["neighbour_name"] = "Renamed"
        self.assertEqual(len(m.period_problems(ren, tip, kind="revised")), 1)

    def test_a_subject_of_the_tip_missing_is_partial(self):
        tip = self.records()
        new = [x for x in tip if x["lad24cd"] != "E09000033"]
        with mock.patch.object(m, "EXPECTED_SUBJECTS", 4):
            bad = m.period_problems(new, tip, kind="revised")
        self.assertTrue(any(b.startswith(m.PARTIAL) and "E09000033" in b
                            for b in bad), bad)

    def test_another_release_into_an_empty_live_table(self):
        recs = self.records()
        bad = m.period_problems(recs, None, kind="new",
                                period="2027-03-01", live_empty=True)
        self.assertTrue(any("empty live table" in b for b in bad), bad)
        self.assertEqual(m.period_problems(recs, None, kind="new",
                                           period=P, live_empty=True), [])


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

class Planning(unittest.TestCase):

    def test_ledger_source_round_trip(self):
        s = m.ledger_source("https://x/y.xlsx", date(2026, 3, 18), P)
        self.assertEqual(m.parse_ledger_source(s),
                         ("https://x/y.xlsx", date(2026, 3, 18), P))
        self.assertEqual(m.release_rank(s), date(2026, 3, 18))
        self.assertIsNone(m.parse_ledger_source("https://x/y.xlsx"))

    def test_ledger_complete(self):
        r = date(2026, 3, 18)
        src = m.ledger_source("u", r, P)
        checked = {P: {(src, "abc")}}
        self.assertEqual(m.ledger_complete(checked, "u", "abc", {P: r}), [P])
        self.assertIsNone(m.ledger_complete(checked, "u", "zzz", {P: r}))
        self.assertIsNone(m.ledger_complete(checked, "v", "abc", {P: r}))
        self.assertIsNone(m.ledger_complete(checked, "u", "abc", {P: r},
                                            stranded=[P]))
        self.assertIsNone(m.ledger_complete({}, "u", "abc", {P: r}))

    def test_plan_periods(self):
        r = date(2026, 3, 18)
        self.assertEqual(m.plan_periods([P], r, {}, {}, "s", "h",
                                        recheck=None, allow_older=False),
                         ([P], [], {}))
        self.assertEqual(m.plan_periods([P], r, {P: r}, {}, "s", "h",
                                        recheck=None, allow_older=False),
                         ([], [P], {}))
        new, rev, skip = m.plan_periods([P], r, {P: r}, {P: {("s", "h")}},
                                        "s", "h", recheck=None,
                                        allow_older=False)
        self.assertIn("checked", skip[P])
        self.assertEqual(m.plan_periods([P], r, {P: r}, {P: {("s", "h")}},
                                        "s", "h", recheck=P,
                                        allow_older=False), ([], [P], {}))
        # an older release than the newest held: skipped unless allowed
        old = "2025-03-12"
        new, rev, skip = m.plan_periods([old], date(2025, 3, 12), {P: r}, {},
                                        "s", "h", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev), ([], []))
        self.assertTrue(skip[old].startswith("older"))
        self.assertEqual(m.plan_periods([old], date(2025, 3, 12), {P: r}, {},
                                        "s", "h", recheck=None,
                                        allow_older=True), ([old], [], {}))
        with self.assertRaises(ValueError):
            m.plan_periods([P], r, {}, {}, "s", "h", recheck=P,
                           allow_older=False)


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

    URL = m.ONS + "/file?uri=" + m.LANDING + "/2026/" + FILE

    def body(self, **kw):
        self.n += 1
        return write_s21(self.root / "src" / f"b{self.n}.xlsx",
                         **kw).read_bytes()

    def test_download_url_and_redirect(self):
        self.assertEqual(m.download_url(EDITION_URI.format("2026"), FILE),
                         self.URL)
        body = self.body()
        final = "https://www.ons.gov.uk/file?uri=/x/2026/moved.xlsx"
        s = _Session(body, final)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, got = m.fetch(self.URL, self.root / "raw", session=s)
        self.assertEqual(got, final)
        self.assertEqual(path.name, "moved.xlsx")
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())

    def test_same_name_different_content_is_kept_beside(self):
        raw = self.root / "raw"
        raw.mkdir()
        old = self.body()
        (raw / FILE).write_bytes(old)
        new = self.body(modified=datetime(2026, 4, 1, tzinfo=UTC))
        s = _Session(new, self.URL)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, _ = m.fetch(self.URL, raw, session=s)
        self.assertEqual((raw / FILE).read_bytes(), old)
        self.assertNotEqual(path.name, FILE)
        self.assertTrue(path.name.startswith(Path(FILE).stem + "-"))
        self.assertEqual(path.read_bytes(), new)
        self.assertIn("kept it untouched", out.getvalue())

    def test_an_existing_same_named_file_does_not_skip_the_download(self):
        raw = self.root / "raw"
        raw.mkdir()
        body = self.body()
        (raw / FILE).write_bytes(body)
        s = _Session(body, self.URL)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, _ = m.fetch(self.URL, raw, session=s)
        self.assertEqual(s.calls, [self.URL])
        self.assertEqual(path, raw / FILE)
        self.assertIn("same content", out.getvalue())

    def test_size_format_and_sheet_checks_keep_nothing(self):
        raw = self.root / "raw"
        for body, url, patch in ((b"x" * 50, self.URL, None),
                                 (b"x" * 5000, self.URL, 10),
                                 (self.body(sheets=("Cover sheet", "Notes")),
                                  self.URL, 10),
                                 (self.body(), self.URL.replace(".xlsx",
                                                                ".csv"), 10)):
            s = _Session(body, url)
            with self.subTest(n=len(body)), \
                    mock.patch.object(m, "MIN_FILE_BYTES",
                                      patch or m.MIN_FILE_BYTES), \
                    self.assertRaises(SystemExit), quiet():
                m.fetch(url, raw, session=s)
        self.assertFalse(raw.exists() and any(raw.iterdir()))


if __name__ == "__main__":
    unittest.main()
