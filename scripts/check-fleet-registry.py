#!/usr/bin/env python3
"""Verify every fleet-apps.json app is mentioned in the known registries.

Exits 1 if any required file is missing a repo, acronym, or DEFAULT_REPOS
entry.  It also checks the registry's Zulip fields:  the realm and
channel must match the simple-chat CLI, and each seat's bot email and credential
file code must be well formed and agree with the CLI's file-name rule.  Run from
the AI-Fleet-Coordinator worktree:

    python3 scripts/check-fleet-registry.py

It also checks the desired-state manifest, ``fleet-seats.json``, when that file is
present:  the manifest must name exactly the live seats of the registry, cover every
(seat, platform, kind) triple, name only platform ids the installer tables define,
and hold rules that are internally consistent.  The manifest file is new, so an
absent file is a skip with a note, never a failure:  a fresh worktree that has not
seeded the manifest yet must still pass this check.

Live-copy checks compare ~/apps/FLEET-PROTOCOL.md against the registry, which exists
on operator seats but never on CI runners.  With FLEET_BOARD_HOME set, the live
checks run against that directory and a missing directory fails the check.  Without
it, ~/apps is used when present;  when neither exists the live checks are
skipped and only the repository-portable assertions run.  The effort logs are
archived (Linear is the tracker), so nothing checks a live effort log or
EFFORT-LOG-PROTOCOL.md any more; `liveBoard` in the registry is an optional
record of where an app's archived log lives.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_sync.identity import GROK_ALIAS_PATTERN, SeatRegistryError, parse_seat_registry
from agent_sync.zulip import DEFAULT_CHANNEL, REALM, credential_file_name
from public_activity_repos import PUBLIC_REPOS

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "fleet-apps.json"
MANIFEST = ROOT / "fleet-seats.json"

# FLEET_BOARD_HOME opts the live-board checks into an explicit board checkout;
# without it ~/apps is used when it exists (operator seats) and the live
# checks are skipped when it does not (CI runners).
_board_home_env = os.environ.get("FLEET_BOARD_HOME") or ""
APPS = Path(_board_home_env) if _board_home_env else Path.home() / "apps"


def load() -> dict:
    return json.loads(REGISTRY.read_text())


def contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    return needle in path.read_text(errors="replace")


# <short name>-bot@<realm host>.  Zulip appends "-bot" to the short name itself.
BOT_EMAIL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-bot@(?P<host>[a-z0-9.-]+)$")


def zulip_errors(data: dict) -> list[str]:
    """Check the registry's Zulip fields against the simple-chat CLI and each other.

    Presence is not required:  GROK-BOT is a set of owner-managed personas with no zuliprc, and
    a retired seat has no bot.  What is present must be well formed.  The bot email cannot be
    derived from the tag (muse-assist-bot@, grok-build-bot@), so it is checked for shape and
    uniqueness only; the file code is derived by the CLI, so it is compared against the CLI.
    """
    errors: list[str] = []
    if data.get("zulipRealm") != REALM:
        errors.append(
            f"zulipRealm {data.get('zulipRealm')!r} must be {REALM!r} (scripts/agent_sync/zulip.py REALM)"
        )
    if data.get("zulipChannel") != DEFAULT_CHANNEL:
        errors.append(
            f"zulipChannel {data.get('zulipChannel')!r} must be {DEFAULT_CHANNEL!r} "
            "(scripts/agent_sync/zulip.py DEFAULT_CHANNEL)"
        )
    host = urlparse(REALM).hostname or ""
    seats = [s for s in data.get("seats", []) if isinstance(s, dict)]
    tags = [s.get("tag") or "" for s in seats]
    for tag in sorted({t for t in tags if tags.count(t) > 1}):
        errors.append(f"seat {tag or '(no tag)'} appears more than once in fleet-apps.json")
    live = {s["tag"] for s in seats if s.get("tag") and not s.get("retired")}
    emails: dict[str, str] = {}
    for seat in seats:
        tag = seat.get("tag") or "(no tag)"
        email = seat.get("zulipBotEmail")
        code = seat.get("zulipFileCode")
        alias = seat.get("aliasOf")
        if alias is not None:
            if not seat.get("retired"):
                errors.append(
                    f"seat {tag} has aliasOf {alias!r} but is not retired; an alias is always retired"
                )
            if alias not in live:
                errors.append(
                    f"seat {tag} is an alias of {alias!r}, which is not a live seat in fleet-apps.json"
                )
        if seat.get("retired") and (email or code):
            errors.append(
                f"seat {tag} is retired and must not carry a Zulip bot (retired seats have none)"
            )
        if bool(email) != bool(code):
            errors.append(
                f"seat {tag} must carry zulipBotEmail and zulipFileCode together, or neither"
            )
        if code and seat.get("tag") and code != credential_file_name(tag).removesuffix("-zuliprc"):
            errors.append(
                f"seat {tag} zulipFileCode {code!r} disagrees with the agent-sync file-name rule "
                f"({credential_file_name(tag).removesuffix('-zuliprc')!r})"
            )
        if email:
            match = BOT_EMAIL_RE.match(email)
            if not match or match.group("host") != host:
                errors.append(
                    f"seat {tag} zulipBotEmail {email!r} must look like <short-name>-bot@{host}"
                )
            elif email.split("@", 1)[0].endswith("-bot-bot"):
                # Zulip appends -bot itself; a short name typed as "codex-bot" made codex-bot-bot@ (2026-10-07).
                errors.append(
                    f"seat {tag} zulipBotEmail {email!r} ends in -bot-bot; the short name must not end in -bot"
                )
            elif email in emails:
                errors.append(f"seat {tag} and seat {emails[email]} share the Zulip bot {email}")
            else:
                emails[email] = tag
    errors.extend(alias_pattern_errors(seats))
    return errors


def seat_apps_errors(data: dict) -> list[str]:
    """`seatApps` (F2, open-loops plan 2026-10-11) says which app each seat runs in, so code that counts apps
    instead of models (a consult quorum) never reads a seat's model off its tag.  It lives beside `seats`, not
    in it, because a dozen tools read `seats` and a BF-* or GB-* row there would make lanes, skill copies and
    listener seats.  Checked:  the key parses the way scripts/agent_sync/identity.py reads it;  every live
    `seats` row has an app in `bySeat`, and `bySeat` names no other tag;  `extraSeats` holds only tags with no
    `seats` row;  no `seats` row (live or retired) also matches a `patterns` row;  tags are written upper case;
    each pattern's `zulipEmail` is a bot email on the realm with one <role> in it."""
    try:
        parsed = parse_seat_registry(data, "fleet-apps.json")
    except SeatRegistryError as exc:
        return [f"seatApps: {exc}"]
    errors: list[str] = []
    seats = [s for s in data.get("seats", []) if isinstance(s, dict) and s.get("tag")]
    # Tags are read the way identity.parse_seat_registry reads them (strip, upper case), so a row written
    # "ghost" and a bySeat key "GHOST" agree here as they do in strict_seat.
    live = {str(s["tag"]).strip().upper() for s in seats if not s.get("retired")}
    rows = {str(s["tag"]).strip().upper() for s in seats}
    apps = data["seatApps"]
    for key in ("bySeat", "extraSeats"):
        for tag in apps[key]:
            if tag != tag.upper():
                errors.append(
                    f"seatApps.{key} key {tag!r} must be written upper case ({tag.upper()!r})"
                )
    for tag in sorted(live - set(parsed.by_seat)):
        errors.append(
            f"live seat {tag} has no app in seatApps.bySeat (every non-retired seat needs one;  "
            "scripts/onboard-new-agent.sh --app adds it)"
        )
    for tag in sorted(set(parsed.by_seat) - live):
        reason = "is retired" if tag in rows else "is not a row of seats"
        errors.append(
            f"seatApps.bySeat lists {tag}, which {reason};  bySeat covers live seats only"
        )
    for tag in sorted(set(parsed.extra) & rows):
        errors.append(
            f"seatApps.extraSeats lists {tag}, which has a row in seats;  a seat is in bySeat or "
            "extraSeats, never both"
        )
    for tag in sorted(set(parsed.by_seat) & set(parsed.extra)):
        errors.append(f"seat {tag} is in both seatApps.bySeat and seatApps.extraSeats")
    for tag in sorted(rows):
        for pattern in parsed.patterns:
            if pattern.regex.fullmatch(tag.upper()):
                errors.append(
                    f"seat {tag} has a row in seats but also matches seatApps.patterns {pattern.match};  "
                    "a pattern is for tags that have no row"
                )
    host = urlparse(REALM).hostname or ""
    for item in apps["patterns"]:
        email = item.get("zulipEmail")
        match = item["match"]
        if email is None:
            continue
        probe = (
            email.replace("<role>", "x")
            if isinstance(email, str) and email.count("<role>") == 1
            else ""
        )
        found = BOT_EMAIL_RE.match(probe)
        if not found or found.group("host") != host:
            errors.append(
                f"seatApps.patterns {match} zulipEmail {email!r} must be a <role>-bot@{host} address "
                "with exactly one <role>"
            )
    return errors


def alias_pattern_errors(seats: list[dict]) -> list[str]:
    """A seat's `aliasPattern` (owner 2026-10-10:  GROK-BUILD in any spelling is the GROK seat) must compile,
    equal the CLI's own copy (agent_sync.identity.GROK_ALIAS_PATTERN), sit on a live seat, match the tag of
    every retired row that is an alias of that seat, and match no other seat tag.  The lane tool and the
    board scripts read it from here, so a pattern that is broken or too wide would sign the wrong seat."""
    errors: list[str] = []
    tags = [s.get("tag") or "" for s in seats]
    for seat in seats:
        raw = seat.get("aliasPattern")
        if raw is None:
            continue
        tag = seat.get("tag") or "(no tag)"
        if not isinstance(raw, str):
            errors.append(f"seat {tag} aliasPattern must be a string")
            continue
        if seat.get("retired"):
            errors.append(
                f"seat {tag} is retired and cannot carry an aliasPattern; the live seat it aliases carries it"
            )
        try:
            compiled = re.compile(raw, re.IGNORECASE | re.ASCII)
        except re.error as exc:
            errors.append(f"seat {tag} aliasPattern {raw!r} does not compile: {exc}")
            continue
        if raw != GROK_ALIAS_PATTERN:
            errors.append(
                f"seat {tag} aliasPattern {raw!r} disagrees with scripts/agent_sync/identity.py GROK_ALIAS_PATTERN "
                f"({GROK_ALIAS_PATTERN!r}); change both"
            )
        aliases = [
            s.get("tag") or "" for s in seats if s.get("aliasOf") == tag and s.get("retired")
        ]
        if not aliases:
            errors.append(
                f"seat {tag} aliasPattern aliases nothing: no retired seat has aliasOf {tag!r}"
            )
        for alias in aliases:
            if not compiled.match(alias):
                errors.append(
                    f"seat {tag} aliasPattern {raw!r} does not match the retired tag {alias}"
                )
        for other in tags:
            if other and other not in aliases and compiled.match(other):
                errors.append(
                    f"seat {tag} aliasPattern {raw!r} matches the tag {other}, which is not an alias of {tag}"
                )
    return errors


def engine_only_errors(data: dict, agent_sync: str) -> list[str]:
    """No live seat may sit in FLEET-PROTOCOL.md's "Engine-only CLIs" row (Platform Defaults).

    That row lists the CLIs that are not seats.  Owner 2026-10-10 made OpenCode a seat, and a merge that
    keeps the old row would put it back (two PRs edit that row).  A retired seat may be listed.
    """
    row = re.search(r"\*\*Engine-only CLIs\*\*\s*\(([^)]*)\)", agent_sync)
    if not row:
        return []
    errors: list[str] = []
    for seat in data.get("seats", []):
        if not isinstance(seat, dict) or seat.get("retired"):
            continue
        for name in sorted({seat.get("tag") or "", seat.get("notesName") or ""} - {""}):
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", row.group(1), re.IGNORECASE):
                errors.append(
                    f"FLEET-PROTOCOL.md lists live seat {seat.get('tag')} ({name}) in its Engine-only CLIs row; "
                    "a seat is not an engine-only CLI"
                )
                break
    return errors


# A mirror of the Fleet-OPS handbook starts with a GENERATED comment line.  That line names Fleet-OPS, so a
# substring check over the whole file would find the repo name and the OPS acronym there even after the body lost
# them.  Every FLEET-PROTOCOL.md check below therefore runs on the text under the header.
_GENERATED_HEADER = re.compile(r"\A<!--\s*GENERATED[^\n]*\n", re.IGNORECASE)


def strip_generated_header(text: str) -> str:
    """The text without a leading GENERATED header line (scripts/check-generated-mirror.py checks the header itself)."""
    return _GENERATED_HEADER.sub("", text, count=1)


def agent_sync_errors(
    apps: list[dict], text: str, where: str, *, fold_repo: bool = False
) -> list[str]:
    """Every registry repo and acronym must appear in the body of an FLEET-PROTOCOL.md copy.  `fold_repo` ignores case in
    the repo name (the live copy has always been matched that way)."""
    body = strip_generated_header(text)
    haystack = body.casefold() if fold_repo else body
    errors: list[str] = []
    for app in apps:
        repo = app["repo"]
        if (repo.casefold() if fold_repo else repo) not in haystack:
            errors.append(f"{where} missing repo {repo}")
        if app["acronym"] not in body:
            errors.append(f"{where} missing acronym {app['acronym']}")
    return errors


def manifest_errors(
    manifest_path: Path | None = None,
    registry_path: Path | None = None,
) -> list[str]:
    """The errors of the desired-state manifest, ``fleet-seats.json``.

    The manifest is checked against the registry roster (the loader refuses a seat
    key the registry does not hold live, and a live seat with no entry), against the
    coverage rule (every seat, platform and kind is covered by a rule or by an
    explicit not-applicable entry), against the platform ids the installer tables
    define, and against the rule rules (a rule names its seat, an owner seat, at
    least one platform and a check type its kind supports).

    An absent manifest is a skip, not a failure:  the file is new, so a fresh
    worktree that has not seeded it yet must still pass this check.  The caller
    prints the note.
    """
    from fleet_config.manifest import (
        known_platforms,
        load_manifest,
        validate_compliance_rules,
        validate_coverage,
        validate_platforms,
        validate_rules,
    )

    target = manifest_path if manifest_path is not None else MANIFEST
    if not target.is_file():
        return []
    registry = registry_path if registry_path is not None else REGISTRY
    errors: list[str] = []
    try:
        manifest = load_manifest(target, registry_path=registry)
    except Exception as exc:  # noqa: BLE001 - a bad manifest is an error, not a crash
        return ["fleet-seats.json: %s" % exc]
    for check, name in (
        (validate_coverage, "coverage"),
        (validate_rules, "rules"),
        (validate_compliance_rules, "compliance rules"),
    ):
        try:
            check(manifest)
        except Exception as exc:  # noqa: BLE001 - a bad manifest is an error, not a crash
            errors.append("fleet-seats.json %s: %s" % (name, exc))
    try:
        validate_platforms(manifest, known_platforms())
    except Exception as exc:  # noqa: BLE001 - a bad manifest is an error, not a crash
        errors.append("fleet-seats.json platforms: %s" % exc)
    return errors


def main() -> int:
    data = load()
    apps = data.get("apps", [])
    errors: list[str] = []

    digest = (ROOT / "scripts" / "build-fleet-daily-digest.py").read_text()
    public_repos = set(PUBLIC_REPOS)
    # The document was renamed:  the mirror and the live copy each hold exactly one name now, and the old one is
    # gone.  The fleet points at FLEET-PROTOCOL.md by its current name only.
    doc_names = ("FLEET-PROTOCOL.md",)
    agent_sync = next(
        (ROOT / n for n in doc_names if (ROOT / n).is_file()), ROOT / doc_names[0]
    ).read_text()
    live_sync = next((APPS / n for n in doc_names if (APPS / n).is_file()), APPS / doc_names[0])
    live_checks = APPS.is_dir() if _board_home_env else live_sync.is_file()
    if _board_home_env and not live_checks:
        errors.append(f"FLEET_BOARD_HOME is not a directory: {APPS}")
    elif not live_checks:
        print(
            f"note: live copy checks skipped (no FLEET_BOARD_HOME, {APPS} has no FLEET-PROTOCOL.md)"
        )

    for app in apps:
        repo = app["repo"]
        acronym = app["acronym"]

        if repo in public_repos and f'"{repo}": (' not in digest:
            errors.append(f"digest REPO_BADGE missing {repo}")
        color = app.get("digestColor") or ""
        badge = app.get("badgeClass") or ""
        if repo in public_repos and color and badge.startswith("repo-"):
            css_var = badge[len("repo-") :]
            needle = f"--{css_var}: {color}"
            if needle not in digest:
                errors.append(
                    f"digest CSS --{css_var} does not match digestColor {color} for {repo}"
                )
        icon = app.get("iconFile")
        if app.get("hasAppIcon") and icon:
            if (
                not (ROOT / icon).is_file()
                and not (ROOT / "agent-logos" / Path(icon).name).is_file()
            ):
                errors.append(f"missing app icon {icon}")

    errors.extend(agent_sync_errors(apps, agent_sync, "coordinator FLEET-PROTOCOL.md"))
    if live_sync.is_file():
        errors.extend(
            agent_sync_errors(
                apps, live_sync.read_text(), "~/apps/FLEET-PROTOCOL.md", fold_repo=True
            )
        )

    errors.extend(zulip_errors(data))
    errors.extend(seat_apps_errors(data))
    errors.extend(engine_only_errors(data, strip_generated_header(agent_sync)))

    if MANIFEST.is_file():
        errors.extend(manifest_errors())
    else:
        print(
            "note: the desired-state manifest (fleet-seats.json) is not here yet;  "
            "the manifest check is skipped"
        )

    colors: dict[str, str] = {}
    for app in apps:
        color = (app.get("digestColor") or "").lower()
        repo = app["repo"]
        if not color:
            continue
        prev = colors.get(color)
        if prev:
            errors.append(f"digestColor {color} reused by {prev} and {repo}")
        else:
            colors[color] = repo

    for wf_name in ("fleet-activity-site.yml", "agent-calendar.yml"):
        wf = ROOT / ".github" / "workflows" / wf_name
        if not wf.is_file():
            errors.append(f"missing .github/workflows/{wf_name}")
            continue
        lists = re.findall(r"FLEET_REPOS:\s*(\S+)", wf.read_text())
        if not lists:
            errors.append(f"{wf_name} missing FLEET_REPOS")
            continue
        for lst in lists:
            configured = set(lst.split(","))
            if configured != public_repos:
                errors.append(f"{wf_name} FLEET_REPOS must match verified public allowlist")

    backup_py = ROOT / "scripts" / "backup-fleet-to-gdrive.py"
    if backup_py.is_file():
        backup_src = backup_py.read_text()
        if "fleet-apps.json" not in backup_src:
            errors.append(
                "scripts/backup-fleet-to-gdrive.py must read fleet-apps.json so new apps are included"
            )
        gha = ROOT / ".github" / "workflows" / "backup-repos.yml"
        if not gha.is_file():
            errors.append("missing .github/workflows/backup-repos.yml (GitHub artifact backup)")
        elif "fleet-apps.json" not in gha.read_text():
            errors.append(".github/workflows/backup-repos.yml must read fleet-apps.json")

    if errors:
        print("fleet registry check FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"fleet registry check OK ({len(apps)} apps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
