# Demand map assurance, 2026-08-20

Phase 5 of the August 2026 assurance programme: check the map's layers, legends
and badge counts, and re-export it from corrected data. Four things were wrong.
The map had been serving run 17, which predated every source correction.

## 1. A checked-in workflow backup had drifted from the workflow

Re-running W1 by hand first failed with a cardinality violation on node 5. The
cause was not the workflow. The node SQL had been taken from
`build_reports/w1_workflow_backup.json`, and that copy was older and shorter
than what n8n actually holds: its `la_population` join was not pinned to the
latest vintage, so with two vintages loaded every authority fanned out to two
rows. The backup also omitted the "Signal Column Pre-flight" node and had
National Aggregates and LA Signals in the wrong order.

The live node in `n8ndb` was correct throughout. There was no live bug.

**Decision: n8ndb is the only source for node SQL.** `Liverpool/verify/run_w1.py`
now reads the nodes and their connection order from `workflow_entity` and
executes them in that order, which is what `scripts/w1_contract_check.py`
already did. The stale extraction was deleted. `*.sql` is globally gitignored,
so no copy of a node query can be committed and later be mistaken for current.

Backups under `build_reports/` remain provenance records of what a node was at a
point in time. They are not to be executed.

## 2. The map's house price layer had never worked

`avg_price_all` is not a `staging_la_signals` column; it lives in
`la_house_prices` and had to be joined on export, exactly as
`hb_sa_claimants_latest` is joined from S8b. It never was. The "Avg House Price"
layer read a field that was not in either exported file, so it rendered nothing.
`annual_change_pct` was missing for the same reason, so the popup's "Annual
Change" row always showed a dash.

Both are now joined from `la_house_prices` at MAX(period) and, more importantly,
both are named in the hard-stop check at the end of `export_map_data.py`. That
guard already existed; the two fields had simply never been added to it, which
is why a silent omission survived. 293 of 296 authorities carry a price.

## 3. The source count was wrong in three different places

The badge said 17, the sidebar heading said 18, and the list underneath named
17 sources. The list included four the map does not use (MoJ offender
statistics, OHID substance misuse, NHS discharge, Census 2021) and omitted
Council Taxbase, which drives a live layer.

**Decision: the count comes from `source_registry.publish_map`.** That flag was
itself wrong for three sources the map plainly surfaces, so it was corrected via
`scripts/backfill_source_registry.py`, the registry being generated:

| Source | Why it is on the map |
| --- | --- |
| S3 ONS Mid-Year Estimates | population is shown in the popup |
| S7 ONS Open Geography Portal | supplies the boundary geometry |
| S12 MHCLG EFS and S.114 | drives the two stress flags |

That gives **15** non-confidential sources for 13 layers (LHA supplies two), and
badge, heading and list now agree with each other and with the registry. S20 is
confidential and is excluded by the same query that produces the count.

## 4. Two layers invited a false reading

**MARAC is published by police force area, not by local authority.** Knowsley,
Liverpool, Sefton, St Helens and Wirral all carried 5,504, because that is the
Merseyside total. The map said nothing about this, so a reader would take it as
Liverpool's own count. The layer now states it in the legend and the popup row
is labelled "MARAC Cases (force area)". This mirrors the caveat the LandAid
paper is required to carry.

**Care leaver accommodation is an upper-tier duty.** 132 of 296 districts carry
a value; the rest are unshaded because the duty is not theirs, not because data
is missing. The legend now says so and the popup row is labelled
"Care Leavers (upper tier)".

A general `note` field was added to the layer definition for this, rendered
beneath the legend, so any layer can state a caveat where the number is read.

## Also done

Every layer description now carries its own vintage, so no reader has to assume
one: TA to March 2026, HB February 2026, register 2025, care leavers to March
2025, MARAC 2025-26, rough sleeping autumn 2025, IMD 2025, LHA 2026-27, HPI
June 2026, CTB 2025, RO4 2024-25.

## Verified after re-export

Run 18, 296 signal rows, 296 GeoJSON features, all 296 joining by `lad24cd`.
Every layer field present in both files. England rough sleeping 4,793, matching
the publication. Liverpool: 35 rough sleepers against 47 the prior year, 208
care leavers in supported settings, 5,504 MARAC cases across Merseyside, 1,847
households in temporary accommodation, average price £185,307.

The map canvas could not be rendered for visual checking because the browser
pane used for it has no WebGL. The data files, layer definitions, badge and
source list were verified in the loaded page instead.
