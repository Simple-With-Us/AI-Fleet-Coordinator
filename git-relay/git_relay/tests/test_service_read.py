"""The already-published review reader reaches only narrowed authenticated reads."""

import json
import tempfile
import unittest
from pathlib import Path
from secrets import token_hex
from unittest import mock

from git_relay.audit import AuditLog
from git_relay.outcomes import OutcomeStore
from git_relay.policy import SeatPolicy
from git_relay.pr_owners import PROwnerStore
from git_relay.server import Relay

REPO = "Simple-With-Us/example"
# Fresh, process-local test credentials. No credential bytes live in source.
TOKEN = token_hex(24)
NATIVE_TOKEN = token_hex(24)
INSTALLATION_TOKEN = token_hex(24)
SHA = "a" * 40
BASE_SHA = "b" * 40
CLAIMS = dict(
    origin_session_claim="session-fixture-1234", origin_letter_claim="AB", origin_generation_claim=3
)


class Client:
    def __init__(self):
        self.calls = []
        self.creates = []
        self.value = None

    def _call(self, method, path, payload=None, *, bearer=None):
        self.calls.append((method, path, bearer))
        return self.value

    def open_pr(self, repo, **kwargs):
        self.creates.append((repo, kwargs))
        return {"number": 7, "url": "https://github.com/%s/pull/7" % repo}


class Minter:
    def __init__(self):
        self.calls = []

    def token_for(self, slug, repo, permissions):
        self.calls.append((slug, repo, permissions))
        return INSTALLATION_TOKEN, 9999999999


class Reads(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.client, self.minter = Client(), Minter()
        self.owners = PROwnerStore(str(root / "owners.jsonl"))
        self.policy = SeatPolicy(
            json.dumps(
                {
                    "swu-operator": {
                        "token": TOKEN,
                        "actions": ["service_read"],
                        "repos": [REPO],
                        "app_slug": "swu-operator",
                        "mint_permissions": {
                            "pull_requests": "read",
                            "checks": "read",
                            "metadata": "read",
                        },
                    },
                    "swu-codex": {
                        "token": NATIVE_TOKEN,
                        "actions": ["open_pr", "service_read"],
                        "repos": [REPO],
                        "app_slug": "swu-codex",
                        "mint_permissions": {"pull_requests": "read", "metadata": "read"},
                    },
                }
            )
        )
        self.relay = Relay(
            self.policy,
            self.client,
            AuditLog(str(root / "audit.jsonl")),
            app_minter=self.minter,
            pr_owners=self.owners,
            outcomes=OutcomeStore(str(root / "outcomes.json")),
        )

    def read(self, verb, **kwargs):
        return self.relay.handle(TOKEN, "service_read", {"repo": REPO, "verb": verb, **kwargs})

    def create(self, **extra):
        args = dict(
            repo=REPO, base="main", head="native-branch", title="[CODEX] fixture", body="fixture"
        )
        return self.relay.handle(NATIVE_TOKEN, "open_pr", {**args, **extra})

    def test_only_dedicated_identity_action_and_repository_are_accepted(self):
        self.assertEqual(
            self.relay.handle("wrong", "service_read", dict(repo=REPO, verb="pull", number=7))[0],
            401,
        )
        self.assertEqual(
            self.relay.handle(
                TOKEN, "open_pr", dict(repo=REPO, base="main", head="b", title="t", body="b")
            )[0],
            403,
        )
        self.assertEqual(
            self.relay.handle(
                TOKEN, "service_read", dict(repo="other/repo", verb="pull", number=7)
            )[0],
            403,
        )
        # Even a seat accidentally granted the read action cannot impersonate Operator.
        self.assertEqual(
            self.relay.handle(NATIVE_TOKEN, "service_read", dict(repo=REPO, verb="pull", number=7))[
                0
            ],
            403,
        )
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.minter.calls, [])

    def test_unknown_verb_extra_fields_paths_and_invalid_types_fail_before_mint(self):
        cases = [
            dict(verb="mint_token"),
            dict(verb="creation_receipts", path="/anything"),
            dict(verb="pull", number=True),
            dict(verb="reviews", number=7, page=0),
            dict(verb="reviews", number=7, page=6),
            dict(verb="check_runs", sha="main", page=1),
            dict(verb="pull", number=7, method="POST"),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(
                    self.relay.handle(TOKEN, "service_read", dict(repo=REPO, **args))[0], 400
                )
        self.assertEqual(self.minter.calls, [])

    def test_pull_is_own_app_read_only_and_projects_no_source_text(self):
        self.client.value = dict(
            number=7,
            state="open",
            head={"sha": SHA},
            base={"sha": BASE_SHA},
            mergeable=False,
            mergeable_state="dirty",
            html_url="https://github.com/%s/pull/7" % REPO,
            title="private title",
            body="private review body",
        )
        status, result = self.read("pull", number=7)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"]["head"]["sha"], SHA)
        self.assertEqual(result["body"]["base"], {"sha": BASE_SHA})
        self.assertIs(result["body"]["mergeable"], False)
        self.assertEqual(result["body"]["mergeable_state"], "dirty")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn(INSTALLATION_TOKEN, json.dumps(result))
        self.assertEqual(
            self.client.calls,
            [("GET", "/repos/%s/pulls/7" % REPO, INSTALLATION_TOKEN)],
        )
        self.assertEqual(
            self.minter.calls,
            [("swu-operator", REPO, {"pull_requests": "read", "metadata": "read"})],
        )
        self.assertEqual(self.relay.outcomes._rows, {})

    def test_pull_projects_absent_or_null_merge_state_as_unknown(self):
        for extra in (
            {},
            {"mergeable": None},
            {"mergeable_state": None},
            {"mergeable": None, "mergeable_state": None},
            {"mergeable": None, "mergeable_state": "unknown"},
        ):
            with self.subTest(extra=extra):
                self.client.value = dict(
                    number=7, state="open", head={"sha": SHA}, base={"sha": BASE_SHA}, **extra
                )
                status, result = self.read("pull", number=7)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["body"]["mergeable"], extra.get("mergeable"))
                self.assertEqual(result["body"]["mergeable_state"], extra.get("mergeable_state"))
                self.assertNotIn("html_url", result["body"])

    def test_pull_projects_each_documented_mergeable_state(self):
        for state in (
            "behind",
            "blocked",
            "clean",
            "dirty",
            "draft",
            "has_hooks",
            "unknown",
            "unstable",
        ):
            with self.subTest(state=state):
                self.client.value = dict(
                    number=7,
                    state="open",
                    head={"sha": SHA},
                    base={"sha": BASE_SHA},
                    mergeable=True,
                    mergeable_state=state,
                )
                status, result = self.read("pull", number=7)
                self.assertEqual(status, 200, result)
                self.assertIs(result["body"]["mergeable"], True)
                self.assertEqual(result["body"]["mergeable_state"], state)

    def test_pull_rejects_unverifiable_merge_metadata(self):
        good = dict(
            number=7,
            state="open",
            head={"sha": SHA},
            base={"sha": BASE_SHA},
            mergeable=False,
            mergeable_state="dirty",
        )
        cases = [
            dict(good, mergeable="false"),
            dict(good, mergeable=0),
            dict(good, mergeable=["false"]),
            dict(good, mergeable={"value": False}),
            dict(good, mergeable_state="conflict"),
            dict(good, mergeable_state=""),
            dict(good, mergeable_state=["dirty"]),
            dict(good, mergeable_state={"state": "dirty"}),
            {k: v for k, v in good.items() if k != "base"},
            dict(good, base={"sha": "bad"}),
            dict(good, base=None),
        ]
        for value in cases:
            with self.subTest(value=value):
                self.client.value = value
                self.assertEqual(self.read("pull", number=7)[0], 502)

    def test_review_and_inline_comment_metadata_preserve_actor_time_and_head(self):
        self.client.value = [
            dict(
                id=81,
                state="CHANGES_REQUESTED",
                commit_id=SHA,
                submitted_at="2026-11-22T01:00:00Z",
                user={"login": "kody-ai[bot]"},
                body="excluded",
            )
        ]
        status, result = self.read("reviews", number=7, page=2)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"][0]["user"], {"login": "kody-ai[bot]"})
        self.assertEqual(result["body"][0]["submitted_at"], "2026-11-22T01:00:00Z")
        self.assertIn("/pulls/7/reviews?per_page=100&page=2", self.client.calls[-1][1])
        self.client.value = [
            dict(
                id=82,
                original_commit_id=SHA,
                created_at="2026-11-22T01:00:00Z",
                user={"login": "sentry[bot]"},
                body="excluded",
            )
        ]
        status, result = self.read("review_comments", number=7, page=1)
        self.assertEqual(status, 200, result)
        self.assertIn("/pulls/7/comments?", self.client.calls[-1][1])
        self.assertNotIn("/issues/", self.client.calls[-1][1])
        self.assertEqual(result["body"][0]["original_commit_id"], SHA)
        self.assertNotIn("excluded", json.dumps(result))

    def test_exact_commit_checks_use_checks_read_not_any_write(self):
        self.client.value = {
            "check_runs": [
                dict(
                    id=91,
                    head_sha=SHA,
                    name="Kody Code Review",
                    status="completed",
                    conclusion="success",
                    completed_at="2026-11-22T01:00:00Z",
                    app={"id": 413034, "slug": "kody-ai"},
                    output={"summary": "excluded"},
                )
            ]
        }
        status, result = self.read("check_runs", sha=SHA, page=1)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"]["check_runs"][0]["app"]["id"], 413034)
        self.assertEqual(self.minter.calls[-1][2], {"checks": "read", "metadata": "read"})
        self.assertNotIn("excluded", json.dumps(result))
        self.client.value["check_runs"][0]["head_sha"] = "b" * 40
        self.assertEqual(self.read("check_runs", sha=SHA, page=1)[0], 502)

    def test_native_claims_are_creation_metadata_not_upstream_pr_arguments(self):
        status, result = self.create(**CLAIMS)
        self.assertEqual(status, 200, result)
        self.assertEqual(
            self.client.creates[0][1],
            dict(base="main", head="native-branch", title="[CODEX] fixture", body="fixture"),
        )
        receipt = self.owners.lookup(REPO, 7)
        self.assertEqual(receipt["seat"], "swu-codex")
        self.assertEqual({k: receipt[k] for k in CLAIMS}, CLAIMS)
        status, result = self.read("creation_receipts")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"][0]["corr"], receipt["corr"])
        self.assertEqual(result["body"][0]["origin_generation_claim"], 3)
        self.assertNotIn("head", result["body"][0])
        # Exact create replay cannot create again or revise the immutable receipt.
        self.assertEqual(self.create(**CLAIMS)[0], 200)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.owners.lookup(REPO, 7), receipt)

    def test_partial_or_malformed_claims_are_rejected_before_creation(self):
        for claims in (
            {"origin_session_claim": CLAIMS["origin_session_claim"]},
            dict(CLAIMS, origin_generation_claim=True),
            dict(CLAIMS, origin_generation_claim=2**53),
            dict(CLAIMS, origin_letter_claim="A"),
        ):
            with self.subTest(claims=claims):
                self.assertEqual(self.create(**claims)[0], 400)
        self.assertEqual(self.client.creates, [])

    def test_ordinary_pr_remains_supported_but_gets_no_invented_native_origin(self):
        self.assertEqual(self.create()[0], 200)
        row = self.owners.lookup(REPO, 7)
        self.assertFalse(set(row) & set(CLAIMS))
        self.assertEqual(self.read("creation_receipts")[1]["body"][0]["seat"], "swu-codex")

    def test_first_confirmed_binding_cannot_be_replaced_and_backfills_are_not_receipts(self):
        self.create(**CLAIMS)
        self.owners.record(
            seat="swu-other",
            repo=REPO,
            number=7,
            head="b",
            base="main",
            origin=dict(CLAIMS, origin_generation_claim=9),
        )
        self.assertEqual(self.owners.lookup(REPO, 7)["origin_generation_claim"], 3)
        self.owners.record(
            seat="swu-other", repo=REPO, number=8, head="b", base="main", source="audit_backfill"
        )
        self.assertEqual(len(self.read("creation_receipts")[1]["body"]), 1)

    def test_missing_or_partial_owner_index_fails_closed_not_empty_census(self):
        self.assertEqual(self.read("creation_receipts")[0], 503)
        self.create(**CLAIMS)
        self.owners.skipped_lines = 1
        self.assertEqual(self.read("creation_receipts")[0], 503)

    def test_index_failure_never_replays_an_already_created_pr(self):
        self.owners.record = mock.Mock(side_effect=OSError("disk unavailable"))
        self.assertEqual(self.create(**CLAIMS)[0], 200)
        self.assertEqual(self.create(**CLAIMS)[0], 200)
        self.assertEqual(len(self.client.creates), 1)
        self.assertEqual(self.read("creation_receipts")[0], 503)

    def test_permission_ceiling_is_required_before_any_mint(self):
        self.policy._seats["swu-operator"]["mint_permissions"].pop("checks")
        self.assertEqual(self.read("check_runs", sha=SHA, page=1)[0], 403)
        self.assertEqual(self.minter.calls, [])

    def test_service_reads_are_never_saved_or_memoized_as_write_outcomes(self):
        self.client.value = []
        for _ in range(2):
            self.assertEqual(self.read("reviews", number=7, page=1)[0], 200)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.relay.outcomes._rows, {})
        self.assertFalse(SeatPolicy.is_write("service_read"))

    def test_existing_client_contract_crosses_the_real_http_handler(self):
        import importlib.util
        import threading
        from http.server import ThreadingHTTPServer

        from git_relay.server import make_handler

        source = Path(__file__).resolve().parents[3] / "scripts/review_alerts/github_read.py"
        spec = importlib.util.spec_from_file_location("review_reader_contract", source)
        reader_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader_module)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.relay))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            reader = reader_module.ServiceReadClient(
                "http://127.0.0.1:%d" % server.server_port, lambda: TOKEN
            )
            self.client.value = dict(
                number=7,
                state="open",
                head={"sha": SHA},
                base={"sha": BASE_SHA},
                mergeable=False,
                mergeable_state="dirty",
            )
            pull = reader.pull(REPO, 7)
            self.assertEqual(pull["head"]["sha"], SHA)
            self.assertEqual(pull["base"]["sha"], BASE_SHA)
            self.assertIs(pull["mergeable"], False)
            self.assertEqual(pull["mergeable_state"], "dirty")
            self.client.value = []
            self.assertEqual(reader.reviews(REPO, 7), [])
            self.assertEqual(reader.review_comments(REPO, 7), [])
            self.client.value = {"check_runs": []}
            self.assertEqual(reader.check_runs(REPO, SHA), [])
            self.create(**CLAIMS)
            rows = reader._read(dict(repo=REPO, verb="creation_receipts"))
            self.assertEqual(rows[0]["origin_session_claim"], CLAIMS["origin_session_claim"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_native_receipt_survives_restart_without_origin_reconstruction(self):
        self.create(**CLAIMS)
        original = self.owners.lookup(REPO, 7)
        restarted = PROwnerStore(self.owners._path)
        self.assertEqual(restarted.protected_for_repo(REPO), [original])
        self.assertEqual(restarted.lookup(REPO, 7)["origin_generation_claim"], 3)

    def test_existing_index_append_failure_is_blocked_after_restart_and_replay(self):
        self.create(**CLAIMS)
        self.assertEqual(self.read("creation_receipts")[0], 200)
        self.client.open_pr = mock.Mock(return_value={"number": 8})
        with mock.patch.object(self.owners, "record", side_effect=OSError("fixture")):
            self.assertEqual(self.create(**dict(CLAIMS, head="second-branch"))[0], 200)
            self.assertEqual(self.create(**dict(CLAIMS, head="second-branch"))[0], 200)
        self.assertEqual(self.client.open_pr.call_count, 1)
        self.assertEqual(self.read("creation_receipts")[0], 503)
        self.relay.pr_owners = PROwnerStore(self.owners._path)
        self.assertEqual(self.read("creation_receipts")[0], 503)
        # Unrelated successful append must not wash away the missing receipt.
        self.relay.pr_owners.record(
            seat="swu-codex", repo=REPO, number=9, head="third", base="main", corr="e" * 32
        )
        self.assertEqual(self.read("creation_receipts")[0], 503)

    def test_pending_fence_is_durable_before_upstream_and_success_resolves_it(self):
        original = self.client.open_pr

        def observe(repo, **kwargs):
            restarted = PROwnerStore(self.owners._path)
            with self.assertRaises(ValueError):
                restarted.protected_for_repo(REPO)
            return original(repo, **kwargs)

        self.client.open_pr = observe
        self.assertEqual(self.create(**CLAIMS)[0], 200)
        restarted = PROwnerStore(self.owners._path)
        self.assertEqual(len(restarted.protected_for_repo(REPO)), 1)
        self.assertFalse(restarted._pending_creations)

    def test_crashed_creation_keeps_census_blocked_without_inventing_owner(self):
        self.create(**CLAIMS)
        self.owners.begin_creation("f" * 32)
        restarted = PROwnerStore(self.owners._path)
        self.assertEqual(len(restarted.for_repo(REPO)), 1)
        with self.assertRaises(ValueError):
            restarted.protected_for_repo(REPO)

    def test_failed_precreate_fence_never_reaches_github(self):
        with mock.patch.object(self.owners, "begin_creation", side_effect=OSError("fixture")):
            self.assertEqual(self.create(**CLAIMS)[0], 503)
        self.assertEqual(self.client.creates, [])
        # This was a no-write refusal, not a stranded known-success create.
        self.assertEqual(self.create(**CLAIMS)[0], 200)
        self.assertEqual(len(self.client.creates), 1)

    def test_definite_refusal_resolves_fence_but_unknown_does_not(self):
        from git_relay.github_client import RelayGitHubError

        self.create(**CLAIMS)
        with mock.patch.object(
            self.client, "open_pr", side_effect=RelayGitHubError(429, "fixture")
        ):
            self.assertEqual(self.create(**dict(CLAIMS, head="rate-limited"))[0], 429)
        self.assertEqual(len(PROwnerStore(self.owners._path).protected_for_repo(REPO)), 1)
        with mock.patch.object(
            self.client, "open_pr", side_effect=RelayGitHubError(502, "fixture")
        ):
            self.assertEqual(self.create(**dict(CLAIMS, head="unknown"))[0], 502)
        with self.assertRaises(ValueError):
            PROwnerStore(self.owners._path).protected_for_repo(REPO)

    def test_failed_refusal_cleanup_retains_fence_across_restart(self):
        from git_relay.github_client import RelayGitHubError

        self.create(**CLAIMS)
        with (
            mock.patch.object(self.client, "open_pr", side_effect=RelayGitHubError(429, "fixture")),
            mock.patch.object(self.owners, "refuse_creation", side_effect=OSError("fixture")),
        ):
            self.assertEqual(self.create(**dict(CLAIMS, head="refused-cleanup-failed"))[0], 429)
        with self.assertRaises(ValueError):
            PROwnerStore(self.owners._path).protected_for_repo(REPO)

    def test_real_owner_write_failure_cannot_be_hidden_by_later_clean_append(self):
        self.create(**CLAIMS)
        self.client.open_pr = mock.Mock(return_value={"number": 8})
        write = self.owners._append_entry

        def fail_owner_row(row, key=None):
            if key is not None:
                with mock.patch("git_relay.pr_owners.os.open", side_effect=OSError("fixture")):
                    return write(row, key)
            return write(row, key)

        with mock.patch.object(self.owners, "_append_entry", side_effect=fail_owner_row):
            self.assertEqual(self.create(**dict(CLAIMS, head="disk-failure"))[0], 200)
        self.assertEqual(self.owners.load_error, "IndexWriteError")
        self.assertEqual(self.read("creation_receipts")[0], 503)
        restarted = PROwnerStore(self.owners._path)
        self.assertIsNone(restarted.load_error)
        with self.assertRaises(ValueError):
            restarted.protected_for_repo(REPO)

    def test_verified_no_follow_repository_move_resolves_fence_across_restart(self):
        import io
        from urllib.error import HTTPError

        from git_relay.github_client import GitHubClient

        self.create(**CLAIMS)
        calls = []

        def moved(req, **kwargs):
            calls.append(req.method)
            raise HTTPError(
                req.full_url,
                307,
                "fixture",
                {"Location": "https://api.github.com/repositories/123/pulls"},
                io.BytesIO(b"{}"),
            )

        self.relay.client = GitHubClient(TOKEN, opener=moved)
        status, answer = self.create(**dict(CLAIMS, head="old-repository"))
        self.assertEqual(status, 400, answer)
        self.assertEqual(answer["error"], "repository moved")
        self.assertEqual(answer["repository_id"], 123)
        self.assertFalse(answer["retryable"])
        self.assertEqual(calls, ["POST"])
        self.assertEqual(self.read("creation_receipts")[0], 200)
        restarted = PROwnerStore(self.owners._path)
        self.assertEqual(len(restarted.protected_for_repo(REPO)), 1)
        self.assertFalse(restarted._pending_creations)

    def test_unproven_redirects_and_unknown_errors_keep_each_durable_fence(self):
        from git_relay.github_client import RelayGitHubError

        self.create(**CLAIMS)
        cases = [
            RelayGitHubError(301, "fixture"),
            RelayGitHubError(307, "fixture", code="repository_moved"),
            RelayGitHubError(502, "fixture", code="repository_moved", moved_repository_id=123),
            RelayGitHubError(0, "fixture"),
            RelayGitHubError(408, "fixture"),
        ]
        for i, failure in enumerate(cases):
            with self.subTest(status=failure.status):
                before = len(self.owners._pending_creations)
                with mock.patch.object(self.client, "open_pr", side_effect=failure):
                    self.create(**dict(CLAIMS, head="uncertain-%d" % i))
                self.assertEqual(len(self.owners._pending_creations), before + 1)
                restarted = PROwnerStore(self.owners._path)
                self.assertEqual(len(restarted._pending_creations), before + 1)
                with self.assertRaises(ValueError):
                    restarted.protected_for_repo(REPO)

    def test_actual_post_create_readback_redirect_or_refusal_stays_unknown(self):
        from git_relay.github_client import GitHubClient, RelayGitHubError

        self.create(**CLAIMS)
        for status in (301, 404, 429):
            with self.subTest(readback_status=status):
                upstream = GitHubClient(TOKEN)
                upstream._sleep = lambda _: None
                post_count = []

                def call(method, path, *args, **kwargs):
                    if method == "POST":
                        post_count.append(path)
                        return {"number": 8, "html_url": "https://github.com/%s/pull/8" % REPO}
                    raise RelayGitHubError(
                        status,
                        "fixture",
                        code="repository_moved" if status == 301 else None,
                        moved_repository_id=123 if status == 301 else None,
                    )

                upstream._call = call
                self.relay.client = upstream
                before = len(self.owners._pending_creations)
                answer_status, answer = self.create(**dict(CLAIMS, head="readback-%d" % status))
                self.assertEqual(answer_status, 502, answer)
                self.assertEqual(answer["readback_status"], status)
                self.assertEqual(answer["outcome"], "unknown")
                self.assertEqual(len(post_count), 1)
                restarted = PROwnerStore(self.owners._path)
                self.assertEqual(len(restarted._pending_creations), before + 1)
                with self.assertRaises(ValueError):
                    restarted.protected_for_repo(REPO)

    def test_corrupt_tail_remains_held_after_later_append_and_restart(self):
        self.create(**CLAIMS)
        original = self.owners.lookup(REPO, 7)
        with open(self.owners._path, "ab") as handle:
            handle.write(b'{"owner_index_event":')
        damaged = PROwnerStore(self.owners._path)
        self.assertEqual(damaged.skipped_lines, 1)
        with self.assertRaises(ValueError):
            damaged.protected_for_repo(REPO)
        damaged.record(seat="swu-codex", repo=REPO, number=9, head="later", base="main")
        with self.assertRaises(ValueError):
            damaged.protected_for_repo(REPO)
        restarted = PROwnerStore(self.owners._path)
        self.assertEqual(restarted.lookup(REPO, 7), original)
        self.assertEqual(restarted.skipped_lines, 1)
        with self.assertRaises(ValueError):
            restarted.protected_for_repo(REPO)
