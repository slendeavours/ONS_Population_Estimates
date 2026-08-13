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
        source_name="DfE SEN2 / Children in Need",
        publisher="DfE",
        acquisition_method="manual",
        cadence="annual", cadence_months=12,
        target_table="care_leaver_accommodation", geography_level="LAD24",
        n8n_workflow_name="Workflow 1",
        caveats=["DfE data is at upper-tier LA level; district-level LAs may "
                 "show NULL or estimated values."],
        completeness_note=("No source documentation file exists. " + CAUTIOUS),
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
        caveats=["IMD 2019 is used, supplemented by the 2025 LA summary. No "
                 "full LSOA-level 2025 IMD has been released.",
                 "imd_rank_of_average_rank ranks LAs from 1 (most deprived) "
                 "to 317 (least deprived) on the average rank of constituent "
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
        revision_note=("DWP applies retrospective revisions to the HB caseload: "
            "Birmingham differs by 9.6% from la_hb_sa_caseload for "
            "Nov-25 for that reason."),
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
            "codes; two historical codes are unresolvable (E07000028, "
            "E07000189)."),
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
                 "keyed (lad24cd, notice_date)."],
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
        completeness_note=(
            "Detail is deliberately withheld — commercial in confidence, held "
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
]

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
