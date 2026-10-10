"""Pure tests for s6_asylum_editions: discovery, the cover, Asy_D11, Asy_D09
and Reg_02 readers, cells, records, reconciliation, geography (on a fake
cursor), ranks, ledger and planning, the stop conditions and the download
rules.

No database and no network: every workbook is written by the tests
(openpyxl for .xlsx, pandas + odfpy for .ods) and the content API replies
and downloads are stubbed. The fixture writers are reused by
test_s6_asylum_loader.
"""
import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s6_asylum_editions as m  # noqa: E402

PUB = {"d11": m.PUBLICATIONS["d11"], "d09": m.PUBLICATIONS["d09"],
       "reg02": m.PUBLICATIONS["reg02"]}
Q3 = ["2025-09-30", "2025-12-31", "2026-03-31"]
Q4 = Q3 + ["2026-06-30"]
NAMES = {"E06000001": "Hartlepool", "E06000002": "Middlesbrough",
         "E08000016": "Barnsley", "E08000038": "Barnsley",
         "E07000223": "Adur", "E07000163": "Craven",
         "E07000164": "Hambleton", "E06000065": "North Yorkshire",
         "S12000033": "Aberdeen City", "W06000015": "Cardiff"}
REGION = {"E06000001": "North East", "E06000002": "North East",
          "E08000016": "Yorkshire and The Humber",
          "E08000038": "Yorkshire and The Humber",
          "E07000223": "South East", "E07000163": "Yorkshire and The Humber",
          "E07000164": "Yorkshire and The Humber",
          "E06000065": "Yorkshire and The Humber",
          "S12000033": "Scotland", "W06000015": "Wales"}
NA_SUB = "N/A - Subsistence Only (Dec 2023 - Dec 2024)"
ENGLISH = ["E06000001", "E06000002", "E08000016", "E07000223"]
D11_ROWS_PER_Q = 5            # English rows per quarter (4 authorities)
NE_ROWS_PER_Q = 2             # non-England rows per quarter
SHA_SPLIT = ("d2207eed53afa18c" "e2d2afb317b6358d")   # never a full literal


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def dtext(iso) -> str:
    return date.fromisoformat(iso).strftime("%d %b %Y")


def barnsley(q) -> str:
    """Barnsley's code in Asy_D11: E08000016 to 30 Sep 2025, then
    E08000038 (the publisher's switch)."""
    return "E08000016" if q <= "2025-09-30" else "E08000038"


def d11_rows(quarters=Q3, *, values=None, drop=(), add=(), floor_row=True,
             unallocated=("2025-09-30",)):
    """[[date, support, region, la, code, accommodation, people]] of a
    fixture Asy_D11: per quarter five English rows (Hartlepool,
    Middlesbrough twice, Barnsley, Adur with 'Subsistence only'), two
    non-England rows, and an unallocated row in the unallocated quarters;
    one pre-2018 row. values {(quarter, code, support): people} overrides;
    drop: (quarter, code, support) left out; add: more rows."""
    out = []
    if floor_row:
        out.append([dtext("2017-12-31"), "Section 4",
                    "N/A - Section 4 (pre-2018)", "N/A - Section 4 (pre-2018)",
                    "N/A - Section 4 (pre-2018)", "Dispersal Accommodation",
                    999])
    for i, q in enumerate(quarters):
        b = barnsley(q)
        base = [("Section 95", "E06000001", "Dispersal Accommodation", 100 + i),
                ("Section 95", "E06000002", "Dispersal Accommodation", 200 + i),
                ("Section 4", "E06000002", "Dispersal Accommodation", 20 + i),
                ("Section 98", b, "Contingency Accommodation - Hotel", 50 + i),
                ("Section 95", "E07000223", "Subsistence only", 5 + i),
                ("Section 95", "S12000033", "Dispersal Accommodation", 300 + i),
                ("Section 95", "W06000015", "Initial Accommodation", 40 + i)]
        for sup, code, acc, n in base:
            if (q, code, sup) in set(drop):
                continue
            n = (values or {}).get((q, code, sup), n)
            out.append([dtext(q), sup, REGION[code], NAMES[code], code, acc, n])
        if q in unallocated:
            out.append([dtext(q), "Section 95", NA_SUB, NA_SUB, NA_SUB,
                        "Subsistence Only", 7])
    for r in add:
        out.append(list(r))
    return out


def pivot_of(rows, dates):
    """{label: [sum per date]} of the data rows (by region), Grand Total."""
    out = {}
    tot = {}
    for r in rows:
        if r[0] not in dates:
            continue
        j = dates.index(r[0])
        if not isinstance(r[6], int):
            continue
        out.setdefault(r[2], [0] * len(dates))[j] += r[6]
        tot[j] = tot.get(j, 0) + r[6]
    out = dict(sorted(out.items()))
    out[m.GRAND_TOTAL] = [tot.get(j) for j in range(len(dates))]
    return out


def published_of(q):
    d = date.fromisoformat(q)
    y, mo = (d.year + 1, d.month - 10) if d.month > 10 else (d.year,
                                                             d.month + 2)
    return date(y, mo, 21)


def cover_rows(kind, q, *, published=None, next_update="26 November 2026",
               label=None, blank_first=False, home_office=True):
    d = date.fromisoformat(q)
    rows = [[None]] if blank_first else []
    rows += [[m.SERIES_LINE], [label or f"year ending {d:%B %Y}"], [PUB[kind]]]
    if home_office:
        rows.append(["Home Office"])
    pub = published or published_of(q)
    rows += [[f"Published: {pub.day} {pub:%B %Y}"],
             [f"Next update: {next_update}"],
             ["Responsible Statistician: A Person"]]
    return rows


def write_d11(path, quarters=Q3, *, rows=None, published=None,
              cover=None, contents_period=None, header=None, pivot=None,
              pivot_dates=None, pivot_filter="(All)", **kw):
    """An Asy_D11 workbook: Cover_sheet, Contents, Asy_D11 (the pivot,
    cached from the data unless pivot {(date text, label): v} overrides) and
    Data_Asy_D11 (title, header, rows)."""
    import openpyxl
    rows = rows if rows is not None else d11_rows(quarters, **kw)
    last = quarters[-1]
    d = date.fromisoformat(last)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cover_sheet"
    for r in (cover or cover_rows("d11", last, published=published)):
        ws.append(r)
    ws = wb.create_sheet("Contents")
    period = contents_period or f"2014 to {d.year} Q{d.month // 3}"
    ws.append([f"Immigration System Statistics, year ending {d:%B %Y}"])
    ws.append(["Asylum - Detailed Datasets"])
    ws.append(["Sheet", "Title", "Variables available", "Period covered",
               "Accredited Official Statistics", "Next planned update"])
    ws.append(["Asy_D11", "pivot table", "...", period, "Yes", "x"])
    ws.append(["Data - Asy_D11", "dataset", "...", period, "Yes", "x"])
    ws = wb.create_sheet("Asy_D11")
    dates = pivot_dates or [dtext(q) for q in quarters]
    ws.append(["Asy_D11: ... - pivot table"])
    ws.append(["Support Type", pivot_filter])
    ws.append(["Accommodation Type", "(All)"])
    ws.append([None])
    ws.append(["Sum of People", "Column Labels"])
    ws.append(["Row Labels"] + dates)
    for label, vals in pivot_of(rows, dates).items():
        vals = [(pivot or {}).get((dt, label), v) for dt, v in zip(dates,
                                                                  vals)]
        ws.append([label] + vals)
    ws = wb.create_sheet("Data_Asy_D11")
    ws.append(["Asy_D11: Asylum seekers ... - dataset"])
    ws.append(list(header or m.D11_HEADERS) + [None, None])
    for r in rows:
        ws.append(list(r) + [None, None])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_d09(path, quarters=Q3, *, rows=None, values=None, **kw):
    """An Asy_D09 workbook whose UK Region / Nation sums equal the fixture
    Asy_D11 rows (values {(date text, region): delta} shifts one)."""
    import openpyxl
    rows = rows if rows is not None else d11_rows(quarters, **kw)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cover_sheet"
    for r in cover_rows("d09", quarters[-1]):
        ws.append(r)
    ws = wb.create_sheet("Data_Asy_D09")
    ws.append(["Asy_D09: ... - dataset"])
    ws.append(list(m.D09_HEADERS))
    done = set()
    for r in rows:
        n = r[6]
        k = (r[0], r[2])
        if k in (values or {}) and k not in done:
            n += values[k]
            done.add(k)
        ws.append([r[0], "Afghanistan", "Asia Central", r[1], r[5], r[2], n])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


REG_LAS = ["E06000001", "E06000002", "BARNSLEY", "E07000223"]
SA_COL = {"Dispersal Accommodation": 7, "Initial Accommodation": 6,
          "Contingency Accommodation - Hotel": 8,
          "Contingency Accommodation - Other": 8, "Other Accommodation": 9,
          "Subsistence Only": 10}


def reg02_body(period, d11=None, *, values=None, star=("E07000223",),
               las=None):
    """Reg_02 body rows [la, region, code, 14 cells] for the fixture
    authorities: supported asylum from the fixture Asy_D11 rows of the same
    period (so the cross-check holds), Homes for Ukraine '*' in the star
    authorities, two non-England rows and the Unknown row. values {code:
    {cell index: v}} overrides (cell indexes as m.REG02_PATHWAYS)."""
    d11 = d11 if d11 is not None else d11_rows(Q4)
    out = []
    for idx, code in enumerate(las or REG_LAS):
        if code == "BARNSLEY":
            code = barnsley(period)
        cells = [10 * (idx + 1), 4, 0, 3, 1, 0, 0, 0, 0, 0, 0]
        for r in d11:
            if r[0] == dtext(period) and r[4] == code:
                acc = " ".join(w.capitalize() for w in r[5].split())
                cells[SA_COL[acc]] += r[6]
        cells[5] = sum(cells[6:11])
        if code in star:
            cells[0] = "*"
        over = (values or {}).get(code, {})
        for i, v in over.items():
            if i < 11:
                cells[i] = v
        tops = [cells[0], cells[1], cells[5]]
        allp = sum(v for v in tops if not isinstance(v, str))
        pop = 100000 + 1000 * idx
        row = cells + [allp, pop, allp / pop]
        for i, v in over.items():
            if i >= 11:
                row[i] = v
        out.append([NAMES.get(code, code), REGION.get(code, "x"), code] + row)
    out.append(["Aberdeen City", "Scotland", "S12000033", 368, 63, 0, 50, 13,
                718, 7, 599, 99, 0, 13, 1149, 230180, 0.0049917])
    out.append(["Unknown", "Unknown", "Unknown", 0, 7144, 1242, 2458, 0, 0, 0,
                0, 0, 0, 0, 7144, "-", "-"])
    return out


def write_reg02(path, period="2026-06-30", *, body=None, title=None,
                header=None, cover=None, published=None, **kw):
    """A Reg_02 workbook (.ods via pandas + odfpy, or .xlsx by the name)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    d = date.fromisoformat(period)
    cov = cover or cover_rows("reg02", period, published=published)
    rows = [[title or f"Reg_02: Immigration groups, by Local Authority, as at "
             f"{d.day} {d:%B %Y}"],
            list(header or m.REG02_HEADERS)]
    rows += body if body is not None else reg02_body(period, **kw)
    if path.suffix == ".xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cover_sheet"
        for r in cov:
            ws.append(r)
        ws = wb.create_sheet("Reg_02")
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path
    import pandas as pd
    width = max(len(r) for r in rows)
    with pd.ExcelWriter(path, engine="odf") as xw:
        pd.DataFrame(cov, dtype=object).to_excel(
            xw, sheet_name="Cover_sheet", header=False, index=False)
        pd.DataFrame([r + [None] * (width - len(r)) for r in rows],
                     dtype=object).to_excel(xw, sheet_name="Reg_02",
                                            header=False, index=False)
    return path


def tables_page(d11=("June 2026", "https://a.example/m1/support-la.xlsx"),
                d09=("June 2026", "https://a.example/m2/support.xlsx"),
                extra=(), title=None, history=("2026-08-27T08:30:04Z",
                                               "Updated tables.")):
    atts = []
    if d11:
        atts.append({"title": "Asylum seekers in receipt of Home Office "
                     "support by local authority detailed datasets, year "
                     f"ending {d11[0]}", "url": d11[1]})
    if d09:
        atts.append({"title": "Asylum seekers in receipt of Home Office "
                     f"support detailed datasets, year ending {d09[0]}",
                     "url": d09[1]})
    atts.append({"title": "Resettlement by local authority detailed "
                 "datasets, year ending June 2026", "url": "x/r.xlsx"})
    atts += [{"title": t, "url": u} for t, u in extra]
    return {"title": title or m.TABLES_TITLE,
            "public_updated_at": "2026-08-27T09:30:04+01:00",
            "details": {"attachments": atts,
                        "change_history": [{"public_timestamp": history[0],
                                            "note": history[1]}]}}


def regional_page(releases, title=None):
    """releases: [(Month YYYY, url)]."""
    return {"title": title or m.REGIONAL_TITLE,
            "public_updated_at": "2026-08-27T09:30:07+01:00",
            "details": {"attachments": [
                {"title": "Regional and local authority data on immigration "
                          f"groups, year ending {ye}", "url": u}
                for ye, u in releases],
                "change_history": [{"public_timestamp": "2026-08-27T08:30:07Z",
                                    "note": "Added."}]}}


def resolver(code):
    return {"E08000038": "E08000016", "E07000163": "E06000065",
            "E07000164": "E06000065"}.get(code, code)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0

    def path(self, name):
        self.n += 1
        return self.root / f"f{self.n}" / name


# ---------------------------------------------------------------------------


class Discovery(unittest.TestCase):

    def test_the_d11_and_d09_attachments(self):
        a = m.asy_d11_attachment(tables_page())
        self.assertEqual(a["year_ending"], date(2026, 6, 30))
        self.assertEqual(a["label"], "year ending June 2026")
        self.assertEqual(m.asy_d09_attachment(tables_page())["url"],
                         "https://a.example/m2/support.xlsx")

    def test_renamed_missing_doubled_and_retitled_raise_listing_titles(self):
        renamed = tables_page(d11=None, extra=[(
            "Asylum seekers in receipt of support by local authority detailed "
            "datasets, year ending September 2026", "x/s.xlsx")])
        doubled = tables_page(extra=[(
            "Asylum seekers in receipt of Home Office support by local "
            "authority detailed datasets, year ending March 2026",
            "x/old.xlsx")])
        for page, needle in ((renamed, "do not fit"),
                             (tables_page(d11=None), "0 attachments"),
                             (doubled, "2 attachments"),
                             (tables_page(title="Immigration statistics"),
                              "page title")):
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as e:
                    m.asy_d11_attachment(page)
                self.assertIn(needle, str(e.exception))
                self.assertIn("Resettlement by local authority", str(e.exception))

    def test_a_non_xlsx_attachment_raises(self):
        with self.assertRaises(ValueError) as e:
            m.asy_d11_attachment(tables_page(d11=("June 2026", "x/a.ods")))
        self.assertIn("not an .xlsx", str(e.exception))

    def test_reg02_newest_across_formats(self):
        page = regional_page([("December 2024", "x/dec-2024.ods"),
                              ("March 2025", "x/mar-2025.xlsx")])
        self.assertEqual(m.newest_reg02(page)["url"], "x/mar-2025.xlsx")
        rel = m.reg02_releases(page)
        self.assertEqual(sorted(rel), [date(2024, 12, 31), date(2025, 3, 31)])

    def test_reg02_two_files_for_one_quarter_and_odd_titles_raise(self):
        for page, needle in (
                (regional_page([("March 2025", "x/a.ods"),
                                ("March 2025", "x/b.xlsx")]), "two attachments"),
                (regional_page([("March 2025", "x/a.csv")]), "not an .ods"),
                (regional_page([]), "no attachment"),
                (regional_page([("March 2025", "x/a.ods")], title="Other"),
                 "page title")):
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as e:
                    m.reg02_releases(page)
                self.assertIn(needle, str(e.exception))
        page = regional_page([("March 2025", "x/a.ods")])
        page["details"]["attachments"].append(
            {"title": "Immigration groups by local authority, Sep 2025",
             "url": "x/b.ods"})
        with self.assertRaises(ValueError) as e:
            m.reg02_releases(page)
        self.assertIn("Sep 2025", str(e.exception))

    def test_discovery_note_and_the_overdue_warning(self):
        seen = {"Asy_D11": "2026-06-30", "Reg_02": "2026-06-30"}
        held = {"Asy_D11": ["2026-03-31", "2026-06-30"],
                "Reg_02": ["2026-06-30"]}
        nu = date(2026, 11, 26)
        lines = m.discovery_note(seen, held, nu, today=date(2026, 11, 26))
        self.assertEqual(len(lines), 2)
        self.assertIn("newest Asy_D11 release the page lists: year ending "
                      "June 2026", lines[0])
        self.assertIn("already held", lines[0])
        lines = m.discovery_note(seen, held, nu, today=date(2026, 11, 27))
        self.assertIn("WARNING", lines[-1])
        self.assertIn("26 November 2026", lines[-1])
        held["Reg_02"] = ["2026-03-31"]
        lines = m.discovery_note(seen, held, nu, today=date(2026, 12, 1))
        self.assertFalse(any("WARNING" in x for x in lines))
        self.assertIn("not held yet", lines[1])
        # the fallback date is the module constant
        held["Reg_02"] = ["2026-06-30"]
        with mock.patch.object(m, "EXPECTED_NEXT_UPDATE", date(2026, 12, 1)):
            self.assertFalse(any("WARNING" in x for x in m.discovery_note(
                seen, held, None, today=date(2026, 11, 30))))
            self.assertTrue(any("WARNING" in x for x in m.discovery_note(
                seen, held, None, today=date(2026, 12, 2))))


class Cover(Tmp):

    def test_cover_and_rank(self):
        p = write_d11(self.path("a.xlsx"), Q4)
        cov = m.read_cover(p)
        self.assertEqual((cov["kind"], cov["label"], cov["year_ending"]),
                         ("d11", "year ending June 2026", date(2026, 6, 30)))
        self.assertEqual(cov["next_update"], date(2026, 11, 26))
        self.assertEqual(cov["rank"], (date(2026, 6, 30), date(2026, 8, 21)))
        self.assertEqual(m.release_rank(cov), cov["rank"])

    def test_blank_leading_row_and_no_home_office_line(self):
        rows = cover_rows("reg02", "2024-03-31", blank_first=True,
                          home_office=False)
        cov = m.parse_cover(rows)
        self.assertEqual(cov["kind"], "reg02")
        self.assertFalse(cov["home_office"])

    def test_bad_covers_raise(self):
        for rows, needle in (
                ([["Other"], ["year ending June 2026"], [PUB["d11"]]],
                 "first line"),
                (cover_rows("d11", "2026-06-30", label="year ending May 2026"),
                 "calendar quarter"),
                ([[m.SERIES_LINE], ["year ending June 2026"], ["Other title"],
                  ["Published: 1 June 2026"], ["Next update: x"]],
                 "publication title"),
                (cover_rows("d11", "2026-06-30")[:4], "Published")):
            with self.subTest(needle=needle):
                with self.assertRaises(ValueError) as e:
                    m.parse_cover(rows)
                self.assertIn(needle, str(e.exception))

    def test_rank_from_the_cover_never_the_name_or_media_id(self):
        # a file named for March 2026 under a June media id says March
        p = write_d11(self.path("support-local-authority-datasets-jun-2026"
                                ".xlsx"), Q3)
        self.assertEqual(m.read_cover(p)["rank"][0], date(2026, 3, 31))
        self.assertIsNone(m.release_rank(
            "https://assets/media/6a85c5e654bcee010d514574/"
            "support-local-authority-datasets-jun-2026.xlsx"))
        self.assertEqual(m.release_rank("x.xlsx; year ending June 2026; "
                                        "published 2026-08-27; note"),
                         (date(2026, 6, 30), date(2026, 8, 27)))


class ReadD11(Tmp):

    def test_reads_a_good_file(self):
        p = write_d11(self.path("a.xlsx"))
        f = m.read_d11(p)
        self.assertEqual(f["period"], date(2026, 3, 31))
        self.assertEqual(len(f["rows"]), len(d11_rows()))
        self.assertEqual(f["pivot"]["dates"][-1], date(2026, 3, 31))
        self.assertEqual(m.reconcile_d11(f, m.build_d11_records(
            f, resolver)[0]), [])

    def test_identity_mismatches_raise(self):
        cases = (
            ({"cover": cover_rows("d11", "2026-06-30"),
              "contents_period": "2014 to 2026 Q2"}, "latest date"),
            ({"contents_period": "2014 to 2025 Q4"}, "period covered"),
            ({"cover": cover_rows("reg02", "2026-03-31")}, "the cover says"),
            ({"pivot_dates": [dtext(q) for q in Q3[:2]]}, "pivot's last"),
            ({"pivot_filter": "Section 95"}, "filter"),
        )
        for kw, needle in cases:
            with self.subTest(needle=needle):
                p = write_d11(self.path("a.xlsx"), **kw)
                with self.assertRaises(ValueError) as e:
                    m.read_d11(p)
                self.assertIn(needle, str(e.exception))

    def test_a_whole_year_period_covered_is_the_december_quarter(self):
        qs = ["2025-06-30", "2025-09-30", "2025-12-31"]
        f = m.read_d11(write_d11(self.path("a.xlsx"), qs,
                                 contents_period="2014 to 2025"))
        self.assertEqual(set(f["contents"].values()), {date(2025, 12, 31)})
        p = write_d11(self.path("a.xlsx"), qs, contents_period="2014 to 2024")
        with self.assertRaises(ValueError) as e:
            m.read_d11(p)
        self.assertIn("period covered", str(e.exception))

    def test_page_and_release_years_must_equal_the_cover(self):
        cov = m.read_cover(write_d11(self.path("a.xlsx")))
        att = m.asy_d11_attachment(tables_page())
        self.assertIn("the page attachment says year ending June 2026",
                      m.identity_problems(cov, att)[0])
        self.assertIn("--release year ending June 2026",
                      m.identity_problems(cov, None, date(2026, 6, 30))[0])
        self.assertEqual(m.identity_problems(cov, None, date(2026, 3, 31)),
                         [])

    def test_unknown_and_missing_headers_are_named(self):
        h = list(m.D11_HEADERS)
        h[4] = "LA Code"
        p = write_d11(self.path("a.xlsx"), header=h)
        with self.assertRaises(ValueError) as e:
            m.read_d11(p)
        self.assertIn("unknown ['LA Code']", str(e.exception))
        self.assertIn("missing ['LAD Code']", str(e.exception))

    def test_people_cells_halt(self):
        for v, needle in ((None, "blank"), (0, "People 0"), ("12", "not a "
                          "whole"), (-3, "negative"), (2.5, "non-integer"),
                          ("*", "not a whole")):
            with self.subTest(v=v):
                rows = d11_rows()
                rows[3][6] = v
                p = write_d11(self.path("a.xlsx"), rows=rows)
                with self.assertRaises(ValueError) as e:
                    m.read_d11(p)
                self.assertIn("row 6 People", str(e.exception))
                self.assertIn(needle, str(e.exception))
        self.assertEqual(m.people_cell(7.0, "x"), 7)
        with self.assertRaises(ValueError):
            m.people_cell(True, "x")

    def test_data_after_an_empty_row_halts(self):
        rows = d11_rows()
        rows.insert(4, [None] * 7)
        p = write_d11(self.path("a.xlsx"), rows=rows)
        with self.assertRaises(ValueError) as e:
            m.read_d11(p)
        self.assertIn("after 1 empty row", str(e.exception))


class Records(Tmp):

    def build(self, rows, resolve=resolver):
        p = write_d11(self.path("a.xlsx"), rows=rows)
        return m.build_d11_records(m.read_d11(p), resolve)

    def test_the_three_tables(self):
        by, merges, dups = self.build(d11_rows())
        self.assertEqual(sorted(by), Q3)         # the pre-2018 row left out
        q = by["2025-09-30"]
        self.assertEqual(len(q["support"]), D11_ROWS_PER_Q)
        self.assertEqual(len(q["non_england"]), NE_ROWS_PER_Q)
        self.assertEqual(q["unallocated"], [{
            "support_type": "Section 95", "accommodation_type":
            "Subsistence Only", "na_reason": NA_SUB,
            "period_ending": "2025-09-30", "people": 7}])
        self.assertEqual(by["2026-03-31"]["unallocated"], [])
        adur = [r for r in q["support"] if r["lad24cd"] == "E07000223"][0]
        self.assertEqual(adur["accommodation_type"], "Subsistence Only")
        ne = {r["lad_code"]: r for r in q["non_england"]}
        self.assertEqual(ne["S12000033"]["country"], "Scotland")
        self.assertEqual(ne["W06000015"]["country"], "Wales")
        bq = {r["lad24cd"] for r in by["2025-12-31"]["support"]}
        self.assertIn("E08000016", bq)           # E08000038 resolved
        self.assertEqual((merges, dups), ([], []))

    def test_unknown_types_and_codes_halt(self):
        for col, v, needle in ((1, "Section 99", "support type"),
                               (5, "Tent", "accommodation type"),
                               (4, "X99000001", "not an E06-E09")):
            with self.subTest(needle=needle):
                rows = d11_rows()
                rows[2][col] = v
                with self.assertRaises(ValueError) as e:
                    self.build(rows)
                self.assertIn(needle, str(e.exception))

    def test_na_rows_unknown_and_verbatim_reasons(self):
        q = dtext("2025-09-30")
        add = [[q, "Section 98", "N/A - Section 98 (pre-Dec 2022)",
                "N/A - Section 98 (pre-Dec 2022)",
                "N/A - Section 98 (pre-Dec 2022)",
                "N/A - Section 98 (pre-2023)", 11],
               [q, "Section 95", "Unknown", "Unknown", "N/A",
                "Dispersal Accommodation", 3]]
        by, _, _ = self.build(d11_rows(add=add))
        un = {(r["support_type"], r["accommodation_type"], r["na_reason"]):
              r["people"] for r in by["2025-09-30"]["unallocated"]}
        self.assertEqual(un[("Section 98", "not_stated",
                             "N/A - Section 98 (pre-Dec 2022)")], 11)
        self.assertEqual(un[("Section 95", "Dispersal Accommodation",
                             "Unknown")], 3)

    def test_merges_duplicates_and_the_last_name(self):
        q = dtext("2022-12-31")
        rows = d11_rows(["2022-12-31"] + Q3, add=[
            [q, "Section 95", "Yorkshire and The Humber", "Craven",
             "E07000163", "Dispersal Accommodation", 6],
            [q, "Section 95", "Yorkshire and The Humber", "Hambleton",
             "E07000164", "Dispersal Accommodation", 4],
            [q, "Section 95", "West Midlands", "Middlesbrough (old name)",
             "E06000002", "Dispersal Accommodation", 9]])
        by, merges, dups = self.build(rows)
        sup = {(r["lad24cd"], r["support_type"], r["accommodation_type"]): r
               for r in by["2022-12-31"]["support"]}
        ny = sup[("E06000065", "Section 95", "Dispersal Accommodation")]
        self.assertEqual((ny["people"], ny["published_la_name"]),
                         (10, "Hambleton"))
        self.assertEqual([x["codes"] for x in merges],
                         [["E07000163", "E07000164"]])
        mb = sup[("E06000002", "Section 95", "Dispersal Accommodation")]
        self.assertEqual((mb["people"], mb["published_la_name"]),
                         (200 + 9, "Middlesbrough (old name)"))
        self.assertEqual([(d["key"], d["people"]) for d in dups], [
            (("E06000002", "Section 95", "Dispersal Accommodation"),
             [200, 9])])

    def test_the_name_never_depends_on_row_order(self):
        import random
        q = dtext("2022-12-31")
        rows = d11_rows(["2022-12-31"] + Q3, add=[
            [q, "Section 95", "Yorkshire and The Humber", "Hambleton",
             "E07000164", "Dispersal Accommodation", 4],
            [q, "Section 95", "Yorkshire and The Humber", "Craven",
             "E07000163", "Dispersal Accommodation", 6],
            [q, "Section 95", "West Midlands", "Middlesbrough (old name)",
             "E06000002", "Dispersal Accommodation", 9]])
        f = m.read_d11(write_d11(self.path("a.xlsx"), rows=rows))
        want = m.build_d11_records(f, resolver)[0]
        sup = {(r["lad24cd"], r["support_type"], r["accommodation_type"]): r
               for r in want["2022-12-31"]["support"]}
        # the highest code wins although it is listed first
        self.assertEqual(sup[("E06000065", "Section 95",
                              "Dispersal Accommodation")]["published_la_name"],
                         "Hambleton")
        for seed in range(5):
            shuffled = dict(f, rows=list(f["rows"]))
            random.Random(seed).shuffle(shuffled["rows"])
            self.assertEqual(m.build_d11_records(shuffled, resolver)[0], want)
            shuffled["rows"].reverse()
            self.assertEqual(m.build_d11_records(shuffled, resolver)[0], want)

    def test_six_same_code_duplicates_halt(self):
        add = []
        for q in ["2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
                  "2023-03-31", "2023-06-30"]:
            for n in (1, 2):
                add.append([dtext(q), "Section 95", "North East", "Hartlepool",
                            "E06000001", "Dispersal Accommodation", n])
        rows = d11_rows(add=add)
        p = write_d11(self.path("a.xlsx"), rows=rows,
                      pivot_dates=[dtext(q) for q in Q3])
        f = m.read_d11(p)
        with self.assertRaises(ValueError) as e:
            m.build_d11_records(f, resolver)
        self.assertIn("6 keys published twice", str(e.exception))
        with mock.patch.object(m, "DUPLICATE_HALT", 6):
            _, _, dups = m.build_d11_records(f, resolver)
        self.assertEqual(len(dups), 6)


RAW = Path(__file__).resolve().parents[2] / "data" / "raw" / "s6_asylum"
# la_code_lookup's single-target new_unitary rows the held Asy_D11 files use
# (2018 onward), for a resolver that needs no database
UNITARY = {"E07000027": "E06000064", "E07000028": "E06000063",
           "E07000029": "E06000063", "E07000030": "E06000064",
           "E07000031": "E06000064", "E07000163": "E06000065",
           "E07000164": "E06000065", "E07000165": "E06000065",
           "E07000168": "E06000065", "E07000169": "E06000065",
           "E07000187": "E06000066", "E07000188": "E06000066",
           "E07000189": "E06000066", "E07000246": "E06000066"}


def real_resolver(code):
    import geography
    code = geography.canonical(code, geography.RECODES_FALLBACK)
    return UNITARY.get(code, code)


@unittest.skipUnless((RAW / "support-local-authority-datasets-jun-2026.xlsx")
                     .is_file() and (RAW / "support-local-authority-datasets-"
                                     "dec-2025.xlsx").is_file(),
                     "the held Asy_D11 files are not on disk")
class RealRowOrder(unittest.TestCase):
    """Read-only, on the held files: the records do not depend on row
    order, and the December 2025 file (which lists the four 2022-2023
    Somerset merges in another order) gives the June 2026 file's names."""

    def test_shuffled_rows_and_the_december_file(self):
        import random
        june = m.read_d11(RAW / "support-local-authority-datasets-jun-2026"
                          ".xlsx")
        want = m.build_d11_records(june, real_resolver)[0]
        shuffled = dict(june, rows=list(june["rows"]))
        random.Random(6).shuffle(shuffled["rows"])
        self.assertEqual(m.build_d11_records(shuffled, real_resolver)[0], want)
        dec = m.read_d11(RAW / "support-local-authority-datasets-dec-2025"
                         ".xlsx")
        got = m.build_d11_records(dec, real_resolver)[0]
        shared = sorted(set(got) & set(want))
        self.assertEqual(len(shared), 32)
        for p in shared:
            for tag in m.D11_TAGS:
                self.assertEqual(got[p][tag], want[p][tag], (p, tag))
        som = [r for r in want["2022-12-31"]["support"]
               if r["lad24cd"] == "E06000066" and r["support_type"] ==
               "Section 95" and r["accommodation_type"] ==
               "Dispersal Accommodation"]
        self.assertEqual([r["published_la_name"] for r in som],
                         ["Somerset West and Taunton"])


class Reconcile(Tmp):

    def test_one_data_cell_off_against_the_pivot_halts(self):
        p = write_d11(self.path("a.xlsx"),
                      pivot={(dtext("2026-03-31"), "North East"): 323})
        f = m.read_d11(p)
        bad = m.reconcile_d11(f, m.build_d11_records(f, resolver)[0])
        self.assertEqual(len(bad), 1)
        self.assertIn("2026-03-31 North East: the pivot says 323", bad[0])
        p = write_d11(self.path("a.xlsx"),
                      pivot={(dtext("2026-03-31"), m.GRAND_TOTAL): 1})
        f = m.read_d11(p)
        self.assertIn("Grand Total", m.reconcile_d11(
            f, m.build_d11_records(f, resolver)[0])[0])

    def test_england_off_against_asy_d09_halts(self):
        f = m.read_d11(write_d11(self.path("a.xlsx")))
        by = m.build_d11_records(f, resolver)[0]
        d9 = m.read_d09(write_d09(self.path("b.xlsx")))
        self.assertEqual(m.reconcile_d09(d9, by, f["cover"]), [])
        d9 = m.read_d09(write_d09(self.path("b.xlsx"), values={
            (dtext("2025-12-31"), "North East"): 1}))
        bad = m.reconcile_d09(d9, by, f["cover"])
        self.assertEqual(len(bad), 1)
        self.assertIn("2025-12-31: England", bad[0])
        d9 = m.read_d09(write_d09(self.path("b.xlsx"), values={
            (dtext("2025-09-30"), NA_SUB): 1}))
        self.assertIn("unallocated", m.reconcile_d09(d9, by)[0])
        d9 = m.read_d09(write_d09(self.path("b.xlsx"), Q4))
        self.assertIn("Asy_D09 is year ending June 2026",
                      m.reconcile_d09(d9, by, f["cover"])[0])

    def reg(self, **kw):
        f = m.read_reg02(write_reg02(self.path("r.ods"), **kw))
        return f, m.build_reg02_records(f, resolver)[1]

    def test_reg02_against_asy_d11_and_its_authority_count(self):
        d11 = d11_rows(Q4)
        tot = m.d11_la_totals([
            {"lad24cd": resolver(r[4]), "people": r[6]} for r in d11
            if r[0] == dtext("2026-06-30") and r[4][:3] in
            m.ENGLISH_PREFIXES])
        with mock.patch.object(m, "REG02_LAS", 4):
            f, recs = self.reg()
            self.assertEqual(m.reconcile_reg02(f, recs, tot), [])
            self.assertEqual(m.reconcile_reg02(f, recs, None), [])
            off = dict(tot, E06000001=tot["E06000001"] + 1)
            bad = m.reconcile_reg02(f, recs, off)
            self.assertEqual(len(bad), 1)
            self.assertIn("E06000001: Reg_02 supported_asylum", bad[0])
            f, recs = self.reg(las=REG_LAS[:3])
            self.assertIn("3 English authorities, expected 4",
                          m.reconcile_reg02(f, recs, None)[0])
        f, recs = self.reg()
        self.assertIn("expected 296", m.reconcile_reg02(f, recs, None)[0])

    def test_reg02_internal_sums(self):
        with mock.patch.object(m, "REG02_LAS", 4):
            f, recs = self.reg(values={"E06000001": {2: 5}})
            self.assertIn("afghan_resettlement: total 4, its parts sum to 9",
                          m.reconcile_reg02(f, recs, None)[0])
            f, recs = self.reg(values={"E06000002": {11: 1}})
            self.assertIn("all_pathways", m.reconcile_reg02(f, recs, None)[0])


class ReadReg02(Tmp):

    def test_markers_zeros_lower_bound_and_percentage(self):
        p = write_reg02(self.path("r.ods"))
        f = m.read_reg02(p)
        self.assertEqual(f["period"], date(2026, 6, 30))
        period, recs = m.build_reg02_records(f, resolver)
        self.assertEqual(period, "2026-06-30")
        self.assertEqual(len(recs), 4 * 12)
        by = {(r["lad24cd"], r["pathway"], r["sub_pathway"]): r for r in recs}
        hfu = by[("E07000223", "homes_for_ukraine", "total")]
        self.assertEqual((hfu["people"], hfu["suppressed"],
                          hfu["source_marker"]), (None, True, "*"))
        zero = by[("E06000001", "afghan_resettlement", "transitional")]
        self.assertEqual((zero["people"], zero["suppressed"],
                          zero["source_marker"]), (0, False, None))
        top = by[("E07000223", "all_pathways", "total")]
        # the old build's text and rounding, on the same cells
        old_marker = (
            "LOWER BOUND: published total excludes the suppressed "
            + ", ".join(["homes_for_ukraine"])
            + " pathway (fewer than 5 people, disclosure control). "
              "True total is higher by between 1 and 4 per "
              "suppressed pathway.")
        self.assertEqual(top["source_marker"], old_marker)
        cells = reg02_body("2026-06-30")[3]
        old_pct = round(float(str(cells[16]).strip().replace("%", "")), 4)
        self.assertEqual(top["percentage_of_population"],
                         Decimal(repr(old_pct)).quantize(Decimal("0.0001")))
        self.assertEqual(top["population"], cells[15])
        self.assertIsNone(hfu["percentage_of_population"])
        self.assertEqual(hfu["population"], cells[15])
        self.assertEqual(by[("E06000001", "all_pathways", "total")]
                         ["source_marker"], None)
        self.assertEqual(m.reg02_counts(f), {
            "england": 4, "scotland": 1, "wales": 0, "northern_ireland": 0,
            "unknown": 1})

    def test_xlsx_with_a_trailing_space_header(self):
        h = list(m.REG02_HEADERS)
        h[15] = "Population "
        p = write_reg02(self.path("r.xlsx"), header=h)
        self.assertEqual(len(m.build_reg02_records(m.read_reg02(p),
                                                   resolver)[1]), 48)

    def test_blank_dash_and_text_in_a_pathway_cell_halt(self):
        for v, needle in ((None, "blank"), ("-", "'-'"), (":", "':'"),
                          ("x", "'x'"), (-1, "negative"), (1.5, "whole")):
            with self.subTest(v=v):
                p = write_reg02(self.path("r.ods"),
                                values={"E06000002": {3: v}})
                f = m.read_reg02(p)
                with self.assertRaises(ValueError) as e:
                    m.build_reg02_records(f, resolver)
                self.assertIn("settled in LA housing", str(e.exception))
                self.assertIn(needle, str(e.exception))

    def test_title_date_and_headers(self):
        p = write_reg02(self.path("r.ods"), title="Reg_02: Immigration groups"
                        ", by Local Authority, as at 31 March 2026")
        with self.assertRaises(ValueError) as e:
            m.read_reg02(p)
        self.assertIn("the cover says year ending June 2026", str(e.exception))
        h = list(m.REG02_HEADERS)
        h.insert(5, "of which, Afghan Resettlement Programme - interim "
                    "(population)")
        p = write_reg02(self.path("r.ods"), header=h)
        with self.assertRaises(ValueError) as e:
            m.read_reg02(p)
        self.assertIn("interim", str(e.exception))


class FakeCur:
    """Answers the queries geography.resolve, resolve_codes and
    load_checks.check_codes make."""

    def __init__(self, lookup, boundaries):
        self.lookup = lookup          # [(old, new, change_type)]
        self.boundaries = set(boundaries)
        self.rows = []

    def execute(self, sql, args=None):
        if "change_type = 'recode'" in sql:
            self.rows = [(o, n) for o, n, t in self.lookup if t == "recode"]
        elif "change_type = 'new_unitary'" in sql:
            self.rows = [(o, n) for o, n, t in self.lookup
                         if t == "new_unitary"]
        elif "FROM public.la_code_lookup" in sql:
            want = set(args[0])
            self.rows = [(o, n) for o, n, t in self.lookup if o in want]
        elif "lad24cd = ANY" in sql:
            self.rows = [(c,) for c in args[0] if c in self.boundaries]
        elif "FROM public.la_boundaries" in sql:
            self.rows = [(c,) for c in sorted(self.boundaries)]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return list(self.rows)


LOOKUP = [("E08000038", "E08000016", "recode"),
          ("E08000039", "E08000019", "recode"),
          ("E07000163", "E06000065", "new_unitary"),
          ("E07000164", "E06000065", "new_unitary"),
          ("E06000028", "E06000058", "merger")]
BOUNDS = ["E06000001", "E06000002", "E08000016", "E08000019", "E06000065",
          "E06000058", "E07000223"]


class Geography(unittest.TestCase):

    def test_resolution(self):
        cur = FakeCur(LOOKUP, BOUNDS)
        rmap, bad = m.resolve_codes(cur, set(), {
            "2025-09-30": {"E08000016", "E07000163"},
            "2025-12-31": {"E08000038", "E06000001"}})
        self.assertEqual(bad, [])
        self.assertEqual(rmap["E08000038"], "E08000016")
        self.assertEqual(rmap["E07000163"], "E06000065")

    def test_both_forms_in_one_period_halt(self):
        _, bad = m.resolve_codes(FakeCur(LOOKUP, BOUNDS), set(), {
            "2025-12-31": {"E08000016", "E08000038"}})
        self.assertTrue(any("both E08000016 and E08000038" in x for x in bad),
                        bad)

    def test_an_unknown_code_and_a_merger_halt(self):
        _, bad = m.resolve_codes(FakeCur(LOOKUP, BOUNDS), set(), {
            "2025-12-31": {"E07000999"}, "2018-03-31": {"E06000028"}})
        self.assertTrue(any("UNEXPLAINED E07000999" in x for x in bad), bad)
        self.assertTrue(any("UNEXPLAINED E06000028" in x for x in bad), bad)

    def test_new_unitary_with_two_targets_halts(self):
        lk = LOOKUP + [("E07000163", "E06000001", "new_unitary")]
        _, bad = m.resolve_codes(FakeCur(lk, BOUNDS), set(), {
            "2022-12-31": {"E07000163"}})
        self.assertTrue(any("2 targets" in x for x in bad), bad)

    def test_check_forms_passes_under_mixed(self):
        import geography
        self.assertEqual(geography.DATASET_FORM["6"][0], "mixed")
        self.assertEqual(geography.check_forms("6", set(), by_period={
            "2025-09-30": {"E08000016", "E08000019"},
            "2025-12-31": {"E08000038", "E08000039"}}), [])


class Ledger(unittest.TestCase):
    J = (date(2026, 6, 30), date(2026, 8, 27))
    M = (date(2026, 3, 31), date(2026, 5, 21))

    def cover(self, rank=None):
        r = rank or self.J
        return {"label": m.ye_text(r[0]), "published": r[1],
                "year_ending": r[0], "next_update": date(2026, 11, 26)}

    def test_ranges_round_trip(self):
        ps = ["2018-03-31", "2018-06-30", "2018-09-30", "2019-03-31"]
        self.assertEqual(m.ranges_text(ps),
                         "2018-03-31..2018-09-30,2019-03-31")
        self.assertEqual(m.parse_ranges(m.ranges_text(ps)), ps)
        self.assertEqual(m.parse_ranges("none"), [])

    def test_ledger_source_round_trip(self):
        s = m.ledger_source("https://x/a.xlsx", self.cover(), {
            "support": ["2026-03-31", "2026-06-30"], "unallocated": [],
            "non_england": ["2026-06-30"]})
        where, rank, nu, tags = m.parse_ledger_source(s)
        self.assertEqual((where, rank, nu), ("https://x/a.xlsx", self.J,
                                             date(2026, 11, 26)))
        self.assertEqual(tags, {"support": ["2026-03-31", "2026-06-30"],
                                "unallocated": [],
                                "non_england": ["2026-06-30"]})
        self.assertEqual(m.release_rank(s), self.J)

    def test_ledger_complete_needs_every_period_in_every_table(self):
        src = m.ledger_source("u", self.cover(), {
            "support": ["2026-03-31", "2026-06-30"],
            "non_england": ["2026-03-31", "2026-06-30"]})
        full = {"support": {"2026-03-31": {(src, "s")},
                            "2026-06-30": {(src, "s")}},
                "non_england": {"2026-03-31": {(src, "s")},
                                "2026-06-30": {(src, "s")}}}
        ranks = {"2026-03-31": self.J, "2026-06-30": self.J}
        self.assertEqual(m.ledger_complete(full, "u", "s", ranks),
                         ["2026-03-31", "2026-06-30"])
        part = {"support": full["support"],
                "non_england": {"2026-03-31": {(src, "s")}}}
        self.assertIsNone(m.ledger_complete(part, "u", "s", ranks))
        self.assertIsNone(m.ledger_complete(full, "u", "other", ranks))
        self.assertIsNone(m.ledger_complete(full, "u", "s", ranks,
                                            stranded=["2026-06-30"]))

    def test_plan_periods(self):
        ranks = {"2026-03-31": self.J}
        new, held, skipped = m.plan_periods(
            ["2026-03-31", "2026-06-30"], self.M, ranks, {}, "s", "x",
            recheck=None, allow_older=False)
        self.assertEqual((new, held), (["2026-06-30"], []))
        self.assertIn("older", skipped["2026-03-31"])
        _, held, _ = m.plan_periods(["2026-03-31"], self.M, ranks, {}, "s",
                                    "x", recheck=None, allow_older=True)
        self.assertEqual(held, ["2026-03-31"])
        chk = {"support": {"2026-03-31": {("s", "x")}}}
        _, held, skipped = m.plan_periods(
            ["2026-03-31"], self.J, ranks, chk, "s", "x", recheck=None,
            allow_older=False, needs={"2026-03-31": ["support"]})
        self.assertEqual(skipped, {"2026-03-31": "checked: this file is "
                                                 "already in the ledger"})
        _, held, _ = m.plan_periods(
            ["2026-03-31"], self.J, ranks, chk, "s", "x", recheck=None,
            allow_older=False, needs={"2026-03-31": ["support", "non_england"]})
        self.assertEqual(held, ["2026-03-31"])
        _, held, _ = m.plan_periods(
            ["2026-03-31"], self.J, ranks, chk, "s", "x",
            recheck="2026-03-31", allow_older=False,
            needs={"2026-03-31": ["support"]})
        self.assertEqual(held, ["2026-03-31"])

    def test_rows_content_sha_ignores_provenance(self):
        r = {"lad24cd": "E06000001", "support_type": "Section 95",
             "accommodation_type": "Dispersal Accommodation", "people": 3,
             "published_la_name": "Hartlepool", "source_marker": None}
        a = m.rows_content_sha(m.SPEC_SUPPORT, [r])
        b = m.rows_content_sha(m.SPEC_SUPPORT, [dict(r, source_edition="x",
                                                     loaded_at="y")])
        self.assertEqual(a, b)
        self.assertNotEqual(a, m.rows_content_sha(m.SPEC_SUPPORT,
                                                  [dict(r, people=4)]))
        self.assertNotEqual(m.rows_content_sha(m.SPEC_SUPPORT, [dict(
            r, source_marker="")]), a)

    def test_period_ranks_take_the_newest(self):
        tips = {"support": {"2026-03-31": {"rank": self.M}}}
        chk = {"support": {"2026-03-31": {(m.ledger_source(
            "u", self.cover(), {"support": ["2026-03-31"]}), "s")}}}
        self.assertEqual(m.period_ranks(tips, chk), {"2026-03-31": self.J})


def srec(code, n, sup="Section 95", acc="Dispersal Accommodation"):
    return {"lad24cd": code, "support_type": sup, "accommodation_type": acc,
            "people": n, "published_la_name": code, "source_marker": None}


def grec(code, pw="supported_asylum", sub="total", n=10):
    return {"lad24cd": code, "pathway": pw, "sub_pathway": sub, "people": n,
            "suppressed": n is None, "source_marker": None if n is not None
            else "*", "population": 1, "percentage_of_population": None,
            "published_la_name": code}


class StopConditions(unittest.TestCase):

    def areas(self, n, base=1000, start=0):
        return [srec(f"E06{i:06d}", base) for i in range(start, start + n)]

    def check(self, new, tip, prev, kind, table="support", hit=True,
              needle=""):
        out = m.period_problems(new, tip, prev, table=table, kind=kind)
        if hit:
            self.assertTrue(any(needle in x for x in out), out)
            self.assertEqual(m.period_problems(
                new, tip, prev, table=table, kind=kind, acknowledged=True)
                if not needle.startswith("PARTIAL") else [], [])
        else:
            self.assertEqual(out, [])

    def test_new_period_thresholds_both_sides(self):
        prev = self.areas(100)
        # England total: 15% passes, 16% halts (spread so no area moves 1500)
        up15 = self.areas(100, 1150)
        up16 = self.areas(100, 1160)
        self.check(up15, None, prev, "new", hit=False)
        self.check(up16, None, prev, "new", needle="England total")
        # authority count: +30 passes, +31 halts (new areas of 1 person)
        p30 = prev + [srec(f"E07{i:06d}", 1) for i in range(30)]
        p31 = prev + [srec(f"E07{i:06d}", 1) for i in range(31)]
        self.check(p30, None, prev, "new", hit=False)
        self.check(p31, None, prev, "new", needle="authorities 100 -> 131")
        # one authority by 1500 passes, 1501 halts
        big = self.areas(400, 10000)
        ok = [dict(r) for r in big]
        ok[0]["people"] += 1500
        bad = [dict(r) for r in big]
        bad[0]["people"] += 1501
        self.check(ok, None, big, "new", hit=False)
        self.check(bad, None, big, "new", needle="more than 1,500 people")
        self.assertEqual(m.period_problems(bad, None, None, table="support",
                                           kind="new"), [])

    def test_revised_thresholds_both_sides(self):
        tip = self.areas(100)
        two = [dict(r, people=r["people"] + (20 if i < 100 else 0))
               for i, r in enumerate(tip)]           # +2.0%
        over = [dict(r, people=r["people"] + 21) for r in tip]
        self.check(two[:40] + tip[40:], tip, None, "revised", hit=False)
        self.check(over[:41] + tip[41:], tip, None, "revised",
                   needle="41 authorities")
        big = self.areas(400, 10000)
        a = [dict(r) for r in big]
        a[0]["people"] += 500
        self.check(a, big, None, "revised", hit=False)
        a[0]["people"] += 1
        self.check(a, big, None, "revised", needle="more than 500 people")
        t = [dict(r, people=r["people"] + 2000) for r in big[:50]] + big[50:]
        self.check(t, big, None, "revised", needle="total")
        # rows added: 10% passes, 11% halts
        add10 = tip + [srec(f"E06{i:06d}", 0 + 1, sup="Section 4")
                       for i in range(10)]
        add11 = tip + [srec(f"E06{i:06d}", 1, sup="Section 4")
                       for i in range(11)]
        self.check(add10, tip, None, "revised", hit=False)
        self.check(add11, tip, None, "revised", needle="11 of 100 rows added")

    def test_a_partial_file_is_rejected_even_when_acknowledged(self):
        tip = self.areas(100)
        for new, needle in ((tip[:89], "89 authorities"),
                            (tip[:89], "89 rows")):
            out = m.period_problems(new, tip, None, table="support",
                                    kind="revised", acknowledged=True)
            self.assertTrue(any(x.startswith(m.PARTIAL) and needle in x
                                for x in out), out)
        out = m.period_problems(tip[:90], tip, None, table="support",
                                kind="revised", acknowledged=True)
        self.assertEqual(out, [])

    def test_non_england_and_unallocated_revisions(self):
        tip = [{"lad_code": f"S12{i:06d}", "support_type": "Section 95",
                "accommodation_type": "Dispersal Accommodation",
                "people": 100, "country": "Scotland",
                "published_la_name": "x"} for i in range(50)]
        new = [dict(r, people=110) for r in tip]
        out = m.period_problems(new, tip, None, table="non_england",
                                kind="revised")
        self.assertTrue(any("total 5,000 -> 5,500" in x for x in out), out)
        un = [{"support_type": "Section 95", "accommodation_type": "x",
               "na_reason": "N/A", "people": 7}]
        out = m.period_problems([], un, None, table="unallocated",
                                kind="revised", acknowledged=True)
        self.assertTrue(any(x.startswith(m.PARTIAL) for x in out), out)

    def test_reg02_thresholds_both_sides(self):
        with mock.patch.object(m, "REG02_LAS", 40):
            las = [f"E06{i:06d}" for i in range(40)]
            tip = [grec(c) for c in las]
            self.check(tip, None, None, "new", table="groups", hit=False)
            out = m.period_problems(tip[:39], None, None, table="groups",
                                    kind="new", acknowledged=True)
            self.assertTrue(out and out[0].startswith(m.PARTIAL), out)
            out = m.period_problems(tip + [grec("E07000001")], None, None,
                                    table="groups", kind="new",
                                    acknowledged=True)
            self.assertIn("41 English authorities", out[0])
            five = [dict(r, people=11) if i < 20 else r
                    for i, r in enumerate(tip)]           # +5.0%, 20 LAs
            six = [dict(r, people=11) if i < 21 else r
                   for i, r in enumerate(tip)]
            self.check(five, tip, None, "revised", table="groups", hit=False)
            self.check(six, tip, None, "revised", table="groups",
                       needle="England supported_asylum total")
        with mock.patch.object(m, "REG02_LAS", 40), \
                mock.patch.object(m, "REG02_REV_PCT", 100):
            moved = [dict(r, population=2) for r in tip]
            self.check(moved[:30] + tip[30:], tip, None, "revised",
                       table="groups", hit=False)
            self.check(moved[:31] + tip[31:], tip, None, "revised",
                       table="groups", needle="31 authorities change")

    def test_blank_flips(self):
        tip = [grec("E06000001", n=0), grec("E06000002", n=None),
               grec("E06000003", n=4)]
        new = [grec("E06000001", n=None), grec("E06000002", n=0),
               grec("E06000003", n=None)]
        self.assertEqual(m.blank_flips(new, tip), [
            (("E06000001", "supported_asylum", "total"), "people", 0, None),
            (("E06000002", "supported_asylum", "total"), "people", None, 0)])


class _Resp:
    def __init__(self, body, url):
        self.content = body
        self.url = url
        self.text = ""

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body, final=None):
        self.body, self.final, self.calls = body, final, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return _Resp(self.body, self.final or url)


class Fetch(Tmp):

    def wb(self, quarters=Q3):
        p = write_d11(self.path("src.xlsx"), quarters)
        return p.read_bytes()

    def test_same_name_rule_and_redirect(self):
        self.enterContext(mock.patch.object(m, "MIN_FILE_BYTES", 1000))
        dest = self.root / "raw"
        body = self.wb()
        url = "https://a.example/m1/support-la.xlsx"
        with quiet():
            path, final = m.fetch(url, dest, _Session(body))
        self.assertEqual((path, final), (dest / "support-la.xlsx", url))
        # same name, same content: kept; downloaded again all the same
        s = _Session(body)
        with quiet():
            path2, _ = m.fetch(url, dest, s)
        self.assertEqual(path2, path)
        self.assertEqual(len(s.calls), 1)
        self.assertIn("ucws-pipeline", s.calls[0][1]["User-Agent"])
        # same name, different content: saved beside, the old one untouched
        other = self.wb(Q4)
        before = path.read_bytes()
        with quiet():
            path3, _ = m.fetch(url, dest, _Session(other))
        sha8 = hashlib.sha256(other).hexdigest()[:8]
        self.assertEqual(path3.name, f"support-la-{sha8}.xlsx")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path3.read_bytes(), other)
        # a redirect: the final URL's name and the final URL returned
        fin = "https://a.example/m9/support-la-new.xlsx"
        with quiet():
            p4, f4 = m.fetch(url, dest, _Session(body, fin))
        self.assertEqual((p4.name, f4), ("support-la-new.xlsx", fin))

    def test_a_file_that_does_not_open_is_not_kept(self):
        dest = self.root / "raw"
        with quiet(), self.assertRaises(SystemExit) as e:
            m.fetch("https://a.example/x.xlsx", dest,
                    _Session(b"x" * (m.MIN_FILE_BYTES + 10)))
        self.assertIn("does not open", str(e.exception.code))
        self.assertEqual(list(dest.iterdir()), [])
        p = write_reg02(self.path("r.ods"))
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet(), \
                self.assertRaises(SystemExit) as e:
            m.fetch("https://a.example/r.ods", dest,
                    _Session(p.read_bytes()), expect=(m.D11_SHEET,))
        self.assertIn("none of the sheets", str(e.exception.code))
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet():
            got, _ = m.fetch("https://a.example/r.ods", dest,
                             _Session(p.read_bytes()), expect=(m.REG02_SHEET,))
        self.assertEqual(got.name, "r.ods")


class Hygiene(unittest.TestCase):

    def test_no_zero_coercion_and_sha_literals_split(self):
        text = Path(m.__file__).read_text(encoding="utf-8")
        import check_loaders
        self.assertEqual(check_loaders._zero_coercions(text), [])
        import re
        self.assertIsNone(re.search(r"[0-9a-f]{64}", text))
        self.assertTrue(SHA_SPLIT)

    def test_no_private_recode_dict(self):
        import ast
        import check_loaders
        tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
        self.assertEqual(check_loaders._recode_dicts(tree), [])
        self.assertEqual(check_loaders._unguarded_commits(tree), [])


if __name__ == "__main__":
    unittest.main()
