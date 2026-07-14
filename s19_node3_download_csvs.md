# Node 3 — Download CSVs

## Type
Python script task

## Purpose
Download the two HPI CSV files and inspect their structure before processing.

## Logic
1. Download File 1 (`Average-prices-2026-04.csv`) to temp directory
2. Download File 2 (`Average-prices-Property-Type-2026-04.csv`) to temp directory
3. Print column names, first 5 rows, row count, and unique area code prefixes
4. Confirm both files pass sanity gate (> 1 KB)

## Key parameters
| Parameter | Value |
|---|---|
| File 1 columns | Date, Region_Name, Area_Code, Average_Price, Monthly_Change, Annual_Change, Average_Price_SA |
| File 2 columns | Date, Region_Name, Area_Code, Detached_Average_Price, Detached_Index, ..., Flat_Average_Price, Flat_Index, ... |
| File 1 rows | 150,705 |
| File 2 rows | 144,232 |

## Behaviour
Idempotent. Files are overwritten on each run.

## Verified output
Both CSVs downloaded and inspected on 2026-07-14. Structure confirmed.
