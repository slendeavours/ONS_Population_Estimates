"""RETIRED 2026-10-11: the first S14 builder (BRMA rates and the BRMA crosswalk).
Use scripts/s14_lha_editions.py. See the message below."""
import sys

_RETIRED = (
    "RETIRED 2026-10-11: use python scripts/s14_lha_editions.py (preview by default). "
    "History: this was the first S14 builder. It had no preview and no --commit flag: "
    "one run created the tables if missing and upserted brma_lha_rates and "
    "la_brma_mapping (INSERT ... ON CONFLICT DO UPDATE, loaded_at = NOW()). It computed "
    "the weekly rates with float round(), its parse_money returned 0 for a blank cell "
    "(latent: the 2026-27 file has no blank), and it took BRMA names from the VOA layer "
    "without converting 'X & Y' to the DWP spelling 'X and Y', so a rerun re-creates the "
    "broken W1 join of 2026-08-20 (decision note 2026-08-21). It also printed a SQL "
    "template for W1 node 5 (with imd2025.rank) for a person to paste; that template was "
    "never executed and went with this file."
)
print(_RETIRED, file=sys.stderr)
sys.exit(2)
