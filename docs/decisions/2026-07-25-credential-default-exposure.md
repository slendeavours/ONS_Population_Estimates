# Database credential published as a fallback default in source

**Date:** 2026-07-25
**Status:** accepted; rotation outstanding at time of writing
**Affects:** the whole Postgres cluster, not only `exempt_pipeline`

## Context

`s15_hpi_build.py` supplied a complete connection blueprint as fallback
defaults — host, port, database name, and literals for both `PG_USER` and
`PG_PASSWORD`. The password literal is the live credential, not a stale
placeholder: it matches the value in `.env`.

It has been in the public repository across **17 commits under two filenames**
(`s19_hpi_build.py` before the renumbering, `s15_hpi_build.py` after), and is
present on `main`.

## Blast radius

The account is not a scoped application role. It is a **Postgres superuser**
with `CREATEDB` and `CREATEROLE`, owning four databases:

| Database | Contains |
|---|---|
| `exempt_pipeline` | the pipeline |
| `n8ndb` | n8n's own application database, including its credential store |
| `ucws` | separate application data |
| `postgres` | cluster default |

So the exposure is cluster-wide read/write/DDL plus role management, not
limited to pipeline data. `n8ndb` holds 24 stored n8n credentials — Airtable,
Anthropic, HubSpot, Microsoft Outlook, OpenAI, Pinecone, Stat-Xplore and
others. Those are encrypted at rest with `N8N_ENCRYPTION_KEY`, which lives in
the same `.env` as the database password. The `.env` itself is gitignored and
not tracked in either repository, so the encryption key was not published — but
anyone holding both would hold everything.

`.env` is confirmed untracked and gitignored in both the outer working
repository and this one. The leak path was the source default, not the
environment file.

## Decision

1. **Rotate.** The published value must be treated as compromised regardless of
   what else is done.
2. **Remove the defaults, fail loudly.** Every build script resolves `PG_USER`
   and `PG_PASSWORD` through `_require_env`, which exits with a hard stop
   rather than falling back to a literal. Host, port and database name keep
   defaults — they are addressing, not credentials.
3. **Do not rewrite history.** Rotation makes the exposed value inert. A
   rewrite would break every existing clone and fork and cannot recover a
   secret that must be assumed already harvested from a public repository.

## Why the scan did not catch it earlier

Two separate failures, and only one of them is the obvious one.

### Scope was the primary failure

The fallback pattern list used during the S6 review **did** contain a rule that
matches this leak — `password\s*=\s*["'][^"']{1,}["']`. It was run over the 16
files in the S6 diff. The leak is in a file S6 does not touch.

A scan scoped to a changeset can only ever find what that changeset introduces.
It cannot find what is already there. **Publishing gates must scan the whole
repository and its history, not the diff under review.** A diff-scoped scan
answers "am I adding a secret", which is a strictly weaker question than "does
this repository contain one".

### Pattern coverage was also a failure — of the tool

The obvious remedy is "install gitleaks", and gitleaks is now installed
(8.30.1). It does not, on its own, solve this.

Verified directly: with the **default ruleset**, both `gitleaks dir .` and
`gitleaks git .` report **"no leaks found"** against a tree and a history that
contain the live credential. The default rules target high-entropy,
structurally recognisable secrets — AWS access keys, JWTs, provider tokens with
known prefixes. A short lowercase dictionary-word password matches none of
them. Low entropy is what made the password weak *and* what made it invisible.

`.gitleaks.toml` therefore adds four rules:

| Rule | Catches |
|---|---|
| `hardcoded-credential-default` | `os.getenv`/`environ.get` for a credential name with a literal second argument |
| `inline-db-credential` | `password`/`user` assigned a literal, with environment lookups allowlisted |
| `connection-uri-with-credentials` | `postgres://user:pass@…` and equivalents |
| `local-absolute-path` | hardcoded machine paths that leak a username |

With that config the same full-history scan returns **8 findings across four
files**, including two in the S6 commits authored during this build.

### A third trap worth recording

`gitleaks git` scans **commit diffs**, not trees. A secret introduced once and
merely persisting thereafter appears only against the commit that introduced
it. Scanning `main -1` returns clean even though `main`'s tree contains the
credential. **Always scan the tree with `gitleaks dir .` as well as the history
with `gitleaks git .`.** Neither alone is sufficient.

## Consequences

- Running any build script without `PG_USER` and `PG_PASSWORD` in the
  environment now fails immediately with a named variable, rather than silently
  connecting as a superuser.
- Rotation must be coordinated. The credential is consumed in more places than
  the `.env` this repository's scripts read:
  1. Postgres itself (`ALTER USER`)
  2. `self-hosted-ai-starter-kit/.env` → `POSTGRES_PASSWORD`, which feeds the
     n8n container's `DB_POSTGRESDB_PASSWORD`
  3. `ucws-repo/.env` → `PG_PASSWORD`, read by the Python build scripts
  4. **Three stored n8n Postgres credentials** — `Postgres account`, `UCWS`,
     `exempt_pipeline` — held encrypted inside `n8ndb` and editable only
     through the n8n interface or API
  Steps 1–3 without step 4 break every n8n workflow that touches Postgres,
  including Workflow 1.
- Consider a scoped, non-superuser role for the pipeline. `pipeline_user`
  already exists and is not a superuser; the build scripts connect as the
  superuser only because that is what the `.env` supplies.

## Related

`docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md` — the standing
rule established there, that an unexplained finding is a gate rather than a
note, applies equally here. A credential default is not "a default"; it is a
published credential until proven otherwise.
