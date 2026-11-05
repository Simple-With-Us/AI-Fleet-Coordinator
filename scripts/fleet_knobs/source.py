"""Fetch registered knob names with their dedicated scoped read binding.

The REST helper verifies grant metadata and exact project, prod environment, and folder before reading named rows.  Missing or malformed bindings fail closed, and results contain only requested knob names.  The caller retains the last-known-good cache when a read fails.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from types import ModuleType

PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
ENVIRONMENT = "prod"
CLI_NAME = "infisical"
# A LaunchAgent's PATH is minimal, so look in the usual install places after PATH.
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
# Bound one metadata-plus-read operation; the negative cache spares sibling processes a repeat wait.
TIMEOUT_SECONDS = 8.0
ENV_NAMES = ("INFISICAL_ENVIRONMENT", "INFISICAL_ENV")
# ``infisical secrets get NAME`` exits 0 and answers a name it does not hold with this value
# (verified against the real CLI, OPS-818), so it means "absent", never a secret's value.
NOT_FOUND_PLACEHOLDERS = frozenset({"*not found*"})
# A value that is still a whole bare Infisical reference such as ${prod.some-folder.NAME}: the
# Fleet-OPS root is stored that way (OPS-818), and every read passes --expand=true so the server
# resolves it.  One that survives is an unresolved reference, never a credential.
UNRESOLVED_REFERENCE = re.compile(r"^\$\{[^}]+\}$")


ENV_TOKEN = "INFISICAL_TOKEN"
ENV_AI_FLEET_COORDINATOR_TOKEN = "INFISICAL_AI_FLEET_COORDINATOR_TOKEN"
ENV_KNOBS_TOKEN = "FLEET_KNOBS_INFISICAL_TOKEN"
# Legacy identity names are stripped from helper environments and never authenticate a request.
ENV_AUTOMATION_ID = "INFISICAL_AUTOMATION_CLIENT_ID"
ENV_AUTOMATION_SECRET = "INFISICAL_AUTOMATION_CLIENT_SECRET"
ENV_HELPER_HOME = "FLEET_SECRETS_HOME"
HELPER_HOME = "~/.local/share/fleet-secrets"
HELPER_MODULE = "_fleet_knobs_fleet_secrets_core"


class FetchFailed(Exception):
    """The fetch did not produce a usable answer.  `reason` is a short token, never output."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def environment_refusal() -> str | None:
    """Why Infisical may not be read (a non-prod slug in the environment), or None."""
    for var in ENV_NAMES:
        slug = (os.environ.get(var) or "").strip().lower()
        if slug and slug != ENVIRONMENT:
            return (
                f"refusing Infisical environment {slug!r} from {var}:  prod is the only "
                "environment (dev and staging are retired);  unset it or set it to prod;  "
                "see INFISICAL.md"
            )
    return None


def find_cli() -> str | None:
    found = shutil.which(CLI_NAME)
    if found:
        return found
    for directory in FALLBACK_DIRS:
        candidate = os.path.join(os.path.expanduser(directory), CLI_NAME)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def argv_for(cli: str) -> list:
    return [
        cli,
        "export",
        f"--env={ENVIRONMENT}",
        f"--projectId={PROJECT_ID}",
        "--format=json",
        "--path=/",
        "--silent",
        "--expand=true",
        "--include-imports=false",
    ]


def fetch(names: Iterable[str]) -> dict[str, str]:
    """The raw string value of each of `names` that Infisical holds (absent names are left out).

    Raises FetchFailed with a reason token.  Never logs or returns anything but the
    requested names."""
    return _scoped_fetch(names, PROJECT_ID, "/", named=True)


def argv_named(cli: str, names: Iterable[str], project_id: str, path: str) -> list:
    return [
        cli,
        "secrets",
        "get",
        *sorted(set(names)),
        f"--env={ENVIRONMENT}",
        f"--projectId={project_id}",
        f"--path={path}",
        "--output=json",
        "--silent",
        "--expand=true",
        "--include-imports=false",
    ]


def fetch_named(names: Iterable[str], *, project_id: str, path: str) -> dict[str, str]:
    """Only named secrets of one folder, checked against the caller's expected project.

    The verifier checks metadata before per-name REST requests, so unrelated folder values
    never enter this process.  Absent names are left out.  Prod only; failures are reduced to
    reason tokens and provider output is never printed."""
    return _scoped_fetch(names, project_id, path, named=True)


def _scoped_fetch(
    names: Iterable[str], project_id: str, path: str, *, named: bool
) -> dict[str, str]:
    refusal = environment_refusal()
    if refusal:
        raise FetchFailed("environment-refused")
    helper = HELPER_LOADER()
    if helper is None:
        raise FetchFailed("scoped-reader-unavailable")
    wanted = set(names)
    env = dict(os.environ)
    token = (env.get(ENV_KNOBS_TOKEN) or "").strip()
    if (
        not token.startswith("st.")
        or not 20 <= len(token) <= 8192
        or any(c.isspace() for c in token)
    ):
        raise FetchFailed("service-token-required")
    env["INFISICAL_AI_FLEET_COORDINATOR_TOKEN"] = token
    env.pop(ENV_TOKEN, None)
    env.pop("INFISICAL_WRITE_TOKEN", None)
    env.pop(ENV_KNOBS_TOKEN, None)
    try:
        result = helper.fetch_project(
            project_id,
            path,
            env,
            token=token,
            names=wanted if named else None,
            timeout=TIMEOUT_SECONDS,
        )
    except TimeoutError:
        raise FetchFailed("timeout") from None
    except Exception:
        raise FetchFailed("scoped-read-failed") from None
    return _pick(result, wanted)


def _helper_candidates() -> list:
    """Where fleet-secrets' ``core.py`` may be, in order.  Paths only;  nothing is imported."""
    out = []
    home = (os.environ.get(ENV_HELPER_HOME) or "").strip()
    if home:
        out.append(os.path.join(os.path.expanduser(home), "fleet_secrets", "core.py"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out.append(os.path.join(here, "fleet_secrets", "core.py"))
    out.append(os.path.join(os.path.expanduser(HELPER_HOME), "fleet_secrets", "core.py"))
    return out


def _load_helper() -> ModuleType | None:
    """fleet-secrets' core module, loaded from a file under a private name, or None."""
    cached = _HELPER_CACHE.get("core")
    if cached is not None:
        return cached
    for candidate in _helper_candidates():
        if not os.path.isfile(candidate):
            continue
        try:
            spec = importlib.util.spec_from_file_location(HELPER_MODULE, candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - optional helper
            continue
        if all(hasattr(module, n) for n in ("fetch_project", "SecretsError")):
            _HELPER_CACHE["core"] = module
            return module
    return None


_HELPER_CACHE: dict = {}
# Tests replace this with a callable that returns a fake helper, or None for "no helper".
HELPER_LOADER: Callable[[], ModuleType | None] = _load_helper


def _without_automation(env: Mapping[str, str]) -> dict[str, str]:
    """Remove retired universal-auth credentials from the scoped-read environment."""
    out = dict(env)
    out.pop(ENV_AUTOMATION_ID, None)
    out.pop(ENV_AUTOMATION_SECRET, None)
    return out


def child_env() -> dict[str, str]:
    """Return an isolated child environment with the dedicated knob reader binding."""
    env = dict(os.environ)
    token = (env.get(ENV_KNOBS_TOKEN) or "").strip()
    if (
        not token.startswith("st.")
        or not 20 <= len(token) <= 8192
        or any(c.isspace() for c in token)
    ):
        raise FetchFailed("service-token-required")
    env["INFISICAL_AI_FLEET_COORDINATOR_TOKEN"] = token
    env.pop(ENV_TOKEN, None)
    env.pop("INFISICAL_WRITE_TOKEN", None)
    env.pop(ENV_KNOBS_TOKEN, None)
    return _without_automation(env)


def _pick(payload, wanted) -> dict[str, str]:
    """Accept the shapes ``export --format=json`` has used:  {KEY: value}, a list of
    {key, value} or {secretKey, secretValue} objects, or an API-style {"secrets": [...]}."""
    if isinstance(payload, dict) and isinstance(payload.get("secrets"), list):
        payload = payload["secrets"]
    found: dict[str, str] = {}
    if isinstance(payload, dict):
        pairs = payload.items()
    elif isinstance(payload, list):
        pairs = []
        for item in payload:
            if not isinstance(item, dict):
                raise FetchFailed("bad-output")
            key = item.get("key", item.get("secretKey"))
            val = item.get("value", item.get("secretValue"))
            pairs.append((key, val))
    else:
        raise FetchFailed("bad-output")
    for key, val in pairs:
        if key in wanted and isinstance(val, (str, int, float, bool)):
            if isinstance(val, str) and val.strip().lower() in NOT_FOUND_PLACEHOLDERS:
                continue
            text = str(val)
            if isinstance(val, bool):
                text = "true" if val else "false"
            if UNRESOLVED_REFERENCE.match(text.strip()):
                # A bare ${prod.folder.NAME} that expansion left alone (target missing, or the
                # identity cannot read the referenced folder).  Fail closed on a reason token;
                # the key and the reference text are never carried (OPS-818).
                raise FetchFailed("unresolved-reference")
            found[key] = text
    return found
