"""The per-dataset Barnsley and Sheffield geography rule (docs/RULES.md rule 4).

On 1 April 2025 (SI 1328/2024) Barnsley E08000016 became E08000038 and
Sheffield E08000019 became E08000039. la_boundaries is LAD May 2024, so the
canonical key stays E08000016/E08000019, and la_code_lookup records the two
rows as change_type = 'recode' (old_code E08000038 -> new_code E08000016).
Publishers differ: some publish the earlier codes, some the new ones, some
switch between periods, and one MHCLG workbook uses both forms on different
sheets of the same release. So each source declares its form here, with the
evidence, and every loader resolves the two areas through this module:

    recode_map, problems = geography.resolve(cur, "15", codes_seen,
                                             by_period=codes_by_period)
    if problems: stop; nothing is loaded

Forms (DATASET_FORM):
  old         publishes E08000016/E08000019
  new         publishes E08000038/E08000039
  mixed       changes between periods or publishers: either form accepted,
              but one period must never carry both forms of one area
  none        no area-level data for these authorities, or keyed by
              something else (BRMA, police force area, provider, location)
  unverified  not yet checked; the evidence says what to check at the next
              load, and the loader prints confirm_note() and goes on

A load whose codes disagree with the declaration stops. The declaration is
then corrected deliberately, with the evidence, in this file; never silently.
When la_boundaries moves to a vintage carrying E08000038/E08000039, the
la_code_lookup rows are reversed and canonical() follows; nothing here
changes except the word "canonical" in the descriptions.

Importing this module needs no database. The authority for the recodes is
la_code_lookup (load_recodes); RECODES_FALLBACK is for pure tests only.
"""
import re

RECODES_FALLBACK = {"E08000038": "E08000016", "E08000039": "E08000019"}

# The two areas: (code in la_boundaries LAD May 2024, code from 1 April 2025).
AREAS = {"Barnsley": ("E08000016", "E08000038"),
         "Sheffield": ("E08000019", "E08000039")}
OLD_CODES = frozenset(old for old, _ in AREAS.values())
NEW_CODES = frozenset(new for _, new in AREAS.values())
FORMS = ("old", "new", "mixed", "none", "unverified")
_DESCRIBE = {"old": "publishes E08000016/E08000019",
             "new": "publishes E08000038/E08000039",
             "none": "no area-level data for these authorities"}

# One entry per source_registry.source_code: (form, evidence). Surveyed
# 2026-10-08, read-only, from the files on disk (data/raw, data/reference),
# the database (publisher-code columns where a table keeps them), the
# registry, the decision notes and the loaders. Detail and the method:
# docs/decisions/2026-10-08-barnsley-sheffield-rule.md.
DATASET_FORM = {
    "1": ("mixed",
          "MHCLG Detailed LA workbooks in data/raw/s1b_a3: 2023Q2-2024Q4 "
          "carry E08000016/19 only; 2025Q1-2025Q4 carry E08000038/39 on the "
          "three sheets S1 reads (A1, TA1, A3). The same releases carry "
          "E08000016/19 on other sheets (A7 in 202509 and 202603; TA3, TA4, "
          "TA4c, TA4s in 202603), so a loader reading those sheets must check "
          "each sheet separately."),
    "10": ("new",
           "Rough sleeping snapshot autumn 2025 (data/reference .ods and "
           "rough_sleeping_snapshot_2025*.csv) carries E08000038/39 only; "
           "the 2026-08-14 scan found la_rough_sleeping on the new codes "
           "only before resolution."),
    "11": ("none",
           "CQC locations carry no GSS codes for these authorities: the four "
           "Care directory with filters files of July, August, September and "
           "October 2026 (data/raw/*_HSCA_Active_Locations.ods, read "
           "2026-10-09) carry none of the four codes in any cell (their only "
           "GSS-like codes are CCG E38...), and scripts/s11_cqc_editions.py "
           "halts on a file that does. lad24cd is assigned by "
           "point-in-polygon against la_boundaries. The postcodes.io "
           "fallback is a second source and returns current codes "
           "(E08000038/39); they resolve through geography.canonical, then "
           "la_boundaries, then la_code_lookup."),
    "12": ("none",
           "No EFS or S.114 row for Barnsley or Sheffield in la_efs_support, "
           "la_s114_notices or data/reference/la_s114_notices.csv; if either "
           "appears, the load stops and the form is declared from that "
           "file."),
    "13": ("old",
           "data/reference/LAHS_open_data_1978-79_to_2024-25.csv and "
           "lahs_waiting_list_2015_2025.csv carry E08000016/19 only, "
           "including 2024-25."),
    "14": ("none",
           "Keyed by BRMA name; lad24cd comes from la_brma_mapping, built "
           "from BRMA polygons."),
    "15": ("new",
           "Average-prices-2026-07.csv and Average-prices-Property-Type-"
           "2026-07.csv (data/raw, read 2026-10-08) carry E08000038/39 for "
           "every month 1995-01 to 2026-07 and never E08000016/19: each "
           "edition republishes the whole back series on current codes."),
    "17": ("none",
           "Keyed by police force area; data/reference/marac_data_2018_2025."
           "csv carries none of the four codes; lad24cd via la_pfa_mapping."),
    "18": ("new",
           "PIPR editions pipr_17june2026, pipr_22july2026, "
           "pipr_16september2026 (data/raw) carry E08000038/39 only, for "
           "the whole back series (docs/s18_pipr_workbook_structure.md)."),
    "19": ("old",
           "Stat-Xplore PIP geography valueset V_C_MASTERGEOG21_LA_TO_REGION "
           "(s19_cache/discovery.json, 2026-10-01) lists Barnsley and "
           "Sheffield as E08000016/19 and has no E08000038/39 member."),
    "1b": ("mixed",
           "la_homelessness_support_needs_editions.publisher_la_code: "
           "E08000016/19 for 2023Q2-2024Q4, E08000038/39 for 2025Q1-2025Q4, "
           "never both in one period; the A3 sheets in data/raw/s1b_a3 "
           "agree."),
    "2": ("mixed",
          "RO4 2024-25 second and third releases (data/reference .ods) carry "
          "E08000016/19 only; the 2025-26 first release carries "
          "E08000038/39 only. One form per financial year."),
    "20": ("none",
           "Withheld source (commercial in confidence), keyed by the "
           "supplier's own area names; it carries none of the four codes "
           "and no loader resolves publisher codes for it."),
    "21": ("new",
           "data/raw/ons_statistical_neighbours_2026.xlsx (Mar-2026 "
           "edition) carries E08000038/39 only (registry known_gotchas "
           "agrees)."),
    "22": ("mixed",
           "Read 2026-10-09 from the files in data/raw/s22_ctb: the Council "
           "Taxbase 2025 local authority level workbook "
           "(2025_Local_Authority_Drop_Down.xlsx) carries E08000038/39 only; "
           "Live Table 615 (Live_Table_615.ods, 2004 to 2025) lists all four "
           "codes, but the codes with numbers are E08000016/19 for "
           "2004-2024 and E08000038/39 for 2025 (each published [x] in the "
           "other years), one form per year. scripts/s22_ctb_editions.py "
           "passes only the codes with a number in each year."),
    "23": ("old",
           "Read 2026-10-09: RP_COMBINED_TOOL_2025_FINAL_V1.1.xlsx "
           "(data/raw/s23_rsh, stock date 2025-03-31) carries Barnsley and "
           "Sheffield as E08000016/19 only, on both the LA subtotal rows and "
           "the provider rows of STOCK_BY_LA; rsh_rp_stock_by_la."
           "publisher_la_code agrees. The 2026 file (stock date 2026-03-31, "
           "after 1 April 2025) may carry E08000038/39: then "
           "scripts/s23_rsh_stock_editions.py stops, and this becomes "
           "'mixed' with that file as evidence (one form per stock date)."),
    "24": ("none",
           "RSH register has no geography (registry caveat); the register "
           "and judgements files carry none of the four codes."),
    "3": ("old",
          "data/raw/s3_mye/mye25tablesew.xlsx (mid-2025, built on 2023 LA "
          "boundaries) carries E08000016/19 only (METHODOLOGY, S3; registry "
          "known_gotchas)."),
    "3b": ("unverified",
           "Census 2021 TS054 via NOMIS: la_tenure_2021 holds E08000016/19 "
           "and no recode is recorded, but no source file is on disk. At "
           "the next load, record the codes NOMIS returns for Barnsley and "
           "Sheffield."),
    "4": ("old",
          "The five DfE care leaver LA files in data/raw/s4_cla (read "
          "2026-10-09: 17-21 accommodation of the 2023, 2024 and 2025 "
          "releases, 22-25 suitability of the 2024 and 2025 releases; "
          "reporting years 2019-2025) carry E08000016/19 only in "
          "new_la_code. The November 2026 release (reporting year 2026, "
          "after 1 April 2025) may switch to E08000038/39, possibly "
          "back-applied as North Yorkshire was; scripts/"
          "s4_care_leaver_editions.py then stops and this is corrected "
          "deliberately (new or mixed)."),
    "5": ("old",
          "File_10_-_IoD2025_Local_Authority_District_Summaries__lower-tier__"
          "v2.xlsx and imd_2025_la_summary.csv (data/reference) carry "
          "E08000016/19 only."),
    "6": ("unverified",
          "Asy_D11 and Reg_02 carry E08000038/39 (docs/s6_asylum_source.md: "
          "the two recodes resolve forward), but no file is on disk and "
          "la_asylum_support keeps no publisher code, so whether earlier "
          "periods carry E08000016/19 is not known. At the next load, list "
          "the codes by period_ending and record new or mixed."),
    "7": ("old",
          "la_boundaries is LAD May 2024 and holds E08000016/19; this "
          "source defines the canonical key."),
    "8": ("unverified",
          "Superseded by 8b; no loader runs. If it is revived, check the "
          "Stat-Xplore HB geography valueset for Barnsley and Sheffield."),
    "8b": ("unverified",
           "Stat-Xplore HB admin LA valueset (V_C_ADMIN_LA) members are not "
           "kept on disk; resolve_geography maps either form through "
           "la_code_lookup. At the next load, record which codes the "
           "valueset lists for Barnsley and Sheffield."),
    "9a": ("old",
           "All 27 DRD monthly webfiles in data/raw/s9a_drd (April 2024 to "
           "July 2026) carry E08000016/19 only (UTLA codes)."),
    "9b": ("mixed",
           "The form is per file, not per month boundary (read 2026-10-09 "
           "from the files in data/raw/s9b_mhsds and scripts/verify/src, "
           "and the March-June 2025 Performance files): Performance files "
           "carry E08000016/19 to May 2025 and E08000038/39 from June 2025; "
           "the year-end Final files carry E08000016/19 for April 2023 to "
           "March 2025 and E08000038/39 from April 2025 (FY2025-26), so a "
           "Final file for April or May 2025 differs in form from the "
           "Performance file it replaces. No file carries both forms of "
           "an area."),
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def _table(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"not a table name: {name!r}")
    return name


def load_recodes(cur, *, lookup="public.la_code_lookup",
                 boundaries="public.la_boundaries") -> dict:
    """{publisher code: canonical code} from la_code_lookup's
    change_type = 'recode' rows (E08000038 -> E08000016, E08000039 ->
    E08000019), with each canonical code mapped to itself. ValueError if
    there are no recode rows, a target is not in la_boundaries, or a code has
    two targets. lookup and boundaries name the tables (tests pass throwaway
    ones)."""
    cur.execute(f"SELECT old_code, new_code FROM {_table(lookup)} "
                "WHERE change_type = 'recode' ORDER BY old_code, new_code")
    rows = cur.fetchall()
    cur.execute(f"SELECT lad24cd FROM {_table(boundaries)}")
    valid = {r[0] for r in cur.fetchall()}
    if not rows:
        raise ValueError(f"{lookup} holds no change_type = 'recode' rows; "
                         "the Barnsley and Sheffield recodes are missing")
    out = {}
    for old, new in rows:
        if new not in valid:
            raise ValueError(f"recode target {new} (from {old}) is not in "
                             f"{boundaries}")
        if out.get(old, new) != new:
            raise ValueError(f"{old} has two recode targets: {out[old]} and "
                             f"{new}")
        out[old] = new
    for new in set(out.values()):
        out[new] = new
    return out


def canonical(code: str, recodes: dict) -> str:
    """The canonical code for a publisher code; any other code unchanged."""
    return recodes.get(code, code)


def _form(source_code):
    try:
        return DATASET_FORM[source_code][0]
    except KeyError:
        raise KeyError(f"source {source_code!r} has no DATASET_FORM entry in "
                       "scripts/geography.py; declare its form with evidence "
                       "before loading") from None


def check_forms(source_code, codes_seen, *, by_period=None) -> list:
    """Problems with the Barnsley and Sheffield codes a load sees (empty list
    = pass). codes_seen: the codes in the data; by_period: {period: codes}
    (also counted as seen). old seeing a new code, new seeing an old code, or
    none seeing either is a problem naming the codes and the declaration;
    mixed fails only on a period holding both forms of one area; unverified
    never fails (print confirm_note). KeyError for an undeclared source."""
    form = _form(source_code)
    by_period = by_period or {}
    seen = set(codes_seen)
    for codes in by_period.values():
        seen |= set(codes)
    fix = ("; nothing is loaded. If the publisher has changed, correct "
           "DATASET_FORM in scripts/geography.py deliberately, with the "
           "evidence")
    bad = {"old": NEW_CODES, "new": OLD_CODES,
           "none": OLD_CODES | NEW_CODES}.get(form)
    if bad is not None:
        hit = sorted(seen & bad)
        if hit:
            return [f"source {source_code!r} is declared {form!r} "
                    f"({_DESCRIBE[form]}) but the data carries "
                    f"{', '.join(hit)}{fix}"]
        return []
    problems = []
    if form == "mixed":
        for period in sorted(by_period, key=str):
            codes = set(by_period[period])
            for name, (old, new) in AREAS.items():
                if old in codes and new in codes:
                    problems.append(
                        f"source {source_code!r} (declared 'mixed') period "
                        f"{period} carries both {old} and {new} for {name}; "
                        "one period must never carry both forms; nothing is "
                        "loaded")
    return problems


def confirm_note(source_code):
    """The one-line note a loader prints for an unverified source (None for a
    verified one)."""
    form = _form(source_code)
    if form != "unverified":
        return None
    return (f"NOTE: source {source_code!r} Barnsley/Sheffield form is "
            f"unverified; confirm and record it in scripts/geography.py "
            f"({DATASET_FORM[source_code][1]})")


def resolve(cur, source_code, area_codes, *, by_period=None) -> tuple:
    """The one call a loader makes: ({publisher code: canonical code} for
    the recoded areas among area_codes (and by_period), problems from
    check_forms). Codes outside the recode pairs are not in the map; they go
    through the loader's ordinary la_boundaries / la_code_lookup check. A
    non-empty problems list means the load stops."""
    _form(source_code)  # KeyError before touching the database
    codes = set(area_codes)
    for cs in (by_period or {}).values():
        codes |= set(cs)
    recodes = load_recodes(cur)
    problems = check_forms(source_code, area_codes, by_period=by_period)
    return {c: recodes[c] for c in sorted(codes) if c in recodes}, problems


def source_sort_key(code: str):
    """'1' < '1b' < '2' < ... < '10'."""
    m = re.match(r"(\d+)(.*)", code)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, code)


def declaration_report() -> str:
    """DATASET_FORM as a Markdown table (for RULES.md and the decision
    note)."""
    lines = ["| Source | Form | Evidence |", "|---|---|---|"]
    for code in sorted(DATASET_FORM, key=source_sort_key):
        form, evidence = DATASET_FORM[code]
        lines.append(f"| {code} | {form} | {evidence.replace('|', '/')} |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(declaration_report())
