"""No private inventory and no effects from an unqualified proof adapter."""

import ast
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "ct_proof_test_target", Path(__file__).with_name("wise_runtime_proof.py")
)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class OwnWiseProofTests(unittest.TestCase):
    def test_unqualified_adapter_never_starts_transport(self):
        run = Mock(side_effect=AssertionError("transport must not start"))
        for hashes in ({}, {"code/patch_secretscan.py": "a" * 64}):
            self.assertIs(proof.prove(hashes, run), False)
        run.assert_not_called()
        self.assertFalse(proof.QUALIFIED)
        self.assertIs(proof.make_proof()(None, {}), False)

    def test_no_embedded_route_or_secret_loader(self):
        source = Path(proof.__file__).read_text()
        ast.parse(source)
        for marker in (
            "REMOTE_SOURCE",
            "known_hosts",
            "simple-tools-bearer",
            "/Users/",
            "fleet-secrets get",
            "MAC_HOST",
        ):
            self.assertNotIn(marker, source)

    def test_timeout_kills_and_reaps_entire_transport_group(self):
        process = Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1), (b"", b"")]
        with (
            patch.object(proof.subprocess, "Popen", return_value=process),
            patch.object(proof.os, "killpg") as kill,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                proof.run_bounded(["fixture"], input=b"", capture_output=True, timeout=1, env={})
        kill.assert_called_once_with(123, proof.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
