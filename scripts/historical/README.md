# Historical scripts

These scripts patched or ran Workflow 1 (W1) inside n8n, before W1 moved into the repo (`sql/w1` and `scripts/w1_run.py`). They are kept for history only and are not used; their imports may no longer resolve.

`s8b_hb_accom_type_build.py` is the original S8b (Stat-Xplore Housing Benefit caseload by accommodation type) loader. It upserted the live table in place, so a DWP revision overwrote what was held. It was replaced on 2026-10-08 by `scripts/s8b_hb_editions.py`, which keeps every revision as a numbered edition. It is kept for history only and cannot run from here: it imports the shared `statxplore_client`, which it can no longer find from the `historical` folder.

`s15_hpi_build.py` is the original S15 (Land Registry UK HPI) loader. It inserted new months only (`ON CONFLICT DO NOTHING`) and discarded revisions of months already held, and it took its edition name from a constant and its files from a hand-off folder under the temp directory. On 2026-10-09 it was replaced by `scripts/s15_hpi_editions.py`, which keeps every revision as a numbered edition. **Do not run it**: it now stops at once with a RETIRED message.

`s19_pip_build.py` is the original S19 (Stat-Xplore PIP claimants) loader. It wrote text month labels such as `Apr-26` and upserted the live table in place. On 2026-10-08 the live months were relabelled to `yyyymm` keys (`Apr-26` is now `202604`, `Jul-26` is `202607`) and the loader was replaced by `scripts/s19_pip_editions.py`. **Do not run it**: it would write labels again and undo the relabel. The saved queries `scripts/s19_query_total.json` and `scripts/s19_query_enhanced_dl.json` were written by this old loader; they are kept but nothing writes them now.
