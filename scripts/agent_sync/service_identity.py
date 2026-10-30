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


#: The Infisical folder that holds the wake runtimes' credentials (Fleet-OPS, the same project as the
#: Operator's own three names), and the one value the OpenRouter wake runtime reads from it.  The key
#: is the Operator's own (owner named it, Oct 30 2026), never another app's OpenRouter key.
WAKE_INFISICAL_PATH = "/wake"
OPENROUTER_KEY_NAME = "OPERATOR_OPENROUTER_API_KEY"
#: A key is one printable ASCII token:  no space, no control character, bounded.  Anything else is a
#: broken vault entry (a pasted line, a JSON blob), refused rather than sent in a header.
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,512}")


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
        creds = replace(
            creds, source=f"infisical Fleet-OPS prod {OPERATOR_INFISICAL_PATH} {names[1]}"
        )
        if creds.email.casefold() != expected_email:
            raise Z.CredentialError(f"{where}: not the pinned service bot")
        return creds
    except Z.CredentialError:
        raise
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise Z.CredentialError(f"{where}: {type(exc).__name__}") from None


def openrouter_api_key(env, home=None, fetch=None):
    """The Operator's OpenRouter key for the headless wake's HTTP runtime, as one string.

    Read the way `operator_credentials` reads the Operator's own three names:  one named value from
    Fleet-OPS (`INFISICAL_FLEET_OPS_PROJECT_ID`), folder `/wake`, through the installed fleet-secrets
    reader, so it gets that reader's in-memory cache, its outage memory and its last-known-good
    fallback.  No local file, no other app's OpenRouter key and no other folder.  Every failure is a
    `CredentialError` carrying a reason token and the name of the secret, never a value, and the key
    is returned to the caller and to nothing else:  the caller hides it from every log and scan."""
    from . import zulip as Z

    home_path = Path(home) if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
    # The project id is the one the daemon's other Fleet-OPS reads use:  the environment variable, else
    # the first line of ~/.agent-sync/fleet-ops-project-id (a shell that runs `test-wake --run` has the
    # file even when it lacks the variable the LaunchAgent carries).  A malformed or missing id fails
    # closed with a message that names both places and never echoes the value.
    project = env.get(PROJECT_ENV, "").strip()
    if not project:
        project = Z.infisical_project_id(env, home_path)
    where = "OpenRouter key: Fleet-OPS {} {}; no fallback".format(
        WAKE_INFISICAL_PATH, OPENROUTER_KEY_NAME
    )
    if not Z.infisical_enabled(env):
        raise Z.CredentialError(f"cannot read {where}: AGENT_SYNC_INFISICAL turns Infisical off")
    names = (OPENROUTER_KEY_NAME,)
    try:
        if fetch is not None:
            values = fetch(names)
        else:
            child = dict(env)
            child[Z.ENV_INFISICAL_PROJECT] = project  # Fleet-OPS, where /wake lives
            try:
                values = Z._default_infisical_fetch(names, child, home_path, WAKE_INFISICAL_PATH)
            except Z.CredentialError as exc:  # a reason token, never a value
                raise Z.CredentialError(f"{where}: {exc}") from None
        value = values.get(OPENROUTER_KEY_NAME, "")
        value = Z._strip_quotes(value) if isinstance(value, str) else ""
        if not value:
            raise Z.CredentialError(f"{where}: not set")
        if re.fullmatch(r"\$\{[^}]+\}", value):
            raise Z.CredentialError(f"{where}: unresolved reference")
        if not _KEY_SHAPE.fullmatch(value):
            raise Z.CredentialError(f"{where}: not a single printable token")
        return value
    except Z.CredentialError:
        raise
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise Z.CredentialError(f"{where}: {type(exc).__name__}") from None
