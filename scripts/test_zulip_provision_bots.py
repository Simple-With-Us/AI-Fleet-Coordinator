"""Tests for scripts/zulip_provision_bots.py:  python3 scripts/test_zulip_provision_bots.py

Runs the script against a fake Zulip server on 127.0.0.1 and the real bounded worker with a loopback provider.
Every key below is a made-up test value.  Nothing here touches the real realm,
Infisical or the retired credentials folder.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import tempfile
import threading
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fleet_knobs.tests._fake import ScopedReadAPI

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("provision_bots", HERE / "zulip_provision_bots.py")
prov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prov)

ADMIN_EMAIL = "admin-bot@127.0.0.1"
ADMIN_KEY = "test-admin-key-AAAA1111"
OLD_KEY = "test-old-codex-key-BBBB2222"
ADMIN_ENV = {
    "ZULIP_OPERATOR_EMAIL": ADMIN_EMAIL,
    "ZULIP_OPERATOR_API_KEY": ADMIN_KEY,
}


class FakeZulip:
    """A tiny Zulip:  users, POST /bots, key regeneration, subscriptions.  Records every request."""

    def __init__(self, caller_role=200):
        self.caller_role = caller_role
        self.requests = []  # (method, path)
        self.subscribed = []  # (email, channels)
        self.counter = 100
        self.keys = {ADMIN_KEY: ADMIN_EMAIL, OLD_KEY: "codex-bot@127.0.0.1"}
        self.users = [
            self._user(1, "Admin", ADMIN_EMAIL, True, caller_role),
            self._user(2, "Codex", "codex-bot@127.0.0.1", True, 400),
            self._user(3, "Cursor", "human@127.0.0.1", False, 400),  # a human holding a roster name
        ]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.removeprefix("/api/v1/")
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode()) if length else {}
                fake.requests.append((method, path))
                auth = self.headers.get("Authorization", "")
                try:
                    email, key = base64.b64decode(auth.split()[1]).decode().split(":", 1)
                except Exception:
                    return self._send(401, {"result": "error", "msg": "bad auth"})
                if fake.keys.get(key) != email:
                    return self._send(401, {"result": "error", "msg": "invalid key"})
                caller = next(u for u in fake.users if u["email"] == email)
                if (method, path) == ("GET", "users/me"):
                    return self._send(200, {"result": "success", **caller})
                if (method, path) == ("GET", "users"):
                    return self._send(200, {"result": "success", "members": fake.users})
                if (method, path) == ("GET", "bots"):
                    return self._send(400, {"result": "error", "msg": "bots cannot list bots"})
                if (method, path) == ("POST", "bots"):
                    if caller["role"] > 200:
                        return self._send(403, {"result": "error", "msg": "not an admin"})
                    fake.counter += 1
                    short = form["short_name"][0]
                    new_email = f"{short}-bot@127.0.0.1"
                    new_key = f"test-new-key-{fake.counter}-CCCC3333"
                    fake.keys[new_key] = new_email
                    fake.users.append(self._mk(fake.counter, form["full_name"][0], new_email))
                    return self._send(
                        200,
                        {
                            "result": "success",
                            "user_id": fake.counter,
                            "api_key": new_key,
                            "full_name": form["full_name"][0],
                            "owner_id": caller["user_id"],
                        },
                    )
                m = re.fullmatch(r"bots/(\d+)/api_key/regenerate", path)
                if method == "POST" and m:
                    target = next(u for u in fake.users if u["user_id"] == int(m.group(1)))
                    for old in [k for k, e in fake.keys.items() if e == target["email"]]:
                        del fake.keys[old]
                    fake.counter += 1
                    new_key = f"test-regen-key-{fake.counter}-DDDD4444"
                    fake.keys[new_key] = target["email"]
                    return self._send(200, {"result": "success", "api_key": new_key})
                if (method, path) == ("POST", "users/me/subscriptions"):
                    names = [s["name"] for s in json.loads(form["subscriptions"][0])]
                    fake.subscribed.append((email, names))
                    return self._send(200, {"result": "success"})
                return self._send(404, {"result": "error", "msg": "no route"})

            _mk = staticmethod(
                lambda uid, name, email: FakeZulip._user(uid, name, email, True, 400)
            )

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    @staticmethod
    def _user(uid, name, email, is_bot, role):
        return {"user_id": uid, "full_name": name, "email": email, "is_bot": is_bot, "role": role}

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.out = self.root / "out"
        self.fake = FakeZulip()
        self.api = ScopedReadAPI(
            {**ADMIN_ENV, "ZULIP_OPERATOR_SITE": self.fake.url},
            project=str(uuid.uuid4()),
            path="/zulip_operator",
            # The consumer's own account:  the shared client checks the token's own name.
            token_name=prov.TOKEN_ACCOUNTS["/zulip_operator"],
        )
        self.api.__enter__()
        self.client = prov._infisical_reader()
        self.original_read = self.client.bounded_fetch_project

        def read(project, folder, env, **options):
            return self.original_read(
                project,
                folder,
                {**env, "FLEET_SECRETS_API": self.api.env["FLEET_SECRETS_API"]},
                **options,
            )

        self.reader_patch = mock.patch.object(
            self.client, "bounded_fetch_project", side_effect=read
        )
        self.reader_patch.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "INFISICAL_FLEET_OPS_PROJECT_ID": self.api.project,
                "INFISICAL_TOKEN": self.api.token,
                # The shared client reads the token's own grant metadata from the same host.
                "FLEET_SECRETS_API": self.api.env["FLEET_SECRETS_API"],
            },
            clear=True,
        )
        self.env.start()
        self.realm = mock.patch.object(prov, "REALM", self.fake.url)
        self.realm.start()
        self.no_input = mock.patch("builtins.input", side_effect=AssertionError("prompted"))
        self.no_input.start()
        self.no_getpass = mock.patch(
            "getpass.getpass", side_effect=AssertionError("prompted"), create=True
        )
        self.no_getpass.start()

    def tearDown(self):
        for p in (self.no_getpass, self.no_input, self.realm, self.env):
            p.stop()
        self.reader_patch.stop()
        self.api.__exit__()
        self.fake.close()
        self.tmp.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = prov.main(["--out-dir", str(self.out), *argv])
            except SystemExit as exc:
                code = exc
        return code, out.getvalue(), err.getvalue()

    def fs_calls(self):
        return [
            urllib.parse.unquote(path.split("/raw/", 1)[1].split("?", 1)[0])
            for path in self.api.requests
            if "/raw/" in path
        ]

    def assert_no_keys(self, *streams):
        text = "\n".join(streams)
        self.assertNotRegex(text, r"test-(admin|old|new|regen)[a-z-]*key|-key-\d+-")
        self.assertNotIn(ADMIN_KEY, text)
        self.assertNotIn(OLD_KEY, text)

    # -- credentials -----------------------------------------------------------------------

    def test_credentials_cross_the_real_bounded_worker_when_env_is_empty(self):
        code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        calls = self.fs_calls()
        self.assertEqual(
            calls,
            [
                "ZULIP_OPERATOR_EMAIL",
                "ZULIP_OPERATOR_API_KEY",
                "ZULIP_OPERATOR_SITE",
            ],
        )
        self.assertIn("role 200", out)
        self.assertIn("/zulip_operator", out)

    def test_the_environment_is_not_a_source_for_the_credential(self):
        # The environment is never a source for a value:  the credential resolves from the
        # consumer's own scoped grant,  and a hand-set copy does not replace it.
        with mock.patch.dict(
            os.environ,
            {
                "ZULIP_OPERATOR_EMAIL": "someone-else@example.test",
                "ZULIP_OPERATOR_API_KEY": "not-the-key",
            },
        ):
            code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        # The credential came from the consumer's own scoped grant,  not from the environment.
        self.assertEqual(
            sorted(self.fs_calls()),
            ["ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_SITE"],
        )

    def test_site_defaults_to_realm_when_absent(self):
        # The scoped reader can not answer SITE (exit 3) but EMAIL and KEY are in the store.
        with mock.patch.dict(self.api.values):
            self.api.values.pop("ZULIP_OPERATOR_SITE", None)
            code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)

    def test_scoped_reader_failure_names_the_account_and_folder_without_provider_prose(self):
        with mock.patch.object(self.api, "status", 503):
            code, out, err = self.run_main("--dry-run")
        self.assertIsInstance(code, SystemExit)
        msg = str(code.code)
        self.assertIn("/zulip_operator", msg)
        self.assertIn(prov.TOKEN_ACCOUNTS["/zulip_operator"], msg)
        self.assertNotIn("LEAKY", msg + out + err)
        self.assert_no_keys(msg, out, err)
        self.assertEqual(self.fake.requests, [])

    def test_missing_canonical_source_fails_loudly_too(self):
        with mock.patch.object(prov, "_infisical_reader", side_effect=ImportError("unavailable")):
            code, out, err = self.run_main("--dry-run")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("/zulip_operator", str(code.code))

    def test_only_the_realm_admin_bot_is_ever_asked_for(self):
        with mock.patch.dict(
            os.environ,
            {
                "ZULIP_EMAIL": "someone@example.invalid",
                "ZULIP_API_KEY": "x",
                "ZULIP_OWNER_EMAIL": "o@x",
            },
        ):
            code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.fs_calls())
        self.assertTrue(all(c.startswith("ZULIP_OPERATOR_") for c in self.fs_calls()))
        self.assertIn(ADMIN_EMAIL, out)

    def test_no_credential_override_option_exists(self):
        for flag in ("--owner-rc", "--personal-key", "--rc", "--email"):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                prov.main([flag, "x", "--dry-run"])
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_credential_for_another_realm_is_refused(self):
        # The site comes from the consumer's own scoped grant,  so a realm it does not name is
        # refused before anything is sent.
        with mock.patch.dict(self.api.values):
            self.api.values["ZULIP_OPERATOR_SITE"] = "https://evil.example"
            code, out, err = self.run_main("--dry-run")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("expected", str(code.code))
        self.assertEqual(self.fake.requests, [])

    def test_non_admin_caller_is_refused(self):
        self.fake.users[0]["role"] = 400
        code, out, err = self.run_main("--yes", "--only", "Clutch")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("role 400", str(code.code))
        self.assertEqual(self.fake.posts(), [])

    def test_bot_caller_is_allowed_for_admin_role(self):
        # The caller IS a bot (is_bot true) with role 200:  no "bots cannot create bots" refusal.
        code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)

    # -- plan and writes -------------------------------------------------------------------

    def test_dry_run_makes_no_post_and_writes_nothing(self):
        code, out, err = self.run_main("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.posts(), [])
        self.assertFalse(self.out.exists())
        self.assertIn("create clutch-bot@", out)
        self.assertIn(
            "exists:  key not recoverable here;  rerun with --regenerate to rotate it", out
        )
        self.assertIn("SKIP: name taken by a non-bot user", out)

    def test_missing_bot_is_created_with_rc_file_and_infisical_names(self):
        code, out, err = self.run_main(
            "--yes", "--only", "Grok", "--only", "OpenCode", "--only", "BF-Builder"
        )
        self.assertEqual(code, 0, err)
        self.assertIn(("POST", "bots"), self.fake.requests)
        self.assertTrue(stat.S_IMODE(self.out.stat().st_mode) == 0o700)
        for stem, short in (
            ("Grok-Build", "grok-build"),
            ("OpenCode", "opencode"),
            ("BF-Builder", "bf-builder"),
        ):
            rc = self.out / f"{stem}-zuliprc"
            self.assertEqual(stat.S_IMODE(rc.stat().st_mode), 0o600)
            text = rc.read_text()
            self.assertIn(f"email={short}-bot@127.0.0.1", text)
            self.assertIn(f"site={self.fake.url}", text)
            self.assertRegex(text, r"key=test-new-key-\d+-CCCC3333")
        self.assertIn(
            "Infisical /zulip: GROK_BUILD_ZULIP_EMAIL, GROK_BUILD_ZULIP_KEY, GROK_BUILD_ZULIP_SITE",
            out,
        )
        self.assertIn(
            "Infisical /zulip: OPENCODE_ZULIP_EMAIL, OPENCODE_ZULIP_KEY, OPENCODE_ZULIP_SITE", out
        )
        self.assertIn(
            "Infisical /zulip: BF_BUILDER_ZULIP_EMAIL, BF_BUILDER_ZULIP_KEY, BF_BUILDER_ZULIP_SITE",
            out,
        )
        self.assertEqual(len(self.fake.subscribed), 3)
        self.assertEqual(self.fake.subscribed[0][1], prov.CHANNELS)
        self.assert_no_keys(out, err)

    def test_existing_bot_is_skipped_without_regenerate(self):
        code, out, err = self.run_main("--yes", "--only", "Codex")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.posts(), [])
        self.assertIn("key not recoverable here", out)
        self.assertFalse((self.out / "Codex-zuliprc").exists())
        self.assertEqual(self.fake.keys.get(OLD_KEY), "codex-bot@127.0.0.1")  # old key still valid

    def test_regenerate_rotates_an_existing_bot_and_warns(self):
        code, out, err = self.run_main("--dry-run", "--regenerate", "--only", "Codex")
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING", out)
        self.assertIn("invalidates", out)
        self.assertEqual(self.fake.posts(), [])
        code, out, err = self.run_main("--yes", "--regenerate", "--only", "Codex")
        self.assertEqual(code, 0, err)
        self.assertIn(("POST", "bots/2/api_key/regenerate"), self.fake.requests)
        self.assertNotIn(("POST", "bots"), self.fake.requests)
        rc = self.out / "Codex-zuliprc"
        self.assertEqual(stat.S_IMODE(rc.stat().st_mode), 0o600)
        self.assertRegex(rc.read_text(), r"key=test-regen-key-\d+-DDDD4444")
        self.assertNotIn(OLD_KEY, self.fake.keys)
        self.assertIn("Infisical /zulip: CODEX_ZULIP_EMAIL, CODEX_ZULIP_KEY, CODEX_ZULIP_SITE", out)
        self.assertEqual(self.fake.subscribed[-1][0], "codex-bot@127.0.0.1")
        self.assert_no_keys(out, err)

    def test_regenerate_requires_only(self):
        code, out, err = self.run_main("--yes", "--regenerate")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("--only", str(code.code))
        self.assertEqual(self.fake.requests, [])

    def test_regenerate_never_touches_a_non_bot_user(self):
        code, out, err = self.run_main("--yes", "--regenerate", "--only", "Cursor")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.posts(), [])
        self.assertIn("non-bot", out)

    def test_existing_file_is_kept_and_blocks_creation_without_force(self):
        self.out.mkdir(mode=0o700)
        keep = self.out / "Clutch-zuliprc"
        keep.write_text("precious")
        code, out, err = self.run_main("--yes", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.posts(), [])  # nothing created, so no key is lost
        self.assertEqual(keep.read_text(), "precious")
        self.assertIn("--force", out)
        code, out, err = self.run_main("--yes", "--force", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        self.assertIn("key=test-new-key", keep.read_text())

    def test_nothing_is_ever_written_under_dot_secrets(self):
        bad = self.home / ".secrets" / "Zulip"
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            prov.main(["--yes", "--only", "Clutch", "--out-dir", str(bad)])
        self.assertIn("~/.secrets", str(cm.exception.code))
        self.assertFalse(bad.exists())
        self.assertEqual(self.fake.posts(), [])

    def test_existing_out_dir_is_tightened_to_700(self):
        self.out.mkdir()
        self.out.chmod(0o755)
        code, out, err = self.run_main("--yes", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)

    def test_untrusted_record_proof_refuses_without_credential_or_provider_text(self):
        self.api.record_omissions = ("secretValueHidden",)
        value = prov.admin_secret("ZULIP_OPERATOR_SITE", required=False)
        self.assertEqual(value, "")

    def test_empty_stdin_at_the_prompt_stops_cleanly(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            code, out, err = self.run_main("--only", "Clutch")
        self.assertEqual(code, 1)
        self.assertIn("Stopped: no input.", out)
        self.assertEqual(self.fake.posts(), [])
        self.assertFalse(self.out.exists())

    def test_answering_n_stops(self):
        with mock.patch("builtins.input", return_value="n"):
            code, out, err = self.run_main("--only", "Clutch")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.posts(), [])

    def test_unknown_only_name_is_an_error(self):
        code, out, err = self.run_main("--dry-run", "--only", "Nobody")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("Nobody", str(code.code))

    # -- never GET /bots, never print a key -----------------------------------------------

    def test_get_bots_is_never_requested_in_any_flow(self):
        self.run_main("--dry-run")
        self.run_main("--yes", "--only", "Clutch", "--only", "FX")
        self.run_main("--yes", "--regenerate", "--only", "Codex")
        self.assertNotIn(("GET", "bots"), self.fake.requests)
        self.assertNotIn("bots", [p for m, p in self.fake.requests if m == "GET"])

    def test_full_roster_run_prints_no_key_anywhere(self):
        code, out, err = self.run_main("--yes")
        self.assertEqual(code, 0, err)
        self.assert_no_keys(out, err)
        created = [r for r in self.fake.requests if r == ("POST", "bots")]
        self.assertEqual(len(created), len(prov.ROSTER) - 2)  # Codex exists, Cursor is a human


if __name__ == "__main__":
    unittest.main()
