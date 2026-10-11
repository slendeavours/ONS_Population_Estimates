"""Retirement of the S3 and S14 n8n loader workflows.

Fixture workflow JSON only: no database, no n8n. A fake cursor stands in for
the n8ndb connection. The S14 fixture has the shape of the real workflow: six
Postgres nodes carry no query text at all (an empty executeQuery)."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import n8n_retire_s3_s14 as m  # noqa: E402

TRIGGER = "When clicking Execute workflow"
S3 = "AlIOqA6ARCRyNDai"
S14 = "S44UCK95djqE3aLw"
EMPTY = "sk295Li2DyMqDSYY"
S3_CODE = "Process Population Data"


def pg(name, query=None, operation="executeQuery"):
    p = {"operation": operation, "options": {}}
    if query is not None:
        p["query"] = query
    return {"name": name, "type": "n8n-nodes-base.postgres", "parameters": p,
            "credentials": {"postgres": {"id": "x", "name": "n"}}}


def code(name, js):
    return {"name": name, "type": "n8n-nodes-base.code", "parameters": {"jsCode": js}}


def http(name, url):
    return {"name": name, "type": "n8n-nodes-base.httpRequest", "parameters": {"url": url}}


def to(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0} for n in names]]}


def s3(js="const supplementRaw = 1; return [];"):
    nodes = [
        {"name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        http("Fetch Nomis Data", "https://www.nomisweb.co.uk/api/x"),
        http("Fetch Supplement CSV", "https://raw.example/mye_2024_supplement.csv"),
        {"name": "Merge", "type": "n8n-nodes-base.merge", "parameters": {}},
        code(S3_CODE, js),
        pg("Create Table If Not Exists", "CREATE TABLE IF NOT EXISTS la_population (a int)"),
        pg("Upsert Population Data", "INSERT INTO la_population SELECT 1"),
        pg("Log Run", "INSERT INTO pipeline_run_log VALUES (1)"),
    ]
    conns = {TRIGGER: to("Fetch Nomis Data"), "Fetch Nomis Data": to("Fetch Supplement CSV", "Merge"),
             "Fetch Supplement CSV": to("Merge"), "Merge": to(S3_CODE),
             S3_CODE: to("Create Table If Not Exists"),
             "Create Table If Not Exists": to("Upsert Population Data"),
             "Upsert Population Data": to("Log Run")}
    return {"id": S3, "name": "ONS Population Estimates (S3)", "active": False,
            "nodes": nodes, "connections": conns}


def s14(url="https://lha-direct.voa.gov.uk/x?{{ $json.postcode }}", branch_ok=True):
    nodes = [
        {"name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        http("Fetch LHA Rates CSV", "https://assets.example/rates.csv"),
        http("Fetch List of Rents CSV", "https://assets.example/rents.csv"),
        {"name": "Merge", "type": "n8n-nodes-base.merge", "parameters": {}},
        code("Process Rate Data", "for (const item of $input.all()) { item.json.myNewField = 1; }"),
        pg("Create Table"),
        pg("Upsert LHA Rates"),
        pg("Create Table If Not Exists1"),
        pg("Upsert BRMA Mapping2"),
        pg("Log Run2"),
        pg("Execute a SQL query", "SELECT lad24cd FROM la_boundaries;"),
        http("Postcodes.io", "https://api.postcodes.io/postcodes"),
        code("Extract Postcode", "return [{json:{postcode:'x'}}];"),
        http("Fetch VOA LHA Rates", url),
        code("Parse VOA Response", "const html = 1; return [];"),
        pg("Create Table If Not Exists", "CREATE TABLE IF NOT EXISTS la_lha_rates (a int)"),
        pg("Upsert BRMA Mapping", "INSERT INTO la_lha_rates SELECT 1"),
        pg("Log Run"),
    ]
    conns = {TRIGGER: to("Execute a SQL query"),
             "Fetch LHA Rates CSV": to("Fetch List of Rents CSV", "Merge"),
             "Fetch List of Rents CSV": to("Merge"),
             "Merge": to("Process Rate Data"),
             "Process Rate Data": to("Create Table"),
             "Create Table": to("Upsert LHA Rates"),
             "Upsert LHA Rates": to("Create Table If Not Exists1"),
             "Create Table If Not Exists1": to("Upsert BRMA Mapping2"),
             "Upsert BRMA Mapping2": to("Log Run2"),
             "Execute a SQL query": to("Postcodes.io"),
             "Postcodes.io": to("Extract Postcode"),
             "Extract Postcode": to("Fetch VOA LHA Rates"),
             "Fetch VOA LHA Rates": to("Parse VOA Response"),
             "Parse VOA Response": to("Create Table If Not Exists"),
             "Create Table If Not Exists": to("Upsert BRMA Mapping"),
             "Upsert BRMA Mapping": to("Log Run")}
    if not branch_ok:
        del conns["Process Rate Data"]
    return {"id": S14, "name": "LHA Rates + List of Rents (S14)", "active": False,
            "nodes": nodes, "connections": conns}


def empty_wf():
    return {"id": EMPTY, "name": "VOA/DWP LHA Rates (S14)", "active": False,
            "nodes": [], "connections": {}}


def fixture():
    return [s3(), s14(), empty_wf()]


def node(w, name):
    return [n for n in w["nodes"] if n["name"] == name][0]


class FakeConn:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, workflows, corrupt_update=False):
        self.rows = {w["id"]: w for w in workflows}
        self.connection = FakeConn()
        self.updates = []
        self.corrupt = corrupt_update
        self._res = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT"):
            w = self.rows.get(params[0])
            self._res = [] if w is None else [
                (w["name"], w["active"], copy.deepcopy(w["nodes"]), copy.deepcopy(w["connections"]))]
        elif s.startswith("UPDATE"):
            nodes_json, wid = params
            nodes = json.loads(nodes_json)
            if self.corrupt:
                nodes = nodes[:-1]
            self.rows[wid]["nodes"] = nodes
            self.updates.append(wid)
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._res


class Downstream(unittest.TestCase):
    def test_branching_and_cycle(self):
        conns = {"a": {"main": [[{"node": "b"}, {"node": "c"}], [{"node": "d"}]]},
                 "b": {"main": [[{"node": "e"}]]}, "c": {"main": [[{"node": "e"}]]}}
        self.assertEqual(m.downstream(conns, "a"), {"b", "c", "d", "e"})
        self.assertEqual(m.downstream({"a": to("b"), "b": to("a")}, "a"), {"a", "b"})


class WriteNodes(unittest.TestCase):
    def test_query_writes_found_selects_ignored(self):
        nodes = [pg("i", "INSERT INTO t VALUES (1)"), pg("c", "CREATE TABLE IF NOT EXISTS t (a int)"),
                 pg("s", "SELECT updated_at FROM t"), code("code", "INSERT")]
        self.assertEqual(sorted(m.write_nodes(nodes)), ["c", "i"])

    def test_empty_execute_query_is_not_a_write_but_is_listed(self):
        nodes = [pg("e"), pg("i", "INSERT INTO t VALUES (1)")]
        self.assertEqual(m.write_nodes(nodes), ["i"])
        self.assertEqual(m.empty_query_nodes(nodes), ["e"])

    def test_queryless_insert_operation_is_a_write(self):
        nodes = [pg("u", operation="upsert"), pg("d", operation="delete")]
        self.assertEqual(sorted(m.write_nodes(nodes)), ["d", "u"])

    def test_unknown_operation_without_query_halts(self):
        with self.assertRaises(m.Halt):
            m.write_nodes([pg("x", operation="somethingNew")])

    def test_blank_query_text_is_an_empty_query(self):
        self.assertEqual(m.write_nodes([pg("b", "  ")]), [])
        self.assertEqual(m.empty_query_nodes([pg("b", "  ")]), ["b"])

    def test_real_shape_s14_has_six_empty_and_four_writes_with_query(self):
        w = s14()
        self.assertEqual(sorted(m.empty_query_nodes(w["nodes"])), sorted([
            "Create Table", "Upsert LHA Rates", "Create Table If Not Exists1",
            "Upsert BRMA Mapping2", "Log Run2", "Log Run"]))
        self.assertEqual(sorted(m.write_nodes(w["nodes"])),
                         ["Create Table If Not Exists", "Upsert BRMA Mapping"])


class Plan(unittest.TestCase):
    def test_s3_as_surveyed_has_no_extras(self):
        self.assertEqual(m.plan(s3()), ([S3_CODE], []))

    def test_s14_as_surveyed_has_no_extras_and_both_code_nodes(self):
        codes, extras = m.plan(s14())
        self.assertEqual(codes, ["Process Rate Data", "Parse VOA Response"])
        self.assertEqual(extras, [])

    def test_s14_every_postgres_node_is_downstream_of_a_replaced_code_node(self):
        w = s14()
        down = set()
        for c in ("Process Rate Data", "Parse VOA Response"):
            down |= m.downstream(w["connections"], c)
        pgs = [n["name"] for n in w["nodes"] if n["type"].endswith(".postgres")]
        # the SELECT on the trigger branch is the only Postgres node upstream
        self.assertEqual([p for p in pgs if p not in down], ["Execute a SQL query"])

    def test_s14_write_with_query_off_the_graph_is_an_extra(self):
        w = s14(branch_ok=False)
        w["nodes"][[n["name"] for n in w["nodes"]].index("Create Table")] = pg(
            "Create Table", "CREATE TABLE IF NOT EXISTS t (a int)")
        w["nodes"][[n["name"] for n in w["nodes"]].index("Upsert LHA Rates")] = pg(
            "Upsert LHA Rates", "INSERT INTO t SELECT 1")
        _, extras = m.plan(w)
        self.assertEqual(sorted(extras), ["Create Table", "Upsert LHA Rates"])

    def test_s14_empty_query_nodes_off_the_graph_are_not_extras(self):
        _, extras = m.plan(s14(branch_ok=False))
        self.assertEqual(extras, [])

    def test_off_graph_write_with_no_query_to_replace_halts(self):
        w = s14(branch_ok=False)
        i = [n["name"] for n in w["nodes"]].index("Upsert LHA Rates")
        w["nodes"][i] = pg("Upsert LHA Rates", operation="upsert")
        with self.assertRaises(m.Halt):
            m.rewrite(w)

    def test_code_node_missing_or_duplicated_halts(self):
        w = s3()
        node(w, S3_CODE)["name"] = "Other"
        with self.assertRaises(m.Halt):
            m.plan(w)
        w = s14()
        w["nodes"].append(copy.deepcopy(node(w, "Parse VOA Response")))
        with self.assertRaises(m.Halt):
            m.plan(w)
        w = s14()
        node(w, "Parse VOA Response")["name"] = "Other"
        with self.assertRaises(m.Halt):
            m.plan(w)


class Rewrite(unittest.TestCase):
    def test_s3_marker_missing_halts(self):
        w = s3(js="return [];")
        w["nodes"] = [n for n in w["nodes"] if n["name"] != "Fetch Supplement CSV"]
        w["connections"].pop("Fetch Supplement CSV")
        with self.assertRaises(m.Halt):
            m.rewrite(w)

    def test_s14_marker_missing_halts_and_either_marker_passes(self):
        w = s14(url="https://elsewhere.example/x")
        w["nodes"] = [n for n in w["nodes"]]
        node(w, "Create Table If Not Exists")["parameters"]["query"] = "CREATE TABLE IF NOT EXISTS other (a int)"
        node(w, "Upsert BRMA Mapping")["parameters"]["query"] = "INSERT INTO other SELECT 1"
        with self.assertRaises(m.Halt):
            m.rewrite(w)
        # the URL marker alone is enough
        m.rewrite(s14(url="https://lha-direct.voa.gov.uk/x"))
        # the table marker alone is enough
        m.rewrite(s14(url="https://elsewhere.example/x"))

    def test_active_or_wrong_name_halts(self):
        w = s3()
        w["active"] = True
        with self.assertRaises(m.Halt):
            m.rewrite(w)
        w = s14()
        w["name"] = "Something else"
        with self.assertRaises(m.Halt):
            m.rewrite(w)

    def test_s3_only_the_code_value_changes(self):
        w = s3()
        new, changed, old = m.rewrite(w)
        self.assertEqual(changed, [S3_CODE])
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        self.assertEqual(sorted(a["name"] for a, b in zip(w["nodes"], new) if a != b), changed)
        got = node({"nodes": new}, S3_CODE)["parameters"]["jsCode"]
        self.assertTrue(got.startswith('throw new Error("RETIRED 2026-10-11'))
        self.assertIn("la_population", got)
        self.assertIn("scripts/s3_mye_editions.py", got)
        self.assertIn("preview by default", got)
        self.assertIn("supplementRaw", old[S3_CODE])

    def test_s14_both_code_nodes_change_and_nothing_else(self):
        w = s14()
        new, changed, old = m.rewrite(w)
        self.assertEqual(sorted(changed), ["Parse VOA Response", "Process Rate Data"])
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        self.assertEqual(sorted(a["name"] for a, b in zip(w["nodes"], new) if a != b), sorted(changed))
        for c in changed:
            got = node({"nodes": new}, c)["parameters"]["jsCode"]
            self.assertTrue(got.startswith('throw new Error("RETIRED 2026-10-11'))
            self.assertIn("scripts/s14_lha_editions.py", got)
            self.assertIn("brma_lha_rates", got)
        # the untouched Code node, the SELECT, and the empty-query nodes are byte-equal
        for name in ("Extract Postcode", "Execute a SQL query", "Create Table", "Log Run"):
            self.assertEqual(node({"nodes": new}, name), node(w, name))

    def test_extras_replaced_by_a_raising_select(self):
        w = s14(branch_ok=False)
        i = [n["name"] for n in w["nodes"]].index("Create Table")
        w["nodes"][i] = pg("Create Table", "CREATE TABLE IF NOT EXISTS t (a int)")
        new, changed, _ = m.rewrite(w)
        self.assertIn("Create Table", changed)
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        q = node({"nodes": new}, "Create Table")["parameters"]["query"]
        self.assertTrue(q.startswith('SELECT 1/0 AS "RETIRED 2026-10-11'))
        self.assertIn("scripts/s14_lha_editions.py", q)
        self.assertEqual(m.plan(dict(w, nodes=new))[1], [])

    def test_second_run_is_noop_and_partial_state_completes(self):
        for w in (s3(), s14()):
            new, _, _ = m.rewrite(w)
            again, changed2, _ = m.rewrite(dict(w, nodes=new))
            self.assertEqual(changed2, [])
            self.assertEqual(again, new)
        # only one of the two S14 Code nodes retired so far
        w = s14()
        part = copy.deepcopy(w["nodes"])
        node({"nodes": part}, "Parse VOA Response")["parameters"]["jsCode"] = m.new_code(S14)
        new, changed, _ = m.rewrite(dict(w, nodes=part))
        self.assertEqual(changed, ["Process Rate Data"])

    def test_different_retirement_text_halts(self):
        with self.assertRaises(m.Halt):
            m.rewrite(s3(js='throw new Error("RETIRED 2026-10-11: other text");'))

    def test_original_not_mutated(self):
        w = s14()
        before = copy.deepcopy(w)
        m.rewrite(w)
        self.assertEqual(w, before)


class Table(unittest.TestCase):
    def test_workflows_as_briefed(self):
        self.assertEqual(set(m.WORKFLOWS), {S3, S14})
        self.assertEqual(m.WORKFLOWS[S3][:2], ("ONS Population Estimates (S3)", ("Process Population Data",)))
        self.assertEqual(m.WORKFLOWS[S14][:2], ("LHA Rates + List of Rents (S14)",
                                                ("Process Rate Data", "Parse VOA Response")))
        self.assertEqual(m.WORKFLOWS[S3][2], ("supplement",))
        self.assertEqual(set(m.WORKFLOWS[S14][2]), {"lha-direct", "la_lha_rates"})
        self.assertEqual(m.WORKFLOWS[S3][3], "s3_mye_editions.py")
        self.assertEqual(m.WORKFLOWS[S14][3], "s14_lha_editions.py")
        self.assertEqual(m.NO_NODES, {EMPTY: "VOA/DWP LHA Rates (S14)"})


class Retire(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_dry_run_writes_nothing(self):
        cur = FakeCursor(fixture())
        res = m.retire(cur, apply=False, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(cur.connection.commits, 0)
        self.assertEqual([r["id"] for r in res], [S3, S14])

    def test_apply_backs_up_updates_reads_back_and_is_idempotent(self):
        ws = fixture()
        before = copy.deepcopy(ws)
        cur = FakeCursor(ws)
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(sorted(cur.updates), sorted([S3, S14]))
        self.assertEqual(cur.connection.commits, 1)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^n8n_s3_s14_.*\.json$")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(set(data["workflows"]), {S3, S14})
        self.assertIn("supplementRaw", data["workflows"][S3]["nodes"][S3_CODE])
        self.assertEqual(set(data["workflows"][S14]["nodes"]), {"Process Rate Data", "Parse VOA Response"})
        # nothing activated, connections and the third workflow untouched
        for w, b in zip(cur.rows.values(), before):
            self.assertFalse(w["active"])
            self.assertEqual(w["connections"], b["connections"])
        self.assertEqual(cur.rows[EMPTY], before[2])
        cur.updates.clear()
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_halt_on_one_workflow_writes_nothing(self):
        ws = fixture()
        ws[1]["active"] = True
        cur = FakeCursor(ws)
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_missing_workflow_halts(self):
        with self.assertRaises(m.Halt):
            m.retire(FakeCursor(fixture()[1:]), apply=False, backup_dir=self.tmp)

    def test_empty_workflow_with_nodes_or_active_or_renamed_halts(self):
        for edit in (lambda w: w.update(nodes=[code("x", "1")]),
                     lambda w: w.update(active=True),
                     lambda w: w.update(name="Other")):
            ws = fixture()
            edit(ws[2])
            cur = FakeCursor(ws)
            with self.subTest():
                with self.assertRaises(m.Halt):
                    m.retire(cur, apply=True, backup_dir=self.tmp)
                self.assertEqual(cur.updates, [])

    def test_empty_workflow_is_reported_and_not_changed(self):
        cur = FakeCursor(fixture())
        res = m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual([r["id"] for r in res], [S3, S14])
        self.assertEqual(m.no_nodes_status(cur), [(EMPTY, "VOA/DWP LHA Rates (S14)", False, 0)])
        self.assertNotIn(EMPTY, cur.updates)

    def test_readback_mismatch_rolls_back(self):
        cur = FakeCursor(fixture(), corrupt_update=True)
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.connection.commits, 0)
        self.assertEqual(cur.connection.rollbacks, 1)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NodeHarness(unittest.TestCase):
    def test_every_new_code_throws_with_loader_name(self):
        for wid, spec in m.WORKFLOWS.items():
            with self.subTest(wid=wid):
                rc, out = m.node_test(m.new_code(wid), spec[3])
                self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
