"""Offline lifecycle contract tests. No provider calls or real PR toggles."""

import copy
import json
import tempfile
import unittest
from types import SimpleNamespace

from git_relay.outcomes import OutcomeStore
from git_relay.pr_lifecycle import Lifecycle
from git_relay.server import _validate_args

REPO = "example/repo"
SHA = "a" * 40
STAMP = "2026-11-04T06:00:00Z"


def pull(state="open", stamp=STAMP):
    branch = {"ref": "feature", "sha": SHA, "repo": {"id": 1, "full_name": REPO}}
    return {
        "id": 10,
        "node_id": "PR_123456",
        "number": 3,
        "html_url": f"https://github.com/{REPO}/pull/3",
        "state": state,
        "draft": False,
        "merged": False,
        "updated_at": stamp,
        "head": copy.deepcopy(branch),
        "base": {**copy.deepcopy(branch), "ref": "main"},
    }


class Client:
    def __init__(self, row=None):
        self.row = row or pull()
        self.calls = []
        self.bad_receipt = False
        self.bad_readback = False
        self.patched = False
        self.failure = None

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload, bearer))
        if method in ("PATCH", "POST"):
            self.patched = True
            if self.failure:
                raise self.failure
            if method == "POST":
                self.row.update(title=payload["title"], body=payload["body"])
            self.row["state"] = payload.get("state", "open")
            self.row["draft"] = payload.get("draft", self.row["draft"])
            self.row["updated_at"] = "2026-11-04T06:00:01Z"
            return {} if self.bad_receipt else copy.deepcopy(self.row)
        if "/git/ref/" in path:
            ref = "main" if path.endswith("/main") else "feature"
            return {"ref": "refs/heads/" + ref, "object": {"type": "commit", "sha": SHA}}
        if path == f"/repos/{REPO}":
            return {"id": 1, "full_name": REPO}
        row = copy.deepcopy(self.row)
        if self.bad_readback and self.patched:
            row["id"] = 99
        return row


class Minter:
    def __init__(self):
        self.calls = []

    def token_for(self, slug, repo, permissions):
        self.calls.append((slug, repo, permissions))
        return "read-token" if permissions.get(
            "pull_requests"
        ) == "read" else "write-token", 9999999999


class Tests(unittest.TestCase):
    def relay(self, client=None, path=None):
        return SimpleNamespace(
            client=client or Client(),
            outcomes=OutcomeStore(path),
            app_minter=Minter(),
            policy=SimpleNamespace(
                app_identity=lambda seat: (seat, {"pull_requests": "write", "contents": "write"})
            ),
        )

    def args(self, **kw):
        return {
            "repo": REPO,
            "number": 3,
            "state": "closed",
            "expected_head_sha": SHA,
            "expected_updated_at": STAMP,
            **kw,
        }

    def run_state(self, r, **kw):
        return Lifecycle(r, "swu-echo", "edit_pr", self.args(**kw)).execute()

    def test_close_and_own_app_tuple(self):
        r = self.relay()
        status, out = self.run_state(r)
        self.assertEqual(status, 200)
        self.assertTrue(out["verified"])
        self.assertEqual(out["state"], "closed")
        self.assertEqual(r.app_minter.calls[-1][2], {"pull_requests": "write"})
        self.assertEqual([x[0] for x in r.client.calls].count("PATCH"), 1)
        self.assertFalse(any("DELETE" == x[0] for x in r.client.calls))

    def test_bad_merged_and_identity_before_write_mint(self):
        variants = [
            lambda x: x.pop("merged"),
            lambda x: x.update(merged=True),
            lambda x: x.update(merged=None),
            lambda x: x.update(merged=0),
            lambda x: x.update(number=4),
            lambda x: x.update(draft=0),
            lambda x: x["base"]["repo"].update(full_name="wrong/repo"),
            lambda x: x["head"].update(repo=None),
        ]
        for change in variants:
            with self.subTest(change=change):
                row = pull()
                change(row)
                r = self.relay(Client(row))
                self.assertNotEqual(self.run_state(r)[0], 200)
                self.assertFalse(
                    any(x[2].get("pull_requests") == "write" for x in r.app_minter.calls)
                )
                self.assertFalse(r.client.patched)

    def test_noop_requires_unmerged_identity(self):
        r = self.relay(Client(pull("closed")))
        status, out = self.run_state(r)
        self.assertEqual(status, 200)
        self.assertTrue(out["noop"])
        self.assertFalse(r.client.patched)
        r = self.relay(Client(pull("closed")))
        r.client.row["merged"] = True
        self.assertNotEqual(self.run_state(r)[0], 200)

    def test_cycles_are_real_operations(self):
        r = self.relay()
        self.assertEqual(self.run_state(r)[0], 200)
        self.assertEqual(
            self.run_state(r, state="open", expected_updated_at=r.client.row["updated_at"])[0], 200
        )
        r.client.row["updated_at"] = "2026-11-04T06:00:02Z"
        self.assertEqual(self.run_state(r, expected_updated_at=r.client.row["updated_at"])[0], 200)
        self.assertEqual(sum(x[0] == "PATCH" for x in r.client.calls), 3)

    def test_unknown_survives_restart_changed_args_and_actor(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/outcomes.json"
            r = self.relay(path=path)
            r.client.bad_receipt = True
            self.assertEqual(self.run_state(r)[1]["outcome"], "unknown")
            second = self.relay(r.client, path)
            status, out = Lifecycle(
                second,
                "swu-other",
                "edit_pr",
                self.args(state="open", expected_updated_at="2026-11-04T06:00:01Z"),
            ).execute()
            self.assertNotEqual(status, 200)
            self.assertEqual(sum(x[0] == "PATCH" for x in r.client.calls), 1)
            self.assertIn("unknown", [v["state"] for v in second.outcomes._rows.values()])

    def test_readback_mismatch_unknown(self):
        r = self.relay()
        r.client.bad_readback = True
        self.assertEqual(self.run_state(r)[1]["outcome"], "unknown")

    def test_strict_shape(self):
        for fields in ({"state": True}, {"state": "merged"}, {"state": "closed"}, {"draft": 1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                _validate_args("edit_pr", {"repo": REPO, "number": 3, **fields})

    def test_atomic_create_draft(self):
        r = self.relay()
        a = {
            "repo": REPO,
            "head": "feature",
            "base": "main",
            "title": "x",
            "body": "y",
            "draft": True,
        }
        status, out = Lifecycle(r, "swu-echo", "open_pr", a).execute()
        self.assertEqual(status, 200)
        self.assertTrue(out["draft"])
        writes = [x for x in r.client.calls if x[0] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0][2]["draft"])

    def test_create_unknown_blocks_alias(self):
        r = self.relay()
        r.client.bad_readback = True
        a = {
            "repo": REPO,
            "head": "feature",
            "base": "main",
            "title": "x",
            "body": "y",
            "draft": True,
        }
        self.assertEqual(Lifecycle(r, "swu-echo", "open_pr", a).execute()[1]["outcome"], "unknown")
        a.update(title="different", draft=False)
        self.assertNotEqual(Lifecycle(r, "swu-echo", "open_pr", a).execute()[0], 200)
        self.assertEqual(sum(x[0] == "POST" for x in r.client.calls), 1)


class UpgradeTests(Tests):
    def test_legacy_unknown_blocks_creation(self):
        from git_relay.outcomes import key_for

        with tempfile.TemporaryDirectory() as d:
            r = self.relay(path=d + "/outcomes.json")
            a = {"repo": REPO, "head": "feature", "base": "main", "title": "old", "body": "old"}
            old = key_for("swu-echo", "open_pr", a)
            r.outcomes.begin(old)
            r.outcomes.finish(old, "unknown")
            r.outcomes = OutcomeStore(d + "/outcomes.json")
            a.update(title="new", draft=True)
            self.assertEqual(Lifecycle(r, "swu-other", "open_pr", a).execute()[0], 409)
            self.assertFalse(r.client.patched)

    def test_changed_creation_intent_is_conflict_not_old_success(self):
        r = self.relay()
        a = {
            "repo": REPO,
            "head": "feature",
            "base": "main",
            "title": "x",
            "body": "y",
            "draft": True,
        }
        self.assertEqual(Lifecycle(r, "swu-echo", "open_pr", a).execute()[0], 200)
        a.update(title="new", body="new", draft=False)
        self.assertEqual(Lifecycle(r, "swu-echo", "open_pr", a).execute()[0], 409)
        self.assertEqual(sum(x[0] == "POST" for x in r.client.calls), 1)

    def test_same_timestamp_four_cycles(self):
        r = self.relay()
        for target in ("closed", "open", "closed", "open"):
            status, out = self.run_state(
                r, state=target, expected_updated_at=r.client.row["updated_at"]
            )
            self.assertEqual(status, 200, out)
        self.assertEqual(sum(x[0] == "PATCH" for x in r.client.calls), 4)

    def test_null_draft_rejected(self):
        with self.assertRaises(ValueError):
            _validate_args(
                "open_pr",
                {
                    "repo": REPO,
                    "head": "f",
                    "base": "main",
                    "title": "x",
                    "body": "y",
                    "draft": None,
                },
            )

    def test_legal_node_characters(self):
        r = self.relay()
        r.client.row["node_id"] = "PR_a/+b=="
        self.assertEqual(self.run_state(r)[0], 200)

    def test_canonical_repo_id_mismatch(self):
        r = self.relay()
        r.client.row["base"]["repo"]["id"] = 999
        self.assertNotEqual(self.run_state(r)[0], 200)
        self.assertFalse(r.client.patched)

    def test_legacy_ready_unknown_blocks_state(self):
        from git_relay.outcomes import key_for

        r = self.relay()
        k = key_for("swu-echo", "wf_pr_ready", {"repo": REPO, "number": 3})
        r.outcomes.begin(k)
        r.outcomes.finish(k, "unknown")
        self.assertEqual(self.run_state(r)[0], 409)
        self.assertFalse(r.client.patched)


if __name__ == "__main__":
    unittest.main()


class RelayIntegration(unittest.TestCase):
    def relay(self, directory):
        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        cfg = {
            "swu-echo": {
                "token": "x" * 32,
                "actions": ["open_pr", "edit_pr", "wf_pr_draft", "wf_pr_ready"],
                "repos": [REPO],
                "app_slug": "swu-echo",
                "mint_permissions": {"pull_requests": "write", "contents": "write"},
            }
        }
        return Relay(
            SeatPolicy(json.dumps(cfg)),
            Client(),
            AuditLog(directory + "/audit"),
            app_minter=Minter(),
            outcomes=OutcomeStore(directory + "/outcomes"),
            workflow_verbs_enabled=True,
        )

    def test_old_unknown_fences_all_alias_routes_across_new_instances(self):
        from git_relay.outcomes import key_for

        with tempfile.TemporaryDirectory() as d:
            r = self.relay(d)
            key = key_for(
                "swu-echo",
                "open_pr",
                {"repo": REPO, "head": "feature", "base": "main", "title": "old", "body": "old"},
            )
            r.outcomes.begin(key)
            r.outcomes.finish(key, "unknown")
            for action, args in [
                ("edit_pr", {"repo": REPO, "number": 3, "title": "new"}),
                ("edit_pr", Tests().args()),
                ("wf_pr_draft", {"repo": REPO, "number": 3}),
                ("wf_pr_ready", {"repo": REPO, "number": 3}),
                (
                    "open_pr",
                    {
                        "repo": REPO,
                        "head": "feature",
                        "base": "main",
                        "title": "new",
                        "body": "new",
                    },
                ),
            ]:
                fresh = self.relay(d)
                status, out = fresh.handle("x" * 32, action, args)
                self.assertEqual(status, 409, (action, out))
                self.assertEqual(out["outcome"], "unknown")
                self.assertEqual(fresh.client.calls, [])
                self.assertEqual(fresh.app_minter.calls, [])

    def test_valid_close_uses_actual_authenticated_route(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.relay(d)
            status, out = r.handle("x" * 32, "edit_pr", Tests().args())
            self.assertEqual(status, 200, out)
            self.assertTrue(out["verified"])
            wrong, _ = r.handle("wrong" * 8, "edit_pr", Tests().args())
            self.assertEqual(wrong, 401)


class FinalIdentityTests(Tests):
    def test_same_name_head_wrong_repository_id_denied(self):
        r = self.relay()
        r.client.row["head"]["repo"]["id"] = 999
        self.assertNotEqual(self.run_state(r)[0], 200)
        self.assertFalse(r.client.patched)

    def test_creation_missing_text_after_write_held_unknown(self):
        class MissingText(Client):
            def _call(self, method, path, payload=None, bearer=None):
                row = super()._call(method, path, payload, bearer)
                if method == "GET" and "/pulls/" in path:
                    row.pop("title", None)
                return row

        r = self.relay(MissingText())
        a = {
            "repo": REPO,
            "head": "feature",
            "base": "main",
            "title": "x",
            "body": "y",
            "draft": True,
        }
        self.assertEqual(Lifecycle(r, "swu-echo", "open_pr", a).execute()[1]["outcome"], "unknown")
