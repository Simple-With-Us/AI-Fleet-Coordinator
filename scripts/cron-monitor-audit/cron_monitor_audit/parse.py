"""Find check-in senders in workflow and script text.

The parsers read source as text. They do not execute it.
"""

from __future__ import annotations

import json
import re

_CRON_KEY = re.compile(r"(?m)^CRON_SCHEDULES\s*=\s*\{")
_PAIR = re.compile(r'"((?:\\.|[^"\\])*)"\s*:\s*"((?:\\.|[^"\\])*)"')
_APP_LITERAL = re.compile(r'(?m)^APP\s*=\s*"([^"]+)"\s*$')
_APP_FALLBACK = re.compile(r'os\.environ\.get\(\s*"SENTRY_APP".*?\bor\s+"([^"]+)"', re.DOTALL)
_IDENTITY_LITERAL = re.compile(r'(?m)^APP_IDENTITY\s*=\s*"([^"]+)"\s*$')
_ASSIGN = re.compile(
    r"""(?:monitor_slug|monitorSlug|[A-Z][A-Z0-9_]*SLUG)\s*=\s*["']([a-z0-9]+(?:-[a-z0-9]+)*)["']"""
)
_PREFIX = re.compile(r"""MONITOR_PREFIX\s*=\s*["']([a-z0-9]+(?:-[a-z0-9]+)*-)["']""")
_PINNED_CALL = re.compile(r"""monitor_slug\(\s*["']([A-Za-z0-9-]+)["']\s*\)""")
_PINNED_DEFAULT = re.compile(r"""or\s+["']([A-Za-z0-9-]+)["']""")


def slugify(name: str) -> str:
    """Same folding as scripts/sentry-ci-report.py."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


class Reporter:
    def __init__(self, identity: str, schedules: dict[str, str] | None, variable: str):
        self.identity = identity
        self.schedules = schedules
        self.variable = variable

    @property
    def known(self) -> bool:
        return self.schedules is not None and bool(self.identity)


def parse_reporter(path: str, text: str) -> Reporter | None:
    name = path.rsplit("/", 1)[-1]
    if name.endswith((".yml", ".yaml")):
        return _parse_workflow(text)
    if name == "sentry-ci-report.py":
        return _parse_script(text)
    return None


def literal_slugs(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for match in _ASSIGN.finditer(stripped):
            found.add(match.group(1))
    return found


def operator_senders(text: str) -> tuple[set[str], str]:
    """Pinned operator slugs, plus the dynamic prefix when one is built."""
    prefix_match = _PREFIX.search(text)
    if not prefix_match:
        return set(), ""
    prefix = prefix_match.group(1)
    pinned: set[str] = set()
    for match in _PINNED_CALL.finditer(text):
        pinned.add(prefix + slugify(match.group(1)))
    # The default applies only in the file that builds the slug.
    if "def monitor_slug" in text:
        for match in _PINNED_DEFAULT.finditer(text):
            pinned.add(prefix + slugify(match.group(1)))
    return pinned, prefix


def workflow_display_name(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("name:"):
            return _yaml_scalar(stripped.split(":", 1)[1].strip())
        return ""
    return ""


def _parse_workflow(text: str) -> Reporter | None:
    if "sentry-ci-report" not in text and "cron_schedules:" not in text:
        return None
    schedules = _json_schedules(_yaml_value(text, "cron_schedules"))
    app = _yaml_value(text, "app")
    identity_raw = _yaml_value(text, "app_identity")
    identity = ""
    if identity_raw and identity_raw not in {'""', "''"}:
        identity = slugify(identity_raw)
    elif app:
        identity = slugify(app)
    if schedules is None and not identity:
        return None
    return Reporter(identity, schedules, "workflow")


def _parse_script(text: str) -> Reporter | None:
    if "monitor_slug" not in text and "CRON_SCHEDULES" not in text:
        return None
    variable = ""
    if "ci-{APP_IDENTITY}" in text:
        variable = "APP_IDENTITY"
    elif "ci-{APP}" in text:
        variable = "APP"
    identity = ""
    literal = _APP_LITERAL.search(text)
    identity_literal = _IDENTITY_LITERAL.search(text)
    if variable == "APP_IDENTITY" and identity_literal:
        identity = slugify(identity_literal.group(1))
    elif literal:
        identity = slugify(literal.group(1))
    elif variable == "APP_IDENTITY":
        fallback = _APP_FALLBACK.search(text)
        if fallback:
            identity = slugify(fallback.group(1))
    schedules = _python_schedules(text)
    if schedules is None and not identity and not variable:
        return None
    return Reporter(identity, schedules, variable)


def _python_schedules(text: str) -> dict[str, str] | None:
    match = _CRON_KEY.search(text)
    if not match:
        return None
    body = _brace_block(text, match.end() - 1)
    if body is None:
        return None
    return {_unescape(key): _unescape(value) for key, value in _PAIR.findall(body)}


def _json_schedules(raw: str | None) -> dict[str, str] | None:
    if raw is None:
        return None
    text = raw.strip()
    # A bare `cron_schedules:` input has no value. That is unknown, not an
    # explicit empty map. Only the literal "{}" means the repo sends nothing.
    if not text:
        return None
    if text == "{}":
        return {}
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    schedules: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            schedules[key] = value
    return schedules


def _brace_block(text: str, start: int) -> str | None:
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _yaml_value(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^[ \t]*{re.escape(key)}:[ \t]*(.*)$", text)
    if not match:
        return None
    return _yaml_scalar(match.group(1).strip())


def _yaml_scalar(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        body = raw[1:-1]
        if raw[0] == "'":
            return body.replace("''", "'")
        return body.replace('\\"', '"')
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _unescape(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")
