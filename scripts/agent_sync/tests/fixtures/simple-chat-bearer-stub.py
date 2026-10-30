"""Run scripts/simple-chat with the hosted gateway stubbed, for subprocess tests only.

BEARER_STUB_MODE (ok, revoked, unavailable, other-seat) picks the gateway's answer;  everything else
is the real CLI and the real bearer gate.
"""

import os
import runpy
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS))
from agent_sync.mcp import stdio as S  # noqa: E402
import importlib.util  # noqa: E402

# Loaded by path:  importing agent_sync.tests would switch on the test-only guards of that package.
_spec = importlib.util.spec_from_file_location("bearer_stub", Path(__file__).resolve().parents[1] / "bearer_stub.py")
bearer_stub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bearer_stub)

mode = os.environ.get("BEARER_STUB_MODE", "ok")
S._bearer_verify = bearer_stub.stub_verify(lambda: mode)
script = str(SCRIPTS / "simple-chat")
sys.argv = [script, *sys.argv[1:]]
runpy.run_path(script, run_name="__main__")
