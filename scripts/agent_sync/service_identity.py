"""Explicit deterministic service identities, never general agent roster entries."""

import os
from dataclasses import replace
from pathlib import Path

OPERATOR = "OPERATOR"
PROJECT_ENV = "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID"


def operator_identity(env):
    from . import zulip as Z

    try:
        bot_id = int(env.get("OPERATOR_ZULIP_USER_ID", ""))
        email = env.get("OPERATOR_ZULIP_EMAIL", "").strip().casefold()
        if bot_id <= 0 or "@" not in email:
            raise ValueError()
        return bot_id, email
    except (ValueError, TypeError):
        raise Z.CredentialError("Set OPERATOR_ZULIP_USER_ID and OPERATOR_ZULIP_EMAIL") from None


# Seats the partition may list that are not agent roster seats:  no reader queue, and never an
# eligible wake sender or a `route.seat` target (config.fleet_tags keeps them out).
SERVICE_SEATS = frozenset({OPERATOR})


def operator_bot(user, env, owner_id):
    bot_id, email = operator_identity(env)
    return (
        isinstance(owner_id, int)
        and owner_id > 0
        and user.get("bot_owner_id") == owner_id
        and user.get("is_bot") is True
        and user.get("user_id") == bot_id
        and str(user.get("email", "")).casefold() == email
    )


def operator_credentials(env, home=None, fetch=None):
    """Read only the two named Operator values, through the installed fleet-secrets reader (the
    environment first, then `fleet-secrets run --only`); no local credential-file fallback."""
    from . import zulip as Z

    _, expected_email = operator_identity(env)
    project = env.get(PROJECT_ENV, "").strip()
    if not project:
        raise Z.CredentialError("Set " + PROJECT_ENV)
    names = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
    where = "OPERATOR service: configured Notes credential store keys %s; no fallback" % ", ".join(
        names
    )
    if not Z.infisical_enabled(env):
        raise Z.CredentialError("cannot read %s: AGENT_SYNC_INFISICAL turns Infisical off" % where)
    try:
        if fetch is not None:
            values = fetch(names)
        else:
            child = dict(env)
            child[Z.ENV_INFISICAL_PROJECT] = project  # the Notes-service store, pinned
            try:
                values = Z._default_infisical_fetch(
                    names,
                    child,
                    home if home is not None else Path(env.get("HOME") or os.path.expanduser("~")),
                )
            except Z.CredentialError as exc:  # a reason token, never a value
                raise Z.CredentialError("%s: %s" % (where, exc)) from None
        selected = {n: values.get(n, "") for n in names}
        selected["OPERATOR_SITE"] = env.get("ZULIP_SITE", "")
        creds = Z.env_credentials(
            selected, email_env=names[0], key_env=names[1], site_env="OPERATOR_SITE", label=OPERATOR
        )
        # The values came from fleet-secrets, not the ambient environment:  report that provenance.
        creds = replace(creds, source="infisical AFC prod %s %s" % (Z.INFISICAL_PATH, names[1]))
        if creds.email.casefold() != expected_email:
            raise Z.CredentialError("%s: not the pinned service bot" % where)
        return creds
    except Z.CredentialError:
        raise
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise Z.CredentialError("%s: %s" % (where, type(exc).__name__)) from None
