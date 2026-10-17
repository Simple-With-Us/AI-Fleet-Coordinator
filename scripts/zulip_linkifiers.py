#!/usr/bin/env python3
"""Bring the realm's linkifiers to the set in TARGET, then prove it by rendering.  Applying is OWNER-RUN.

Only a realm administrator can add, edit or delete a linkifier, and Jay is the only one
(docs/protocols/zulip-fleet-guide.md, How Linkifiers Work).  Planning and checking need
only a member key, so any seat can run them:

    python3 scripts/zulip_linkifiers.py                 # plan:  diff live rules against TARGET
    python3 scripts/zulip_linkifiers.py --check         # render CHECKS and report each verdict
    python3 scripts/zulip_linkifiers.py --apply --owner-rc PATH   # owner:  apply the plan, then check

Credentials, first hit wins:  --owner-rc PATH (or --rc PATH for a read-only run), then env
ZULIP_EMAIL + ZULIP_API_KEY, then ~/.secrets/Zulip/Claude-zuliprc.  Keys are never printed.

Rules are matched to live ones by id.  An id in TARGET that is missing live, or a TARGET row
with no id, is created;  creation always lands last in realm order, which is safe here because
every new row is disjoint from the generic `repo#id` fallback (it needs a `#`).  A live rule
that TARGET does not list is reported and left alone unless --delete-unlisted is passed.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REALM = "https://simplewithus.zulipchat.com"
GH = "https://github.com/Simple-With-Us"

# Linear team keys, read from Linear on 2026-10-16 (Clutch's key is C, CodeCaps's is CC).
LINEAR_TEAMS = ("AFC", "CTS", "OPS", "SWU", "AR", "BF", "CC", "CL", "CT", "DD", "FL", "HH", "PS", "ST", "UM", "C")
# Sentry projects in org simple-with-us;  a short id is the upper-cased slug, a dash and a base-32 counter.
SENTRY_PROJECTS = ("AGENT-SESSIONS", "AUTOROTATE", "BOTFLEET", "CODECAPS", "CONGRESS-TRADE", "CONTACTLOGO",
                   "DEALDEX", "FLEET-INFRA", "FLEETLINK", "HOGHUNTER", "PERSONAL-SITE", "SOCRATIC-TRADE",
                   "USAGE-MONITOR")


def _gh(acronym: str, repo: str) -> tuple[str, str]:
    return f"{acronym}#(?P<id>[0-9]+)", f"{GH}/{repo}/issues/{{id}}"


# (id or None, pattern, url_template), in realm order.  None means "create".
TARGET: list[tuple[int | None, str, str]] = [
    (5117, "SWU/(?P<slug>[A-Za-z0-9_-]+)", "https://simplewithus.com/{slug}"),
    # Nested paths:  segments joined by `/` or `.`, never ending on punctuation, and `{+path}` so `/` is not %2F.
    (5118, "fleetlink/(?P<path>[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*)", "https://fleetlink.online/{+path}"),
    (5119, "(?P<id>(?:" + "|".join(LINEAR_TEAMS) + ")-[0-9]+)", "https://linear.app/simple-with-us/issue/{id}"),
    (5120, *_gh("BF", "BotFleet")),
    (5121, *_gh("CT", "Congress.Trade")),
    (5122, *_gh("ST", "Socratic-Trade")),
    (5123, *_gh("UM", "Usage-Monitor")),
    (5124, *_gh("DD", "DealDex")),
    (5125, *_gh("CL", "ContactLogo")),
    (5126, *_gh("AFC", "ai-fleet-coordinator")),
    (5127, *_gh("PS", "Personal-Site")),
    (5128, *_gh("HH", "HogHunter")),
    (5129, "OPS#(?P<id>[0-9]+)", "https://github.com/jaywedgeworth22/Fleet-OPS/issues/{id}"),
    (5130, *_gh("CC", "CodeCaps")),
    (5131, "GH@(?P<user>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)", "https://github.com/{user}/{repo}"),
    (5132, *_gh("CAPS", "CodeCaps")),
    (5133, *_gh("FL", "FleetLink")),
    (5135, *_gh("CK", "Clutch")),
    (5136, *_gh("CTS", "congress-trading-shared")),
    (5138, *_gh("CLUTCH", "Clutch")),
    (5139, "(?P<repo>[A-Za-z0-9_.-]+)#(?P<id>[0-9]+)", f"{GH}/{{repo}}/issues/{{id}}"),
    (None, "(?P<id>(?:" + "|".join(SENTRY_PROJECTS) + ")-[0-9A-Z]+)",
     "https://simple-with-us.sentry.io/issues/?query={id}"),
    (None, "bc-(?P<id>[0-9a-f]{8}[A-Za-z0-9_-]*)", "https://cursor.com/agents/bc-{id}"),
]

# What a reader types, and the href the server must render (None:  must stay plain text).
CHECKS: list[tuple[str, str | None]] = [
    ("AFC-535", "https://linear.app/simple-with-us/issue/AFC-535"),
    ("CTS-12", "https://linear.app/simple-with-us/issue/CTS-12"),
    ("SWU-4", "https://linear.app/simple-with-us/issue/SWU-4"),
    ("AR-9", "https://linear.app/simple-with-us/issue/AR-9"),
    ("C-12", "https://linear.app/simple-with-us/issue/C-12"),
    ("CC-7", "https://linear.app/simple-with-us/issue/CC-7"),
    ("LIN-42", None),
    ("ST#88", f"{GH}/Socratic-Trade/issues/88"),
    ("OPS#953", "https://github.com/jaywedgeworth22/Fleet-OPS/issues/953"),
    ("CC#5", f"{GH}/CodeCaps/issues/5"),
    ("CAPS#12", f"{GH}/CodeCaps/issues/12"),
    ("CLUTCH#5", f"{GH}/Clutch/issues/5"),
    ("CK#5", f"{GH}/Clutch/issues/5"),
    ("CT#2316", f"{GH}/Congress.Trade/issues/2316"),
    ("Autorotate#4", f"{GH}/Autorotate/issues/4"),
    ("fleetlink/zulip", "https://fleetlink.online/zulip"),
    ("fleetlink/zulip/zulip-fleet-guide.md.", "https://fleetlink.online/zulip/zulip-fleet-guide.md"),
    ("BOTFLEET-16", "https://simple-with-us.sentry.io/issues/?query=BOTFLEET-16"),
    ("CONGRESS-TRADE-4A", "https://simple-with-us.sentry.io/issues/?query=CONGRESS-TRADE-4A"),
    ("bc-a1b2c3d4-e5f6", "https://cursor.com/agents/bc-a1b2c3d4-e5f6"),
]


def credentials(rc_path: str | None) -> tuple[str, str, str]:
    path = rc_path
    if not path and os.environ.get("ZULIP_EMAIL") and os.environ.get("ZULIP_API_KEY"):
        return os.environ["ZULIP_EMAIL"], os.environ["ZULIP_API_KEY"], os.environ.get("ZULIP_SITE", REALM)
    path = path or os.path.expanduser("~/.secrets/Zulip/Claude-zuliprc")
    cfg = configparser.ConfigParser()
    if not cfg.read(path) or "api" not in cfg:
        sys.exit(f"no [api] section in {path}")
    api = cfg["api"]
    return api["email"], api["key"], api.get("site", REALM).rstrip("/")


class Zulip:
    def __init__(self, email: str, key: str, site: str) -> None:
        self.base = f"{site}/api/v1/"
        self.auth = "Basic " + base64.b64encode(f"{email}:{key}".encode()).decode()

    def call(self, method: str, path: str, data: dict | None = None) -> dict:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = json.loads(exc.read() or b"{}").get("msg", "")
            raise SystemExit(f"{method} {path}:  HTTP {exc.code} {detail}") from None

    def linkifiers(self) -> list[dict]:
        return self.call("GET", "realm/linkifiers")["linkifiers"]

    def render(self, content: str) -> str:
        return self.call("POST", "messages/render", {"content": content})["rendered"]


def plan(live: list[dict]) -> tuple[list[tuple], list[dict]]:
    """Return (steps, unlisted).  A step is ("edit", id, pattern, url) or ("add", None, pattern, url)."""
    by_id = {rule["id"]: rule for rule in live}
    steps = []
    for rule_id, pattern, url in TARGET:
        current = by_id.get(rule_id) if rule_id is not None else None
        if current is None:
            steps.append(("add", None, pattern, url))
        elif (current["pattern"], current["url_template"]) != (pattern, url):
            steps.append(("edit", rule_id, pattern, url))
    listed = {rule_id for rule_id, _, _ in TARGET}
    unlisted = [rule for rule in live if rule["id"] not in listed]
    return steps, unlisted


def check(zulip: Zulip) -> int:
    failures = 0
    for text, href in CHECKS:
        html = zulip.render(text)
        linked = 'href="' in html
        ok = (not linked) if href is None else f'href="{href}"' in html.replace("&amp;", "&")
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {text:42} {'plain text' if href is None else href}")
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} render checks pass")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rc", help="zuliprc for a read-only plan or check")
    parser.add_argument("--owner-rc", help="the owner's zuliprc (needed for --apply)")
    parser.add_argument("--apply", action="store_true", help="apply the plan (realm administrator only)")
    parser.add_argument("--check", action="store_true", help="render CHECKS and report")
    parser.add_argument("--delete-unlisted", action="store_true", help="with --apply, delete live rules TARGET omits")
    args = parser.parse_args(argv)

    zulip = Zulip(*credentials(args.owner_rc or args.rc))
    if args.check and not args.apply:
        return 1 if check(zulip) else 0

    steps, unlisted = plan(zulip.linkifiers())
    for kind, rule_id, pattern, url in steps:
        print(f"{kind:4} {rule_id or 'new':>5}  {pattern}  ->  {url}")
    for rule in unlisted:
        print(f"{'del' if args.delete_unlisted else 'keep'} {rule['id']:>5}  {rule['pattern']}  (not in TARGET)")
    if not steps and not (unlisted and args.delete_unlisted):
        print("live rules already match TARGET")
    if not args.apply:
        return 0

    for kind, rule_id, pattern, url in steps:
        data = {"pattern": pattern, "url_template": url}
        if kind == "edit":
            zulip.call("PATCH", f"realm/filters/{rule_id}", data)
        else:
            zulip.call("POST", "realm/filters", data)
    if args.delete_unlisted:
        for rule in unlisted:
            zulip.call("DELETE", f"realm/filters/{rule['id']}")
    print(f"applied {len(steps)} change(s);  checking")
    return 1 if check(zulip) else 0


if __name__ == "__main__":
    sys.exit(main())
