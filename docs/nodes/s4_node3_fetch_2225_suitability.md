# S4 Node 3 — Fetch 22-25 Accommodation Suitability CSV 2023–2025

> **Superseded 2026-10-09.** The n8n S4 workflow this note describes, and the `scripts/verify/rebuild_care_leavers.py` rebuild that followed it (now `scripts/historical/s4_rebuild_care_leavers.py`, which stops with a RETIRED message), are replaced by `scripts/s4_care_leaver_editions.py`, which keeps each release's figures for each year as an edition (`care_leaver_accommodation_editions`) and finds the latest release and both dataset ids itself. The source note is `docs/s4_care_leaver_source.md`. **Wrong in the text below:** the 22-25 dataset is not persistent. Its id was a96ea38e in 2024 and bd5240e0 in 2025, and its title changes, so a re-run does not pick up the next release automatically. The text below is left as written.

**Type:** HTTP Request

## Purpose

Fetches the DfE care leaver 22-25 accommodation suitability by LA dataset. Persistent EES dataset: a single UUID that updates in place with each annual release.

## URL

```
https://explore-education-statistics.service.gov.uk/data-catalogue/data-set/bd5240e0-76f9-4aa2-a307-7f3129a947a4/csv
```

## Configuration

- Method: GET
- Response Format: Text
- Put Output in Field: `data`
- Authentication: None

## Output

1 item, `data` field containing CSV. Columns: `time_period`, `time_identifier`, `geographic_level`, `country_code`, `country_name`, `region_code`, `region_name`, `old_la_code`, `la_name`, `new_la_code`, `care_leaver_age`, `breakdown`, `care_leaver_count`, `care_leaver_percent`.

## Schema differences from the 17-21 files

| 17-21 column | 22-25 equivalent |
|---|---|
| `age` | `care_leaver_age` (`22 years` … `25 years`) |
| `accommodation_type` | `breakdown` (`Accommodation considered suitable`, `Accommodation considered unsuitable`, `No information`, `Total`) |
| `number` | `care_leaver_count` |
| `percentage` | `care_leaver_percent` |

The 2025 release of the 17-21 accommodation dataset adopted this same naming. See [s4_care_leaver_source.md](../s4_care_leaver_source.md).

## Notes

- Suitability only, no accommodation type breakdown
- `No information` includes both those not in touch and those whose accommodation is unknown
- Covers only young people who contacted the authority and requested support, so figures are partial. DfE notes 2023 may undercount 24-year-olds by around 3% and 25-year-olds by around 10%
- Persistent UUID, so a re-run picks up the next release automatically

## Connection

- Input: Manual Trigger, runs in parallel with Nodes 1 and 2
- Output: Merge node, input 3

## Verified Output

1 item, `data` populated, years 2023–2025 confirmed. (2026-03-31)
