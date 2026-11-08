import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "git-relay/deploy/provider-filename-1511"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class ProviderRollout(unittest.TestCase):
    def test_builder_refuses_unpinned_live_source(self):
        builder = module("provider_builder", SOURCE / "build.py")
        with self.assertRaisesRegex(ValueError, "baseline drift"):
            builder.overlay(b"# Not the reviewed deployed baseline\n")
        self.assertEqual(len(builder.MODULES), 8)
        self.assertNotIn("workflow_verbs.py", builder.MODULES)

    def test_proof_old_and_new_exact_refusals(self):
        proof = module("provider_runtime_proof", SOURCE / "runtime_proof.py")
        for new in [False, True]:

            class Fake(proof.RuntimeProof):
                def __init__(self):
                    self.calls = []

                def call(self, action, args, new=new):
                    self.calls.append((action, args))
                    if action == "wf_repo_get":
                        return 200, {
                            "repository": {"full_name": self.REPO, "default_branch": "main"}
                        }
                    self.assert_safe(args)
                    if args["message"].startswith("sk-proj-"):
                        return 400, {"error": "invalid patch message"}
                    return 400, {
                        "error": "default branch is not writable"
                        if new
                        else "invalid patch message"
                    }

                @staticmethod
                def assert_safe(args):
                    assert args["branch"] == "main"
                    assert args["path"] == "README.md"
                    assert args["expected_head_sha"] == "0" * 40
                    assert args["edits"] == [{"start": 0, "end": 0, "text": ""}]

            instance = Fake()
            self.assertTrue(instance({}, {"code/provider_scan.py": "new" if new else None}))
            self.assertEqual(len(instance.calls), 4)

    def test_proof_refuses_wrong_default_and_unexpected_success(self):
        proof = module("provider_runtime_proof", SOURCE / "runtime_proof.py")

        class WrongDefault(proof.RuntimeProof):
            def call(self, action, args):
                return 200, {"repository": {"full_name": self.REPO, "default_branch": "other"}}

        self.assertFalse(WrongDefault("synthetic")({}, {}))

        class Unexpected(proof.RuntimeProof):
            def call(self, action, args):
                if action == "wf_repo_get":
                    return 200, {"repository": {"full_name": self.REPO, "default_branch": "main"}}
                return 200, {"commit": "unexpected"}

        self.assertFalse(Unexpected("synthetic")({}, {"code/provider_scan.py": "new"}))

    def test_transport_rejects_malformed_and_large_receipts(self):
        proof = module("provider_runtime_proof", SOURCE / "runtime_proof.py")

        class Reply(io.BytesIO):
            status = 200

        class Opener:
            def __init__(self, data):
                self.data = data

            def open(self, request, timeout):
                return Reply(self.data)

        for raw in [b"not-json", b"x" * 65537]:
            self.assertEqual(
                proof.RuntimeProof("synthetic", Opener(raw)).call("wf_repo_get", {}), (None, None)
            )

    def test_overlay_exact_site_count_and_nonprovider_suffix(self):
        builder = module("provider_builder_overlay", SOURCE / "build.py")
        base = "from .secret_scan import find_secret\n"
        pattern = r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"
        raw = (base + "\n".join(f"P{n}={pattern!r}" for n in range(18))).encode()
        with patch.object(builder, "digest", side_effect=[builder.BEFORE, builder.AFTER]):
            result = builder.overlay(raw)
        self.assertEqual(result.count(b"=PROVIDER_PATTERN"), 18)
        self.assertIn(b"from .provider_scan import PROVIDER_PATTERN", result)
        for count in (17, 19):
            raw = (base + "\n".join(f"P{n}={pattern!r}" for n in range(count))).encode()
            with (
                patch.object(builder, "digest", return_value=builder.BEFORE),
                self.assertRaisesRegex(ValueError, "census drift"),
            ):
                builder.overlay(raw)
        raw = (base + "P=" + repr(pattern + "|unknown")).encode()
        with (
            patch.object(builder, "digest", return_value=builder.BEFORE),
            self.assertRaisesRegex(ValueError, "nonprovider suffix"),
        ):
            builder.overlay(raw)

    def test_malformed_repository_and_redirect_refuse(self):
        proof = module("provider_proof_malformed", SOURCE / "runtime_proof.py")
        self.assertIsNone(proof.NoRedirect().redirect_request(None))
        for malformed in [None, [], {"repository": None}, {"repository": []}]:

            class Fake(proof.RuntimeProof):
                def call(self, action, args, malformed=malformed):
                    return 200, malformed

            self.assertFalse(Fake("synthetic")({}, {}))

    def test_runner_placeholder_is_not_permanent_refusal(self):
        text = (SOURCE / "runner.template.py").read_text()
        self.assertIn("MANIFEST_DIGEST_PLACEHOLDER", text)
        self.assertNotIn('if MANIFEST_DIGEST=="e66', text)
