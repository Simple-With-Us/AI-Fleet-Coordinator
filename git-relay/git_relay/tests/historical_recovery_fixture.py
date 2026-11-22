"""Explicit test-only historical source, never a production builder fallback."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "finite-recovery-before-freeze-diagnostics-v1"


def historical_source_root(test, builder):
    expected = json.loads((FIXTURE / "SHA256.json").read_text())
    test.assertEqual(builder.MODULES, expected)
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    package = root / "git-relay" / "git_relay"
    package.mkdir(parents=True)
    for name, digest in expected.items():
        raw = (FIXTURE / (name + ".txt")).read_bytes()
        test.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        (package / name).write_bytes(raw)
    # Existing exact historical workflow image is input to mocked image gates.
    # No evolving current code is selected or imported into the fixture.
    shutil.copyfile(
        Path(__file__).parent / "fixtures" / "workflow-1549.py.txt",
        package / "workflow_verbs.py",
    )
    return root
