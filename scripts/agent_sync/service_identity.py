"""Explicit deterministic service identities, never general agent roster entries."""

OPERATOR = "OPERATOR"
OPERATOR_BOT_ID = 1217148
OPERATOR_EMAIL = "operator-bot@simplewithus.zulipchat.com"
AFC_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"


def operator_bot(user):
    return (
        user.get("is_bot") is True
        and user.get("user_id") == OPERATOR_BOT_ID
        and str(user.get("email", "")).casefold() == OPERATOR_EMAIL
    )


def operator_credentials(env, home=None, fetch=None):
    """Read only named Operator values; no local credential-file fallback."""
    from . import zulip as Z

    names = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
    try:
        if fetch is None:
            from fleet_secrets import core

            child = dict(env)
            child["INFISICAL_FLEET_OPS_PROJECT_ID"] = AFC_PROJECT
            values = core.fetch_all("/zulip", child)
            core.reject_unresolved(values, names)
        else:
            values = fetch(names)
        selected = {n: values.get(n, "") for n in names}
        selected["OPERATOR_SITE"] = "https://simplewithus.zulipchat.com"
        creds = Z.env_credentials(
            selected, email_env=names[0], key_env=names[1], site_env="OPERATOR_SITE", label=OPERATOR
        )
        if creds.email.casefold() != OPERATOR_EMAIL:
            raise ValueError()
        return creds
    except Exception:
        raise Z.CredentialError(
            "OPERATOR service credential unavailable in AFC prod /zulip; no fallback"
        ) from None
