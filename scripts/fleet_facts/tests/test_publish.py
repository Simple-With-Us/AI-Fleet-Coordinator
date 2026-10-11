"""The publish cycle:  documents, signing, the repository gate, the writes, the status file, and every way it stops.

Everything outside is scripted:  a fake `gh` (ghfake.py), a scripted lane doctor and open-loops, a fake K1 key,
a fixed clock.  No network, no real HOME, no Infisical (`apps_provider` and `key_provider` are always passed in)."""
import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import tempfile
import shutil
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fleet_facts import publish as p
from fleet_facts import repo as repomod
from fleet_facts import runner, safety, signing
from fleet_facts.runner import Result

from .builders import doctor_checkout, doctor_report
from .ghfake import REPO, FakeGitHub

SIGNER = "fixture-signing-key-0123456789abcdefABCDEF"
NOW = datetime(2026, 10, 11, 17, 30, 0, tzinfo=timezone.utc)      # Sun, Oct 11, 12:30pm Central
USERS = "/" + "Users" + "/someone"
LANE_PATH = USERS + "/apps/lanes/Example/claude-stale-lane"


def iso(hours_ago: float) -> str:
    from datetime import timedelta
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_secret() -> str:
    return "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5"       # shaped like a GitHub token, built at run time


class Machine:
    """The scripted outside world of one cycle."""

    def __init__(self, gh: FakeGitHub) -> None:
        self.gh = gh
        self.calls: list[list[str]] = []
        self.lane_delay = 0.0
        self.lane_report = doctor_report(
            [doctor_checkout(LANE_PATH, unpushed=3, last_change_at=iso(30), branch="claude/stale-branch", safety="UNKNOWN")],
            generated_at=iso(0.1))
        self.autopush = {"lanes": [{"lane": "claude-snap", "repo": "example-org/example-repo", "branch": "claude/snap",
                                    "snapshot_only": True, "idle_hours": 1.0}],
                         "refused": [{"lane": "claude-refused", "reason": "diverged:  the remote has commits this lane does not " * 3}]}
        self.needs_owner = [{"identifier": "FFX-12", "seat": "CODEX", "state": "replied", "replied": {"kind": "reaction"}}]
        self.list_text = "# Full list\n\n- Example item\n"
        self.fail_open_loops = None

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        name = os.path.basename(argv[0])
        if name == "gh":
            return self.gh(argv, timeout=timeout)
        if name == "lane":
            if self.lane_delay:
                time.sleep(self.lane_delay)
            return Result(0, json.dumps(self.lane_report), "")
        if name == "fleet-autopush":
            return Result(0, json.dumps(self.autopush), "")
        if name == "needs-owner":
            return Result(0, json.dumps(self.needs_owner), "")
        if name == "open-loops":
            if self.fail_open_loops == argv[1]:
                return Result(1, "", "boom")
            out = argv[argv.index("--out") + 1]
            Path(out).write_text(self.list_text if argv[1] == "render" else "{}", encoding="utf-8")
            return Result(0, "", "")
        return Result(None, reason="not found")

    def names(self) -> list[str]:
        return [os.path.basename(c[0]) for c in self.calls]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        for d in (self.home, self.bin, self.home / "apps"):
            d.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin})
        patcher.start()
        self.addCleanup(patcher.stop)
        extra = mock.patch("fleet_facts.runner.extra_dirs", return_value=[str(self.home / ".local" / "bin")])
        extra.start()
        self.addCleanup(extra.stop)
        self.program(self.home / "apps" / "lane")
        for name in ("fleet-autopush", "needs-owner", "open-loops"):
            self.program(self.bin / name)
        self.gh = FakeGitHub()
        self.machine = Machine(self.gh)
        self.state = self.home / ".local" / "state" / "fleet-facts"
        self.sleeps: list[float] = []

    @staticmethod
    def program(path: Path) -> None:
        path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        path.chmod(0o755)

    def cycle(self, **over):
        args = dict(run=self.machine, now=NOW, key_provider=lambda: SIGNER, load1=lambda: 1.0, sleep=self.sleeps.append, repo=REPO,
                    gh="gh", apps_provider=lambda: [])
        args.update(over)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            outcome = p.run_cycle(**args)
        self.log = out.getvalue()
        return outcome

    def status(self) -> dict:
        return json.loads((self.state / "mac-facts-publish.json").read_text(encoding="utf-8"))


class Published(Sandbox):
    def test_three_files_are_written_in_order_and_signed(self):
        outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("published", 0), outcome.reason)
        self.assertEqual(self.gh.put_paths(), [p.FULL_LIST, p.GB_FACTS, p.FULL_FACTS])
        self.assertEqual(outcome.files, {p.FULL_LIST: "created", p.GB_FACTS: "created", p.FULL_FACTS: "created"})
        for rel in (p.GB_FACTS, p.FULL_FACTS):
            document = json.loads(self.gh.written(rel))
            self.assertTrue(signing.verify(document, SIGNER, signed_for=rel), rel)
            self.assertEqual(document["generated_at"], "2026-10-11T17:30:00Z")
            self.assertEqual(document["parts"], list(p.PARTS))
        self.assertEqual(self.gh.written(p.FULL_LIST).decode(), self.machine.list_text)

    def test_the_gate_runs_before_the_first_write_and_the_writes_are_never_forced(self):
        self.cycle()
        first_write = next(i for i, c in enumerate(self.gh.calls) if "-X" in c)
        gate = [c for c in self.gh.calls[:first_write]]
        self.assertTrue(any(c[1:3] == ["repo", "view"] for c in gate))
        self.assertTrue(any("hooks" in " ".join(c) for c in gate))
        self.assertTrue(any("installations" in " ".join(c) for c in gate))
        for call in self.gh.calls:
            self.assertNotIn("force", " ".join(call).lower())

    def test_the_json_carries_the_full_list_hash_and_the_markdown_matches_it(self):
        self.cycle()
        info = json.loads(self.gh.written(p.GB_FACTS))["full_list"]
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["file"], p.FULL_LIST)
        self.assertEqual(info["sha256"], signing.sha256_hex(self.gh.written(p.FULL_LIST)))
        self.assertEqual(info["bytes"], len(self.gh.written(p.FULL_LIST)))

    def test_the_grok_bot_copy_has_no_mac_paths_and_the_full_copy_keeps_them(self):
        self.cycle()
        gb = self.gh.written(p.GB_FACTS).decode()
        full = self.gh.written(p.FULL_FACTS).decode()
        for needle in ("/" + "Users", "/" + "Volumes", "~/"):
            self.assertNotIn(needle, gb)
        self.assertIn("claude-stale-lane", gb)
        self.assertIn("/" + "Users" + "/someone/apps/lanes", full)
        for rel in (p.GB_FACTS, p.FULL_FACTS):
            self.assertEqual(json.loads(self.gh.written(rel))["kind"], {p.GB_FACTS: "mac-facts-gb", p.FULL_FACTS: "mac-facts"}[rel])

    def test_gb_quotes_are_cut_to_eighty_characters_and_the_full_copy_keeps_the_text(self):
        self.cycle()
        gb = json.loads(self.gh.written(p.GB_FACTS))
        full = json.loads(self.gh.written(p.FULL_FACTS))
        gb_reason = gb["autopush"]["document"]["refused"][0]["reason"]
        full_reason = full["autopush"]["document"]["refused"][0]["reason"]
        self.assertEqual(len(gb_reason), 80)
        self.assertGreater(len(full_reason), 80)

    def test_the_documents_carry_the_parts_the_consumer_reads(self):
        self.cycle()
        document = json.loads(self.gh.written(p.FULL_FACTS))
        for name in ("lanes", "autopush", "backup", "needs_owner"):
            self.assertIn(name, document["sources"])
        self.assertEqual(document["sources"]["lanes"]["status"], "ok")
        self.assertEqual(document["sources"]["autopush"]["status"], "ok")
        self.assertEqual(document["sources"]["backup"]["status"], "absent")
        self.assertIn("attention", document["lanes"])
        self.assertEqual(document["sources"]["publish"]["status"], "ok")

    def test_the_status_file_and_the_log_hold_no_facts_and_no_key(self):
        self.cycle()
        status = self.status()
        self.assertEqual(status["result"], "published")
        self.assertFalse(status["stale"])
        self.assertEqual(status["last_published_at"], "2026-10-11T17:30:00Z")
        self.assertEqual(status["last_published_generated_at"], "2026-10-11T17:30:00Z")
        self.assertEqual(status["at_local"], "Sun, Oct 11, 12:30pm")
        self.assertEqual(status["files"][p.GB_FACTS], "created")
        self.assertEqual(status["sources"]["lanes"], "ok")
        self.assertEqual(stat.S_IMODE((self.state / "mac-facts-publish.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        everything = json.dumps(status) + self.log
        for needle in (SIGNER, "claude-stale-lane", "claude/stale-branch", "diverged", "someone"):
            self.assertNotIn(needle, everything)
        self.assertIn("12:30pm", self.log)

    def test_a_second_run_with_the_same_input_leaves_every_file_unchanged(self):
        self.cycle()
        shutil.rmtree(self.home / ".cache")       # no lane report to reuse, so the second run sees what the first saw
        again = self.cycle(now=NOW)
        self.assertEqual(again.files, {p.FULL_LIST: "unchanged", p.GB_FACTS: "unchanged", p.FULL_FACTS: "unchanged"})
        self.assertEqual(len(self.gh.puts), 3, "no second write")

    def test_a_reused_lane_report_is_said_so_in_the_facts(self):
        self.cycle()
        self.cycle(now=NOW)
        self.assertTrue(json.loads(self.gh.written(p.FULL_FACTS))["lanes"]["reused_report"])

    def test_no_open_loops_means_no_list_and_the_json_says_so(self):
        (self.bin / "open-loops").unlink()
        outcome = self.cycle()
        self.assertEqual(outcome.result, "published")
        self.assertNotIn(p.FULL_LIST, self.gh.put_paths())
        self.assertTrue(outcome.files[p.FULL_LIST].startswith("skipped"))
        info = json.loads(self.gh.written(p.GB_FACTS))["full_list"]
        self.assertEqual(info["status"], "absent")
        self.assertNotIn("sha256", info)

    def test_a_failed_render_is_reported_and_does_not_stop_the_json(self):
        self.machine.fail_open_loops = "render"
        outcome = self.cycle()
        self.assertEqual(outcome.result, "published")
        info = json.loads(self.gh.written(p.FULL_FACTS))["full_list"]
        self.assertEqual(info["status"], "failed")
        self.assertIn("open-loops render exited 1", info["reason"])

    def test_a_secret_in_the_list_is_withheld_but_the_json_is_still_published(self):
        self.machine.list_text = "# Full list\n\n- note " + fake_secret() + "\n"
        outcome = self.cycle()
        self.assertEqual(outcome.result, "published")
        self.assertNotIn(p.FULL_LIST, self.gh.put_paths())
        info = json.loads(self.gh.written(p.GB_FACTS))["full_list"]
        self.assertEqual(info["status"], "failed")
        self.assertIn("secret-shaped", info["reason"])
        self.assertNotIn(fake_secret(), json.dumps(info))

    def test_the_failed_lane_doctor_is_published_as_failed_not_as_zero(self):
        self.machine.lane_report = doctor_report([], generated_at=iso(0.1), gh="error")
        self.cycle()
        document = json.loads(self.gh.written(p.GB_FACTS))
        self.assertEqual(document["sources"]["lanes"]["status"], "failed")
        self.assertNotIn("lanes", document, "a failed source carries no counts, so a consumer cannot read nothing-wrong into it")

    def test_a_write_failure_stops_the_rest_and_says_which(self):
        self.gh.put_fail[p.GB_FACTS] = ["500", "500"]
        outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("failed", 4))
        self.assertEqual(outcome.files, {p.FULL_LIST: "created", p.GB_FACTS: "failed"})
        self.assertNotIn(p.FULL_FACTS, self.gh.put_paths())
        status = self.status()
        self.assertTrue(status["stale"])
        self.assertIsNone(status["last_published_at"])


class Refusals(Sandbox):
    def assertNothingSent(self):
        self.assertEqual(self.gh.puts, [])
        self.assertEqual(self.gh.write_calls(), [])

    def test_no_key_means_nothing_is_published_and_nothing_is_even_gathered(self):
        def no_key():
            raise signing.KeyUnavailable("FLEET_FACTS_SIGNING_KEY is unavailable (unreachable:cli-missing)")
        outcome = self.cycle(key_provider=no_key)
        self.assertEqual((outcome.result, outcome.exit_code), ("no-key", 3))
        self.assertIn("nothing was published", outcome.reason)
        self.assertEqual(self.machine.calls, [])
        self.assertEqual(self.status()["result"], "no-key")
        self.assertIn("no-key", self.log)

    def test_a_public_repository_is_refused_and_nothing_is_sent(self):
        self.gh.visibility = "PUBLIC"
        outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3))
        self.assertIn("not private", outcome.reason)
        self.assertNothingSent()
        self.assertEqual([c for c in self.machine.calls if os.path.basename(c[0]) != "gh"], [], "refused before the lane doctor ran")

    def test_the_gate_is_looked_at_again_right_before_the_writes(self):
        flips = {"n": 0}
        original = self.gh.__call__

        def flipping(argv, *, timeout, env=None, cwd=None):
            if argv[1:3] == ["repo", "view"]:
                flips["n"] += 1
                if flips["n"] == 2:
                    self.gh.visibility = "PUBLIC"      # made public while the doctor ran
            return original(argv, timeout=timeout)
        self.machine.gh = flipping
        outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3))
        self.assertEqual(flips["n"], 2)
        self.assertNothingSent()
        self.assertIn("lane", self.machine.names(), "the doctor did run")

    def test_an_installed_app_is_refused_unless_the_owner_named_it(self):
        self.gh.installs = [{"id": 1, "app_slug": "example-app", "repository_selection": "all"}]
        outcome = self.cycle()
        self.assertEqual(outcome.result, "refused")
        self.assertIn("example-app", outcome.reason)
        self.assertNothingSent()
        allowed = self.cycle(apps_provider=lambda: ["example-app"])
        self.assertEqual(allowed.result, "published")
        self.assertEqual(allowed.notes, ["example-app"])

    def test_a_webhook_is_refused(self):
        self.gh.hooks = [{"id": 1}]
        self.assertEqual(self.cycle().result, "refused")
        self.assertNothingSent()

    def test_a_secret_that_survives_redaction_withholds_everything(self):
        real = safety.secretscan

        class Wrapper:
            def __init__(self, inner):
                self.inner = inner

            def redact_fields(self, obj, known=()):
                return self.inner.redact_fields(obj, known)

            def scan(self, text, known=()):
                return "a split secret" if "claude/stale-branch" in text else self.inner.scan(text, known)
        with mock.patch.object(safety, "secretscan", lambda: Wrapper(real())):
            outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3))
        self.assertIn("still in", outcome.reason)
        self.assertNothingSent()
        self.assertNotIn("claude/stale-branch", json.dumps(self.status()))

    def test_a_missing_safety_helper_publishes_nothing(self):
        with mock.patch.object(safety, "secretscan", side_effect=safety.HelpersMissing("the F3 helper secretscan.py was not found")):
            outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3))
        self.assertNothingSent()

    def test_gh_missing_is_a_refusal(self):
        with mock.patch.object(p, "which", return_value=None):
            outcome = self.cycle(gh=None)
        self.assertEqual(outcome.result, "refused")
        self.assertIn("gh is not installed", outcome.reason)


class Skips(Sandbox):
    def test_high_load_skips_everything_and_keeps_the_last_published_marks_stale(self):
        first = self.cycle()
        self.assertEqual(first.result, "published")
        self.machine.calls.clear()
        self.gh.calls.clear()
        later = self.cycle(now=NOW.replace(hour=20), load1=lambda: 300.0)
        self.assertEqual((later.result, later.exit_code), ("skipped", 0))
        self.assertIn("load1 300", later.reason)
        self.assertEqual(self.machine.calls, [], "nothing was gathered")
        self.assertEqual(self.gh.calls, [], "nothing was sent or even asked")
        status = self.status()
        self.assertTrue(status["stale"])
        self.assertEqual(status["result"], "skipped")
        self.assertEqual(status["last_published_at"], "2026-10-11T17:30:00Z", "the last real publish is remembered")
        self.assertEqual(status["load1"], 300.0)
        self.assertEqual(json.loads(self.gh.written(p.GB_FACTS))["generated_at"], "2026-10-11T17:30:00Z", "never re-dated")

    def test_load_exactly_at_the_limit_runs(self):
        self.assertEqual(self.cycle(load1=lambda: 250.0).result, "published")

    def test_a_second_run_while_one_is_running_exits_quietly(self):
        self.state.mkdir(parents=True, mode=0o700)
        import fcntl
        fd = os.open(str(self.state / "mac-facts-publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome = self.cycle()
        self.assertEqual((outcome.result, outcome.exit_code), ("skipped", 0))
        self.assertIn("another publish is running", outcome.reason)
        self.assertEqual(self.machine.calls, [])
        self.assertFalse((self.state / "mac-facts-publish.json").exists(), "the running copy owns the status file")

    def test_the_lock_is_released_after_a_run(self):
        self.cycle()
        self.assertEqual(self.cycle().result, "published")


class Cap(Sandbox):
    def test_the_cap_ends_the_run_and_kills_the_children(self):
        self.machine.lane_delay = 5
        killed = []
        before = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        real_cap = p.hard_cap
        with mock.patch.object(p, "kill_live", side_effect=lambda: killed.append(1) or 1), \
                mock.patch.object(p, "hard_cap", lambda seconds: real_cap(1)):
            outcome = self.cycle()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((outcome.result, outcome.exit_code), ("over-cap", 4))
        self.assertEqual(killed, [1])
        self.assertEqual(self.gh.puts, [])
        self.assertEqual(self.status()["result"], "over-cap")
        self.assertEqual(signal.getsignal(signal.SIGALRM), before, "the signal handler is put back")
        self.assertEqual(signal.alarm(0), 0, "and no alarm is left pending")

    def test_bounded_clamps_every_child_to_what_is_left_and_refuses_to_start_with_no_room(self):
        seen = []
        clock = {"now": 0.0}
        deadline = p.Deadline(100, clock=lambda: clock["now"])

        def inner(argv, *, timeout, env=None, cwd=None):
            seen.append(timeout)
            return Result(0, "", "")
        run = p.bounded(inner, deadline, 30)
        run(["x"], timeout=1200)
        clock["now"] = 60
        run(["x"], timeout=1200)
        clock["now"] = 68
        self.assertEqual(run(["x"], timeout=1200).reason, "out of time")
        self.assertEqual(seen, [70, 10])

    def test_no_write_phase_starts_with_less_than_a_minute_left(self):
        ticks = iter([0.0, 0.0] + [1190.0] * 200)
        outcome = self.cycle(cap_seconds=1200, clock=lambda: next(ticks))
        self.assertEqual((outcome.result, outcome.exit_code), ("over-cap", 4))
        self.assertEqual(self.gh.puts, [])
        self.assertEqual([c for c in self.gh.calls if c[1:3] == ["repo", "view"]].__len__(), 1, "only the early look at the gate, not the one before the writes")
        self.assertEqual(self.gh.write_calls(), [])

    def test_kill_live_ends_a_running_child(self):
        box = {}

        def work():
            box["result"] = runner.run(["sleep", "30"], timeout=60)
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic() + 10
        while not runner._LIVE_GROUPS and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(runner.kill_live(), 1)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["result"].rc, -signal.SIGKILL)
        self.assertEqual(runner._LIVE_GROUPS, set())


class DryRun(Sandbox):
    def test_a_dry_run_writes_the_files_locally_and_sends_nothing(self):
        out = self.root / "dry"
        outcome = self.cycle(dry_run_dir=out)
        self.assertEqual((outcome.result, outcome.exit_code), ("dry-run", 0))
        self.assertEqual(self.gh.calls, [])
        for rel in (p.FULL_LIST, p.GB_FACTS, p.FULL_FACTS):
            self.assertEqual(stat.S_IMODE((out / rel).stat().st_mode), 0o600, rel)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertTrue(signing.verify(json.loads((out / p.GB_FACTS).read_text()), SIGNER, signed_for=p.GB_FACTS))
        self.assertFalse((self.state / "mac-facts-publish.json").exists(), "a dry run does not overwrite the real status")


class OneFile(Sandbox):
    def local(self, name: str, text: str) -> str:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def publish(self, rel, local, **over):
        args = dict(run=self.gh, key_provider=lambda: SIGNER, repo=REPO, gh="gh", apps_provider=lambda: [], sleep=self.sleeps.append)
        args.update(over)
        return p.publish_file(rel, local, **args)

    def test_a_json_file_is_signed_and_written(self):
        outcome = self.publish("gb/sweep.json", self.local("a.json", json.dumps({"generated_at": iso(1), "n": 1})))
        self.assertEqual((outcome.result, outcome.files), ("published", {"gb/sweep.json": "created"}))
        document = json.loads(self.gh.written("gb/sweep.json"))
        self.assertTrue(signing.verify(document, SIGNER, signed_for="gb/sweep.json"))

    def test_a_markdown_file_is_scanned_and_written_as_it_is(self):
        outcome = self.publish("gb/notes.md", self.local("a.md", "# Notes\n"))
        self.assertEqual(outcome.result, "published")
        self.assertEqual(self.gh.written("gb/notes.md"), b"# Notes\n")

    def test_a_secret_is_never_written(self):
        outcome = self.publish("gb/notes.md", self.local("a.md", "token " + fake_secret()))
        self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3))
        self.assertEqual(self.gh.calls, [])
        redacted = self.publish("gb/x.json", self.local("b.json", json.dumps({"note": fake_secret(), "n": 2})))
        self.assertEqual(redacted.result, "published")
        written = self.gh.written("gb/x.json").decode()
        self.assertNotIn(fake_secret(), written)
        self.assertIn("[withheld: secret-shaped]", written)
        self.assertEqual(json.loads(written)["redacted"], ["$.note"])

    def test_the_key_itself_is_never_written_even_inside_a_field(self):
        outcome = self.publish("gb/x.json", self.local("c.json", json.dumps({"oops": "the key is " + SIGNER})))
        self.assertEqual(outcome.result, "published")
        self.assertNotIn(SIGNER, self.gh.written("gb/x.json").decode())

    def test_a_forbidden_path_is_refused_before_anything_else(self):
        for rel in (".github/workflows/x.yml", "../x.json", "docs/a.md", "/mac-facts.json"):
            outcome = self.publish(rel, self.local("a.md", "x"))
            self.assertEqual((outcome.result, outcome.exit_code), ("refused", 3), rel)
        self.assertEqual(self.gh.calls, [])

    def test_no_key_and_a_public_repository_both_stop_it(self):
        def no_key():
            raise signing.KeyUnavailable("unset")
        self.assertEqual(self.publish("gb/a.md", self.local("a.md", "x"), key_provider=no_key).result, "no-key")
        self.gh.visibility = "PUBLIC"
        self.assertEqual(self.publish("gb/a.md", self.local("a.md", "x")).result, "refused")
        self.assertEqual(self.gh.puts, [])

    def test_bad_input_files(self):
        self.assertEqual(self.publish("gb/a.json", str(self.root / "missing.json")).exit_code, 2)
        self.assertEqual(self.publish("gb/a.json", self.local("a.json", "[1]")).exit_code, 2)
        self.assertEqual(self.publish("gb/a.json", self.local("b.json", "{nope")).exit_code, 2)
        link = self.root / "link.json"
        link.symlink_to(self.local("real.json", "{}"))
        self.assertEqual(self.publish("gb/a.json", str(link)).exit_code, 2, "a symbolic link is not read")

    def test_dry_run_signs_and_sends_nothing(self):
        outcome = self.publish("gb/a.json", self.local("a.json", "{}"), dry_run=True)
        self.assertEqual(outcome.result, "dry-run")
        self.assertEqual(self.gh.calls, [])


class Verify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, document) -> str:
        path = self.dir / "f.json"
        path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        return str(path)

    def test_exit_codes(self):
        signed = signing.sign({"signed_for": "mac-facts.json", "generated_at": "2026-10-11T12:00:00Z"}, SIGNER)
        env = {signing.KEY_ENV: SIGNER}
        code, line = p.verify_file(self.write(signed), env)
        self.assertEqual((code, line.startswith("verified:")), (0, True))
        self.assertEqual(p.verify_file(self.write(signed), env, signed_for="mac-facts.json")[0], 0)
        self.assertEqual(p.verify_file(self.write(signed), env, signed_for="gb/mac-facts.json")[0], 1)
        tampered = dict(signed, generated_at="2026-10-11T13:00:00Z")
        self.assertEqual(p.verify_file(self.write(tampered), env)[0], 1)
        self.assertEqual(p.verify_file(self.write({"a": 1}), env)[0], 1)
        self.assertEqual(p.verify_file(self.write("{nope"), env)[0], 1)
        self.assertEqual(p.verify_file(str(self.dir / "missing"), env)[0], 1)
        self.assertEqual(p.verify_file(self.write(signed), {signing.KEY_ENV: "x" * 40 + "y"})[0], 1, "a different key")
        self.assertEqual(p.verify_file(self.write(signed), {})[0], 3, "no key:  cannot check")
        self.assertEqual(p.verify_file(self.write(signed), {signing.KEY_ENV: "short"})[0], 3)

    def test_two_is_never_a_verdict(self):
        """open-loops reads exit status 2 as 'this fleet-facts has no verify command';  a tampered file must not look like that."""
        signed = signing.sign({"n": 1}, SIGNER)
        codes = {p.verify_file(self.write(d), e)[0] for d in (signed, dict(signed, n=2), "{x") for e in ({signing.KEY_ENV: SIGNER}, {}, {signing.KEY_ENV: "x"})}
        self.assertNotIn(2, codes)

    def test_the_output_never_holds_the_key_or_the_file(self):
        signed = signing.sign({"secret_looking": "value-in-the-file"}, SIGNER)
        for env in ({signing.KEY_ENV: SIGNER}, {signing.KEY_ENV: "y" * 40}):
            code, line = p.verify_file(self.write(signed), env)
            self.assertNotIn(SIGNER, line)
            self.assertNotIn("value-in-the-file", line)


class TheDocuments(unittest.TestCase):
    """build_documents on its own, with a hand-made facts document."""

    def facts(self):
        return {"schema": 1, "tool": "fleet-facts", "version": "1.0.0", "generated_at": "2026-10-11T12:00:00Z", "host": "example-host",
                "no_mac": False, "parts": ["lanes"], "sources": {"lanes": {"status": "ok", "reason": None}},
                "lanes": {"total": 1, "attention": [{"path": LANE_PATH, "branch": "claude/x", "unpushed": 1}]}, "redacted": []}

    def test_redaction_is_per_field_and_recorded_in_sources(self):
        facts = self.facts()
        facts["lanes"]["attention"][0]["reasons"] = ["fine", "oops " + fake_secret()]
        files = p.build_documents(facts, {"status": "absent", "reason": "x", "file": p.FULL_LIST}, SIGNER)
        for rel, data in files.items():
            text = data.decode()
            self.assertNotIn(fake_secret(), text, rel)
            document = json.loads(text)
            self.assertIn("[withheld: secret-shaped]", text)
            self.assertEqual(document["sources"]["publish"]["redacted"], ["$.lanes.attention[0].reasons[1]"])
            self.assertEqual(document["redacted"], ["$.lanes.attention[0].reasons[1]"])
            self.assertEqual(document["lanes"]["attention"][0]["branch"], "claude/x", "the rest of the file is intact")

    def test_the_signing_key_in_a_field_is_withheld(self):
        facts = self.facts()
        facts["lanes"]["attention"][0]["reasons"] = ["the key " + SIGNER]
        files = p.build_documents(facts, {"status": "absent", "reason": "x", "file": p.FULL_LIST}, SIGNER)
        for data in files.values():
            self.assertNotIn(SIGNER, data.decode())

    def test_the_input_facts_are_not_changed(self):
        facts = self.facts()
        before = json.dumps(facts, sort_keys=True)
        p.build_documents(facts, {"status": "ok", "file": p.FULL_LIST, "sha256": "0" * 64, "bytes": 1}, SIGNER)
        self.assertEqual(json.dumps(facts, sort_keys=True), before)

    def test_the_output_is_stable_ascii(self):
        facts = self.facts()
        facts["lanes"]["attention"][0]["keep_reason"] = "caf\u00e9"
        files = p.build_documents(facts, {"status": "absent", "reason": "x", "file": p.FULL_LIST}, SIGNER)
        for data in files.values():
            data.decode("ascii")
            self.assertTrue(data.endswith(b"\n"))
        again = p.build_documents(facts, {"status": "absent", "reason": "x", "file": p.FULL_LIST}, SIGNER)
        self.assertEqual(files, again)


class Launcher(unittest.TestCase):
    def test_the_state_folder_is_under_home(self):
        with mock.patch.dict(os.environ, {"HOME": "/tmp/example-home"}):
            self.assertEqual(p.state_dir(), Path("/tmp/example-home/.local/state/fleet-facts"))


if __name__ == "__main__":
    unittest.main()
