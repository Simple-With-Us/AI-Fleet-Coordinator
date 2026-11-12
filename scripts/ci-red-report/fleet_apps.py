"""Load fleet repository names from fleet-apps.json (stdlib only)."""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT_FROM_PACKAGE = Path(__file__).resolve().parents[2]
DEFAULT_FLEET_APPS = REPO_ROOT_FROM_PACKAGE / "fleet-apps.json"
ENV_FLEET_APPS_JSON = "FLEET_APPS_JSON"


class FleetAppsError(ValueError):
    pass


def fleet_apps_path(env: dict[str, str] | None = None) -> Path:
    if env is None:
        env = os.environ
    override = env.get(ENV_FLEET_APPS_JSON, "").strip()
    if override:
        return Path(override)
    return DEFAULT_FLEET_APPS


def load_fleet_repo_names(
    *, path: Path | None = None, env: dict[str, str] | None = None
) -> list[str]:
    """Return de-duplicated `repo` values from fleet-apps.json apps list."""
    apps_path = path if path is not None else fleet_apps_path(env)
    try:
        raw = apps_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetAppsError(f"cannot read {apps_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FleetAppsError(f"invalid JSON in {apps_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FleetAppsError(f"{apps_path}: root must be an object")
    apps = data.get("apps")
    if not isinstance(apps, list):
        raise FleetAppsError(f"{apps_path}: missing apps array")
    names: list[str] = []
    seen: set[str] = set()
    for row in apps:
        if not isinstance(row, dict):
            continue
        repo = row.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            continue
        name = repo.strip()
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        raise FleetAppsError(f"{apps_path}: no repo names in apps")
    return names


def missing_fleet_token_visibility_note(*, scanning_repo: str) -> str:
    return (
        f"Scanning only {scanning_repo}; repository secret FLEET_READ_TOKEN is not set on "
        "Simple-With-Us/AI-Fleet-Coordinator. "
        "Add it to enable scanning every repo listed in fleet-apps.json."
    )


def fleet_token_visibility_note() -> str:
    return "FLEET_READ_TOKEN is set; scanning every non-archived repo listed in fleet-apps.json."
