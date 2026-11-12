"""Unit tests for fleet_lanes (unittest, no network, no subprocess).

    cd scripts && python3 -m unittest fleet_lanes.tests -v

Importing this package clears the fleet roots (FLEET_CODE_ROOT, FLEET_APPS_ROOT, FLEET_LANES_ROOT, ...) from the process
environment, see hermetic.py:  on a server host they name the live folders.
"""
from __future__ import annotations

import pathlib
import unittest

from . import hermetic  # noqa: F401  (the import clears the fleet roots for every test in this process)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    here = pathlib.Path(__file__).parent
    top = here.parent.parent
    tests.addTests(loader.discover(str(here), pattern="test_*.py", top_level_dir=str(top)))
    return tests
