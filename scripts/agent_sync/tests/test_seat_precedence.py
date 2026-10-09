"""Seat precedence (AGENT-SYNC § Identity Rules, owner 2026-10-09) and the bot check, end to end.

The lead case (T1) is the one a plain bot-matches-seat check passes:  a BotFleet bot on the Codex
engine whose Codex rules exported AGENT_SEAT=CODEX and whose credential is codex-bot's own file.
Only the launcher-only AGENT_LAUNCH_SEAT can refuse it.  T-numbers are the design's test table
(seat-precedence design, section 4)."""
from __future__ import annotations

import json
import os
import secrets

from agent_sync import adapters as A
from agent_sync import identity as I
from agent_sync import live as L
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import TAG, Harness, write_rc
from agent_sync.tests.mcp_harness import McpHarness
from agent_sync.tests.test_attach import LEASE, AttachHarness

CODEX_EMAIL = "codex-bot@zulip.test"
PLUMBER_EMAIL = "bf-plumber-bot@zulip.test"
LAUNCHED = {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-PLUMBER"}


class ResolveSeatTests(Harness):
    """identity.resolve_seat on its own:  no network, no files."""

    def test_launcher_seat_wins_and_a_matching_name_is_fine(self) -> None:
        got = I.resolve_seat({**LAUNCHED, "AGENT_SEAT": "bf-plumber"}, flag="BF-Plumber", default="CODEX")
        self.assertEqual((got.seat, got.source, got.launcher, got.problem), ("BF-PLUMBER", "launcher", "botfleet", None))

    def test_any_other_name_under_a_launcher_seat_is_refused(self) -> None:
        for env, flag, label in (({"AGENT_SEAT": "CODEX"}, None, "AGENT_SEAT"), ({}, "CLAUDE", "--as"),
                                 ({"AGENT_TAG": "GROK"}, None, "AGENT_TAG")):
            with self.subTest(label=label):
                got = I.resolve_seat({**LAUNCHED, **env}, flag=flag)
                self.assertIsNone(got.seat)
                self.assertIn("launcher botfleet assigned BF-PLUMBER, but %s names" % label, got.problem)

    def test_a_launcher_with_no_seat_never_takes_a_default(self) -> None:
        got = I.resolve_seat({"AGENT_LAUNCHER": "botfleet", "AGENT_SEAT": "CLAUDE"}, flag="CLAUDE", default="CLAUDE")
        self.assertIsNone(got.seat)
        self.assertIn("no seat assigned by botfleet", got.problem)

    def test_ordinary_order_is_flag_then_seat_then_tag_then_default(self) -> None:
        self.assertEqual(I.resolve_seat({"AGENT_SEAT": "A", "AGENT_TAG": "B"}, flag="c", default="D").seat, "C")
        self.assertEqual(I.resolve_seat({"AGENT_SEAT": "A", "AGENT_TAG": "B"}, default="D").seat, "A")
        self.assertEqual(I.resolve_seat({"AGENT_TAG": "B"}, default="D").seat, "B")
        self.assertEqual(I.resolve_seat({"AGENT_TAG": "B"}, default="D", use_tag=False).seat, "D")
        self.assertEqual(I.resolve_seat({}, default="D").source, "default")
        self.assertEqual(I.resolve_seat({}), I.Resolution(None, "none"))

    def test_check_bot(self) -> None:
        bot = {"email": PLUMBER_EMAIL, "is_bot": True}
        self.assertIsNone(I.check_bot(bot, "BF-PLUMBER", PLUMBER_EMAIL))
        self.assertIn("authenticates as BF-PLUMBER", I.check_bot(bot, "CODEX", PLUMBER_EMAIL))
        self.assertIn("not a bot account", I.check_bot({"email": "jay@x", "is_bot": False}, "JAY", "jay@x"))
        self.assertIn("answered as", I.check_bot(bot, "BF-PLUMBER", CODEX_EMAIL))

    def test_partition(self) -> None:
        self.assertEqual(I.served_here("CLAUDE", {}), (True, ""))
        served, why = I.served_here("BF-PLUMBER", {})
        self.assertFalse(served)
        self.assertIn("seat BF-PLUMBER is not served by the Mac listener", why)
        self.assertFalse(I.served_here("NOT-A-SEAT", {})[0])
        self.assertTrue(I.served_here("GB-COMPILER", {"AGENT_SYNC_INSTANCE": "server"})[0])
        missing = {"AGENT_SYNC_PARTITION": str(self.tmp / "nope.toml")}
        self.assertEqual(I.served_here("CLAUDE", missing)[0], False, "fails closed")


class CliSeatTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.codex_key = secrets.token_hex(16)
        self.plumber_key = secrets.token_hex(16)
        self.fake.add_bot(CODEX_EMAIL, "Codex", self.codex_key)
        self.fake.add_bot(PLUMBER_EMAIL, "BF-Plumber", self.plumber_key)
        self.codex_rc = write_rc(self.secrets_dir / "Codex-zuliprc", email=CODEX_EMAIL, key=self.codex_key,
                                 site=self.fake.url)
        self.plumber_rc = write_rc(self.secrets_dir / "BF-Plumber-zuliprc", email=PLUMBER_EMAIL,
                                   key=self.plumber_key, site=self.fake.url)

    def tearDown(self) -> None:
        super().tearDown()
        everything = "\n".join(self.transcript)
        for key in (self.codex_key, self.plumber_key):
            self.assertNotIn(key, everything, "another bot's key reached the output")

    def message_posts(self) -> list:
        return [r for r in self.fake.requests if r.method == "POST" and r.path.strip("/") == "messages"]

    def post(self, *extra: str, env: dict[str, str]):
        return self.run_cli("post", "--topic", "seat test", *extra, "hello", env=env)

    # ---- launcher refusals:  nothing reaches the server -------------------------------------
    def test_t1_a_launched_bot_whose_engine_exported_codex_is_refused(self) -> None:
        result = self.post(env=self.env(**LAUNCHED, AGENT_SEAT="CODEX"))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("launcher botfleet assigned BF-PLUMBER", result.err)
        self.assertEqual(self.fake.requests, [], "refused before any request, so codex-bot's key never left")
        self.assertFalse((self.state_dir / "CODEX").exists())

    def test_t2_an_as_flag_cannot_re_pin_a_launched_session(self) -> None:
        result = self.post("--as", "CLAUDE", env=self.env(**LAUNCHED, AGENT_SEAT=None))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("launcher botfleet assigned BF-PLUMBER, but --as names CLAUDE", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_t3_a_launcher_with_no_seat_refuses_even_an_explicit_seat(self) -> None:
        for extra, env in ((("--as", "CLAUDE"), {"AGENT_SEAT": None}), ((), {}),
                           (("--default-seat", "CLAUDE"), {"AGENT_SEAT": None})):
            with self.subTest(extra=extra):
                result = self.post(*extra, env=self.env(AGENT_LAUNCHER="botfleet", **env))
                self.assertEqual(result.code, 3, result.err)
                self.assertIn("no seat assigned by botfleet", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_an_agent_tag_that_differs_from_the_launcher_seat_is_refused(self) -> None:
        result = self.post(env=self.env(**LAUNCHED, AGENT_SEAT=None, AGENT_TAG="CODEX"))
        self.assertEqual(result.code, 3, result.err)
        self.assertEqual(self.fake.requests, [])

    def test_a_launched_bot_posts_as_its_own_seat_with_its_own_key(self) -> None:
        result = self.post(env=self.env(**LAUNCHED, AGENT_SEAT="BF-PLUMBER"))
        self.assertEqual(result.code, 0, result.err)
        message = self.fake.messages[-1]
        self.assertEqual(message["sender_email"], PLUMBER_EMAIL)
        self.assertTrue(message["content"].startswith("[BF-PLUMBER·%s]" % TAG), message["content"])

    # ---- the bot check ----------------------------------------------------------------------
    def test_t4_another_bots_rc_is_refused_after_users_me(self) -> None:
        result = self.post(env=self.env(AGENT_SEAT="CODEX", ZULIP_RC=str(self.plumber_rc)))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("the credential authenticates as BF-PLUMBER (%s), not CODEX" % PLUMBER_EMAIL, result.err)
        self.assertEqual(self.message_posts(), [])
        self.assertEqual([r.path for r in self.fake.requests], ["users/me"])

    def test_t5_a_missing_seat_file_does_not_fall_through_to_another_bots_triple(self) -> None:
        self.codex_rc.unlink()
        result = self.post(env=self.env(AGENT_SEAT="CODEX", ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key,
                                        ZULIP_SITE=self.fake.url))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("authenticates as CLAUDE", result.err)
        self.assertEqual(self.message_posts(), [])

    def test_t6_a_renamed_rc_cannot_lie_about_the_seat(self) -> None:
        liar = write_rc(self.tmp / "elsewhere" / "Codex-zuliprc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        result = self.post("--rc", str(liar), env=self.env(AGENT_SEAT=None))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("authenticates as CLAUDE", result.err)
        self.assertIn("not CODEX", result.err)
        self.assertEqual(self.message_posts(), [])

    def test_t7_an_ordinary_session_posts_as_its_seat(self) -> None:
        result = self.post("--as", "CODEX", env=self.env(AGENT_SEAT=None))
        self.assertEqual(result.code, 0, result.err)
        message = self.fake.messages[-1]
        self.assertEqual(message["sender_email"], CODEX_EMAIL)
        self.assertTrue(message["content"].startswith("[CODEX·%s]" % TAG), message["content"])

    def test_t8_whoami_prints_both_identities_and_exits_3_on_a_mismatch(self) -> None:
        env = self.env(AGENT_SEAT="CODEX", ZULIP_RC=str(self.plumber_rc))
        result = self.run_cli("whoami", env=env)
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("bot seat:   BF-PLUMBER", result.out)
        self.assertIn("seat:       CODEX (AGENT_SEAT)", result.out)
        self.assertIn("verified:   NO", result.out)
        self.assertIn("authenticates as BF-PLUMBER", result.err)
        result = self.run_cli("whoami", "--json", env=env)
        self.assertEqual(result.code, 3)
        info = json.loads(result.out)
        self.assertEqual((info["seat"], info["bot_seat"], info["verified"]), ("CODEX", "BF-PLUMBER", False))
        result = self.run_cli("whoami", "--json")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual((info["verified"], info["problem"], info["launcher"]), (True, None, None))

    def test_every_network_command_checks_the_bot_first(self) -> None:
        env = self.env(AGENT_SEAT="CODEX", ZULIP_RC=str(self.plumber_rc))
        for argv in (("inbox",), ("topics",), ("read", "--topic", "t"), ("react", "--id", "1", "eyes"),
                     ("resolve", "--topic", "t"), ("channels",)):
            with self.subTest(argv=argv):
                self.fake.requests.clear()
                result = self.run_cli(*argv, env=env)
                self.assertEqual(result.code, 3, result.err)
                self.assertEqual([r.path for r in self.fake.requests], ["users/me"])

    # ---- --default-seat ---------------------------------------------------------------------
    def test_default_seat_fills_only_an_empty_slot(self) -> None:
        def seat_of(*argv: str, **env: str | None) -> tuple[str, str]:
            result = self.run_cli("whoami", "--json", *argv, env=self.env(**env))
            self.assertEqual(result.code, 0, result.err)
            info = json.loads(result.out)
            return info["seat"], info["seat_source"]

        self.assertEqual(seat_of("--default-seat", "CODEX", AGENT_SEAT=None), ("CODEX", "default"))
        self.assertEqual(seat_of("--default-seat", "CODEX"), ("CLAUDE", "AGENT_SEAT"))
        self.assertEqual(seat_of("--default-seat", "CODEX", **LAUNCHED, AGENT_SEAT=None), ("BF-PLUMBER", "launcher"))

    # ---- local views -----------------------------------------------------------------------
    def test_local_views_never_show_claudes_inbox_to_a_launched_session(self) -> None:
        paths = L.SeatPaths(str(self.state_dir), "CLAUDE")
        L.append_jsonl(paths.seat_inbox, [{"seq": 1, "kind": "message", "id": 5, "content": "CLAUDE ONLY"}])
        result = self.run_cli("inbox", "--local", env=self.env(AGENT_LAUNCHER="botfleet", AGENT_SEAT=None))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("no seat assigned by botfleet", result.err)
        result = self.run_cli("inbox", "--local", env=self.env(**LAUNCHED, AGENT_SEAT=None))
        self.assertNotIn("CLAUDE ONLY", result.out + result.err)
        result = self.run_cli("inbox", "--local")
        self.assertIn("CLAUDE ONLY", result.out, "an ordinary CLAUDE session still sees its own inbox")


class McpSeatTests(McpHarness):
    PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def test_a_registration_passes_a_default_seat_instead_of_pinning_agent_seat(self) -> None:
        code, lines, stderr = self.spawn(self.mcp_env(AGENT_SEAT=None), [self.PING], argv=("--default-seat", "CLAUDE"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines[0]["result"], {})
        self.assertIn("serving seat CLAUDE", stderr)

    def test_a_launcher_with_no_seat_refuses_the_default(self) -> None:
        env = self.mcp_env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet")
        code, lines, stderr = self.spawn(env, [self.PING], argv=("--default-seat", "CLAUDE"))
        self.assertEqual((code, lines), (3, []))
        self.assertIn("no seat assigned by botfleet", stderr)

    def test_a_launcher_seat_beats_the_default_and_needs_its_own_key(self) -> None:
        env = self.mcp_env(AGENT_SEAT=None, **LAUNCHED)
        code, lines, stderr = self.spawn(env, [self.PING], argv=("--default-seat", "CLAUDE"))
        self.assertEqual((code, lines), (3, []))
        self.assertIn("BF-Plumber-zuliprc", stderr)
        self.assertNotIn("serving seat CLAUDE", stderr)

    def test_an_agent_seat_that_differs_from_the_launcher_seat_exits_3(self) -> None:
        code, lines, stderr = self.spawn(self.mcp_env(**LAUNCHED), [self.PING])
        self.assertEqual((code, lines), (3, []))
        self.assertIn("launcher botfleet assigned BF-PLUMBER, but AGENT_SEAT names CLAUDE", stderr)


class HookSeatTests(AttachHarness):
    def claude_inbox_item(self) -> None:
        L.append_jsonl(self.seat_paths().seat_inbox, [{"seq": 1, "kind": "message", "id": 1, "content": "x"}])

    def no_leases(self, *seats: str) -> None:
        for seat in seats:
            leases = L.SeatPaths(self.root, seat).leases
            self.assertFalse(os.path.exists(leases) and os.listdir(leases), "a lease under %s" % seat)

    def test_t9_a_launched_session_gets_no_lease_and_no_claude_text(self) -> None:
        self.claude_inbox_item()
        for event in ("session-start", "prompt", "stop"):
            code, out, err = self.hook(event, {"source": "startup"}, **LAUNCHED)
            self.assertEqual((code, out, err), (0, "", ""), event)
        self.no_leases("CLAUDE", "BF-PLUMBER")

    def test_t10_a_seat_the_mac_listener_does_not_serve_gets_no_lease(self) -> None:
        code, out, err = self.hook("session-start", {"source": "startup"}, AGENT_SEAT="BF-PLUMBER", AGENT_SYNC_ATTACH="1")
        self.assertEqual(code, 0)
        self.assertIn("seat BF-PLUMBER is not served by the Mac listener", self.context_of(out))
        self.assertNotIn("CLAUDE", out)
        self.no_leases("CLAUDE", "BF-PLUMBER")
        code, out, err = self.hook("prompt", {"prompt": "hi"}, AGENT_SEAT="BF-PLUMBER")
        self.assertEqual((code, out), (0, ""), "one line at session start, then silence")

    def test_t11_a_launcher_with_no_seat_is_inert(self) -> None:
        self.claude_inbox_item()
        code, out, err = self.hook("session-start", {"source": "startup"}, AGENT_LAUNCHER="botfleet")
        self.assertEqual((code, out, err), (0, "", ""))
        self.no_leases("CLAUDE")

    def test_t12_attach_zero_turns_the_hooks_off(self) -> None:
        code, out, err = self.hook("session-start", {"source": "startup"}, AGENT_SYNC_ATTACH="0",
                                   CLAUDE_CODE_ENTRYPOINT="claude-desktop")
        self.assertEqual((code, out), (0, ""))
        self.no_leases("CLAUDE")

    def test_t13_an_ordinary_desktop_session_still_takes_the_claude_default(self) -> None:
        code, out, err = self.hook("session-start", {"source": "startup"}, CLAUDE_CODE_ENTRYPOINT="claude-desktop")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.seat_paths().lease(LEASE)))

    def test_attach_one_cannot_attach_a_launched_session(self) -> None:
        code, out, err = self.hook("session-start", {"source": "startup"}, AGENT_SYNC_ATTACH="1", **LAUNCHED,
                                   AGENT_SEAT="BF-PLUMBER")
        self.assertEqual((code, out), (0, ""))
        self.no_leases("CLAUDE", "BF-PLUMBER")

    def test_the_rewake_watcher_exits_at_once_when_launched(self) -> None:
        self.write_config(rewake=True)
        code, out, err = self.attach_run("--rewake", env=self.hook_env(**LAUNCHED))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_a_generic_lease_follows_the_precedence_and_the_partition(self) -> None:
        base = dict(CLAUDE_PID=None, CLAUDE_CODE_ENTRYPOINT=None)
        code, out, err = self.attach_run("--topic", "t", env=self.hook_env(**base, **LAUNCHED, AGENT_SEAT="CODEX"))
        self.assertEqual(code, 2)
        self.assertIn("launcher botfleet assigned BF-PLUMBER", err)
        code, out, err = self.attach_run("--topic", "t", "--as", "BF-PLUMBER", env=self.hook_env(**base))
        self.assertEqual(code, 2)
        self.assertIn("not served by the Mac listener", err)
        self.no_leases("CODEX", "BF-PLUMBER")


class WakeLauncherTests(Harness):
    def test_a_wake_is_launched_with_its_seat_and_never_attaches(self) -> None:
        parent = {"USER": "jay", "AGENT_SEAT": "CODEX", "AGENT_LAUNCH_SEAT": "BF-PLUMBER", "AGENT_LAUNCHER": "botfleet",
                  "ZULIP_RC": "/x", "CLAUDE_CODE_SESSION_ID": "y", "AGENT_SYNC_ATTACH": "1"}
        env = A.claude_env(parent, str(self.home), "/usr/bin", seat="CLAUDE")
        self.assertEqual({k: v for k, v in env.items() if k.startswith(("AGENT", "ZULIP", "CLAUDE_CODE_SESSION"))},
                         {"AGENT_LAUNCHER": "agent-sync-wake", "AGENT_LAUNCH_SEAT": "CLAUDE", "AGENT_SEAT": "CLAUDE",
                          "AGENT_SYNC_ATTACH": "0"})
        plain = A.claude_env(parent, str(self.home), "/usr/bin")
        self.assertFalse([k for k in plain if k.startswith(("AGENT", "ZULIP"))], "no seat:  no identity at all")
