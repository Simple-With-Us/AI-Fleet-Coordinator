import importlib.util
import json
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location("release", "scripts/deploy-alert-workers.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)

# A second alert target for the tests that prove one target cannot block another.
# alert-ingress is the only real one since pr-conflict-watch retired on 2026-11-06.
OTHER = {
    "deployer": "alert-workers-deploy",
    "root": "workers/other/",
    "config": "workers/other/wrangler.toml",
    "paths": ["workers/other/src/**"],
    "ignore": ["*.test.*"],
    "health": "https://other.invalid/health",
    "ok": True,
}


def two_targets():
    return patch.dict(m.TARGETS)  {"other-worker": OTHER})
