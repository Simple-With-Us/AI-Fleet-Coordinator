"""Named-secret adapter.  Secrets resolve through the fleet-secrets CLI only.

Owner decision 2026-10-21:  no process-environment secrets, no .env files and
no direct Infisical HTTP in fleet components.  The CLI holds its own
independently scoped identity;  deployment must verify that identity is denied
outside the approved secret paths (/operator-github/callers, /github-apps).
"""

import re
import subprocess

from .github_token_router import RoutingError, secret_names

LOOKUP_TIMEOUT_SECONDS = 10


def fleet_secrets_get(path, name):
    """Read one named secret via `fleet-secrets get`;  errors name the variable,
    never a value or backend detail.  The path argument documents the intended
    secret folder for reviewers and injected test transports;  the CLI resolves
    it from its own configuration."""
    try:
        result = subprocess.run(
            ["fleet-secrets", "get", name],
            capture_output=True,
            timeout=LOOKUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RoutingError(name + " unavailable: fleet-secrets lookup timed out") from None
    except FileNotFoundError:
        raise RoutingError(name + " unavailable: fleet-secrets executable not found") from None
    except OSError:
        raise RoutingError(name + " unavailable: fleet-secrets lookup failed") from None
    if result.returncode:
        raise RoutingError(name + " unavailable: fleet-secrets lookup failed")
    value = result.stdout.decode("utf-8", "replace").strip()
    if not value:
        raise RoutingError(name + " unavailable: fleet-secrets returned an empty value")
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
