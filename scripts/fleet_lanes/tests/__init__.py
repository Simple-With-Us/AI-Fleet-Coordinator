"""Unit tests for fleet_lanes (unittest, no network, no subprocess).

    cd scripts && python3 -m unittest fleet_lanes.tests -v
"""
from __future__ import annotations

import pathlib
import unittest


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    here = pathlib.Path(__file__).parent
    top = here.parent.parent
    tests.addTests(loader.discover(str(here), pattern="test_*.py", top_level_dir=str(top)))
    return tests
