#!/usr/bin/env python3
"""Create the fleet's Zulip seat bots and write one zuliprc per bot.  OWNER-RUN ONLY.

Agents must not run this: creating bot users is account creation, which the
owner does himself.  The script authenticates as the owner (a personal API key,
never a bot key), so run it in your own terminal:

    python3 scripts/zulip_provision_bots.py            # plan, then asks y/N
    python3 scripts/zulip_provision_bots.py --yes      # no prompt
    python3 scripts/zulip_provision_bots.py --dry-run  # plan only, writes nothing

Owner credentials, first hit wins:
  1. --owner-rc PATH (a zuliprc downloaded from Settings > Account & privacy > API key)
  2. ~/.secrets/Zulip/Jay-zuliprc
  3. env ZULIP_OWNER_EMAIL + ZULIP_OWNER_API_KEY (exported in your own shell)
  4. prompt: email, then the API key via getpass (never echoed)

For each bot in ROSTER it:
  - creates a generic bot (POST /bots) unless one with that full name exists,
  - writes ~/.secrets/Zulip/<file>-zuliprc with mode 600 if that file is missing
    (an existing file is never overwritten unless --force),
  - subscribes the bot to CHANNELS using the bot's own key.

Keys are never printed.  Output names bots, emails and file paths only.
Existing bots you own get their zuliprc written from GET /bots, so a lost file
can be recovered by rerunning.  Grok Bot (GB-*) personas are out of scope.
"""
import argparse
import base64
import configparser
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REALM = "https://simplewithus.zulipchat.com"
SECRETS_DIR = os.path.expanduser("~/.secrets/Zulip")
CHANNELS = ["agent-sync", "alerts", "builds", "trading", "ideas"]

# (full name shown in Zulip, short name, zuliprc file stem).
# Zulip appends "-bot" to the short name, in the API and in the web form alike:
# short name "codex" becomes codex-bot@.  Never end a short name with "-bot"
# (that produced codex-bot-bot@ on 2026-10-07).  File stem rule: split the seat
# tag on hyphens; parts of 2 letters or fewer stay uppercase, longer parts are
# Title Case (GROK-BUILD -> Grok-Build, BF-COMPILER -> BF-Compiler, MM -> MM).
ROSTER = [
    # Fleet seats.  Display names are Jay's choice; short name and file code follow the seat tag.
    ("Claude", "claude", "Claude"),
    ("Codex", "codex", "Codex"),
    ("Antigravity", "ag", "AG"),
    ("Cursor", "cursor", "Cursor"),
    ("GROK-BUILD", "grok-build", "Grok-Build"),
    ("Clutch", "clutch", "Clutch"),
    ("FX", "fx", "FX"),
    ("MiniMax", "mm", "MM"),
    ("Muse Code", "mc", "MC"),
    ("Rob (Muse)", "muse-assist", "MA"),
    # BotFleet role bots
    ("BF-Builder", "bf-builder", "BF-Builder"),
    ("BF-Compiler", "bf-compiler", "BF-Compiler"),
    ("BF-Deployer", "bf-deployer", "BF-Deployer"),
    ("BF-Designer", "bf-designer", "BF-Designer"),
    ("BF-Fixer", "bf-fixer", "BF-Fixer"),
    ("BF-Housekeeper", "bf-housekeeper", "BF-Housekeeper"),
    ("BF-Monitor", "bf-monitor", "BF-Monitor"),
    ("BF-Oracle", "bf-oracle", "BF-Oracle"),
    ("BF-Plumber", "bf-plumber", "BF-Plumber"),
    ("BF-Publisher", "bf-publisher", "BF-Publisher"),
]


class ZulipError(Exception):
    pass


def call(email, key, method, path, data=None):
    auth = "Basic " + base64.b64encode(f"{email}:{key}".encode()).decode()
    url = f"{REALM}/api/v1/{path}"
    body = None
    if data is not None and method == "GET":
        url += "?" + urllib.parse.urlencode(data)
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": auth, "User-Agent": "agent-sync-provision/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as err:
        try:
            payload = json.load(err)
        except Exception:
            raise ZulipError(f"HTTP {err.code} on {method} {path}") from None
    except urllib.error.URLError as err:
        raise ZulipError(f"network error on {method} {path}: {err.reason}") from None
    if payload.get("result") != "success":
        raise ZulipError(f"{method} {path}: {payload.get('msg', 'error')} ({payload.get('code', '')})")
    return payload


def read_rc(path):
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise SystemExit(f"cannot read {path}")
    api = cp["api"]
    return api["email"], api["key"], api.get("site", REALM).rstrip("/")


def owner_credentials(args):
    path = args.owner_rc or os.path.join(SECRETS_DIR, "Jay-zuliprc")
    if os.path.exists(path):
        if os.stat(path).st_mode & 0o077:
            raise SystemExit(f"{path} is readable by other users; run chmod 600 on it first")
        email, key, site = read_rc(path)
        source = path
    elif args.owner_rc:
        raise SystemExit(f"{args.owner_rc} not found")
    elif os.environ.get("ZULIP_OWNER_EMAIL") and os.environ.get("ZULIP_OWNER_API_KEY"):
        email, key = os.environ["ZULIP_OWNER_EMAIL"].strip(), os.environ["ZULIP_OWNER_API_KEY"].strip()
        site, source = REALM, "env ZULIP_OWNER_EMAIL/ZULIP_OWNER_API_KEY"
    else:
        email = input("Your Zulip login email: ").strip()
        key = getpass.getpass("Your personal Zulip API key (not echoed): ").strip()
        site, source = REALM, "prompt"
    if urllib.parse.urlparse(site).hostname != urllib.parse.urlparse(REALM).hostname:
        raise SystemExit(f"owner credential is for {site}, expected {REALM}")
    return email, key, source


def write_rc(path, email, key):
    text = f"[api]\nemail={email}\nkey={key}\nsite={REALM}\n"
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--owner-rc", help="path to YOUR personal zuliprc (not a bot's)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--force", action="store_true", help="overwrite existing zuliprc files")
    ap.add_argument("--only", action="append", help="limit to these full names (repeatable)")
    args = ap.parse_args(argv)

    email, key, source = owner_credentials(args)
    me = call(email, key, "GET", "users/me")
    if me.get("is_bot"):
        raise SystemExit("that credential is a bot; bots cannot create bots.  Use your personal API key.")
    print(f"Acting as {me.get('full_name')} <{email}> (credentials from {source})")

    owned = {b["full_name"]: b for b in call(email, key, "GET", "bots").get("bots", [])}
    everyone = {u["full_name"]: u for u in call(email, key, "GET", "users").get("members", [])}
    roster = [r for r in ROSTER if not args.only or r[0] in args.only]

    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    plan = []
    for full_name, short_name, stem in roster:
        path = os.path.join(SECRETS_DIR, f"{stem}-zuliprc")
        if full_name in owned:
            action = "exists (yours)"
        elif full_name in everyone:
            action = "SKIP: name taken by a user or bot you do not own"
        else:
            action = f"create {short_name}-bot@"
        file_action = "keep file" if os.path.exists(path) and not args.force else "write file"
        plan.append((full_name, short_name, path, action, file_action))
        print(f"  {full_name:<16} {action:<44} {file_action:<10} {path}")

    if args.dry_run:
        print("Dry run: nothing created or written.")
        return 0
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Stopped.")
        return 1

    failures = 0
    for full_name, short_name, path, action, file_action in plan:
        try:
            if action.startswith("SKIP"):
                continue
            if action.startswith("create"):
                call(email, key, "POST", "bots",
                     {"full_name": full_name, "short_name": short_name, "bot_type": 1})
                owned = {b["full_name"]: b for b in call(email, key, "GET", "bots").get("bots", [])}
            bot = owned.get(full_name)
            if not bot or not bot.get("api_key"):
                raise ZulipError("bot created but its key was not returned by GET /bots")
            bot_email, bot_key = bot.get("email") or bot.get("username"), bot["api_key"]
            if file_action == "write file":
                write_rc(path, bot_email, bot_key)
            call(bot_email, bot_key, "POST", "users/me/subscriptions",
                 {"subscriptions": json.dumps([{"name": c} for c in CHANNELS])})
            print(f"  ok  {full_name:<16} {bot_email}  file={'written' if file_action == 'write file' else 'kept'}")
        except ZulipError as err:
            failures += 1
            print(f"  ERR {full_name:<16} {err}", file=sys.stderr)
    print(f"Done: {len(plan) - failures} ok, {failures} failed.  Files are in {SECRETS_DIR} with mode 600.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
