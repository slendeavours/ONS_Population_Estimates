"""statxplore_client with patched requests/time: no network, no real key."""
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests  # noqa: E402
import statxplore_client as c  # noqa: E402

KEY = "test-key"


def resp(status=200, text="{}", headers=None):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    r.json.return_value = __import__("json").loads(text)
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    return r


class ClientTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"StatXplore_API_Key": KEY})
        env.start()
        self.addCleanup(env.stop)
        sl = mock.patch("time.sleep")
        self.sleep = sl.start()
        self.addCleanup(sl.stop)
        c._last_api_call = 0.0

    def test_missing_key_is_a_hard_stop(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                c.get_api_key()
        self.assertIn("StatXplore_API_Key", str(cm.exception))

    def test_503_is_retried_then_succeeds(self):
        with mock.patch("requests.get",
                        side_effect=[resp(503), resp(200, '{"a": 1}')]):
            data, _ = c.api_get("/schema")
        self.assertEqual(data, {"a": 1})
        self.sleep.assert_any_call(30)

    def test_gives_up_after_retries(self):
        with mock.patch("requests.get", side_effect=[resp(503)] * 3):
            with self.assertRaises(requests.HTTPError):
                c.api_get("/schema")
        self.assertEqual([x.args[0] for x in self.sleep.call_args_list
                          if x.args[0] >= 30], [30, 60])

    def test_paging_follows_link_header(self):
        p1 = resp(200, '{"children": [{"id": 1}]}',
                  {"Link": '<https://x/next>; rel="next"'})
        p2 = resp(200, '{"children": [{"id": 2}]}')
        with mock.patch("requests.get", side_effect=[p1, p2]) as g:
            out = c.api_get_all_pages("/schema/x")
        self.assertEqual(out, [{"id": 1}, {"id": 2}])
        self.assertEqual(g.call_args_list[1].args[0], "https://x/next")

    def test_throttle_spaces_calls(self):
        c._last_api_call = 100.0
        with mock.patch("time.time", return_value=100.25):
            c.throttle()
        self.sleep.assert_called_once()
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.75)

    def test_post_retries_and_returns_json(self):
        with mock.patch("requests.post",
                        side_effect=[resp(502), resp(200, '{"ok": 1}')]) as p:
            self.assertEqual(c.api_post("/table", {"q": 1}), {"ok": 1})
        self.assertEqual(p.call_args.kwargs["timeout"], 120)
        self.sleep.assert_any_call(30)

    def test_key_never_in_exception_text(self):
        with mock.patch("requests.get",
                        side_effect=requests.ConnectionError("down")):
            with self.assertRaises(requests.RequestException) as cm:
                c.api_get("/schema")
        self.assertNotIn(KEY, str(cm.exception))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                c.get_api_key()
        self.assertNotIn(KEY, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
