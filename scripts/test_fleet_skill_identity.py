#!/usr/bin/env python3
"""Tests for per-seat fleet skill identity specialization."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from fleet_skill_identity import (
    _SEAT_PIN_SOURCE,
    BRANCH_EXAMPLE_CANONICAL,
    BRANCH_EXAMPLE_SLUG,
    BRANCH_PREFIX,
    COORDINATOR_SELF_ID,
    FORBIDDEN_LOCAL_IOS_SHIP,
    GB_ROLE_TAGS,
    LAUNCHER_CLAUSE,
    NEVER_INSTALL,
    SEATS,
    catalog_seats,
    catalog_skill_names,
    head_has_fleet_wake,
    is_grok_bot_tag,
    platform_installs,
    repo_platform_copies,
    seat_pin_block,
    set_yaml_description,
    skill_allowed_for_seat,
    specialize_from_monet,
    specialize_universal,
    zulip_identity_sentence,
)

SESSION = os.path.join(ROOT, "docs", "fleet-skills", "session-start", "SKILL.md")
GAP = os.path.join(ROOT, "docs", "fleet-skills", "sentence-gap", "SKILL.md")
NOTES = os.path.join(ROOT, "docs", "fleet-skills", "apple-notes", "SKILL.md")
COORD = os.path.join(ROOT, "docs", "fleet-skills", "fleet-coordination", "SKILL.md")


def _session() -> str:
    with open(SESSION, encoding="utf-8") as f:
        return f.read()


def _notes() -> str:
    with open(NOTES, encoding="utf-8") as f:
        return f.read()


def _protocol_doc_path() -> Path:
    """The protocol document is being renamed:  the mirror holds it under exactly one of the two names.
    Remove the old name when the rename is complete."""
    for name in ("FLEET-PROTOCOL.md",):
        if Path(ROOT, name).is_file():
            return Path(ROOT, name)
    return Path(ROOT, "FLEET-PROTOCOL.md")


def _coord() -> str:
    with open(COORD, encoding="utf-8") as f:
        return f.read()


def _arm(tag: str) -> str:
    """The rendered default arm of the seat-precedence block for an ordinary session."""
    return f'else SEAT="${{AGENT_SEAT:-{tag}}}"; fi'


def _ordinary(notes: str, tag: str) -> str:
    return f"this is an ordinary {notes} session, and your seat is **{tag}**"


class SpecializeTests(unittest.TestCase):
    def test_cursor_is_not_monet(self) -> None:
        out = specialize_from_monet(_session(), SEATS["cursor"], skill_name="session-start")
        self.assertIn(_arm("CURSOR"), out)
        self.assertIn(_ordinary("Cursor", "CURSOR"), out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertIn("[CURSOR]", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertIn("cursor/<slug>", out)
        self.assertIn("GB-<NAME>", out)
        self.assertIn("not `[GROK-BOT]`", out)
        self.assertNotIn("two different Claude accounts", out)
        self.assertIn("This install is for `CURSOR`", out)

    def test_branch_worked_example_matches_seat_prefix(self) -> None:
        peer_literal = "claude/afc-123-zulip-cutover"
        self.assertEqual(BRANCH_EXAMPLE_CANONICAL, f"monet/{BRANCH_EXAMPLE_SLUG}")
        skills = ("session-start", "board-ops", "fleet-coordination")
        for skill_name in skills:
            base = _load_skill(skill_name)
            for key, seat in SEATS.items():
                if key == "claude_shared" or seat.retired:
                    continue
                out = specialize_from_monet(base, seat, skill_name=skill_name)
                if seat.mode == "grok_bot":
                    want = f"{BRANCH_PREFIX}/{BRANCH_EXAMPLE_SLUG}"
                else:
                    want = f"{seat.prefix}/{BRANCH_EXAMPLE_SLUG}"
                self.assertIn(want, out, f"{key} {skill_name}")
                if seat.mode != "grok_bot" and seat.prefix != "claude":
                    self.assertNotIn(peer_literal, out, f"{key} {skill_name}")

    def test_ag_suffix_is_antigravity(self) -> None:
        out = specialize_from_monet(_session(), SEATS["ag"], skill_name="session-start")
        self.assertIn(_arm("AG"), out)
        self.assertIn(_ordinary("Antigravity", "AG"), out)
        self.assertIn("ag/<slug>", out)
        self.assertIn("trading-antigravity", out)
        self.assertNotIn("trading-monet", out)

    def test_ag_notes_name_is_antigravity(self) -> None:
        # The canonical apple-notes text is seat-neutral (`[TEAM_KEY, Agent]`), so the
        # seat's Notes name comes from the install banner, never from Monet.
        out = specialize_from_monet(_notes(), SEATS["ag"], skill_name="apple-notes")
        self.assertIn("Notes `Antigravity`", out)
        self.assertNotIn("Notes `AG`", out)
        self.assertNotRegex(out, r"\[(?:APP|ST, CT|TEAM_KEY), (?!Agent\])[A-Z][a-z][A-Za-z ]*\]")

    def test_cursor_and_grok_notes_names(self) -> None:
        for key, notes in (("cursor", "Cursor"), ("grok", "Grok"), ("codex", "Codex")):
            out = specialize_from_monet(_notes(), SEATS[key], skill_name="apple-notes")
            self.assertIn(f"Notes `{notes}`", out, key)
            self.assertNotRegex(
                out, r"\[(?:APP|ST, CT|TEAM_KEY), (?!Agent\])[A-Z][a-z][A-Za-z ]*\]", key
            )

    def test_fleet_coordination_seats_table_preserved(self) -> None:
        out = specialize_from_monet(_coord(), SEATS["ag"], skill_name="fleet-coordination")
        self.assertIn("Antigravity / Gemini: `AG`", out)
        self.assertIn("Cursor: `CURSOR`", out)
        self.assertIn("Codex: `CODEX`", out)
        self.assertIn("Clutch: `CLUTCH`", out)
        self.assertIn("Claude: `CLAUDE`", out)
        self.assertNotIn("Monet: `", out)

    def test_specialize_universal(self) -> None:
        out_sess = specialize_universal(_session(), skill_name="session-start")
        self.assertIn("set AGENT_SEAT to your platform default", out_sess)
        self.assertIn("Platform Defaults", out_sess)
        self.assertIn("`[<YOUR_TAG>·session8]` tag", out_sess)
        self.assertIn("<seat>/<slug>", out_sess)
        self.assertNotIn("AGENT_SEAT=MONET", out_sess)

        out_not = specialize_universal(_notes(), skill_name="apple-notes")
        self.assertIn("[TEAM_KEY, Agent]", out_not)
        self.assertNotRegex(
            out_not, r"\[(?:APP|ST, CT|TEAM_KEY), (?!Agent\])[A-Z][a-z][A-Za-z ]*\]"
        )

    def test_codex_grok_renoir_deepseek_tags(self) -> None:
        mapping = {
            "codex": "CODEX",
            "grok": "GROK",
            "renoir": "RENOIR",
            "deepseek": "DSH",
            "minimax": "MM",
            "grok-build": "GROK",  # the Grok Build pack signs GROK (owner 2026-10-10)
            "claude": "CLAUDE",
        }
        src = _session()
        for key, tag in mapping.items():
            out = specialize_from_monet(src, SEATS[key], skill_name="session-start")
            if SEATS[key].retired:  # never anyone's default:  the arm fails
                self.assertIn(
                    f'else SEAT="${{AGENT_SEAT:?{tag} is retired; take no work as {tag}}}"; fi',
                    out,
                    key,
                )
            else:
                self.assertIn(_arm(tag), out, key)
            self.assertNotIn("AGENT_SEAT=MONET", out, key)
        grok_bot = specialize_from_monet(src, SEATS["grok-bot"], skill_name="session-start")
        for tag in (
            GB_ROLE_TAGS
        ):  # the list is the MCP servers' own (gb-identities.json), whatever it holds
            self.assertIn(tag, grok_bot)
        self.assertNotIn("GB-CONDUCTOR", grok_bot)
        self.assertNotRegex(grok_bot, r"GB-COMPILE(?!R)")
        self.assertIn("[GB-<NAME>]", grok_bot)
        self.assertNotIn("AGENT_SEAT=MONET", grok_bot)
        self.assertNotIn("AGENT_SEAT=GROK-BOT", grok_bot)
        self.assertNotIn("You are **GROK-BOT**", grok_bot)
        self.assertNotIn("You are **CURSOR**", grok_bot)
        self.assertNotIn("You are **MONET**", grok_bot)
        self.assertIn(f"{BRANCH_PREFIX}/{BRANCH_EXAMPLE_SLUG}", grok_bot)
        self.assertNotIn("claude/afc-123-zulip-cutover", grok_bot)

    def test_kimi_retired_banner(self) -> None:
        out = specialize_from_monet(_session(), SEATS["kimi"], skill_name="session-start")
        self.assertIn("[KIMI]", out)
        self.assertIn("Retired seat", out)
        self.assertNotIn("Start every Kimi", out)
        self.assertNotIn("triple-claim before editing", out)
        self.assertFalse(SEATS["kimi"].write_home)

    def test_renoir_stays_uninstalled(self) -> None:
        self.assertFalse(SEATS["renoir"].write_home)
        dests = [dest for dest, _seat in platform_installs()]
        self.assertFalse(any("/.kimi/" in dest or dest.endswith("/.kimi/skills") for dest in dests))
        self.assertFalse(
            any("/.renoir/" in dest or dest.endswith("/.renoir/skills") for dest in dests)
        )
        out = specialize_from_monet(_session(), SEATS["renoir"], skill_name="session-start")
        self.assertIn("Retired seat", out)
        self.assertNotIn("not yet active", out)
        self.assertIn("Do not install to `~/.renoir/skills`", out)

    def test_claude_shared_pin(self) -> None:
        out = specialize_from_monet(_session(), SEATS["claude_shared"], skill_name="session-start")
        #  No "other tools scan this too" banner (owner 2026-10-11):  every platform now has its
        #  own repo tree, so the warning was both obsolete and a confusing apology in a file the
        #  other seats were reading anyway.  The identity assertions are what must hold.
        self.assertNotIn("Shared `~/.claude/skills`", out)
        self.assertNotIn("other tools scan it too", out)
        self.assertIn(_arm("CLAUDE"), out)
        self.assertIn(_ordinary("Claude", "CLAUDE"), out)
        self.assertNotIn("MONET, CLAUDE, or RENOIR", out)

    def test_sentence_gap_keeps_protocol_name(self) -> None:
        with open(GAP, encoding="utf-8") as f:
            src = f.read()
        out = specialize_from_monet(src, SEATS["cursor"], skill_name="sentence-gap")
        #  The protocol attribution is generic, never the retired Monet seat (Kody rule 13).
        self.assertIn("the canonical portable protocol", out)
        self.assertNotIn("Monet", out[:600])

    def test_quoted_description_folds_for_fx(self) -> None:
        out = specialize_from_monet(_session(), SEATS["codex"], skill_name="session-start")
        self.assertIn("description: >-", out.split("---")[1])
        self.assertNotIn("description: Start", out.split("---")[1])
        self.assertIn("just a small fix.", out)

    def test_fx_seat_is_not_cursor(self) -> None:
        self.assertEqual(SEATS["fx"].tag, "FX")
        self.assertTrue(SEATS["fx"].dest.endswith("/.fx/skills") or "fx/skills" in SEATS["fx"].dest)
        out = specialize_from_monet(_session(), SEATS["fx"], skill_name="session-start")
        self.assertIn(_arm("FX"), out)
        self.assertIn(_ordinary("Fx", "FX"), out)
        self.assertNotIn("AGENT_SEAT=CURSOR", out)
        self.assertIn("[FX·session8]", out)

    def test_fold_unwraps_quoted_yaml_string(self) -> None:
        from fleet_skill_identity import fold_yaml_description

        src = (
            "---\n"
            "name: cloudflare-one\n"
            'description: "Guides Cloudflare One \\"Zero Trust\\" work."\n'
            "---\n\n"
            "# Cloudflare One\n"
        )
        out = fold_yaml_description(src)
        self.assertIn("description: >-", out)
        self.assertIn('Guides Cloudflare One "Zero Trust" work.', out)
        self.assertNotIn('description: "', out)

    def test_fold_quotes_description_containing_a_colon(self) -> None:
        """A plain scalar holding ': ' cannot parse, so it must be folded.

        The sentence-gap description grew a "Mechanism by surface:" clause and
        the fold only ever looked for quotes, so every rendered tree shipped
        front matter no YAML parser could read.  PyYAML is not a dependency of
        this suite, so assert the structure rather than parsing it.
        """
        from fleet_skill_identity import fold_yaml_description

        src = (
            "---\n"
            "name: sentence-gap\n"
            "description: Always use a gap. Mechanism by surface: two spaces here.\n"
            "---\n\n"
            "# Sentence gap\n"
        )
        out = fold_yaml_description(src)
        self.assertIn("description: >-", out)
        self.assertIn("  Always use a gap. Mechanism by surface: two spaces here.", out)
        self.assertNotIn("description: Always use a gap.", out)
        self.assertTrue(out.endswith("# Sentence gap\n"))

    def test_fold_leaves_a_plain_safe_description_alone(self) -> None:
        from fleet_skill_identity import fold_yaml_description

        src = (
            "---\n"
            "name: cloudflare-one\n"
            "description: Guides Cloudflare One Zero Trust work.\n"
            "---\n\n"
            "# Cloudflare One\n"
        )
        out = fold_yaml_description(src)
        self.assertIn("description: Guides Cloudflare One Zero Trust work.", out)
        self.assertNotIn("description: >-", out)

    def test_seat_banner_stays_after_the_front_matter_when_the_skill_has_no_h1(self) -> None:
        """A skill that starts at `## ` has no top-level heading to insert after.

        The seat banner was prepended ahead of the YAML front matter in that
        case, so no front-matter parser could read the name or the description
        of any seat copy that carried one.  The banner belongs after the
        closing delimiter.
        """
        seat = SEATS["clutch"]
        src = (
            "---\n"
            "name: secret-safety\n"
            "description: >-\n"
            "  Hardcoded, mandatory safeguards for handling secrets.\n"
            "---\n\n"
            "## Why this exists\n\n"
            "Because.\n"
        )
        out = specialize_from_monet(src, seat, skill_name="secret-safety")
        self.assertTrue(out.startswith("---\nname: secret-safety\n"))
        self.assertIn("Runtime (Clutch)", out)
        closing = out.index("\n---\n", 4) + len("\n---\n")
        self.assertLess(closing, out.index("Runtime (Clutch)"))
        self.assertLess(out.index("Runtime (Clutch)"), out.index("## Why this exists"))


DOCS = os.path.join(ROOT, "docs", "fleet-skills")


def _load_skill(name: str) -> str:
    path = os.path.join(DOCS, name, "SKILL.md")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class CatalogAndShipBanTests(unittest.TestCase):
    def test_ios_ship_never_installed(self) -> None:
        self.assertIn("ios-ship", NEVER_INSTALL)
        self.assertNotIn("ios-ship", catalog_skill_names(DOCS))
        self.assertFalse(os.path.isdir(os.path.join(DOCS, "ios-ship")))
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "skills", "ios-ship")))
        for key, seat in SEATS.items():
            self.assertFalse(skill_allowed_for_seat("ios-ship", seat), key)

    def test_mac_cleanup_omitted_from_grok_bot(self) -> None:
        self.assertFalse(skill_allowed_for_seat("mac-cleanup", SEATS["grok-bot"]))
        self.assertTrue(skill_allowed_for_seat("mac-cleanup", SEATS["cursor"]))
        self.assertTrue(skill_allowed_for_seat("session-start", SEATS["grok-bot"]))

    def test_housekeeper_allowed_for_grok_bot(self) -> None:
        self.assertTrue(skill_allowed_for_seat("housekeeper", SEATS["grok-bot"]))
        self.assertTrue(skill_allowed_for_seat("housekeeper", SEATS["grok"]))
        self.assertTrue(skill_allowed_for_seat("housekeeper", SEATS["cursor"]))

    def test_drive_grok_tui_every_seat(self) -> None:
        self.assertTrue(skill_allowed_for_seat("drive-grok-tui", SEATS["grok-bot"]))
        self.assertTrue(skill_allowed_for_seat("drive-grok-tui", SEATS["cursor"]))
        self.assertTrue(skill_allowed_for_seat("drive-grok-tui", SEATS["grok"]))
        self.assertTrue(skill_allowed_for_seat("drive-grok-tui", SEATS["monet"]))
        self.assertTrue(skill_allowed_for_seat("drive-grok-tui", SEATS["claude"]))

    def test_rendered_skills_ban_local_mac_ios_ship(self) -> None:
        names = catalog_skill_names(DOCS)
        self.assertTrue(names)
        for key, seat in SEATS.items():
            for name in names:
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                for fragment in FORBIDDEN_LOCAL_IOS_SHIP:
                    self.assertNotIn(
                        fragment,
                        out,
                        f"{key}/{name} still teaches {fragment!r}",
                    )
        universal_sess = specialize_universal(
            _load_skill("session-start"), skill_name="session-start"
        )
        for fragment in FORBIDDEN_LOCAL_IOS_SHIP:
            self.assertNotIn(fragment, universal_sess)

    def test_unstick_pr_keeps_dealdex_hosted_ship(self) -> None:
        src = _load_skill("unstick-pr")
        self.assertIn("macos-latest", src)
        self.assertIn("DealDex's hosted Actions ship stays", src)
        for key, seat in SEATS.items():
            if not skill_allowed_for_seat("unstick-pr", seat):
                continue
            out = specialize_from_monet(src, seat, skill_name="unstick-pr")
            self.assertIn(
                "DealDex's hosted Actions ship stays",
                out,
                f"{key}/unstick-pr dropped the DealDex hosted-ship keep",
            )
            self.assertIn("macos-latest", out, key)

    def test_rendered_skills_do_not_ban_hosted_macos_latest(self) -> None:
        banned = (
            "github-hosted macos-latest is banned",
            "hosted macos-latest is banned",
            "do not use github-hosted macos-latest",
            "remove dealdex ci",
            "delete dealdex's hosted",
            "turn off dealdex",
        )
        names = catalog_skill_names(DOCS)
        hits = []
        for key, seat in SEATS.items():
            for name in names:
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(_load_skill(name), seat, skill_name=name).lower()
                for fragment in banned:
                    if fragment in out:
                        hits.append(f"{key}/{name}:{fragment}")
        self.assertEqual(
            hits,
            [],
            "rendered skills must not ban DealDex GitHub-hosted macos-latest:\n" + "\n".join(hits),
        )


class PerSeatVoiceTests(unittest.TestCase):
    def test_each_named_seat_is_not_another_seat(self) -> None:
        names = catalog_skill_names(DOCS)
        named = [key for key, seat in SEATS.items() if seat.mode in {"exclusive", "grok_bot"}]
        for key in named:
            seat = SEATS[key]
            for name in names:
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                if seat.tag != "MONET":
                    self.assertNotIn(
                        "shared Monet template",
                        out,
                        f"{key}/{name} still says shared Monet template",
                    )
                for other_key in named:
                    if other_key == key:
                        continue
                    other = SEATS[other_key]
                    if other.tag in {seat.tag, "GB-<NAME>"}:
                        continue
                    self.assertNotIn(
                        f"You are **{other.tag}**",
                        out,
                        f"{key}/{name} says You are **{other.tag}**",
                    )
                    self.assertNotIn(
                        f"This install is for `{other.tag}`",
                        out,
                        f"{key}/{name} install banner is {other.tag}",
                    )
                    pin = re.search(
                        rf"export AGENT_SEAT={re.escape(other.tag)}(?![\w-])",
                        out,
                    )
                    self.assertIsNone(
                        pin,
                        f"{key}/{name} exports AGENT_SEAT={other.tag}",
                    )

    def test_ag_skills_do_not_address_reader_as_other_seats(self) -> None:
        forbidden = (
            "You are **CLAUDE**",
            "You are **MONET**",
            "You are **CURSOR**",
            "You are **GROK**",
            "This pack is for the **MONET** Claude account",
            "This pack is for **CLAUDE**",
            "This pack is for **CURSOR**",
            "This pack is for **GROK**",
            "two different Claude accounts",
            "Claude/Monet transcript",
            "Load `~/.claude/skills",
            "AGENT_SEAT=MONET",
            "AGENT_SEAT=CLAUDE",
            "This install is for `CLAUDE`",
            "This install is for `MONET`",
            "This install is for `CURSOR`",
            "This install is for `GROK`",
            "shared Monet template",
            "Co-Authored-By: Claude <noreply@anthropic.com>",
            "~/Desktop/fleet-skills",
        )
        for name in catalog_skill_names(DOCS):
            out = specialize_from_monet(_load_skill(name), SEATS["ag"], skill_name=name)
            for phrase in forbidden:
                self.assertNotIn(phrase, out, f"ag/{name}: {phrase}")

    def test_codex_skills_do_not_address_reader_as_claude(self) -> None:
        forbidden = (
            "You are **CLAUDE**",
            "You are **MONET**",
            "This pack is for the **MONET** Claude account",
            "Claude/Monet transcript",
            "Load `~/.claude/skills",
            "shared Monet template",
            "Co-Authored-By: Claude <noreply@anthropic.com>",
            "~/Desktop/fleet-skills",
        )
        for name in catalog_skill_names(DOCS):
            out = specialize_from_monet(_load_skill(name), SEATS["codex"], skill_name=name)
            for phrase in forbidden:
                self.assertNotIn(phrase, out, f"codex/{name}: {phrase}")

    def test_cursor_skills_do_not_address_reader_as_monet(self) -> None:
        for name in catalog_skill_names(DOCS):
            out = specialize_from_monet(_load_skill(name), SEATS["cursor"], skill_name=name)
            self.assertNotIn("You are **MONET**", out, name)
            self.assertNotIn("AGENT_SEAT=MONET", out, name)
            self.assertNotIn("This install is for `MONET`", out, name)
            self.assertNotIn("This pack is for the **MONET** Claude account", out, name)

    def test_kimi_is_kimi_voiced(self) -> None:
        out = specialize_from_monet(_session(), SEATS["kimi"], skill_name="session-start")
        self.assertIn("[KIMI]", out)
        self.assertIn("Retired", out)
        self.assertNotIn("Start every Kimi", out)
        self.assertNotIn("You are **MONET**", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)

    def test_universal_is_not_claude_flavored(self) -> None:
        for name in catalog_skill_names(DOCS):
            out = specialize_universal(_load_skill(name), skill_name=name)
            self.assertNotIn("You are **MONET**", out, name)
            self.assertNotIn("You are **CLAUDE**", out, name)
            self.assertNotIn("AGENT_SEAT=MONET", out, name)
            self.assertNotIn("Load `~/.claude/skills", out, name)
            self.assertNotIn("Claude/Monet transcript", out, name)

    def test_claude_shared_does_not_claim_reader_is_monet(self) -> None:
        for name in catalog_skill_names(DOCS):
            if not skill_allowed_for_seat(name, SEATS["claude_shared"]):
                continue
            out = specialize_from_monet(_load_skill(name), SEATS["claude_shared"], skill_name=name)
            self.assertNotIn("You are **MONET**", out, name)
            self.assertNotIn("AGENT_SEAT=MONET", out, name)
            self.assertNotIn("This install is for `MONET`", out, name)
            self.assertNotIn("MONET, CLAUDE, or RENOIR", out, name)
            self.assertNotIn("[MONET·session8", out, name)


class CoordinatorSelfIdTests(unittest.TestCase):
    """Coordinator/ops self-id is AFC.  FLEET is a Grok Bot wake only."""

    FORBIDDEN_COORDINATOR_SELF = (
        "This install is for `FLEET`",
        "You are **FLEET**",
        "Tag `[FLEET]`.",
        "This pack is for **FLEET**",
        "This install is for `GB-FLEET`",
        "You are **GB-FLEET**",
        "This pack is for **GB-FLEET**",
        "| **`AFL`** |",
        "| `AFL` |",
    )

    def test_constants_and_gb_roles(self) -> None:
        self.assertEqual(COORDINATOR_SELF_ID, "AFC")
        self.assertIn("GB-COMPILER", GB_ROLE_TAGS)
        self.assertIn("GB-ORACLE", GB_ROLE_TAGS)
        self.assertNotIn("GB-COMPILE", GB_ROLE_TAGS)
        gb = json.loads(
            (
                Path(__file__).resolve().parent / "agent_sync" / "mcp" / "gb-identities.json"
            ).read_text()
        )
        self.assertEqual(list(GB_ROLE_TAGS), gb["enabled"], "one list:  the MCP servers' GB table")
        for stale in ("GB-CONDUCTOR", "GB-NURSE", "GB-ACCOUNTANT"):
            self.assertNotIn(stale, GB_ROLE_TAGS)
        self.assertTrue(is_grok_bot_tag("GB-ORACLE"))
        self.assertTrue(is_grok_bot_tag("GB-COMPILER"))
        self.assertFalse(is_grok_bot_tag("AFC"))
        self.assertTrue(head_has_fleet_wake("[MONET->FLEET] sync-1"))
        self.assertTrue(head_has_fleet_wake("[CLAUDE·1a2b3c4d] @**all** HALT"))
        self.assertTrue(
            head_has_fleet_wake("[CLAUDE·1a2b3c4d] @*fleet* HALT"),
            "the retired group form is still read",
        )
        self.assertFalse(head_has_fleet_wake("[CLAUDE·1a2b3c4d] @**Codex** HALT"))
        self.assertFalse(head_has_fleet_wake("[FLEET] sync-1"))
        self.assertFalse(head_has_fleet_wake("[AFC] sync-1"))

    def test_fleet_apps_acronym_is_afc(self) -> None:
        import json

        data = json.loads(Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8"))
        rows = [a for a in data["apps"] if a["repo"] == "AI-Fleet-Coordinator"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["acronym"], "AFC")
        self.assertNotEqual(rows[0]["acronym"], "FLEET")

    def test_minimax_is_mm_and_deepseek_harness_is_dsh(self) -> None:
        import json

        self.assertEqual(SEATS["minimax"].tag, "MM")
        self.assertEqual(SEATS["minimax"].notes, "MiniMax")
        self.assertEqual(SEATS["deepseek"].tag, "DSH")
        self.assertEqual(SEATS["deepseek"].notes, "DeepSeek Harness")
        data = json.loads(Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8"))
        tags = {s["tag"]: s for s in data["seats"]}
        self.assertEqual(tags["MM"]["notesName"], "MiniMax")
        self.assertEqual(tags["DSH"]["notesName"], "DeepSeek Harness")
        self.assertNotIn("MINIMAX", tags)
        self.assertNotIn("DEEPSEEK", tags)
        text = _protocol_doc_path().read_text(encoding="utf-8")
        # The Oct 7 rewrite moved the seat roster into the Active Seats
        # table (MM is a row) and the Availability list (DSH is retired).
        self.assertIn("| MM | `MiniMax` | `minimax/` |", text)
        self.assertNotRegex(text, r"(?m)^\| DSH \|")
        self.assertIn("DSH, DeepSeek Harness (2026-09-19)", text)
        self.assertIn("`[MM]`", text)
        self.assertNotIn("| **MiniMax (`MINIMAX`)** |", text)
        self.assertNotIn("| **DeepSeek (`DEEPSEEK`)** |", text)
        coord = _coord()
        self.assertIn("MiniMax (MM): `MM` (display `MiniMax`, prefix `minimax/`", coord)
        self.assertIn("Retired:  MONET, RENOIR, HARNESS, DSH, KIMI have no bot", coord)
        self.assertNotIn("MiniMax: `[MINIMAX]`", coord)
        self.assertNotIn("DeepSeek: `[DEEPSEEK]`", coord)

    def test_agent_sync_self_id_is_afc_not_fleet(self) -> None:
        text = _protocol_doc_path().read_text(encoding="utf-8")
        self.assertIn("| `AFC` |", text)
        self.assertIn("Simple-With-Us/AI-Fleet-Coordinator", text)
        self.assertIn("[AFC] sync-N", text)
        # The fleet-wide wake rule now lives in "Undirected, Directed, and
        # Fleet-Wide"; `fleet` is a recipient only, never a sender or an acronym.
        self.assertIn("### Undirected, Directed, and Fleet-Wide", text)
        self.assertIn("every listening seat on every platform", text)
        self.assertIn("`fleet` is a recipient only", text)
        self.assertNotIn("| `AFL` |", text)
        for phrase in self.FORBIDDEN_COORDINATOR_SELF:
            self.assertNotIn(phrase, text, phrase)
        self.assertNotIn("This coordinator/ops system signs as `[FLEET]`", text)

    def test_canonical_skills_use_afc_not_fleet_self_name(self) -> None:
        for name in (
            "session-start",
            "fleet-coordination",
            "apple-notes",
            "land-lane",
            "deploy-verify",
        ):
            src = _load_skill(name)
            self.assertIn("AFC", src, name)
            for phrase in self.FORBIDDEN_COORDINATOR_SELF:
                self.assertNotIn(phrase, src, f"{name}: {phrase}")
            self.assertNotRegex(src, r"GB-COMPILE(?!R)", msg=name)
            if name == "session-start":
                self.assertIn("| AFC |", src)
                self.assertNotIn("| FLEET | `~/apps/fleet-monet`", src)
                self.assertNotIn("| FLEET |", src.split("AI-Fleet-Coordinator")[1][:80])
            if name == "fleet-coordination":
                self.assertIn("| **`AFC`** |", src)
            if name == "apple-notes":
                self.assertIn("| AFC |", src)
                self.assertNotIn("| FLEET | cross-app", src)
            if name == "land-lane":
                self.assertIn("| AFC |", src)
                self.assertNotIn("| FLEET | No app test gate", src)
            if name == "deploy-verify":
                self.assertIn("| AFC |", src)
                self.assertNotIn("| FLEET | GitHub Pages digest", src)

    def test_specialized_banners_do_not_call_coordinator_fleet(self) -> None:
        names = [
            n
            for n in catalog_skill_names(DOCS)
            if n
            in {
                "session-start",
                "fleet-coordination",
                "apple-notes",
                "land-lane",
                "deploy-verify",
            }
        ]
        self.assertTrue(names)
        for key, seat in SEATS.items():
            for name in names:
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                for phrase in self.FORBIDDEN_COORDINATOR_SELF:
                    self.assertNotIn(phrase, out, f"{key}/{name}: {phrase}")
                if name == "session-start" and not seat.retired:
                    self.assertIn("| AFC |", out, f"{key}/{name} lost AFC acronym")
                if name == "fleet-coordination":
                    self.assertIn("AFC", out, f"{key}/{name}")
                    self.assertNotRegex(out, r"GB-COMPILE(?!R)", msg=f"{key}/{name}")

    # AFC and OPS are repo acronyms (owner 2026-10-16), never a sender or a signature:
    # no canonical skill and no seat render may call either one the coordinator's self-id.
    AFC_SIGNATURE_PHRASES = (
        "self-id is `AFC`",
        "self-id is `{COORDINATOR_SELF_ID}`",
        "signs as `AFC`",
        "Sign Zulip as `[AFC]`",
        "posts as `[AFC]`",
        "Sibling infra identity",
        "Coordinator/ops self-id",
    )

    def test_afc_is_a_repo_acronym_never_a_signature(self) -> None:
        for name in catalog_skill_names(DOCS):
            src = _load_skill(name)
            for phrase in self.AFC_SIGNATURE_PHRASES:
                self.assertNotIn(phrase, src, f"canonical {name}: {phrase}")
            for key, seat in SEATS.items():
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(src, seat, skill_name=name)
                for phrase in self.AFC_SIGNATURE_PHRASES:
                    self.assertNotIn(phrase, out, f"{key}/{name}: {phrase}")
        stop = specialize_from_monet(
            _load_skill("session-start"), SEATS["kimi"], skill_name="session-start"
        )
        self.assertIn("`AFC` is a repo acronym, never a sender or a signature", stop)

    def test_by_seat_catalog_matches_specialization(self) -> None:
        kimi_ss = Path(DOCS, "by-seat", "kimi", "session-start", "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Start every Kimi", kimi_ss)
        self.assertIn("Retired seat", kimi_ss)
        gb = Path(DOCS, "by-seat", "grok-bot", "session-start", "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("GB-COMPILER", gb)
        self.assertIn("GB-ORACLE", gb)
        self.assertNotRegex(gb, r"GB-COMPILE(?!R)")
        cursor_ss = Path(DOCS, "by-seat", "cursor", "session-start", "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("| AFC |", cursor_ss)
        self.assertNotIn("This install is for `FLEET`", cursor_ss)


GUIDE = os.path.join(ROOT, "docs", "protocols", "zulip-fleet-guide.md")
RETIRED_HOME_FRAGMENTS = ("/Desktop/fleet-skills", "/.deepseek/", "/.kimi/", "/.renoir/")


def _guide_roster() -> dict[str, tuple[str, str]]:
    """Seat tag -> (bot local part, credential file code) from the guide."""
    text = Path(GUIDE).read_text(encoding="utf-8")
    start = text.index("### Agent Seats")
    end = text.index("### BotFleet (BF) Bots")
    rows: dict[str, tuple[str, str]] = {}
    for line in text[start:end].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not re.fullmatch(r"[A-Z][A-Z-]*", cells[0]):
            continue
        rows[cells[0]] = (cells[2].rstrip("@"), cells[3])
    return rows


def _bot_local(seat) -> str:
    local = seat.zulip_bot
    return local if local.endswith("-bot") else f"{local}-bot"


class ZulipIdentityAndRetirementTests(unittest.TestCase):
    """The identity writer speaks Zulip, drops retired homes, and knows CLUTCH."""

    ACTIVE_CATALOG_TAGS = {
        "CLAUDE",
        "CODEX",
        "AG",
        "CURSOR",
        "GROK",
        "CLUTCH",
        "FX",
        "MM",
        "MC",
        "MA",
    }

    def test_grok_roster_mention_is_grok_build_not_grok_build_tag(self) -> None:
        text = Path(GUIDE).read_text(encoding="utf-8")
        start = text.index("### Agent Seats")
        end = text.index("### BotFleet (BF) Bots")
        markdown_rows = [
            line for line in text[start:end].splitlines() if line.strip().startswith("|")
        ]
        self.assertNotIn(
            "@**GROK-BUILD**",
            "\n".join(markdown_rows),
            "the Who's Here table must not use a GROK-BUILD mention",
        )
        grok_row = [line for line in markdown_rows if line.strip().startswith("| GROK |")]
        self.assertEqual(len(grok_row), 1)
        self.assertIn("@**Grok (Build)**", grok_row[0])

    def test_every_zulip_identity_matches_the_guide_roster(self) -> None:
        roster = _guide_roster()
        self.assertTrue(roster, "guide roster table not found")
        seen: set[str] = set()
        for key, seat in SEATS.items():
            if not seat.zulip_bot:
                continue
            if seat.tag not in roster:
                # GROK-BUILD is an alias row that posts through GROK's bot.
                self.assertEqual((_bot_local(seat), seat.zulip_rc), roster["GROK"], key)
                continue
            seen.add(seat.tag)
            self.assertEqual((_bot_local(seat), seat.zulip_rc), roster[seat.tag], key)
        self.assertTrue(self.ACTIVE_CATALOG_TAGS <= seen, self.ACTIVE_CATALOG_TAGS - seen)

    def test_retired_seats_have_no_bot_home_or_install_banner(self) -> None:
        retired = {k: s for k, s in SEATS.items() if s.retired}
        self.assertEqual(set(retired), {"monet", "renoir", "deepseek", "kimi"})
        for key, seat in retired.items():
            self.assertEqual(seat.zulip_bot, "", key)
            self.assertFalse(seat.write_home, key)
            self.assertEqual(zulip_identity_sentence(seat), "", key)
            out = specialize_from_monet(_session(), seat, skill_name="session-start")
            self.assertIn("Retired seat", out, key)
            self.assertNotIn("This install is for", out, key)

    def test_no_install_target_is_a_retired_or_catalog_only_home(self) -> None:
        dests = [dest for dest, _seat in platform_installs()]
        for dest in dests:
            for fragment in RETIRED_HOME_FRAGMENTS:
                self.assertNotIn(fragment, dest + "/", dest)
            self.assertNotIn("/by-seat/", dest)
        tags = {seat.tag for _dest, seat in platform_installs()}
        self.assertFalse(tags & {"MONET", "RENOIR", "DSH", "KIMI"}, tags)
        self.assertIn("CLUTCH", tags)

    def test_retired_session_start_is_inert(self) -> None:
        for key in ("monet", "renoir", "deepseek", "kimi"):
            seat = SEATS[key]
            out = specialize_from_monet(_session(), seat, skill_name="session-start")
            front = out.split("---")[1]
            self.assertIn(f"{seat.tag} is retired", front, key)
            self.assertNotIn("Start every", out, key)
            self.assertNotIn("triple-claim before editing", out.split("---")[1], key)
            if key == "kimi":
                # Kimi's copy is only a Stop section.  MONET, RENOIR and DSH keep
                # their body until the peer-screen lane lands (STOP_ONLY_RETIRED).
                self.assertIn("## Stop", out, key)
                self.assertNotIn("## 1. Identity", out, key)
                self.assertIn("`AFC`", out, key)
            else:
                self.assertNotIn("## Stop", out, key)

    def test_set_yaml_description_replaces_inline_and_folded_values(self) -> None:
        folded = "---\nname: x\ndescription: >-\n  old one\n  old two\n---\n\n# X\n"
        inline = "---\nname: x\ndescription: old\n---\n\n# X\n"
        for src in (folded, inline):
            out = set_yaml_description(src, "new value")
            self.assertIn("description: >-\n  new value\n---", out)
            self.assertNotIn("old", out)
            self.assertTrue(out.endswith("# X\n"))

    def test_shared_claude_home_renders_as_claude(self) -> None:
        shared = SEATS["claude_shared"]
        self.assertEqual(shared.tag, "CLAUDE")
        self.assertEqual((shared.zulip_bot, shared.zulip_rc), ("claude-bot", "Claude"))
        homes = dict((seat.seat_key, dest) for dest, seat in platform_installs())
        self.assertTrue(homes["claude_shared"].endswith("/.claude/skills"))
        repo_claude = [
            (dest, seat)
            for dest, seat in repo_platform_copies(ROOT)
            if dest.endswith(os.path.join(".claude", "skills"))
        ]
        self.assertEqual(len(repo_claude), 1)
        self.assertEqual(repo_claude[0][1].tag, "CLAUDE")
        # The shared home is a second home for the claude pack, not another pack.
        self.assertNotIn(shared, catalog_seats())
        self.assertIn(SEATS["claude"], catalog_seats())

    def test_clutch_has_the_dsh_skill_home_and_its_own_identity(self) -> None:
        seat = SEATS["clutch"]
        self.assertEqual(
            (seat.tag, seat.notes, seat.prefix, seat.suffix),
            ("CLUTCH", "Clutch", "clutch", "clutch"),
        )
        self.assertTrue(seat.write_home)
        self.assertEqual(
            seat.dest, "~/.clutch/dsh/skills"
        )  # the engine's $DSH_HOME/skills, rank 400
        self.assertIn((os.path.expanduser("~/.clutch/dsh/skills"), seat), platform_installs())
        self.assertFalse(seat.retired)
        self.assertIn(seat, catalog_seats())
        self.assertTrue(skill_allowed_for_seat("mac-cleanup", seat))
        out = specialize_from_monet(_session(), seat, skill_name="session-start")
        self.assertIn(_arm("CLUTCH"), out)
        self.assertIn(_ordinary("Clutch", "CLUTCH"), out)
        self.assertIn("Runtime (Clutch)", out)
        self.assertIn("beats the CLUTCH default", out)
        self.assertNotIn("no skill home", out)
        self.assertNotIn("Catalog copy", out)
        self.assertIn("clutch/<slug>", out)
        self.assertIn("clutch-bot@simplewithus.zulipchat.com", out)
        self.assertIn("`CLUTCH_ZULIP_KEY`", out)
        self.assertIn("[CLUTCH·session8]", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertNotIn("DSH·session8", out)

    def test_session_tag_and_board_by_are_the_readers_own(self) -> None:
        for key, seat in SEATS.items():
            if seat.retired or seat.mode == "grok_bot":
                continue
            ss = specialize_from_monet(_session(), seat, skill_name="session-start")
            board = specialize_from_monet(_load_skill("board-ops"), seat, skill_name="board-ops")
            self.assertIn(f"`[{seat.tag}·session8]` tag", ss, key)
            self.assertNotIn("[MONET·session8", ss, key)
            self.assertIn(
                f"`--by` for this seat is your verified seat (`{seat.tag}` in an ordinary",
                board,
                key,
            )
            self.assertIn("--by <YOUR_TAG>", board, key)
            self.assertNotIn(f"--by {seat.tag}", board, key)
        gb = specialize_from_monet(_session(), SEATS["grok-bot"], skill_name="session-start")
        self.assertNotIn("MONET·session8", gb)
        uni = specialize_universal(_session(), skill_name="session-start")
        self.assertNotIn("MONET·session8", uni)
        self.assertIn("<YOUR_TAG>·session8", uni)
        uni_board = specialize_universal(_load_skill("board-ops"), skill_name="board-ops")
        self.assertNotIn("for this seat is `MONET`", uni_board)

    def test_slack_survives_only_as_a_retirement_note(self) -> None:
        """Every Slack mention in a rendered skill, the README or the writer says retired."""
        offenders: list[str] = []

        def scan(label: str, text: str) -> None:
            for n, line in enumerate(text.splitlines(), 1):
                if re.search(r"slack", line, re.IGNORECASE) and "retired" not in line.lower():
                    offenders.append(f"{label}:{n}: {line.strip()[:100]}")

        names = catalog_skill_names(DOCS)
        for key, seat in SEATS.items():
            for name in names:
                if skill_allowed_for_seat(name, seat):
                    scan(
                        f"{key}/{name}",
                        specialize_from_monet(_load_skill(name), seat, skill_name=name),
                    )
        for name in names:
            scan(f"universal/{name}", specialize_universal(_load_skill(name), skill_name=name))
            scan(f"source/{name}", _load_skill(name))
        scan("README-add-in-app.md", Path(DOCS, "README-add-in-app.md").read_text(encoding="utf-8"))
        for script in ("fleet_skill_identity.py", "install-fleet-skills.py"):
            scan(script, Path(ROOT, "scripts", script).read_text(encoding="utf-8"))
        self.assertEqual(offenders, [], "live Slack instruction survived:\n" + "\n".join(offenders))


class AgentSeatPinTests(unittest.TestCase):
    """The seat-precedence block (FLEET-PROTOCOL.md § Identity Rules, owner 2026-10-09).

    BotFleet runs Claude, Codex and other CLIs as engines for its own bots, and
    those engines load these skills.  A bare `export AGENT_SEAT=<PLATFORM>`
    would stamp the platform seat over the seat the launcher assigned.  Every
    identity block reads AGENT_LAUNCH_SEAT first, fails closed (exit 3) when a
    launcher set none, takes the platform default only in an ordinary session,
    and never overwrites an AGENT_SEAT that is already set.  T15 and T16 of the
    seat-precedence design.
    """

    FIX = "python3 scripts/install-fleet-skills.py --repo-only"
    EXPORT = 'export AGENT_SEAT="${AGENT_SEAT:-$SEAT}"'

    @staticmethod
    def _exports(text: str) -> list[str]:
        return [l for l in text.splitlines() if l.startswith("export AGENT_SEAT")]

    @staticmethod
    def _block(text: str) -> str:
        """The rendered seat block, from its `if` line through the whoami line."""
        match = re.search(
            r'(?ms)^if \[ -n "\$\{AGENT_LAUNCH_SEAT:-\}" \].*?^simple-chat whoami --as "\$SEAT"$',
            text,
        )
        return match.group(0) if match else ""

    def _renders(self) -> list[tuple[str, str]]:
        src = _session()
        out: list[tuple[str, str]] = []
        for key, seat in SEATS.items():
            out.append((key, specialize_from_monet(src, seat, skill_name="session-start")))
        out.append(("universal", specialize_universal(src, skill_name="session-start")))
        return out

    def test_canonical_block_is_the_monet_block_the_renderer_replaces(self) -> None:
        # The canonical pack carries the retired MONET seat's own block (what
        # seat_pin_block() renders for MONET, whose default arm fails), never a
        # bare assignment.  If its shape drifts from the renderer's source
        # pattern, this test names the dead replacement instead of letting it rot.
        src = _session()
        block = seat_pin_block(SEATS["monet"])
        self.assertEqual(self._exports(src), [self.EXPORT])
        self.assertIn(block, src)
        self.assertEqual(_SEAT_PIN_SOURCE.findall(src), [block])
        self.assertIn("MONET is retired", block)

    def test_every_render_has_one_block_and_no_bare_assignment(self) -> None:
        for key, out in self._renders():
            if key == "kimi":  # STOP-only retired copy: the identity section is cut
                self.assertEqual(self._exports(out), [], key)
                continue
            self.assertEqual(self._exports(out), [self.EXPORT], key)
            self.assertTrue(self._block(out), key)
            self.assertNotRegex(out, r"(?m)^export AGENT_SEAT=[^\"]", key)
            self.assertNotIn("@@", out, f"{key}: an identity token survived the render")

    def test_exclusive_seat_block_names_its_own_seat_and_platform(self) -> None:
        for key, seat in SEATS.items():
            if key in {"kimi", "grok-bot"}:
                continue
            out = specialize_from_monet(_session(), seat, skill_name="session-start")
            if seat.retired:
                self.assertIn("a retired catalog pack is never a platform default", out, key)
                self.assertIn("A retired pack takes no work", out, key)
                self.assertNotIn("branch prefix from your launcher", out, key)
                self.assertNotIn(f"`{seat.prefix}` for {seat.tag}", out, key)
                continue
            self.assertIn(_arm(seat.tag), self._block(out).splitlines(), key)
            self.assertIn(_ordinary(seat.notes, seat.tag), out, key)
            self.assertIn(
                f"`<branch-prefix>` below is your seat's branch prefix:  `{seat.prefix}`", out, key
            )

    def test_t15_every_render_carries_the_launcher_clause(self) -> None:
        for key, out in self._renders():
            if key == "kimi":
                self.assertNotIn(LAUNCHER_CLAUSE, out)
                continue
            self.assertIn(LAUNCHER_CLAUSE, out, key)
            self.assertIn("you have no seat:  do no fleet action", out, key)
            if not SEATS.get(key) or not SEATS[key].retired:
                self.assertNotIn("[MONET·", out, key)

    def test_t15_board_and_claim_lines_carry_no_platform_seat(self) -> None:
        for key, seat in SEATS.items():
            if seat.retired:
                continue
            out = specialize_from_monet(_session(), seat, skill_name="session-start")
            self.assertIn("--by <YOUR_TAG>", out, key)
            self.assertNotRegex(out, r"--by [A-Z]", key)
            self.assertIn("claim:  <branch-prefix>/<slug>", out, key)
            self.assertNotIn(f"claim:  {seat.prefix}/", out, key)

    def test_grok_bot_has_no_platform_default(self) -> None:
        out = specialize_from_monet(_session(), SEATS["grok-bot"], skill_name="session-start")
        self.assertIn('else SEAT="${AGENT_SEAT:-${AGENT_TAG:?set GB-', self._block(out))

    def test_tracked_trees_carry_no_bare_assignment(self) -> None:
        # Every rendered tree, not only the ones a fresh-render test compares:
        # the root skills/ tree, the platform trees and the by-seat catalog.
        # docs/fleet-skills itself is the hand-edited Monet upload pack.
        offenders: list[str] = []
        roots = [Path(ROOT, "skills"), Path(DOCS, "by-seat")]
        roots += [Path(dest) for dest, _seat in repo_platform_copies(ROOT)]
        for root in roots:
            for path in sorted(root.rglob("SKILL.md")):
                text = path.read_text(encoding="utf-8")
                if re.search(r'(?m)^export AGENT_SEAT=[^"]', text) or "@@" in text:
                    offenders.append(os.path.relpath(str(path), ROOT))
        self.assertEqual(offenders, [], f"bare AGENT_SEAT export; re-render with: {self.FIX}")

    @unittest.skipUnless(shutil.which("bash"), "bash is needed to run the seat block")
    def test_t16_the_block_follows_the_precedence_in_a_real_shell(self) -> None:
        import subprocess

        base = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        base["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")

        def run(block: str, **env: str) -> tuple[int, str, str]:
            script = block.replace(
                'simple-chat whoami --as "$SEAT"', 'printf "%s|%s" "$SEAT" "$AGENT_SEAT"'
            )
            done = subprocess.run(
                ["bash", "-c", script], env={**base, **env}, capture_output=True, text=True
            )
            return done.returncode, done.stdout, done.stderr

        for key, out in self._renders():
            if key == "kimi":
                continue
            block = self._block(out)
            check = subprocess.run(["bash", "-n", "-c", block], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, f"{key}: {check.stderr}")
            # A launcher's seat wins, and the AGENT_SEAT an engine's rules exported is left alone
            # (the CLI then refuses the mismatch).
            self.assertEqual(
                run(
                    block,
                    AGENT_LAUNCHER="botfleet",
                    AGENT_LAUNCH_SEAT="BF-PLUMBER",
                    AGENT_SEAT="CODEX",
                ),
                (0, "BF-PLUMBER|CODEX", ""),
                key,
            )
            self.assertEqual(
                run(block, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER")[:2],
                (0, "BF-PLUMBER|BF-PLUMBER"),
                key,
            )
            # A launcher with no seat fails closed, before any default is read.
            code, stdout, stderr = run(block, AGENT_LAUNCHER="botfleet", AGENT_SEAT="CLAUDE")
            self.assertEqual((code, stdout), (3, ""), key)
            self.assertIn("no seat assigned by botfleet", stderr, key)
            # A seat already set is kept.
            self.assertEqual(
                run(block, AGENT_SEAT="BF-LAUNCHED")[:2], (0, "BF-LAUNCHED|BF-LAUNCHED"), key
            )
            # Nothing set:  the platform default, or a loud failure where there is none.
            code, stdout, stderr = run(block)
            seat = SEATS.get(key)
            if seat is not None and seat.mode == "exclusive" and not seat.retired:
                self.assertEqual((code, stdout), (0, "%s|%s" % (seat.tag, seat.tag)), key)
            else:
                self.assertNotEqual(code, 0, key)
                self.assertEqual(stdout, "", key)
            if key == "grok-bot":
                self.assertEqual(
                    run(block, AGENT_TAG="GB-ORACLE")[:2], (0, "GB-ORACLE|GB-ORACLE"), key
                )


# Skills whose tracked renders are held at their origin/main text.  The
# Slack sweep (board 18f61cf4) was told not to touch anything named
# drive-grok-tui, so its by-seat and platform renders are skipped by the two
# freshness checks below.  To unfreeze:  delete this constant, run
# `python3 scripts/install-fleet-skills.py --repo-only`, and commit the five
# drive-grok-tui renders plus the new by-seat/clutch copy it produces.
FROZEN_RENDERS = frozenset({"drive-grok-tui"})


class CatalogMatchesFreshRenderTests(unittest.TestCase):
    """by-seat/ and skills/ must equal a fresh render, so a second --repo-only is a no-op."""

    FIX = "python3 scripts/install-fleet-skills.py --repo-only"

    def test_by_seat_catalog_equals_fresh_render(self) -> None:
        stale: list[str] = []
        expected_dirs = set()
        for seat in catalog_seats():
            key = seat.seat_key or seat.tag.lower()
            expected_dirs.add(key)
            if seat.retired:
                #  A retired seat's catalog copy is history, and the installer no longer
                #  re-renders it, so it is held at its retirement text rather than
                #  compared against a fresh render.
                continue
            for name in catalog_skill_names(DOCS):
                if name in FROZEN_RENDERS:
                    continue
                path = Path(DOCS, "by-seat", key, name, "SKILL.md")
                if not skill_allowed_for_seat(name, seat):
                    if path.exists():
                        stale.append(os.path.relpath(str(path), ROOT) + " (must not exist)")
                    continue
                want = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                have = path.read_text(encoding="utf-8") if path.is_file() else None
                if have != want:
                    stale.append(os.path.relpath(str(path), ROOT))
        on_disk = {p.name for p in Path(DOCS, "by-seat").iterdir() if p.is_dir()}
        self.assertEqual(on_disk, expected_dirs, "by-seat dirs differ from catalog_seats()")
        self.assertEqual(stale, [], f"stale by-seat copies; re-render with: {self.FIX}")

    def test_universal_skills_equal_fresh_render(self) -> None:
        stale = []
        for name in catalog_skill_names(DOCS):
            path = Path(ROOT, "skills", name, "SKILL.md")
            want = specialize_universal(_load_skill(name), skill_name=name)
            if not path.is_file() or path.read_text(encoding="utf-8") != want:
                stale.append(os.path.relpath(str(path), ROOT))
        self.assertEqual(stale, [], f"stale skills/ copies; re-render with: {self.FIX}")


class RetiredCatalogPackRenderTests(unittest.TestCase):
    """A retired seat's catalog pack is rendered from the canonical pack, like every
    other seat's.

    The owner ruled on Oct 22 2026 that the rename guard's skip of the retired
    seats' catalog copies goes away:  the copies are rendered from the canonical
    pack like every other seat's are, so a stale consumer under one of those
    folders is caught instead of passing unnoticed.  A retired seat's pack still
    carries its retirement notice, because that notice comes from the renderer and
    not from a hand edit.

    So the pin here is a positive one.  A retired pack is a fresh render of the
    canonical pack for that seat, byte for byte, and it carries the retirement
    notice the renderer writes.  A pack left behind by hand, and a pack that has
    drifted from the one home, fail both.
    """

    def _retired_seats(self) -> list:
        return [seat for seat in catalog_seats() if seat.retired]

    def test_a_retired_pack_is_a_fresh_render_of_the_canonical_pack(self) -> None:
        seats = self._retired_seats()
        self.assertTrue(seats, "the registry names no retired seat")
        for seat in seats:
            key = seat.seat_key or seat.tag.lower()
            seat_root = Path(DOCS, "by-seat", key)
            self.assertTrue(seat_root.is_dir(), "%s is missing" % seat_root)
            for name in catalog_skill_names(DOCS):
                if not skill_allowed_for_seat(name, seat):
                    continue
                src = Path(DOCS, name, "SKILL.md")
                if not src.is_file():
                    continue
                got_path = seat_root / name / "SKILL.md"
                self.assertTrue(got_path.is_file(), "%s is missing" % got_path)
                want = specialize_from_monet(src.read_text(encoding="utf-8"), seat, skill_name=name)
                self.assertEqual(
                    got_path.read_text(encoding="utf-8"),
                    want,
                    "%s/%s is not a fresh render of the canonical pack" % (key, name),
                )

    def test_a_retired_pack_carries_its_retirement_notice(self) -> None:
        for seat in self._retired_seats():
            key = seat.seat_key or seat.tag.lower()
            path = Path(DOCS, "by-seat", key, "session-start", "SKILL.md")
            self.assertTrue(path.is_file(), "%s is missing" % path)
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                seat.tag,
                text,
                "%s/session-start does not name the seat it was rendered for" % key,
            )
            self.assertIn(
                "retired",
                text.lower(),
                "%s/session-start carries no retirement notice" % key,
            )


class RepoPlatformCopiesTests(unittest.TestCase):
    """The repo-tracked platform trees must equal a fresh render of the pack.

    Sessions opened in this repo load the platform tree directly, so a stale copy
    teaches the old lane commands.  Every platform that owns a tool home gets a
    tree here, rendered by that platform's own seat (incident, Oct 11 2026:  MM
    fell through to the CLAUDE-rendered `.claude/skills` because no `.minimax`
    tree existed).
    """

    FIX = "python3 scripts/install-fleet-skills.py --repo-only"

    def test_every_tool_home_platform_gets_a_repo_tree(self) -> None:
        copies = repo_platform_copies(ROOT)
        by_dest = {dest: seat for dest, seat in copies}
        for seat in SEATS.values():
            if seat.retired or not seat.dest.startswith("~/"):
                continue
            want = os.path.join(ROOT, *seat.dest[2:].split("/"))
            if seat.seat_key in ("claude", "grok-build"):
                continue  # deliberate exclusions, see NO_REPO_TREE
            self.assertIn(want, by_dest, f"{seat.seat_key} has no repo tree")
            self.assertIs(by_dest[want], seat, want)

    def test_repo_trees_are_distinct(self) -> None:
        dests = [dest for dest, _seat in repo_platform_copies(ROOT)]
        self.assertEqual(len(dests), len(set(dests)), "two seats collide on one tree")

    def test_retired_seats_get_no_new_repo_tree(self) -> None:
        dests = {dest for dest, _seat in repo_platform_copies(ROOT)}
        for key, seat in SEATS.items():
            if not seat.retired:
                continue
            self.assertNotIn(os.path.join(ROOT, *seat.dest[2:].split("/")), dests, key)

    def test_the_three_original_trees_keep_their_seats(self) -> None:
        by_dest = {dest: seat for dest, seat in repo_platform_copies(ROOT)}
        for rel, key in (
            (os.path.join(".claude", "skills"), "claude_shared"),
            (os.path.join(".cursor", "skills"), "cursor"),
            (os.path.join(".grok", "skills"), "grok"),
        ):
            self.assertIn(os.path.join(ROOT, rel), by_dest, rel)
            self.assertEqual(by_dest[os.path.join(ROOT, rel)], SEATS[key], rel)

    def test_nine_trees_never_teach_claude_branch_example(self) -> None:
        peer_literal = "claude/afc-123-zulip-cutover"
        skills = ("session-start", "board-ops", "fleet-coordination")
        for dest, seat in repo_platform_copies(ROOT):
            if seat.prefix == "claude":
                continue
            for skill_name in skills:
                path = Path(dest, skill_name, "SKILL.md")
                self.assertTrue(path.is_file(), path)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    peer_literal,
                    text,
                    f"{seat.tag} {skill_name} in {path}",
                )
                self.assertIn(
                    f"{seat.prefix}/{BRANCH_EXAMPLE_SLUG}",
                    text,
                    f"{seat.tag} {skill_name} in {path}",
                )

    def test_minimax_tree_is_rendered_for_mm(self) -> None:
        """The tree MiniMax Code reads first inside this repo must say MM."""
        path = Path(ROOT, ".minimax", "skills", "board-ops", "SKILL.md")
        self.assertTrue(path.is_file(), f"{path} missing; re-render with: {self.FIX}")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Board CLI on Linear (MM)", text)
        for wanted in (
            "`[MM·session8]`",
            "minimax/",
            "mm-bot@simplewithus.zulipchat.com",
            "MM_ZULIP_KEY",
        ):
            self.assertIn(wanted, text, wanted)
        for foreign in ("CLAUDE", "CURSOR", "CLAUDE_ZULIP_KEY", "`[CLAUDE·session8]`"):
            self.assertNotIn(foreign, text, foreign)
        self.assertIn("minimax/afc-123-zulip-cutover", text)
        self.assertNotIn("claude/afc-123-zulip-cutover", text)

    def test_tracked_platform_copies_match_fresh_render(self) -> None:
        stale: list[str] = []
        for dest, seat in repo_platform_copies(ROOT):
            for name in catalog_skill_names(DOCS):
                if name in FROZEN_RENDERS or not skill_allowed_for_seat(name, seat):
                    continue
                path = Path(dest, name, "SKILL.md")
                want = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                have = path.read_text(encoding="utf-8") if path.is_file() else None
                if have != want:
                    stale.append(os.path.relpath(str(path), ROOT))
        self.assertEqual(stale, [], f"stale platform copies; re-render with: {self.FIX}")

    def test_platform_copies_carry_no_never_install_skill(self) -> None:
        for dest, _seat in repo_platform_copies(ROOT):
            for name in NEVER_INSTALL:
                self.assertFalse(Path(dest, name).exists(), f"{dest}/{name} must not exist")

    def test_platform_copies_do_not_teach_retired_seat_flat_lane(self) -> None:
        """The old session-start text told readers to add a flat monet lane."""
        for dest, _seat in repo_platform_copies(ROOT):
            text = Path(dest, "session-start", "SKILL.md").read_text(encoding="utf-8")
            self.assertNotRegex(text, r"worktree add -b \S+ ~/apps/<prefix>-monet", dest)
            self.assertIn("lane new", text, dest)


def _load_installer():
    path = os.path.join(ROOT, "scripts", "install-fleet-skills.py")
    spec = importlib.util.spec_from_file_location("install_fleet_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AlwaysOnRuleFileTests(unittest.TestCase):
    """The always-on rule files a tool home reads are installed from the repo.

    Without this, the live copy of a repo-tracked rule file drifts silently and a
    seat reads the stale one (the sentence-gap rule sat a month behind the repo).
    """

    def setUp(self) -> None:
        self.installer = _load_installer()

    def test_every_repo_rule_file_is_mapped(self) -> None:
        repo_rules = sorted(
            os.path.relpath(os.path.join(root, name), ROOT)
            for root, _dirs, files in os.walk(os.path.join(ROOT, ".cursor", "rules"))
            for name in files
            if name.endswith(".mdc")
        )
        self.assertTrue(repo_rules, "no repo cursor rules found to check")
        mapped = {rel for rules in self.installer.ALWAYS_ON_RULE_FILES.values() for rel in rules}
        for rel in repo_rules:
            self.assertIn(rel, mapped, f"{rel} is not installed to a tool home")

    def test_the_installed_gap_rule_is_the_current_one(self) -> None:
        rel = ".cursor/rules/sentence-gap.mdc"
        mapped = {rule for rules in self.installer.ALWAYS_ON_RULE_FILES.values() for rule in rules}
        self.assertIn(rel, mapped)
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("Current rule (owner ruling 2026-10-23)", body)
        # The retired per-surface table: two literal ASCII spaces for files,
        # commits, terminals and untested surfaces.
        self.assertNotIn("| two literal ASCII spaces |", body)
        # The entity belongs to the two exception panes and nowhere else.
        self.assertNotIn("Any other agent chat pane that renders Markdown", body)

    def _install_with_home(self, home: str) -> list[str]:
        env_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            return self.installer._install_always_on_rules()
        finally:
            if env_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = env_home

    def test_the_copy_lands_at_the_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            # The tool home has to exist already:  the install is gated on it.
            for tool_home in self.installer.ALWAYS_ON_RULE_FILES:
                os.makedirs(os.path.join(home, tool_home.replace("~/", "", 1)), exist_ok=True)
            written = self._install_with_home(home)
            self.assertTrue(written, "nothing was installed")
            for rules in self.installer.ALWAYS_ON_RULE_FILES.values():
                for src_rel, dest_rel in rules.items():
                    path = os.path.join(home, dest_rel.replace("~/", "", 1))
                    self.assertTrue(os.path.isfile(path), path)
                    src = os.path.join(ROOT, src_rel)
                    with open(path, "rb") as a, open(src, "rb") as b:
                        self.assertEqual(a.read(), b.read())

    def test_a_tool_home_that_is_not_there_is_not_created(self) -> None:
        # tool_home_exists() reads the tool home to decide whether the tool is installed
        # at all, so creating an empty one here would flip the next run's answer and hand
        # a host a tool it never had.
        with tempfile.TemporaryDirectory() as home:
            written = self._install_with_home(home)
            self.assertEqual(written, [], "a rule was installed into a home that was not there")
            for tool_home in self.installer.ALWAYS_ON_RULE_FILES:
                path = os.path.join(home, tool_home.replace("~/", "", 1))
                self.assertFalse(os.path.exists(path), f"{tool_home} was created")


class InstallerRepoOnlyTests(unittest.TestCase):
    """install-fleet-skills.py --repo-only touches the repo and never HOME."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        self.home = base / "home"
        docs = self.repo / "docs" / "fleet-skills"
        docs.mkdir(parents=True)
        for name in ("session-start", "board-ops"):
            shutil.copytree(Path(DOCS, name), docs / name)
        # Tool homes exist, so a full install would write into them.
        for tool in (".claude", ".cursor", ".grok", ".fx"):
            (self.home / tool).mkdir(parents=True)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.installer = _load_installer()
        self.installer.REPO_ROOT = str(self.repo)
        self.installer.DOCS_SKILLS = str(docs)
        self.installer.ROOT_SKILLS = str(self.repo / "skills")
        self.installer.BY_SEAT = str(docs / "by-seat")

    def _restore_home(self) -> None:
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

    def _run(self, argv: list[str]) -> str:
        # Refuse to run against anything but the fake home.
        self.assertEqual(os.path.expanduser("~"), str(self.home))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.installer.main(argv)
        return out.getvalue()

    def _home_files(self) -> list[str]:
        return sorted(
            os.path.relpath(os.path.join(d, f), self.home)
            for d, _dirs, files in os.walk(self.home)
            for f in files
        )

    def test_purge_removes_a_skill_left_under_a_renamed_old_name(self) -> None:
        """TEMPORARY with RENAMED_SKILL_OLD_NAMES:  delete this test and the next one when the rename is complete."""
        from fleet_skill_identity import RENAMED_SKILL_OLD_NAMES

        old = "agent-sync-tools-guide"
        self.assertIn(old, RENAMED_SKILL_OLD_NAMES)
        dest = self.home / ".claude" / "skills"
        (dest / old).mkdir(parents=True)
        (dest / old / "SKILL.md").write_text("stale\n", encoding="utf-8")
        (dest / f"{old}.zip").write_bytes(b"stale")
        keep = dest / "session-start"
        keep.mkdir(parents=True)
        (keep / "SKILL.md").write_text("keep\n", encoding="utf-8")
        self.installer._purge_retired(str(dest), install_target=True)
        self.assertFalse((dest / old).exists())
        self.assertFalse((dest / f"{old}.zip").exists())
        self.assertTrue((keep / "SKILL.md").is_file())

    def test_a_renamed_name_is_never_purged_from_the_source_pack(self) -> None:
        """TEMPORARY with RENAMED_SKILL_OLD_NAMES.  Removing a renamed skill from the repo is a git operation."""
        old = "agent-sync-tools-guide"
        docs = self.repo / "docs" / "fleet-skills"
        (docs / old).mkdir(parents=True)
        (docs / old / "SKILL.md").write_text("local edit\n", encoding="utf-8")
        self.installer._purge_retired(str(docs))
        self.assertTrue((docs / old / "SKILL.md").is_file())
        names = catalog_skill_names(str(docs))
        self.assertNotIn(old, names)

    def test_the_live_pack_exposes_the_new_guide_name_only(self) -> None:
        """TEMPORARY with RENAMED_SKILL_OLD_NAMES."""
        names = catalog_skill_names(DOCS)
        self.assertIn("simple-tools-guide", names)
        self.assertNotIn("agent-sync-tools-guide", names)

    def test_repo_only_renders_platform_copies_and_skips_home(self) -> None:
        out = self._run(["--repo-only"])
        for tree in (".claude", ".cursor", ".grok"):
            self.assertTrue(
                (self.repo / tree / "skills" / "session-start" / "SKILL.md").is_file(),
                tree,
            )
        self.assertTrue((self.repo / "skills" / "session-start" / "SKILL.md").is_file())
        self.assertEqual(self._home_files(), [])
        self.assertIn("Home-dir installs: skipped (--repo-only)", out)
        self.assertNotIn("fx-scanned", out)

    def test_repo_only_copies_equal_a_fresh_render(self) -> None:
        self._run(["--repo-only"])
        for dest, seat in repo_platform_copies(str(self.repo)):
            for name in ("session-start", "board-ops"):
                want = specialize_from_monet(_load_skill(name), seat, skill_name=name)
                got = Path(dest, name, "SKILL.md").read_text(encoding="utf-8")
                self.assertEqual(got, want, f"{dest}/{name}")

    def test_default_mode_still_installs_into_home(self) -> None:
        # Every write target must resolve inside the fake home, so this test
        # can never reach the real one (an absolute dest would escape it).
        home = str(self.home) + os.sep
        for dest, _seat in platform_installs():
            self.assertTrue(dest.startswith(home), dest)
        for raw in self.installer.FX_SCAN_ROOTS:
            self.assertTrue(os.path.expanduser(raw).startswith(home), raw)
        out = self._run([])
        self.assertIn("Home-dir installs:\n", out)
        self.assertNotIn("--repo-only", out)
        self.assertTrue(self._home_files(), "default mode must write tool homes")

    def test_default_mode_never_writes_retired_or_catalog_only_homes(self) -> None:
        # Make every one of those homes' parent exist, so only the seat table,
        # not a missing folder, can keep the installer out of them.
        for parent in (".deepseek", ".kimi", ".renoir", "Desktop"):
            (self.home / parent).mkdir(parents=True)
        self._run([])
        wrote = [
            f
            for f in self._home_files()
            if f.startswith((".deepseek/", ".kimi/", ".renoir/", "Desktop/", ".clutch/"))
        ]
        self.assertEqual(wrote, [])
        self.assertTrue((self.home / ".claude" / "skills" / "session-start" / "SKILL.md").is_file())
        shared = (self.home / ".claude" / "skills" / "session-start" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(_arm("CLAUDE"), shared)
        self.assertIn(_ordinary("Claude", "CLAUDE"), shared)
        self.assertNotIn("MONET, CLAUDE, or RENOIR", shared)

    def test_clutch_home_is_written_only_when_the_engine_home_exists(self) -> None:
        # No ~/.clutch/dsh: Clutch is not installed, so no folder is made for it.
        self._run([])
        self.assertEqual([f for f in self._home_files() if f.startswith(".clutch/")], [])
        # With the engine home present, the pack lands in its skill root and is Clutch's own voice.
        (self.home / ".clutch" / "dsh").mkdir(parents=True)
        out = self._run([])
        self.assertIn(".clutch/dsh/skills", out)
        got = (self.home / ".clutch" / "dsh" / "skills" / "session-start" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(_arm("CLUTCH"), got)
        self.assertIn("Runtime (Clutch)", got)
        self.assertTrue(got.startswith("---\nname: session-start\n"))
        self.assertFalse((self.home / ".clutch" / "dsh" / "skills" / "ios-ship").exists())

    def test_unchanged_pack_zip_is_not_rewritten(self) -> None:
        self._run(["--repo-only"])
        zpath = Path(self.installer.DOCS_SKILLS, "session-start.zip")
        self.assertTrue(zpath.is_file())
        os.utime(zpath, (1, 1))
        before = zpath.read_bytes()
        self._run(["--repo-only"])
        self.assertEqual(zpath.read_bytes(), before)
        self.assertEqual(int(zpath.stat().st_mtime), 1)
        with zipfile.ZipFile(zpath) as zf:
            self.assertEqual(zf.namelist(), ["session-start/SKILL.md"])

    def test_changed_pack_zip_is_rewritten(self) -> None:
        self._run(["--repo-only"])
        md = Path(self.installer.DOCS_SKILLS, "session-start", "SKILL.md")
        md.write_text(md.read_text(encoding="utf-8") + "\nextra line\n", encoding="utf-8")
        self._run(["--repo-only"])
        with zipfile.ZipFile(Path(self.installer.DOCS_SKILLS, "session-start.zip")) as zf:
            self.assertIn(b"extra line", zf.read("session-start/SKILL.md"))


class BranchPrefixMatchesLaneNewTests(unittest.TestCase):
    """`lane new` names the branch <first registry branchPrefix><slug>.

    A skill that lists another prefix for a seat sends readers to a branch
    that does not match the lane they just created.
    """

    def _registry_first_prefix(self, tag: str) -> str:
        import json

        data = json.loads(Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8"))
        row = next(r for r in data["seats"] if r["tag"] == tag)
        return row["branchPrefixes"][0]

    def test_coordination_skill_leads_with_registry_prefix_for_ag(self) -> None:
        prefix = self._registry_first_prefix("AG")
        self.assertEqual(prefix, "ag/")
        text = _load_skill("fleet-coordination")
        line = next(l for l in text.splitlines() if l.strip().startswith("- Antigravity / Gemini:"))
        self.assertIn(f"prefix `{prefix}`", line)
        # The bare "`agent/` or `ag/`" pairing offered two valid prefixes.
        self.assertNotIn("`agent/` or `ag/`", line)

    def test_template_agents_pairs_lane_new_with_seat_prefix_branch(self) -> None:
        text = Path(ROOT, "TEMPLATE-AGENTS.md").read_text(encoding="utf-8")
        paragraphs = text.split("\n\n")

        def para(marker: str) -> str:
            hits = [p for p in paragraphs if marker in p]
            self.assertEqual(len(hits), 1, marker)
            return hits[0]

        launch_block = para("Launch yourself in your own lane")
        start = launch_block.index("- **Launch yourself in your own lane")
        launch = launch_block[start : launch_block.index("\n- **", start + 1)]
        self.assertNotIn("`agent/<name>`", launch)
        self.assertIn("<seat prefix>/<slug>", launch)

        cursor = para("lanes/Socratic-Trade/cursor-<slug>")
        self.assertIn("`cursor/<slug>`", cursor)
        # The new lane's branch is not the legacy standing lane's branch.
        self.assertNotIn("cursor-<slug>`), on its own branch (`agent/cursor`)", cursor)


class LinearTeamListTests(unittest.TestCase):
    """The Linear team list in the skills must match the board's own routing table.

    It is a literal, because a static skill must not shell out to the board CLI at
    read time.  The literal is therefore anchored here to `TEAM_KEYS` in
    scripts/linear-board/board, so adding a team to the board without updating the
    skill fails this test instead of silently rotting again (FL and CC went missing
    this way;  FL is FleetLink, CC is CodeCaps, both live 2026-10-11).
    """

    # A run of uppercase team acronyms, however each file chooses to introduce it.
    # Three or more, so single all-caps words like API or CLI never match.
    # Acronyms may be longer than three letters (CLUTCH is six).
    LIST = re.compile(r"\b[A-Z]{1,}(?:,\s*[A-Z]{1,}){2,}\b")

    def _team_keys(self) -> tuple[str, ...]:
        # Parse just the assignment:  importing the board would pull in its
        # credential setup, and a test must not need a Linear login to run.
        path = Path(ROOT, "scripts", "linear-board", "board")
        match = re.search(
            r"(?m)^TEAM_KEYS = (\(.*?\))\s*$", path.read_text(encoding="utf-8"), re.DOTALL
        )
        self.assertIsNotNone(match, "TEAM_KEYS not found in scripts/linear-board/board")
        return tuple(ast.literal_eval(match.group(1)))

    def _sources(self) -> list[Path]:
        #  Only files this repo owns.  FLEET-PROTOCOL.md is deliberately absent:  it
        #  is a generated mirror of the Fleet-OPS handbook, check-generated-mirror.py
        #  refuses an edit here, and its copy of the team list is fixed where it is
        #  generated rather than in this tree.
        return [
            Path(ROOT, "docs", "fleet-skills", "board-ops", "SKILL.md"),
            Path(ROOT, "docs", "protocols", "linear-migration.md"),
        ]

    def _assert_list(self, path: Path, want: str) -> None:
        text = path.read_text(encoding="utf-8")
        lists = {m.group(0) for m in self.LIST.finditer(text)}
        self.assertIn(
            want,
            lists,
            f"{path} has no team list matching the board's TEAM_KEYS;  found {sorted(lists)[:4]}",
        )

    def test_every_source_carries_the_boards_team_keys(self) -> None:
        want = ", ".join(self._team_keys())
        for path in self._sources():
            self._assert_list(path, want)

    def test_every_rendered_copy_carries_the_same_list(self) -> None:
        want = ", ".join(self._team_keys())
        roots = [Path(ROOT, "skills"), Path(ROOT, "docs", "fleet-skills", "by-seat")]
        roots += [Path(dest) for dest, _seat in repo_platform_copies(ROOT)]
        checked = 0
        for root in roots:
            for path in sorted(root.rglob("board-ops/SKILL.md")):
                self._assert_list(path, want)
                checked += 1
        self.assertGreater(checked, 10, "expected the team list in every rendered copy")


class AgentSyncRosterTests(unittest.TestCase):
    """The availability roster must agree with the seat table and registry.

    The Oct 7 rewrite replaced the old "## Agent availability" section and the
    bold seat-table rows with "### Availability" (an Available bullet and a
    Retired bullet list) and the "### Active Seats" table.
    """

    def _registry(self) -> list[dict]:
        import json

        data = json.loads(Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8"))
        return data["seats"]

    def _text(self) -> str:
        return _protocol_doc_path().read_text(encoding="utf-8")

    def _section(self, start_marker: str, end_marker: str) -> str:
        text = self._text()
        start = text.index(start_marker)
        return text[start : text.index(end_marker, start + len(start_marker))]

    def _roster(self) -> str:
        return self._section("\n### Availability\n", "\n## ")

    def _active_table_tags(self) -> set[str]:
        table = self._section("\n### Active Seats\n", "\n- **Tags.**")
        return {m.group(1) for m in re.finditer(r"(?m)^\| ([A-Z][A-Z-]*) \|", table)}

    def _available_paragraph(self) -> str:
        roster = self._roster()
        start = roster.index("**Available (normal):**")
        return roster[start : roster.index("\n- **Retired.**", start)]

    def _retired_block(self) -> str:
        roster = self._roster()
        start = roster.index("- **Retired.**")
        return roster[start:]

    def test_active_seats_table_lists_no_retired_seat(self) -> None:
        active = self._active_table_tags()
        retired = {r["tag"] for r in self._registry() if r.get("retired")}
        self.assertEqual(active & retired, set(), active)
        self.assertIn("CLUTCH", active)
        self.assertIn("CLAUDE", active)

    def test_available_list_has_no_retired_seat_and_names_clutch(self) -> None:
        before = self._available_paragraph()
        after = self._retired_block()
        retired = [r["tag"] for r in self._registry() if r.get("retired")]
        for tag in retired:
            self.assertNotRegex(before, rf"\b{re.escape(tag)}\b", tag)
        self.assertRegex(before, r"\bCLUTCH\b")
        for tag in ("MONET", "RENOIR", "HARNESS"):
            self.assertRegex(after, rf"\b{tag}\b", tag)

    def test_roster_never_says_renoir_is_a_future_seat(self) -> None:
        self.assertNotIn("future third seat", self._text())
        self.assertIn("RENOIR (owner 2026-10-07)", self._retired_block())

    def test_retired_list_matches_the_seat_table(self) -> None:
        text = self._text()
        active = self._active_table_tags()
        block = self._retired_block()
        for tag in ("MONET", "RENOIR", "HARNESS"):
            self.assertNotIn(tag, active, tag)
            row = next(
                (l for l in block.splitlines() if l.lstrip().startswith(f"- {tag} (")),
                None,
            )
            self.assertIsNotNone(row, f"{tag} missing from the Retired list")
            self.assertIn("2026-10-07", row, tag)
            self.assertNotRegex(text, rf"(?m)^\| {tag} \|", tag)


class NotesTitleArchiveTests(unittest.TestCase):
    def test_committed_notes_archive_matches_canonical_title_source(self):
        source = Path(NOTES).read_bytes()
        with zipfile.ZipFile(Path(ROOT, "docs", "fleet-skills", "apple-notes.zip")) as archive:
            packed = archive.read("apple-notes/SKILL.md")
        self.assertEqual(packed, source)
        self.assertIn(b"[TEAM_KEY, Agent]", packed)
        self.assertIn(b"[ST, CT, Agent]", packed)
        self.assertNotIn(b"[APP, Agent]", packed)


class VMSeatLaneTests(unittest.TestCase):
    def test_vm_steps_replace_mac_lane_commands_and_keep_identity(self):
        for name in ("session-start", "fleet-coordination", "land-lane", "closeout"):
            source = Path(ROOT, "docs", "fleet-skills", name, "SKILL.md").read_text()
            rendered = specialize_from_monet(source, SEATS["muse-assist"], skill_name=name)
            self.assertIn(
                "durable VM checkout" if name in ("land-lane", "closeout") else "durable", rendered
            )
            self.assertNotRegex(rendered, r"(?m)^\s*~/apps/lane (?:new|retire|prune)")
            self.assertNotIn("Make one lane per task", rendered)
            self.assertNotIn("lane.lane new", rendered)
            self.assertNotIn("create the lane by hand", rendered)
            self.assertNotIn("checkout is the lane", rendered)
            self.assertIn("MA", rendered)
        ordinary = specialize_from_monet(_session(), SEATS["codex"], skill_name="session-start")
        self.assertIn("~/apps/lane new", ordinary)

    def test_ma_app_table_keeps_repository_metadata_without_mac_paths(self):
        rendered = specialize_from_monet(
            _load_skill("session-start"), SEATS["muse-assist"], skill_name="session-start"
        )
        table = [line for line in rendered.splitlines() if line.startswith("|")]
        self.assertTrue(any("AI-Fleet-Coordinator" in row and "AFC" in row for row in table))
        self.assertFalse(any("~/apps/lanes/" in row for row in table))
        self.assertFalse(any("Lane folder" in row for row in table))

    def test_ma_claims_use_cloud_and_actual_durable_checkout(self):
        for name in ("session-start", "fleet-coordination", "board-ops"):
            rendered = specialize_from_monet(
                _load_skill(name), SEATS["muse-assist"], skill_name=name
            )
            self.assertNotIn("--env Mac", rendered, name)
            self.assertNotIn('where "~/apps/lanes/', rendered, name)
            self.assertIn("--env cloud", rendered, name)
            for location in re.findall(r'--where "([^"\n]*)"', rendered):
                self.assertEqual(location, "$(pwd -P) @ $(git branch --show-current)", name)
        ordinary = specialize_from_monet(
            _load_skill("board-ops"), SEATS["codex"], skill_name="board-ops"
        )
        self.assertIn("--env Mac", ordinary)

    def test_ma_unstick_keeps_diagnostics_without_mac_lane_lookup(self):
        rendered = specialize_from_monet(
            _load_skill("unstick-pr"), SEATS["muse-assist"], skill_name="unstick-pr"
        )
        self.assertNotIn("~/apps/lane path", rendered)
        self.assertIn('cd "<durable VM checkout path>"', rendered)
        self.assertIn("git merge-tree --write-tree", rendered)
        self.assertIn("DealDex's hosted Actions ship stays", rendered)

    def test_mac_sweep_launcher_is_not_installed_to_ma(self):
        self.assertFalse(skill_allowed_for_seat("pr-sweep", SEATS["muse-assist"]))
        for key, seat in SEATS.items():
            if seat.tag != "MA":
                self.assertTrue(skill_allowed_for_seat("pr-sweep", seat), key)

    def test_every_installed_ma_skill_avoids_mac_lane_invocations(self):
        docs = Path(ROOT, "docs", "fleet-skills")
        for name in catalog_skill_names(str(docs)):
            if not skill_allowed_for_seat(name, SEATS["muse-assist"]):
                continue
            rendered = specialize_from_monet(
                _load_skill(name), SEATS["muse-assist"], skill_name=name
            )
            self.assertNotIn("~/apps/lane ", rendered, name)


def _tracked_files() -> list[str]:
    """Every tracked file in the repository, so a guard can see all of them.

    The git call carries a timeout and its own exit codes:  a hung git is 124, a
    missing git is 127 and a git that refuses is 128, so a caller can tell the
    three apart instead of seeing one generic failure.  The call is made at
    import time on purpose:  an exit code raised inside a test method is caught
    by the test runner and reported as an ordinary error, which is exactly the
    indistinguishability this is here to remove.
    """
    try:
        done = subprocess.run(
            ["git", "-C", ROOT, "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(124)
    except FileNotFoundError:
        raise SystemExit(127)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(128) from exc
    return [p for p in done.stdout.split("\0") if p]


TRACKED_FILES = _tracked_files()


class PrivateInventoryTests(unittest.TestCase):
    """AFC-504: the published copies name where the private inventory lives.

    They never carry the values.  The patterns below are the shapes the fleet's own
    internal addressing uses, so a copy that grows one is caught whatever host it
    names, and this test source never carries an address itself.

    The alias of the origin box is found the same way.  The guard used to hold
    its value in a pattern, which put the value in the indexed test source; it
    now holds the shapes a private host travels in, so the source carries none
    and the guard still catches the value in whatever file it reaches.

    The guard reads every tracked text file, not only the published copies: a guard
    that cannot see a file gives false assurance.  The files that still carry the
    inventory on purpose are listed with the reason and the board item that owns the
    decision to move them, so the list is the fleet's own published inventory of
    where its private inventory lives, and it shrinks as AFC-505 is worked through.
    """

    # Tailscale hands every node an address in the CGNAT range 100.64.0.0/10.
    MESH_IP = re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")
    # MagicDNS names end in the tailnet domain.
    MAGICDNS = re.compile(r"\b[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.ts\.net\b")
    # The private host of the origin box, found by shape and never by value.
    # An ssh target is written as a user and a host, and a file that keeps a
    # host in a variable says so in the name of the variable.  These patterns
    # carry no host of their own, so the guard source never carries the
    # inventory it looks for.
    ALIAS_ROOT_AT = re.compile(r"\broot@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)")
    ALIAS_NAMED = re.compile(
        r"""(?i:\b(?:ssh[_ -]?alias|host[_ -]?alias)\b[ \t]*(?:=|:=|:|\bis\b)[ \t]*"""
        r"""["'](?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)["'])"""
    )
    # A variable whose name says it holds a host or an alias.  The name is
    # upper case because a default lives in a constant, and the value is a
    # single label because an ssh alias is one.
    HOST_NAME = re.compile(r"^(?:[A-Z0-9]+_)*(?:SSH_)?(?:HOST|HOSTNAME|ALIAS|SSH_ALIAS)$")
    HOST_ASSIGNMENT = re.compile(
        r"""^[ \t]*(?:export[ \t]+|declare[ \t]+-\w+[ \t]+|local[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"""
        r"""[ \t]*=[ \t]*(?P<value>"[^"]*"|'[^']*'|[^\s#]+)"""
    )
    HOST_ENV_DEFAULT = re.compile(
        r"""os\.environ\.get\(\s*["'](?P<name>[A-Za-z_][A-Za-z0-9_]*)["']\s*,\s*"""
        r"""(?P<value>"[^"]*"|'[^']*')"""
    )
    # A single label, which is the shape of an ssh alias.
    SINGLE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    # The names and the addresses reserved for documentation.  They carry no
    # inventory, and the house convention in a fixture is one of them.
    RESERVED_HOST = re.compile(
        r"^(?:localhost|local|invalid|example|example\.(?:com|net|org|test))$", re.IGNORECASE
    )
    DOC_ADDRESS = re.compile(r"^(?:192\.0\.2\.\d{1,3}|198\.51\.100\.\d{1,3}|203\.0\.113\.\d{1,3})$")
    LOOPBACK = re.compile(r"^(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|::1)$")
    # The ssh command forms a shell line can take.  A prose file only counts a
    # command that starts its line, because a paragraph that mentions ssh is
    # not a command.  A code file also counts a command after a shell keyword
    # or an operator, so `if ssh ...` and `$(ssh ...)` are commands there too.
    SSH_COMMAND_PROSE = re.compile(r"^[ \t]*(?P<ssh>ssh[ \t]+)(?![=])")
    SSH_COMMAND_CODE = re.compile(
        r"(?:^[ \t]*(?:(?:if|then|else|while|until|do|!)[ \t]+)?"
        r"|(?:&&|\|\||[;&|]|\$\(|`|\{)[ \t]*)"
        r"(?P<ssh>ssh[ \t]+)(?![=])"
    )
    # A prose file is a document, so a command in one has to start its line.
    PROSE_SUFFIXES = (".md", ".txt", ".rst")
    PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>")
    # The ssh options that take a value in the next token, so a host search
    # steps over the option and its value instead of taking one of them.
    SSH_OPTIONS_WITH_VALUE = {
        "-b",
        "-c",
        "-D",
        "-E",
        "-e",
        "-F",
        "-I",
        "-i",
        "-J",
        "-L",
        "-l",
        "-m",
        "-o",
        "-O",
        "-p",
        "-Q",
        "-R",
        "-S",
        "-W",
        "-w",
    }
    # A shell assignment, so a target hoisted into a variable is still a target.
    ASSIGNMENT = re.compile(
        r"^[ \t]*(?:export[ \t]+|declare[ \t]+-\w+[ \t]+|local[ \t]+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s#]+)"
    )

    # The files that still carry a private address on purpose.  Each one names
    # the reason, so the list reads as an inventory and not as a backlog of
    # accidents.
    ADDRESS_ALLOWLIST = {
        "docs/EFFORT-LOG.md": "history: a record of what was done, never rewritten",
        "docs/MAC-LOCAL-PROCESSES.md": "AFC-505: the live inventory of Mac local processes",
        "docs/operator-listener-hosting.md": "AFC-505: the hosting table names a live address",
        "docs/RAG-FLEET-INFRA.md": "AFC-505: the service endpoint table names a live address",
        "docs/reviews/2026-09-14-fleet-rag-bm25-reranker-ab.md": "history: a review record",
        "docs/rollouts/2026-09-01-dsh-tailscale-shellular-thinking.md": "history: a rollout record",
        "docs/rollouts/2026-09-02-fleet-rag-memory-platform.md": "history: a rollout record",
        "scripts/agent_sync/tests/test_wake_alerter.py": "AFC-505: a fixture that names the address it alerts on",
        "scripts/fleet_config/tests/test_compliance.py": "AFC-400: a fixture for the internal address shape the rule looks for",
        "scripts/agent_sync/wake_alerter.py": "AFC-505: a live script that reads the address it alerts on",
        "scripts/fleet-recall-service/.env.example": "AFC-505: the service example names the address it points at",
        "scripts/fleet-recall-service/README.md": "AFC-505: the service readme names the address it points at",
        "scripts/fleet-recall-service/compose.example.yaml": "AFC-505: the service example names the address it points at",
        "scripts/fleet_rag/public_fallback.py": "AFC-505: a live fallback that reaches the service by address",
        "scripts/fleet_rag/tests/test_public_fallback.py": "AFC-505: a fixture for the fallback above",
        "scripts/hetzner/fleet-qdrant-health.sh": "AFC-505: a live health script that reaches the box by address",
        "scripts/hetzner/fleet-qdrant-snapshot.sh": "AFC-505: a live snapshot script that reaches the box by address",
        "scripts/recall-tunnel": "AFC-505: a live tunnel that reaches the box by address",
    }

    # The files that still carry a private host on purpose, for the same
    # reason.  The guard finds each of them by shape, so an entry earns its
    # place while the file still trips one of those shapes.
    ALIAS_ALLOWLIST = {
        "docs/MAC-LOCAL-PROCESSES.md": "AFC-505: the live inventory of Mac local processes",
        "docs/protocols/coolify-fleet-cli-sync.md": "AFC-505: the protocol for the sync that reaches the box",
        "fleet-sentry-monitor/monitor.py": "AFC-505: a live monitor that defaults to the alias",
        "scripts/agent_sync/muse_bridge_mac.py": "AFC-505: the live bridge that defaults to the alias",
        "scripts/fleet-cli-sync.py": "AFC-505: a live sync that names the receiver it ships to",
        "scripts/fleet_rag/doctor.py": "AFC-505: a live doctor that defaults to the alias",
        "scripts/recall-tunnel": "AFC-505: a live tunnel that reaches the box by alias and address",
        "scripts/test_fleet_cli_sync.py": "AFC-505: a fixture that asserts the receiver the sync names",
    }

    # The files that may carry a literal ssh target on purpose.  A fixture that
    # names a command has to name it literally to mean anything, and the house
    # convention is an RFC 5737 documentation address for exactly that.
    SSH_ALLOWLIST = {
        "scripts/fleet_lanes/tests/fixtures_guard.py": "a fixture that names the command shape it allows",
    }

    def _published_skill_files(self) -> list[Path]:
        roots = [Path(DOCS), Path(ROOT, "skills")]
        for dest, _seat in repo_platform_copies(ROOT):
            roots.append(Path(dest))
        files: list[Path] = []
        for base in roots:
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("SKILL.md")):
                files.append(path)
        return files

    def _tracked_text_files(self) -> list[str]:
        """Every tracked file that reads as text, so the guard sees the repository.

        The list comes from git rather than from a glob, so a file with no
        extension is scanned like any other.
        """
        this_file = os.path.relpath(os.path.abspath(__file__), ROOT)
        files: list[str] = []
        for rel in sorted(TRACKED_FILES):
            if rel == this_file:
                # This source carries the patterns themselves.
                continue
            try:
                Path(ROOT, rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            files.append(rel)
        return files

    def _logical_lines(self, text: str) -> list[tuple[int, str]]:
        """The text as logical lines, with a backslash continuation joined on.

        A command split over two lines is one command, and a target that sits on
        the second line is still a target.
        """
        out: list[tuple[int, str]] = []
        lines = text.split("\n")
        number = 0
        index = 0
        while index < len(lines):
            number += 1
            line = lines[index]
            while line.rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                line = line.rstrip()[:-1] + " " + lines[index].strip()
            out.append((number, line))
            index += 1
        return out

    def _tokens(self, line: str) -> list[str]:
        """The line's tokens, with a quoted run kept as one token.

        A quoted run stays whole, so an option value with spaces in it does
        not come apart and turn into a host.
        """
        out: list[str] = []
        current: list[str] = []
        quote: str | None = None
        for char in line:
            if quote is None:
                if char in "\"'":
                    quote = char
                    current.append(char)
                    continue
                if char.isspace():
                    if current:
                        out.append("".join(current))
                        current = []
                    continue
                current.append(char)
                continue
            current.append(char)
            if char == quote:
                quote = None
        if current:
            out.append("".join(current))
        return out

    def _ssh_commands(self, rel: str, line: str) -> list[str]:
        """The ssh commands on a line, each as its text from the word ssh on.

        A prose file only counts a command that starts its line, because a
        paragraph that mentions ssh is not a command.  A code file also counts
        a command after a shell keyword or an operator, so `if ssh ...` and
        `$(ssh ...)` are commands there too.
        """
        pattern = (
            self.SSH_COMMAND_PROSE if rel.endswith(self.PROSE_SUFFIXES) else self.SSH_COMMAND_CODE
        )
        return [line[match.start("ssh") :] for match in pattern.finditer(line)]

    def _ssh_host(self, command: str) -> str:
        """The host an ssh command names, or an empty string when it names none.

        The options and their values come off first, so the first token left is
        the host and not an option.
        """
        tokens = self._tokens(command)
        if not tokens or tokens[0] != "ssh":
            return ""
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token in self.SSH_OPTIONS_WITH_VALUE:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            return token.strip("\"'")
        return ""

    def _literal_variables(self, text: str) -> dict[str, str]:
        """The variables a file gives a literal value, so a target hoisted into
        one is a target in disguise.

        A variable that carries a variable or a placeholder is resolved at
        runtime and stays out of this list.
        """
        out: dict[str, str] = {}
        for line in text.split("\n"):
            match = self.ASSIGNMENT.match(line)
            if not match:
                continue
            name = match.group(1)
            value = match.group(2).strip("\"'")
            if "$" in value or self.PLACEHOLDER.search(value):
                continue
            out[name] = value
        return out

    def test_published_skill_copies_carry_no_private_addresses(self) -> None:
        files = self._published_skill_files()
        self.assertTrue(files, "no published skill copy was found to scan")
        hits: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in (self.MESH_IP, self.MAGICDNS):
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    hits.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(
            hits,
            [],
            "a published skill copy must name the private inventory, not carry it:\n"
            + "\n".join(hits),
        )

    def _address_hits(self, rel: str, text: str) -> list[str]:
        """The private addresses a file carries."""
        hits: list[str] = []
        for pattern in (self.MESH_IP, self.MAGICDNS):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel}:{line}")
        return hits

    def _reserved_host(self, value: str) -> bool:
        """Whether a value is a name or an address reserved for documentation."""
        return bool(
            self.RESERVED_HOST.match(value)
            or self.DOC_ADDRESS.match(value)
            or self.LOOPBACK.match(value)
        )

    def _host_literals(self, text: str) -> list[tuple[int, str, str]]:
        """The variables whose name says they hold a host, with the value.

        The value is a literal, because a variable that carries a variable or
        a placeholder is resolved at runtime and is not inventory.
        """
        out: list[tuple[int, str, str]] = []
        for number, line in enumerate(text.split("\n"), 1):
            for pattern in (self.HOST_ASSIGNMENT, self.HOST_ENV_DEFAULT):
                for match in pattern.finditer(line):
                    name = match.group("name")
                    if not self.HOST_NAME.match(name):
                        continue
                    value = match.group("value").strip("\"'")
                    if "$" in value or "<" in value:
                        continue
                    if not self.SINGLE_LABEL.match(value):
                        continue
                    out.append((number, name, value))
        return out

    def _alias_hits(self, rel: str, text: str) -> list[str]:
        """The places a file carries a host in the shape of private inventory.

        The shapes are the ones in which a private host reaches a file: an ssh
        target written as a user and a host, and a variable whose name says it
        holds a host given a literal value.  Neither shape names a host, so
        this source carries no inventory of its own, and a copy of the guard
        in a published pack is as safe as the tree.
        """
        hits: list[str] = []
        for pattern in (self.ALIAS_ROOT_AT, self.ALIAS_NAMED):
            for match in pattern.finditer(text):
                if self._reserved_host(match.group("host")):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel}:{line} (an ssh target written as a host)")
        for number, name, value in self._host_literals(text):
            if self._reserved_host(value):
                continue
            hits.append(f"{rel}:{number} ({name} holds a literal host)")
        return hits

    def _ssh_hits(self, rel: str, text: str) -> list[str]:
        """The ssh commands in a file whose target is a literal.

        A target that arrives in the environment is fine, and so is a target
        that is hoisted into a variable the file itself gives a literal value:
        that is a literal in disguise, so it counts.
        """
        hits: list[str] = []
        literals = self._literal_variables(text)
        for number, line in self._logical_lines(text):
            if line.strip().startswith("#"):
                # A comment explains a command, it is not one.
                continue
            for command in self._ssh_commands(rel, line):
                host = self._ssh_host(command)
                if not host or self.PLACEHOLDER.search(host):
                    continue
                variables = re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", host)
                if not variables:
                    hits.append(f"{rel}:{number} (an ssh line with a literal target)")
                    continue
                hoisted = sorted(v for v in variables if v in literals)
                if hoisted:
                    hits.append(
                        f"{rel}:{number} (an ssh target hoisted into " + ", ".join(hoisted) + ")"
                    )
        return hits

    def _zip_members(self) -> list[tuple[str, str, str]]:
        """Every member of every published pack archive, with its archive, name
        and text.

        The archives are what a seat installs, so a guard that opens only the
        files in the tree cannot see what the tree ships.  A stale archive
        carries the text the tree no longer has, which is exactly the exposure
        this guard has to catch.
        """
        out: list[tuple[str, str, str]] = []
        for path in sorted(Path(DOCS).glob("*.zip")):
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    try:
                        text = archive.read(name).decode("utf-8")
                    except (UnicodeDecodeError, KeyError):
                        continue
                    out.append((path.name, name, text))
        return out

    def test_no_published_pack_archive_carries_private_inventory(self) -> None:
        hits: list[str] = []
        for archive, member, text in self._zip_members():
            label = f"{archive}::{member}"
            for hit in self._address_hits(member, text):
                hits.append(hit.replace(member, label, 1))
            for hit in self._alias_hits(member, text):
                hits.append(hit.replace(member, label, 1))
            for hit in self._ssh_hits(member, text):
                hits.append(hit.replace(member, label, 1))
        self.assertEqual(
            hits,
            [],
            "a published pack archive must not carry private inventory, because "
            "the archive is what a seat installs:\n" + "\n".join(hits),
        )

    def test_no_tracked_file_carries_a_private_address(self) -> None:
        hits: list[str] = []
        for rel in self._tracked_text_files():
            if rel in self.ADDRESS_ALLOWLIST:
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            hits.extend(self._address_hits(rel, text))
        self.assertEqual(
            hits,
            [],
            "a tracked file must not carry a private address; the address is "
            "operator inventory, and the files that keep it on purpose are listed "
            "in ADDRESS_ALLOWLIST:\n" + "\n".join(hits),
        )

    def test_no_tracked_file_carries_the_ssh_alias(self) -> None:
        hits: list[str] = []
        for rel in self._tracked_text_files():
            if rel in self.ALIAS_ALLOWLIST:
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            hits.extend(self._alias_hits(rel, text))
        self.assertEqual(
            hits,
            [],
            "a tracked file must not carry the ssh alias; the alias is operator "
            "inventory, and the files that keep it on purpose are listed in "
            "ALIAS_ALLOWLIST:\n" + "\n".join(hits),
        )

    def test_no_tracked_file_sshes_a_literal_target(self) -> None:
        hits: list[str] = []
        for rel in self._tracked_text_files():
            if rel in self.ALIAS_ALLOWLIST or rel in self.SSH_ALLOWLIST:
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            hits.extend(self._ssh_hits(rel, text))
        self.assertEqual(
            hits,
            [],
            "a tracked file must not ssh a literal host target; the target arrives "
            "in the environment, and the files that keep a literal on purpose are "
            "listed in ALIAS_ALLOWLIST and SSH_ALLOWLIST:\n" + "\n".join(hits),
        )

    def test_the_allowlists_name_only_files_that_still_carry_the_inventory(self) -> None:
        """An allowlist entry earns its place by still carrying the inventory.

        A file that has been cleaned up must come off the list, so the list
        shrinks as the board items that own each file are worked through
        instead of becoming a blanket that covers everything.
        """
        tracked = set(self._tracked_text_files())
        stale: list[str] = []
        for rel, reason in sorted(self.ADDRESS_ALLOWLIST.items()):
            if not reason.strip():
                stale.append(f"{rel} (no reason given)")
                continue
            if rel not in tracked:
                stale.append(f"{rel} (not a tracked text file)")
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            if not self._address_hits(rel, text):
                stale.append(f"{rel} (no longer carries a private address)")
        for rel, reason in sorted(self.ALIAS_ALLOWLIST.items()):
            if not reason.strip():
                stale.append(f"{rel} (no reason given)")
                continue
            if rel not in tracked:
                stale.append(f"{rel} (not a tracked text file)")
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            if not self._alias_hits(rel, text):
                stale.append(f"{rel} (no longer carries the ssh alias)")
        for rel, reason in sorted(self.SSH_ALLOWLIST.items()):
            if not reason.strip():
                stale.append(f"{rel} (no reason given)")
                continue
            if rel not in tracked:
                stale.append(f"{rel} (not a tracked text file)")
                continue
            text = Path(ROOT, rel).read_text(encoding="utf-8")
            if not self._ssh_hits(rel, text):
                stale.append(f"{rel} (no longer carries a literal ssh target)")
        self.assertEqual(
            stale,
            [],
            "an allowlist entry must name a file that still carries the private "
            "inventory; clean the file up and take the entry off the list:\n" + "\n".join(stale),
        )


class GuardedBlockSyntaxTests(unittest.TestCase):
    """A `:?` guard whose word holds a quote cannot be parsed.

    bash reads the word of a `:?` guard inside the surrounding double quotes,
    so an apostrophe in it opens a single-quoted string that never closes and
    the whole block fails to parse.  The guard line reads like ordinary
    English and the snippet cannot run at all, so the check belongs to the
    pack:  a seat that copies the block gets a syntax error instead of the
    lock the prose describes.
    """

    FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
    GUARD = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^}]*\}")

    def _texts(self):
        for root, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
            for name in files:
                if name.endswith((".md", ".mdc")):
                    path = os.path.join(root, name)
                    try:
                        with open(path, encoding="utf-8") as handle:
                            yield path, handle.read()
                    except (OSError, UnicodeDecodeError):
                        continue

    def test_no_guard_word_holds_a_quote(self) -> None:
        offenders: list[str] = []
        for path, text in self._texts():
            for match in self.GUARD.finditer(text):
                if "'" in match.group(0) or '"' in match.group(0):
                    line = text[:match.start()].count("\n") + 1
                    offenders.append("%s:%d %s" % (os.path.relpath(path, ROOT), line, match.group(0)))
        self.assertEqual(
            offenders,
            [],
            "a `:?` guard word holds a quote, so the block cannot parse:\n" + "\n".join(offenders),
        )

    def test_every_guarded_block_parses(self) -> None:
        broken: list[str] = []
        for path, text in self._texts():
            for block in self.FENCE.findall(text):
                if ":?" not in block:
                    continue
                proc = subprocess.run(["bash", "-n"], input=block, capture_output=True, text=True)
                if proc.returncode != 0:
                    first = proc.stderr.strip().split("\n")[0][:90]
                    broken.append("%s: %s" % (os.path.relpath(path, ROOT), first))
        self.assertEqual(
            broken,
            [],
            "a block with a `:?` guard does not parse:\n" + "\n".join(broken),
        )


if __name__ == "__main__":
    unittest.main()
