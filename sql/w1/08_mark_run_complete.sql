UPDATE staging_runs
SET status = 'complete'
WHERE run_id = $1;