# 2026-08-16 — S1 reconstruction: suppression markers, the 2025Q1 gap, and a back-series divergence

Item 4. The extraction step that produced `la_statutory_homelessness` existed
nowhere — node 1 fetched a pre-processed CSV from GitHub and the ODS→CSV
conversion behind it was never committed. It is now
`scripts/s1_extract_ods.py`, and running it against the published files
surfaced three separate things.

## 1. `..` was being stored as zero

MHCLG marks a suppressed or unavailable cell `..`. The path that built the
stored data coerced that to `0`. Absent and zero then became the same value,
and every downstream label read the marker as a real observation.

**`submission_gap` fired on eight authorities in run 16. Seven were markers.
One — E06000053, Isles of Scilly — is a genuine zero**, which is plausible for
roughly 2,200 households. So the label was right once in eight.

`num()` now returns `None` for `""`, `-`, `..`, `:`, `*`, `n/a`, `x`. A marker
stays absent rather than becoming a number.

### What was corrected

**54 cells across 2025Q2 and 2025Q3 changed from `0` to NULL** — 19 TA cells
and 35 across the assessment measures. These two quarters and only these two
were corrected, because only they reproduce exactly from the current published
file (see §3), so only for them is the marker verdict evidence rather than
inference.

| Period | TA households | Before | After |
| --- | ---: | ---: | ---: |
| 2025Q2 | non-null LAs | 296 | 284 |
| 2025Q3 | non-null LAs | 296 | 289 |

**129 stored zeros across the seven earlier quarters are untouched** and are
still ambiguous. They cannot be classified until §3 is settled.

## 2. `period` is a financial-year quarter, and 2026Q1 does not exist

The three quarters queued for loading were 2025Q1, 2025Q4 and 2026Q1. Read from
the files' own table titles rather than from their names:

| Repo period | File title says | Status |
| --- | --- | --- |
| 2025Q1 | April to June 2025 | loaded |
| 2025Q4 | January to March 2026 | loaded |
| 2026Q1 | April to June 2026 | **not published** |

`homelessness_quarter_urls.quarter_label` says the same — `2024Q4` is
`Jan-Mar 2025`. The most recent release MHCLG has published is January to March
2026, issued 13 August 2026, and that **is** 2025Q4. There is no third quarter
to load; 2026Q1 is due around October 2026.

`la_statutory_homelessness` now holds **11 quarters, 296 rows each**, 2023Q2
through 2025Q4. The 2025Q1 gap is closed.

## 3. Seven quarters no longer match what MHCLG publishes — open

The reproduction gate compares the stored table against the current published
file, cell by cell, with `IS DISTINCT FROM` so that NULL ≠ 0 is caught.

| Period | Cells differing (excluding marker fixes) |
| --- | ---: |
| 2023Q2 – 2024Q4 | 1,038 – 1,128 per quarter |
| 2025Q2 | **0** |
| 2025Q3 | **0** |

Keys reconcile exactly — 296 both ways, every quarter. The two most recent
quarters reproduce perfectly. The seven older ones do not, on 200–230 of 296
authorities for the assessment measures.

**This is not an extraction fault.** The 2023Q2 file itself gives Hartlepool
172 initial assessments where the table holds 193; headers, column indices and
row counts are identical across the editions.

Three findings point the same way:

- `homelessness_quarter_urls.notes` already says **"Revised"** against exactly
  2023Q2, 2023Q3, 2024Q1–2024Q4 and 2025Q1 — set when the URLs were discovered.
- The GOV.UK collection dates the October–December 2024 release to **June
  2026**, after the 2026-04-01 bulk load.
- 2025Q2 was loaded in that same bulk load and *does* reproduce, so the cause
  is per-quarter republication rather than anything about the loading run.

The likeliest reading is that MHCLG revised the back series after the load. It
is not proven, because settling it needs the original editions, which are gone.

**Impact is small on the published measure and large on the others.**
`households_in_ta` differs on 9–25 authorities per quarter, totals moving by
40–206 households in ~100,000 (under 0.2%). The assessment measures differ on
roughly three quarters of authorities.

**Not restated. This needs a decision** — restating seven quarters of five
measures is materially more than the reproduction gate this item called for.

## 4. Structural changes the header-driven reader caught

**2025Q4 redesigned all three sheets.** Flat single-row headers replacing
merged multi-row ones, every A3 support-need column prefixed `Support need`,
and the whole A3 block shifted three columns left — mental health moved from
column 21 to 18, where column 21 now holds *domestic abuse*.

**A fixed-offset reader would have loaded domestic abuse as mental health and
reported success.** This is the most probable mechanism behind the known S1
support-need misalignment.

Resolution is now: candidates ordered most-specific first, the first candidate
matching *exactly one* column wins, a candidate matching several is abandoned
rather than resolved by preferring the leftmost, and rate columns
(`per thousand`, `(000s)`) are excluded before matching because these tables
are loaded as counts. If nothing resolves uniquely it halts and names the
ambiguity.

**MHCLG also changed container format mid-series** — 2023Q4, 2024Q1 and 2024Q2
are `.xlsx`, everything either side `.ods`. Both readers return the same shape.
Container format is not a property of the data and is not a property of the
pipeline.

## 5. Barnsley and Sheffield

Published on their post-April-2025 codes E08000038/39 against boundaries that
carry E08000016/19. Resolved through `la_code_lookup` on `change_type =
'recode'` only — a recode renumbers one area, whereas `new_unitary` and
`merger` are abolitions, and folding predecessors onto a successor would count
that successor once per predecessor. Anything that still fails to resolve halts.

## 6. Provenance

`la_statutory_homelessness` gains `source_file` and `extracted_at` (additive,
guarded). The seven historical quarters carry NULL there, which is honest —
nobody can say which edition produced them, and that is the whole finding.

## 7. Gate 13

Green. `homelessness_quarter_urls` now carries rows for 2025Q3 and 2025Q4 with
URLs **verified by Content-Length against the stored files**, not assumed from
a page summary. `loaded` is re-derived from actual row counts for all 11 rows
rather than set by whatever intended to load them. The known-red entry is
removed. The suite exits 2 on gate 14 alone.

## 8. Marker sweep across the other MHCLG sources

Run as a data test — does the table hold zeros and NULLs, or only zeros?

| Source | Table | Verdict |
| --- | --- | --- |
| S1b | `la_homelessness_support_needs` | both present — loader distinguishes |
| S2 | `ro4_housing_expenditure` | both present — loader distinguishes |
| S13 | `la_housing_register` | both present — loader distinguishes |
| **S10** | **`la_rough_sleeping`** | **22 and 27 zeros, no NULL anywhere** |

S10 carries S1's signature. A genuine zero is highly plausible for a rough
sleeping snapshot, so this is lower risk than S1 was — but the table cannot
prove it either way, and that is the same blind spot. Open; settling it needs
the source file.

Separately, `la_housing_register.reasonable_preference` is NULL in all 3,256
rows — a column that has never been populated.

## Still open

1. The seven-quarter divergence in §3 — restate to the current editions or not.
2. Re-running W1 so the map reflects 2025Q4. Blocked by the same-day guard:
   run 16 exists and backs the current feed, so this means discarding run 16
   and moving the published TA headline from **124,142 to 130,775**.
3. S10 zeros — verify against source.
