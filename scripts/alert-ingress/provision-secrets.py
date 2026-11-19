#!/usr/bin/env python3
"""Provision the alert-ingress Worker's secret bindings from Infisical.

Dry run by default.  `--apply` writes.

Infisical first, the Worker second.  A value that exists only in the Worker is
a value Infisical cannot govern, so a rotation there would not reach the
integration; the Worker is a copy, never the source of truth.

What this does, in order:

  1. Reads the per-vendor Zulip bot pairs, the generated secrets and the
     vendor-issued Sentry secret from AFC prod /zulip in ONE batched read.
     These are generic bots that already exist; nothing is created and no
     account is made.  The PagerDuty pair is optional until its bot exists.
  2. Reads or creates four generated secrets in the same folder:
     INGRESS_SHARED_TOKEN, DATADOG_WEBHOOK_SECRET, BETTERSTACK_WEBHOOK_SECRET
     and PAGERDUTY_WEBHOOK_SECRET.  An existing value is reused, never
     replaced, so a re-run is idempotent and a rotation in Infisical
     propagates.  SENTRY_WEBHOOK_SECRET is Sentry's own client secret and is
     never generated.
  3. PUTs the values to the Worker as secret_text bindings, one binding per
     PUT, under the names the Worker asks for
     (workers/alert-ingress/src/index.js, credentialSlug()).

Every credential is read from the environment first and the canonical named reader
second, so a value the environment already holds is never fetched again.  Folder reads and
generated-secret writes use separate, caller-scoped Infisical service tokens.

Names and lengths are printed; no value is ever printed, logged or persisted.
The values live in this process's memory for the length of the run.

Generated-secret writes retain the separate scoped AFC write token and the
existing direct-write approval and unknown-outcome contract.
"""

import json
import os
import pathlib
import secrets as _secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from itertools import chain

# This checkout/payload must carry the canonical library beside scripts/alert-ingress.
# A missing sibling never authorizes an executable, user-site or PATH fallback.
SCRIPTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
CANONICAL_READER = SCRIPTS_ROOT / "fleet_infisical.py"
EXIT_MISSING_EXECUTABLE = 127  # Compatibility exit: required code payload is unavailable.
sys.path.insert(0, str(SCRIPTS_ROOT))
try:
    if CANONICAL_READER.is_symlink() or not CANONICAL_READER.is_file():
        raise ImportError()
    import fleet_infisical as scoped_secrets

    if pathlib.Path(scoped_secrets.__file__).resolve() != CANONICAL_READER:
        raise ImportError()
    if not all(
        callable(getattr(scoped_secrets, name, None))
        for name in ("bounded_fetch_project", "infisical_project_id", "verify_service_token")
    ):
        raise ImportError()
except (Exception, SystemExit):
    print("STOP: shared Infisical reader is unavailable; nothing was written.", file=sys.stderr)
    raise SystemExit(EXIT_MISSING_EXECUTABLE) from None

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
    ("PAGERDUTY", "ZULIP_PAGERDUTY_BOT_EMAIL", "ZULIP_BOT_EMAIL_PAGERDUTY"),
    ("PAGERDUTY", "ZULIP_PAGERDUTY_BOT_API_KEY", "ZULIP_BOT_API_KEY_PAGERDUTY"),
)

# Sources whose bot pair may not exist yet.  A missing pair is skipped with a
# line instead of stopping the run:  the Worker fails that source closed until
# the pair is in Infisical, and every other source is still provisioned.
OPTIONAL_SOURCES = frozenset({"PAGERDUTY"})

# Generated once, stored in Infisical, copied to the Worker.  Each is a value
# this fleet chooses:  a bearer token, or a static header the vendor is told to
# send.
GENERATED = (
    "INGRESS_SHARED_TOKEN",
    "DATADOG_WEBHOOK_SECRET",
    "BETTERSTACK_WEBHOOK_SECRET",
    "PAGERDUTY_WEBHOOK_SECRET",
)

# Issued by the vendor, never generated here.  Sentry signs every body with
# its internal integration's client secret, which Sentry creates, so a value
# made up here could never verify a real delivery.  The operator copies the
# vendor's value into Infisical;  this run copies it to the Worker when it is
# there and says so when it is not.
VENDOR_ISSUED = ("SENTRY_WEBHOOK_SECRET",)

READ_TOKEN_AFC = "ALERT_INGRESS_AFC_READ_TOKEN"
READ_TOKEN_FLEETOPS = "ALERT_INGRESS_FLEETOPS_READ_TOKEN"
WRITE_TOKEN_AFC = "ALERT_INGRESS_AFC_WRITE_TOKEN"

# The read operation retains the old subprocess's 90-second limit and timeout exit.
EXIT_TIMEOUT = 124
READ_TIMEOUT = 90.0
READ_MAX_OUTPUT_BYTES = 1024 * 1024

# The reason word for a write whose outcome the server never confirmed.  Such
# a write may already have been applied, so it is never a definite failure.
OUTCOME_UNKNOWN = "outcome_unknown"


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


def fleet_secrets_get(name, project, path):
    """Compatibility entrypoint: one canonical named read, or None when absent."""
    return fleet_secrets_get_many((name,), project, path).get(name)


def fleet_secrets_get_many(names, project, path):
    """Read only the requested names using this provisioner's existing own binding.

    Missing names are omitted by the shared reader.  No executable, CLI output,
    absence parsing, batch retry or single-name fanout participates in the read.
    """
    iterator = iter(names)
    empty = object()
    first = next(iterator, empty)
    if first is empty:
        return {}
    if (project, path) == (AFC_SLUG, SECRET_PATH):
        token_name = READ_TOKEN_AFC
    elif (project, path) == (FLEETOPS_SLUG, "/"):
        token_name = READ_TOKEN_FLEETOPS
    else:
        raise ReadError("unsupported provisioner read purpose")
    read_token = require_service_token((os.environ.get(token_name) or "").strip(), token_name)
    try:
        project_id = (
            AFC
            if project == AFC_SLUG
            else scoped_secrets.infisical_project_id(
                os.environ, scoped_secrets.home_dir(os.environ), FLEETOPS_SLUG
            )
        )
        env = {
            key: os.environ[key]
            for key in (scoped_secrets.ENV_API, *scoped_secrets.ENV_NAMES)
            if key in os.environ
        }
        env[scoped_secrets.ENV_READ_PATHS] = path
        return scoped_secrets.bounded_fetch_project(
            project_id,
            path,
            env,
            token=read_token,
            names=chain((first,), iterator),
            timeout=READ_TIMEOUT,
            writer=False,
            max_output_bytes=READ_MAX_OUTPUT_BYTES,
            strict_records=True,
        )
    except TimeoutError:
        raise ReadError("Infisical scoped read timed out", EXIT_TIMEOUT) from None
    except scoped_secrets.SecretsError as error:
        if error.message == "bounded-read-timeout":
            raise ReadError("Infisical scoped read timed out", EXIT_TIMEOUT) from None
        raise ReadError("Infisical scoped read failed") from None


def batch_values(names, project, path):
    """Every named value:  the environment first, one bounded named read second.

    A name the folder does not hold is an error here, not a silent gap:  the
    caller asked for a value it cannot do without.  The names are ever
    reported;  the values never are.
    """
    values = {}
    missing = []
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            values[name] = value
        else:
            missing.append(name)
    if missing:
        fetched = fleet_secrets_get_many(missing, project, path)
        for name in missing:
            value = fetched.get(name)
            if not value:
                raise ReadError(f"{name} is absent from {project}{path}")
            values[name] = value
    return values


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


def require_service_token(token, name):
    if (
        not isinstance(token, str)
        or not token.startswith("st.")
        or not 20 <= len(token) <= 2048
        or any(c.isspace() for c in token)
    ):
        raise ReadError(f"{name} is missing or invalid")
    return token


def infisical_upsert(token, name, value):
    """One secret written into AFC prod /zulip.

    Infisical creates the path when it is new, so the four generated secrets
    land beside the bot pairs.
    """
    token = require_service_token(token, WRITE_TOKEN_AFC)
    env = {key: item for key, item in os.environ.items() if key not in scoped_secrets.AUTH_ENV_VARS}
    env[scoped_secrets.ENV_WRITE_PATHS] = SECRET_PATH
    try:
        scoped_secrets.verify_service_token(token, AFC, SECRET_PATH, env, writer=True)
    except scoped_secrets.SecretsError:
        raise ReadError(f"{WRITE_TOKEN_AFC} scope verification failed") from None
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

    # 1. Every value this run needs, read in one canonical named batch.  The
    #    environment comes first for every name, so a value the environment
    #    already holds is never fetched again.
    bot_names = [infisical_name for _source, infisical_name, _binding in BOT_PAIRS]
    wanted = bot_names + list(GENERATED) + list(VENDOR_ISSUED)
    from_env = {name: os.environ.get(name, "").strip() for name in wanted}
    to_fetch = [name for name in wanted if not from_env[name]]
    try:
        fetched = fleet_secrets_get_many(to_fetch, AFC_SLUG, SECRET_PATH)
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return error.code

    values = {}
    present = {
        name: from_env.get(name) or fetched.get(name) for _source, name, _binding in BOT_PAIRS
    }
    skipped = sorted(
        source
        for source in OPTIONAL_SOURCES
        if not all(present[name] for src, name, _b in BOT_PAIRS if src == source)
    )
    try:
        for source, infisical_name, binding in BOT_PAIRS:
            if source in skipped:
                continue
            value = present[infisical_name]
            if value is None:
                raise ReadError(f"{infisical_name} is absent from {AFC_SLUG}{SECRET_PATH}")
            values[binding] = value
            print(f"  read  {infisical_name} -> {binding}  (len {len(value)})")
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return error.code
    for source in skipped:
        print(
            f"  skip  the {source} bot pair is not in {AFC_SLUG}{SECRET_PATH} yet;  that source stays closed"
        )
    for name in VENDOR_ISSUED:
        value = from_env[name] or fetched.get(name)
        if value is None:
            print(f"  skip  {name} is vendor-issued and not in {AFC_SLUG}{SECRET_PATH} yet")
            continue
        values[name] = value
        print(f"  read  {name}  (len {len(value)})")

    # 2. The existing own Fleet-OPS root reader fetches only the Cloudflare
    #    token in apply mode, before anything is written.  No machine identity
    #    or other consumer binding participates in this read.
    fleetops_names = []
    if not dry:
        fleetops_names.append("CLOUDFLARE_FLEET_API_TOKEN")
    try:
        fleetops = batch_values(fleetops_names, FLEETOPS_SLUG, "/")
    except ReadError as error:
        print(f"STOP: {error}.  Nothing was written.")
        return error.code

    # 3. The generated secrets:  read what exists, create what does not.
    created = []
    infisical_token = (os.environ.get(WRITE_TOKEN_AFC) or "").strip()
    if not dry:
        try:
            infisical_token = require_service_token(infisical_token, WRITE_TOKEN_AFC)
        except ReadError as error:
            print(f"STOP: {error}.  Nothing was written.")
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

    # 4. The Worker bindings.  The Cloudflare token came with the fleet-ops
    #    named batch above; no additional credential read occurs here.
    cloudflare_token = fleetops["CLOUDFLARE_FLEET_API_TOKEN"]
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
