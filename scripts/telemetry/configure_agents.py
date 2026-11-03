"""Fetch telemetry producer settings through the scoped Infisical client."""

from __future__ import annotations

from collections.abc import Mapping

from fleet_secrets import core

TOKEN_ENV = "INFISICAL_TELEMETRY_CONFIG_READ_TOKEN"
PROJECT_ENV = "INFISICAL_PROJECT_ID"
PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"


def fetch_export(env: Mapping[str, str]) -> dict[str, str]:
    """Fetch the prod root with this use case's read-only token and exact grant."""
    configured_project = (env.get(PROJECT_ENV) or "").strip()
    token = (env.get(TOKEN_ENV) or "").strip()
    if configured_project and configured_project != PROJECT_ID:
        raise core.SecretsError("telemetry-project-id-mismatch")
    if not token:
        raise core.SecretsError(f"service-token-required: set {TOKEN_ENV}")
    token = core.check_service_token(token)

    helper_env = dict(env)
    helper_env[core.ENV_READ_PATHS] = "/"
    helper_env.setdefault("INFISICAL_ENVIRONMENT", "prod")
    helper_env.setdefault("INFISICAL_ENV", "prod")
    for name in core.AUTH_ENV_VARS | {TOKEN_ENV}:
        helper_env.pop(name, None)
    return core.fetch_project(PROJECT_ID, "/", helper_env, token=token)
