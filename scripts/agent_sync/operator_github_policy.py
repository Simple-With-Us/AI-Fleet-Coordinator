"""Server-side seat allowlist for the Operator GitHub broker (OPS-966).

This module is the trusted policy lookup the broker's `policy_for`, `key_for`
and App-credential loader bind to.  The table is code-reviewed configuration:
adding a seat, a repository, or a wider ceiling is a pull request, never a
runtime input.  Client input cannot select an App; the broker maps the
authenticated caller seat through this table only.

Pilot scope (proposed in OPERATOR-GITHUB-REVIEW.md, exact repos and ceilings
still owner-confirmed): CODEX and ECHO, AI-Fleet-Coordinator only, read-only
ceilings matching the reviewed client command surface.  Widening is a code
change here.  Nothing in this module starts a server or enables the broker.
"""
from __future__ import annotations

import re

from .github_token_router import RoutingError, target_parts
from .operator_github import CallerPolicy

#: seat -> (app slug, {owner/repo: {permission: ceiling}}).  Exact names only;
#: no wildcards, no inferred seats, no PAT fallback.
_READ_PILOT = {'contents': 'read', 'pull-requests': 'read', 'metadata': 'read'}

POLICY = {
    'CODEX': ('swu-codex', {
        'Simple-With-Us/AI-Fleet-Coordinator': dict(_READ_PILOT),
    }),
    'ECHO': ('swu-echo', {
        'Simple-With-Us/AI-Fleet-Coordinator': dict(_READ_PILOT),
    }),
}

_CEILING_KEYS = frozenset({'contents', 'pull-requests', 'issues', 'metadata'})
_CEILING_VALUES = frozenset({'read', 'write'})


def _validate(policy):
    seats = set()
    apps = set()
    for seat, (slug, repos) in policy.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_-]{0,31}', seat):
            raise RoutingError('Invalid seat in policy')
        if not re.fullmatch(r'swu-[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            raise RoutingError('Invalid App slug in policy')
        if not isinstance(repos, dict) or not repos:
            raise RoutingError('Policy seat needs explicit repositories')
        for repo, ceilings in repos.items():
            target_parts(repo)
            if not isinstance(ceilings, dict) or not ceilings:
                raise RoutingError('Policy repository needs an explicit ceiling')
            if not set(ceilings) <= _CEILING_KEYS:
                raise RoutingError('Policy ceiling names a permission outside the reviewed set')
            if not set(ceilings.values()) <= _CEILING_VALUES:
                raise RoutingError('Policy ceiling value must be read or write')
            # The router adds metadata: read to every request (GitHub grants it
            # implicitly), so every ceiling must cover it explicitly.
            if ceilings.get('metadata') != 'read':
                raise RoutingError('Policy ceiling must include metadata: read')
        seats.add(seat)
        apps.add(slug)
    return frozenset(seats), frozenset(apps)


SEATS, APPS = _validate(POLICY)


def load_policy(seat):
    """Trusted server-side lookup: the caller's policy, or None when the seat is
    not in the allowlist (the broker then refuses with 403)."""
    entry = POLICY.get(seat)
    if entry is None:
        return None
    slug, repos = entry
    return CallerPolicy(app_slug=slug, repositories={r.casefold(): c for r, c in repos.items()})


def caller_key_loader(secrets_adapter):
    """Bind a NamedSecrets adapter to the allowlisted caller names only."""
    allowed = SEATS

    def key_for(seat):
        if seat not in allowed:
            return None
        return secrets_adapter.caller_key(seat)

    return key_for


def app_credentials_loader(secrets_adapter):
    """Bind a NamedSecrets adapter to the App slugs named in the policy only."""
    allowed = APPS

    def credentials_for(slug):
        if slug not in allowed:
            raise RoutingError('App not authorized')
        return secrets_adapter.app_credentials(slug)

    return credentials_for
