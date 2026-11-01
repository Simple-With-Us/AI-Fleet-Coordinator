"""Per-seat GitHub App tokens, minted and refreshed by the relay.

Seats never see an App id, private key, or the 1-hour installation-token
expiry:  the relay reads each seat App's credentials (GH_APP_<SLUG>_ID and
GH_APP_<SLUG>_PRIVATE_KEY, prod /github-apps) through the `fleet-secrets`
CLI only - owner decision 2026-10-21:  no process-environment secrets and no
direct Infisical HTTP in the relay - signs a JWT with the local openssl
binary (stdlib has no RSA), and caches installation tokens in memory,
refreshing 5 minutes early.  The private key reaches openssl through an inherited pipe fd,
so nothing is written to disk and no secret is ever logged or returned beyond
the minted token a seat is allowed to use.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time

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


# The CLI's own folder-path rules (scripts/fleet_secrets/core.py check_path):
# anything weaker here would pass a bad path down and surface as a misleading
# missing-key error.
_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]*")

# The CLI's exit-3 line (scripts/fleet_secrets/cli.py cmd_get):
#   fleet-secrets: key NAME[, NAME ...] is not in Infisical at PATH
# Absent names are read from that list as exact tokens, never by substring,
# so a requested name that is a prefix of an absent one is not blamed too.
_ABSENT_RE = re.compile(r"\bkey (?P<names>[A-Za-z0-9_, ]+?) is not in Infisical at ")


def _absent_names(detail: str, wanted: list) -> list:
    """The requested names the CLI's exit-3 line lists;  the first name when
    no line parses (exit 3 always means one is absent:  fail closed).  Every
    stderr line is searched, so a warning printed before it changes nothing."""
    match = next(filter(None, map(_ABSENT_RE.search, detail.splitlines())), None)
    listed = {n.strip() for n in match.group("names").split(",")} if match else set()
    return [n for n in wanted if n in listed] or wanted[:1]


class FleetSecretsSource:
    """Reads named secrets through the `fleet-secrets` CLI.

    The only approved credential path for the relay:  no environment reads,
    no direct Infisical calls.  The CLI's own identity and backend are its
    concern;  this class runs it with a timeout, caches values in memory, and
    never logs or includes what it returns.

    `secrets()` reads several names in ONE CLI invocation:  `fleet-secrets
    get` exports the whole folder per call, so an App's id and private key
    must be fetched together - per-name calls would re-export the folder for
    every name.  A name the CLI reports absent (exit 3) is remembered for the
    process lifetime, so an incomplete App pair does not drive a full folder
    export per request;  add the secret and restart the relay to pick it up.

    `path` is the Fleet-OPS prod folder the names are read from.  It
    defaults to /github-apps, where the GH_APP_<SLUG>_ID and
    GH_APP_<SLUG>_PRIVATE_KEY pairs actually live (verified live
    2026-10-23):  without it `fleet-secrets get` reads the project root
    and every App-key lookup misses.

    `credential_file`, when set, names a protected file (systemd
    LoadCredential, root 0600) holding the ONE Infisical token for this
    source's folder scope:  it is read at call time and passed to the CLI
    child with an allowlist environment (INFISICAL_FLEET_OPS_PROJECT_ID
    required, INFISICAL_ENVIRONMENT=prod set, no HOME), so no co-located
    secret in the relay's own environment is ever inherited and the CLI
    cannot fall through to ~/.agent-sync file resolution.  When unset the
    child inherits the process environment - the legacy shape the fleet's
    other local tooling still uses.
    """

    def __init__(
        self,
        executable: str = "fleet-secrets",
        runner=subprocess.run,
        path: str = "/github-apps",
        credential_file: str | None = None,
        environ=os.environ,
    ):
        if not _PATH_RE.fullmatch(path or "") or ".." in path.split("/"):
            raise AppTokenError(f"fleet-secrets path is not a usable folder path: {path!r}")
        self._credential_file = credential_file
        self._environ = environ
        self._exe = executable
        self._run = runner
        self._path = path
        self._cache: dict[str, str] = {}
        self._missing: set[str] = set()

    def secret(self, name: str) -> str:
        return self.secrets([name])[name]

    def verified_secret(self, name: str) -> str:
        """One folder-PROVEN secret via `fleet-secrets get-verified`.

        Unlike `secret`, the CLI answers only when the REST raw-secret row's
        own secretPath is exactly this source's folder:  a legacy-scoped
        token whose --path clamps to another folder fails closed instead of
        returning the wrong scope's value.  Exit 3 (absent) is remembered for
        the process lifetime, like `secret`;  every other failure raises.
        """
        if not name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise AppTokenError(f"not a usable secret name: {name!r}")
        if name in self._cache:
            return self._cache[name]
        if name in self._missing:
            raise AppTokenError(f"fleet-secrets has no value for {name}")
        try:
            result = self._run_scoped([self._exe, "get-verified", name, "--path", self._path])
        except subprocess.TimeoutExpired:
            raise AppTokenError(f"fleet-secrets lookup timed out for {name}") from None
        except FileNotFoundError:
            raise AppTokenError("fleet-secrets executable not found") from None
        except OSError as exc:
            raise AppTokenError(
                f"fleet-secrets lookup failed for {name}: {type(exc).__name__}"
            ) from None
        if result.returncode == 3:
            self._missing.add(name)
            raise AppTokenError(f"fleet-secrets has no value for {name}")
        if result.returncode:
            raise AppTokenError(
                f"fleet-secrets get-verified failed for {name}: exit {result.returncode}"
            )
        value = result.stdout.decode("utf-8", "replace").rstrip("\n")
        if not value:
            raise AppTokenError(f"fleet-secrets has no value for {name}")
        self._cache[name] = value
        return value

    def secrets(self, names) -> dict:
        """Read several names in one CLI invocation (one folder export)."""
        names = list(names)
        known_missing = [n for n in names if n in self._missing]
        if known_missing:
            raise AppTokenError(f"fleet-secrets has no value for {known_missing[0]}")
        wanted = [n for n in names if n not in self._cache]
        if wanted:
            self._fetch(wanted)
        return {n: self._cache[n] for n in names}

    # Child-environment allowlist for scoped reads:  what the CLI needs to
    # run, plus the one token from the credential file.  Nothing else in the
    # relay's environment is the CLI's business.
    # Matches the supported fleet-secrets core:  ENV_PROJECT /
    # ENV_NAMES in scripts/fleet_secrets/core.py.  The required
    # INFISICAL_FLEET_OPS_PROJECT_ID binding is what keeps the CLI off
    # its ~/.agent-sync project-file fallback (expanduser reaches the
    # runtime user's home regardless of HOME), and HOME is simply not
    # forwarded.
    _CHILD_ENV_KEYS = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "INFISICAL_FLEET_OPS_PROJECT_ID",
    )

    def _child_env(self):
        if self._credential_file is None:
            return None  # transitional: inherit (see the class docstring)
        try:
            with open(self._credential_file, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            raise AppTokenError(f"credential file unreadable for {self._path}") from None
        if not token:
            raise AppTokenError(f"credential file empty for {self._path}")
        if not (self._environ.get("INFISICAL_FLEET_OPS_PROJECT_ID") or "").strip():
            raise AppTokenError(
                "INFISICAL_FLEET_OPS_PROJECT_ID is not set:  without it the "
                "CLI would resolve the Fleet-OPS project from the runtime "
                "user's ~/.agent-sync file instead of the explicit binding"
            )
        env = {k: self._environ[k] for k in self._CHILD_ENV_KEYS if k in self._environ}
        env["INFISICAL_TOKEN"] = token
        env["INFISICAL_ENVIRONMENT"] = "prod"  # the CLI contract:  Fleet-OPS prod only
        return env

    def _run_scoped(self, argv):
        kwargs = {}
        env = self._child_env()
        if env is not None:
            kwargs["env"] = env
        return self._run(argv, capture_output=True, timeout=30, **kwargs)

    def _fetch(self, wanted) -> None:
        try:
            result = self._run_scoped([self._exe, "get", *wanted, "--path", self._path])
        except subprocess.TimeoutExpired:
            raise AppTokenError(f"fleet-secrets lookup timed out for {wanted[0]}") from None
        except FileNotFoundError:
            raise AppTokenError("fleet-secrets executable not found") from None
        except OSError as exc:
            raise AppTokenError(
                f"fleet-secrets lookup failed for {wanted[0]}: {type(exc).__name__}"
            ) from None
        if result.returncode:
            # Exit 3 is a clean export that lacked the key:  remember the
            # absence so an incomplete App pair cannot drive one folder
            # export per request.  Only names the CLI actually lists are
            # cached.  Exit 2 (usage) names keys and reason tokens, never
            # values, so its first line is safe to surface - it is what tells
            # an operator a bad --path apart from a missing key;  the usage
            # block the CLI prints after it is dropped.  Any other failure can
            # carry backend detail on stderr:  report only the variable name
            # and the exit code.
            detail = result.stderr.decode("utf-8", "replace").strip()
            if result.returncode == 3:
                absent = _absent_names(detail, wanted)
                self._missing.update(absent)
                raise AppTokenError(f"fleet-secrets has no value for {absent[0]}")
            if result.returncode == 2 and detail:
                reason = detail.splitlines()[0]
                raise AppTokenError(f"fleet-secrets lookup failed for {wanted[0]}: {reason}")
            raise AppTokenError(
                f"fleet-secrets lookup failed for {wanted[0]} (exit {result.returncode})"
            )
        raw = result.stdout.decode("utf-8", "replace").strip()
        if len(wanted) == 1:
            values = {wanted[0]: raw}
        else:
            # Several names print one JSON object.  Any other stdout (a stray
            # banner, a format change) is a lookup failure like the rest -
            # AppTokenError, so the relay answers 503 unavailable, not 400 -
            # and its message names the variable, never the output.
            try:
                values = json.loads(raw)
            except ValueError:
                values = None
            if not isinstance(values, dict):
                raise AppTokenError(f"fleet-secrets returned unreadable output for {wanted[0]}")
        for name in wanted:
            value = str(values.get(name) or "").strip()
            if not value:
                raise AppTokenError(f"fleet-secrets returned an empty value for {name}")
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
        # One CLI invocation per App, not per name:  `fleet-secrets get`
        # exports the whole folder on every call.
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
                raise InstallationReadError(exc.status, exc.retry_after) from None
            raise AppTokenError("installation metadata unavailable") from None
        finally:
            del bearer

    def token_for(self, slug: str, repo: str, permissions: dict) -> tuple[str, float]:
        """Mint (or serve from cache) an installation token;  returns (token, expiry epoch)."""
        for key in permissions:
            if key not in MINT_PERMISSION_CEILING:
                raise AppTokenError(f"permission {key!r} is outside the mint ceiling")
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
