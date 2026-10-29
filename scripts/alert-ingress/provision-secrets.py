#!/usr/bin/env python3
"""Provision the alert-ingress Worker's secret bindings from Infisical.

Dry run by default.  `--apply` writes.

Infisical first, the Worker second.  A value that exists only in the Worker is
a value Infisical cannot govern, so a rotation there would not reach the
integration; the Worker is a copy, never the source of truth.

What this does, in order:

  1. Reads the six per-vendor Zulip bot pairs and the four generated secrets
     from AFC prod /zulip in ONE batched read.  These are generic bots that
     already exist; nothing is created and no account is made.
  2. Reads or creates four generated secrets in the same folder:
     INGRESS_SHARED_TOKEN, DATADOG_WEBHOOK_SECRET, BETTERSTACK_WEBHOOK_SECRET
     and SENTRY_WEBHOOK_SECRET.  An existing value is reused, never replaced,
     so a re-run is idempotent and a rotation in Infisical propagates.
  3. PUTs the ten values to the Worker as secret_text bindings, one binding
     per PUT, under the names the Worker asks for
     (workers/alert-ingress/src/index.js, credentialSlug()).

Every credential is read from the environment first and from `fleet-secrets`
second, so a value the environment already holds is never fetched again.

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
import re
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

# Exit codes the shell reads:  124 is a timeout, 127 a missing executable.
EXIT_TIMEOUT = 124
EXIT_MISSING_EXECUTABLE = 127

# The reason word for a write whose outcome the server never confirmed.  Such
# a write may already have been applied, so it is never a definite failure.
OUTCOME_UNKNOWN = "outcome_unknown"

# `fleet-secrets get` answers a name the folder does not hold with this fixed
# sentence and exit code 3.  Only the sentence is read, and only names that
# were asked for are accepted, so nothing else in the stream can be mistaken
# for a name.
ABSENT_PATTERN = re.compile(r"key ([A-Za-z0-9_]+(?:, [A-Za-z0-9_]+)*) is not in Infisical at ")


class ReadError(Exception):
    """A read or write failed.  Carries a reason word only:  no payload kept."""

    def __init__(self, reason, code=1):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is refused, not followed.

    Every request here carries a bearer token, and `urlopen` forwards the
    `Authorization` header to wherever a redirect points, so a redirect could
    hand the credential to another origin.  The opener below refuses them.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


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
        raise ReadError(f"{name}:  fleet-secrets timed out", EXIT_TIMEOUT) from None
    except FileNotFoundError:
        raise ReadError(
            f"{name}:  fleet-secrets is not installed", EXIT_MISSING_EXECUTABLE
        ) from None
    except OSError as error:
        raise ReadError(
            f"{name}:  fleet-secrets could not run ({type(error).__name__})"
        ) from None
    if proc.returncode == FLEET_SECRETS_ABSENT:
        return None
    if proc.returncode != 0:
        raise ReadError(f"{name}:  fleet-secrets exited {proc.returncode}")
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError(f"{name}:  fleet-secrets returned unreadable output")


def _absent_names(stderr, requested):
    """The names the CLI reported as absent, and only those."""
    if stderr is None:
        return set()
    text = stderr.decode("utf-8", "replace")
    absent = set()
    for match in ABSENT_PATTERN.finditer(text):
        for name in match.group(1).split(", "):
            if name in requested:
                absent.add(name)
    return absent


def fleet_secrets_batch(names, project, path):
    """The command that reads several names in ONE Infisical export.

    `fleet-secrets get NAME...` exports the folder once and answers with a JSON
    object of name to value.  The copy installed on a machine may still be the
    older one that takes a single name, so the repo's own copy is preferred
    when it sits next to this script.
    """
    package = pathlib.Path(__file__).resolve().parent.parent / "fleet_secrets"
    if (package / "__main__.py").is_file():
        env = dict(os.environ)
        root = str(package.parent)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
        return (
            [
                sys.executable,
                "-m",
                "fleet_secrets",
                "get",
                *names,
                "--path",
                path,
                "--project",
                project,
            ],
            env,
        )
    return (
        [fleet_secrets_binary(), "get", *names, "--path", path, "--project", project],
        None,
    )


def fleet_secrets_get_many(names, project, path):
    """Every requested name in ONE Infisical read.

    `fleet-secrets get NAME...` exports the whole folder once and answers with
    the names it holds, so ten per-name calls -- ten exports of the same
    folder -- become one.  A name the folder does not hold is reported as
    absent, not as an error:  the caller decides which ones it may do without.

    The CLI names the absent ones in its own fixed sentence on stderr;  only
    that sentence is read, and only names that were asked for are accepted.
    """
    if not names:
        return {}
    found = {}
    pending = list(names)
    while pending:
        argv, env = fleet_secrets_batch(pending, project, path)
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise ReadError(
                "fleet-secrets timed out reading the folder", EXIT_TIMEOUT
            ) from None
        except FileNotFoundError:
            raise ReadError(
                "fleet-secrets is not installed", EXIT_MISSING_EXECUTABLE
            ) from None
        except OSError as error:
            raise ReadError(
                f"fleet-secrets could not run ({type(error).__name__})"
            ) from None
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ReadError("fleet-secrets returned unreadable output") from None
            if not isinstance(payload, dict):
                raise ReadError("fleet-secrets returned an unexpected shape")
            found.update(
                {name: payload[name] for name in pending if name in payload}
            )
            break
        if proc.returncode == FLEET_SECRETS_ABSENT:
            absent = _absent_names(proc.stderr, pending)
            if absent:
                pending = [name for name in pending if name not in absent]
                continue
        elif proc.returncode != 2:
            raise ReadError(f"fleet-secrets exited {proc.returncode}")
        # The slow path:  the CLI could not take the whole batch, so every
        # remaining name is read on its own.
        for name in pending:
            value = fleet_secrets_get(name, project, path)
            if value is not None:
                found[name] = value
        break
    return found


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


def api(method, url, token, data=None, timeout=35, write=None):
    """One JSON API call.  Returns the parsed answer, or raises.

    `write` says whether the call changes something.  A write that fails with
    a server error, a timeout or a broken connection may still have been
    applied, so it is reported as an unknown outcome and never as a definite
    failure;  a permanent 4xx is a refusal, which is a different thing.
    """
    if write is None:
        write = method.upper() != "GET"
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read(256 * 1024)
    except urllib.error.HTTPError as error:
        if write and (error.code >= 500 or error.code == 408):
            raise ReadError(OUTCOME_UNKNOWN) from None
        if 400 <= error.code < 500:
            raise ReadError(f"refused-http-{error.code}") from None
        raise ReadError(f"http-{error.code}") from None
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, TimeoutError):
            raise ReadError(OUTCOME_UNKNOWN if write else "timeout") from None
        raise ReadError(OUTCOME_UNKNOWN if write else "network") from None
    except TimeoutError:
        raise ReadError(OUTCOME_UNKNOWN if write else "timeout") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise ReadError("not-json") from None


def infisical_login():
    """The automation identity's access token.  Never printed."""
    names = ("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET")
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        values.update(fleet_secrets_get_many(missing, FLEETOPS_SLUG, "/"))
    client_id = values["INFISICAL_AUTOMATION_CLIENT_ID"]
    client_secret = values["INFISICAL_AUTOMATION_CLIENT_SECRET"]
    if not client_id:
        raise ReadError("INFISICAL_AUTOMATION_CLIENT_ID is absent")
    if not client_secret:
        raise ReadError("INFISICAL_AUTOMATION_CLIENT_SECRET is absent")
    answer = api(
        "POST",
        f"{INFISICAL_BASE}/api/v1/auth/universal-auth/login",
        None,
        {"clientId": client_id, "clientSecret": client_secret},
        write=False,
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

    # 1. Every value this run needs, read in ONE Infisical export.  The
    #    environment comes first for every name, so a value the environment
    #    already holds is never fetched again.
    bot_names = [infisical_name for _source, infisical_name, _binding in BOT_PAIRS]
    wanted = bot_names + list(GENERATED)
    from_env = {name: os.environ.get(name, "").strip() for name in wanted}
    to_fetch = [name for name in wanted if not from_env[name]]
    try:
        fetched = fleet_secrets_get_many(to_fetch, AFC_SLUG, SECRET_PATH)
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return error.code

    values = {}
    try:
        for _source, infisical_name, binding in BOT_PAIRS:
            value = from_env[infisical_name] or fetched.get(infisical_name)
            if value is None:
                raise ReadError(f"{infisical_name} is absent from {AFC_SLUG}{SECRET_PATH}")
            values[binding] = value
            print(f"  read  {infisical_name} -> {binding}  (len {len(value)})")
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return error.code

    # 2. The generated secrets:  read what exists, create what does not.
    created = []
    try:
        infisical_token = infisical_login()
    except ReadError as error:
        print(f"STOP: the Infisical login failed ({error}).  Nothing was written.")
        return error.code

    for name in GENERATED:
        existing = from_env[name] or fetched.get(name)
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
            if error.reason == OUTCOME_UNKNOWN:
                print(
                    f"STOP: the answer for {name} did not say whether Infisical took "
                    f"it ({error}).  The write may already have been applied;  re-run to "
                    "reconcile.  The Worker was not touched."
                )
            else:
                print(
                    f"STOP: {name} could not be written to Infisical ({error}).  "
                    "The Worker was not touched."
                )
            return error.code
        values[name] = fresh
        created.append(name)
        print(f"  wrote {name} to Infisical {AFC_SLUG}{SECRET_PATH}  (len {len(fresh)})")

    if dry:
        print(f"Dry run: {len(values)} bindings would be set on {WORKER}.  Re-run with --apply.")
        return 0

    # 3. The Worker bindings.
    try:
        cloudflare_token = handoff("CLOUDFLARE_FLEET_API_TOKEN", FLEETOPS_SLUG, "/")
    except ReadError as error:
        print(
            f"STOP: the Cloudflare token could not be read ({error}).  Infisical holds "
            "every value;  re-run to reconcile."
        )
        return error.code
    bound = 0
    for name, value in values.items():
        try:
            worker_put(cloudflare_token, name, value)
        except ReadError as error:
            if error.reason == OUTCOME_UNKNOWN:
                print(
                    f"STOP: the answer for the binding {name} did not say whether the "
                    f"Worker took it ({error}).  {bound} of {len(values)} bindings are "
                    "set;  Infisical holds every value;  re-run to reconcile."
                )
            else:
                print(
                    f"STOP: the Worker refused the binding for {name} ({error}).  "
                    f"{bound} of {len(values)} bindings are set;  Infisical holds every "
                    "value;  re-run to reconcile."
                )
            return error.code
        bound += 1
        print(f"  bound {name}  (len {len(value)})")
    print(
        f"Done: {len(values)} bindings on {WORKER}, {len(created)} secret(s) created in Infisical."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
