"""Retirement of the n8n RO4 (S2) loader workflow.

Fixture workflow JSON only: no database, no n8n. A fake cursor stands in for
the n8ndb connection. The fixture has the shape of the real workflow."""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import n8n_retire_ro4 as m  # noqa: E402

TRIGGER = "When clicking Execute workflow"
OLD_JS = "const x = parseFloat(cols[4]) || 0; return [];"


def pg(name, query):
    return {"name": name, "type": "n8n-nodes-base.postgres",
            "parameters": {"operation": "executeQuery", "query": query},
            "credentials": {"postgres": {"id": "x", "name": "n"}}}


def to(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0} for n in names]]}


def ro4(js=OLD_JS, offgraph=False):
    nodes = [
        {"name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
        {"name": "Fetch RO4 CSV", "type": "n8n-nodes-base.httpRequest",
         "parameters": {"url": "https://raw.example/ro4.csv"}},
        {"name": m.CODE_NODE, "type": "n8n-nodes-base.code", "parameters": {"jsCode": js}},
        pg("Create Table If Not Exists", "CREATE TABLE IF NOT EXISTS ro4_housing_expenditure (a int)"),
        pg("Upsert RO4 Data", "INSERT INTO ro4_housing_expenditure SELECT 1"),
        pg("Log Run", "INSERT INTO pipeline_run_log VALUES (1)"),
    ]
    conns = {TRIGGER: to("Fetch RO4 CSV"), "Fetch RO4 CSV": to(m.CODE_NODE),
             m.CODE_NODE: to("Create Table If Not Exists"),
             "Create Table If Not Exists": to("Upsert RO4 Data"),
             "Upsert RO4 Data": to("Log Run")}
    if offgraph:
        del conns["Create Table If Not Exists"]
    return {"id": m.WID, "name": m.NAME, "active": False, "nodes": nodes, "connections": conns}


def node(nodes, name):
    return [n for n in nodes if n["name"] == name][0]


class FakeConn:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeCursor:
    def __init__(self, wf, corrupt_update=False):
        self.wf = wf
        self.connection = FakeConn()
        self.updates = 0
        self.corrupt = corrupt_update
        self._res = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).upper()
        if s.startswith("SELECT"):
            w = self.wf if params[0] == self.wf["id"] else None
            self._res = [] if w is None else [
                (w["name"], w["active"], copy.deepcopy(w["nodes"]), copy.deepcopy(w["connections"]))]
        elif s.startswith("UPDATE"):
            nodes = json.loads(params[0])
            if self.corrupt:
                nodes = nodes[:-1]
            self.wf["nodes"] = nodes
            self.updates += 1
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._res


class Plan(unittest.TestCase):
    def test_as_surveyed_has_no_extras(self):
        self.assertEqual(m.plan(ro4()), [])

    def test_every_write_node_is_downstream_of_the_code_node(self):
        w = ro4()
        down = m.downstream(w["connections"], m.CODE_NODE)
        self.assertEqual(sorted(m.write_nodes(w["nodes"])), sorted(down & set(m.write_nodes(w["nodes"]))))
        self.assertEqual(len(m.write_nodes(w["nodes"])), 3)

    def test_off_graph_write_is_an_extra(self):
        self.assertEqual(m.plan(ro4(offgraph=True)), ["Upsert RO4 Data", "Log Run"])

    def test_code_node_missing_or_duplicated_halts(self):
        w = ro4()
        node(w["nodes"], m.CODE_NODE)["name"] = "Other"
        with self.assertRaises(m.Halt):
            m.plan(w)
        w = ro4()
        w["nodes"].append(copy.deepcopy(node(w["nodes"], m.CODE_NODE)))
        with self.assertRaises(m.Halt):
            m.plan(w)


class Rewrite(unittest.TestCase):
    def test_only_the_code_value_changes(self):
        w = ro4()
        new, changed, old = m.rewrite(w)
        self.assertEqual(changed, [m.CODE_NODE])
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        self.assertEqual(sorted(a["name"] for a, b in zip(w["nodes"], new) if a != b), changed)
        got = node(new, m.CODE_NODE)["parameters"]["jsCode"]
        self.assertTrue(got.startswith('throw new Error("RETIRED 2026-10-12'))
        self.assertIn("ro4_housing_expenditure", got)
        self.assertIn("scripts/ro4_editions.py", got)
        self.assertIn("preview by default", got)
        self.assertEqual(old[m.CODE_NODE], OLD_JS)

    def test_extras_replaced_by_a_raising_select(self):
        w = ro4(offgraph=True)
        new, changed, _ = m.rewrite(w)
        self.assertEqual(sorted(changed), sorted([m.CODE_NODE, "Upsert RO4 Data", "Log Run"]))
        self.assertEqual(m.blanked(new, changed), m.blanked(w["nodes"], changed))
        q = node(new, "Upsert RO4 Data")["parameters"]["query"]
        self.assertTrue(q.startswith('SELECT 1/0 AS "RETIRED 2026-10-12'))
        self.assertIn("scripts/ro4_editions.py", q)
        self.assertEqual(m.plan(dict(w, nodes=new)), [])

    def test_marker_missing_halts(self):
        with self.assertRaises(m.Halt):
            m.rewrite(ro4(js="return [];"))

    def test_active_or_wrong_name_or_id_halts(self):
        for edit in (lambda w: w.update(active=True), lambda w: w.update(name="Other"),
                     lambda w: w.update(id="other")):
            w = ro4()
            edit(w)
            with self.assertRaises(m.Halt):
                m.rewrite(w)

    def test_second_run_is_noop(self):
        w = ro4()
        new, _, _ = m.rewrite(w)
        again, changed2, _ = m.rewrite(dict(w, nodes=new))
        self.assertEqual(changed2, [])
        self.assertEqual(again, new)

    def test_different_retirement_text_halts(self):
        with self.assertRaises(m.Halt):
            m.rewrite(ro4(js='throw new Error("RETIRED 2026-10-12: other text");'))

    def test_original_not_mutated(self):
        w = ro4()
        before = copy.deepcopy(w)
        m.rewrite(w)
        self.assertEqual(w, before)


class Retire(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_dry_run_writes_nothing(self):
        cur = FakeCursor(ro4())
        m.retire(cur, apply=False, backup_dir=self.tmp)
        self.assertEqual(cur.updates, 0)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(cur.connection.commits, 0)

    def test_apply_backs_up_updates_reads_back_and_is_idempotent(self):
        w = ro4()
        before = copy.deepcopy(w)
        cur = FakeCursor(w)
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, 1)
        self.assertEqual(cur.connection.commits, 1)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^n8n_ro4_.*\.json$")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["workflows"][m.WID]["nodes"], {m.CODE_NODE: OLD_JS})
        self.assertFalse(w["active"])
        self.assertEqual(w["connections"], before["connections"])
        m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, 1)
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_halt_writes_nothing(self):
        w = ro4()
        w["active"] = True
        cur = FakeCursor(w)
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.updates, 0)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_missing_workflow_halts(self):
        w = ro4()
        cur = FakeCursor(dict(w, id="zz"))
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=False, backup_dir=self.tmp)

    def test_readback_mismatch_rolls_back(self):
        cur = FakeCursor(ro4(), corrupt_update=True)
        with self.assertRaises(m.Halt):
            m.retire(cur, apply=True, backup_dir=self.tmp)
        self.assertEqual(cur.connection.commits, 0)
        self.assertEqual(cur.connection.rollbacks, 1)


class Table(unittest.TestCase):
    def test_as_briefed(self):
        self.assertEqual(m.WID, "L55sz2PN4W9V0E0I")
        self.assertEqual(m.NAME, "Revenue Outturn RO4 (S2)")
        self.assertEqual(m.CODE_NODE, "Process RO4 Data")
        self.assertEqual(m.LOADER, "ro4_editions.py")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NodeHarness(unittest.TestCase):
    def test_new_code_throws_with_loader_name(self):
        rc, out = m.node_test(m.new_code(), m.LOADER)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
