import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class FileHistory(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "path": "docs/a+b.txt", "head_sha": SHA, **kw}

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_file_history", REPO)

    def test_exact_pinned_encoded_path_bounded_metadata(self):
        c = Client(
            [
                {
                    "sha": "b" * 40,
                    "parents": [{"sha": "c" * 40}],
                    "commit": {"message": "not returned"},
                    "author": {"email": "not returned"},
                }
            ]
        )
        out = self.handle(c).run("wf_file_history", self.args(page=2, per_page=1))
        self.assertEqual(
            out,
            {
                "commits": [{"sha": "b" * 40, "parents": ["c" * 40]}],
                "path": "docs/a+b.txt",
                "head_sha": SHA,
                "page": 2,
                "has_more": True,
            },
        )
        self.assertEqual(
            c.calls,
            [
                (
                    "GET",
                    f"/repos/{REPO}/commits?sha={SHA}&path=docs%2Fa%2Bb.txt&per_page=1&page=2",
                    None,
                    "own",
                )
            ],
        )
        self.assertEqual(SPECS["wf_file_history"], ({"contents": "read"}, False))
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_file_history", REPO)
        self.assertEqual(m.calls[0][2], {"contents": "read", "metadata": "read"})

    def test_pages_not_false_complete(self):
        for values in [[], [{"sha": "b" * 40, "parents": []}]]:
            out = self.handle(Client(values)).run("wf_file_history", self.args(per_page=2))
            self.assertFalse(out["has_more"])

    def test_closed_args(self):
        for bad in [
            {"path": "../x"},
            {"page": 0},
            {"per_page": 51},
            {"head_sha": True},
            {"follow": True},
            {"path": "a/" * 17 + "x"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_file_history", self.args(**bad), REPO)

    def test_malformed_upstream_refused_not_silently_skipped(self):
        for values in [
            {},
            [{}],
            [{"sha": True, "parents": []}],
            [{"sha": "b" * 40, "parents": [{}]}],
            [{"sha": "b" * 40, "parents": []}] * 31,
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(values)).run("wf_file_history", self.args())


class FileHistoryRelayBoundary(unittest.TestCase):
    def relay(self, tmp, *, enabled=True, listed=True, contents=True):
        import json
        import os

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        perms = {"metadata": "read", **({"contents": "read"} if contents else {})}
        seats = {
            "swu-wise": {
                "token": "w" * 32,
                "actions": ["wf_file_history"] if listed else ["wf_runs"],
                "repos": [REPO],
                "app_slug": "swu-wise",
                "mint_permissions": perms,
            }
        }
        return Relay(
            SeatPolicy(json.dumps(seats)),
            Client(),
            AuditLog(os.path.join(tmp, "audit.jsonl")),
            app_minter=Minter(),
            workflow_verbs_enabled=enabled,
        )

    def test_disabled_unlisted_wrong_caller_repo_and_missing_ceiling_never_mint(self):
        import tempfile

        for action in ("wf_file_history",):
            for case in ("disabled", "unlisted", "caller", "repo", "ceiling", "badargs"):
                with self.subTest(action=action, case=case), tempfile.TemporaryDirectory() as tmp:
                    r = self.relay(
                        tmp,
                        enabled=case != "disabled",
                        listed=case != "unlisted",
                        contents=case != "ceiling",
                    )
                    args = {"repo": "Other/Repo" if case == "repo" else REPO, "head_sha": SHA}
                    args.update({"path": "docs/a.txt"})
                    if case == "badargs":
                        args["unexpected"] = True
                    status, _ = r.handle("x" * 32 if case == "caller" else "w" * 32, action, args)
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(r.client.calls, [])
                    self.assertEqual(r.app_minter.calls, [])

    def test_actual_relay_dispatch_contents_read_and_no_write_or_cache(self):
        import tempfile

        for action in ("wf_file_history",):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                r = self.relay(tmp)
                payload = [{"sha": "b" * 40, "parents": []}]
                r.client = Client(payload, payload)
                args = {"repo": REPO, "head_sha": SHA, "path": "docs/a.txt"}
                for _ in range(2):
                    status, body = r.handle("w" * 32, action, args)
                    self.assertEqual(status, 200, body)
                self.assertEqual(len(r.client.calls), 2)
                self.assertTrue(
                    all(
                        c[0] == "GET" and c[3] == "opaque-installation-bearer"
                        for c in r.client.calls
                    )
                )
                self.assertTrue(
                    all(
                        c[2] == {"contents": "read", "metadata": "read"} for c in r.app_minter.calls
                    )
                )
