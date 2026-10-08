# Historical scripts

These scripts patched or ran Workflow 1 (W1) inside n8n, before W1 moved into the repo (`sql/w1` and `scripts/w1_run.py`). They are kept for history only and are not used; their imports may no longer resolve.

`s8b_hb_accom_type_build.py` is the original S8b (Stat-Xplore Housing Benefit caseload by accommodation type) loader. It upserted the live table in place, so a DWP revision overwrote what was held. It was replaced on 2026-10-08 by `scripts/s8b_hb_editions.py`, which keeps every revision as a numbered edition. It is kept for history only; it imports `statxplore_client` as the new loader does, but should not be run.
