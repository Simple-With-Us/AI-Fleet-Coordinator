#!/usr/bin/env python3
"""The Senate relay reads its secret through the shared scoped reader, with the
account named.

The relay lives outside this repository,  so it cannot use the shared client as a
library:  it runs the one reader command every consumer outside this repository runs.
That reader takes the account,  and the account is what makes the read this
consumer's:  an ambient environment token must never satisfy it,  and a reader that
cannot answer must be named so the operator repairs the right component.

No real reader,  no real secret,  no network,  and the relay never starts.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_infisical as fi

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "senate-relay-runtime" / "run.sh"

# The reader the runtime must use,  as the shared installer places it.
READER_RELATIVE = "apps/infisical-scoped/infisical-scoped.py"


def synthetic_token() -> str:
    """A service-token-shaped value, made up and assembled here so no token-shaped literal sits."""
    return ".".join(["st", "synthetic", "notarealvalue", "notarealvalue"])


# A reader that records what it was asked and refuses the read.
FAKE_READER = """#!/usr/bin/env python3
import json, os, sys
record = os.environ.get("FAKE_READER_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)
sys.exit(int(os.environ.get("FAKE_READER_EXIT", "4")))
"""


class SenateRelayReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="senate-relay-reader-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.reader = self.home / READER_RELATIVE
        self.reader.parent.mkdir(parents=True)
        self.reader.write_text(FAKE_READER, encoding="utf-8")
        self.record = self.home / "asked.json"

    def run_launcher(self, **env_overrides: str) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "FAKE_READER_RECORD": str(self.record),
            "SENATE_RELAY_REQUIRE": "1",
        }
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(LAUNCHER)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_the_read_names_the_account_and_the_folder(self) -> None:
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.record.is_file(), "the scoped reader was never asked")
        asked = self.record.read_text(encoding="utf-8")
        self.assertIn('"get"', asked)
        self.assertIn('"SENATE_RELAY_SECRET"', asked)
        self.assertIn('"--folder"', asked)
        self.assertIn('"/senate-relay"', asked)
        self.assertIn('"--account"', asked)
        self.assertIn('"scout-runtime-ro"', asked)

    def test_an_ambient_token_never_satisfies_the_read(self) -> None:
        # A token in the environment is not this consumer's grant:  the read still
        # goes through the named account,  and it still fails when the store cannot
        # answer.
        ambient = {fi.ENV_TOKEN: synthetic_token()}
        result = self.run_launcher(**ambient)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.record.is_file(), "the scoped reader was never asked")

    def test_a_refused_read_names_the_reader_the_account_and_the_folder(self) -> None:
        result = self.run_launcher(FAKE_READER_EXIT="4")
        self.assertEqual(result.returncode, 1)
        self.assertIn("scout-runtime-ro", result.stderr)
        self.assertIn(str(self.reader), result.stderr)
        self.assertIn("/senate-relay", result.stderr)
        # The reason names what actually refused:  the grant or the store.
        self.assertIn("grant refused", result.stderr)

    def test_a_missing_name_says_so(self) -> None:
        result = self.run_launcher(FAKE_READER_EXIT="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in that folder", result.stderr)

    def test_a_missing_reader_is_named(self) -> None:
        self.reader.unlink()
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1)
        self.assertIn("scout-runtime-ro", result.stderr)
        self.assertIn(str(self.reader), result.stderr)

    def test_the_launcher_never_reads_through_the_ambient_reader(self) -> None:
        # The shared command without an account resolves its token from the
        # environment.  The runtime names its account instead,  so that command is
        # not on this path.
        text = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("fleet_infisical_cli.py", text)
        self.assertIn("--account", text)


if __name__ == "__main__":
    unittest.main()
