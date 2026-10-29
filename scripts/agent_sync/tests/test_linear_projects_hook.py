"""AFC-565:  the Operator's Linear project webhook (linear_projects_hook.py) and its
route on the listener API (listener_api.py).

The hook is driven directly (no socket);  one test runs it behind a real
ListenerReadApi on an ephemeral port.  Nothing here touches the network.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_linear_projects_hook
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

import linear_project_linkifiers as LPL
from agent_sync import linear_projects_hook as H
from agent_sync import listener_api as API

SECRET = "linear-webhook-secret-long"


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def project_event(action="create", stamp=None):
    return {
        "type": "Project",
        "action": action,
        "webhookTimestamp": stamp if stamp is not None else int(time.time() * 1000),
        "data": {
            "identifier": "P-BF-6",
            "slugId": "5afcbc055535",
            "url": "https://linear.app/simple-with-us/project/rewrite-5afcbc055535",
        },
    }


def signed(payload):
    body = json.dumps(payload).encode("utf-8")
    return body, {"Linear-Signature": sign(body)}


class HookTests(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.events = []
        self.hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda secrets: self.runs.append(secrets),
            clock=time.time,
            log=lambda event, **fields: self.events.append((event, fields)),
        )

    def tearDown(self):
        for event, fields in self.events:
            self.assertNotIn(SECRET, json.dumps(fields))
            self.assertNotIn("rewrite-5afcbc055535-body", json.dumps(fields))

    def post(self, payload, headers=None, path=H.PATH):
        body = json.dumps(payload).encode("utf-8")
        heads = dict(headers) if headers is not None else {"Linear-Signature": sign(body)}
        return self.hook.handle("POST", path, heads, body)

    def test_post_starts_one_run_and_answers_200(self):
        status, answer = self.post(project_event())
        self.assertEqual((status, answer), (200, {"ok": True, "queued": True}))
        deadline = time.time() + 5
        while not self.runs and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0]["LINEAR_WEBHOOK_SECRET"], SECRET)

    def test_a_run_that_raises_system_exit_does_not_stop_later_runs(self):
        # zulip_linkifiers.Zulip.call raises SystemExit on an HTTP error.
        calls = []

        def runner(secrets):
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit("PATCH realm/filters/1:  HTTP 400")

        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET}, runner, clock=time.time, log=lambda *a, **k: None
        )
        for _ in range(2):
            body, heads = signed(project_event())
            self.assertEqual(hook.handle("POST", H.PATH, heads, body)[0], 200)
            deadline = time.time() + 5
            while hook.queued and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(calls), 2)

    def test_only_well_formed_codes_and_slugs_reach_the_log(self):
        payload = project_event()
        payload["data"] = {"identifier": "P-BF-6\nforged", "slugId": "not-a-slug", "url": "x"}
        self.post(payload)
        queued = [fields for event, fields in self.events if event == "linear-projects-queued"]
        self.assertEqual(queued[-1]["code"], None)
        self.assertEqual(queued[-1]["slug"], None)

    def test_update_queues_too(self):
        status, answer = self.post(project_event(action="update"))
        self.assertEqual((status, answer), (200, {"ok": True, "queued": True}))

    def test_anything_but_a_project_create_or_update_is_ignored(self):
        for payload in (
            {**project_event(), "type": "Issue"},
            {**project_event(), "action": "remove"},
            {**project_event(), "action": "delete"},
        ):
            status, answer = self.post(payload)
            self.assertEqual((status, answer), (200, {"ok": True, "ignored": True}))
        time.sleep(0.2)
        self.assertEqual(self.runs, [])

    def test_only_post(self):
        body, headers = signed(project_event())
        for method in ("GET", "PUT", "DELETE"):
            status, _ = self.hook.handle(method, H.PATH, headers, body)
            self.assertEqual(status, 405)

    def test_oversized_bodies_are_413(self):
        big = b"x" * (H.MAX_BODY + 1)
        status, _ = self.hook.handle("POST", H.PATH, {"Linear-Signature": sign(big)}, big)
        self.assertEqual(status, 413)

    def test_bad_or_missing_signature_is_401(self):
        body, _ = signed(project_event())
        status, _ = self.hook.handle("POST", H.PATH, {"Linear-Signature": "0" * 64}, body)
        self.assertEqual(status, 401)
        status, _ = self.hook.handle("POST", H.PATH, {}, body)
        self.assertEqual(status, 401)

    def test_stale_events_are_401(self):
        old = project_event(stamp=int(time.time() * 1000) - 61_000)
        self.assertEqual(self.post(old)[0], 401)

    def test_unknown_paths_are_404(self):
        body, headers = signed(project_event())
        self.assertEqual(self.hook.handle("POST", "/linear/v1/other", headers, body)[0], 404)

    def test_coalescing_three_events_during_a_run_cause_one_extra_run(self):
        entered = threading.Event()
        release = threading.Event()
        runs = []
        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda secrets: (entered.set(), release.wait(timeout=10), runs.append(1)),
            clock=time.time,
        )
        bodies = []
        for _ in range(3):
            body = json.dumps(project_event()).encode("utf-8")
            bodies.append(({"Linear-Signature": sign(body)}, body))
        # The first event starts the run;  the other two arrive while it is blocked.
        self.assertEqual(hook.handle("POST", H.PATH, *bodies[0]), (200, {"ok": True, "queued": True}))
        self.assertTrue(entered.wait(timeout=5))
        self.assertEqual(hook.handle("POST", H.PATH, *bodies[1]), (200, {"ok": True, "queued": True}))
        self.assertEqual(hook.handle("POST", H.PATH, *bodies[2]), (200, {"ok": True, "queued": True}))
        release.set()
        deadline = time.time() + 5
        while len(runs) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(runs), 2, "exactly one extra run after the current one")
        time.sleep(0.2)
        self.assertEqual(len(runs), 2, "no third run")

    def test_no_arg_runners_work_too(self):
        runs = []
        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda: runs.append(1),
            clock=time.time,
        )
        body, headers = signed(project_event())
        self.assertEqual(hook.handle("POST", H.PATH, headers, body)[0], 200)
        deadline = time.time() + 5
        while not runs and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(runs, [1])


class SecretsFailedTests(unittest.TestCase):
    def test_every_request_is_503_and_the_reason_is_logged_once(self):
        events = []

        def loader():
            raise LPL.SecretsRefused("token_not_read_only")

        hook = H.LinearProjectsHook(
            loader, lambda s: None, log=lambda e, **f: events.append((e, f))
        )
        body, headers = signed(project_event())
        for _ in range(2):
            status, answer = hook.handle("POST", H.PATH, headers, body)
            self.assertEqual((status, answer), (503, {"error": "not_configured"}))
        reasons = [f for e, f in events if e == "linear-projects-secrets"]
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0], {"reason": "token_not_read_only"})


class ListenerApiSocketTests(unittest.TestCase):
    def test_a_signed_body_posts_through_the_server(self):
        runs = []
        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda secrets: runs.append(secrets),
            clock=time.time,
        )
        api = API.ListenerReadApi("127.0.0.1", 0, "/tmp", {}, linear_projects=hook)
        api.start()
        self.addCleanup(api.close)
        host, port = api.address
        body = json.dumps(project_event()).encode("utf-8")
        req = urllib.request.Request(
            "http://%s:%d%s" % (host, port, H.PATH),
            data=body,
            method="POST",
            headers={"Linear-Signature": sign(body)},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            answer = json.loads(response.read())
            self.assertEqual((response.status, answer), (200, {"ok": True, "queued": True}))
        deadline = time.time() + 5
        while not runs and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(runs), 1)
        # An unsigned body is still refused, and POST elsewhere is still 405.
        unsigned = urllib.request.Request(
            "http://%s:%d%s" % (host, port, H.PATH), data=body, method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(unsigned, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                urllib.request.Request(
                    "http://%s:%d/listener/v1/JET/status" % (host, port),
                    data=b"{}",
                    method="POST",
                ),
                timeout=5,
            )
        caught.exception.close()
        self.assertEqual(caught.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
