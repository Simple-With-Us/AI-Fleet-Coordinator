"""sparkle_public_key.swift derives the right public key, which make_appcast.sh compares with
the SUPublicEDKey built into the archive before it signs anything.

The vector is RFC 8032, section 7.1, test 1 (a published test key, not Muster's).  Swift exists
with CryptoKit only on the Mac, so the Ubuntu job skips this and the Muster macOS build job runs it.
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "sparkle_public_key.swift")
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


# CryptoKit is Apple's, so only a Mac can run the script, even where Linux has a swift toolchain
# (the Ubuntu runner does).
@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("swift"), "needs swift with CryptoKit (macOS)"
)
class SparklePublicKeyTests(unittest.TestCase):
    def run_with(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "key")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(base64.b64encode(data).decode() + "\n")
            return subprocess.run(
                ["swift", SCRIPT, path], capture_output=True, text=True, timeout=600
            )

    def test_seed_gives_its_public_key(self):
        result = self.run_with(SEED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), base64.b64encode(PUBLIC).decode())

    def test_older_96_byte_form_gives_its_last_32_bytes(self):
        result = self.run_with(bytes(64) + PUBLIC)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), base64.b64encode(PUBLIC).decode())

    def test_other_lengths_are_refused_without_printing_the_key(self):
        result = self.run_with(SEED[:31])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(base64.b64encode(SEED[:31]).decode(), result.stderr)


if __name__ == "__main__":
    unittest.main()
