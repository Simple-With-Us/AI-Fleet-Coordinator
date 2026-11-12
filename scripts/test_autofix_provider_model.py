#!/usr/bin/env python3
"""Tests for autofix provider route and model resolution."""

from __future__ import annotations

import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "autofix_provider_model.sh")


def sh(*args: str) -> str:
    body = subprocess.check_output(["bash", "-c", f"source {SCRIPT}; {args[0]}"], text=True)
    return body.strip()


class AutofixProviderModelTests(unittest.TestCase):
    def test_trim_base_url(self) -> None:
        self.assertEqual(sh('autofix_route_for_base "  https://api.meta.ai  "'), "meta")

    def test_meta_muse_model_gains_prefix(self) -> None:
        resolved = sh(
            "autofix_resolve_model_for_route meta muse-spark-1.3-contributor MiniMax-M3.1-Flash"
        )
        self.assertEqual(resolved, "meta/muse-spark-1.3-contributor")

    def test_meta_prefixed_model_unchanged(self) -> None:
        resolved = sh(
            "autofix_resolve_model_for_route meta meta/muse-spark-1.3-contributor MiniMax-M3.1-Flash"
        )
        self.assertEqual(resolved, "meta/muse-spark-1.3-contributor")

    def test_minimax_route_leaves_muse_model(self) -> None:
        resolved = sh(
            "autofix_resolve_model_for_route minimax muse-spark-1.3-contributor MiniMax-M3.1-Flash"
        )
        self.assertEqual(resolved, "muse-spark-1.3-contributor")


if __name__ == "__main__":
    unittest.main()
