import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "wise_proof", Path(__file__).with_name("wise_runtime_proof.py")
)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class OwnWiseProofTests(unittest.TestCase):
    def response(self, **changes):
        result = dict(returncode=0, stderr=b"", stdout=json.dumps(proof.EXPECTED).encode())
        result.update(changes)
        return SimpleNamespace(**result)

    def test_no_process_for_unqualified_image(self):
        def bad(*args, **kwargs):
            self.fail("must not run")

        for hashes in ({}, {"code/patch_secretscan.py": "0" * 64}):
            self.assertFalse(proof.prove(hashes, bad))

    def test_fixed_command_no_credential_transfer(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            key_option = next(s for s in argv if s.startswith("UserKnownHostsFile="))
            path = Path(key_option.split("=", 1)[1])
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(path.read_text(), proof.MAC_HOST + " " + proof.MAC_HOST_KEY + "\n")
            self.assertEqual(kwargs["input"], proof.REMOTE_SOURCE.encode())
            self.assertEqual(kwargs["timeout"], 35)
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("BatchMode=yes", argv)
            self.assertEqual(argv[-2:], ["jay@" + proof.MAC_HOST, "python3 -"])
            return self.response()

        for digest in (proof.BEFORE, proof.AFTER):
            self.assertTrue(proof.prove({"code/patch_secretscan.py": digest}, run))
        self.assertEqual(len(calls), 2)

    def test_unexpected_transport_output_refuses(self):
        for result in (
            self.response(returncode=1),
            self.response(stderr=b"warning"),
            self.response(stdout=b"not json"),
            self.response(stdout=b"x" * 513),
            self.response(stdout=b'{"ok":true,"seat":"ECHO"}'),
            self.response(stdout=b'{"ok":false}'),
        ):
            self.assertFalse(
                proof.prove({"code/patch_secretscan.py": proof.AFTER}, lambda *a, **k: result)
            )

    def test_timeout_and_exception_refuse(self):
        for error in (OSError("failure"), subprocess.TimeoutExpired("fixed", 35)):

            def run(*a, **k):
                raise error

            self.assertFalse(proof.prove({"code/patch_secretscan.py": proof.AFTER}, run))

    def test_remote_source_is_syntax_valid_and_null_fenced(self):
        compile(proof.REMOTE_SOURCE, "fixed-remote-proof", "exec")
        self.assertIn("'expected_head_sha':'0'*40", proof.REMOTE_SOURCE)
        self.assertIn("'base_blob_sha':'0'*40", proof.REMOTE_SOURCE)
        self.assertNotIn("ECHO", proof.REMOTE_SOURCE)
        self.assertNotIn("CODEX", proof.REMOTE_SOURCE)
        self.assertNotIn("originLock", proof.REMOTE_SOURCE)
        self.assertNotIn("content_b64", proof.REMOTE_SOURCE)


if __name__ == "__main__":
    unittest.main()
