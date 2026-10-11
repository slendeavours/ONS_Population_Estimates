# S3 ONS mid-year population estimates: first load on the editions engine, and the July 2026 revision of mid-2024

Status: done 2026-10-11. Works like [S10](2026-10-10-s10-editions-first-load.md) (a file restates held years).
Loader: `scripts/s3_mye_editions.py`; gates: `scripts/s3_mye_editions_verify.py` (all PASS, exit 0).
Design: `docs/superpowers/specs/2026-10-11-last-four-sources-design.md` (audit record, not approval); plan
`docs/superpowers/plans/2026-10-11-w1-pair-editions.md`. Scott said "go ahead with S3 and S14".

**No value W1 reads changed.** W1 reads `population` at the latest `reference_year` (2025); every 2025 row is exactly as
it was (hash below). What did change is mid-2024: the July 2026 file restates it for 295 of 296 authorities (England
58,620,101 to 58,607,170, -12,931). That is stored as edition 2 of 2024 and refreshed into the live 2024 rows. Edition 1
of 2024 keeps the July 2025 figures. The map is unaffected and W1 and `refresh_map.py` were not run (Scott deferred W1 and
the map until every source is done).

**One thing to know before the next W1 run:** the 295 refreshed 2024 rows now carry `loaded_at` 2026-10-11 22:29:07 UTC (the
edition's), so `refresh_map.py --check` will report `la_population` as loaded after the last W1 run although W1's read
(2025) is unchanged. That is expected, not a fault.

## Before-state (read-only, confirmed against the live table before anything was written)

The live table had been rewritten by accident earlier the same day (the old S3 script re-upserted the 2025 rows) and then
repaired: see [2026-10-11-s3-s14-accidental-rebuild.md](2026-10-11-s3-s14-accidental-rebuild.md). The values were never
different, only `loaded_at` of 2025, which was set back. So the survey of the same morning holds again, and was
re-confirmed first: 592 rows, 296 per year, PK `(lad24cd, reference_year)`, 0 NULL, 0 zero, sums 2024 58,620,101 and 2025
58,834,812, `loaded_at` 2024 all 2026-03-26 09:40:44.303564 UTC and 2025 all 2026-08-13 21:11:31.051713 UTC, survey hash
(md5 over every column except `loaded_at`, NULL as `~`, joined by `|`, ordered by year then code)
`fe07de1ef4526faa77db179ec7eae1b9`.

Each sha256 is written as two 32-hex halves (`first32` then `last32`; join them) so the credential scan, which flags a
64-hex run, stays untouched. They were printed with `python scripts/s3_mye_editions_verify.py --print-note-lines` before any
`ddl`, and are the lines the verify script's gates 16 and 17 read.

- `before-state` is the loader's `rows_content_sha` for one year: `lad24cd`, `population` and `la_name`, `loaded_at`
  excluded. It equals edition 1's content hash.
- `before-state-all` hashes every stored column of the whole table, `loaded_at` included, ordered by year then code.
- `w1-read` is what W1 reads: `lad24cd|population` for 2025. It must be the same before and after, and it is.

before-state la_population 2024 rows=296 sha256-first32=881e5eaabdf2d63c0658c23e0042503a sha256-last32=48d464fbcb2dc4253531e19aa1c7d182
before-state la_population 2025 rows=296 sha256-first32=6b9c6b4ec06f1e4eddd5051d7157e7e5 sha256-last32=aa0472226dc5b33ab82b591c263667a3
before-state-all la_population rows=592 sha256-first32=a1b397deb01063c36dc51accf1572980 sha256-last32=2f9f24b7f45a0b8d595e06a6722bf819
w1-read la_population 2025 rows=296 sha256-first32=7f407c7e19457d0d529025839cd09209 sha256-last32=6cb74dc2f83b08338c361745e6102a6f

After the refresh, the 2025 `w1-read` hash is the same and the 2025 rows are untouched (`refresh-latest` has a before/after
guard that refuses any change outside the keys it names).

## How the table came to hold what it holds (checked 2026-10-11 against the database, the run log and the docs)

- **2024, 2026-03-26.** The n8n workflow "ONS Population Estimates (S3)" (`AlIOqA6ARCRyNDai`) loaded mid-2024 from the NOMIS
  API (`NM_2002_1`) plus a chat-made "supplement" CSV for 11 authorities whose current-boundary figures NOMIS did not
  give (now `data/reference/mye_2024_supplement.csv`). It left 15 run-log rows, ids 2 to 16 (ids 2 to 15 each record 293
  rows; id 16, started 09:40:44, records 296 and matches the table's one `loaded_at`, 09:40:44.303564). Because NOMIS names
  drop the comma, four names are in a different form from the workbook's: Bristol City of, Herefordshire County of,
  Kingston upon Hull City of, Bournemouth Christchurch and Poole. The populations equal `mye24tablesew.xlsx` (MYE2) on all
  296; edition 1 keeps the four names as held.
- **The workflow could not run again as it stood.** Its upsert said `ON CONFLICT (lad24cd)`, which no longer matches the
  primary key once the key was widened, and it would first have run `CREATE TABLE IF NOT EXISTS`. Retired in Task 1 of this
  plan (commit `de39a43`): the Code node "Process Population Data" now throws RETIRED, and the three write nodes sit
  downstream of it. Nothing was activated.
- **2025, 2026-08-13.** `scripts/s3_mye_refresh.py`, run-log id 86 (21:11:30 UTC, 296 rows), loaded mid-2025 from
  `mye25tablesew.xlsx` and widened the primary key from `(lad24cd)` to `(lad24cd, reference_year)` so 2024 could stay. The
  run-log note says "released 2026-07-28": that is the ONS API's `releaseDate` (2026-07-28T23:00Z, which is 29 July in
  London). The workbook's Cover says "originally published at 9.30am 29 July 2026". The loader takes the rank from the Cover and
  prints the API date only. The 296 populations and names equal the workbook's MYE2 exactly.
- **The accidental write of 2026-10-11 20:30 UTC.** While the old loaders were being retired, the new tests that prove each
  retired script exits at once were run before the guards existed, so `s3_mye_refresh.py` ran for real and re-upserted the
  296 rows of 2025 with identical values. It wrote **no run-log row** (there is no id 500); the incident note is its record.
  `loaded_at` of 2025 was set back to its surveyed value in the repair.
- The old script is now a guard that exits 2 at its first statement. It was never run or imported again.

## What the July 2026 file says about mid-2024

`mye25tablesew.xlsx` (sha256 starting 85b4e105e9618043; Cover "June 2025", "originally published at 9.30am 29 July 2026";
next publication "summer 2027") restates mid-2011 to mid-2025 in MYE4, and its MYE7 is "July 2026 revisions to mid-2022 to
mid-2024". Checked read-only before loading:

- MYE4 mid-2024 equals MYE7's "revised July 2026" column on all 296. MYE7's "published July 2025" column equals the live
  2024 on all 296 (so live 2024 held the July 2025 figures and was out of date).
- 295 of 296 populations differ (the only authority unchanged is E07000203). England 58,620,101 to 58,607,170, **-12,931**:
  85 authorities go up and 210 go down. The four names also differ (the comma forms above).
- Largest moves: Westminster E09000033 +5,698 (+2.713%), Newham E09000025 +4,956, Haringey E09000014 +4,674, Tower Hamlets
  E09000030 +4,267, Waltham Forest E09000031 +4,045, Enfield E09000010 +3,987, Bradford E08000032 -3,787, Camden E09000007
  +3,615. The full list is at the end of this note (from `python scripts/s3_mye_editions_verify.py --print-revision`).
- Reconciliation in the file is exact: the English authorities sum to the England row for mid-2024 (58,607,170) and
  mid-2025 (58,834,812); the mid-2024 workbook reconciles at 58,620,101.
- **2025: 0 differences** against live.

### The stop limit used

The revised-year limit for an authority is 7.68%, not 2%. MYE7 shows City of London revised by +3.487% (mid-2022) and +6.674%
(mid-2023) and Westminster by +2.713% (mid-2024); a 2% limit would have rejected every revision year MYE7 records, including
this one, for ordinary revisions. The limit is the largest observed plus one point (7.674%, stored as `REV_AREA_BP = 768`);
the national limit stays at 0.5% (the largest observed is 0.1007%). The July 2026 revision is therefore a normal revision
needing only `--acknowledge 2024`. The numbers are in the loader's docstring and a test pins them. If the literal 2% is
ever wanted, change one constant and a revision would also need `--accept-large-revision`.

## What was written (2026-10-11)

1. `ddl --commit`: `la_population_editions` (append-only; triggers `s3_editions_immutable` and `s3_editions_no_truncate`
   refuse UPDATE, DELETE and TRUNCATE) and `la_population_editions_file_checks`. No change to the live table.
2. `migrate-legacy data/raw/s3_mye/mye24tablesew.xlsx data/raw/s3_mye/mye25tablesew.xlsx --commit` (after a preview and a
   `--simulate` that rolled back, both equal to Task 2's read-only preview): edition 1 "as loaded" for 2024 (296 rows) and
   2025 (296 rows), proved against a re-read of each held workbook (0 unexplained differences; the four name forms listed and
   named in the loader). Run-log id 517, 592 rows. **A ledger row only for the 2025 workbook**, outcome `unchanged`. None for
   the 2024 workbook, because it does not reproduce edition 1 on every column (the four names), so recording it as
   `unchanged` would be untrue.
3. `load` (preview, which showed 2024 REJECTED pending `--acknowledge 2024` and 2025 "skipped (checked: this file is already
   in the ledger)", the expected wording once the 2025 ledger row exists), then `load --acknowledge 2024 --simulate`
   (rolled back) and `load --acknowledge 2024 --commit`: edition 2 of 2024 (296 rows, population sum 58,607,170,
   published 2026-07-29), a ledger row for `mye25tablesew.xlsx` with outcome `revised`. Run-log id 518, 296 rows.
4. `refresh-latest` (preview, `--simulate`, `--commit`): 295 live rows of 2024 refreshed from edition 2 (the one unchanged
   row, E07000203, keeps its original `loaded_at` 2026-03-26 09:40:44.303564). The before/after guard passed. 2025 untouched.
5. `status`: OK, nothing to do. `s3_mye_editions_verify.py`: all 24 gates PASS, exit 0 (gate 17, the map's year, included).
   No `zz%` table, function or view remains. No W1, `refresh_map.py`, export, `push.py` or `git push`.

## Summer 2027 (the next release; the Cover says "summer 2027")

From `ONS_Population_Estimates`:

1. `python scripts/s3_mye_editions.py load` finds the newest "Mid-<yyyy>: 2023 local authority boundaries" edition on the ONS
   landing page and downloads the workbook to `data/raw/s3_mye` (also in a preview; the database is not written). Read the
   whole preview. Expect a new period 2026, and 2024 and 2025 compared (the file restates both). A year restated with
   different figures is REJECTED until `--acknowledge YEAR` names it; a restated 2025 is expected to need that. Another
   boundary year in the edition name, or an unknown code, halts.
2. `load --acknowledge 2025 --simulate` (as needed), then `--commit`; then `refresh-latest` (preview, `--commit`); then
   `status` and `python scripts/s3_mye_editions_verify.py` (exit 0). **Run `refresh-latest` before W1:** it copies the
   edition's `loaded_at` onto the live rows, and a W1 run made before it would read the old figures. `la_population` is a
   W1 input (population at the latest year).
3. If ONS gives no new release by 30 September 2027 the discovery step warns that one is overdue. A file older than the
   newest held, with equal rank but different content, or an older workbook given by `--file`, stops unless the named flag
   (`--allow-older-file`, `--accept-reissue YEAR`) is given and is logged.
4. What a halt means: the loader halts, naming what it saw, rather than guess (exit 1, nothing stored, no ledger row). A blank,
   text, marker or zero in an English LA cell, a changed header, the Cover, MYE2 and MYE4 disagreeing on the year, MYE2 and
   MYE4 disagreeing on the newest year, authorities not summing to the England row, fewer authorities than the held tip.

## How to reverse

`python scripts/s3_mye_editions.py restore-edition PERIOD N` (preview by default) stores edition N's rows as the next
edition; `refresh-latest --commit` then applies it to the live table. To return 2024 to the July 2025 figures: `restore-edition
2024 1`, then `refresh-latest --commit`. Nothing is ever deleted from the editions tables.

## The 295 restated authorities (mid-2024 population, July 2025 workbook to July 2026 workbook)

```
295 authorities: mid-2024 population in mye24tablesew.xlsx (MYE2) against mye25tablesew.xlsx (MYE4); England -12,931
E06000001 Hartlepool: 98,180 -> 97,868 (-312)
E06000002 Middlesbrough: 156,161 -> 154,700 (-1,461)
E06000003 Redcar and Cleveland: 139,228 -> 138,788 (-440)
E06000004 Stockton-on-Tees: 206,800 -> 206,018 (-782)
E06000005 Darlington: 112,489 -> 112,150 (-339)
E06000006 Halton: 131,543 -> 131,247 (-296)
E06000007 Warrington: 215,391 -> 214,640 (-751)
E06000008 Blackburn with Darwen: 162,540 -> 161,618 (-922)
E06000009 Blackpool: 144,191 -> 143,353 (-838)
E06000010 Kingston upon Hull, City of: 275,401 -> 274,727 (-674)
E06000011 East Riding of Yorkshire: 355,884 -> 355,378 (-506)
E06000012 North East Lincolnshire: 159,911 -> 159,738 (-173)
E06000013 North Lincolnshire: 171,336 -> 171,424 (+88)
E06000014 York: 209,301 -> 208,237 (-1,064)
E06000015 Derby: 274,149 -> 273,656 (-493)
E06000016 Leicester: 388,348 -> 386,755 (-1,593)
E06000017 Rutland: 41,443 -> 41,382 (-61)
E06000018 Nottingham: 331,077 -> 330,561 (-516)
E06000019 Herefordshire, County of: 191,047 -> 192,067 (+1,020)
E06000020 Telford and Wrekin: 195,952 -> 196,090 (+138)
E06000021 Stoke-on-Trent: 270,425 -> 270,010 (-415)
E06000022 Bath and North East Somerset: 200,028 -> 200,539 (+511)
E06000023 Bristol, City of: 494,399 -> 494,225 (-174)
E06000024 North Somerset: 224,578 -> 224,667 (+89)
E06000025 South Gloucestershire: 306,332 -> 305,766 (-566)
E06000026 Plymouth: 272,067 -> 271,904 (-163)
E06000027 Torbay: 140,126 -> 139,862 (-264)
E06000030 Swindon: 243,875 -> 243,324 (-551)
E06000031 Peterborough: 223,655 -> 224,152 (+497)
E06000032 Luton: 239,090 -> 238,047 (-1,043)
E06000033 Southend-on-Sea: 185,256 -> 184,503 (-753)
E06000034 Thurrock: 180,989 -> 181,287 (+298)
E06000035 Medway: 292,655 -> 291,691 (-964)
E06000036 Bracknell Forest: 130,806 -> 130,502 (-304)
E06000037 West Berkshire: 165,112 -> 164,918 (-194)
E06000038 Reading: 182,907 -> 182,809 (-98)
E06000039 Slough: 167,359 -> 166,739 (-620)
E06000040 Windsor and Maidenhead: 158,943 -> 158,760 (-183)
E06000041 Wokingham: 187,200 -> 187,433 (+233)
E06000042 Milton Keynes: 305,884 -> 305,806 (-78)
E06000043 Brighton and Hove: 283,870 -> 285,063 (+1,193)
E06000044 Portsmouth: 214,321 -> 212,936 (-1,385)
E06000045 Southampton: 259,424 -> 260,604 (+1,180)
E06000046 Isle of Wight: 141,660 -> 141,356 (-304)
E06000047 County Durham: 538,011 -> 536,793 (-1,218)
E06000049 Cheshire East: 421,298 -> 420,472 (-826)
E06000050 Cheshire West and Chester: 371,652 -> 370,951 (-701)
E06000051 Shropshire: 332,455 -> 331,751 (-704)
E06000052 Cornwall: 583,289 -> 582,205 (-1,084)
E06000053 Isles of Scilly: 2,366 -> 2,383 (+17)
E06000054 Wiltshire: 523,700 -> 524,001 (+301)
E06000055 Bedford: 194,976 -> 194,881 (-95)
E06000056 Central Bedfordshire: 315,877 -> 316,062 (+185)
E06000057 Northumberland: 331,420 -> 330,615 (-805)
E06000058 Bournemouth, Christchurch and Poole: 408,967 -> 408,009 (-958)
E06000059 Dorset: 389,947 -> 389,341 (-606)
E06000060 Buckinghamshire: 578,772 -> 578,432 (-340)
E06000061 North Northamptonshire: 373,871 -> 375,336 (+1,465)
E06000062 West Northamptonshire: 439,811 -> 441,277 (+1,466)
E06000063 Cumberland: 280,495 -> 280,014 (-481)
E06000064 Westmorland and Furness: 230,185 -> 229,821 (-364)
E06000065 North Yorkshire: 635,270 -> 633,692 (-1,578)
E06000066 Somerset: 588,328 -> 588,173 (-155)
E07000008 Cambridge: 149,352 -> 151,666 (+2,314)
E07000009 East Cambridgeshire: 92,906 -> 93,197 (+291)
E07000010 Fenland: 104,896 -> 105,442 (+546)
E07000011 Huntingdonshire: 190,619 -> 190,370 (-249)
E07000012 South Cambridgeshire: 172,544 -> 172,543 (-1)
E07000032 Amber Valley: 130,451 -> 130,238 (-213)
E07000033 Bolsover: 83,773 -> 83,679 (-94)
E07000034 Chesterfield: 106,045 -> 105,807 (-238)
E07000035 Derbyshire Dales: 71,757 -> 71,762 (+5)
E07000036 Erewash: 114,253 -> 113,816 (-437)
E07000037 High Peak: 91,959 -> 91,892 (-67)
E07000038 North East Derbyshire: 106,646 -> 106,550 (-96)
E07000039 South Derbyshire: 117,493 -> 117,169 (-324)
E07000040 East Devon: 158,239 -> 157,986 (-253)
E07000041 Exeter: 138,399 -> 139,007 (+608)
E07000042 Mid Devon: 84,993 -> 85,102 (+109)
E07000043 North Devon: 101,222 -> 101,035 (-187)
E07000044 South Hams: 92,148 -> 92,054 (-94)
E07000045 Teignbridge: 138,548 -> 138,296 (-252)
E07000046 Torridge: 69,841 -> 69,714 (-127)
E07000047 West Devon: 58,923 -> 58,849 (-74)
E07000061 Eastbourne: 104,259 -> 104,271 (+12)
E07000062 Hastings: 91,219 -> 91,017 (-202)
E07000063 Lewes: 102,363 -> 102,323 (-40)
E07000064 Rother: 96,133 -> 95,947 (-186)
E07000065 Wealden: 166,908 -> 166,776 (-132)
E07000066 Basildon: 193,632 -> 192,810 (-822)
E07000067 Braintree: 164,304 -> 163,745 (-559)
E07000068 Brentwood: 79,326 -> 78,959 (-367)
E07000069 Castle Point: 90,581 -> 90,121 (-460)
E07000070 Chelmsford: 188,803 -> 188,328 (-475)
E07000071 Colchester: 200,222 -> 199,730 (-492)
E07000072 Epping Forest: 137,451 -> 137,184 (-267)
E07000073 Harlow: 98,235 -> 98,130 (-105)
E07000074 Maldon: 69,131 -> 69,039 (-92)
E07000075 Rochford: 89,815 -> 89,521 (-294)
E07000076 Tendring: 156,759 -> 156,408 (-351)
E07000077 Uttlesford: 95,106 -> 94,953 (-153)
E07000078 Cheltenham: 121,739 -> 121,461 (-278)
E07000079 Cotswold: 91,661 -> 91,728 (+67)
E07000080 Forest of Dean: 89,753 -> 89,848 (+95)
E07000081 Gloucester: 138,598 -> 137,857 (-741)
E07000082 Stroud: 125,680 -> 125,507 (-173)
E07000083 Tewkesbury: 101,949 -> 101,708 (-241)
E07000084 Basingstoke and Deane: 193,110 -> 192,965 (-145)
E07000085 East Hampshire: 129,975 -> 129,865 (-110)
E07000086 Eastleigh: 142,933 -> 142,678 (-255)
E07000087 Fareham: 115,428 -> 115,208 (-220)
E07000088 Gosport: 82,921 -> 82,727 (-194)
E07000089 Hart: 103,162 -> 103,031 (-131)
E07000090 Havant: 126,985 -> 126,794 (-191)
E07000091 New Forest: 176,116 -> 175,845 (-271)
E07000092 Rushmoor: 105,751 -> 105,444 (-307)
E07000093 Test Valley: 135,201 -> 135,169 (-32)
E07000094 Winchester: 135,632 -> 135,444 (-188)
E07000095 Broxbourne: 101,900 -> 101,886 (-14)
E07000096 Dacorum: 161,420 -> 161,139 (-281)
E07000098 Hertsmere: 110,212 -> 109,993 (-219)
E07000099 North Hertfordshire: 137,201 -> 136,818 (-383)
E07000102 Three Rivers: 95,807 -> 95,575 (-232)
E07000103 Watford: 107,171 -> 106,799 (-372)
E07000105 Ashford: 140,936 -> 140,812 (-124)
E07000106 Canterbury: 162,100 -> 162,372 (+272)
E07000107 Dartford: 125,011 -> 124,594 (-417)
E07000108 Dover: 119,768 -> 119,805 (+37)
E07000109 Gravesham: 110,671 -> 110,715 (+44)
E07000110 Maidstone: 187,767 -> 189,189 (+1,422)
E07000111 Sevenoaks: 122,748 -> 122,650 (-98)
E07000112 Folkestone and Hythe: 112,411 -> 111,908 (-503)
E07000113 Swale: 158,379 -> 158,754 (+375)
E07000114 Thanet: 142,691 -> 142,158 (-533)
E07000115 Tonbridge and Malling: 136,853 -> 136,685 (-168)
E07000116 Tunbridge Wells: 119,694 -> 119,435 (-259)
E07000117 Burnley: 99,233 -> 99,195 (-38)
E07000118 Chorley: 120,839 -> 120,579 (-260)
E07000119 Fylde: 85,447 -> 85,453 (+6)
E07000120 Hyndburn: 86,058 -> 85,895 (-163)
E07000121 Lancaster: 145,006 -> 145,332 (+326)
E07000122 Pendle: 99,777 -> 99,643 (-134)
E07000123 Preston: 162,864 -> 161,593 (-1,271)
E07000124 Ribble Valley: 65,794 -> 65,761 (-33)
E07000125 Rossendale: 73,045 -> 72,841 (-204)
E07000126 South Ribble: 116,113 -> 115,727 (-386)
E07000127 West Lancashire: 121,995 -> 122,054 (+59)
E07000128 Wyre: 118,743 -> 118,366 (-377)
E07000129 Blaby: 108,165 -> 107,845 (-320)
E07000130 Charnwood: 188,385 -> 188,310 (-75)
E07000131 Harborough: 104,713 -> 104,619 (-94)
E07000132 Hinckley and Bosworth: 116,682 -> 116,612 (-70)
E07000133 Melton: 54,052 -> 53,922 (-130)
E07000134 North West Leicestershire: 111,881 -> 111,876 (-5)
E07000135 Oadby and Wigston: 61,695 -> 61,293 (-402)
E07000136 Boston: 71,080 -> 72,288 (+1,208)
E07000137 East Lindsey: 145,183 -> 145,345 (+162)
E07000138 Lincoln: 105,114 -> 104,939 (-175)
E07000139 North Kesteven: 122,468 -> 122,346 (-122)
E07000140 South Holland: 99,298 -> 99,638 (+340)
E07000141 South Kesteven: 147,151 -> 147,049 (-102)
E07000142 West Lindsey: 99,208 -> 98,928 (-280)
E07000143 Breckland: 146,620 -> 146,784 (+164)
E07000144 Broadland: 138,157 -> 137,892 (-265)
E07000145 Great Yarmouth: 100,529 -> 100,666 (+137)
E07000146 King's Lynn and West Norfolk: 156,206 -> 156,192 (-14)
E07000147 North Norfolk: 103,217 -> 103,431 (+214)
E07000148 Norwich: 147,182 -> 147,119 (-63)
E07000149 South Norfolk: 148,448 -> 148,384 (-64)
E07000170 Ashfield: 129,572 -> 129,210 (-362)
E07000171 Bassetlaw: 124,937 -> 124,742 (-195)
E07000172 Broxtowe: 114,565 -> 114,124 (-441)
E07000173 Gedling: 120,179 -> 119,996 (-183)
E07000174 Mansfield: 113,138 -> 113,291 (+153)
E07000175 Newark and Sherwood: 127,886 -> 127,863 (-23)
E07000176 Rushcliffe: 126,736 -> 126,124 (-612)
E07000177 Cherwell: 170,426 -> 170,734 (+308)
E07000178 Oxford: 166,034 -> 167,827 (+1,793)
E07000179 South Oxfordshire: 156,470 -> 156,315 (-155)
E07000180 Vale of White Horse: 149,347 -> 149,421 (+74)
E07000181 West Oxfordshire: 120,941 -> 120,826 (-115)
E07000192 Cannock Chase: 104,088 -> 103,955 (-133)
E07000193 East Staffordshire: 129,659 -> 130,069 (+410)
E07000194 Lichfield: 111,932 -> 111,710 (-222)
E07000195 Newcastle-under-Lyme: 127,727 -> 127,474 (-253)
E07000196 South Staffordshire: 114,423 -> 114,174 (-249)
E07000197 Stafford: 141,556 -> 141,610 (+54)
E07000198 Staffordshire Moorlands: 96,651 -> 96,528 (-123)
E07000199 Tamworth: 81,117 -> 80,877 (-240)
E07000200 Babergh: 97,033 -> 96,906 (-127)
E07000202 Ipswich: 140,274 -> 140,042 (-232)
E07000207 Elmbridge: 141,926 -> 141,985 (+59)
E07000208 Epsom and Ewell: 83,288 -> 83,179 (-109)
E07000209 Guildford: 151,359 -> 151,682 (+323)
E07000210 Mole Valley: 88,709 -> 88,541 (-168)
E07000211 Reigate and Banstead: 159,134 -> 158,579 (-555)
E07000212 Runnymede: 92,118 -> 92,321 (+203)
E07000213 Spelthorne: 107,074 -> 106,453 (-621)
E07000214 Surrey Heath: 94,492 -> 94,276 (-216)
E07000215 Tandridge: 90,586 -> 90,058 (-528)
E07000216 Waverley: 134,284 -> 134,737 (+453)
E07000217 Woking: 105,679 -> 105,981 (+302)
E07000218 North Warwickshire: 67,117 -> 67,045 (-72)
E07000219 Nuneaton and Bedworth: 141,565 -> 141,554 (-11)
E07000220 Rugby: 122,378 -> 122,666 (+288)
E07000221 Stratford-on-Avon: 146,258 -> 146,196 (-62)
E07000222 Warwick: 154,889 -> 155,541 (+652)
E07000223 Adur: 64,889 -> 64,736 (-153)
E07000224 Arun: 170,064 -> 170,692 (+628)
E07000225 Chichester: 128,934 -> 129,206 (+272)
E07000226 Crawley: 124,008 -> 123,758 (-250)
E07000227 Horsham: 151,521 -> 151,071 (-450)
E07000228 Mid Sussex: 161,755 -> 161,122 (-633)
E07000229 Worthing: 113,866 -> 113,515 (-351)
E07000234 Bromsgrove: 101,685 -> 101,413 (-272)
E07000235 Malvern Hills: 83,227 -> 83,262 (+35)
E07000236 Redditch: 87,847 -> 87,728 (-119)
E07000237 Worcester: 106,671 -> 106,566 (-105)
E07000238 Wychavon: 138,017 -> 138,187 (+170)
E07000239 Wyre Forest: 103,913 -> 103,558 (-355)
E07000240 St Albans: 151,012 -> 150,722 (-290)
E07000241 Welwyn Hatfield: 122,819 -> 123,051 (+232)
E07000242 East Hertfordshire: 156,875 -> 156,766 (-109)
E07000243 Stevenage: 91,774 -> 91,234 (-540)
E07000244 East Suffolk: 249,664 -> 249,581 (-83)
E07000245 West Suffolk: 188,485 -> 188,197 (-288)
E08000001 Bolton: 310,085 -> 308,904 (-1,181)
E08000002 Bury: 198,921 -> 198,136 (-785)
E08000003 Manchester: 589,670 -> 588,597 (-1,073)
E08000004 Oldham: 251,560 -> 250,725 (-835)
E08000005 Rochdale: 235,561 -> 234,919 (-642)
E08000006 Salford: 294,348 -> 293,508 (-840)
E08000007 Stockport: 303,929 -> 302,184 (-1,745)
E08000008 Tameside: 239,643 -> 238,895 (-748)
E08000009 Trafford: 241,025 -> 239,618 (-1,407)
E08000010 Wigan: 344,922 -> 344,245 (-677)
E08000011 Knowsley: 162,565 -> 161,714 (-851)
E08000012 Liverpool: 508,961 -> 508,202 (-759)
E08000013 St. Helens: 188,861 -> 188,397 (-464)
E08000014 Sefton: 286,281 -> 285,298 (-983)
E08000015 Wirral: 328,873 -> 328,209 (-664)
E08000016 Barnsley: 251,770 -> 251,325 (-445)
E08000017 Doncaster: 319,765 -> 320,248 (+483)
E08000018 Rotherham: 276,595 -> 276,332 (-263)
E08000019 Sheffield: 582,493 -> 580,235 (-2,258)
E08000021 Newcastle upon Tyne: 320,605 -> 318,852 (-1,753)
E08000022 North Tyneside: 215,025 -> 214,110 (-915)
E08000023 South Tyneside: 151,393 -> 150,736 (-657)
E08000024 Sunderland: 288,606 -> 286,696 (-1,910)
E08000025 Birmingham: 1,183,618 -> 1,182,920 (-698)
E08000026 Coventry: 369,026 -> 368,707 (-319)
E08000027 Dudley: 331,930 -> 331,239 (-691)
E08000028 Sandwell: 353,860 -> 353,270 (-590)
E08000029 Solihull: 221,242 -> 220,299 (-943)
E08000030 Walsall: 295,678 -> 294,626 (-1,052)
E08000031 Wolverhampton: 281,251 -> 280,541 (-710)
E08000032 Bradford: 563,605 -> 559,818 (-3,787)
E08000033 Calderdale: 210,929 -> 209,828 (-1,101)
E08000034 Kirklees: 447,847 -> 445,780 (-2,067)
E08000035 Leeds: 845,189 -> 842,884 (-2,305)
E08000036 Wakefield: 367,666 -> 366,925 (-741)
E08000037 Gateshead: 202,760 -> 201,422 (-1,338)
E09000001 City of London: 15,111 -> 15,497 (+386)
E09000002 Barking and Dagenham: 232,747 -> 234,866 (+2,119)
E09000003 Barnet: 405,050 -> 406,198 (+1,148)
E09000004 Bexley: 256,434 -> 256,288 (-146)
E09000005 Brent: 352,976 -> 355,398 (+2,422)
E09000006 Bromley: 335,319 -> 334,641 (-678)
E09000007 Camden: 216,943 -> 220,558 (+3,615)
E09000008 Croydon: 409,342 -> 408,636 (-706)
E09000009 Ealing: 385,985 -> 387,680 (+1,695)
E09000010 Enfield: 327,434 -> 331,421 (+3,987)
E09000011 Greenwich: 299,528 -> 301,259 (+1,731)
E09000012 Hackney: 266,758 -> 268,651 (+1,893)
E09000013 Hammersmith and Fulham: 188,687 -> 190,803 (+2,116)
E09000014 Haringey: 263,850 -> 268,524 (+4,674)
E09000015 Harrow: 270,724 -> 270,987 (+263)
E09000016 Havering: 276,274 -> 277,042 (+768)
E09000017 Hillingdon: 329,185 -> 327,886 (-1,299)
E09000018 Hounslow: 299,424 -> 300,047 (+623)
E09000019 Islington: 223,024 -> 226,183 (+3,159)
E09000020 Kensington and Chelsea: 144,518 -> 147,255 (+2,737)
E09000021 Kingston upon Thames: 172,692 -> 172,964 (+272)
E09000022 Lambeth: 316,920 -> 318,864 (+1,944)
E09000023 Lewisham: 301,255 -> 302,432 (+1,177)
E09000024 Merton: 218,539 -> 220,020 (+1,481)
E09000025 Newham: 374,523 -> 379,479 (+4,956)
E09000026 Redbridge: 321,231 -> 323,006 (+1,775)
E09000027 Richmond upon Thames: 196,678 -> 196,975 (+297)
E09000028 Southwark: 314,786 -> 317,058 (+2,272)
E09000029 Sutton: 214,525 -> 213,672 (-853)
E09000030 Tower Hamlets: 331,886 -> 336,153 (+4,267)
E09000031 Waltham Forest: 279,737 -> 283,782 (+4,045)
E09000032 Wandsworth: 337,655 -> 339,727 (+2,072)
E09000033 Westminster: 209,996 -> 215,694 (+5,698)
```
