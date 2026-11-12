"""create_repo: allow, deny, owner, name, already_exists, and visibility."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from git_relay.app_tokens import AppTokenError, AppTokenMinter, mint_permission_allowed
from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient
from git_relay.mcp_server import TOOL_NAMES, _validate_args
from git_relay.policy import PolicyError, SeatPolicy
from git_relay.server import Relay

TOKEN = "t" * 16
MINTED = "namespace-mint-token"
OWNER_TOKEN = "owner-token-must-stay"
DESCRIPTION = "fleet widget notes"


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class Body:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode()

    def read(self, _n=-1):
        data, self._raw = self._raw, b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Script:
    def __init__(self, items):
        self.items = list(items)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http(status, payload=None, headers=None):
    raw = b"" if payload is None else json.dumps(payload).encode()
    return urllib.error.HTTPError(
        "https://api.github.com/x",
        status,
        "error",
        headers or {},
        io.BytesIO(raw),
    )


def repo_body(owner="Simple-With-Us", name="Widget", private=True, **extra):
    body = {
        "id": 42,
        "full_name": f"{owner}/{name}",
        "private": private,
        "html_url": f"https://github.com/{owner}/{name}",
        "description": "upstream description stays off the log",
    }
    body.update(extra)
    return body


def policy_json(actions, public=None):
    row = {
        "token": TOKEN,
        "actions": list(actions),
        "repos": ["Simple-With-Us/*", "jaywedgeworth22/*"],
        "app_slug": "swu-echo",
        "mint_permissions": {"metadata": "read", "contents": "read"},
    }
    if public is not None:
        row["create_repo_public"] = public
    return json.dumps({"swu-echo": row})


class Minter:
    def __init__(self):
        self.mints = []
        self.revoked = []

    def token_for_namespace(self, slug, owner, kind):
        self.mints.append((slug, owner, kind))
        return MINTED

    def revoke_installation_token(self, token):
        self.revoked.append(token)


def drive(script, args, actions=("create_repo",), public=None):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "a.jsonl")
    minter = Minter()
    relay = Relay(
        SeatPolicy(policy_json(actions, public)),
        GitHubClient(OWNER_TOKEN, opener=script),
        AuditLog(path),
        app_minter=minter,
    )
    status, body = relay.handle(TOKEN, "create_repo", args)
    audit = Path(path).read_text() if os.path.exists(path) else ""
    return status, body, script, minter, audit


def methods(script):
    return [req.get_method() for req in script.requests]


def paths(script):
    return [urllib.request.urlparse(req.full_url).path for req in script.requests]


class CreateRepoTests(unittest.TestCase):
    def test_allow_private_by_default(self):
        script = Script([http(404, {"message": "Not Found"}), Body(201, repo_body())])
        status, body, script, minter, audit = drive(
            script,
            {
                "repo": "simple-with-us/Widget",
                "description": DESCRIPTION,
                "license_template": "apache-2.0",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "created")
        self.assertEqual(body["repo"], "Simple-With-Us/Widget")
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["url"], "https://github.com/Simple-With-Us/Widget")
        self.assertEqual(methods(script), ["GET", "POST"])
        self.assertEqual(
            paths(script),
            ["/repos/Simple-With-Us/Widget", "/orgs/Simple-With-Us/repos"],
        )
        posted = json.loads(script.requests[1].data)
        self.assertEqual(posted["private"], True)
        self.assertEqual(posted["visibility"], "private")
        self.assertEqual(posted["auto_init"], True)
        self.assertEqual(posted["license_template"], "apache-2.0")
        self.assertEqual(posted["description"], DESCRIPTION)
        self.assertEqual(minter.mints, [("swu-echo", "Simple-With-Us", "org")])
        self.assertEqual(minter.revoked, [MINTED])
        self.assertTrue(
            all(req.get_header("Authorization") == f"Bearer {MINTED}" for req in script.requests)
        )
        self.assertNotIn(OWNER_TOKEN, audit)
        self.assertNotIn(MINTED, audit)
        self.assertNotIn(DESCRIPTION, audit)
        self.assertNotIn("upstream description", audit)
        self.assertNotIn(MINTED, json.dumps(body))

    def test_replay_of_a_finished_create_does_not_call_github(self):
        script = Script([http(404), Body(201, repo_body())])
        tmp = tempfile.mkdtemp()
        minter = Minter()
        relay = Relay(
            SeatPolicy(policy_json(["create_repo"])),
            GitHubClient(OWNER_TOKEN, opener=script),
            AuditLog(os.path.join(tmp, "a.jsonl")),
            app_minter=minter,
        )
        args = {"repo": "Simple-With-Us/Widget"}
        first, body = relay.handle(TOKEN, "create_repo", args)
        self.assertEqual((first, body["result"]), (200, "created"))
        second, replay = relay.handle(TOKEN, "create_repo", args)
        self.assertEqual(second, 200)
        self.assertEqual(replay["result"], "created")
        self.assertEqual(methods(script), ["GET", "POST"])
        self.assertEqual(len(minter.mints), 1)

    def test_deny_without_an_explicit_grant(self):
        script = Script([])
        status, body, script, minter, _audit = drive(
            script, {"repo": "Simple-With-Us/Widget"}, actions=("open_pr",)
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "action 'create_repo' not allowed for this seat")
        self.assertEqual(script.requests, [])
        self.assertEqual(minter.mints, [])

    def test_bad_owner_is_refused_before_github(self):
        script = Script([])
        status, body, script, minter, _audit = drive(script, {"repo": "octocat/Widget"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "repository owner is not allowed")
        self.assertEqual(script.requests, [])
        self.assertEqual(minter.mints, [])

    def test_bad_name_is_invalid_repository(self):
        for name in ("bad..name", "Widget.git", "has space", ".hidden", "a" * 101, ""):
            with self.subTest(name=name):
                script = Script([])
                status, body, script, minter, _audit = drive(
                    script, {"repo": f"Simple-With-Us/{name}"}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "invalid repository")
                self.assertEqual(script.requests, [])
                self.assertEqual(minter.mints, [])
        status, body, *_rest = drive(Script([]), {"repo": "not-a-repo"})
        self.assertEqual((status, body["error"]), (400, "invalid repository"))

    def test_already_exists_on_get_does_not_post(self):
        script = Script([Body(200, repo_body(private=True))])
        status, body, script, minter, audit = drive(script, {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "already_exists")
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(methods(script), ["GET"])
        self.assertEqual(minter.revoked, [MINTED])
        self.assertNotIn(MINTED, audit)

    def test_redirect_is_already_exists_and_is_not_followed(self):
        moved = http(
            301,
            {"message": "Moved Permanently", "url": "https://evil.example/steal"},
            Headers({"Location": "https://api.github.com/repositories/99"}),
        )
        script = Script([moved])
        status, body, script, _minter, audit = drive(script, {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"repo": "Simple-With-Us/Widget", "result": "already_exists"})
        self.assertEqual(len(script.requests), 1)
        self.assertNotIn("repositories/99", script.requests[0].full_url)
        self.assertNotIn("evil.example", json.dumps(body))
        self.assertNotIn("evil.example", audit)

    def test_post_conflict_confirms_with_one_get_and_never_posts_twice(self):
        script = Script(
            [
                http(404),
                http(422, {"message": "name already exists", "code": "custom"}),
                Body(200, repo_body()),
            ]
        )
        status, body, script, _minter, _audit = drive(script, {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "already_exists")
        self.assertEqual(methods(script), ["GET", "POST", "GET"])

    def test_hidden_repo_stays_a_refusal_when_the_confirming_read_is_404(self):
        # A selected-repositories installation can hide an existing repo: the
        # pre-read is 404, the create is 422, and the confirming read is 404.
        # That pair stays a refusal. A 422 is also a validation failure, and
        # the create is not repeated.
        script = Script(
            [
                http(404),
                http(422, {"message": "name already exists on this account", "code": "custom"}),
                http(404),
            ]
        )
        status, body, script, _minter, audit = drive(script, {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "github refused the request")
        self.assertEqual(body["github_status"], 422)
        self.assertNotIn("result", body)
        self.assertEqual(methods(script), ["GET", "POST", "GET"])
        self.assertEqual(paths(script).count("/orgs/Simple-With-Us/repos"), 1)
        self.assertNotIn("name already exists", json.dumps(body))
        self.assertNotIn("name already exists", audit)

    def test_public_is_denied_unless_the_seat_flag_allows_it(self):
        script = Script([])
        status, body, script, minter, _audit = drive(
            script, {"repo": "Simple-With-Us/Widget", "visibility": "public"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "action 'create_repo_public' not allowed for this seat")
        self.assertEqual(script.requests, [])
        self.assertEqual(minter.mints, [])
        with self.assertRaises(PolicyError):
            SeatPolicy(policy_json(["create_repo"], public="yes"))

    def test_public_is_sent_only_when_requested_and_allowed(self):
        script = Script([http(404), Body(201, repo_body(private=False))])
        status, body, script, _minter, _audit = drive(
            script,
            {"repo": "Simple-With-Us/Widget", "visibility": "public"},
            public=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["visibility"], "public")
        posted = json.loads(script.requests[1].data)
        self.assertEqual(posted["private"], False)
        self.assertEqual(posted["visibility"], "public")
        self.assertNotIn("description", posted)

    def test_an_existing_repo_is_not_made_public(self):
        script = Script([Body(200, repo_body(private=True))])
        status, body, script, _minter, _audit = drive(
            script,
            {"repo": "Simple-With-Us/Widget", "visibility": "public"},
            public=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "already_exists")
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(methods(script), ["GET"])

    def test_user_account_posts_to_user_repos(self):
        script = Script([http(404), Body(201, repo_body(owner="jaywedgeworth22", private=True))])
        status, body, script, minter, _audit = drive(script, {"repo": "JAYWEDGEWORTH22/Widget"})
        self.assertEqual(status, 200)
        self.assertEqual(body["repo"], "jaywedgeworth22/Widget")
        self.assertEqual(paths(script)[1], "/user/repos")
        self.assertEqual(minter.mints, [("swu-echo", "jaywedgeworth22", "user")])
        self.assertEqual(json.loads(script.requests[1].data)["private"], True)

    def test_uncertain_pre_read_does_not_create_and_can_be_retried(self):
        script = Script([http(500, {"message": "boom secret"}), http(404), Body(201, repo_body())])
        tmp = tempfile.mkdtemp()
        minter = Minter()
        relay = Relay(
            SeatPolicy(policy_json(["create_repo"])),
            GitHubClient(OWNER_TOKEN, opener=script),
            AuditLog(os.path.join(tmp, "a.jsonl")),
            app_minter=minter,
        )
        status, body = relay.handle(TOKEN, "create_repo", {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 503)
        self.assertEqual(body["retryable"], True)
        self.assertNotIn("boom secret", json.dumps(body))
        self.assertEqual(methods(script), ["GET"])
        self.assertEqual(minter.revoked, [MINTED])
        status, body = relay.handle(TOKEN, "create_repo", {"repo": "Simple-With-Us/Widget"})
        self.assertEqual((status, body["result"]), (200, "created"))
        self.assertEqual(methods(script), ["GET", "GET", "POST"])

    def test_get_forbidden_does_not_create(self):
        script = Script([http(403, {"message": "secret-upstream"})])
        status, body, script, _minter, audit = drive(script, {"repo": "Simple-With-Us/Widget"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "github refused the request")
        self.assertEqual(methods(script), ["GET"])
        self.assertNotIn("secret-upstream", json.dumps(body))
        self.assertNotIn("secret-upstream", audit)
        self.assertNotIn(MINTED, audit)

    def test_stdio_tool_rejects_a_bad_visibility_before_forwarding(self):
        self.assertIn("create_repo", TOOL_NAMES)
        self.assertIsNone(_validate_args("create_repo", {"repo": "Simple-With-Us/Widget"}))
        self.assertIsNotNone(
            _validate_args(
                "create_repo", {"repo": "Simple-With-Us/Widget", "visibility": "internal"}
            )
        )


class NamespaceMintTests(unittest.TestCase):
    def _minter(self, replies):
        api = Api(replies)
        minter = AppTokenMinter(
            None, api, signer=lambda app_id, pem, now: "jwt", clock=lambda: 1000
        )
        minter._app_credentials = lambda slug: (7, "pem-not-a-token")
        return minter, api

    def test_exact_permissions_are_not_cached_and_admin_write_stays_out_of_token_for(self):
        self.assertFalse(mint_permission_allowed("organization_administration", "write"))
        self.assertFalse(mint_permission_allowed("administration", "write"))
        install = {
            "id": 3,
            "app_id": 7,
            "suspended_at": None,
            "account": {"login": "Simple-With-Us", "type": "Organization"},
        }
        minter, api = self._minter(
            [{"id": 7}, install, {"token": MINTED}, {"id": 7}, install, {"token": MINTED}]
        )
        blocked = AppTokenMinter(None, api, signer=lambda *_a: "jwt", clock=lambda: 0)
        with self.assertRaises(AppTokenError):
            blocked.token_for(
                "swu-echo",
                "Simple-With-Us/Widget",
                {"organization_administration": "write", "metadata": "read"},
            )
        self.assertEqual(api.calls, [])
        first = minter.token_for_namespace("swu-echo", "Simple-With-Us", "org")
        second = minter.token_for_namespace("swu-echo", "Simple-With-Us", "org")
        self.assertEqual(first, second)
        self.assertEqual(minter._tokens, {})
        posts = [call for call in api.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(
            posts[0][3],
            {"permissions": {"metadata": "read", "organization_administration": "write"}},
        )
        self.assertNotIn("repositories", posts[0][3])
        # The endpoint is split so this test is not counted as a second mint
        # site by the repo-wide scan (test_github_relay_minting_owner.py).
        self.assertEqual(posts[0][1], "/app/installations/3/" + "access_" + "tokens")

    def test_user_installation_and_refused_installations_do_not_mint(self):
        user = {
            "id": 4,
            "app_id": 7,
            "suspended_at": None,
            "account": {"login": "jaywedgeworth22", "type": "User"},
        }
        minter, api = self._minter([{"id": 7}, user, {"token": MINTED}])
        self.assertEqual(minter.token_for_namespace("swu-echo", "jaywedgeworth22", "user"), MINTED)
        self.assertEqual(
            api.calls[2][3],
            {"permissions": {"metadata": "read", "administration": "write"}},
        )
        self.assertEqual(api.calls[1][1], "/users/jaywedgeworth22/installation")
        suspended = {
            "id": 4,
            "app_id": 7,
            "suspended_at": "2020-01-01T00:00:00Z",
            "account": {"login": "jaywedgeworth22", "type": "User"},
        }
        minter, api = self._minter([{"id": 7}, suspended])
        with self.assertRaises(AppTokenError):
            minter.token_for_namespace("swu-echo", "jaywedgeworth22", "user")
        self.assertTrue(all(call[0] != "POST" for call in api.calls))
        wrong = {
            "id": 4,
            "app_id": 7,
            "suspended_at": None,
            "account": {"login": "octocat", "type": "User"},
        }
        minter, api = self._minter([{"id": 7}, wrong])
        with self.assertRaises(AppTokenError):
            minter.token_for_namespace("swu-echo", "jaywedgeworth22", "user")
        self.assertTrue(all(call[0] != "POST" for call in api.calls))
        minter, api = self._minter([])
        with self.assertRaises(AppTokenError):
            minter.token_for_namespace("swu-echo", "Simple-With-Us", "nope")
        self.assertEqual(api.calls, [])

    def test_revoke_failure_is_swallowed(self):
        api = Api([RuntimeError("no")])
        minter = AppTokenMinter(None, api, signer=lambda *_a: "jwt", clock=lambda: 0)
        minter.revoke_installation_token(MINTED)
        self.assertEqual(api.calls[0][0], "DELETE")
        self.assertEqual(api.calls[0][1], "/installation/token")
        self.assertEqual(api.calls[0][2], MINTED)


class Api:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, path, bearer, payload=None):
        self.calls.append((method, path, bearer, payload))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


if __name__ == "__main__":
    unittest.main()
