"""Tests for scripts/zulip_provision_bots.py:  python3 scripts/test_zulip_provision_bots.py

Runs the script against a fake Zulip server on 127.0.0.1 and a fake fleet-secrets.
Every key below is a made-up test value.  Nothing here touches the real realm,
Infisical or ~/.secrets.
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
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("provision_bots", HERE / "zulip_provision_bots.py")
prov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prov)

ADMIN_EMAIL = "admin-bot@127.0.0.1"
ADMIN_KEY = "test-admin-key-AAAA1111"
OLD_KEY = "test-old-codex-key-BBBB2222"
ADMIN_ENV = {
    "REALM_ADMIN_ZULIP_EMAIL": ADMIN_EMAIL,
    "REALM_ADMIN_ZULIP_KEY": ADMIN_KEY,
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


FAKE_SECRETS = """#!{python}
import os, sys
log = {log!r}
with open(log, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_FS_FAIL") == "1":
    sys.stderr.write("LEAKY-STDERR-should-never-surface test-admin-key-AAAA1111\\n")
    sys.exit(3)
values = {{
    "REALM_ADMIN_ZULIP_EMAIL": "{email}",
    "REALM_ADMIN_ZULIP_KEY": "{key}",
    "REALM_ADMIN_ZULIP_SITE": "{site}",
}}
if sys.argv[1:2] != ["get"] or sys.argv[3:5] != ["--path", "/zulipadmin"] or sys.argv[2] not in values:
    sys.exit(3)
print(values[sys.argv[2]])
"""


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.out = self.root / "out"
        self.fake = FakeZulip()
        self.fs_log = self.root / "fs.log"
        self.fs_log.write_text("")
        fs = self.root / "fleet-secrets"
        fs.write_text(
            FAKE_SECRETS.format(
                python=sys.executable,
                log=str(self.fs_log),
                email=ADMIN_EMAIL,
                key=ADMIN_KEY,
                site=self.fake.url,
            )
        )
        fs.chmod(0o755)
        self.fs = fs
        env = {k: v for k, v in os.environ.items() if not k.startswith("REALM_ADMIN_")}
        env.update({"HOME": str(self.home), "FLEET_SECRETS_BIN": str(fs)})
        self.env = mock.patch.dict(os.environ, env, clear=True)
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
        return [line for line in self.fs_log.read_text().splitlines() if line]

    def assert_no_keys(self, *streams):
        text = "\n".join(streams)
        self.assertNotRegex(text, r"test-(admin|old|new|regen)[a-z-]*key|-key-\d+-")
        self.assertNotIn(ADMIN_KEY, text)
        self.assertNotIn(OLD_KEY, text)

    # -- credentials -----------------------------------------------------------------------

    def test_credentials_from_fleet_secrets_zulipadmin_when_env_is_empty(self):
        code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        calls = self.fs_calls()
        self.assertEqual(
            calls,
            [
                "get REALM_ADMIN_ZULIP_EMAIL --path /zulipadmin",
                "get REALM_ADMIN_ZULIP_KEY --path /zulipadmin",
                "get REALM_ADMIN_ZULIP_SITE --path /zulipadmin",
            ],
        )
        self.assertIn("role 200", out)
        self.assertIn("/zulipadmin", out)

    def test_env_wins_over_fleet_secrets(self):
        with mock.patch.dict(os.environ, {**ADMIN_ENV, "REALM_ADMIN_ZULIP_SITE": self.fake.url}):
            code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fs_calls(), [])

    def test_site_defaults_to_realm_when_absent(self):
        # fleet-secrets can not answer SITE (exit 3) but EMAIL and KEY are in env.
        with mock.patch.dict(os.environ, {**ADMIN_ENV, "FAKE_FS_FAIL": "1"}):
            code, out, err = self.run_main("--dry-run", "--only", "Clutch")
        self.assertEqual(code, 0, err)

    def test_fleet_secrets_failure_names_folder_and_key_but_no_value_or_stderr(self):
        with mock.patch.dict(os.environ, {"FAKE_FS_FAIL": "1"}):
            code, out, err = self.run_main("--dry-run")
        self.assertIsInstance(code, SystemExit)
        msg = str(code.code)
        self.assertIn("/zulipadmin", msg)
        self.assertIn("REALM_ADMIN_ZULIP_EMAIL", msg)
        self.assertNotIn("LEAKY", msg + out + err)
        self.assert_no_keys(msg, out, err)
        self.assertEqual(self.fake.requests, [])

    def test_missing_binary_fails_loudly_too(self):
        with mock.patch.dict(os.environ, {"FLEET_SECRETS_BIN": str(self.root / "nope")}):
            code, out, err = self.run_main("--dry-run")
        self.assertIsInstance(code, SystemExit)
        self.assertIn("/zulipadmin", str(code.code))

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
        self.assertTrue(all(c.split()[1].startswith("REALM_ADMIN_ZULIP_") for c in self.fs_calls()))
        self.assertIn(ADMIN_EMAIL, out)

    def test_no_credential_override_option_exists(self):
        for flag in ("--owner-rc", "--personal-key", "--rc", "--email"):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                prov.main([flag, "x", "--dry-run"])
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_credential_for_another_realm_is_refused(self):
        with mock.patch.dict(
            os.environ, {**ADMIN_ENV, "REALM_ADMIN_ZULIP_SITE": "https://evil.example"}
        ):
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

    def test_undecodable_fleet_secrets_output_does_not_crash(self):
        self.fs.write_text("#!/bin/sh\nprintf 'ok\\377\\376'\n")
        value = prov.admin_secret("REALM_ADMIN_ZULIP_SITE", required=False)
        self.assertIsInstance(value, str)
        self.assertTrue(value.startswith("ok"))

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
