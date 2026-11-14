"""Credentials, HTTP, embeddings, and a small Qdrant client for the fleet-agents corpus.

Credentials come from the environment when set, otherwise from Infisical via an explicitly bound scoped
read-only service token.  Nothing reads the old flat credentials file any more (OPS-818).
Values are never printed or logged.

Run your own: set QDRANT_URL, QDRANT_API_KEY, TEI_URL, TEI_API_KEY and
QDRANT_FLEET_COLLECTION in the environment and nothing else is needed -- no Infisical, no
credentials file, no code edits.  Everything Infisical-shaped is a default that the
environment overrides: FLEET_RAG_INFISICAL_API, FLEET_RAG_INFISICAL_PROJECT,
FLEET_RAG_INFISICAL_ENV and FLEET_RAG_HANDOFF_FILE (an explicit KEY=value file of your own;
there is no default file).

Read vs write: if QDRANT_READONLY_API_KEY is available it is used for every read path
(search / scroll / count / stats); QDRANT_API_KEY is used only for upsert / delete.  Callers
that only read may pass need_write=False so a machine without the write key still works.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from itertools import chain
from typing import Any

from .storage_guard import StorageGuard, StorageGuardError

# Infisical defaults.  Every one of these is a *default*, overridable from the
# environment so a third party can point the same code at their own Infisical (or self-hosted
# Infisical) and their own credentials file without editing this module.  See infisical_api(),
# infisical_project(), infisical_env() and handoff_file() below for the override names.
INFISICAL_API = "https://app.infisical.com/api"
SHARED_PROJECT = "18f563a3-9c88-454c-96eb-28fc9678f3ba"
SHARED_ENV = "prod"
NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000fee7")

READ_KEYS = ("TEI_URL", "TEI_API_KEY", "QDRANT_URL", "QDRANT_FLEET_COLLECTION")
INFISICAL_READ_TOKEN: str = "FLEET_RECALL_INFISICAL_TOKEN"
# Only an explicit binding to this consumer's authorized bootstrap store is accepted.
# No retired flat-file path is discovered.  The process binding, including an explicit empty
# marker, takes precedence over the optional caller-declared protected file.
INFISICAL_READ_TOKEN_FILE = "FLEET_RECALL_INFISICAL_TOKEN_FILE"
READ_TOKEN_FILE_MAX = 16384
WRITE_KEYS = ("QDRANT_API_KEY",)
OPTIONAL_KEYS = (
    "QDRANT_READONLY_API_KEY",
    "TEI_EMBED_MODEL",
    "TEI_RERANK_URL",
    "TEI_RERANK_API_KEY",
)
ALL_KEYS = READ_KEYS + WRITE_KEYS + OPTIONAL_KEYS

EMBED_MODEL_TAG = "BAAI/bge-m3-selfhosted"
EMBED_BATCH = 8  # the ONNX backend caps a batch at 8 requests
DEFAULT_TIMEOUT = 120
RETRIES = 4
BACKOFF_MAX = 10  # seconds, the exponential-backoff cap between retries

# Overridable at call time (env is read inside http_json(), never baked in at import), so a
# caller behind a slower hop -- the SSH tunnel to the private Qdrant/TEI host, see
# scripts/recall-tunnel's `env` -- can widen the retry budget without a code change, and tests
# can set/clear them per-case.  RETRIES/BACKOFF_MAX above stay the defaults when neither is set.
RETRIES_ENV = "RECALL_HTTP_RETRIES"
BACKOFF_MAX_ENV = "RECALL_HTTP_BACKOFF_MAX"

# Sane caps for the two env overrides above, enforced by _env_int().  A negative
# RECALL_HTTP_RETRIES made `range(retries + 1)` empty, so http_json returned "request failed:
# NoneType" without ever sending a request; a negative RECALL_HTTP_BACKOFF_MAX reached
# `time.sleep(negative)`, a raw ValueError that broke the only-FleetRagError contract.  Both
# are now floored at 0 (falling back to the default, same as a non-numeric value) and an
# unreasonably large value is clamped rather than honored outright.
MAX_RETRIES = 20
MAX_BACKOFF_MAX = 300  # seconds

# Cross-encoder rerank (TEI /rerank, BAAI/bge-reranker-v2-m3).  Optional: only used when both
# TEI_RERANK_URL and TEI_RERANK_API_KEY are configured, and every failure falls back silently.
RERANK_BATCH = 32
RERANK_TIMEOUT = 8  # TOTAL seconds for one rerank() call, across every batch

# "Lesson" prefetch: agent contributions in these categories get their own prefetch so a matching
# lesson always enters the fusion near the top instead of drowning under doc chunks.
LESSON_SOURCE = "agent-contribution"
LESSON_CATEGORIES = ("lesson", "preference", "decision", "runbook")
# Dense cosine floor for the lesson prefetch.  RRF hands the top item of every prefetch list a
# high fused rank whatever its score, so without a floor the best-of-33-contributions lands in
# the top 5 of every query.  Measured 2026-09-02 (bge-m3, live corpus): relevant lessons score
# 0.62-0.74, unrelated ones 0.37-0.55.
LESSON_SCORE_THRESHOLD = 0.6


class FleetRagError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP


def _env_int(name: str, default: int, *, min_value: int = 0, max_value: int | None = None) -> int:
    """int(os.environ[name]) when set, parseable, and >= min_value, else `default`; a value
    above max_value (when given) is clamped to max_value rather than rejected.  Read fresh on
    every call (never cached at import time) so a test's os.environ patch, or an `eval
    "$(recall-tunnel env)"` in a long-lived shell, takes effect immediately.

    A value below min_value -- e.g. a negative RECALL_HTTP_RETRIES, which would make
    range(retries + 1) empty and silently skip every request -- is treated the same as a
    non-numeric value: fall back to `default`.
    """
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        n = int(v)
    except ValueError:
        return default
    if n < min_value:
        return default
    if max_value is not None and n > max_value:
        return max_value
    return n


def http_json(
    url: str,
    body: Any = None,
    headers: dict | None = None,
    method: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int | None = None,
    before_attempt=None,
) -> Any:
    """JSON request with bounded retries on 408 / 429 / 5xx / connection errors.  Never logs
    bodies.

    `retries` defaults to RETRIES, overridable per-process via RECALL_HTTP_RETRIES; the
    exponential-backoff cap defaults to BACKOFF_MAX seconds, overridable via
    RECALL_HTTP_BACKOFF_MAX.  Both env vars are read at call time, not baked in as a default
    argument, so `recall-tunnel env`'s wider budget (RECALL_HTTP_RETRIES=8,
    RECALL_HTTP_BACKOFF_MAX=60 -- about 4-5 minutes total) takes effect the moment it is
    exported, and tests can set/clear them per-case.  A caller that passes `retries` explicitly
    (e.g. the public-fallback REST twin's PUBLIC_RETRIES=1) is never affected by the env var.

    408 (Request Timeout) is retried like 429/5xx: Qdrant's upsert-by-id, set-payload-by-id and
    delete-by-id/filter calls are idempotent, so retrying one after a tunnel stall replays the
    exact same bounded point request, never the larger recall operation that planned it.
    """
    if retries is None:
        retries = _env_int(RETRIES_ENV, RETRIES, min_value=0, max_value=MAX_RETRIES)
    backoff_max = _env_int(BACKOFF_MAX_ENV, BACKOFF_MAX, min_value=0, max_value=MAX_BACKOFF_MAX)
    data = json.dumps(body).encode() if body is not None else None
    method = method or ("POST" if body is not None else "GET")
    last: Exception | None = None
    for attempt in range(retries + 1):
        if before_attempt is not None:
            before_attempt(len(data) if data is not None else 0)
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                try:
                    return json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    # The body stays out of the message.  A 200 from a proxy or a
                    # truncated tunnel can be HTML or a partial object, and that text
                    # is not a reason to retry.
                    raise FleetRagError(
                        f"malformed JSON from {_host(url)}{urllib.parse.urlparse(url).path}"
                    ) from None
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(2**attempt, backoff_max))
                continue
            detail = ""
            try:
                detail = e.read(400).decode(errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise FleetRagError(
                f"HTTP {e.code} from {_host(url)}{urllib.parse.urlparse(url).path}: {detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(min(2**attempt, backoff_max))
                continue
            raise FleetRagError(f"{type(e).__name__} reaching {_host(url)}") from None
    raise FleetRagError(f"request failed: {type(last).__name__}")


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc


# --------------------------------------------------------------------------- credentials


def infisical_api() -> str:
    """Infisical API base.  FLEET_RAG_INFISICAL_API overrides it (self-hosted Infisical)."""
    return (os.environ.get("FLEET_RAG_INFISICAL_API") or INFISICAL_API).rstrip("/")


def infisical_project() -> str:
    """Infisical project (workspace) id.  FLEET_RAG_INFISICAL_PROJECT overrides it."""
    return os.environ.get("FLEET_RAG_INFISICAL_PROJECT") or SHARED_PROJECT


def infisical_env() -> str:
    """Infisical environment slug.  FLEET_RAG_INFISICAL_ENV may name it, but prod is
    the only environment (owner decision 2026-10-10, mirroring
    infisical_settings._require_prod): any other slug is refused loudly instead of
    silently fetching from a stale dev/staging copy.  Empty means prod."""
    slug = (os.environ.get("FLEET_RAG_INFISICAL_ENV") or "").strip().lower()
    if not slug:
        return SHARED_ENV
    if slug != SHARED_ENV:
        raise FleetRagError(
            f"refusing Infisical environment {slug!r}: prod is the only environment "
            "(dev and staging are retired); unset FLEET_RAG_INFISICAL_ENV or set it "
            "to prod; see INFISICAL.md"
        )
    return slug


def handoff_file() -> pathlib.Path | None:
    """An explicit credentials file of the caller's own, or None.

    FLEET_RAG_HANDOFF_FILE names it.  There is no default file, and the file is
    configuration owned by the caller rather than an Infisical authentication source.  Values are never printed or logged.
    """
    override = os.environ.get("FLEET_RAG_HANDOFF_FILE")
    return pathlib.Path(override).expanduser() if override else None


def _identity(prefix: str) -> tuple[None, None]:
    """Legacy compatibility seam; recall never resolves shared machine identities."""
    return None, None


FLEET_SECRETS_TIMEOUT = 60


def fleet_secrets_many(names: Iterable[str]) -> dict[str, str]:
    """Present named values from the consumer's explicit default Fleet-OPS root reader.

    Missing binding, invalid input or a failed read returns an empty map; a named 404 omits
    only that name.  The separate recall-project token is never a fallback for this route.
    """
    source = {
        key: value
        for key in (
            "INFISICAL_TOKEN",
            "INFISICAL_FLEET_OPS_PROJECT_ID",
            "FLEET_SECRETS_API",
            "INFISICAL_ENVIRONMENT",
            "INFISICAL_ENV",
            "HOME",
        )
        if (value := os.environ.get(key)) is not None
    }
    token = (source.get("INFISICAL_TOKEN") or "").strip()
    if not token:
        return {}
    try:
        import fleet_infisical as secrets
    except ImportError:
        return {}
    error_type = getattr(secrets, "SecretsError", None)
    default_project = getattr(secrets, "DEFAULT_PROJECT", None)
    if (
        not all(
            callable(getattr(secrets, name, None))
            for name in ("infisical_project_id", "home_dir", "bounded_fetch_project")
        )
        or default_project != "fleet-ops"
        or not isinstance(error_type, type)
        or not issubclass(error_type, Exception)
    ):
        return {}
    try:
        project = secrets.infisical_project_id(source, secrets.home_dir(source), default_project)
        # The shared API validates and bounds the iterator before starting its read-only worker.
        # No ambient read-path declaration can widen this exact default-root consumer route.
        got = secrets.bounded_fetch_project(
            project,
            "/",
            source,
            token=token,
            names=(name for name in names if name),
            timeout=FLEET_SECRETS_TIMEOUT,
        )
    except (error_type, OSError, TypeError, ValueError):
        return {}
    return {key: value for key, value in got.items() if isinstance(value, str) and value}


def fleet_secret(name: str) -> str | None:
    """One optional value through the shared default Fleet-OPS reader."""
    return fleet_secrets_many([name]).get(name)


def fleet_secret_get(name: str) -> str | None:
    """One optional named value, including INFISICAL_* names; strip surrounding whitespace."""
    value = (fleet_secrets_many([name]).get(name) or "").strip()
    return value or None


class _ReadTokenError(FleetRagError):
    """Closed bootstrap reasons shared by the reader, nightly and metadata-only doctor."""

    _MESSAGES = {
        "token-binding-empty": f"{INFISICAL_READ_TOKEN} is explicitly empty; no file fallback is allowed",
        "token-file-empty": f"{INFISICAL_READ_TOKEN_FILE} is explicitly empty",
        "token-file-path": f"{INFISICAL_READ_TOKEN_FILE} must name an absolute authorized bootstrap file",
        "token-file-retired": f"{INFISICAL_READ_TOKEN_FILE} cannot select a retired .secrets source or alias",
        "token-file": f"{INFISICAL_READ_TOKEN_FILE} is missing, unreadable or symlinked",
        "token-file-mode": f"{INFISICAL_READ_TOKEN_FILE} must be an owned regular mode 600 or 400 file in a protected store",
        "token-file-size": f"{INFISICAL_READ_TOKEN_FILE} exceeds the bootstrap size limit",
        "token-invalid": f"{INFISICAL_READ_TOKEN} must contain only this reader's scoped service token",
    }

    def __init__(self, code: str):
        self.code = code if code in self._MESSAGES else "token-invalid"
        super().__init__(self._MESSAGES[self.code])


def _checked_read_token(value: str) -> str:
    try:
        from fleet_secrets import core as secrets

        return secrets.check_service_token(value)
    except (ImportError, ValueError, TypeError):
        raise _ReadTokenError("token-invalid") from None
    except secrets.SecretsError:
        raise _ReadTokenError("token-invalid") from None


def _process_read_token(env: Mapping[str, str]) -> str | None:
    if INFISICAL_READ_TOKEN not in env:
        return None
    value = env[INFISICAL_READ_TOKEN]
    if not isinstance(value, str) or not value.strip():
        raise _ReadTokenError("token-binding-empty")
    return _checked_read_token(value.strip())


def _read_token_file_path(env: Mapping[str, str] | None = None) -> pathlib.Path | None:
    """Only the explicit current-store selector; no default and no token content read."""
    source = os.environ if env is None else env
    if INFISICAL_READ_TOKEN_FILE not in source:
        return None
    named = source[INFISICAL_READ_TOKEN_FILE]
    if not isinstance(named, str) or not named.strip():
        raise _ReadTokenError("token-file-empty")
    named = named.strip()
    if "\x00" in named:
        raise _ReadTokenError("token-file-path")
    if named == "~" or named.startswith("~/"):
        home = source.get("HOME") or ""
        if not isinstance(home, str) or not pathlib.Path(home).is_absolute():
            raise _ReadTokenError("token-file-path")
        path = pathlib.Path(home) / named[2:]
    elif named.startswith("~"):
        raise _ReadTokenError("token-file-path")
    else:
        path = pathlib.Path(named)
    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise _ReadTokenError("token-file-path")
    if any(part.casefold() == ".secrets" for part in path.parts):
        raise _ReadTokenError("token-file-retired")
    try:
        parent = path.parent.resolve()
    except (OSError, RuntimeError, ValueError):
        raise _ReadTokenError("token-file-path") from None
    if any(part.casefold() == ".secrets" for part in parent.parts):
        raise _ReadTokenError("token-file-retired")
    return parent / path.name


def _open_read_token_file(
    env: Mapping[str, str] | None = None,
) -> tuple[pathlib.Path, int] | None:
    """Checked explicit bootstrap (path, fd), or None; never read its contents."""
    path = _read_token_file_path(env)
    if path is None:
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    opened = []
    file_fd = None
    uid = os.getuid()

    def check_directory(fd: int, location: pathlib.Path) -> None:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
            raise _ReadTokenError("token-file-mode")
        if info.st_mode & 0o022:
            # Only the standard root-owned sticky temporary anchors may be shared.
            # Sticky custody protects each caller's owned subtree beneath them.
            if not (
                info.st_uid == 0
                and info.st_mode & stat.S_ISVTX
                and location in (pathlib.Path("/tmp"), pathlib.Path("/private/tmp"))
            ):
                raise _ReadTokenError("token-file-mode")

    try:
        directory = os.open(path.anchor, flags)
        opened.append(directory)
        location = pathlib.Path(path.anchor)
        check_directory(directory, location)
        for part in path.parent.parts[1:]:
            directory = os.open(part, flags, dir_fd=directory)
            opened.append(directory)
            location /= part
            check_directory(directory, location)
        file_fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory,
        )
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
        ):
            raise _ReadTokenError("token-file-mode")
        if info.st_size > READ_TOKEN_FILE_MAX:
            raise _ReadTokenError("token-file-size")
        result = path, file_fd
        file_fd = None
        return result
    except OSError:
        raise _ReadTokenError("token-file") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(opened):
            os.close(descriptor)


def _decode_read_token(text: str) -> str:
    """A single raw token or this consumer's existing assignment format, never dotenv."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) != 1:
        raise _ReadTokenError("token-invalid")
    value = lines[0]
    if not value.startswith("st."):
        if value.startswith("export "):
            value = value[len("export ") :].lstrip()
        key, sep, value = value.partition("=")
        if not sep or key.strip() != INFISICAL_READ_TOKEN:
            raise _ReadTokenError("token-invalid")
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise _ReadTokenError("token-invalid")
            value = value[1:-1]
    return _checked_read_token(value)


def _read_token_file(env: Mapping[str, str] | None = None) -> str | None:
    selected = _open_read_token_file(env)
    if selected is None:
        return None
    _path, fd = selected
    try:
        data = bytearray()
        while len(data) <= READ_TOKEN_FILE_MAX:
            chunk = os.read(fd, min(4096, READ_TOKEN_FILE_MAX + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > READ_TOKEN_FILE_MAX:
            raise _ReadTokenError("token-file-size")
        return _decode_read_token(bytes(data).decode("utf-8"))
    except (OSError, UnicodeError):
        raise _ReadTokenError("token-invalid") from None
    finally:
        os.close(fd)


def _read_token_bound(env: Mapping[str, str] | None = None) -> bool:
    """An explicit binding was configured, including an invalid/empty marker; not grant proof."""
    source = os.environ if env is None else env
    return INFISICAL_READ_TOKEN in source or INFISICAL_READ_TOKEN_FILE in source


def read_token_binding(env: Mapping[str, str] | None = None) -> str | None:
    """Only this reader's explicit process or authorized current-store file binding."""
    source = os.environ if env is None else env
    process = _process_read_token(source)
    return process if process is not None else _read_token_file(source)


def infisical_login() -> str | None:
    """The explicit read-only service-token binding, without a login."""
    from fleet_secrets import core as secrets

    env = dict(os.environ)
    token = (read_token_binding() or "").strip()
    if not token:
        if any(
            (env.get(name) or "").strip()
            for name in (
                secrets.ENV_TOKEN,
                secrets.ENV_AI_FLEET_COORDINATOR_TOKEN,
                secrets.ENV_WRITE_TOKEN,
            )
        ):
            raise FleetRagError(f"bind the recall reader as {INFISICAL_READ_TOKEN}")
        return None
    try:
        return secrets.check_service_token(token)
    except secrets.SecretsError:
        raise FleetRagError("invalid scoped Infisical read-token binding") from None


def infisical_fetch(wanted: Iterable[str]) -> dict[str, str]:
    """Read this configured project's root; never borrow Fleet-OPS credentials."""
    from fleet_secrets import core as secrets

    infisical_env()
    iterator = iter(wanted)
    empty = object()
    first = next(iterator, empty)
    if first is empty:
        return {}
    read_token = read_token_binding()
    if read_token is None:
        raise FleetRagError(
            f"bind the recall reader as {INFISICAL_READ_TOKEN} or {INFISICAL_READ_TOKEN_FILE}"
        )
    env = {name: os.environ[name] for name in secrets.ENV_NAMES if name in os.environ}
    env[secrets.ENV_API] = infisical_api()
    env[secrets.ENV_READ_PATHS] = "/"
    try:
        return secrets.bounded_fetch_project(
            infisical_project(),
            "/",
            env,
            token=read_token,
            names=chain((first,), iterator),
            timeout=30.0,
        )
    except TimeoutError:
        raise FleetRagError("Infisical scoped read timed out") from None
    except secrets.SecretsError as exc:
        raise FleetRagError("Infisical scoped read failed: " + exc.message) from None


def load_config(need_write: bool = False, extra: Iterable[str] = ()) -> dict[str, str]:
    """Environment first, then Infisical.  Returns the keys that were found.

    Environment-only operation is fully supported: when QDRANT_URL, QDRANT_API_KEY, TEI_URL,
    TEI_API_KEY and QDRANT_FLEET_COLLECTION are all set (plus any `extra`), no Infisical call
    is needed.  Otherwise Infisical reads require the dedicated FLEET_RECALL_INFISICAL_TOKEN
    (the variable, or its mode-600 token file), whose exact project, prod environment, and folder scope are verified before reading.

    Raises FleetRagError listing the missing required keys (never their values).
    """
    wanted = list(ALL_KEYS) + [k for k in extra if k not in ALL_KEYS]
    cfg = {k: os.environ[k] for k in wanted if os.environ.get(k)}
    required = list(READ_KEYS) + (list(WRITE_KEYS) if need_write else []) + list(extra)
    # A complete environment is supported standalone, so an unrelated service-token
    # binding must not turn an offline configuration into an Infisical authentication attempt.
    if all(k in cfg for k in required) and not _read_token_bound():
        return cfg
    tok = infisical_login()
    if all(k in cfg for k in required) and (
        tok is None or all(k in cfg for k in OPTIONAL_KEYS if k in wanted)
    ):
        return cfg
    if tok:
        # An explicit binding that fails verification is not a fallback invitation.
        for key, value in infisical_fetch([k for k in wanted if k not in cfg]).items():
            cfg.setdefault(key, value)
    missing = [k for k in required if k not in cfg]
    if missing:
        raise FleetRagError(
            "missing credentials: "
            + ", ".join(missing)
            + " (set them in the environment, or in the configured Infisical"
            + ' project; see docs/RAG-FLEET-INFRA.md "Run your own")'
        )
    return cfg


# --------------------------------------------------------------------------- embeddings


def embed(cfg: dict[str, str], texts: list[str]) -> list[list[float]]:
    """Embed texts with the self-hosted bge-m3 endpoint.  Over-length inputs are truncated."""
    out: list[list[float]] = []
    base = cfg["TEI_URL"].rstrip("/")
    hdr = {"Authorization": f"Bearer {cfg['TEI_API_KEY']}"}
    for i in range(0, len(texts), EMBED_BATCH):
        out += http_json(
            f"{base}/embed", {"inputs": texts[i : i + EMBED_BATCH], "truncate": True}, hdr
        )
    return out


def embedder_healthy(cfg: dict[str, str]) -> bool:
    try:
        with urllib.request.urlopen(f"{cfg['TEI_URL'].rstrip('/')}/health", timeout=30) as r:
            return r.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


# --------------------------------------------------------------------------- rerank


def rerank_configured(cfg: dict[str, str]) -> bool:
    return bool(cfg.get("TEI_RERANK_URL") and cfg.get("TEI_RERANK_API_KEY"))


def rerank_healthy(cfg: dict[str, str]) -> bool:
    """Like embedder_healthy, for the optional reranker.  Only call when rerank_configured(cfg)."""
    try:
        with urllib.request.urlopen(f"{cfg['TEI_RERANK_URL'].rstrip('/')}/health", timeout=30) as r:
            return r.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, KeyError):
        return False


def rerank(
    cfg: dict[str, str], query: str, texts: list[str], timeout: int | None = None
) -> list[float]:
    """Cross-encoder relevance of each text to the query via TEI's /rerank.

    Returns one score per input text, aligned to `texts`, batched RERANK_BATCH at a time.
    `timeout` (default RERANK_TIMEOUT) is a total budget for the whole call, not per batch: a
    100-candidate rerank never takes 4 x RERANK_TIMEOUT.  Raises FleetRagError when the
    endpoint is not configured, the budget runs out before every batch is scored, or the
    endpoint returns anything but a full aligned score set.  Callers that want a silent
    fallback (fused order) catch FleetRagError.
    """
    if not rerank_configured(cfg):
        raise FleetRagError("rerank endpoint not configured (TEI_RERANK_URL / TEI_RERANK_API_KEY)")
    if not texts:
        return []
    base = cfg["TEI_RERANK_URL"].rstrip("/")
    hdr = {"Authorization": f"Bearer {cfg['TEI_RERANK_API_KEY']}"}
    budget = timeout or RERANK_TIMEOUT
    deadline = time.monotonic() + budget
    scores: list[float] = []
    for i in range(0, len(texts), RERANK_BATCH):
        batch = texts[i : i + RERANK_BATCH]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FleetRagError(
                f"rerank budget of {budget}s exhausted after {i} of {len(texts)} texts"
            )
        try:
            res = http_json(
                f"{base}/rerank",
                {"query": query, "texts": batch, "truncate": True},
                hdr,
                timeout=remaining,
                retries=0,
            )
        except FleetRagError:
            raise
        except Exception as e:  # noqa: BLE001 - malformed JSON etc.: same fallback path
            raise FleetRagError(f"rerank failed: {type(e).__name__}") from None
        part = [0.0] * len(batch)
        seen = 0
        for row in res if isinstance(res, list) else []:
            try:
                idx, score = int(row["index"]), float(row["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(batch):
                part[idx] = score
                seen += 1
        if seen != len(batch):
            raise FleetRagError(f"rerank returned {seen} scores for {len(batch)} texts")
        scores += part
    return scores


# --------------------------------------------------------------------------- ids / text


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(digest: str) -> str:
    return str(uuid.uuid5(NAMESPACE, digest))


def now_ms() -> int:
    return int(time.time() * 1000)


_STOP = set(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "our",
        "we",
        "do",
        "does",
        "did",
        "not",
        "no",
        "yes",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "me",
        "my",
    ]
)


def query_terms(query: str, max_terms: int = 8) -> list[str]:
    """Keyword terms for the full-text side of hybrid search: lowercase, no stopwords, >2 chars."""
    seen: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_\-./]{2,}", query.lower()):
        tok = tok.strip(".-/")
        if len(tok) < 3 or tok in _STOP or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_terms:
            break
    return seen


# --------------------------------------------------------------------------- Qdrant


class Qdrant:
    """Minimal REST client scoped to one collection.  Reads use the read-only key when present."""

    def __init__(self, cfg: dict[str, str], collection: str | None = None):
        self.base = cfg["QDRANT_URL"].rstrip("/")
        self.collection = collection or cfg["QDRANT_FLEET_COLLECTION"]
        self._read_key = cfg.get("QDRANT_READONLY_API_KEY") or cfg.get("QDRANT_API_KEY")
        self._write_key = cfg.get("QDRANT_API_KEY")
        try:
            self.storage_guard = StorageGuard.from_env(self.base, self.collection)
        except StorageGuardError as exc:
            raise FleetRagError(str(exc)) from None
        if not self._read_key:
            raise FleetRagError("no Qdrant key available for reads")

    # -- plumbing
    def _call(
        self,
        path: str,
        body: Any = None,
        method: str | None = None,
        write: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int | None = None,
    ) -> Any:
        key = self._write_key if write else self._read_key
        if write and not key:
            raise FleetRagError("QDRANT_API_KEY (write key) not available on this machine")

        def check_storage(size):
            if self.storage_guard is not None:
                try:
                    self.storage_guard.check(size)
                except StorageGuardError as exc:
                    raise FleetRagError(str(exc)) from None

        try:
            options = {"before_attempt": check_storage} if write else {}
            return http_json(
                f"{self.base}{path}",
                body,
                {"api-key": key},
                method,
                timeout=timeout,
                retries=retries,
                **options,
            )
        except FleetRagError as e:
            # A staged read-only key that the server has not loaded yet answers 401/403.
            # Fall back to the write key for the rest of this process instead of failing reads.
            if (
                not write
                and self._write_key
                and key != self._write_key
                and ("HTTP 401" in str(e) or "HTTP 403" in str(e))
            ):
                self._read_key = self._write_key
                return http_json(
                    f"{self.base}{path}",
                    body,
                    {"api-key": self._write_key},
                    method,
                    timeout=timeout,
                    retries=retries,
                )
            raise

    def _cpath(self, suffix: str = "") -> str:
        return f"/collections/{self.collection}{suffix}"

    # -- reads
    def healthz(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/healthz", timeout=30) as r:
                return r.status == 200
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            return False

    def info(self) -> dict:
        return self._call(self._cpath())["result"]

    def create_payload_index(self, field_name: str, field_schema: str) -> None:
        """Create one payload index; Qdrant treats an identical existing index as a no-op."""
        self._call(
            self._cpath("/index"),
            {"field_name": field_name, "field_schema": field_schema},
            method="PUT",
            write=True,
        )

    def count(self, flt: dict | None = None, exact: bool = True) -> int:
        body: dict[str, Any] = {"exact": exact}
        if flt:
            body["filter"] = flt
        return self._call(self._cpath("/points/count"), body)["result"]["count"]

    def retrieve(
        self, point_id: str, *, with_payload: Any = True, with_vector: bool = False
    ) -> dict | None:
        """One point by id, or None when it does not exist."""
        import urllib.parse

        path = self._cpath("/points/" + urllib.parse.quote(point_id, safe=""))
        if with_payload is not True or with_vector:
            path += "?" + urllib.parse.urlencode(
                {"with_payload": str(with_payload).lower(), "with_vector": str(with_vector).lower()}
            )
        try:
            response = self._call(path, method="GET")
        except FleetRagError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        if not isinstance(response, dict):
            raise FleetRagError("Qdrant retrieve returned a malformed response")
        point = response.get("result")
        if not isinstance(point, dict):
            raise FleetRagError("Qdrant retrieve returned a malformed point")
        if not isinstance(point.get("id"), str) or not point["id"]:
            raise FleetRagError("Qdrant retrieve returned a point without a valid id")
        if point.get("payload") is not None and not isinstance(point["payload"], dict):
            raise FleetRagError("Qdrant retrieve returned an invalid payload")
        if "vector" in point and not isinstance(point["vector"], (list, dict)):
            raise FleetRagError("Qdrant retrieve returned an invalid vector")
        return point

    def scroll(
        self,
        flt: dict | None = None,
        limit: int = 256,
        with_payload: Any = True,
        with_vector: bool = False,
    ):
        """Yield points matching the filter, paging through the collection."""
        offset = None
        while True:
            body: dict[str, Any] = {
                "limit": limit,
                "with_payload": with_payload,
                "with_vector": with_vector,
            }
            if flt:
                body["filter"] = flt
            if offset is not None:
                body["offset"] = offset
            response = self._call(self._cpath("/points/scroll"), body)
            if not isinstance(response, dict):
                raise FleetRagError("Qdrant scroll returned a malformed response")
            res = response.get("result")
            if not isinstance(res, dict):
                raise FleetRagError("Qdrant scroll returned a malformed result")
            points = res.get("points", [])
            if not isinstance(points, list):
                raise FleetRagError("Qdrant scroll returned malformed points")
            for point in points:
                if not isinstance(point, dict):
                    raise FleetRagError("Qdrant scroll returned a malformed point")
                yield point
            offset = res.get("next_page_offset")
            if offset is None:
                break

    def search_dense(
        self, vector: list[float], limit: int = 5, flt: dict | None = None
    ) -> list[dict]:
        body: dict[str, Any] = {"vector": vector, "limit": limit, "with_payload": True}
        if flt:
            body["filter"] = flt
        return self._call(self._cpath("/points/search"), body)["result"]

    def _prefetch(
        self, vector: list[float], terms: list[str], flt: dict | None, pl: int, prefer_lessons: bool
    ) -> list[dict]:
        """Prefetch list shared by query_hybrid and query_groups.

        1. plain dense; 2. dense restricted to points whose full-text index matches any keyword
        term (only when there are terms); 3. dense restricted to agent-contribution lessons
        (only when prefer_lessons).  RRF fusion of these means a matching lesson always enters
        the fused list near the top, and keyword hits are not drowned by pure-vector neighbours.
        """
        prefetch: list[dict] = [{"query": vector, "limit": pl, **({"filter": flt} if flt else {})}]
        if terms:
            kw_filter: dict[str, Any] = {
                "should": [{"key": "text", "match": {"text": t}} for t in terms]
            }
            if flt:
                kw_filter = {"must": [flt, kw_filter]}
            prefetch.append({"query": vector, "limit": pl, "filter": kw_filter})
        if prefer_lessons:
            prefetch.append(
                {
                    "query": vector,
                    "limit": pl,
                    "filter": lesson_filter(flt),
                    "score_threshold": LESSON_SCORE_THRESHOLD,
                }
            )
        return prefetch

    def query_hybrid(
        self,
        vector: list[float],
        terms: list[str],
        limit: int = 5,
        flt: dict | None = None,
        prefetch_limit: int | None = None,
        prefer_lessons: bool = True,
    ) -> list[dict]:
        """Dense + keyword (+ lesson) fusion via the Query API.

        Prefetches (see _prefetch) fused with reciprocal rank fusion.  Falls back to plain
        dense search when there are no usable terms and no lesson boost was requested.
        """
        if not terms and not prefer_lessons:
            return self.search_dense(vector, limit, flt)
        pl = prefetch_limit or max(limit * 4, 20)
        body = {
            "prefetch": self._prefetch(vector, terms, flt, pl, prefer_lessons),
            "query": {"fusion": "rrf"},
            "limit": limit,
            "with_payload": True,
        }
        return self._call(self._cpath("/points/query"), body)["result"]["points"]

    def query_groups(
        self,
        vector: list[float],
        terms: list[str],
        limit: int = 5,
        flt: dict | None = None,
        group_by: str = "doc_id",
        group_size: int = 1,
        prefetch_limit: int | None = None,
        prefer_lessons: bool = True,
    ) -> list[dict]:
        """Same fusion as query_hybrid, but one group per `group_by` value (Qdrant >= 1.19).

        Returns up to `limit` groups as dicts {"id": <group value>, "hits": [points...]} in
        fused order; each group holds at most `group_size` of that document's chunks, best
        first.  Uses POST /collections/{c}/points/query/groups.
        """
        pl = prefetch_limit or max(limit * 4, 20)
        body = {
            "prefetch": self._prefetch(vector, terms, flt, pl, prefer_lessons),
            "query": {"fusion": "rrf"},
            "limit": limit,
            "group_by": group_by,
            "group_size": max(1, group_size),
            "with_payload": True,
        }
        res = self._call(self._cpath("/points/query/groups"), body)["result"]
        return [{"id": g.get("id"), "hits": list(g.get("hits", []))} for g in res.get("groups", [])]

    # -- writes
    def upsert(self, points: list[dict], wait: bool = True) -> str:
        if not points:
            return "ok"
        res = self._call(
            self._cpath(f"/points?wait={'true' if wait else 'false'}"),
            {"points": points},
            method="PUT",
            write=True,
            timeout=300,
        )
        return res.get("status", "?")

    def delete_by_filter(self, flt: dict, wait: bool = True) -> str:
        res = self._call(
            self._cpath(f"/points/delete?wait={'true' if wait else 'false'}"),
            {"filter": flt},
            method="POST",
            write=True,
            timeout=300,
        )
        return res.get("status", "?")

    def delete_ids(self, ids: list[str], wait: bool = True) -> str:
        if not ids:
            return "ok"
        res = self._call(
            self._cpath(f"/points/delete?wait={'true' if wait else 'false'}"),
            {"points": ids},
            method="POST",
            write=True,
            timeout=300,
        )
        return res.get("status", "?")

    def set_payload(self, ids: list[str], payload: dict, wait: bool = True) -> str:
        res = self._call(
            self._cpath(f"/points/payload?wait={'true' if wait else 'false'}"),
            {"points": ids, "payload": payload},
            method="POST",
            write=True,
        )
        return res.get("status", "?")

    def create_snapshot(self, *, wait: bool = True) -> dict:
        query = "?wait=true" if wait else ""
        return self._call(
            self._cpath(f"/snapshots{query}"),
            {},
            method="POST",
            write=True,
            timeout=1800,
            retries=0,
        )["result"]

    def snapshot(self) -> dict:
        """Alias for create_snapshot (wait=true)."""
        return self.create_snapshot(wait=True)

    def list_snapshots(self) -> list[dict]:
        return list(self._call(self._cpath("/snapshots"), method="GET")["result"])

    def delete_snapshot(self, name: str) -> None:
        import urllib.parse

        encoded = urllib.parse.quote(name, safe="")
        self._call(self._cpath(f"/snapshots/{encoded}"), method="DELETE", write=True, timeout=120)


# --------------------------------------------------------------------------- filters


def match_filter(**fields: str | None) -> dict | None:
    """Exact-match filter on payload fields; None values are skipped."""
    must = [{"key": k, "match": {"value": v}} for k, v in fields.items() if v]
    return {"must": must} if must else None


def lesson_filter(flt: dict | None = None) -> dict:
    """Filter for the lesson prefetch: agent contributions in LESSON_CATEGORIES, AND the caller's
    filter when there is one (so category/app/seat/since_days restrictions still apply)."""
    must: list[dict] = [
        {"key": "source", "match": {"value": LESSON_SOURCE}},
        {"key": "category", "match": {"any": list(LESSON_CATEGORIES)}},
    ]
    if flt:
        must.append(flt)
    return {"must": must}


def build_point(text: str, payload: dict) -> dict:
    """Payload plus deterministic id; the caller supplies the vector afterwards."""
    digest = content_hash(text)
    return {
        "id": point_id(digest),
        "payload": {
            **payload,
            "text": text,
            "content_hash": digest,
            "embed_model": EMBED_MODEL_TAG,
        },
    }


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)
