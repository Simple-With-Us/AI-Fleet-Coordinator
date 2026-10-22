"""Per-seat GitHub App tokens, minted and refreshed by the relay.

Seats never see an App id, private key, or the 1-hour installation-token
expiry:  the relay reads each seat App's credentials from Infisical
(GH_APP_<SLUG>_ID and GH_APP_<SLUG>_PRIVATE_KEY in the configured project,
prod, /github-apps), signs a JWT with the local openssl binary (stdlib has no
RSA), and caches installation tokens in memory, refreshing 5 minutes early.
Nothing is written to disk and no secret is ever logged or returned beyond
the minted token a seat is allowed to use.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 30
REFRESH_MARGIN_SECONDS = 300
MINT_PERMISSION_CEILING = ("contents", "pull_requests", "issues", "actions", "metadata")


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
    fd, name = tempfile.mkstemp(prefix="pr-relay-sign-", suffix=".pem")
    try:
        with open(fd, "w") as f:
            f.write(pem)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", name],
                input=unsigned,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise AppTokenError(f"app signer unavailable: {type(exc).__name__}") from None
        if result.returncode:
            raise AppTokenError("app signing failed")
        return (unsigned + b"." + _b64(result.stdout)).decode()
    finally:
        import os

        os.unlink(name)


class InfisicalSource:
    """Reads one named secret at a time from Infisical via universal auth."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        workspace_id: str,
        environment: str = "prod",
        secret_path: str = "/github-apps",
        base_url: str = "https://app.infisical.com",
        opener=None,
    ):
        if not client_id or not client_secret or not workspace_id:
            raise AppTokenError("Infisical machine identity is not configured")
        self._cid = client_id
        self._csec = client_secret
        self._workspace = workspace_id
        self._env = environment
        self._path = secret_path
        self._base = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._token = ""
        self._cache: dict[str, str] = {}

    def _call(self, method: str, path: str, payload: dict | None, auth: bool) -> dict:
        req = urllib.request.Request(
            self._base + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "swu-pr-relay",
                **({"Authorization": f"Bearer {self._token}"} if auth else {}),
            },
        )
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(1024 * 1024)
        except (urllib.error.URLError, OSError) as exc:
            raise AppTokenError(f"infisical unavailable: {type(exc).__name__}") from None
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise AppTokenError("infisical reply was not JSON") from None

    def _login(self) -> None:
        data = self._call(
            "POST",
            "/api/v1/auth/universal-auth/login",
            {"clientId": self._cid, "clientSecret": self._csec},
            auth=False,
        )
        self._token = str(data.get("accessToken") or "")
        if not self._token:
            raise AppTokenError("infisical login returned no token")

    def secret(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        if not self._token:
            self._login()
        from urllib.parse import quote

        path = (
            "/api/v3/secrets/raw/"
            + quote(name, safe="")
            + f"?workspaceId={self._workspace}&environment={self._env}"
            + f"&secretPath={quote(self._path, safe='')}"
        )
        data = self._call("GET", path, None, auth=True)
        value = str((data.get("secret") or {}).get("secretValue") or "")
        if not value:
            raise AppTokenError(f"infisical secret {name} is missing or empty")
        self._cache[name] = value
        return value


class AppTokenMinter:
    """Mints and caches installation tokens per (app slug, repo, permissions)."""

    def __init__(self, source, api, signer=sign_jwt, clock=time.time):
        self._source = source
        self._api = api  # callable(method, path, bearer, payload=None) -> dict
        self._signer = signer
        self._clock = clock
        self._tokens: dict[str, tuple[float, str]] = {}

    def _app_credentials(self, slug: str) -> tuple[int, str]:
        prefix = "GH_APP_" + slug.upper().replace("-", "_")
        try:
            app_id = int(self._source.secret(prefix + "_ID").strip())
        except ValueError:
            raise AppTokenError(f"app id for {slug} is not an integer") from None
        return app_id, self._source.secret(prefix + "_PRIVATE_KEY")

    def token_for(self, slug: str, repo: str, permissions: dict) -> str:
        for key in permissions:
            if key not in MINT_PERMISSION_CEILING:
                raise AppTokenError(f"permission {key!r} is outside the mint ceiling")
        cache_key = json.dumps(
            [slug, repo.casefold(), sorted(permissions.items())], separators=(",", ":")
        )
        now = self._clock()
        cached = self._tokens.get(cache_key)
        if cached and cached[0] - now > REFRESH_MARGIN_SECONDS:
            return cached[1]
        app_id, pem = self._app_credentials(slug)
        bearer = self._signer(app_id, pem, int(now))
        del pem
        app = self._api("GET", "/app", bearer)
        if app.get("id") != app_id:
            raise AppTokenError("app key belongs to a different app")
        owner, name = repo.split("/", 1)
        install = self._api("GET", f"/repos/{owner}/{name}/installation", bearer)
        iid = install.get("id")
        if (
            not isinstance(iid, int)
            or iid <= 0
            or install.get("suspended_at")
            or str((install.get("account") or {}).get("login", "")).casefold() != owner.casefold()
        ):
            raise AppTokenError("app installation is missing, suspended, or on another account")
        data = self._api(
            "POST",
            f"/app/installations/{iid}/access_tokens",
            bearer,
            {"repositories": [name], "permissions": permissions},
        )
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
        return token
