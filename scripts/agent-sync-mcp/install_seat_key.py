#!/usr/bin/env python3
"""Install a hosted seat's Zulip bot key into the agent-sync MCP Worker's secrets.

    python3 -I install_seat_key.py GROK-WEB            # checks only, changes nothing
    python3 -I install_seat_key.py GROK-WEB --apply    # wrangler secret put ZULIP_KEY_GROK_WEB
    python3 -I install_seat_key.py GROK-WEB --delete --apply   # remove it (kill switch)

The key never touches a file, a command line, a log or this process's output.
It is read from Infisical (project "AI Fleet Coordinator", environment prod,
folder /zulip, through the INFISICAL_AUTOMATION identity), checked against
Zulip, and written to `wrangler secret put` on stdin.  Spec 3.6 says the
Worker gets its copy from Infisical's Cloudflare sync;  until Jay sets that up
(owner item A4), this script is that sync, run by hand.  Rerun it after a
rotation.

Before anything is written it proves the key is fit for the seat, the same
checks the Worker repeats every 10 minutes:  the seat is in HOSTED_SEATS, the
Infisical email matches the seat's configured Worker email binding, and
Zulip's users/me for the key is that bot, a bot, and a member (role 400), never
an administrator or owner.  So an admin key (openai-dot-bot until Fri, Oct 9) is refused and never installed.

Seat keys come only from Infisical:  `--zuliprc` is gone, so nothing is read from
any local credential file (owner 2026-10-18, AFC-347).

Standard library only.  Python 3.11+.  Run from scripts/agent-sync-mcp/.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WRANGLER_JSONC = HERE / "wrangler.jsonc"
INFISICAL_API = "https://app.infisical.com"
INFISICAL_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator (INFISICAL.md)
FLEET_OPS_PROJECT = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"  # Fleet-OPS (fleet-secrets core.py)
INFISICAL_ENV = "prod"
INFISICAL_PATH = "/zulip"
REALM = "https://simplewithus.zulipchat.com"
ACCOUNT_ID = "3a9368057468d0909cafaa85df12d1b7"  # Usage.Jays.Services (decision D1)
MEMBER_ROLE = 400

# Seat -> (Infisical key prefix, Worker secret name, wrangler var for the email, the bot's seat tag source).
SEATS = {
    "CURSOR": ("ZULIP_CURSOR", "ZULIP_KEY_CURSOR", "ZULIP_EMAIL_CURSOR"),
    "CLAUDE": ("ZULIP_CLAUDE", "ZULIP_CLAUDE_API_KEY", "ZULIP_CLAUDE_EMAIL"),
    # MA (Muse Assist, the seat Jay calls Rob):  its bot key already lives in
    # Fleet-OPS prod /zulip as MA_ZULIP_* (names verified live 2026-10-23), so
    # read it there instead of duplicating it into the AI Fleet Coordinator
    # project (secret consolidation).  The 5-tuple is (project, email name,
    # key name, Worker secret name, email var).
    "MA": (
        FLEET_OPS_PROJECT,
        "MA_ZULIP_EMAIL",
        "MA_ZULIP_KEY",
        "ZULIP_KEY_MA",
        "ZULIP_EMAIL_MA",
    ),
    "JET": ("ZULIP_JET", "ZULIP_KEY_JET", "ZULIP_EMAIL_JET"),
    "GROK-WEB": ("ZULIP_GROK_WEB", "ZULIP_KEY_GROK_WEB", "ZULIP_EMAIL_GROK_WEB"),
    "ECHO": ("ZULIP_ECHO", "ZULIP_KEY_ECHO", "ZULIP_EMAIL_ECHO"),
    "WISE": ("ZULIP_WISE", "ZULIP_KEY_WISE", "ZULIP_EMAIL_WISE"),
}
# Bot email local parts whose seat tag is not the upper-cased local part
# (cli.py EMAIL_TAG_OVERRIDES, src/contract.js EMAIL_TAG_OVERRIDES).
TAG_OVERRIDES = {
    "muse-assist": "MA",
    "openai-dot": "JET",
    "instinct-bat": "ECHO",
    "instinct-owl": "WISE",
}


def fail(message: str) -> NoReturn:  # type: ignore[name-defined]
    raise SystemExit(f"install_seat_key: {message}")


def handoff(name: str) -> str:
    """One credential:  the environment first, then `fleet-secrets get` (OPS-818).  The flat
    handoff file is retired (AFC-359).  Only the name is ever reported."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    binary = shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )
    try:
        proc = subprocess.run([binary, "get", name], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    fail(f"{name} is not in the environment or fleet-secrets (name only; no value printed)")


def wrangler_vars() -> dict:
    """wrangler.jsonc vars (comments stripped outside strings)."""
    text = WRANGLER_JSONC.read_text(encoding="utf-8")
    out, i, in_string = [], 0, False
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        else:
            out.append(c)
        i += 1
    return json.loads("".join(out))["vars"]


def http_json(
    method: str, url: str, *, body: dict | None = None, headers: dict | None = None
) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "agent-sync-mcp install_seat_key")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        fail(
            f"{method} {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path} answered HTTP {exc.code}"
        )


def infisical_pair(
    email_name: str, key_name: str, project: str = INFISICAL_PROJECT
) -> tuple[str, str]:
    """(email, key) for a seat from Infisical.  Neither is printed."""
    login = http_json(
        "POST",
        f"{INFISICAL_API}/api/v1/auth/universal-auth/login",
        body={
            "clientId": handoff("INFISICAL_AUTOMATION_CLIENT_ID"),
            "clientSecret": handoff("INFISICAL_AUTOMATION_CLIENT_SECRET"),
        },
    )
    token = login.get("accessToken") or fail("Infisical login returned no access token")
    query = urllib.parse.urlencode(
        {
            "workspaceId": project,
            "environment": INFISICAL_ENV,
            "secretPath": INFISICAL_PATH,
        }
    )
    auth_header = "Bearer " + token
    data = http_json(
        "GET", f"{INFISICAL_API}/api/v3/secrets/raw?{query}", headers={"Authorization": auth_header}
    )
    found = {s.get("secretKey"): s.get("secretValue") or "" for s in data.get("secrets", [])}
    email, key = found.get(email_name, ""), found.get(key_name, "")
    if not email or not key:
        fail(
            f"{email_name} or {key_name} is missing or empty in Infisical {INFISICAL_ENV}{INFISICAL_PATH} (project {project})"
        )
    return email.strip(), key.strip()


def seat_tag(user: dict) -> str:
    local = str(user.get("email") or "").split("@", 1)[0].lower()
    local = local.removesuffix("-bot")
    return TAG_OVERRIDES.get(local, local.upper())


def zulip_me(email: str, key: str) -> dict:
    basic = base64.b64encode(f"{email}:{key}".encode()).decode()
    return http_json("GET", f"{REALM}/api/v1/users/me", headers={"Authorization": "Basic " + basic})


def wrangler(args: list[str], stdin: str | None, key_for_scrub: str) -> int:
    env = dict(os.environ)
    env.update(
        {
            "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
            "CLOUDFLARE_EMAIL": handoff("CLOUDFLARE_JAY_ACCOUNT_EMAIL"),
            "CLOUDFLARE_API_KEY": handoff("CLOUDFLARE_JAY_API_KEY"),
            "WRANGLER_SEND_METRICS": "false",
        }
    )
    proc = subprocess.run(
        ["npx", "--no-install", "wrangler", *args],
        cwd=HERE,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=600,
    )
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            if key_for_scrub and key_for_scrub in line:
                line = line.replace(key_for_scrub, "[redacted]")
            print("  wrangler: " + line)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("seat", choices=sorted(SEATS))
    parser.add_argument(
        "--apply", action="store_true", help="write the Worker secret (default:  checks only)"
    )
    parser.add_argument(
        "--delete", action="store_true", help="remove the Worker secret instead (with --apply)"
    )
    args = parser.parse_args()

    entry = SEATS[args.seat]
    if len(entry) == 5:
        project, email_name, key_name, secret_name, email_var = entry
    else:
        prefix, secret_name, email_var = entry
        project, email_name, key_name = INFISICAL_PROJECT, f"{prefix}_EMAIL", f"{prefix}_API_KEY"
    vars_ = wrangler_vars()
    hosted = [s.strip() for s in str(vars_.get("HOSTED_SEATS", "")).split(",") if s.strip()]

    if args.delete:
        print(f"{'Removing' if args.apply else 'Would remove'} Worker secret {secret_name}.")
        if not args.apply:
            return 0
        return wrangler(["secret", "delete", secret_name], "y\n", "")

    if args.seat not in hosted:
        fail(
            f"{args.seat} is not in HOSTED_SEATS ({', '.join(hosted) or 'none'}) in wrangler.jsonc;  add it there first (DEPLOY.md)"
        )
    expected_email = str(vars_.get(email_var, "")).strip().lower()
    source = "Infisical"
    email, key = infisical_pair(email_name, key_name, project=project)
    checks = []
    checks.append((f"{source} email equals {email_var}", email.lower() == expected_email))
    checks.append(("key is 32 letters and digits", bool(re.fullmatch(r"[A-Za-z0-9]{32}", key))))
    me = zulip_me(email, key)
    role = me.get("role")
    checks.append(("users/me is a bot", me.get("is_bot") is True))
    checks.append(
        ("users/me email is the seat's bot", str(me.get("email") or "").lower() == expected_email)
    )
    checks.append((f"users/me signs as {args.seat}", seat_tag(me) == args.seat))
    checks.append(
        (
            f"role is member (400), live role {role}",
            role == MEMBER_ROLE and not me.get("is_admin") and not me.get("is_owner"),
        )
    )
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not all(ok for _, ok in checks):
        fail("refused:  this key is not fit for a hosted seat (spec 3.6), nothing was written")
    if not args.apply:
        print(f"Checks pass.  Rerun with --apply to write Worker secret {secret_name}.")
        return 0
    print(f"Writing Worker secret {secret_name} (value on stdin, never printed).")
    return wrangler(["secret", "put", secret_name], key, key)


if __name__ == "__main__":
    sys.exit(main())
