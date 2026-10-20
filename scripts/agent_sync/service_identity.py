"""Explicit deterministic service identities, never general agent roster entries."""

import os
from dataclasses import replace
from pathlib import Path

OPERATOR = "OPERATOR"
OPERATOR_BOT_ID = 1217148
OPERATOR_EMAIL = "operator-bot@simplewithus.zulipchat.com"
# The Notes service reads AI Fleet Coordinator prod /zulip, not Fleet-OPS /zulip_operator (the
# provisioning copy), so the reader is pinned to this project rather than resolving one from the
# ambient environment or ~/.agent-sync/fleet-ops-project-id.
AFC_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
# Seats the partition may list that are not agent roster seats:  no reader queue, and never an
# eligible wake sender or a `route.seat` target (config.fleet_tags keeps them out).
SERVICE_SEATS = frozenset({OPERATOR})


def operator_bot(user):
    return (
        user.get("is_bot") is True
        and user.get("user_id") == OPERATOR_BOT_ID
        and str(user.get("email", "")).casefold() == OPERATOR_EMAIL
    )


def operator_credentials(env, home=None, fetch=None):
    """Read only the two named Operator values, through the installed fleet-secrets reader (the
    environment first, then `fleet-secrets run --only`); no local credential-file fallback."""
    from . import zulip as Z

    names = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
    where = "OPERATOR service: AFC prod /zulip keys %s; no fallback" % ", ".join(names)
    try:
        if fetch is not None:
            values = fetch(names)
        else:
            child = dict(env)
            child["INFISICAL_FLEET_OPS_PROJECT_ID"] = AFC_PROJECT  # the Notes-service store, pinned
            try:
                values = Z._default_infisical_fetch(
                    names,
                    child,
                    home if home is not None else Path(env.get("HOME") or os.path.expanduser("~")),
                )
            except Z.CredentialError as exc:  # a reason token, never a value
                raise Z.CredentialError("%s: %s" % (where, exc)) from None
        selected = {n: values.get(n, "") for n in names}
        selected["OPERATOR_SITE"] = "https://simplewithus.zulipchat.com"
        creds = Z.env_credentials(
            selected, email_env=names[0], key_env=names[1], site_env="OPERATOR_SITE", label=OPERATOR
        )
        # The values came from fleet-secrets, not the ambient environment:  report that provenance.
        creds = replace(creds, source="infisical AFC prod %s %s" % (Z.INFISICAL_PATH, names[1]))
        if creds.email.casefold() != OPERATOR_EMAIL:
            raise Z.CredentialError("%s: not the pinned service bot" % where)
        return creds
    except Z.CredentialError:
        raise
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise Z.CredentialError("%s: %s" % (where, type(exc).__name__)) from None
