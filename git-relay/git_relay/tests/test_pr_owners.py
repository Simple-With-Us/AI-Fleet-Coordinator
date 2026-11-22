"""PR-ownership index and bracketed title-tag convention.

Covers:
  - the store's durable append, reload, lookup and merge rules;
  - rebuild from the audit log (confirmed and unconfirmed creations);
  - the relay's open_pr hook:  who created it, recorded at creation;
  - the title convention:  a seat's own bracket leads every created title.
"""

import json
import tempfile
import unittest
from pathlib import Path

from git_relay.audit import AuditLog
from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore
from git_relay.policy import PolicyError, SeatPolicy
from git_relay.pr_owners import BACKFILL, CONFIRMED, PROwnerStore, rebuild
from git_relay.server import Relay

ECHO_TOKEN = "e" * 32
MM_TOKEN = "m" * 32
ARGS = {
    "repo": "example/fixture",
    "base": "main",
    "head": "echo/fixture-change",
    "title": "Title",
    "body": "Body",
}
POLICY = json.dumps(
    {
        "swu-echo": {
            "token": ECHO_TOKEN,
            "actions": ["open_pr", "edit_pr"],
            "repos": [ARGS["repo"]],
            "app_slug": "swu-echo",
            "mint_permissions": {"pull_requests": "write"},
        },
        "swu-mm": {
            "token": MM_TOKEN,
            "actions": ["open_pr"],
            "repos": [ARGS["repo"]],
            "title_tag": "MM",
        },
    }
)


class Client:
    """Offline GitHubClient stand-in:  one open_pr, scripted outcome."""

    def __init__(self, number=1, error=None):
        self.calls = []
        self.number = number
        self.error = error

    def open_pr(self, repo, base, head, title, body, **kwargs):
        self.calls.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        if self.error is not None:
            raise self.error
        return {
            "number": self.number,
            "url": f"https://github.com/{repo}/pull/{self.number}",
            "verified": True,
        }

    def edit_pr(self, repo, number, title=None, body=None, **kwargs):
        self.calls.append({"repo": repo, "number": number, "title": title, "body": body})
        return {"number": number, "url": f"https://github.com/{repo}/pull/{number}"}


class FakeMinter:
    def token_for(self, *args, **kwargs):
        return "synthetic-minted-token", 9999999999


def make_relay(directory, client, minter=None, **kwargs):
    # The creation fence binds every create to a durable outcome
    # correlation, so the relay needs a real outcome store.
    kwargs.setdefault("outcomes", OutcomeStore(str(Path(directory) / "outcomes.json")))
    return Relay(
        SeatPolicy(POLICY),
        client,
        AuditLog(str(Path(directory) / "audit.jsonl")),
        app_minter=minter,
        pr_owners=PROwnerStore(str(Path(directory) / "owners.jsonl")),
        **kwargs,
    )


def audit_rows(directory):
    path = Path(directory) / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class StoreTests(unittest.TestCase):
    def test_record_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "owners.jsonl")
            store = PROwnerStore(path)
            store.record(
                seat="swu-echo",
                repo="o/r",
                number=7,
                head="echo/x",
                base="main",
                corr="c" * 8,
            )
            again = PROwnerStore(path)
            row = again.lookup("o/r", 7)
            self.assertEqual(row["seat"], "swu-echo")
            self.assertEqual(row["number"], 7)
            self.assertIs(row["confirmed"], True)
            self.assertEqual(row["source"], CONFIRMED)
            self.assertIsNone(again.lookup("o/r", 8))
            self.assertIsNone(again.lookup("o/other", 7))

    def test_confirmed_upgrades_unconfirmed_and_never_downgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PROwnerStore(str(Path(directory) / "owners.jsonl"))
            store.record(
                seat="swu-echo", repo="o/r", number=3, head="h", base="main", confirmed=False
            )
            self.assertFalse(store.lookup("o/r", 3)["confirmed"])
            store.record(seat="swu-echo", repo="o/r", number=3, head="h", base="main")
            self.assertTrue(store.lookup("o/r", 3)["confirmed"])
            store.record(
                seat="swu-mm", repo="o/r", number=3, head="h2", base="main", confirmed=False
            )
            row = store.lookup("o/r", 3)
            self.assertTrue(row["confirmed"])
            self.assertEqual(row["seat"], "swu-echo")

    def test_bad_lines_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owners.jsonl"
            path.write_text(
                '{"repo": "o/r", "number": 1, "seat": "swu-echo", "head": "h",'
                ' "base": "main", "confirmed": true, "source": "open_pr", "ts": "t",'
                ' "corr": null}\n'
                "not json\n"
                '{"repo": "", "number": 2}\n'
            )
            store = PROwnerStore(str(path))
            self.assertEqual(len(store), 1)
            self.assertEqual(store.skipped_lines, 2)

    def test_for_seat_and_for_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PROwnerStore(str(Path(directory) / "owners.jsonl"))
            store.record(seat="swu-echo", repo="o/r", number=1, head="h", base="main")
            store.record(seat="swu-mm", repo="o/r", number=2, head="h", base="main")
            store.record(seat="swu-echo", repo="o/s", number=3, head="h", base="main")
            self.assertEqual(sorted(r["number"] for r in store.for_seat("swu-echo")), [1, 3])
            self.assertEqual([r["number"] for r in store.for_repo("o/s")], [3])

    def test_an_invalid_direct_claim_is_refused_before_a_row_is_stored(self):
        # The direct claim columns go through the same shape checks as the
        # shared origin dict:  a row that held an invalid claim would make
        # the protected census raise for the whole repository.
        with tempfile.TemporaryDirectory() as directory:
            store = PROwnerStore(str(Path(directory) / "owners.jsonl"))
            with self.assertRaises(ValueError):
                store.record(
                    seat="swu-echo",
                    repo="o/r",
                    number=1,
                    head="h",
                    base="main",
                    origin_session_claim="not a session",
                )
            self.assertEqual(len(store), 0)
            self.assertIsNone(store.lookup("o/r", 1))

    def test_confirmed_for_repo_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PROwnerStore(str(Path(directory) / "owners.jsonl"))
            store.record(seat="swu-echo", repo="o/r", number=1, head="h", base="main")
            store.record(
                seat="swu-echo", repo="o/r", number=2, head="h", base="main", confirmed=False
            )
            store.record(
                seat="swu-echo",
                repo="o/r",
                number=3,
                head="h",
                base="main",
                source="audit_backfill",
            )
            # Only a confirmed open_pr row is an eligible creation receipt.
            rows, overflow = store.confirmed_for_repo("O/R", 10)
            self.assertEqual([r["number"] for r in rows], [1])
            self.assertFalse(overflow)
            # The bound is decided before the rows are built.
            rows, overflow = store.confirmed_for_repo("o/r", 1)
            self.assertEqual(len(rows), 1)
            self.assertFalse(overflow)
            store.record(seat="swu-echo", repo="o/r", number=4, head="h", base="main")
            rows, overflow = store.confirmed_for_repo("o/r", 1)
            self.assertEqual(len(rows), 1)
            self.assertTrue(overflow)


class RelayHookTests(unittest.TestCase):
    def open_pr(self, directory, client, token=ECHO_TOKEN, extra=None, **kwargs):
        relay = make_relay(directory, client, **kwargs)
        response = relay.handle(token, "open_pr", {**ARGS, **(extra or {})})
        return relay, response

    def test_success_records_owner_and_keeps_conforming_title(self):
        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(
                directory, Client(), extra={"title": "[ECHO] Add the thing"}
            )
            self.assertEqual(response[0], 200)
            self.assertEqual(relay.client.calls[0]["title"], "[ECHO] Add the thing")
            row = relay.pr_owners.lookup(ARGS["repo"], 1)
            self.assertEqual(row["seat"], "swu-echo")
            self.assertEqual(row["head"], ARGS["head"])
            self.assertIs(row["confirmed"], True)
            self.assertIsNotNone(row["corr"])
            rows = [r for r in audit_rows(directory) if r["action"] == "open_pr"]
            self.assertIs(rows[-1].get("owner_recorded"), True)
            self.assertNotIn("title_prefixed", rows[-1])
            # The title itself stays hashed in the audit row.
            self.assertIn("title_sha256", rows[-1])
            self.assertNotIn("title", rows[-1])

    def test_missing_bracket_gets_the_seat_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(directory, Client())
            self.assertEqual(response[0], 200)
            self.assertEqual(relay.client.calls[0]["title"], "[ECHO] Title")
            rows = [r for r in audit_rows(directory) if r["action"] == "open_pr"]
            self.assertIs(rows[-1].get("title_prefixed"), True)

    def test_multithreaded_session_bracket_is_preserved(self):
        # The current convention is the space form:  [SEAT ??.#.?].
        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(
                directory, Client(), token=MM_TOKEN, extra={"title": "[MM SS.2.M] fix it"}
            )
            self.assertEqual(response[0], 200)
            self.assertEqual(relay.client.calls[0]["title"], "[MM SS.2.M] fix it")
            self.assertEqual(relay.pr_owners.lookup(ARGS["repo"], 1)["seat"], "swu-mm")

    def test_legacy_colon_session_bracket_still_conforms(self):
        # Pre-existing PRs use [MM: SS.2.M];  the colon is accepted as legacy
        # input only - it is not the current convention.
        policy = SeatPolicy(POLICY)
        self.assertEqual(
            policy.apply_title_tag("swu-mm", "[MM: SS.2.M] fix it"),
            ("[MM: SS.2.M] fix it", False),
        )

    def test_other_bracket_gets_the_creators_tag_first(self):
        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(
                directory, Client(), extra={"title": "[ZC: LL.2.S] claimed"}
            )
            self.assertEqual(response[0], 200)
            self.assertEqual(relay.client.calls[0]["title"], "[ECHO] [ZC: LL.2.S] claimed")

    def test_unknown_outcome_records_unconfirmed_claim(self):
        error = RelayGitHubError(502, "outcome unknown", unconfirmed_number=41)
        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(directory, Client(error=error))
            self.assertEqual(response[0], 502)
            self.assertEqual(response[1]["outcome"], "unknown")
            row = relay.pr_owners.lookup(ARGS["repo"], 41)
            self.assertEqual(row["seat"], "swu-echo")
            self.assertIs(row["confirmed"], False)
            rows = [r for r in audit_rows(directory) if r["action"] == "open_pr"]
            self.assertEqual(rows[-1].get("unconfirmed_number"), 41)
            self.assertIs(rows[-1].get("owner_recorded"), True)

    def test_store_failure_refuses_the_creation_before_it_reaches_github(self):
        # The creation fence is durable BEFORE the upstream create:  an
        # owner index that cannot be written refuses the create, so no PR is
        # ever created that no row can bind.
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            relay = make_relay(directory, client)
            # A store whose file cannot be written:  the fence raises OSError.
            relay.pr_owners = PROwnerStore(str(Path(directory) / "missing" / "owners.jsonl"))
            response = relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            self.assertEqual(response[0], 503)
            self.assertEqual(len(client.calls), 0)

    def test_idempotent_replay_records_once(self):
        with tempfile.TemporaryDirectory() as directory:
            relay, first = self.open_pr(directory, Client())
            second = relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            self.assertEqual(second, first)
            self.assertEqual(len(relay.pr_owners), 1)
            self.assertEqual(len(relay.client.calls), 1)

    def test_idempotency_key_binds_the_original_title(self):
        """A pre-convention pending row must match its post-deploy retry."""
        from git_relay.outcomes import key_for

        with tempfile.TemporaryDirectory() as directory:
            relay, response = self.open_pr(directory, Client())
            self.assertEqual(response[0], 200)
            # Upstream saw the prefixed title...
            self.assertEqual(relay.client.calls[0]["title"], "[ECHO] Title")
            # ...but the outcome row is keyed on the seat's original args.
            original_key = key_for("swu-echo", "open_pr", dict(ARGS))
            self.assertIsNotNone(relay.outcomes.corr_of(original_key))

    def test_edit_pr_retitle_is_tagged_and_keyed_on_original(self):
        from git_relay.outcomes import key_for

        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client(), minter=FakeMinter())
            status, _ = relay.handle(
                ECHO_TOKEN,
                "edit_pr",
                {"repo": ARGS["repo"], "number": 9, "title": "renamed"},
            )
            self.assertEqual(status, 200, _)
            self.assertEqual(relay.client.calls[-1]["title"], "[ECHO] renamed")
            original_key = key_for(
                "swu-echo",
                "edit_pr",
                {"repo": ARGS["repo"], "number": 9, "title": "renamed"},
            )
            self.assertIsNotNone(relay.outcomes.corr_of(original_key))
            # A retitle carrying the seat's own bracket passes through.
            relay.handle(
                ECHO_TOKEN,
                "edit_pr",
                {"repo": ARGS["repo"], "number": 9, "title": "[ECHO] renamed again"},
            )
            self.assertEqual(relay.client.calls[-1]["title"], "[ECHO] renamed again")
            # edit_pr writes no ownership row.
            self.assertEqual(len(relay.pr_owners), 0)

    def test_unrelated_actions_have_no_owner_row(self):
        with tempfile.TemporaryDirectory() as directory:
            relay, _ = self.open_pr(directory, Client())
            self.assertEqual(len(relay.pr_owners), 1)
            # A denied call records audit only.
            status, _ = relay.handle(ECHO_TOKEN, "merge_pr", {"repo": ARGS["repo"], "number": 1})
            self.assertEqual(status, 403)
            self.assertEqual(len(relay.pr_owners), 1)


class RebuildTests(unittest.TestCase):
    def test_rebuild_from_audit_recovers_confirmed_and_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client(number=1))
            relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            relay.client = Client(number=2)
            relay.handle(MM_TOKEN, "open_pr", {**ARGS, "head": "mm/other", "title": "Other"})
            error = RelayGitHubError(502, "outcome unknown", unconfirmed_number=55)
            relay.client = Client(error=error)
            relay.handle(ECHO_TOKEN, "open_pr", {**ARGS, "head": "echo/third"})
            # A fresh store rebuilt only from the audit log.
            fresh = PROwnerStore(str(Path(directory) / "rebuilt.jsonl"))
            counts = rebuild(str(Path(directory) / "audit.jsonl"), fresh)
            self.assertEqual(counts["audit_rows"], 3)
            self.assertEqual(counts["added"], 3)
            first = fresh.lookup(ARGS["repo"], 1)
            self.assertEqual(first["seat"], "swu-echo")
            self.assertIs(first["confirmed"], True)
            self.assertEqual(first["source"], BACKFILL)
            second = fresh.lookup(ARGS["repo"], 2)
            self.assertEqual(second["seat"], "swu-mm")
            third = fresh.lookup(ARGS["repo"], 55)
            self.assertEqual(third["seat"], "swu-echo")
            self.assertIs(third["confirmed"], False)

    def test_rebuild_preserves_existing_confirmed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            # Rebuild into the LIVE store:  the confirmed live row is kept.
            counts = rebuild(str(Path(directory) / "audit.jsonl"), relay.pr_owners)
            self.assertEqual(counts["added"], 0)
            self.assertEqual(counts["skipped"], 1)
            self.assertEqual(relay.pr_owners.lookup(ARGS["repo"], 1)["source"], CONFIRMED)


class StoreRobustnessTests(unittest.TestCase):
    def test_unreadable_or_invalid_store_never_stops_the_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            # A directory at the store path:  load degrades to empty.
            store = PROwnerStore(str(Path(directory) / "owners.jsonl") + "/")
            store = PROwnerStore(directory)  # a directory, not a file
            self.assertEqual(store.load_error, "IsADirectoryError")
            self.assertEqual(len(store), 0)
            # Invalid UTF-8 bytes:  load degrades, counts nothing.
            bad = Path(directory) / "bad.jsonl"
            bad.write_bytes(b'{"repo": "o/r"\xff\xfe')
            store = PROwnerStore(str(bad))
            self.assertIsNotNone(store.load_error)
            # And the relay still runs:  it refuses the create that the
            # broken index cannot bind, and stays up for every other call.
            client = Client()
            relay = make_relay(directory, client)
            relay.pr_owners = PROwnerStore(directory)
            response = relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            self.assertEqual(response[0], 503)
            self.assertEqual(len(client.calls), 0)

    def test_torn_tail_is_terminated_before_the_next_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owners.jsonl"
            path.write_text(
                '{"repo": "o/r", "number": 1, "seat": "swu-echo", "head": "h",'
                ' "base": "main", "confirmed": true, "source": "open_pr", "ts": "t",'
                ' "corr": null}\n{"repo": "o/r", "number":'
            )
            store = PROwnerStore(str(path))
            self.assertEqual(len(store), 1)
            self.assertEqual(store.skipped_lines, 1)
            store.record(seat="swu-mm", repo="o/r", number=2, head="h", base="main")
            # The torn fragment stays (and stays skipped on load), but the
            # new row lands on its own line instead of gluing onto it.
            lines = [l for l in path.read_text().splitlines() if l.strip()]
            self.assertEqual(len(lines), 3)  # valid row, torn fragment, new row
            self.assertEqual(json.loads(lines[-1])["number"], 2)
            self.assertIsNotNone(store.lookup("o/r", 2))
            reloaded = PROwnerStore(str(path))
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded.skipped_lines, 1)

    def test_repo_keys_fold_case(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PROwnerStore(str(Path(directory) / "owners.jsonl"))
            store.record(
                seat="swu-echo", repo="Example/FixtureRepo", number=5, head="h", base="main"
            )
            self.assertIsNotNone(store.lookup("example/fixturerepo", 5))
            self.assertEqual(len(store.for_repo("EXAMPLE/FIXTUREREPO")), 1)
            # The row keeps the creation-time spelling.
            self.assertEqual(store.lookup("example/fixturerepo", 5)["repo"], "Example/FixtureRepo")


class RebuildConflictTests(unittest.TestCase):
    def test_conflicting_confirmed_binding_is_counted_and_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client(number=1))
            relay.handle(ECHO_TOKEN, "open_pr", ARGS)
            # A pre-existing confirmed row for a DIFFERENT seat:  rebuild
            # surfaces the conflict and keeps the first binding.
            relay.pr_owners.lookup(ARGS["repo"], 1)["seat"]  # sanity
            store = relay.pr_owners
            store._by_pr[(ARGS["repo"].casefold(), 1)]["seat"] = "swu-mm"
            counts = rebuild(str(Path(directory) / "audit.jsonl"), store)
            self.assertEqual(counts["conflicts"], 1)
            self.assertEqual(store.lookup(ARGS["repo"], 1)["seat"], "swu-mm")


class CliTests(unittest.TestCase):
    def test_lookup_names_owner_only_when_confirmed(self):
        import io
        from contextlib import redirect_stdout

        from git_relay.pr_owners import main

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "owners.jsonl")
            store = PROwnerStore(path)
            store.record(seat="swu-echo", repo="o/r", number=1, head="h", base="main")
            store.record(
                seat="swu-mm", repo="o/r", number=2, head="h", base="main", confirmed=False
            )
            out = io.StringIO()
            with redirect_stdout(out):
                main(["lookup", "--store", path, "o/r", "1"])
            answer = json.loads(out.getvalue())
            self.assertEqual(answer["owner"], "swu-echo")
            self.assertIs(answer["confirmed"], True)
            out = io.StringIO()
            with redirect_stdout(out):
                main(["lookup", "--store", path, "o/r", "2"])
            answer = json.loads(out.getvalue())
            self.assertIsNone(answer["owner"])
            self.assertIs(answer["confirmed"], False)
            self.assertEqual(answer["record"]["seat"], "swu-mm")
            out = io.StringIO()
            with redirect_stdout(out):
                main(["lookup", "--store", path, "o/r", "3"])
            self.assertIsNone(json.loads(out.getvalue())["owner"])


class TitleTagPolicyTests(unittest.TestCase):
    def test_tag_derivation_and_override(self):
        policy = SeatPolicy(POLICY)
        self.assertEqual(policy.title_tag("swu-echo"), "ECHO")
        self.assertEqual(policy.title_tag("swu-mm"), "MM")
        self.assertEqual(policy.apply_title_tag("swu-echo", "plain"), ("[ECHO] plain", True))
        self.assertEqual(policy.apply_title_tag("swu-echo", "[ECHO] done"), ("[ECHO] done", False))
        self.assertEqual(
            policy.apply_title_tag("swu-mm", "[MM 📁AFC] wide"), ("[MM 📁AFC] wide", False)
        )

    def test_multiword_tag_matches_whole(self):
        raw = json.dumps(
            {
                "swu-mm": {
                    "token": MM_TOKEN,
                    "actions": ["open_pr"],
                    "repos": ["o/r"],
                    "title_tag": "MM SS",
                }
            }
        )
        policy = SeatPolicy(raw)
        self.assertEqual(policy.apply_title_tag("swu-mm", "[MM SS] x"), ("[MM SS] x", False))
        # Space form (current convention) after a multiword tag.
        self.assertEqual(
            policy.apply_title_tag("swu-mm", "[MM SS HH.1.M] x"), ("[MM SS HH.1.M] x", False)
        )
        # Colon form after a multiword tag is legacy-tolerated.
        self.assertEqual(
            policy.apply_title_tag("swu-mm", "[MM SS: HH.1.M] x"), ("[MM SS: HH.1.M] x", False)
        )
        self.assertEqual(policy.apply_title_tag("swu-mm", "[MM] x"), ("[MM SS] [MM] x", True))

    def test_case_and_spacing_are_canonicalized(self):
        policy = SeatPolicy(POLICY)
        self.assertEqual(policy.apply_title_tag("swu-echo", "[echo] x"), ("[ECHO] x", True))
        self.assertEqual(policy.apply_title_tag("swu-echo", "[ ECHO] x"), ("[ECHO] x", True))
        # A different-tag bracket is not a boundary match:  dash is not a separator.
        self.assertEqual(
            policy.apply_title_tag("swu-echo", "[ECHO-2] x"), ("[ECHO] [ECHO-2] x", True)
        )

    def test_underivable_tag_requires_config(self):
        raw = json.dumps(
            {
                "swu-grok.bot": {
                    "token": "g" * 32,
                    "actions": ["open_pr"],
                    "repos": ["o/r"],
                }
            }
        )
        with self.assertRaises(PolicyError):
            SeatPolicy(raw)

    @staticmethod
    def _two_seat_policy(tag_a, tag_b):
        return json.dumps(
            {
                "swu-echo": {
                    "token": ECHO_TOKEN,
                    "actions": ["open_pr"],
                    "repos": ["o/r"],
                    "title_tag": tag_a,
                },
                "swu-mm": {
                    "token": MM_TOKEN,
                    "actions": ["open_pr"],
                    "repos": ["o/r"],
                    "title_tag": tag_b,
                },
            }
        )

    def test_duplicate_tag_refused_at_load(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(self._two_seat_policy("MM", "MM"))

    def test_tag_prefix_collision_refused_at_load(self):
        # "MM" would accept "[MM SS] x" (space boundary) as its own bracket,
        # colliding with the "MM SS" seat;  same for the colon boundary.
        with self.assertRaises(PolicyError):
            SeatPolicy(self._two_seat_policy("MM", "MM SS"))
        with self.assertRaises(PolicyError):
            SeatPolicy(self._two_seat_policy("MM SS", "MM"))

    def test_tag_dash_extension_is_not_a_collision(self):
        # Dash is not a boundary:  "[MM-2] x" never conforms for tag "MM",
        # so the pair loads and each seat keeps its own brackets.
        policy = SeatPolicy(self._two_seat_policy("MM", "MM-2"))
        # swu-echo's tag is "MM":  "[MM-2] x" is not its bracket (dash is not
        # a boundary), so its own tag goes in front.
        self.assertEqual(policy.apply_title_tag("swu-echo", "[MM-2] x"), ("[MM] [MM-2] x", True))
        # swu-mm's tag is "MM-2":  its own bracket conforms untouched.
        self.assertEqual(policy.apply_title_tag("swu-mm", "[MM-2] x"), ("[MM-2] x", False))

    def test_invalid_title_tag_refused_at_load(self):
        for bad in ("", "lowercase ok?", "[ECHO]", "x" * 33, 5):
            with self.subTest(bad=bad):
                raw = json.dumps(
                    {
                        "swu-echo": {
                            "token": ECHO_TOKEN,
                            "actions": ["open_pr"],
                            "repos": ["o/r"],
                            "title_tag": bad,
                        }
                    }
                )
                with self.assertRaises(PolicyError):
                    SeatPolicy(raw)


if __name__ == "__main__":
    unittest.main()
