# 2026-08-14 — S1's support-need columns hold the wrong publisher columns

Found during S1b discovery, which had to match the already-loaded columns
against Table A3 in order to produce an honest published-versus-loaded gap
table. The gap table is the thing that found it.

## What is wrong

`la_statutory_homelessness` carries five named support-need columns. Three to
five of them, depending on the quarter, contain a different publisher column
from the one their name claims.

**2025Q2** — every support-need column is four positions to the left of its
label:

| Column | Holds | Should hold |
|---|---|---|
| `mental_health` | Care leaver aged 21-24 | History of mental health problems |
| `learning_disability` | Care leaver aged 25+ | Learning disability |
| `drug_dependency` | Learning disability | Drug dependency needs |
| `alcohol_dependency` | At risk of / has experienced sexual abuse | Alcohol dependency needs |
| `rough_sleeping_history` | Drug dependency needs | History of rough sleeping |

**2025Q3** — a different offset again. `mental_health` and
`learning_disability` are correct; `drug_dependency` holds Alcohol dependency,
`alcohol_dependency` holds Offending history, and `rough_sleeping_history`
holds History of repeat homelessness.

`support_needs_total` is correct throughout. It holds the publisher's
"households with one or more support needs owed a duty", matching on 294 of
296 authorities for 2025Q3.

## Why this is proved rather than inferred

For 2025Q2 the evidence is closed. `homelessness_quarter_urls` names the exact
asset the quarter was loaded from —
`media/699dab21532c9ad91ebbcbe8/Statutory_Homelessness_Detailed_Local_Authority_Data_202509.ods`
— and that asset is byte-identical to the file this investigation read, 1,789,649
bytes. Same file in, different numbers out. A revision cannot explain it,
because there is only one file.

Middlesbrough makes it concrete. The loaded `mental_health` for 2025Q2 is 6.
The publisher's mental health figure for Middlesbrough that quarter is 297.
Six is the Care leaver aged 21-24 figure, four columns to the left.

The match rate is 289 of 296 authorities on every mis-mapped column, which is
the signature of a column offset rather than a data difference: a wrong column
matches almost everywhere, and the handful that do not are authorities where
two adjacent categories happen to differ.

## What it does not affect

**Nothing published has ever read these columns.** `staging_la_signals` takes
`ta_households_current` and `ta_households_prev_year` from S1, and Workflow 1
node 5 reads `households_in_ta`. No query in the repository reads
`mental_health`, `learning_disability`, `drug_dependency`,
`alcohol_dependency` or `rough_sleeping_history` from
`la_statutory_homelessness`. The System Pressure Briefing and the council
briefings read the signals table, never this one.

So this is a latent data-quality defect, not a published-output error. That is
the same finding shape as the 2025Q1 quarter gap recorded in
[2026-08-14-s1-quarter-gap-and-provenance.md](2026-08-14-s1-quarter-gap-and-provenance.md):
the exposure is nil and the defect is real, and both statements have to be
made.

## A third defect found on the way

S1 splits Barnsley and Sheffield across two codes. `la_statutory_homelessness`
holds them as `E08000016` and `E08000019` for 2023Q2 to 2024Q4, and as
`E08000038` and `E08000039` for 2025Q2 and 2025Q3. A time series for either
authority silently breaks in 2025.

`la_code_lookup` resolves `E08000038` to `E08000016` — the pipeline's
canonical code for Barnsley is the older one, because `la_boundaries` retains
it. S1 did not resolve through the lookup, which is what operating rule 5
exists to prevent.

## What was done about it

**Nothing to S1's data.** S1 keeps the temporary accommodation series that
feeds Workflow 1, and that series is not implicated. Rewriting five columns of
a table nothing reads, during a build commissioned to add a different source,
would be scope the work did not ask for.

**S1b carries all 24 published categories, including the five S1 nominally
holds.** That was going to be the recommendation from the multi-response
argument anyway; this makes it necessary rather than tidy. The five S1 columns
should be treated as deprecated and read from `la_homelessness_support_needs`
instead.

## What stops it happening again

S1b extracts by matching column labels, not by counting positions, and:

- every populated column in the sheet must match exactly one canonical
  category, and every expected category must match exactly one column, or the
  build stops and names what it could not place;
- publisher codes resolve through `la_code_lookup`, so Barnsley is one
  authority across all eleven quarters;
- the resolved source URL is stored on every row, so the file that produced any
  figure can be re-read.

The label matcher proved itself before it was trusted. Its first run halted on
three unmapped columns in the 2026 layout, because the footnote stripper was
eating the trailing digits of "aged 16 to 17". A positional reader would have
loaded that silently — which is exactly the failure being documented here.

## The general point

S1 was built without per-row provenance and without a reconciliation between
what was extracted and what the publisher labelled. Both gaps were invisible
for months and both surfaced within a day of each other, from two different
directions. A column of plausible numbers under the wrong name is the quietest
kind of wrong there is, and the only thing that catches it is making the
extractor account for every column it did not use.
