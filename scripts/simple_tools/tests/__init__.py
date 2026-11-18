"""Tests for simple_tools (unittest only, fake Zulip server on 127.0.0.1, no live network)."""

from simple_tools import heartbeat as _heartbeat
from simple_tools import zulip as _zulip


def _no_live_infisical(names, *_args):  # pragma: no cover - a guard
    """Infisical /zulip (OPS-818, AFC-359) must never reach the real fleet-secrets from a test.  A test that
    exercises it patches `zulip._default_infisical_fetch` (see Harness.infisical_values)."""
    raise _zulip.CredentialError("infisical disabled in tests")


def _no_live_infisical_names(*_args):  # pragma: no cover - a guard
    raise _zulip.CredentialError("infisical disabled in tests")


def _no_live_dsn(*_args):  # pragma: no cover - a guard
    return ""


_zulip._default_infisical_fetch = _no_live_infisical
_zulip._default_infisical_names = _no_live_infisical_names
_heartbeat._default_dsn_fetch = _no_live_dsn


def _no_live_fleetlink_token(*_args):  # pragma: no cover - a guard
    return None


from simple_tools.mcp import fleetlink_tools as _fleetlink

_fleetlink._default_token_fetch = _no_live_fleetlink_token
