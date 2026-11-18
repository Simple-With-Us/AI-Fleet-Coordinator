"""Per-seat GitHub App tokens, minted and refreshed by the relay.

Seats never see an App id, private key, or the 1-hour installation-token
expiry:  the relay reads each seat App's credentials (GH_APP_<SLUG>_ID and
GH_APP_<SLUG>_PRIVATE_KEY, prod /github-apps) through the SHARED Infisical
client and the relay's own scoped service token - owner decision 2026-10-21:
no process-environment secrets, and the one client the fleet reads secrets
with rather than a reader of the relay's own - signs a JWT with the local
openssl binary (stdlib has no RSA), and caches installation tokens in memory,
refreshing 5 minutes early.  The private key reaches openssl through an inherited pipe fd,
so nothing is written to disk and no secret is ever logged or returned beyond
the minted token a seat is allowed to use.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
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
    if text.startswith("there is no value for "):
        return True
    if text.startswith("the store returned an empty value for "):
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


# The shared client's own folder-path rules (scripts/fleet_infisical.py
# check_path):  anything weaker here would pass a bad path down and surface as a
# misleading missing-key error.
_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]*")

# Where the shared fleet Infisical client lives on this host.  The relay runs
# from /opt/git-relay/git-relay, and the client is installed beside it in
# /opt/git-relay/scripts.  GIT_RELAY_INFISICAL_CLIENT names one file outright;
# GIT_RELAY_INFISICAL_CLIENT_DIRS names a comma-separated list of directories.
_CLIENT_ENV = "GIT_RELAY_INFISICAL_CLIENT"
_CLIENT_DIRS_ENV = "GIT_RELAY_INFISICAL_CLIENT_DIRS"
_DEFAULT_CLIENT_DIRS = ("/opt/git-relay/scripts",)

# The loaded client module, cached so one process loads it once.
_CLIENT = None


def _client_candidates():
    """The shared client file paths this host may carry, in order."""
    explicit = (os.environ.get(_CLIENT_ENV) or "").strip()
    if explicit:
        yield Path(explicit)
        return
    raw = (os.environ.get(_CLIENT_DIRS_ENV) or "").strip()
    if raw:
        directories = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        directories = list(_DEFAULT_CLIENT_DIRS)
    # A checkout runs the client from the repository's own scripts/ directory.
    directories.append(str(Path(__file__).resolve().parent.parent.parent / "scripts"))
    for directory in directories:
        yield Path(directory) / "fleet_infisical.py"


def load_infisical_client():
    """Import the shared fleet Infisical client, or fail closed.

    The relay holds no second reader of its own:  the shared client is the one
    implementation the whole fleet reads secrets with, and this host installs a
    copy of it.  A missing copy is a startup error, never a silent fallback to
    an ambient token.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    for path in _client_candidates():
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location("fleet_infisical", path)
        except OSError:
            continue
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["fleet_infisical"] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop("fleet_infisical", None)
            raise AppTokenError("the shared Infisical client could not be loaded") from None
        _CLIENT = module
        return module
    raise AppTokenError("the shared Infisical client is not installed on this host")


class InfisicalSource:
    """Reads named secrets through the shared Infisical client.

    The relay's own scoped service token is the only credential:  it is read
    from the protected credential file the unit binds (systemd LoadCredential),
    never from the process environment, and the token's own grant is verified
    before any value is used, so a token clamped to another folder fails closed
    instead of answering for the wrong scope.

    `secrets()` reads several names under ONE grant check and ONE folder
    request:  an App's id and private key must be fetched together, because a
    per-name call would re-verify the grant for every name.  A name the store
    does not hold is remembered for the process lifetime, so an incomplete App
    pair does not drive a folder read per request;  add the secret and restart
    the relay to pick it up.

    `path` is the Fleet-OPS prod folder the names are read from.  It defaults
    to /github-apps, where the GH_APP_<SLUG>_ID and GH_APP_<SLUG>_PRIVATE_KEY
    pairs actually live (verified live 2026-10-23).

    The value never appears in a message or a log:  an error names the key, the
    folder or the refusal, and nothing else.
    """

    def __init__(
        self,
        path: str = "/github-apps",
        credential_file: str | None = None,
        environ=os.environ,
        client=None,
        project: str | None = None,
    ):
        if not _PATH_RE.fullmatch(path or "") or ".." in path.split("/"):
            raise AppTokenError(f"secret folder path is not a usable folder path: {path!r}")
        self._path = path
        self._credential_file = credential_file
        self._environ = environ
        self._client = client
        self._project = project
        self._cache: dict[str, str] = {}
        self._missing: set[str] = set()
        self._scoped_env: dict | None = None
        self._token = ""

    def _module(self):
        if self._client is None:
            self._client = load_infisical_client()
        return self._client

    def _read_token(self) -> str:
        """The one scoped service token this source runs under."""
        if self._credential_file:
            try:
                with open(self._credential_file, "r", encoding="utf-8") as handle:
                    token = handle.read().strip()
            except OSError:
                raise AppTokenError(f"credential file unreadable for {self._path}") from None
            if not token:
                raise AppTokenError(f"credential file empty for {self._path}")
            return token
        token = (self._environ.get("INFISICAL_TOKEN") or "").strip()
        if not token:
            raise AppTokenError(f"no scoped service token is bound for {self._path}")
        return token

    def _scope(self):
        """The env the client reads with, declaring exactly this token's grant.

        The declared folder set is the token's own scope, read from the store:
        the client refuses a read whose declared set is not the grant's set, so
        declaring the grant is what makes a clamped token fail closed instead
        of silently serving the wrong folder.
        """
        if self._scoped_env is None:
            client = self._module()
            env = dict(self._environ)
            if not (env.get(client.ENV_PROJECT) or "").strip():
                raise AppTokenError(
                    f"{client.ENV_PROJECT} is not set:  the relay's Infisical project is not bound"
                )
            token = self._read_token()
            try:
                paths = client.token_scopes(token, env)
            except client.SecretsError:
                raise AppTokenError(f"the bound service token refused {self._path}") from None
            if self._path not in paths:
                raise AppTokenError(f"the bound service token does not cover {self._path}")
            env[client.ENV_READ_PATHS] = ",".join(paths)
            self._scoped_env = env
            self._token = token
        return self._scoped_env

    def _project_id(self) -> str:
        if self._project:
            return self._project
        client = self._module()
        value = (self._scope().get(client.ENV_PROJECT) or "").strip()
        if not value:
            raise AppTokenError(
                f"{client.ENV_PROJECT} is not set:  the relay's Infisical project is not bound"
            )
        return value

    def secret(self, name: str) -> str:
        return self.secrets([name])[name]

    def verified_secret(self, name: str) -> str:
        """One folder-PROVEN secret.

        Unlike `secret`, the answer's own row must show it came from exactly
        this folder:  a legacy-scoped token whose grant clamps to another
        folder fails closed instead of returning the wrong scope's value.
        """
        if not name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise AppTokenError(f"not a usable secret name: {name!r}")
        if name in self._cache:
            return self._cache[name]
        if name in self._missing:
            raise AppTokenError(f"there is no value for {name}")
        client = self._module()
        env = self._scope()
        try:
            value = client.fetch_verified(name, self._path, env, token=self._token)
        except client.SecretsError as exc:
            if exc.code == 3:
                self._missing.add(name)
                raise AppTokenError(f"there is no value for {name}") from None
            raise AppTokenError(f"the store lookup failed for {name}") from None
        if not value:
            raise AppTokenError(f"the store returned an empty value for {name}")
        self._cache[name] = value
        return value

    def secrets(self, names) -> dict:
        """Read several names under one grant check and one folder request."""
        names = list(names)
        known_missing = [n for n in names if n in self._missing]
        if known_missing:
            raise AppTokenError(f"there is no value for {known_missing[0]}")
        wanted = [n for n in names if n not in self._cache]
        if wanted:
            self._fetch(wanted)
        return {n: self._cache[n] for n in names}

    def _fetch(self, wanted) -> None:
        client = self._module()
        env = self._scope()
        project = self._project_id()
        try:
            values = client.fetch_project(project, self._path, env, token=self._token, names=wanted)
        except client.SecretsError:
            raise AppTokenError(f"the store lookup failed for {wanted[0]}") from None
        for name in wanted:
            value = str(values.get(name) or "").strip()
            if not value:
                self._missing.add(name)
                raise AppTokenError(f"there is no value for {name}")
            self._cache[name] = value


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
        # One grant check and one folder request per App, not per name.
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
