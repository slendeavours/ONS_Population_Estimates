"""Retirement of the seven n8n loader workflows for the no-loader sources.

Fixture workflow JSON only: no database, no n8n. A fake cursor stands in for
the n8ndb connection."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import n8n_retire_no_loader_sources as m  # noqa: E402

TRIGGER = "When clicking Execute workflow"


def pg(name, query):
    return {"name": name, "type": "n8n-nodes-base.postgres",
            "parameters": {"operation": "executeQuery", "query": query},
            "credentials": {"postgres": {"id": "x", "name": "n"}}}


def code(name, js):
    return {"name": name, "type": "n8n-nodes-base.code",
            "parameters": {"jsCode": js}}


def to(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0} for n in names]]}


def simple(name="Rough Sleeping Snapshot (S10)", wid="yeIcdi9WM8QoFVpC",
           js="return [{json:{a: parseInt(x) || 0}}];", node="Process Rough Sleeping Data"):
    nodes = [
        {"name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {"url": "u"}},
        code(node, js),
        pg("Create Table", "CREATE TABLE IF NOT EXISTS t (a int)"),
        pg("Upsert", "INSERT INTO t SELECT 1"),
        pg("Log Run", "SELECT 1"),
    ]
    conns = {TRIGGER: to("Fetch"), "Fetch": to(node), node: to("Create Table"),
             "Create Table": to("Upsert"), "Upsert": to("Log Run")}
    return {"id": wid, "name": name, "active": False, "nodes": nodes, "connections": conns}


def s7(off_trigger):
    nodes = [
        {"name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"name": "Fetch LA Boundaries", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
        code("Process Boundaries", "const features = $input.first().json.features; return [];"),
        pg("Upsert LA Boundaries", "INSERT INTO la_boundaries SELECT 1"),
        pg("Log Run", "SELECT agent_name FROM pipeline_run_log LIMIT 1;"),
        pg("Seed Code Lookup from Boundaries", "INSERT INTO la_code_lookup SELECT 1"),
        pg("Delete Historical Code Changes", "DELETE FROM la_code_lookup WHERE change_type != 'current';"),
        pg("Insert Historical Code Changes", "INSERT INTO la_code_lookup VALUES (1)"),
    ]
    conns = {TRIGGER: to("Fetch LA Boundaries"),
             "Fetch LA Boundaries": to("Process Boundaries"),
             "Process Boundaries": to("Upsert LA Boundaries"),
             "Upsert LA Boundaries": to("Log Run"),
             "Seed Code Lookup from Boundaries": to("Delete Historical Code Changes"),
             "Delete Historical Code Changes": to("Insert Historical Code Changes")}
    if off_trigger:
        conns[TRIGGER] = to("Fetch LA Boundaries", "Seed Code Lookup from Boundaries")
    else:
        conns["Log Run"] = to("Seed Code Lookup from Boundaries")
    return {"id": "moZB3CYE96j4OcFp", "name": "LA Boundaries (S7)", "active": False,
            "nodes": nodes, "connections": conns}


class FakeConn:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    """n8n workflow rows by id: name, active, nodes, connections."""

    def __init__(self, workflows):
        self.rows = {w["id"]: w for w in workflows}
        self.connection = FakeConn()
        self.updates = []
        self._res = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT"):
            w = self.rows.get(params[0])
            self._res = [] if w is None else [
                (w["name"], w["active"], copy.deepcopy(w["nodes"]), copy.deepcopy(w["connections"]))]
        elif s.startswith("UPDATE"):
            nodes_json, wid = params
            self.rows[wid]["nodes"] = json.loads(nodes_json)
            self.updates.append(wid)
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._res


def seven_fixture():
    ws = []
    for wid, (name, node, marker, loader) in m.WORKFLOWS.items():
        ws.append(s7(False) if wid == "moZB3CYE96j4OcFp"
                  else simple(name=name, wid=wid, js="const x = 1; // " + marker, node=node))
    return ws


class Downstream(unittest.TestCase):
    def test_branching_graph(self):
        conns = {"a": {"main": [[{"node": "b"}, {"node": "c"}], [{"node": "d"}]]},
                 "b": {"main": [[{"node": "e"}]]},
                 "c": {"main": [[{"node": "e"}]]},
                 "d": {"main": [[]]},
                 "x": {"main": [[{"node": "a"}]]}}
        self.assertEqual(m.downstream(conns, "a"), {"b", "c", "d", "e"})
        self.assertEqual(m.downstream(conns, "e"), set())
        self.assertEqual(m.downstream(conns, "x"), {"a", "b", "c", "d", "e"})

    def test_cycle_terminates(self):
        conns = {"a": to("b"), "b": to("a")}
        self.assertEqual(m.downstream(conns, "a"), {"a", "b"})


class WriteNodes(unittest.TestCase):
    def test_writes_found_selects_ignored(self):
        nodes = [pg("i", "INSERT INTO t VALUES (1)"), pg("u", "update t set a=1"),
                 pg("d", "DELETE FROM t"), pg("c", "CREATE TABLE IF NOT EXISTS t (a int)"),
                 pg("p", "DROP TABLE t"), pg("t", "TRUNCATE t"),
                 pg("s", "SELECT updated_at, created FROM t"),
                 code("code", "INSERT")]
        self.assertEqual(sorted(m.write_nodes(nodes)), ["c", "d", "i", "p", "t", "u"])

    def test_postgres_node_without_query_halts(self):
        n = pg("x", "SELECT 1")
        del n["parameters"]["query"]
        with self.assertRaises(m.Halt):
            m.write_nodes([n])


class Plan(unittest.TestCase):
    def test_all_downstream_no_extras(self):
        self.assertEqual(m.plan(simple()), ("Process Rough Sleeping Data", []))

    def test_s7_as_surveyed_has_no_extras(self):
        self.assertEqual(m.plan(s7(False)), ("Process Boundaries", []))

    def test_write_off_trigger_is_extra(self):
        code_node, extras = m.plan(s7(True))
        self.assertEqual(code_node, "Process Boundaries")
        self.assertEqual(sorted(extras), ["Delete Historical Code Changes",
                                          "Insert Historical Code Changes",
                                          "Seed Code Lookup from Boundaries"])

    def test_s7_lookup_node_missing_halts(self):
        w = s7(False)
        w["nodes"] = [n for n in w["nodes"] if n["name"] != "Delete Historical Code Changes"]
        with self.assertRaises(m.Halt):
            m.plan(w)

    def test_code_node_missing_or_duplicated_halts(self):
        w = simple()
        w["nodes"][2]["name"] = "Other"
        with self.assertRaises(m.Halt):
            m.plan(w)
        w = simple()
        w["nodes"].append(copy.deepcopy(w["nodes"][2]))
        with self.assertRaises(m.Halt):
            m.plan(w)


class Rewrite(unittest.TestCase):
    def test_marker_missing_halts(self):
        with self.assertRaises(m.Halt):
            m.rewrite(simple(js="return [{json:{a: parseInt(x)}}];"))

    def test_active_or_wrong_name_halts(self):
        w = simple()
        w["active"] = True
        with self.assertRaises(m.Halt):
            m.rewrite(w)
        with self.assertRaises(m.Halt):
            m.rewrite(simple(name="Something else"))

    def test_only_code_value_changes(self):
        w = simple()
        new, changed, _ = m.rewrite(w)
        self.assertEqual(changed, ["Process Rough Sleeping Data"])
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        self.assertNotEqual(new, w["nodes"])
        got = [n for n in new if n["name"] == changed[0]][0]["parameters"]["jsCode"]
        self.assertTrue(got.startswith('throw new Error("RETIRED 2026-10-'))
        self.assertIn("la_rough_sleeping", got)
        self.assertIn("scripts/s10_rough_sleeping_editions.py", got)
        self.assertIn("preview by default", got)

    def test_extras_replaced_and_only_their_queries(self):
        w = s7(True)
        new, changed, _ = m.rewrite(w)
        self.assertEqual(sorted(changed), sorted([
            "Process Boundaries", "Seed Code Lookup from Boundaries",
            "Delete Historical Code Changes", "Insert Historical Code Changes"]))
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        q = [n for n in new if n["name"] == "Delete Historical Code Changes"][0]["parameters"]["query"]
        self.assertTrue(q.startswith('SELECT 1/0 AS "RETIRED 2026-10-'))
        self.assertIn("scripts/s7_boundaries_editions.py", q)
        # after retirement no node of the workflow can write
        self.assertEqual(m.plan(dict(w, nodes=new))[1], [])

    def test_second_run_is_noop(self):
        for w in (simple(), s7(True)):
            new, _, _ = m.rewrite(w)
            again, changed2, _ = m.rewrite(dict(w, nodes=new))
            self.assertEqual(changed2, [])
            self.assertEqual(again, new)

    def test_different_retirement_text_halts(self):
        with self.assertRaises(m.Halt):
            m.rewrite(simple(js='throw new Error("RETIRED 2026-10-10: other text");'))

    def test_original_not_mutated(self):
        w = simple()
        before = copy.deepcopy(w)
        m.rewrite(w)
        self.assertEqual(w, before)


class Table(unittest.TestCase):
    def test_seven_workflows_as_briefed(self):
        self.assertEqual(set(m.WORKFLOWS), {
            "9QtgP8Gl3JPri6Um", "zCMCwdF24AZgkyna", "moZB3CYE96j4OcFp",
            "yeIcdi9WM8QoFVpC", "gbgnThonagiTgvaR", "pQdnfy7IpJXdxzIM",
            "OYMTEK8A9j8bdpkf"})
        self.assertEqual(m.WORKFLOWS["gbgnThonagiTgvaR"][1:3], ("Process EFS + S114", "NAME_TO_LAD24CD"))
        self.assertEqual(m.WORKFLOWS["OYMTEK8A9j8bdpkf"][2], "parseFloat")
        self.assertEqual(m.WORKFLOWS["yeIcdi9WM8QoFVpC"][2], "|| 0")
        self.assertEqual(m.WORKFLOWS["moZB3CYE96j4OcFp"][2], "features")
        self.assertEqual({v[3] for v in m.WORKFLOWS.values()}, {
            "s3b_tenure_editions.py", "s5_imd_editions.py", "s7_boundaries_editions.py",
            "s10_rough_sleeping_editions.py", "s12_financial_stress_editions.py",
            "s13_lahs_editions.py", "s17_marac_editions.py"})


class Retire(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_dry_run_writes_nothing(self):
        cur = FakeCursor(seven_fixture())
        res = m.retire(cur, apply=False, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(cur.connection.commits, 0)
        self.assertEqual(len(res), 7)

    def test_apply_backs_up_updates_and_is_idempotent(self):
        cur = FakeCursor(seven_fixture())
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(sorted(cur.updates), sorted(m.WORKFLOWS))
        self.assertEqual(cur.connection.commits, 1)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^n8n_no_loader_sources_.*\.json$")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(set(data["workflows"]), set(m.WORKFLOWS))
        for wid, (name, node, marker, loader) in m.WORKFLOWS.items():
            self.assertIn(marker, data["workflows"][wid]["nodes"][node])
        cur.updates.clear()
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_halt_on_one_workflow_writes_nothing(self):
        ws = seven_fixture()
        ws[3]["active"] = True
        cur = FakeCursor(ws)
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, [])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_missing_workflow_halts(self):
        with self.assertRaises(m.Halt):
            m.retire(FakeCursor(seven_fixture()[:-1]), apply=False, backup_dir=self.tmp)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NodeHarness(unittest.TestCase):
    def test_every_new_code_throws_with_loader_name(self):
        for wid, (name, node, marker, loader) in m.WORKFLOWS.items():
            with self.subTest(wid=wid):
                rc, out = m.node_test(m.new_code(wid), loader)
                self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
