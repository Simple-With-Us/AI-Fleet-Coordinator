"""Offline-reviewed prototype. Not installed or enabled on any host.

Call credential_loader(slug) to read that seat's ID and PEM from Infisical
Fleet-OPS prod /github-apps. No shared tuple and no stored installation IDs.
Only a caller-selected repository and explicit permissions may be used.
This module never prints tokens or keys and has no credential fallback.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"

# Owner decision 2026-10-22 (Q5):  git-relay is the ONE path that mints per-bot
# GitHub App tokens.  This Operator GitHub token broker (OPS-966) is kept for
# review history only and must never mint.  The guard below is a module
# constant and a hard refusal, not an environment variable, a constructor flag
# or a fallback:  nothing here can re-enable seat App token minting, so the
# broker fails closed and names the path that does own minting.
MINTING_OWNER = "git-relay"
MINTING_REFUSED = (
    "seat GitHub App token minting is owned by git-relay (GR-MCP); the Operator "
    "GitHub token broker is disabled and will not mint"
)


def refuse_minting():
    """Fail closed before any credential read, cache write or GitHub call.

    The rest of this module is the reviewed OPS-966 implementation, retained
    unchanged for review history.  It is unreachable from token_for and must
    stay that way unless the owner decides git-relay no longer owns minting.
    """
    raise RoutingError(MINTING_REFUSED + "; use " + MINTING_OWNER, outcome="refused")


class RoutingError(Exception):
    """status is the provider HTTP code when one exists.

    outcome classifies the failure for callers that must decide whether a
    retry can duplicate a side effect:  "refused" (a definite provider
    answer), "retryable" (a read-side failure before any write), or
    "outcome_unknown" (a write may already have happened).
    """

    def __init__(self, message, status=None, outcome=None):
        super().__init__(message)
        self.status = status
        self.outcome = outcome


def secret_names(slug):
    if not re.fullmatch(r"swu-[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RoutingError("Explicit swu App slug required")
    prefix = "GH_APP_" + slug.upper().replace("-", "_")
    return prefix + "_ID", prefix + "_PRIVATE_KEY"


_CREDENTIAL_MARKERS = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "AKIA",
    "-----BEGIN",
)

# OpenAI-style keys need a length qualifier:  a bare "sk-" also matches
# ordinary repository names such as flask-app, task-manager or disk-usage.
_CREDENTIAL_PATTERNS = (re.compile(r"sk-[A-Za-z0-9_-]{16,}"),)


def target_parts(target):
    # Refuse credential-shaped input before the value is relayed to GitHub or
    # persisted in a cache key, and never echo it back.
    if (
        not isinstance(target, str)
        or any(m in target for m in _CREDENTIAL_MARKERS)
        or any(p.search(target) for p in _CREDENTIAL_PATTERNS)
    ):
        # Field-specific refusal code;  callers log the code, never the value.
        raise RoutingError("secret_refused:target", outcome="refused")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", target):
        raise RoutingError("Explicit owner/repo required; no inferred default")
    owner, repo = target.split("/")
    if repo in (".", ".."):
        raise RoutingError("Invalid repository")
    return owner, repo


def permission_set(permissions):
    p = dict(permissions)
    if not p or any(
        not re.fullmatch(r"[a-z_]+", k) or v not in ("read", "write") for k, v in p.items()
    ):
        raise RoutingError("Explicit read/write permissions required")
    if p.get("metadata", "read") != "read":
        raise RoutingError("Metadata permission must be read")
    p["metadata"] = "read"
    return p


def compact(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def jwt(app_id, pem, clock=time.time):
    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    now = int(clock())
    unsigned = b".".join(
        [
            b64(compact({"alg": "RS256", "typ": "JWT"})),
            b64(compact({"iat": now - 60, "exp": now + 540, "iss": str(app_id)})),
        ]
    )
    # mkstemp is mode 0600. PEM exists only for signing and is then unlinked.
    fd, name = tempfile.mkstemp(prefix="gh-app-sign-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", name],
                input=unsigned,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise RoutingError("App signing timed out", outcome="retryable") from None
        except FileNotFoundError:
            # A local failure before any GitHub request:  a definite refusal,
            # never an unknown outcome that would burn the caller's nonce.
            raise RoutingError(
                "App signer unavailable: openssl not found", outcome="refused"
            ) from None
        if result.returncode:
            raise RoutingError("App signing failed", outcome="refused")
        return (unsigned + b"." + b64(result.stdout)).decode()
    finally:
        os.unlink(name)


def _masked_pem(pem):
    """First-eight/last-four of the key body, armor stripped - the masked
    diagnostic a rejected provider credential must report, never the value."""
    body = "".join(line.strip() for line in pem.splitlines() if "-----" not in line)
    if len(body) < 12:
        return None
    return body[:8] + "\u2026" + body[-4:]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_call(method, path, bearer, body=None):
    """_github_call plus outcome normalization:  every failure from a
    possibly-sent POST leaves with outcome_unknown (never replay it), and
    every read-side failure with retryable, even when the raise site (for
    example _bounded_json) did not classify it."""
    try:
        return _github_call(method, path, bearer, body)
    except RoutingError as exc:
        if exc.outcome is None:
            exc.outcome = "outcome_unknown" if method == "POST" else "retryable"
        raise


def _github_call(method, path, bearer, body=None):
    if not path.startswith("/") or path.startswith("//"):
        raise RoutingError("Invalid API path")
    req = urllib.request.Request(
        API + path,
        data=None if body is None else compact(body),
        method=method,
        headers={
            "Authorization": "Bearer " + bearer,
            "Accept": "application/vnd.github+json",
            "User-Agent": "swu-operator-github-router/0.1",
            "X-GitHub-Api-Version": "2026-03-10",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as r:
            raw = r.read(1048577)
            if len(raw) > 1048576:
                raise RoutingError(
                    "GitHub response too large",
                    outcome="outcome_unknown" if method == "POST" else "retryable",
                )
            data = json.loads(raw)
            _bounded_json(data)
            if not isinstance(data, dict):
                raise RoutingError(
                    "GitHub response is not a JSON object",
                    outcome="outcome_unknown" if method == "POST" else "retryable",
                )
            return data
    except urllib.error.HTTPError as e:
        # Never include response bodies or headers in exceptions/logs.
        # A POST that timed out or hit 408/5xx may already have minted.
        if e.code == 408 or e.code >= 500:
            outcome = "outcome_unknown" if method == "POST" else "retryable"
        else:
            outcome = "refused"
        err = RoutingError(
            "GitHub request failed, HTTP " + str(e.code), status=e.code, outcome=outcome
        )
        err.code = e.code
        retry_after = e.headers.get("Retry-After") if e.headers else None
        # isdigit() also passes non-decimal digits int() rejects (e.g. "²");
        # isdecimal() keeps the parse safe inside this error handler.
        if isinstance(retry_after, str) and retry_after.isdecimal():
            err.retry_after = int(retry_after)
        raise err from None
    except (OSError, ValueError):
        raise RoutingError(
            "GitHub transport or response failed",
            outcome="outcome_unknown" if method == "POST" else "retryable",
        ) from None


def _bounded_json(value, _depth=0, _budget=None):
    """Reject hostile provider JSON before any field is trusted."""
    if _budget is None:
        _budget = [512]
    _budget[0] -= 1
    if _depth > 32 or _budget[0] < 0:
        raise RoutingError("GitHub response too complex")
    if isinstance(value, dict):
        for k, v in value.items():
            _bounded_json(k, _depth + 1, _budget)
            _bounded_json(v, _depth + 1, _budget)
    elif isinstance(value, list):
        for item in value:
            _bounded_json(item, _depth + 1, _budget)


class TokenRouter:
    def __init__(self, credential_loader, cache_dir, api=github_call, signer=jwt, clock=time.time):
        self.loader, self.cache = credential_loader, Path(cache_dir)
        self.api, self.signer, self.clock = api, signer, clock

    def _cache_file(self, scope):
        # Reject a symlinked cache directory before creating anything.
        if self.cache.is_symlink():
            raise RoutingError("Cache directory must not be a symlink")
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        s = self.cache.lstat()
        if (
            not stat.S_ISDIR(s.st_mode)
            or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) & 0o077
        ):
            raise RoutingError("Cache directory must be owned, private, non-symlink")
        return self.cache / (hashlib.sha256(compact(scope)).hexdigest() + ".json")

    def _read(self, path, scope):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise RoutingError("Unsafe cache file") from None
        with os.fdopen(fd) as f:
            s = os.fstat(f.fileno())
            if (
                not stat.S_ISREG(s.st_mode)
                or s.st_uid != os.getuid()
                or stat.S_IMODE(s.st_mode) & 0o077
            ):
                raise RoutingError("Unsafe cache file permissions")
            try:
                d = json.load(f)
                expires = dt.datetime.fromisoformat(
                    d["expires_at"].replace("Z", "+00:00")
                ).timestamp()
                if d["scope"] == scope and expires - self.clock() > 300 and d.get("token"):
                    return d["token"]
            except (KeyError, ValueError, TypeError):
                return None
        return None

    def token_for(self, slug, target, permissions):
        refuse_minting()  # owner decision 2026-10-22 (Q5):  git-relay owns minting
        secret_names(slug)  # Validate before credential retrieval.
        owner, repo = target_parts(target)
        requested = permission_set(permissions)
        app_id, pem = self.loader(slug)
        try:
            app_id = int(app_id)
            if app_id <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise RoutingError("Invalid App ID", outcome="refused") from None
        pem_preview = _masked_pem(pem)
        bearer = self.signer(app_id, pem)
        del pem
        try:
            app = self.api("GET", "/app", bearer)
        except RoutingError as exc:
            if exc.status in (401, 403) and pem_preview:
                exc.credential_preview = pem_preview
            raise
        if (
            not isinstance(app.get("id"), int)
            or isinstance(app.get("id"), bool)
            or not isinstance(app.get("slug"), str)
        ):
            raise RoutingError("GitHub /app identity has malformed fields", outcome="refused")
        if app["id"] != app_id or app["slug"] != slug:
            raise RoutingError("Key belongs to a different App", outcome="refused")
        install = self.api("GET", "/repos/" + owner + "/" + repo + "/installation", bearer)
        account = install.get("account")
        if (
            not isinstance(install.get("app_id"), int)
            or isinstance(install.get("app_id"), bool)
            or install.get("suspended_at")
            or not isinstance(account, dict)
            or not isinstance(account.get("login"), str)
            or account["login"].casefold() != owner.casefold()
        ):
            raise RoutingError("Installation identity mismatch or suspension", outcome="refused")
        iid = install.get("id")
        if not isinstance(iid, int) or iid <= 0:
            raise RoutingError("Missing installation ID", outcome="refused")
        scope = {
            "app_id": app_id,
            "installation_id": iid,
            "repository": target.casefold(),
            "permissions": requested,
        }
        cache_file = self._cache_file(scope)
        token = self._read(cache_file, scope)
        if token:
            return token
        data = self.api(
            "POST",
            "/app/installations/" + str(iid) + "/access_tokens",
            bearer,
            {"repositories": [repo], "permissions": requested},
        )
        if not isinstance(data, dict):
            raise RoutingError("Minted-token response is not an object")
        repos = data.get("repositories")
        if not isinstance(repos, list) or any(not isinstance(x, dict) for x in repos):
            raise RoutingError("Minted-token response has malformed repositories")
        if len(repos) != 1 or str(repos[0].get("full_name", "")).casefold() != target.casefold():
            raise RoutingError("Minted repository scope differs from request")
        actual = data.get("permissions")
        if not isinstance(actual, dict) or any(
            not isinstance(k, str) or v not in ("read", "write") for k, v in actual.items()
        ):
            raise RoutingError("Minted-token response has malformed permissions")
        if actual != requested:
            raise RoutingError("Minted permission scope differs from request")
        token, expiry = data.get("token"), data.get("expires_at")
        try:
            seconds = (
                dt.datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp() - self.clock()
            )
        except (AttributeError, TypeError, ValueError):
            raise RoutingError("Missing or invalid expiry") from None
        if not isinstance(token, str) or not token or not 300 < seconds <= 3900:
            raise RoutingError("Invalid token or expiry")
        fd, temp = tempfile.mkstemp(dir=self.cache)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"scope": scope, "token": token, "expires_at": expiry}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, cache_file)
            # A crash must not lose the rename:  fsync the directory too.
            dfd = os.open(self.cache, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return token


# Deliberately no CLI that emits a token, no gh wrapper installation, and no
# live activation. Integrator must supply an Infisical-only loader and consume
# the token in-process, never in logs. No fallback to PAT or saved gh login.
