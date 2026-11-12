"""Offline: open_pr reports a number only for a pull request the provider confirms."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from git_relay import github_client, mcp_server
from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError
from git_relay.mcp_server import TOOLS, RelayMcp
from git_relay.policy import SeatPolicy
from git_relay.server import OutcomeStore, Relay
from git_relay.tests.test_draft_creation import ARGS, POLICY, PULLS, READY_RECEIPT, TOKEN, URL
from git_relay.tests.test_git_relay import FakeResponse

OWNER = "synthetic-existing-owner-pr-token"
FORK = {**READY_RECEIPT, "head": {"ref": "feature", "label": "Forker:feature"}}
OTHER_HEAD = {**READY_RECEIPT, "head": {"ref": "other", "label": "example:other"}}


def http_error(request, status, headers=None):
    return urllib.error.HTTPError(
        request.full_url, status, "error", headers or {}, io.BytesIO(b'{"message":"Not Found"}')
    )


def fails(status, headers=None):
    return lambda request: http_error(request, status, headers)


class RawResponse(FakeResponse):
    """A 200 whose body is the given bytes, not JSON."""

    def __init__(self, raw):
        self._raw = raw

    def __enter__(self):
        # The same object answers every attempt:  each starts at the beginning.
        self._pos = 0
        return self


class OpenPrReadBack(unittest.TestCase):
    def run_open(
        self, readback, extra=None, create=READY_RECEIPT, replays=0, reload=False, create_delay=0
    ):
        """readback is the GET's answer:  a payload, a callable that returns the error to
        raise, a RawResponse, or a tuple of those (one per GET, the last one repeating).
        create_delay is how many seconds the create POST takes on the injected clock."""
        calls = []
        answers = list(readback) if isinstance(readback, tuple) else [readback]
        self.sleeps = []
        self.timeouts = []
        self.spent = 0

        def opener(request, timeout):
            calls.append(request)
            if request.get_method() == "GET":
                self.timeouts.append(timeout)
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, RawResponse):
                    return answer
                if callable(answer):
                    error = answer(request)
                    self.addCleanup(getattr(error, "close", lambda: None))
                    raise error
                return FakeResponse(answer)
            self.spent += create_delay
            return FakeResponse(create)

        client = GitHubClient(OWNER, opener=opener)
        # No real waiting:  the clock moves only by what the read-back asked to sleep and by
        # the create's own delay.
        client._sleep = self.sleeps.append
        client._clock = lambda: sum(self.sleeps) + self.spent
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit"
            store = str(Path(directory) / "outcomes.json") if reload else None
            args = {**ARGS, **(extra or {})}
            responses = []
            relay = None
            for _ in range(1 + replays):
                if relay is None or reload:
                    # reload:  every call is a restarted relay reading the store from disk.
                    relay = Relay(
                        SeatPolicy(POLICY),
                        client,
                        AuditLog(str(path)),
                        outcomes=OutcomeStore(store),
                    )
                responses.append(relay.handle(TOKEN, "open_pr", args))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        return responses, calls, rows

    def assert_unknown(self, response, code):
        status, payload = response
        self.assertEqual(status, 502)
        self.assertEqual(payload["github_status"], 502)
        self.assertEqual(payload["outcome"], "unknown")
        self.assertIs(payload["retryable"], False)
        self.assertEqual(payload["github_code"], code)
        # Never a bare number presented as confirmed.
        self.assertNotIn("number", payload)
        self.assertNotIn("unconfirmed_number", payload)
        self.assertNotIn("url", payload)
        self.assertNotIn("verified", payload)

    def assert_unknown_row(self, row, code, unconfirmed=1):
        self.assertEqual(row["result"], "github_unknown")
        self.assertEqual(row["github_code"], code)
        # The number is not recorded as the pull request's:  only as unconfirmed.
        self.assertNotIn("number", row)
        self.assertNotIn("pr", row)
        if unconfirmed is None:
            self.assertNotIn("unconfirmed_number", row)
        else:
            self.assertEqual(row["unconfirmed_number"], unconfirmed)

    def test_success_is_verified_and_keeps_existing_keys(self):
        (response,), calls, rows = self.run_open(READY_RECEIPT)
        self.assertEqual(response, (200, {"number": 1, "url": URL, "verified": True}))
        self.assertEqual([c.get_method() for c in calls], ["POST", "GET"])
        self.assertEqual(calls[1].full_url, PULLS + "/1")
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer " + OWNER)
        self.assertEqual(self.timeouts, [github_client.READBACK_TIMEOUT_SECONDS])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(rows[-1]["result"], "ok")
        self.assertIs(rows[-1]["verified"], True)
        self.assertNotIn("unconfirmed_number", rows[-1])

    def test_replay_of_verified_success_returns_stored_answer(self):
        responses, calls, _ = self.run_open(READY_RECEIPT, replays=1)
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(len(calls), 2)

    def test_draft_path_is_read_back_and_verified(self):
        draft = {**READY_RECEIPT, "draft": True}
        (response,), calls, _ = self.run_open(draft, extra={"draft": True}, create=draft)
        self.assertEqual(response, (200, {"number": 1, "url": URL, "verified": True}))
        self.assertEqual(len(calls), 2)
        # A read-back that says the pull request is not a draft is a mismatch, and held.
        responses, calls, rows = self.run_open(
            READY_RECEIPT, extra={"draft": True}, create=draft, replays=1
        )
        self.assert_unknown(responses[0], "open_pr_readback_mismatch")
        self.assert_unknown_row(rows[0], "open_pr_readback_mismatch")
        self.assertEqual(responses[1][0], 409)
        self.assertEqual([c.get_method() for c in calls], ["POST", "GET"])

    def test_transport_failure_on_read_back_is_retried_then_unknown(self):
        (response,), calls, rows = self.run_open(lambda request: urllib.error.URLError("down"))
        self.assert_unknown(response, "open_pr_readback_failed")
        self.assertEqual(response[1]["readback_status"], 0)
        self.assertNotIn("readback_retry_after_seconds", response[1])
        self.assertEqual([c.get_method() for c in calls], ["POST", "GET", "GET", "GET"])
        self.assertEqual(self.sleeps, list(github_client.READBACK_BACKOFF_SECONDS))
        self.assert_unknown_row(rows[-1], "open_pr_readback_failed")
        self.assertEqual(rows[-1]["readback_status"], 0)

    def test_http_errors_on_read_back_are_unknown_never_terminal_or_retryable(self):
        # status -> read-backs made:  only an early, throttled or failing read is retried.
        for status, gets in ((404, 3), (408, 3), (429, 3), (500, 3), (503, 3), (401, 1), (403, 1)):
            with self.subTest(status=status):
                (response,), calls, rows = self.run_open(fails(status))
                self.assert_unknown(response, "open_pr_readback_failed")
                self.assertEqual(response[1]["readback_status"], status)
                self.assertEqual(len(calls), 1 + gets)
                self.assert_unknown_row(rows[-1], "open_pr_readback_failed")
                self.assertEqual(rows[-1]["readback_status"], status)

    def test_read_back_that_recovers_within_the_budget_is_verified(self):
        for first in (fails(404), fails(502), lambda request: urllib.error.URLError("down")):
            with self.subTest(first=first):
                (response,), calls, rows = self.run_open((first, READY_RECEIPT))
                self.assertEqual(response, (200, {"number": 1, "url": URL, "verified": True}))
                self.assertEqual([c.get_method() for c in calls], ["POST", "GET", "GET"])
                self.assertEqual(self.sleeps, [github_client.READBACK_BACKOFF_SECONDS[0]])
                self.assertEqual(rows[-1]["result"], "ok")

    def test_retry_after_is_honoured_only_inside_the_budget(self):
        throttled = fails(429, {"Retry-After": "6"})
        (response,), calls, _ = self.run_open((throttled, READY_RECEIPT))
        self.assertEqual(response[0], 200)
        self.assertEqual(self.sleeps, [6])
        # The second read gets what is left of the budget, not a fresh full timeout.
        self.assertEqual(self.timeouts, [3, 2])
        self.assertLessEqual(sum(self.sleeps) + self.timeouts[-1], 8)
        # A wait that cannot fit ends the read-back at once and is reported, not slept.
        for status in (429, 403):
            with self.subTest(status=status):
                (response,), calls, rows = self.run_open(fails(status, {"Retry-After": "30"}))
                self.assert_unknown(response, "open_pr_readback_failed")
                self.assertEqual(response[1]["readback_status"], status)
                self.assertEqual(response[1]["readback_retry_after_seconds"], 30)
                self.assertEqual(rows[-1]["readback_retry_after_seconds"], 30)
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.sleeps, [])
        # A 403 that names a short Retry-After is a throttle and is retried.
        (response,), calls, _ = self.run_open((fails(403, {"Retry-After": "1"}), READY_RECEIPT))
        self.assertEqual(response[0], 200)
        self.assertEqual(len(calls), 3)

    def test_read_back_bounds_fit_inside_the_callers_timeouts(self):
        self.assertLessEqual(github_client.READBACK_TIMEOUT_SECONDS, 10)
        # The relay's whole-call budget is the MCP wrapper's own wait, not a second number.
        self.assertEqual(github_client.CALLER_BUDGET_SECONDS, mcp_server.TIMEOUT_SECONDS)
        self.assertLess(
            github_client.READBACK_BUDGET_SECONDS + github_client.READBACK_MARGIN_SECONDS,
            github_client.CALLER_BUDGET_SECONDS,
        )
        self.assertEqual(
            len(github_client.READBACK_BACKOFF_SECONDS), github_client.READBACK_ATTEMPTS - 1
        )
        self.run_open(fails(500))
        self.assertTrue(
            all(1 <= t <= github_client.READBACK_TIMEOUT_SECONDS for t in self.timeouts)
        )

    def test_slow_create_caps_or_skips_the_read_back(self):
        limit = github_client.CALLER_BUDGET_SECONDS - github_client.READBACK_MARGIN_SECONDS
        # 20 s spent on the create:  6 s of the 8 s budget remain, and the whole call
        # (create, reads and waits) ends inside the caller's wait, margin included.
        (response,), calls, _ = self.run_open(fails(500), create_delay=20)
        self.assert_unknown(response, "open_pr_readback_failed")
        self.assertLessEqual(self.spent + sum(self.sleeps), limit)
        self.assertEqual(self.timeouts, [3, 3, 3])
        # 25 s spent:  one second is left, so the one read gets a one second timeout and no
        # retry wait fits.
        (response,), calls, _ = self.run_open(fails(500), create_delay=limit - 1)
        self.assert_unknown(response, "open_pr_readback_failed")
        self.assertEqual(self.timeouts, [1])
        self.assertEqual(self.sleeps, [])
        # Less than a minimum attempt left:  the read-back is skipped, never started, and
        # the answer is the explicit outcome unknown with readback_status 0.
        for delay in (limit - 0.5, limit, 29, 100):
            with self.subTest(delay=delay):
                responses, calls, rows = self.run_open(READY_RECEIPT, create_delay=delay, replays=1)
                self.assert_unknown(responses[0], "open_pr_readback_skipped")
                self.assertEqual(responses[0][1]["readback_status"], 0)
                self.assert_unknown_row(rows[0], "open_pr_readback_skipped")
                self.assertEqual(rows[0]["readback_status"], 0)
                self.assertEqual([c.get_method() for c in calls], ["POST"])
                self.assertEqual(self.timeouts, [])
                # Held like every other unknown outcome:  no second create.
                self.assertEqual(responses[1][0], 409)

    def test_read_back_uses_the_creates_credential(self):
        task = "task-scoped-" + "credential"
        seen = []

        def opener(request, timeout):
            seen.append((request.get_method(), request.get_header("Authorization")))
            return FakeResponse(READY_RECEIPT)

        client = GitHubClient(OWNER, opener=opener)
        self.assertEqual(client.open_pr(**ARGS, bearer=task)["verified"], True)
        self.assertEqual(seen, [("POST", "Bearer " + task), ("GET", "Bearer " + task)])
        # No bearer:  both calls use the client's own token, as before.
        seen.clear()
        client.open_pr(**ARGS)
        self.assertEqual(seen, [("POST", "Bearer " + OWNER), ("GET", "Bearer " + OWNER)])
        # An empty task credential never falls back to the owner token.
        seen.clear()
        with self.assertRaises(ValueError):
            client.open_pr(**ARGS, bearer="")
        self.assertEqual(seen, [])

    def test_trickling_read_back_hits_the_deadline(self):
        now = [0]
        reads = []
        raw = json.dumps(READY_RECEIPT).encode()

        class Trickle:
            """One byte per read, two seconds each:  no single read ever times out."""

            pos = 0

            def read(self, limit):
                now[0] += 2
                reads.append(limit)
                piece = raw[self.pos : self.pos + 1]
                self.pos += 1
                return piece

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout):
            return FakeResponse(READY_RECEIPT) if request.get_method() == "POST" else Trickle()

        client = GitHubClient(OWNER, opener=opener)
        client._clock = lambda: now[0]
        client._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        with self.assertRaises(RelayGitHubError) as ctx:
            client.open_pr(**ARGS)
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "open_pr_readback_failed")
        # A deadline expiry is a transport failure, not a mismatch or an HTTP answer.
        self.assertEqual(ctx.exception.readback_status, 0)
        self.assertEqual(ctx.exception.unconfirmed_number, 1)
        # Nothing ran past the budget (plus the one read that crossed it).
        self.assertLessEqual(now[0], github_client.READBACK_BUDGET_SECONDS + 2)
        self.assertLessEqual(len(reads), github_client.READBACK_BUDGET_SECONDS // 2 + 1)
        self.assertTrue(all(n <= github_client.READBACK_READ_CHUNK for n in reads))

    def test_blocking_read_cannot_outlive_the_deadline(self):
        """A real socket's read(n) blocks until n bytes arrive;  only read1 returns per receive."""
        now = [0]
        raw = json.dumps(READY_RECEIPT).encode()
        calls = {"read": 0, "read1": 0}

        class Socketlike:
            """A peer sending one byte every two seconds, with http.client's blocking read(n)."""

            pos = 0

            def _receive(self):
                now[0] += 2
                piece = raw[self.pos : self.pos + 1]
                self.pos += 1
                return piece

            def read(self, limit):
                calls["read"] += 1
                out = b""
                while len(out) < limit:
                    piece = self._receive()
                    if not piece:
                        break
                    out += piece
                return out

            def read1(self, limit):
                calls["read1"] += 1
                return self._receive()

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout):
            return FakeResponse(READY_RECEIPT) if request.get_method() == "POST" else Socketlike()

        client = GitHubClient(OWNER, opener=opener)
        client._clock = lambda: now[0]
        client._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        with self.assertRaises(RelayGitHubError) as ctx:
            client.open_pr(**ARGS)
        self.assertEqual(ctx.exception.code, "open_pr_readback_failed")
        self.assertEqual(ctx.exception.readback_status, 0)
        self.assertEqual(calls["read"], 0)
        self.assertGreater(calls["read1"], 0)
        # Within the budget plus the one receive that crossed it (read(n) would run to EOF).
        self.assertLessEqual(now[0], github_client.READBACK_BUDGET_SECONDS + 2)

    def test_draft_receipt_that_does_not_verify_names_its_code_and_number(self):
        draft = {**READY_RECEIPT, "draft": True}
        bad = [
            {**draft, "html_url": URL.replace("/1", "/2")},
            {**draft, "html_url": URL.replace("example/fixture", "other/repo")},
            {**draft, "draft": False},
            {**draft, "draft": None},
        ]
        for create in bad:
            with self.subTest(create=create):
                responses, calls, rows = self.run_open(
                    draft, extra={"draft": True}, create=create, replays=1
                )
                self.assert_unknown(responses[0], "open_pr_receipt_mismatch")
                self.assertNotIn("readback_status", responses[0][1])
                self.assert_unknown_row(rows[0], "open_pr_receipt_mismatch")
                self.assertEqual(responses[1][0], 409)
                # The create's receipt failed:  no read-back was attempted.
                self.assertEqual([c.get_method() for c in calls], ["POST"])
        # With no usable number there is nothing to record as unconfirmed.
        (response,), _, rows = self.run_open(
            draft, extra={"draft": True}, create={**draft, "number": "1", "draft": False}
        )
        self.assert_unknown(response, "open_pr_receipt_mismatch")
        self.assert_unknown_row(rows[-1], "open_pr_receipt_mismatch", unconfirmed=None)

    def test_non_json_read_back_is_unknown(self):
        (response,), calls, rows = self.run_open(RawResponse(b"<html>not json</html>"))
        self.assert_unknown(response, "open_pr_readback_failed")
        self.assertEqual(response[1]["readback_status"], 0)
        self.assertEqual(len(calls), 1 + github_client.READBACK_ATTEMPTS)
        self.assert_unknown_row(rows[-1], "open_pr_readback_failed")

    def test_mismatched_read_back_is_unknown(self):
        good = READY_RECEIPT
        bad = [
            OTHER_HEAD,
            {**good, "base": {**good["base"], "ref": "release"}},
            {**good, "base": {**good["base"], "repo": {"full_name": "other/repo"}}},
            {**good, "number": 2},
            {**good, "number": True},
            {**good, "head": None},
            {**good, "base": None},
            {**good, "html_url": URL.replace("example/fixture", "other/repo")},
            {**good, "html_url": None},
            {},
            [],
            None,
            "scalar",
        ]
        for readback in bad:
            with self.subTest(readback=readback):
                (response,), calls, rows = self.run_open(readback)
                self.assert_unknown(response, "open_pr_readback_mismatch")
                self.assertEqual(response[1]["readback_status"], 200)
                # A mismatch is an answer, not a failed read:  it is not retried.
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.sleeps, [])
                self.assert_unknown_row(rows[-1], "open_pr_readback_mismatch")
                self.assertEqual(rows[-1]["readback_status"], 200)

    def test_ready_create_with_missing_or_foreign_url_is_never_verified(self):
        urls = [
            None,
            {},
            "url",
            URL + "?token=private",
            URL.replace("/1", "/2"),
            URL.replace("example/fixture", "other/repo"),
            URL.replace("github.com", "github.com.invalid"),
            URL + "x" * 2048,
        ]
        for url in urls:
            with self.subTest(url=url):
                create = {**READY_RECEIPT, "html_url": url}
                (response,), calls, rows = self.run_open(READY_RECEIPT, create=create)
                self.assert_unknown(response, "open_pr_readback_mismatch")
                self.assertEqual(len(calls), 2)
                self.assert_unknown_row(rows[-1], "open_pr_readback_mismatch")
        create = {key: value for key, value in READY_RECEIPT.items() if key != "html_url"}
        (response,), _, _ = self.run_open(READY_RECEIPT, create=create)
        self.assert_unknown(response, "open_pr_readback_mismatch")
        # The provider's own casing of the same page is the same page.
        (response,), _, _ = self.run_open(
            READY_RECEIPT, create={**READY_RECEIPT, "html_url": URL.replace("example", "Example")}
        )
        self.assertEqual(response[0], 200)

    def test_create_receipt_without_usable_number_is_unknown_before_read_back(self):
        for number in (None, 0, "1", True, 2**53):
            with self.subTest(number=number):
                (response,), calls, rows = self.run_open(
                    READY_RECEIPT, create={**READY_RECEIPT, "number": number}
                )
                self.assert_unknown(response, "open_pr_receipt_invalid")
                self.assertNotIn("readback_status", response[1])
                self.assertEqual(len(calls), 1)
                self.assert_unknown_row(rows[-1], "open_pr_receipt_invalid", unconfirmed=None)

    def test_replay_after_unknown_is_refused_with_no_upstream_call(self):
        cases = (
            (fails(404), "open_pr_readback_failed", 1 + github_client.READBACK_ATTEMPTS),
            (OTHER_HEAD, "open_pr_readback_mismatch", 2),
        )
        for readback, code, upstream in cases:
            for reload in (False, True):
                with self.subTest(code=code, reload=reload):
                    responses, calls, rows = self.run_open(readback, replays=2, reload=reload)
                    self.assert_unknown(responses[0], code)
                    for status, payload in responses[1:]:
                        self.assertEqual(status, 409)
                        self.assertEqual(payload["outcome"], "unknown")
                        self.assertIs(payload["retryable"], False)
                        self.assertNotIn("number", payload)
                    # One create in total:  no second create, no replayed success, and no
                    # upstream call at all for a held retry (also after a restart).
                    self.assertEqual(len(calls), upstream)
                    self.assertEqual([c.get_method() for c in calls].count("POST"), 1)
                    self.assertEqual(
                        [row["result"] for row in rows],
                        ["github_unknown", "duplicate_refused", "duplicate_refused"],
                    )

    def test_head_forms_accepted_by_the_create_are_matched(self):
        cases = [
            ({"head": "forker:feature"}, FORK, None),
            ({"head": "forker:fixture:feature"}, FORK, None),
            ({"head": "forker:refs/heads/feature"}, FORK, None),
            ({"head": "someone:feature"}, FORK, "open_pr_readback_mismatch"),
            ({"head": "someone:fixture:feature"}, FORK, "open_pr_readback_mismatch"),
            ({"head": "fixture:forker:feature"}, FORK, "open_pr_readback_mismatch"),
            (
                {"head": "forker:feature"},
                {**FORK, "head": {"ref": "feature"}},
                "open_pr_readback_mismatch",
            ),
            ({"head": "refs/heads/feature", "base": "refs/heads/main"}, READY_RECEIPT, None),
            ({"head": "Feature"}, READY_RECEIPT, "open_pr_readback_mismatch"),
        ]
        for extra, readback, code in cases:
            with self.subTest(extra=extra):
                (response,), _, _ = self.run_open(readback, extra=extra)
                if code is None:
                    self.assertEqual(response, (200, {"number": 1, "url": URL, "verified": True}))
                else:
                    self.assert_unknown(response, code)

    def test_client_raises_502_with_a_stable_code(self):
        def opener(request, timeout):
            if request.get_method() == "GET":
                error = http_error(request, 404)
                self.addCleanup(error.close)
                raise error
            return FakeResponse(READY_RECEIPT)

        client = GitHubClient(OWNER, opener=opener)
        client._sleep = lambda seconds: None
        with self.assertRaises(RelayGitHubError) as ctx:
            client.open_pr(**ARGS)
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "open_pr_readback_failed")
        self.assertEqual(ctx.exception.readback_status, 404)
        self.assertEqual(ctx.exception.unconfirmed_number, 1)

    def test_mcp_description_states_the_read_back(self):
        description = next(t for t in TOOLS if t["name"] == "open_pr")["description"]
        self.assertIn("read back", description)
        self.assertIn("verified", description)
        self.assertIn("unknown", description)
        for word in (
            "github_code",
            "readback_status",
            "readback_retry_after_seconds",
            "open_pr_readback_skipped",
            "open_pr_receipt_mismatch",
        ):
            self.assertIn(word, description)

    def call_mcp(self, status, body, name="open_pr"):
        """The MCP wrapper's answer when the relay answers HTTP status with body."""

        def opener(request, timeout):
            error = urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode())
            )
            self.addCleanup(error.close)
            raise error

        wrapper = RelayMcp("https://relay.example.test", "synthetic-own-bearer", opener=opener)
        answer = wrapper.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": ARGS},
            }
        )
        self.assertIs(answer["result"]["isError"], True)
        return json.loads(answer["result"]["content"][0]["text"])

    def test_mcp_outcome_unknown_carries_the_read_back_detail_but_no_number(self):
        relay_body = {
            "error": "github call outcome unknown",
            "github_status": 502,
            "retryable": False,
            "outcome": "unknown",
            "github_code": "open_pr_readback_failed",
            "readback_status": 429,
            "readback_retry_after_seconds": 30,
            "number": 7,
            "unconfirmed_number": 7,
            "url": URL,
        }
        error = self.call_mcp(502, relay_body)
        self.assertEqual(error["code"], "outcome_unknown")
        self.assertIs(error["retryable"], False)
        self.assertEqual(error["http_status"], 502)
        self.assertEqual(error["github_code"], "open_pr_readback_failed")
        self.assertEqual(error["readback_status"], 429)
        self.assertEqual(error["readback_retry_after_seconds"], 30)
        for key in ("number", "unconfirmed_number", "url"):
            self.assertNotIn(key, error)
        self.assertNotIn(URL, json.dumps(error))
        # A transport failure and a skipped read-back both say 0.
        error = self.call_mcp(
            502,
            {
                "outcome": "unknown",
                "github_code": "open_pr_readback_skipped",
                "readback_status": 0,
            },
        )
        self.assertEqual(error["github_code"], "open_pr_readback_skipped")
        self.assertEqual(error["readback_status"], 0)
        self.assertNotIn("readback_retry_after_seconds", error)
        # Without the fields the answer is the plain unknown outcome, as before.
        error = self.call_mcp(502, {"outcome": "unknown"})
        self.assertEqual(set(error), {"code", "message", "retryable", "http_status"})
        # Values that are not a short code or a bounded number are dropped, never echoed.
        error = self.call_mcp(
            502,
            {
                "outcome": "unknown",
                "github_code": "not a code " + URL,
                "readback_status": True,
                "readback_retry_after_seconds": "30",
            },
        )
        self.assertEqual(set(error), {"code", "message", "retryable", "http_status"})
        # Only open_pr's answer is widened.
        widened = {"github_code": "open_pr_readback_failed", "readback_status": 404}
        self.assertEqual(mcp_server._readback_detail("open_pr", widened), widened)
        self.assertEqual(mcp_server._readback_detail("edit_pr", widened), {})


if __name__ == "__main__":
    unittest.main()
