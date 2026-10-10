"""Pure tests for s12_financial_stress_editions (no database, no network).

The page fixtures are GOV.UK content-API JSON built here from the shapes of
the held year pages (data/raw/s12_efs, read 2026-10-10): a two-column table
headed 'Local authority' and 'Exceptional Financial Support requests from
local authorities: <yyyy-yy>', cells with <br> line breaks, the Housing
Revenue Account and Police Force tables under their <h2> headings, the
Birmingham revised-profile table, and the Capitalisation directions section
of attachment links. The S.114 registers are written to a temporary
directory that the tests make the only allowed root.
"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s12_financial_stress_editions as m  # noqa: E402

YEAR_BASE = "/guidance/exceptional-financial-support-for-local-authorities-for-{}"
HEADER = "Exceptional Financial Support requests from local authorities: {}"

# real la_boundaries names and codes (a subset, as read 2026-10-10)
BOUNDARIES = {
    "Barnet": "E09000003", "Bexley": "E09000004", "Birmingham": "E08000025",
    "Bournemouth, Christchurch and Poole": "E06000058",
    "Bradford": "E08000032", "City of London": "E09000001",
    "Croydon": "E09000008", "Cumberland": "E06000063",
    "Enfield": "E09000010", "Gloucester": "E07000081",
    "Hammersmith and Fulham": "E09000013", "Haringey": "E09000014",
    "Kensington and Chelsea": "E09000020", "Lambeth": "E09000022",
    "Luton": "E06000032", "Medway": "E06000035",
    "North Northamptonshire": "E06000061", "Nottingham": "E06000018",
    "Peterborough": "E06000031", "Plymouth": "E06000026",
    "Redcar and Cleveland": "E06000003", "Shropshire": "E06000051",
    "Slough": "E06000039", "Somerset": "E06000066",
    "South Gloucestershire": "E06000025", "Thurrock": "E06000034",
    "Westmorland and Furness": "E06000064",
    "Windsor and Maidenhead": "E06000040", "Wirral": "E08000015",
    "Woking": "E07000217", "Wokingham": "E06000041",
}


def cell_html(*lines):
    return "<br>".join(lines)


def table(rows, year, *, header=True):
    head = ("<thead><tr><th scope=\"col\">Local authority</th>"
            f"<th scope=\"col\">{HEADER.format(year)}</th></tr></thead>"
            if header else "")
    body = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows)
    return f"<table>{head}<tbody>{body}</tbody></table>"


def body(year, main, *, hra=(), police=(), other_years=None, directions=(),
         main_heading=False, header_year=None, extra=""):
    """A year page body. main/hra/police: [(name, cell html)];
    other_years: (authority, [(year, cell html)]); directions: link titles
    under 'Capitalisation directions'."""
    hy = header_year or year
    out = ["<div class=\"govspeak\"><p>In the financial year "
           f"{year} the government agreed support.</p>"]
    if main_heading:
        out.append(f"<h2 id=\"x\">{HEADER.format(year)}</h2>")
    out.append(table(main, hy))
    if hra:
        out.append("<h2 id=\"housing-revenue-account\">Housing Revenue "
                   "Account</h2><p>One council.</p>" + table(hra, hy))
    if police:
        out.append("<h2 id=\"police-force\">Police Force</h2>"
                   + table(police, hy))
    if other_years:
        who, rows = other_years
        out.append(f"<h2 id=\"rev\">{who} – Revised Exceptional "
                   "Financial Support for previous financial years</h2>"
                   "<p>The detail of the revised profile is below:</p>"
                   + table(rows, hy, header=False))
    out.append(extra)
    if directions:
        out.append("<h2 id=\"capitalisation-directions\">Capitalisation "
                   "directions</h2>")
        for t in directions:
            out.append("<p><span class=\"gem-c-attachment-link\"><a "
                       "class=\"govuk-link\" href=\"https://assets.example/"
                       f"x.pdf\">{t}</a> (PDF, 1 KB, 2 pages)</span></p>")
    out.append("<h2 id=\"interventions-in-local-authorities\">Interventions "
               "in local authorities</h2><p><a href=\"/x\">Statutory "
               "intervention: London Borough of Croydon</a></p></div>")
    return "".join(out)


def page(year, main, *, updated="2026-02-23T15:03:00+00:00", history=None,
         title=None, base_path=None, **kw):
    hist = history if history is not None else [
        {"note": "First published.", "public_timestamp": "2024-02-29T00:00:00Z"}]
    return {
        "title": title or f"Exceptional Financial Support for local "
                          f"authorities for {year}",
        "base_path": base_path or YEAR_BASE.format(year),
        "document_type": "detailed_guide",
        "public_updated_at": updated,
        "details": {"body": body(year, main, **kw), "change_history": hist},
    }


def collection(years, *, extra=(), title=None):
    docs = [{"title": f"Exceptional Financial Support for local authorities "
                      f"for {y}", "base_path": YEAR_BASE.format(y),
             "public_updated_at": "2026-02-23T15:03:00Z"} for y in years]
    docs += [{"title": t, "base_path": "/guidance/other",
              "public_updated_at": "2026-01-01T00:00:00Z"} for t in extra]
    return {"title": title or m.COLLECTION_TITLE,
            "base_path": m.COLLECTION_PATH, "links": {"documents": docs}}


def book(aliases=None):
    """A NameBook on the fixture boundaries and the committed alias file
    (or the given aliases)."""
    if aliases is None:
        aliases, _ = m.load_names()
    return m.NameBook(BOUNDARIES, aliases)


def exclusions():
    return m.load_names()[1]


def records(pg, directions=None, aliases=None):
    """efs_records with directions resolved from the page itself."""
    nb = book(aliases)
    ident = m.page_identity(pg)
    if directions is None:
        directions = m.resolve_directions(
            m.capitalisation_directions(pg["details"]["body"], ident["year"]),
            nb, exclusions())
    return m.efs_records(pg, nb, exclusions(), directions)


def D(x):
    return Decimal(x).quantize(Decimal("0.001"))


# ---------------------------------------------------------------------------
# The cell grammar
# ---------------------------------------------------------------------------

class Grammar(unittest.TestCase):

    def fig(self, html, year="2025-26"):
        c = m.parse_cell(html)
        f = c.figure(year)
        return (f.amount, f.qual) if f else None

    def test_first_amount_forms(self):
        self.assertEqual(self.fig("£55.7m (support agreed in-principle)"),
                         (D("55.7"), "in-principle"))
        self.assertEqual(self.fig("£113.0m"), (D("113.0"), None))
        self.assertEqual(self.fig("£3.7m (in the form of grant)", "2020-21"),
                         (D("3.7"), "grant"))
        self.assertEqual(self.fig("£125m (original capitalisation of £100m "
                                  "in 2017-18 was extended by £25m)",
                                  "2020-21"), (D("125"), "extended"))
        self.assertEqual(self.fig("£50.0m (covering 2023-24 to 2024-25)",
                                  "2023-24"), (D("50.0"), "covering"))
        self.assertEqual(self.fig("£0.926m (support agreed in-principle)"),
                         (D("0.926"), "in-principle"))

    def test_subsequently_revised_picks_the_last_figure(self):
        croydon = cell_html(
            "£136.0m (support agreed in-principle)", "",
            " This was subsequently revised to £110.3m (support agreed "
            "in-principle)", "",
            "Note: For support agreed in-principle for 2023-24, this has "
            "been revised to £50.0m (from £63.0m), and for support agreed "
            "in-principle for 2024-25, this has been revised to £51m (from "
            "£38m)")
        self.assertEqual(self.fig(croydon), (D("110.3"), "in-principle"))
        twice = cell_html("£3.0m (support agreed in-principle)", "",
                          "This was subsequently revised to £20.0m (support "
                          "agreed in-principle)", "",
                          "This was subsequently revised to: £24.5m (support "
                          "agreed in-principle)")
        self.assertEqual(self.fig(twice), (D("24.5"), "in-principle"))
        birmingham = cell_html(
            "£685.0m (support agreed in-principle)", "",
            "This was subsequently revised to:",
            "£490.0m (support agreed in-principle for 2024-25)", "",
            "£570.1m agreed in-principle covering:", "2020-21: £288.4m",
            "2021-22: £109.5m", "2022-23: £172.2m")
        self.assertEqual(self.fig(birmingham, "2024-25"),
                         (D("490.0"), "in-principle"))
        c = m.parse_cell(birmingham)
        self.assertEqual([(s.year, s.amount) for s in c.others("2024-25")],
                         [("2020-21", D("288.4")), ("2021-22", D("109.5")),
                          ("2022-23", D("172.2"))])
        thurrock = cell_html(
            "£180.17m (support agreed in-principle)",
            "£452.491m agreed in-principle for 2022-23", "",
            "This was reprofiled to:", "",
            "£40.0m agreed in-principle for 2022-23 (February 2024), then "
            "subsequently reprofiled to £130m agreed in-principle for 2022-23 "
            "(February 2025)", "",
            "£234.5m agreed in-principle for 2023-24 (February 2024), then "
            "subsequently reprofiled to £184.0m agreed in-principle for "
            "2023-24 (February 2025)")
        self.assertEqual(self.fig(thurrock, "2023-24"),
                         (D("184.0"), "in-principle"))
        self.assertEqual(
            [(s.year, s.amount) for s in
             m.parse_cell(thurrock).others("2023-24")],
            [("2022-23", D("452.491")), ("2022-23", D("40.0")),
             ("2022-23", D("130"))])

    def test_statements_about_another_year(self):
        c = m.parse_cell("£10.0m<br>£20.0m for 2024-25")
        self.assertEqual(c.figure("2025-26").amount, D("10.0"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2025-26")],
                         [("2024-25", D("20.0"))])
        c = m.parse_cell("£58.0m (support agreed in-principle)<br>£22.0m "
                         "(support agreed in-principle for 2025-26)")
        self.assertEqual(c.figure("2026-27").amount, D("58.0"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2026-27")],
                         [("2025-26", D("22.0"))])
        c = m.parse_cell(
            "£72.0m (support agreed in-principle)<br><br>This was "
            "subsequently revised to £62.11m (support agreed in-principle)"
            "<br><br>Note: For support agreed in-principle for 2022-23, this "
            "has been revised to £130.0m (from £40.0m agreed in February "
            "2024), for 2023-24, this has been revised to £184.0m (from "
            "£234.5m agreed in February 2024), and for 2024-25, this has "
            "been revised to £96.0m (from £68.6m agreed in February 2024)")
        self.assertEqual(c.figure("2025-26").amount, D("62.11"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2025-26")],
                         [("2022-23", D("130.0")), ("2023-24", D("184.0")),
                          ("2024-25", D("96.0"))])
        c = m.parse_cell("£25.3m (support agreed in-principle)<br><br>Note: "
                         "For support agreed in principle for 2024-25, this "
                         "has been revised to £17.6m (from £6m)")
        self.assertEqual([(s.year, s.amount) for s in c.others("2025-26")],
                         [("2024-25", D("17.6"))])
        c = m.parse_cell("£56.614m<br><br>£210.458m covering:<br>2018-19: "
                         "£78.015m<br>2019-20: £47.536m")
        self.assertEqual(c.figure("2022-23").amount, D("56.614"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2022-23")],
                         [("2018-19", D("78.015")), ("2019-20", D("47.536"))])
        c = m.parse_cell("£6.6m agreed in-principle for 2023-24, then "
                         "subsequently reprofiled to £6.52m for 2024-25")
        self.assertEqual((c.figure("2023-24").amount,
                          c.figure("2023-24").qual), (D("6.6"), "in-principle"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2023-24")],
                         [("2024-25", D("6.52"))])
        c = m.parse_cell("£95.6m (support agreed in-principle)<br><br>This "
                         "was subsequently revised to:<br>£93.6m (support "
                         "agreed in-principle for 2024-25)<br><br>£235.1m "
                         "agreed in-principle for 2023-24")
        self.assertEqual(c.figure("2024-25").amount, D("93.6"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2024-25")],
                         [("2023-24", D("235.1"))])
        c = m.parse_cell("£63.0m (support agreed in-principle)<br><br>This "
                         "was subsequently revised to £45.118m (support "
                         "agreed in-principle)<br><br>Note: Provisionally "
                         "includes revised support agreed in 2024-25, "
                         "subject to final confirmation")
        self.assertEqual(c.figure("2025-26").amount, D("45.118"))
        self.assertEqual(c.others("2025-26"), [])
        # the publisher's own misspelling, as on the 2025-26 page
        c = m.parse_cell("£89.9m (support agreed in-principle)<br><br>Note: "
                         "Provisionally incudes revised support agreed in "
                         "2024-25, subject to final confirmation")
        self.assertEqual(c.figure("2025-26").amount, D("89.9"))
        # the revised-profile table's cells (Birmingham, 2026-27 page)
        c = m.parse_cell("£99.5m (from £288.4m - support agreed in-principle)")
        self.assertEqual((c.figure("2020-21").amount,
                          c.figure("2020-21").qual), (D("99.5"), "in-principle"))

    def test_a_cell_with_only_other_years_has_no_own_figure(self):
        c = m.parse_cell("£26.9m for 2024-25")
        self.assertIsNone(c.figure("2025-26"))
        self.assertEqual([(s.year, s.amount) for s in c.others("2025-26")],
                         [("2024-25", D("26.9"))])

    def test_withdrawn_cell(self):
        c = m.parse_cell("Council provided with in-principle support but "
                         "withdrew its request")
        self.assertTrue(c.withdrawn)
        self.assertIsNone(c.figure("2020-21"))

    def test_unknown_forms_halt_naming_the_cell(self):
        for html in ("£12.0m (support agreed in principle, subject to review)",
                     "About £5m", "£5bn", "£1.2345m",
                     "£10.0m<br>£2.0m pending",
                     "£210.458m covering:",
                     "£3.0m<br>This was subsequently revised to:",
                     "£4.0m but withdrew its request",
                     "To be confirmed",
                     "Note: For support agreed in-principle for 2024-25, this "
                     "has been revised to £5m"):
            with self.subTest(html=html):
                with self.assertRaises(ValueError) as cm:
                    m.parse_cell(html, where="2025-26 page, row Testshire")
                self.assertIn("2025-26 page, row Testshire", str(cm.exception))

    def test_cell_text_reparses_to_the_same_cell(self):
        html = ("£136.0m (support agreed in-principle)<br><br> This was "
                "subsequently revised to £110.3m (support agreed in-principle)")
        c = m.parse_cell(html)
        again = m.parse_cell(c.text)
        self.assertEqual(again.figure("2025-26"), c.figure("2025-26"))
        self.assertEqual(c.text, "£136.0m (support agreed in-principle)\n"
                         "This was subsequently revised to £110.3m (support "
                         "agreed in-principle)")


# ---------------------------------------------------------------------------
# Tables, headings, directions, identity, discovery
# ---------------------------------------------------------------------------

class Tables(unittest.TestCase):

    def test_heading_kinds(self):
        b = body("2026-27", [("Barnet", "£79.6m (support agreed in-principle)")],
                 main_heading=True,
                 hra=[("City of London", "£2.65m (support agreed in-principle)")],
                 police=[("Kent Police and Crime Commissioner",
                          "£29.0m for 2025-26")],
                 other_years=("Birmingham", [("2020-21", "£99.5m (from "
                                              "£288.4m - support agreed "
                                              "in-principle)")]))
        got = [(h, m.table_kind(h)[0], len(rows)) for h, rows in m.tables(b)]
        self.assertEqual([k for _, k, _ in got],
                         ["main", "hra", "police", "other-years"])
        self.assertEqual(m.table_kind(got[3][0]), ("other-years", "Birmingham"))
        self.assertEqual(m.table_kind(None), ("main", None))
        self.assertEqual(m.table_kind("Capitalisation support for 2024-25"),
                         ("capitalisation-support", "2024-25"))

    def test_an_unknown_heading_halts(self):
        b = body("2025-26", [("Barnet", "£55.7m")],
                 extra="<h2 id=\"assurance-reviews\">Assurance reviews</h2>"
                 + table([("Barnet", "£1.0m")], "2025-26"))
        with self.assertRaises(ValueError) as cm:
            m.tables(b)
        self.assertIn("Assurance reviews", str(cm.exception))

    def test_capitalisation_directions_by_year(self):
        b = body("2025-26", [("Bradford", "£113.0m")], directions=(
            "Bradford capitalisation direction 2025-26",
            "Enfield capitalisation direction 2024-25",
            "Slough capitalisation direction 2018-19 to 2026-27",
            "Eastbourne capitalisation direction 2020-21 and 2025-26",
            "South Yorkshire Mayoral Combined Authority Capitalisation "
            "Direction 2024-25"))
        self.assertEqual(m.capitalisation_directions(b, "2025-26"),
                         {"Bradford", "Slough", "Eastbourne"})
        self.assertEqual(m.capitalisation_directions(b, "2024-25"),
                         {"Enfield", "Slough",
                          "South Yorkshire Mayoral Combined Authority"})
        b = body("2020-21", [("Redcar & Cleveland", "£3.7m (in the form of "
                                                    "grant)")],
                 directions=("Redcar and Cleveland grant determination 2020-21",
                             "Croydon capitalisation direction 2020-21"))
        self.assertEqual(m.capitalisation_directions(b, "2020-21"), {"Croydon"})
        self.assertEqual(m.section_names(b), {"Redcar and Cleveland",
                                              "Croydon"})
        bad = body("2025-26", [("Bradford", "£113.0m")],
                   directions=("Bradford direction letter",))
        with self.assertRaises(ValueError):
            m.capitalisation_directions(bad, "2025-26")


class Identity(unittest.TestCase):

    def test_identity_from_the_page(self):
        pg = page("2025-26", [("Barnet", "£55.7m")],
                  updated="2026-08-18T15:12:33+01:00",
                  history=[{"note": "Added directions",
                            "public_timestamp": "2026-08-18T14:12:33Z"},
                           {"note": "First published.",
                            "public_timestamp": "2025-02-20T00:00:00Z"}])
        ident = m.page_identity(pg)
        self.assertEqual(ident["year"], "2025-26")
        self.assertEqual(ident["rank"],
                         datetime(2026, 8, 18, 14, 12, 33, tzinfo=timezone.utc))
        self.assertEqual(m.rank_text(ident["rank"]),
                         "page updated 2026-08-18T14:12:33Z")

    def test_rank_from_updated_and_history_never_the_base_path(self):
        a = page("2025-26", [("Barnet", "£55.7m")],
                 updated="2026-02-23T15:02:38Z",
                 history=[{"note": "x", "public_timestamp":
                           "2026-08-18T14:12:33Z"}])
        b = page("2025-26", [("Barnet", "£55.7m")],
                 updated="2026-08-18T14:12:33Z", history=[],
                 base_path="/guidance/something-else-2019-20")
        self.assertEqual(m.page_identity(a)["rank"],
                         m.page_identity(b)["rank"])
        c = page("2025-26", [("Barnet", "£55.7m")],
                 updated="2026-03-01T00:00:00Z", history=[],
                 base_path=YEAR_BASE.format("2026-27"))
        self.assertLess(m.page_identity(c)["rank"],
                        m.page_identity(a)["rank"])
        self.assertEqual(m.page_identity(c)["year"], "2025-26")

    def test_title_or_header_for_another_year_halts(self):
        with self.assertRaises(ValueError):
            m.page_identity(page("2025-26", [("Barnet", "£1m")],
                                 title="Exceptional Financial Support for "
                                       "local authorities for 2024-25 draft"))
        with self.assertRaises(ValueError) as cm:
            m.page_identity(page("2025-26", [("Barnet", "£1m")],
                                 header_year="2024-25"))
        self.assertIn("2024-25", str(cm.exception))
        no_table = page("2025-26", [])
        no_table["details"]["body"] = "<div><p>No table.</p></div>"
        with self.assertRaises(ValueError):
            m.page_identity(no_table)

    def test_year_pages(self):
        yp = m.year_pages(collection(["2025-26", "2026-27"],
                                     extra=["Exceptional Financial Support: "
                                            "assurance reviews"]))
        self.assertEqual(dict(yp), {"2025-26": YEAR_BASE.format("2025-26"),
                                    "2026-27": YEAR_BASE.format("2026-27")})
        self.assertEqual(yp.others, ["Exceptional Financial Support: "
                                     "assurance reviews"])
        with self.assertRaises(ValueError) as cm:
            m.year_pages(collection(["2026-27"]), held=["2025-26", "2026-27"])
        self.assertIn("2025-26", str(cm.exception))
        dup = collection(["2025-26", "2025-26"])
        with self.assertRaises(ValueError):
            m.year_pages(dup)
        with self.assertRaises(ValueError):
            m.year_pages(collection(["2025-26"], title="Something else"))


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

class Names(unittest.TestCase):

    def test_haringey_is_e09000014_never_e09000013(self):
        aliases, excl = m.load_names()
        self.assertEqual(m.match_name("Haringey", BOUNDARIES, aliases, excl),
                         "E09000014")
        self.assertNotIn("E09000013", set(aliases.values()))
        self.assertNotIn("Haringey", aliases)

    def test_aliases_and_exclusions(self):
        aliases, excl = m.load_names()
        self.assertEqual(m.match_name("Windsor & Maidenhead", BOUNDARIES,
                                      aliases, excl), "E06000040")
        self.assertEqual(m.match_name("Copeland", BOUNDARIES, aliases, excl),
                         "E06000063")
        for name in ("East Sussex", "Worcestershire", "Norfolk",
                     "Kent Police and Crime Commissioner",
                     "South Yorkshire Mayoral Combined Authority"):
            self.assertIsNone(m.match_name(name, BOUNDARIES, aliases, excl))
        raw = json.loads(m.NAMES_FILE.read_text(encoding="utf-8"))
        for name, a in raw["aliases"].items():
            self.assertTrue(a["seen"], name)
            self.assertNotIn(name, BOUNDARIES)

    def test_unmatched_and_near_miss_names_halt_never_choosing(self):
        aliases, excl = m.load_names()
        nb = {k: v for k, v in BOUNDARIES.items() if k != "Gloucester"}
        with self.assertRaises(ValueError) as cm:
            m.match_name("Gloucester", nb, aliases, excl)
        self.assertIn("Gloucester", str(cm.exception))
        self.assertNotIn("E06000025", str(cm.exception))
        nb = {k: v for k, v in BOUNDARIES.items() if k != "Woking"}
        with self.assertRaises(ValueError) as cm:
            m.match_name("Woking", nb, aliases, excl)
        self.assertNotIn("E06000041", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            m.match_name("Haringey Council", BOUNDARIES, aliases, excl)
        msg = str(cm.exception)
        self.assertIn("'Haringey'", msg)          # listed as a candidate
        self.assertIn("never chosen", msg)
        with self.assertRaises(ValueError):
            m.match_name("haringey", BOUNDARIES, aliases, excl)

    def test_no_fuzzy_matching_in_the_module(self):
        src = Path(m.__file__).read_text(encoding="utf-8")
        self.assertNotIn("difflib", src)
        self.assertNotIn("get_close_matches", src)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class Records(unittest.TestCase):

    def test_statuses_amounts_and_hra(self):
        pg = page("2025-26", [
            ("Barnet", "£55.7m (support agreed in-principle)"),
            ("Bradford", "£113.0m"),
            ("Croydon", "£136.0m (support agreed in-principle)<br><br>This "
                        "was subsequently revised to £110.3m (support agreed "
                        "in-principle)"),
            ("Shropshire", "£26.9m for 2024-25"),
            ("Worcestershire", "£33.6m"),
            ("Haringey", "£40.6m<br>£10m for 2024-25"),
        ], hra=[("Lambeth", "£40.0m (support agreed in-principle)")],
            police=[("South Yorkshire Mayoral Combined Authority",
                     "£17.0m (support agreed for 2024-25)")],
            directions=("Bradford capitalisation direction 2025-26",
                        "Haringey capitalisation direction 2025-26",
                        "Worcestershire capitalisation direction 2025-26",
                        "Shropshire capitalisation direction 2024-25",
                        "South Yorkshire Mayoral Combined Authority "
                        "Capitalisation Direction 2024-25"),
            updated="2026-08-18T14:12:33Z")
        recs = records(pg)
        by = {r["lad24cd"]: r for r in recs}
        self.assertEqual(set(by), {"E09000003", "E08000032", "E09000008",
                                   "E06000051", "E09000014", "E09000022"})
        self.assertEqual((by["E09000003"]["amount_m"],
                          by["E09000003"]["status"]),
                         (D("55.7"), "agreed-in-principle"))
        self.assertEqual((by["E08000032"]["amount_m"],
                          by["E08000032"]["status"]),
                         (D("113.0"), "capitalisation-direction"))
        self.assertEqual(by["E09000008"]["amount_m"], D("110.3"))
        self.assertEqual((by["E06000051"]["amount_m"],
                          by["E06000051"]["status"]),
                         (None, "other-years-only"))
        self.assertIs(by["E09000022"]["hra_only"], True)
        self.assertIs(by["E09000003"]["hra_only"], False)
        self.assertEqual(by["E09000014"]["status"], "capitalisation-direction")
        self.assertEqual(by["E08000032"]["page_updated_at"],
                         datetime(2026, 8, 18, 14, 12, 33, tzinfo=timezone.utc))
        self.assertEqual(by["E09000008"]["cell_text"].splitlines()[0],
                         "£136.0m (support agreed in-principle)")
        self.assertEqual(sorted(n for n, _, _ in recs.excluded),
                         ["South Yorkshire Mayoral Combined Authority",
                          "Worcestershire"])
        self.assertEqual([(s["lad24cd"], s["year"], s["amount"])
                          for s in recs.statements],
                         [("E06000051", "2024-25", D("26.9")),
                          ("E09000014", "2024-25", D("10"))])

    def test_a_bare_amount_needs_a_capitalisation_direction(self):
        pg = page("2025-26", [("Bradford", "£113.0m")])
        with self.assertRaises(ValueError) as cm:
            records(pg)
        self.assertIn("Bradford", str(cm.exception))
        self.assertIn("2025-26", str(cm.exception))

    def test_withdrawn_grant_extended(self):
        pg = page("2020-21", [
            ("Bexley", "Council provided with in-principle support but "
                       "withdrew its request"),
            ("Redcar & Cleveland", "£3.7m (in the form of grant)"),
            ("Lambeth", "£125m (original capitalisation of £100m in 2017-18 "
                        "was extended by £25m)")],
            directions=("Bexley capitalisation direction 2020-21",))
        by = {r["lad24cd"]: r for r in records(pg)}
        self.assertEqual((by["E09000004"]["amount_m"],
                          by["E09000004"]["status"]), (None, "withdrawn"))
        self.assertEqual(by["E06000003"]["status"], "grant")
        self.assertEqual(by["E09000022"]["status"], "capitalisation-extended")
        self.assertEqual(by["E09000022"]["amount_m"], D("125"))

    def test_excluded_counties_and_police_are_counted(self):
        pg = page("2026-27", [
            ("Barnet", "£79.6m (support agreed in-principle)"),
            ("East Sussex", "£70.0m (support agreed in-principle)"),
            ("Worcestershire", "£59.933m (support agreed in-principle)")],
            police=[("Kent Police and Crime Commissioner", "£29.0m for 2025-26"),
                    ("South Yorkshire Mayoral Combined Authority",
                     "£6.6m (support agreed in-principle for 2025-26)")])
        recs = records(pg)
        self.assertEqual([r["lad24cd"] for r in recs], ["E09000003"])
        self.assertEqual(len(recs.excluded), 4)
        pg = page("2024-25", [("Slough", "£23.078m")],
                  extra="<h2 id=\"c\">Capitalisation support for 2024-25</h2>"
                  + table([("Norfolk", "£36.063m (support agreed in February "
                                       "2026)")], "2024-25", header=False),
                  directions=("Slough capitalisation direction 2024-25",
                              "Norfolk capitalisation direction 2024-25"))
        recs = records(pg)
        self.assertEqual([r["lad24cd"] for r in recs], ["E06000039"])
        self.assertEqual([n for n, _, _ in recs.excluded], ["Norfolk"])

    def test_an_unmatched_row_halts_naming_the_page(self):
        pg = page("2025-26", [("Gloucestershire", "£5.0m (support agreed "
                                                  "in-principle)")])
        with self.assertRaises(ValueError) as cm:
            records(pg)
        self.assertIn("Gloucestershire", str(cm.exception))

    def test_an_authority_in_two_tables_halts(self):
        pg = page("2025-26", [("Lambeth", "£5.0m (support agreed "
                                          "in-principle)")],
                  hra=[("Lambeth", "£40.0m (support agreed in-principle)")])
        with self.assertRaises(ValueError):
            records(pg)

    def test_revised_profile_table_gives_statements_for_its_authority(self):
        pg = page("2026-27", [("Barnet", "£79.6m (support agreed "
                                         "in-principle)")],
                  other_years=("Birmingham", [
                      ("2024-25", "£405.7m (from £491.1m - support agreed "
                                  "in-principle)"),
                      ("2025-26", "£36.7m (from £180m - support agreed "
                                  "in-principle)")]))
        recs = records(pg)
        self.assertEqual([r["lad24cd"] for r in recs], ["E09000003"])
        self.assertEqual([(s["lad24cd"], s["year"], s["amount"])
                          for s in recs.statements],
                         [("E08000025", "2024-25", D("405.7")),
                          ("E08000025", "2025-26", D("36.7"))])


# ---------------------------------------------------------------------------
# Statements about other years, the withdrawn-only rule
# ---------------------------------------------------------------------------

class Statements(unittest.TestCase):

    def pages(self):
        p24 = page("2024-25", [
            ("Birmingham", "£685.0m (support agreed in-principle)<br><br>This "
                           "was subsequently revised to:<br>£490.0m (support "
                           "agreed in-principle for 2024-25)"),
            ("Croydon", "£51.0m (support agreed in-principle)")])
        p25 = page("2025-26", [
            ("Birmingham", "£180.0m (support agreed in-principle)<br><br>Note: "
                           "For support agreed in-principle for 2024-25, this "
                           "has been revised to £490.0m (from £685.0m)"),
            ("Croydon", "£110.3m (support agreed in-principle)<br><br>Note: "
                        "For support agreed in-principle for 2023-24, this "
                        "has been revised to £50.0m (from £63.0m), and for "
                        "support agreed in-principle for 2024-25, this has "
                        "been revised to £52m (from £38m)"),
            ("Enfield", "£10.0m (support agreed in-principle)<br>£20.0m for "
                        "2024-25")])
        return {"2024-25": records(p24), "2025-26": records(p25)}

    def test_each_statement_compared_with_its_years_own_page(self):
        st = m.other_year_statements(self.pages())
        got = {(s["lad24cd"], s["year"]): s["outcome"] for s in st}
        self.assertEqual(got, {("E08000025", "2024-25"): "agrees",
                               ("E09000008", "2023-24"): "no page",
                               ("E09000008", "2024-25"): "differs",
                               ("E09000010", "2024-25"): "absent"})
        dis = m.disagreements(st)
        self.assertEqual(list(dis), ["2024-25"])
        self.assertEqual(dis["2024-25"][0]["own_amount"], D("51.0"))
        self.assertEqual(dis["2024-25"][0]["amount"], D("52"))

    def test_only_the_last_statement_per_year_on_a_page_is_compared(self):
        p22 = page("2022-23", [("Croydon", "£25.0m (support agreed "
                                           "in-principle)")])
        p23 = page("2023-24", [("Thurrock", cell_html(
            "£180.17m (support agreed in-principle)",
            "£452.491m agreed in-principle for 2022-23",
            "This was reprofiled to:",
            "£40.0m agreed in-principle for 2022-23 (February 2024), then "
            "subsequently reprofiled to £130m agreed in-principle for "
            "2022-23 (February 2025)"))])
        st = m.other_year_statements({"2022-23": records(p22),
                                      "2023-24": records(p23)})
        outcomes = [(s["amount"], s["outcome"]) for s in st]
        self.assertEqual(outcomes, [(D("452.491"), "superseded on the same "
                                     "page"),
                                    (D("40.0"), "superseded on the same page"),
                                    (D("130"), "absent")])


class WithdrawnRule(unittest.TestCase):

    def recs(self, year, rows):
        return records(page(year, rows))

    def test_named_withdrawn_only_authorities_are_dropped(self):
        w = ("Council provided with in-principle support but withdrew its "
             "request")
        by = {"2020-21": self.recs("2020-21", [("Bexley", w), ("Peterborough", w),
                                               ("Luton", "£35.0m (support "
                                                         "agreed in-principle)")]),
              "2021-22": self.recs("2021-22", [("Bexley", w), ("Luton", w)]),
              "2026-27": self.recs("2026-27", [("Peterborough", "£5.68m "
                                                "(support agreed "
                                                "in-principle)")])}
        kept, dropped = m.apply_withdrawn_rule(by)
        self.assertEqual(sorted((c, y) for c, y, _ in dropped),
                         [("E09000004", "2020-21"), ("E09000004", "2021-22")])
        self.assertEqual(sorted(r["lad24cd"] for r in kept["2020-21"]),
                         ["E06000031", "E06000032"])
        self.assertEqual([r["status"] for r in kept["2021-22"]], ["withdrawn"])

    def test_a_named_authority_with_support_halts(self):
        by = {"2020-21": self.recs("2020-21", [("Bexley", "£5.0m (support "
                                                          "agreed in-principle)")])}
        with self.assertRaises(ValueError) as cm:
            m.apply_withdrawn_rule(by)
        self.assertIn("E09000004", str(cm.exception))

    def test_an_unnamed_withdrawn_only_authority_halts(self):
        w = ("Council provided with in-principle support but withdrew its "
             "request")
        by = {"2023-24": self.recs("2023-24", [("Wirral", w)])}
        with self.assertRaises(ValueError) as cm:
            m.apply_withdrawn_rule(by)
        self.assertIn("E08000015", str(cm.exception))
        # support held in another period (not read in this run) clears it
        held = {"2020-21": [{"lad24cd": "E08000015", "status":
                             "agreed-in-principle", "amount_m": D("9.0")}]}
        kept, dropped = m.apply_withdrawn_rule(by, held=held)
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept["2023-24"]), 1)


# ---------------------------------------------------------------------------
# Ranks, ledger, planning
# ---------------------------------------------------------------------------

class Ledger(unittest.TestCase):
    R1 = datetime(2026, 2, 23, 15, 2, 38, tzinfo=timezone.utc)
    R2 = datetime(2026, 8, 18, 14, 12, 33, tzinfo=timezone.utc)

    def test_ledger_source_round_trip(self):
        s = m.ledger_source("https://www.gov.uk/api/content/x", "2025-26",
                            self.R2)
        self.assertEqual(m.parse_ledger_source(s),
                         ("https://www.gov.uk/api/content/x", "2025-26",
                          self.R2))
        self.assertEqual(m.release_rank(s), self.R2)
        self.assertIsNone(m.release_rank("as loaded: n8n run-log 27"))

    def test_plan_periods(self):
        w = "data/raw/s12_efs/p.json"
        sha = "ab" * 32
        pages = {"2025-26": {"where": w, "sha": sha, "rank": self.R1},
                 "2026-27": {"where": w + "2", "sha": "cd" * 32,
                             "rank": self.R2},
                 "2027-28": {"where": w + "3", "sha": "ef" * 32,
                             "rank": self.R2}}
        ranks = {"2025-26": self.R2, "2026-27": None}
        checked = {"2026-27": {(m.ledger_source(w + "2", "2026-27", self.R2),
                                "cd" * 32)}}
        new, revised, skipped = m.plan_periods(pages, ranks, checked)
        self.assertEqual(new, ["2027-28"])
        self.assertEqual(revised, [])
        self.assertTrue(skipped["2025-26"].startswith("older"))
        self.assertTrue(skipped["2026-27"].startswith("unchanged"))
        new, revised, skipped = m.plan_periods(
            pages, ranks, checked, recheck="2026-27", allow_older=True)
        self.assertEqual(revised, ["2025-26", "2026-27"])
        new, revised, skipped = m.plan_periods(pages, ranks, checked,
                                               only="2027-28")
        self.assertEqual((new, revised), (["2027-28"], []))
        self.assertTrue(skipped["2025-26"].startswith("not requested"))
        with self.assertRaises(ValueError):
            m.plan_periods(pages, ranks, checked, recheck="2019-20")


# ---------------------------------------------------------------------------
# The S.114 register (manual input)
# ---------------------------------------------------------------------------

S114_HEADER = ",".join(m.S114_HEADER)
NOTE = ("Northamptonshire County Council, abolished 31 March 2021; retained "
        "against the issuing code.")


def register_text(rows, as_at="2026-10-10"):
    return f"register_as_at,{as_at}\n{S114_HEADER}\n" + "".join(
        r + "\n" for r in rows)


ROW_EVID = ("Croydon,E09000008,2020-11-11,2020-21,Overspend,exact,direct,,,"
            "https://www.croydon.gov.uk/s114.pdf,Section 114 report,2026-10-10")
ROW_BARE = "Woking,E07000217,2023-06-07,2023-24,Risky investments,exact,direct,,,,,"
ROW_PRED = ("Northamptonshire,E10000021,2018-02-02,2017-18,Overspend,exact,"
            f"predecessor,E06000061;E06000062,\"{NOTE}\",,,")
S114_NAMES = {"Croydon": "E09000008", "Woking": "E07000217",
              "North Northamptonshire": "E06000061",
              "West Northamptonshire": "E06000062"}


class S114(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.object(m, "S114_ROOTS", (self.root,))
        p.start()
        self.addCleanup(p.stop)

    def write(self, rows, **kw):
        path = self.root / "la_s114_notices.csv"
        path.write_text(register_text(rows, **kw), encoding="utf-8")
        return path

    def test_reads_rows_and_reports_unevidenced(self):
        reg = m.read_s114(self.write([ROW_EVID, ROW_BARE, ROW_PRED]),
                          boundaries=S114_NAMES)
        self.assertEqual(reg.as_at, date(2026, 10, 10))
        self.assertEqual(len(reg.rows), 3)
        self.assertEqual([r["lad24cd"] for r in reg.unevidenced],
                         ["E07000217", "E10000021"])
        recs = reg.by_period
        self.assertEqual(sorted(recs), ["2017-18", "2020-21", "2023-24"])
        pred = recs["2017-18"][0]
        self.assertEqual(pred["successor_codes"], ["E06000061", "E06000062"])
        self.assertEqual(pred["notice_date"], date(2018, 2, 2))
        self.assertIsNone(pred["evidence_url"])
        croydon = recs["2020-21"][0]
        self.assertEqual(croydon["checked_on"], date(2026, 10, 10))
        self.assertIsNone(croydon["successor_codes"])

    def test_predecessor_without_successors_halts(self):
        bad = ROW_PRED.replace("E06000061;E06000062", "")
        with self.assertRaises(SystemExit):
            m.read_s114(self.write([bad]), boundaries=S114_NAMES)
        nonote = ROW_PRED.replace(f"\"{NOTE}\"", "")
        with self.assertRaises(SystemExit):
            m.read_s114(self.write([nonote]), boundaries=S114_NAMES)

    def test_unknown_date_confirmed_and_bad_rows_halt(self):
        for row in (ROW_BARE.replace("exact", "approx"),
                    ROW_BARE.replace("2023-06-07", "2023-06"),
                    ROW_BARE.replace("E07000217", "E07000999"),
                    ROW_BARE.replace("Woking", "Wokingham"),
                    ROW_BARE.replace(",direct,", ",inherited,"),
                    ROW_EVID.replace("https://www.croydon.gov.uk/s114.pdf",
                                     "https://en.wikipedia.org/wiki/Croydon"),
                    ROW_EVID.replace(",2026-10-10", ",yesterday")):
            with self.subTest(row=row):
                with self.assertRaises(SystemExit):
                    m.read_s114(self.write([row]), boundaries=S114_NAMES)

    def test_a_financial_year_off_its_date_is_a_warning(self):
        row = ROW_BARE.replace("2023-06-07", "2024-06-07")
        reg = m.read_s114(self.write([row]), boundaries=S114_NAMES)
        self.assertEqual(len(reg.warnings), 1)
        self.assertIn("2023-24", reg.warnings[0])

    def test_outside_the_roots_or_legacy_format_halts(self):
        path = self.write([ROW_BARE])
        with mock.patch.object(m, "S114_ROOTS", (self.root / "elsewhere",)):
            with self.assertRaises(SystemExit):
                m.read_s114(path, boundaries=S114_NAMES)
        legacy = self.root / "legacy.csv"
        legacy.write_text("la_name,lad24cd,notice_date,financial_year,reason,"
                          "date_confirmed\nWoking,E07000217,2023-06-07,"
                          "2023-24,Risky investments,exact\n",
                          encoding="utf-8")
        with self.assertRaises(SystemExit):
            m.read_s114(legacy, boundaries=S114_NAMES)


class ContentSha(unittest.TestCase):

    def test_content_sha_ignores_provenance_and_scale(self):
        a = [{"lad24cd": "E09000003", "financial_year": "2025-26",
              "amount_m": Decimal("55.7"), "status": "agreed-in-principle",
              "hra_only": False, "cell_text": "£55.7m",
              "page_updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}]
        b = [dict(a[0], amount_m=Decimal("55.700"), page_updated_at=None)]
        self.assertEqual(m.rows_content_sha(a), m.rows_content_sha(b))
        c = [dict(a[0], hra_only=True)]
        self.assertNotEqual(m.rows_content_sha(a), m.rows_content_sha(c))
        d = [dict(a[0], amount_m=None, status="withdrawn")]
        self.assertNotEqual(m.rows_content_sha(a), m.rows_content_sha(d))


if __name__ == "__main__":
    unittest.main()
