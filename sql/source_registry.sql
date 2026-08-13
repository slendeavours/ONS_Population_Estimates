-- ===========================================================================
-- Source refresh control layer for the exempt accommodation pipeline.
--
--   source_registry   what every source is, how it is acquired, when it is due
--   source_check_log  every check for a new edition, including failed checks
--   vw_source_due     the due list, derived from the registry + pipeline_run_log
--
-- Infrastructure, not a data source. It records nothing about local
-- authorities and carries no counterparty detail.
--
-- Additive only. Every CREATE is IF NOT EXISTS, every ALTER is guarded.
-- Safe to run against a database that already has all three objects.
--
-- Applied by scripts/backfill_source_registry.py.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. pipeline_run_log gains source_code
--
-- Discovery finding: source_number is varchar(5), not integer, so 3b / 8b /
-- 9a / 9b already store distinctly and no sub-source collapses onto its
-- parent. source_code is therefore an explicit join key to source_registry
-- rather than a fix for a lost distinction. It is nullable and unconstrained:
-- the log is an immutable audit record, historical rows may name sources that
-- are later deprecated, and rows whose source cannot be established honestly
-- keep a null.
--
-- No foreign key to source_registry, deliberately. See docs/SOURCE_REGISTRY.md.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'pipeline_run_log'
          AND column_name = 'source_code'
    ) THEN
        ALTER TABLE pipeline_run_log ADD COLUMN source_code text;
        COMMENT ON COLUMN pipeline_run_log.source_code IS
            'Registry source code, backfilled only where unambiguous. NULL '
            'means the source could not be established from source_number, '
            'agent_name and notes together — not that the run had no source.';
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 1b. pipeline_run_log.status: constrain new writes, do not normalise history
--
-- The log holds 'success' (83) and 'complete' (2). Both mean success. The two
-- 'complete' rows are an accurate record of what those builds wrote and are
-- left exactly as they are — rewriting an audit table to tidy a vocabulary is
-- the wrong trade.
--
-- NOT VALID is the whole point: existing rows are never re-checked, new
-- inserts and updates are. This codifies the convention already in force
-- rather than narrowing it — no failure has ever been logged, because a build
-- that fails rolls its transaction back and exits non-zero without writing a
-- row at all.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'pipeline_run_log_status_new_writes_chk') THEN
        ALTER TABLE pipeline_run_log ADD CONSTRAINT
            pipeline_run_log_status_new_writes_chk
            CHECK (status = 'success') NOT VALID;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 2. source_registry
--
-- last_success_at is deliberately absent. Success is derived from
-- pipeline_run_log in vw_source_due so there is exactly one truth about
-- what ran.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_registry (
    -- Identity
    source_code             text PRIMARY KEY,
    source_name             text NOT NULL,
    publisher               text NOT NULL,
    series_name             text,

    -- Acquisition
    landing_page_url        text,
    acquisition_method      text NOT NULL,
    api_endpoint            text,
    auth_required           boolean NOT NULL DEFAULT false,
    auth_env_var            text,
    known_gotchas           text,

    -- Cadence
    cadence                 text NOT NULL,
    cadence_months          integer,
    expected_lag_days       integer,
    publication_window      text,
    next_expected_at        date,

    -- Target
    target_table            text,
    natural_key             text[],
    geography_level         text,
    join_path               text,

    -- Provenance
    n8n_workflow_name       text,
    build_script_path       text,
    node_docs_path          text,
    source_doc_path         text,
    verification_checks     jsonb,

    -- Caveats
    caveats                 text[],
    completeness_note       text,

    -- Dual lens
    ucws_lens               text,
    hss_lens                text,

    -- Publication control
    confidential            boolean NOT NULL DEFAULT false,
    publish_github          boolean NOT NULL DEFAULT true,
    publish_map             boolean NOT NULL DEFAULT false,

    -- Refresh control
    refresh_tier            text NOT NULL,
    status                  text NOT NULL,
    superseded_by           text REFERENCES source_registry(source_code),

    -- State written by the check job
    latest_period_loaded    text,
    last_check_at           timestamptz,
    last_seen_fingerprint   text,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- metrics arrived after the first build, so it is added by guarded ALTER
-- rather than folded into the CREATE above: the CREATE is IF NOT EXISTS and
-- would be skipped on an existing table, leaving the column absent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'source_registry'
          AND column_name = 'metrics'
    ) THEN
        ALTER TABLE source_registry ADD COLUMN metrics text[];
    END IF;
END $$;

COMMENT ON COLUMN source_registry.metrics IS
    'What the source actually gives you, one element per metric. Backfilled '
    'from the Metric(s) column of the hand-written METHODOLOGY register so '
    'the generated inventory is a superset of the table it replaces, not a '
    'reduction of it.';

COMMENT ON TABLE source_registry IS
    'One row per registered source. docs/METHODOLOGY.md is the authority for '
    'which sources exist and what number each carries; this table is the '
    'authority for how each one is acquired and when it is next due. A NULL '
    'is an honest record that the repository does not document the field — '
    'never an inferred value. See docs/SOURCE_REGISTRY.md.';

COMMENT ON COLUMN source_registry.refresh_tier IS
    'Assigned by acquisition mechanics, not importance. A: stable '
    'machine-readable endpoint, stable schema, unattended ingestion safe. '
    'B: landing page plus file download, file URL changes per edition, '
    'layout stable — detection safe, ingestion gated. C: manual only, '
    'unstable schema, third-party or confidential. Undocumented mechanics '
    'are assigned C, because the cautious tier is the correct default.';

COMMENT ON COLUMN source_registry.completeness_note IS
    'Records why fields are null where the reason is known — in particular '
    'whether a null is undocumented (a gap to close) or withheld by '
    'confidentiality policy (not a gap).';

COMMENT ON COLUMN source_registry.target_table IS
    'The principal table this source loads. Sources that write several tables '
    'record the rest in caveats; this column is not a complete table list.';

-- Controlled vocabularies. Guarded so a re-run does not fail on an existing
-- constraint, and so nothing existing is dropped.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_acquisition_method_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_acquisition_method_chk
            CHECK (acquisition_method IN ('api','landing_page','manual','derived'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_cadence_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_cadence_chk
            CHECK (cadence IN ('monthly','quarterly','annual','periodic','static'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_geography_level_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_geography_level_chk
            CHECK (geography_level IS NULL OR geography_level IN
                   ('LAD24','UTLA','BRMA','PFA','LSOA','entity','none'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_ucws_lens_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_ucws_lens_chk
            CHECK (ucws_lens IS NULL OR ucws_lens IN
                   ('primary','secondary','context','infrastructure','none'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_hss_lens_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_hss_lens_chk
            CHECK (hss_lens IS NULL OR hss_lens IN
                   ('primary','secondary','context','infrastructure','none'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_refresh_tier_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_refresh_tier_chk
            CHECK (refresh_tier IN ('A','B','C'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_status_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_status_chk
            CHECK (status IN ('active','pending_build','deprecated','superseded'));
    END IF;

    -- A source cannot supersede itself.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_registry_superseded_by_not_self_chk') THEN
        ALTER TABLE source_registry ADD CONSTRAINT
            source_registry_superseded_by_not_self_chk
            CHECK (superseded_by IS NULL OR superseded_by <> source_code);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_source_registry_status_tier
    ON source_registry (status, refresh_tier);
CREATE INDEX IF NOT EXISTS idx_source_registry_next_expected
    ON source_registry (next_expected_at);

-- updated_at maintenance. CREATE OR REPLACE on the function is additive in
-- effect: the body is fixed, so replacing it cannot change behaviour.
CREATE OR REPLACE FUNCTION source_registry_touch_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_source_registry_updated_at'
          AND tgrelid = 'source_registry'::regclass
    ) THEN
        CREATE TRIGGER trg_source_registry_updated_at
            BEFORE UPDATE ON source_registry
            FOR EACH ROW EXECUTE FUNCTION source_registry_touch_updated_at();
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 3. source_check_log
--
-- check_failed is a distinct outcome from no_change. A source that could not
-- be reached is not a source that is up to date.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_check_log (
    check_id            bigserial PRIMARY KEY,
    source_code         text NOT NULL REFERENCES source_registry(source_code),
    checked_at          timestamptz NOT NULL DEFAULT now(),
    check_method        text NOT NULL,
    outcome             text NOT NULL,
    fingerprint_before  text,
    fingerprint_after   text,
    detected_period     text,
    http_status         integer,
    error_detail        text,
    notes               text
);

COMMENT ON TABLE source_check_log IS
    'One row per attempt to detect a new edition. An attempt that failed is '
    'recorded as check_failed and is never collapsed into no_change: an '
    'unreachable source is not an up-to-date source.';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_check_log_check_method_chk') THEN
        ALTER TABLE source_check_log ADD CONSTRAINT
            source_check_log_check_method_chk
            CHECK (check_method IN ('govuk_content_api','http_head',
                                    'landing_page','api_probe','manual'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'source_check_log_outcome_chk') THEN
        ALTER TABLE source_check_log ADD CONSTRAINT
            source_check_log_outcome_chk
            CHECK (outcome IN ('no_change','new_edition','url_changed','check_failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_source_check_log_source_checked
    ON source_check_log (source_code, checked_at DESC);


-- ---------------------------------------------------------------------------
-- 4. vw_source_due
--
-- Run-log resolution, in order:
--   1. pipeline_run_log.source_code, where populated.
--   2. Fallback on source_number, but only for numbers that carry no
--      populated source_code on any row. Once a number has been resolved on
--      at least one row, its unresolved rows are known to be contested and
--      are not attributed by number. This is what stops the stale
--      "Source 19 - Land Registry UK HPI" row (S15's build, logged under 19
--      before the renumbering) from being read as an S19 run.
--   3. Neither resolves: never_loaded. Another source's run is never
--      borrowed to fill the gap.
--
-- Successful statuses are whitelisted as ('success','complete') — both appear
-- in the log and both mean success. verify_source_registry.py fails if a
-- status outside the known vocabulary appears, so a new value cannot silently
-- turn a loaded source into never_loaded.
--
-- last_success_at uses COALESCE(completed_at, started_at): two historical
-- rows completed without writing completed_at, and treating those as never
-- having run would be wrong.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_source_due AS
WITH successful AS (
    SELECT source_code,
           source_number,
           COALESCE(completed_at, started_at) AS success_at
    FROM pipeline_run_log
    WHERE status IN ('success', 'complete')
),
resolved_numbers AS (
    -- source_numbers that have been positively resolved on at least one row
    SELECT DISTINCT source_number
    FROM pipeline_run_log
    WHERE source_code IS NOT NULL AND source_number IS NOT NULL
),
by_code AS (
    SELECT source_code AS code, MAX(success_at) AS last_success_at
    FROM successful
    WHERE source_code IS NOT NULL
    GROUP BY source_code
),
by_number AS (
    SELECT s.source_number AS code, MAX(s.success_at) AS last_success_at
    FROM successful s
    WHERE s.source_code IS NULL
      AND s.source_number IS NOT NULL
      AND s.source_number NOT IN (SELECT source_number FROM resolved_numbers)
    GROUP BY s.source_number
),
runs AS (
    SELECT code, MAX(last_success_at) AS last_success_at
    FROM (SELECT * FROM by_code UNION ALL SELECT * FROM by_number) u
    GROUP BY code
),
checks AS (
    SELECT DISTINCT ON (source_code)
           source_code, checked_at, outcome
    FROM source_check_log
    ORDER BY source_code, checked_at DESC
),
base AS (
    SELECT r.source_code,
           r.source_name,
           r.publisher,
           r.cadence,
           r.refresh_tier,
           r.status,
           run.last_success_at,
           r.latest_period_loaded,
           r.last_check_at,
           c.outcome AS last_check_outcome,
           CASE
               WHEN r.next_expected_at IS NOT NULL THEN r.next_expected_at
               WHEN run.last_success_at IS NOT NULL
                    AND r.cadence_months IS NOT NULL
                   THEN (run.last_success_at::date
                         + (r.cadence_months || ' months')::interval
                         + (COALESCE(r.expected_lag_days, 0) || ' days')::interval
                        )::date
               ELSE NULL
           END AS next_due_at
    FROM source_registry r
    LEFT JOIN runs   run ON run.code = r.source_code
    LEFT JOIN checks c   ON c.source_code = r.source_code
)
SELECT b.source_code,
       b.source_name,
       b.publisher,
       b.cadence,
       b.refresh_tier,
       b.status,
       b.last_success_at,
       b.latest_period_loaded,
       b.last_check_at,
       b.last_check_outcome,
       b.next_due_at,
       CASE WHEN b.next_due_at IS NULL THEN NULL
            ELSE (CURRENT_DATE - b.next_due_at) END AS days_overdue,
       CASE
           -- Manual sources are never chased automatically.
           WHEN b.refresh_tier = 'C' THEN 'manual_only'
           -- No successful run resolves to this source.
           WHEN b.last_success_at IS NULL THEN 'never_loaded'
           -- Static series with no expected date is genuinely not due.
           WHEN b.cadence = 'static' AND b.next_due_at IS NULL THEN 'not_due'
           -- No derivable due date: this is not evidence of being up to date.
           WHEN b.next_due_at IS NULL THEN 'check_stale'
           WHEN b.next_due_at > CURRENT_DATE THEN
               CASE WHEN b.last_check_at IS NULL
                         OR b.last_check_at < now() - interval '45 days'
                    THEN 'check_stale' ELSE 'not_due' END
           WHEN b.next_due_at >= CURRENT_DATE - 30 THEN 'due'
           ELSE 'overdue'
       END AS due_status
FROM base b;

COMMENT ON VIEW vw_source_due IS
    'The due list. One row per source_registry row. next_due_at comes from '
    'next_expected_at where set, otherwise from the last successful run plus '
    'cadence_months plus expected_lag_days. Where neither is derivable the '
    'source reports check_stale, not not_due — an underivable date is not '
    'evidence that nothing is owed.';
