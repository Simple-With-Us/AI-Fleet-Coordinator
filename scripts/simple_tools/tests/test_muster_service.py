import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_tools import muster_service as M
from simple_tools import zulip as Z

SITE = "https://example.zulipchat.com"
OPERATOR_ID = 44
OWNER_ID = 11
OPERATOR_EMAIL = "operator-bot@example.test"
CONFIG = (
    "[daemon]\n"
    'instance = "mac"\n'
    "owner_user_id = %d\n"
    "operator_user_id = %d\n"
    'operator_email = "%s"\n' % (OWNER_ID, OPERATOR_ID, OPERATOR_EMAIL)
)


class Client:
    """A fake Zulip client that records every request and answers the three reads."""

    def __init__(self):
        self.me = {
            "user_id": OPERATOR_ID,
            "email": OPERATOR_EMAIL,
            "is_bot": True,
            "bot_owner_id": OWNER_ID,
        }
        self.members = [
            {"user_id": 2, "full_name": "Zed", "email": "zed@example.test", "is_bot": False, "is_active": True, "role": 400},
            {"user_id": 9001, "full_name": "Claude", "email": "claude-bot@example.test", "is_bot": True, "is_active": True, "role": 400},
            {"user_id": 9003, "full_name": "Old", "email": "old-bot@example.test", "is_bot": True, "is_active": False, "role": 400},
        ]
        self.messages = [
            {"id": 7, "type": "stream", "display_recipient": "Work-In-Progress", "subject": "older", "sender_email": "claude-bot@example.test", "sender_full_name": "Claude", "sender_id": 9001, "timestamp": 1, "content": "old"},
            {"id": 8, "type": "stream", "display_recipient": "Work-In-Progress", "subject": "newest", "sender_email": "claude-bot@example.test", "sender_full_name": "Claude", "sender_id": 9001, "timestamp": 2, "content": "I am at quota.", "flags": ["read"]},
        ]
        self.requests = []

    def request(self, method, path, params=None, **kw):
        self.requests.append((method, path, params, kw))
        if path == "users/me":
            return self.me
        if path == "users":
            return {"members": self.members}
        if path == "messages":
            return {"messages": self.messages}
        raise AssertionError("unexpected path " + path)

    def post(self, *a, **k):  # must never be reachable
        raise AssertionError("post reached")


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".simple-tools").mkdir()
        self.config = self.home / ".simple-tools" / "listener.toml"
        self.config.write_text(CONFIG)
        self.env = {"HOME": str(self.home), "SIMPLE_TOOLS_REALM": SITE}
        self.client = Client()
        self.creds = Z.Credentials(OPERATOR_EMAIL, "test-key-value", SITE, "test")

    def run_action(self, action, env=None, **kw):
        args = argparse.Namespace(action=action, limit=kw.get("limit", 500), sender=kw.get("sender", ""))
        return M.execute(args, env=self.env if env is None else env, client=self.client, credentials=self.creds)

    def refused(self, code, action="users", env=None, **kw):
        with self.assertRaises(M.Refused) as caught:
            self.run_action(action, env=env, **kw)
        self.assertEqual(caught.exception.code, code)

    # The two reads

    def test_users_match_the_users_json_inactive_shape(self):
        rows = self.run_action("users")
        self.assertEqual([r["full_name"] for r in rows], ["Claude", "Old", "Zed"])
        claude = rows[0]
        self.assertEqual(set(claude), {"user_id", "full_name", "email", "is_bot", "is_active", "role", "seat"})
        self.assertEqual(claude["seat"], "CLAUDE")
        self.assertFalse(rows[1]["is_active"])
        self.assertNotIn("seat", rows[2])

    def test_users_limit_bounds_the_rows(self):
        self.assertEqual(len(self.run_action("users", limit=1)), 1)
        for bad in (0, M.MAX_USERS + 1, True):
            self.refused("bad-request", limit=bad)

    def test_last_post_is_one_public_narrow_and_the_newest_message(self):
        rows = self.run_action("last-post", sender="Claude-Bot@example.test")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], 8)
        self.assertEqual(row["channel"], "Work-In-Progress")
        self.assertEqual(row["topic"], "newest")
        self.assertEqual(row["timestamp"], 2)
        self.assertEqual(row["content"], "I am at quota.")
        self.assertEqual(row["sender_email"], "claude-bot@example.test")
        self.assertNotIn("flags", row)
        reads = [r for r in self.client.requests if r[1] == "messages"]
        self.assertEqual(len(reads), 1)
        params = reads[0][2]
        self.assertEqual(
            json.loads(params["narrow"]),
            [
                {"operator": "channels", "operand": "public"},
                {"operator": "sender", "operand": "claude-bot@example.test"},
            ],
        )
        self.assertEqual(params["anchor"], "newest")
        self.assertEqual(params["num_before"], 1)
        self.assertEqual(params["num_after"], 0)

    def test_last_post_drops_direct_messages_and_other_senders(self):
        self.client.messages = [
            {"id": 9, "type": "private", "sender_email": "claude-bot@example.test", "content": "dm"},
            {"id": 10, "type": "stream", "display_recipient": "x", "subject": "y", "sender_email": "other@example.test"},
        ]
        self.assertEqual(self.run_action("last-post", sender="claude-bot@example.test"), [])

    def test_last_post_bounds_the_body(self):
        self.client.messages[1]["content"] = "x" * (M.MAX_CONTENT + 50)
        row = self.run_action("last-post", sender="claude-bot@example.test")[0]
        self.assertEqual(len(row["content"]), M.MAX_CONTENT)

    def test_sender_that_is_not_one_address_is_refused_before_any_request(self):
        for bad in ("", "-x@example.test", "a b@example.test", "noaddress", "a@b@c", "a\n@b"):
            self.client.requests.clear()
            self.refused("bad-request", action="last-post", sender=bad)
            self.assertEqual(self.client.requests, [])

    def test_malformed_responses_are_refused(self):
        self.client.members = "nope"
        self.refused("malformed-response")
        # A response that is not an object at all is malformed too, never a generic failure.
        original = self.client.request

        def not_an_object(method, path, params=None, **kw):
            return original(method, path, params, **kw) if path == "users/me" else ["x"]

        self.client.request = not_an_object
        self.refused("malformed-response")
        self.refused("malformed-response", action="last-post", sender="claude-bot@example.test")
        self.client.request = original
        self.client.messages = None
        self.refused("malformed-response", action="last-post", sender="claude-bot@example.test")

    # Read only

    def test_only_get_on_three_paths_is_ever_requested(self):
        self.run_action("users")
        self.run_action("last-post", sender="claude-bot@example.test")
        self.assertTrue(self.client.requests)
        for method, path, _params, kw in self.client.requests:
            self.assertEqual(method, "GET")
            self.assertIn(path, M.ALLOWED_GETS)
            self.assertEqual(kw["timeout"], M.REQUEST_TIMEOUT)
            self.assertIsNotNone(kw["deadline"])
        self.assertEqual([r[1] for r in self.client.requests], ["users/me", "users", "users/me", "messages"])

    def test_read_only_client_has_no_write_surface(self):
        reader = M.ReadOnlyClient(self.client, 0.0)
        for name in ("post", "patch", "delete", "put", "upload", "request"):
            self.assertFalse(hasattr(reader, name), name)
        with self.assertRaises(M.Refused):
            reader.get("users/me/subscriptions")
        with self.assertRaises(M.Refused):
            reader.get("messages/flags")
        self.assertEqual(self.client.requests, [])

    # Identity and pins

    def test_wrong_identity_or_owner_is_refused_before_any_read(self):
        for field, value in [
            ("user_id", 45),
            ("email", "other@example.test"),
            ("is_bot", False),
            ("bot_owner_id", 12),
        ]:
            old = self.client.me[field]
            self.client.me[field] = value
            self.client.requests.clear()
            self.refused("identity-mismatch")
            self.assertEqual([r[1] for r in self.client.requests], ["users/me"])
            self.client.me[field] = old

    def test_remote_and_launched_seat_refused(self):
        for extra in ({"SIMPLE_CHAT_REMOTE": "1"}, {"AGENT_LAUNCHER": "1"}, {"AGENT_LAUNCH_SEAT": "CLAUDE"}):
            self.refused("launched-or-remote", env=dict(self.env, **extra))
        self.assertEqual(self.client.requests, [])

    def test_missing_pins_fail_closed(self):
        for line in ("owner_user_id", "operator_user_id", "operator_email"):
            text = "\n".join(l for l in CONFIG.splitlines() if not l.startswith(line))
            self.config.write_text(text)
            self.refused("pins-missing")
        self.config.unlink()
        self.refused("pins-missing")
        self.assertEqual(self.client.requests, [])

    def test_environment_pins_win_over_the_config(self):
        self.config.write_text('[daemon]\nowner_user_id = %d\n' % OWNER_ID)
        env = dict(self.env, OPERATOR_ZULIP_USER_ID=str(OPERATOR_ID), OPERATOR_ZULIP_EMAIL=OPERATOR_EMAIL)
        self.assertTrue(self.run_action("users", env=env))
        self.config.write_text(CONFIG)
        self.refused("identity-mismatch", env=dict(self.env, OPERATOR_ZULIP_USER_ID="45"))

    def test_malformed_pins_are_not_skipped(self):
        for bad in (
            "[daemon]\noperator_user_id = true\n",
            "[daemon]\nowner_user_id = -1\n",
            '[daemon]\noperator_email = 5\n',
            "not toml [",
        ):
            self.config.write_text(bad)
            self.refused("pins-unreadable")
        self.config.write_text(CONFIG)
        self.refused("pins-unreadable", env=dict(self.env, OPERATOR_ZULIP_USER_ID="abc"))
        self.refused("pins-unreadable", env=dict(self.env, OPERATOR_ZULIP_EMAIL="no-at-sign"))
        self.assertEqual(self.client.requests, [])

    def test_plain_http_realm_refused(self):
        self.refused("realm-invalid", env=dict(self.env, SIMPLE_TOOLS_REALM="http://example.test"))

    def test_credential_for_another_realm_refused(self):
        self.creds = Z.Credentials(OPERATOR_EMAIL, "k", "https://other.invalid", "test")
        self.refused("realm-mismatch")
        self.assertEqual(self.client.requests, [])

    def test_store_realm_checked_and_only_the_operator_folder_read(self):
        with (
            patch.object(Z, "infisical_project_id", return_value="project"),
            patch.object(
                Z,
                "_default_infisical_fetch",
                return_value={
                    "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
                    "ZULIP_OPERATOR_API_KEY": "k",
                    "ZULIP_OPERATOR_SITE": "https://other.invalid",
                },
            ) as fetch,
        ):
            with self.assertRaises(M.Refused) as caught:
                M.connect(self.env)
            self.assertEqual(caught.exception.code, "realm-mismatch")
            self.assertEqual(fetch.call_args.args[3], "/zulip_operator")
            self.assertEqual(
                fetch.call_args.args[0],
                ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE"),
            )

    def test_unreadable_credential_is_one_code(self):
        with (
            patch.object(Z, "infisical_project_id", return_value="project"),
            patch.object(Z, "_default_infisical_fetch", side_effect=Z.CredentialError("fleet-secrets key-absent (exit 3)")),
        ):
            with self.assertRaises(M.Refused) as caught:
                M.connect(self.env)
        self.assertEqual(caught.exception.code, "credential-unreadable")

    def test_stored_credential_for_another_bot_refused(self):
        with (
            patch.object(Z, "infisical_project_id", return_value="project"),
            patch.object(
                Z,
                "_default_infisical_fetch",
                return_value={
                    "ZULIP_OPERATOR_EMAIL": "claude-bot@example.test",
                    "ZULIP_OPERATOR_API_KEY": "k",
                    "ZULIP_OPERATOR_SITE": SITE,
                },
            ),
        ):
            with self.assertRaises(M.Refused) as caught:
                M.connect(self.env)
        self.assertEqual(caught.exception.code, "credential-unreadable")

    # The command line

    def main(self, argv, error=None, result=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if error is not None or result is not None:
                with patch.object(M, "execute", side_effect=error, return_value=result):
                    code = M.main(argv, env=self.env)
            else:
                code = M.main(argv, env=self.env)
        return code, out.getvalue(), err.getvalue()

    def test_refusals_print_one_fixed_line_and_never_provider_text(self):
        secret = "provider said test-key-value at https://internal.example"
        cases = [
            (Z.ApiError(secret, status=401), "credential-rejected"),
            (Z.ApiError(secret, status=500), "zulip-error"),
            (Z.NetworkError(secret), "zulip-unreachable"),
            (RuntimeError(secret), "failed"),
            (M.Refused("pins-missing"), "pins-missing"),
        ]
        for error, code in cases:
            status, out, err = self.main(["users"], error=error)
            self.assertEqual(status, 3)
            self.assertEqual(out, "")
            self.assertEqual(err, "muster-service: refused (%s)\n" % code)
            self.assertNotIn("test-key-value", err)

    def test_bad_arguments_are_refused_without_echo(self):
        for argv in (["post"], ["users", "--as=CLAUDE"], ["last-post", "--to", "x"], [], ["--help"]):
            status, out, err = self.main(argv)
            self.assertEqual(status, 3, argv)
            self.assertEqual(err, "muster-service: refused (bad-request)\n")

    def test_success_prints_one_json_object_per_line(self):
        rows = [{"user_id": 1, "full_name": "A B"}, {"user_id": 2}]
        status, out, err = self.main(["users"], result=rows)
        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertEqual([json.loads(l) for l in out.split("\n") if l], rows)
        self.assertEqual(len(out.rstrip("\n").split("\n")), 2, lines)

    def test_refusal_codes_are_fixed(self):
        self.assertEqual(M.Refused("anything else").code, "failed")


if __name__ == "__main__":
    unittest.main()
