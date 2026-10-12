# S21: ONS statistical nearest neighbours (Table 7a, LTLA global)

<!-- repo-meta
status: active
last-reviewed: 2026-10-12
type: source
consumed-by: scripts/s21_neighbours_editions.py, scripts/s21_neighbours_editions_verify.py
-->

| | |
|---|---|
| Publisher | Office for National Statistics (ONS), the Subnational Methods for Dissemination team |
| Series | "Clustering similar local authorities and statistical nearest neighbours in the UK", Table 7a: LTLA global statistical nearest neighbours. One edition (2026), released 18 March 2026 |
| Landing page | https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/datasets/clusteringsimilarlocalauthoritiesandstatisticalnearestneighboursintheuk (its `/data` JSON lists the editions) |
| Cadence | Irregular. The dataset page lists one edition and no earlier versions |
| Live table | `la_statistical_neighbours` (1,480 rows), a derived subset of the editions, one release (`release_date` 2026-03-18) |
| Editions | `la_statistical_neighbours_editions` (5,920 rows in edition 1; append-only) with a `la_statistical_neighbours_editions_file_checks` ledger |
| Natural key | live and editions `(lad24cd, statistical_neighbour_lad24cd)`, editions also by `release_date` and edition |
| Loader | `scripts/s21_neighbours_editions.py`; gates `scripts/s21_neighbours_editions_verify.py` (21) |
| Record | `docs/decisions/2026-10-11-s21-editions-first-load.md` |

**Not a W1, export or map input.** The two readers are `analysis/council_briefing/council_briefing_build.py` and `analysis/liverpool_paper/extract.py`; each selects `statistical_neighbour_lad24cd` by name for one authority. A change to the live table changes the comparator sets they build.

## What the table is

Table 7a lists, for each lower-tier local authority in the UK, its 20 statistical nearest neighbours: a code and a name for each of "Neighbour 1" to "Neighbour 20". The file holds 361 subject rows (England 296, Scotland 32, Wales 22, Northern Ireland 11). Of the 5,920 neighbour entries of the 296 English authorities, 5,447 are English, 362 Welsh, 97 Scottish and 14 Northern Irish.

What the publisher says about the method (the file's Notes sheet, read 2026-10-12):

- The statistics are labelled "official statistics in development" (note 1).
- The models use the local authority and combined authority boundaries as at March 2026 (note 2).
- Nearest neighbours are calculated using euclidean distance (note 17). The global neighbours use the same indicators as the global clustering model but are not part of it, so an area's neighbours may not all be in its cluster (note 18). The indicators are listed on the file's Data Sources sheet.
- City of London and Isles of Scilly are only in the demographic clustering model because of missing data; they appear in all the nearest neighbours models (note 7). Both are in Table 7a with 20 neighbours.
- Missing values are marked "[x]" and suppressed ones "[c]" (note 12). Table 7a holds neither.

**What the file does not say.** It does not define the order of the neighbours. The headers read "Neighbour 1" to "Neighbour 20" and the table is of nearest neighbours, so the loader takes the file's order, Neighbour 1 first, as the publisher's rank. That is an assumption, shared with the retired build.

## What the live table is: not the publisher's top five

`la_statistical_neighbours` holds, for each English authority, **the five nearest English neighbours of the publisher's twenty, renumbered 1 to 5**. The rule is one constant in the loader, `LIVE_RULE`: per authority, the publisher's neighbours whose code is English (E06, E07, E08, E09), in the publisher's order; the first five are live, numbered 1 to 5. Scott decided on 2026-10-11 to keep it. It is the rule the old build applied; applied to the held file it reproduces all 1,480 live pairs and ranks (296 of 296 authorities, 0 differences either way).

What it does on the March 2026 file:

- 238 of the 1,480 pairs carry a rank different from the publisher's, in 71 authorities (those whose publisher top five include a Welsh, Scottish or Northern Irish authority).
- 105 pairs come from publisher ranks 6 to 15 (the furthest is 15).
- No authority has fewer than five live pairs.

**The live source text does not name the rule.** The `source` column reads "ONS Clustering similar LAs and statistical nearest neighbours, Table 7a (LTLA global), Mar-2026 edition" on every row, from the old build. It says nothing about English-only or renumbering. It was left as held: `refresh-latest` had nothing to change (live equals `LIVE_RULE` applied to the latest edition), and a refresh only to rewrite the text was not run. A release loaded later carries a label containing "live rule: the five nearest English neighbours of the publisher's twenty, renumbered 1 to 5", and `refresh-latest` copies the latest edition's label to live; edition 1's label is the as-loaded one, which does not. Until a later edition is refreshed into live, this note and the registry row are where the rule is stated. Anything quoting these comparators should say "five nearest English statistical neighbours", not "the ONS top five".

**The alternative**, if Scott wants it later: the publisher's top five as published, English only, ranks not renumbered (71 authorities would have fewer than five comparators). That is a change of `LIVE_RULE` and one `refresh-latest --acknowledge 2026-03-18`, not a reload: the editions keep `publisher_rank` and `england_rank` for every row. Gate 2 of the verify script pins "English only, ranks 1 to 5, five per authority" and would need a deliberate change.

## Discovery

`load` reads the dataset page's `/data` JSON. Its `datasets` list names the editions as `<landing>/<yyyy>`; the loader takes the newest and reads that edition's `/data` JSON for the one `.xlsx` download and its `releaseDate`. It halts, naming what it saw, when no edition is listed, a listed URI does not fit `<landing>/<yyyy>`, an edition is listed twice, the edition's label differs from its URI, or there is not exactly one `.xlsx` download. A newer edition than the one held is named in the preview; it is read, previewed and stored as editions only (below).

The file is downloaded to `data/raw/s21_neighbours/` (git-ignored; a preview downloads too and writes nothing to the database). A download never overwrites a same-named file with different content (it is saved as `<stem>-<sha8><suffix>`); redirects are followed and the final URL recorded. `--file PATH` (through `manual_input.require_under`) reads a local copy, with `--no-page` to skip the page. The held file is `2026clusteringsimilarlocalauthoritiesandnearestneighbours.xlsx` (sha256 starting 26e9ca037015db12), byte-identical to `data/raw/ons_statistical_neighbours_2026.xlsx`, the file the old build read.

## File identity (rule 3): from the file, on every path

The release, the period and the rank of a file are read from the file:

- The Cover sheet's title "Clustering similar local authorities and statistical nearest neighbours in the UK" and its line "The data tables in this spreadsheet were originally published at 9.30am 18 March 2026." The Cover date is the release date and the rank. It is never the file name, the URL or the API date.
- The Contents entry for sheet 7.a and the sheet's own title, "Table 7a: LTLA global statistical nearest neighbours".
- The header row, exactly: `Local Authority Code`, `Local Authority Name`, then `Neighbour n: code`, `Neighbour n: name` for n = 1 to 20 (42 columns).
- Note 2 (boundaries "as at March 2026") and note 12 (the `[x]` marker) are captured verbatim into the edition's label; docProps modified (2026-03-17T14:25:14Z) is printed.
- When the page is used, the edition label's year must be the Cover's year, and the API `releaseDate` (2026-03-18T00:00:00.000Z) must be the Cover date or the evening before (a midnight in British Summer Time).

## Blanks and markers (rule 1)

Every subject row carries 20 code and name pairs. A blank pair, the `[x]` marker or any other text in a code cell halts, naming the cell; there is none in the 2026 file. Nothing is coerced. A code must be nine characters (`[ENSW]` and eight digits); a subject listing itself or one neighbour twice halts.

## Geography (rule 4)

The file carries Barnsley and Sheffield as **E08000038 and E08000039**, as subject rows and in 93 neighbour cells of the whole sheet (74 and 19; 73 of them in English rows), and never as E08000016 or E08000019. Source 21 is declared `new` in `scripts/geography.py`. The loader resolves every subject code and every English neighbour code through `geography.resolve`, so they become the canonical E08000016 and E08000019, and each canonical code must be in `la_boundaries`; anything else is UNEXPLAINED and stops. Welsh, Scottish and Northern Irish neighbour codes are stored verbatim and never resolved. There is no private recode dict.

## Editions and revisions

An edition is the publisher's Table 7a for every English authority: 296 subjects x 20 neighbours = 5,920 rows. Each row holds the resolved neighbour code, the publisher's code and name, `publisher_rank` (1 to 20), `england_rank` (the order among the English neighbours; NULL, with the reason in `null_reasons`, for a neighbour that is not English: 473 rows), `neighbour_nation`, and `neighbour_rank` (the live rank under `LIVE_RULE`; NULL outside live, reason in `null_reasons`).

**Revision evidence: none.** The dataset page lists one edition and no earlier versions, so nothing has been compared (registry `revises_back_series` false). A reissue of the 2026 release (same Cover date, different content) is the next edition; it reaches live only through `refresh-latest`, which needs `--acknowledge 2026-03-18` when it changes a live pair or rank. A byte-identical re-read is `unchanged` on every path, including against edition 1 "as loaded".

**A new ONS edition is not a refresh.** Another Cover date is a new vintage. `load --commit` stores it as editions only; it never reaches the live table through `load` or `refresh-latest` (`live_single_period`: the live table has a CHECK allowing one release date). Adopting it would change every authority's comparators, so it is a decision for Scott and a deliberate change (the live release, the CHECK, the gates). `check_sources.py 21` reads the dataset page's `releaseDate` and reports a later one.

Edition 1 is the table as held, recorded by the one-off `migrate-legacy` (2026-10-12, run-log 543): the held file read again, with the proof that `LIVE_RULE` applied to it reproduces every live row. Its content sha256 is in the decision note.

## Stop limits

In `load` a stopped release is REJECTED: nothing stored, no ledger row, exit 1, and a partial run-log row. It stops when:

- fewer than 296 English authorities are in the file, or (a reissue) an authority of the held edition is missing: no flag releases it;
- the release is not 2026-03-18 and live is empty: no flag releases it;
- the Cover date equals the held release but the content differs (two files claiming the same release, or a publisher reissue keeping its date): `--accept-reissue 2026-03-18` stores it as the next edition, after reading the release's notes;
- a reissue changes any pair, rank, name or nation against the held edition: `--acknowledge 2026-03-18` releases it too, after reading the preview;
- the file is older than the newest release held: `--allow-older-file`.

Each acknowledgement is written to the edition's label and the run log.

## How to run

Run from `ONS_Population_Estimates`. Every writing command previews unless given `--commit` (`--simulate` runs it and rolls back).

```bash
python scripts/s21_neighbours_editions.py status
python scripts/s21_neighbours_editions.py load            # preview; downloads the newest edition
python scripts/s21_neighbours_editions.py load --simulate
python scripts/s21_neighbours_editions.py load --commit
python scripts/s21_neighbours_editions.py refresh-latest   # preview; --commit applies a reissue
python scripts/s21_neighbours_editions_verify.py
```

Other commands: `ddl` and `migrate-legacy FILE` (both already run), `restore-edition RELEASE N`. `load` takes `--file PATH [--no-page]`, `--recheck RELEASE`, `--allow-older-file`, `--accept-reissue RELEASE` and `--acknowledge RELEASE`. There is no `sync-new`.

**After any `refresh-latest` that changes live, gates 16 and 17 of the verify script fail** until the decision note's `before-state` and `live-read` hash lines are updated. They compare edition 1 and the live rows with the migration-time state, so they fail by design when a reissue changes a live pair; read the change, then update the lines deliberately (the verify script prints the new ones with `--print-note-lines`). A plain `load` follows the newest edition on the landing page; a reissue of the live release after a later edition exists goes through `--file --no-page`.

## Known traps

- **Calling the live ranks the publisher's.** They are renumbered among English neighbours; 238 pairs differ.
- **Assuming five neighbours means the first five.** 105 pairs come from publisher ranks 6 to 15.
- **Using E08000016 and E08000019 in the file's terms.** The file has E08000038 and E08000039; the canonical key is E08000016/19 while `la_boundaries` is LAD May 2024.
- **Putting a new ONS edition in the live table.** The CHECK refuses it, and so do the loader and the gates.
- **Trusting the live `source` text.** It does not name the rule (above).
- **Reading the order as defined.** The file does not define it.

## History

- **2026-07-24 08:56 UTC, run-log 66:** `scripts/s21_statistical_neighbours_build.py` loaded 1,480 rows from `data/raw/ons_statistical_neighbours_2026.xlsx`, by TRUNCATE then insert. The script was first committed on 2026-08-20 (59ae151), four weeks later. It took the list of authorities from the latest `staging_la_signals` run (a W1 output), dropped every neighbour not in it (all Welsh, Scottish and Northern Irish ones), kept the first five left in the publisher's order and renumbered them 1 to 5. It wrote when run (no preview, no `--commit`), checked nothing about the file itself, and skipped a blank or non-English code cell silently. It recoded Barnsley and Sheffield (E08000038/39 to E08000016/19) with a private dict whose comment says the file "still carries the pre-recode codes". That comment is the wrong way round: the file carries the new codes, which the dict maps to the old ones that `la_boundaries` and the live table use (checked 2026-10-12 against the file, `la_boundaries` and the live table).
- **2026-10-11:** Scott decided to keep the current live rule. The old build was not run or imported while the loader was written.
- **2026-10-12:** `ddl` (editions table, ledger, the live `release_date` column) and `migrate-legacy` (edition 1, run-log 543); no live pair, rank or code changed. The old build is retired: it exits 2 at its first statement, in a test that runs it only in a scrubbed sandbox.
