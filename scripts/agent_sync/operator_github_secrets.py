"""Named-secret adapter.  Secrets resolve through the fleet-secrets CLI only.

Owner decision 2026-10-21:  no process-environment secrets, no .env files and
no direct Infisical HTTP in fleet components.  The CLI holds its own
independently scoped identity;  deployment must verify that identity is denied
outside the approved secret paths (/operator-github/callers, /github-apps).
"""

import os
import re
import subprocess
from pathlib import Path

from .github_token_router import RoutingError, secret_names
from .zulip import fleet_secrets_bin

LOOKUP_TIMEOUT_SECONDS = 10


def fleet_secrets_get(path, name):
    """Read one named secret via `fleet-secrets get NAME --path PATH --project fleet-ops`;
    errors name the variable, never a value or backend detail.  Every local failure
    happens before any provider request, so the broker releases the caller nonce:
    "refused" for a missing or rejecting store, "retryable" for a timeout."""
    # An explicit FLEET_SECRETS_BIN override must survive a failing Path.home().
    try:
        binary = fleet_secrets_bin(os.environ, Path.home())
    except (OSError, RuntimeError):
        explicit = (os.environ.get("FLEET_SECRETS_BIN") or "").strip()
        binary = os.path.expanduser(explicit) if explicit else None
    if not binary:
        raise RoutingError(
            name + " unavailable: fleet-secrets executable not found", outcome="refused"
        )
    try:
        result = subprocess.run(
            [binary, "get", name, "--path", path, "--project", "fleet-ops"],
            capture_output=True,
            timeout=LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RoutingError(
            name + " unavailable: fleet-secrets lookup timed out", outcome="retryable"
        ) from None
    except FileNotFoundError:
        raise RoutingError(
            name + " unavailable: fleet-secrets executable not found", outcome="refused"
        ) from None
    except OSError:
        raise RoutingError(
            name + " unavailable: fleet-secrets lookup failed", outcome="refused"
        ) from None
    if result.returncode:
        raise RoutingError(name + " unavailable: fleet-secrets lookup failed", outcome="refused")
    value = result.stdout.decode("utf-8", "replace").strip()
    if not value:
        raise RoutingError(
            name + " unavailable: fleet-secrets returned an empty value", outcome="refused"
        )
    return value


class NamedSecrets:
    """Allowlisted name-to-value lookup for caller keys and App credentials."""

    def __init__(self, seats, apps, *, transport=None):
        self.seats = frozenset(seats)
        self.apps = frozenset(apps)
        self.transport = transport or fleet_secrets_get

    def caller_key(self, seat):
        if seat not in self.seats or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", seat):
            return None
        return self.transport(
            "/operator-github/callers", "OPERATOR_GITHUB_CALLER_" + seat + "_HMAC"
        )

    def app_credentials(self, slug):
        if slug not in self.apps:
            raise RoutingError("App not authorized")
        return tuple(self.transport("/github-apps", name) for name in secret_names(slug))
