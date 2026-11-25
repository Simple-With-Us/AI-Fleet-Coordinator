"""Inactive collaborator custody preparation. No dispatch or live policy registration.

This module does not grant App permissions. An integration must authenticate the
seat, authorize the ordinary action/repository, and obtain its exact grants from
protected server policy. Never construct grants from a caller's request.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from .app_tokens import AppTokenError

ACTIONS = frozenset({"wf_collaborator_set", "wf_collaborator_remove"})
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!.*\.\.)[A-Za-z0-9_.-]{1,100}")
_SEAT = re.compile(r"swu-[a-z0-9-]{1,90}")


class CustodyCleanupPending(RuntimeError):
    """Revocation is unverified. Never replay an operation after this result."""


class CustodyDenied(ValueError):
    """Fixed refusal without upstream text or credentials."""


@dataclass(frozen=True)
class RepoAdminGrant:
    """A protected-policy grant. Equality is exact except repository case."""

    seat: str
    repo: str
    actions: frozenset[str]


def parse_grants(seat, value, configured_repos):
    """Parse an optional protected-policy field, never request arguments.

    Empty/absent denies every operation. Wildcards in the ordinary repository
    scope do not imply typed grants. Every grant repository must also be listed
    explicitly in that ordinary scope. No permission map or credential is accepted.
    """
    if not isinstance(seat, str) or not _SEAT.fullmatch(seat):
        raise CustodyDenied("invalid custody seat")
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 100:
        raise CustodyDenied("invalid custody grants")
    if not isinstance(configured_repos, (list, tuple, set, frozenset)) or any(
        not isinstance(repo, str) for repo in configured_repos
    ):
        raise CustodyDenied("invalid custody repository scope")
    explicit = {repo.casefold() for repo in configured_repos if _REPO.fullmatch(repo)}
    grants, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"repo", "actions"}:
            raise CustodyDenied("invalid custody grant")
        repo, actions = item["repo"], item["actions"]
        if (
            not isinstance(repo, str)
            or not _REPO.fullmatch(repo)
            or repo.casefold() not in explicit
            or repo.casefold() in seen
            or not isinstance(actions, list)
            or not actions
            or len(actions) > len(ACTIONS)
            or any(not isinstance(action, str) or action not in ACTIONS for action in actions)
            or len(actions) != len(set(actions))
        ):
            raise CustodyDenied("invalid custody grant")
        seen.add(repo.casefold())
        grants.append(RepoAdminGrant(seat, repo.casefold(), frozenset(actions)))
    return tuple(grants)


def authorize(grants, seat, app_slug, action, repo):
    """Additional gate only; this does not replace ordinary server authorization."""
    if (
        not isinstance(seat, str)
        or not _SEAT.fullmatch(seat)
        or app_slug != seat
        or not isinstance(action, str)
        or action not in ACTIONS
        or not isinstance(repo, str)
        or not _REPO.fullmatch(repo)
        or not isinstance(grants, tuple)
        or not any(
            isinstance(grant, RepoAdminGrant)
            and grant.seat == seat
            and grant.repo == repo.casefold()
            and action in grant.actions
            for grant in grants
        )
    ):
        raise CustodyDenied("collaborator custody not granted")


@contextmanager
def installation_lease(minter, grants, seat, app_slug, action, repo):
    """One uncached own-App repository token, revocation attempted on every exit path.

    Preparation only. No current route calls this function. The fixed permission
    set is deliberately not added to token_for's generic mint ceiling. A caller
    never supplies a token, permission map, account path or installation id.
    """
    authorize(grants, seat, app_slug, action, repo)
    try:
        app_id, pem = minter._app_credentials(app_slug)
        try:
            bearer = minter._signer(app_id, pem, int(minter._clock()))
        finally:
            del pem
    except Exception:
        raise AppTokenError("collaborator App credential unavailable") from None

    def api(method, path, auth, body=None):
        try:
            return minter._api(method, path, auth, body)
        except Exception:
            raise AppTokenError("collaborator custody upstream unavailable") from None

    issued = None
    try:
        app = api("GET", "/app", bearer)
        if (
            not isinstance(app, dict)
            or type(app_id) is not int
            or app_id <= 0
            or type(app.get("id")) is not int
            or app["id"] != app_id
            or app.get("slug") != app_slug
        ):
            raise AppTokenError("own App identity unverifiable")
        owner, name = repo.split("/", 1)
        installation = api(
            "GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/installation", bearer
        )
        account = installation.get("account") if isinstance(installation, dict) else None
        permissions = installation.get("permissions") if isinstance(installation, dict) else None
        if (
            not isinstance(installation, dict)
            or type(installation.get("id")) is not int
            or not 1 <= installation["id"] <= 9007199254740991
            or type(installation.get("app_id")) is not int
            or installation["app_id"] != app_id
            or installation.get("app_slug") != app_slug
            or "suspended_at" not in installation
            or installation["suspended_at"] is not None
            or not isinstance(account, dict)
            or not isinstance(account.get("login"), str)
            or account["login"].casefold() != owner.casefold()
            or not isinstance(permissions, dict)
            or permissions.get("administration") != "write"
        ):
            raise AppTokenError("collaborator installation unverifiable")
        try:
            # Mint through the owning module: app_tokens is the one path that
            # may reach the installation-token endpoint, so custody asks for
            # the issuance instead of addressing the endpoint itself.
            minted = minter._mint_installation_token(
                installation["id"],
                bearer,
                [name],
                {"metadata": "read", "administration": "write"},
            )
        except Exception:
            raise AppTokenError("collaborator custody upstream unavailable") from None
        # Capture before checking the rest so a malformed token response can
        # still be revoked. No token enters the generic minter cache.
        candidate = minted.get("token") if isinstance(minted, dict) else None
        if not isinstance(candidate, str) or not candidate:
            raise AppTokenError("collaborator mint unverifiable")
        issued = candidate
        if (
            not isinstance(minted.get("permissions"), dict)
            or minted["permissions"] != {"metadata": "read", "administration": "write"}
            or not isinstance(minted.get("repositories"), list)
            or len(minted["repositories"]) != 1
            or not isinstance(minted["repositories"][0], dict)
            or not isinstance(minted["repositories"][0].get("full_name"), str)
            or minted["repositories"][0]["full_name"].casefold() != repo.casefold()
        ):
            raise AppTokenError("collaborator mint scope unverifiable")
        try:
            expires_at = datetime.fromisoformat(minted.get("expires_at", "").replace("Z", "+00:00"))
            expiry = expires_at.timestamp() if expires_at.tzinfo is not None else 0
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise AppTokenError("collaborator mint expiry unverifiable") from None
        # Read the clock once: two reads could straddle an expiry boundary and
        # accept a mint that no single instant accepts.
        now = minter._clock()
        if not now < expiry <= now + 3600:
            raise AppTokenError("collaborator mint expiry unverifiable")
        yield issued
    finally:
        del bearer
        if issued is not None:
            try:
                minter._api("DELETE", "/installation/token", issued)
            except Exception:
                raise CustodyCleanupPending(
                    "collaborator token cleanup unverified; do not replay"
                ) from None
