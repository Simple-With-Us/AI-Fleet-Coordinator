"""Named-secret adapter.  Secrets resolve through the consumer's own scoped token only.

Owner decision 2026-10-21:  no process-environment secrets, no dotenv files and
no direct Infisical HTTP in fleet components.  The reader holds its own
independently scoped identity;  deployment must verify that identity is denied
outside the approved secret paths (/operator-github/callers, /github-apps).

Each folder has its own read-only service token, held in the consumer's own
store (the owner's login keychain on the Mac, a private file on a server) and
read through the shared Infisical client, which checks the token's own grant
and the answer's own provenance before a value is used.  There is no shared
reader, no machine identity and no cached copy:  a missing token, a locked
keychain or a refused grant yields nothing, and the refusal says which.
"""

import os
import re

from .github_token_router import RoutingError, secret_names

LOOKUP_TIMEOUT_SECONDS = 10
# One read-only grant per folder, each named for the folder it covers.
TOKEN_ACCOUNTS = {
    "/operator-github/callers": "operator-github-callers-ro",
    "/github-apps": "operator-github-apps-ro",
}


def scoped_secret_get(path, name):
    """Read one named secret through the consumer's own scoped token.

    Errors name the variable, never a value or a backend detail.  Every local failure
    happens before any provider request, so the broker releases the caller nonce:
    "refused" for a missing or rejecting store, "retryable" for a timeout.

    The shared client lets a socket timeout through as a `TimeoutError`,  so the timeout
    branch below is the live path for a hung lookup,  not a defensive leftover."""
    account = TOKEN_ACCOUNTS.get(path)
    if not account:
        raise RoutingError(name + " unavailable: no scoped token covers " + path, outcome="refused")
    try:
        import fleet_infisical as FI

        env = dict(os.environ)
        env[FI.ENV_READ_PATHS] = path
        value, status = FI.fetch_scoped(name, path, account, env, timeout=LOOKUP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise RoutingError(
            name + " unavailable: Infisical lookup timed out", outcome="retryable"
        ) from None
    except Exception:
        raise RoutingError(
            name + " unavailable: Infisical lookup failed", outcome="refused"
        ) from None
    if status != "ok":
        if status == "keychain-timeout":
            raise RoutingError(
                name + " unavailable: the scoped token store did not answer in time",
                outcome="retryable",
            )
        raise RoutingError(
            name + " unavailable: Infisical lookup failed (" + status + ")", outcome="refused"
        )
    if not value:
        raise RoutingError(
            name + " unavailable: Infisical returned an empty value", outcome="refused"
        )
    return value


class NamedSecrets:
    """Allowlisted name-to-value lookup for caller keys and App credentials."""

    def __init__(self, seats, apps, *, transport=None):
        self.seats = frozenset(seats)
        self.apps = frozenset(apps)
        self.transport = transport or scoped_secret_get

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
