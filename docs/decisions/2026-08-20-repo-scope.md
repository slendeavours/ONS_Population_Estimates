# What belongs in this repository, 2026-08-20

This repository is the **reproducibility record for the pipeline and the demand
map**. Someone who has never seen this work should be able to read it and repeat
the exercise: where each source comes from, how it is extracted and loaded, what
the method is, what went wrong and how it was corrected.

**In scope**

- Source acquisition, transformation and load, per source
- The source registry, methodology, data dictionary and node documentation
- The verification suite and the gates that guard publication
- Decision records, including the flaws found in our own method and the fixes
- `index.html` and the exported `data/` the demand map reads

**Out of scope**

Research and analysis produced *for the business* using this data. It is a
different kind of artefact with a different audience, and publishing it exposes
commercial thinking with no reproducibility benefit to anyone.

## What was removed, and why it mattered

27 files were removed on 2026-08-20 and moved to `ucws-repo/analysis/` on the
home drive, outside any checkout:

| Group | Files |
| --- | --- |
| YADA analysis | 6 scripts, 1 decision record |
| SPB | 5 scripts |
| Council briefing | build script, structure doc, prompt |
| Priority market reassessment | 1 script |
| Client paper build and render images | 11 |

Two further files existed only in history and were recovered before the purge:
`Liverpool/data.json` and `docs/SPB_EDITION_1_SUMMARY.md`.

**This was not only tidiness.** The repository is public.
`scripts/priority_market_reassessment.py` ranked authorities on yield and
argued downside cases, which is exactly the commercial framing external papers
built on this data are required to exclude, and it was readable by anyone.
`yada_run2_build.py` queried `commercial_rate_card` and
`v_la_rate_triangulation`. The neutral-naming decision of the same date held —
the partner is not identifiable — but the analysis had no business being
published at all.

The client name appeared in 11 tracked files, including a paper cover line. That
was never a decision, only a default. Remaining references in retained
documentation are now neutral: the technical point survives, the client does not.

History was rewritten with `git filter-repo` to purge these paths from all 141
commits, and the branch was force-pushed. The repository had 0 forks and 0 stars,
so the rewrite was low risk.

## Structure fixed at the same time

`Liverpool/verify/` held ten scripts that are not client work at all:
`run_w1.py` re-runs the national workflow, `rebuild_care_leavers.py` rebuilds all
155 upper-tier authorities, `verify_hb.py` and `verify_pip.py` check all 296.
Pipeline tooling had been filed under a client engagement because that is where
the work happened to be done. It now lives at `scripts/verify/`.

## The test to apply

Before adding a file, ask whether it helps someone **replicate the data**. If it
is instead something the data was used **to argue**, it belongs on the home
drive.
