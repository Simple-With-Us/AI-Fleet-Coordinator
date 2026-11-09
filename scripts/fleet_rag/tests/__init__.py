"""Unit tests for fleet_rag (unittest, no network).

    cd scripts && python3 -m unittest fleet_rag.tests -v

Two shell suites live beside these and are run on their own (they drive whole scripts
against a throwaway $HOME, and reach no network either):

    cd scripts && bash fleet_rag/tests/test_installer.sh     # scripts/install-fleet-rag.sh
    cd scripts && bash fleet_rag/tests/test_cloud_setup.sh   # scripts/cloud-setup.sh
"""
from __future__ import annotations

import pathlib
import unittest


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    here = pathlib.Path(__file__).parent
    top = here.parent.parent
    tests.addTests(loader.discover(str(here), pattern="test_*.py", top_level_dir=str(top)))
    return tests
