"""Explicit deterministic service identities, never general agent roster entries."""

import os
import re
from dataclasses import replace
from pathlib import Path

OPERATOR = "OPERATOR"
#: The Operator's own three names live in the Fleet-OPS project (the approved
#: credential source, OPS-818 rule 8) in /zulip_operator - verified present there
#: 2026-10-21.  An earlier revision pinned this read to the AFC project ("the
#: Notes-service store"), where the names do not exist, so the read kept failing
#: (Kodus on #791).  The project comes from the same INFISICAL_FLEET_OPS_PROJECT_ID
#: setting every other fleet credential read uses (zulip.ENV_INFISICAL_PROJECT).
PROJECT_ENV = "INFISICAL_FLEET_OPS_PROJECT_ID"
#: The Infisical folder that holds the Operator's own three names.  The seat keys
#: are in /zulip; the Operator keeps its own folder, so the read has to name the
#: folder or the names are not there to be found.
OPERATOR_INFISICAL_PATH = "/zulip_operator"


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
    where = "OPERATOR service: Fleet-OPS {} keys {}; no fallback".format(
        OPERATOR_INFISICAL_PATH, ", ".join(names)
    )
    if not Z.infisical_enabled(env):
        raise Z.CredentialError(f"cannot read {where}: AGENT_SYNC_INFISICAL turns Infisical off")
    try:
        if fetch is not None:
            values = fetch(names)
        else:
            child = dict(env)
            child[Z.ENV_INFISICAL_PROJECT] = project  # Fleet-OPS, where /zulip_operator lives
            try:
                values = Z._default_infisical_fetch(
                    names,
                    child,
                    home if home is not None else Path(env.get("HOME") or os.path.expanduser("~")),
                    OPERATOR_INFISICAL_PATH,
                )
            except Z.CredentialError as exc:  # a reason token, never a value
                raise Z.CredentialError(f"{where}: {exc}") from None
        selected = {n: values.get(n, "") for n in names}
        unresolved = [
            n
            for n, value in selected.items()
            if isinstance(value, str) and re.fullmatch(r"\$\{[^}]+\}", value.strip())
        ]
        if unresolved:
            raise Z.CredentialError("Operator unresolved reference: " + ", ".join(unresolved))
        selected["ZULIP_SITE"] = env.get("ZULIP_SITE", "")
        creds = Z.env_credentials(
            selected, email_env=names[0], key_env=names[1], site_env="ZULIP_SITE", label=OPERATOR
        )
        # The values came from fleet-secrets, not the ambient environment:  report that provenance.
        creds = replace(creds, source=f"infisical Fleet-OPS prod {OPERATOR_INFISICAL_PATH} {names[1]}")
        if creds.email.casefold() != expected_email:
            raise Z.CredentialError(f"{where}: not the pinned service bot")
        return creds
    except Z.CredentialError:
        raise
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise Z.CredentialError(f"{where}: {type(exc).__name__}") from None
