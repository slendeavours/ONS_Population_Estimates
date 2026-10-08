"""Make the LLM Report Generation workflow quote the like-for-like national TA.

Follows scripts/historical/w1_national_ta_like_for_like.py. staging_national now carries
the matched-set TA figures; the report workflow still handed the model the two
all-reporting totals (130,775 and 115,431) next to a +3.14% year-on-year, so a
section could have written "up from 115,431 to 130,775" beside +3.14%. Those
totals cover different sets of authorities.

Changes, two nodes of workflow "LLM Report Generation":
  * Fetch Staging Data: also select the three matched columns and the latest TA
    period (ta_period);
  * Define Sections: the national object drops the prior-year all-reporting
    total, carries the matched figures and a period label derived from
    ta_period, Section 2 and Section 6 are told to quote the change only on the
    matched base, and the stale hardcoded "Q2 2025" labels (two) read the
    derived label.

The workflow is inactive; nothing is generated. Each replacement must match
exactly once or the script stops. A node test (node, offline) runs the new
Define Sections code against the live run's values and checks the prompts.

Usage:
    python scripts/report_workflow_national_ta.py --dry-run   # test only, writes nothing
    python scripts/report_workflow_national_ta.py --apply
"""
import argparse
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import ENV, get_readonly_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO / "build_reports" / "w1_node_backups"


def log(m):
    print(f"{datetime.datetime.now():%H:%M:%S} {m}", flush=True)


def n8n_conn():
    return psycopg2.connect(
        host="localhost", port=int(ENV.get("PG_PORT", "5432")),
        dbname="n8ndb", user=ENV.get("PG_USER"), password=ENV.get("PG_PASSWORD"))

WORKFLOW = "LLM Report Generation"

FETCH_OLD = "    n.ta_yoy_pct,\n"
FETCH_NEW = ("    n.ta_yoy_pct,\n    n.ta_matched_authorities,\n"
             "    n.ta_households_current_matched,\n"
             "    n.ta_households_prev_year_matched,\n"
             "    (SELECT MAX(period) FROM la_statutory_homelessness) AS ta_period,\n")

NATIONAL_OLD = """const national = {
  ta_current: data.ta_households_current,
  ta_prev: data.ta_households_prev_year,
  ta_yoy_pct: data.ta_yoy_pct,
"""
NATIONAL_NEW = """// Financial-year quarters: Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar of the next year.
function taPeriodLabel(p) {
  const m = /^(\\d{4})Q([1-4])$/.exec(p || '');
  if (!m) return 'latest quarter';
  const y = Number(m[1]);
  const span = { 1: ['Apr-Jun', y], 2: ['Jul-Sep', y], 3: ['Oct-Dec', y], 4: ['Jan-Mar', y + 1] }[m[2]];
  return `${span[0]} ${span[1]} (${p})`;
}

// The like-for-like year-on-year compares authorities reporting in BOTH years.
// ta_england_total_reporting covers a different set of authorities from the
// prior year, so no prior-year total for it is passed: a change must never be
// derived from it.
const national = {
  ta_period_label: taPeriodLabel(data.ta_period),
  ta_england_total_reporting: data.ta_households_current,
  ta_matched_authorities: data.ta_matched_authorities,
  ta_matched_current: data.ta_households_current_matched,
  ta_matched_prior_year: data.ta_households_prev_year_matched,
  ta_yoy_pct_like_for_like: data.ta_yoy_pct,
"""

S2_OLD = "- Cover TA trend (current vs prior year, YoY%), rough sleeping,"
S2_NEW = ("- Cover TA trend: quote the year-on-year change ONLY from ta_matched_current, "
          "ta_matched_prior_year and ta_yoy_pct_like_for_like, and state ta_matched_authorities "
          "(the authorities reporting in both years). ta_england_total_reporting is a different "
          "set of authorities: quote it as a total only and never derive a change from it. "
          "Also cover rough sleeping,")

S6_OLD = ("- Demand trajectory: TA households nationally at ${national.ta_current} (Q2 2025), "
          "up ${national.ta_yoy_pct}% year-on-year")
S6_NEW = ("- Demand trajectory: ${national.ta_england_total_reporting} households in TA across the "
          "authorities reporting in ${national.ta_period_label}; like-for-like, ${national.ta_matched_current} "
          "against ${national.ta_matched_prior_year} a year earlier across ${national.ta_matched_authorities} "
          "authorities reporting in both years (${national.ta_yoy_pct_like_for_like}% year-on-year). "
          "Quote the change only on the like-for-like basis")

S1_OLD = "- Cover: national demand trajectory, top LA opportunities"
S1_NEW = ("- Cover: national demand trajectory (quote any TA change only from ta_matched_current, "
          "ta_matched_prior_year and ta_yoy_pct_like_for_like; never compare ta_england_total_reporting "
          "with a prior-year figure), top LA opportunities")

LABEL_OLD_1 = "Signal: households_in_ta from MHCLG statutory homelessness Q2 2025."
LABEL_NEW_1 = "Signal: households_in_ta from MHCLG statutory homelessness ${national.ta_period_label}."
LABEL_OLD_2 = "| Statutory Homelessness TA Live Tables | MHCLG | Q2 2025 (Jul-Sep) | LA |"
LABEL_NEW_2 = "| Statutory Homelessness TA Live Tables | MHCLG | ${national.ta_period_label} | LA |"


def once(text, old, new, what):
    if text.count(old) != 1:
        sys.exit(f"HALT: expected exactly one {what}, found {text.count(old)}")
    return text.replace(old, new)


def patch_fetch(sql):
    if "ta_households_current_matched" in sql:
        return sql
    return once(sql, FETCH_OLD, FETCH_NEW, "'n.ta_yoy_pct,' line in Fetch Staging Data")


def patch_sections(js):
    if "ta_yoy_pct_like_for_like" in js:
        # Already applied. A second stage added the Section 1 line later.
        return js if S1_NEW in js else once(js, S1_OLD, S1_NEW, "Section 1 national demand trajectory line")
    js = once(js, NATIONAL_OLD, NATIONAL_NEW, "national object")
    js = once(js, S2_OLD, S2_NEW, "Section 2 TA instruction")
    js = once(js, S6_OLD, S6_NEW, "Section 6 demand trajectory line")
    js = once(js, S1_OLD, S1_NEW, "Section 1 national demand trajectory line")
    js = once(js, LABEL_OLD_1, LABEL_NEW_1, "'Q2 2025' tenant-type signal label")
    js = once(js, LABEL_OLD_2, LABEL_NEW_2, "'Q2 2025' data appendix row")
    return js


HARNESS = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const row = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
row.convergence = []; row.la_signals = [];
row.tenant_rankings = ['single_homeless', 'rough_sleepers', 'care_leavers', 'domestic_abuse', 'older_complex']
  .map(t => ({ tenant_type: t, rank_position: 1, lad24cd: 'E00000000' }));
const $ = () => ({ first: () => ({ json: row }) });
const out = new Function('$', code)($);
const by = Object.fromEntries(out.map(o => [o.json.section_id, o.json]));
const p2 = by.section_2.generation_prompt, p6 = by.section_6.generation_prompt;
const bad = [];
const chk = (c, m) => { if (!c) bad.push(m); };
chk(by.section_2.data.ta_matched_prior_year === row.ta_households_prev_year_matched, 'matched prior not in section 2 data');
chk(!JSON.stringify(by.section_2.data).includes(String(row.ta_households_prev_year)), 'all-reporting prior total leaked into section 2 data');
chk(p2.includes('ta_yoy_pct_like_for_like'), 'section 2 lacks like-for-like instruction');
chk(p6.includes(String(row.ta_households_current_matched)) && p6.includes(String(row.ta_households_prev_year_matched)), 'section 6 lacks matched figures');
chk(!p6.includes(String(row.ta_households_prev_year)), 'section 6 shows all-reporting prior total');
for (const s of out) chk(!/Q2 2025/.test(s.json.generation_prompt), s.json.section_id + ' still has the stale Q2 2025 label');
chk(p6.includes('Jan-Mar 2026 (2025Q4)'), 'period label not derived');
chk(by.section_1.generation_prompt.includes('ta_yoy_pct_like_for_like'), 'section 1 lacks like-for-like instruction');
for (const s of out) chk(!/undefined|NaN/.test(s.json.generation_prompt), s.json.section_id + ' prompt contains undefined/NaN');
console.log(JSON.stringify({ sections: out.length, bad }));
process.exit(bad.length ? 1 : 0);
"""


def live_row():
    conn = get_readonly_conn()
    cur = conn.cursor()
    cur.execute("""SELECT n.run_id, n.ta_households_current, n.ta_households_prev_year, n.ta_yoy_pct,
                          n.ta_matched_authorities, n.ta_households_current_matched,
                          n.ta_households_prev_year_matched,
                          (SELECT MAX(period) FROM la_statutory_homelessness),
                          n.rough_sleeping_current, n.rough_sleeping_prev_year, n.bb_spend_total_000,
                          n.nightly_paid_spend_total_000, n.hb_sa_caseload_total, n.housing_register_total
                   FROM staging_national n
                   WHERE n.run_id = (SELECT MAX(run_id) FROM staging_runs WHERE status = 'complete')""")
    r = cur.fetchone()
    keys = ["run_id", "ta_households_current", "ta_households_prev_year", "ta_yoy_pct",
            "ta_matched_authorities", "ta_households_current_matched",
            "ta_households_prev_year_matched", "ta_period", "rough_sleeping_current",
            "rough_sleeping_prev_year", "bb_spend_total_000", "nightly_paid_spend_total_000",
            "hb_sa_caseload_total", "housing_register_total"]
    conn.close()
    return {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in zip(keys, r)}


def node_test(js):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "code.js").write_text(js, encoding="utf-8")
        (d / "row.json").write_text(json.dumps(live_row()), encoding="utf-8")
        (d / "h.js").write_text(HARNESS, encoding="utf-8")
        p = subprocess.run(["node", str(d / "h.js"), str(d / "code.js"), str(d / "row.json")],
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        sys.exit("choose --apply or --dry-run")

    nc = n8n_conn()
    cur = nc.cursor()
    cur.execute("SELECT id, nodes FROM workflow_entity WHERE name = %s", (WORKFLOW,))
    rows = cur.fetchall()
    if len(rows) != 1:
        sys.exit(f"HALT: expected one workflow named {WORKFLOW!r}, found {len(rows)}")
    wf_id, nodes = rows[0]
    by = {n["name"]: n for n in nodes}
    fetch, sections = by["Fetch Staging Data"], by["Define Sections"]
    old_fetch, old_js = fetch["parameters"]["query"], sections["parameters"]["jsCode"]
    new_fetch, new_js = patch_fetch(old_fetch), patch_sections(old_js)

    rc, out = node_test(new_js)
    log(f"node test of the new Define Sections code: {'PASS' if rc == 0 else 'FAIL'} {out}")
    if rc != 0:
        sys.exit("HALT: node test failed; nothing written")

    if args.dry_run:
        log(f"DRY RUN: Fetch Staging Data {'would change' if new_fetch != old_fetch else 'unchanged'}; "
            f"Define Sections {'would change' if new_js != old_js else 'unchanged'}; nothing written")
        nc.close()
        return 0

    if new_fetch == old_fetch and new_js == old_js:
        log("both nodes already carry the patch; nothing written")
        nc.close()
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    backup = BACKUP_DIR / f"report_workflow_backup_{stamp}.json"
    backup.write_text(json.dumps(
        {"backed_up_at": stamp, "workflow_id": wf_id, "workflow": WORKFLOW,
         "nodes": {"Fetch Staging Data": old_fetch, "Define Sections": old_js}},
        indent=1), encoding="utf-8")
    log(f"previous node text backed up to {backup.relative_to(REPO)}")

    fetch["parameters"]["query"] = new_fetch
    sections["parameters"]["jsCode"] = new_js
    cur.execute('UPDATE workflow_entity SET nodes = %s, "updatedAt" = now() WHERE id = %s',
                (json.dumps(nodes), wf_id))
    nc.commit()
    cur.execute("SELECT nodes FROM workflow_entity WHERE id = %s", (wf_id,))
    stored = {n["name"]: n["parameters"] for n in cur.fetchone()[0]}
    if (stored["Fetch Staging Data"]["query"] != new_fetch
            or stored["Define Sections"]["jsCode"] != new_js):
        sys.exit("HALT: readback does not match what was written")
    log("readback confirms both nodes")
    nc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
