"""Fleet repo list, local checkout, and read-only GitHub fetches."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cron_monitor_audit.errors import AuditError
from cron_monitor_audit.parse import slugify, workflow_display_name

_MAX_BYTES = 1_000_000
_CODE_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".yml", ".yaml"}


@dataclass(frozen=True)
class App:
    owner: str
    repo: str
    display: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class RepoSource:
    full_name: str
    readable: bool
    names_known: bool
    workflow_names: tuple[str, ...]
    files: tuple[tuple[str, str], ...]
    reason: str = ""


@dataclass(frozen=True)
class CodeHit:
    path: str
    fragment: str


@dataclass(frozen=True)
class CodeSearch:
    state: str
    detail: str
    hits: dict[str, tuple[CodeHit, ...]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_name(value: str) -> bool:
    if not value or len(value) > 100 or ".." in value:
        return False
    return all(char.isalnum() or char in "_.-" for char in value)


def load_apps(path: Path) -> list[App]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"could not read fleet app list {path}") from exc
    if not isinstance(payload, dict):
        raise AuditError("fleet app list was not an object")
    owner = payload.get("owner")
    apps = payload.get("apps")
    if not isinstance(owner, str) or not _github_name(owner):
        raise AuditError("fleet app list owner is invalid")
    if not isinstance(apps, list) or not apps:
        raise AuditError("fleet app list has no apps")
    rows: list[App] = []
    for app in apps:
        if not isinstance(app, dict):
            raise AuditError("fleet app list has an app that is not an object")
        repo = app.get("repo")
        display = app.get("displayName") or repo
        if not isinstance(repo, str) or not _github_name(repo):
            raise AuditError("fleet app list has an invalid repo name")
        if not isinstance(display, str):
            raise AuditError("fleet app list has an invalid display name")
        rows.append(App(owner, repo, display))
    return rows


def include_local_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    norm = norm.removeprefix("./")
    if norm.startswith("scripts/cron-monitor-audit/"):
        return False
    parts = norm.split("/")
    if "tests" in parts or "__pycache__" in parts:
        return False
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return False
    if norm.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
        return True
    return norm.startswith("scripts/") and Path(name).suffix in _CODE_SUFFIXES


def scan_checkout(root: Path, full_name: str) -> RepoSource:
    files: list[tuple[str, str]] = []
    names: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not include_local_path(relative):
            continue
        if path.stat().st_size > _MAX_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        files.append((relative, text))
        if relative.startswith(".github/workflows/"):
            display = workflow_display_name(text)
            if display:
                names.append(display)
    return RepoSource(
        full_name=full_name,
        readable=True,
        names_known=True,
        workflow_names=tuple(names),
        files=tuple(files),
    )


def github_collection_root(base: str) -> str:
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise AuditError("GitHub API base must be https")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def build_github_request(url: str, token: str, accept: str) -> Request:
    if not url.startswith("https://"):
        raise AuditError("refusing a GitHub request that is not https")
    if token and token in url:
        raise AuditError("refusing a GitHub request that puts the token in the URL")
    headers = {"Accept": accept, "User-Agent": "fleet-cron-monitor-audit"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, method="GET", headers=headers)


def fetch_repos(
    apps: list[App],
    token: str,
    base: str = "https://api.github.com",
    opener=None,
    skip: set[str] | None = None,
) -> tuple[list[RepoSource], str]:
    """Return one source per app and the token that still works.

    A 401 drops the token and retries that call without one. Later calls stay
    anonymous. The token is never copied into an error.
    """
    if opener is None:
        opener = build_opener(_NoRedirect)
    root = github_collection_root(base)
    current = token.strip()
    repos: list[RepoSource] = []
    skipped = skip or set()
    for app in apps:
        if app.full_name in skipped:
            continue
        try:
            source, current = _fetch_repo(app.full_name, current, root, opener)
        except AuditError as exc:
            source = RepoSource(
                full_name=app.full_name,
                readable=False,
                names_known=False,
                workflow_names=(),
                files=(),
                reason=str(exc),
            )
        repos.append(source)
    return repos, current


def search_code(
    slugs: list[str],
    org: str,
    token: str,
    base: str = "https://api.github.com",
    opener=None,
) -> CodeSearch:
    if not token.strip():
        return CodeSearch("unavailable", "no GitHub token", {})
    if not _github_name(org):
        raise AuditError("GitHub org is invalid")
    if opener is None:
        opener = build_opener(_NoRedirect)
    root = github_collection_root(base)
    hits: dict[str, tuple[CodeHit, ...]] = {}
    for slug in slugs:
        url = f"{root}/search/code?q={quote(slug)}+org:{quote(org)}&per_page=5"
        status, _, body = _read(opener, url, token, "application/vnd.github.text-match+json")
        if status in {401, 403, 422}:
            return CodeSearch("unavailable", f"HTTP {status}", hits)
        if status == 429:
            return CodeSearch("incomplete", "HTTP 429", hits)
        if status != 200:
            return CodeSearch("unavailable", f"HTTP {status}", hits)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return CodeSearch("unavailable", "not JSON", hits)
        found: list[CodeHit] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            fragments: list[str] = []
            for match in item.get("text_matches") or []:
                if isinstance(match, dict) and isinstance(match.get("fragment"), str):
                    fragments.append(match["fragment"])
            found.append(CodeHit(path, "\n".join(fragments)))
        hits[slug] = tuple(found)
    return CodeSearch("ok", "", hits)


def _fetch_repo(full_name: str, token: str, root: str, opener) -> tuple[RepoSource, str]:
    owner, _, repo = full_name.partition("/")
    if not _github_name(owner) or not _github_name(repo):
        raise AuditError("GitHub repo name is invalid")
    list_url = f"{root}/repos/{owner}/{repo}/actions/workflows?per_page=100"
    status, _, body = _read(opener, list_url, token, "application/vnd.github+json")
    if status == 401 and token:
        token = ""
        status, _, body = _read(opener, list_url, token, "application/vnd.github+json")
    if status != 200:
        raise AuditError(f"GitHub workflows for {full_name} returned HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AuditError(f"GitHub workflows for {full_name} was not JSON") from None
    workflows = payload.get("workflows")
    if not isinstance(workflows, list):
        raise AuditError(f"GitHub workflows for {full_name} was not a list")
    names: list[str] = []
    files: list[tuple[str, str]] = []
    for workflow in workflows:
        if not isinstance(workflow, dict):
            continue
        if str(workflow.get("state") or "active") != "active":
            continue
        name = workflow.get("name")
        path = workflow.get("path")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        if not isinstance(path, str) or not path:
            continue
        label = f"{name or ''} {path}".lower()
        if "sentry" not in label and "cron" not in label:
            continue
        text, token = _file_text(owner, repo, path, token, root, opener)
        if text is not None:
            files.append((path, text))
    script, token = _file_text(owner, repo, "scripts/sentry-ci-report.py", token, root, opener)
    if script is not None:
        files.append(("scripts/sentry-ci-report.py", script))
    return (
        RepoSource(
            full_name=full_name,
            readable=True,
            names_known=True,
            workflow_names=tuple(names),
            files=tuple(files),
        ),
        token,
    )


def _file_text(
    owner: str, repo: str, path: str, token: str, root: str, opener
) -> tuple[str | None, str]:
    url = f"{root}/repos/{owner}/{repo}/contents/{path}"
    status, _, body = _read(opener, url, token, "application/vnd.github.raw")
    if status == 401 and token:
        token = ""
        status, _, body = _read(opener, url, token, "application/vnd.github.raw")
    if status == 404:
        return None, token
    if status != 200:
        raise AuditError(f"GitHub file {owner}/{repo}/{path} returned HTTP {status}")
    try:
        return body.decode("utf-8"), token
    except UnicodeError:
        raise AuditError(f"GitHub file {owner}/{repo}/{path} was not text") from None


def _read(opener, url: str, token: str, accept: str) -> tuple[int, str, bytes]:
    request = build_github_request(url, token, accept)
    if request.get_method() != "GET":
        raise AuditError("refusing a GitHub request that is not GET")
    host = urlsplit(url).hostname or ""
    try:
        with opener.open(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            body = response.read(_MAX_BYTES + 1)
    except HTTPError as exc:
        exc.read(256)
        return exc.code, "", b""
    except URLError:
        raise AuditError("GitHub could not be reached") from None
    except TimeoutError:
        raise AuditError("GitHub timed out") from None
    if urlsplit(request.full_url).hostname != host:
        raise AuditError("refusing a GitHub response that left the API host")
    if len(body) > _MAX_BYTES:
        raise AuditError("GitHub response was too large")
    return status, "", body


def fleet_identities(apps: list[App]) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for app in apps:
        for label in (app.repo, app.display):
            identity = slugify(label)
            if len(identity) < 3:
                continue
            mapping.setdefault(identity, set()).add(app.full_name)
    return mapping
