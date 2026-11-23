import importlib.util
import json
import os
import tempfile
import types
import unittest
import urllib.error

spec = importlib.util.spec_from_file_location(
    "fbs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-bearer-status.py")
)
fbs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fbs)

DOC_OK = {
    "rows": [{"identity": "WISE", "kind": "hosted", "problem": "ok", "status": {"active": True}}],
    "diagnostics": [],
}
DOC_BAD = {
    "rows": [
        {
            "identity": "ZC",
            "kind": "bearer_only",
            "problem": "credentials_missing",
            "missingRecords": {"names": ["ZC_ZULIP_KEY"]},
            "status": {},
        }
    ],
    "diagnostics": [
        {"kind": "mcp", "reason": "mcp_http_401", "count": 5, "ts": 100},
        {"kind": "gr", "reason": "relay_refused", "count": 2, "ts": 101},
    ],
}

DIAG0 = [
    {"kind": "mcp", "reason": "mcp_http_401", "count": 288, "ts": 1},
    {"kind": "gr_assert", "reason": "x_refused", "count": 9, "ts": 1},
]


class T(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.args = types.SimpleNamespace(
            write=os.path.join(d, "o.json"),
            state=os.path.join(d, "s.json"),
            url="u",
            no_notify=False,
            dry_run=False,
        )
        self.sent = []

    def go(self, doc, now, token="a.b.c", exc=None):
        def fetch(url, tok):
            if exc:
                raise exc
            return doc

        return fbs.run(
            self.args,
            now=now,
            token_fn=lambda: token,
            fetch_fn=fetch,
            notify_fn=lambda t, x: self.sent.append((t, x)),
        )

    def test_dedup_realarm_and_recovery(self):
        self.go(DOC_BAD, 1000)
        keys = [x for _, x in self.sent]
        self.assertTrue(any("ZC: credentials_missing (missing ZC_ZULIP_KEY)" in k for k in keys))
        self.assertFalse(
            any("401" in k for k in keys)
        )  # first poll: diagnostics only set the baseline
        self.go({**DOC_BAD, "diagnostics": []}, 1300)  # same problem, no new growth
        alarms_before = len([1 for t, _ in self.sent if "alarm" in t])
        self.go({**DOC_BAD, "diagnostics": []}, 1400)
        self.assertEqual(len([1 for t, _ in self.sent if "alarm" in t]), alarms_before)  # de-duped
        self.go({**DOC_BAD, "diagnostics": []}, 1000 + 6 * 3600 + 5)
        self.assertEqual(
            len([1 for t, _ in self.sent if "alarm" in t]), alarms_before + 1
        )  # 6 h re-alarm
        self.go(DOC_OK, 1000 + 7 * 3600)
        self.assertTrue(any("recovered" in t and "problem:ZC" in x for t, x in self.sent))
        with open(self.args.write) as f:
            self.assertEqual(json.load(f)["alarms"], [])
        self.assertEqual(oct(os.stat(self.args.write).st_mode & 0o777), "0o600")

    def test_first_poll_baselines_and_only_growth_alarms(self):
        self.go({"rows": [], "diagnostics": DIAG0}, 10)
        self.assertEqual(self.sent, [])  # lifetime counts are history, not an alarm
        self.go({"rows": [], "diagnostics": DIAG0}, 400)
        self.assertEqual(self.sent, [])
        grown = [{**DIAG0[0], "count": 291}, {**DIAG0[1], "count": 12}]
        out = self.go({"rows": [], "diagnostics": grown}, 800)
        keys = sorted(a["key"] for a in out["alarms"])
        self.assertEqual(keys, ["delta:mcp_http_401", "delta:relay_refusal"])
        self.assertTrue(any("3 new /mcp 401" in x for _, x in self.sent))
        self.assertTrue(any("3 new relay refusals" in x for _, x in self.sent))
        out = self.go({"rows": [], "diagnostics": grown}, 1200)  # no further growth: recovers
        self.assertEqual(out["alarms"], [])
        reset = [{**DIAG0[0], "count": 2}]
        out = self.go(
            {"rows": [], "diagnostics": reset}, 1600
        )  # evicted row began again: counts in full
        self.assertTrue(any("2 new /mcp 401" in x for _, x in self.sent))

    def test_no_login_and_refused_login_alarm_and_keep_identity_alarms(self):
        out = self.go(None, 10, token=None)
        self.assertEqual(out["error"], "access_login_needed")
        self.go(DOC_BAD, 20)
        out = self.go(None, 30, exc=urllib.error.HTTPError("u", 403, "x", {}, None))
        keys = [a["key"] for a in out["alarms"]]
        self.assertIn("access:login_needed", keys)
        self.assertIn("problem:ZC:credentials_missing", keys)

    def test_output_has_no_secret_shapes(self):
        out = self.go(DOC_BAD, 5)
        s = json.dumps(out)
        self.assertNotIn("sbt_", s)
        self.assertNotIn("token", s.lower().replace("tokens", ""))


class ReviewFixTests(unittest.TestCase):
    def test_delta_edge_keys_clear_silently(self):
        state = {
            "active": {
                "delta:mcp:mcp_http_401": {"since": 1, "lastAlarm": 1},
                "access:login_needed": {"since": 1, "lastAlarm": 1},
            }
        }
        notify, recovered = fbs.reconcile(state, {}, 100)
        self.assertEqual(notify, [])
        self.assertEqual(recovered, ["access:login_needed"])
        self.assertEqual(state["active"], {})

    def test_access_token_falls_through_to_next_binary(self):
        good = "a.b.c"
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[0])
            if cmd[0] == "/bad":
                raise OSError("broken")
            return types.SimpleNamespace(returncode=0, stdout=good + "\n")

        old = (fbs.CLOUDFLARED, fbs.subprocess.run, fbs.os.path.exists)
        try:
            fbs.CLOUDFLARED = ["/bad", "/good"]
            fbs.subprocess.run = fake_run
            fbs.os.path.exists = lambda p: True
            self.assertEqual(fbs.access_token(), good)
        finally:
            fbs.CLOUDFLARED, fbs.subprocess.run, fbs.os.path.exists = old
        self.assertEqual(calls, ["/bad", "/good"])

    def test_write_atomic_leaves_no_tmp_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TypeError):
                fbs.write_atomic(os.path.join(d, "s.json"), {"x": object()})
            self.assertEqual(os.listdir(d), [])
            fbs.write_atomic(os.path.join(d, "s.json"), {"x": 1})
            self.assertEqual(os.listdir(d), ["s.json"])


class ReviewRound2Tests(unittest.TestCase):
    def _args(self, d):
        return types.SimpleNamespace(
            state=os.path.join(d, "state.json"),
            write=os.path.join(d, "out.json"),
            url="https://x.invalid/admin/api/bearers",
            dry_run=False,
            no_notify=True,
        )

    def test_a_non_dict_state_file_resets(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            with open(a.state, "w") as f:
                f.write("[1, 2]")
            out = fbs.run(a, now=1000, token_fn=lambda: "a.b.c", fetch_fn=lambda u, t: DOC_OK, notify_fn=lambda *x: None)
            self.assertTrue(out["ok"])

    def test_a_network_failure_keeps_the_login_alarm(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            fbs.run(a, now=1000, token_fn=lambda: None, fetch_fn=None, notify_fn=lambda *x: None)

            def boom(u, t):
                raise OSError("down")

            out = fbs.run(a, now=1300, token_fn=lambda: "a.b.c", fetch_fn=boom, notify_fn=lambda *x: None)
            keys = [x["key"] for x in out["alarms"]]
            self.assertIn("access:login_needed", keys)
            self.assertEqual(out["recovered"], [])

    def test_diag_baseline_survives_a_poll_without_the_row(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            full = {"rows": [], "diagnostics": [{"kind": "mcp", "reason": "mcp_http_401", "count": 50, "ts": 1}]}
            empty = {"rows": [], "diagnostics": []}
            docs = iter([full, empty, full])
            for t in (1000, 1300, 1600):
                out = fbs.run(a, now=t, token_fn=lambda: "a.b.c", fetch_fn=lambda u, tk: next(docs), notify_fn=lambda *x: None)
            self.assertEqual(out["alarms"], [])

    def test_a_redirect_is_not_followed(self):
        handler = fbs._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.invalid/"))

if __name__ == "__main__":
    unittest.main()
