# S1b Node 2: Map A3 Columns by Label

- **Type:** Code (parse and assert)
- **Purpose:** Match every populated column of sheet A3 to exactly one canonical category code, across two incompatible sheet layouts, and stop the build if anything is unaccounted for.
- **Credential:** None.

## Why this node exists

S1 read A3 by column position. Three to five of its five named support-need
columns hold a different publisher column from the one their name claims, in
an offset that differs between quarters — `mental_health` for 2025Q2 holds
"Care leaver aged 21-24", so Middlesbrough's true 297 is stored as 6. A
positional reader cannot fail loudly; it produces plausible numbers under the
wrong name.

This node is the control that replaces it. See
`docs/decisions/2026-08-14-s1-support-need-column-misalignment.md`.

## Two layouts

| | `legacy_37col` (to 2025Q3) | `v2026_34col` (2025Q4 on) |
|---|---|---|
| Columns | 37 | 34 |
| Header | merged, rows 1–4 | single row, row 5 |
| Footnotes | trailing bare digits | `[note n]` |
| Categories mapped | 31 | 32 |

## Code

Header text for each column is the concatenation of every non-empty cell in
header rows 0–5, normalised:

```python
def norm(text):
    s = str(text).replace("–", "-").replace("’", "'").replace("�", "-")
    s = re.sub(r"\[note \d+\]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()
```

Trailing footnote digits are deliberately **not** stripped. Every pattern is a
substring match that does not anchor at the end, so a trailing footnote is
harmless, whereas stripping trailing digits eats the "17" out of
"aged 16 to 17". That defect was present in the first draft and this node
caught it — the build halted on three unmapped columns rather than loading
them wrongly.

Twenty-four support-need patterns, matched against the normalised text:

| Category code | Pattern |
|---|---|
| `young_person_16_17` | `young person aged 16 ?(?:-\|to) ?17` |
| `young_person_18_25` | `young person aged 18 ?(?:-\|to) ?25` |
| `young_parent` | `young parent` |
| `care_leaver_18_20` | `care leaver aged 18 ?(?:-\|to) ?20` |
| `care_leaver_21_24` | `care leaver aged 21 ?(?:-\|to) ?24` |
| `care_leaver_25_plus` | `care leaver aged 25 ?(?:\+\|or over)` |
| `care_leaver_legacy_combined` | `care leaver.*(?:retired option\|legacy combined)` |
| `physical_ill_health_disability` | `physical ill health` |
| `mental_health_history` | `history of mental health` |
| `learning_disability` | `learning disability` |
| `sexual_abuse` | `sexual abuse` |
| `domestic_abuse` | `experienced domestic abuse` |
| `non_domestic_abuse` | `abuse \(non ?-? ?domestic` |
| `drug_dependency` | `drug dependency` |
| `alcohol_dependency` | `alcohol dependency` |
| `offending_history` | `offending history` |
| `repeat_homelessness_history` | `history of repeat homelessness` |
| `rough_sleeping_history` | `history of rough sleeping` |
| `former_asylum_seeker` | `former asylum seeker` |
| `old_age` | `old age` |
| `served_in_hm_forces` | `served in hm forces` |
| `access_to_education_employment_training` | `access to education` |
| `modern_slavery_victim` | `victim of modern slavery` |
| `difficulties_budgeting` | `difficulties budgeting` |

Plus three breakdown patterns (`hh_no_support_needs`,
`hh_unknown_support_needs`, `hh_one_or_more_support_needs`) and two totals
(`total_support_needs_count`, `hh_owed_prevention_or_relief_duty`).

`domestic_abuse` matches "experienced domestic abuse" only, so it does not
also match "experienced abuse (non-domestic abuse)" in the adjacent column.

## The one/two/three-or-more columns

In the legacy layout these carry no distinguishing text — the header cells are
literally `1`, `2` and `3+` under a merged "Number of households" heading. They
are located by position from the `hh_one_or_more_support_needs` column at
offsets +1, +2, +3, and then **asserted** against those exact header strings.
Position is used only where the label cannot identify the column, and never
without a check. In `v2026_34col` they are fully labelled and matched by
pattern like everything else.

## Assertions — any failure halts

1. No category matches more than one column.
2. No column is claimed by two categories.
3. All 24 support-need categories are found.
4. All three breakdown columns and the needs-count total are found.
5. **Every populated data column is mapped.** A populated column is one
   carrying a value on at least one local authority row. An unmapped populated
   column halts the build and is named in the error.

Assertion 5 is the one that matters. It makes the extractor account for every
column it did not use, which is the only way a silent misalignment surfaces.

## Behaviour

- **Conflict handling:** Not applicable — read-only.
- **Re-run safety:** Pure function of the file. Parsed sheets are cached per
  process because the verification suite reads each quarter three times and an
  ODS parse costs roughly fifteen seconds.

## Connection

None. Operates on files fetched by node 1.

## Verified Output

- `legacy_37col`: 31 columns mapped, 10 quarters (2023Q2–2025Q3).
- `v2026_34col`: 32 columns mapped, 1 quarter (2025Q4).
- Unmapped populated columns: 0 in all eleven.
- Verified 2026-08-14.
