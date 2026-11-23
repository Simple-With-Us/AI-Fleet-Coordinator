"""Hermetic tests for the exact-version MiniMax context bridge and assigned transport."""

import importlib.util
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


BR = module("mm_bridge", ROOT / "simple_tools/minimax_origin_bridge.py")
AD = module("assigned_stdio", ROOT / "mcp-servers/assigned-hosted-stdio.py")
SID = "mvs_" + "a" * 32
OTHER = "mvs_" + "b" * 32
UUID = "019a1234-abcd-7123-8123-0123456789ab"


class Claims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"HOME": str(self.home), "__MAVIS_RUNTIME_SESSION_ID": OTHER}
        self.context = {
            "version": 1,
            "platform": "minimax",
            "session_id": SID,
            "turn_id": "turn-1",
            "tool_call_id": "call-1",
        }
        self.note("MM", SID, "minimax")

    def note(self, seat, sid, platform):
        folder = self.home / ".simple-tools" / seat / "letters"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (sid + ".json")
        path.write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "platform": platform,
                    "state": "open",
                    "label": "OO",
                    "generation": 3,
                }
            )
        )
        path.chmod(0o600)
        return path

    def claims(self, context=None, seat="MM"):
        return AD.native_origin_claims(
            self.env, seat, {BR.META_KEY: self.context if context is None else context}
        )

    def test_exact_session_ignores_conflicting_startup_environment(self):
        self.assertEqual(
            self.claims(),
            {
                "origin_session_claim": SID,
                "origin_letter_claim": "OO",
                "origin_generation_claim": 3,
            },
        )

    def test_no_context_does_not_fall_back_to_environment(self):
        self.assertEqual(AD.native_origin_claims(self.env, "MM", {}), {})
        self.assertEqual(AD.native_origin_claims(self.env, "MM", None), {})

    def test_invalid_contexts_hold(self):
        bad = [None, [], "session"]
        for key, value in [
            ("version", True),
            ("version", 2),
            ("platform", "codex"),
            ("session_id", UUID),
            ("session_id", "mvs_../bad"),
            ("turn_id", ""),
            ("tool_call_id", " "),
            ("turn_id", "x" * 257),
        ]:
            bad.append({**self.context, key: value})
        bad += [
            {k: v for k, v in self.context.items() if k != "turn_id"},
            {**self.context, "caller_override": SID},
        ]
        for context in bad:
            with self.subTest(context=context):
                self.assertEqual(
                    AD.native_origin_claims(self.env, "MM", {BR.META_KEY: context}), {}
                )

    def test_other_seats_and_cross_platform_metadata_hold(self):
        for seat in ("CODEX", "CLAUDE", "OPENCODE", "MM-OTHER"):
            self.assertEqual(self.claims(seat=seat), {})
        self.assertEqual(
            AD.native_origin_claims(self.env, "MM", {BR.META_KEY: self.context, "threadId": UUID}),
            {},
        )

    def test_closed_mismatched_and_unsafe_notes_hold(self):
        path = self.note("MM", SID, "minimax")
        original = json.loads(path.read_text())
        for key, value in [
            ("state", "closed"),
            ("platform", "codex"),
            ("session_id", OTHER),
            ("generation", True),
            ("label", "O"),
        ]:
            path.write_text(json.dumps({**original, key: value}))
            self.assertEqual(self.claims(), {})
        path.write_text(json.dumps(original))
        path.chmod(0o644)
        self.assertEqual(self.claims(), {})
        path.unlink()
        path.symlink_to(self.note("MM", OTHER, "minimax"))
        self.assertEqual(self.claims(), {})

    def test_adapter_injects_exact_claims_and_strips_caller_origin_fields(self):
        captured = []

        class Reply(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}

        def opener(request, timeout):
            captured.append(json.loads(request.data))
            return Reply(b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}')

        adapter = AD.Adapter("sbt_mm_" + "x" * 43, opener=opener, env=self.env)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "gr_open_pr",
                "arguments": {
                    "origin_session_claim": OTHER,
                    "origin_letter_claim": "ZZ",
                    "origin_generation_claim": 99,
                    "origin_extra": "forged",
                    "title": "test",
                },
                "_meta": {BR.META_KEY: self.context},
            },
        }
        adapter.call(request)
        args = captured[-1]["params"]["arguments"]
        self.assertEqual(args["identity"], "MM")
        self.assertEqual(args["origin_session_claim"], SID)
        self.assertEqual(args["origin_letter_claim"], "OO")
        self.assertEqual(args["origin_generation_claim"], 3)
        self.assertNotIn("origin_extra", args)
        del request["params"]["_meta"]
        adapter.call(request)
        self.assertFalse(
            any(name.startswith("origin_") for name in captured[-1]["params"]["arguments"])
        )

    def test_shape_diagnostic_is_private_empty_marker_only_and_one_shot(self):
        marker = self.home / ".simple-tools/MM/origin-identities-shape.once"
        params = {
            "name": "identities",
            "_meta": {BR.META_KEY: self.context, "untrusted-sensitive-key": "do-not-log"},
            "arguments": {"secret": "never-log-this"},
        }
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            AD.origin_shape_once(self.env, "MM", params)
        self.assertEqual(output.getvalue(), "")
        marker.touch(mode=0o600)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            AD.origin_shape_once(self.env, "CODEX", params)
            AD.origin_shape_once(self.env, "MM", {**params, "name": "gr_open_pr"})
        self.assertTrue(marker.exists())
        with contextlib.redirect_stderr(output):
            AD.origin_shape_once(self.env, "MM", params)
            AD.origin_shape_once(self.env, "MM", params)
        self.assertFalse(marker.exists())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        result = json.loads(output.getvalue())
        self.assertTrue(result["private_note_claims_valid"])
        self.assertTrue(result["exact_context_keys"])
        self.assertEqual(result["field_types"]["session_id"], "string")
        for value in (
            SID,
            "turn-1",
            "call-1",
            "do-not-log",
            "never-log-this",
            "untrusted-sensitive-key",
        ):
            self.assertNotIn(value, output.getvalue())
        for mode, text in ((0o644, ""), (0o600, "content")):
            marker.write_text(text)
            marker.chmod(mode)
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                AD.origin_shape_once(self.env, "MM", params)
            self.assertEqual(output.getvalue(), "")
            self.assertTrue(marker.exists())
            marker.unlink()

    def test_codex_existing_per_call_path_is_preserved(self):
        self.note("CODEX", UUID, "codex")
        claims = AD.native_origin_claims(self.env, "CODEX", {"threadId": UUID})
        self.assertEqual(claims["origin_session_claim"], UUID)
        self.assertEqual(
            AD.native_origin_claims(
                self.env, "CODEX", {"threadId": UUID, "x-codex-turn-metadata": {"thread_id": "bad"}}
            ),
            {},
        )


class RuntimeBridge(unittest.TestCase):
    def test_actual_projection_is_per_call_concurrent_and_not_from_arguments(self):
        # Execute the exact inserted JS expressions, not a Python reimplementation.
        projection = BR.PLUGIN_NEW.replace("this.pool.callTool", "capture")
        dispatch = BR.POOL_NEW.split("},void 0,{timeout:l,")[0] + "})"
        script = r"""
const seen=[]; const Go=e=>e.pluginName; const CNt=x=>x;
async function capture(server, name, args, override, options) {
  await new Promise(resolve=>setTimeout(resolve, args.delay));
  const r=name,n=args,s=options,c={client:{callTool:x=>x}};
  return DISPATCH;
}
async function invoke(s,a,e={pluginName:"fleet-own-tools",server:{name:"simple-tools"}}) {
  const r={name:"read"},o=undefined; return PROJECTION;
}
const base={turnId:"turn",toolCallId:"call"};
const owned=s=>({...s,afcNativeLease:{sessionId:s.sessionId,turnId:s.turnId}});
const first="mvs_"+"a".repeat(32), second="mvs_"+"b".repeat(32);
const out=await Promise.all([
 invoke(owned({...base,sessionId:first}),{delay:5,session_id:second,_meta:{forged:true}}),
 invoke(owned({...base,sessionId:second}),{delay:0,session_id:first}),
 invoke(owned({...base,sessionId:first}),{delay:0},{pluginName:"other",server:{name:"simple-tools"}}),
 invoke({...base,sessionId:"miniapp:fake"},{delay:0}),
 invoke({sessionId:first},{delay:0})
]); console.log(JSON.stringify(out));
""".replace("PROJECTION", projection).replace("DISPATCH", dispatch)
        done = subprocess.run(
            ["node", "--input-type=module"],
            input=script,
            text=True,
            capture_output=True,
            check=True,
        )
        out = json.loads(done.stdout)
        self.assertEqual(out[0]["_meta"][BR.META_KEY]["session_id"], SID)
        self.assertEqual(out[1]["_meta"][BR.META_KEY]["session_id"], OTHER)
        for item in out[2:]:
            self.assertNotIn("_meta", item)
        self.assertEqual(out[0]["arguments"]["session_id"], OTHER)

    def test_actual_child_context_branch_holds_both_child_types(self):
        # Exact installed child-context prefix at chunk-66K6XTTT offset 7997140.
        # Both branches carry parentAgentConfig; it is NOT a custody discriminator.
        child = "({sessionId:a.persistent?c:s.sessionId,turnId:u,workspaceRoot:s.workspaceRoot,reporter:g,parentAgentConfig:a.config})"
        # CONTEXT_NEW ends after the next member separator; close the object itself.
        top = BR.CONTEXT_NEW[len("return") :].rstrip(",") + "}"
        projection = BR.PLUGIN_NEW.replace("this.pool.callTool", "capture")
        script = (
            r"""
const Go=e=>e.pluginName,CNt=x=>x,capture=(...args)=>args[4];
const parent="mvs_"+"a".repeat(32),c="mvs_"+"b".repeat(32),u="child-turn",g={};
function child(persistent) { const a={persistent,config:{}},s={sessionId:parent,workspaceRoot:"/work"}; return CHILD; }
function top() { const e={lease:{sessionId:parent,turnId:"parent-turn"},agent:{agentName:"MM"},preparation:{agentConfig:{}}}; return TOP; }
function project(s) {const e={pluginName:"fleet-own-tools",server:{name:"simple-tools"}},r={name:"read"},a={},o=undefined;return PROJECTION;}
const np=child(false),p=child(true),main=top();
console.log(JSON.stringify({np,p,main,results:[project({...np,toolCallId:"call-child"}),project({...p,toolCallId:"call-child"}),project({...main,toolCallId:"call-main"})]}));
""".replace("CHILD", child)
            .replace("TOP", top)
            .replace("PROJECTION", projection)
        )
        done = subprocess.run(
            ["node", "--input-type=module"],
            input=script,
            text=True,
            capture_output=True,
            check=True,
        )
        out = json.loads(done.stdout)
        self.assertEqual(out["np"]["sessionId"], SID)
        self.assertEqual(out["p"]["sessionId"], OTHER)
        self.assertNotIn("afcNativeOrigin", out["results"][0])
        self.assertNotIn("afcNativeOrigin", out["results"][1])
        self.assertEqual(out["results"][2]["afcNativeOrigin"]["session_id"], SID)
        # Top-level marker is sourced from the native active lease.
        self.assertEqual(out["main"]["afcNativeLease"]["sessionId"], SID)

    def test_installer_drift_version_idempotence_and_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chunks").mkdir()
            package = root / "package.json"
            package.write_text(json.dumps({"name": "@minimax-ai/code", "version": "0.7.4"}))
            path = root / BR.CHUNK
            original = (BR.PLUGIN_OLD + ";" + BR.POOL_OLD).encode()
            path.write_bytes(original)
            with mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(original)):
                BR.run_chunk(root)
                self.assertEqual(path.read_bytes(), original)
                BR.run_chunk(root, apply=True)
                expected = path.read_bytes()
                self.assertNotEqual(expected, original)
                BR.run_chunk(root, apply=True)
                self.assertEqual(path.read_bytes(), expected)
                path.write_bytes(expected + b"drift")
                with self.assertRaises(ValueError):
                    BR.run_chunk(root, apply=True, rollback=True)
                path.write_bytes(expected)
                BR.run_chunk(root, apply=True, rollback=True)
                self.assertEqual(path.read_bytes(), original)
                package.write_text(json.dumps({"name": "@minimax-ai/code", "version": "0.7.5"}))
                with self.assertRaises(ValueError):
                    BR.run_chunk(root, apply=True)

    def test_partial_install_and_interrupted_rollback_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chunks").mkdir()
            (root / "package.json").write_text(
                json.dumps({"name": "@minimax-ai/code", "version": "0.7.4"})
            )
            plugin = (BR.PLUGIN_OLD + ";" + BR.POOL_OLD).encode()
            context = BR.CONTEXT_OLD.encode()
            (root / BR.CHUNK).write_bytes(plugin)
            (root / BR.CONTEXT_CHUNK).write_bytes(context)
            with (
                mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(plugin)),
                mock.patch.object(BR, "CONTEXT_SHA256", BR.digest(context)),
            ):
                # Crash after context installed, before plugin backup exists.
                BR.run_chunk(root, apply=True, context=True)
                self.assertFalse((root / (BR.CHUNK + BR.BACKUP_SUFFIX)).exists())
                BR.run(root, rollback=True, apply=True)
                self.assertEqual((root / BR.CONTEXT_CHUNK).read_bytes(), context)
                self.assertEqual((root / BR.CHUNK).read_bytes(), plugin)
                # Full install, then interruption after plugin rollback.
                BR.run(root, apply=True)
                BR.run_chunk(root, rollback=True, apply=True)
                BR.run(root, rollback=True, apply=True)
                self.assertEqual((root / BR.CONTEXT_CHUNK).read_bytes(), context)
                self.assertEqual((root / BR.CHUNK).read_bytes(), plugin)
                # Unknown bytes without backup can never count as restored.
                (root / (BR.CHUNK + BR.BACKUP_SUFFIX)).unlink()
                (root / BR.CHUNK).write_bytes(plugin + b"drift")
                with self.assertRaises((ValueError, OSError)):
                    BR.run(root, rollback=True, apply=True)

    def test_backup_directory_is_durable_before_live_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chunks").mkdir()
            (root / "package.json").write_text(
                json.dumps({"name": "@minimax-ai/code", "version": "0.7.4"})
            )
            original = (BR.PLUGIN_OLD + ";" + BR.POOL_OLD).encode()
            path = root / BR.CHUNK
            path.write_bytes(original)
            events = []
            real_fsync = os.fsync
            real_replace = BR.replace

            def fsync(fd):
                events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                real_fsync(fd)

            def replace(*args):
                self.assertGreaterEqual(len(events), 2)
                self.assertEqual(events[-2:], ["file", "directory"])
                events.append("replace")
                real_replace(*args)

            with (
                mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(original)),
                mock.patch.object(BR.os, "fsync", side_effect=fsync),
                mock.patch.object(BR, "replace", side_effect=replace),
            ):
                BR.run_chunk(root, apply=True)
            self.assertIn("replace", events)
            # A directory durability failure leaves live source untouched.
            path.write_bytes(original)
            (root / (BR.CHUNK + BR.BACKUP_SUFFIX)).unlink()

            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("synthetic directory sync failure")
                real_fsync(fd)

            with (
                mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(original)),
                mock.patch.object(BR.os, "fsync", side_effect=fail_directory),
            ):
                with self.assertRaises(OSError):
                    BR.run_chunk(root, apply=True)
            self.assertEqual(path.read_bytes(), original)
            # A retry must sync the already-visible backup directory entry too.
            with (
                mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(original)),
                mock.patch.object(BR.os, "fsync", side_effect=fail_directory),
            ):
                with self.assertRaises(OSError):
                    BR.run_chunk(root, apply=True)
            self.assertEqual(path.read_bytes(), original)

    def test_backup_file_sync_failure_retry_requires_file_then_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chunks").mkdir()
            (root / "package.json").write_text(
                json.dumps({"name": "@minimax-ai/code", "version": "0.7.4"})
            )
            original = (BR.PLUGIN_OLD + ";" + BR.POOL_OLD).encode()
            path = root / BR.CHUNK
            path.write_bytes(original)
            events = []
            real_fsync, real_replace = os.fsync, BR.replace

            def fail_file(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("synthetic backup content sync failure")
                real_fsync(fd)

            with mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(original)):
                with mock.patch.object(BR.os, "fsync", side_effect=fail_file):
                    with self.assertRaises(OSError):
                        BR.run_chunk(root, apply=True)
                    self.assertTrue((root / (BR.CHUNK + BR.BACKUP_SUFFIX)).exists())
                    with self.assertRaises(OSError):
                        BR.run_chunk(root, apply=True)
                self.assertEqual(path.read_bytes(), original)

                def fsync(fd):
                    events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                    real_fsync(fd)

                def replace(*args):
                    self.assertEqual(events, ["file", "directory"])
                    real_replace(*args)

                with (
                    mock.patch.object(BR.os, "fsync", side_effect=fsync),
                    mock.patch.object(BR, "replace", side_effect=replace),
                ):
                    BR.run_chunk(root, apply=True)

    def test_bad_anchor_and_hash_refuse(self):
        with self.assertRaises(ValueError):
            BR.patched(b"upstream changed")
        with mock.patch.object(BR, "ORIGINAL_SHA256", BR.digest(b"no anchors")):
            with self.assertRaises(ValueError):
                BR.patched(b"no anchors")


if __name__ == "__main__":
    unittest.main()
