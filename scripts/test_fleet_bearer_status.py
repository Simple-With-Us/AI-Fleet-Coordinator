import importlib.util, json, os, tempfile, types, unittest, urllib.error

spec = importlib.util.spec_from_file_location("fbs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet-bearer-status.py"))
fbs = importlib.util.module_from_spec(spec); spec.loader.exec_module(fbs)

DOC_OK = {"rows": [{"identity": "WISE", "kind": "hosted", "problem": "ok", "status": {"active": True}}], "diagnostics": []}
DOC_BAD = {"rows": [{"identity": "ZC", "kind": "bearer_only", "problem": "credentials_missing", "missingRecords": {"names": ["ZC_ZULIP_KEY"]}, "status": {}}],
           "diagnostics": [{"kind": "mcp", "reason": "mcp_http_401", "count": 5, "ts": 100}, {"kind": "gr", "reason": "relay_refused", "count": 2, "ts": 101}]}


class T(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.args = types.SimpleNamespace(write=os.path.join(d, "o.json"), state=os.path.join(d, "s.json"), url="u", no_notify=False, dry_run=False)
        self.sent = []

    def go(self, doc, now, token="a.b.c", exc=None):
        def fetch(url, tok):
            if exc: raise exc
            return doc
        return fbs.run(self.args, now=now, token_fn=lambda: token, fetch_fn=fetch, notify_fn=lambda t, x: self.sent.append((t, x)))

    def test_dedup_realarm_and_recovery(self):
        self.go(DOC_BAD, 1000)
        keys = [x for _, x in self.sent]
        self.assertTrue(any("ZC: credentials_missing (missing ZC_ZULIP_KEY)" in k for k in keys))
        self.assertTrue(any("401" in k for k in keys)); self.assertTrue(any("relay refusals" in k for k in keys))
        self.go({**DOC_BAD, "diagnostics": []}, 1300)  # same problem, no new growth
        alarms_before = len([1 for t, _ in self.sent if "alarm" in t])
        self.go({**DOC_BAD, "diagnostics": []}, 1400)
        self.assertEqual(len([1 for t, _ in self.sent if "alarm" in t]), alarms_before)  # de-duped
        self.go({**DOC_BAD, "diagnostics": []}, 1000 + 6 * 3600 + 5)
        self.assertEqual(len([1 for t, _ in self.sent if "alarm" in t]), alarms_before + 1)  # 6 h re-alarm
        self.go(DOC_OK, 1000 + 7 * 3600)
        self.assertTrue(any("recovered" in t and "problem:ZC" in x for t, x in self.sent))
        with open(self.args.write) as f:
            self.assertEqual(json.load(f)["alarms"], [])
        self.assertEqual(oct(os.stat(self.args.write).st_mode & 0o777), "0o600")

    def test_no_login_and_refused_login_alarm_and_keep_identity_alarms(self):
        out = self.go(None, 10, token=None)
        self.assertEqual(out["error"], "access_login_needed")
        self.go(DOC_BAD, 20)
        out = self.go(None, 30, exc=urllib.error.HTTPError("u", 403, "x", {}, None))
        keys = [a["key"] for a in out["alarms"]]
        self.assertIn("access:login_needed", keys); self.assertIn("problem:ZC:credentials_missing", keys)

    def test_output_has_no_secret_shapes(self):
        out = self.go(DOC_BAD, 5)
        s = json.dumps(out)
        self.assertNotIn("sbt_", s); self.assertNotIn("token", s.lower().replace("tokens", ""))


if __name__ == "__main__":
    unittest.main()
