# S6: asylum_series_breaks row 80, the subsistence-only authorities

Status: done 2026-10-10. Found by the final review of the S6 editions branch
(`.superpowers/sdd/2026-10-10-s6-editions/final-review.md`, M2).

## The sentence

`asylum_series_breaks` row 80 (Section 95, 2023-12-31 to 2024-12-31) said, in its
`comparability` text:

> Local authority counts and England totals are depressed across those five periods. 32 English LAs that appeared only via subsistence-only claimants at 2023-09-30 disappear entirely from 2023-12-31.

The count of 32 is right. "Disappear entirely from 2023-12-31" is not.

## Evidence (read-only, on the held `la_asylum_support`, 2026-10-10)

| Measure | Authorities |
|---|---:|
| Present at 2023-09-30 with only Subsistence Only rows | 32 |
| of those, absent at 2023-12-31 | 26 |
| of those, absent in all five break periods | 13 |
| of those, present again at 2025-03-31 | 29 |
| All authorities present at 2023-09-30 and absent at 2023-12-31 | 34 |

The other six of the 32 have other accommodation types at 2023-12-31. The other
claims in the row hold: in the five periods the 5 Subsistence Only rows of
`la_asylum_support_unallocated` hold 18,598 people, and `la_asylum_support` has no
Subsistence Only row.

## Change

The new text: "Local authority counts and England totals are depressed across
those five periods. 32 English LAs appeared at 2023-09-30 only through
subsistence-only rows; 26 of them are absent at 2023-12-31 and 13 are absent in
all five periods."

- One UPDATE of `comparability` on `asylum_series_breaks` where `break_id = 80` and
  the text equals the old sentence above, in one transaction that rolls back unless it
  changes exactly 1 row. It changed 1 row.
- The same text in `SERIES_BREAKS` in `scripts/s6_asylum_editions.py`, so gate 20
  (which compares the table with the constant) still passes.

The row is the pipeline's own annotation, not a publisher figure or a sent
deliverable, so the never-overwrite rule does not apply. The old sentence is kept
here, in git, and in the retired `scripts/historical/s6_asylum_build.py`. Nothing
else was written to the database.
