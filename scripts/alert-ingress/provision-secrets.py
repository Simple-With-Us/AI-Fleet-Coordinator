#!/usr/bin/env python3
"""Provision the alert-ingress Worker's secret bindings from Infisical.

Dry run by default.  `--apply` writes.

Infisical first, the Worker second.  A value that exists only in the Worker is
a value Infisical cannot govern, so a rotation there would not reach the
integration; the Worker is a copy, never the source of truth.

What this does, in order:

  1. Reads the six per-vendor Zulip bot pairs from AFC prod /zulip.  These are
     generic bots that already exist; nothing is created and no account is
     made.
  2. Reads or creates four generated secrets in the same folder:
     INGRESS_SHARED_TOKEN, DATADOG_WEBHOOK_SECRET, BETTERSTACK_WEBHOOK_SECRET
     and SENTRY_WEBHOOK_SECRET.  An existing value is reused, never replaced,
     so a re-run is idempotent and a rotation in Infisical propagates.
  3. PUTs the ten values to the Worker as secret_text bindings, one binding
     per PUT, under the names the Worker asks for
     (workers/alert-ingress/src/index.js, credentialSlug()).

Names and lengths are printed; no value is ever printed, logged or persisted.
The values live in this process's memory for the length of the run.

`fleet-secrets set` cannot write these names (its allowlist covers CACHE_*
under /linear and the CLI logins), so the write goes through the automation
identity's API, the pattern scripts/agent-sync-mcp/scripts/populate-scoped-folders.py
already uses.
"""

import json
import os
import pathlib
import secrets as _secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Project identifiers, pinned: a copy must read from and write to exactly this
# project (populate-scoped-folders.py, kody 4365623903).  Neither is a credential.
AFC = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
AFC_SLUG = "ai-fleet-coordinator"
FLEETOPS_SLUG = "fleet-ops"
ENV = "prod"

WORKER = "alert-ingress"
ACCOUNT = "3a9368057468d0909cafaa85df12d1b7"

INFISICAL_BASE = "https://app.infisical.com"
CLOUDFLARE_BASE = "https://api.cloudflare.com/client/v4"

USER_AGENT = "AI-Fleet-Coordinator alert-ingress provisioner"

# The folder the alert credentials live in.  The bot pairs are already there;
# the four generated secrets land beside them.
SECRET_PATH = "/zulip"

# Infisical name -> Worker binding name.
#
# The bot pairs keep their own names in Infisical and are copied under the
# names the Worker's credentialSlug() asks for; the generated secrets already
# carry the Worker's name, so their copy is one for one.
BOT_PAIRS = (
    ("DATADOG", "ZULIP_DATADOG_BOT_EMAIL", "ZULIP_BOT_EMAIL_DATADOG"),
    ("DATADOG", "ZULIP_DATADOG_BOT_API_KEY", "ZULIP_BOT_API_KEY_DATADOG"),
    ("BETTERSTACK", "ZULIP_BETTERSTACK_BOT_EMAIL", "ZULIP_BOT_EMAIL_BETTERSTACK"),
    ("BETTERSTACK", "ZULIP_BETTERSTACK_BOT_API_KEY", "ZULIP_BOT_API_KEY_BETTERSTACK"),
    ("SENTRY", "ZULIP_SENTRY_BOT_EMAIL", "ZULIP_BOT_EMAIL_SENTRY"),
    ("SENTRY", "ZULIP_SENTRY_BOT_API_KEY", "ZULIP_BOT_API_KEY_SENTRY"),
)

# Generated once, stored in Infisical, copied to the Worker.
GENERATED = (
    "INGRESS_SHARED_TOKEN",
    "DATADOG_WEBHOOK_SECRET",
    "BETTERSTACK_WEBHOOK_SECRET",
    "SENTRY_WEBHOOK_SECRET",
)

FLEET_SECRETS_ABSENT = 3


class ReadError(Exception):
    """A read or write failed.  Carries a reason word only:  no payload kept."""


def fleet_secrets_binary():
    return shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )


def fleet_secrets_get(name, project, path):
    """One value through `fleet-secrets get`, or None when it is absent.

    fleet-secrets expands Infisical references and refuses an unresolved one,
    so a literal ${...} is never returned.  Any other failure raises with a
    reason word only;  stderr is discarded unread so no server text leaks.
    """
    argv = [
        fleet_secrets_binary(),
        "get",
        name,
        "--path",
        path,
        "--project",
        project,
    ]
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ReadError("timeout") from None
    except FileNotFoundError:
        raise ReadError("missing-executable") from None
    except OSError:
        raise ReadError("unavailable") from None
    if proc.returncode == FLEET_SECRETS_ABSENT:
        return None
    if proc.returncode != 0:
        raise ReadError(f"exit-{proc.returncode}")
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError("bad-output") from None


def handoff(name, project, path):
    """One credential:  the environment first, then fleet-secrets.

    The name is ever reported;  the value never is.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = fleet_secrets_get(name, project, path)
    if value is None:
        raise ReadError(f"{name} is absent")
    return value


def api(method, url, token, data=None, timeout=35):
    """One JSON API call.  Returns the parsed answer, or raises."""
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as error:
        raise ReadError(f"http-{error.code}") from None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            raise ReadError("timeout") from None
        raise ReadError("network") from None
    except TimeoutError:
        raise ReadError("timeout") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ReadError("not-json") from None


def infisical_login():
    """The automation identity's access token.  Never printed."""
    client_id = handoff("INFISICAL_AUTOMATION_CLIENT_ID", FLEETOPS_SLUG, "/")
    client_secret = handoff("INFISICAL_AUTOMATION_CLIENT_SECRET", FLEETOPS_SLUG, "/")
    answer = api(
        "POST",
        f"{INFISICAL_BASE}/api/v1/auth/universal-auth/login",
        None,
        {"clientId": client_id, "clientSecret": client_secret},
    )
    token = answer.get("accessToken") if isinstance(answer, dict) else None
    if not isinstance(token, str) or not token:
        raise ReadError("login-refused")
    return token


def infisical_upsert(token, name, value):
    """One secret written into AFC prod /zulip.

    Infisical creates the path when it is new, so the four generated secrets
    land beside the bot pairs.
    """
    api(
        "POST",
        f"{INFISICAL_BASE}/api/v3/secrets/raw/{urllib.parse.quote(name)}",
        token,
        {
            "workspaceId": AFC,
            "environment": ENV,
            "secretPath": SECRET_PATH,
            "secretValue": value,
            "type": "shared",
        },
    )


def worker_put(token, name, value):
    """One Worker's secret binding.

    The endpoint takes one bare binding object per PUT -- {"name","text","type"}
    -- and answers 201.  The {"put": [...]} wrapper the other Cloudflare
    endpoints use is refused here with "Binding must have a name.", because the
    wrapper's own key reads as a binding that has no name.
    """
    answer = api(
        "PUT",
        f"{CLOUDFLARE_BASE}/accounts/{ACCOUNT}/workers/scripts/{WORKER}/secrets",
        token,
        {"name": name, "text": value, "type": "secret_text"},
    )
    if not isinstance(answer, dict) or answer.get("success") is not True:
        raise ReadError("worker-put-refused")


def main():
    dry = "--apply" not in sys.argv
    mode = "DRY RUN - no writes" if dry else "APPLYING"

    print(f"{mode}: alert-ingress secret provisioning")

    # 1. The bot pairs, read from Infisical.
    values = {}
    try:
        for _source, infisical_name, binding in BOT_PAIRS:
            value = fleet_secrets_get(infisical_name, AFC_SLUG, SECRET_PATH)
            if value is None:
                raise ReadError(f"{infisical_name} is absent from {AFC_SLUG}{SECRET_PATH}")
            values[binding] = value
            print(f"  read  {infisical_name} -> {binding}  (len {len(value)})")
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return 1

    # 2. The generated secrets:  read what exists, create what does not.
    created = []
    try:
        infisical_token = infisical_login()
    except ReadError as error:
        print(f"STOP: the Infisical login failed ({error}).  Nothing was written.")
        return 1

    for name in GENERATED:
        existing = fleet_secrets_get(name, AFC_SLUG, SECRET_PATH)
        if existing is not None:
            values[name] = existing
            print(f"  read  {name}  (len {len(existing)})")
            continue
        fresh = _secrets.token_urlsafe(32)
        if dry:
            values[name] = fresh
            created.append(name)
            print(f"  would create  {name}  (len {len(fresh)})")
            continue
        try:
            infisical_upsert(infisical_token, name, fresh)
        except ReadError as error:
            print(
                f"STOP: {name} could not be written to Infisical ({error}).  "
                "The Worker was not touched."
            )
            return 1
        values[name] = fresh
        created.append(name)
        print(f"  wrote {name} to Infisical {AFC_SLUG}{SECRET_PATH}  (len {len(fresh)})")

    if dry:
        print(
            f"Dry run: {len(values)} bindings would be set on {WORKER}.  "
            "Re-run with --apply."
        )
        return 0

    # 3. The Worker bindings.
    cloudflare_token = handoff("CLOUDFLARE_FLEET_API_TOKEN", FLEETOPS_SLUG, "/")
    bound = 0
    for name, value in values.items():
        try:
            worker_put(cloudflare_token, name, value)
        except ReadError as error:
            print(
                f"STOP: the Worker refused the binding for {name} ({error}).  "
                f"{bound} of {len(values)} bindings are set;  Infisical holds every "
                "value;  re-run to reconcile."
            )
            return 1
        bound += 1
        print(f"  bound {name}  (len {len(value)})")
    print(
        f"Done: {len(values)} bindings on {WORKER}, "
        f"{len(created)} secret(s) created in Infisical."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
