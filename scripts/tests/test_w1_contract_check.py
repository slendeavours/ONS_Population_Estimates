"""W1 contract check reads the repo's LA Signals SQL, not n8n's database.

Pure text; no database is touched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import w1_contract_check  # noqa: E402
import w1_steps  # noqa: E402


class ContractCheckRepoSqlTest(unittest.TestCase):
    def test_node5_sql_is_the_repo_file(self):
        step = next(s for s in w1_steps.STEPS if s.name == "LA Signals")
        self.assertEqual(w1_contract_check.node5_sql(),
                         w1_steps.read_step(step))

    def test_parse_node5_finds_all_columns_of_the_repo_sql(self):
        insert_cols, select_items, set_cols = w1_contract_check.parse_node5(
            w1_contract_check.node5_sql())
        self.assertTrue(insert_cols)
        self.assertIn("lad24cd", insert_cols)
        self.assertIn("run_id", insert_cols)
        self.assertEqual(len(insert_cols), len(select_items))

    def test_check_has_no_n8n_dependency(self):
        src = Path(w1_contract_check.__file__).read_text(encoding="utf-8")
        self.assertNotIn("n8n_conn", src)
        self.assertNotIn("workflow_entity", src)


if __name__ == "__main__":
    unittest.main()
