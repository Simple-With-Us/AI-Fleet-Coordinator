"""Compatibility alias for the one shared direct Infisical command implementation."""

import sys

import fleet_infisical_cli as _cli

sys.modules[__name__] = _cli
