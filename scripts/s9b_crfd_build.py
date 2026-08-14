"""S9b — MHSDS MHS26 Clinically Ready for Discharge (CRFD) monthly ETL.

Reconstructed 2026-08-14, for the same reason as S9a: 11,248 rows were live
and wired into Workflow 1 with no code path back to them. Rebuilt from
docs/nodes/s9b_node1..s9b_node3 and verified by exact reproduction against the
live table.

nhs_mh_crfd records the source URL per row, so the rebuild is pointed at the
files that produced the live data rather than at whatever the publication
pages serve today.

The monthly files are large — around 70 MB for the 2023 CSVs, 1.6 GB across
the full set — so they are streamed and parsed a line at a time rather than
downloaded and held. Only MHS26 local-authority rows survive the filter, a few
hundred per file out of millions.

Usage:
    python scripts/s9b_crfd_build.py --reproduce
    python scripts/s9b_crfd_build.py --load --url <file-url> --period YYYY-MM-01
"""
import argparse
import csv
import io
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "ucws-pipeline/s9b (+sl@slendeavours.org)"}
TABLE = "nhs_mh_crfd"
STAGING = "nhs_mh_crfd_repro"

MEASURE = "MHS26"
ENGLAND_PREFIXES = ("E06", "E07", "E08", "E09")
SUPPRESSION = {"*", "-", "..", ""}

INSERT_COLS = ["reporting_period", "lad24cd", "la_name", "measure_id",
               "measure_name", "measure_value", "source"]


def halt(msg):
    sys.exit(f"HALT: {msg}")


def open_stream(url):
    """Yield decoded text lines from a CSV, or from the CSV inside a ZIP.

    A ZIP has to land on disk because the central directory is at the end;
    a plain CSV is streamed straight through and never stored.
    """
    req = urllib.request.Request(url, headers=UA)
    if url.lower().endswith(".zip"):
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    tmp.write(chunk)
            tmp.close()
            with zipfile.ZipFile(tmp.name) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".csv")]
                if not names:
                    halt(f"{url}: no CSV inside the archive")
                # The data file is the largest CSV; the others are metadata.
                name = max(names, key=lambda n: z.getinfo(n).file_size)
                with z.open(name) as f:
                    for line in io.TextIOWrapper(f, encoding="utf-8-sig",
                                                 errors="replace"):
                        yield line
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    else:
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in io.TextIOWrapper(r, encoding="utf-8-sig",
                                         errors="replace"):
                yield line


def parse(url, period):
    """MHS26 local-authority rows from one monthly file."""
    rows, header, idx = [], None, {}
    for line in csv.reader(open_stream(url)):
        if header is None:
            header = [h.strip().upper() for h in line]
            need = ("MEASURE_ID", "BREAKDOWN", "PRIMARY_LEVEL",
                    "SECONDARY_LEVEL", "MEASURE_VALUE")
            missing = [c for c in need if c not in header]
            if missing:
                halt(f"{url}: header missing {missing}")
            idx = {c: header.index(c) for c in header}
            continue
        if len(line) < len(header):
            continue
        if line[idx["MEASURE_ID"]].strip() != MEASURE:
            continue
        if "local authority" not in line[idx["BREAKDOWN"]].strip().lower():
            continue
        code = line[idx["PRIMARY_LEVEL"]].strip()
        if not code.startswith(ENGLAND_PREFIXES):
            continue
        if line[idx["SECONDARY_LEVEL"]].strip().upper() != "NONE":
            continue

        raw = line[idx["MEASURE_VALUE"]].strip()
        # '*' is small-number suppression. NULL, never zero.
        value = None
        if raw not in SUPPRESSION:
            try:
                value = int(float(raw.replace(",", "")))
            except ValueError:
                value = None

        name_col = ("PRIMARY_LEVEL_DESCRIPTION" if "PRIMARY_LEVEL_DESCRIPTION"
                    in idx else None)
        measure_name_col = "MEASURE_NAME" if "MEASURE_NAME" in idx else None
        rows.append((period, code,
                     line[idx[name_col]].strip() if name_col else None,
                     MEASURE,
                     line[idx[measure_name_col]].strip()
                     if measure_name_col else None,
                     value, url))
    return rows


def write_rows(cur, table, rows):
    placeholders = ", ".join(["%s"] * len(INSERT_COLS))
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in INSERT_COLS
        if c not in ("reporting_period", "lad24cd", "measure_id"))
    sql = (f"INSERT INTO {table} ({', '.join(INSERT_COLS)}) "
           f"VALUES ({placeholders}) "
           f"ON CONFLICT (reporting_period, lad24cd, measure_id) DO UPDATE SET "
           f"{updates}, loaded_at = now()")
    psycopg2.extras.execute_batch(cur, sql, rows)


def diff(cur):
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    live_n = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {STAGING}")
    repro_n = cur.fetchone()[0]
    key = "reporting_period, lad24cd, measure_id"
    cur.execute(f"SELECT COUNT(*) FROM (SELECT {key} FROM {TABLE} "
                f"EXCEPT SELECT {key} FROM {STAGING}) d")
    only_live = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM (SELECT {key} FROM {STAGING} "
                f"EXCEPT SELECT {key} FROM {TABLE}) d")
    only_repro = cur.fetchone()[0]

    mismatches = []
    for col in ("la_name", "measure_name", "measure_value"):
        cur.execute(f"""
            SELECT COUNT(*) FROM {TABLE} a JOIN {STAGING} b
              ON a.reporting_period = b.reporting_period
             AND a.lad24cd = b.lad24cd AND a.measure_id = b.measure_id
            WHERE a."{col}" IS DISTINCT FROM b."{col}"
        """)
        n = cur.fetchone()[0]
        if n:
            cur.execute(f"""
                SELECT a.reporting_period, a.lad24cd, a."{col}", b."{col}"
                FROM {TABLE} a JOIN {STAGING} b
                  ON a.reporting_period = b.reporting_period
                 AND a.lad24cd = b.lad24cd AND a.measure_id = b.measure_id
                WHERE a."{col}" IS DISTINCT FROM b."{col}" LIMIT 3
            """)
            mismatches.append((col, n, cur.fetchall()))
    return live_n, repro_n, only_live, only_repro, mismatches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reproduce", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--period")
    ap.add_argument("--limit", type=int, help="reproduce only the first N periods")
    args = ap.parse_args()
    if not (args.reproduce or args.load):
        halt("choose --reproduce or --load")

    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        if args.load:
            if not (args.url and args.period):
                halt("--load needs --url and --period")
            rows = parse(args.url, args.period)
            print(f"{args.period}: {len(rows)} MHS26 LA rows")
            write_rows(cur, TABLE, rows)
            conn.commit()
            print("COMMITTED")
            return 0

        cur.execute(f"SELECT DISTINCT reporting_period::text, source "
                    f"FROM {TABLE} ORDER BY 1")
        targets = cur.fetchall()
        if args.limit:
            targets = targets[:args.limit]
        print(f"reproducing {len(targets)} period(s) from recorded source URLs")

        cur.execute(f"CREATE TABLE IF NOT EXISTS {STAGING} "
                    f"(LIKE {TABLE} INCLUDING ALL)")
        cur.execute(f"TRUNCATE {STAGING}")
        for period, url in targets:
            rows = parse(url, period)
            write_rows(cur, STAGING, rows)
            print(f"  {period}  {len(rows):>4} rows  {url.rsplit('/', 1)[-1][:50]}",
                  flush=True)

        live_n, repro_n, only_live, only_repro, mismatches = diff(cur)
        conn.commit()
        print()
        print(f"live rows      : {live_n}"
              + ("  (full table; a limited run compares a subset)"
                 if args.limit else ""))
        print(f"rebuilt rows   : {repro_n}")
        print(f"keys only live : {only_live}")
        print(f"keys only repro: {only_repro}")
        if mismatches:
            print(f"columns differing: {len(mismatches)}")
            for col, n, sample in mismatches:
                print(f"  {col}: {n} row(s)")
                for s in sample:
                    print(f"     {s[0]} {s[1]}: live={s[2]!r} repro={s[3]!r}")
        else:
            print("columns differing: 0")
        ok = (not only_repro and not mismatches
              and (args.limit or (live_n == repro_n and not only_live)))
        print()
        print("EXACT REPRODUCTION" if ok else "REPRODUCTION FAILED")
        return 0 if ok else 1
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
