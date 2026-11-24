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
# Production lists no open finding now;  these tests need one, so they pin their own.
fbs.OPEN_FINDINGS = frozenset({("WISE", "wake")})

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


def _row(ident, kind, problem, count=None):
    r = {"identity": ident, "kind": kind, "problem": problem, "status": {}}
    if count is not None:
        r["mismatches"] = {"count": count}
    return r


class OAuthSeatTests(unittest.TestCase):
    def test_a_hosted_seat_without_a_bearer_is_not_an_alarm_but_a_bearer_only_one_is(self):
        doc = {
            "rows": [
                _row("GROK-WEB", "hosted", "no_bearer"),
                _row("BF-FIXER", "bearer_only", "no_bearer"),
            ],
            "diagnostics": [],
        }
        alarms, _ = fbs.desired_alarms(doc, None)
        self.assertEqual(sorted(alarms), ["problem:BF-FIXER:no_bearer"])

    def test_wrong_secret_alarms_on_growth_only(self):
        doc = {"rows": [_row("ECHO", "hosted", "wrong_secret_attempts", 4)], "diagnostics": []}
        alarms, counts = fbs.desired_alarms(doc, None)
        self.assertEqual(alarms, {})
        self.assertEqual(counts, {"mismatch|ECHO": 4})
        alarms, _ = fbs.desired_alarms(doc, {"mismatch|ECHO": 4})
        self.assertEqual(alarms, {})
        alarms, _ = fbs.desired_alarms(doc, {})
        self.assertEqual(alarms, {})
        doc["rows"][0]["mismatches"]["count"] = 6
        alarms, _ = fbs.desired_alarms(doc, {"mismatch|ECHO": 4})
        self.assertEqual(list(alarms), ["delta:wrong_secret:ECHO"])
        self.assertIn("2 new", alarms["delta:wrong_secret:ECHO"])


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
            out = fbs.run(
                a,
                now=1000,
                token_fn=lambda: "a.b.c",
                fetch_fn=lambda u, t: DOC_OK,
                notify_fn=lambda *x: None,
            )
            self.assertTrue(out["ok"])

    def test_a_network_failure_keeps_the_login_alarm(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            fbs.run(a, now=1000, token_fn=lambda: None, fetch_fn=None, notify_fn=lambda *x: None)

            def boom(u, t):
                raise OSError("down")

            out = fbs.run(
                a, now=1300, token_fn=lambda: "a.b.c", fetch_fn=boom, notify_fn=lambda *x: None
            )
            keys = [x["key"] for x in out["alarms"]]
            self.assertIn("access:login_needed", keys)
            self.assertEqual(out["recovered"], [])

    def test_diag_baseline_survives_a_poll_without_the_row(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            full = {
                "rows": [],
                "diagnostics": [{"kind": "mcp", "reason": "mcp_http_401", "count": 50, "ts": 1}],
            }
            empty = {"rows": [], "diagnostics": []}
            docs = iter([full, empty, full])
            for t in (1000, 1300, 1600):
                out = fbs.run(
                    a,
                    now=t,
                    token_fn=lambda: "a.b.c",
                    fetch_fn=lambda u, tk: next(docs),
                    notify_fn=lambda *x: None,
                )
            self.assertEqual(out["alarms"], [])

    def test_a_non_dict_active_state_resets(self):
        state = {"active": ["x"]}
        notify, recovered = fbs.reconcile(state, {"k": "v"}, 100)
        self.assertEqual(notify, [("k", "v")])
        self.assertEqual(list(state["active"]), ["k"])

    def test_a_redirect_is_not_followed(self):
        handler = fbs._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://other.invalid/")
        )


class ReviewRound3Tests(ReviewRound2Tests):
    def test_an_unusable_wrong_secret_count_still_alarms(self):
        for bad in (None, "x", -1, True):
            r = _row("ECHO", "hosted", "wrong_secret_attempts")
            if bad is not None:
                r["mismatches"] = {"count": bad}
            alarms, _ = fbs.desired_alarms({"rows": [r], "diagnostics": []}, {})
            self.assertEqual(list(alarms), ["problem:ECHO:wrong_secret_attempts"], bad)

    def test_mismatch_baseline_is_kept_when_diagnostics_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._args(d)
            docs = iter(
                [
                    {"rows": [_row("ECHO", "hosted", "wrong_secret_attempts", 4)]},
                    {"rows": [_row("ECHO", "hosted", "wrong_secret_attempts", 4)]},
                    {"rows": [_row("ECHO", "hosted", "wrong_secret_attempts", 6)]},
                ]
            )
            outs = [
                fbs.run(
                    a,
                    now=t,
                    token_fn=lambda: "a.b.c",
                    fetch_fn=lambda u, tk: next(docs),
                    notify_fn=lambda *x: None,
                )
                for t in (1000, 1300, 1600)
            ]
            self.assertEqual(outs[1]["alarms"], [])
            self.assertEqual(len(outs[2]["alarms"]), 1)


class ServiceTokenFileTests(unittest.TestCase):
    def _write(self, d, mode, doc):
        p = os.path.join(d, "svc.json")
        with open(p, "w") as f:
            json.dump(doc, f)
        os.chmod(p, mode)
        return p

    def test_reads_a_user_only_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, 0o600, {"clientId": "id.access", "clientSecret": "s3"})
            self.assertEqual(fbs.service_token(p), ("id.access", "s3"))

    def test_ignores_a_loose_missing_or_malformed_file(self):
        with tempfile.TemporaryDirectory() as d:
            loose = self._write(d, 0o644, {"clientId": "a", "clientSecret": "b"})
            self.assertIsNone(fbs.service_token(loose))
            self.assertIsNone(fbs.service_token(os.path.join(d, "none.json")))
            bad = self._write(d, 0o600, {"clientId": "a"})
            self.assertIsNone(fbs.service_token(bad))
            bad2 = self._write(d, 0o600, ["x"])
            self.assertIsNone(fbs.service_token(bad2))

    def test_ignores_a_symlink_and_header_splitting_values(self):
        with tempfile.TemporaryDirectory() as d:
            real = self._write(d, 0o600, {"clientId": "a", "clientSecret": "b"})
            link = os.path.join(d, "link.json")
            os.symlink(real, link)
            self.assertIsNone(fbs.service_token(link))
            for cid, sec in (("a\r\nX: y", "b"), ("a", "b\n"), ("a b", "c"), ("a", "b\x7f")):
                bad = self._write(d, 0o600, {"clientId": cid, "clientSecret": sec})
                self.assertIsNone(fbs.service_token(bad))

    def test_fetch_sends_service_headers_not_the_jwt_header(self):
        seen = {}

        class R:
            def read(self, n):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class O:
            def open(self, req, timeout=0):
                seen.update({k.lower(): v for k, v in req.header_items()})
                return R()

        old = fbs._OPENER
        fbs._OPENER = O()
        try:
            fbs.fetch("https://x/y", ("cid", "sec"))
            self.assertEqual(seen.get("cf-access-client-id"), "cid")
            self.assertEqual(seen.get("cf-access-client-secret"), "sec")
            self.assertNotIn("cf-access-token", seen)
        finally:
            fbs._OPENER = old


class SeatToolsTests(unittest.TestCase):
    DOC = {
        "rows": [
            {
                "seat": "JET",
                "problem": "ok",
                "tools": [
                    {"tool": "zulip", "state": "ok"},
                    {"tool": "relay", "state": "not_applicable"},
                ],
            },
            {
                "seat": "ECHO",
                "problem": "x",
                "tools": [
                    {"tool": "linear", "state": "missing"},
                    {"tool": "recall", "state": "unavailable"},
                ],
            },
            {"seat": "WISE", "problem": "x", "tools": [{"tool": "wake", "state": "missing"}]},
        ]
    }

    def test_missing_alarms_findings_do_not_and_unavailable_needs_two_polls(self):
        a, f, un = fbs.seat_tool_alarms(self.DOC, None)
        self.assertEqual(set(a), {"seat-tool:ECHO:linear:missing"})
        self.assertEqual(f, [{"seat": "WISE", "tool": "wake", "state": "missing"}])
        self.assertEqual(un, ["ECHO|recall"])
        a2, _f, _u = fbs.seat_tool_alarms(self.DOC, un)
        self.assertIn("seat-tool:ECHO:recall:unavailable", a2)

    def test_run_writes_seat_tools_and_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            args = types.SimpleNamespace(
                url="u",
                seat_tools_url="s",
                write=os.path.join(d, "o.json"),
                state=os.path.join(d, "st.json"),
                no_notify=True,
                dry_run=False,
            )
            docs = {"u": DOC_OK, "s": self.DOC}
            out = fbs.run(
                args,
                now=1000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=lambda url, t: docs[url],
                notify_fn=lambda *x: None,
            )
            self.assertIn("seat-tool:ECHO:linear:missing", [x["key"] for x in out["alarms"]])
            self.assertEqual(out["findings"][0]["seat"], "WISE")
            self.assertEqual(out["seatTools"][0]["tools"]["relay"], "not_applicable")
            docs["s"] = {
                "rows": [
                    {"seat": "ECHO", "problem": "ok", "tools": [{"tool": "linear", "state": "ok"}]}
                ]
            }
            out = fbs.run(
                args,
                now=2000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=lambda url, t: docs[url],
                notify_fn=lambda *x: None,
            )
            self.assertEqual(out["alarms"], [])
            self.assertEqual(
                [r["key"] for r in out["recovered"]], ["seat-tool:ECHO:linear:missing"]
            )

    def test_unreadable_seat_tools_keeps_alarms(self):
        with tempfile.TemporaryDirectory() as d:
            args = types.SimpleNamespace(
                url="u",
                seat_tools_url="s",
                write=os.path.join(d, "o.json"),
                state=os.path.join(d, "st.json"),
                no_notify=True,
                dry_run=False,
            )
            ok = {"u": DOC_OK, "s": self.DOC}

            def fetch(url, t):
                if url == "s" and fetch.down:
                    raise OSError("down")
                return ok[url]

            fetch.down = False
            fbs.run(
                args,
                now=1000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=fetch,
                notify_fn=lambda *x: None,
            )
            fetch.down = True
            out = fbs.run(
                args,
                now=2000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=fetch,
                notify_fn=lambda *x: None,
            )
            self.assertIn("seat-tool:ECHO:linear:missing", [x["key"] for x in out["alarms"]])
            self.assertEqual(out["seatToolsError"], "unreachable")


class SeatToolsRobustnessTests(unittest.TestCase):
    def _args(self, d):
        return types.SimpleNamespace(
            url="u",
            seat_tools_url="s",
            write=os.path.join(d, "o.json"),
            state=os.path.join(d, "st.json"),
            no_notify=True,
            dry_run=False,
        )

    def test_odd_shapes_do_not_abort(self):
        for bad in (
            {"rows": None},
            {"rows": [None, 3, {"seat": "A", "tools": None}]},
            {"rows": [{"seat": "A", "tools": [None]}]},
        ):
            a, f, u = fbs.seat_tool_alarms(bad, "junk")
            self.assertEqual((a, f, u), ({}, [], []))
            self.assertEqual(
                fbs.seat_tool_rows(bad),
                [{"seat": "A", "problem": None, "tools": {}}]
                if bad["rows"] and any(isinstance(x, dict) for x in bad["rows"])
                else [],
            )

    def test_bearers_api_down_keeps_seat_tool_alarms_and_unreadable_keeps_view(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            good = {"u": DOC_OK, "s": SeatToolsTests.DOC}
            fbs.run(
                args,
                now=1000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=lambda url, t: good[url],
                notify_fn=lambda *x: None,
            )

            def down(url, t):
                raise OSError("down")

            out = fbs.run(
                args,
                now=2000,
                token_fn=lambda: ("a", "b"),
                fetch_fn=down,
                notify_fn=lambda *x: None,
            )
            self.assertIn("seat-tool:ECHO:linear:missing", [x["key"] for x in out["alarms"]])
            self.assertEqual(out["recovered"], [])

    def test_unreadable_poll_resets_unavailable_baseline_and_keeps_findings(self):
        with tempfile.TemporaryDirectory() as d:
            args = self._args(d)
            docs = {"u": DOC_OK, "s": SeatToolsTests.DOC}
            run = lambda now: fbs.run(
                args,
                now=now,
                token_fn=lambda: ("a", "b"),
                fetch_fn=fetch,
                notify_fn=lambda *x: None,
            )

            def fetch(url, t):
                if url == "s" and docs.get("down"):
                    raise OSError("down")
                return docs[url]

            run(1000)  # first unavailable poll: no alarm
            docs["down"] = True
            out = run(2000)
            self.assertEqual(out["findings"][0]["seat"], "WISE")
            self.assertTrue(out["seatTools"])
            self.assertEqual(out["seatToolsError"], "unreachable")
            docs["down"] = False
            out = run(3000)  # unavailable again, but the run was broken: still no alarm
            self.assertNotIn("seat-tool:ECHO:recall:unavailable", [x["key"] for x in out["alarms"]])


class SeatToolsOwnerRulingTests(unittest.TestCase):
    def test_grok_web_wake_is_not_applicable_and_bearers_outage_resets_baseline(self):
        doc = {
            "rows": [
                {
                    "seat": "GROK-WEB",
                    "problem": "x",
                    "tools": [{"tool": "wake", "state": "missing"}],
                },
                {
                    "seat": "WISE",
                    "problem": "x",
                    "tools": [
                        {"tool": "wake", "state": "missing"},
                        {"tool": "recall", "state": "unavailable"},
                    ],
                },
            ]
        }
        a, f, u = fbs.seat_tool_alarms(doc, None)
        self.assertEqual(a, {})
        self.assertEqual(f, [{"seat": "WISE", "tool": "wake", "state": "missing"}])
        self.assertEqual(fbs.seat_tool_rows(doc)[0]["tools"]["wake"], "not_applicable")
        self.assertEqual(fbs.seat_tool_rows(doc)[1]["tools"]["wake"], "missing")
        with tempfile.TemporaryDirectory() as d:
            args = types.SimpleNamespace(
                url="u",
                seat_tools_url="s",
                write=os.path.join(d, "o.json"),
                state=os.path.join(d, "st.json"),
                no_notify=True,
                dry_run=False,
            )
            docs = {"u": DOC_OK, "s": doc}

            def fetch(url, t):
                if docs.get("down"):
                    raise OSError("down")
                return docs[url]

            run = lambda now: fbs.run(
                args,
                now=now,
                token_fn=lambda: ("a", "b"),
                fetch_fn=fetch,
                notify_fn=lambda *x: None,
            )
            run(1000)
            docs["down"] = True
            run(2000)  # bearers API unreadable: the unavailable run is broken
            docs["down"] = False
            out = run(3000)
            self.assertNotIn("seat-tool:WISE:recall:unavailable", [x["key"] for x in out["alarms"]])


if __name__ == "__main__":
    unittest.main()


class ProductionOpenFindingsTests(unittest.TestCase):
    def test_no_open_finding_is_listed_in_production(self):
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertEqual(fresh.OPEN_FINDINGS, frozenset())
