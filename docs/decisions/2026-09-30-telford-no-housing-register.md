# Telford and Wrekin has no housing register, 2026-09-30

## What was wrong

The Housing Register layer showed Telford and Wrekin at 0 households, the lowest
band, which reads as no demand. Telford does not run a housing register; it
signposts to housing associations and keeps a pool of households seeking advice.
LAHS reports 0 for it from 2022, and the identical 2,374 for 2018 to 2021 looks
carried forward, not counted. It is the only authority of 296 reporting 0 in 2025.

A zero here means "not applicable", not "no households on a list".

## What was done

`la_housing_register.households_on_register` set to NULL for Telford 2022, 2023
and 2025 (the three reported zeros; 2024 was already NULL), with the reason
appended to each row's `source`. Backup: `la_housing_register_bak_20260930`.
Workflow 1 re-run as run 20 and the map data re-exported. The only differences
from run 19 are Telford's `housing_register` (0 to NULL) and its `data_quality`
(now `incomplete`, `housing_register` absent). The map shows "No data".

The 2018 to 2021 value of 2,374 was left as reported and is unverified.
`data/reference/lahs_waiting_list_2015_2025.csv` still holds 0, because that
file is a copy of what LAHS published.

## Consequence for anyone quoting the layer

Do not describe Telford as having no waiting-list demand. Say it does not
operate a housing register. Any national total of households on registers
excludes it. The 2025 sum in the reference CSV (1,340,527) is unaffected as
0 added nothing.
