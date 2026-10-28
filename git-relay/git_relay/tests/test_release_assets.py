import base64
import json
import os
import tempfile
import unittest

from test_workflow_verbs import REPO, Client, Policy
from test_workflow_wiring import TOKEN, Minter

from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.workflow_verbs import Refused, WorkflowVerbs

ASSET = {"id": 7, "name": "proof.txt", "state": "uploaded", "size": 2}


class Tests(unittest.TestCase):
    def handle(self, action, client):
        return WorkflowVerbs(client, Policy(), "swu-wise", "own", "swu-wise", action, REPO)

    def test_metadata_list_and_get_validate_shapes(self):
        for action, args, reply in [
            ("wf_release_assets", {"release_id": 3}, [ASSET]),
            ("wf_release_asset_get", {"asset_id": 7}, ASSET),
        ]:
            out = self.handle(action, Client(reply)).run(action, {"repo": REPO, **args})
            self.assertTrue(out)
            with self.assertRaises(RelayGitHubError):
                self.handle(action, Client(None)).run(action, {"repo": REPO, **args})

    def test_upload_bound_to_fixed_release_own_bearer(self):
        c = Client(
            {
                "id": 3,
                "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/3/assets{{?name,label}}",
            }
        )
        seen = []
        c.upload_release_asset = lambda *args: seen.append(args) or ASSET
        args = {
            "repo": REPO,
            "release_id": 3,
            "name": "proof.txt",
            "content_b64": base64.b64encode(b"hi").decode(),
        }
        self.assertEqual(
            self.handle("wf_release_asset_upload", c).run("wf_release_asset_upload", args)["asset"][
                "id"
            ],
            7,
        )
        self.assertEqual(seen, [(REPO, 3, "proof.txt", b"hi", "own")])
        for content in [b"\xff", b"ghp_" + b"a" * 30, b"\x00", b"x" * 32769]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_release_asset_upload",
                    {**args, "content_b64": base64.b64encode(content).decode()},
                    REPO,
                )
        with self.assertRaises(Refused):
            self.handle(
                "wf_release_asset_upload", Client({"id": 3, "upload_url": "https://evil.invalid"})
            ).run("wf_release_asset_upload", args)

    def test_binary_client_pins_host_and_sends_raw_bytes(self):
        seen = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self, n):
                return b'{"id":7}'

        def opener(req, timeout):
            seen.append(req)
            return Response()

        c = GitHubClient("owner-unused", opener=opener)
        self.assertEqual(
            c.upload_release_asset("example/repo", 3, "proof.txt", b"hi", "own")["id"], 7
        )
        req = seen[0]
        self.assertEqual(
            req.full_url,
            "https://uploads.github.com/repos/example/repo/releases/3/assets?name=proof.txt",
        )
        self.assertEqual(req.data, b"hi")
        self.assertEqual(req.get_header("Authorization"), "Bearer own")
        self.assertEqual(req.get_header("Content-type"), "application/octet-stream")


class WiringTests(unittest.TestCase):
    def test_uncertain_upload_is_held_and_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            action = "wf_release_asset_upload"
            seats = json.dumps(
                {
                    "swu-wise": {
                        "token": TOKEN,
                        "actions": [action],
                        "repos": [REPO],
                        "app_slug": "swu-wise",
                        "mint_permissions": {"contents": "write", "metadata": "read"},
                    }
                }
            )
            c = Client(
                {
                    "id": 3,
                    "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/3/assets{{?name,label}}",
                }
            )
            seen = []
            c.upload_release_asset = lambda *args: seen.append(args) or {}
            r = Relay(
                SeatPolicy(seats),
                c,
                AuditLog(os.path.join(tmp, "a.jsonl")),
                app_minter=Minter(),
                workflow_verbs_enabled=True,
            )
            args = {"repo": REPO, "release_id": 3, "name": "proof.txt", "content_b64": "aGk="}
            status, body = r.handle(TOKEN, action, args)
            self.assertEqual((status, body["outcome"], body["retryable"]), (502, "unknown", False))
            self.assertEqual(r.handle(TOKEN, action, args)[0], 409)
            self.assertEqual(len(seen), 1)
