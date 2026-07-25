# Database credential published as a fallback default in source

**Date:** 2026-07-25
**Status:** accepted; rotation outstanding at time of writing
**Affects:** the whole Postgres cluster, not only `exempt_pipeline`

## Context

`s15_hpi_build.py` supplied a complete connection blueprint as fallback
defaults — host, port, database name, and literals for both `PG_USER` and
`PG_PASSWORD`. The password literal is the live credential, not a stale
placeholder: it matches the value in `.env`.

It has been in the public repository across **25 commits under two filenames**
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

## Exposure window (assumption, not a finding)

**Assume public from 2026-07-14**, the date the credential was first committed.

The repository was created on GitHub on 2026-03-25 and is public now. Whether
it was ever private cannot be established: GitHub exposes visibility-change
history only through the *organisation* audit log, and `slendeavours` is a user
account, so no such log exists. This is recorded as an assumption so nobody
later mistakes it for a verified timeline.

Zero forks and zero stars are **not** evidence that nothing was cloned. Clone
counts are not public, automated scrapers index public repositories
continuously, and a credential in a public repository must be treated as
harvested from the moment it lands.

## Reachability of port 5432 — what bounds the incident

Established from the host on 2026-07-25:

| Check | Finding |
|---|---|
| Compose publishes | `"5432:5432"` with no host IP, so `docker port` reports `0.0.0.0:5432` and `[::]:5432` — **all interfaces** |
| `listen_addresses` | `*` |
| `pg_hba.conf` final rule | `host all all all scram-sha-256` — **any host, any user, any database**, password only |
| TLS | `ssl = off` — connections are unencrypted |
| Windows Firewall | No rule scoped to 5432. Two enabled inbound `Docker Desktop Backend` **Allow** rules on the **Public** profile, which permit inbound to published container ports |
| Host address | `192.168.1.203/24` on WiFi, gateway `192.168.1.254` — RFC1918, behind NAT |
| Log retention | Container log spans **2026-05-09 to present**, so it covers the whole exposure window and has not rotated past it |
| `log_connections` | **off** |
| `log_line_prefix` | `'%m [%p] '` — **no `%h`**, so no client address on any line |

**Conclusion: LAN-reachable is confirmed. Internet-reachability could not be
excluded from the host.** Anyone on `192.168.1.0/24` could reach
`192.168.1.203:5432` and authenticate as a superuser with the published
credential. Whether that extended beyond the LAN depends on two things not
readable from this machine: the router's inbound port-forwarding or UPnP state,
and the Cloudflare tunnel's ingress rules. The tunnel is token-based and
remotely managed (`tunnel run --token …`), so its configuration lives in the
Cloudflare Zero Trust dashboard rather than on disk. `cloudflared` shares the
`demo` Docker network with `postgres`, so it *can* route to `postgres:5432`;
whether any ingress rule does so is unknown.

### What the logs can and cannot tell us

25 `FATAL` lines are retained. Authentication failures for user `postgres`
cluster on 15–16 June 2026 — a signature consistent with automated scanning,
but **predating the 14 July exposure**. One `pipeline_user` failure on 10 July.
**None after 14 July.**

That absence is not reassurance. With `log_connections = off` and no `%h` in
the log prefix, **a successful login using the correct credential leaves no
trace whatsoever.** The log can show failed attempts; it cannot show whether
anyone succeeded. Absence of evidence here is not evidence of absence.

## Scan coverage

gitleaks reports commits processed, not commits examined; the two differ by 13
in this repository and the mechanism for 12 of them was not established. For
secrets with a known literal value, coverage is complete and independently
verified across all 78 commits. For unknown secrets, up to 13 commits may not
have been pattern-matched; `gitleaks dir` on the working tree mitigates this
for anything currently present.

Report the tool's figure as **commits reported by tool**, never as "commits
scanned", so it is not mistaken for a coverage guarantee.

Commits orphaned by a branch rebuild remain in the local object store but are
unreachable from any ref. Unreachable objects are never transferred on clone,
and none of those branches were pushed, so they are local-only and are not
exposure. `git gc` will drop them.

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

### The clone was shallow

The first "full history" scan ran against a **shallow clone**: 22 commits of an
actual 78. `git rev-parse --is-shallow-repository` returned true. The count of
affected commits was revised from 17 to **25** after `git fetch --unshallow`.

Publishing gates must assert `--is-shallow-repository` is false before treating
any history scan as complete. A shallow clone will report clean on history it
has never seen.

### Scan counts are not coverage guarantees

`gitleaks git` reports fewer commits scanned than the repository contains, and
the shortfall is not fully attributable:

| Scope | `git rev-list --count` | gitleaks reports |
|---|---:|---:|
| `--all` | 78 | 65 |
| `--branches` | 73 | 62 |
| `--remotes` | 62 | 51 |
| `HEAD` | 67 | 56 |

Established by testing: `--log-opts` is honoured (`-n 3` scans 3, `-n 20` scans
20); the default scope behaves as `--all`, verified by checking out `main`
(62 reachable) and still seeing 65 scanned; merge commits are skipped, since
`X` and `X --no-merges` return identical counts, and `git log -p` emits no
patch for a merge. Only one commit in this repository has an empty diff, so
merges account for one of the missing thirteen. **The remaining twelve are
unattributed.** Treat "N commits scanned" as telemetry, not as proof that N
commits were examined.

Separately, commits orphaned by a branch rebuild remain in the object store but
are unreachable from any ref, so neither `--all` nor gitleaks sees them. They
are equally invisible to anyone cloning, and `git gc` will eventually drop
them, but they are not covered by any scan.

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
- **Follow-up, not this window: `N8N_ENCRYPTION_KEY` is weak.** 16 characters,
  lowercase letters plus two punctuation marks, only 10 distinct characters,
  Shannon entropy 3.12 bits per character. That is a **human-chosen passphrase,
  not a random key** — roughly 50 bits of real entropy against a naive attacker
  and materially less against a dictionary attack.
  `N8N_USER_MANAGEMENT_JWT_SECRET` has the same profile. Verified they are not
  equal to each other and share no four-character substring with the database
  password, so there is no credential reuse.
  It was never published (searched by literal value across all 78 commits), so
  this is hardening rather than incident response.
  **Consequence of rotating it: all 24 stored n8n credentials become
  undecryptable and every one must be re-entered by hand** — Airtable,
  Anthropic, HubSpot, Microsoft Outlook, OpenAI, Pinecone, Stat-Xplore and the
  rest. That is a scheduled maintenance task with the credential values to hand,
  not something to fold into a database password rotation.
- Consider a scoped, non-superuser role for the pipeline. `pipeline_user`
  already exists and is not a superuser; the build scripts connect as the
  superuser only because that is what the `.env` supplies.

## Related

`docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md` — the standing
rule established there, that an unexplained finding is a gate rather than a
note, applies equally here. A credential default is not "a default"; it is a
published credential until proven otherwise.
