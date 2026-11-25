import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "capture", Path(__file__).with_name("assigned-hosted-stdio.py")
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
SID = "019a1234-abcd-7123-8123-0123456789ab"


class NativeCapture(unittest.TestCase):
    def fixture(self, d):
        root = Path(d) / ".simple-tools/CODEX/letters"
        root.mkdir(parents=True)
        p = root / (SID + ".json")
        p.write_text(
            json.dumps(
                dict(session_id=SID, platform="codex", state="open", label="AA", generation=7)
            )
        )
        p.chmod(0o600)
        return p, dict(HOME=d, CODEX_THREAD_ID=SID)

    def test_exact_note_and_no_subprocess_or_registry(self):
        with tempfile.TemporaryDirectory() as d:
            p, env = self.fixture(d)
            self.assertEqual(
                M.native_origin_claims(env, "CODEX", {"threadId": SID}),
                dict(origin_session_claim=SID, origin_letter_claim="AA", origin_generation_claim=7),
            )
            self.assertEqual(M.native_origin_claims(env, "CLAUDE", {"threadId": SID}), {})

    def test_only_per_call_current_thread_never_startup_or_root_session(self):
        with tempfile.TemporaryDirectory() as d:
            p, env = self.fixture(d)
            for meta in (
                None,
                {},
                {"session_id": SID},
                {"x-codex-turn-metadata": {"session_id": SID}},
                {"threadId": SID, "x-codex-turn-metadata": {"thread_id": "other"}},
                {"threadId": True},
            ):
                self.assertEqual(M.native_origin_claims(env, "CODEX", meta), {})
            for meta in (
                {"threadId": SID},
                {"x-codex-turn-metadata": {"thread_id": SID, "session_id": "root"}},
            ):
                self.assertEqual(
                    M.native_origin_claims(env, "CODEX", meta)["origin_session_claim"], SID
                )
            # One process handles multiple chats. No first-call state may survive.
            self.assertEqual(
                M.native_origin_claims(
                    env, "CODEX", {"threadId": "019a1234-abcd-7123-8123-0123456789ac"}
                ),
                {},
            )
            self.assertEqual(M.native_origin_claims(env, "CODEX", {}), {})

    def test_bad_stale_cross_session_single_letter_generation_and_unsafe_paths_hold(self):
        with tempfile.TemporaryDirectory() as d:
            p, env = self.fixture(d)
            note = json.loads(p.read_text())
            for over in (
                dict(session_id="other"),
                dict(platform="claude"),
                dict(state="closed"),
                dict(label="A"),
                dict(generation=True),
                dict(generation=0),
            ):
                p.write_text(json.dumps(dict(note, **over)))
                self.assertEqual(M.native_origin_claims(env, "CODEX", {"threadId": SID}), {})
            p.write_text(json.dumps(note))
            p.chmod(0o644)
            self.assertEqual(M.native_origin_claims(env, "CODEX", {"threadId": SID}), {})
            p.chmod(0o600)
            p.rename(p.with_suffix(".bak"))
            p.symlink_to(p.with_suffix(".bak"))
            self.assertEqual(M.native_origin_claims(env, "CODEX", {"threadId": SID}), {})
            p.unlink()
            p.with_suffix(".bak").rename(p)
            folder = p.parent
            folder.rename(folder.with_name("other"))
            folder.symlink_to(folder.with_name("other"), target_is_directory=True)
            self.assertEqual(M.native_origin_claims(env, "CODEX", {"threadId": SID}), {})

    def test_actual_hosted_name_attaches_only_derived_claims_and_other_tools_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p, env = self.fixture(d)
            sent = []

            class Response(io.BytesIO):
                status = 200
                headers = {"Content-Type": "application/json"}

            def opener(req, **kw):
                sent.append(json.loads(req.data))
                return Response(b'{"jsonrpc":"2.0","id":7,"result":{"content":[]}}')

            adapter = M.Adapter("sbt_codex_" + "a" * 43, opener, env=env)
            message = dict(
                jsonrpc="2.0",
                id=7,
                method="tools/call",
                params=dict(
                    name="gr_open_pr",
                    _meta={"threadId": SID},
                    arguments=dict(
                        repo="example/x", origin_session_claim="wrong", origin_token="forbidden"
                    ),
                ),
            )
            adapter.call(message)
            a = sent[-1]["params"]["arguments"]
            self.assertEqual(a["origin_session_claim"], SID)
            self.assertNotIn("origin_token", a)
            p.unlink()
            adapter.call(message)
            self.assertFalse(any(k.startswith("origin_") for k in sent[-1]["params"]["arguments"]))
            message["params"]["name"] = "gr_wf_pr_get"
            adapter.call(message)
            self.assertEqual(sent[-1]["params"]["arguments"]["origin_session_claim"], "wrong")




class CallShapeProbe(unittest.TestCase):
    def arm(self, d, identity, *, size=0, mode=0o600):
        folder = Path(d) / ".simple-tools" / identity
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        (Path(d) / ".simple-tools").chmod(0o700)
        marker = folder / "origin-call-shape.once"
        marker.write_bytes(b"x" * size)
        marker.chmod(mode)
        return marker

    def emit(self, d, identity, params):
        out = io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stderr(out):
            M.call_shape_once({"HOME": d}, identity, params)
        return out.getvalue()

    def test_armed_reports_shape_once_without_values_or_unknown_names(self):
        with tempfile.TemporaryDirectory() as d:
            marker = self.arm(d, "AG")
            params = {
                "name": "gr_open_pr",
                "_meta": {
                    "threadId": "thread-secret-value",
                    "custom-context": "another-secret",
                    "other": 1,
                },
            }
            line = self.emit(d, "AG", params)
            marker_gone = not marker.exists()
            self.assertTrue(marker_gone)
            shape = json.loads(line)
            self.assertEqual(shape["diagnostic"], "origin-call-shape/1")
            self.assertEqual(shape["tool"], "gr_open_pr")
            self.assertEqual(shape["meta_type"], "object")
            self.assertEqual(shape["known_keys"]["threadId"], "string")
            self.assertEqual(
                shape["known_keys"]["com.simplewithus/native-origin/1"], "missing-or-null"
            )
            self.assertEqual(shape["other_key_count"], 2)
            self.assertNotIn("thread-secret-value", line)
            self.assertNotIn("another-secret", line)
            self.assertNotIn("custom-context", line)
            self.assertNotIn("other", line.replace("other_key_count", ""))
            self.assertEqual(self.emit(d, "AG", params), "")

    def test_missing_meta_still_reports_absence(self):
        with tempfile.TemporaryDirectory() as d:
            self.arm(d, "MC")
            shape = json.loads(self.emit(d, "MC", {"name": "identities"}))
            self.assertEqual(shape["meta_type"], "missing-or-null")
            self.assertEqual(shape["other_key_count"], 0)
            self.assertTrue(
                all(v == "missing-or-null" for v in shape["known_keys"].values())
            )

    def test_unarmed_or_unsafe_marker_never_emits(self):
        with tempfile.TemporaryDirectory() as d:
            params = {"name": "identities", "_meta": {"threadId": "x"}}
            self.assertEqual(self.emit(d, "AG", params), "")
            self.arm(d, "AG", size=3)
            self.assertEqual(self.emit(d, "AG", params), "")
            marker = self.arm(d, "MC")
            marker.chmod(0o644)
            self.assertEqual(self.emit(d, "MC", params), "")

    def test_unsafe_identity_never_emits_or_walks(self):
        with tempfile.TemporaryDirectory() as d:
            self.arm(d, "AG")
            for identity in ("../AG", "ag", "A G", ""):
                self.assertEqual(self.emit(d, identity, {"name": "identities"}), "")
            self.assertTrue((Path(d) / ".simple-tools/AG/origin-call-shape.once").exists())

class CallShapeProbeHardening(unittest.TestCase):
    def emit(self, d, identity, params):
        out = io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stderr(out):
            M.call_shape_once({"HOME": d}, identity, params)
        return out.getvalue()

    def arm(self, d, identity):
        folder = Path(d) / ".simple-tools" / identity
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        (Path(d) / ".simple-tools").chmod(0o700)
        marker = folder / "origin-call-shape.once"
        marker.write_bytes(b"")
        marker.chmod(0o600)
        return marker

    def test_caller_controlled_tool_name_never_emitted(self):
        with tempfile.TemporaryDirectory() as d:
            self.arm(d, "AG")
            secret = "bearer-token-value-9f8e7d"
            huge = "x" * (1024 * 1024)
            params = {
                "name": "prefix " + secret + " " + huge,
                "_meta": {"threadId": "t"},
            }
            line = self.emit(d, "AG", params)
            shape = json.loads(line)
            self.assertEqual(shape["tool"], "other")
            self.assertNotIn(secret, line)
            self.assertLess(len(line), 1024)

    def test_allowlisted_tool_labels_report_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            for tool in M.PROBE_TOOL_LABELS:
                with self.subTest(tool=tool):
                    self.arm(d, "AG")
                    shape = json.loads(self.emit(d, "AG", {"name": tool}))
                    self.assertEqual(shape["tool"], tool)

    def test_repeated_calls_leave_descriptor_count_stable(self):
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.is_dir():
            self.skipTest("needs /proc/self/fd")
        with tempfile.TemporaryDirectory() as d:
            before = len(list(fd_dir.iterdir()))
            for _ in range(50):
                self.emit(d, "AG", {"name": "identities"})  # unarmed
                self.emit(d, "../AG", {"name": "identities"})  # unsafe
                self.emit(d, "AG", None)
            self.arm(d, "AG")
            self.emit(d, "AG", {"name": "identities"})  # armed, consumes
            for _ in range(50):
                self.emit(d, "AG", {"name": "identities"})  # consumed
            self.assertEqual(len(list(fd_dir.iterdir())), before)

    def test_one_armed_generation_emits_at_most_once_across_threads(self):
        import threading

        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as d:
            self.arm(d, "AG")
            barrier = threading.Barrier(8)
            out = io.StringIO()

            def race():
                barrier.wait(5)
                M.call_shape_once({"HOME": d}, "AG", {"name": "gr_open_pr", "_meta": {}})

            with redirect_stderr(out):
                threads = [threading.Thread(target=race) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
            lines = [line for line in out.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
