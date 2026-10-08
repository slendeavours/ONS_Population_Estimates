DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM staging_runs
        WHERE run_date::date = CURRENT_DATE
        AND status = 'complete'
    ) THEN
        RAISE EXCEPTION 'A completed run already exists for today (%). Delete it from staging_runs first if you want to re-run.', CURRENT_DATE;
    END IF;
END $$;

INSERT INTO staging_runs (status, notes)
VALUES ('in_progress', 'Workflow 1 pre-computation run')
RETURNING run_id;