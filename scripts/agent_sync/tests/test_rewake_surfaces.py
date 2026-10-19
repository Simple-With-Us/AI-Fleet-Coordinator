"""AFC-358:  rewake_verified is split per surface (terminal and Desktop Code tab), the old key still
sets both, and the watcher, the prompt hook and the daemon's wake-capable test use the session's own."""

from __future__ import annotations

import os
import tomllib
import unittest

from agent_sync import config as C
from agent_sync import live as L
from agent_sync.tests.listener_harness import fake_watcher
from agent_sync.tests.test_attach import LEASE, AttachHarness, message_item
from agent_sync.tests.test_daemon import MENTION, DaemonHarness

BARE = "[platform.claude-code]\n%s\n"


def flags(text: str) -> tuple[bool, bool]:
    return L.rewake_flags(tomllib.loads(BARE % text))


class FlagTests(unittest.TestCase):
    def test_the_old_key_sets_both_and_the_default_is_off(self) -> None:
        self.assertEqual(flags(""), (False, False))
        self.assertEqual(flags("rewake_verified = true"), (True, True))
        self.assertEqual(flags("rewake_verified = false"), (False, False))
        self.assertEqual(
            flags('rewake_verified = "yes"'), (False, False), "only a real true counts"
        )

    def test_a_per_surface_key_wins_over_the_old_one_for_its_surface(self) -> None:
        self.assertEqual(flags("rewake_verified_cli = true"), (True, False))
        self.assertEqual(flags("rewake_verified_desktop = true"), (False, True))
        self.assertEqual(
            flags("rewake_verified = true\nrewake_verified_desktop = false"), (True, False)
        )
        self.assertEqual(
            flags("rewake_verified = false\nrewake_verified_cli = true"), (True, False)
        )

    def test_a_non_boolean_per_surface_key_is_false_and_never_falls_back_to_the_old_key(self) -> None:
        self.assertEqual(flags('rewake_verified = true\nrewake_verified_desktop = "false"'), (True, False))
        self.assertEqual(flags('rewake_verified = true\nrewake_verified_cli = "true"'), (False, True))
        self.assertEqual(flags("rewake_verified = true\nrewake_verified_cli = 1\nrewake_verified_desktop = 0"), (False, False))

    def test_a_non_boolean_per_surface_key_is_a_reported_config_problem_that_keeps_the_seat(self) -> None:
        cfg = C.from_dict(tomllib.loads(
            '[daemon]\nowner_user_id = 1\n' + BARE % 'rewake_verified = true\nrewake_verified_desktop = "false"'
            + '[seat.CLAUDE]\nbot = "Claude"\nwake = "inbox"\n'))
        self.assertEqual((cfg.rewake_verified_cli, cfg.rewake_verified_desktop), (True, False))
        self.assertTrue(any("rewake_verified_desktop must be true or false" in e for e in cfg.errors), cfg.errors)
        self.assertIn("CLAUDE", cfg.seats, "the seat keeps capturing")

    def test_surface_names_come_from_the_entrypoint(self) -> None:
        self.assertEqual(L.surface_of("cli"), "cli")
        self.assertEqual(L.surface_of("claude-desktop"), "desktop")
        for other in ("sdk-cli", "claude-vscode", "", None, 3):
            self.assertIsNone(L.surface_of(other), other)

    def test_an_unnamed_surface_needs_both_flags(self) -> None:
        self.assertTrue(L.rewake_verified_for(True, True, None))
        self.assertFalse(L.rewake_verified_for(True, False, None))
        self.assertFalse(L.rewake_verified_for(False, True, "sdk"))
        self.assertTrue(L.rewake_verified_for(True, False, "cli"))
        self.assertFalse(L.rewake_verified_for(True, False, "desktop"))

    def test_config_exposes_both_flags(self) -> None:
        cfg = C.from_dict(tomllib.loads(BARE % "rewake_verified_cli = true"))
        self.assertEqual(
            (cfg.rewake_verified_cli, cfg.rewake_verified_desktop, cfg.rewake_verified),
            (True, False, False),
        )


class WatcherTests(AttachHarness):
    def set_flags(self, text: str) -> None:
        self.write_config()
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text().replace("rewake_verified = false", text))

    def session(self, entrypoint: str):
        return self.hook_env(CLAUDE_CODE_ENTRYPOINT=entrypoint)

    def test_the_watcher_arms_only_for_a_verified_surface(self) -> None:
        self.set_flags("rewake_verified_cli = true")
        self.attach_run(
            "--hook", "session-start", stdin='{"source": "startup"}', env=self.session("cli")
        )
        L.append_live(self.seat_paths(), LEASE, [message_item(1)])
        code, out, err = self.attach_run("--rewake", env=self.session("cli"))
        self.assertEqual(code, 2, "a terminal session is verified and wakes")
        self.assertIn("Zulip item", err)

    def test_an_unverified_desktop_session_never_arms_a_watcher(self) -> None:
        self.set_flags("rewake_verified_cli = true")
        self.attach_run(
            "--hook",
            "session-start",
            stdin='{"source": "startup"}',
            env=self.session("claude-desktop"),
        )
        L.append_live(self.seat_paths(), LEASE, [message_item(1)])
        code, out, err = self.attach_run("--rewake", env=self.session("claude-desktop"))
        self.assertEqual((code, err), (0, ""), "exits at once")
        self.assertFalse(os.path.exists(self.seat_paths().rewake_lock(LEASE)))
        self.assertEqual(len(L.pending_items(self.seat_paths(), LEASE)), 1)

    def test_the_prompt_hook_claims_bodies_only_on_a_verified_surface(self) -> None:
        self.set_flags("rewake_verified_desktop = true")
        for entry, claims in (("claude-desktop", True), ("cli", False)):
            with self.subTest(entrypoint=entry):
                self.attach_run("--hook", "session-end", stdin="{}", env=self.session(entry))
                self.attach_run(
                    "--hook",
                    "session-start",
                    stdin='{"source": "startup"}',
                    env=self.session(entry),
                )
                L.append_live(self.seat_paths(), LEASE, [message_item(7, "INTERRUPT BODY")])
                code, out, err = self.attach_run(
                    "--hook", "prompt", stdin='{"prompt": "hi"}', env=self.session(entry)
                )
                self.assertEqual("INTERRUPT BODY" in out, claims, out)

    def test_the_lease_records_its_entrypoint(self) -> None:
        self.set_flags("rewake_verified_cli = true")
        self.attach_run(
            "--hook",
            "session-start",
            stdin='{"source": "startup"}',
            env=self.session("claude-desktop"),
        )
        self.assertEqual(L.load_lease(self.seat_paths(), LEASE)["entrypoint"], "claude-desktop")


class DaemonCapableTests(DaemonHarness):
    def set_flags(self, text: str) -> None:
        self.write_config()
        path = self.state_dir / "listener.toml"
        path.write_text(path.read_text().replace("rewake_verified = false", text))

    def views(self, daemon):
        return {v.lease_id: v.wake_capable for v in daemon.lease_views(daemon.seats["CLAUDE"])}

    def test_wake_capable_follows_the_leases_own_surface(self) -> None:
        self.set_flags("rewake_verified_cli = true")
        daemon = self.started()
        for lease_id, entry in (("term", "cli"), ("desk", "claude-desktop"), ("old", None)):
            self.write_lease(lease_id, entrypoint=entry)
            self.addCleanup(os.close, fake_watcher(self.seat_paths(), lease_id))
        self.assertEqual(self.views(daemon), {"term": True, "desk": False, "old": False})

    def test_a_mention_skips_the_unverified_desktop_session_for_the_verified_terminal_one(
        self,
    ) -> None:
        self.set_flags("rewake_verified_cli = true")
        daemon = self.started()
        self.write_lease("term", entrypoint="cli", last_prompt=10.0)
        self.write_lease("desk", entrypoint="claude-desktop", last_prompt=90.0)
        for lease_id in ("term", "desk"):
            self.addCleanup(os.close, fake_watcher(self.seat_paths(), lease_id))
        mid = self.fake.add_message("Codex", "other", "elsewhere", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual(self.inbox()[0]["delivered_to"], "term")
        self.assertEqual(
            [i["id"] for i in self.live_items("term") if i["kind"] == "message"], [mid]
        )

    def test_status_reports_each_surface(self) -> None:
        self.set_flags("rewake_verified_cli = true")
        status = self.started().status()
        self.assertEqual(
            (
                status["rewake_verified_cli"],
                status["rewake_verified_desktop"],
                status["rewake_verified"],
            ),
            (True, False, False),
        )


if __name__ == "__main__":
    unittest.main()
