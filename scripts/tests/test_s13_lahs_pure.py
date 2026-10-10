"""Pure tests for s13_lahs_editions: discovery, the year ODS and open data
CSV readers, cells, the two zero rules, successor sums, the CSV-ODS cross
check, imputations, geography, ranks, ledger and planning, the stop
conditions, the named correction and the download rules.

No database and no network: every .csv and .ods file is written by the tests
(the fixture writers are reused by test_s13_lahs_loader), the content API
replies are stubbed, and geography runs on a stand-in cursor that answers
the three queries resolve_codes makes.
"""
import contextlib
import csv
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s13_lahs_editions as m  # noqa: E402

# The fixture's authorities. Barnsley is published on its old code with the
# CSV's own LAD24CD on the new one (the June 2026 file's form). Cumberland's
# three predecessors report separately until 2022-23.
LA = {"E06000001": ("Hartlepool", "E06000001"),
      "E06000002": ("Middlesbrough", "E06000002"),
      "E08000016": ("Barnsley", "E08000038"),
      "E06000020": ("Telford and Wrekin", "E06000020"),
      "E07000026": ("Allerdale", "E06000063"),
      "E07000028": ("Carlisle", "E06000063"),
      "E07000029": ("Copeland", "E06000063"),
      "E06000063": ("Cumberland", "E06000063")}
PREDECESSORS = ("E07000026", "E07000028", "E07000029")
YEARS = ("2017-18", "2023-24", "2024-25")
KEYS = ["E06000001", "E06000002", "E06000020", "E06000063", "E08000016"]
OPEN_DATA = "Local Authority Housing Statistics open data 1978-79 to {}"
YEAR_DOC = "Local Authority Housing Statistics data returns for {} to {}"
YEAR_ODS = ("Local Authority Housing Statistics regional and local authority "
            "data {} to {}")
YEAR_BASE = ("/government/statistical-data-sets/local-authority-housing-"
             "statistics-data-returns-for-{}-to-{}")
SYMBOLS = ("[x] = not available, [z] = not applicable, [s] = suppressed due "
           "to data quality concerns")
DEF_CC1A = "Households on housing register (or waiting list)"
DEF_CC2A = ("Have you changed your housing register (or waiting list) "
            "criteria since last year in light of the changes in the Localism "
            "Act 2011?")
DEF_CC5A = ("Households on housing register (or waiting list) with reasonable "
            "preference")
CSV_HEADER = ["local_authority", "local_authority_code", "LAD24NM", "LAD24CD",
              "LAD24TYPE", "region_name", "Year", "status", "a0a", "cc1a",
              "cc1aa", "cc2a", "cc2b", "cc5a"]


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def codes_in(label):
    """The publisher codes reporting in a year: Cumberland's predecessors up
    to 2022-23, Cumberland itself from 2023-24."""
    later = label >= "2023-24"
    return [c for c in LA if (c == "E06000063") == later
            or c not in PREDECESSORS + ("E06000063",)]


def cell(code, label, col):
    """The published cell (text, as the CSV has it). Hartlepool publishes 0
    households in 2023-24 (a published zero); Telford and Wrekin 0 from
    2022-23 and [x] in 2023-24; Allerdale 0 in 2017-18; cc2a is [z] for
    every authority in 2024-25 (as published)."""
    y = int(label[:4]) - 2000
    if col == "cc1a":
        if code == "E06000001" and label == "2023-24":
            return "0"
        if code == "E06000020":
            return "[x]" if label == "2023-24" else "0"
        if code == "E07000026" and label <= "2017-18":
            return "0"
        n = 1000 + 37 * list(LA).index(code) + 11 * y
        return f"{n:,}"
    if col == "cc2a":
        if label == "2024-25":
            return "[z]"
        return "Yes" if (list(LA).index(code) + y) % 3 == 0 else "No"
    if col == "cc5a":
        return str(200 + 7 * list(LA).index(code) + y)
    raise KeyError(col)


def csv_rows(years=YEARS, overrides=None, statuses=None, drop=()):
    """[dict] of the fixture CSV, years in the file's order. overrides:
    {(code, label, col): text}; statuses: {(code, label): status}."""
    overrides = overrides or {}
    statuses = statuses or {}
    out = []
    for label in ("2010-11",) + tuple(years):
        for code in codes_in(label):
            name, lad = LA[code]
            r = {"local_authority": name, "local_authority_code": code,
                 "LAD24NM": LA[lad][0] if lad in LA else name,
                 "LAD24CD": lad, "LAD24TYPE": lad[:3],
                 "region_name": "North", "Year": label,
                 "status": statuses.get((code, label), "Submitted"),
                 "a0a": "Y", "cc1aa": "5", "cc2b": "N"}
            for col in ("cc1a", "cc2a", "cc5a"):
                r[col] = overrides.get((code, label, col), cell(code, label,
                                                                 col))
            for c in drop:
                r.pop(c, None)
            if (code, label) not in drop:
                out.append(r)
    return out


def write_csv(path, years=YEARS, *, header=None, rows=None, **kw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = header or CSV_HEADER
    rows = rows if rows is not None else csv_rows(years, **kw)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in header})
    return path


def _odsval(text):
    """A CSV cell as the ODS holds it: numbers as numbers, markers as text."""
    if not isinstance(text, str):
        return text
    t = text.replace(",", "")
    return int(t) if t.isdigit() else text


def ods_rows(label="2024-25", *, cc2a=False, overrides=None):
    """The Local_Authority_Data rows of the fixture's year (header row 3)."""
    overrides = overrides or {}
    head = ["local_authority", "local_authority_code", "region_name",
            "region_code", "status", "a0a", "cc1a"]
    head += (["cc2a"] if cc2a else []) + ["cc1aa", "cc5a"]
    rows = [["Local Authority Data"] + [None] * (len(head) - 1),
            ["This worksheet contains one table."] + [None] * (len(head) - 1),
            head]
    for code in codes_in(label):
        vals = {c: _odsval(overrides.get((code, c), cell(code, label, c)))
                for c in ("cc1a", "cc2a", "cc5a")}
        r = [LA[code][0], code, "North", "E12000001", "Submitted", "Y",
             vals["cc1a"]]
        r += ([vals["cc2a"]] if cc2a else []) + [5, vals["cc5a"]]
        rows.append(r)
    return rows


def cover_rows(label="2024-25", *, title_year=None, latest="25 June 2026",
               next_update="November 2026 to February 2027", symbols=SYMBOLS):
    rows = [f"Local Authority Housing Statistics (LAHS) Data: "
            f"{title_year or label}", "Description", "Housing data ...",
            "Contact", "housing.statistics@communities.gov.uk"]
    if symbols is not None:
        rows += ["Symbols Used", symbols]
    if latest is not None:
        rows += ["Latest update", latest]
    if next_update is not None:
        rows += ["Next Update", next_update]
    rows += ["Sources", "Local Authority Housing Statistics"]
    return [[r] for r in rows]


def dictionary_rows(*, cc2a=False, defs=None):
    defs = dict({"cc1a": DEF_CC1A, "cc2a": DEF_CC2A, "cc5a": DEF_CC5A},
                **(defs or {}))
    rows = [["Data Dictionary", None, None, None],
            ["This worksheet contains one table.", None, None, None],
            ["Column", "Type", "Description", "Note"],
            ["local_authority_code", "character", "Local authority code ...",
             None],
            ["cc1a", "numeric", defs["cc1a"], "From 31 March 2021 Telford "
             "& Wrekin Council does not operate a housing register."]]
    if cc2a:
        rows.append(["cc2a", "character", defs["cc2a"], None])
    rows.append(["cc5a", "numeric", defs["cc5a"], None])
    return rows


def write_ods(path, label="2024-25", *, cover=None, cc2a=False, la=None,
              imputations=None, dictionary=None, overrides=None,
              imputations_header=None):
    """A year .ods of the 2024-25 layout: Cover, Notes, Local_Authority_Data,
    Imputations, Data_Dictionary. imputations: [(name, var, orig, imputed)];
    default Middlesbrough cc1a and Hartlepool a3a."""
    import pandas as pd
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    imp = imputations if imputations is not None else [
        ("Middlesbrough", "cc1a", "[x]", 1409), ("Hartlepool", "a3a", "[x]",
                                                 12.5)]
    with pd.ExcelWriter(path, engine="odf") as w:
        pd.DataFrame(cover_rows(label, **(cover or {})), dtype=object
                     ).to_excel(w, sheet_name="Cover", header=False,
                                index=False)
        pd.DataFrame([["Notes"], ["[note 9] ..."]]).to_excel(
            w, sheet_name="Notes", header=False, index=False)
        pd.DataFrame(la if la is not None else ods_rows(
            label, cc2a=cc2a, overrides=overrides), dtype=object).to_excel(
            w, sheet_name="Local_Authority_Data", header=False, index=False)
        pd.DataFrame([["Imputations", None, None, None],
                      ["This worksheet contains one table.", None, None,
                       None],
                      list(imputations_header or (
                          "Local authority", "Variable", "Original value",
                          "Imputed value"))] + [list(r) for r in imp],
                     dtype=object).to_excel(w, sheet_name="Imputations",
                                            header=False, index=False)
        pd.DataFrame(dictionary if dictionary is not None
                     else dictionary_rows(cc2a=cc2a), dtype=object).to_excel(
            w, sheet_name="Data_Dictionary", header=False, index=False)
    return path


def open_data_page(label="2024-25", url=None, *, title=None, extra=(),
                   history=()):
    url = url or ("https://assets.example/media/c1/LAHS_open_data_1978-79_to_"
                  f"{label}.csv")
    atts = [{"title": OPEN_DATA.format(label), "url": url}]
    atts += [{"title": t, "url": u} for t, u in extra]
    return {"title": title or m.OPEN_DATA_TITLE,
            "public_updated_at": "2026-06-25T09:30:02+01:00",
            "details": {"attachments": atts, "change_history": [
                {"public_timestamp": ts, "note": n} for ts, n in (history or [
                    ("2026-06-25T08:30:02Z", "Updated following scheduled "
                     "revisions period for 2024-25 returns")])]}}


def _pair(label):
    a = int(label[:4])
    return a, a + 1


def collection(*labels, extra=(), title=None):
    docs = [{"title": YEAR_DOC.format(*_pair(lb)),
             "base_path": YEAR_BASE.format(*_pair(lb))} for lb in labels]
    docs += [{"title": t, "base_path": f"/government/x-{i}"}
             for i, t in enumerate(extra)]
    docs.append({"title": m.OPEN_DATA_TITLE, "base_path": m.OPEN_DATA_PATH})
    return {"title": title or m.COLLECTION_TITLE,
            "links": {"documents": docs}}


def year_page(label="2024-25", url=None, *, title=None, extra=()):
    url = url or ("https://assets.example/media/o1/LAHS_accessible_"
                  f"{label}_tables.ods")
    atts = [{"title": f"Local Authority Housing Statistics tables "
                      f"{_pair(label)[0]} to {_pair(label)[1]}",
             "url": "https://assets.example/media/x/LAHS.xlsx"},
            {"title": YEAR_ODS.format(*_pair(label)), "url": url}]
    atts += [{"title": t, "url": u} for t, u in extra]
    return {"title": title or YEAR_DOC.format(*_pair(label)),
            "public_updated_at": "2026-06-25T09:30:02+01:00",
            "details": {"attachments": atts, "change_history": [
                {"public_timestamp": "2026-06-25T08:30:02Z",
                 "note": "Updated following scheduled revisions"}]}}


class _Cur:
    """Answers resolve_codes' queries: the recode rows, la_code_lookup's
    new_unitary and merger rows, and la_boundaries."""

    BOUNDS = ["E06000001", "E06000002", "E06000020", "E06000063",
              "E08000016", "E08000019", "E06000059", "E06000066"]
    LOOKUP = [("E07000026", "E06000063", "new_unitary"),
              ("E07000028", "E06000063", "new_unitary"),
              ("E07000029", "E06000063", "new_unitary"),
              ("E07000150", "E06000061", "new_unitary")]  # target not bounded

    def __init__(self, bounds=None, lookup=None):
        self.bounds = self.BOUNDS if bounds is None else bounds
        self.lookup = self.LOOKUP if lookup is None else lookup
        self.result = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        if "change_type = 'recode'" in s:
            self.result = [("E08000038", "E08000016"),
                           ("E08000039", "E08000019")]
        elif "la_code_lookup" in s and "new_unitary" in s:
            self.result = [(o, n) for o, n, t in self.lookup
                           if t in ("new_unitary", "merger")]
        elif "la_code_lookup" in s:
            self.result = [(o,) for o, _, _ in self.lookup]
        elif "la_boundaries" in s:
            self.result = [(c,) for c in self.bounds]
        else:
            raise AssertionError(f"unexpected SQL {s}")

    def fetchall(self):
        return list(self.result)


class Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.n = 0

    def path(self, suffix):
        self.n += 1
        return self.root / f"f{self.n}{suffix}"

    def ods(self, label="2024-25", **kw):
        return write_ods(self.path(".ods"), label, **kw)

    def csv(self, years=YEARS, **kw):
        return write_csv(self.path(".csv"), years, **kw)

    def ods_raises(self, *needles, **kw):
        p = self.ods(**kw)
        with self.assertRaises(ValueError) as e:
            m.read_year_ods(p)
        for n in needles:
            self.assertIn(n, str(e.exception))
        return str(e.exception)


def build(od, ods=None, cur=None, period=None):
    """{period: records} of a read open data file: cells parsed, the zero
    rules applied, codes resolved on the stand-in cursor, successors built,
    imputed flags from the ODS for its year."""
    cur = cur or _Cur()
    parsed, applied = m.zero_rules(m.parse_rows(od["rows"]))
    out = {}
    for p in sorted({r["period"] for r in parsed}):
        if period is not None and p != period:
            continue
        rows = [r for r in parsed if r["period"] == p]
        rmap, problems = m.resolve_codes(
            cur, {r["code"] for r in rows}, p,
            {r["code"]: r["lad24cd_pub"] for r in rows})
        assert not problems, problems
        recs = m.successor_records(rows, rmap, p)
        if ods is not None and m.label_year(ods["year"]) == int(p):
            m.set_imputed(recs, m.imputed_flags(ods))
        out[p] = recs
    return out, applied


def by_key(records):
    return {r["lad24cd"]: r for r in records}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class Discovery(unittest.TestCase):

    def test_the_open_data_csv_is_chosen_and_the_accessible_copy_ignored(self):
        page = open_data_page(extra=[(
            "Local Authority Housing Statistics open data (accessible "
            "version) 1978-79 to 2024-25", "https://a.example/x.ods")])
        a = m.open_data_attachment(page)
        self.assertEqual(a["year"], "2024-25")
        self.assertTrue(a["url"].endswith(".csv"))
        self.assertEqual(len(a["ignored"]), 1)

    def test_attachment_selection_raises_on_none_or_several(self):
        none = open_data_page()
        none["details"]["attachments"] = [{"title": "Something else",
                                           "url": "https://a/x.csv"}]
        with self.assertRaises(ValueError) as e:
            m.open_data_attachment(none)
        self.assertIn("Something else", str(e.exception))
        two = open_data_page(extra=[(OPEN_DATA.format("2024-25"),
                                     "https://a.example/y.csv")])
        with self.assertRaises(ValueError) as e:
            m.open_data_attachment(two)
        self.assertIn("2 attachments", str(e.exception))
        notcsv = open_data_page(url="https://a.example/x.ods")
        with self.assertRaises(ValueError):
            m.open_data_attachment(notcsv)
        retitled = open_data_page(title="LAHS open data")
        with self.assertRaises(ValueError) as e:
            m.open_data_attachment(retitled)
        self.assertIn("LAHS open data", str(e.exception))

    def test_newest_year_page_and_strays(self):
        coll = collection("2022-23", "2023-24", "2024-25", extra=[
            "Local authority housing statistics data returns for 2019 to "
            "2020", "Completing local authority housing statistics 2025 to "
            "2026: guidance notes and bulk upload"])
        self.assertEqual(m.latest_year_page(coll),
                         ("2024-25", YEAR_BASE.format(2024, 2025)))
        self.assertEqual(m.year_page_for(coll, "2023-24"),
                         ("2023-24", YEAR_BASE.format(2023, 2024)))
        stray = collection("2024-25", extra=[
            "Local authority housing statistics data returns for 2025 to "
            "2026"])
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(stray)
        self.assertIn("2025 to 2026", str(e.exception))
        self.assertIn("2024-25", str(e.exception))
        two = collection("2024-25", "2024-25")
        with self.assertRaises(ValueError):
            m.latest_year_page(two)
        with self.assertRaises(ValueError) as e:
            m.latest_year_page(collection())
        self.assertIn("titles seen", str(e.exception))
        with self.assertRaises(ValueError):
            m.check_collection(collection("2024-25", title="Housing"))
        with self.assertRaises(ValueError):
            m.year_page_for(coll, "2025-26")

    def test_the_year_ods_attachment(self):
        a = m.year_ods_attachment(year_page())
        self.assertEqual(a["year"], "2024-25")
        self.assertTrue(a["url"].endswith(".ods"))
        with self.assertRaises(ValueError):
            m.year_ods_attachment(year_page(extra=[(YEAR_ODS.format(
                2024, 2025), "https://a.example/z.ods")]))
        page = year_page()
        page["details"]["attachments"] = page["details"]["attachments"][:1]
        with self.assertRaises(ValueError) as e:
            m.year_ods_attachment(page)
        self.assertIn("Local Authority Housing Statistics tables", str(
            e.exception))

    def test_discovery_note_names_the_newest_and_warns_when_late(self):
        page = open_data_page("2024-25")
        lines = m.discovery_note("2024-25", ["2025"], today=date(2026, 12, 1))
        self.assertIn("2024-25", lines[0])
        self.assertIn("no newer", lines[0])
        self.assertFalse(any("WARNING" in x for x in lines))
        lines = m.discovery_note("2024-25", ["2025"], today=date(2027, 3, 2))
        self.assertTrue(any("WARNING" in x and "2025-26" in x for x in lines))
        self.assertEqual(m.discovery_note("2025-26", ["2025"]), [])
        self.assertTrue(m.change_history(page))


# ---------------------------------------------------------------------------
# Labels and cells
# ---------------------------------------------------------------------------

class Cells(unittest.TestCase):

    def test_year_labels(self):
        self.assertEqual(m.label_year("2014-15"), 2015)
        self.assertEqual(m.year_label(2025), "2024-25")
        for bad in ("2014-16", "2014/15", "14-15", "1999-00x"):
            with self.assertRaises(ValueError):
                m.label_year(bad)
        self.assertEqual(m.label_year("1999-00"), 2000)

    def test_markers_give_null_with_their_flags(self):
        self.assertEqual(m.cell_int("[x]", "w"), (None, "not_available"))
        self.assertEqual(m.cell_int("[z]", "w"), (None, "not_applicable"))
        self.assertEqual(m.cell_int("[s]", "w"), (None, "suppressed"))
        self.assertEqual(m.cell_yes_no("[z]", "w"), (None, "not_applicable"))
        self.assertEqual(m.cell_yes_no("[x]", "w"), (None, "not_available"))

    def test_numbers_and_thousands_separators(self):
        self.assertEqual(m.cell_int("1,234", "w"), (1234, None))
        self.assertEqual(m.cell_int("12,345,678", "w"), (12345678, None))
        self.assertEqual(m.cell_int("0", "w"), (0, None))
        self.assertEqual(m.cell_int(1121, "w"), (1121, None))
        self.assertEqual(m.cell_yes_no("Yes", "w"), (True, None))
        self.assertEqual(m.cell_yes_no("No", "w"), (False, None))

    def test_anything_else_halts(self):
        for bad in ("x", "-", "", "  ", "n/a", "-5", "1.5", "1,23", "[c]",
                    None, True, -3, 2.5, "12a"):
            with self.subTest(v=bad), self.assertRaises(ValueError) as e:
                m.cell_int(bad, "row 9 cc1a")
            self.assertIn("row 9 cc1a", str(e.exception))
        for bad in ("Y", "yes", "", "[s]x", None, 1, "TRUE"):
            with self.subTest(v=bad), self.assertRaises(ValueError):
                m.cell_yes_no(bad, "w")


# ---------------------------------------------------------------------------
# The year ODS
# ---------------------------------------------------------------------------

class YearOds(Tmp):

    def test_reads_the_facts(self):
        o = m.read_year_ods(self.ods())
        self.assertEqual(o["year"], "2024-25")
        self.assertEqual(o["latest_update"], date(2026, 6, 25))
        self.assertEqual(o["rank"], date(2026, 6, 25))
        self.assertEqual(o["next_update"], "November 2026 to February 2027")
        self.assertEqual(o["markers"], {"x": "not available",
                                        "z": "not applicable",
                                        "s": "suppressed due to data quality "
                                             "concerns"})
        self.assertEqual(set(o["las"]), set(codes_in("2024-25")))
        self.assertFalse(o["has_cc2a"])
        self.assertEqual(o["las"]["E06000020"]["cc1a"], 0)
        self.assertEqual(o["dictionary"]["cc1a"], DEF_CC1A)
        self.assertIn(("Middlesbrough", "cc1a", "[x]", 1409),
                      [tuple(x) for x in o["imputations"]])

    def test_cc2a_column_and_definition_when_present(self):
        o = m.read_year_ods(self.ods(cc2a=True))
        self.assertTrue(o["has_cc2a"])
        self.assertEqual(o["las"]["E06000001"]["cc2a"], "[z]")

    def test_cover_year_mismatch_raises(self):
        p = self.ods(cover={"title_year": "2023-24"})
        self.assertEqual(m.read_year_ods(p)["year"], "2023-24")
        with self.assertRaises(ValueError) as e:
            m.read_year_ods(p, year="2024-25")
        self.assertIn("2023-24", str(e.exception))
        self.assertIn("expected 2024-25", str(e.exception))

    def test_missing_latest_update_raises(self):
        self.ods_raises("Latest update", cover={"latest": None})
        self.ods_raises("Latest update", cover={"latest": "soon"})

    def test_missing_symbols_or_next_update_raises(self):
        self.ods_raises("Symbols", cover={"symbols": None})
        self.ods_raises("[s]", cover={"symbols": "[x] = not available, "
                                                 "[z] = not applicable"})
        self.ods_raises("Next Update", cover={"next_update": None})

    def test_a_changed_definition_raises(self):
        for var in ("cc1a", "cc5a"):
            with self.subTest(var=var):
                self.ods_raises(var, "meaning", dictionary=dictionary_rows(
                    defs={var: "Households on the register at 1 April"}))
        self.ods_raises("cc2a", "meaning", cc2a=True, dictionary=dictionary_rows(
            cc2a=True, defs={"cc2a": "Is the register jointly managed?"}))
        # a cc2a column with no definition, or a definition missing
        self.ods_raises("cc2a", cc2a=True, dictionary=dictionary_rows())
        d = [r for r in dictionary_rows() if r[0] != "cc5a"]
        self.ods_raises("cc5a", dictionary=d)

    def test_header_missing_a_column_raises(self):
        rows = ods_rows()
        rows[2] = [c if c != "cc5a" else "cc5b" for c in rows[2]]
        self.ods_raises("cc5a", la=rows)
        rows = ods_rows()
        rows[2] = [c if c != "status" else "state" for c in rows[2]]
        self.ods_raises("status", la=rows)

    def test_imputations_header_and_names(self):
        self.ods_raises("Imputations", imputations_header=(
            "Authority", "Variable", "Original value", "Imputed value"))
        o = m.read_year_ods(self.ods(imputations=[("Nowhere", "cc1a", "[x]",
                                                   3)]))
        with self.assertRaises(ValueError) as e:
            m.imputed_flags(o)
        self.assertIn("Nowhere", str(e.exception))

    def test_imputed_flags(self):
        o = m.read_year_ods(self.ods())
        f = m.imputed_flags(o)
        self.assertIn(("E06000002", "cc1a"), f)
        self.assertIn(("E06000001", "a3a"), f)
        self.assertNotIn(("E06000001", "cc1a"), f)

    def test_a_duplicate_or_odd_row_raises(self):
        rows = ods_rows()
        rows.append(list(rows[3]))
        self.ods_raises("twice", la=rows)
        rows = ods_rows()
        rows.append(["England", "E92000001", None, None, "Submitted", "Y", 1,
                     5, 1])
        self.ods_raises("E92000001", la=rows)

    def test_rank_from_the_cover_never_the_name(self):
        a = self.ods(cover={"latest": "12 February 2026"})
        b = self.root / "LAHS_accessible_2024-25_tables_25_June_2026.ods"
        b.write_bytes(a.read_bytes())
        self.assertEqual(m.read_year_ods(b)["rank"], date(2026, 2, 12))
        self.assertEqual(m.release_rank(m.read_year_ods(a)),
                         date(2026, 2, 12))
        self.assertIsNone(m.release_rank("LAHS 25 June 2026.csv"))
        self.assertIsNone(m.release_rank(
            "https://assets.example/media/2026-06-25/x.csv"))
        self.assertEqual(m.release_rank(
            "x.csv; LAHS 2024-25; latest update 2026-06-25; where"),
            date(2026, 6, 25))


# ---------------------------------------------------------------------------
# The open data CSV
# ---------------------------------------------------------------------------

class OpenData(Tmp):

    def test_reads_the_years_from_2014_15(self):
        od = m.read_open_data(self.csv())
        self.assertEqual(od["years"], list(YEARS))
        self.assertEqual(od["newest"], "2024-25")
        self.assertNotIn("2010-11", {r["year"] for r in od["rows"]})
        r = [x for x in od["rows"] if x["code"] == "E08000016"
             and x["year"] == "2024-25"][0]
        self.assertEqual(r["period"], "2025")
        self.assertEqual(r["lad24cd_pub"], "E08000038")
        self.assertEqual(r["status"], "Submitted")
        self.assertGreater(r["line"], 1)

    def test_years_argument_limits_the_rows(self):
        od = m.read_open_data(self.csv(), years=["2024-25"])
        self.assertEqual({r["year"] for r in od["rows"]}, {"2024-25"})
        self.assertEqual(od["newest"], "2024-25")

    def test_header_missing_a_column_raises(self):
        for col in ("LAD24CD", "status", "cc5a", "Year",
                    "local_authority_code"):
            with self.subTest(col=col):
                header = [c for c in CSV_HEADER if c != col]
                p = self.csv(header=header)
                with self.assertRaises(ValueError) as e:
                    m.read_open_data(p)
                self.assertIn(col, str(e.exception))

    def test_a_bad_year_or_code_raises(self):
        rows = csv_rows()
        rows[-1]["Year"] = "2024/25"
        with self.assertRaises(ValueError) as e:
            m.read_open_data(self.csv(rows=rows))
        self.assertIn("2024/25", str(e.exception))
        rows = csv_rows()
        rows[-1]["local_authority_code"] = "E92000001"
        with self.assertRaises(ValueError) as e:
            m.read_open_data(self.csv(rows=rows))
        self.assertIn("E92000001", str(e.exception))
        rows = csv_rows()
        rows.append(dict(rows[-1]))
        with self.assertRaises(ValueError) as e:
            m.read_open_data(self.csv(rows=rows))
        self.assertIn("twice", str(e.exception))


# ---------------------------------------------------------------------------
# Records: cells, zero rules, successors
# ---------------------------------------------------------------------------

class Records(Tmp):

    def read(self, **kw):
        return m.read_open_data(self.csv(**kw))

    def test_a_published_zero_stays_zero(self):
        recs, _ = build(self.read())
        r = by_key(recs["2024"])["E06000001"]
        self.assertEqual(r["households_on_register"], 0)
        self.assertIsNone(r["value_flag"])

    def test_the_two_zero_rules_fire_only_on_their_keys_and_years(self):
        recs, applied = build(self.read())
        tel = by_key(recs["2025"])["E06000020"]
        self.assertIsNone(tel["households_on_register"])
        self.assertEqual(tel["value_flag"],
                         "households_on_register=not_applicable; "
                         "jointly_managed_register=not_applicable")
        # 2024: Telford published [x], not 0: the marker's own flag
        self.assertEqual(by_key(recs["2024"])["E06000020"]["value_flag"],
                         "households_on_register=not_available")
        cum = by_key(recs["2018"])["E06000063"]
        self.assertIsNone(cum["households_on_register"])
        self.assertTrue(cum["value_flag"].startswith(
            "households_on_register=part_missing"), cum["value_flag"])
        self.assertEqual([(a["rule"], a["code"], a["period"], a["column"])
                          for a in applied],
                         [("ALLERDALE_ZERO", "E07000026", "2018",
                           "households_on_register"),
                          ("TELFORD_NO_REGISTER", "E06000020", "2025",
                           "households_on_register")])
        # Telford before 2022 and Allerdale after 2018 are untouched; the
        # rules touch nothing but cc1a 0
        early = self.read(years=("2017-18", "2018-19", "2020-21"),
                          overrides={("E06000020", "2020-21", "cc1a"): "0",
                                     ("E07000026", "2018-19", "cc1a"): "0"})
        recs, applied = build(early)
        self.assertEqual(by_key(recs["2021"])["E06000020"][
            "households_on_register"], 0)
        self.assertEqual([a["period"] for a in applied], ["2018"])
        self.assertEqual(by_key(recs["2019"])["E06000063"][
            "households_on_register"],
            sum(int(cell(c, "2018-19", "cc1a").replace(",", ""))
                for c in PREDECESSORS[1:]))

    def test_the_rules_are_named_constants_with_evidence(self):
        self.assertEqual([r["name"] for r in m.ZERO_RULES],
                         ["TELFORD_NO_REGISTER", "ALLERDALE_ZERO"])
        self.assertEqual(m.TELFORD_NO_REGISTER["code"], "E06000020")
        self.assertEqual(m.TELFORD_NO_REGISTER["first_year"], 2022)
        self.assertIsNone(m.TELFORD_NO_REGISTER["last_year"])
        self.assertIn("2026-09-30-telford-no-housing-register.md",
                      m.TELFORD_NO_REGISTER["evidence"])
        self.assertEqual(m.ALLERDALE_ZERO["code"], "E07000026")
        self.assertEqual((m.ALLERDALE_ZERO["first_year"],
                          m.ALLERDALE_ZERO["last_year"]), (2015, 2018))
        self.assertEqual(m.ALLERDALE_ZERO["flag"], "not_counted")
        self.assertIn("Scott", m.ALLERDALE_ZERO["evidence"])
        for r in m.ZERO_RULES:
            self.assertEqual(r["column"], "households_on_register")
        self.assertIn("reported 0 means not applicable",
                      m.TELFORD_NO_REGISTER["note"])

    def test_three_predecessors_all_present_sum(self):
        rows = [{"code": c, "lad24cd_pub": "E06000063", "period": "2019",
                 "line": i + 2, "status": s,
                 "households_on_register": h, "jointly_managed_register": j,
                 "reasonable_preference": rp, "flags": {}}
                for i, (c, s, h, j, rp) in enumerate([
                    ("E07000029", "Submitted", 100, False, 10),
                    ("E07000026", "Saved", 200, False, 20),
                    ("E07000028", "Submitted", 300, False, 30)])]
        (r,) = m.successor_records(rows, {c: "E06000063" for c in
                                          PREDECESSORS}, "2019")
        self.assertEqual(r["households_on_register"], 600)
        self.assertEqual(r["reasonable_preference"], 60)
        self.assertIs(r["jointly_managed_register"], False)
        self.assertEqual(r["predecessor_codes"],
                         "E07000026;E07000028;E07000029")
        self.assertEqual(r["return_status"], "Saved;Submitted;Submitted")
        self.assertIsNone(r["value_flag"])
        self.assertIsNone(r["imputed_cc1a"])

    def test_one_missing_predecessor_gives_null_part_missing(self):
        od = self.read(overrides={("E07000028", "2017-18", "cc1a"): "[x]",
                                  ("E07000026", "2017-18", "cc1a"): "5"})
        recs, _ = build(od)
        r = by_key(recs["2018"])["E06000063"]
        self.assertIsNone(r["households_on_register"])
        self.assertTrue(r["value_flag"].startswith(
            "households_on_register=part_missing"), r["value_flag"])
        self.assertIsNotNone(r["reasonable_preference"])

    def test_predecessors_disagreeing_on_cc2a_give_parts_disagree(self):
        od = self.read(overrides={("E07000026", "2017-18", "cc2a"): "Yes",
                                  ("E07000028", "2017-18", "cc2a"): "No",
                                  ("E07000029", "2017-18", "cc2a"): "No"})
        r = by_key(build(od)[0]["2018"])["E06000063"]
        self.assertIsNone(r["jointly_managed_register"])
        self.assertIn("jointly_managed_register=parts_disagree",
                      r["value_flag"])
        od = self.read(overrides={(c, "2017-18", "cc2a"): "Yes"
                                  for c in PREDECESSORS})
        r = by_key(build(od)[0]["2018"])["E06000063"]
        self.assertIs(r["jointly_managed_register"], True)

    def test_single_row_values_and_flags(self):
        od = self.read(overrides={("E06000002", "2023-24", "cc5a"): "[s]"})
        recs, _ = build(od)
        r = by_key(recs["2024"])["E06000002"]
        self.assertEqual(r["households_on_register"],
                         int(cell("E06000002", "2023-24", "cc1a")
                             .replace(",", "")))
        self.assertIsNone(r["reasonable_preference"])
        self.assertEqual(r["value_flag"], "reasonable_preference=suppressed")
        self.assertEqual(r["predecessor_codes"], "E06000002")
        b = by_key(recs["2025"])["E08000016"]
        self.assertEqual(b["predecessor_codes"], "E08000016")
        self.assertIsNone(b["jointly_managed_register"])
        self.assertEqual(b["value_flag"],
                         "jointly_managed_register=not_applicable")

    def test_a_bad_cell_halts_naming_the_line(self):
        od = self.read(overrides={("E06000002", "2023-24", "cc1a"): "-"})
        with self.assertRaises(ValueError) as e:
            m.parse_rows(od["rows"])
        self.assertIn("line", str(e.exception))
        self.assertIn("cc1a", str(e.exception))

    def test_duplicate_source_rows_for_a_key_halt(self):
        rows = [{"code": "E06000001", "lad24cd_pub": "E06000001",
                 "period": "2019", "line": n, "status": "Submitted",
                 "households_on_register": 1, "jointly_managed_register": True,
                 "reasonable_preference": 1, "flags": {}} for n in (2, 3)]
        with self.assertRaises(ValueError) as e:
            m.successor_records(rows, {"E06000001": "E06000001"}, "2019")
        self.assertIn("twice", str(e.exception))

    def test_imputed_flags_mark_the_newest_year(self):
        od = self.read()
        o = m.read_year_ods(self.ods())
        recs, _ = build(od, o)
        self.assertIs(by_key(recs["2025"])["E06000002"]["imputed_cc1a"],
                      True)
        self.assertIs(by_key(recs["2025"])["E06000001"]["imputed_cc1a"],
                      False)
        self.assertIsNone(by_key(recs["2024"])["E06000002"]["imputed_cc1a"])

    def test_content_sha_covers_the_flags_not_the_provenance(self):
        recs, _ = build(self.read())
        a = m.rows_content_sha(recs["2025"])
        b = m.rows_content_sha([dict(r, live_source="x") for r in
                                recs["2025"]])
        self.assertEqual(a, b)
        for col, v in (("value_flag", "households_on_register=suppressed"),
                       ("return_status", "Saved"), ("imputed_cc1a", True),
                       ("predecessor_codes", "E07000001"),
                       ("reasonable_preference", 1)):
            changed = [dict(r) for r in recs["2025"]]
            changed[0][col] = v
            self.assertNotEqual(m.rows_content_sha(changed), a, col)


# ---------------------------------------------------------------------------
# CSV against ODS
# ---------------------------------------------------------------------------

class CrossCheck(Tmp):

    def test_equal_passes(self):
        od = m.read_open_data(self.csv())
        m.cross_check(od, m.read_year_ods(self.ods()))
        m.cross_check(od, m.read_year_ods(self.ods(cc2a=True)))

    def test_one_cell_different_halts(self):
        od = m.read_open_data(self.csv())
        for col, v in (("cc1a", 999), ("cc5a", "[x]")):
            with self.subTest(col=col):
                o = m.read_year_ods(self.ods(overrides={("E06000002", col):
                                                        v}))
                with self.assertRaises(ValueError) as e:
                    m.cross_check(od, o)
                self.assertIn("E06000002", str(e.exception))
                self.assertIn(col, str(e.exception))
        o = m.read_year_ods(self.ods(cc2a=True, overrides={
            ("E06000002", "cc2a"): "No"}))
        with self.assertRaises(ValueError):
            m.cross_check(od, o)

    def test_cc2a_absent_from_the_ods_needs_z_in_the_csv(self):
        od = m.read_open_data(self.csv(overrides={("E06000002", "2024-25",
                                                   "cc2a"): "Yes"}))
        with self.assertRaises(ValueError) as e:
            m.cross_check(od, m.read_year_ods(self.ods()))
        self.assertIn("cc2a", str(e.exception))

    def test_year_or_authorities_differing_halts(self):
        od = m.read_open_data(self.csv())
        with self.assertRaises(ValueError):
            m.cross_check(od, m.read_year_ods(self.ods("2023-24")))
        rows = ods_rows()
        rows.pop()
        with self.assertRaises(ValueError) as e:
            m.cross_check(od, m.read_year_ods(self.ods(la=rows)))
        self.assertIn("only in the CSV", str(e.exception))


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

class Geography(unittest.TestCase):

    CODES = {"E06000001", "E08000016", "E07000026", "E07000028"}

    def pub(self, **kw):
        out = {"E06000001": "E06000001", "E08000016": "E08000038",
               "E07000026": "E06000063", "E07000028": "E06000063"}
        out.update(kw)
        return out

    def test_predecessors_resolve_through_la_code_lookup(self):
        rmap, problems = m.resolve_codes(_Cur(), self.CODES, "2019",
                                         self.pub())
        self.assertEqual(problems, [])
        self.assertEqual(rmap["E07000026"], "E06000063")
        self.assertEqual(rmap["E08000016"], "E08000016")

    def test_lad24cd_disagreeing_halts_except_barnsley_sheffield(self):
        _, problems = m.resolve_codes(_Cur(), self.CODES, "2019",
                                      self.pub(E06000001="E06000002"))
        self.assertTrue(any("E06000001" in p and "LAD24CD" in p
                            for p in problems), problems)
        _, problems = m.resolve_codes(_Cur(), self.CODES, "2019",
                                      self.pub(E08000016="E08000016"))
        self.assertEqual(problems, [])
        _, problems = m.resolve_codes(_Cur(), self.CODES, "2019",
                                      self.pub(E08000016="E08000039"))
        self.assertTrue(problems)

    def test_an_unknown_code_halts(self):
        _, problems = m.resolve_codes(_Cur(), {"E06000001", "E07000999"},
                                      "2019")
        self.assertTrue(any("UNEXPLAINED E07000999" in p for p in problems),
                        problems)
        # a lookup row whose target is not in la_boundaries explains nothing
        _, problems = m.resolve_codes(_Cur(), {"E07000150"}, "2019")
        self.assertTrue(any("UNEXPLAINED E07000150" in p for p in problems))

    def test_a_new_code_halts_against_the_old_declaration(self):
        _, problems = m.resolve_codes(_Cur(), {"E06000001", "E08000038"},
                                      "2025")
        self.assertTrue(any("E08000038" in p and "'old'" in p
                            for p in problems), problems)

    def test_dorset_districts_resolve_only_through_la_code_lookup(self):
        dorset = ["E07000049", "E07000050", "E07000051", "E07000052",
                  "E07000053"]
        rows = [(c, "E06000059", "new_unitary") for c in dorset]
        cur = _Cur(lookup=_Cur.LOOKUP + rows)
        rmap, problems = m.resolve_codes(
            cur, set(dorset), "2019", {c: "E06000059" for c in dorset})
        self.assertEqual(problems, [])
        self.assertEqual(set(rmap.values()), {"E06000059"})
        # the publisher's LAD24CD must agree with the lookup row
        _, problems = m.resolve_codes(cur, {"E07000049"}, "2019",
                                      {"E07000049": "E06000058"})
        self.assertTrue(problems)
        # without the lookup rows there is no other route (no private table)
        _, problems = m.resolve_codes(_Cur(), {"E07000049"}, "2019",
                                      {"E07000049": "E06000059"})
        self.assertTrue(any("UNEXPLAINED E07000049" in p for p in problems),
                        problems)
        self.assertFalse(hasattr(m, "LOOKUP_GAPS"))

    def test_the_declaration_is_old_with_the_file_evidence(self):
        import geography
        form, ev = geography.DATASET_FORM["13"]
        self.assertEqual(form, "old")
        self.assertIn("LAD24CD", ev)
        self.assertIn("E08000038/39", ev)
        self.assertIn("2026-10-10", ev)


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

R1, R2 = date(2026, 2, 12), date(2026, 6, 25)


class Planning(unittest.TestCase):

    def test_ledger_source_round_trip(self):
        s = m.ledger_source("https://a/x.csv", R2, "2024-25",
                            ["2025", "2015"], ods="y.ods sha256 0123")
        self.assertEqual(m.parse_ledger_source(s),
                         ("https://a/x.csv", R2, ["2015", "2025"]))
        self.assertEqual(m.release_rank(s), R2)
        self.assertIsNone(m.parse_ledger_source("nonsense"))

    def test_ledger_complete_needs_the_pair_for_every_period(self):
        src = m.ledger_source("u", R2, "2024-25", ["2024", "2025"])
        checked = {"2024": {(src, "s1")}}
        self.assertIsNone(m.ledger_complete(checked, "u", "s1", {}))
        checked["2025"] = {(src, "s1")}
        self.assertEqual(m.ledger_complete(checked, "u", "s1", {}),
                         ["2024", "2025"])
        self.assertIsNone(m.ledger_complete(checked, "u", "s2", {}))
        self.assertIsNone(m.ledger_complete(checked, "v", "s1", {}))
        self.assertIsNone(m.ledger_complete(checked, "u", "s1", {},
                                            stranded=["2025"]))

    def test_plan_periods_older_checked_and_recheck(self):
        ranks = {"2024": R2, "2025": R1}
        new, rev, skip = m.plan_periods(["2024", "2025", "2026"], R1, ranks,
                                        {}, "src", "sha", recheck=None,
                                        allow_older=False)
        self.assertEqual((new, rev), (["2026"], ["2025"]))
        self.assertTrue(skip["2024"].startswith("older"))
        new, rev, skip = m.plan_periods(["2024", "2025"], R1, ranks,
                                        {"2025": {("src", "sha")}}, "src",
                                        "sha", recheck=None,
                                        allow_older=True)
        self.assertEqual(rev, ["2024"])
        self.assertTrue(skip["2025"].startswith("checked"))
        new, rev, skip = m.plan_periods(["2025"], R1, ranks,
                                        {"2025": {("src", "sha")}}, "src",
                                        "sha", recheck="2025",
                                        allow_older=False)
        self.assertEqual(rev, ["2025"])
        with self.assertRaises(ValueError):
            m.plan_periods(["2025"], R1, ranks, {}, "src", "sha",
                           recheck="2019", allow_older=False)
        # a new period before the newest held year is a back-fill
        new, rev, skip = m.plan_periods(["2014"], R2, {"2015": R1}, {}, "s",
                                        "h", recheck=None, allow_older=False)
        self.assertEqual(new, [])
        self.assertTrue(skip["2014"].startswith("older"))

    def test_tip_ranks_take_the_ledger_into_account(self):
        tips = {"2025": {"rank": R1}}
        checked = {"2025": {(m.ledger_source("u", R2, "2024-25", ["2025"]),
                             "h")}}
        self.assertEqual(m.tip_ranks(tips, checked), {"2025": R2})


# ---------------------------------------------------------------------------
# Stop conditions and the named correction
# ---------------------------------------------------------------------------

def recs(values, period="2025", **extra):
    """Records {lad24cd: households} with fixed other columns."""
    return [dict({"lad24cd": k, "reporting_year": period,
                  "households_on_register": v,
                  "jointly_managed_register": False,
                  "reasonable_preference": 10, "value_flag": None,
                  "return_status": "Submitted", "imputed_cc1a": None,
                  "predecessor_codes": k}, **extra.get(k, {}))
            for k, v in values.items()]


def areas(n, base=1000):
    return {f"E0700{i:04d}": base for i in range(n)}


class StopConditions(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(m, "EXPECTED_AREAS", 100)
        p.start()
        self.addCleanup(p.stop)

    def test_the_constants(self):
        self.assertEqual((m.NEW_TOTAL_PCT, m.NEW_AREAS_MOVING,
                          m.NEW_AREA_PCT, m.REV_TOTAL_PCT, m.REV_MAX_AREAS),
                         (15, 30, 50, 2, 20))
        self.assertIn("calibrat", m.__doc__.lower())

    def test_new_year_england_sum_both_sides(self):
        prev = recs(areas(100))
        ok = recs({k: 1150 for k in areas(100)})            # +15%: fine
        self.assertEqual(m.period_problems(ok, None, prev, kind="new"), [])
        bad = recs({k: 1151 for k in areas(100)})
        self.assertTrue(any("England" in p for p in
                            m.period_problems(bad, None, prev, kind="new")))

    def test_new_year_authorities_moving_both_sides(self):
        prev = recs(areas(100))
        v = areas(100)
        for i, k in enumerate(v):
            if i < 30:
                v[k] = 1600             # 30 authorities +60%
            elif i < 60:
                v[k] = 600              # balance the England sum
        found = m.period_problems(recs(v), None, prev, kind="new")
        self.assertFalse(any("more than 50%" in p for p in found), found)
        v[list(v)[60]] = 1600
        v[list(v)[61]] = 400
        found = m.period_problems(recs(v), None, prev, kind="new")
        self.assertTrue(any("more than 50%" in p for p in found), found)

    def test_revised_year_england_sum_both_sides(self):
        tip = recs(areas(100))
        v = areas(100)
        for k in list(v)[:20]:
            v[k] = 1100                 # +2,000 on 100,000: 2%
        self.assertEqual(m.period_problems(recs(v), tip, None,
                                           kind="revised"), [])
        v[list(v)[0]] = 1101
        self.assertTrue(any("England" in p for p in m.period_problems(
            recs(v), tip, None, kind="revised")))

    def test_revised_year_authorities_changing_both_sides(self):
        tip = recs(areas(100))
        v = areas(100)
        for k in list(v)[:20]:
            v[k] = 1001
        self.assertEqual(m.period_problems(recs(v), tip, None,
                                           kind="revised"), [])
        v[list(v)[20]] = 1001
        self.assertTrue(any("authorities change" in p for p in
                            m.period_problems(recs(v), tip, None,
                                              kind="revised")))

    def test_fewer_authorities_than_the_tip_is_partial_always(self):
        tip = recs(areas(100))
        new = recs(dict(list(areas(100).items())[:99]))
        found = m.period_problems(new, tip, None, kind="revised")
        self.assertTrue(any(p.startswith(m.PARTIAL) for p in found))
        found = m.period_problems(new, None, tip, kind="new")
        self.assertTrue(any(p.startswith(m.PARTIAL) for p in found))

    def test_null_never_counts_as_zero_in_the_sums(self):
        tip = recs(areas(100))
        v = areas(100)
        v[list(v)[0]] = None
        found = m.period_problems(recs(v), tip, None, kind="revised")
        self.assertTrue(any("0/NULL" in p or "NULL" in p for p in found),
                        found)


class Correction(unittest.TestCase):

    def test_classify_groups(self):
        tip = recs({"E06000063": 100, "E06000001": 5, "E06000058": 7},
                   "2016", **{k: {"reasonable_preference": None}
                              for k in ("E06000063", "E06000001",
                                        "E06000058")})
        new = recs({"E06000063": None, "E06000001": 5, "E06000058": 30},
                   "2016", E06000063={"predecessor_codes":
                                      "E07000026;E07000028;E07000029",
                                      "value_flag": "households_on_register"
                                                    "=part_missing"},
                   E06000058={"predecessor_codes": "E06000028;E06000029"})
        g = m.classify_changes(new, tip, "2016")
        self.assertEqual(g["cc5a_filled"], ["E06000001", "E06000058",
                                            "E06000063"])
        self.assertEqual(g["allerdale"], ["E06000063"])
        self.assertEqual(g["predecessor_sum"], ["E06000058"])
        self.assertEqual(g["other"], [])
        new[1]["households_on_register"] = 6
        self.assertEqual(m.classify_changes(new, tip, "2016")["other"],
                         ["E06000001"])

    def test_the_named_correction_needs_exact_counts(self):
        a = {"periods": {"2016": {"cc5a_filled": 3, "predecessor_sum": 1,
                                  "allerdale": 1}},
             "decided": "x", "why": "y"}
        g = {"cc5a_filled": ["a", "b", "c"], "predecessor_sum": ["d"],
             "allerdale": ["e"], "other": []}
        with mock.patch.dict(m.ACKNOWLEDGED_CORRECTIONS, {"t": a}):
            self.assertEqual(m.correction_problems("t", "2016", g), [])
            g2 = dict(g, other=["f"])
            self.assertTrue(m.correction_problems("t", "2016", g2))
            g3 = dict(g, predecessor_sum=[])
            self.assertTrue(m.correction_problems("t", "2016", g3))
            self.assertTrue(m.correction_problems("t", "2017", g))

    def test_the_real_correction_is_named_and_covers_eleven_years(self):
        a = m.ACKNOWLEDGED_CORRECTIONS["lahs-correction-2026-10"]
        self.assertEqual(sorted(a["periods"]),
                         [str(y) for y in range(2015, 2026)])
        self.assertEqual(sum(p.get("predecessor_sum", 0)
                             for p in a["periods"].values())
                         + sum(p.get("allerdale", 0)
                               for p in a["periods"].values()), 76)
        self.assertEqual(sum(p.get("allerdale", 0)
                             for p in a["periods"].values()), 4)
        self.assertEqual(sum(p["cc5a_filled"] for p in a["periods"].values()),
                         3241)
        self.assertNotIn("predecessor_sum", a["periods"]["2025"])
        self.assertIn("Scott", a["decided"])


# ---------------------------------------------------------------------------
# Downloads
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

    def test_redirect_and_same_name_rule(self):
        body = self.csv().read_bytes()
        raw = self.root / "raw"
        s = _Session(body, "https://assets.example/media/2/LAHS.csv")
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            path, final = m.fetch("https://assets.example/media/1/LAHS.csv",
                                  raw, session=s, kind="csv")
        self.assertEqual(final, "https://assets.example/media/2/LAHS.csv")
        self.assertEqual(path.read_bytes(), body)
        self.assertIn("redirected", out.getvalue())
        other = self.csv(overrides={("E06000002", "2024-25", "cc1a"): "7"}
                         ).read_bytes()
        s2 = _Session(other, final)
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet() as out:
            p2, _ = m.fetch(final, raw, session=s2, kind="csv")
        self.assertEqual(path.read_bytes(), body)     # never overwritten
        self.assertNotEqual(p2, path)
        self.assertEqual(p2.read_bytes(), other)
        self.assertIn("kept it untouched", out.getvalue())
        self.assertEqual(s2.calls, [final])            # downloaded anyway
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet():
            p3, _ = m.fetch(final, raw, session=_Session(body, final),
                            kind="csv")
        self.assertEqual(p3, path)

    def test_wrong_kind_or_small_file_halts(self):
        body = self.csv().read_bytes()
        with self.assertRaises(SystemExit), quiet():
            m.fetch("https://a/x.ods", self.root / "raw",
                    session=_Session(body, "https://a/x.ods"), kind="csv")
        with self.assertRaises(SystemExit), quiet():
            m.fetch("https://a/x.csv", self.root / "raw",
                    session=_Session(body, "https://a/x.csv"), kind="csv")
        ods = self.ods().read_bytes()
        with mock.patch.object(m, "MIN_FILE_BYTES", 10), quiet():
            p, _ = m.fetch("https://a/y.ods", self.root / "raw",
                           session=_Session(ods, "https://a/y.ods"),
                           kind="ods")
        self.assertEqual(p.read_bytes(), ods)


if __name__ == "__main__":
    unittest.main()
