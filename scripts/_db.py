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
