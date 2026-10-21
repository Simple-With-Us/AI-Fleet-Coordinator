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
import signal
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


CALLER_KEY_NAME = "OPERATOR_GITHUB_CALLER_KEY"


def caller_key():
    """Load the caller signing key from approved sources only.

    Environment first, then `fleet-secrets get`.  A caller-chosen local file
    is not an approved credential path and is not consulted.  Failures name
    the variable operators must provision, never the value seen.
    """
    key = os.environ.get(CALLER_KEY_NAME)
    if key is None:
        try:
            result = subprocess.run(
                ["fleet-secrets", "get", CALLER_KEY_NAME],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ClientError(
                CALLER_KEY_NAME + " unavailable: fleet-secrets lookup failed"
            ) from None
        if result.returncode:
            raise ClientError(CALLER_KEY_NAME + " unavailable: fleet-secrets lookup failed")
        key = result.stdout.decode("utf-8", "replace").strip()
    if not 32 <= len(key) <= 4096:
        raise ClientError(CALLER_KEY_NAME + " is missing or invalid")
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
    env = {k: v for k, v in original.items() if not k.startswith(("GH_", "GITHUB_", "GIT_"))}
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


def execute(url, seat, repo, permissions, command, *, popen=subprocess.Popen):
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
    key = caller_key()
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
        return run_bounded(cmd, env=env, popen=popen)
    finally:
        env.pop("GH_TOKEN", None)


def run_bounded(command, env, timeout=300, *, popen=subprocess.Popen):
    """Run the reviewed gh command with a hard ceiling.

    The child is terminated and reaped on timeout, interruption, SIGTERM and
    any unexpected wait failure, so an interrupted runner never leaves a gh
    process holding GH_TOKEN.  A missing executable exits 127 so callers can
    distinguish it from a command failure.
    """
    try:
        proc = popen(command, env=env)
    except FileNotFoundError:
        raise SystemExit(127) from None
    previous = signal.getsignal(signal.SIGTERM)

    def _forward(signum, frame):
        if proc.poll() is None:
            proc.terminate()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _forward)
    try:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise ClientError("gh command timed out") from None
        except BaseException:
            # KeyboardInterrupt, SystemExit and unexpected wait failures all
            # terminate and reap the child before propagating.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="operator github exec")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--permissions", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    a = parser.parse_args(argv)
    try:
        pairs = [v.split(":") for v in a.permissions.split(",")]
        if any(len(p) != 2 for p in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ClientError("Invalid permissions")
        command = a.command[1:] if a.command[:1] == ["--"] else a.command
        return execute(a.origin, a.caller, a.repo, dict(pairs), command)
    except Exception as e:
        # Safe typed errors only; never display external HTTP bodies or tokens.
        msg = str(e) if isinstance(e, ClientError) else "request failed"
        print("operator github: " + msg, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
