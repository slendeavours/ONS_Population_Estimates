# S24 — RSH register of registered providers and regulatory judgements

<!-- repo-meta
status: active
last-reviewed: 2026-08-14
type: source
consumed-by: scripts/s24_rsh_register_build.py, scripts/s24_rsh_register_verify.py
-->

| | |
|---|---|
| Publisher | Regulator of Social Housing |
| Series | Registered providers of social housing (monthly); Regulatory judgements and enforcement notices |
| Landing pages | https://www.gov.uk/government/publications/registered-providers-of-social-housing<br>https://www.gov.uk/government/publications/regulatory-judgements-and-enforcement-notices |
| Cadence | Register monthly, around mid-month; judgements as issued |
| Target tables | `rsh_registered_providers`, `rsh_regulatory_judgements`, `rsh_enforcement_notices` |
| Geography | **None** |
| Built | 2026-08-14 |

## Purpose — risk management, not analysis

The income route runs through a registered provider partner. A non-compliant
regulatory judgement, an enforcement notice or a de-registration is a material
event for the business, and the point of holding this is **to be told rather
than to find out**.

Secondary use: de-registrations are a business development signal. The failed
operator's landlords need a new manager.

## Deliberately not wired into Workflow 1

**S24 has no local authority geography and must not acquire one.** RSH does
not publish provider addresses or contact details, so there is nothing to
apportion. It is not in `staging_la_signals`, it is not a map layer, and
`publish_map` is false.

This is a design decision, recorded so it is not wired in later by reflex. A
provider's registered office is not where its stock is, and manufacturing a
geography from one would be worse than having none — it would put a confident
wrong number on a map.

The verification suite enforces it: gate 2 fails if any geography column
appears on these tables or if any S24 column reaches `staging_la_signals`.

For provider stock **by** authority, use S23, which has a real geography.

## What discovery established

The build brief allowed for regulatory judgements being available only as
individual documents per provider, in which case they were to be recorded as a
limitation rather than built.

**They are not.** RSH publishes a machine-readable
`RegulatoryJudgementsNotices_Published.xlsx` with consumer, governance,
viability and rent gradings per provider, each with its own grade date and a
change description, plus a second sheet of enforcement notices. The gradings
table was therefore built.

## The three tables

### `rsh_registered_providers` — monthly snapshot

One row per provider per snapshot date. 1,579 providers at 24 July 2026:
1,260 non-profit, 232 local authority, 87 profit.

Fields: registration number, organisation name, registration date,
designation, corporate form, notes. That is everything RSH publishes — there
is no address, no contact, no stock figure.

### `rsh_regulatory_judgements` — gradings

308 judgements covering 305 distinct providers, keyed on
`(registration_number, publication_date)`.

| Grade | Values seen | Ungraded |
|---|---|---:|
| Consumer | C1, C1\*, C2, C2\*, C3, C4 | 82 |
| Governance | G1, G1\*, G2, G2\*, G3 | 108 |
| Viability | V1, V1\*, V2, V3 | 108 |
| Rent | — | 302 |

### `rsh_enforcement_notices`

2 current notices, both on Economic Standards through Reactive Engagement.

## Change detection is why snapshots are stored

The register page carries **only the current month**. There is no archive.

So history exists only because this table stores one row per provider per
snapshot date rather than maintaining a current-state table. "What changed
since last month" is answerable from the table itself:

```sql
-- de-registered between two snapshots
SELECT p.registration_number, p.organisation_name
FROM rsh_registered_providers p
WHERE p.snapshot_date = '2026-07-24'
  AND NOT EXISTS (
    SELECT 1 FROM rsh_registered_providers q
    WHERE q.snapshot_date = '2026-08-24'
      AND q.registration_number = p.registration_number);
```

A load that overwrote the previous snapshot would destroy the only evidence of
what changed. Because the source is monthly and small — 1,579 rows — keeping
every snapshot costs nothing.

**De-registration is an absence, not an event.** The snapshot lists current
providers only, so a de-registration has no published date of its own and is
inferred from two snapshots. That is a limitation of the source, stated rather
than papered over.

## Unassessed grades

RSH writes `-` where a grade has not been assessed. Stored as NULL, never as
an empty string and never as the literal `-`, so "not assessed" stays
distinguishable from a real grading. The paired change description
("Not assessed yet", "Assessed and unchanged") is retained, so the reason
survives alongside the null.

Local authority providers receive **consumer gradings only**, so governance
and viability are legitimately null for all 100 of them rather than missing.

## Known traps

- **Wiring this into W1.** It has no geography. See above.
- **Overwriting the snapshot.** Destroys the change history, which is the
  point of the source.
- **Taking the snapshot date from the run date.** The file is published
  mid-month; the date comes from the attachment title.
- **Excel serial dates.** Some grade dates arrive as raw serials rather than
  typed dates.
- **Assuming a registration number is a stable identity.** L4331 appears in
  the judgements table under two landlord names. The publisher's "Name and Reg
  Code Change Details" column is stored verbatim rather than resolved.
- **Reading absence of a judgement as a clean bill of health.** Only 308 of
  1,579 providers have been assessed.
- **Committing the raw register workbook.** It carries a hidden sheet holding
  a stray account token from the publisher's own tooling. Nothing from it is
  read or stored, and `data/raw/` is gitignored.

## Reproducing

```bash
python scripts/s24_rsh_register_build.py --discover
python scripts/s24_rsh_register_build.py --load
python scripts/s24_rsh_register_verify.py
```
