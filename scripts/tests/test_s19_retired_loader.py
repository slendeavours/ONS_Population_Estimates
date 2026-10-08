"""The retired S19 loader (scripts/historical/s19_pip_build.py) stops at once."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "historical"))
import psycopg2  # noqa: E402
import requests  # noqa: E402
import s19_pip_build  # noqa: E402


class RetiredLoader(unittest.TestCase):
    def test_main_exits_retired_without_database_or_network(self):
        boom = AssertionError("touched the database or network")
        with mock.patch.object(psycopg2, "connect", side_effect=boom),                 mock.patch.object(requests, "get", side_effect=boom),                 mock.patch.object(requests, "post", side_effect=boom),                 mock.patch.object(requests, "Session", side_effect=boom):
            with self.assertRaises(SystemExit) as ctx:
                s19_pip_build.main()
        self.assertIn("RETIRED", str(ctx.exception.code))


if __name__ == "__main__":
    unittest.main()
