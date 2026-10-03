#!/usr/bin/env python3
"""Specialize Monet-canonical fleet SKILL.md text for a destination seat.

docs/fleet-skills stays the Monet / Claude.app upload pack.  Every other
platform install must rewrite Slack tags, Notes names, branch prefixes,
worktree suffixes, and reader-facing Claude/Monet voice or that seat will
sign as Monet.  Skills that are meaningless on a harness are skipped, not
rewritten into a Claude-voiced copy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


IDENTITY_TOKEN = "@@SEAT_IDENTITY_PARAGRAPH@@"
YOU_ARE_TOKEN = "@@SEAT_YOU_ARE@@"
SEAT_LINE_TOKEN = "@@SEAT_BRANCH_LINE@@"
NEVER_PUSH_TOKEN = "@@SEAT_NEVER_PUSH@@"


@dataclass(frozen=True)
class Seat:
    tag: str
    notes: str
    prefix: str
    suffix: str
    dest: str
    mode: str  # exclusive | claude_shared
    identity_paragraph: str
    extra_banner: str = ""
    write_home: bool = True
    seat_key: str = ""


def _banner(tag: str, notes: str, prefix: str, suffix: str) -> str:
    inherit = "a shared template"
    if tag == "MONET":
        inherit = "another seat's upload pack"
    return (
        f"> **This install is for `{tag}`.** Slack `[{tag}]`.  Notes `{notes}`.  "
        f"Branches `{prefix}/`.  Worktrees `~/apps/<app>-{suffix}`.  Do not inherit "
        f"another seat's tag from {inherit}.\n\n"
    )


# Coordinator / ops self-id for this repo (Simple-With-Us/AI-Fleet-Coordinator).
# FLEET is a Slack wake (every listening seat on every platform), not this system's name.
COORDINATOR_SELF_ID = "AFC"
OPS_SELF_ID = "OPS"
FLEET_WAKE = "FLEET"

GB_ROLE_TAGS = (
    "GB-CONDUCTOR",
    "GB-MONITOR",
    "GB-FIXER",
    "GB-DEPLOYER",
    "GB-COMPILER",
    "GB-NURSE",
    "GB-HOUSEKEEPER",
    "GB-ACCOUNTANT",
    "GB-ORACLE",
)


def _gb_role_list() -> str:
    parts: list[str] = []
    for tag in GB_ROLE_TAGS:
        if tag == "GB-COMPILER":
            parts.append(f"`[{tag}]` (Compiler)")
        else:
            parts.append(f"`[{tag}]`")
    return ", ".join(parts)


def gb_role_pin_list() -> str:
    """AGENT_TAG=?set … list for Grok Bot skill pins."""
    *head, last = GB_ROLE_TAGS
    return ", ".join(head) + f", or {last}"


def is_grok_bot_tag(tag: str) -> bool:
    t = (tag or "").strip()
    return t.startswith("GB-") or t in GB_ROLE_TAGS


def head_has_fleet_wake(head: str) -> bool:
    """True when Slack header uses FLEET as a Grok Bot wake, not a sender name."""
    return "->FLEET" in head or "->FLEET]" in head


GB_ROLE_LIST = _gb_role_list()

GROK_BOT_BANNER = (
    "> **This install is for Grok Bot roles.** Slack tag is `[GB-<NAME>]` — "
    f"{GB_ROLE_LIST}.  Notes name is the role in Title Case (`Conductor`, "
    "`Monitor`, …).  Cloud branches are often `cursor/`.  Never `[GROK-BOT]`, "
    "`[CURSOR]`, `[GROK]`, or `[MONET]`.\n\n"
)

GROK_BOT_IDENTITY = (
    "This pack is for **Grok Bot** roles driving Cursor cloud agents.  "
    f"Slack tag is `[GB-<NAME>]` — one of {GB_ROLE_LIST}.  "
    "Not `[GROK-BOT]`, not `[CURSOR]`, not `[GROK]`, not `[MONET]`.  "
    "Notes name is the role in Title Case.  Cloud branches are often "
    "`cursor/<slug>`.  Pin `AGENT_TAG` to your GB role before Slack or "
    "`board --by`.  Local Cursor IDE on the Mac is `[CURSOR]`.  Mac Grok TUI "
    "is `[GROK]`."
)

CURSOR_EXTRA = (
    "> **Runtime fork (Cursor).** Local Cursor IDE / Auto on this Mac is "
    "`[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, "
    "your Slack tag is `[GB-<NAME>]` "
    f"({', '.join(GB_ROLE_TAGS)}) — not `[GROK-BOT]`, not `[CURSOR]`, and not "
    "`[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you "
    "are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.\n\n"
)

GROK_EXTRA = (
    "> **Runtime fork (Grok).** Mac Grok TUI / CLI is `[GROK]`.  If this session "
    "is **Grok Build**, pin `AGENT_SEAT=GROK-BUILD`, tag `[GROK-BUILD]`, branches "
    "`grok-build/`, worktrees `~/apps/<app>-grok-build`.  Grok Bot (Cursor cloud) "
    "uses `[GB-<NAME>]` role tags, not this pack and not `[GROK-BOT]`.  "
    "Never `[MONET]`.\n\n"
)

CLAUDE_SHARED_BANNER = (
    "> **Shared `~/.claude/skills`.** Monet, Claude/Fable, and (when active) Renoir "
    "all load this directory.  Do not treat the word Monet in examples as proof of "
    "your seat.  Pin `AGENT_SEAT` / `AGENT_TAG` from the logged-in account before "
    "Slack or `board --by`:\n"
    "> - Monet → `MONET`, Notes `Monet`, `monet/`, `~/apps/<app>-monet`\n"
    "> - Claude / Fable → `CLAUDE`, Notes `Claude`, `claude/`, `~/apps/<app>-claude`\n"
    "> - Renoir → `RENOIR`, Notes `Renoir`, `renoir/`, `~/apps/<app>-renoir`\n"
    "> Cursor, Grok, Grok Bot, Codex, AG, DeepSeek, Kimi, and Fx have their own "
    "skill dirs and must not take identity from here.\n\n"
)

KIMI_IDENTITY = (
    "This pack is for **KIMI**.  Tag `[KIMI]`.  Notes name `Kimi`.  "
    "**KIMI is retired / unavailable long-term (owner 2026-08-21).** Do not "
    "start a Kimi session.  Do not take new work, do not leave Kimi In Progress, "
    "and do not reserve future lanes for Kimi.  If you are reading this after a "
    "mistaken spawn, say so on Slack and stop.  Never sign as Monet."
)

KIMI_RETIRED_BANNER = (
    "> **Retired seat.** Owner directive 2026-08-21: do not assign or "
    "accept new Kimi work.  Do not start a Kimi session.  Do not take work.  "
    "This catalog copy is inactive — do not install to `~/.kimi`.\n\n"
)

MINIMAX_EXTRA = (
    "> **Runtime (MiniMax).** MiniMax Code has no global rules file.  The "
    "fleet pointer lives in `~/.minimax/memory/user.md` (user memory, injected "
    "into every session's system prompt); per-repo `AGENTS.md` is project "
    "memory.  Skills here are loaded on demand from `<available_skills>`, so "
    "read the one that matches before acting — nothing in this directory is "
    "auto-applied.  `config.yaml` ships `permissionMode: bypassPermissions`, so "
    "nothing prompts: hold the destructive-op pause yourself.\n\n"
)

RENOIR_INACTIVE_BANNER = (
    "> **Inactive seat.** Renoir is not yet active.  Do not install to "
    "`~/.renoir/skills`.  Do not take fleet work until the owner opens the "
    "seat.\n\n"
)

SEATS: dict[str, Seat] = {
    "cursor": Seat(
        "CURSOR", "Cursor", "cursor", "cursor",
        "~/.cursor/skills", "exclusive",
        "This pack is for the **CURSOR** seat (Cursor IDE and Auto on this Mac).  "
        "Tag `[CURSOR]`.  Notes name `Cursor`.  Branches `cursor/<slug>` only.  "
        "Worktrees `~/apps/<prefix>-cursor`.  Never post Slack as `[MONET]`, "
        "`[CLAUDE]`, or `[GROK]`.  A skill copied from the Monet pack is not your "
        "name — this install is.  Pin `AGENT_SEAT=CURSOR`.  Incident: 2026-08-23 "
        "Cursor inherited Monet identity from an unspecialized skill copy.",
        extra_banner=CURSOR_EXTRA,
        seat_key="cursor",
    ),
    "ag": Seat(
        "AG", "Antigravity", "ag", "antigravity",
        "~/.gemini/skills", "exclusive",
        "This pack is for **AG** (Antigravity / Gemini).  Tag `[AG]`.  Notes name "
        "`Antigravity`.  Branches `ag/<slug>` (keep `agent/antigravity` only if the lane "
        "already uses it).  Worktrees `~/apps/<prefix>-antigravity`.  Never sign "
        "as Monet, Cursor, or Claude.  Pin `AGENT_SEAT=AG`.",
        seat_key="ag",
    ),
    "codex": Seat(
        "CODEX", "Codex", "codex", "codex",
        "~/.codex/skills", "exclusive",
        "This pack is for **CODEX**.  Tag `[CODEX]`.  Notes name `Codex`.  "
        "Branches `codex/<slug>` only.  Worktrees `~/apps/<prefix>-codex`.  "
        "Never sign as Monet.  Pin `AGENT_SEAT=CODEX`.",
        seat_key="codex",
    ),
    "grok": Seat(
        "GROK", "Grok", "grok", "grok",
        "~/.grok/skills", "exclusive",
        "This pack is for the **GROK** Mac TUI / CLI seat.  Tag `[GROK]`.  "
        "Notes name `Grok`.  Branches `grok/<slug>` only.  Worktrees "
        "`~/apps/<prefix>-grok`.  Never sign as Monet or Grok Bot.  Pin "
        "`AGENT_SEAT=GROK`.",
        extra_banner=GROK_EXTRA,
        seat_key="grok",
    ),
    "grok-build": Seat(
        "GROK-BUILD", "Grok Build", "grok-build", "grok-build",
        "~/.grok-build/skills", "exclusive",
        "This pack is for **GROK-BUILD** (Grok Build TUI / App Builder).  Tag "
        "`[GROK-BUILD]`.  Notes name `Grok Build`.  Branches `grok-build/<slug>` "
        "only.  Worktrees `~/apps/<prefix>-grok-build`.  Do not use `grok/` or "
        "sign as GROK or a Grok Bot `[GB-<NAME>]` role.  Pin "
        "`AGENT_SEAT=GROK-BUILD`.",
        seat_key="grok-build",
    ),
    "fx": Seat(
        "FX", "Fx", "fx", "fx",
        "~/.fx/skills", "exclusive",
        "This pack is for the **FX** terminal agent (`fx` / `fx.sh`).  Tag "
        "`[FX]`.  Notes name `Fx`.  Branches `fx/<slug>` only.  Worktrees "
        "`~/apps/<prefix>-fx`.  This is not Cursor, not Codex, and not Monet.  "
        "Pin `AGENT_SEAT=FX`.",
        extra_banner=(
            "> **Runtime (fx).** Local Cursor IDE remains `[CURSOR]`.  Codex CLI "
            "remains `[CODEX]`.  Do not inherit those tags from a shared skill "
            "directory fx also scans (`~/.claude/skills`, `~/.codex/skills`).  "
            "Prefer `~/.fx/skills` for this seat.\n\n"
        ),
        seat_key="fx",
    ),
    "grok-bot": Seat(
        "GB-<NAME>", "Grok Bot", "cursor", "cursor",
        "docs/fleet-skills/by-seat/grok-bot", "grok_bot",
        GROK_BOT_IDENTITY,
        extra_banner=GROK_BOT_BANNER,
        write_home=False,
        seat_key="grok-bot",
    ),
    "claude": Seat(
        "CLAUDE", "Claude", "claude", "claude",
        "docs/fleet-skills/by-seat/claude", "exclusive",
        "This pack is for **CLAUDE** (Claude / Fable account).  Tag `[CLAUDE]`.  "
        "Notes name `Claude`.  Branches `claude/<slug>` only.  Worktrees "
        "`~/apps/<prefix>-claude`.  Monet is a different Claude login (`MONET`, "
        "`monet/`).  Renoir is a different seat (`RENOIR`).  Never sign as Monet.  "
        "Pin `AGENT_SEAT=CLAUDE`.",
        write_home=False,
        seat_key="claude",
    ),
    "monet": Seat(
        "MONET", "Monet", "monet", "monet",
        "~/Desktop/fleet-skills", "exclusive",
        "This pack is for the **MONET** Claude account.  Tag `[MONET]`.  Notes "
        "name `Monet`.  Branches `monet/<slug>` only.  CLAUDE and MONET are two "
        "different Claude accounts.  Local `~/.claude` (hooks, memory, skills) is "
        "shared.  The worktree folder is **not** a seat signal.  Pin "
        "`AGENT_SEAT=MONET`.  If the owner did not name Monet and the worktree is "
        "anonymous, **ask** — do not default to CLAUDE.  Incident: 2026-07-05 "
        "CLAUDE↔MONET ping-pong from inferred seats.",
        write_home=True,
        seat_key="monet",
    ),
    "renoir": Seat(
        "RENOIR", "Renoir", "renoir", "renoir",
        "~/.renoir/skills", "exclusive",
        "This pack is for **RENOIR** (future third Claude-family seat).  Tag "
        "`[RENOIR]`.  Notes name `Renoir`.  Branches `renoir/<slug>` only.  "
        "Worktrees `~/apps/<prefix>-renoir`.  Renoir is not Monet and not Claude.  "
        "If this seat is not yet active, do not take fleet work — say so.  Pin "
        "`AGENT_SEAT=RENOIR`.",
        extra_banner=RENOIR_INACTIVE_BANNER,
        write_home=False,
        seat_key="renoir",
    ),
    "deepseek": Seat(
        "DSH", "DeepSeek Harness", "deepseek", "deepseek",
        "~/.deepseek/skills", "exclusive",
        "This pack is for **DSH** (DeepSeek Harness).  Tag `[DSH]`.  "
        "Notes name `DeepSeek Harness`.  Branches `deepseek/<slug>` only.  "
        "Worktrees `~/apps/<prefix>-deepseek`.  Running a DeepSeek model "
        "*inside Cursor* does not make you this seat — that is `[CURSOR]`.  "
        "Former Slack tag `DEEPSEEK` is retired.  Pin `AGENT_SEAT=DSH`.",
        seat_key="deepseek",
    ),
    "minimax": Seat(
        "MM", "MiniMax", "minimax", "minimax",
        "~/.minimax/skills", "exclusive",
        "This pack is for **MM** (MiniMax Code on the Mavis local runtime).  "
        "Tag `[MM]`.  Notes name `MiniMax`.  Branches `minimax/<slug>` only.  "
        "Worktrees `~/apps/<prefix>-minimax`.  Running a MiniMax *model* inside "
        "another harness does not make you this seat.  Built-in Mavis sub-agents "
        "(`explore`, `worker`, `verifier`) inherit `MM` — they do not get "
        "their own Slack identity.  Former Slack tag `MINIMAX` is retired.  "
        "Pin `AGENT_SEAT=MM`.",
        extra_banner=MINIMAX_EXTRA,
        seat_key="minimax",
    ),
    "kimi": Seat(
        "KIMI", "Kimi", "kimi", "kimi",
        "~/.kimi/skills", "exclusive",
        KIMI_IDENTITY,
        extra_banner=KIMI_RETIRED_BANNER,
        write_home=False,
        seat_key="kimi",
    ),
    "claude_shared": Seat(
        "MONET", "Monet", "monet", "monet",
        "~/.claude/skills", "claude_shared",
        "This pack is for the **MONET** Claude account.  Tag `[MONET]`.  "
        "Notes name `Monet`.  Branches `monet/<slug>` only.",
        extra_banner=CLAUDE_SHARED_BANNER,
        seat_key="claude_shared",
    ),
}

MONET_PACK_LINE = (
    "This pack is for the **MONET** Claude account.  Tag `[MONET]`.  "
    "Notes name `Monet`.  Branches `monet/<slug>` only."
)

MONET_CLAUDE_SHARED_PARA = (
    "CLAUDE and MONET are two different Claude accounts.  Local `~/.claude` "
    "(hooks, memory, skills) is shared.  The worktree folder is **not** a seat "
    "signal.  Pin `AGENT_SEAT=MONET`.  If the owner did not name Monet and the "
    "worktree is anonymous, **ask** — do not default to CLAUDE.  Incident: "
    "2026-07-05 CLAUDE↔MONET ping-pong from inferred seats."
)

IDENTITY_SKILL_NAMES = {
    "session-start",
    "board-ops",
    "closeout",
    "apple-notes",
    "land-lane",
    "pickup-seat",
    "owner-copy",
    "secret-handoff",
    "deploy-verify",
    "unstick-pr",
    "codex-triage",
    "fleet-coordination",
    "fleet-infra",
    "dns-and-registrars",
    "mac-cleanup",
    "housekeeper",
    "sentence-gap",
}

# Never install these to any seat (including Monet / Claude.app zips).
# Compiler / GB-COMPILER owns iOS builds on GitHub-hosted macos-latest.
# DealDex's hosted Actions ship stays — do not disable it.
# Fleet seats must not be taught a local Mac xcodebuild / TestFlight /
# ios-ship-now / --force-ship loop.
NEVER_INSTALL = frozenset({"ios-ship"})

# Optional allowlist of Seat.seat_key values.  A missing key means every
# seat except NEVER_INSTALL.  Use this when a skill is only meaningful on
# one harness.  `codex-triage` is not Codex-only — the name is historical;
# the body is GitHub review-thread triage for every seat that lands PRs.
# `mac-cleanup` is Mac disk cleanup; omit from cloud Grok Bot.
SKILL_SEAT_ALLOWLIST: dict[str, frozenset[str]] = {
    "mac-cleanup": frozenset({
        "cursor",
        "ag",
        "codex",
        "grok",
        "grok-build",
        "fx",
        "claude",
        "monet",
        "renoir",
        "deepseek",
        "kimi",
        "minimax",
        "claude_shared",
    }),
}

CLAUDE_FAMILY_TAGS = frozenset({"MONET", "CLAUDE", "RENOIR"})

# Fragments that mean "run a local Mac iOS ship" or "--force-ship".
# Rendered skills must not teach these.  Historical rollouts may still
# mention them.
FORBIDDEN_LOCAL_IOS_SHIP = (
    "--force-ship",
    "com.jay.ios-ship-now",
    "ios-ship-now",
    "trading-live-mac-ci",
    "xcodebuild and `xcrun simctl` via bash are pre-approved",
    "TestFlight-ship fleet iOS",
    "see `ios-ship`",
    "scripts/ios-ship-testflight.sh",
    "Mac Xcode/TestFlight ship runners",
    "Mac Xcode ship runners",
)

_PROTECT = [
    ("Monet: `[MONET]` (display `Monet`, branch prefix `monet/`)", "@@SEAT_MONET_ROW@@"),
    ("Monet: `[MONET]`", "@@SEAT_MONET_TAG_ROW@@"),
    ("(Antigravity/Gemini, Monet, Claude, Cursor, Grok, Codex, DeepSeek)", "@@SEAT_ALL_LIST@@"),
    ("Antigravity/Gemini, Monet, Claude, Cursor, Grok, Codex, DeepSeek", "@@SEAT_ALL_LIST2@@"),
    ("Release notes **must not** contain agent names (`Monet`, `Claude`, `Grok`, …).", "@@IOS_SHIP_NO_NAMES@@"),
    ("Release notes **must not** contain agent names (`Monet`, `Claude`, `Grok`, …)", "@@IOS_SHIP_NO_NAMES2@@"),
    ("Release notes **must not** contain agent names", "@@IOS_SHIP_NO_NAMES3@@"),
    ("Monet and Claude Code both use", "@@CLAUDE_COMMIT_TRAILER@@"),
    ("Monet and Claude Code", "@@MONET_CLAUDE_CODE@@"),
    ("Monet's portable", "@@PORTABLE1@@"),
    ("Monet portable", "@@PORTABLE2@@"),
    ("Monet's protocol", "@@PORTABLE3@@"),
    ("Socratic.Trade-monet", "@@LANE1@@"),
    ("agent/monet", "@@LANE2@@"),
    ("CLAUDE↔MONET", "@@INCIDENT@@"),
    ("Monet, Renoir, and Claude Code", "@@PS1@@"),
    ("Monet/Renoir/Claude", "@@PS2@@"),
    ("Monet / Claude.app", "@@SEAT_MONET_CLAUDE_APP@@"),
    ("Monet, Claude, and", "@@SEAT_MONET_CLAUDE_AND@@"),
    ("Monet, Claude/Fable", "@@SEAT_MONET_CLAUDE_FABLE@@"),
    ("Monet, Grok, Claude", "@@SEAT_MONET_GROK_CLAUDE@@"),
    ("Monet, Cursor, or Claude", "@@SEAT_MONET_CURSOR_CLAUDE@@"),
    ("Monet or Claude", "@@SEAT_MONET_OR_CLAUDE@@"),
        ("Monet, Cursor, or Grok", "@@SEAT_MONET_CURSOR_GROK@@"),
        ("`~/Desktop/fleet-skills/sentence-gap/SKILL.md` — Monet portable paste", "@@SEAT_MONET_DESKTOP_GAP@@"),
        ("Grok Bot: `[GB-<NAME>]`", "@@SEAT_GROK_BOT_ROW@@"),
    ]



def _protect(text: str) -> str:
    for src, tok in _PROTECT:
        text = text.replace(src, tok)
    return text


def _unprotect(text: str) -> str:
    for src, tok in _PROTECT:
        text = text.replace(tok, src)
    return text


def is_claude_family(seat: Seat) -> bool:
    return seat.mode == "claude_shared" or seat.tag in CLAUDE_FAMILY_TAGS


def skill_home_dir(seat: Seat) -> str:
    if seat.dest.startswith("~"):
        return seat.dest
    return "this seat's skill directory"


def catalog_skill_names(docs_skills: str) -> list[str]:
    names: list[str] = []
    if not os.path.isdir(docs_skills):
        return names
    for entry in sorted(os.listdir(docs_skills)):
        path = os.path.join(docs_skills, entry)
        if not os.path.isdir(path) or entry.startswith(".") or entry == "by-seat":
            continue
        if entry in NEVER_INSTALL:
            continue
        if os.path.isfile(os.path.join(path, "SKILL.md")):
            names.append(entry)
    return names


def skill_allowed_for_seat(skill_name: str, seat: Seat) -> bool:
    if skill_name in NEVER_INSTALL:
        return False
    allow = SKILL_SEAT_ALLOWLIST.get(skill_name)
    if allow is None:
        return True
    key = seat.seat_key or seat.tag.lower()
    return key in allow


def _rewrite_reader_voice(text: str, seat: Seat) -> str:
    """Stop addressing a non-Monet reader as if they are Monet/Claude."""
    if seat.tag == "MONET" and seat.mode == "exclusive":
        return text
    swaps = [
        (
            "(`~/Desktop/fleet-skills/sentence-gap/SKILL.md` — Monet portable paste).",
            "(this pack's `sentence-gap` — Monet portable protocol).",
        ),
        (
            "`~/Desktop/fleet-skills/sentence-gap/SKILL.md` — Monet portable paste",
            "this pack's `sentence-gap` — Monet portable protocol",
        ),
        (
            "from a shared Monet template",
            "from a shared template",
        ),
    ]
    if not is_claude_family(seat):
        home = skill_home_dir(seat)
        swaps.extend(
            [
                (
                    "Load `~/.claude/skills/secret-safety/SKILL.md` as well when that file exists.",
                    f"Load `{home}/secret-safety/SKILL.md` as well when that file exists.",
                ),
                (
                    "- `~/.claude/skills/secret-safety/SKILL.md`",
                    f"- `{home}/secret-safety/SKILL.md`",
                ),
                (
                    "Chat replies (Claude/Monet transcript):",
                    "Chat replies (this Markdown transcript):",
                ),
                (
                    "**Chat replies** (Claude/Monet transcript):",
                    "**Chat replies** (this Markdown transcript):",
                ),
                (
                    'Claude Code only offers "Always Allow" when the command has a stable prefix.',
                    "Some agent CLIs only allowlist a stable command prefix.",
                ),
                (
                    "Co-Authored-By: Claude <noreply@anthropic.com>",
                    "Co-Authored-By: <peer's existing trailer>",
                ),
            ]
        )
    for old, new in swaps:
        text = text.replace(old, new)
    return text


def _rewrite_universal_voice(text: str) -> str:
    swaps = [
        (
            "Load `~/.claude/skills/secret-safety/SKILL.md` as well when that file exists.",
            "Load `<YOUR_SKILLS_DIR>/secret-safety/SKILL.md` as well when that file exists.",
        ),
        (
            "- `~/.claude/skills/secret-safety/SKILL.md`",
            "- `<YOUR_SKILLS_DIR>/secret-safety/SKILL.md`",
        ),
        (
            "Chat replies (Claude/Monet transcript):",
            "Chat replies (Markdown chat transcript):",
        ),
        (
            "**Chat replies** (Claude/Monet transcript):",
            "**Chat replies** (Markdown chat transcript):",
        ),
        (
            'Claude Code only offers "Always Allow" when the command has a stable prefix.',
            "Some agent CLIs only allowlist a stable command prefix.",
        ),
        (
            "(`~/Desktop/fleet-skills/sentence-gap/SKILL.md` — Monet portable paste).",
            "(this pack's `sentence-gap` — Monet portable protocol).",
        ),
    ]
    for old, new in swaps:
        text = text.replace(old, new)
    return text


def _specialize_grok_bot(text: str, seat: Seat, skill_name: str) -> str:
    pin = (
        f'AGENT_TAG="${{AGENT_TAG:?set {gb_role_pin_list()}}}"'
    )
    text = _stash_identity_source(text)
    text = _protect(text)
    ordered = [
        ("AGENT_SEAT=MONET", pin.replace("AGENT_TAG", "AGENT_SEAT", 1)),
        ("AGENT_TAG=MONET", pin),
        ("SLACK_AGENT_NAME=MONET", "SLACK_AGENT_NAME=$AGENT_TAG"),
        ("--by MONET", '--by "$AGENT_TAG"'),
        ("--mine MONET", '--mine "$AGENT_TAG"'),
        ("[MONET->", "[$AGENT_TAG->"),
        ("[MONET]", "[$AGENT_TAG]"),
        ("`[MONET`", "`[$AGENT_TAG`"),
        ("`[MONET ", "`[$AGENT_TAG "),
        ("`[MONET]", "`[$AGENT_TAG]"),
        ("**MONET**", "**$AGENT_TAG**"),
        ("(MONET)", "(GB role)"),
        ("# Session start (MONET)", "# Session start (Grok Bot)"),
        ("# Pick up a seat (MONET)", "# Pick up a seat (Grok Bot)"),
        ("# Closeout (MONET)", "# Closeout (Grok Bot)"),
        ("# Apple Notes (MONET)", "# Apple Notes (Grok Bot)"),
        ("# THE BOARD (MONET)", "# THE BOARD (Grok Bot)"),
        ("# Land a feature branch (MONET)", "# Land a feature branch (Grok Bot)"),
        ("# Owner-facing copy (MONET)", "# Owner-facing copy (Grok Bot)"),
        ("# Secret handoff (MONET)", "# Secret handoff (Grok Bot)"),
        ("# Deploy verification (MONET)", "# Deploy verification (Grok Bot)"),
        ("# iOS agent loop (MONET)", "# iOS agent loop (Grok Bot)"),
        ("# Unstick a blocked PR (MONET)", "# Unstick a blocked PR (Grok Bot)"),
        ("# Review-thread triage (MONET)", "# Review-thread triage (Grok Bot)"),
        ("monet/<slug>", "cursor/<slug>"),
        ("monet/fix", "cursor/fix"),
        ("`monet/", "`cursor/"),
        (" monet/", " cursor/"),
        ("-b monet/", "-b cursor/"),
        ("@ monet/", "@ cursor/"),
        ("-monet-", "-cursor-"),
        ("-monet`", "-cursor`"),
        ("-monet ", "-cursor "),
        ("-monet\n", "-cursor\n"),
        ("-monet (", "-cursor ("),
        ("Monet worktree", "Grok Bot worktree"),
        ("Monet session", "Grok Bot session"),
        ("every Monet", "every Grok Bot"),
        ("Start every Monet", "Start every Grok Bot"),
        ("Finish a Monet", "Finish a Grok Bot"),
        ("Land a Monet", "Land a Grok Bot"),
        ("whenever Monet", "whenever a Grok Bot"),
        ("Use whenever Monet", "Use whenever a Grok Bot"),
        ("Notes name `Monet`", "Notes name in Title Case for the GB role"),
        ("then `Monet` (Title Case", "then the GB role (Title Case"),
        ("then `Monet`", "then the GB role"),
        ("[APP, Monet]", "[APP, <GB role>]"),
        ("[ST, CT, Monet]", "[ST, CT, <GB role>]"),
        ("(Monet):", "(<GB role>):"),
        ("Monet (not Claude)", "your GB role (not Cursor, not Grok TUI)"),
        ("Monet — never skip", "this GB role — never skip"),
        ("Monet's job on these", "this GB role's job on these"),
        ("parallel Monet lanes", "parallel Grok Bot lanes"),
        (
            "Acronyms first, then `Monet` (Title Case, not all-caps Slack tags).",
            "Acronyms first, then the GB role in Title Case (not `[GROK-BOT]`).",
        ),
        ("Acronyms first, then `Monet`", "Acronyms first, then the GB role in Title Case"),
        ("Monet/peer", "Grok Bot/peer"),
        ("`FLEET`, `MONET`", "`FLEET`, `$AGENT_TAG`"),
    ]
    for old, new in ordered:
        text = text.replace(old, new)
    text = text.replace(IDENTITY_TOKEN, seat.identity_paragraph)
    text = text.replace(
        YOU_ARE_TOKEN,
        "You are **$AGENT_TAG** (a `[GB-<NAME>]` role).  Cloud branches are often `cursor/`.",
    )
    text = text.replace(
        SEAT_LINE_TOKEN,
        "Seat: **$AGENT_TAG**.  Branch: `cursor/<slug>`.  Never `[GROK-BOT]`.  Never `[CURSOR]`.",
    )
    text = text.replace(
        NEVER_PUSH_TOKEN,
        "Never sign as `[GROK-BOT]`, `[CURSOR]`, `[GROK]`, or `[MONET]`.  Only your `[GB-<NAME>]` tag.",
    )
    text = _unprotect(text)
    text = _rewrite_reader_voice(text, seat)
    banners = GROK_BOT_BANNER
    if skill_name in IDENTITY_SKILL_NAMES:
        banners = GROK_BOT_BANNER
    text = _insert_after_first_heading(text, banners)
    return fold_yaml_description(text)


def _insert_after_first_heading(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "".join(lines[: i + 1] + ["\n", block] + lines[i + 1 :])
    return block + text


def _stash_identity_source(text: str) -> str:
    text = text.replace(
        MONET_PACK_LINE + "\n\n" + MONET_CLAUDE_SHARED_PARA,
        IDENTITY_TOKEN,
    )
    text = text.replace(MONET_PACK_LINE, IDENTITY_TOKEN)
    text = text.replace(MONET_CLAUDE_SHARED_PARA, "")
    text = text.replace(
        "You are **MONET**.  Keep `monet/` branches.",
        YOU_ARE_TOKEN,
    )
    text = text.replace(
        "Seat: **MONET**.  Branch: `monet/<slug>`.  Never `claude/`.",
        SEAT_LINE_TOKEN,
    )
    text = text.replace("Seat: **MONET**.  Branch: `monet/<slug>`.", SEAT_LINE_TOKEN)
    text = text.replace(
        "Never open or push `claude/*` from a Monet session.",
        NEVER_PUSH_TOKEN,
    )
    return text


def _unstash_identity(text: str, seat: Seat) -> str:
    article = "an" if seat.notes[:1].lower() in "aeiou" else "a"
    never = (
        f"Never open or push another seat's prefix from {article} {seat.notes} session.  "
        f"Only `{seat.prefix}/`."
    )
    if seat.tag == "MONET":
        never = "Never open or push `claude/*` from a Monet session."
    text = text.replace(IDENTITY_TOKEN, seat.identity_paragraph)
    text = text.replace(
        YOU_ARE_TOKEN,
        f"You are **{seat.tag}**.  Keep `{seat.prefix}/` branches.",
    )
    extra_never = ""
    if seat.tag not in {"MONET", "CLAUDE"}:
        extra_never = "  Never `claude/`.  Never `monet/`."
    text = text.replace(
        SEAT_LINE_TOKEN,
        f"Seat: **{seat.tag}**.  Branch: `{seat.prefix}/<slug>`.{extra_never}",
    )
    text = text.replace(NEVER_PUSH_TOKEN, never)
    return text


def fold_yaml_description(text: str) -> str:
    """Rewrite SKILL.md `description` to a `>-` block when quotes would break fx.

    fx's skill metadata parser rejects inline `"` / `'` in YAML descriptions
    (`malformed_quote`).  A folded `>-` block is the documented fix.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    front = text[4:end]
    body = text[end + 5 :]
    lines = front.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("description:"):
            out.append(line)
            i += 1
            continue
        raw = line[len("description:") :].strip()
        collected: list[str] = []
        if raw in {">", ">-", "|", "|-", ">|"} or (
            raw.startswith(">") or raw.startswith("|")
        ):
            extra = raw.lstrip(">-|").strip()
            if extra:
                collected.append(extra)
            i += 1
            while i < len(lines) and (
                lines[i].startswith(" ") or lines[i].startswith("\t")
            ):
                collected.append(lines[i].strip())
                i += 1
            value = " ".join(p for p in collected if p)
        else:
            value = raw
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                quote = value[0]
                inner = value[1:-1]
                if quote == '"':
                    inner = inner.replace('\\"', '"')
                value = inner
            i += 1
        if '"' in value or "'" in value or "${" in value:
            out.append("description: >-")
            out.append("  " + value)
        else:
            out.append("description: " + value)
    return "---\n" + "\n".join(out) + "\n---\n" + body


def rewrite_skill_tree(root: str) -> int:
    """Fold quoted descriptions under a skills directory.  Returns files changed."""
    if not os.path.isdir(root):
        return 0
    changed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if "SKILL.md" not in filenames:
            continue
        path = os.path.join(dirpath, "SKILL.md")
        with open(path, encoding="utf-8") as handle:
            old = handle.read()
        new = fold_yaml_description(old)
        if new != old:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(new)
            changed += 1
    return changed


def specialize_from_monet(text: str, seat: Seat, skill_name: str = "") -> str:
    if seat.mode == "grok_bot":
        return _specialize_grok_bot(text, seat, skill_name)

    if seat.mode == "claude_shared":
        out = text.replace(
            MONET_PACK_LINE + "\n\n" + MONET_CLAUDE_SHARED_PARA,
            "This shared pack is for the Claude-family login that is active "
            "right now.  Pin `AGENT_SEAT` to **MONET**, **CLAUDE**, or **RENOIR** "
            "before Slack or `board --by`.  Do not guess from the worktree folder.",
        )
        out = out.replace(
            MONET_PACK_LINE,
            "This shared pack is for the Claude-family login that is active "
            "right now.  Pin `AGENT_SEAT` to **MONET**, **CLAUDE**, or **RENOIR**.",
        )
        out = out.replace(
            "You are **MONET**.  Keep `monet/` branches.",
            "You are **$AGENT_SEAT** (MONET, CLAUDE, or RENOIR).  Keep that seat's prefix.",
        )
        out = out.replace(
            "Seat: **MONET**.  Branch: `monet/<slug>`.  Never `claude/`.",
            "Seat: **$AGENT_SEAT**.  Branch: `<monet|claude|renoir>/<slug>`.",
        )
        out = _insert_after_first_heading(out, seat.extra_banner or CLAUDE_SHARED_BANNER)
        out = out.replace(
            "AGENT_SEAT=MONET",
            'AGENT_SEAT="${AGENT_SEAT:?set MONET, CLAUDE, or RENOIR}"',
        )
        out = out.replace(
            "AGENT_TAG=MONET",
            'AGENT_TAG="${AGENT_SEAT:?set MONET, CLAUDE, or RENOIR}"',
        )
        out = out.replace("--by MONET", '--by "$AGENT_SEAT"')
        out = out.replace("--mine MONET", '--mine "$AGENT_SEAT"')
        out = out.replace("[MONET]", '[$AGENT_SEAT]')
        out = out.replace("monet/<slug>", "<monet|claude|renoir>/<slug>")
        return fold_yaml_description(out)

    text = _stash_identity_source(text)
    text = _protect(text)

    ordered = [
        ("AGENT_SEAT=MONET", f"AGENT_SEAT={seat.tag}"),
        ("AGENT_TAG=MONET", f"AGENT_TAG={seat.tag}"),
        ("SLACK_AGENT_NAME=MONET", f"SLACK_AGENT_NAME={seat.tag}"),
        ("--by MONET", f"--by {seat.tag}"),
        ("--mine MONET", f"--mine {seat.tag}"),
        ("[MONET->", f"[{seat.tag}->"),
        ("[MONET]", f"[{seat.tag}]"),
        ("`[MONET`", f"`[{seat.tag}`"),
        ("`[MONET ", f"`[{seat.tag} "),
        ("`[MONET]", f"`[{seat.tag}]"),
        ("**MONET**", f"**{seat.tag}**"),
        ("(MONET)", f"({seat.tag})"),
        ("# Session start (MONET)", f"# Session start ({seat.tag})"),
        ("# Pick up a seat (MONET)", f"# Pick up a seat ({seat.tag})"),
        ("# Closeout (MONET)", f"# Closeout ({seat.tag})"),
        ("# Apple Notes (MONET)", f"# Apple Notes ({seat.tag})"),
        ("# THE BOARD (MONET)", f"# THE BOARD ({seat.tag})"),
        ("# Land a feature branch (MONET)", f"# Land a feature branch ({seat.tag})"),
        ("# Owner-facing copy (MONET)", f"# Owner-facing copy ({seat.tag})"),
        ("# Secret handoff (MONET)", f"# Secret handoff ({seat.tag})"),
        ("# Deploy verification (MONET)", f"# Deploy verification ({seat.tag})"),
        ("# iOS agent loop (MONET)", f"# iOS agent loop ({seat.tag})"),
        ("# Unstick a blocked PR (MONET)", f"# Unstick a blocked PR ({seat.tag})"),
        ("# Review-thread triage (MONET)", f"# Review-thread triage ({seat.tag})"),
        ("monet/<slug>", f"{seat.prefix}/<slug>"),
        ("monet/fix", f"{seat.prefix}/fix"),
        ("`monet/", f"`{seat.prefix}/"),
        (" monet/", f" {seat.prefix}/"),
        ("-b monet/", f"-b {seat.prefix}/"),
        ("@ monet/", f"@ {seat.prefix}/"),
        ("-monet-", f"-{seat.suffix}-"),
        ("-monet`", f"-{seat.suffix}`"),
        ("-monet ", f"-{seat.suffix} "),
        ("-monet\n", f"-{seat.suffix}\n"),
        ("-monet (", f"-{seat.suffix} ("),
        ("Monet worktree", f"{seat.notes} worktree"),
        ("Monet session", f"{seat.notes} session"),
        ("every Monet", f"every {seat.notes}"),
        ("Start every Monet", f"Start every {seat.notes}"),
        ("Finish a Monet", f"Finish a {seat.notes}"),
        ("Land a Monet", f"Land a {seat.notes}"),
        ("whenever Monet", f"whenever {seat.notes}"),
        ("Use whenever Monet", f"Use whenever {seat.notes}"),
        ("Notes name `Monet`", f"Notes name `{seat.notes}`"),
        ("then `Monet` (Title Case", f"then `{seat.notes}` (Title Case"),
        ("then `Monet`", f"then `{seat.notes}`"),
        ("[APP, Monet]", f"[APP, {seat.notes}]"),
        ("[ST, CT, Monet]", f"[ST, CT, {seat.notes}]"),
        ("(Monet):", f"({seat.notes}):"),
        ("Monet (not Claude)", f"{seat.notes} (not another seat)"),
        ("Monet — never skip", f"{seat.notes} — never skip"),
        ("Monet's job on these", f"{seat.notes}'s job on these"),
        ("parallel Monet lanes", f"parallel {seat.notes} lanes"),
        ("Acronyms first, then `Monet` (Title Case, not all-caps Slack tags).", f"Acronyms first, then `{seat.notes}` (Title Case, not all-caps Slack tags)."),
        ("Acronyms first, then `Monet`", f"Acronyms first, then `{seat.notes}`"),
        ("Monet/peer", f"{seat.notes}/peer"),
        ("`FLEET`, `MONET`", f"`FLEET`, `{seat.tag}`"),
    ]
    for old, new in ordered:
        text = text.replace(old, new)

    text = _unstash_identity(text, seat)
    text = _unprotect(text)
    text = _rewrite_reader_voice(text, seat)

    banners = ""
    if skill_name in IDENTITY_SKILL_NAMES:
        banners += _banner(seat.tag, seat.notes, seat.prefix, seat.suffix)
    banners += seat.extra_banner
    if banners:
        text = _insert_after_first_heading(text, banners)
    if seat.seat_key == "kimi":
        text = _apply_retired_kimi(text, skill_name)
    return fold_yaml_description(text)


def _apply_retired_kimi(text: str, skill_name: str) -> str:
    """Keep Kimi voice on catalog copies; never teach start-every-session or take-work."""
    retired = (
        "KIMI is retired.  Do not start a Kimi session.  Do not take work.  "
        "If you are reading this after a mistaken spawn, say so on Slack and stop."
    )
    text = text.replace(
        "Start every Kimi session on this Mac — poll Slack, read THE BOARD, pin "
        "AGENT_SEAT=KIMI, pick the seat worktree, then triple-claim before "
        "editing. Use at session start, after a resume, when switching apps, or "
        "whenever you are about to begin substantial work. Kimi (not another "
        "seat) — never skip this for \"just a small fix.\"",
        retired,
    )
    text = text.replace("Start every Kimi session", "KIMI is retired — do not start a Kimi session")
    text = text.replace("Start every Kimi", "KIMI is retired — do not start")
    if skill_name == "session-start":
        marker = "## 1. Identity"
        idx = text.find(marker)
        if idx >= 0:
            text = (
                text[:idx]
                + "## Stop\n\n"
                + retired
                + "  Do not export `AGENT_SEAT=KIMI` to take work.  "
                "Do not poll, claim, or pick a Kimi lane.  "
                "Coordinator self-id is `AFC`.  `FLEET` wakes every listening seat on every platform.\n"
            )
    return text


def specialize_universal(text: str, skill_name: str = "") -> str:
    """Render a neutral, universal version of the fleet skill for root skills/."""
    text = _stash_identity_source(text)
    text = _protect(text)

    universal_identity = (
        "This universal skill applies across all agent platforms and seats.  "
        "Identify your active seat (**AG**, **CURSOR**, **CODEX**, **GROK**, "
        "**GROK-BUILD**, **CLAUDE**, **MONET**, **RENOIR**, **DSH**, **MM**, "
        "**FX**, or a Grok Bot `[GB-<NAME>]` role), use your own Slack tag "
        "(e.g. `[AG]`, `[CURSOR]`, `[GB-CONDUCTOR]`, `[DSH]`, `[MM]`), branch "
        "prefix (`<seat>/<slug>`), worktree (`~/apps/<app>-<seat>`), and Apple "
        "Notes name (`Antigravity`, `Cursor`, `Codex`, `Grok`, `Claude`, "
        "`Monet`, `DeepSeek Harness`, `MiniMax`, `Fx`, or the GB role in "
        "Title Case)."
    )

    text = text.replace(IDENTITY_TOKEN, universal_identity)
    text = text.replace(
        YOU_ARE_TOKEN,
        "You are **<YOUR_AGENT_TAG>**.  Keep `<seat>/` branches.",
    )
    text = text.replace(
        SEAT_LINE_TOKEN,
        "Seat: **<YOUR_AGENT_TAG>**.  Branch: `<seat>/<slug>`.",
    )
    text = text.replace(
        NEVER_PUSH_TOKEN,
        "Never open or push another seat's prefix from your session.  Only `<seat>/`.",
    )

    ordered_universal = [
        ("AGENT_SEAT=MONET", "AGENT_SEAT=<YOUR_SEAT>"),
        ("AGENT_TAG=MONET", "AGENT_TAG=<YOUR_TAG>"),
        ("SLACK_AGENT_NAME=MONET", "SLACK_AGENT_NAME=<YOUR_TAG>"),
        ("--by MONET", "--by <YOUR_TAG>"),
        ("--mine MONET", "--mine <YOUR_TAG>"),
        ("[MONET->", "[<YOUR_TAG>->"),
        ("[MONET]", "[<YOUR_TAG>]"),
        ("`[MONET`", "`[<YOUR_TAG>`"),
        ("`[MONET ", "`[<YOUR_TAG> "),
        ("`[MONET]", "`[<YOUR_TAG>]"),
        ("**MONET**", "**<YOUR_AGENT_TAG>**"),
        ("(MONET)", "(Universal)"),
        ("# Session start (MONET)", "# Session start (Universal)"),
        ("# Pick up a seat (MONET)", "# Pick up a seat (Universal)"),
        ("# Closeout (MONET)", "# Closeout (Universal)"),
        ("# Apple Notes (MONET)", "# Apple Notes (Universal)"),
        ("# THE BOARD (MONET)", "# THE BOARD (Universal)"),
        ("# Land a feature branch (MONET)", "# Land a feature branch (Universal)"),
        ("# Owner-facing copy (MONET)", "# Owner-facing copy (Universal)"),
        ("# Secret handoff (MONET)", "# Secret handoff (Universal)"),
        ("# Deploy verification (MONET)", "# Deploy verification (Universal)"),
        ("# iOS agent loop (MONET)", "# iOS agent loop (Universal)"),
        ("# Unstick a blocked PR (MONET)", "# Unstick a blocked PR (Universal)"),
        ("# Review-thread triage (MONET)", "# Review-thread triage (Universal)"),
        ("monet/<slug>", "<seat>/<slug>"),
        ("monet/fix", "<seat>/fix"),
        ("`monet/", "`<seat>/"),
        (" monet/", " <seat>/"),
        ("-b monet/", "-b <seat>/"),
        ("@ monet/", "@ <seat>/"),
        ("-monet-", "-<seat>-"),
        ("-monet`", "-<seat>`"),
        ("-monet ", "-<seat> "),
        ("-monet\n", "-<seat>\n"),
        ("-monet (", "-<seat> ("),
        ("Monet worktree", "seat worktree"),
        ("Monet session", "agent session"),
        ("every Monet", "every agent"),
        ("Start every Monet", "Start every agent"),
        ("Finish a Monet", "Finish an agent"),
        ("Land a Monet", "Land a"),
        ("whenever Monet", "whenever an agent"),
        ("Use whenever Monet", "Use whenever an agent"),
        ("Notes name `Monet`", "Notes name in Title Case (e.g. `Antigravity`, `Cursor`, `Codex`, `Grok`, `Claude`, `Monet`)"),
        ("then `Monet` (Title Case", "then `<Agent>` (Title Case"),
        ("then `Monet`", "then `<Agent>`"),
        ("[APP, Monet]", "[APP, Agent]"),
        ("[ST, CT, Monet]", "[ST, CT, Agent]"),
        ("(Monet):", "(<Seat>):"),
        ("Monet (not Claude)", "your seat (not another agent)"),
        ("Monet — never skip", "your seat — never skip"),
        ("Monet's job on these", "the agent's job on these"),
        ("parallel Monet lanes", "parallel agent lanes"),
        ("Acronyms first, then `Monet` (Title Case, not all-caps Slack tags).", "Acronyms first, then agent name in Title Case (e.g. `Antigravity`, `Cursor`, `Codex`, `Grok`, `Claude`, `Monet`, `DeepSeek`, `Fx`), not all-caps Slack tags."),
        ("Acronyms first, then `Monet`", "Acronyms first, then agent name in Title Case"),
        ("Monet/peer", "peer"),
        ("`FLEET`, `MONET`", "`FLEET`, `<YOUR_TAG>`"),
    ]

    for old, new in ordered_universal:
        text = text.replace(old, new)

    text = _unprotect(text)
    text = _rewrite_universal_voice(text)
    return fold_yaml_description(text)



def platform_installs() -> list[tuple[str, Seat]]:
    rows: list[tuple[str, Seat]] = []
    for seat in SEATS.values():
        if not seat.write_home:
            continue
        dest = os.path.expanduser(seat.dest)
        if dest.startswith("docs/"):
            continue
        rows.append((dest, seat))
    return rows


def catalog_seats() -> list[Seat]:
    """Seats that get a rendered copy under docs/fleet-skills/by-seat/."""
    return [
        s
        for key, s in SEATS.items()
        if key != "claude_shared"
    ]


def repo_platform_copies(repo_root: str) -> list[tuple[str, Seat]]:
    """Repo-tracked skill trees the installer must re-render."""
    return [
        (os.path.join(repo_root, ".claude", "skills"), SEATS["claude_shared"]),
        (os.path.join(repo_root, ".cursor", "skills"), SEATS["cursor"]),
        (os.path.join(repo_root, ".grok", "skills"), SEATS["grok"]),
    ]


def tool_home_exists(dest: str) -> bool:
    """True when the platform home (parent of …/skills) already exists."""
    expanded = os.path.expanduser(dest)
    parent = os.path.dirname(expanded.rstrip(os.sep))
    return os.path.isdir(parent)
