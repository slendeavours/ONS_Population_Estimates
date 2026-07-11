# Node 6 - Upsert BRMA LHA Rates

## Type
Postgres UPSERT

## Purpose
Load 152 BRMA LHA rates (weekly and monthly) into brma_lha_rates table. Safe to re-run.

## Query

```sql
INSERT INTO brma_lha_rates
(brma_name, financial_year, sar_weekly, one_bed_weekly, two_bed_weekly,
 three_bed_weekly, four_bed_weekly, sar_monthly, one_bed_monthly, two_bed_monthly,
 three_bed_monthly, four_bed_monthly, source)
VALUES
  ('Aylesbury', '2026-27', 102.15, 184.62, 269.62, 384.23, 380.77,
   425.00, 770.00, 1122.50, 1597.50, 1585.00,
   'DWP UC LHA rates 2026-27, https://www.gov.uk/government/publications/universal-credit-local-housing-allowance-rates-2026-to-2027'),
  ('Barnsley', '2026-27', 73.00, 95.77, 123.65, 166.81, 178.85,
   303.33, 398.33, 514.33, 693.33, 743.33,
   'DWP UC LHA rates 2026-27, ...'),
  ...
  (152 rows total)
ON CONFLICT (brma_name, financial_year) DO UPDATE SET
    sar_weekly = EXCLUDED.sar_weekly,
    one_bed_weekly = EXCLUDED.one_bed_weekly,
    two_bed_weekly = EXCLUDED.two_bed_weekly,
    three_bed_weekly = EXCLUDED.three_bed_weekly,
    four_bed_weekly = EXCLUDED.four_bed_weekly,
    sar_monthly = EXCLUDED.sar_monthly,
    one_bed_monthly = EXCLUDED.one_bed_monthly,
    two_bed_monthly = EXCLUDED.two_bed_monthly,
    three_bed_monthly = EXCLUDED.three_bed_monthly,
    four_bed_monthly = EXCLUDED.four_bed_monthly,
    loaded_at = NOW();
```

## Logic
- INSERT 152 rows (one per England BRMA)
- Week conversion: `weekly = monthly * 12 / 52`, rounded to 2 decimals
- Stores both monthly (original DWP values) and weekly (pipeline values)
- financial_year = '2026-27' (frozen at April 2024 levels)
- ON CONFLICT (brma_name, financial_year): UPDATE all rate columns if BRMA+year exists

## Behaviour
- **Idempotent**: safe to re-run, no duplicate errors
- **Auditable**: both monthly and weekly values preserved
- **Atomic**: all 152 rows upserted together
- Conversion formula documented for reproducibility

## Rate Examples
| BRMA | SAR weekly | 1-bed weekly | 4-bed weekly | Source |
|------|------------|-------------|-------------|---------|
| Birmingham | 78.83 | 173.08 | 276.35 | DWP 2026-27 |
| Fylde Coast | 80.97 | 200.00 | 300.00 | DWP 2026-27 |
| Greater Liverpool | 79.47 | 200.00 | 288.85 | DWP 2026-27 |

## Connection
- Input: Output of Node 1 (parsed DWP CSV, 152 BRMAs)
- Output: brma_lha_rates table populated (152 rows)
- Prerequisite: Node 4 (table must exist)
