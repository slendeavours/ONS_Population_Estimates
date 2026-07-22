# W1 Node 5 Patch: S19 PIP Claimant Columns

## Overview

Adds three new columns to `staging_la_signals` from S19 (DWP PIP Claimants via Stat-Xplore).

## New Columns

| Column | Source | Type | Description |
|---|---|---|---|
| `pip_total_claimants` | `la_pip_claimants` | INTEGER | Total PIP cases with entitlement in the LA |
| `pip_enhanced_daily_living` | `la_pip_claimants` | INTEGER | Subset: enhanced daily living component (sharper HSS demand signal) |
| `pip_rate_per_1000` | Derived | NUMERIC(8,2) | `pip_total_claimants / population * 1000` — normalised demand rate |

## New JOIN

Add after existing S9 CRFD join:

```sql
LEFT JOIN la_pip_claimants pip
    ON pip.lad24cd = b.lad24cd
    AND pip.month = 'Apr-26'
```

When monthly refresh adds new periods, change to:

```sql
    AND pip.month = (SELECT MAX(month) FROM la_pip_claimants)
```

## New SELECT Columns

```sql
pip.pip_total_claimants,
pip.pip_enhanced_daily_living,
ROUND(pip.pip_total_claimants::NUMERIC / NULLIF(p.population, 0) * 1000, 2) AS pip_rate_per_1000,
```

Where `p` is the `la_population` alias already in the query.

## New ON CONFLICT SET

```sql
pip_total_claimants = EXCLUDED.pip_total_claimants,
pip_enhanced_daily_living = EXCLUDED.pip_enhanced_daily_living,
pip_rate_per_1000 = EXCLUDED.pip_rate_per_1000,
```

## Node 9 Export Patch

Add to the GeoJSON properties object in Node 9:

```sql
'pip_total_claimants', sig.pip_total_claimants,
'pip_enhanced_daily_living', sig.pip_enhanced_daily_living,
'pip_rate_per_1000', sig.pip_rate_per_1000,
```

## Run 11 Coverage

| Column | Populated | NULL |
|---|---|---|
| pip_total_claimants | 296/296 | 0 |
| pip_enhanced_daily_living | 296/296 | 0 |
| pip_rate_per_1000 | 296/296 | 0 |

## HSS Three-Layer Package

Run 11 completes the HSS three-layer package in `staging_la_signals`:

| Layer | Source | Columns | Coverage |
|---|---|---|---|
| Supply | S11 CQC | `supported_living_locations` | 296/296 |
| Demand | S19 PIP | `pip_total_claimants`, `pip_enhanced_daily_living`, `pip_rate_per_1000` | 296/296 |
| Flow | S9 DRD + CRFD | `drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, `crfd_days` | 296/296 DRD, 205/296 CRFD |

## Notes

- Run 11 was created by copying run 10 signals and adding the PIP LEFT JOIN (n8n API was not accessible for direct Node 5 update)
- PIP coverage is 296/296 with zero DWP suppression in Apr-26
- The `pip_rate_per_1000` derived rate uses `la_population` from the same row, not a separate join
- Barnsley (E08000016) and Sheffield (E08000019) have NULL TA columns in both run 10 and 11 — LGR successor code gap, not a PIP issue
