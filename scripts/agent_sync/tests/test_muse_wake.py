"""The `muse` wake type (AFC-291):  the argv, the MSP envelope, the pin discipline, the provider
quota gate, and the subscription-backed capping decision.

Everything here is deterministic and offline except `LiveMuseTest`, which runs the real
`muse exec` against the real wake schema -- the one test that proves the runtime, not the mock.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_sync import adapters as A
from agent_sync import config as C
from agent_sync import live as L
from agent_sync import wakes as W


def _record(payload_type: str, **payload: object) -> str:
    return json.dumps({"schema_version": 1, "record_type": "event", "payload_type": payload_type,
                       "payload": payload})


class MuseArgvTest(unittest.TestCase):
    def test_argv_states_the_locked_down_wake(self) -> None:
        argv = A.muse_argv("/usr/local/bin/muse", "muse-spark-1.3-contributor")
        self.assertEqual(argv[0], "/usr/local/bin/muse")
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[2:4], ["-c", "run.toolset=[]"])
        # Every switch that makes "read this topic, touch nothing" true rather than inferred.
        self.assertIn("--approval-mode", argv)
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "never")
        for switch in ("--disable-shell", "--disable-write", "--disable-web-tools", "--disable-reminders", "--no-session-log",
                       "--no-foreign-personal-context", "--json"):
            self.assertIn(switch, argv)
        # The sandbox is left ON:  muse has no --disable-sandbox in this argv.
        self.assertNotIn("--disable-sandbox", argv)
        self.assertNotIn("--yolo", argv)
        self.assertEqual(argv[argv.index("--output-schema") + 1], A.MUSE_SCHEMA_SLOT)

    def test_the_contributor_tier_is_the_default_and_the_dear_one_is_never_chosen(self) -> None:
        self.assertEqual(C.MUSE_DEFAULT_MODEL, "muse-spark-1.3-contributor")
        # The catalog's plain muse-spark-1.3 is 15x dearer;  a default must never name it.
        self.assertNotEqual(C.MUSE_DEFAULT_MODEL, "muse-spark-1.3")

    def test_effort_is_a_tier_muse_actually_runs(self) -> None:
        # Gate 0:  `none` is accepted by `model-profile show` and REJECTED by a real run on the
        # meta provider (exit 2).  `minimal` is the lowest tier that actually runs.
        self.assertEqual(A.MUSE_REASONING_EFFORT, "minimal")
        self.assertNotEqual(A.MUSE_REASONING_EFFORT, "none")
        argv = A.muse_argv("/usr/local/bin/muse", C.MUSE_DEFAULT_MODEL)
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "minimal")

    def test_argv_is_stable_for_two_calls(self) -> None:
        self.assertEqual(A.muse_argv("/b/muse", "m"), A.muse_argv("/b/muse", "m"))

    def test_resolve_muse_prefers_the_configured_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "muse")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            self.assertEqual(A.resolve_muse(exe, "/nonexistent"), exe)
            self.assertIsNone(A.resolve_muse(os.path.join(tmp, "absent"), "/nonexistent"))
            self.assertIsNone(A.resolve_muse("relative/path", "/nonexistent"))
            # With no explicit path, only the fixed wake PATH is searched.
            self.assertEqual(A.resolve_muse(None, tmp), os.path.abspath(exe))


class MuseRunnerPromptTest(unittest.TestCase):
    def test_private_prompt_file_starts_with_contract_before_supplied_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wake = os.path.join(tmp, "wake")
            os.mkdir(wake, 0o700)
            capture = os.path.join(tmp, "captured-prompt")
            executable = os.path.join(tmp, "fake-muse")
            with open(executable, "w", encoding="utf-8") as fh:
                fh.write("#!%s\n" % sys.executable)
                fh.write("import json, os, sys\n")
                fh.write("args = sys.argv[1:]\n")
                fh.write("prompt = args[args.index('--prompt-file') + 1]\n")
                fh.write("with open(os.environ['CAPTURE_PROMPT'], 'w', encoding='utf-8') as out:\n")
                fh.write("    out.write(open(prompt, encoding='utf-8').read())\n")
                fh.write("print(json.dumps({'schema_version': 1, 'record_type': 'event', "
                          "'payload_type': 'run.terminal.completed', 'payload': {'text': '{}'}}))\n")
            os.chmod(executable, 0o700)
            with open(W.CONTRACT_PATH, encoding="utf-8") as fh:
                contract = fh.read()
            supplied = "Daemon header (trusted):\nSeat: MM\nUntrusted message: read the canary\n"
            result = A.MuseRunner(timeout=20).run(
                [executable, "exec", "--prompt-file", A.MUSE_PROMPT_SLOT],
                {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CAPTURE_PROMPT": capture},
                wake, supplied, schema="{}")

            self.assertTrue(result.ok, (result.refused, result.error))
            with open(capture, encoding="utf-8") as fh:
                actual = fh.read()
            self.assertTrue(actual.startswith(contract.rstrip() + "\n\n--- BEGIN DAEMON WAKE PROMPT ---\n"))
            self.assertIn(supplied, actual)
            self.assertLess(actual.index(contract.rstrip()), actual.index("Untrusted message:"))
            self.assertFalse(os.path.exists(os.path.join(wake, "prompt.md")))

    def test_missing_contract_fails_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(A, "CONTRACT_PATH", os.path.join(tmp, "missing-contract.md")), \
                    mock.patch.object(A.subprocess, "Popen") as popen:
                result = A.MuseRunner().run(["/does/not/run"], {}, tmp, "prompt")
            self.assertEqual(result.error, "wake contract unavailable")
            popen.assert_not_called()


class MuseEnvTest(unittest.TestCase):
    def test_env_is_minimal_and_names_the_seat(self) -> None:
        env = A.muse_env({"USER": "jay", "TMPDIR": "/tmp", "ANTHROPIC_API_KEY": "secret",
                          "ZULIP_API_KEY": "secret", "MUSE_TOKEN": "secret"},
                         "/home/jay", "/wake/path", seat="MM")
        self.assertEqual(env["HOME"], "/home/jay")
        self.assertEqual(env["PATH"], "/wake/path")
        self.assertEqual(env["AGENT_LAUNCHER"], A.WAKE_LAUNCHER)
        self.assertEqual(env["AGENT_LAUNCH_SEAT"], "MM")
        self.assertEqual(env["AGENT_SEAT"], "MM")
        self.assertEqual(env["AGENT_SYNC_ATTACH"], "0")
        # No inherited identity or vendor credential may survive into the run.
        for leaked in ("ANTHROPIC_API_KEY", "ZULIP_API_KEY", "MUSE_TOKEN"):
            self.assertNotIn(leaked, env)

    def test_without_a_seat_no_identity_is_injected(self) -> None:
        env = A.muse_env({"USER": "jay"}, "/home/jay", "/wake/path")
        self.assertNotIn("AGENT_SEAT", env)
        self.assertNotIn("AGENT_LAUNCHER", env)


class MuseEnvelopeTest(unittest.TestCase):
    """The MSP records, not claude's stream-json."""

    def test_reads_the_final_answer_and_the_model(self) -> None:
        lines = [_record("run.model.configured", model_id="muse-spark-1.3-contributor"),
                 _record("status", text="starting"),
                 _record("run.terminal.completed",
                         text='{"action":"none","board":null,"owner_note":null,"reply":null,'
                              '"risk":null,"route":null}')]
        answer, why, models = A.parse_muse_records(lines)
        self.assertIsNone(why)
        self.assertEqual(models, ["muse-spark-1.3-contributor"])
        self.assertEqual(W.validate(A.parse_muse_result(answer))[1], None)

    def test_a_populated_result_survives_the_round_trip(self) -> None:
        payload = {"action": "board", "reply": "Filed it.", "owner_note": None, "risk": None,
                   "route": None, "board": {"title": "muse Gate 0 timing", "severity": "P3", "desc": "d"}}
        answer, why, _ = A.parse_muse_records([_record("run.terminal.completed", text=json.dumps(payload))])
        self.assertIsNone(why)
        result, err = W.validate(A.parse_muse_result(answer))
        self.assertIsNone(err)
        self.assertEqual(result["action"], "board")
        self.assertEqual(result["board"]["severity"], "P3")

    def test_a_non_completed_terminal_is_a_refusal_that_names_itself(self) -> None:
        answer, why, _ = A.parse_muse_records(
            [_record("run.terminal.cancelled", reason="the run was stopped")])
        self.assertIsNone(answer)
        self.assertIn("cancelled", why or "")

    def test_malformed_and_unknown_records_are_ignored_not_fatal(self) -> None:
        lines = ["not json at all", "", "[]", json.dumps({"payload_type": "run.model.configured"}),
                 _record("something.new", text="ignored"),
                 _record("run.terminal.completed", text='{"action":"none"}')]
        answer, why, models = A.parse_muse_records(lines)
        self.assertIsNone(why)
        self.assertIsNotNone(answer)
        self.assertEqual(models, [])

    def test_no_terminal_record_is_no_answer(self) -> None:
        answer, why, _ = A.parse_muse_records([_record("status", text="working")])
        self.assertIsNone(answer)
        self.assertIsNone(why)

    def test_parse_result_is_lenient_about_a_fence(self) -> None:
        self.assertEqual(A.parse_muse_result('```json\n{"action":"none"}\n```'), {"action": "none"})
        self.assertIsNone(A.parse_muse_result("no object here"))
        self.assertIsNone(A.parse_muse_result(None))


class MusePinTest(unittest.TestCase):
    """Its own binary pin, and it never self-heals (the AFC-256 discipline)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = L.SeatPaths(self.tmp.name, "MM")
        os.makedirs(self.paths.seat_dir, exist_ok=True)

    def _muse(self, name: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        return path

    def test_never_verified_holds_and_says_why(self) -> None:
        state = A.muse_pin_state(self.paths, self._muse("muse"))
        self.assertFalse(state["pinned"])
        self.assertEqual(state["why"], "not_pinned")

    def test_no_binary_is_its_own_reason(self) -> None:
        self.assertEqual(A.muse_pin_state(self.paths, None)["why"], "no_muse")

    def test_a_claude_pin_can_never_stand_in_for_a_muse_one(self) -> None:
        claude = self._muse("claude")
        A.record_pin(self.paths, claude, time.time())
        state = A.muse_pin_state(self.paths, self._muse("muse"))
        self.assertFalse(state["pinned"])
        self.assertEqual(state["why"], "not_pinned")

    def test_pinned_only_after_a_person_pins(self) -> None:
        muse = self._muse("muse")
        self.assertFalse(A.muse_pinned(self.paths, muse))
        A.record_muse_pin(self.paths, muse, time.time(), model=C.MUSE_DEFAULT_MODEL)
        self.assertTrue(A.muse_pinned(self.paths, muse))

    def test_a_moved_binary_is_stale_not_self_healed(self) -> None:
        first = self._muse("muse")
        A.record_muse_pin(self.paths, first, time.time())
        moved = self._muse("muse2")  # the realpath changed under us, as an auto-update does
        state = A.muse_pin_state(self.paths, moved)
        self.assertFalse(state["pinned"])
        self.assertEqual(state["why"], "pin_stale")
        self.assertEqual(state["pinned_muse"], A.binary_pin(first))
        self.assertEqual(state["muse"], A.binary_pin(moved))

    def test_pin_requires_a_passing_candidate_within_the_day(self) -> None:
        muse = self._muse("muse")
        self.assertIn("no passing test-wake", A.muse_pin_candidate(self.paths, muse, time.time()))
        A.record_muse_candidate(self.paths, muse, time.time() - 100000)
        self.assertIn("over a day old", A.muse_pin_candidate(self.paths, muse, time.time()))
        A.record_muse_candidate(self.paths, self._muse("other"), time.time())
        self.assertIn("binary changed", A.muse_pin_candidate(self.paths, muse, time.time()))
        A.record_muse_candidate(self.paths, muse, time.time())
        self.assertIsNone(A.muse_pin_candidate(self.paths, muse, time.time()))
        self.assertTrue(A.muse_pinned(self.paths, muse))


class QuotaGateTest(unittest.TestCase):
    """No reason to wake an agent that cannot run -- checked before the subprocess spawns."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, windows: list[dict], *, age: float = 60.0) -> str:
        path = os.path.join(self.tmp.name, "quota.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": "usage-monitor-local-quotas", "version": 1, "producer": "codecaps",
                       "generatedAt": _iso(self.now - age), "windows": windows}, fh)
        return path

    def test_a_fresh_exhaustion_blocks_and_names_the_reset(self) -> None:
        path = self._write([{"provider": "Claude Code", "isExhausted": True, "skip": True,
                             "remainingPercent": 0, "resetAt": "2026-10-13T21:59:59Z"}])
        block = A.quota_block("Claude Code", self.now, path)
        self.assertIsNotNone(block)
        self.assertIn("exhausted", block)

    def test_an_available_provider_never_blocks(self) -> None:
        path = self._write([{"provider": "MiniMax", "isExhausted": False, "skip": False,
                             "remainingPercent": 34.0}])
        self.assertIsNone(A.quota_block("MiniMax", self.now, path))

    def test_a_stale_snapshot_never_blocks(self) -> None:
        # A quota source that is down must not silence the fleet:  an old reading is not a verdict.
        path = self._write([{"provider": "Claude Code", "isExhausted": True, "skip": True}], age=3600)
        self.assertIsNone(A.quota_block("Claude Code", self.now, path))

    def test_a_future_dated_snapshot_never_blocks(self) -> None:
        path = self._write([{"provider": "Claude Code", "isExhausted": True}], age=-3600)
        self.assertIsNone(A.quota_block("Claude Code", self.now, path))

    def test_a_missing_malformed_or_unknown_provider_never_blocks(self) -> None:
        self.assertIsNone(A.quota_block("Claude Code", self.now, os.path.join(self.tmp.name, "absent")))
        broken = os.path.join(self.tmp.name, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(A.quota_block("Claude Code", self.now, broken))
        path = self._write([{"provider": "MiniMax", "isExhausted": False}])
        self.assertIsNone(A.quota_block("Nobody At All", self.now, path))

    def test_exhaustion_is_the_producers_own_verdict_never_a_derived_zero(self) -> None:
        # 0% with neither flag means a passed reset or an unknown remainder, not a refusal.
        path = self._write([{"provider": "Grok", "isExhausted": False, "skip": False,
                             "remainingPercent": 0.0}])
        state = A.provider_quota("Grok", self.now, path)
        self.assertTrue(state["known"])
        self.assertFalse(state["exhausted"])
        self.assertIsNone(A.quota_block("Grok", self.now, path))

    def test_a_symlink_is_refused(self) -> None:
        real = self._write([{"provider": "Claude Code", "isExhausted": True}])
        link = os.path.join(self.tmp.name, "link.json")
        os.symlink(real, link)
        self.assertIsNone(A.quota_block("Claude Code", self.now, link))

    def test_the_muse_provider_is_never_capped_by_the_gate(self) -> None:
        # muse runs on the MiniMax subscription the user already pays for;  the gate is for the
        # providers whose quota the snapshot actually speaks to.
        path = self._write([{"provider": "MiniMax", "isExhausted": True, "skip": True}])
        self.assertIsNone(A.quota_block("minimax", self.now, path))

    def test_the_generated_at_reader_survives_dst(self) -> None:
        # The naive strptime reading was wrong by an hour twice a year, which would make a fresh
        # reading look stale.  UTC is the only correct reading of the trailing Z.
        self.assertAlmostEqual(A._quota_generated_at("2026-10-11T23:12:55Z"), 1791760375.0, places=0)
        self.assertIsNone(A._quota_generated_at("not a time"))
        self.assertIsNone(A._quota_generated_at(None))


class SubscriptionCappingTest(unittest.TestCase):
    """The deliberate decision (AFC-291):  no dollar reserve for a subscription-backed seat."""

    def test_a_muse_seat_reserves_nothing(self) -> None:
        cfg = _config_with_wake("muse")
        self.assertEqual(cfg.seats["MM"].wake_max_usd, 0.0)

    def test_a_claude_seat_still_reserves_its_maximum(self) -> None:
        self.assertEqual(_config_with_wake("claude").seats["MM"].wake_max_usd, C.WAKE_MAX_BUDGET_USD)

    def test_the_count_caps_are_unchanged_and_still_the_spam_guard(self) -> None:
        budget = _config_with_wake("muse").seats["MM"].budget
        self.assertEqual(budget["wakes_per_hour"], 6)
        self.assertEqual(budget["wakes_per_day"], 40)
        self.assertEqual(budget["per_topic_per_hour"], 2)
        self.assertEqual(budget["owner_per_day"], 20)
        self.assertEqual(budget["owner_per_topic_per_hour"], 6)

    def test_a_muse_seat_is_still_bounded_by_the_count_caps(self) -> None:
        # The dollar axis is inert, so prove the count axis is what actually stops it.
        budget = _config_with_wake("muse").seats["MM"].budget
        now = time.time()
        views = {"w%d" % i: {"state": "done", "owner": False, "topic_key": "t", "cost_usd": 0.0,
                              "times": {"accepted": now - 60}} for i in range(6)}
        self.assertEqual(W.budget_block(views, budget, owner=False, key="other", now=now, run_max=0.0),
                         "wakes_per_hour")

    def test_usd_per_day_never_blocks_a_muse_seat(self) -> None:
        # Every wake costs 0, so a ceiling of dollars spent against dollars spent is a real ceiling
        # that is never reached, rather than a fictional reserve.
        cfg = _config_with_wake("muse")
        budget = dict(cfg.seats["MM"].budget)
        self.assertGreater(budget["usd_per_day"], 0)
        self.assertIsNone(W.budget_block({}, budget, owner=True, key="t", now=time.time(), run_max=0.0))


class MuseConfigTest(unittest.TestCase):
    def test_muse_is_a_worker_wake(self) -> None:
        self.assertIn("muse", C.KNOWN_WAKES)
        self.assertIn("muse", C.WORKER_WAKES)

    def test_a_muse_seat_defaults_to_the_contributor_tier(self) -> None:
        self.assertEqual(_config_with_wake("muse").seats["MM"].model, C.MUSE_DEFAULT_MODEL)

    def test_the_binary_and_provider_keys_belong_to_a_muse_seat_only(self) -> None:
        cfg = _config_with_wake("claude", extra={"muse": "/usr/local/bin/muse"})
        self.assertTrue(any("applies only to wake" in e for e in cfg.errors))

    def test_a_relative_muse_path_is_refused(self) -> None:
        cfg = _config_with_wake("muse", extra={"muse": "muse"})
        self.assertTrue(any("muse must be an absolute path" in e for e in cfg.errors))

    def test_held_back_catch_up_applies_to_a_muse_seat(self) -> None:
        # A muse seat runs a turn, so it is exactly the case catch_up exists for.
        self.assertTrue(_config_with_wake("muse").seats["MM"].held_back["catch_up"])


class LiveMuseTest(unittest.TestCase):
    """The one test that touches the real runtime.  Skipped unless MUSE_WAKE_LIVE=1."""

    def test_the_real_wake_schema_round_trips_through_muse_exec(self) -> None:
        if os.environ.get("MUSE_WAKE_LIVE") != "1":
            self.skipTest("set MUSE_WAKE_LIVE=1 to run the real muse exec")
        home = os.path.expanduser("~")
        wake_path = A.default_wake_path(home)
        muse = A.resolve_muse(None, wake_path)
        if not muse:
            self.skipTest("no muse binary on the wake PATH")
        work = tempfile.mkdtemp(prefix="afc291-live-")
        os.chmod(work, 0o700)
        prompt = "Daemon header (trusted):\nSeat: MM\n" \
            'Where: channel "agent-sync", topic "AFC-291 muse headless wake type"\n' \
            "Board filing: disabled; never use action board\n" \
            "Routing: disabled (no owner trigger); leave route null\n" \
            "Return only the JSON object the schema describes.\n"
        result = A.MuseRunner(timeout=300).run(A.muse_argv(A.binary_pin(muse), C.MUSE_DEFAULT_MODEL),
                                                A.muse_env(os.environ, home, wake_path, seat="MM"),
                                                work, prompt, schema=W.schema_text(["CLAUDE", "MM"]))
        self.assertTrue(result.ok, "muse refused or failed: %s" % (result.refused or result.error))
        self.assertEqual(result.models, [C.MUSE_DEFAULT_MODEL])
        self.assertEqual(result.cost_usd, 0.0)  # subscription-backed:  no marginal cost to report
        _, why = W.validate(result.parsed)
        self.assertIsNone(why)


def _iso(when: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config_with_wake(wake: str, extra: dict | None = None) -> C.Config:
    # `provider` is the muse seat's CodeCaps key, so only a muse seat gets one by default;  a
    # claude seat asking for it is the case MuseConfigTest checks is refused.
    seat: dict = {"bot": "MiniMax", "wake": wake}
    if wake == "muse":
        seat["provider"] = "Claude Code"
    seat.update(extra or {})
    return C.from_dict({"daemon": {"owner_user_id": 12}, "seat": {"MM": seat}})


if __name__ == "__main__":
    unittest.main()
