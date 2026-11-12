"""GROK's last mile.  grok-drive and the registry are fakes:  no test prompts a live chat."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import grok_wake as GW
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC

SID = "01a1cd37-4371-7f13-866e-ab3ef60d58f8"
BODY = "THE-MESSAGE-BODY-MUST-NOT-TRAVEL"
SUMMARY = "THE-TRANSCRIPT-SUMMARY-MUST-NOT-TRAVEL"
CWD = "/Users/jay/apps/lanes/example"


def request(host: str = "mac", generation: object = 2, **wake_over) -> dict:
    wake = {
        "wake_id": "w-1",
        "message_id": 639615408,
        "channel": "Work-In-Progress",
        "topic": "AFC-694 Operator cross-platform coordination",
        "sender": "Claude",
        "dm": False,
        "zulip_link": "https://example.invalid/m/1",
        "letter": "BB",
        "link_note": WR.LINK_NOTES["session-id"],
        "excerpt": BODY,
        "content": BODY,
        "body": BODY,
    }
    wake.update(wake_over)
    return {
        "platform": "grok-leader",
        "target": {"session_id": SID, "letter": "BB", "host": host, "generation": generation},
        "wake": wake,
    }


def held() -> dict:
    return {
        "found": True,
        "seat": "GROK",
        "native_session_id": SID,
        "label": "BB",
        "generation": 2,
        "verified_host": "mac",
        "state": "active",
    }


class Drive:
    """The grok-drive boundary:  list, then prompt only if the test's chat is idle."""

    def __init__(self, sessions: list[dict], prompt: dict | None = None) -> None:
        self.sessions = sessions
        self.prompt = {"queued": True, "ok": True} if prompt is None else prompt
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        kind = argv[2]
        if kind == self.fail_on:
            raise subprocess.TimeoutExpired(argv, timeout)
        if kind == "list":
            payload = {"ok": True, "count": len(self.sessions), "sessions": self.sessions}
            return 0, json.dumps(payload), ""
        if kind == "prompt":
            return 0, json.dumps(self.prompt), ""
        raise AssertionError(kind)

    def prompted(self) -> list[str] | None:
        for argv in self.calls:
            if argv[2] == "prompt":
                return argv
        return None


def row(state: str = "idle", **over) -> dict:
    base = {
        "sessionId": SID,
        "live": True,
        "turnState": state,
        "cwd": CWD,
        "lastTurnSummary": SUMMARY,
        "title": "BB: Operator Wake Help",
    }
    base.update(over)
    return base


class GrokWake(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="grok-wake-")
        self.env = {"HOME": self.tmp, "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "http://127.0.0.1:9"}
        self.registry = mock.Mock(return_value=held())
        patcher = mock.patch.object(WR.registry_api, "call", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, body: dict | None = None, drive: Drive | None = None, **kw):
        drive = drive or Drive([row()])
        answer = GW.deliver_request(body if body is not None else request(), env=self.env, runner=drive, **kw)
        return drive, answer

    def test_the_builder_line_matches_the_module(self) -> None:
        entry = SVC.BUILDERS["GROK"]
        self.assertEqual(
            (entry.platform, entry.module, entry.function, entry.handler, entry.session_re),
            ("grok-leader", "simple_tools.grok_wake", "router_platform", "deliver_request",
             SVC.UUID_SESSION),
        )
        self.assertEqual(GW.SEAT, "GROK")
        self.assertEqual(GW.SESSION_RE.pattern, entry.session_re)
        self.assertIsNotNone(GW.SESSION_RE.fullmatch(SID))
        self.assertIsNone(GW.SESSION_RE.fullmatch("sess_" + SID))
        ready = SVC.prepare("GROK", entry, self.env)
        self.assertLessEqual(set(WR.ROUTER_DEFAULTS), set(ready.defaults))
        self.assertLessEqual(GW.NEVER_RAN, ready.never_ran)
        self.assertIs(ready.defaults["delivery_enabled"], False)
        platform = GW.router_platform(self.env, ready.defaults)
        self.assertEqual(
            (platform.seat, platform.platform, platform.mention, platform.target_re.pattern),
            ("GROK", "grok-leader", "Grok", entry.session_re),
        )

    def test_only_a_mac_host_is_sent_over_ssh(self) -> None:
        settings = dict(GW.DEFAULTS, mac_ssh="mac-host")
        platform = GW.router_platform(self.env, settings)
        target = WR.Target(SID, letter="BB", host="mac", generation=2)
        wake = WR.Wake("w-1", message_id=1)
        remote = mock.Mock(return_value=WR.Delivery("sent", "ssh:grok-drive", ack="queued"))
        with mock.patch.object(GW.CW, "deliver_remote", remote):
            sent = platform.deliver(target, wake)
            refused = platform.deliver(WR.Target(SID, letter="BB", host="server", generation=2), wake)
        self.assertEqual((sent.outcome, sent.ack), ("sent", "queued"))
        remote.assert_called_once_with(self.env, settings, target, wake, platform="grok-leader")
        self.assertEqual((refused.outcome, refused.code), ("refused", "unknown-host"))
        self.assertIn("unknown-host", GW.NEVER_RAN)

    def test_an_idle_chat_is_prompted_with_metadata_and_the_ids_come_back_exact(self) -> None:
        drive, answer = self.deliver()
        self.assertEqual(
            (answer["wake_id"], answer["session_id"], answer["generation"]), ("w-1", SID, 2)
        )
        self.assertIs(type(answer["generation"]), int)
        self.assertEqual(
            (answer["delivery"]["outcome"], answer["delivery"]["ack"], answer["delivery"]["route"]),
            ("sent", "unknown", "grok-drive"),
        )
        self.registry.assert_called_once()
        _env, op, params = self.registry.call_args.args
        self.assertEqual(op, "resolve")
        self.assertEqual(params, {"platform": "grok-leader", "letter": "BB"})
        prompted = drive.prompted()
        self.assertIsNotNone(prompted)
        assert prompted is not None
        self.assertEqual(prompted[2:8], ["prompt", "--session-id", SID, "--cwd", CWD, "--prompt"])
        self.assertEqual(prompted[-2:], ["--from-name", "wake-router"])
        for flag in ("--queue", "--self", "--wait", "--await-reply"):
            self.assertNotIn(flag, prompted)
        text = prompted[prompted.index("--prompt") + 1]
        self.assertIn("639615408", text)
        self.assertIn(WR.LINK_NOTES["session-id"], text)
        self.assertIn("AFC-694 Operator cross-platform coordination", text)
        self.assertNotIn(BODY, text)
        self.assertNotIn(SUMMARY, text)
        self.assertNotIn("excerpt", text)
        joined = "\n".join(prompted)
        self.assertNotIn(BODY, joined)
        self.assertNotIn(SUMMARY, joined)
        self.assertEqual([call[2] for call in drive.calls], ["list", "prompt"])
        self.assertTrue(str(drive.calls[0][1]).endswith("grok-drive.py"))
        self.assertIn(self.tmp, drive.calls[0][1])

    def test_a_queued_acceptance_counts_even_when_ok_is_absent(self) -> None:
        _drive, answer = self.deliver(drive=Drive([row()], {"queued": True}))
        self.assertEqual(answer["delivery"]["outcome"], "sent")

    def test_registry_is_read_before_any_drive_call(self) -> None:
        order: list[str] = []

        def call(*_args, **_kwargs):
            order.append("registry")
            return held()

        def runner(argv, timeout):
            order.append(argv[2])
            return Drive([row()])(argv, timeout)

        self.registry.side_effect = call
        GW.deliver_request(request(), env=self.env, runner=runner)
        self.assertEqual(order, ["registry", "list", "prompt"])

    def test_a_released_chat_is_not_prompted(self) -> None:
        self.registry.return_value = {**held(), "generation": 9}
        drive, answer = self.deliver()
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("refused", WR.RELEASED))
        self.assertEqual(drive.calls, [])
        self.assertIn(WR.RELEASED, WR.NEVER_RAN)

    def test_a_registry_that_cannot_be_read_does_not_prompt(self) -> None:
        self.registry.side_effect = OSError("down")
        drive, answer = self.deliver()
        self.assertEqual(answer["delivery"],
                         {"outcome": "failed", "route": "", "ack": "unknown", "code": "registry-unavailable", "note": ""})
        self.assertEqual(drive.calls, [])

    def test_a_registry_with_no_key_is_not_configured(self) -> None:
        self.registry.return_value = {"error": "no key", "code": "not_configured"}
        drive, answer = self.deliver()
        self.assertEqual(answer["delivery"]["code"], WR.NOT_CONFIGURED)
        self.assertEqual(answer["delivery"]["outcome"], "failed")
        self.assertEqual(drive.calls, [])

    def test_another_host_is_refused_before_the_registry(self) -> None:
        drive, answer = self.deliver(request(host="server"))
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("refused", "unknown-host"))
        self.assertEqual(drive.calls, [])
        self.registry.assert_not_called()

    def test_a_working_or_needs_input_chat_is_busy_and_not_prompted(self) -> None:
        for state in ("working", "needs-input", "unknown"):
            with self.subTest(state=state):
                drive, answer = self.deliver(drive=Drive([row(state)]))
                self.assertEqual(answer["delivery"]["outcome"], "busy")
                self.assertEqual(answer["delivery"]["code"], "turn-" + state)
                self.assertIsNone(drive.prompted())

    def test_a_chat_that_is_not_live_is_refused_before_a_prompt(self) -> None:
        drive, answer = self.deliver(drive=Drive([row(live=False)]))
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("refused", "not-live"))
        self.assertIsNone(drive.prompted())
        self.assertIn("not-live", GW.NEVER_RAN)

    def test_a_missing_chat_on_a_complete_list_is_not_live(self) -> None:
        drive, answer = self.deliver(drive=Drive([]))
        self.assertEqual(answer["delivery"]["code"], "not-live")
        self.assertEqual(len(drive.calls), 1)

    def test_an_incomplete_list_without_the_chat_is_retried(self) -> None:
        drive = Drive([])

        def runner(argv, timeout):
            drive.calls.append(list(argv))
            return 0, json.dumps({"ok": False, "partial": True, "sessions": []}), ""

        answer = GW.deliver_request(request(), env=self.env, runner=runner)
        self.assertEqual(answer["delivery"]["code"], "drive-list-unavailable")
        self.assertEqual(answer["delivery"]["outcome"], "failed")

    def test_a_race_into_working_stays_busy(self) -> None:
        drive = Drive([row()], {"ok": False, "turnState": "working", "queued": False})
        _drive, answer = self.deliver(drive=drive)
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("busy", "turn-working"))
        self.assertIsNotNone(drive.prompted())

    def test_prompting_this_process_is_refused_before_anything_ran(self) -> None:
        drive = Drive([row()], {"ok": False, "self": True, "turnState": "idle"})
        _drive, answer = self.deliver(drive=drive)
        self.assertEqual(answer["delivery"]["code"], "self-session")
        self.assertIn("self-session", GW.NEVER_RAN)

    def test_a_prompt_timeout_is_final_and_a_list_timeout_is_retried(self) -> None:
        listing = Drive([row()])
        listing.fail_on = "list"
        _drive, answer = self.deliver(drive=listing)
        self.assertEqual(answer["delivery"]["code"], "drive-list-timeout")
        self.assertEqual(answer["delivery"]["outcome"], "failed")
        prompting = Drive([row()])
        prompting.fail_on = "prompt"
        _drive, answer = self.deliver(drive=prompting)
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("refused", "drive-timeout"))
        self.assertNotIn("drive-timeout", GW.NEVER_RAN)

    def test_a_bad_generation_is_refused_before_anything_runs(self) -> None:
        drive, answer = self.deliver(request(generation=True))
        self.assertEqual(answer["delivery"]["code"], "bad-request")
        self.assertIsNone(answer["generation"])
        self.assertEqual(answer["wake_id"], "w-1")
        self.assertEqual(drive.calls, [])
        self.registry.assert_not_called()

    def test_a_missing_cwd_is_refused_before_a_prompt(self) -> None:
        drive, answer = self.deliver(drive=Drive([row(cwd="")]))
        self.assertEqual(answer["delivery"]["code"], "no-cwd")
        self.assertIn("no-cwd", GW.NEVER_RAN)
        self.assertIsNone(drive.prompted())

    def test_the_drive_path_stays_under_home(self) -> None:
        path = GW._drive_path(self.env)
        self.assertEqual(
            path, Path(self.tmp) / "apps" / "grok-acp-runtime" / "grok-drive.py"
        )

    def test_grok_drive_path_is_settable(self) -> None:
        env = dict(self.env, GROK_DRIVE="/opt/fleet/grok-drive.py")
        drive, _answer = self.deliver()
        self.assertIn(self.tmp, drive.calls[0][1])
        drive = Drive([row()])
        GW.deliver_request(request(), env=env, runner=drive)
        self.assertEqual(drive.calls[0][1], "/opt/fleet/grok-drive.py")

    def test_ok_without_queued_is_sent_and_unconfirmed(self) -> None:
        _drive, answer = self.deliver(drive=Drive([row()], {"ok": True, "queued": False}))
        self.assertEqual(
            (answer["delivery"]["outcome"], answer["delivery"]["ack"]),
            ("sent", "unknown"),
        )

    def test_an_oserror_after_the_prompt_starts_is_final(self) -> None:
        def runner(argv, timeout):
            if argv[2] == "list":
                return Drive([row()])(argv, timeout)
            raise OSError("gone")

        answer = GW.deliver_request(request(), env=self.env, runner=runner)
        self.assertEqual(answer["delivery"]["outcome"], "refused")
        self.assertEqual(answer["delivery"]["code"], "drive-prompt-oserror")
        self.assertNotIn("drive-prompt-oserror", GW.NEVER_RAN)

    def test_a_held_session_lock_is_busy_and_does_not_prompt(self) -> None:
        import fcntl

        lock = GW._session_lock(self.env, SID)
        self.addCleanup(lock.close)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        drive, answer = self.deliver()
        self.assertEqual((answer["delivery"]["outcome"], answer["delivery"]["code"]),
                         ("busy", "session-locked"))
        self.assertEqual(drive.calls, [])
        self.registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
