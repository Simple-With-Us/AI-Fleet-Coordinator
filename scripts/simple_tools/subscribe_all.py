#!/usr/bin/env python3
"""Subscribe every fleet bot to every public Zulip channel, each with its own key.

Owner ruling, Sat, Oct 10, 2026:  "I want bots to wake to any channel or DM" ... "not just
simple-tools or one other since we may start using the other channels more for things".  Zulip
delivers a channel message only to a bot subscribed to that channel, so a mention in a channel
the bot never joined never reaches the listener.  This script closes that gap for the channels
that exist today;  the listener (scripts/simple_tools/daemon.py, `auto_subscribe`) keeps it closed
for channels created later.

Idempotent.  Dry run by default;  `--apply` subscribes.  Output is seat tags and channel names
only:  never an email, a key or a message.

    cd scripts && python3 -m simple_tools.subscribe_all                 # what would change
    cd scripts && python3 -m simple_tools.subscribe_all --apply         # do it
    cd scripts && python3 -m simple_tools.subscribe_all --source mac    # Fleet-OPS /zulip bots only

Credentials, deduplicated by bot email:
  mac        every bot with <BOT>_ZULIP_EMAIL, _KEY and _SITE in Infisical Fleet-OPS /zulip (the Mac's
             source since AFC-359, read through fleet-secrets in one call;  no credential file)
  infisical  every ZULIP_<CODE>_EMAIL / ZULIP_<CODE>_API_KEY pair in Infisical prod /zulip,
             through the read-only AGENT_SYNC_SUBSCRIBE_INFISICAL_TOKEN service-token binding.
Only bots are touched:  a key whose users/me is a human, or an admin or owner, is skipped.
Private channels are never joined, and nobody else's subscriptions are read or changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable

from . import zulip as Z
from .identity import seat_tag_for

INFISICAL_API = "https://app.infisical.com"
INFISICAL_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
INFISICAL_ENV = "prod"
INFISICAL_PATH = "/zulip"
INFISICAL_TOKEN_ENV = "AGENT_SYNC_SUBSCRIBE_INFISICAL_TOKEN"
BOT_ROLES = (300, 400)  # moderator, member:  the roles the listener accepts
# Bots never need a notification;  set every new subscription quiet so nothing reaches a phone.
QUIET = {
    "desktop_notifications": False,
    "audible_notifications": False,
    "push_notifications": False,
    "email_notifications": False,
    "wildcard_mentions_notify": False,
}


class Failure(Exception):
    pass


def public_channels(client: Z.ZulipClient) -> dict[int, str]:
    """Public, unarchived channels by stream id.  Private (invite-only) channels are left out."""
    result = client.get("streams", {"include_public": "true", "include_subscribed": "true"})
    out: dict[int, str] = {}
    for s in result.get("streams") or []:
        if not isinstance(s.get("stream_id"), int) or s.get("invite_only") or s.get("is_archived"):
            continue
        out[s["stream_id"]] = str(s.get("name") or "")
    return out


def subscribed_ids(client: Z.ZulipClient) -> set[int]:
    return {
        s["stream_id"]
        for s in client.get("users/me/subscriptions").get("subscriptions") or []
        if isinstance(s.get("stream_id"), int)
    }


def ensure_public(client: Z.ZulipClient, *, apply: bool) -> tuple[list[str], list[str]]:
    """Subscribe this bot to every public channel it is missing.  Returns (missing names, joined
    names).  The add endpoint takes names and would create a channel that does not exist, so the
    names come from a GET /streams made just before, and only names on it are sent."""
    public = public_channels(client)
    have = subscribed_ids(client)
    missing = {sid: name for sid, name in public.items() if sid not in have and name}
    if not apply or not missing:
        return sorted(missing.values()), []
    result = client.post(
        "users/me/subscriptions", {"subscriptions": [{"name": n} for n in sorted(missing.values())]}
    )
    joined = sorted({n for names in (result.get("subscribed") or {}).values() for n in names})
    quiet(client, [sid for sid, name in missing.items() if name in joined])
    return sorted(missing.values()), joined


def quiet(client: Z.ZulipClient, stream_ids: Iterable[int]) -> None:
    data = [
        {"stream_id": sid, "property": prop, "value": value}
        for sid in stream_ids
        for prop, value in QUIET.items()
    ]
    if data:
        client.post("users/me/subscriptions/properties", {"subscription_data": data})


# ---- credentials --------------------------------------------------------------------------------
def mac_credentials() -> list[Z.Credentials]:
    """Every seat bot in Infisical Fleet-OPS /zulip (never the owner or an integration bot:
    zulip.NOT_SEAT_PREFIXES), fetched in one call and held in memory only."""
    env, home = os.environ, Path.home()
    seats = sorted(Z.infisical_seats(env, home))
    Z.prefetch_infisical(seats, env, home)
    out = []
    for seat in seats:
        try:
            out.append(Z.infisical_credentials(seat, env=env, home=home))
        except Z.CredentialError as exc:
            print("skip %s:  %s" % (seat, exc), file=sys.stderr)
    return out


def _service_token() -> str:
    token = os.environ.get(INFISICAL_TOKEN_ENV, "").strip()
    if (
        not token.startswith("st.")
        or not 20 <= len(token) <= 2048
        or any(c.isspace() for c in token)
    ):
        raise Failure(f"{INFISICAL_TOKEN_ENV} is missing or is not a valid service token")
    return token


def _http_json(
    method: str, url: str, *, body: dict | None = None, headers: dict | None = None
) -> dict:
    req = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(), method=method
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "simple-tools subscribe_all")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise Failure(
            "%s %s answered HTTP %d" % (method, urllib.parse.urlsplit(url).path, exc.code)
        ) from None


def infisical_credentials(realm: str) -> list[Z.Credentials]:
    # Keep the listener package importable in its runtime image, which intentionally contains
    # only scripts/simple_tools and scripts/simple-chat.  This optional CLI helper is installed on
    # the host, not in the listener image; load it only when this explicit command is invoked.
    try:
        from fleet_secrets import core as scoped_secrets
    except ModuleNotFoundError as exc:
        if exc.name not in {"fleet_secrets", "fleet_secrets.core"}:
            raise
        raise Failure(
            "Infisical source unavailable (fleet-secrets helper is not installed)"
        ) from None

    token = _service_token()
    env = {
        key: value for key, value in os.environ.items() if key not in scoped_secrets.AUTH_ENV_VARS
    }
    env[scoped_secrets.ENV_READ_PATHS] = INFISICAL_PATH
    env["INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID"] = INFISICAL_PROJECT
    env[scoped_secrets.ENV_AI_FLEET_COORDINATOR_TOKEN] = token
    try:
        found = scoped_secrets.fetch_project(INFISICAL_PROJECT, INFISICAL_PATH, env, token=token)
    except scoped_secrets.SecretsError as exc:
        raise Failure("Infisical scoped read failed (" + exc.message + ")") from None
    out = []
    for name in sorted(found):
        if not (name.startswith("ZULIP_") and name.endswith("_EMAIL")):
            continue
        prefix = name[: -len("_EMAIL")]
        email, key = found.get(name, ""), found.get(prefix + "_API_KEY", "")
        if email and key:
            out.append(
                Z.Credentials(email=email, key=key, site=realm, source="infisical:" + prefix)
            )
    return out


def gather(source: str, realm: str) -> list[Z.Credentials]:
    creds: list[Z.Credentials] = []
    if source in ("mac", "both"):
        creds += mac_credentials()
    if source in ("infisical", "both"):
        creds += infisical_credentials(realm)
    seen: dict[str, Z.Credentials] = {}
    for c in (
        creds
    ):  # the Fleet-OPS copy wins over the server project's for the same bot:  it is listed first
        seen.setdefault(c.email.casefold(), c)
    return list(seen.values())


# ---- main ---------------------------------------------------------------------------------------
def run(creds: list[Z.Credentials], realm: str, *, apply: bool, out=sys.stdout) -> int:
    failures = 0
    rows: list[tuple[str, int, int]] = []
    for c in creds:
        try:
            Z.verify_realm(c, realm)
            client = Z.ZulipClient(c, realm)
            try:
                me = client.get("users/me")
            except Z.ApiError as exc:
                if "incoming webhook" in str(exc):
                    continue  # an integration bot (PagerDuty, Sentry):  it cannot subscribe or read
                raise
            if not me.get("is_bot") or me.get("role") not in BOT_ROLES:
                continue  # a human, or an admin or owner bot:  never touched
            tag = seat_tag_for(me)
            before = len(subscribed_ids(client))
            missing, joined = ensure_public(client, apply=apply)
            after = before + len(joined)
            rows.append((tag, before, after))
            verb = "joined" if apply else "would join"
            names = joined if apply else missing
            print(
                "%-16s before %2d  after %2d  %s %s"
                % (
                    tag,
                    before,
                    after if apply else before + len(missing),
                    verb,
                    ", ".join("#" + n for n in names) or "nothing",
                ),
                file=out,
            )
        except (Z.SimpleToolsError, Failure) as exc:
            failures += 1
            print("%-16s error:  %s" % (c.source.rsplit("/", 1)[-1], exc), file=out)
    print(
        "%d bots checked, %d errors%s"
        % (len(rows), failures, "" if apply else " (dry run;  --apply to subscribe)"),
        file=out,
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="subscribe (default:  dry run)")
    p.add_argument("--source", choices=("mac", "infisical", "both"), default="both")
    p.add_argument("--channels", action="store_true", help="list the public channels and exit")
    args = p.parse_args(argv)
    realm = Z.realm_url(os.environ)
    try:
        creds = gather(args.source, realm)
    except Failure as exc:
        print("subscribe_all:  %s" % exc, file=sys.stderr)
        return 2
    if not creds:
        print("subscribe_all:  no credentials found", file=sys.stderr)
        return 2
    if args.channels:
        client = Z.ZulipClient(creds[0], realm)
        for sid, name in sorted(public_channels(client).items(), key=lambda kv: kv[1]):
            print("%-10d #%s" % (sid, name))
        return 0
    return run(creds, realm, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
