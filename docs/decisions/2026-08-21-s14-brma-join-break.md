# S14 BRMA join broken by an accidental rebuild, 2026-08-21

## What happened

On 2026-08-20 a routine check ran `import s14_lha_rates_build_v2` to confirm the
file parsed. That script is 425 lines of top-level code with no main guard, so
the import executed the whole S14 build and rebuilt `brma_lha_rates` and
`la_brma_mapping` from source.

The rebuild was checked at the time and looked clean: 151 BRMAs, 296 mappings,
Liverpool unchanged at 79.47 and 115.38 weekly. It was not clean.

## Two defects, both invisible to a row count

**Name spelling.** `la_brma_mapping` takes its `brma_name` from the VOA boundary
layer, which writes `Hull & East Riding`. `brma_lha_rates` takes it from the DWP
CSV, which writes `Hull and East Riding`. W1 node 5 joins the two on that text
key. The rebuild wrote 37 mapping rows in the ampersand form, so **38
authorities would have returned NULL LHA rates on the next W1 run**.

The spot check missed it because Liverpool's BRMA is `Greater Liverpool`, which
contains no ampersand. Checking the one authority under discussion is not a
check on a class of error.

**Missing rate row.** Both S14 scripts read the DWP file as "row 0 title, row 1
headers, data from row 2". The file's row 0 *is* the header row and data starts
at row 1, so the first BRMA alphabetically was consumed as a header on every
run. Ashford's rates did not exist in the table at all, and 151 of 152 BRMAs
loaded.

Verified against the source: the file has 152 BRMAs, row 0 reads
`BRMA,Monthly UC LHA rates 2026 to 2027 - SAR,...`, row 1 is Ashford, and no
name in it contains an ampersand.

## What was done

- Both scripts now read row 0 as the header and data from row 1.
- Ashford's rate row was restored from the published CSV.
- The 37 mapping rows were normalised to the DWP spelling.
- `s14_lha_rates_build_v2.py` now raises ImportError if imported, because
  wrapping its flow in a function would mean re-indenting the whole file.
- Verified: 152 rate rows against 152 in the source, 296 of 296 authorities
  resolving, and every authority reproducing run 18's stored LHA values exactly.

Run 18 predates the rebuild and was never affected, so nothing published was
wrong. The exposure was entirely forward-looking.

## Gate 18

A count could not see either defect, so the suite now asserts the join instead:
every authority in `la_brma_mapping` must resolve to a rate row at the latest
financial year. Re-introducing the ampersand spelling in a rolled-back
transaction makes it fail on 42 rows, so it detects the fault it was written for.

## The general lesson

**Never import a build script to test that it parses.** Use `py_compile`. An
import runs module-level code, and a build script's module level is the build.

**When two tables are joined on a name, the names have two publishers.** Assert
the join, not the row counts on either side.
