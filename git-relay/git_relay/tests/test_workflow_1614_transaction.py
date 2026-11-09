import ast
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "deploy" / "workflow-1614"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("workflow1614_builder", "build.py")
proof = load("workflow1614_proof", "runtime_proof.py")


class BuilderTests(unittest.TestCase):
    def test_baseline_drift_refuses(self):
        with self.assertRaisesRegex(ValueError, "baseline drift"):
            builder.image(b"changed", b"candidate")

    def test_candidate_drift_refuses(self):
        with (
            patch.object(builder, "digest", side_effect=[builder.BEFORE, "wrong"]),
            self.assertRaisesRegex(ValueError, "candidate drift"),
        ):
            builder.image(b"before", b"candidate")

    def test_image_parses_syntax(self):
        with (
            patch.object(
                builder, "digest", side_effect=[builder.BEFORE, builder.AFTER]
            ),
            self.assertRaises(SyntaxError),
        ):
            builder.image(b"before", b"def broken(")

    def test_candidate_gate_accepts_only_pinned_bytes(self):
        with patch.object(
            builder, "digest", side_effect=[builder.BEFORE, builder.AFTER]
        ):
            self.assertEqual(builder.image(b"before", b"x = 1"), b"x = 1")

    def test_render_binds_exact_manifest_and_modules(self):
        raw = b"manifest"
        sources = {"git_relay/a.py": b"content"}
        entry = builder.render_entry(
            (HERE / "runner.template.py").read_text(), raw, sources
        )
        self.assertNotIn("PLACEHOLDER", entry)
        self.assertIn(builder.digest(raw), entry)
        self.assertIn(builder.digest(b"content"), entry)
        self.assertIn(builder.ROOT, entry)

    def test_render_refuses_missing_or_repeated_placeholders(self):
        template = (HERE / "runner.template.py").read_text()
        for changed in [
            template.replace("MANIFEST_DIGEST_PLACEHOLDER", "missing"),
            template + "\n# MANIFEST_DIGEST_PLACEHOLDER",
        ]:
            with self.assertRaises(ValueError):
                builder.render_entry(changed, b"manifest", {"a": b"content"})

    def test_build_has_only_one_exact_file_and_fixed_bindings(self):
        root = Path(__file__).resolve().parents[3]
        # The historical fixture is the exact live baseline. The builder also
        # requires the isolated merged 1614 candidate, never evolving main.
        historical = Path(__file__).parent / "fixtures" / "workflow-1549.py.txt"
        self.assertEqual(builder.digest(historical.read_bytes()), builder.BEFORE)
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before"
            before.write_bytes(b"before")
            output = Path(directory) / "new"
            with patch.object(builder, "image", return_value=b"candidate"):
                receipt = builder.build(root, before, output)
            manifest = json.loads((output / "manifest.json").read_bytes())
            self.assertEqual(
                manifest["files"],
                [
                    {
                        "root": "code",
                        "name": "workflow_verbs.py",
                        "before": builder.BEFORE,
                        "after": builder.AFTER,
                        "meta": {"uid": 0, "gid": 0, "mode": 420},
                    }
                ],
            )
            self.assertEqual(manifest["runner"], builder.ROOT + "/runner.py")
            self.assertEqual(manifest["unit"], builder.UNIT)
            self.assertEqual(manifest["config"], builder.CONFIG)
            self.assertEqual(
                receipt["manifest"],
                builder.digest((output / "manifest.json").read_bytes()),
            )
            self.assertFalse((output / "1.candidate").exists())
            with (
                self.assertRaises(FileExistsError),
                patch.object(builder, "image", return_value=b"candidate"),
            ):
                builder.build(root, before, output)

    def test_wrong_directory_binding_refuses(self):
        template = (HERE / "runner.template.py").read_text()
        for changed in [
            template.replace(builder.ROOT, "/wrong"),
            template.replace("DIRECTORY = Path(", "DIRECTORY = str("),
        ]:
            with self.assertRaises(ValueError):
                builder.render_entry(changed, b"manifest", {"a": b"content"})

    def test_files_explicit_mode_under_permissive_or_strict_umask(self):
        for mask in (0, 0o777):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "artifact"
                old = os.umask(mask)
                try:
                    builder.write_file(target, b"source")
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                self.assertEqual(target.read_bytes(), b"source")
                with self.assertRaises(FileExistsError):
                    builder.write_file(target, b"overwrite")

    def test_module_open_is_nonblocking_and_no_follow(self):
        tree = ast.parse((HERE / "runner.template.py").read_text())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "open"
        ]
        self.assertEqual(len(calls), 1)
        flags = ast.unparse(calls[0].args[1])
        self.assertIn("O_NONBLOCK", flags)
        self.assertIn("O_NOFOLLOW", flags)


class ProofTests(unittest.TestCase):
    def test_proof_pin(self):
        self.assertEqual(
            builder.digest((HERE / "runtime_proof.py").read_bytes()), builder.PROOF
        )

    def test_exact_inventory_no_write_calls_and_candidate_labels(self):
        for digest in [proof.RuntimeProof.BASELINE, proof.RuntimeProof.CANDIDATE]:
            p = proof.RuntimeProof("test-only")
            p.selector = lambda identity: True
            calls = []

            def call(action, args, calls=calls, repo=p.REPO):
                calls.append((action, args))
                if action == "wf_repo_get":
                    return 200, {
                        "repository": {"full_name": repo, "default_branch": "main"}
                    }
                return 200, {"issues": [], "complete": False}

            p.call = call
            self.assertTrue(p({"pid": 1}, {"code/workflow_verbs.py": digest}))
            self.assertEqual([x[0] for x in calls], ["wf_repo_get", "wf_issue_list"])
            self.assertEqual("labels" in calls[1][1], digest == p.CANDIDATE)
            if digest == p.CANDIDATE:
                self.assertEqual(len(calls[1][1]["labels"].split(",")), 10)
            self.assertFalse(p({}, {"code/workflow_verbs.py": "wrong"}))
            self.assertEqual(len(calls), 2)

    def test_selector_and_bad_provider_refuse_before_followup(self):
        p = proof.RuntimeProof("test-only")
        p.selector = lambda identity: False
        p.call = lambda *args: self.fail("unexpected provider call")
        self.assertFalse(p({}, {"code/workflow_verbs.py": p.CANDIDATE}))
        p.selector = lambda identity: True
        p.call = lambda *args: (200, {"repository": {"full_name": "wrong"}})
        self.assertFalse(p({}, {"code/workflow_verbs.py": p.CANDIDATE}))

    def test_selector_exact_file_policy_and_physical_binding(self):
        p = proof.RuntimeProof("test-only")
        env = b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json\0"
        for raw, same, digest, allowed in [
            (env, True, p.POLICY, True),
            (env, False, p.POLICY, False),
            (env, True, "wrong", False),
            (b"GIT_RELAY_SEATS_JSON={}\0", True, p.POLICY, False),
            (env + b"GIT_RELAY_SEATS_JSON={}\0", True, p.POLICY, False),
        ]:
            with (
                patch.object(proof.Path, "read_bytes", side_effect=[raw, b"policy"]),
                patch.object(proof.os.path, "samefile", return_value=same),
                patch.object(proof.hashlib, "sha256") as digest_mock,
            ):
                digest_mock.return_value.hexdigest.return_value = digest
                self.assertEqual(p.selector({"pid": 1}), allowed)

    def test_issue_provider_error_and_malformed_output_refuse(self):
        for response in [(401, {}), (200, {}), (200, {"issues": [], "complete": True})]:
            p = proof.RuntimeProof("test-only")
            p.selector = lambda identity: True
            replies = iter(
                [
                    (
                        200,
                        {"repository": {"full_name": p.REPO, "default_branch": "main"}},
                    ),
                    response,
                ]
            )
            p.call = lambda *args, replies=replies: next(replies)
            self.assertFalse(p({}, {"code/workflow_verbs.py": p.CANDIDATE}))

    def test_transport_json_status_and_size_bounds(self):
        class Response(io.BytesIO):
            status = 200

        class Opener:
            def __init__(self, raw):
                self.raw = raw

            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response(self.raw)

        for raw, expected in [
            (
                b'{"issues": [], "complete": false}',
                (200, {"issues": [], "complete": False}),
            ),
            (b"x" * 65537, (None, None)),
            (b"not json", (None, None)),
            (b"\\xff", (None, None)),
        ]:
            opener = Opener(raw)
            p = proof.RuntimeProof("test-only", opener)
            self.assertEqual(p.call("wf_issue_list", {"repo": p.REPO}), expected)
            self.assertEqual(opener.timeout, 15)
            self.assertEqual(opener.request.full_url, p.URL + "wf_issue_list")

    def test_no_redirects(self):
        self.assertIsNone(
            proof.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "http://other"
            )
        )

    def test_proof_pin_tamper_refuses_build(self):
        root = Path(__file__).resolve().parents[3]
        before = Path(__file__).parent / "fixtures" / "workflow-1549.py.txt"
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(builder, "PROOF", "wrong"),
            self.assertRaisesRegex(ValueError, "runtime proof module drift"),
        ):
            builder.build(root, before, Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
