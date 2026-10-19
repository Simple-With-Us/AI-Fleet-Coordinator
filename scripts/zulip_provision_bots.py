#!/usr/bin/env python3
"""Create the fleet's Zulip seat bots and write one zuliprc per bot.  OWNER-RUN ONLY.

OWNER-RUN ONLY:  agents never run this.  Creating bot users is account creation, and
--regenerate rotates a live bot's key, which breaks that bot until Infisical is updated.
Run it in your own terminal:

    python3 scripts/zulip_provision_bots.py --dry-run              # plan only, no writes, no POSTs
    python3 scripts/zulip_provision_bots.py                        # plan, then asks y/N
    python3 scripts/zulip_provision_bots.py --yes --only "Codex"   # no prompt, one bot
    python3 scripts/zulip_provision_bots.py --only "Codex" --regenerate   # rotate an existing bot's key

Credentials (AFC-359:  the owner's key is not needed, nothing prompts, nothing under ~/.secrets):
it signs in only as the realm-admin bot (role 200, administrator).  Each of
REALM_ADMIN_ZULIP_EMAIL and REALM_ADMIN_ZULIP_KEY is read from the environment first, then
from Infisical Fleet-OPS folder /zulipadmin through `fleet-secrets get NAME --path /zulipadmin`
(binary:  env FLEET_SECRETS_BIN, else ~/.local/bin/fleet-secrets).  REALM_ADMIN_ZULIP_SITE is
read the same way and defaults to the realm when absent.  A fleet-secrets failure stops the
run with a message naming the folder and the key, never the value.

The caller must be an administrator or the owner (role <= 200);  anyone else is refused.

For each bot in ROSTER it:
  - lists the realm's users (GET /users) and finds the roster bot by display name or email,
  - creates a missing bot (POST /bots, generic bot, owned by the caller) and takes its key
    from the response,
  - leaves an existing bot alone:  Zulip never returns an existing bot's key, so it prints
    "exists:  key not recoverable here" and skips it.  With --regenerate (which needs --only)
    it rotates that bot's key (POST /bots/{id}/api_key/regenerate).  WARNING:  that
    invalidates the bot's current key everywhere, so the bot stays broken until the new key
    is stored in Infisical /zulip,
  - skips a name that belongs to a non-bot user,
  - writes <out-dir>/<file>-zuliprc (mode 600, atomically) for each new or regenerated bot.
    --out-dir defaults to ~/.agent-sync/provisioned/ (created mode 700).  An existing file is
    never overwritten without --force, and such a bot is skipped BEFORE anything is created
    or rotated, so a key can never be lost to a kept file.  Nothing is ever written under
    ~/.secrets,
  - subscribes the bot to CHANNELS with the bot's own key.

After each bot it prints, names only, what to store in Infisical Fleet-OPS folder /zulip:
<PREFIX>_ZULIP_EMAIL, <PREFIX>_ZULIP_KEY, <PREFIX>_ZULIP_SITE, where PREFIX is the file stem in
upper case with hyphens as underscores (Grok-Build -> GROK_BUILD).  Load them, then delete
the out-dir.  Keys are never printed.

What the realm-admin bot CANNOT do (needs the organization OWNER, role 100, i.e. Jay's own
login in the web UI):  change a user's role to owner, deactivate or reactivate users, and
change realm-level settings.  GET /bots answers HTTP 400 to a bot caller, so this script
never uses it.  Grok Bot (GB-*) personas are out of scope.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REALM = "https://simplewithus.zulipchat.com"
ADMIN_FOLDER = "/zulipadmin"
DEFAULT_OUT_DIR = "~/.agent-sync/provisioned"
ROLE_OWNER, ROLE_ADMIN = 100, 200


def fleet_secrets_bin():
    return os.environ.get("FLEET_SECRETS_BIN") or os.path.expanduser("~/.local/bin/fleet-secrets")


def admin_secret(name, required=True):
    """A realm-admin value:  env var of that name first, then fleet-secrets /zulipadmin.

    Never prints a value or the tool's stderr.  Returns "" for a missing optional value and
    exits with a message naming the folder and key for a missing required one.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    problem = ""
    try:
        proc = subprocess.run(
            [fleet_secrets_bin(), "get", name, "--path", ADMIN_FOLDER],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            problem = f"fleet-secrets exited {proc.returncode}"
        else:
            value = proc.stdout.rstrip("\n").strip()
            if not value:
                problem = "fleet-secrets returned an empty value"
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        problem = f"fleet-secrets could not run ({type(err).__name__})"
    if value:
        return value
    if not required:
        return ""
    raise SystemExit(
        f"missing {name} in Infisical Fleet-OPS {ADMIN_FOLDER} ({problem}).  Set env {name} or "
        f"fix `fleet-secrets get {name} --path {ADMIN_FOLDER}`."
    )


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
    ("Grok", "grok-build", "Grok-Build"),
    ("Clutch", "clutch", "Clutch"),
    ("FX", "fx", "FX"),
    ("MiniMax", "mm", "MM"),
    ("Muse Code", "mc", "MC"),
    (
        "OpenCode",
        "opencode",
        "OpenCode",
    ),  # the stem breaks the Title Case rule: SEAT_FILE_OVERRIDES in agent_sync/zulip.py
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
    elif method != "GET":
        body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Authorization": auth, "User-Agent": "agent-sync-provision/2"},
    )
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
        raise ZulipError(
            f"{method} {path}: {payload.get('msg', 'error')} ({payload.get('code', '')})"
        )
    return payload


def credentials():
    """(email, key, source):  the realm-admin bot, from env else Infisical /zulipadmin."""
    email = admin_secret("REALM_ADMIN_ZULIP_EMAIL")
    key = admin_secret("REALM_ADMIN_ZULIP_KEY")
    site = admin_secret("REALM_ADMIN_ZULIP_SITE", required=False) or REALM
    if urllib.parse.urlparse(site).hostname != urllib.parse.urlparse(REALM).hostname:
        raise SystemExit(f"credential is for {site}, expected {REALM}")
    return email, key, f"realm-admin bot (env, else Infisical {ADMIN_FOLDER})"


def infisical_prefix(stem):
    return stem.upper().replace("-", "_")


def write_rc(path, email, key):
    text = f"[api]\nemail={email}\nkey={key}\nsite={REALM}\n"
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def resolve_out_dir(out_dir):
    """The real path of out_dir;  refuses anything under ~/.secrets."""
    real = os.path.realpath(os.path.expanduser(out_dir))
    secrets = os.path.realpath(os.path.expanduser("~/.secrets"))
    if real == secrets or real.startswith(secrets + os.sep):
        raise SystemExit("refusing to write under ~/.secrets (AFC-359);  pick another --out-dir")
    return real


def make_out_dir(real):
    """Create the directory (or tighten an existing one) to mode 700;  it holds new bot keys."""
    os.makedirs(real, mode=0o700, exist_ok=True)
    if os.stat(real).st_uid == os.getuid():
        os.chmod(real, 0o700)


def expected_email(short_name):
    return f"{short_name}-bot@{urllib.parse.urlparse(REALM).hostname}"


def find_member(members, full_name, short_name):
    """The realm member a roster row refers to, by display name first, then by bot email."""
    for member in members:
        if member.get("full_name") == full_name:
            return member
    want = expected_email(short_name)
    for member in members:
        if (member.get("email") or "").lower() == want:
            return member
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--force", action="store_true", help="overwrite existing zuliprc files")
    ap.add_argument(
        "--regenerate",
        action="store_true",
        help="rotate the key of an existing roster bot (needs --only);  invalidates its current key everywhere",
    )
    ap.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"where to write the zuliprc files (default {DEFAULT_OUT_DIR}/, created mode 700)",
    )
    ap.add_argument("--only", action="append", help="limit to these full names (repeatable)")
    args = ap.parse_args(argv)
    if args.regenerate and not args.only:
        raise SystemExit("--regenerate needs --only NAME:  it invalidates the bot's current key")

    email, key, source = credentials()
    me = call(email, key, "GET", "users/me")
    role = me.get("role")
    if not isinstance(role, int) or role > ROLE_ADMIN:
        raise SystemExit(
            f"{me.get('full_name')} has role {role};  creating bots here needs an administrator "
            f"(200) or the owner (100).  Ask Jay."
        )
    print(f"Acting as {me.get('full_name')} <{email}> role {role} (credentials from {source})")

    members = call(email, key, "GET", "users").get("members", [])
    roster = [r for r in ROSTER if not args.only or r[0] in args.only]
    unknown = sorted(set(args.only or []) - {r[0] for r in ROSTER})
    if unknown:
        raise SystemExit(f"--only names not in ROSTER: {', '.join(unknown)}")

    out_dir = resolve_out_dir(args.out_dir)

    plan = []  # (full_name, short_name, stem, path, kind, member, note)
    for full_name, short_name, stem in roster:
        path = os.path.join(out_dir, f"{stem}-zuliprc")
        member = find_member(members, full_name, short_name)
        file_kept = os.path.exists(path) and not args.force
        if member is None:
            kind, note = "create", f"create {short_name}-bot@"
        elif not member.get("is_bot"):
            kind, note = "skip", "SKIP: name taken by a non-bot user"
        elif args.regenerate:
            kind = "regenerate"
            note = "regenerate key (WARNING: invalidates its current key everywhere;  update Infisical /zulip)"
        else:
            kind = "skip"
            note = "exists:  key not recoverable here;  rerun with --regenerate to rotate it"
        if kind in ("create", "regenerate") and file_kept:
            kind, note = "skip", f"SKIP: {path} exists;  use --force or remove it first"
        plan.append((full_name, short_name, stem, path, kind, member, note))
        print(f"  {full_name:<16} {note}")
        if kind in ("create", "regenerate"):
            print(f"  {'':<16} -> {path}")

    if args.dry_run:
        print("Dry run: nothing created, rotated or written.")
        return 0
    if not any(p[4] in ("create", "regenerate") for p in plan):
        print("Nothing to create or rotate.")
        return 0
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Stopped.")
        return 1

    make_out_dir(out_dir)
    failures = 0
    done = 0
    for full_name, short_name, stem, path, kind, member, note in plan:
        if kind == "skip":
            continue
        try:
            if kind == "create":
                made = call(
                    email,
                    key,
                    "POST",
                    "bots",
                    {"full_name": full_name, "short_name": short_name, "bot_type": 1},
                )
                bot_id, bot_key = made.get("user_id"), made.get("api_key")
                bot_email = made.get("email")
                if not bot_email and bot_id is not None:
                    fresh = call(email, key, "GET", "users").get("members", [])
                    bot_email = next(
                        (u.get("email") for u in fresh if u.get("user_id") == bot_id), None
                    )
                bot_email = bot_email or expected_email(short_name)
            else:
                bot_id, bot_email = member["user_id"], member["email"]
                bot_key = call(email, key, "POST", f"bots/{bot_id}/api_key/regenerate").get(
                    "api_key"
                )
            if not bot_key:
                raise ZulipError("Zulip did not return an API key for this bot")
            write_rc(path, bot_email, bot_key)
            call(
                bot_email,
                bot_key,
                "POST",
                "users/me/subscriptions",
                {"subscriptions": json.dumps([{"name": c} for c in CHANNELS])},
            )
            prefix = infisical_prefix(stem)
            verb = "created" if kind == "create" else "key rotated"
            print(f"  ok  {full_name:<16} {bot_email}  {verb}, file written: {path}")
            print(
                f"      Infisical /zulip: {prefix}_ZULIP_EMAIL, {prefix}_ZULIP_KEY, {prefix}_ZULIP_SITE"
            )
            done += 1
        except ZulipError as err:
            failures += 1
            print(f"  ERR {full_name:<16} {err}", file=sys.stderr)
    print(
        f"Done: {done} ok, {failures} failed.  Files are in {out_dir} with mode 600;  "
        "load the values into Infisical /zulip, then delete the directory."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
