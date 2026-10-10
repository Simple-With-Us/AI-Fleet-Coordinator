#!/usr/bin/env python3
"""Specialize Monet-canonical fleet SKILL.md text for a destination seat.

docs/fleet-skills stays the Monet / Claude.app upload pack.  Every other
platform install must rewrite Zulip identity (the seat's bot, its zuliprc
path, and its `[SEAT·session8]` tag), Notes names, branch prefixes,
worktree suffixes, and reader-facing Claude/Monet voice, or that seat will
sign as Monet.  Skills that are meaningless on a harness are skipped, not
rewritten into a Claude-voiced copy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


IDENTITY_TOKEN = "@@SEAT_IDENTITY_PARAGRAPH@@"
YOU_ARE_TOKEN = "@@SEAT_YOU_ARE@@"
SEAT_LINE_TOKEN = "@@SEAT_BRANCH_LINE@@"
NEVER_PUSH_TOKEN = "@@SEAT_NEVER_PUSH@@"
SEAT_PIN_TOKEN = "@@SEAT_PIN_BLOCK@@"

# The canonical pack's identity block is the retired MONET seat's own block,
# exactly what seat_pin_block() renders for MONET (#405 made the canonical
# carry the renderer's own output).  Every render swaps the whole block for the
# destination seat's seat-precedence block (AGENT-SYNC § Identity Rules, owner
# 2026-10-09):  a launcher's AGENT_LAUNCH_SEAT, no seat at all when a launcher
# set none, then AGENT_SEAT, then the platform default.  The pattern also takes
# the two older canonical shapes (a bare `export AGENT_SEAT=MONET`, and the
# pin-or-fail line plus its never-overwrite sentence), so an older canonical is
# still swapped whole.

# The sentence every rendered identity block ends with.  The tests and
# check-seat-blocks.py look for it:  a pack without it has no launcher clause.
LAUNCHER_CLAUSE = (
    "Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`, and never overwrite an "
    "`AGENT_SEAT` you found already set."
)
_OLD_NEVER_OVERWRITE = (
    "Never overwrite an `AGENT_SEAT` that is already set:  a launcher such as "
    "BotFleet assigns its bots' seats."
)
_SEAT_PIN_SOURCE = re.compile(
    r"```bash\n(?:"
    r"export AGENT_SEAT=[^\n]+\n```(?:\n\n" + re.escape(_OLD_NEVER_OVERWRITE) + r")?"
    r"|if \[ -n \"\$\{AGENT_LAUNCH_SEAT:-\}\" \][^`]*?```\n\nYour seat is the first[^\n]*\n\nStop if [^\n]*?"
    + re.escape(LAUNCHER_CLAUSE)
    + r")"
)

# What a rendered command writes where the seat goes (`board ... --by`).  The
# identity block exports AGENT_SEAT from the verified seat when it is unset.
BY_SEAT = '"$AGENT_SEAT"'
# The placeholder for the reader's branch prefix in example claim posts.  A
# launched bot's prefix is its launcher's to name, so no render hard-codes one.
BRANCH_PREFIX = "<branch-prefix>"


def _seat_block(default_arm: str, ordinary: str, extra: str = "") -> str:
    """The seat-precedence code block plus the rules around it.

    `default_arm` is the shell word for an ordinary session (the platform
    default, or a `${...:?...}` that fails when there is none); `ordinary`
    says what an ordinary session is.  The block fails closed:  a launcher
    with no seat exits 3 before any default is read.
    """
    return (
        "```bash\n"
        'if [ -n "${AGENT_LAUNCH_SEAT:-}" ]; then SEAT="$AGENT_LAUNCH_SEAT"\n'
        'elif [ -n "${AGENT_LAUNCHER:-}" ]; then echo "no seat assigned by $AGENT_LAUNCHER" >&2; exit 3\n'
        f"else SEAT={default_arm}; fi\n"
        'export AGENT_SEAT="${AGENT_SEAT:-$SEAT}"\n'
        'agent-sync whoami --as "$SEAT"\n'
        "```\n\n"
        "Your seat is the first of these that applies (AGENT-SYNC § Identity Rules):  "
        "a seat Jay names to you in this conversation; a seat your launcher assigned "
        "(`AGENT_LAUNCH_SEAT` with `AGENT_LAUNCHER`, matching your launch prompt), "
        f"which beats this file whatever model you are; otherwise {ordinary}  "
        "If `AGENT_LAUNCHER` is set with no `AGENT_LAUNCH_SEAT`, or they disagree "
        "with your launch prompt, you have no seat:  do no fleet action, and say so."
        "\n\n"
        "Stop if `whoami` shows another seat's bot or the credential is missing "
        "(the CLI also refuses on its own).  Never use another seat's credential or "
        "Jay's account.  Your shell may not keep exports between commands, so pass "
        "`--as <SEAT>` on every agent-sync call, and read `\"$AGENT_SEAT\"` in the "
        f"commands below as the seat you verified.{extra}  {LAUNCHER_CLAUSE}"
    )


def _bot_address(seat: "Seat") -> str:
    local = seat.zulip_bot
    if local and not local.endswith("-bot"):
        local = f"{local}-bot"
    return f"{local}@" if local else ""


def seat_pin_block(seat: "Seat") -> str:
    """Identity block for one seat:  its platform default arm, never an overwrite."""
    prefix_line = (
        f"  `{BRANCH_PREFIX}` below is your seat's branch prefix:  `{seat.prefix}` "
        f"for {seat.tag}, or the one your launcher names."
    )
    if seat.retired:
        # A retired seat is never anyone's default:  the arm fails instead.
        return _seat_block(
            f'"${{AGENT_SEAT:?{seat.tag} is retired; take no work as {seat.tag}}}"',
            f"you have no seat here:  {seat.tag} is retired and is no platform's default.",
            prefix_line,
        )
    bot = _bot_address(seat)
    return _seat_block(
        f'"${{AGENT_SEAT:-{seat.tag}}}"',
        f"this is an ordinary {seat.notes} session, and your seat is **{seat.tag}**"
        + (f" (bot `{bot}`)." if bot else "."),
        prefix_line,
    )


def universal_pin_block() -> str:
    """Identity block for the neutral skills/ tree (no seat is known)."""
    return _seat_block(
        '"${AGENT_SEAT:?set AGENT_SEAT to your platform default from AGENT-SYNC Identity Rules}"',
        "take your platform's default from AGENT-SYNC § Identity Rules › Platform "
        "Defaults (a platform with no default asks Jay).",
        f"  `{BRANCH_PREFIX}` below is your seat's branch prefix.",
    )


def grok_bot_pin_block() -> str:
    """Identity block for a Grok Bot role.

    Grok Bot is the launcher for its roles, so there is no platform default:
    the role comes from the launcher, else from AGENT_SEAT or AGENT_TAG, and
    the shell fails loudly when none is set.
    """
    return _seat_block(
        f'"${{AGENT_SEAT:-${{AGENT_TAG:?set {gb_role_pin_list()}}}}}"',
        "your seat is the `GB-<NAME>` role Grok Bot gave you; there is no platform default.",
        f"  `{BRANCH_PREFIX}` below is `cursor` for a Cursor cloud agent.",
    )


@dataclass(frozen=True)
class Seat:
    tag: str
    notes: str
    prefix: str
    suffix: str
    dest: str
    mode: str  # exclusive | grok_bot
    identity_paragraph: str
    extra_banner: str = ""
    write_home: bool = True
    seat_key: str = ""
    # Zulip identity (owner 2026-10-07).  `zulip_bot` is the
    # bot's email local part; the realm is simplewithus.zulipchat.com for all of
    # them.  `zulip_rc` is the credential file code under ~/.secrets/Zulip/.
    # Both are fixed per bot in the guide's roster — take them from there, never
    # derive one from the other or from the display name.
    zulip_bot: str = ""
    zulip_rc: str = ""
    # A retired seat keeps a catalog copy (history, upload packs) that is inert:
    # no install banner, no start-a-session description, no working procedure.
    retired: bool = False


def zulip_credential_path(seat: Seat) -> str:
    """The seat's own zuliprc path.  Never another seat's file."""
    return f"~/.secrets/Zulip/{seat.zulip_rc or seat.tag}-zuliprc"


def zulip_identity_sentence(seat: Seat) -> str:
    """One sentence naming the bot and its credential file.

    `zulip_bot` holds the local part WITHOUT the `-bot` suffix, because Zulip
    appends it itself (owner-confirmed Wed, Oct 7): entering `mm-bot` would
    produce mm-bot-bot@.  Build the address once, here.
    """
    if not seat.zulip_bot:
        return ""
    local = seat.zulip_bot
    if not local.endswith("-bot"):
        local = f"{local}-bot"
    return (
        f"Zulip bot `{local}@simplewithus.zulipchat.com`, credential file "
        f"`{zulip_credential_path(seat)}` (mode 600).  Session tag "
        f"`[{seat.tag}·session8]`, and the `agent-sync` CLI writes it for you."
    )


def _banner(tag: str, notes: str, prefix: str, suffix: str, zulip: str = "") -> str:
    inherit = "a shared template"
    if tag == "MONET":
        inherit = "another seat's upload pack"
    return (
        f"> **This install is for `{tag}`.**  Chat tag `[{tag}·session8]`.  "
        f"Notes `{notes}`.  "
        f"Branches `{prefix}/`.  Lanes `~/apps/lanes/<Repo>/{suffix}-<slug>`.  Do not inherit "
        f"another seat's tag from {inherit}."
        f"{zulip}\n\n"
    )


# Coordinator / ops self-id for this repo (Simple-With-Us/AI-Fleet-Coordinator).
# FLEET is a recipient word for the fleet-wide wake in Zulip (`@**all**` in
# #agent-sync topic `fleet`), not this system's name.
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
    """True when a post header carries the fleet-wide wake, not a sender name.

    Zulip writes the fleet wake as `@**all**` (owner 2026-10-09; there is no
    `fleet` user group).  `@*fleet*` and `->FLEET` are retired header forms,
    still accepted because older docs and posts quote them.
    """
    return "@**all**" in head or "@*fleet*" in head or "->FLEET" in head or "→FLEET" in head


GB_ROLE_LIST = _gb_role_list()

GROK_BOT_BANNER = (
    "> **This install is for Grok Bot roles.** Chat tag is `[GB-<NAME>]` — "
    f"{GB_ROLE_LIST}.  Notes name is the role in Title Case (`Conductor`, "
    "`Monitor`, …).  Cloud branches are often `cursor/`.  Never `[GROK-BOT]`, "
    "`[CURSOR]`, `[GROK]`, or `[MONET]`.\n\n"
)

GROK_BOT_IDENTITY = (
    "This pack is for **Grok Bot** roles driving Cursor cloud agents.  "
    f"Chat tag is `[GB-<NAME>]` — one of {GB_ROLE_LIST}.  "
    "Not `[GROK-BOT]`, not `[CURSOR]`, not `[GROK]`, not `[MONET]`.  "
    "Notes name is the role in Title Case.  Cloud branches are often "
    "`cursor/<slug>`.  Pin `AGENT_TAG` to your GB role before posting or "
    "`board --by`.  Local Cursor IDE on the Mac is `[CURSOR]`.  Mac Grok TUI "
    "is `[GROK]`."
)

CURSOR_EXTRA = (
    "> **Runtime fork (Cursor).** Local Cursor IDE / Auto on this Mac is "
    "`[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, "
    "your chat tag is `[GB-<NAME>]` "
    f"({', '.join(GB_ROLE_TAGS)}) — not `[GROK-BOT]`, not `[CURSOR]`, and not "
    "`[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you "
    "are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.\n\n"
)

GROK_EXTRA = (
    "> **Runtime fork (Grok).** Mac Grok TUI / CLI is `[GROK]`, and so is Grok "
    "Build:  one seat (owner 2026-10-08), so never sign `GROK-BUILD`, a retired "
    "alias the CLI refuses.  Old `grok-build/` branches stay readable.  Grok Bot "
    "(Cursor cloud) uses `[GB-<NAME>]` role tags, not this pack and not "
    "`[GROK-BOT]`.  Never `[MONET]`.\n\n"
)

# CLAUDE is the only Claude seat (owner 2026-10-07), so the shared
# `~/.claude/skills` home renders as CLAUDE.  Other tools scan this directory
# (fx does), which is the one thing the banner has to say.
CLAUDE_SHARED_BANNER = (
    "> **Shared `~/.claude/skills`.**  This directory is the `CLAUDE` seat's "
    "skill home, and other tools scan it too.  Other seats (Codex, Cursor, "
    "Grok, Grok-Web, AG, Clutch, FX, MM, MC, MA) must take identity from their "
    "own pack, never from here.\n\n"
)

CLAUDE_IDENTITY = (
    "This pack is for **CLAUDE** (the Claude account, the only Claude seat since "
    "owner 2026-10-07).  Session tag `[CLAUDE·session8]`.  Notes name `Claude`.  "
    "Branches `claude/<slug>` only.  Lanes `~/apps/lanes/<Repo>/claude-<slug>`.  "
    "MONET and RENOIR are retired.  Never sign as Monet.  Pin `AGENT_SEAT=CLAUDE`."
)

MONET_IDENTITY = (
    "This pack is for the retired **MONET** Claude account (owner 2026-10-07: the "
    "account and app are no longer used).  Historical session tag "
    "`[MONET·session8]`, with no Zulip bot.  Notes name `Monet`.  Branches "
    "`monet/<slug>` stay readable.  `CLAUDE` is the only Claude seat.  Do not take "
    "new work as MONET."
)

MONET_RETIRED_BANNER = (
    "> **Retired seat.**  Owner directive 2026-10-07: the Monet Claude account and "
    "app are no longer used, and `CLAUDE` is the only Claude seat.  Do not take "
    "work as MONET, do not leave MONET In Progress, and do not install this pack "
    "anywhere.  This catalog copy is inactive.\n\n"
)

DSH_IDENTITY = (
    "This pack is for the retired **DSH** (DeepSeek Harness) seat.  Use **CLUTCH** "
    "for `Simple-With-Us/Clutch` (DSH plus MiniMax, formerly Harness).  Notes name "
    "`DeepSeek Harness`.  Branches `deepseek/<slug>` stay readable.  A DeepSeek "
    "*model* inside Cursor is `[CURSOR]`.  Former tag `DEEPSEEK` is retired.  DSH "
    "has no Zulip bot.  Do not take new work as DSH."
)

DSH_RETIRED_BANNER = (
    "> **Retired seat.**  DSH (DeepSeek Harness) is retired (2026-09-19): use "
    "`CLUTCH` for `Simple-With-Us/Clutch`.  Do not take work as DSH and do not "
    "install this pack to `~/.deepseek/skills`.  This catalog copy is inactive.\n\n"
)

CLUTCH_IDENTITY = (
    "This pack is for **CLUTCH** (the Clutch seat, owner of `Simple-With-Us/Clutch`: "
    "the DSH and MiniMax drivers, ACP bridges, and cordis profiles).  Session tag "
    "`[CLUTCH·session8]`.  Notes name `Clutch`.  Branches `clutch/<slug>` only.  "
    "Lanes `~/apps/lanes/Clutch/clutch-<slug>`.  One seat for every model run "
    "through Clutch (owner 2026-10-07: no per-model split).  Replaces HARNESS and "
    "DSH.  Never sign as Monet.  Default seat `CLUTCH` unless Jay names another or a launcher "
    "set `AGENT_LAUNCH_SEAT`;  never overwrite an `AGENT_SEAT` that is already set."
)

CLUTCH_EXTRA = (
    "> **Runtime (Clutch).**  Clutch runs the DSH engine (`clutch`, pm2 `clutch-web`, the "
    "Clutch Mac app).  The engine finds skills in `~/.clutch/dsh/skills` before the shared "
    "`~/.agents/skills` pack it also reads, so this pack shadows that one.  It loads "
    "`~/.clutch/dsh/AGENTS.md` and then the project `AGENTS.md` chain, and reads neither "
    "`~/AGENTS.md` nor `~/.claude/CLAUDE.md`.  Start `clutch` inside a lane "
    "(`~/apps/lanes/Clutch/clutch-<slug>`).  A seat a launcher assigned (BotFleet, a bridge) "
    "beats the CLUTCH default.\n\n"
)

KIMI_IDENTITY = (
    "This pack is for **KIMI**.  Tag `[KIMI]`.  Notes name `Kimi`.  "
    "**KIMI is retired / unavailable long-term (owner 2026-08-21).** Do not "
    "start a Kimi session.  Do not take new work, do not leave Kimi In Progress, "
    "and do not reserve future lanes for Kimi.  If you are reading this after a "
    "mistaken spawn, say so in your own chat with the owner and stop.  Never sign as Monet."
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

RENOIR_RETIRED_BANNER = (
    "> **Retired seat.**  Owner directive 2026-10-07: the seat never opened, and "
    "the Renoir Claude account and app are no longer used.  `CLAUDE` is the only "
    "Claude seat.  Do not install to `~/.renoir/skills`.  Do not take fleet work "
    "as RENOIR.  This catalog copy is inactive.\n\n"
)

RENOIR_IDENTITY = (
    "This pack is for the retired **RENOIR** seat (the seat never opened; owner "
    "2026-10-07).  Notes name `Renoir`.  Branches `renoir/<slug>` stay readable.  "
    "Renoir is not Monet and not Claude.  `CLAUDE` is the only Claude seat.  Do not "
    "take fleet work as RENOIR."
)

MUSE_CODE_EXTRA = (
    "> **Runtime (Muse Code).** Muse Code (`muse` CLI) is the interactive "
    "terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches "
    "`muse-code/`.  New lanes are made with `~/apps/lane new <app> <slug>` at "
    "`~/apps/lanes/<Repo>/muse-code-<slug>`, and `AGENT_SEAT=MC` comes from the "
    "`muse-seat` wrapper, never from a guess.  Start `muse` inside a lane; `muse -w` "
    "appears to make a worktree inside `~/Code/<App>/.muse/worktrees`, so do not use it.  "
    "Distinct from **Muse Assist** "
    "(`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative "
    "assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load "
    "automatically when the workspace is trusted in `~/.config/muse/trust.json`, and the "
    "user-level `~/.claude/CLAUDE.md` loads as a fallback.  "
    "Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.  "
    "Setup checklist: `docs/MUSE-ONBOARDING.md`.\n\n"
)

MUSE_ASSIST_BANNER = (
    "> **Cloud VM batch agent.** Muse Assist (`[MA]`, former tag `[MUSE]`) is the "
    "Meta Muse cloud VM batch compute and creative assistant dispatched via Mac/iOS apps.  "
    "Unmetered VM compute for multi-day heavy jobs (transcoding, large migrations).  "
    "Distinct from **Muse Code** (`[MC]`, branches `muse-code/`).  "
    "This catalog copy is reference-only — do not install to `~/.muse`.\n\n"
)

SEATS: dict[str, Seat] = {
    "cursor": Seat(
        "CURSOR", "Cursor", "cursor", "cursor",
        "~/.cursor/skills", "exclusive",
        "This pack is for the **CURSOR** seat (Cursor IDE and Auto on this Mac).  "
        "Zulip session tag `[CURSOR·session8]`.  Notes name `Cursor`.  Branches "
        "`cursor/<slug>` only.  Lanes `~/apps/lanes/<Repo>/cursor-<slug>`.  Never post as `[MONET]`, "
        "`[CLAUDE]`, or `[GROK]`.  A skill copied from the Monet pack is not your "
        "name — this install is.  Pin `AGENT_SEAT=CURSOR`.  Incident: 2026-08-23 "
        "Cursor inherited Monet identity from an unspecialized skill copy.",
        extra_banner=CURSOR_EXTRA,
        seat_key="cursor",
        zulip_bot="cursor-bot",
        zulip_rc="Cursor",
    ),
    "ag": Seat(
        "AG", "Antigravity", "ag", "antigravity",
        "~/.gemini/skills", "exclusive",
        "This pack is for **AG** (Antigravity / Gemini).  Session tag `[AG·session8]`.  Notes name "
        "`Antigravity`.  Branches `ag/<slug>` (keep `agent/antigravity` only if the lane "
        "already uses it).  Lanes `~/apps/lanes/<Repo>/antigravity-<slug>`.  Never sign "
        "as Monet, Cursor, or Claude.  Pin `AGENT_SEAT=AG`.",
        seat_key="ag",
        zulip_bot="ag-bot",
        zulip_rc="AG",
    ),
    "codex": Seat(
        "CODEX", "Codex", "codex", "codex",
        "~/.codex/skills", "exclusive",
        "This pack is for **CODEX**.  Session tag `[CODEX·session8]`.  Notes name `Codex`.  "
        "Branches `codex/<slug>` only.  Lanes `~/apps/lanes/<Repo>/codex-<slug>`.  "
        "Never sign as Monet.  Pin `AGENT_SEAT=CODEX`.",
        seat_key="codex",
        zulip_bot="codex-bot",
        zulip_rc="Codex",
    ),
    "grok": Seat(
        "GROK", "Grok", "grok", "grok",
        "~/.grok/skills", "exclusive",
        "This pack is for the **GROK** Mac TUI / CLI seat.  Session tag `[GROK·session8]`.  "
        "Notes name `Grok`.  Branches `grok/<slug>` only.  Lanes "
        "`~/apps/lanes/<Repo>/grok-<slug>`.  Never sign as Monet or Grok Bot.  Pin "
        "`AGENT_SEAT=GROK`.",
        extra_banner=GROK_EXTRA,
        seat_key="grok",
        zulip_bot="grok-build-bot",
        zulip_rc="Grok-Build",
    ),
    "grok-build": Seat(
        "GROK-BUILD", "Grok Build", "grok-build", "grok-build",
        "~/.grok-build/skills", "exclusive",
        "This pack is for **GROK-BUILD** (Grok Build TUI / App Builder).  Session "
        "tag `[GROK-BUILD·session8]`.  Notes name `Grok Build`.  Branches `grok-build/<slug>` "
        "only.  Lanes `~/apps/lanes/<Repo>/grok-build-<slug>`.  Do not use `grok/` or "
        "sign as GROK or a Grok Bot `[GB-<NAME>]` role.  Pin "
        "`AGENT_SEAT=GROK-BUILD`.",
        seat_key="grok-build",
        zulip_bot="grok-build-bot",
        zulip_rc="Grok-Build",
    ),
    "fx": Seat(
        "FX", "Fx", "fx", "fx",
        "~/.fx/skills", "exclusive",
        "This pack is for the **FX** terminal agent (`fx` / `fx.sh`).  Session tag "
        "`[FX·session8]`.  Notes name `Fx`.  Branches `fx/<slug>` only.  Lanes "
        "`~/apps/lanes/<Repo>/fx-<slug>`.  This is not Cursor, not Codex, and not Monet.  "
        "Pin `AGENT_SEAT=FX`.",
        extra_banner=(
            "> **Runtime (fx).** Local Cursor IDE remains `[CURSOR]`.  Codex CLI "
            "remains `[CODEX]`.  Do not inherit those tags from a shared skill "
            "directory fx also scans (`~/.claude/skills`, `~/.codex/skills`).  "
            "Prefer `~/.fx/skills` for this seat.\n\n"
        ),
        seat_key="fx",
        zulip_bot="fx-bot",
        zulip_rc="FX",
    ),
    "grok-bot": Seat(
        "GB-<NAME>", "Grok Bot", "cursor", "cursor",
        "docs/fleet-skills/by-seat/grok-bot", "grok_bot",
        GROK_BOT_IDENTITY,
        extra_banner=GROK_BOT_BANNER,
        write_home=False,
        seat_key="grok-bot",
        zulip_bot="",
        zulip_rc="",
    ),
    "claude": Seat(
        "CLAUDE", "Claude", "claude", "claude",
        "docs/fleet-skills/by-seat/claude", "exclusive",
        CLAUDE_IDENTITY,
        write_home=False,
        seat_key="claude",
        zulip_bot="claude-bot",
        zulip_rc="Claude",
    ),
    "monet": Seat(
        "MONET", "Monet", "monet", "monet",
        "~/Desktop/fleet-skills", "exclusive",
        MONET_IDENTITY,
        extra_banner=MONET_RETIRED_BANNER,
        write_home=False,
        retired=True,
        seat_key="monet",
        zulip_bot="",
        zulip_rc="",
    ),
    "renoir": Seat(
        "RENOIR", "Renoir", "renoir", "renoir",
        "~/.renoir/skills", "exclusive",
        RENOIR_IDENTITY,
        extra_banner=RENOIR_RETIRED_BANNER,
        write_home=False,
        retired=True,
        seat_key="renoir",
        zulip_bot="",
        zulip_rc="",
    ),
    "deepseek": Seat(
        "DSH", "DeepSeek Harness", "deepseek", "deepseek",
        "~/.deepseek/skills", "exclusive",
        DSH_IDENTITY,
        extra_banner=DSH_RETIRED_BANNER,
        write_home=False,
        retired=True,
        seat_key="deepseek",
        zulip_bot="",
        zulip_rc="",
    ),
    "clutch": Seat(
        "CLUTCH", "Clutch", "clutch", "clutch",
        "~/.clutch/dsh/skills", "exclusive",
        CLUTCH_IDENTITY,
        extra_banner=CLUTCH_EXTRA,
        seat_key="clutch",
        zulip_bot="clutch-bot",
        zulip_rc="Clutch",
    ),
    "minimax": Seat(
        "MM", "MiniMax", "minimax", "minimax",
        "~/.minimax/skills", "exclusive",
        "This pack is for **MM** (MiniMax Code on the Mavis local runtime).  "
        "Session tag `[MM·session8]`.  Notes name `MiniMax`.  Branches `minimax/<slug>` only.  "
        "Lanes `~/apps/lanes/<Repo>/minimax-<slug>`.  Running a MiniMax *model* inside "
        "another harness does not make you this seat.  Built-in Mavis sub-agents "
        "(`explore`, `worker`, `verifier`) inherit `MM` — they do not get "
        "their own Zulip bot.  Former tag `MINIMAX` is retired.  "
        "Pin `AGENT_SEAT=MM`.",
        extra_banner=MINIMAX_EXTRA,
        seat_key="minimax",
        zulip_bot="mm-bot",
        zulip_rc="MM",
    ),
    "kimi": Seat(
        "KIMI", "Kimi", "kimi", "kimi",
        "~/.kimi/skills", "exclusive",
        KIMI_IDENTITY,
        extra_banner=KIMI_RETIRED_BANNER,
        write_home=False,
        retired=True,
        seat_key="kimi",
        zulip_bot="",
        zulip_rc="",
    ),
    "muse-code": Seat(
        "MC", "Muse Code", "muse-code", "muse-code",
        "~/.config/muse/skills", "exclusive",
        "This pack is for **MC** (Muse Code interactive terminal coding agent).  "
        "Session tag `[MC·session8]`.  Notes name `Muse Code`.  Branches `muse-code/<slug>` only.  "
        "Lanes `~/apps/lanes/<Repo>/muse-code-<slug>` (make one with "
        "`~/apps/lane new <app> <slug>`).  Distinct from Muse Assist "
        "(`[MA]`, branches `muse-assist/`).  Never sign as Monet, Claude, or Codex.  "
        "Pin `AGENT_SEAT=MC` / `AGENT_TAG=MC`.",
        extra_banner=MUSE_CODE_EXTRA,
        seat_key="muse-code",
        zulip_bot="mc-bot",
        zulip_rc="MC",
    ),
    "muse-assist": Seat(
        "MA", "Muse Assist", "muse-assist", "muse-assist",
        "docs/fleet-skills/by-seat/muse-assist", "exclusive",
        "This pack is for **MA** (Muse Assist cloud VM batch compute & creative agent).  "
        "Session tag `[MA·session8]`.  Notes name `Muse Assist`.  Branches `muse-assist/<slug>` "
        "(historical `muse/<slug>`).  No lane on the Mac (cloud VM).  "
        "Former tag `MUSE` is migrated to `MA` (owner 2026-10-04) to cleanly "
        "distinguish from Muse Code (`[MC]`).  Pin `AGENT_SEAT=MA` / `AGENT_TAG=MA`.",
        extra_banner=MUSE_ASSIST_BANNER,
        write_home=False,
        seat_key="muse-assist",
        zulip_bot="muse-assist-bot",
        zulip_rc="MA",
    ),
    # The shared Claude Code skill home.  CLAUDE is the only Claude seat, so it
    # renders as CLAUDE.  It is a second home for the `claude` pack, not a
    # separate seat, so it gets no by-seat catalog copy (see catalog_seats).
    "claude_shared": Seat(
        "CLAUDE", "Claude", "claude", "claude",
        "~/.claude/skills", "exclusive",
        CLAUDE_IDENTITY,
        extra_banner=CLAUDE_SHARED_BANNER,
        seat_key="claude_shared",
        zulip_bot="claude-bot",
        zulip_rc="Claude",
    ),
}

MONET_PACK_LINE = (
    "This pack is for the **MONET** Claude account.  Session tag "
    "`[MONET·session8]`.  Notes name `Monet`.  Branches `monet/<slug>` only."
)

# The universal (root skills/) rendering of the same line.  `specialize_universal`
# swaps this for the neutral seat-agnostic wording, so both forms must stay in
# sync with the Zulip contract: a seat tag is the `[SEAT·session8]` session tag.
MONET_PACK_LINE_UNIVERSAL = (
    "This pack is for the **<YOUR_AGENT_TAG>** Claude account.  Session tag "
    "`<YOUR_TAG>·session8`.  Notes name in Title Case (e.g. `Antigravity`, "
    "`Cursor`, `Codex`, `Grok`, `Claude`, `Monet`).  Branches "
    "`<seat>/<slug>` only."
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
        "clutch",
        "kimi",
        "minimax",
        "muse-code",
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
    return seat.tag in CLAUDE_FAMILY_TAGS


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
        ("--by MONET", f"--by {BY_SEAT}"),
        ("--mine MONET", f"--mine {BY_SEAT}"),
        ("claim:  monet/<slug>", f"claim:  {BRANCH_PREFIX}/<slug>"),
        ("`--by` for this seat is `MONET`", "`--by` for this seat is `$AGENT_TAG`"),
        ("[MONET·session8", "[$AGENT_TAG·session8"),
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
            "Acronyms first, then `Monet` (Title Case, not all-caps seat tags).",
            "Acronyms first, then the GB role in Title Case (not `[GROK-BOT]`).",
        ),
        ("Acronyms first, then `Monet`", "Acronyms first, then the GB role in Title Case"),
        ("Monet/peer", "Grok Bot/peer"),
        ("`FLEET`, `MONET`", "`FLEET`, `$AGENT_TAG`"),
    ]
    for old, new in ordered:
        text = text.replace(old, new)
    text = text.replace(SEAT_PIN_TOKEN, grok_bot_pin_block())
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
    text = _SEAT_PIN_SOURCE.sub(SEAT_PIN_TOKEN, text)
    text = text.replace(
        MONET_PACK_LINE + "\n\n" + MONET_CLAUDE_SHARED_PARA,
        IDENTITY_TOKEN,
    )
    text = text.replace(MONET_PACK_LINE_UNIVERSAL, IDENTITY_TOKEN)
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
    text = text.replace(SEAT_PIN_TOKEN, seat_pin_block(seat))
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


_PIN_PROSE = re.compile(r"Pin `AGENT_SEAT=([A-Z0-9-]+)`(?: / `AGENT_TAG=\1`)?\.")


def _launcher_aware_prose(text: str) -> str:
    """Seat paragraphs said "Pin `AGENT_SEAT=X`."  Under the owner's seat-precedence
    rule X is only the ordinary session's default, so say that instead."""
    return _PIN_PROSE.sub(
        lambda m: f"`{m.group(1)}` is the default seat of an ordinary session; a launcher's seat wins (Identity).",
        text,
    )


def specialize_from_monet(text: str, seat: Seat, skill_name: str = "") -> str:
    if seat.mode == "grok_bot":
        return _specialize_grok_bot(text, seat, skill_name)

    text = _stash_identity_source(text)
    text = _protect(text)

    ordered = [
        ("AGENT_SEAT=MONET", f"AGENT_SEAT={seat.tag}"),
        ("AGENT_TAG=MONET", f"AGENT_TAG={seat.tag}"),
        ("--by MONET", f"--by {BY_SEAT}"),
        ("--mine MONET", f"--mine {BY_SEAT}"),
        ("`--by` for this seat is `MONET`",
         f"`--by` for this seat is your verified seat (`{seat.tag}` in an ordinary {seat.notes} session)"),
        ("claim:  monet/<slug>", f"claim:  {BRANCH_PREFIX}/<slug>"),
        ("[MONET·session8", f"[{seat.tag}·session8"),
        ("[MONET->", f"[{seat.tag}->"),
        ("[MONET]", f"[{seat.tag}]"),
        ("`[MONET`", f"`[{seat.tag}`"),
        ("`[MONET ", f"`[{seat.tag} "),
        ("`[MONET]", f"`[{seat.tag}]"),
        ("Session tag `[MONET·session8]`", f"Session tag `[{seat.tag}·session8]`"),
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
        ("Acronyms first, then `Monet`", f"Acronyms first, then `{seat.notes}`"),
        ("Monet/peer", f"{seat.notes}/peer"),
        ("`FLEET`, `MONET`", f"`FLEET`, `{seat.tag}`"),
    ]
    for old, new in ordered:
        text = text.replace(old, new)

    text = _unstash_identity(text, seat)
    text = _unprotect(text)
    text = _rewrite_reader_voice(text, seat)
    text = _launcher_aware_prose(text)

    banners = ""
    if skill_name in IDENTITY_SKILL_NAMES and not seat.retired:
        zulip = zulip_identity_sentence(seat)
        banners += _banner(seat.tag, seat.notes, seat.prefix, seat.suffix, f"  {zulip}" if zulip else "")
    banners += seat.extra_banner
    if banners:
        text = _insert_after_first_heading(text, banners)
    if seat.retired:
        text = _apply_retired(text, seat, skill_name)
    return fold_yaml_description(text)


def retired_description(seat: Seat) -> str:
    """The session-start description for a retired seat's catalog copy.

    No quotes, backticks, "#" or ": " so it stays a valid plain YAML scalar
    after fold_yaml_description.
    """
    return (
        f"{seat.tag} is retired.  Do not start a {seat.notes} session.  Do not "
        "take work.  If you are reading this after a mistaken spawn, say so in "
        "your own chat with the owner and stop."
    )


def set_yaml_description(text: str, value: str) -> str:
    """Replace the whole front-matter `description:` value (inline or folded).

    Matching the old sentence is brittle: when the canonical wording changes
    the match silently stops firing and the retired text leaks the live
    start-a-session description.  Replacing the key cannot miss.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    lines = text[4:end].split("\n")
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("description:") and not replaced:
            i += 1
            while i < len(lines) and (
                lines[i].startswith(" ") or lines[i].startswith("\t")
            ):
                i += 1
            out.append("description: >-")
            out.append("  " + value)
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        out.append("description: >-")
        out.append("  " + value)
    return "---\n" + "\n".join(out) + text[end:]


# Retired seats whose session-start copy is cut down to a Stop section.  Kimi
# was already cut on main.  MONET, RENOIR and DSH keep the full body for now,
# because the peer-screen lane is still editing lines inside it; cutting them
# is a follow-up once that lands.
STOP_ONLY_RETIRED = frozenset({"kimi"})


def _apply_retired(text: str, seat: Seat, skill_name: str) -> str:
    """Make a retired seat's session-start say it is retired, not "start a session"."""
    if skill_name != "session-start":
        return text
    text = set_yaml_description(text, retired_description(seat))
    if seat.seat_key not in STOP_ONLY_RETIRED:
        return text
    marker = "## 1. Identity"
    idx = text.find(marker)
    if idx >= 0:
        text = (
            text[:idx]
            + "## Stop\n\n"
            + f"{seat.tag} is retired.  Do not start a {seat.notes} session.  Do not take "
            "work.  If you are reading this after a mistaken spawn, say so in "
            "your own chat with the owner and stop.  "
            + f"Do not export `AGENT_SEAT={seat.tag}` to take work.  Do not claim work "
            f"or pick a {seat.notes} lane.  Coordinator self-id is `{COORDINATOR_SELF_ID}`.  "
            "A fleet-wide wake is `@**all**` in #agent-sync topic `fleet`.\n"
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
        "**FX**, or a Grok Bot `[GB-<NAME>]` role), use your own Zulip session "
        "tag (e.g. `[AG·session8]`, `[CURSOR·session8]`, `[GB-CONDUCTOR]`, "
        "`[DSH·session8]`, `[MM·session8]`), your seat's own bot and "
        "`~/.secrets/Zulip/<file code>-zuliprc`, branch "
        "prefix (`<seat>/<slug>`), lane (`~/apps/lanes/<Repo>/<seat>-<slug>`), and Apple "
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
        ("--by MONET", f"--by {BY_SEAT}"),
        ("--mine MONET", f"--mine {BY_SEAT}"),
        ("claim:  monet/<slug>", f"claim:  {BRANCH_PREFIX}/<slug>"),
        ("`--by` for this seat is `MONET`", "`--by` for this seat is `<YOUR_TAG>`"),
        ("[MONET·session8", "[<YOUR_TAG>·session8"),
        ("[MONET->", "[<YOUR_TAG>->"),
        ("[MONET]", "[<YOUR_TAG>]"),
        ("`[MONET`", "`[<YOUR_TAG>`"),
        ("`[MONET ", "`[<YOUR_TAG> "),
        ("`[MONET]", "`[<YOUR_TAG>]"),
        ("Tag `<YOUR_TAG>`", "Session tag `<YOUR_TAG>·session8`"),
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
        ("Acronyms first, then `Monet` (Title Case, not all-caps seat tags).", "Acronyms first, then agent name in Title Case (e.g. `Antigravity`, `Cursor`, `Codex`, `Grok`, `Claude`, `Monet`, `DeepSeek`, `Fx`), not all-caps seat tags."),
        ("Acronyms first, then `Monet`", "Acronyms first, then agent name in Title Case"),
        ("Monet/peer", "peer"),
        ("`FLEET`, `MONET`", "`FLEET`, `<YOUR_TAG>`"),
    ]

    for old, new in ordered_universal:
        text = text.replace(old, new)
    text = text.replace(SEAT_PIN_TOKEN, universal_pin_block())

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
    """Seats that get a rendered copy under docs/fleet-skills/by-seat/.

    `claude_shared` is the `claude` pack's second home, not a seat of its own.
    """
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
