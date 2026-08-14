"""Shared Postgres connection helper for host-side build scripts.

PG_HOST in .env is the docker-internal name 'postgres'; from the Windows host
the same server is reachable on localhost:5432. Matches the established
access pattern (see scripts/s21_statistical_neighbours_build.py).

No credential has a fallback default. PG_USER and PG_PASSWORD must come from
the environment or a local .env, and the script stops with a clear error
rather than guessing. Host, port and database name keep defaults; they are
addressing, not credentials.
"""
import os
import sys
from pathlib import Path

import psycopg2

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env():
    """Read the first .env found alongside the repo, if any."""
    env = {}
    for candidate in (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
        break
    return env


ENV = load_env()


def _require(name):
    value = os.environ.get(name) or ENV.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or a local .env. This script will not guess a credential.")
    return value


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("PG_HOST_OVERRIDE", "localhost"),
        port=int(ENV.get("PG_PORT", "5432")),
        dbname=ENV.get("PG_DATABASE", "exempt_pipeline"),
        user=_require("PG_USER"),
        password=_require("PG_PASSWORD"),
    )


def get_readonly_conn():
    """A connection that cannot commit a write.

    Verification scripts run on this. A suite that can write is one wrong
    argument away from corrupting what it verifies, which is not theoretical:
    on 2026-08-14 s18_pipr_verify.py fell back to a stale default edition and
    its idempotency check rewrote 71,442 rows of a freshly loaded edition.
    Requiring the argument fixed that instance; this removes the capability.

    Two layers, because the first is not always available:

      1. Connect as PG_READONLY_USER where it is configured. ucws_readonly
         holds SELECT on the public schema and no write grant at all, so a
         write fails on privileges.
      2. Always set the session read-only regardless. Without a readonly
         credential this is the only barrier, and it still makes writes fail
         at the database rather than relying on the script behaving.

    A caller that genuinely needs to write inside a verification run must ask
    for it explicitly and roll back — see read_write_probe().
    """
    user = os.environ.get("PG_READONLY_USER") or ENV.get("PG_READONLY_USER")
    password = (os.environ.get("PG_READONLY_PASSWORD")
                or ENV.get("PG_READONLY_PASSWORD"))
    if user and password:
        conn = psycopg2.connect(
            host=os.environ.get("PG_HOST_OVERRIDE", "localhost"),
            port=int(ENV.get("PG_PORT", "5432")),
            dbname=ENV.get("PG_DATABASE", "exempt_pipeline"),
            user=user, password=password)
    else:
        conn = get_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    conn.autocommit = False
    return conn


def readonly_identity():
    """(user, whether a dedicated readonly role is in use) for reporting."""
    user = os.environ.get("PG_READONLY_USER") or ENV.get("PG_READONLY_USER")
    password = (os.environ.get("PG_READONLY_PASSWORD")
                or ENV.get("PG_READONLY_PASSWORD"))
    if user and password:
        return user, True
    return (os.environ.get("PG_USER") or ENV.get("PG_USER")), False
