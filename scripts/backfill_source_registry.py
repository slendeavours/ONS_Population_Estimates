"""Build the source refresh control layer and populate it from the repository.

Applies sql/source_registry.sql, backfills pipeline_run_log.source_code where
it can be established, upserts one source_registry row per source registered
in docs/METHODOLOGY.md, and writes docs/SOURCE_REGISTRY_GAPS.md.

Two rules govern every value below.

  1. Every field is derived from this repository — METHODOLOGY.md, the source
     documentation files, the node documentation files and the build scripts.
     Nothing is derived from general knowledge of the publisher.

  2. Where the repository does not document a field, the value is NULL. A
     visible null is honest; an inferred value is not. The gap report is the
     work list for closing them.

Three NOT NULL columns cannot hold a null, so they carry a documented cautious
default instead: acquisition_method 'manual' and refresh_tier 'C' where the
acquisition mechanics are undocumented. Every row that relies on that default
says so in completeness_note, and the gap report lists them separately from
genuine nulls.

Idempotent. Re-running upserts the same rows and never overwrites a non-null
value with a null, so hand-edited fields survive.

Usage:
    python scripts/backfill_source_registry.py            # build + backfill
    python scripts/backfill_source_registry.py --log-run  # also log the run
"""
import argparse
import re
import sys
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Windows consoles default to cp1252; this output contains em dashes and
# arrows lifted verbatim from the documentation.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DDL_PATH = REPO / "sql" / "source_registry.sql"
GAP_REPORT = REPO / "docs" / "SOURCE_REGISTRY_GAPS.md"

# scripts/register_lib.py is deliberately not imported. Its module-level
# PUBLISH constant resolves to REPO/"ONS_Population_Estimates", which is
# correct only when it runs from the outer working copy and is wrong inside
# this published checkout. The register parser below resolves METHODOLOGY.md
# under either layout instead.

# The run-log source_number reserved for this build. '0' is already taken by
# the report generators and 'w1' by the workflow orchestration, so REGISTRY
# takes its own non-numeric key rather than colliding with either.
REGISTRY_SOURCE_NUMBER = "REG"
REGISTRY_SOURCE_CODE = "REGISTRY"
AGENT_NAME = "Source Registry Build"

# Statuses in pipeline_run_log that mean the run succeeded. Both appear in the
# live log. vw_source_due uses the same list; verify_source_registry.py fails
# if a status outside the full known vocabulary appears.
SUCCESS_STATUSES = ("success", "complete")
KNOWN_STATUSES = ("success", "complete")

# Run-log keys that are not sources. They are agents: the report generators
# and the Workflow 1 orchestration itself.
NON_SOURCE_RUN_KEYS = {"0", "w1"}

CAUTIOUS = ("Acquisition mechanics are not documented in the repository. "
            "acquisition_method 'manual' and refresh_tier 'C' are the "
            "cautious defaults, not documented findings.")

# ---------------------------------------------------------------------------
# The registry, derived from the repository. Provenance for each source is the
# file named in source_doc_path / node_docs_path / build_script_path, plus the
# register table and the standing rules in docs/METHODOLOGY.md.
#
# natural_key is taken from the live PRIMARY KEY of target_table, verified
# against pg_constraint rather than transcribed from prose.
# ---------------------------------------------------------------------------
SOURCES = [
    dict(
        source_code="1",
        source_name="DLUHC H-CLIC",
        publisher="DLUHC",
        acquisition_method="manual",
        known_gotchas=(
            "Per-quarter file URLs are curated by hand in the "
            "homelessness_quarter_urls table; GOV.UK asset URLs differ per "
            "quarter and again when a quarter is revised."),
        cadence="quarterly", cadence_months=3,
        target_table="la_statutory_homelessness", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        caveats=["H-CLIC is quarterly. The pipeline uses the most recent "
                 "quarter end, which may vary by LA submission."],
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="2",
        source_name="MHCLG RO4",
        publisher="MHCLG",
        acquisition_method="manual",
        cadence="annual", cadence_months=12,
        target_table="ro4_housing_expenditure", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="3",
        source_name="ONS Mid-Year Estimates",
        publisher="ONS",
        series_name="Estimates of the population for England and Wales",
        landing_page_url=(
            "https://www.ons.gov.uk/peoplepopulationandcommunity/"
            "populationandmigration/populationestimates/datasets/"
            "estimatesofthepopulationforenglandandwales"),
        api_endpoint=(
            "https://www.ons.gov.uk/peoplepopulationandcommunity/"
            "populationandmigration/populationestimates/datasets/"
            "estimatesofthepopulationforenglandandwales/data"),
        acquisition_method="landing_page",
        known_gotchas=(
            "No file URL is stored. The edition is resolved from the dataset "
            "landing page at run time, which also re-confirms the publication "
            "date independently of anything written in this repository. "
            "Sheet 'MYE2 - Persons', header row 8, 'All ages' column. The "
            "release covers 318 England-and-Wales local authorities; England "
            "is filtered on the code prefix (E06/E07/E08/E09), never assumed "
            "to be the whole file. Where a release states its boundary "
            "vintage that is the predictor of which Barnsley and Sheffield "
            "codes appear, not the publication date: the mid-2025 edition is "
            "built on 2023 local authority boundaries and publishes "
            "E08000016 and E08000019 despite postdating the recode."),
        cadence="annual", cadence_months=12,
        target_table="la_population", geography_level="LAD24",
        build_script_path="scripts/s3_mye_refresh.py",
        n8n_workflow_name="Workflow 1",
        caveats=[
            "la_population is multi-year. Its key was widened from (lad24cd) "
            "to (lad24cd, reference_year) and prior vintages are retained, so "
            "every join must pin the vintage or the join fans out.",
            "E10/E11 counties, E12 regions and the E92 country row are "
            "excluded as aggregates that would double count."],
        completeness_note=(
            "Refreshed to mid-2025 on 2026-08-13. 592 rows, 296 per year, "
            "England total 58,834,812 reconciling exactly to the publisher's "
            "own England row."),
        latest_period_loaded="Mid-2025: 2023 local authority boundaries",
        refresh_tier="B", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="3b",
        source_name="Census 2021 TS054",
        publisher="ONS",
        acquisition_method="manual",
        cadence="periodic", cadence_months=120,
        publication_window="Decennial",
        target_table="la_tenure_2021", geography_level="LAD24",
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="4",
        detected_period_type="reference_period",
        revises_back_series=True,
        revision_note=("Each annual release republishes several prior years "
            "with revisions applied, so the newer edition must be loaded "
            "first. The pre-2026-08-20 build deduplicated first-occurrence-"
            "wins with the older file at index 0 and retained superseded "
            "figures for 7 rows across 2020-2023."),
        source_name="DfE Children Looked After (SSDA903) care leaver accommodation",
        publisher="DfE",
        series_name=("Children looked after in England including adoptions: "
                     "care leaver activity and accommodation"),
        landing_page_url=("https://explore-education-statistics.service.gov.uk/"
                          "find-statistics/children-looked-after-in-england-"
                          "including-adoptions"),
        acquisition_method="api",
        known_gotchas=(
            "A new EES dataset UUID is issued per release for the 17-21 "
            "accommodation file; the 22-25 suitability file is persistent. "
            "The 2025 release renamed columns from age/accommodation_type/"
            "number to care_leaver_age/breakdown/care_leaver_count. Code "
            "reading the old names against the new file matches no category, "
            "routes every row to 'other' and returns zero for every bucket "
            "WITHOUT raising an error, so any edition change needs a column-"
            "name assertion that fails loudly. EES exposes no working content "
            "API for this publication and the data catalogue is a JavaScript "
            "app, so UUIDs must be taken from the release data guidance page."),
        cadence="annual", cadence_months=12,
        publication_window="November, for the reporting year ending 31 March",
        target_table="care_leaver_accommodation",
        geography_level="UTLA",
        join_path=("lad24cd via la_code_lookup for unitary and metropolitan "
                   "authorities. County councils have no LAD24 successor and "
                   "are carried on their own E10 code; they do not join "
                   "la_boundaries."),
        node_docs_path="docs/nodes/s4_node1_fetch_1721_2023.md",
        source_doc_path="docs/s4_care_leaver_source.md",
        verification_checks={
            "rebuild_date": "2026-08-20",
            "replication": ("808 of 815 rows reproduced exactly under the "
                            "documented bucketing rule against source CSVs"),
            "replication_exceptions": ("7 rows, all overlapping years where "
                                       "the older edition had been retained"),
            "published_column": ("semi_independent_published matches DfE at "
                                 "155/155 authorities for reporting year "
                                 "2025, zero mismatches"),
        },
        caveats=["semi_independent is a pipeline aggregate of three DfE "
                 "categories (semi-independent transitional, foyers, "
                 "supported lodgings) and is not DfE's published category. "
                 "External documents must quote semi_independent_published.",
                 "From reporting year 2024 the DfE category means Ofsted-"
                 "registered supported accommodation only; before 2024 it "
                 "included unregistered provision. Counts must not be "
                 "trended across that boundary.",
                 "Suppressed cells (c/k/z/x) are added as zero on the 17-21 "
                 "path, so bucket counts and total_care_leavers are minima. "
                 "total_published carries DfE's own Total row.",
                 "DfE publishes 155 upper-tier authorities including 24 "
                 "county councils. Until 2026-08-20 an inner join on "
                 "la_code_lookup dropped all 24 counties silently, so England "
                 "totals and national ranks were computed over 132 of 155.",
                 "Point-in-time count at 31 March, not a flow. Annual need is "
                 "higher.",
                 "The 22-25 cohort covers only those who contacted the "
                 "authority and requested support, so figures are partial."],
        ucws_lens="context", hss_lens="primary",
        completeness_note=(
            "155 of 155 upper-tier authorities for reporting years 2019-2025 "
            "(17-21 accommodation), including the 24 county councils restored "
            "on 2026-08-20. 22-25 suitability covers 2023-2025."),
        metrics=["Care leavers in supported accommodation (published DfE "
                 "category)",
                 "Care leavers in supported accommodation (wider pipeline "
                 "aggregate)"],
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="5",
        source_name="MHCLG IMD",
        publisher="MHCLG",
        acquisition_method="manual",
        cadence="periodic", cadence_months=60,
        publication_window="Every ~5 years",
        target_table="la_imd_2025", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        caveats=["IMD 2025 is used: MHCLG English Indices of Deprivation "
                 "2025, File 10 Local Authority District Summaries "
                 "(lower-tier) v2, published 30 October 2025. Verified "
                 "against the published file at 296 of 296 authorities on "
                 "2026-08-20. An earlier caveat stating that IMD 2019 was "
                 "used was wrong and understated the data actually held.",
                 "imd_rank_of_average_rank ranks LAs from 1 (most deprived) "
                 "to 296 (least deprived) on the average rank of constituent "
                 "LSOAs."],
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="6",
        revises_back_series=True,
        revision_note=("The Home Office has revised these tables historically: "
            "accommodation type in June 2024, geographic distribution in "
            "August 2024, accommodation types again in November 2025. "
            "Each load is a full replace of the periods it covers."),
        source_name="Home Office Asy_D11 / Reg_02",
        publisher="Home Office / MHCLG",
        series_name=("Immigration system statistics, quarterly release "
                     "(Asy_D11); Regional and local authority data on "
                     "immigration groups (Reg_02)"),
        landing_page_url=("https://www.gov.uk/government/statistical-data-sets/"
                          "immigration-system-statistics-data-tables"),
        acquisition_method="landing_page",
        known_gotchas=(
            "Download URLs are discovered from the landing page at run time. "
            "GOV.UK asset URLs change with every release, so none are "
            "hardcoded. Reg_02 has its own landing page: "
            "https://www.gov.uk/government/statistical-data-sets/"
            "immigration-system-statistics-regional-and-local-authority-data. "
            "S6 uses a build-local resolution layer for three codes that "
            "la_code_lookup handled wrongly or not at all (E07000027, "
            "E07000028, E07000189); the workaround was retired on 2026-07-26 "
            "once the lookup was corrected."),
        cadence="quarterly", cadence_months=3, expected_lag_days=56,
        publication_window="Quarterly, roughly 8 weeks after quarter end",
        target_table="la_asylum_support", geography_level="LAD24",
        join_path=("Code-first cascade: direct match against la_boundaries, "
                   "then forward via la_code_lookup. Name-based matching is "
                   "never reached."),
        build_script_path="s6_asylum_build.py",
        node_docs_path="docs/nodes/s6_node1..s6_node9",
        source_doc_path="docs/s6_asylum_source.md",
        verification_checks={"script": "s6_asylum_verify.py", "checks": 13,
                             "gate": "any failure rolls the whole transaction "
                                     "back and exits non-zero"},
        caveats=[
            "Figures are based on the registered address of the person, which "
            "is not necessarily where they regularly reside.",
            "Unaccompanied asylum-seeking children are excluded. UASC are "
            "supported by local authority children's services, not Home "
            "Office asylum support. This is not a count of all asylum seekers "
            "in an area.",
            "An absent LA means 'not published', not 'none'. Zeros are never "
            "published, so coverage is reported as a count and never gated "
            "against 296.",
            "The Home Office has revised these tables historically. Each load "
            "is a full replace of the periods it covers; do not assume prior "
            "periods are immutable.",
            "The published 'UK Region / Nation' column is unreliable — five "
            "LAD codes are assigned to more than one region across the "
            "window. It is not stored; region is derived from la_boundaries.",
            "Data is extracted on the last day of the quarter, or the closest "
            "possible date, and can change daily. Treat as provisional."],
        completeness_note=(
            "Two structural breaks make the England series non-comparable "
            "before 2025-03-31; they are recorded in asylum_series_breaks. "
            "Standalone: not wired into Workflow 1, adds no "
            "staging_la_signals column, no tenant type and no map layer. "
            "Additional tables: la_asylum_support_unallocated, "
            "asylum_support_non_england, la_immigration_groups, "
            "asylum_series_breaks."),
        latest_period_loaded="Year ending March 2026 (2026-03-31)",
        refresh_tier="B", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="7",
        source_name="ONS Open Geography Portal",
        publisher="ONS",
        series_name="Local Authority Districts (May 2024) Boundaries UK BGC",
        acquisition_method="manual",
        known_gotchas=(
            "la_boundaries is LAD May 2024 BGC and carries E08000016 and "
            "E08000019 for Barnsley and Sheffield. The 1 April 2025 recode "
            "(SI 1328/2024) means sources published after that date may use "
            "E08000038 and E08000039 instead, and must resolve through "
            "la_code_lookup as change_type = 'recode'."),
        cadence="periodic",
        publication_window="On boundary changes",
        target_table="la_boundaries", geography_level="LAD24",
        caveats=["Simplified to approximately 20% of original vertex count "
                 "for web performance.",
                 "England only — 296 districts, unitary authorities and "
                 "metropolitan boroughs."],
        completeness_note=(
            "The boundary vintage was settled from load provenance, not "
            "inferred: la_boundaries.source_date is 2024-05-01 for all 296 "
            "rows and the S7 run log note reads 'LA boundaries loaded — May "
            "2024 BGC — England only'. The code list is identical across "
            "LAD24 vintages, so the loaded data cannot adjudicate it alone. "
            + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="8",
        source_name="DWP STAT-Xplore",
        publisher="DWP",
        acquisition_method="manual",
        cadence="monthly", cadence_months=1,
        target_table="la_hb_sa_caseload", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        completeness_note=(
            "No source documentation file and no build script exist for S8. "
            "The register names STAT-Xplore as the platform, but how this "
            "pipeline acquires the extract is not documented. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="8b",
        revises_back_series=True,
        revision_note=(
            "DWP revises the HB caseload in place and publishes no "
            "revision note that any check surfaces. Proven 2026-08-14: "
            "202511 SA moved on 285 of 296 LAs between the S8 load on "
            "2026-04-01 and the S8b load on 2026-07-22 - Birmingham "
            "31,117 to 34,101, +9.6% - and a live probe confirmed the "
            "API now returns the later figures. This is not a blanket "
            "Stat-Xplore property: S19 PIP Apr-26 reproduced exactly, "
            "296 of 296, delta 0.000%. The two tests are not "
            "like-for-like, though - HB was compared over four months "
            "and PIP over one - so PIP is left unflagged rather than "
            "asserted not to revise."),
        source_name="DWP Stat-Xplore HB (accommodation type)",
        publisher="DWP",
        series_name=("Housing Benefit caseload (str:database:hb_new), "
                     "Accommodation Type field "
                     "str:field:hb_new:V_F_HB_NEW:SATA"),
        api_endpoint="https://stat-xplore.dwp.gov.uk/webapi/rest/v1",
        acquisition_method="api",
        auth_required=True, auth_env_var="StatXplore_API_Key",
        known_gotchas=(
            "The DWP release note described this breakdown as quarterly; "
            "Stat-Xplore returns monthly granularity, so the implied refresh "
            "cadence is monthly, not quarterly. The geography valueset has "
            "372 members, 316 English, resolved to 296 current LAD24CD "
            "codes. The build recorded E07000028 and E07000189 as "
            "unresolvable; both now resolve after the 2026-07-26 "
            "la_code_lookup correction, and both were verified on "
            "2026-08-14 to carry zero claimants in every month loaded, "
            "so nothing was lost to them."),
        cadence="monthly", cadence_months=1,
        target_table="la_hb_accom_type_caseload", geography_level="LAD24",
        build_script_path="scripts/s8b_hb_accom_type_build.py",
        source_doc_path="outputs/s8b_source_summary.md",
        caveats=["Birmingham differs by 9.6% from la_hb_sa_caseload for "
                 "Nov-25, attributable to retrospective revisions applied by "
                 "DWP.",
                 "The UNKNOWN category is negligible (24-68 nationally) and "
                 "declining."],
        completeness_note=(
            "The source documentation file lives in outputs/, outside the "
            "published tree, so it is not available to readers of this "
            "repository."),
        latest_period_loaded="202602",
        refresh_tier="A", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="9a",
        latest_period_loaded="2026-06-01",
        build_script_path="scripts/s9a_drd_build.py",
        verification_checks={"method": "exact reproduction",
                             "rows": 3978,
                             "reproduced": "2026-08-14",
                             "cell_differences": 0},
        detected_period_type="reference_period",
        revises_back_series=True,
        revision_note=("NHS England revises DRD in annual waves and announces them on "
            "the publication page. April 2025 to March 2026 inclusive "
            "were revised and republished on 9 July 2026; April 2024 to "
            "April 2025 on 10 July 2025. Revised files carry a -Revised "
            "filename suffix, so a revision is detectable from the link "
            "list without downloading anything."),
        source_name="NHS DRD monthly",
        publisher="NHSE",
        series_name="Discharge Ready Date (DRD) monthly data, acute",
        landing_page_url=("https://www.england.nhs.uk/statistics/"
                          "statistical-work-areas/discharge-delays/"
                          "discharge-ready-date/"),
        api_endpoint="https://www.gov.uk/api/search.json",
        acquisition_method="landing_page",
        known_gotchas=(
            "File URLs follow the pattern .../Discharge-Ready-Date-monthly-"
            "data-webfile-MonthName-YYYY[-Revised].xlsx but must be "
            "discovered from the live page, never constructed — URLs include "
            "a publication-date path component that varies. "
            "Corrected 2026-08-14: this was registered against the acute "
            "discharge sitrep page, which is a different publication and "
            "stops at September 2024. DRD has its own page. The 2026-07-13 "
            "load used the correct files despite the node documentation "
            "naming the sitrep page, so this was a documentation defect, "
            "not a data defect. Detection now runs through the GOV.UK "
            "search API — each month is published as an official statistic "
            "titled 'Timeliness of Acute Hospital Discharges (Discharge "
            "Ready Date) for {Month} {Year}' — while the NHS England page "
            "remains the file source."),
        cadence="monthly", cadence_months=1,
        target_table="nhs_drd_discharge_delays", geography_level="UTLA",
        join_path=("utla_lad_mapping, a population-weighted UTLA to LAD "
                   "crosswalk; exposed at LAD level as "
                   "vw_drd_discharge_delays_lad."),
        node_docs_path="docs/nodes/s9a_node1..s9a_node3",
        n8n_workflow_name="Workflow 1",
        caveats=["DRD % and average columns are UTLA-level pass-through for "
                 "county districts. All E07 districts under an E10 county "
                 "inherit the same value."],
        refresh_tier="B", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="9b",
        latest_period_loaded="2026-06-01",
        build_script_path="scripts/s9b_crfd_build.py",
        verification_checks={"method": "exact reproduction",
                             "rows": 11248, "periods": 38,
                             "reproduced": "2026-08-14",
                             "cell_differences": 0},
        detected_period_type="reference_period",
        source_name="MHSDS MHS26",
        publisher="NHS Digital",
        series_name="Mental Health Services Monthly Statistics, measure MHS26",
        landing_page_url=("https://digital.nhs.uk/data-and-information/"
                          "publications/statistical/"
                          "mental-health-services-monthly-statistics"),
        acquisition_method="landing_page",
        known_gotchas=(
            "Publication pages use the slug "
            "performance-{month}-{year}; pre-October 2023 publications use "
            "performance-{month}-provisional-{next_month}-{year}. The data "
            "file download URL is on files.digital.nhs.uk with unpredictable "
            "hash paths and must be discovered from the publication page "
            "HTML, never constructed. File naming varies by period: CSV with "
            "a version suffix (Apr-Aug 2023), plain CSV (Sep-Dec 2023), ZIP "
            "containing a CSV (Jan 2024 onward)."),
        cadence="monthly", cadence_months=1,
        target_table="nhs_mh_crfd", geography_level="LAD24",
        join_path="Direct LA level; exposed as vw_mh_crfd_lad.",
        node_docs_path="docs/nodes/s9b_node1..s9b_node3",
        n8n_workflow_name="Workflow 1",
        caveats=["MHS26 covers MH and LD/autism combined. No disaggregated "
                 "source is available, so the mental_health and "
                 "learning_disability tenant types share the same signal.",
                 "28-46% of LAs have suppressed MHS26 values per month. Those "
                 "LAs are excluded from tenant-type rankings."],
        refresh_tier="B", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="10",
        source_name="DLUHC Rough Sleeping Snapshot",
        publisher="DLUHC",
        acquisition_method="manual",
        cadence="annual", cadence_months=12,
        publication_window="autumn",
        target_table="la_rough_sleeping", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        caveats=["The rough sleeping count is a single-night snapshot. Actual "
                 "levels may be significantly higher."],
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="11",
        detected_period_type="reference_period",
        source_name="CQC Care directory with filters",
        publisher="CQC",
        series_name="HSCA Active Locations",
        landing_page_url="https://www.cqc.org.uk/about-us/transparency/using-cqc-data",
        acquisition_method="landing_page",
        known_gotchas=(
            "The download URL is extracted from the CQC data page on every "
            "run, never hardcoded: CQC is migrating its directory to a new "
            "digital system and the file URL moves with each edition. The "
            "link pattern is an href whose filename contains "
            "HSCA_Active_Locations, in .ods or .xlsx — both formats have been "
            "published historically. The ODS route cannot use odfpy: "
            "content.xml is roughly 440 MB uncompressed and odfpy loads it "
            "whole (MemoryError), so the script stream-parses the zip entry "
            "with xml.etree.iterparse."),
        cadence="monthly", cadence_months=1,
        target_table="cqc_locations", geography_level="entity",
        join_path=("Location postcodes resolved to lad24cd in "
                   "scripts/s11_cqc_map.py, then aggregated to LA for "
                   "staging_la_signals."),
        build_script_path="scripts/s11_cqc_fetch.py",
        node_docs_path="docs/nodes/s11_node1..s11_node7",
        verification_checks={"script": "scripts/s11_cqc_verify.py"},
        n8n_workflow_name="Workflow 1",
        completeness_note=(
            "S11 is the pipeline's only supply-side source: every other "
            "source measures need, S11 records existing CQC-registered "
            "provision. It is stored agnostically; the pipeline does not "
            "score or rank markets."),
        latest_period_loaded="2026-07-01",
        refresh_tier="B", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="12",
        source_name="MHCLG EFS / published S.114 notices",
        publisher="MHCLG / LAs",
        acquisition_method="manual",
        cadence="periodic",
        publication_window="Published as issued",
        target_table="la_efs_support", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        caveats=["S.114 notices are held in a second table, la_s114_notices, "
                 "keyed (lad24cd, notice_date).",
                 "Notices are attributed to the authority that issued them and "
                 "are never propagated to successors. la_s114_notices.attribution "
                 "is 'direct' where the issuer still exists (13 notices, 10 "
                 "authorities) and 'predecessor' where it does not (2, both "
                 "Northamptonshire County Council E10000021, abolished 31 March "
                 "2021). In 2018 Northamptonshire was two-tier: the county held "
                 "social care and education while housing and homelessness sat "
                 "with the seven districts, so the issuer did not hold the "
                 "functions this source signals about. Propagating would also "
                 "fan out - one predecessor, two successors, doubling the notice "
                 "on any join by predecessor. successor_codes is reference only "
                 "and must never be used as a join path; North and West "
                 "Northamptonshire correctly carry s114_flag = false."],
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="13",
        source_name="DLUHC LAHS",
        publisher="DLUHC",
        acquisition_method="manual",
        cadence="annual", cadence_months=12,
        target_table="la_housing_register", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="14",
        source_name="VOA/DWP LHA rates",
        publisher="VOA/DWP",
        series_name="Universal Credit Local Housing Allowance rates, England",
        acquisition_method="manual",
        known_gotchas=(
            "The documented acquisition is an HTTP GET against a per-edition "
            "GOV.UK assets URL written into the node — for 2026-27, "
            "https://assets.publishing.service.gov.uk/media/"
            "69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv. Assets "
            "URLs change every edition, so the URL is replaced by hand rather "
            "than discovered. The file has no separate title row: treating "
            "row 1 as headers silently drops the first BRMA (Ashford)."),
        cadence="annual", cadence_months=12,
        publication_window="late January",
        target_table="brma_lha_rates", geography_level="BRMA",
        join_path=("la_brma_mapping, a lad24cd to brma_name crosswalk built "
                   "from BRMA boundary polygons."),
        node_docs_path="docs/nodes/s14_node1..s14_node7",
        n8n_workflow_name="Workflow 1",
        caveats=["Rates are frozen at April 2024 levels and published as "
                 "monthly values per bedroom category; the build converts to "
                 "weekly as monthly * 12 / 52.",
                 "152 BRMAs, England only — Scotland, Wales and Northern "
                 "Ireland are excluded."],
        completeness_note=(
            "The build script s14_lha_rates_build.py is not in the published "
            "tree, so build_script_path is null here. refresh_tier is C "
            "because the edition URL is supplied by hand, not because the "
            "source is unimportant."),
        latest_period_loaded="2026-27",
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="15",
        revises_back_series=True,
        revision_note=("Every edition republishes the full back series and the monthly "
            "upsert revises any previously provisional values."),
        source_name="Land Registry UK HPI",
        publisher="HM Land Registry",
        series_name=("UK House Price Index: average prices and property type "
                     "breakdowns"),
        landing_page_url="https://www.gov.uk/government/collections/uk-house-price-index-reports",
        acquisition_method="landing_page",
        known_gotchas=(
            "The file URL changes every edition. Resolve it dynamically: "
            "fetch the collections page, extract the first "
            "/government/statistical-data-sets/"
            "uk-house-price-index-data-downloads-* link, then from that page "
            "extract Average-prices-{YYYY}-{MM}.csv and "
            "Average-prices-Property-Type-{YYYY}-{MM}.csv. Every edition "
            "republishes the full back series from 1968 (all-property) and "
            "1995 (by type); only data from 2022-01-01 onward is loaded."),
        cadence="monthly", cadence_months=1, expected_lag_days=42,
        publication_window="~6 weeks after the reference month",
        target_table="la_house_prices", geography_level="LAD24",
        join_path=("lad24cd via la_code_lookup, plus hard recodes for "
                   "post-boundary-change Barnsley and Sheffield."),
        build_script_path="s15_hpi_build.py",
        node_docs_path=("s19_node1_fetch_collection_page.md .. "
                        "s19_node6_log_run.md (misnamed: these are S15's node "
                        "docs, left over from the S19 to S15 renumbering)"),
        source_doc_path="s15_hpi_source.md",
        verification_checks={"script": "s15_hpi_build.py", "checks": 6,
                             "gate": "all checks must PASS before the run is "
                                     "logged; on FAIL the script exits 1 and "
                                     "no log entry is written"},
        caveats=["Open-market prices only. Right-to-buy, shared ownership and "
                 "sub-market transactions are excluded where identifiable.",
                 "The Land Registry suppresses average prices where "
                 "transaction volumes are too low for statistical "
                 "reliability. These are stored as NULL, not estimated.",
                 "Isles of Scilly (E06000053) has no HPI data published.",
                 "Editions are published ~6 weeks after the reference month, "
                 "so the most recent period will typically be 2-3 months "
                 "behind the current date."],
        completeness_note=(
            "Standalone: not wired into Workflow 1 and adds no "
            "staging_la_signals column. Reaches the map through its own "
            "hpi_la_prices.json. Covers 295 of 296 English LAs."),
        latest_period_loaded="April 2026 edition",
        refresh_tier="B", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="17",
        latest_period_loaded="2025-26",
        source_name="SafeLives MARAC data",
        publisher="SafeLives",
        acquisition_method="manual",
        cadence="annual", cadence_months=12,
        publication_window="6-9 months after the reference period",
        target_table="marac_cases", geography_level="PFA",
        join_path="la_pfa_mapping, a lad24cd to police force area crosswalk.",
        n8n_workflow_name="Workflow 1",
        caveats=["SafeLives publishes MARAC data 6-9 months after the "
                 "reference period. The current run may show prior-year "
                 "figures."],
        completeness_note=(
            "No source documentation file exists. The 6-9 month lag is a "
            "range, so expected_lag_days is null rather than a midpoint. "
            + CAUTIOUS),
        refresh_tier="C", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="18",
        detected_period_type="publication_date",
        revises_back_series=True,
        revision_note=("Every edition republishes the full back series from January "
            "2015 and revises the prior provisional month, so loading "
            "the latest edition finalises earlier months automatically."),
        source_name="ONS PIPR",
        publisher="ONS",
        series_name=("Price Index of Private Rents, UK: monthly price "
                     "statistics"),
        landing_page_url=("https://www.ons.gov.uk/economy/"
                          "inflationandpriceindices/datasets/"
                          "priceindexofprivaterentsukmonthlypricestatistics"),
        acquisition_method="landing_page",
        known_gotchas=(
            "The landing page URL is stable; the file URL is not. Each "
            "monthly edition gets a new slug (publication date) and an "
            "unpredictable numeric filename suffix. Never hardcode the file "
            "URL — fetch the landing page and take the first (newest) xlsx "
            "link. Every edition republishes the full back series from "
            "January 2015 and revises the prior provisional month, so only "
            "the latest edition is ever downloaded."),
        cadence="monthly", cadence_months=1,
        publication_window="Mid-month, covering the previous calendar month",
        target_table="la_private_rents", geography_level="LAD24",
        join_path=("lad24cd via la_code_lookup, plus a CHD-verified recode "
                   "mapping for Barnsley and Sheffield. Successor "
                   "relationships live in la_geography and la_succession."),
        build_script_path="scripts/s18_pipr_fetch.py",
        source_doc_path="docs/s18_pipr_source.md",
        verification_checks={"script": "scripts/s18_pipr_verify.py"},
        caveats=["Housing-benefit tenancies are excluded where identifiable. "
                 "Figures represent open-market opportunity cost, not "
                 "HB-supported rents.",
                 "Stock-based measure: new and existing tenancies are "
                 "blended, so PIPR lags the price of a newly agreed lease in "
                 "a rising market."],
        ucws_lens="primary", hss_lens="context",
        completeness_note=(
            "294 of 296 English LAs — Isles of Scilly and City of London are "
            "not published. Raw rent levels do not go on the demand map; the "
            "derived income-versus-rent spread will, as separate future "
            "work."),
        refresh_tier="B", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="19",
        revision_note=(
            "Tested once, negative, not established. Apr-26 was re-probed "
            "against the live API on 2026-08-14 and reproduced exactly, "
            "296 of 296, delta 0.000%. That is not evidence PIP does not "
            "revise: the S8b finding compared a four-month window and this "
            "compared one, so the tests are not like-for-like. "
            "revises_back_series stays NULL rather than false. "
            "For any Stat-Xplore source the revision check has to be data "
            "comparison, not metadata comparison. DWP moved 285 of 296 LAs "
            "on the HB caseload with no revision note anywhere a check "
            "looks, so periodic re-probe against stored values is the only "
            "reliable signal."),
        source_name="DWP Stat-Xplore PIP",
        publisher="DWP",
        series_name=("PIP Cases with Entitlement from 2019 "
                     "(str:database:PIP_Monthly_new)"),
        api_endpoint="https://stat-xplore.dwp.gov.uk/webapi/rest/v1",
        acquisition_method="api",
        auth_required=True, auth_env_var="StatXplore_API_Key",
        known_gotchas=(
            "Table queries use the recodes pattern: explicit member URI maps "
            "in the recodes object, with dimensions referencing field IDs "
            "only. Including valueset URIs in dimensions causes a "
            "DUPLICATE_RECODES error. Batched at 15 LAs per API call to avoid "
            "504 timeouts. To force a full re-discovery, delete "
            "s19_cache/discovery.json before running. Until 2026-08-14 this "
            "build read an environment variable named Stat-Xplore_Token, "
            "which nothing defines, so it hard-stopped before reaching the "
            "API; it now reads StatXplore_API_Key, the same name "
            "scripts/s8b_hb_accom_type_build.py uses against the same "
            "Stat-Xplore account."),
        cadence="monthly", cadence_months=1, expected_lag_days=60,
        publication_window="Caseload snapshot, ~2 months lag",
        target_table="la_pip_claimants", geography_level="LAD24",
        join_path=("lad24cd direct match against Census 2021 MASTERGEOG21; no "
                   "historical-code summing is required for current "
                   "geography."),
        build_script_path="scripts/s19_pip_build.py",
        node_docs_path="docs/nodes/s19_node1..s19_node6",
        source_doc_path="docs/s19_pip_source.md",
        n8n_workflow_name="Workflow 1",
        caveats=["DWP applies statistical disclosure control. Values below a "
                 "rounding threshold are published as '..' and load as NULL. "
                 "Absence of a row or a NULL value means no published data, "
                 "not zero."],
        ucws_lens="context", hss_lens="primary",
        completeness_note=(
            "Wired into Workflow 1 since run 11: staging_la_signals carries "
            "pip_total_claimants, pip_enhanced_daily_living and "
            "pip_rate_per_1000. The rate is defined in v_la_pip_rates, which "
            "also exposes population_reference_year because the numerator "
            "refreshes monthly and the denominator annually."),
        latest_period_loaded="Apr-26",
        refresh_tier="A", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="20",
        source_name="Commercial rate card (private)",
        publisher="Withheld",
        acquisition_method="manual",
        cadence="periodic",
        publication_window="As supplied",
        geography_level="LAD24",
        confidential=True, publish_github=False, publish_map=False,
        latest_period_loaded="2026-08-04",
        completeness_note=(
            "Three editions are held. The most recent expanded coverage from "
            "119 to 125 areas, mapping to 155 local authorities against 150 "
            "before, and changed no rate: every area carried over is priced "
            "identically to the prior edition. Detail is deliberately "
            "withheld — commercial in confidence, held "
            "in exempt_pipeline only and never exported to the public "
            "repository, the signals JSON or the map. The nulls in this row "
            "are policy, not documentation gaps. The counterparty name "
            "appears in the table names themselves, so target_table, "
            "build_script_path and source_doc_path are not recorded: "
            "source_registry would otherwise become an artefact that "
            "discloses the counterparty without ever naming it. See the "
            "confidentiality standing rule in docs/METHODOLOGY.md."),
        refresh_tier="C", status="active",
    ),
    dict(
        source_code="21",
        source_name=("ONS Clustering similar local authorities and "
                     "statistical nearest neighbours in the UK"),
        publisher="ONS",
        series_name="Table 7a — LTLA global statistical nearest neighbours",
        landing_page_url=(
            "https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/"
            "datasets/clusteringsimilarlocalauthoritiesandstatistical"
            "nearestneighboursintheuk"),
        acquisition_method="manual",
        known_gotchas=(
            "The build reads a pre-downloaded workbook from "
            "data/raw/ons_statistical_neighbours_2026.xlsx; there is no "
            "discovery step, so a new edition must be downloaded by hand. "
            "The Mar-2026 edition carries E08000038 and E08000039 for "
            "Barnsley and Sheffield, remapped to E08000016 and E08000019 "
            "directly in the build script rather than through "
            "la_code_lookup, which is deliberately unchanged."),
        cadence="periodic",
        publication_window="Irregular (Mar 2026 edition)",
        target_table="la_statistical_neighbours", geography_level="LAD24",
        completeness_note=(
            "Free download, no login required. The build script "
            "s21_statistical_neighbours_build.py is not in the published "
            "tree, so build_script_path is null here. refresh_tier is C "
            "because acquisition is a hand download."),
        latest_period_loaded="Mar 2026 edition",
        refresh_tier="C", status="active",
        publish_github=True, publish_map=False,
    ),
    dict(
        source_code="22",
        revises_back_series=True,
        revision_note=("Tables 1, 2, 3a, 3b and 4 of the 2025 taxbase were revised on "
            "21 January 2026 after corrections from 22 authorities. The "
            "release page carries the revision date."),
        source_name="MHCLG Council Taxbase (CTB form) + Live Table 615",
        publisher="MHCLG",
        series_name=("Local authority Council Taxbase in England; Live Table "
                     "615: vacant dwellings by local authority district, "
                     "England, from 2004"),
        landing_page_url="https://www.gov.uk/government/collections/council-taxbase-statistics",
        api_endpoint="https://www.gov.uk/api/content",
        acquisition_method="api",
        known_gotchas=(
            "Both files are resolved at runtime from their publisher landing "
            "pages via the GOV.UK content API; no file URL is hardcoded. The "
            "header row is row 6 on every data sheet — row 5 carries the "
            "table label spanning each block, row 7 the England total and "
            "rows 8-303 the 296 billing authorities. Column offsets differ "
            "per table block and are not uniform. Live Table 615 has its own "
            "landing page: https://www.gov.uk/government/statistical-data-"
            "sets/live-tables-on-dwelling-stock-including-vacants."),
        cadence="annual", cadence_months=12,
        publication_window=("November, revised the following January; "
                            "Table 615 updated with the dwelling "
                            "stock live tables"),
        target_table="la_council_taxbase_empties", geography_level="LAD24",
        join_path=("MHCLG publishes Barnsley and Sheffield as E08000038 and "
                   "E08000039; both resolve through la_code_lookup as "
                   "change_type = 'recode'."),
        build_script_path="scripts/s22_ctb_empties_build.py",
        node_docs_path="docs/nodes/s22_node1..s22_node10",
        source_doc_path="docs/s22_source_structure.md",
        verification_checks={"script": "scripts/s22_verify.py",
                             "hard_checks": 6, "soft_checks": 4,
                             "report": "docs/s22_verification.md"},
        n8n_workflow_name="Workflow 1",
        caveats=["Only the current year is published in the release workbook, "
                 "so a single year is loaded; the series is built up one "
                 "release at a time from November each year.",
                 "Table 615 covers 2004 to 2025, but that series is not "
                 "complete for any single geography over the full period: "
                 "891 rows across 80 published codes are districts abolished "
                 "under local government reorganisation and carry a null "
                 "lad24cd. They are deliberately not aggregated into "
                 "successor unitaries.",
                 "Long-term empty is not the same as vacant. The Council "
                 "Taxbase and Table 615 use different definitions and "
                 "different snapshot dates, and are not reconciled to each "
                 "other.",
                 "Structural break 2024-04-01: the Empty Homes Premium "
                 "threshold moved from 2 years to 1 year, so "
                 "empty_homes_premium_count is not comparable across that "
                 "date.",
                 "Structural break 2025-04-01: the Second Homes Premium was "
                 "introduced, applied by 211 of 296 authorities, so "
                 "second_homes is affected by reclassification from that "
                 "date.",
                 "premium_coverage_pct is directional only and can never "
                 "reach 100: long-term empty starts at six months while the "
                 "premium starts at twelve. It is not a compliance rate."],
        completeness_note=(
            "296 of 296 authorities for taxbase year 2025, complete. "
            "Additional tables: la_ctb_exemption_classes, "
            "la_vacant_dwellings_615, ctb_series_breaks. Rates are derived in "
            "v_la_empty_homes_rates and never stored."),
        latest_period_loaded="2025",
        refresh_tier="B", status="active",
        publish_github=True, publish_map=True,
    ),
    dict(
        source_code="1b",
        source_name="MHCLG statutory homelessness Table A3",
        publisher="MHCLG",
        series_name=("Statutory homelessness in England: detailed local "
                     "authority-level tables, Table A3"),
        landing_page_url=("https://www.gov.uk/government/collections/"
                          "homelessness-statistics"),
        acquisition_method="landing_page",
        api_endpoint="https://www.gov.uk/api/content",
        auth_required=False,
        known_gotchas=(
            "Two incompatible sheet layouts. Quarters to October-December 2025 "
            "use a 37-column merged four-row header; the January-March 2026 "
            "release rewrote A3 to 34 columns with a single labelled header row "
            "and [note n] markers, and renamed every category. The build "
            "matches columns by label and refuses to load if any populated "
            "column is unmatched, because the alternative - reading by position "
            "- is what mis-mapped the S1 columns. "
            "Suppression notation also differs by layout: legacy uses '..' for "
            "a non-submitting authority and '-' for a suppressed breakdown, "
            "v2026 uses '[x]' and '[c]'. "
            "Older quarters carry the pre-2025 Barnsley and Sheffield codes "
            "E08000016 and E08000019, which must be resolved through "
            "la_code_lookup or the same authority appears under two codes. "
            "GOV.UK still serves superseded assets: the _revised files recorded "
            "in homelessness_quarter_urls for four quarters resolve but are no "
            "longer linked from any release page, so editions are resolved live "
            "from the release attachments rather than from that table."),
        cadence="quarterly", cadence_months=3, expected_lag_days=120,
        publication_window=("Roughly four months after quarter end; the "
                            "January-March 2026 edition landed 13 August 2026"),
        target_table="la_homelessness_support_needs", geography_level="LAD24",
        join_path="lad24cd resolved through la_code_lookup",
        build_script_path="scripts/s1b_support_needs_build.py",
        source_doc_path="docs/s1b_support_needs_source.md",
        node_docs_path="docs/nodes/s1b_node1_resolve_editions.md",
        metrics=["Households owed a duty by each of 24 published support needs",
                 "Households with no, unknown, one, two and three or more "
                 "support needs",
                 "Total count of support needs reported"],
        caveats=[
            "A3 is multi-response. A household can report several support "
            "needs, and each need is counted once per household, so the 24 "
            "category figures do not sum to the household total and must never "
            "be added together. category_group separates the multi-response "
            "categories ('support_need') from the mutually exclusive household "
            "breakdown ('needs_breakdown').",
            "Suppressed and missing values are stored as NULL with a "
            "value_flag of 'suppressed' or 'missing', never as zero. A "
            "breakdown is suppressed where an authority has fewer than five "
            "households with support needs.",
            "The publisher's England and regional rows are weighted to impute "
            "for non-submitting authorities and rounded to the nearest 10, so "
            "the loaded LA rows sum to less than the published England total. "
            "They are not loaded as areas.",
            "'Care leaver aged 21+' was retired on 1 April 2023 and split into "
            "'21-24' and '25+'. The retired option is still reported and is "
            "loaded as care_leaver_legacy_combined; the three overlap while "
            "authorities migrate."],
        completeness_note=(
            "296 of 296 authorities for all eleven quarters from July-September "
            "2023 to January-March 2026, complete. 101,232 rows. "
            "Built 2026-08-14 as an extension of S1, not a replacement: S1 "
            "keeps the temporary accommodation series that feeds Workflow 1. "
            "S1b covers all 24 published support-need categories, including the "
            "five S1 nominally holds, because those five were found during this "
            "build to hold the wrong publisher columns - see "
            "docs/decisions/2026-08-14-s1-support-need-column-misalignment.md. "
            "S1b is not wired into staging_la_signals; it is queried directly."),
        latest_period_loaded="2025Q4",
        revises_back_series=True,
        revision_note=(
            "Quarters are revised and republished in place. The currently "
            "linked attachment differs from the original for July-September "
            "2023 (_fixed), April-June 2024 (_fix) and April-June 2025 "
            "(_corrected), and homelessness_quarter_urls additionally records "
            "_revised assets for four quarters that GOV.UK still serves but no "
            "longer links. edition_variant and source_url on every row record "
            "which file that row came from."),
        detected_period_type="reference_period",
        # Which cohorts present as homeless is the closest published proxy for
        # referral mix (HSS); for UCWS it indicates the support profile of the
        # households an area is placing.
        hss_lens="primary", ucws_lens="secondary",
        refresh_tier="B", status="active",
        confidential=False, publish_github=True, publish_map=False,
    ),
    dict(
        source_code="23",
        source_name="RSH registered provider social housing stock",
        publisher="Regulator of Social Housing",
        series_name=("Registered provider social housing stock and rents in "
                     "England, registered providers look-up tool"),
        landing_page_url=(
            "https://www.gov.uk/government/statistics/registered-provider-"
            "social-housing-stock-and-rents-in-england-2024-to-2025"),
        acquisition_method="landing_page",
        api_endpoint="https://www.gov.uk/api/content",
        auth_required=False,
        known_gotchas=(
            "The local authority breakdown is not published as a data file. It "
            "is the STOCK_BY_LA sheet inside the look-up tool workbook, which "
            "exists to drive the workbook's own search box, so it is an "
            "internal sheet that could be renamed without notice. The build "
            "asserts the exact header set and stops if it changes. "
            "The same sheet mixes three grains - provider rows, 296 LA subtotal "
            "rows (RP_Type = 'LA') and 9 regional rows - and a load that does "
            "not filter on RP_Type double-counts by roughly three times. "
            "The release landing page URL carries the edition years, so it "
            "changes every year and cannot be treated as stable."),
        cadence="annual", cadence_months=12, expected_lag_days=211,
        publication_window="Autumn each year",
        target_table="rsh_rp_stock_by_la", geography_level="LAD24",
        join_path="lad24cd resolved through la_code_lookup",
        build_script_path="scripts/s23_rsh_stock_build.py",
        source_doc_path="docs/s23_rsh_stock_source.md",
        node_docs_path="docs/nodes/s23_node1_resolve_edition.md",
        metrics=["Supported housing and housing for older people units per "
                 "provider per LA",
                 "General needs self-contained units and bedspaces",
                 "Low cost home ownership units",
                 "Total owned social stock"],
        caveats=[
            "LA_SHHOP combines supported housing with housing for older "
            "people and RSH does not split them at local authority level. A "
            "large share is sheltered and retirement housing, so the figure is "
            "an upper bound on supported provision of the kind this pipeline "
            "is about and must not be read as a count of exempt-accommodation "
            "style units.",
            "Stock date and publication date are different and both are "
            "stored. The return is a snapshot at 31 March and publication "
            "follows roughly seven months later, so the newest available "
            "figure is up to nineteen months old before the next one lands.",
            "Loaded rows are unweighted. The publisher's headline national "
            "figures in the additional tables are weighted to impute for small "
            "providers filing the short SDR form, so they are slightly higher: "
            "4,533,055 unweighted against 4,546,653 weighted for total social "
            "stock at 31 March 2025.",
            "Stock is recorded where it is owned, not where it is managed. A "
            "provider owning stock in an area is not evidence that it operates "
            "there."],
        completeness_note=(
            "296 of 296 authorities, 10,171 provider-by-authority rows for the "
            "2024 to 2025 edition, stock at 31 March 2025. 504,902 supported "
            "housing and older people units nationally; 295 of 296 authorities "
            "carry some. Verified against the publisher's own 296 LA subtotal "
            "rows, which reconcile exactly on all five measures. "
            "SDR and LADR are held in one table with a provider_type column "
            "because the publisher already merges them into this sheet with an "
            "identical column set. The first direct supply-side measure in the "
            "pipeline: S11 counts CQC locations and S8 counts HB caseload, "
            "both indirect. Not yet wired into staging_la_signals."),
        latest_period_loaded="2025-03-31",
        revises_back_series=True,
        revision_note=(
            "Established from the publisher's own technical notes: RSH states "
            "it will republish the statistics in the April of the year "
            "following initial publication where aggregate changes made by "
            "providers require a major revision, and makes non-scheduled "
            "corrections where a substantial error or methodological issue is "
            "identified."),
        detected_period_type="reference_period",
        # Registered supported provision is the direct comparator for a
        # supported housing scheme (HSS); for UCWS it is a different model, but
        # the stock competes for the same referrals.
        hss_lens="primary", ucws_lens="secondary",
        refresh_tier="B", status="active",
        confidential=False, publish_github=True, publish_map=False,
    ),
    dict(
        source_code="24",
        source_name="RSH register of registered providers",
        publisher="Regulator of Social Housing",
        series_name=("Registered providers of social housing (monthly); "
                     "Regulatory judgements and enforcement notices"),
        landing_page_url=("https://www.gov.uk/government/publications/"
                          "registered-providers-of-social-housing"),
        acquisition_method="landing_page",
        api_endpoint="https://www.gov.uk/api/content",
        auth_required=False,
        known_gotchas=(
            "Two separate publications, resolved from two pages: the monthly "
            "register snapshot and the regulatory judgements and notices "
            "table. "
            "The register page carries only the current month and there is no "
            "archive, so history exists only because this table stores one row "
            "per provider per snapshot. A load that overwrote a current-state "
            "table would destroy the only record of what changed. "
            "The snapshot date comes from the attachment title, not from the "
            "run date - the file is published mid-month and a later run must "
            "still record the publisher's date. "
            "Some grade dates arrive as raw Excel serials rather than typed "
            "dates. "
            "The register workbook carries a hidden sheet holding a stray "
            "account token from the publisher's tooling; it is not read and "
            "nothing from it is stored, and the raw file is not committed."),
        cadence="monthly", cadence_months=1, expected_lag_days=0,
        publication_window="Around the middle of each month",
        target_table="rsh_registered_providers", geography_level="entity",
        build_script_path="scripts/s24_rsh_register_build.py",
        source_doc_path="docs/s24_rsh_register_source.md",
        node_docs_path="docs/nodes/s24_node1_resolve_publications.md",
        metrics=["Registered provider count by designation and corporate form",
                 "Consumer, governance, viability and rent gradings per "
                 "provider with grade dates",
                 "Enforcement notices",
                 "Month-on-month registrations and de-registrations"],
        caveats=[
            "No geography. RSH does not publish provider addresses or contact "
            "details, so this source cannot be apportioned to a local "
            "authority. It is deliberately not wired into staging_la_signals "
            "and deliberately not a map layer, and the verification suite "
            "fails if either appears. A provider's registered office is not "
            "where its stock is.",
            "De-registration is an absence, not an event. The snapshot lists "
            "current providers only, so a de-registration is detected by "
            "comparing two snapshot dates and has no published date of its "
            "own.",
            "A registration number can be reused or transferred: L4331 appears "
            "in the judgements table under two different landlord names. The "
            "publisher's 'Name and Reg Code Change Details' column is stored "
            "verbatim rather than resolved.",
            "Judgements cover only providers that have been assessed - 308 of "
            "1,579 registered providers. Absence of a judgement is not a "
            "clean bill of health.",
            "Local authority providers receive consumer gradings only, so "
            "governance and viability are legitimately null for them."],
        completeness_note=(
            "Register snapshot 24 July 2026: 1,579 providers (1,260 "
            "non-profit, 232 local authority, 87 profit). Judgements edition "
            "12 August 2026: 308 judgements, 2 enforcement notices. "
            "Discovery established that regulatory judgements ARE published in "
            "a machine-readable table, not only as individual documents, so "
            "the gradings table was built rather than recorded as a "
            "limitation. Additional tables: rsh_regulatory_judgements, "
            "rsh_enforcement_notices. "
            "Held for risk management rather than analysis: the income route "
            "runs through a registered provider partner, and a downgrade, an "
            "enforcement notice or a de-registration is a material event."),
        latest_period_loaded="2026-07-24",
        revises_back_series=False,
        revision_note=(
            "The register is a snapshot, not a series, so there is no back "
            "series to revise. Each month is a new snapshot stored alongside "
            "the previous ones rather than replacing them."),
        detected_period_type="publication_date",
        # The RP partner and the competitor set are both on this register and a
        # downgrade is a material event for the income route (HSS); for UCWS
        # de-registrations are a business development signal, because the
        # failed operator's landlords need a new manager.
        hss_lens="primary", ucws_lens="secondary",
        refresh_tier="B", status="active",
        confidential=False, publish_github=True, publish_map=False,
    ),
]


GOVUK_API = "https://www.gov.uk/api/content"

# ---------------------------------------------------------------------------
# Tier-C mechanics pass, 2026-08-14.
#
# Eleven sources sat at tier C on the cautious default — acquisition mechanics
# undocumented, so the most cautious tier was assigned rather than a finding
# recorded. This block is the result of establishing them, and is merged over
# the definitions above so the discovery is legible as one pass rather than
# scattered through eleven dicts.
#
# Seven moved to B and one to A. Three stay at C, now on evidence: S4 because
# no working DfE API path was found, S12 because S.114 notices have no central
# publication to watch, S17 because SafeLives is a third party with no API.
# "Checked and not established" is a different statement from "not checked",
# and completeness_note carries which one applies.
# ---------------------------------------------------------------------------
TIER_C_FINDINGS = {
    "1": dict(
        tier="B", method="landing_page",
        completeness_extra=(
            " AUDIT 2026-08-14, RESOLVED 2026-08-16: the 2025Q1 gap and the "
            "unrecorded 2025Q3 provenance are both closed. Eleven quarters, "
            "2023Q2 to 2025Q4, 296 rows each. period is a FINANCIAL-YEAR "
            "quarter - 2025Q1 is April to June 2025 and 2025Q4 is January to "
            "March 2026 - read from the files' own table titles, so 2026Q1 is "
            "April to June 2026 and is not yet published. Every quarter now "
            "carries a row in homelessness_quarter_urls with reproduction "
            "status. See "
            "docs/decisions/2026-08-14-s1-quarter-gap-and-provenance.md and "
            "docs/decisions/"
            "2026-08-16-s1-reconstruction-markers-and-revision.md."),
        url="https://www.gov.uk/government/collections/homelessness-statistics",
        api=GOVUK_API,
        ptype="reference_period",
        revises=True,
        revision_note=(
            "Quarters are revised and republished in place, measured rather "
            "than inferred from file names. On 2026-08-16 every loaded quarter "
            "was re-extracted from the currently published file and compared "
            "cell by cell. 2025Q2, 2025Q3 and 2025Q4 reproduce exactly. "
            "2023Q2 to 2024Q4 do not, and the divergence is TWO defects, not "
            "one. (a) The A1 measures - total_assessments, owed_duty, "
            "prevention_duty, relief_duty - differ on 200-230 of 296 "
            "authorities, consistent with revision: the 2023Q2 file itself "
            "gives Hartlepool 172 initial assessments where the table holds "
            "193; homelessness_quarter_urls.notes already recorded 'Revised' "
            "against exactly those periods; the GOV.UK collection dates the "
            "October-December 2024 release to June 2026, after the "
            "2026-04-01 bulk load; and 2025Q2 came from that same load and "
            "does reproduce. Still unrestated - a reload is its own backlog "
            "item. (b) support_needs_total was NOT revision but misalignment: "
            "it held 'households with one support need' instead of 'one or "
            "more', at 45-47% of correct, matching the adjacent "
            "hh_one_support_need column on 148-164 of 296. The tell was the "
            "rate - a divergence hitting 99% of authorities on one column "
            "while hitting 75% on the others is not a revision. Corrected in "
            "place 2026-08-16 across 2,072 rows and verified 296/296 against "
            "S1b in all eleven quarters. Per-quarter status is in "
            "homelessness_quarter_urls.reproduces_from_source, joined onto "
            "the data by v_la_statutory_homelessness."),
        note=("Mechanics established 2026-08-14. Quarterly releases titled "
              "'Statutory homelessness in England: {month} to {month} {year}' "
              "sit under the Homelessness statistics collection, which "
              "resolves through the GOV.UK content API. Detection is "
              "automatable; ingestion stays gated because the per-quarter "
              "file URLs are still curated by hand.")),
    "2": dict(
        tier="B", method="landing_page",
        url=("https://www.gov.uk/government/collections/"
             "local-authority-revenue-expenditure-and-financing"),
        api=GOVUK_API,
        ptype="reference_period",
        revises=True,
        revision_note=(
            "Each financial year is published three times — budget, then "
            "provisional outturn, then final outturn — so a loaded year is "
            "superseded twice before it settles. RO4 is the outturn return, "
            "not the budget."),
        note=("Mechanics established 2026-08-14. The outturn releases are "
              "'Local authority revenue expenditure and financing England: "
              "{years} individual local authority data outturn' under a "
              "collection that resolves through the GOV.UK content API.")),
    "3b": dict(
        tier="B", method="api",
        url=("https://www.nomisweb.co.uk/sources/census_2021_bulk"),
        api="https://www.nomisweb.co.uk/api/v01",
        ptype="reference_period",
        note=("Mechanics established 2026-08-14. Census 2021 tables are "
              "retrievable from the NOMIS API, which responds and is "
              "machine-readable. Cadence makes this close to academic: the "
              "next census is 2031, so detection will not fire for years.")),
    "4": dict(
        tier="C", method="api",
        url=("https://explore-education-statistics.service.gov.uk/"
             "find-statistics/children-looked-after-in-england-including-"
             "adoptions"),
        ptype="reference_period",
        note=("Mechanics established 2026-08-20, superseding the 2026-08-14 "
              "check. The release was previously misidentified as SEN2 / "
              "Children in Need; it is the SSDA903 Children Looked After "
              "return. Dataset CSVs are retrievable without auth from "
              "/data-catalogue/data-set/{uuid}/csv, so acquisition is an API "
              "rather than manual. Tier C stands because the UUID changes "
              "each release and must be read by hand from the release data "
              "guidance page: the data catalogue front end is a JavaScript "
              "app and EES exposes no content API path for this "
              "publication.")),
    "5": dict(
        tier="B", method="landing_page",
        url=("https://www.gov.uk/government/collections/"
             "english-indices-of-deprivation"),
        api=GOVUK_API,
        ptype="reference_period",
        note=("Mechanics established 2026-08-14. The English indices of "
              "deprivation collection resolves through the GOV.UK content "
              "API. Detection is automatable, though a new IMD edition is a "
              "deliberate pipeline decision rather than a routine refresh.")),
    "7": dict(
        tier="B", method="api",
        url="https://geoportal.statistics.gov.uk/",
        api=("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/"
             "services"),
        ptype=None,
        note=("Mechanics established 2026-08-14. The Open Geography Portal "
              "exposes an ArcGIS REST service list — 3,906 services, "
              "machine-readable, searchable for LAD*BGC vintages. Detection "
              "is automatable. Ingestion is heavily gated: changing the "
              "boundary vintage re-bases every join in the pipeline and is a "
              "deliberate decision, never a routine refresh.")),
    "8": dict(
        tier="A", method="api",
        status="superseded", superseded_by="8b",
        completeness_extra=(
            " SUPERSEDED by S8b on 2026-08-14. Both read the same measure from "
            "the same Stat-Xplore hb_new database, and a live probe of 202511 "
            "returned S8b's values on 296 of 296 LAs. S8b already carries six "
            "months against S8's one. Two sources maintaining one number will "
            "diverge and nothing would surface it, so hb_sa_caseload now comes "
            "from S8b. la_hb_sa_caseload is kept rather than dropped so the "
            "provenance of W1 runs 4-12 stays readable."),
        url=None,
        api="https://stat-xplore.dwp.gov.uk/webapi/rest/v1",
        ptype="reference_period",
        auth=True, auth_var="StatXplore_API_Key",
        note=("Mechanics established 2026-08-14. Same Stat-Xplore API, same "
              "account and same credential as S8b, which already runs "
              "unattended: the endpoint responds 401 without a key and the "
              "schema is discoverable programmatically. Tier A describes the "
              "mechanics; no build script exists yet, which build_script_path "
              "records separately.")),
    "10": dict(
        tier="B", method="landing_page",
        url="https://www.gov.uk/government/collections/homelessness-statistics",
        api=GOVUK_API,
        ptype="reference_period",
        note=("Mechanics established 2026-08-14. Annual releases titled "
              "'Rough sleeping snapshot in England: autumn {year}' sit under "
              "the same Homelessness statistics collection as S1 and resolve "
              "through the GOV.UK content API. The autumn snapshot is "
              "published the following February.")),
    "12": dict(
        tier="C", method="manual",
        url=("https://www.gov.uk/government/collections/"
             "exceptional-financial-support-for-local-authorities"),
        ptype=None,
        note=("Mechanics established 2026-08-14, and tier C is now evidenced "
              "rather than assumed. The EFS half resolves through the GOV.UK "
              "content API and could be detected. The S.114 half cannot: "
              "notices are issued and published by individual local "
              "authorities with no central register, so no endpoint exists to "
              "watch. Automating only the detectable half would report the "
              "source as checked while the manual half went unwatched, which "
              "is worse than reporting it manual. Split the source if the EFS "
              "half is ever worth automating on its own.")),
    "13": dict(
        tier="B", method="landing_page",
        url=("https://www.gov.uk/government/collections/"
             "local-authority-housing-data"),
        api=GOVUK_API,
        ptype="reference_period",
        note=("Mechanics established 2026-08-14. LAHS returns are published "
              "as statistical data sets, 'Local authority housing statistics "
              "data returns for {years}', under the Local authority housing "
              "data collection, which resolves through the GOV.UK content "
              "API.")),
    "17": dict(
        tier="C", method="manual",
        url=("https://safelives.org.uk/practice-support/"
             "resources-marac-meetings/latest-marac-data/"),
        ptype=None,
        note=("Mechanics established 2026-08-14, and tier C is now evidenced. "
              "SafeLives is a third-party charity publishing to its own site "
              "with no API and no stable file-URL pattern. The page responds, "
              "so detection by page fingerprint is possible, but ingestion "
              "stays manual and the 6-9 month publication lag makes frequent "
              "checking pointless.")),
}

# Every column the upsert writes, in order. Columns absent from a source dict
# are written as NULL and preserved by COALESCE on re-run.
COLUMNS = [
    "source_code", "source_name", "publisher", "series_name",
    "landing_page_url", "acquisition_method", "api_endpoint", "auth_required",
    "auth_env_var", "known_gotchas",
    "cadence", "cadence_months", "expected_lag_days", "publication_window",
    "next_expected_at",
    "target_table", "natural_key", "geography_level", "join_path",
    "n8n_workflow_name", "build_script_path", "node_docs_path",
    "source_doc_path", "verification_checks",
    "caveats", "completeness_note",
    "ucws_lens", "hss_lens",
    "confidential", "publish_github", "publish_map",
    "refresh_tier", "status", "superseded_by",
    "latest_period_loaded", "metrics",
    "revises_back_series", "revision_note",
    "detected_period_type",
]
# Columns that must never be nulled out by a re-run, but are also never
# overwritten from this script once set by the check job.
CHECK_JOB_COLUMNS = ("last_check_at", "last_seen_fingerprint")

# NOT NULL columns, for the empty-string check and the gap report.
NOT_NULL = ("source_code", "source_name", "publisher", "acquisition_method",
            "cadence", "refresh_tier", "status")


def halt(msg):
    sys.exit(f"HALT: {msg}")


def find_methodology():
    """docs/METHODOLOGY.md, under either checkout layout."""
    for cand in (REPO / "docs" / "METHODOLOGY.md",
                 REPO / "ONS_Population_Estimates" / "docs" / "METHODOLOGY.md"):
        if cand.exists():
            return cand
    halt("docs/METHODOLOGY.md not found — it is the source register and this "
         "build will not proceed without it")


def parse_register():
    """Rows of the METHODOLOGY.md source register table, keyed by S#."""
    rows, in_table = {}, False
    for line in find_methodology().read_text(encoding="utf-8").splitlines():
        if line.startswith("| S# | Source |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells or set(cells[0]) <= set("-: "):
                continue
            rows[cells[0]] = {
                "source": cells[1],
                "metrics": cells[2] if len(cells) > 2 else "",
                "publisher": cells[3] if len(cells) > 3 else "",
                "frequency": cells[4] if len(cells) > 4 else "",
            }
    if not rows:
        halt("the METHODOLOGY register table could not be parsed")
    return rows


def register_sort_key(s):
    return (int(re.match(r"\d+", s).group()), s)


def split_metrics(cell):
    """Split a Metric(s) cell into its parts, and report the separators used.

    Splits only at parenthesis depth zero. 'LHA weekly rates (SAR, 1-4 bed) by
    BRMA, mapped to LAs' has to break at the second comma and not the first,
    and a naive split would cut the bracket in half.

    Where the cell uses semicolons at top level, only semicolons separate -
    the commas inside those clauses are prose, not list items. S22's cell is
    two semicolon-separated clauses, the first of which is itself a
    comma-separated list; splitting on both would flatten a structure the
    author put there on purpose, and the rejoin would come back with the
    wrong punctuation.

    Returns (parts, separators) so the caller can reconstruct the original
    text exactly and prove nothing was lost.
    """
    def top_level(text, chars):
        found, depth = [], 0
        for i, ch in enumerate(text):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            elif depth == 0 and ch in chars:
                found.append(i)
        return found

    delims = ";" if top_level(cell, ";") else ","
    cuts = top_level(cell, delims)
    parts, seps, prev = [], [], 0
    for i in cuts:
        parts.append(cell[prev:i].strip())
        seps.append(cell[i])
        prev = i + 1
    parts.append(cell[prev:].strip())
    return [p for p in parts if p], seps


def rejoin_metrics(parts, seps):
    """Reconstruct the original cell from a split, for the round-trip check."""
    out = ""
    for i, part in enumerate(parts):
        out += part
        if i < len(seps):
            out += seps[i] + " "
    return out


# Key columns that carry a period. Anything else in a primary key is
# geography or a category. Ordered by preference where a table has more
# than one.
PERIOD_COLUMNS = (
    "reporting_period", "period_ending", "period", "month",
    "taxbase_year", "snapshot_year", "reference_year", "financial_year",
    "reporting_year", "rate_card_date", "year",
)

# Non-key columns that record which edition a row came from, for tables keyed
# on an entity rather than a period.
EDITION_COLUMNS = ("source_file_date", "file_date", "edition_date",
                   "snapshot_date")


def derive_latest_period(cur, table, pk_cols):
    """MAX of the target table's period key, as loaded.

    Derived from the live table rather than from documentation, because this
    is the one field where the database is the authority: it records what is
    actually loaded, not what a source document said was loaded at the time
    it was written. That is also what turns a check from "a newer edition
    exists somewhere" into a comparison against what this pipeline holds.
    """
    period_col = next((c for c in PERIOD_COLUMNS if c in (pk_cols or [])), None)
    if not period_col:
        # Some tables key on an entity rather than a period and carry the
        # edition as an ordinary column — cqc_locations is keyed on
        # location_id and records source_file_date. Without this fallback
        # those sources keep a documented value that goes stale the moment a
        # new edition is loaded.
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
              AND column_name = ANY(%s)
            ORDER BY array_position(%s, column_name)
            LIMIT 1
        """, (table, list(EDITION_COLUMNS), list(EDITION_COLUMNS)))
        row = cur.fetchone()
        if not row:
            return None
        period_col = row[0]
    cur.execute(f'SELECT MAX("{period_col}")::text FROM "{table}"')
    value = cur.fetchone()[0]
    return value


def live_primary_keys(cur):
    """Primary key column lists, read from the live catalogue."""
    cur.execute("""
        SELECT rel.relname,
               ARRAY(SELECT a.attname
                     FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                     JOIN pg_attribute a
                       ON a.attrelid = rel.oid AND a.attnum = k.attnum
                     ORDER BY k.ord)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE con.contype = 'p' AND ns.nspname = 'public'
    """)
    return dict(cur.fetchall())


def apply_ddl(cur):
    if not DDL_PATH.exists():
        halt(f"{DDL_PATH} not found")
    cur.execute(DDL_PATH.read_text(encoding="utf-8"))


def backfill_run_log_source_code(cur, register_codes):
    """Populate pipeline_run_log.source_code only where it is unambiguous.

    A row resolves when its source_number maps to exactly one register source
    and agent_name does not name a different registered series. Where the
    number and the agent name disagree, the row keeps a null and is reported.
    """
    cur.execute("""
        SELECT id, source_number, agent_name, notes, source_code
        FROM pipeline_run_log
        ORDER BY id
    """)
    rows = cur.fetchall()

    resolved, unresolved, already = [], [], 0
    for run_id, number, agent, notes, existing in rows:
        # A writer that set its own source_code has already resolved the row.
        # Counting it as unresolved misreports the build's own run entry,
        # whose source_number is deliberately outside the register.
        if existing is not None:
            already += 1
            continue
        if number in NON_SOURCE_RUN_KEYS:
            unresolved.append((run_id, number, agent,
                               "not a source — agent/orchestration run key"))
            continue
        if number not in register_codes:
            unresolved.append((run_id, number, agent,
                               "source_number is not in the METHODOLOGY register"))
            continue
        # The register maps this number to exactly one source. Contradiction
        # test: does agent_name name a different registered series?
        contradiction = None
        for other, name in register_codes.items():
            if other == number:
                continue
            if agent and name and name.lower() in (agent or "").lower():
                contradiction = other
                break
        if contradiction:
            unresolved.append((
                run_id, number, agent,
                f"source_number says {number} but agent_name names the series "
                f"registered as S{contradiction} — unresolved conflict, not a "
                f"clean disambiguation"))
            continue
        resolved.append((number, run_id))

    psycopg2.extras.execute_batch(
        cur,
        "UPDATE pipeline_run_log SET source_code = %s WHERE id = %s",
        resolved)
    return resolved, unresolved, already


def upsert_sources(cur, pks, register):
    """Upsert every registry row. Never overwrites a non-null with a null."""
    rows = []
    lossy = []
    for src in SOURCES:
        record = dict(src)
        f = TIER_C_FINDINGS.get(record["source_code"])
        if f:
            record["refresh_tier"] = f["tier"]
            record["acquisition_method"] = f["method"]
            record["completeness_note"] = (
                f["note"] + f.get("completeness_extra", ""))
            for k in ("status", "superseded_by"):
                if f.get(k):
                    record[k] = f[k]
            for key, col in (("url", "landing_page_url"),
                             ("api", "api_endpoint"),
                             ("ptype", "detected_period_type"),
                             ("revision_note", "revision_note")):
                if f.get(key):
                    record[col] = f[key]
            if f.get("auth"):
                record["auth_required"] = True
                record["auth_env_var"] = f["auth_var"]
            if f.get("revises"):
                record["revises_back_series"] = True

        # metrics is backfilled from the register's own Metric(s) cell, and
        # the split is proved reversible before it is stored. A split that
        # does not round-trip is kept whole rather than silently reshaped.
        cell = (register.get(record["source_code"], {}).get("metrics") or "").strip()
        if cell:
            parts, seps = split_metrics(cell)
            if rejoin_metrics(parts, seps) != cell:
                lossy.append((record["source_code"], cell,
                              rejoin_metrics(parts, seps)))
                parts = [cell]
            record["metrics"] = parts
        record.setdefault("auth_required", False)
        record.setdefault("confidential", False)
        record.setdefault("publish_github", True)
        record.setdefault("publish_map", False)

        # natural_key comes from the live primary key of target_table, so it
        # cannot drift from the database it describes.
        target = record.get("target_table")
        if target:
            if target not in pks:
                halt(f"S{record['source_code']}: target_table {target!r} does "
                     f"not exist in the database")
            record["natural_key"] = pks[target]
            # The live table overrides any documented value: it is the only
            # authority on what is actually loaded right now.
            derived = derive_latest_period(cur, target, pks[target])
            if derived:
                record["latest_period_loaded"] = derived

        vc = record.get("verification_checks")
        if vc is not None:
            record["verification_checks"] = psycopg2.extras.Json(vc)

        rows.append(tuple(record.get(col) for col in COLUMNS))

    placeholders = ", ".join(["%s"] * len(COLUMNS))
    updates = ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, source_registry.{c})"
        for c in COLUMNS if c != "source_code")
    sql = (f"INSERT INTO source_registry ({', '.join(COLUMNS)}) "
           f"VALUES ({placeholders}) "
           f"ON CONFLICT (source_code) DO UPDATE SET {updates}")
    psycopg2.extras.execute_batch(cur, sql, rows)
    return len(rows), lossy


def collect_gaps(cur):
    """Null fields per source, plus where the missing value would come from."""
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='source_registry'
        ORDER BY ordinal_position
    """)
    all_cols = [r[0] for r in cur.fetchall()]
    # Columns the backfill is not responsible for.
    skip = {"created_at", "updated_at", "last_check_at",
            "last_seen_fingerprint", "natural_key"}
    cols = [c for c in all_cols if c not in skip]

    cur.execute(f"SELECT {', '.join(cols)}, confidential, completeness_note "
                f"FROM source_registry ORDER BY source_code")
    names = cols + ["_confidential", "_note"]
    gaps = {}
    for row in cur.fetchall():
        rec = dict(zip(names, row))
        code = rec["source_code"]
        gaps[code] = {
            "confidential": rec["_confidential"],
            "note": rec["_note"],
            "nulls": [c for c in cols if rec[c] is None],
        }
    return cols, gaps


# Where a missing value would have to come from. Keyed by column.
GAP_SOURCE = {
    "series_name": "the publisher's dataset or table title, as named in a source documentation file",
    "landing_page_url": "the publisher's landing page, recorded in a source or node documentation file",
    "api_endpoint": "the build script or node documentation, where acquisition is an API",
    "auth_env_var": "the build script, where the source needs a credential",
    "known_gotchas": "a source documentation file — acquisition traps are only known once written down",
    "cadence_months": "the publisher's stated cadence, where it is regular enough to express in months",
    "expected_lag_days": "the publisher's stated publication lag, in days",
    "publication_window": "the publisher's stated release window",
    "next_expected_at": "the publisher's release calendar. Not derivable from anything in this repository — a stated window such as 'late January' is not a date, and inventing one would be a guess",
    "target_table": "the build script or node documentation",
    "geography_level": "the target table's geography column and the join path",
    "join_path": "the build script's geography resolution step",
    "n8n_workflow_name": "the n8n workflow that runs the source, where one does",
    "build_script_path": "the build script, if it is in the published tree",
    "node_docs_path": "the per-node documentation under docs/nodes/",
    "source_doc_path": "a source documentation file — the single largest gap for undocumented sources",
    "verification_checks": "the source's verification suite and its documented check count",
    "caveats": "the source documentation. Caveats travel with the data, so an absent caveat list is a risk, not a tidy row",
    "completeness_note": "the source documentation's coverage statement",
    "ucws_lens": "an explicit dual-lens note in the source documentation",
    "hss_lens": "an explicit dual-lens note in the source documentation",
    "superseded_by": "only populated when a source is replaced; null is correct for an active source",
    "latest_period_loaded": "the check job, or the source documentation's 'month loaded' field",
}


def write_gap_report(cols, gaps, unresolved):
    published = {k: v for k, v in gaps.items() if not v["confidential"]}
    withheld = sorted(k for k, v in gaps.items() if v["confidential"])

    counts = {}
    for code, g in published.items():
        for c in g["nulls"]:
            counts[c] = counts.get(c, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    lines = [
        "# Source registry — gap report",
        "",
        "Generated by `scripts/backfill_source_registry.py`. This is the work "
        "list for hardening the registry, not a defect list: every entry is a "
        "field the repository does not document, recorded as NULL rather than "
        "inferred.",
        "",
        "Confidential sources are excluded from this report. Their fields are "
        "withheld by policy, not missing, and listing them here would make "
        "this document an artefact that discloses them. "
        f"{len(withheld)} source(s) excluded on that basis.",
        "",
        "## Null fields by field name, ranked",
        "",
        "The top of this list is the documentation debt.",
        "",
        "| Field | Sources missing it | Where the value would come from |",
        "| --- | ---: | --- |",
    ]
    for col, n in ranked:
        lines.append(f"| `{col}` | {n} | {GAP_SOURCE.get(col, '—')} |")

    lines += [
        "",
        f"Total: {sum(counts.values())} null fields across "
        f"{len(published)} published sources.",
        "",
        "## Null fields by source",
        "",
    ]
    for code in sorted(published, key=register_sort_key):
        g = published[code]
        lines.append(f"### S{code}")
        lines.append("")
        if not g["nulls"]:
            lines.append("No null fields.")
        else:
            lines.append("| Field | Where the value would come from |")
            lines.append("| --- | --- |")
            for c in g["nulls"]:
                lines.append(f"| `{c}` | {GAP_SOURCE.get(c, '—')} |")
        if g["note"]:
            lines.append("")
            lines.append(f"**Note.** {g['note']}")
        lines.append("")

    lines += [
        "## `pipeline_run_log.source_code` rows left null",
        "",
        "A null here means the source could not be established from "
        "`source_number`, `agent_name` and `notes` together. The log is an "
        "immutable audit record, so no row is rewritten to make it tidy.",
        "",
        "| Run id | `source_number` | `agent_name` | Why it is null |",
        "| ---: | --- | --- | --- |",
    ]
    for run_id, number, agent, why in unresolved:
        lines.append(f"| {run_id} | `{number}` | {agent} | {why} |")
    lines.append("")

    GAP_REPORT.parent.mkdir(parents=True, exist_ok=True)
    GAP_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sum(counts.values()), ranked


def log_run(cur, row_count, gap_count):
    cur.execute("""
        INSERT INTO pipeline_run_log
            (agent_name, source_number, source_code, status, rows_written,
             started_at, completed_at, notes)
        VALUES (%s, %s, %s, 'success', %s, now(), now(), %s)
        RETURNING id
    """, (AGENT_NAME, REGISTRY_SOURCE_NUMBER, REGISTRY_SOURCE_CODE, row_count,
          f"Source registry build. source_registry: {row_count} rows. "
          f"Gap report: {gap_count} null fields across published sources. "
          f"source_number '{REGISTRY_SOURCE_NUMBER}' chosen because it "
          f"collides with no register source number and no existing run-log "
          f"key ('0' is the report generators, 'w1' the workflow "
          f"orchestration). Objects: source_registry, source_check_log, "
          f"vw_source_due, pipeline_run_log.source_code."))
    return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-run", action="store_true",
                    help="insert the pipeline_run_log row for this build")
    args = ap.parse_args()

    register = parse_register()
    register_codes = {k: v["source"] for k, v in register.items()}
    declared = set(register_codes)
    built = {s["source_code"] for s in SOURCES}
    if declared != built:
        halt(f"registry rows do not match the METHODOLOGY register. "
             f"In register only: {sorted(declared - built)}. "
             f"In this script only: {sorted(built - declared)}.")

    conn = get_conn()
    conn.autocommit = False
    # source_registry carries a writer-only trigger: it is a generated
    # table, and a direct edit either reverts on the next backfill or
    # persists until someone adds a declaration. This connection is a
    # declared writer.
    cur_flag = conn.cursor()
    cur_flag.execute("SET ucws.registry_writer = 'on'")
    cur_flag.close()
    cur = conn.cursor()
    try:
        apply_ddl(cur)
        pks = live_primary_keys(cur)

        resolved, unresolved, already = backfill_run_log_source_code(
            cur, register_codes)
        n_rows, lossy = upsert_sources(cur, pks, register)

        cols, gaps = collect_gaps(cur)
        gap_count, ranked = write_gap_report(cols, gaps, unresolved)

        run_log_id = None
        if args.log_run:
            run_log_id = log_run(cur, n_rows, gap_count)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    print(f"source_registry rows upserted : {n_rows}")
    print(f"run-log source_code populated : {len(resolved)}")
    print(f"run-log source_code left null : {len(unresolved)}")
    print(f"run-log already carried a code: {already}")
    print(f"null fields (published only)  : {gap_count}")
    if lossy:
        print(f"metrics kept whole (no split) : {len(lossy)} "
              f"-> {[c for c, _, _ in lossy]}")
    print(f"gap report                    : {GAP_REPORT.relative_to(REPO)}")
    if run_log_id:
        print(f"pipeline_run_log id           : {run_log_id}")
    print()
    print("Top documentation debt:")
    for col, n in ranked[:8]:
        print(f"  {n:>3}  {col}")


if __name__ == "__main__":
    main()
