"""RETIRED 2026-10-11: the old S3 refresh (mid-2025 populations). Use
scripts/s3_mye_editions.py. See the message below."""
import sys

_RETIRED = (
    "RETIRED 2026-10-11: use python scripts/s3_mye_editions.py (preview by default). "
    "History: this script loaded the mid-2025 estimates once, on 2026-08-13 "
    "(pipeline_run_log id 86), and widened the la_population key to (lad24cd, "
    "reference_year) in the same run. It upserted every row of the file "
    "(INSERT ... ON CONFLICT DO UPDATE, loaded_at = now()) with no preview and no "
    "--commit flag, loaded a second time as its idempotency gate, and kept no edition "
    "history, so a rerun after a revision would have overwritten the held year in place. "
    "The 2024 population came earlier from the n8n workflow 'ONS Population Estimates "
    "(S3)' (pipeline_run_log ids 2 to 16, 2026-03-26), also retired."
)
print(_RETIRED, file=sys.stderr)
sys.exit(2)
