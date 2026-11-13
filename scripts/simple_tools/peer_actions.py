"""Append one row to the Peer-Actions Registry Google Sheet (AFC-745).

Columns:  When | Acting Seat | Requesting Seat | Request (Source) | Action Taken | Result.
The Fleet-OPS Google service account JSON (GOOGLE_SHEETS_SA at /google-sheets-sa) is read from
Infisical through a scoped service token only — never fleet-secrets universal auth, never printed.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import identity as I
from . import secretscan

TAB_TITLE = "Peer-Actions Registry"
APPEND_RANGE = "'Peer-Actions Registry'!A:F"
DEFAULT_SPREADSHEET_ID = "1ZlHD3kJOcOPnVdkMYbHt_vfLbHKzsI7O-MpsM7Cq_NY"
CENTRAL_TZ = ZoneInfo("America/Chicago")

ENV_SPREADSHEET_ID = "PEER_ACTIONS_SPREADSHEET_ID"
ENV_INFISICAL_API = "PEER_ACTIONS_INFISICAL_API"
ENV_INFISICAL_PROJECT = "PEER_ACTIONS_INFISICAL_PROJECT_ID"
ENV_INFISICAL_TOKEN = "PEER_ACTIONS_INFISICAL_TOKEN"
ENV_INFISICAL_TOKEN_FILE = "PEER_ACTIONS_INFISICAL_TOKEN_FILE"
ENV_SA_JSON = "GOOGLE_SHEETS_SA"
INFISICAL_SECRET_PATH = "/google-sheets-sa"
INFISICAL_SECRET_NAME = "GOOGLE_SHEETS_SA"
FLEET_OPS_PROJECT_FILE = (
    Path(__file__).resolve().parent.parent / ".simple-tools" / "fleet-ops-project-id"
)

PRIVATE_FILE_MAX_BYTES = 8192
SEAT_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
TEXT_MAX = 4000
TEXT_MIN = 1
OAUTH_URL = "https://oauth2.googleapis.com/token"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API_TIMEOUT_S = 30.0
MAX_RETRY_ATTEMPTS = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects; urllib would re-send the Authorization header to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _http_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


class PeerActionsError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def read_secret_file(path: str) -> str:
    try:
        st = os.lstat(path)
        if not os.path.isfile(path) or (st.st_mode & 0o077):
            return ""
        with open(path, "rb") as fh:
            raw = fh.read(PRIVATE_FILE_MAX_BYTES + 1)
    except OSError:
        return ""
    if len(raw) > PRIVATE_FILE_MAX_BYTES:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except ValueError:
        return ""


def _secret_from_file_or_env(file_env: str, value_env: str) -> str:
    path = _env(file_env)
    if path:
        return read_secret_file(path)
    return _env(value_env)


def _project_id(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    explicit = (
        source.get(ENV_INFISICAL_PROJECT) or source.get("INFISICAL_FLEET_OPS_PROJECT_ID") or ""
    ).strip()
    if explicit:
        return explicit
    try:
        return FLEET_OPS_PROJECT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _validate_service_token(token: str) -> None:
    if (
        not token.startswith("st.")
        or len(token) < 20
        or len(token) > 2048
        or any(c.isspace() for c in token)
    ):
        raise PeerActionsError(
            "not_configured", "a scoped Infisical service token is required", retryable=False
        )


def _infisical_get_secret(
    token: str,
    project_id: str,
    *,
    api_base: str,
    env: Mapping[str, str] | None = None,
) -> str:
    _validate_service_token(token)
    if not project_id:
        raise PeerActionsError(
            "not_configured", "the Fleet-OPS Infisical project id is not configured"
        )
    base = (api_base or "https://app.infisical.com").rstrip("/")
    qs = urllib.parse.urlencode(
        {
            "workspaceId": project_id,
            "environment": "prod",
            "secretPath": INFISICAL_SECRET_PATH,
            "expandSecretReferences": "true",
        }
    )
    req = urllib.request.Request(
        f"{base}/api/v3/secrets/raw/{urllib.parse.quote(INFISICAL_SECRET_NAME)}?{qs}",
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "simple-peer-actions/1.0",
        },
    )
    opener = _http_opener()
    try:
        with opener.open(req, timeout=API_TIMEOUT_S) as resp:
            raw = resp.read(512 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise PeerActionsError(
                "not_configured", "GOOGLE_SHEETS_SA is not in Fleet-OPS prod /google-sheets-sa"
            ) from None
        raise PeerActionsError(
            "infisical_unavailable",
            f"Infisical answered HTTP {exc.code}",
            retryable=exc.code in {429, 500, 502, 503, 504},
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
        raise PeerActionsError(
            "infisical_unavailable", type(exc).__name__, retryable=True
        ) from None
    if len(raw) > 512 * 1024:
        raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is not valid JSON") from exc
    value = payload.get("secret", {}).get("secretValue") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is empty")
    return value.strip()


def load_service_account_json(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    direct = (source.get(ENV_SA_JSON) or "").strip()
    if direct:
        try:
            return json.loads(direct)
        except json.JSONDecodeError as exc:
            raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is not valid JSON") from exc
    token = _secret_from_file_or_env(ENV_INFISICAL_TOKEN_FILE, ENV_INFISICAL_TOKEN)
    if not token:
        raise PeerActionsError(
            "not_configured",
            f"set {ENV_SA_JSON}, or {ENV_INFISICAL_TOKEN_FILE} / {ENV_INFISICAL_TOKEN} for Infisical",
        )
    project = _project_id(source)
    api = _env(ENV_INFISICAL_API) if env is None else (source.get(ENV_INFISICAL_API) or "").strip()
    raw = _infisical_get_secret(
        token, project, api_base=api or "https://app.infisical.com", env=source
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is not valid JSON") from exc


def _clean_field(
    name: str, value: str, *, min_len: int = TEXT_MIN, max_len: int = TEXT_MAX,
    known: Iterable[str] = (),
) -> str:
    if not isinstance(value, str):
        raise PeerActionsError("invalid_argument", f"{name} must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < min_len:
        raise PeerActionsError("invalid_argument", f"{name} is required")
    if len(text) > max_len:
        raise PeerActionsError("invalid_argument", f"{name} must be at most {max_len} characters")
    if "\n" in text:
        raise PeerActionsError("invalid_argument", f"{name} must be a single line")
    if secretscan.scan(text, known):
        raise PeerActionsError("refused_secret", f"{name} contains credential-shaped text")
    return text


def _clean_seat(name: str, value: str, known: Iterable[str] = ()) -> str:
    text = _clean_field(name, value, max_len=32, known=known)
    seat = I.canonical_seat(text)
    if not SEAT_PATTERN.match(seat):
        raise PeerActionsError("invalid_argument", f"{name} must be a fleet seat tag")
    return seat


def central_timestamp(now: datetime | None = None) -> str:
    instant = now or datetime.now(CENTRAL_TZ)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=CENTRAL_TZ)
    else:
        instant = instant.astimezone(CENTRAL_TZ)
    return instant.strftime("%Y-%m-%d %I:%M:%S %p CT")


@dataclass(frozen=True)
class LogEntry:
    when: str
    acting_seat: str
    requesting_seat: str
    request_source: str
    action_taken: str
    result: str

    def row(self) -> list[str]:
        return [
            self.when,
            self.acting_seat,
            self.requesting_seat,
            self.request_source,
            self.action_taken,
            self.result,
        ]


def build_entry(
    *,
    acting_seat: str,
    requesting_seat: str,
    request_source: str,
    action_taken: str,
    result: str,
    now: datetime | None = None,
    known: Iterable[str] = (),
) -> LogEntry:
    known = tuple(known)
    return LogEntry(
        when=central_timestamp(now),
        acting_seat=_clean_seat("acting_seat", acting_seat, known),
        requesting_seat=_clean_seat("requesting_seat", requesting_seat, known),
        request_source=_clean_field("request", request_source, known=known),
        action_taken=_clean_field("action_taken", action_taken, known=known),
        result=_clean_field("result", result, known=known),
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _google_access_token(sa: Mapping[str, Any], *, now: int | None = None) -> str:
    if not isinstance(sa.get("client_email"), str) or not isinstance(sa.get("private_key"), str):
        raise PeerActionsError("invalid_secret", "GOOGLE_SHEETS_SA is not a service account key")
    issued = now if now is not None else int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": sa["client_email"],
        "scope": SHEETS_SCOPE,
        "aud": OAUTH_URL,
        "iat": issued,
        "exp": issued + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}".encode()
    fd, path = tempfile.mkstemp(prefix="peer-actions-sa-", suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sa["private_key"])
        try:
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", path],
                input=signing_input,
                capture_output=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise PeerActionsError(
                "sheets_auth_failed", "openssl is required to sign the Google JWT"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PeerActionsError(
                "sheets_auth_failed", "Google JWT signing timed out", retryable=True
            ) from exc
        if proc.returncode:
            raise PeerActionsError("sheets_auth_failed", "Google JWT signing failed")
        assertion = signing_input.decode() + "." + _b64url(proc.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    body = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    ).encode()
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise PeerActionsError(
            "sheets_auth_failed", f"Google OAuth answered HTTP {exc.code}"
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError) as exc:
        raise PeerActionsError("sheets_auth_failed", type(exc).__name__, retryable=True) from exc
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise PeerActionsError("sheets_auth_failed", "Google OAuth returned no access token")
    return token


def _retry_after(headers: Mapping[str, str], attempt: int) -> float:
    raw = headers.get("Retry-After") or headers.get("retry-after") or ""
    if raw.isdigit():
        return float(min(int(raw), 120))
    return float(min(2**attempt, 32))


class SheetsClient:
    """Injectable Google Sheets append client for tests."""

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        token_provider: Any | None = None,
        opener: Any | None = None,
        sleep: Any | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self._token_provider = token_provider
        self._opener = opener or _http_opener()
        self._sleep = sleep or time.sleep

    def append(self, row: list[str]) -> dict[str, Any]:
        if self._token_provider is None:
            raise PeerActionsError("not_configured", "no Google token provider")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/"
            f"{urllib.parse.quote(APPEND_RANGE, safe='')}:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
        )
        body = json.dumps({"values": [row]}, separators=(",", ":")).encode()
        attempt = 0
        while True:
            attempt += 1
            token = self._token_provider()
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with self._opener.open(req, timeout=API_TIMEOUT_S) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < MAX_RETRY_ATTEMPTS:
                    self._sleep(_retry_after(dict(exc.headers.items()), attempt))
                    continue
                raise PeerActionsError(
                    "sheets_write_failed",
                    f"Google Sheets answered HTTP {exc.code}",
                    retryable=exc.code in {429, 500, 502, 503, 504},
                ) from None
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
                json.JSONDecodeError,
            ) as exc:
                raise PeerActionsError(
                    "sheets_write_failed", type(exc).__name__, retryable=True
                ) from exc


def log_row(
    entry: LogEntry,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    sheets: SheetsClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    spreadsheet_id = (source.get(ENV_SPREADSHEET_ID) or DEFAULT_SPREADSHEET_ID).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise PeerActionsError("invalid_argument", f"{ENV_SPREADSHEET_ID} is invalid")
    result = {
        "dry_run": dry_run,
        "spreadsheet_id": spreadsheet_id,
        "tab": TAB_TITLE,
        "row": entry.row(),
    }
    if dry_run:
        return result
    client = sheets
    if client is None:
        sa = load_service_account_json(source)
        token_cache: dict[str, Any] = {}

        def token() -> str:
            if "value" not in token_cache or token_cache.get("exp", 0) <= int(time.time()):
                token_cache["value"] = _google_access_token(sa)
                token_cache["exp"] = int(time.time()) + 3000
            return token_cache["value"]

        client = SheetsClient(spreadsheet_id=spreadsheet_id, token_provider=token)
    updated = client.append(entry.row())
    result["updated_range"] = (
        updated.get("updates", {}).get("updatedRange") if isinstance(updated, dict) else None
    )
    result["updated_rows"] = (
        updated.get("updates", {}).get("updatedRows") if isinstance(updated, dict) else None
    )
    return result
