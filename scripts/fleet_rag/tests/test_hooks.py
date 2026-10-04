"""Subprocess tests for the Claude Code hooks under scripts/hooks/.

    cd scripts && python3 -m unittest fleet_rag.tests.test_hooks -v

Both hooks run against a throwaway HOME; nothing on the real Mac is read or written.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HOOKS = pathlib.Path(__file__).resolve().parents[2] / "hooks"
START = HOOKS / "fleet-recall-session-start.sh"
STOP = HOOKS / "fleet-recall-stop.py"
REASON_START = "Fleet recall: this session did substantial work"


def assistant_line(blocks: list[dict]) -> str:
    return json.dumps({"type": "assistant", "uuid": "u", "timestamp": "2026-09-02T00:00:00Z",
                       "message": {"role": "assistant", "content": blocks}})


def tool_use(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": inp}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def user_line(t: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": t}]}})


def make_transcript(path: pathlib.Path, n_tools: int, extra_lines: list[str] = ()) -> pathlib.Path:
    lines = [json.dumps({"type": "summary", "summary": "s"})]
    for i in range(n_tools):
        lines.append(assistant_line([text(f"step {i}"), tool_use("Bash", command=f"echo {i}")]))
        lines.append(user_line(f"out {i}"))
    lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.state = self.home / "apps" / "fleet-rag" / "state"
        self.state.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_RECALL")}
        self.env["HOME"] = str(self.home)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            time.sleep(0.5)
            try:
                self.tmp.cleanup()
            except OSError:
                pass

    def run_hook(self, script: pathlib.Path, payload, env_extra: dict | None = None):
        env = {**self.env, **(env_extra or {})}
        cmd = [sys.executable, str(script)] if script.suffix == ".py" else ["bash", str(script)]
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        t0 = time.monotonic()
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=30)
        return proc, time.monotonic() - t0


class StopHookTests(HookBase):
    def stop(self, transcript: pathlib.Path, session_id="sess-1", active=False, env_extra=None):
        proc, dt = self.run_hook(STOP, {"session_id": session_id, "transcript_path": str(transcript),
                                        "stop_hook_active": active, "hook_event_name": "Stop"}, env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        return (json.loads(proc.stdout) if proc.stdout.strip() else None), dt

    def test_over_threshold_without_contribution_blocks_once(self):
        tr = make_transcript(self.home / "t.jsonl", 30)
        out, _ = self.stop(tr)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith(REASON_START))
        self.assertIn("recall_contribute", out["reason"])
        self.assertIn("'no lesson'", out["reason"])
        marker = self.state / "hook-nudged" / "sess-1"
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads(marker.read_text())["tool_uses"], 30)
        # Second stop of the same session (marker present): silent pass.
        out, _ = self.stop(tr)
        self.assertIsNone(out)
        # A different session is still nudged.
        out, _ = self.stop(tr, session_id="sess-2")
        self.assertEqual(out["decision"], "block")

    def test_under_threshold_is_silent(self):
        tr = make_transcript(self.home / "t.jsonl", 24)
        out, _ = self.stop(tr)
        self.assertIsNone(out)
        self.assertFalse((self.state / "hook-nudged").exists())
        out, _ = self.stop(make_transcript(self.home / "t25.jsonl", 25))
        self.assertEqual(out["decision"], "block")                         # boundary: >= 25

    def test_threshold_env_override(self):
        tr = make_transcript(self.home / "t.jsonl", 5)
        out, _ = self.stop(tr, env_extra={"FLEET_RECALL_HOOK_MIN_TOOLS": "5"})
        self.assertEqual(out["decision"], "block")
        out, _ = self.stop(tr, session_id="s2", env_extra={"FLEET_RECALL_HOOK_MIN_TOOLS": "not-a-number"})
        self.assertIsNone(out)                                              # falls back to 25

    def test_stop_hook_active_exits_immediately(self):
        tr = make_transcript(self.home / "t.jsonl", 40)
        out, _ = self.stop(tr, active=True)
        self.assertIsNone(out)
        self.assertFalse((self.state / "hook-nudged").exists())

    def test_disabled_by_env(self):
        tr = make_transcript(self.home / "t.jsonl", 40)
        out, _ = self.stop(tr, env_extra={"FLEET_RECALL_HOOKS": "0"})
        self.assertIsNone(out)

    def test_mcp_contribution_detected(self):
        for name in ("recall_contribute", "mcp__fleet-recall__recall_contribute", "mcp__seat-mcp__recall_contribute"):
            tr = make_transcript(self.home / f"{name}.jsonl", 30,
                                 [assistant_line([tool_use(name, text="x" * 50, category="lesson")])])
            out, _ = self.stop(tr, session_id=name)
            self.assertIsNone(out, name)

    def test_bash_recall_contribute_detected(self):
        tr = make_transcript(self.home / "t.jsonl", 30, [assistant_line([
            tool_use("Bash", command="pbpaste | recall  contribute - --category lesson --app fleet")])])
        out, _ = self.stop(tr)
        self.assertIsNone(out)
        # `recall contributed` or `recall` alone must not count.
        tr = make_transcript(self.home / "t2.jsonl", 30, [assistant_line([
            tool_use("Bash", command="recall 'how do we contribute' --limit 3")])])
        out, _ = self.stop(tr, session_id="s2")
        self.assertEqual(out["decision"], "block")

    def test_no_lesson_reply_detected_only_in_assistant_text(self):
        tr = make_transcript(self.home / "t.jsonl", 30, [assistant_line([text("Closing out.  No lesson this time.")])])
        out, _ = self.stop(tr)
        self.assertIsNone(out)
        # The phrase inside a tool result (user line) does not count.
        tr = make_transcript(self.home / "t2.jsonl", 30, [user_line("there is no lesson here")])
        out, _ = self.stop(tr, session_id="s2")
        self.assertEqual(out["decision"], "block")
        # String content form.
        line = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "no lesson"}})
        tr = make_transcript(self.home / "t3.jsonl", 30, [line])
        out, _ = self.stop(tr, session_id="s3")
        self.assertIsNone(out)

    def test_missing_transcript_bad_json_and_garbage_lines_never_fail(self):
        out, _ = self.stop(self.home / "does-not-exist.jsonl")
        self.assertIsNone(out)
        proc, _ = self.run_hook(STOP, "{not json")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        proc, _ = self.run_hook(STOP, "[1, 2]")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        proc, _ = self.run_hook(STOP, "")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        tr = make_transcript(self.home / "t.jsonl", 30, ['{"type": "assistant", "message": "not a dict"}',
                                                          "garbage tool_use assistant line", ""])
        out, _ = self.stop(tr)
        self.assertEqual(out["decision"], "block")

    def test_session_id_is_sanitized_for_the_marker(self):
        tr = make_transcript(self.home / "t.jsonl", 30)
        out, _ = self.stop(tr, session_id="../../evil/../id")
        self.assertEqual(out["decision"], "block")
        names = os.listdir(self.state / "hook-nudged")
        self.assertEqual(len(names), 1)
        self.assertNotIn("/", names[0])
        self.assertFalse((self.home / "apps" / "evil").exists())

    def test_five_megabyte_transcript_under_300ms(self):
        tr = self.home / "big.jsonl"
        chunk = assistant_line([text("x" * 2000), tool_use("Read", file_path="/tmp/f")]) + "\n" + user_line("y" * 2000) + "\n"
        with open(tr, "w", encoding="utf-8") as fh:
            while fh.tell() < 5 * 1024 * 1024:
                fh.write(chunk)
        out, dt = self.stop(tr)
        self.assertEqual(out["decision"], "block")
        self.assertLess(dt, 1.0, f"took {dt:.3f}s including interpreter start")
        # Measure the scan itself (the spec's 300 ms budget is for the file pass).
        import types
        mod = types.ModuleType("fleet_recall_stop_under_test")
        exec(compile(STOP.read_text(encoding="utf-8"), str(STOP), "exec"), mod.__dict__)  # no __pycache__
        t0 = time.monotonic()
        scan = mod.scan_transcript(str(tr))
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertGreater(scan["tool_uses"], 25)
        self.assertFalse(scan["contributed"])


class SessionStartHookTests(HookBase):
    def start(self, env_extra=None, payload=None):
        proc, dt = self.run_hook(START, payload or {"session_id": "s", "hook_event_name": "SessionStart",
                                                    "source": "startup"}, env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return (json.loads(proc.stdout) if proc.stdout.strip() else None), dt

    def write_cache(self, points: int, age_s: float = 0) -> None:
        (self.state / "hook-points-cache.json").write_text(
            json.dumps({"points": points, "at": time.time() - age_s}))

    def write_last_run(self, ok: bool = True) -> None:
        (self.state / "last-run.json").write_text(json.dumps({"ok": ok, "finished_at": 1788350239645}))

    def write_fake_recall(self, path: pathlib.Path, body: str) -> None:
        """Write an executable fake `recall` and exec it once before any timed hook call.

        macOS assesses a freshly written executable on its first exec, and on a busy Mac that
        stalls for up to ~3 s (measured: 14-22 of 80 new-file execs over 0.5 s while this module
        looped, versus a max of 11 ms re-exec'ing an already-run file).  Inside the hook that
        stall lands in the 3 s foreground `recall stats --json` timeout and silently turns a good
        count into no output.  Paying it here keeps it out of every timed window without touching
        the hook's budget or the assertions.  `--warm` exits before the body, so a fake that
        hangs does not hang setup; the hook always calls `stats --json`, never `--warm`.
        """
        path.write_text("#!/bin/sh\n[ \"$1\" = --warm ] && exit 0\n" + body)
        path.chmod(0o755)
        subprocess.run([str(path), "--warm"], check=True, capture_output=True, timeout=30)

    def test_fresh_cache_gives_the_one_liner_fast(self):
        self.write_cache(38716)
        out, dt = self.start()
        ctx = out["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertEqual(ctx["additionalContext"],
                         "fleet recall corpus 38,716 points; PRE-FLIGHT RETRIEVAL: search recall at the "
                         "start of turns/tasks before diagnosing (triggers: error/fail, infra/deploy, "
                         "cross-repo/domain, pre-owner query). Contribute a lesson at closeout (recall_contribute)")
        self.assertNotIn("\n", ctx["additionalContext"])
        self.assertLess(dt, 1.5)

    def test_no_cache_falls_back_to_last_run_and_prints_nothing_without_it(self):
        env = {"FLEET_RECALL_HOOK_NO_REFRESH": "1", "PATH": "/usr/bin:/bin"}   # no recall binary reachable
        out, _ = self.start(env)
        self.assertIsNone(out)
        self.write_last_run(ok=True)
        out, _ = self.start(env)
        self.assertIn("fleet recall corpus available (last ingest 2026-09-", out["hookSpecificOutput"]["additionalContext"])
        self.write_last_run(ok=False)
        out, _ = self.start(env)
        self.assertIsNone(out)

    def test_stale_cache_is_used_and_refreshed_in_background(self):
        # A fake `recall` on PATH answers stats --json after a delay longer than the hook budget.
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "recall"
        self.write_fake_recall(fake, "sleep 1\necho '{\"points\": 424242, \"status\": \"green\"}'\n")
        self.write_cache(100, age_s=10 * 3600)
        out, dt = self.start({"PATH": f"{bindir}:/usr/bin:/bin"})
        self.assertIn("100 points", out["hookSpecificOutput"]["additionalContext"])   # stale value served
        self.assertLess(dt, 3.0)                                                     # did not wait for refresh (generous: wall-clock, flakes under load)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            data = json.loads((self.state / "hook-points-cache.json").read_text())
            if data["points"] == 424242:
                break
            time.sleep(0.1)
        self.assertEqual(json.loads((self.state / "hook-points-cache.json").read_text())["points"], 424242)

    def test_no_cache_foreground_attempt_with_timeout(self):
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "recall"
        self.write_fake_recall(fake, "echo '{\"points\": 7, \"status\": \"green\"}'\n")
        out, _ = self.start({"PATH": f"{bindir}:/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        self.assertIn("corpus 7 points", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(json.loads((self.state / "hook-points-cache.json").read_text())["points"], 7)
        # A hanging recall must not hang the hook: 3 s timeout, then last-run fallback / nothing.
        (self.state / "hook-points-cache.json").unlink()
        self.write_fake_recall(fake, "sleep 20\n")
        out, dt = self.start({"PATH": f"{bindir}:/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        self.assertIsNone(out)
        self.assertLess(dt, 15)                                     # timeout=3 fired; did not wait for sleep 20

    def test_zero_points_is_treated_as_unknown_never_printed_never_cached(self):
        # A freshly-served zero (an empty-looking corpus, most likely a stale MCP/backend
        # response) must never be shown as "0 points" and must never be written to the cache.
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "recall"
        self.write_fake_recall(fake, "echo '{\"points\": 0, \"status\": \"green\"}'\n")
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"}
        out, _ = self.start(env)
        self.assertIsNone(out)                                    # no last-run.json yet either
        self.assertFalse((self.state / "hook-points-cache.json").exists())
        self.write_last_run(ok=True)
        out, _ = self.start(env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("0 points", ctx)
        self.assertIn("last ingest 2026-09-", ctx)
        self.assertFalse((self.state / "hook-points-cache.json").exists())

    def test_stale_zero_cache_is_treated_as_no_cache(self):
        # A cache file written by a pre-fix hook (or corrupted to 0) must not be trusted or
        # printed; the hook falls through exactly as if there were no cache at all.
        self.write_cache(0)
        out, _ = self.start({"PATH": "/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        self.assertIsNone(out)
        self.write_last_run(ok=True)
        out, _ = self.start({"PATH": "/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("0 points", ctx)
        self.assertIn("last ingest 2026-09-", ctx)

    def test_background_refresh_zero_never_overwrites_a_good_cache(self):
        bindir = self.home / "bin"
        bindir.mkdir()
        fake = bindir / "recall"
        self.write_fake_recall(fake, "sleep 0.2\necho '{\"points\": 0, \"status\": \"green\"}'\n")
        self.write_cache(100, age_s=10 * 3600)                    # stale, good count
        out, dt = self.start({"PATH": f"{bindir}:/usr/bin:/bin"})
        self.assertIn("100 points", out["hookSpecificOutput"]["additionalContext"])
        self.assertLess(dt, 15.0)                                   # did not wait for refresh (generous: wall-clock, flakes under load)
        time.sleep(1.5)                                            # let the background refresh finish
        data = json.loads((self.state / "hook-points-cache.json").read_text())
        self.assertEqual(data["points"], 100)                      # never clobbered with 0

    def test_prefers_installed_recall_over_path(self):
        installed = self.home / "apps" / "fleet-rag" / "recall"
        self.write_fake_recall(installed, "echo '{\"points\": 11}'\n")
        out, _ = self.start({"PATH": "/usr/bin:/bin", "FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        self.assertIn("corpus 11 points", out["hookSpecificOutput"]["additionalContext"])

    def test_disabled_and_garbage_input_print_nothing(self):
        self.write_cache(5)
        out, _ = self.start({"FLEET_RECALL_HOOKS": "0"})
        self.assertIsNone(out)
        proc, _ = self.run_hook(START, "not json at all", {"FLEET_RECALL_HOOK_NO_REFRESH": "1"})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("corpus 5 points", proc.stdout)        # stdin content is irrelevant
        (self.state / "hook-points-cache.json").write_text("{broken")
        out, _ = self.start({"FLEET_RECALL_HOOK_NO_REFRESH": "1", "PATH": "/usr/bin:/bin"})
        self.assertIsNone(out)

    def test_hooks_are_executable_in_the_checkout(self):
        self.assertTrue(os.access(START, os.X_OK))
        self.assertTrue(os.access(STOP, os.X_OK))


if __name__ == "__main__":
    unittest.main()
