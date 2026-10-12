"""RETIRED 2026-10-12: the old S21 build (ONS statistical nearest neighbours).
Use scripts/s21_neighbours_editions.py. See the message below."""
import sys

_RETIRED = (
    "RETIRED 2026-10-12: use python scripts/s21_neighbours_editions.py (preview by default). "
    "History: this script loaded la_statistical_neighbours once, on 2026-07-24 "
    "(pipeline_run_log id 66, 1,480 rows), from data/raw/ons_statistical_neighbours_2026.xlsx; "
    "it was first committed on 2026-08-20 (59ae151), four weeks after that load. It wrote when "
    "run, with no preview and no --commit flag, by TRUNCATE then insert, so a rerun would have "
    "replaced every row and kept no edition history. Its rule: take the list of authorities from "
    "the latest staging_la_signals run (a Workflow 1 output, so the result depended on that "
    "table), drop every neighbour not in it (all Welsh, Scottish and Northern Irish ones), keep "
    "the first five left in the publisher's order and renumber them 1 to 5; it halted only if an "
    "authority had no usable neighbour. It checked nothing about the file itself (no Cover, "
    "sheet-title or header check) and skipped a blank or non-English code cell silently. It "
    "recoded Barnsley and Sheffield (E08000038 and E08000039 to E08000016 and E08000019) with a "
    "private dict whose comment says the file 'still carries the pre-recode codes'; that is "
    "the wrong way round, because the file carries the new codes E08000038 and E08000039 "
    "and the dict maps them to the old ones that la_boundaries and the live table use. "
    "The loader keeps the same rule (LIVE_RULE) with la_boundaries and geography.resolve, "
    "and stores the publisher's whole table as editions."
)
print(_RETIRED, file=sys.stderr)
sys.exit(2)
