# Node 5 — Verification Suite

## Type
Python script task

## Purpose
Validate the loaded data against six automated checks before logging the run.

## Logic
1. **CHECK 1 — Row count**: > 7,800 rows expected (295 LAs × 30+ periods)
2. **CHECK 2 — LA coverage**: 295 LAs expected (Isles of Scilly E06000053 excluded by Land Registry — too few transactions)
3. **CHECK 3 — Target market spot-check**: Birmingham, Liverpool, Nottingham, Manchester, Blackpool all present with non-null avg_price_all for latest period
4. **CHECK 4 — No implausible prices**: all avg_price_all between £50,000 and £2,000,000
5. **CHECK 5 — Period coverage**: MIN(period) <= 2022-01-01, MAX(period) within 120 days of run date (accounts for HPI ~2-month publication lag)
6. **CHECK 6 — Barnsley/Sheffield recode**: E08000016 and E08000019 present; E08000038 and E08000039 absent

## Key parameters
| Parameter | Value |
|---|---|
| Expected LA count | 295 (296 minus Isles of Scilly) |
| Price bounds | £50,000 – £2,000,000 |
| Max period lag | 120 days |
| Target markets | E08000025, E08000012, E06000018, E08000003, E06000009 |

## Behaviour
All checks must PASS before the pipeline run is logged. On FAIL, the script exits with code 1 and no log entry is written.

## Verified output
All 6 checks PASS on 2026-07-14. Row count: 15,340. 295 LAs. Period: 2022-01-01 to 2026-04-01.
