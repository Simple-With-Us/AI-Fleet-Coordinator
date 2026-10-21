"""Operator github exec client. No token-print command and no remote execution.

Initial reviewed surface must remain restricted until command target binding is
expanded. Supports gh pr list/view/checks and gh repo view only. Caller names
identify the signing key, not the App; broker policy binds the App independently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .github_token_router import permission_set, target_parts
from .operator_github import signature


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def private_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        s = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(s.st_mode)
            or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) & 0o077
        ):
            raise ClientError("Caller key must be an owned mode 0600 regular file")
        key = f.read(4097).strip()
    if not 32 <= len(key) <= 4096:
        raise ClientError("Invalid caller key")
    return key


def bound_command(command, repo):
    target_parts(repo)
    if not command or command[0] != "gh":
        raise ClientError("Only reviewed gh read commands supported")
    # Reject all options not specifically supported, aliases, URLs, arbitrary API
    # requests, extensions, subprocess template functions and cross-repo selectors.
    if command[1:3] == ["pr", "list"]:
        rest = command[3:]
        out = ["gh", "pr", "list", "--repo", repo]
        i = 0
        while i < len(rest):
            if rest[i] not in ("--state", "--limit") or i + 1 >= len(rest):
                raise ClientError("Unsupported gh option")
            opt, val = rest[i : i + 2]
            if opt == "--state" and val not in ("open", "closed", "merged", "all"):
                raise ClientError("Invalid state")
            if opt == "--limit" and (not val.isdigit() or not 1 <= int(val) <= 100):
                raise ClientError("Invalid limit")
            out.extend([opt, val])
            i += 2
        return out
    if command[1:3] in (["pr", "view"], ["pr", "checks"]):
        if len(command) != 4 or not re.fullmatch(r"[1-9][0-9]{0,8}", command[3]):
            raise ClientError("Explicit numeric PR required")
        return command + ["--repo", repo]
    if command[1:3] == ["repo", "view"] and len(command) == 3:
        return command + [repo]
    raise ClientError("Command not in reviewed allowlist")


def child_environment(original, result):
    env = {
        k: v
        for k, v in original.items()
        if not k.startswith(("GH_", "GITHUB_", "GIT_CONFIG_", "GIT_AUTHOR_", "GIT_COMMITTER_"))
    }
    env.update(
        {
            "GH_TOKEN": result["token"],
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_CONFIG_DIR": "/nonexistent/operator-gh-config",
            "GIT_AUTHOR_NAME": result["git_author_name"],
            "GIT_AUTHOR_EMAIL": result["git_author_email"],
            "GIT_COMMITTER_NAME": result["git_author_name"],
            "GIT_COMMITTER_EMAIL": result["git_author_email"],
        }
    )
    return env


def execute(url, seat, key_path, repo, permissions, command, *, run=subprocess.run):
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ClientError("Broker origin must be HTTPS with no path, credentials or query")
    if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", seat):
        raise ClientError("Invalid caller")
    cmd = bound_command(command, repo)
    perms = permission_set(permissions)
    if any(v != "read" for v in perms.values()):
        raise ClientError("Initial client supports read permissions only")
    key = private_key(key_path)
    path = "/operator/v1/github/" + seat + "/token"
    body = json.dumps(
        {"repo": repo, "permissions": perms}, sort_keys=True, separators=(",", ":")
    ).encode()
    stamp = str(int(time.time()))
    nonce = secrets.token_hex(32)
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Operator-Timestamp": stamp,
            "X-Operator-Nonce": nonce,
            "X-Operator-Signature": signature(key, path, stamp, nonce, body),
        },
    )
    del key
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as r:
            if r.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ClientError("Invalid broker response")
            raw = r.read(32769)
            if len(raw) > 32768:
                raise ClientError("Oversized broker response")
            result = json.loads(raw)
    except urllib.error.HTTPError as e:
        raise ClientError("Broker refused, HTTP " + str(e.code)) from None
    except (OSError, ValueError):
        raise ClientError("Broker connection or response failed") from None
    if (
        result.get("repo") != repo
        or result.get("permissions") != perms
        or not isinstance(result.get("token"), str)
        or not result["token"]
    ):
        raise ClientError("Broker scope mismatch")
    if not re.fullmatch(r"swu-[a-z0-9-]+", result.get("app_slug", "")):
        raise ClientError("Invalid App identity")
    login = result["app_slug"] + "[bot]"
    if result.get("git_author_name") != login or not re.fullmatch(
        r"[1-9][0-9]*\+" + re.escape(login) + r"@users\.noreply\.github\.com",
        result.get("git_author_email", ""),
    ):
        raise ClientError("Invalid bot author identity")
    # Resolve actual gh executable outside repo-controlled PATH before activation.
    # Client integration must inject a pinned host-installed gh binary; current
    # default locates it through controlled system paths only.
    import shutil

    gh = shutil.which("gh", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not gh:
        raise ClientError("System gh executable unavailable")
    cmd[0] = gh
    env = child_environment(os.environ, result)
    del result
    try:
        return run(cmd, env=env, check=False).returncode
    finally:
        env.pop("GH_TOKEN", None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="operator github exec")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--caller-key-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--permissions", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv)
    try:
        pairs = [v.split(":") for v in a.permissions.split(",")]
        if any(len(p) != 2 for p in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ClientError("Invalid permissions")
        command = a.command[1:] if a.command[:1] == ["--"] else a.command
        return execute(a.origin, a.caller, a.caller_key_file, a.repo, dict(pairs), command)
    except Exception as e:
        # Safe typed errors only; never display external HTTP bodies or tokens.
        msg = str(e) if isinstance(e, ClientError) else "request failed"
        print("operator github: " + msg, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
