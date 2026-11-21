"""Per-seat GitHub App tokens, minted and refreshed by the relay.

Seats never see an App id, private key, or the 1-hour installation-token
expiry:  the relay reads each seat App's credentials (GH_APP_<SLUG>_ID and
GH_APP_<SLUG>_PRIVATE_KEY, prod /github-apps) through the canonical bounded
Infisical reader and each source's own protected token file, then signs a JWT with the local openssl
binary (stdlib has no RSA), and caches installation tokens in memory,
refreshing 5 minutes early.  The private key reaches openssl through an inherited pipe fd,
so nothing is written to disk and no secret is ever logged or returned beyond
the minted token a seat is allowed to use.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

TIMEOUT_SECONDS = 30
REFRESH_MARGIN_SECONDS = 300
MINT_PERMISSION_CEILING = (
    "contents",
    "workflows",
    "pull_requests",
    "issues",
    "actions",
    "checks",
    "actions_variables",
    "statuses",
    "deployments",
    "metadata",
)

# Permission keys mintable only at read level.  These widen what the relay can
# READ (repository webhooks, deploy keys, organization installations); the
# level check below makes a write mint for any of them a hard error, so no
# verb or seat can ever receive a write-scoped token for them.
MINT_READ_ONLY_PERMISSIONS = (
    "administration",
    "repository_hooks",
    "organization_administration",
)


# Exact permission objects for create_repo.  Equality-checked.  These writes are
# outside the seat mint ceiling on purpose: token_for rejects them, and only
# token_for_namespace may request them.  No repository list is sent, so the
# token can see and create under that one account.  It is not cached.
_NAMESPACE_CREATE_PERMISSIONS = {
    "org": {"metadata": "read", "organization_administration": "write"},
    "user": {"metadata": "read", "administration": "write"},
}


def mint_permission_allowed(key, level):
    """True when a mint request for (key, level) stays inside the ceiling.

    Keys in MINT_PERMISSION_CEILING mint at the requested level exactly as
    before; keys in MINT_READ_ONLY_PERMISSIONS mint only at read level.
    """
    if key in MINT_PERMISSION_CEILING:
        return level in ("read", "write")
    return key in MINT_READ_ONLY_PERMISSIONS and level == "read"


class InstallationReadError(Exception):
    """Safe status/retry-only read failure, never upstream prose or credentials."""

    def __init__(self, status, retry_after=None):
        super().__init__("installation metadata read failed")
        self.status = status if type(status) is int and 0 <= status <= 599 else 0
        self.retry_after = (
            retry_after if type(retry_after) is int and 0 <= retry_after <= 86400 else None
        )


class AppTokenError(Exception):
    """A mint or credential lookup failed;  never carries secret material."""

    def __init__(self, message, *, reason=None, failure_kind=None):
        self.reason = reason if reason == "token-expired-or-invalid" else None
        self.failure_kind = failure_kind if failure_kind in ("refused", "unavailable") else None
        detail = [item for item in (self.reason, self.failure_kind) if item is not None]
        super().__init__(message + (": " + " / ".join(detail) if detail else ""))


# Messages that mean this seat's App material is missing or unusable, not a
# transient lookup or an upstream mint glitch.  Matched on the fixed prefixes
# AppTokenError uses.  The HTTP answer is the closed reason, never this text.
# A missing, suspended or mismatched installation is not here:  the id and key are
# fine and adding them again would not help, so it stays a 503 that says so.  A
# slug or repo that fails the shape check ("invalid installation lookup") is not a
# credential problem either.
_NO_APP_CREDENTIAL_EXACT = frozenset(
    {
        "app key belongs to a different app",
        "own App identity unverifiable",
    }
)


def missing_app_credential(exc: BaseException) -> bool:
    """True when the mint failed because the seat's own App id or key is absent or unusable."""
    text = str(exc)
    if text in _NO_APP_CREDENTIAL_EXACT:
        return True
    if text.startswith(("Infisical has no value for ", "fleet-secrets has no value for ")):
        return True
    if text.startswith(
        ("Infisical returned an empty value for ", "fleet-secrets returned an empty value for ")
    ):
        return True
    if text.startswith("app id for ") and text.endswith(" is not an integer"):
        return True
    return False


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def sign_jwt(app_id: int, pem: str, now: int) -> str:
    unsigned = b".".join(
        [
            _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode()),
            _b64(
                json.dumps(
                    {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")
                ).encode()
            ),
        ]
    )
    # The key never touches the filesystem:  it goes to openssl through an
    # inherited pipe fd (/dev/fd/N), written before the child starts - a PEM
    # is well under the pipe buffer, so the write cannot block.
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, pem.encode())
    finally:
        os.close(write_fd)
    try:
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"],
                input=unsigned,
                capture_output=True,
                timeout=10,
                pass_fds=(read_fd,),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise AppTokenError(f"app signer unavailable: {type(exc).__name__}") from None
        if result.returncode:
            raise AppTokenError("app signing failed")
        return (unsigned + b"." + _b64(result.stdout)).decode()
    finally:
        os.close(read_fd)


# Keep the established folder syntax while the shared reader proves its exact grant and rows.
_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]*")
_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
_CLIENT_LOCK = threading.RLock()
_CLIENT_CACHE = None
_CLIENT_NAME = "_git_relay_fleet_infisical"
_MAX_CLIENT_BYTES = 2 * 1024 * 1024
_MAX_TOKEN_BYTES = 8194
_MAX_BATCH_BYTES = 1024 * 1024


def _shared_client():
    """Execute only the fixed canonical sibling source, never an import-path candidate."""
    global _CLIENT_CACHE
    expected = Path(__file__).resolve().parents[2] / "scripts/fleet_infisical.py"
    with _CLIENT_LOCK:
        helper = None
        try:
            directory_fd = os.open(expected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                directory = os.fstat(directory_fd)
                fd = os.open(
                    expected.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CLIENT_BYTES:
                        raise ValueError()
                    raw = source.read(_MAX_CLIENT_BYTES + 1)
                    after = os.fstat(source.fileno())
                current = os.stat(expected.name, dir_fd=directory_fd, follow_symlinks=False)
                current_directory = expected.parent.lstat()
                if (directory.st_dev, directory.st_ino) != (
                    current_directory.st_dev,
                    current_directory.st_ino,
                ):
                    raise ValueError()
            finally:
                os.close(directory_fd)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            identities = [
                tuple(getattr(info, key) for key in fields) for info in (before, after, current)
            ]
            if len(raw) > _MAX_CLIENT_BYTES or not identities[0] == identities[1] == identities[2]:
                raise ValueError()
            identity = (str(expected), identities[0], hashlib.sha256(raw).digest())
            if _CLIENT_CACHE is not None and _CLIENT_CACHE[0] == identity:
                sys.modules[_CLIENT_NAME] = _CLIENT_CACHE[1]
                return _CLIENT_CACHE[1]
            spec = importlib.util.spec_from_file_location(_CLIENT_NAME, expected)
            if spec is None or spec.loader is None:
                raise ValueError()
            helper = importlib.util.module_from_spec(spec)
            sys.modules[_CLIENT_NAME] = helper
            exec(compile(raw, str(expected), "exec"), vars(helper))
            if not callable(helper.bounded_fetch_project):
                raise ValueError()
            _CLIENT_CACHE = (identity, helper)
            return helper
        except BaseException as exc:
            cached = None if _CLIENT_CACHE is None else _CLIENT_CACHE[1]
            _CLIENT_CACHE = None
            for owned in (helper, cached):
                if owned is not None and sys.modules.get(_CLIENT_NAME) is owned:
                    del sys.modules[_CLIENT_NAME]
            if isinstance(exc, Exception):
                raise AppTokenError("canonical Infisical reader unavailable") from None
            raise


class FleetSecretsSource:
    """The existing source protocol, backed by one canonical bounded named reader.

    Every source uses its own explicit protected token file and project declaration.  Only
    prod and its exact folder are passed to the reader; no ambient token, HOME lookup,
    external application or other consumer's binding can supply credentials.  Verified
    values and confirmed missing names retain the existing per-source process cache.
    """

    def __init__(self, path="/github-apps", credential_file=None, environ=os.environ, reader=None):
        if not _PATH_RE.fullmatch(path or "") or any(p in (".", "..") for p in path.split("/")):
            raise AppTokenError("Infisical source folder is invalid")
        self._credential_file = credential_file
        self._environ = environ
        self._reader = reader  # Explicit dependency injection; production factories do not set it.
        self._path = path
        self._cache: dict[str, str] = {}
        self._missing: set[str] = set()

    @staticmethod
    def _names(names):
        names = list(names)
        if any(not isinstance(n, str) or not _NAME_RE.fullmatch(n) for n in names):
            raise AppTokenError("Infisical secret name is invalid")
        return list(dict.fromkeys(names))

    def _token(self):
        if not self._credential_file:
            raise AppTokenError(f"credential file binding required for {self._path}")
        try:
            fd = os.open(self._credential_file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid not in (0, os.geteuid())
                    or stat.S_IMODE(before.st_mode) & 0o077
                    or before.st_size > _MAX_TOKEN_BYTES
                ):
                    raise ValueError()
                raw = source.read(_MAX_TOKEN_BYTES + 1)
                after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            identities = [
                tuple(getattr(info, key) for key in fields)
                for info in (before, after, os.stat(self._credential_file, follow_symlinks=False))
            ]
            if len(raw) > _MAX_TOKEN_BYTES or not identities[0] == identities[1] == identities[2]:
                raise ValueError()
            token = raw.decode("utf-8").strip()
        except (OSError, ValueError, UnicodeError):
            raise AppTokenError(f"credential file unreadable for {self._path}") from None
        if not token:
            raise AppTokenError(f"credential file empty for {self._path}")
        return token

    def _read(self, wanted):
        # Pin and execute trusted code before opening the binding or passing any token.
        client = _shared_client()
        reader = self._reader if self._reader is not None else client.bounded_fetch_project
        declared = self._environ.get("INFISICAL_FLEET_OPS_PROJECT_ID")
        project = declared.strip() if isinstance(declared, str) else ""
        if not project:
            raise AppTokenError("INFISICAL_FLEET_OPS_PROJECT_ID is required for this source")
        try:
            # Nonempty explicit input makes the canonical resolver's project-file fallback unreachable.
            project = client.infisical_project_id(
                {"INFISICAL_FLEET_OPS_PROJECT_ID": project}, Path("/")
            )
        except client.SecretsError:
            raise AppTokenError("INFISICAL_FLEET_OPS_PROJECT_ID must be a UUID") from None
        token = self._token()
        try:
            values = reader(
                project,
                self._path,
                {"INFISICAL_ENVIRONMENT": "prod", "INFISICAL_READ_PATHS": self._path},
                token=token,
                names=tuple(wanted),
                timeout=TIMEOUT_SECONDS,
                max_output_bytes=_MAX_BATCH_BYTES,
                strict_records=True,
            )
        except TimeoutError:
            raise AppTokenError(
                f"Infisical lookup timed out for {wanted[0]}", failure_kind="unavailable"
            ) from None
        except client.SecretsError as exc:
            raise AppTokenError(
                f"Infisical lookup failed for {wanted[0]}",
                reason=exc.reason,
                failure_kind=exc.failure_kind,
            ) from None
        except Exception:
            raise AppTokenError(f"Infisical lookup failed for {wanted[0]}") from None
        finally:
            token = None
        if not isinstance(values, dict) or any(
            n not in wanted or not isinstance(v, str) for n, v in values.items()
        ):
            raise AppTokenError(f"Infisical returned unreadable output for {wanted[0]}")
        return values

    def _fetch(self, wanted, *, verified=False):
        values = self._read(wanted)
        missing = [name for name in wanted if name not in values]
        if missing:
            self._missing.update(missing)
            raise AppTokenError(f"Infisical has no value for {missing[0]}")
        normalized = {
            name: values[name].rstrip("\n") if verified else values[name].strip() for name in wanted
        }
        for name, value in normalized.items():
            if not value:
                raise AppTokenError(f"Infisical returned an empty value for {name}")
        self._cache.update(normalized)

    def secret(self, name: str) -> str:
        return self.secrets([name])[name]

    def verified_secret(self, name: str) -> str:
        name = self._names([name])[0]
        if name in self._missing:
            raise AppTokenError(f"Infisical has no value for {name}")
        if name not in self._cache:
            self._fetch([name], verified=True)
        return self._cache[name]

    def secrets(self, names) -> dict:
        names = self._names(names)
        missing = [name for name in names if name in self._missing]
        if missing:
            raise AppTokenError(f"Infisical has no value for {missing[0]}")
        wanted = [name for name in names if name not in self._cache]
        if wanted:
            self._fetch(wanted)
        return {name: self._cache[name] for name in names}


class AppTokenMinter:
    """Mints and caches installation tokens per (app slug, repo, permissions)."""

    def __init__(self, source, api, signer=sign_jwt, clock=time.time):
        self._source = source
        self._api = api  # callable(method, path, bearer, payload=None) -> dict
        self._signer = signer
        self._clock = clock
        self._tokens: dict[str, tuple[float, str]] = {}
        # One lock per cache key, not one global lock:  a slow mint for one
        # (app, repo, permissions) triple must not head-of-line block every
        # other seat's unrelated mints on the ThreadingHTTPServer.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _app_credentials(self, slug: str) -> tuple[int, str]:
        prefix = "GH_APP_" + slug.upper().replace("-", "_")
        # One bounded named-read operation per App pair, never a whole-folder export.
        pair = self._source.secrets([prefix + "_ID", prefix + "_PRIVATE_KEY"])
        try:
            app_id = int(pair[prefix + "_ID"].strip())
        except ValueError:
            raise AppTokenError(f"app id for {slug} is not an integer") from None
        return app_id, pair[prefix + "_PRIVATE_KEY"]

    def installation_metadata(self, slug: str, repo: str) -> dict:
        """Own-App accepted installation metadata only. No access token is minted."""
        if not re.fullmatch(r"swu-[a-z0-9-]{1,90}", slug) or not re.fullmatch(
            r"(?!.*\.\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo
        ):
            raise AppTokenError("invalid installation lookup")
        app_id, pem = self._app_credentials(slug)
        bearer = self._signer(app_id, pem, int(self._clock()))
        del pem
        try:
            app = self._api("GET", "/app", bearer)
            if (
                not isinstance(app, dict)
                or type(app.get("id")) is not int
                or app["id"] != app_id
                or app.get("slug") != slug
            ):
                raise AppTokenError("own App identity unverifiable")
            owner, name = repo.split("/", 1)
            install = self._api("GET", f"/repos/{owner}/{name}/installation", bearer)
            if not isinstance(install, dict):
                raise AppTokenError("installation metadata unverifiable")
            account = install.get("account")
            permissions = install.get("permissions")
            iid = install.get("id")
            suspended = install.get("suspended_at")
            if (
                type(iid) is not int
                or not 0 < iid <= 9007199254740991
                or type(install.get("app_id")) is not int
                or install.get("app_id") != app_id
                or install.get("app_slug") != slug
                or not isinstance(account, dict)
                or type(account.get("id")) is not int
                or not 0 < account["id"] <= 9007199254740991
                or not isinstance(account.get("login"), str)
                or account["login"].casefold() != owner.casefold()
                or not isinstance(permissions, dict)
                or len(permissions) > 256
                or any(
                    not isinstance(k, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", k)
                    or v not in ("read", "write")
                    for k, v in permissions.items()
                )
                or "suspended_at" not in install
                or (
                    suspended is not None
                    and (
                        not isinstance(suspended, str)
                        or not re.fullmatch(r"[0-9]{4}-[0-9TZ:.+\-]{10,40}", suspended)
                    )
                )
            ):
                raise AppTokenError("installation metadata unverifiable")
            return {
                "installation": {
                    "app_slug": slug,
                    "installation_id": iid,
                    "account": {"id": account["id"], "login": account["login"]},
                    "accepted_permissions": dict(permissions),
                    "suspended": suspended is not None,
                }
            }
        except AppTokenError:
            raise
        except Exception as exc:
            from .github_client import RelayGitHubError

            if isinstance(exc, RelayGitHubError):
                if exc.code == "repository_moved":
                    raise
                raise InstallationReadError(exc.status, exc.retry_after) from None
            raise AppTokenError("installation metadata unavailable") from None
        finally:
            del bearer

    def moved_repository_name(self, slug: str, repo_id: int) -> str | None:
        """The current owner/name of a repository GitHub said moved, read as the seat's own App.

        Read-only and least-privilege:  the App's installation for that repository id, one
        installation token limited to that single repository and ``metadata: read``, one GET of
        the repository, then the token is revoked.  The token never leaves this method.  Any
        failure answers None:  the caller still reports the move, only without the new name.
        """
        if (
            not re.fullmatch(r"swu-[a-z0-9-]{1,90}", slug)
            or type(repo_id) is not int
            or not 0 < repo_id <= 2**53 - 1
            or not mint_permission_allowed("metadata", "read")
        ):
            return None
        token = None
        try:
            app_id, pem = self._app_credentials(slug)
            bearer = self._signer(app_id, pem, int(self._clock()))
            del pem
            install = self._api("GET", f"/repositories/{repo_id}/installation", bearer)
            iid = install.get("id") if isinstance(install, dict) else None
            if (
                type(iid) is not int
                or iid <= 0
                or install.get("app_id") != app_id
                or install.get("suspended_at")
            ):
                return None
            data = self._api(
                "POST",
                f"/app/installations/{iid}/access_tokens",
                bearer,
                {"repository_ids": [repo_id], "permissions": {"metadata": "read"}},
            )
            del bearer
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                token = None
                return None
            repo = self._api("GET", f"/repositories/{repo_id}", token)
            if not isinstance(repo, dict):
                return None
            full_name = repo.get("full_name")
            if (
                repo.get("id") != repo_id
                or not isinstance(full_name, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/(?!.*\.\.)[A-Za-z0-9_.-]{1,100}", full_name
                )
            ):
                return None
            return full_name
        except Exception:
            return None
        finally:
            if token:
                try:
                    self._api("DELETE", "/installation/token", token)
                except Exception:
                    pass  # it expires within the hour regardless

    def token_for(self, slug: str, repo: str, permissions: dict) -> tuple[str, float]:
        """Mint (or serve from cache) an installation token;  returns (token, expiry epoch)."""
        for key, level in permissions.items():
            if not mint_permission_allowed(key, level):
                raise AppTokenError(
                    f"permission {key!r} at level {level!r} is outside the mint ceiling"
                )
        cache_key = json.dumps(
            [slug, repo.casefold(), sorted(permissions.items())], separators=(",", ":")
        )
        # The check-then-mint runs under this key's lock:  two concurrent
        # calls for the same key must not both run the whole mint (openssl +
        # three GitHub calls), while different keys proceed in parallel.
        with self._locks_guard:
            lock = self._locks.setdefault(cache_key, threading.Lock())
        with lock:
            now = self._clock()
            cached = self._tokens.get(cache_key)
            if cached and cached[0] - now > REFRESH_MARGIN_SECONDS:
                return cached[1], cached[0]
            app_id, pem = self._app_credentials(slug)
            bearer = self._signer(app_id, pem, int(now))
            del pem
            app = self._api("GET", "/app", bearer)
            if not isinstance(app, dict) or app.get("id") != app_id:
                raise AppTokenError("app key belongs to a different app")
            owner, name = repo.split("/", 1)
            install = self._api("GET", f"/repos/{owner}/{name}/installation", bearer)
            if not isinstance(install, dict):
                raise AppTokenError("installation lookup was malformed")
            iid = install.get("id")
            if (
                not isinstance(iid, int)
                or iid <= 0
                or install.get("suspended_at")
                or str((install.get("account") or {}).get("login", "")).casefold()
                != owner.casefold()
            ):
                raise AppTokenError("app installation is missing, suspended, or on another account")
            data = self._api(
                "POST",
                f"/app/installations/{iid}/access_tokens",
                bearer,
                {"repositories": [name], "permissions": permissions},
            )
            if not isinstance(data, dict):
                raise AppTokenError("minted-token response was malformed")
            token = data.get("token")
            expires_at = str(data.get("expires_at") or "")
            if not isinstance(token, str) or not token or not expires_at:
                raise AppTokenError("minted-token response was malformed")
            import datetime as dt

            try:
                expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                raise AppTokenError("minted-token expiry was malformed") from None
            self._tokens[cache_key] = (expiry, token)
            return token, expiry

    def token_for_namespace(self, slug: str, owner: str, kind: str) -> str:
        """Mint one uncached installation token for creating a repository.

        The permission object is the fixed pair for `kind`.  A caller cannot
        pass its own permissions.  The token is not stored on this minter.
        """
        permissions = _NAMESPACE_CREATE_PERMISSIONS.get(kind)
        if (
            permissions is None
            or not re.fullmatch(r"swu-[a-z0-9-]{1,90}", slug)
            or not isinstance(owner, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        ):
            raise AppTokenError("invalid namespace mint")
        app_id, pem = self._app_credentials(slug)
        bearer = self._signer(app_id, pem, int(self._clock()))
        del pem
        token = None
        try:
            app = self._api("GET", "/app", bearer)
            if not isinstance(app, dict) or app.get("id") != app_id:
                raise AppTokenError("app key belongs to a different app")
            if kind == "org":
                path = f"/orgs/{quote(owner, safe='')}/installation"
                expected_type = "Organization"
            else:
                path = f"/users/{quote(owner, safe='')}/installation"
                expected_type = "User"
            install = self._api("GET", path, bearer)
            if not isinstance(install, dict):
                raise AppTokenError("namespace mint failed")
            account = install.get("account") if isinstance(install.get("account"), dict) else {}
            iid = install.get("id")
            if (
                type(iid) is not int
                or iid <= 0
                or install.get("app_id") != app_id
                or install.get("suspended_at")
                or account.get("type") != expected_type
                or str(account.get("login", "")).casefold() != owner.casefold()
            ):
                raise AppTokenError("app installation is missing, suspended, or on another account")
            data = self._api(
                "POST",
                f"/app/installations/{iid}/access_tokens",
                bearer,
                {"permissions": dict(permissions)},
            )
            if not isinstance(data, dict):
                raise AppTokenError("namespace mint failed")
            minted = data.get("token")
            if not isinstance(minted, str) or not minted:
                raise AppTokenError("namespace mint failed")
            token = minted
            issued = token
            token = None
            return issued
        except AppTokenError:
            raise
        except Exception:
            raise AppTokenError("namespace mint failed") from None
        finally:
            del bearer
            if token:
                self.revoke_installation_token(token)

    def revoke_installation_token(self, token: str) -> None:
        """Revoke one installation token.  A failure does not change the call result."""
        if not isinstance(token, str) or not token:
            return
        try:
            self._api("DELETE", "/installation/token", token)
        except Exception:
            return
