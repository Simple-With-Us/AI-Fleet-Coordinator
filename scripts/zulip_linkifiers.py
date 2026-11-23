#!/usr/bin/env python3
"""Bring the realm's linkifiers to the set in TARGET, then prove it by rendering.  Applying uses the realm-admin bot (--admin).

Only a realm administrator can add, edit or delete a linkifier, and the one credential that does
it is the realm-admin bot (docs/protocols/zulip-fleet-guide.md, How Linkifiers Work).  Planning and
checking need only a member key, so any seat can run them:

    python3 scripts/zulip_linkifiers.py                 # plan:  diff live rules against TARGET
    python3 scripts/zulip_linkifiers.py --check         # render CHECKS, and diff Linear's teams with rule 5119
    python3 scripts/zulip_linkifiers.py --apply --admin # realm-admin bot:  apply the plan, then check

Credentials:  with --admin, only the realm-admin bot:  env ZULIP_OPERATOR_EMAIL / _API_KEY / _SITE
first, then Infisical Fleet-OPS /zulip_operator through the canonical bounded named reader (no legacy-name fallback).
Without --admin (plan and check), first hit wins:  --rc PATH, then env ZULIP_EMAIL + ZULIP_API_KEY, then
the verified seat's Infisical names (/zulip, then the root).  --apply needs --admin.  Keys are never printed.
No credential file is read (AFC-359).

Rules are matched to live ones by id, then by identical pattern, because Zulip numbers a rule it
creates itself (a deleted and re-added rule, a restored backup, a fresh realm) and the pin in
TARGET then points at nothing.  A TARGET row that matches no live rule is created.  Zulip puts a
new rule last, so after any change the script reorders the realm (PATCH /realm/linkifiers) to
keep the generic `repo#id` fallback last;  otherwise it would claim `PagerDuty#384` before the
PagerDuty rule saw it.  The reorder also runs when a call fails part-way, and a rerun of --apply
finishes whatever is left without adding anything twice.  A live rule that TARGET does not list
is reported and left alone unless --delete-unlisted is passed.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REALM = "https://simplewithus.zulipchat.com"
GH = "https://github.com/Simple-With-Us"

# Linear team keys, read from Linear on 2026-10-16 (Clutch's key is C, CodeCaps's is CC).  `--check` compares
# this list with Linear's own (through the board CLI) so a new team cannot go unlinked silently.
LINEAR_TEAMS = (
    "AFC",
    "CTS",
    "OPS",
    "SWU",
    "AR",
    "BF",
    "CC",
    "CL",
    "CT",
    "DD",
    "FL",
    "HH",
    "PS",
    "ST",
    "UM",
    "C",
)
# Sentry projects in org simple-with-us;  a short id is the upper-cased slug, a dash and a base-32 counter.
SENTRY_PROJECTS = (
    "AGENT-SESSIONS",
    "AUTOROTATE",
    "BOTFLEET",
    "CODECAPS",
    "CONGRESS-TRADE",
    "CONTACTLOGO",
    "DEALDEX",
    "FLEET-INFRA",
    "FLEETLINK",
    "HOGHUNTER",
    "BOTL-SITE",
    "SOCRATIC-TRADE",
    "USAGE-MONITOR",
)


def _gh(acronym: str, repo: str) -> tuple[str, str]:
    return f"{acronym}#(?P<id>[0-9]+)", f"{GH}/{repo}/issues/{{id}}"


# (id or None, pattern, url_template), in realm order.  None means "create".
TARGET: list[tuple[int | None, str, str]] = [
    (5117, "SWU/(?P<slug>[A-Za-z0-9_-]+)", "https://simplewithus.com/{slug}"),
    # Nested paths:  segments joined by `/` or `.`, never ending on punctuation, and `{+path}` so `/` is not %2F.
    (
        5118,
        "fleetlink/(?P<path>[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*)",
        "https://fleetlink.online/{+path}",
    ),
    # The short sibling of 5118 (owner example:  fl/fleet-tools).  Same shape, same host.
    (
        5151,
        "fl/(?P<path>[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*)",
        "https://fleetlink.online/{+path}",
    ),
    (
        5119,
        "(?P<id>(?:" + "|".join(LINEAR_TEAMS) + ")-[0-9]+)",
        "https://linear.app/simple-with-us/issue/{id}",
    ),
    (5120, *_gh("BF", "BotFleet")),
    (5121, *_gh("CT", "Congress.Trade")),
    (5122, *_gh("ST", "Socratic-Trade")),
    (5123, *_gh("UM", "Usage-Monitor")),
    (5124, *_gh("DD", "DealDex")),
    (5125, *_gh("CL", "ContactLogo")),
    (5126, *_gh("AFC", "ai-fleet-coordinator")),
    (5127, *_gh("PS", "Personal-Site")),
    (5128, *_gh("HH", "HogHunter")),
    (5129, *_gh("OPS", "Fleet-OPS")),
    (5130, *_gh("CC", "CodeCaps")),
    (
        5131,
        "GH@(?P<user>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
        "https://github.com/{user}/{repo}",
    ),
    (5132, *_gh("CAPS", "CodeCaps")),
    (5133, *_gh("FL", "FleetLink")),
    (5135, *_gh("CK", "Clutch")),
    (5136, *_gh("CTS", "congress-trading-shared")),
    (5138, *_gh("CLUTCH", "Clutch")),
    # Clutch's acronym since 2026-10-23;  owner 2026-11-10:  C#n links to Clutch everywhere.
    # Anchored with a lookbehind:  unanchored, the one-letter C rule claims the tail of any
    # word ending in C (MUSIC#5 rendered as Clutch) before the generic fallback runs.
    (None, "(?<![A-Za-z0-9])C#(?P<id>[0-9]+)", f"{GH}/Clutch/issues/{{id}}"),
    # Every app answers to its acronym and its repo name (owner 2026-11-10).
    (None, *_gh("AR", "Autorotate")),
    (None, *_gh("SWU", "Simple-With-Us")),
    (None, *_gh("FLL", "fleetlink-legacy")),
    # Linear projects, generated from Linear by the Operator's webhook (AFC-565);  do not edit by hand
    # BEGIN linear-projects:  generated by scripts/linear_project_linkifiers.py;  do not edit by hand
    (5155, "P-AFC-7", "https://linear.app/simple-with-us/project/9c28cf61dcc8"),
    (5156, "P-AFC-8", "https://linear.app/simple-with-us/project/272cdd72e951"),
    (5157, "P-BF-1", "https://linear.app/simple-with-us/project/cc08b4cf163b"),
    (5158, "P-BF-6", "https://linear.app/simple-with-us/project/5afcbc055535"),
    (5159, "P-CTS-5", "https://linear.app/simple-with-us/project/a04bf2274b2d"),
    (5160, "P-OPS-2", "https://linear.app/simple-with-us/project/22f992634f02"),
    (5161, "P-OPS-4", "https://linear.app/simple-with-us/project/32fd751b0b36"),
    (5162, "P-SWU-3", "https://linear.app/simple-with-us/project/6e26b4c70c72"),
    (5167, "P-UM-9", "https://linear.app/simple-with-us/project/dd28b9b2ee11"),
    # END linear-projects
    (5139, "(?P<repo>[A-Za-z0-9_.-]+)#(?P<id>[0-9]+)", f"{GH}/{{repo}}/issues/{{id}}"),
    (
        5141,
        "(?P<id>(?:" + "|".join(SENTRY_PROJECTS) + ")-[0-9A-Z]+)",
        "https://simple-with-us.sentry.io/issues/?query={id}",
    ),
    (5142, "bc-(?P<id>[0-9a-f]{8}[A-Za-z0-9_-]*)", "https://cursor.com/agents/bc-{id}"),
    # Spelled out (owner 2026-10-16) so it never reads as an app acronym.  Numeric ids also match the
    # generic rule, which is why `order` keeps the generic rule last.
    (5143, "PagerDuty#(?P<id>[A-Z0-9]+)", "https://jays-services.pagerduty.com/incidents/{id}"),
]
GENERIC_ID = (
    5139  # the `repo#id` fallback;  it must stay last or it claims every `X#n` before its own rule
)

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
    ("OPS#953", f"{GH}/Fleet-OPS/issues/953"),
    ("Fleet-OPS#953", f"{GH}/Fleet-OPS/issues/953"),
    ("CC#5", f"{GH}/CodeCaps/issues/5"),
    ("CAPS#12", f"{GH}/CodeCaps/issues/12"),
    ("C#5", f"{GH}/Clutch/issues/5"),
    ("MUSIC#5", f"{GH}/MUSIC/issues/5"),
    ("Clutch#5", f"{GH}/Clutch/issues/5"),
    ("CodeCaps#5", f"{GH}/CodeCaps/issues/5"),
    ("HH#5", f"{GH}/HogHunter/issues/5"),
    ("HogHunter#5", f"{GH}/HogHunter/issues/5"),
    ("AR#5", f"{GH}/Autorotate/issues/5"),
    ("SWU#5", f"{GH}/Simple-With-Us/issues/5"),
    ("FL#5", f"{GH}/FleetLink/issues/5"),
    ("FLL#5", f"{GH}/fleetlink-legacy/issues/5"),
    ("CLUTCH#5", f"{GH}/Clutch/issues/5"),
    ("CK#5", f"{GH}/Clutch/issues/5"),
    ("CT#2316", f"{GH}/Congress.Trade/issues/2316"),
    ("Autorotate#4", f"{GH}/Autorotate/issues/4"),
    ("fleetlink/zulip", "https://fleetlink.online/zulip"),
    (
        "fleetlink/zulip/zulip-fleet-guide.md.",
        "https://fleetlink.online/zulip/zulip-fleet-guide.md",
    ),
    ("fl/fleet-tools", "https://fleetlink.online/fleet-tools"),
    ("P-BF-6", "https://linear.app/simple-with-us/project/5afcbc055535"),
    ("P-UM-9", "https://linear.app/simple-with-us/project/dd28b9b2ee11"),
    ("BOTFLEET-16", "https://simple-with-us.sentry.io/issues/?query=BOTFLEET-16"),
    ("CONGRESS-TRADE-4A", "https://simple-with-us.sentry.io/issues/?query=CONGRESS-TRADE-4A"),
    ("bc-a1b2c3d4-e5f6", "https://cursor.com/agents/bc-a1b2c3d4-e5f6"),
    ("PagerDuty#384", "https://jays-services.pagerduty.com/incidents/384"),
    ("PagerDuty#Q1ABC23", "https://jays-services.pagerduty.com/incidents/Q1ABC23"),
]


_READER_LOCK = threading.RLock()
_READER_CACHE = None
_READER_NAME = "_zulip_linkifiers_fleet_infisical"
_MAX_READER_BYTES = 2 * 1024 * 1024

# This consumer's own scoped read-only service tokens:  one grant per Infisical folder,  each named
# for the folder it covers and held in the consumer's own store (the owner's login keychain on the
# Mac, a private file on a server).  The store is the authority and is read first;  an environment
# binding is used only when the token's own grant metadata proves the narrow grant.
TOKEN_ACCOUNTS = {
    "/zulip_operator": "zulip-linkifiers-operator-ro",
    "/zulip": "zulip-linkifiers-zulip-ro",
    "/": "zulip-linkifiers-root-ro",
}


class BindingRefused(Exception):
    """The consumer's own scoped token is not usable:  the message names the account and the refusal."""


def _infisical_reader():
    """Execute only the fixed canonical sibling source, never an import-path candidate."""
    global _READER_CACHE
    expected = Path(__file__).resolve().parent / "fleet_infisical.py"
    with _READER_LOCK:
        helper = None
        try:
            directory_fd = os.open(expected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                directory = os.fstat(directory_fd)
                fd = os.open(
                    expected.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_READER_BYTES:
                        raise ValueError()
                    raw = source.read(_MAX_READER_BYTES + 1)
                    after = os.fstat(source.fileno())
                current = os.stat(expected.name, dir_fd=directory_fd, follow_symlinks=False)
                current_directory = expected.parent.lstat()
                if (directory.st_dev, directory.st_ino) != (
                    current_directory.st_dev,
                    current_directory.st_ino,
                ):
                    raise ValueError()
            finally:
                os.close(directory_fd)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            identities = [
                tuple(getattr(info, key) for key in fields) for info in (before, after, current)
            ]
            if len(raw) > _MAX_READER_BYTES or not identities[0] == identities[1] == identities[2]:
                raise ValueError()
            identity = (str(expected), identities[0], hashlib.sha256(raw).digest())
            if _READER_CACHE is not None and _READER_CACHE[0] == identity:
                sys.modules[_READER_NAME] = _READER_CACHE[1]
                return _READER_CACHE[1]
            spec = importlib.util.spec_from_file_location(_READER_NAME, expected)
            if spec is None or spec.loader is None:
                raise ValueError()
            helper = importlib.util.module_from_spec(spec)
            sys.modules[_READER_NAME] = helper
            exec(compile(raw, str(expected), "exec"), vars(helper))
            if not callable(helper.bounded_fetch_project):
                raise ValueError()
            _READER_CACHE = (identity, helper)
            return helper
        except BaseException as exc:
            cached = None if _READER_CACHE is None else _READER_CACHE[1]
            _READER_CACHE = None
            for owned in (helper, cached):
                if owned is not None and sys.modules.get(_READER_NAME) is owned:
                    del sys.modules[_READER_NAME]
            if isinstance(exc, Exception):
                raise ImportError("canonical Infisical reader unavailable") from None
            raise


def _read_infisical_names(names, folder):
    """Read only this caller's named values through the pinned canonical worker."""
    try:
        client = _infisical_reader()
        declared = os.environ.get("INFISICAL_FLEET_OPS_PROJECT_ID", "").strip()
        if not declared:
            return {}
        project = client.infisical_project_id(
            {"INFISICAL_FLEET_OPS_PROJECT_ID": declared}, Path("/")
        )
        token = _own_token(client, folder, project)
        if not token:
            return {}
        return client.bounded_fetch_project(
            project,
            folder,
            {"INFISICAL_ENVIRONMENT": "prod", "INFISICAL_READ_PATHS": folder},
            token=token,
            names=tuple(names),
            timeout=30,
            max_output_bytes=65536,
            strict_records=True,
        )
    except BindingRefused:
        raise
    except Exception:
        return {}


def _own_token(client, folder, project):
    """This consumer's own scoped token:  its own store first,  a bound copy only when it proves itself.

    The store is the authority:  the consumer's own account token is read from the host's approved
    store first,  and it is the credential the read runs under.  An environment binding is a second
    source only when the token's own grant metadata proves it is this consumer's narrow grant:  the
    workspace is the project,  the permissions are exactly read,  the granted folder is exactly this
    one,  and the token's own name is this account.  A general binding scoped to "/" or to several
    folders fails that check,  so it can no longer serve a consumer,  and a hand-set value never
    replaces the reviewed grant.  The refusal names the account the consumer needs and which
    refusal it was."""
    account = TOKEN_ACCOUNTS.get(folder)
    if not account:
        raise BindingRefused("no scoped token covers %s" % folder)
    home = os.environ.get("HOME", "").strip()
    try:
        token, _source = client.consumer_binding(
            account,
            folder,
            dict(os.environ),
            project,
            home=Path(home) if home else None,
        )
    except Exception as exc:  # noqa: BLE001 - a store that cannot answer is a failure, not an empty token
        detail = getattr(exc, "message", "") or type(exc).__name__
        raise BindingRefused(
            "the %s scoped token for %s could not be resolved (%s)" % (account, folder, detail)
        ) from None
    return token


def _fleet_secret(name: str) -> str:
    """One named read, retaining the member-only /zulip-to-root retry."""
    folders = ("/zulip_operator",) if name.startswith("ZULIP_OPERATOR_") else ("/zulip", "/")
    for folder in folders:
        value = _read_infisical_names((name,), folder).get(name, "").strip()
        if value:
            return value
    return ""


def credentials(rc_path: str | None, admin: bool = False) -> tuple[str, str, str]:
    """This consumer's own credential, from Infisical only.

    The environment is not a source for a value either:  the names resolve from the consumer's own
    scoped grant,  and a refusal names the account to provision."""
    try:
        return _credentials(rc_path, admin)
    except BindingRefused as exc:
        sys.exit(str(exc))


def _credentials(rc_path: str | None, admin: bool = False) -> tuple[str, str, str]:
    if admin:
        email, key, site = (
            _fleet_secret(f"ZULIP_OPERATOR_{'API_KEY' if part == 'KEY' else part}")
            for part in ("EMAIL", "KEY", "SITE")
        )
        if not (email and key):
            sys.exit(
                "no ZULIP_OPERATOR_EMAIL / ZULIP_OPERATOR_API_KEY in Infisical Fleet-OPS "
                "/zulip_operator"
            )
        return email, key, (site or REALM).rstrip("/")
    path = rc_path
    if not path:
        seat = (os.environ.get("AGENT_LAUNCH_SEAT") or os.environ.get("AGENT_SEAT") or "").strip()
        if not seat:
            sys.exit(
                "no verified seat:  set AGENT_SEAT (or pass --rc / Zulip env) so this run uses its own credential"
            )
        bot = seat.upper().replace("-", "_")
        email, key, site = (
            _fleet_secret(f"{bot}_ZULIP_{part}") for part in ("EMAIL", "KEY", "SITE")
        )
        if not (email and key):
            sys.exit(
                f"no {bot}_ZULIP_EMAIL / {bot}_ZULIP_KEY in Infisical Fleet-OPS (and no --rc or env)"
            )
        return email, key, (site or REALM).rstrip("/")
    cfg = configparser.ConfigParser()
    if not cfg.read(path) or "api" not in cfg:
        sys.exit(f"no [api] section in {path}")
    api = cfg["api"]
    return api["email"], api["key"], api.get("site", REALM).rstrip("/")


class ZulipError(Exception):
    """A Zulip API failure (HTTP error).  An Exception, never SystemExit, so
    callers choose:  the CLI exits 1 with the message, the webhook worker logs
    and keeps serving, and optional fetches degrade to None ids."""


class Zulip:
    def __init__(self, email: str, key: str, site: str) -> None:
        self.base = f"{site}/api/v1/"
        self.auth = "Basic " + base64.b64encode(f"{email}:{key}".encode()).decode()

    def call(self, method: str, path: str, data: dict | None = None) -> dict:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method, headers={"Authorization": self.auth}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("msg", "")
            except (ValueError, OSError):
                detail = ""
            raise ZulipError(f"{method} {path}:  HTTP {exc.code} {detail}") from None

    def linkifiers(self) -> list[dict]:
        return self.call("GET", "realm/linkifiers")["linkifiers"]

    def render(self, content: str) -> str:
        return self.call("POST", "messages/render", {"content": content})["rendered"]


def match(live: list[dict]) -> list[dict | None]:
    """The live rule each TARGET row stands for, in TARGET order (None:  no live rule matches it).

    A row matches its pinned id first.  A row whose id is missing live, or that has none, falls back to
    a live rule with exactly its pattern, so a rule Zulip renumbered is edited or kept, never added a
    second time.  One live rule stands for one row.
    """
    by_id = {rule["id"]: rule for rule in live}
    matched = [by_id.get(rule_id) if rule_id is not None else None for rule_id, _, _ in TARGET]
    taken = {rule["id"] for rule in matched if rule is not None}
    for index, (_, pattern, _) in enumerate(TARGET):
        if matched[index] is not None:
            continue
        for rule in live:
            if rule.get("pattern") == pattern and rule["id"] not in taken:
                matched[index] = rule
                taken.add(rule["id"])
                break
    return matched


def plan(live: list[dict]) -> tuple[list[tuple], list[dict]]:
    """Return (steps, unlisted).  A step is ("edit", id, pattern, url) or ("add", None, pattern, url)."""
    steps = []
    matched = match(live)
    for (_, pattern, url), current in zip(TARGET, matched):
        if current is None:
            steps.append(("add", None, pattern, url))
        elif (current["pattern"], current["url_template"]) != (pattern, url):
            steps.append(("edit", current["id"], pattern, url))
    listed = {rule["id"] for rule in matched if rule is not None}
    unlisted = [rule for rule in live if rule["id"] not in listed]
    return steps, unlisted


def order(live: list[dict]) -> list[int] | None:
    """The realm order with the generic rule moved last, or None when it already is.

    The generic rule is found the way `match` finds any row:  by GENERIC_ID, else by its pattern.
    """
    generic = next(
        (rule for (rule_id, _, _), rule in zip(TARGET, match(live)) if rule_id == GENERIC_ID),
        None,
    )
    ids = [rule["id"] for rule in live]
    if generic is None or ids[-1] == generic["id"]:
        return None
    return [i for i in ids if i != generic["id"]] + [generic["id"]]


def team_drift(linear_keys: list[str]) -> tuple[list[str], list[str]]:
    """(keys Linear has that rule 5119 lacks, keys 5119 lists that Linear no longer has)."""
    return sorted(set(linear_keys) - set(LINEAR_TEAMS)), sorted(
        set(LINEAR_TEAMS) - set(linear_keys)
    )


def linear_team_keys() -> list[str] | None:
    """Every Linear team key, read with the board CLI (its own seat credentials), or None when unavailable."""
    if not shutil.which("simple-board"):
        return None
    try:
        proc = subprocess.run(
            ["simple-board", "api", "query", "{ teams(first: 250) { nodes { key } } }"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return [team["key"] for team in json.loads(proc.stdout)["teams"]["nodes"]]
    except (ValueError, KeyError, TypeError):
        return None


def check_linear() -> int:
    keys = linear_team_keys()
    if keys is None:
        print(
            "FAIL  Linear team drift:  the board CLI is missing or could not read Linear (pass --skip-linear to waive)"
        )
        return 1
    missing, extra = team_drift(keys)
    for key in missing:
        print(
            f"FAIL  Linear team {key} is not in LINEAR_TEAMS, so {key}-n issue ids render as plain text"
        )
    for key in extra:
        print(f"FAIL  LINEAR_TEAMS lists {key}, which is no longer a Linear team")
    print(
        f"Linear team drift:  {'none' if not (missing or extra) else f'{len(missing)} missing, {len(extra)} extra'}"
        f" ({len(keys)} teams)"
    )
    return len(missing) + len(extra)


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


def restore_order(zulip: Zulip) -> None:
    """Move the generic rule last if a change left it earlier (Zulip appends every new rule)."""
    reordered = order(zulip.linkifiers())
    if reordered:
        zulip.call("PATCH", "realm/linkifiers", {"ordered_linkifier_ids": json.dumps(reordered)})
        print(f"moved the generic rule {reordered[-1]} back to last")


def apply_plan(zulip: Zulip, steps: list[tuple], doomed: list[dict]) -> None:
    """Make the changes and delete `doomed`, then keep the generic rule last.

    The calls are not transactional.  When one fails, the order is still restored before the error
    goes on, so a half-applied run never leaves the generic rule ahead of a rule it would shadow;
    rerunning --apply then does the rest, because `plan` matches live rules by pattern as well as id.
    """
    try:
        for kind, rule_id, pattern, url in steps:
            data = {"pattern": pattern, "url_template": url}
            if kind == "edit":
                zulip.call("PATCH", f"realm/filters/{rule_id}", data)
            else:
                zulip.call("POST", "realm/filters", data)
        for rule in doomed:
            zulip.call("DELETE", f"realm/filters/{rule['id']}")
    except BaseException:
        try:
            restore_order(zulip)
        except Exception as exc:  # noqa: BLE001 - the original error matters, not the reorder
            print(f"could not restore the rule order:  {exc}", file=sys.stderr)
        raise
    restore_order(zulip)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rc", help="zuliprc for a read-only plan or check")
    parser.add_argument(
        "--admin", action="store_true", help="sign in as the realm-admin bot (required for --apply)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="apply the plan (realm administrator only)"
    )
    parser.add_argument("--check", action="store_true", help="render CHECKS and report")
    parser.add_argument(
        "--skip-linear", action="store_true", help="with --check, skip the Linear team drift check"
    )
    parser.add_argument(
        "--delete-unlisted",
        action="store_true",
        help="with --apply, delete live rules TARGET omits",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.admin:
        parser.error("--apply needs --admin (the realm-admin bot is the only write credential)")
    if args.admin and args.rc:
        parser.error("--admin and --rc are mutually exclusive")
    zulip = Zulip(*credentials(args.rc, admin=args.admin))
    try:
        if args.check and not args.apply:
            return 1 if check(zulip) + (0 if args.skip_linear else check_linear()) else 0

        steps, unlisted = plan(zulip.linkifiers())
        for kind, rule_id, pattern, url in steps:
            print(f"{kind:4} {rule_id or 'new':>5}  {pattern}  ->  {url}")
        for rule in unlisted:
            print(
                f"{'del' if args.delete_unlisted else 'keep'} {rule['id']:>5}  {rule['pattern']}  (not in TARGET)"
            )
        if (
            not steps
            and not (unlisted and args.delete_unlisted)
            and order(zulip.linkifiers()) is None
        ):
            print("live rules already match TARGET")
        if not args.apply:
            return 0

        apply_plan(zulip, steps, unlisted if args.delete_unlisted else [])
        print(f"applied {len(steps)} change(s);  checking")
        return 1 if check(zulip) else 0
    except ZulipError as exc:
        print(f"zulip: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"zulip: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
