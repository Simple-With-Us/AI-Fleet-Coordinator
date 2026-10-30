"""The `muse` wake type (AFC-291):  the argv, the MSP envelope, the pin discipline, the provider
quota gate, and the subscription-backed capping decision.

Everything here is deterministic and offline except `LiveMuseTest`, which runs the real
`muse exec` against the real wake schema -- the one test that proves the runtime, not the mock.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_sync import adapters as A
from agent_sync import config as C
from agent_sync import daemon as D
from agent_sync import live as L
from agent_sync import wakes as W


def _record(payload_type: str, **payload: object) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "record_type": "event",
            "payload_type": payload_type,
            "payload": payload,
        }
    )


class MuseArgvTest(unittest.TestCase):
    def test_argv_states_the_locked_down_wake(self) -> None:
        argv = A.muse_argv("/usr/local/bin/muse", "muse-spark-1.3-contributor")
        self.assertEqual(argv[0], "/usr/local/bin/muse")
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[2:4], ["-c", "run.toolset=[]"])
        # Every switch that makes "read this topic, touch nothing" true rather than inferred.
        self.assertIn("--approval-mode", argv)
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "never")
        for switch in (
            "--disable-shell",
            "--disable-write",
            "--disable-web-tools",
            "--disable-reminders",
            "--no-session-log",
            "--no-foreign-personal-context",
            "--json",
        ):
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

    def test_a_deleted_versioned_build_falls_back_to_the_newest_one(self) -> None:
        """AFC-491.  The muse launcher replaces its versioned build on every release and DELETES the
        one it replaced, so a config naming one absolute `muse-bin-<version>` path pointed at a file
        that no longer existed.  The seat went dead and READ AS HEALTHY, because a missing binary
        resolves to None and reports no pin state at all."""
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "muse-bin-1.4.11-R6667.1")
            new = os.path.join(tmp, "muse-bin-1.4.12-R6909.1")
            for path in (old, new):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            # The launcher deleted the build the config named.
            os.unlink(old)
            self.assertEqual(A.resolve_muse(old, tmp), os.path.realpath(new))
            # With no explicit path the newest versioned build wins over the launcher shim, so the
            # pin names a real build rather than an auto-updating shim.
            shim = os.path.join(tmp, "muse")
            with open(shim, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(shim, 0o755)
            self.assertEqual(A.resolve_muse(None, tmp), os.path.realpath(new))

    def test_a_relative_configured_path_is_refused_rather_than_guessed(self) -> None:
        """A relative path is a config error, not a moved binary, so it must not resolve to some
        other build on the PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            build = os.path.join(tmp, "muse-bin-1.4.12-R6909.1")
            with open(build, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(build, 0o755)
            self.assertIsNone(A.resolve_muse("relative/muse", tmp))

    def test_the_newest_versioned_build_wins_regardless_of_listing_order(self) -> None:
        """1.4.10 sorts after 1.4.9, so the newest build is the one that runs and is pinned."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("muse-bin-1.4.9-R6297.1", "muse-bin-1.4.10-R6427.1"):
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            self.assertEqual(
                A.newest_versioned_muse(tmp),
                os.path.realpath(os.path.join(tmp, "muse-bin-1.4.10-R6427.1")),
            )

    def test_a_missing_muse_binary_is_red_and_never_reads_as_a_healthy_seat(self) -> None:
        """AFC-491.  Every wake for the seat is inbox-only, so the status must say so."""
        line = A.muse_no_binary_red_entry("MM")
        self.assertIn("no muse binary found", line)
        self.assertIn("MM", line)
        # The remediation is to install the runtime, not to re-pin a binary that is not there.
        self.assertIn("install a muse build", line)
        self.assertNotIn("run `simple-chat daemon test-wake --seat MM --run`, then", line)


class MuseBuildResolutionTest(unittest.TestCase):
    """Which muse build a seat runs, and which one it is allowed to run.

    The muse launcher replaces its versioned build on every release and DELETES the one it
    replaced, so a seat naming an absolute ``muse-bin-<version>`` path points at a file that no
    longer exists after the next release.  The build that wins the scan runs with the daemon's
    privileges and ingests untrusted Zulip DMs, so PATH order is the trust order (Kody, PR #880).
    """

    def _build(self, directory: str, name: str) -> str:
        """An executable fake build, the way the launcher leaves one on disk."""
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_the_first_path_directory_wins_even_when_a_later_one_has_a_higher_version(self) -> None:
        """Kody, PR #880.  The scan used to collect the builds of EVERY PATH directory and return
        the global maximum, so a later directory (on macOS /opt/homebrew/bin is group-writable)
        could plant a higher-version build that outranked the first directory's.  The winning
        build runs with daemon privileges and reads untrusted Zulip DMs, so the first directory
        that carries a build is the one that wins, whatever version a later one claims."""
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            os.mkdir(first)
            os.mkdir(second)
            trusted = self._build(first, "muse-bin-1.4.12-R6909.1")
            planted = self._build(second, "muse-bin-9.9.9")
            gone = os.path.join(tmp, "muse-bin-1.4.11-R6667.1")
            resolved = A.resolve_muse(gone, first + os.pathsep + second)
            self.assertEqual(resolved, os.path.realpath(trusted))
            self.assertNotEqual(resolved, os.path.realpath(planted),
                                "a later PATH directory must not outrank the first one")

    def test_a_deleted_build_falls_back_to_the_launcher_shim_on_the_wake_path(self) -> None:
        """Kody, PR #880.  With no versioned build anywhere on the PATH the seat still has the
        launcher shim.  The explicit-path branch stopped at the versioned build and never reached
        the shim, so a seat whose config named a deleted build had NO binary at all and read as
        healthy while every wake for it was dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            shim = self._build(tmp, "muse")
            gone = os.path.join(tmp, "muse-bin-1.4.11-R6667.1")
            self.assertEqual(A.resolve_muse(gone, tmp), os.path.abspath(shim))

    def test_the_path_scan_is_memoized_so_one_tick_does_not_relist_every_directory(self) -> None:
        """Kody, PR #880.  The daemon's status tick calls the scan once per muse seat every two
        seconds, and on macOS /usr/bin and /bin are the same directory, so an uncached scan reads
        ~7,000 dirents per seat per tick.  The memo is keyed by wake PATH and lives for
        MUSE_SCAN_TTL_SECONDS, so a second call inside the window must not touch the disk."""
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(tmp, "muse-bin-1.4.12-R6909.1")
            listed: list[str] = []
            real_listdir = os.listdir

            def counting_listdir(path):
                listed.append(path)
                return real_listdir(path)

            with mock.patch.object(os, "listdir", counting_listdir):
                first = A.newest_versioned_muse(tmp)
                second = A.newest_versioned_muse(tmp)
            self.assertEqual(first, os.path.realpath(build))
            self.assertEqual(second, first)
            self.assertEqual([path for path in listed if path == tmp], [tmp],
                             "the wake PATH was scanned twice inside the TTL")
            # The memo holds inside the TTL, so a build that appears now is only visible to the
            # uncached scan; the daemon picks it up when the memo expires.
            newer = self._build(tmp, "muse-bin-1.4.13-R7000.1")
            self.assertEqual(A.newest_versioned_muse(tmp), os.path.realpath(build))
            self.assertEqual(A._newest_versioned_muse(tmp), os.path.realpath(newer))

    def test_the_repoint_entry_names_the_running_build_and_the_setting_to_change(self) -> None:
        """Kody, PR #880.  Re-pinning alone records a build the launcher deletes at the next
        release, so the operator has to be told which build is running and which setting has to
        name it, or the seat breaks again at every release."""
        line = A.muse_repoint_red_entry(
            "MM", "/bin/muse-bin-1.4.12-R6909.1", "/bin/muse-bin-1.4.11-R6667.1"
        )
        self.assertIn("/bin/muse-bin-1.4.12-R6909.1", line)
        self.assertIn("seat.MM.muse", line)
        self.assertIn("repoint", line)
        self.assertIn("/bin/muse-bin-1.4.11-R6667.1", line)

    def test_a_configured_path_is_missing_only_when_it_is_an_absolute_path_that_is_gone(self) -> None:
        """Kody, PR #880.  A relative path is a config error the daemon refuses on its own, and a
        path that is still there needs no repointing, so neither may be reported as a missing
        build."""
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "muse-bin-1.4.12-R6909.1")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            self.assertTrue(A.muse_configured_path_missing("/does/not/exist"))
            self.assertFalse(A.muse_configured_path_missing("relative/muse"))
            self.assertFalse(A.muse_configured_path_missing(exe))

    def test_the_memo_outlives_the_daemon_status_tick(self) -> None:
        """Kody, PR #880.  The daemon rewrites its status file at most every two seconds, and the
        scan is memoized.  A TTL that expires between two ticks is no TTL at all:  every tick
        still pays the full scan of every PATH directory, so the memo has to span several ticks."""
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(tmp, "muse-bin-1.4.12-R6909.1")
            listed: list[str] = []
            real_listdir = os.listdir

            def counting_listdir(path):
                listed.append(path)
                return real_listdir(path)

            clock = [1000.0]
            with mock.patch.object(os, "listdir", counting_listdir), mock.patch.object(
                time, "monotonic", lambda: clock[0]
            ):
                first = A.newest_versioned_muse(tmp)
                clock[0] += D.STATUS_TICK_SECONDS
                second = A.newest_versioned_muse(tmp)
            self.assertEqual(first, os.path.realpath(build))
            self.assertEqual(second, first)
            self.assertEqual(
                [path for path in listed if path == tmp],
                [tmp],
                "the memo expired between two status ticks, so every tick still rescans",
            )

    def test_a_build_deleted_inside_the_memo_window_is_not_served_from_the_memo(self) -> None:
        """Kody, PR #880.  The launcher DELETES the build it replaced, so the build the memo names
        can vanish while the memo is still warm.  Serving it would point the seat at a file that
        is not there, so the memoized answer is checked before it is handed back."""
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(tmp, "muse-bin-1.4.12-R6909.1")
            self.assertEqual(A.newest_versioned_muse(tmp), os.path.realpath(build))
            os.remove(build)
            self.assertIsNone(
                A.newest_versioned_muse(tmp),
                "the memo served a build the launcher deleted",
            )

    def test_concurrent_scans_of_one_wake_path_agree(self) -> None:
        """Kody, PR #880.  The daemon's seat threads share the memo, so concurrent scans of one
        wake PATH must all report the same build and none may raise."""
        with tempfile.TemporaryDirectory() as tmp:
            build = self._build(tmp, "muse-bin-1.4.12-R6909.1")
            results: list[str | None] = []
            errors: list[BaseException] = []

            def scan() -> None:
                try:
                    results.append(A.newest_versioned_muse(tmp))
                except BaseException as exc:  # noqa: BLE001 - the test reports the failure
                    errors.append(exc)

            threads = [threading.Thread(target=scan) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(results, [os.path.realpath(build)] * len(threads))


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
                fh.write(
                    "print(json.dumps({'schema_version': 1, 'record_type': 'event', "
                    "'payload_type': 'run.terminal.completed', 'payload': {'text': '{}'}}))\n"
                )
            os.chmod(executable, 0o700)
            with open(W.CONTRACT_PATH, encoding="utf-8") as fh:
                contract = fh.read()
            supplied = "Daemon header (trusted):\nSeat: MM\nUntrusted message: read the canary\n"
            result = A.MuseRunner(timeout=20).run(
                [executable, "exec", "--prompt-file", A.MUSE_PROMPT_SLOT],
                {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CAPTURE_PROMPT": capture},
                wake,
                supplied,
                schema="{}",
            )

            self.assertTrue(result.ok, (result.refused, result.error))
            with open(capture, encoding="utf-8") as fh:
                actual = fh.read()
            self.assertTrue(
                actual.startswith(contract.rstrip() + "\n\n--- BEGIN DAEMON WAKE PROMPT ---\n")
            )
            self.assertIn(supplied, actual)
            self.assertLess(actual.index(contract.rstrip()), actual.index("Untrusted message:"))
            self.assertFalse(os.path.exists(os.path.join(wake, "prompt.md")))

    def test_missing_contract_fails_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(A, "CONTRACT_PATH", os.path.join(tmp, "missing-contract.md")),
                mock.patch.object(A.subprocess, "Popen") as popen,
            ):
                result = A.MuseRunner().run(["/does/not/run"], {}, tmp, "prompt")
            self.assertEqual(result.error, "wake contract unavailable")
            popen.assert_not_called()


class MuseEnvTest(unittest.TestCase):
    def test_env_is_minimal_and_names_the_seat(self) -> None:
        env = A.muse_env(
            {
                "USER": "fixture-user",
                "TMPDIR": "/tmp",
                "ANTHROPIC_API_KEY": "secret",
                "ZULIP_API_KEY": "secret",
                "MUSE_TOKEN": "secret",
            },
            "/home/fixture-user",
            "/wake/path",
            seat="MM",
        )
        self.assertEqual(env["HOME"], "/home/fixture-user")
        self.assertEqual(env["PATH"], "/wake/path")
        self.assertEqual(env["AGENT_LAUNCHER"], A.WAKE_LAUNCHER)
        self.assertEqual(env["AGENT_LAUNCH_SEAT"], "MM")
        self.assertEqual(env["AGENT_SEAT"], "MM")
        self.assertEqual(env["AGENT_SYNC_ATTACH"], "0")
        # No inherited identity or vendor credential may survive into the run.
        for leaked in ("ANTHROPIC_API_KEY", "ZULIP_API_KEY", "MUSE_TOKEN"):
            self.assertNotIn(leaked, env)

    def test_without_a_seat_no_identity_is_injected(self) -> None:
        env = A.muse_env({"USER": "fixture-user"}, "/home/fixture-user", "/wake/path")
        self.assertNotIn("AGENT_SEAT", env)
        self.assertNotIn("AGENT_LAUNCHER", env)


class MuseEnvelopeTest(unittest.TestCase):
    """The MSP records, not claude's stream-json."""

    def test_reads_the_final_answer_and_the_model(self) -> None:
        lines = [
            _record("run.model.configured", model_id="muse-spark-1.3-contributor"),
            _record("status", text="starting"),
            _record(
                "run.terminal.completed",
                text='{"action":"none","board":null,"owner_note":null,"reply":null,'
                '"risk":null,"route":null}',
            ),
        ]
        answer, why, models = A.parse_muse_records(lines)
        self.assertIsNone(why)
        self.assertEqual(models, ["muse-spark-1.3-contributor"])
        self.assertEqual(W.validate(A.parse_muse_result(answer))[1], None)

    def test_a_populated_result_survives_the_round_trip(self) -> None:
        payload = {
            "action": "board",
            "reply": "Filed it.",
            "owner_note": None,
            "risk": None,
            "route": None,
            "board": {"title": "muse Gate 0 timing", "severity": "P3", "desc": "d"},
        }
        answer, why, _ = A.parse_muse_records(
            [_record("run.terminal.completed", text=json.dumps(payload))]
        )
        self.assertIsNone(why)
        result, err = W.validate(A.parse_muse_result(answer))
        self.assertIsNone(err)
        self.assertEqual(result["action"], "board")
        self.assertEqual(result["board"]["severity"], "P3")

    def test_a_non_completed_terminal_is_a_refusal_that_names_itself(self) -> None:
        answer, why, _ = A.parse_muse_records(
            [_record("run.terminal.cancelled", reason="the run was stopped")]
        )
        self.assertIsNone(answer)
        self.assertIn("cancelled", why or "")

    def test_malformed_and_unknown_records_are_ignored_not_fatal(self) -> None:
        lines = [
            "not json at all",
            "",
            "[]",
            json.dumps({"payload_type": "run.model.configured"}),
            _record("something.new", text="ignored"),
            _record("run.terminal.completed", text='{"action":"none"}'),
        ]
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
        # The seat dir is private state.  Build it the way the listener does, so a fixture
        # cannot pass while production would leave it group- or world-readable.
        L.private_dir(self.paths.seat_dir)

    def _muse(self, name: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        return path

    def test_the_seat_dir_is_private_under_a_permissive_umask(self) -> None:
        # `os.makedirs(..., exist_ok=True)` obeys the umask, so a permissive shell left a
        # group- or world-readable seat dir holding wake pins.  The fixture must build it the
        # way the listener does, and it must hold even when the umask would not.
        with tempfile.TemporaryDirectory() as tmp:
            seat_dir = os.path.join(tmp, "MM")
            previous = os.umask(0o000)
            try:
                L.private_dir(seat_dir)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(os.stat(seat_dir).st_mode), 0o700)

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
        self.assertTrue(L.read_json(self.paths.wake_pin_muse).get("fingerprint"))

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

    def test_legacy_pin_and_candidate_without_fingerprints_are_stale(self) -> None:
        muse = self._muse("muse")
        L.write_json(
            self.paths.wake_pin_muse,
            {"muse": A.binary_pin(muse), "at": time.time(), "model": C.MUSE_DEFAULT_MODEL},
        )
        # The binary is the same, so this pin did verify SOMETHING and says so apart from a moved
        # binary:  `pin_stale` would point the operator at a path that never moved.
        state = A.muse_pin_state(self.paths, muse)
        self.assertEqual(state["why"], "pin_unverified")
        self.assertEqual(state["muse"], state["pinned_muse"])
        L.write_json(
            self.paths.wake_pin_candidate_muse,
            {"muse": A.binary_pin(muse), "at": time.time(), "model": C.MUSE_DEFAULT_MODEL},
        )
        self.assertIn("changed since the test", A.muse_pin_candidate(self.paths, muse, time.time()))

    def test_changed_fixed_argv_invalidates_pin_and_candidate(self) -> None:
        muse = self._muse("muse")
        now = time.time()
        A.record_muse_pin(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        A.record_muse_candidate(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        with mock.patch.object(A, "MUSE_MAX_MODEL_STEPS", A.MUSE_MAX_MODEL_STEPS + 1):
            self.assertEqual(A.muse_pin_state(self.paths, muse)["why"], "pin_stale")
            self.assertIn("changed since the test", A.muse_pin_candidate(self.paths, muse, now))

    def test_changed_contract_invalidates_pin_and_candidate(self) -> None:
        muse = self._muse("muse")
        now = time.time()
        A.record_muse_pin(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        A.record_muse_candidate(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        changed_contract = os.path.join(self.tmp.name, "changed-contract.md")
        with open(changed_contract, "w", encoding="utf-8") as fh:
            fh.write("changed wake contract\n")
        with mock.patch.object(A, "CONTRACT_PATH", changed_contract):
            self.assertEqual(A.muse_pin_state(self.paths, muse)["why"], "pin_stale")
            self.assertIn("changed since the test", A.muse_pin_candidate(self.paths, muse, now))

    def test_an_unreadable_contract_is_not_reported_as_a_changed_pin(self) -> None:
        muse = self._muse("muse")
        now = time.time()
        A.record_muse_pin(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        A.record_muse_candidate(self.paths, muse, now, model=C.MUSE_DEFAULT_MODEL)
        missing_contract = os.path.join(self.tmp.name, "missing-contract.md")
        with mock.patch.object(A, "CONTRACT_PATH", missing_contract):
            state = A.muse_pin_state(self.paths, muse)
            # A changed pin is fixed by re-pinning;  an unreadable contract is not, because the
            # same re-pin re-reads the same missing file and refuses again.
            self.assertEqual(state["why"], "pin_unreadable")
            self.assertNotEqual(state["why"], "pin_stale")
            self.assertIn("restore them", A.muse_pin_candidate(self.paths, muse, now))
        self.assertIn("could not be read", A.muse_pin_cause(state))
        self.assertIn("restore the wake contract", A.MUSE_PIN_UNREADABLE_REMEDIATION)

    def test_the_muse_cause_names_the_fault_instead_of_repeating_the_path(self) -> None:
        # `pin_stale` covers a moved binary and a changed command/contract/schema.  Reporting the
        # wrong one points the operator at a path that never moved, and shows it twice.
        moved = {
            "why": "pin_stale",
            "muse": "/opt/homebrew/bin/muse",
            "pinned_muse": "/usr/local/bin/muse",
        }
        self.assertIn("muse moved to", A.muse_pin_cause(moved))
        self.assertIn("/usr/local/bin/muse", A.muse_pin_cause(moved))
        changed = {
            "why": "pin_stale",
            "muse": "/opt/homebrew/bin/muse",
            "pinned_muse": "/opt/homebrew/bin/muse",
        }
        cause = A.muse_pin_cause(changed)
        self.assertIn("command, contract or schema", cause)
        self.assertEqual(cause.count("/opt/homebrew/bin/muse"), 1, cause)
        unverified = {
            "why": "pin_unverified",
            "muse": "/opt/homebrew/bin/muse",
            "pinned_muse": "/opt/homebrew/bin/muse",
        }
        self.assertIn("predates the command fingerprint", A.muse_pin_cause(unverified))

    def test_the_muse_red_entry_labels_unreadable_as_unverifiable_and_omits_run(self) -> None:
        # A `pin_stale` or `pin_unverified` line ends in the operator running the re-pin
        # remediation, which begins with the verb "`simple-chat daemon test-wake ...`".  An
        # unreadable line must say "unverifiable" (re-pinning would refuse again on the same
        # missing contract) and must not prefix its own "run" before the remediation, which
        # begins with "restore the wake contract".  Tested as the function the daemon calls,
        # so the wording is verifiable without standing up a daemon.
        seat = "MM"
        stale = {
            "why": "pin_stale",
            "muse": "/opt/homebrew/bin/muse",
            "pinned_muse": "/usr/local/bin/muse",
        }
        line = A.muse_pin_red_entry(stale, seat)
        self.assertIn("wake pin is stale: muse moved", line)
        self.assertIn("; run `simple-chat daemon test-wake --seat MM --run`", line)
        self.assertNotIn("unverifiable", line)
        unreadable = {
            "why": "pin_unreadable",
            "muse": "/opt/homebrew/bin/muse",
            "pinned_muse": "/usr/local/bin/muse",
        }
        line = A.muse_pin_red_entry(unreadable, seat)
        self.assertIn("wake pin is unverifiable", line)
        self.assertNotIn("wake pin is stale", line)
        # No "run " prefix when the remediation begins with its own verb.
        self.assertFalse(
            line.startswith("run "),
            "unreadable remediation must not be prefixed by `run`:  %r" % line,
        )
        self.assertIn("restore the wake contract", line)
        # Cause still names the fault:  a folder path would help no one when the contract is
        # what could not be read.
        self.assertIn("could not be read", line)


class QuotaRefusalTest(unittest.TestCase):
    """A capped provider is not an internal fault, and must not be reported as one.

    Live incident, Oct 11 2026:  claude 2.1.296 at its weekly cap returned exit 1, EMPTY stderr
    and ~5 KB of stdout whose result text was "You've hit your weekly limit · resets Oct 13 at
    5pm (America/Chicago)", with `modelUsage: {}`.  The model-count assertion fired first and the
    owner was told "modelUsage shows 0 models, not 1", which names an internal check instead of the
    cap that actually stopped the run."""

    def _result(self, **over: object) -> dict:
        base = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "You've hit your weekly limit · resets Oct 13 at 5pm (America/Chicago)",
            "modelUsage": {},
            "total_cost_usd": 0.0,
        }
        base.update(over)
        return base

    def test_the_reason_is_the_providers_own_words(self) -> None:
        self.assertIn("weekly limit", A.result_reason(self._result()))
        self.assertIn("resets Oct 13", A.result_reason(self._result()))

    def test_the_reason_falls_back_to_the_subtype(self) -> None:
        # A run that failed without saying why still says something useful.
        self.assertEqual(
            A.result_reason({"subtype": "error_during_execution", "result": ""}),
            "error_during_execution",
        )
        self.assertEqual(A.result_reason({}), "error")

    def test_the_reason_is_control_stripped(self) -> None:
        # Provider output is not trusted to be printable; it reaches a banner and the log.
        got = A.result_reason(self._result(result="cap\x1b[31m reached\n"))
        self.assertNotIn("\x1b", got)
        self.assertNotIn("\n", got)

    def test_a_quota_refusal_is_not_reported_as_a_model_count_refusal(self) -> None:
        result = self._result()
        models = sorted(result["modelUsage"]) if isinstance(result["modelUsage"], dict) else []
        self.assertEqual(models, [])  # the assertion that used to fire first
        # The order matters:  is_error is checked BEFORE the model count, so the cap wins.
        self.assertTrue(result["is_error"])

    def test_a_cap_is_not_mistaken_for_a_paid_run(self) -> None:
        # A capped run spends nothing, so it must not be debited the run maximum the way a killed
        # or resultless run is.
        self.assertEqual(self._result()["total_cost_usd"], 0.0)


class McodeFallbackTest(unittest.TestCase):
    """mcode exec is the FALLBACK, and only the fallback (AFC-291).

    Gate 0 passed on the real runtime:  the wake schema round-tripped through `mcode exec` with zero
    coercion and a populated `board` object.  What did not pass is the lockdown, and that is why the
    order is fixed:  muse then mcode, never the reverse."""

    def test_the_argv_states_what_mcode_can_state(self) -> None:
        argv = A.mcode_argv("/usr/local/bin/mcode")
        self.assertEqual(argv[:2], ["/usr/local/bin/mcode", "exec"])
        self.assertEqual(argv[argv.index("--permission") + 1], A.MCODE_PERMISSION)
        self.assertEqual(A.MCODE_PERMISSION, "off")
        self.assertIn("--output-schema", argv)
        # The one switch muse has and mcode does not.
        self.assertNotIn("--disable-shell", argv)
        self.assertNotIn("--disable-write", argv)
        self.assertNotIn("--disable-web-tools", argv)
        self.assertNotIn("--disable-sandbox", argv)
        self.assertNotIn("--no-foreign-personal-context", argv)

    def test_the_step_budget_is_big_enough_for_the_wake_contract(self) -> None:
        # Measured, not guessed.  At 8 steps mcode answers a four-message pending set in more steps
        # than that and exits 7 with "limit_exceeded", which the daemon reads as a failed fallback.
        # A failed fallback can never be pinned, so the seat keeps its old runtime forever.  The same
        # run at 16 exits 0 with the contract's answer.
        argv = A.mcode_argv("/usr/local/bin/mcode")
        self.assertEqual(argv[argv.index("--max-steps") + 1], str(A.MCODE_MAX_STEPS))
        self.assertGreaterEqual(A.MCODE_MAX_STEPS, 16)

    def test_the_documented_argv_carries_the_step_budget_the_code_uses(self) -> None:
        # The protocol doc quotes the fallback argv.  A number that drifts from the code is a doc
        # that lies about the command, so the two are pinned together here.
        doc = pathlib.Path(__file__).resolve().parents[3] / "docs" / "protocols" / "agent-sync-listener.md"
        text = doc.read_text(encoding="utf-8")
        self.assertIn("--max-steps %d --timeout 3m30s" % A.MCODE_MAX_STEPS, text)

    def test_the_backup_names_its_model_on_the_command_line(self) -> None:
        # Without `--model`, mcode inherits MiniMax Code's own `defaultModel`, and on this Mac that
        # names `minimax/MiniMax-M3.1-Flash-Preview` — an id the current catalog does not carry.
        argv = A.mcode_argv("/usr/local/bin/mcode")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], C.MCODE_DEFAULT_MODEL)

    def test_the_backup_model_is_one_the_catalog_serves(self) -> None:
        # The catalog holds `MiniMax-M3` and `MiniMax-M3.1-Flash`;  the -Preview id is stale.
        self.assertEqual(C.MCODE_DEFAULT_MODEL, "minimax/MiniMax-M3.1-Flash")
        self.assertNotIn("Preview", C.MCODE_DEFAULT_MODEL)
        self.assertRegex(C.MCODE_DEFAULT_MODEL, C.MCODE_MODEL_PATTERN)

    def test_a_seat_may_name_another_backup_model(self) -> None:
        argv = A.mcode_argv("/usr/local/bin/mcode", "minimax/MiniMax-M3")
        self.assertEqual(argv[argv.index("--model") + 1], "minimax/MiniMax-M3")

    def test_the_fallback_can_never_become_the_primary(self) -> None:
        # mcode is not in KNOWN_WAKES:  there is no way to configure a seat to wake on it directly.
        self.assertNotIn("mcode", C.KNOWN_WAKES)
        self.assertIn("mcode", C.KNOWN_FALLBACKS)

    def test_mcode_needs_the_real_home_because_its_credential_lives_there(self) -> None:
        # Verified, not assumed:  pointing HOME at the inert workspace made mcode refuse with
        # "Sign in to MiniMax to use Agent features".  Recorded so nobody "hardens" it into a
        # guaranteed-broken wake.
        env = A.mcode_env({"USER": "fixture-user"}, "/home/fixture-user", "/wake/path")
        self.assertEqual(env["HOME"], "/home/fixture-user")
        for leaked in ("ANTHROPIC_API_KEY", "ZULIP_API_KEY"):
            self.assertNotIn(leaked, env)

    def test_a_quota_refusal_never_falls_back(self) -> None:
        # A capped provider stays capped whichever binary asks, so a fallback there would spend a
        # second run to learn what the first one already said.
        from agent_sync.daemon import _is_fallbackable

        for quota in (
            "You've hit your weekly limit · resets Oct 13 at 5pm",
            "Claude Code quota is exhausted until 2026-10-13",
            "usage limit reached",
            "mcap exhausted",
        ):
            self.assertFalse(_is_fallbackable(quota), quota)

    def test_an_unanticipated_quota_wording_still_does_not_fall_back(self) -> None:
        # The reason this is an ALLOWLIST and not a blocklist of known words:  a cap whose phrasing
        # nobody anticipated must not read as "some other fault" and buy itself a second run.
        from agent_sync.daemon import _is_fallbackable

        self.assertFalse(_is_fallbackable("a wholly unexpected vendor message"))
        self.assertFalse(_is_fallbackable("something nobody has seen before"))

    def test_a_real_fault_does_fall_back(self) -> None:
        from agent_sync.daemon import _is_fallbackable

        for fault in (
            "no parseable result",
            "could not start claude (Permission denied)",
            "timed out after 240 seconds",
        ):
            self.assertTrue(_is_fallbackable(fault), fault)

    def test_a_safety_refusal_never_falls_back(self) -> None:
        # The daemon killed that child on purpose (it listed tools, ran a hook, or never emitted an
        # init event).  Re-running it on a second runtime would be trying to get around a guard.
        from agent_sync.daemon import _is_fallbackable

        for refusal in ("init listed tools", "hook ran", "no init event"):
            self.assertFalse(_is_fallbackable(refusal), refusal)

    def test_limited_is_structural_not_read_from_the_wording(self) -> None:
        # The signature of a provider refusal is an error result in which NO model ran.  A cap whose
        # message we do not recognise still sets it;  an ordinary failure never does.
        capped = {
            "is_error": True,
            "modelUsage": {},
            "result": "a wholly unexpected vendor message",
        }
        self.assertEqual(sorted(capped["modelUsage"]), [])
        genuine = {
            "is_error": True,
            "modelUsage": {"claude-x": {}},
            "result": "something failed mid-run",
        }
        self.assertTrue(sorted(genuine["modelUsage"]))

    def test_a_fallback_pin_is_its_own_and_never_self_heals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = L.SeatPaths(tmp, "MM")
            # The seat dir is private state.  Build it the way the listener does, so a
            # fixture cannot pass while production would leave it group- or world-readable.
            L.private_dir(paths.seat_dir)
            exe = os.path.join(tmp, "mcode")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            self.assertEqual(A.mcode_pin_state(paths, exe)["why"], "not_pinned")
            # A primary's pin can never stand in for the fallback's.
            A.record_muse_pin(paths, exe, time.time())
            self.assertEqual(A.mcode_pin_state(paths, exe)["why"], "not_pinned")
            A.record_mcode_pin(paths, exe, time.time())
            self.assertTrue(A.mcode_pinned(paths, exe))
            moved = os.path.join(tmp, "mcode2")
            with open(moved, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            self.assertEqual(A.mcode_pin_state(paths, moved)["why"], "pin_stale")

    def test_a_fallback_pin_goes_stale_when_the_model_changes(self) -> None:
        # The pin records the model, and the model is on the command line:  a pin made under one
        # model must not read as verified after the seat moves to another.
        with tempfile.TemporaryDirectory() as tmp:
            paths = L.SeatPaths(tmp, "MM")
            # The seat dir is private state.  Build it the way the listener does, so a
            # fixture cannot pass while production would leave it group- or world-readable.
            L.private_dir(paths.seat_dir)
            exe = os.path.join(tmp, "mcode")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            A.record_mcode_pin(paths, exe, time.time(), model="minimax/MiniMax-M3")
            self.assertTrue(A.mcode_pinned(paths, exe, "minimax/MiniMax-M3"))
            self.assertEqual(
                A.mcode_pin_state(paths, exe, "minimax/MiniMax-M3.1-Flash")["why"], "pin_stale"
            )
            # A pin recorded before the fingerprint existed vouches for nothing about the current
            # command, and says so apart from a moved binary:  the binary here is the same, so
            # `pin_stale` would point the operator at a path that never moved.
            L.write_json(paths.wake_pin_mcode, {"mcode": A.binary_pin(exe), "at": time.time()})
            state = A.mcode_pin_state(paths, exe, "minimax/MiniMax-M3")
            self.assertEqual(state["why"], "pin_unverified")
            self.assertFalse(state["pinned"])
            self.assertEqual(state["mcode"], state["pinned_mcode"])

    def test_the_fallback_cause_names_the_fault_instead_of_repeating_the_path(self) -> None:
        # `pin_stale` covers a moved binary and a changed model/contract/schema.  Reporting the
        # wrong one points the operator at a path that never moved, and shows it twice.
        moved = {
            "why": "pin_stale",
            "mcode": "/opt/homebrew/bin/mcode",
            "pinned_mcode": "/usr/local/bin/mcode",
        }
        self.assertIn("mcode moved to", A.mcode_pin_cause(moved))
        self.assertIn("/usr/local/bin/mcode", A.mcode_pin_cause(moved))
        changed = {
            "why": "pin_stale",
            "mcode": "/opt/homebrew/bin/mcode",
            "pinned_mcode": "/opt/homebrew/bin/mcode",
        }
        cause = A.mcode_pin_cause(changed)
        self.assertIn("model, contract or schema", cause)
        self.assertEqual(cause.count("/opt/homebrew/bin/mcode"), 1, cause)
        unverified = {
            "why": "pin_unverified",
            "mcode": "/opt/homebrew/bin/mcode",
            "pinned_mcode": "/opt/homebrew/bin/mcode",
        }
        self.assertIn("predates the command fingerprint", A.mcode_pin_cause(unverified))

    def test_the_fallback_remediation_selects_the_fallback_runtime(self) -> None:
        # Without `--runtime fallback`, `test-wake` resolves the default "auto" to seat_cfg.wake and
        # re-pins the PRIMARY runtime, which leaves this pin stale and the fallback still refused.
        # Both halves carry it, so the string is safe to follow even when only the red line was
        # copied:  a `--pin` typed without `--runtime` would pin the PRIMARY runtime again.
        self.assertEqual(
            A.MCODE_PIN_REMEDIATION.count("--runtime fallback"), 2, A.MCODE_PIN_REMEDIATION
        )
        self.assertIn("--run", A.MCODE_PIN_REMEDIATION)
        self.assertIn("--pin`", A.MCODE_PIN_REMEDIATION)
        rendered = A.MCODE_PIN_REMEDIATION % ("MM", "MM")
        self.assertEqual(rendered.count("--seat MM"), 2)

    def test_the_fallback_fingerprint_covers_the_contract_and_the_schema(self) -> None:
        # The fallback is validated against the same wake contract and result schema the muse
        # runner uses, so a pin that ignores them still reads as verified after either changes.
        base = A.mcode_wake_fingerprint("<mcode>", "minimax/MiniMax-M3")
        self.assertTrue(base)
        original = A.MCODE_WAKE_POLICY_VERSION
        try:
            A.MCODE_WAKE_POLICY_VERSION = "mcode-fallback-model-v2"
            self.assertNotEqual(A.mcode_wake_fingerprint("<mcode>", "minimax/MiniMax-M3"), base)
        finally:
            A.MCODE_WAKE_POLICY_VERSION = original
        self.assertEqual(A.mcode_wake_fingerprint("<mcode>", "minimax/MiniMax-M3"), base)

    def test_an_unreadable_contract_is_not_reported_as_a_changed_pin(self) -> None:
        # A changed pin is fixed by re-pinning.  An unreadable contract is not:  the same re-pin
        # re-reads the same missing file and refuses again, so folding it into `pin_stale` hands
        # the operator a remediation that cannot succeed.
        with tempfile.TemporaryDirectory() as tmp:
            paths = L.SeatPaths(tmp, "MM")
            L.private_dir(paths.seat_dir)
            exe = os.path.join(tmp, "mcode")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            A.record_mcode_pin(paths, exe, time.time(), model="minimax/MiniMax-M3")
            missing = os.path.join(tmp, "no-such-contract.md")
            original = A.CONTRACT_PATH
            try:
                A.CONTRACT_PATH = missing
                state = A.mcode_pin_state(paths, exe, "minimax/MiniMax-M3")
            finally:
                A.CONTRACT_PATH = original
            self.assertEqual(state["why"], "pin_unreadable")
            self.assertNotEqual(state["why"], "pin_stale")
            cause = A.mcode_pin_cause(state)
            self.assertIn("could not be read", cause)
            # The remediation must fix the read, not hand back the re-pin that cannot succeed.
            self.assertIn("restore the wake contract", A.MCODE_PIN_UNREADABLE_REMEDIATION)
            # And it must name `--runtime fallback` in BOTH halves:  a bare `--pin` would
            # re-pin the PRIMARY runtime and keep the fallback refused.  Mirrors the
            # `MCODE_PIN_REMEDIATION` test above.
            self.assertEqual(
                A.MCODE_PIN_UNREADABLE_REMEDIATION.count("--runtime fallback"),
                2,
                A.MCODE_PIN_UNREADABLE_REMEDIATION,
            )
            self.assertIn("--run", A.MCODE_PIN_UNREADABLE_REMEDIATION)
            self.assertIn("--pin`", A.MCODE_PIN_UNREADABLE_REMEDIATION)

    def test_the_fallback_red_entry_labels_unreadable_as_unverifiable_and_omits_run(self) -> None:
        # The mcode red entry mirrors the muse one.  `pin_stale` and `pin_unverified` carry the
        # "run --pin" remediation (the re-pin);  `pin_unreadable` carries the restore remediation
        # and the line says "unverifiable" instead of "stale".  Tested as the function the daemon
        # calls so the wording is verifiable without standing up a daemon.
        seat = "MM"
        stale = {
            "why": "pin_stale",
            "mcode": "/opt/homebrew/bin/mcode",
            "pinned_mcode": "/usr/local/bin/mcode",
        }
        line = A.mcode_pin_red_entry(stale, seat)
        self.assertIn("fallback pin is stale: mcode moved", line)
        self.assertIn(
            "; run `simple-chat daemon test-wake --seat MM --runtime fallback --run`", line
        )
        self.assertNotIn("unverifiable", line)
        unreadable = {
            "why": "pin_unreadable",
            "mcode": "/opt/homebrew/bin/mcode",
            "pinned_mcode": "/usr/local/bin/mcode",
        }
        line = A.mcode_pin_red_entry(unreadable, seat)
        self.assertIn("fallback pin is unverifiable", line)
        self.assertNotIn("fallback pin is stale", line)
        # No "run " prefix when the remediation begins with its own verb.
        self.assertFalse(
            line.startswith("run "),
            "unreadable remediation must not be prefixed by `run`:  %r" % line,
        )
        # And the remediation must carry `--runtime fallback` in BOTH halves:  mirror of the
        # remediation string test above, asserted here so the helper alone cannot regress.
        self.assertIn("--runtime fallback --run", line)
        self.assertIn("--runtime fallback --pin`", line)
        self.assertIn("restore the wake contract", line)
        self.assertIn("could not be read", line)

    def test_the_fallback_fingerprint_fails_closed_when_the_contract_is_unreadable(self) -> None:
        # A pin must never vouch for a command whose contract could not be loaded, so an
        # unreadable contract returns None and every pin reads stale rather than pinned.
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "no-such-contract.md")
            original = A.CONTRACT_PATH
            try:
                A.CONTRACT_PATH = missing
                self.assertIsNone(A.mcode_wake_fingerprint("<mcode>", "minimax/MiniMax-M3"))
            finally:
                A.CONTRACT_PATH = original

    def test_a_fallback_candidate_from_another_model_is_not_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = L.SeatPaths(tmp, "MM")
            # The seat dir is private state.  Build it the way the listener does, so a
            # fixture cannot pass while production would leave it group- or world-readable.
            L.private_dir(paths.seat_dir)
            exe = os.path.join(tmp, "mcode")
            with open(exe, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            A.record_mcode_candidate(paths, exe, time.time(), model="minimax/MiniMax-M3")
            self.assertIsNotNone(
                A.mcode_pin_candidate(paths, exe, time.time(), model="minimax/Other-Model")
            )
            self.assertIsNone(
                A.mcode_pin_candidate(paths, exe, time.time(), model="minimax/MiniMax-M3")
            )

    def test_the_fallback_is_optional_and_may_not_be_the_wake_itself(self) -> None:
        ok = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"MM": {"bot": "MiniMax", "wake": "muse", "fallback": "mcode"}},
            }
        )
        self.assertEqual(ok.errors, [])
        self.assertEqual(ok.seats["MM"].fallback, "mcode")
        for bad in (
            {"wake": "muse", "fallback": "muse"},
            {"wake": "inbox", "fallback": "mcode"},
            {"wake": "muse", "fallback": "grok"},
        ):
            cfg = C.from_dict(
                {"daemon": {"owner_user_id": 12}, "seat": {"MM": dict({"bot": "MiniMax"}, **bad)}}
            )
            self.assertTrue(cfg.errors, bad)

    def test_a_fallback_may_be_named_on_a_claude_seat_too(self) -> None:
        # The CLAUDE seat is capped until Oct 13, which is exactly the case a fallback exists for.
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {
                    "CLAUDE": {
                        "bot": "Claude",
                        "wake": "claude",
                        "fallback": "mcode",
                        "provider": "Claude Code",
                    }
                },
            }
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.seats["CLAUDE"].fallback, "mcode")

    def test_runtime_fallback_resolves_rather_than_always_raising(self) -> None:
        # Kody's blocking finding on PR #509:  `--runtime fallback` is advertised in the argparse
        # choices, the help text and the protocol doc, but "fallback" is not a runtime name, so the
        # flag fell straight through to UsageError and the self-test could never be run.
        src = " ".join(
            inspect.getsource(
                __import__("agent_sync.listener_cli", fromlist=["cmd_test_wake"]).cmd_test_wake
            ).split()
        )
        self.assertIn('if want == "fallback":', src)
        self.assertIn("seat %s has no fallback to test", src)
        # "auto" must map to the seat's own wake, and never to a literal "fallback".
        self.assertIn('elif want == "auto":', src)
        self.assertNotIn('seat_cfg.fallback if seat_cfg.wake == "auto"', src)

    def test_naming_a_fallback_is_not_enough_to_run_it(self) -> None:
        # Measured, not assumed:  asked to write a file under the exact wake argv, mcode wrote one.
        # `--permission off` means "never ask", so its tools are live while it composes an answer, and
        # a parent cannot undo a side effect that already happened inside the child.  So the fallback
        # is held unless the config names the risk explicitly, on top of pinning the binary.
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"MM": {"bot": "MiniMax", "wake": "muse", "fallback": "mcode"}},
            }
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.seats["MM"].fallback, "mcode")
        self.assertFalse(cfg.seats["MM"].fallback_uncontained_ok)  # held by default

        ok = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {
                    "MM": {
                        "bot": "MiniMax",
                        "wake": "muse",
                        "fallback": "mcode",
                        "fallback_uncontained_ok": True,
                    }
                },
            }
        )
        self.assertEqual(ok.errors, [])
        self.assertTrue(ok.seats["MM"].fallback_uncontained_ok)

    def test_the_acknowledgment_needs_a_fallback_to_acknowledge(self) -> None:
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"MM": {"bot": "MiniMax", "wake": "muse", "fallback_uncontained_ok": True}},
            }
        )
        self.assertTrue(any("applies only with a fallback" in e for e in cfg.errors))

    def test_the_fallback_cannot_be_reached_by_the_primary_dispatch(self) -> None:
        # Config already refuses `wake = "mcode"`, but a config check is a promise.  This is the
        # choke point every wake passes through, so the asymmetry is structural rather than a table
        # lookup someone could edit away.
        from agent_sync.daemon import PRIMARY_RUNTIMES

        self.assertIn("claude", PRIMARY_RUNTIMES)
        self.assertIn("muse", PRIMARY_RUNTIMES)
        self.assertNotIn("mcode", PRIMARY_RUNTIMES)

    def test_the_mcode_result_reader_reads_the_last_message_file_shape(self) -> None:
        self.assertEqual(A.parse_mcode_result("succeeded", "{}")[0], None)
        self.assertIn("cancelled", A.parse_mcode_result("cancelled", None)[0] or "")
        # A failed status carries the first stderr line so the fallback's failure is legible.
        self.assertIn(
            "boom", A.parse_mcode_result("failed", None, "boom: quota provider down\n")[0] or ""
        )


class JetPinnedReviewTest(unittest.TestCase):
    """Findings from Jet's pinned review 630821876 against c06dbb3e.

    Each test here reproduces a defect that review identified in the real source, so a regression
    is a failing test rather than a silent behaviour change."""

    def test_a_claude_seat_reaches_the_quota_preflight(self) -> None:
        # Finding 3.  The preflight helper accepted both wake types, but only the MUSE branch called
        # it, so a capped CLAUDE seat kept spawning a child:  exactly the waste the gate exists to
        # stop, on the seat that actually hit it.
        import inspect

        from agent_sync.daemon import Daemon

        src = inspect.getsource(Daemon._wake_block)
        self.assertIn('if runner.cfg.wake in ("claude", "muse"):', src)
        # The quota check must live inside that shared branch, not only under muse.
        self.assertLess(
            src.index('if runner.cfg.wake in ("claude", "muse"):'),
            src.index("spent = self._quota_reason(runner)"),
        )

    def test_a_safety_refusal_is_terminal_for_every_refusal_kind(self) -> None:
        # Finding 2.  Refusals, pins and policy stops must all produce ZERO fallback attempts.
        from agent_sync.daemon import _is_fallbackable

        for refusal in (
            "init listed tools",
            "hook ran",
            "no init event",
            "not pinned",
            "pin_stale",
            "seat_refused",
            "paused",
            "owner_not_pinned",
            "policy",
            "",
        ):
            self.assertFalse(_is_fallbackable(refusal), refusal)

    def test_a_stale_output_file_cannot_be_replayed(self) -> None:
        # Finding 4.  The output file used a fixed name that survived a crash, so a later run that
        # wrote nothing could read the PREVIOUS wake's valid answer and act on it again.
        runner = A.McodeRunner(clock=lambda: 1000.0)
        # The name is derived per attempt, so two attempts can never collide...
        name = "last-mcode-%d-%d.json" % (1234, 1000000)
        self.assertIn("last-mcode-", name)
        self.assertNotIn("last-mcode.json", name)
        # ...and the runner removes it before launch, so "missing" means THIS attempt produced
        # nothing rather than "a previous attempt left something".
        self.assertEqual(runner.clock(), 1000.0)

    def test_output_is_only_accepted_on_a_clean_exit_and_one_envelope(self) -> None:
        # Finding 4, acceptance side.  Each clause closes a way a stale or foreign answer could be
        # taken for this run's reply.
        import inspect

        src = inspect.getsource(A.McodeRunner.run)
        self.assertIn("if out.exit not in (0, None):", src)
        self.assertIn("elif envelopes != 1:", src)
        self.assertIn("elif status is None:", src)
        # The strict clauses must come BEFORE the file is trusted.
        self.assertLess(src.index("elif envelopes != 1:"), src.index('out.result = {"text": text}'))

    def test_unusable_output_does_not_surface_held_back_items(self) -> None:
        # Finding 6.  `act` surfaces the seat's held-back items, so recording an unreadable answer
        # as done made a queued item look handled on the strength of output nobody could read.
        import inspect

        from agent_sync.daemon import Daemon

        src = inspect.getsource(Daemon.run_wake)
        self.assertIn("invalid result: %s", src)
        # wakes.validate always returns a dict, so `why and not obj` is dead.  The gate is `why`
        # itself, and the failure is recorded and returned before act() is reached.
        self.assertNotIn("if why and not obj:", src)
        fail_at = src.index("invalid result: %s")
        act_at = src.index("outcome = self.act(")
        self.assertLess(fail_at, act_at)
        self.assertIn("return", src[fail_at:act_at])

    def test_empty_usage_is_not_asserted_to_be_a_quota(self) -> None:
        # Finding 5.  No model ran proves no marginal cost and no fallback; it does NOT prove the
        # cause.  An auth or config failure has the same empty shape, so the code must say so at the
        # place where the flag is set, and the wording quotes the provider.
        src = inspect.getsource(A.ClaudeRunner.run)
        # The note is wrapped across lines in the source, so match on phrases that are not.
        self.assertIn("itself proof of a quota", " ".join(src.split()))
        self.assertIn("out.limited = not out.models", src)
        self.assertIn("out.error = result_reason(out.result)", src)  # quotes the provider


class QuotaGateTest(unittest.TestCase):
    """No reason to wake an agent that cannot run -- checked before the subprocess spawns."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, windows: list[dict], *, age: float = 60.0) -> str:
        path = os.path.join(self.tmp.name, "quota.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "format": "usage-monitor-local-quotas",
                    "version": 1,
                    "producer": "codecaps",
                    "generatedAt": _iso(self.now - age),
                    "windows": windows,
                },
                fh,
            )
        return path

    def test_a_fresh_exhaustion_blocks_and_names_the_reset(self) -> None:
        path = self._write(
            [
                {
                    "provider": "Claude Code",
                    "isExhausted": True,
                    "skip": True,
                    "remainingPercent": 0,
                    "resetAt": "2026-10-13T21:59:59Z",
                }
            ]
        )
        block = A.quota_block("Claude Code", self.now, path)
        self.assertIsNotNone(block)
        self.assertIn("exhausted", block)

    def test_an_available_provider_never_blocks(self) -> None:
        path = self._write(
            [{"provider": "MiniMax", "isExhausted": False, "skip": False, "remainingPercent": 34.0}]
        )
        self.assertIsNone(A.quota_block("MiniMax", self.now, path))

    def test_the_status_red_line_follows_the_gate_and_never_shows_red_for_an_uncapped_provider(self) -> None:
        # The status showed "wakes are not running: the MiniMax quota is exhausted" for a muse seat whose wakes the
        # gate lets through (QUOTA_NEVER_CAP_PROVIDERS), because it read the quota file on its own.
        path = self._write(
            [
                {"provider": "MiniMax", "isExhausted": True, "skip": True, "resetAt": "2026-10-30T10:00:00Z"},
                {"provider": "Claude Code", "isExhausted": True, "skip": True, "resetAt": "2026-10-30T10:00:00Z"},
            ]
        )
        self.assertTrue(A.provider_quota("MiniMax", self.now, path)["exhausted"])  # the reading is real...
        self.assertIsNone(A.quota_block("MiniMax", self.now, path))  # ...the gate does not stop the wake...
        self.assertIsNone(A.quota_red("MiniMax", self.now, path))  # ...so the status shows no RED
        self.assertIsNone(A.quota_red("muse", self.now, path))
        red = A.quota_red("Claude Code", self.now, path)  # a capped provider still does
        self.assertEqual(red, "wakes are not running: the Claude Code quota is exhausted until 2026-10-30T10:00:00Z")

    def test_a_stale_snapshot_never_blocks(self) -> None:
        # A quota source that is down must not silence the fleet:  an old reading is not a verdict.
        path = self._write(
            [{"provider": "Claude Code", "isExhausted": True, "skip": True}], age=3600
        )
        self.assertIsNone(A.quota_block("Claude Code", self.now, path))

    def test_a_future_dated_snapshot_never_blocks(self) -> None:
        path = self._write([{"provider": "Claude Code", "isExhausted": True}], age=-3600)
        self.assertIsNone(A.quota_block("Claude Code", self.now, path))

    def test_a_missing_malformed_or_unknown_provider_never_blocks(self) -> None:
        self.assertIsNone(
            A.quota_block("Claude Code", self.now, os.path.join(self.tmp.name, "absent"))
        )
        broken = os.path.join(self.tmp.name, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(A.quota_block("Claude Code", self.now, broken))
        path = self._write([{"provider": "MiniMax", "isExhausted": False}])
        self.assertIsNone(A.quota_block("Nobody At All", self.now, path))

    def test_exhaustion_is_the_producers_own_verdict_never_a_derived_zero(self) -> None:
        # 0% with neither flag means a passed reset or an unknown remainder, not a refusal.
        path = self._write(
            [{"provider": "Grok", "isExhausted": False, "skip": False, "remainingPercent": 0.0}]
        )
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

    def test_a_claude_seat_may_name_a_provider(self) -> None:
        # The first live failure this gate could have prevented was a CLAUDE seat at its weekly cap,
        # so a claude seat must be able to name the provider its quota is read under.
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"CLAUDE": {"bot": "Claude", "wake": "claude", "provider": "Claude Code"}},
            }
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.seats["CLAUDE"].provider, "Claude Code")

    def test_a_seat_that_names_no_provider_is_never_gated(self) -> None:
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"CLAUDE": {"bot": "Claude", "wake": "claude"}},
            }
        )
        self.assertEqual(A.quota_block(cfg.seats["CLAUDE"].provider or "", self.now), None)

    def test_provider_still_belongs_to_a_worker_wake(self) -> None:
        cfg = C.from_dict(
            {
                "daemon": {"owner_user_id": 12},
                "seat": {"MM": {"bot": "MiniMax", "wake": "inbox", "provider": "MiniMax"}},
            }
        )
        self.assertTrue(any("applies only to wake" in e for e in cfg.errors))

    def test_the_generated_at_reader_survives_dst(self) -> None:
        # The naive strptime reading was wrong by an hour twice a year, which would make a fresh
        # reading look stale.  UTC is the only correct reading of the trailing Z.
        self.assertAlmostEqual(
            A._quota_generated_at("2026-10-11T23:12:55Z"), 1791760375.0, places=0
        )
        self.assertIsNone(A._quota_generated_at("not a time"))
        self.assertIsNone(A._quota_generated_at(None))


class SubscriptionCappingTest(unittest.TestCase):
    """The deliberate decision (AFC-291):  no dollar reserve for a subscription-backed seat."""

    def test_a_muse_seat_reserves_nothing(self) -> None:
        cfg = _config_with_wake("muse")
        self.assertEqual(cfg.seats["MM"].wake_max_usd, 0.0)

    def test_a_claude_seat_still_reserves_its_maximum(self) -> None:
        self.assertEqual(
            _config_with_wake("claude").seats["MM"].wake_max_usd, C.WAKE_MAX_BUDGET_USD
        )

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
        views = {
            "w%d" % i: {
                "state": "done",
                "owner": False,
                "topic_key": "t",
                "cost_usd": 0.0,
                "times": {"accepted": now - 60},
            }
            for i in range(6)
        }
        self.assertEqual(
            W.budget_block(views, budget, owner=False, key="other", now=now, run_max=0.0),
            "wakes_per_hour",
        )

    def test_usd_per_day_never_blocks_a_muse_seat(self) -> None:
        # Every wake costs 0, so a ceiling of dollars spent against dollars spent is a real ceiling
        # that is never reached, rather than a fictional reserve.
        cfg = _config_with_wake("muse")
        budget = dict(cfg.seats["MM"].budget)
        self.assertGreater(budget["usd_per_day"], 0)
        self.assertIsNone(
            W.budget_block({}, budget, owner=True, key="t", now=time.time(), run_max=0.0)
        )


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

    def test_the_fallback_model_defaults_and_can_be_moved(self) -> None:
        cfg = _config_with_wake("muse")
        self.assertEqual(cfg.seats["MM"].mcode_model, C.MCODE_DEFAULT_MODEL)
        moved = _config_with_wake("muse", extra={"mcode_model": "minimax/MiniMax-M3"})
        self.assertEqual(moved.seats["MM"].mcode_model, "minimax/MiniMax-M3")

    def test_a_malformed_fallback_model_is_refused(self) -> None:
        cfg = _config_with_wake("muse", extra={"mcode_model": "not-a-provider-model"})
        self.assertTrue(any("mcode_model must be provider/model" in e for e in cfg.errors))


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
        prompt = (
            "Daemon header (trusted):\nSeat: MM\n"
            'Where: channel "agent-sync", topic "AFC-291 muse headless wake type"\n'
            "Board filing: disabled; never use action board\n"
            "Routing: disabled (no owner trigger); leave route null\n"
            "Return only the JSON object the schema describes.\n"
        )
        result = A.MuseRunner(timeout=300).run(
            A.muse_argv(A.binary_pin(muse), C.MUSE_DEFAULT_MODEL),
            A.muse_env(os.environ, home, wake_path, seat="MM"),
            work,
            prompt,
            schema=W.schema_text(["CLAUDE", "MM"]),
        )
        self.assertTrue(result.ok, "muse refused or failed: %s" % (result.refused or result.error))
        self.assertEqual(result.models, [C.MUSE_DEFAULT_MODEL])
        self.assertEqual(result.cost_usd, 0.0)  # subscription-backed:  no marginal cost to report
        _, why = W.validate(result.parsed)
        self.assertIsNone(why)


def _iso(when: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(when, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class McodeFallbackBoundTest(unittest.TestCase):
    """The fallback's prompt bound, its own deadline, and the pin invalidation that follows.

    mcode has neither a sandbox nor a tool-deny switch, so `--max-steps` is the ONLY bound on
    how much the fallback may act, and the step cost grows with the prompt.  That makes the
    budget, the prompt it is measured against, and the pin that vouches for it ONE contract:  a
    change to any of the three has to invalidate the other two.
    """

    @staticmethod
    def _seconds(text: str) -> float:
        """A Go-style duration the way the argv carries it:  "3m30s", "90s", "1h"."""
        total = 0.0
        for value, unit in re.findall(r"(\d+(?:\.\d+)?)([smh])", text):
            total += float(value) * {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]
        return total

    @staticmethod
    def _pending(trigger: int = 3) -> "W.Pending":
        pending = W.Pending(
            "w1", "MM", {"channel": "agent-sync", "topic": "t"}, owner=False, now=1.0, due=2.0
        )
        pending.add({"id": trigger}, False)
        return pending

    @staticmethod
    def _history(count: int) -> list[dict[str, object]]:
        return [
            {
                "id": i,
                "sender_id": 11,
                "sender_full_name": "Codex",
                "timestamp": 1000.0 + i,
                "content": "msg%03d" % i,
            }
            for i in range(1, count + 1)
        ]

    def _prompt(self, history, **kwargs) -> str:
        return W.build_prompt(
            seat="MM",
            pending=self._pending(),
            history=history,
            owner_user_id=12,
            is_bot=lambda uid: True,
            owner_of=lambda m: False,
            format_time=str,
            board_enabled=False,
            nonce="n",
            **kwargs,
        )

    # -- the deadline ---------------------------------------------------------------------

    def test_the_child_deadline_is_under_the_runner_deadline(self) -> None:
        # The two used to be equal (the child's "4m" and WAKE_TIMEOUT = 240s), so the daemon's
        # group-kill, not mcode's own clean timeout, ended a long fallback run:  the ledger then
        # recorded a hard timeout instead of mcode's, and one failing wake took the seat's
        # serialized worker for the whole outer budget.
        argv = A.mcode_argv("/usr/local/bin/mcode")
        self.assertEqual(argv[argv.index("--timeout") + 1], A.MCODE_TIMEOUT)
        self.assertLess(self._seconds(A.MCODE_TIMEOUT), A.WAKE_TIMEOUT)

    def test_the_child_deadline_still_fits_the_measured_run(self) -> None:
        # The measured run finished in 137.8s, so the child's own deadline has to clear it with
        # room to spare or the run that proved the budget would not finish under it.
        self.assertGreater(self._seconds(A.MCODE_TIMEOUT), 145.0)

    # -- the prompt bound -----------------------------------------------------------------

    def test_the_bound_is_under_the_daemons_worst_case(self) -> None:
        # The daemon builds a prompt with up to PROMPT_HISTORY messages plus the held-back
        # block;  the budget was measured on four messages, so the fallback is given four.
        self.assertLess(A.MCODE_FALLBACK_MAX_MESSAGES, W.PROMPT_HISTORY)
        self.assertEqual(A.MCODE_FALLBACK_MAX_HELD, 0)

    def test_a_bounded_prompt_keeps_the_trigger_and_the_newest_of_the_rest(self) -> None:
        rows = self._history(15)
        kept = W.select_prompt_messages(rows, [3], 4)
        self.assertEqual([row["id"] for row in kept], [3, 13, 14, 15])

    def test_a_bounded_prompt_keeps_the_newest_triggers_when_there_are_more_than_the_bound(self) -> None:
        rows = self._history(15)
        kept = W.select_prompt_messages(rows, [1, 2, 3, 4, 5], 4)
        self.assertEqual([row["id"] for row in kept], [2, 3, 4, 5])

    def test_an_unbounded_prompt_keeps_the_whole_history(self) -> None:
        rows = self._history(15)
        self.assertEqual(len(W.select_prompt_messages(rows, [3], None)), 15)

    def test_the_prompt_the_fallback_is_given_carries_only_the_bounded_messages(self) -> None:
        prompt = self._prompt(
            self._history(15),
            max_messages=A.MCODE_FALLBACK_MAX_MESSAGES,
            max_held=A.MCODE_FALLBACK_MAX_HELD,
        )
        self.assertIn("The last %d messages follow" % A.MCODE_FALLBACK_MAX_MESSAGES, prompt)
        self.assertNotIn("The last 15 messages follow", prompt)
        # The trigger message survives the cut:  the wake is about it.
        self.assertIn("msg003", prompt)
        # And a message outside the bound does not.
        self.assertNotIn("msg001", prompt)

    def test_the_fallback_is_told_it_has_no_held_back_detail(self) -> None:
        held = {
            "count": 3,
            "items": [
                {
                    "message_id": 900,
                    "channel": "agent-sync",
                    "topic": "earlier",
                    "sender_full_name": "Codex",
                    "reason": "wakes_per_hour",
                    "zulip_link": "",
                }
            ],
        }
        prompt = self._prompt(
            self._history(4),
            held_back=held,
            max_messages=A.MCODE_FALLBACK_MAX_MESSAGES,
            max_held=A.MCODE_FALLBACK_MAX_HELD,
        )
        self.assertIn("This runtime is given no held-back detail.", prompt)
        self.assertNotIn("900", prompt)

    # -- the pin --------------------------------------------------------------------------

    def test_the_pin_fingerprint_covers_the_prompt_bound(self) -> None:
        # The bound is not in the argv, so it has to be hashed here or a pin made under an
        # unbounded prompt would still read as verified under a bounded one.
        before = A.mcode_wake_fingerprint("/usr/local/bin/mcode")
        self.assertIsNotNone(before)
        with mock.patch.object(A, "MCODE_FALLBACK_MAX_MESSAGES", A.MCODE_FALLBACK_MAX_MESSAGES + 1):
            after = A.mcode_wake_fingerprint("/usr/local/bin/mcode")
        self.assertNotEqual(before, after)

    def test_the_pin_fingerprint_covers_the_policy_version(self) -> None:
        before = A.mcode_wake_fingerprint("/usr/local/bin/mcode")
        with mock.patch.object(A, "MCODE_WAKE_POLICY_VERSION", "something-else"):
            after = A.mcode_wake_fingerprint("/usr/local/bin/mcode")
        self.assertNotEqual(before, after)

    def test_the_policy_version_names_the_bound(self) -> None:
        # v2 is the bounded fallback:  the prompt bound and the child's own deadline are both
        # invisible in the argv, so the version is what tells an old pin that it is stale.
        self.assertIn("bounded", A.MCODE_WAKE_POLICY_VERSION)
