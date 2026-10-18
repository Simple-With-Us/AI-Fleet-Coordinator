"""Tests for agent_sync (unittest only, fake Zulip server on 127.0.0.1, no live network)."""
from agent_sync import zulip as _zulip


def _no_live_infisical(names):  # pragma: no cover - a guard
    """The Infisical credential fallback (OPS-818) must never reach the real CLI from a test.  A test that
    exercises it patches `zulip._default_infisical_fetch` (see Harness.infisical_values)."""
    raise _zulip.CredentialError("infisical disabled in tests")


_zulip._default_infisical_fetch = _no_live_infisical
