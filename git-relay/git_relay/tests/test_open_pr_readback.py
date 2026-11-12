"""Offline: open_pr reports a number only for a pull request the provider confirms."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from git_relay import github_client
from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError
from git_relay.mcp_server import TOOLS
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


class OpenPrReadBack(unittest.TestCase):
    def run_open(self, readback, extra=None, create=READY_RECEIPT, replays=0, reload=False):
        """readback is the GET's answer:  a payload, a callable that returns the error to
        raise, a RawResponse, or a tuple of those (one per GET, the last one repeating)."""
        calls = []
        answers = list(readback) if isinstance(readback, tuple) else [readback]
        self.sleeps = []
        self.timeouts = []

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
            return FakeResponse(create)

        client = GitHubClient(OWNER, opener=opener)
        # No real waiting:  the clock moves only by what the read-back asked to sleep.
        client._sleep = self.sleeps.append
        client._clock = lambda: sum(self.sleeps)
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
                        SeatPolicy(POLICY), client, AuditLog(str(path)), outcomes=OutcomeStore(store)
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
        self.assertLess(github_client.READBACK_BUDGET_SECONDS, github_client.TIMEOUT_SECONDS)
        self.assertEqual(
            len(github_client.READBACK_BACKOFF_SECONDS), github_client.READBACK_ATTEMPTS - 1
        )
        self.run_open(fails(500))
        self.assertTrue(all(1 <= t <= github_client.READBACK_TIMEOUT_SECONDS for t in self.timeouts))

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


if __name__ == "__main__":
    unittest.main()
