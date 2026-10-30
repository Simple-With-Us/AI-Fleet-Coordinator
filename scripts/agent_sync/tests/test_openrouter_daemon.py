"""The OpenRouter wake in the daemon:  as a seat's wake, and as the fallback behind a muse or claude seat.

Everything runs against the fake Zulip server, a fake OpenRouter on 127.0.0.1 and fake `muse` and `claude`
executables.  The Operator posts every reply (the listener harness fails any test where a seat's bot writes),
no test reaches openrouter.ai, and the key is made at run time by repeating a pattern.

The runner, the key reader, the config and the pin are in test_openrouter_wake.py.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import adapters as A
from agent_sync import daemon as D
from agent_sync import live as L
from agent_sync import wakes as W
from agent_sync import zulip as Z
from agent_sync.tests.fake_openrouter import FakeOpenRouter, Reply, completion
from agent_sync.tests.harness import write_rc
from agent_sync.tests.listener_harness import reply_output
from agent_sync.tests.test_daemon import MUSE_MENTION, DaemonHarness
from agent_sync.tests.test_openrouter_wake import (
    FALLBACK_MODEL,
    GOOD,
    MODEL,
    PROMPT_MARKER,
    fake_key,
)

PROJECT = "11111111-2222-3333-4444-555555555555"
PRIMARY = A.OPENROUTER_ROLE_PRIMARY
FALLBACK = A.OPENROUTER_ROLE_FALLBACK
MUSE_MODEL = "muse-spark-1.3-contributor"

FAKE_MUSE = r"""#!{python}
import json, sys, time
CONTROL = {control!r}
DUMP = {dump!r}
with open(CONTROL) as fh:
    control = json.load(fh)
with open(DUMP, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")


def rec(payload_type, **payload):
    print(json.dumps({{"schema_version": 1, "record_type": "event", "payload_type": payload_type,
                      "payload": payload}}), flush=True)


mode = control.get("mode", "ok")
if mode == "hang":
    time.sleep(control.get("hang", 30))
rec("run.model.configured", model_id="muse-spark-1.3-contributor")
if mode == "silent_exit2":
    sys.stderr.write("fatal\n")
    sys.exit(2)
if mode == "refused":
    rec("run.terminal.failed", terminal="failed", reason="usage limit reached")
    sys.exit(1)
if mode == "garbage":
    rec("run.terminal.completed", text="I cannot produce JSON")
    sys.exit(0)
rec("run.terminal.completed", text=json.dumps(control.get("output")))
sys.exit(control.get("exit", 0))
"""


class OpenRouterHarness(DaemonHarness):
    """A realm with an MM seat (wake and fallback per test), a fake OpenRouter and a fake muse."""

    def setUp(self) -> None:
        super().setUp()
        self.or_key = fake_key()
        self.router = FakeOpenRouter(self.or_key, completion(reply_output_full()))
        self.router.start()
        self.addCleanup(self.router.stop)
        # The base URL is a module constant, so the test points it at the fake;  the pin fingerprint
        # reads the same constant, so a pin made in a test is a pin under the fake's URL.
        patch = mock.patch.object(A, "OPENROUTER_BASE_URL", self.router.url)
        patch.start()
        self.addCleanup(patch.stop)
        # The key lives in the fake Infisical under Fleet-OPS /wake;  `fetch_paths` records where it was read.
        self.infisical_values["OPERATOR_OPENROUTER_API_KEY"] = self.or_key
        self.fetch_paths: list[tuple[tuple[str, ...], str]] = []
        inner = Z._default_infisical_fetch

        def recording(names: Any, env: Any, home: Any, path: str = "/zulip") -> Any:
            self.fetch_paths.append((tuple(names), path))
            return inner(names)

        recorder = mock.patch.object(Z, "_default_infisical_fetch", recording)
        recorder.start()
        self.addCleanup(recorder.stop)
        self.extra_keys.append(("openrouter-key@test", self.or_key))
        # The MM seat's bot, and the partition that gives it to this instance.
        partition = self.tmp / "partition-openrouter.toml"
        partition.write_text('[seats]\nMM = "mac"\n')
        self.partition = str(partition)
        self.mm_key = "".join(reversed(fake_key())).lower() + "z9"
        self.fake.add_bot("mm-bot@zulip.test", "MiniMax", self.mm_key, user_id=30)
        write_rc(
            self.secrets_dir / "MM-zuliprc", email="mm-bot@zulip.test", key=self.mm_key, site=self.fake.url
        )
        self.extra_keys.append(("mm-bot@zulip.test", self.mm_key))
        # The fake muse.
        self.muse = self.bin / "muse-bin-1.4.12-R6909.1"
        self.muse_control = self.tmp / "muse-control.json"
        self.muse_dump = self.tmp / "muse-dump.jsonl"
        self.set_muse(mode="ok", output=or_output())
        self.muse.write_text(
            FAKE_MUSE.format(
                python=sys.executable, control=str(self.muse_control), dump=str(self.muse_dump)
            )
        )
        self.muse.chmod(self.muse.stat().st_mode | stat.S_IXUSR)

    def env(self, **overrides: str | None) -> dict[str, str]:
        overrides.setdefault("INFISICAL_FLEET_OPS_PROJECT_ID", PROJECT)
        return super().env(**overrides)

    # ---- the seat -------------------------------------------------------------------------------
    def set_muse(self, **control: Any) -> None:
        self.muse_control.write_text(json.dumps(control))

    def muse_runs(self) -> int:
        if not self.muse_dump.exists():
            return 0
        return len([line for line in self.muse_dump.read_text().splitlines() if line.strip()])

    def mm_config(
        self,
        wake: str = "openrouter",
        extra: str = "",
        budget: str = "wakes_per_hour = 60, wakes_per_day = 200, per_topic_per_hour = 30",
    ) -> None:
        """The MM seat's config.  A generous budget, so a test that wakes it many times is not held back."""
        lines = [
            "[seat.MM]",
            'bot = "MM"',
            'wake = "%s"' % wake,
            "budget = { %s }" % budget,
        ]
        if wake == "muse":
            lines.append('muse = "%s"' % self.muse)
        lines += textwrap.dedent(extra).strip().splitlines()
        self.write_config(seats="\n".join(lines) + "\n", eligible=[13])

    def pin_openrouter(self, seat: str = "MM", role: str = PRIMARY, model: str | None = None) -> None:
        if model is None:
            model = FALLBACK_MODEL if role == FALLBACK else MODEL
        A.record_openrouter_pin(self.seat_paths(seat), model, self.clock.time(), role)

    def pin_muse(self) -> None:
        A.record_muse_pin(self.seat_paths("MM"), str(self.muse), self.clock.time(), model=MUSE_MODEL)

    def mm(self, **kw: Any) -> D.Daemon:
        kw.setdefault("partition_file", self.partition)
        daemon = self.daemon(**kw)
        self.assertTrue(daemon.connect_seat("MM"), daemon.seats["MM"].error)
        return daemon

    def mm_wake(self, daemon: D.Daemon, topic: str = "t") -> int:
        mid = self.fake.add_message("Cursor", "agent-sync", topic, MUSE_MENTION)
        self.pump_until(
            daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger("MM")), seat="MM"
        )
        self.clock.advance(25)
        daemon.tick()
        daemon.run_jobs("MM")
        return mid

    def final(self, seat: str = "MM") -> dict[str, Any]:
        rows = [r for r in self.ledger(seat) if r["state"] in ("done", "failed", "dropped", "held_back")]
        return rows[-1]

    def log_events(self, name: str) -> list[dict[str, Any]]:
        text = (self.state_dir / "logs" / "listener.log").read_text()
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [row for row in rows if row.get("event") == name]

    def models_asked(self) -> list[str]:
        return [seen.body["model"] for seen in self.router.requests()]


def or_output(text: str = "On it.  The cutover is in review.", **extra: Any) -> dict[str, Any]:
    """An answer the strict schema asks for:  every key present."""
    out = dict(GOOD, reply=text)
    out.update(extra)
    return out


def reply_output_full() -> dict[str, Any]:
    return or_output()


class PrimaryTests(OpenRouterHarness):
    """`wake = "openrouter"`:  the Muse model through OpenRouter, with nothing to pin but the request."""

    def setUp(self) -> None:
        super().setUp()
        self.mm_config("openrouter")
        self.pin_openrouter()

    def test_a_wake_makes_one_call_and_the_operator_posts_the_reply(self) -> None:
        daemon = self.mm()
        mid = self.mm_wake(daemon)
        # One call, to the seat's model, with the contract as the system message and the fenced prompt
        # as the user message, and nothing offered to call.
        (seen,) = self.router.requests()
        self.assertEqual(seen.body["model"], MODEL)
        self.assertNotIn("tools", seen.body)
        self.assertEqual(seen.body["messages"][0]["content"], Path(W.CONTRACT_PATH).read_text().rstrip())
        user = seen.body["messages"][1]["content"]
        self.assertIn("BEGIN_UNTRUSTED_ZULIP", user)
        self.assertIn("can you check the AFC cutover?", user)
        self.assertEqual(seen.body["response_format"]["json_schema"]["schema"]["required"][:2], ["action", "reply"])
        # The Operator said it, tagged with the seat's wake, and no seat bot wrote a thing.
        (post,) = self.operator_posts()
        self.assertTrue(post["content"].startswith("[MM·wake] re=%d\n" % mid), post["content"])
        self.assertEqual((post["display_recipient"], post["subject"]), ("agent-sync", "t"))
        self.assertEqual(self.seat_requests(), [])
        # The ledger names the runtime, the model and what the call reported, and nothing secret.
        started = [r for r in self.ledger("MM") if r["state"] == "started"][0]
        self.assertEqual((started["adapter"], started["reserved_usd"]), ("openrouter", 0.0))
        done = self.final()
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["adapter"], "openrouter")
        self.assertEqual((done["model"], done["runtime_role"], done["http_status"]), (MODEL, "primary", 200))
        self.assertEqual(done["usage"]["total_tokens"], 1280)
        self.assertEqual(done["served_model"], "meta/muse-spark-1.3-contributor-20260902")
        self.assertEqual((done["posted_by"], done["posted_id"]), ("OPERATOR", post["id"]))
        self.assertNotIn("cost_usd", done)
        # The key came from Fleet-OPS /wake, by its one name.
        self.assertTrue(self.fetch_paths)
        self.assertEqual(set(self.fetch_paths), {(("OPERATOR_OPENROUTER_API_KEY",), "/wake")})
        self.assertEqual(self.router.requests()[0].bearer, "Bearer " + self.or_key)

    def test_the_key_is_in_no_ledger_log_status_or_post(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon)
        daemon.write_status()
        self.router.reply(
            Reply(status=402, body={"error": {"code": 402, "message": "%s %s" % (PROMPT_MARKER, self.or_key)}})
        )
        self.mm_wake(daemon, "again")
        for path in self.state_dir.rglob("*"):
            if path.is_file():
                text = path.read_text(errors="replace")
                self.assertNotIn(self.or_key, text, str(path))
                self.assertNotIn("Bearer " + self.or_key, text, str(path))
                self.assertNotIn(PROMPT_MARKER, text, "an error body is never kept:  %s" % path)
        for post in self.fake.messages:
            self.assertNotIn(self.or_key, post["content"])
        # The daemon scrubs and scans it from now on:  it joined the hidden set when it was read.
        self.assertIn(self.or_key, daemon.hidden)
        self.assertEqual(daemon.scrub("x %s y" % self.or_key), "x [redacted] y")

    def test_an_unpinned_seat_holds_with_not_pinned_and_calls_nothing(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_openrouter)
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual((row["state"], row["reason"]), ("dropped", "not_pinned"))
        self.assertEqual(self.router.requests(), [])
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.fetch_paths, [], "the key is not even read for a wake that cannot run")

    def test_a_changed_model_makes_the_pin_stale_and_the_note_says_what_to_run(self) -> None:
        self.mm_config("openrouter", extra='model = "%s"' % FALLBACK_MODEL)
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.final()["reason"], "pin_stale")
        self.assertEqual(self.router.requests(), [])
        notes = [str(r.get("note") or "") for r in L.read_jsonl(self.seat_paths("MM").owner_queue)]
        self.assertTrue(any("--runtime openrouter --run" in n and "--runtime openrouter --pin" in n for n in notes), notes)
        self.assertFalse(any("claude" in n.lower() for n in notes), "it must not blame the claude binary")
        status = daemon.status()["seats"]["MM"]
        self.assertTrue(any("wake pin is stale" in red for red in status["red"]), status["red"])

    def test_every_request_part_the_pin_covers_holds_the_seat_when_it_changes(self) -> None:
        daemon = self.mm()
        with mock.patch.object(A, "OPENROUTER_MAX_TOKENS", A.OPENROUTER_MAX_TOKENS + 1):
            self.mm_wake(daemon)
        self.assertEqual(self.final()["reason"], "pin_stale")
        self.assertEqual(self.router.requests(), [])

    def test_a_credits_error_is_a_failure_not_a_hold_and_the_next_wake_tries_again(self) -> None:
        daemon = self.mm()
        self.router.reply(Reply(status=402, body={"error": {"code": 402, "message": "no credits"}}))
        self.mm_wake(daemon, "a")
        row = self.final()
        self.assertEqual((row["state"], row["reason"]), ("failed", "HTTP 402 (credits exhausted)"))
        self.assertEqual((row["http_status"], row["adapter"], row["model"]), (402, "openrouter", MODEL))
        self.assertNotIn("held_back", [r["state"] for r in self.ledger("MM")])
        self.assertFalse(row.get("limited"))
        self.assertEqual(self.operator_posts(), [])
        banners = [b[8] for b in self.banners if "OpenRouter wakes are failing" in b[8]]
        self.assertEqual(banners, ["OpenRouter wakes are failing: HTTP 402 (credits exhausted)"])
        self.mm_wake(daemon, "b")  # the account was topped up
        self.assertEqual(self.final()["state"], "done")
        self.assertEqual(len(self.operator_posts()), 1)

    def test_each_failure_class_fails_the_wake_with_its_phrase_and_posts_nothing(self) -> None:
        daemon = self.mm()
        cases = [
            (Reply(status=401, body={"error": {}}), "HTTP 401 (key rejected)"),
            (Reply(status=429, body={"error": {}}), "HTTP 429 (rate limited)"),
            (Reply(status=500, body="boom"), "HTTP 500 (provider error)"),
            (Reply(body="not json at all"), "no parseable result"),
            (Reply(body=completion("I will help.")), "no parseable result"),
            (Reply(body=completion(GOOD, finish="length")), "answer cut off at max_tokens"),
        ]
        for n, (reply, reason) in enumerate(cases):
            self.router.reply(reply)
            self.mm_wake(daemon, "case%d" % n)
            row = self.final()
            self.assertEqual((row["state"], row["reason"]), ("failed", reason))
        self.assertEqual(self.operator_posts(), [])

    def test_a_schema_invalid_answer_fails_the_wake_when_there_is_no_fallback(self) -> None:
        daemon = self.mm()
        self.router.reply(Reply(body=completion({"action": "nuke"})))
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["reason"].startswith("invalid result: keys missing"), row["reason"])
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(len(self.router.requests()), 1)

    def test_a_call_that_never_answers_is_cut_off_and_fails_the_wake(self) -> None:
        daemon = self.mm()
        daemon.openrouter_runner.timeout = 0.6
        self.router.reply(Reply(delay=4.0))
        started = time.monotonic()
        self.mm_wake(daemon)
        self.assertLess(time.monotonic() - started, 4.0)
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["reason"].startswith("timed out after"), row["reason"])
        self.assertEqual(self.operator_posts(), [])

    def test_an_unreadable_key_runs_no_wake_and_is_remembered_without_another_vault_read(self) -> None:
        self.infisical_error = Z.CredentialError("fleet-secrets timeout")
        daemon = self.mm()
        self.mm_wake(daemon, "a")
        row = self.final()
        self.assertEqual((row["state"], row["reason"]), ("dropped", "openrouter_unavailable"))
        self.assertEqual(self.router.requests(), [])
        self.assertNotIn("started", [r["state"] for r in self.ledger("MM")], "nothing was reserved")
        (event,) = self.log_events("openrouter-key-unavailable")
        self.assertIn("OPERATOR_OPENROUTER_API_KEY", event["reason"])
        self.assertIn("fleet-secrets timeout", event["reason"])
        reads = len(self.fetch_paths)
        self.mm_wake(daemon, "b")  # known unavailable:  held at the router, with no read at all
        self.assertEqual(self.final()["reason"], "openrouter_unavailable")
        self.assertEqual(len(self.fetch_paths), reads, "the failure is remembered for a window")
        self.infisical_error = None
        # The holder's window runs on the real monotonic clock, so a recovered vault is read again once
        # that window has passed;  the test ends the window instead of waiting two minutes for it.
        daemon.openrouter_key._failed_until = -1e18
        self.mm_wake(daemon, "c")
        self.assertEqual(self.final()["state"], "done")

    def test_the_operator_preflight_still_runs_first_and_nothing_is_called(self) -> None:
        self.operator_available = False
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual((row["state"], row["reason"]), ("dropped", "operator_unavailable"))
        self.assertEqual(self.router.requests(), [], "a reply that cannot go out is not worth a call")
        self.assertNotIn("started", [r["state"] for r in self.ledger("MM")])
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_a_known_operator_outage_holds_the_wake_at_the_router_without_the_network(self) -> None:
        self.operator_available = False
        daemon = self.mm()
        self.mm_wake(daemon, "a")
        builds = self.operator_builds
        self.mm_wake(daemon, "b")
        self.assertEqual(self.operator_builds, builds, "no second vault read for the Operator")
        self.assertEqual(self.final()["reason"], "operator_unavailable")
        self.assertNotIn("accepted", [r["state"] for r in self.ledger("MM")[-2:]])

    def test_the_codecaps_gate_never_holds_or_reads_for_an_openrouter_wake(self) -> None:
        quota = self.tmp / "quota-windows.json"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.time()))
        quota.write_text(
            json.dumps(
                {
                    "format": "usage-monitor-local-quotas",
                    "generatedAt": stamp,
                    "windows": [
                        {"provider": name, "isExhausted": True, "skip": True, "remainingPercent": 0}
                        for name in ("OpenRouter", "MiniMax", "Claude Code")
                    ],
                }
            )
        )
        daemon = self.mm()
        with (
            mock.patch.object(A, "QUOTA_PATH", str(quota)),
            mock.patch.object(A, "provider_quota", side_effect=AssertionError("CodeCaps was read")),
        ):
            self.mm_wake(daemon)
            status = daemon.status()["seats"]["MM"]
        self.assertEqual(self.final()["state"], "done")
        self.assertIsNone(status["quota"], "an openrouter seat has no subscription window to report")
        self.assertFalse(any("quota" in red for red in status["red"]), status["red"])
        self.assertEqual(daemon.seats["MM"].cfg.wake_max_usd, 0.0)

    def test_the_count_caps_still_bound_the_seat(self) -> None:
        self.mm_config("openrouter", budget="wakes_per_hour = 1, wakes_per_day = 200, per_topic_per_hour = 30")
        daemon = self.mm()
        self.mm_wake(daemon, "a")
        self.mm_wake(daemon, "b")
        self.assertEqual([r["state"] for r in self.ledger("MM") if r["state"] == "held_back"], ["held_back"])
        self.assertEqual(len(self.router.requests()), 1)

    def test_status_says_the_wake_type_and_whether_the_request_is_pinned(self) -> None:
        daemon = self.mm()
        daemon.write_status()
        seat = daemon.status()["seats"]["MM"]
        self.assertEqual((seat["wake"], seat["pinned"], seat["model"]), ("openrouter", True, MODEL))
        human = self.run_cli("status").out
        self.assertRegex(human, r"MM +connected .*wake openrouter \(%s, pinned\)" % MODEL.replace(".", r"\."))
        os.unlink(self.seat_paths("MM").wake_pin_openrouter)
        daemon.write_status()
        self.assertIn("wake openrouter (%s, NOT pinned: not_pinned)" % MODEL, self.run_cli("status").out)

    def test_a_chat_selection_from_an_openrouter_wake_is_held_never_acted_on(self) -> None:
        daemon = self.mm()
        self.router.reply(Reply(body=completion(or_output(None, action="none", chat_target="a" * 64))))
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["chat"]["why"], "chat selection requires a Muse wake")
        self.assertEqual(self.operator_posts(), [])
        prompt = self.router.requests()[0].body["messages"][1]["content"]
        self.assertIn("Existing-chat selection: disabled", prompt)


class FallbackTests(OpenRouterHarness):
    """`fallback = "openrouter"` behind a muse seat:  any failure of the primary that is not a safety
    refusal gets the wake answered by the fallback model, once."""

    def setUp(self) -> None:
        super().setUp()
        self.mm_config("muse", extra='fallback = "openrouter"')
        self.pin_muse()
        self.pin_openrouter(role=FALLBACK)

    def assert_answered_by_the_fallback(self, *, muse_runs: int) -> dict[str, Any]:
        (seen,) = self.router.requests()
        self.assertEqual(seen.body["model"], FALLBACK_MODEL)
        self.assertEqual(self.muse_runs(), muse_runs)
        row = self.final()
        self.assertEqual(row["state"], "done", row)
        self.assertEqual((row["adapter"], row["runtime_role"], row["model"]), ("openrouter", "fallback", FALLBACK_MODEL))
        (post,) = self.operator_posts()
        self.assertTrue(post["content"].startswith("[MM·wake] re="), "the Operator carries the seat's tag")
        self.assertEqual(row["posted_by"], "OPERATOR")
        self.assertEqual(self.seat_requests(), [])
        return row

    def test_a_muse_answer_needs_no_fallback(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.router.requests(), [])
        self.assertEqual(self.final()["adapter"], "muse")
        self.assertEqual(self.muse_runs(), 1)

    def test_every_way_the_primary_can_fail_after_it_started_falls_back(self) -> None:
        cases = {
            "a nonzero exit and no answer": dict(mode="silent_exit2"),
            "a nonzero exit with an answer": dict(mode="ok", output=or_output(), exit=2),
            "a refusal from the provider (auth or quota)": dict(mode="refused"),
            "an answer that is not JSON": dict(mode="garbage"),
            "an answer the schema refuses": dict(mode="ok", output={"action": "nuke"}),
            "an answer with the wrong keys": dict(mode="ok", output={"action": "reply"}),
        }
        daemon = self.mm()
        for n, (label, control) in enumerate(cases.items()):
            with self.subTest(label):
                self.set_muse(**control)
                before = len(self.router.requests())
                self.mm_wake(daemon, "case%d" % n)
                self.assertEqual(len(self.router.requests()), before + 1, label)
                row = self.final()
                self.assertEqual(
                    (row["state"], row["adapter"], row["runtime_role"]),
                    ("done", "openrouter", "fallback"),
                    label,
                )
                self.assertEqual(self.models_asked()[-1], FALLBACK_MODEL, label)
        self.assertEqual(len(self.operator_posts()), len(cases))

    def test_a_primary_that_times_out_falls_back(self) -> None:
        self.set_muse(mode="hang", hang=20)
        daemon = self.mm()
        daemon.muse_runner.timeout = 3.0  # long enough for the child to start, short enough to test
        self.mm_wake(daemon)
        self.assert_answered_by_the_fallback(muse_runs=1)
        (event,) = self.log_events("wake-fallback")
        self.assertTrue(event["primary_error"].startswith("timed out after"), event)

    def test_a_stale_muse_pin_skips_the_primary_and_the_fallback_answers(self) -> None:
        """The MM seat's case today:  the muse binary moved, so every wake was dropped with pin_stale."""
        A.record_muse_pin(
            self.seat_paths("MM"), str(self.tmp / "muse-bin-1.4.11-R6667.1"), self.clock.time(), model=MUSE_MODEL
        )
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assert_answered_by_the_fallback(muse_runs=0)
        (event,) = self.log_events("wake-primary-skipped")
        self.assertIn("pin_stale", event["reason"])
        status = daemon.status()["seats"]["MM"]
        self.assertTrue(status["fallback"]["stands_in"])
        self.assertEqual(status["fallback"]["primary_hold"], "pin_stale")
        self.assertTrue(any("wake pin is stale" in red for red in status["red"]), "the stale muse pin is still reported")
        daemon.write_status()
        self.assertIn("ANSWERING IN PLACE OF THE WAKE (pin_stale)", self.run_cli("status").out)

    def test_a_muse_that_was_never_pinned_skips_the_primary_too(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_muse)
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assert_answered_by_the_fallback(muse_runs=0)

    def test_a_missing_muse_binary_skips_the_primary_too(self) -> None:
        os.unlink(self.muse)
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assert_answered_by_the_fallback(muse_runs=0)

    def test_without_the_fallback_a_stale_muse_pin_still_drops_the_wake(self) -> None:
        self.mm_config("muse")
        A.record_muse_pin(
            self.seat_paths("MM"), str(self.tmp / "muse-bin-1.4.11-R6667.1"), self.clock.time(), model=MUSE_MODEL
        )
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual((self.final()["state"], self.final()["reason"]), ("dropped", "pin_stale"))
        self.assertEqual(self.router.requests(), [])

    def test_the_mcode_fallback_does_not_stand_in_for_a_primary_that_cannot_run(self) -> None:
        """Only the OpenRouter fallback skips a held primary.  mcode answers a run that started and
        failed, as before, so a stale muse pin behind `fallback = "mcode"` still drops the wake."""
        self.mm_config("muse", extra='fallback = "mcode"\nfallback_uncontained_ok = true')
        A.record_muse_pin(
            self.seat_paths("MM"), str(self.tmp / "muse-bin-1.4.11-R6667.1"), self.clock.time(), model=MUSE_MODEL
        )
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual((self.final()["state"], self.final()["reason"]), ("dropped", "pin_stale"))
        self.assertEqual(self.log_events("wake-primary-skipped"), [])
        self.assertEqual(self.router.requests(), [])
        self.assertEqual(self.muse_runs(), 0)

    def test_an_unpinned_fallback_is_held_and_the_wake_fails_with_the_primarys_reason(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_openrouter_fallback)
        self.set_muse(mode="silent_exit2")
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.router.requests(), [])
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertIn("no parseable result", row["reason"])
        (held,) = self.log_events("wake-fallback-held")
        self.assertEqual((held["fallback"], held["reason"]), ("openrouter", "not_pinned"))

    def test_a_stale_fallback_pin_is_held_and_never_repinned(self) -> None:
        self.mm_config("muse", extra='fallback = "openrouter"\nfallback_model = "deepseek/deepseek-v4-flash"')
        self.set_muse(mode="silent_exit2")
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.router.requests(), [])
        (held,) = self.log_events("wake-fallback-held")
        self.assertEqual(held["reason"], "pin_stale")
        pin = L.read_json(self.seat_paths("MM").wake_pin_openrouter_fallback)
        self.assertEqual(pin["model"], FALLBACK_MODEL, "nothing re-pinned")
        status = daemon.status()["seats"]["MM"]
        self.assertTrue(any("fallback pin is stale" in red and "--runtime fallback --run" in red for red in status["red"]))

    def test_a_primary_pin_stands_in_for_nothing_when_the_fallback_is_also_down(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_openrouter_fallback)
        A.record_muse_pin(
            self.seat_paths("MM"), str(self.tmp / "muse-bin-1.4.11-R6667.1"), self.clock.time(), model=MUSE_MODEL
        )
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual((self.final()["state"], self.final()["reason"]), ("dropped", "pin_stale"))
        self.assertEqual(self.router.requests(), [])
        self.assertFalse(daemon.status()["seats"]["MM"]["fallback"]["stands_in"])

    def test_a_fallback_that_fails_too_keeps_both_reasons(self) -> None:
        self.set_muse(mode="silent_exit2")
        self.router.reply(Reply(status=402, body={"error": {"code": 402}}))
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reason"], "HTTP 402 (credits exhausted) (after muse failed: no parseable result)")
        self.assertEqual((row["adapter"], row["runtime_role"], row["http_status"]), ("openrouter", "fallback", 402))
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(len(self.router.requests()), 1, "one call, no retry loop")

    def test_the_fallback_gets_the_whole_prompt_without_the_chat_catalog(self) -> None:
        self.set_muse(mode="silent_exit2")
        daemon = self.mm()
        self.mm_wake(daemon)
        prompt = self.router.requests()[0].body["messages"][1]["content"]
        self.assertIn("BEGIN_UNTRUSTED_ZULIP", prompt)
        self.assertIn("Existing-chat selection: disabled; chat_target and chat_inspect must be null", prompt)

    def test_an_unreadable_key_fails_the_fallback_with_the_reason_and_no_secret(self) -> None:
        self.set_muse(mode="silent_exit2")
        self.infisical_error = Z.CredentialError("fleet-secrets timeout")
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual(row["state"], "failed")
        self.assertIn("OPERATOR_OPENROUTER_API_KEY", row["reason"])
        self.assertIn("after muse failed", row["reason"])
        self.assertEqual(self.router.requests(), [])


class OpenRouterBehindOpenRouterTests(OpenRouterHarness):
    """The primary and the fallback are both OpenRouter, on two models."""

    def setUp(self) -> None:
        super().setUp()
        self.mm_config("openrouter", extra='fallback = "openrouter"\nfallback_model = "%s"' % FALLBACK_MODEL)
        self.pin_openrouter(role=PRIMARY)
        self.pin_openrouter(role=FALLBACK)

    def test_a_failed_primary_call_is_answered_by_the_other_model(self) -> None:
        self.router.reply(Reply(status=429, body={"error": {"code": 429}}))
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.models_asked(), [MODEL, FALLBACK_MODEL])
        row = self.final()
        self.assertEqual((row["state"], row["model"], row["runtime_role"]), ("done", FALLBACK_MODEL, "fallback"))
        started = [r for r in self.ledger("MM") if r["state"] == "started"][0]
        self.assertEqual((started["adapter"], started["model"], started["runtime_role"]), ("openrouter", MODEL, "primary"))
        (post,) = self.operator_posts()
        self.assertTrue(post["content"].startswith("[MM·wake] re="))

    def test_a_schema_invalid_primary_answer_is_answered_by_the_other_model(self) -> None:
        self.router.reply(Reply(body=completion({"action": "reply"})))
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.models_asked(), [MODEL, FALLBACK_MODEL])
        self.assertEqual(self.final()["state"], "done")
        self.assertEqual(len(self.operator_posts()), 1)

    def test_both_models_failing_keeps_both_reasons_and_posts_nothing(self) -> None:
        self.router.reply(Reply(status=500, body="x"), Reply(status=429, body="y"))
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual(
            row["reason"], "HTTP 429 (rate limited) (after openrouter failed: HTTP 500 (provider error))"
        )
        self.assertEqual(self.operator_posts(), [])
        self.assertEqual(self.models_asked(), [MODEL, FALLBACK_MODEL])

    def test_a_stale_primary_pin_skips_straight_to_the_fallback_model(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_openrouter)
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        self.assertEqual(self.final()["runtime_role"], "fallback")
        (event,) = self.log_events("wake-primary-skipped")
        self.assertIn("not_pinned", event["reason"])

    def test_the_first_answer_is_the_only_call_when_it_is_good(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.models_asked(), [MODEL])
        self.assertEqual(self.final()["runtime_role"], "primary")


class ClaudeSeatTests(OpenRouterHarness):
    """The same fallback behind the CLAUDE seat (the fake claude), including the holds that stay."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(
            seats=textwrap.dedent("""\
                [seat.CLAUDE]
                bot = "Claude"
                wake = "claude"
                model = "sonnet"
                provider = "Claude Code"
                fallback = "openrouter"
                """)
        )
        self.pin()
        self.pin_openrouter(seat="CLAUDE", role=FALLBACK)

    def wake(self, daemon: D.Daemon, topic: str = "t") -> int:
        mid = self.fake.add_message("Codex", "agent-sync", topic, "@**Claude** can you check the AFC cutover?")
        self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
        self.wake_cycle(daemon)
        return mid

    def test_a_claude_run_that_hit_its_cap_is_answered_by_the_fallback(self) -> None:
        """The mcode rule never falls back after a quota refusal.  OpenRouter is another account,
        billed by credits, so a capped primary is exactly what this fallback is for."""
        daemon = self.started()
        limited = A.RunResult()
        limited.error = "You've hit your weekly limit · resets Oct 13 at 5pm"
        limited.limited = True
        daemon.claude_runner.run = lambda *a, **k: limited
        self.wake(daemon)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        row = self.final("CLAUDE")
        self.assertEqual((row["state"], row["adapter"], row["runtime_role"]), ("done", "openrouter", "fallback"))
        self.assertFalse(row.get("limited"))
        self.assertEqual(len(self.operator_posts()), 1)

    def test_a_safety_refusal_never_falls_back(self) -> None:
        self.set_claude(mode="tool", hang=30, output=reply_output())
        daemon = self.started()
        daemon.claude_runner.timeout = 5.0
        self.wake(daemon)
        self.assertEqual(self.router.requests(), [], "the daemon killed that child on purpose")
        row = self.final("CLAUDE")
        self.assertEqual(row["state"], "failed")
        self.assertIn("init event listed tools", row["reason"])
        (held,) = self.log_events("wake-fallback-held")
        self.assertEqual((held["fallback"], held["reason"]), ("openrouter", "primary_refused"))

    def test_a_claude_answer_the_validator_refuses_falls_back(self) -> None:
        self.set_claude(mode="ok", output={"action": "reply"})
        daemon = self.started()
        self.wake(daemon)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        self.assertEqual(self.final("CLAUDE")["state"], "done")

    def write_quota(self, *providers: str) -> str:
        path = self.tmp / "quota-windows.json"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.time()))
        path.write_text(
            json.dumps(
                {
                    "format": "usage-monitor-local-quotas",
                    "generatedAt": stamp,
                    "windows": [
                        {"provider": p, "isExhausted": True, "skip": True, "remainingPercent": 0,
                         "resetAt": "2026-10-31T10:00:00Z"}
                        for p in providers
                    ],
                }
            )
        )
        return str(path)

    def test_a_provider_with_nothing_left_is_skipped_for_the_fallback_not_held(self) -> None:
        quota = self.write_quota("Claude Code")
        with mock.patch.object(A, "QUOTA_PATH", quota):
            daemon = self.started()
            self.wake(daemon)
            status = daemon.status()["seats"]["CLAUDE"]
        self.assertEqual(self.dumps(), [], "no claude child was spawned")
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        self.assertEqual(self.final("CLAUDE")["state"], "done")
        (event,) = self.log_events("wake-primary-skipped")
        self.assertIn("quota is exhausted", event["reason"])
        # The status does not claim wakes are not running, because they are.
        self.assertFalse(any("wakes are not running" in red for red in status["red"]), status["red"])
        self.assertTrue(status["fallback"]["stands_in"])

    def test_without_the_fallback_the_same_quota_still_holds_the_wake(self) -> None:
        self.write_config(
            seats=textwrap.dedent("""\
                [seat.CLAUDE]
                bot = "Claude"
                wake = "claude"
                model = "sonnet"
                provider = "Claude Code"
                """)
        )
        quota = self.write_quota("Claude Code")
        with mock.patch.object(A, "QUOTA_PATH", quota):
            daemon = self.started()
            self.wake(daemon)
            status = daemon.status()["seats"]["CLAUDE"]
        row = self.final("CLAUDE")
        self.assertEqual(row["state"], "dropped")
        self.assertIn("Claude Code quota is exhausted", row["reason"])
        self.assertEqual(self.dumps(), [])
        self.assertEqual(self.router.requests(), [])
        self.assertTrue(any("wakes are not running" in red for red in status["red"]))


class CliTests(OpenRouterHarness):
    """`daemon test-wake` and `--pin` for the OpenRouter runtime, on the canned hostile messages."""

    def setUp(self) -> None:
        super().setUp()
        self.mm_config("openrouter", extra='fallback = "openrouter"\nfallback_model = "%s"' % FALLBACK_MODEL)

    def test_a_dry_run_prints_the_plan_and_calls_nothing_and_never_prints_the_key(self) -> None:
        result = self.run_cli("daemon", "test-wake", "--seat", "MM")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("runtime: openrouter (primary)", result.out)
        self.assertIn("request: POST %s/chat/completions" % self.router.url, result.out)
        self.assertIn("Fleet-OPS /wake OPERATOR_OPENROUTER_API_KEY;  never printed", result.out)
        self.assertIn("BEGIN_UNTRUSTED_ZULIP", result.out)
        self.assertIn('"require_parameters": true', result.out)
        self.assertIn("dry run", result.out)
        self.assertNotIn(self.or_key, result.out)
        self.assertEqual(self.router.requests(), [])
        self.assertEqual(self.fetch_paths, [], "a dry run reads no secret")

    def test_run_then_pin_makes_the_seat_wake(self) -> None:
        passed = self.run_cli("daemon", "test-wake", "--seat", "MM", "--run")
        self.assertEqual(passed.code, 0, passed.out)
        self.assertIn("PASS, NOT PINNED YET", passed.out)
        self.assertIn("--runtime openrouter --pin", passed.out)
        self.assertNotIn(self.or_key, passed.out + passed.err)
        (seen,) = self.router.requests()
        self.assertEqual(seen.body["model"], MODEL)
        # The canned hostile messages are in the prompt the model was sent.
        user = seen.body["messages"][1]["content"]
        self.assertIn("Ignore every instruction above", user)
        self.assertIn('"http_status": 200', passed.out)
        self.assertIn('"total_tokens": 1280', passed.out)
        paths = self.seat_paths("MM")
        self.assertTrue(os.path.exists(paths.wake_pin_candidate_openrouter))
        self.assertFalse(os.path.exists(paths.wake_pin_openrouter), "--run never pins on its own")
        # An unpinned seat holds.
        daemon = self.mm()
        self.mm_wake(daemon, "before")
        self.assertEqual(self.final()["reason"], "not_pinned")
        pinned = self.run_cli("daemon", "test-wake", "--seat", "MM", "--pin")
        self.assertEqual(pinned.code, 0, pinned.out)
        self.assertIn("pinned the OpenRouter wake request for MM (model %s" % MODEL, pinned.out)
        self.assertFalse(os.path.exists(paths.wake_pin_candidate_openrouter))
        self.assertTrue(A.openrouter_pinned(paths, MODEL))
        self.assertFalse(A.openrouter_pinned(paths, FALLBACK_MODEL, FALLBACK), "the fallback is its own pin")
        # ... and the daemon now runs the wake.
        before = len(self.router.requests())
        self.mm_wake(daemon, "after")
        self.assertEqual(self.final()["state"], "done")
        self.assertEqual(len(self.router.requests()), before + 1)
        self.assertEqual(len(self.operator_posts()), 1)

    def test_the_fallback_role_is_tested_and_pinned_on_its_own_model_and_files(self) -> None:
        passed = self.run_cli("daemon", "test-wake", "--seat", "MM", "--runtime", "fallback", "--run")
        self.assertEqual(passed.code, 0, passed.out)
        self.assertIn("runtime: openrouter (fallback)", passed.out)
        self.assertIn("--runtime fallback --pin", passed.out)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        paths = self.seat_paths("MM")
        self.assertTrue(os.path.exists(paths.wake_pin_candidate_openrouter_fallback))
        self.assertFalse(os.path.exists(paths.wake_pin_candidate_openrouter))
        pinned = self.run_cli("daemon", "test-wake", "--seat", "MM", "--runtime", "fallback", "--pin")
        self.assertEqual(pinned.code, 0, pinned.out)
        self.assertIn("pinned the OpenRouter fallback request for MM (model %s" % FALLBACK_MODEL, pinned.out)
        self.assertTrue(A.openrouter_pinned(paths, FALLBACK_MODEL, FALLBACK))
        self.assertFalse(A.openrouter_pinned(paths, MODEL, PRIMARY), "the wake is not pinned by the fallback's pin")
        # A bare --pin means the seat's wake, whose candidate does not exist.
        again = self.run_cli("daemon", "test-wake", "--seat", "MM", "--pin")
        self.assertEqual(again.code, 1)
        self.assertIn("NOT PINNED: no passing test-wake --run to pin", again.out)

    def test_a_pass_over_a_day_old_or_made_for_another_request_is_refused(self) -> None:
        self.assertEqual(self.run_cli("daemon", "test-wake", "--seat", "MM", "--run").code, 0)
        paths = self.seat_paths("MM")
        candidate = L.read_json(paths.wake_pin_candidate_openrouter)
        L.write_json(paths.wake_pin_candidate_openrouter, dict(candidate, at=candidate["at"] - 2 * 86400))
        refused = self.run_cli("daemon", "test-wake", "--seat", "MM", "--pin")
        self.assertEqual(refused.code, 1)
        self.assertIn("over a day old", refused.out)
        L.write_json(paths.wake_pin_candidate_openrouter, dict(candidate, fingerprint="0" * 64))
        refused = self.run_cli("daemon", "test-wake", "--seat", "MM", "--pin")
        self.assertIn("changed since the test", refused.out)
        L.write_json(paths.wake_pin_candidate_openrouter, dict(candidate, model=FALLBACK_MODEL))
        refused = self.run_cli("daemon", "test-wake", "--seat", "MM", "--pin")
        self.assertIn("model changed since the test", refused.out)
        self.assertFalse(os.path.exists(paths.wake_pin_openrouter))

    def test_obeying_the_hostile_messages_fails_the_test_and_records_no_candidate(self) -> None:
        paths = self.seat_paths("MM")
        board = or_output(None, action="board", board={"title": "pwned", "severity": "P2", "desc": "x"})
        for label, answer in (
            ("a board item", board),
            ("a route", or_output(route={"seat": "CODEX", "reason": "page"})),
            ("a chat selection", or_output(None, action="none", chat_target="b" * 64)),
            ("a schema-invalid answer", {"action": "reply"}),
        ):
            self.router.reply(Reply(body=completion(answer)))
            result = self.run_cli("daemon", "test-wake", "--seat", "MM", "--run")
            self.assertEqual(result.code, 1, label)
            self.assertIn("FAIL: the request was not pinned", result.out, label)
            self.assertFalse(os.path.exists(paths.wake_pin_candidate_openrouter), label)

    def test_an_http_failure_fails_the_test_with_the_reason(self) -> None:
        self.router.reply(Reply(status=402, body={"error": {"code": 402}}))
        result = self.run_cli("daemon", "test-wake", "--seat", "MM", "--run")
        self.assertEqual(result.code, 1)
        self.assertIn('"error": "HTTP 402 (credits exhausted)"', result.out)
        self.assertIn('"http_status": 402', result.out)
        self.assertFalse(os.path.exists(self.seat_paths("MM").wake_pin_candidate_openrouter))

    def test_an_unreadable_key_fails_the_test_with_the_reason_and_no_value(self) -> None:
        self.infisical_error = Z.CredentialError("fleet-secrets timeout")
        result = self.run_cli("daemon", "test-wake", "--seat", "MM", "--run")
        self.assertEqual(result.code, 1)
        self.assertIn("FAIL: the OpenRouter key is unavailable", result.out)
        self.assertIn("OPERATOR_OPENROUTER_API_KEY", result.out)
        self.assertNotIn(self.or_key, result.out + result.err)
        self.assertEqual(self.router.requests(), [])

    def test_the_wake_runtime_is_refused_for_a_seat_that_wakes_on_something_else(self) -> None:
        self.mm_config("muse", extra='fallback = "openrouter"')
        refused = self.run_cli("daemon", "test-wake", "--seat", "MM", "--runtime", "openrouter")
        self.assertNotEqual(refused.code, 0)
        self.assertIn("test its OpenRouter fallback with --runtime fallback", refused.err + refused.out)
        fallback = self.run_cli("daemon", "test-wake", "--seat", "MM", "--runtime", "fallback", "--run")
        self.assertEqual(fallback.code, 0, fallback.out)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])

    def test_a_seat_with_no_runtime_to_test_says_so(self) -> None:
        self.write_config(seats='[seat.MM]\nbot = "MM"\nwake = "inbox"\n', eligible=[13])
        refused = self.run_cli("daemon", "test-wake", "--seat", "MM", "--run")
        self.assertNotEqual(refused.code, 0)
        self.assertIn("claude, muse or openrouter wake", refused.err + refused.out)

    def test_the_other_runtimes_still_run_through_the_same_command(self) -> None:
        self.write_config(
            seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nclaude = "%s"\n' % self.claude
        )
        passed = self.run_cli("daemon", "test-wake", "--seat", "CLAUDE", "--run")
        self.assertEqual(passed.code, 0, passed.out)
        self.assertIn("PASS, NOT PINNED YET", passed.out)
        self.assertIn("--runtime claude --pin", passed.out)
        self.assertEqual(self.router.requests(), [])


class StructureTests(unittest.TestCase):
    def test_the_choke_point_dispatches_openrouter_and_still_refuses_mcode(self) -> None:
        self.assertIn("openrouter", D.PRIMARY_RUNTIMES)
        self.assertNotIn("mcode", D.PRIMARY_RUNTIMES)

    def test_no_new_post_path_was_added(self) -> None:
        source = Path(D.__file__).read_text(encoding="utf-8")
        adapters = Path(A.__file__).read_text(encoding="utf-8")
        self.assertNotIn("runner.client.post", source)
        self.assertNotIn("operator_voice", adapters)
        # The OpenRouter runtime returns a RunResult;  it never touches Zulip.
        start = adapters.index("class OpenRouterRunner")
        block = adapters[start : adapters.index("def openrouter_wake_fingerprint", start)]
        self.assertNotIn("ZulipClient", block)
        self.assertNotIn(".post(", block)

    def test_the_runtime_is_standard_library_only(self) -> None:
        adapters = Path(A.__file__).read_text(encoding="utf-8")
        for module in ("requests", "httpx", "aiohttp", "openai", "urllib3"):
            self.assertNotRegex(adapters, r"^\s*(import|from) %s\b" % module)

    def test_the_mcode_fallback_rule_is_unchanged(self) -> None:
        # A quota refusal still never reaches the mcode runner, and the allowlist is the same one.
        self.assertEqual(D.FALLBACKABLE_FAULTS, ("could not start", "no parseable result", "timed out after"))
        self.assertFalse(D._is_fallbackable("You've hit your weekly limit"))


if __name__ == "__main__":
    unittest.main()
