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


if __name__ == "__main__":
    unittest.main()
