"""Compatibility import for the shared direct Infisical client.

New consumers import fleet_infisical directly.  This alias retains the existing
CLI and import contract without another reader implementation.
"""

import sys

import fleet_infisical as _client

_compat_name = __name__
globals().update(vars(_client))
sys.modules[_compat_name] = _client
