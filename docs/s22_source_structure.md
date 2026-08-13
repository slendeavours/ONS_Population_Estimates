# S22 — source structure report

Generated 2026-08-13T01:19:02+00:00. Both files resolved at runtime from their publisher landing pages via the GOV.UK content API; no file URL is hardcoded in this build.

## Source A — Local authority Council Taxbase in England

| field | value |
|---|---|
| landing page | https://www.gov.uk/government/statistics/council-taxbase-2025-in-england |
| release title | Council Taxbase 2025 in England |
| taxbase year | 2025 |
| first published | 2025-11-06T00:00:00+00:00 |
| revised / last updated | 2026-01-21T09:30:00+00:00 |
| attachment | Council Taxbase: Local authority level data for 2025 (revised) |
| resolved URL | https://assets.publishing.service.gov.uk/media/696f605ff6aa424b452e3359/2025_Local_Authority_Drop_Down.xlsx |
| format | application/vnd.openxmlformats-officedocument.spreadsheetml.sheet |
| file size | 1,800,242 bytes |
| technical notes | https://www.gov.uk/government/statistics/council-taxbase-2025-in-england/local-authority-council-taxbase-in-england-2025-technical-notes |

Attachments on the release page:

- Local authority Council Taxbase in England 2025 (revised)
- Local authority Council Taxbase in England 2025: Technical notes (revised)
- Tables 1 to 5: Council Taxbase in England 2025 (revised)
- Council Taxbase: Local authority level data for 2025 (revised)

### Sheets

| sheet | rows | columns |
|---|---|---|
| Cover | 20 | 1 |
| Contents | 113 | 3 |
| Notes | 13 | 4 |
| CTB Form | 114 | 29 |
| CTB Supplementary Form | 124 | 24 |
| CT Support | 33 | 37 |
| Family Annexe | 19 | 24 |
| Empty Properties | 103 | 34 |
| Second Homes | 93 | 37 |
| Council Taxbase Data | 303 | 336 |
| Supplementary Data | 303 | 92 |
| Council Tax Support Data | 303 | 80 |
| Family Annexe Data | 303 | 26 |
| Empty Properties Data | 303 | 394 |
| Second Homes Data | 303 | 116 |
| LA list | 298 | 2 |

Header row position: **row 6** on every data sheet. Row 5 carries the table label spanning each block, row 6 the column headers, row 7 the England total, rows 8-303 the 296 billing authorities.

Identity columns (row 6, 0-indexed): 0 `E-code`, 1 `ONS Code`, 2 `Region`, 3 `Local Authority`, 4 `Notes`.

### Tables used, as published

| table | column header used | 0-indexed column | published title |
|---|---|---|---|
| 1.01 | Total | 13 | Table 1.01 Total Number of Dwellings on Valuation List (Line 1) |
| 1.11 | Total | 118 | Table 1.11. Number of dwellings in line 7 classed as second homes on 6 October 2025 (Line 11) |
| 1.17 | Total | 178 | Table 1.17. Number of dwellings in line 7 classed as empty and being charged the Empty Homes Premium on 6 October 2025 (Line 14) |
| 1.18 | Total | 188 | Table 1.18. Total number of dwellings in line 7 classed as empty on 6 October 2025 (Line 15) |
| 1.19 | Total | 198 | Table 1.19. Number of dwellings that are classed as empty on 6 October 2025 and have been for more than 6 months (Line 16) |
| 2.01 | Class B ... Class W | 5-29 | Table 2.01. Number of dwellings on the Valuation List on 10 September 2025 that were in exempt classes B, D to W |

Table 2.01 on the `Supplementary Data` sheet publishes the exemption class breakdown **at local authority level**, one column per class A to W plus `Total exemptions`. Classes used for this build: B, D, E, F, G, H, I, J, K, L, Q.

Only the current taxbase year is present in this workbook. No prior-year columns are published in the same file, so a single year (2025) is loaded.

## Source B — Table 615, vacant dwellings by local authority district

| field | value |
|---|---|
| landing page | https://www.gov.uk/government/statistical-data-sets/live-tables-on-dwelling-stock-including-vacants |
| attachment | Table 615: vacant dwellings by local authority district: England, from 2004 |
| resolved URL | https://assets.publishing.service.gov.uk/media/6a2bf816e50716856ed4afdd/Live_Table_615.ods |
| format | application/vnd.oasis.opendocument.spreadsheet |
| file size | 311,603 bytes |
| landing page last updated | 2026-06-25T09:30:03+01:00 |

### Sheet `All_vacants`

All vacants: All vacant dwellings by local authority district, England, from 2004 [note 1] [note 2] [note 5] [note 6] [note 7] [note 8] [note 9] [note 10]

- 391 rows x 24 columns
- header row position: row 3
- column headers as published: `ONS code`, `Area`, `01/11/2004`, `10/10/2005`, `09/10/2006`, `08/10/2007`, `06/10/2008`, `05/10/2009`, `04/10/2010`, `03/10/2011`, `01/10/2012`, `07/10/2013`, `06/10/2014`, `05/10/2015`, `03/10/2016`, `02/10/2017`, `01/10/2018`, `07/10/2019`, `05/10/2020`, `04/10/2021`, `03/10/2022`, `02/10/2023`, `07/10/2024`, `06/10/2025`

### Sheet `All_long_term_vacants`

All long-term vacants: All long-term vacant dwellings by local authority district, England, from 2004 [note 1] [note 3] [note 5] [note 6] [note 7] [note 8] [note 9] [note 10]

- 391 rows x 24 columns
- header row position: row 3
- column headers as published: `ONS code`, `Area`, `01/11/2004`, `10/10/2005`, `09/10/2006`, `08/10/2007`, `06/10/2008`, `05/10/2009`, `04/10/2010`, `03/10/2011`, `01/10/2012`, `07/10/2013`, `06/10/2014`, `05/10/2015`, `03/10/2016`, `02/10/2017`, `01/10/2018`, `07/10/2019`, `05/10/2020`, `04/10/2021`, `03/10/2022`, `02/10/2023`, `07/10/2024`, `06/10/2025`

Each data column header is the snapshot date for that year. Rows include England (`E92000001`) and the nine regions (`E12...`) as well as districts; only `E06`/`E07`/`E08`/`E09` rows are loaded. Suppressed or not-applicable cells are published as `[x]`.

## National headline figures printed on the release page

These are the Phase 6 reconciliation targets. They are taken from the MHCLG statistical release text itself, not from any secondary commentary.

| figure | as printed on the release page | value |
|---|---|---|
| total dwellings | "In England, there were a total of 25.8 million dwellings as of 10 September 2025" | 25800000 |
| empty dwellings (all, excluding exempt) | "there were 542,000 dwellings recorded as empty for the purposes of council tax as of 10 September 2025" | 542000 |
| empty homes charged a premium | "153,000 dwellings being charged an Empty Homes Premium" | 153000 |
| second homes | "There were 268,000 dwellings recorded as second homes for the purposes of council tax" | 268000 |
| unoccupied exempt dwellings | "There were 212,000 dwellings that were receiving an exemption that were unoccupied" | 212000 |

**Not printed on the release page:** the release states no national figure for dwellings empty for more than six months, and none for dwellings empty for less than six months. Those two are NOT FOUND on the release page — they are not unchecked. For those two only, the reconciliation target is the publisher's own England total row (row 7) in the same local-authority-level workbook, which is stated in the verification report wherever the figure appears.

## Structural breaks stated by the publisher

Both are stated in the release text at https://www.gov.uk/government/statistics/council-taxbase-2025-in-england and in the technical notes at https://www.gov.uk/government/statistics/council-taxbase-2025-in-england/local-authority-council-taxbase-in-england-2025-technical-notes:

- **1 April 2024** — "authorities could charge an Empty Homes Premium of up to 100% for properties that have been empty for between 1 and 2 years. Previously the premium could only be applied where properties had been empty for 2 or more years." Premium counts are not comparable across this date.
- **1 April 2025** — "authorities could charge a Second Homes Premium of up to 100% on properties that were reported to be second homes for council tax purposes." Second home counts from this date are affected by reclassification behaviour.
