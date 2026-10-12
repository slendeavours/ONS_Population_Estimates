"""RETIRED 2026-10-11: the simplified S14 builder, top-level code with no main
guard. Use scripts/s14_lha_editions.py. The guard also runs on import."""
import sys

_RETIRED = (
    "RETIRED 2026-10-11: use python scripts/s14_lha_editions.py (preview by default). "
    "History: this was top-level code with no main guard, so importing it ran the whole "
    "build. A routine import check did exactly that on 2026-08-20 (pipeline_run_log id "
    "97): it ran DROP TABLE ... CASCADE on la_brma_mapping and brma_lha_rates and rebuilt "
    "both, leaving 'X & Y' BRMA names that no longer matched the rates (decision note "
    "2026-08-21). It had no preview and no --commit flag, skipped any rate row that failed "
    "to parse silently, and wrote the log note found on ids 52 and 97."
)
print(_RETIRED, file=sys.stderr)
sys.exit(2)
