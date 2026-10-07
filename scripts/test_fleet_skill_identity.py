#!/usr/bin/env python3
"""Tests for per-seat fleet skill identity specialization."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from fleet_skill_identity import (  # noqa: E402
    COORDINATOR_SELF_ID,
    FORBIDDEN_LOCAL_IOS_SHIP,
    GB_ROLE_TAGS,
    NEVER_INSTALL,
    SEATS,
    catalog_skill_names,
    head_has_fleet_wake,
    is_grok_bot_tag,
    platform_installs,
    repo_platform_copies,
    skill_allowed_for_seat,
    specialize_from_monet,
    specialize_universal,
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


def _coord() -> str:
    with open(COORD, encoding="utf-8") as f:
        return f.read()


class SpecializeTests(unittest.TestCase):
    def test_cursor_is_not_monet(self) -> None:
        out = specialize_from_monet(_session(), SEATS["cursor"], skill_name="session-start")
        self.assertIn("AGENT_SEAT=CURSOR", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertIn("[CURSOR]", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertIn("cursor/<slug>", out)
        self.assertIn("GB-<NAME>", out)
        self.assertIn("not `[GROK-BOT]`", out)
        self.assertNotIn("two different Claude accounts", out)
        self.assertIn("This install is for `CURSOR`", out)

    def test_ag_suffix_is_antigravity(self) -> None:
        out = specialize_from_monet(_session(), SEATS["ag"], skill_name="session-start")
        self.assertIn("AGENT_SEAT=AG", out)
        self.assertIn("ag/<slug>", out)
        self.assertIn("trading-antigravity", out)
        self.assertNotIn("trading-monet", out)

    def test_ag_notes_name_is_antigravity(self) -> None:
        out = specialize_from_monet(_notes(), SEATS["ag"], skill_name="apple-notes")
        self.assertIn("[APP, Antigravity]", out)
        self.assertNotIn("[APP, AG]", out)
        self.assertIn("then `Antigravity` (Title Case", out)

    def test_cursor_and_grok_notes_names(self) -> None:
        out_cur = specialize_from_monet(_notes(), SEATS["cursor"], skill_name="apple-notes")
        self.assertIn("[APP, Cursor]", out_cur)
        out_grok = specialize_from_monet(_notes(), SEATS["grok"], skill_name="apple-notes")
        self.assertIn("[APP, Grok]", out_grok)
        out_codex = specialize_from_monet(_notes(), SEATS["codex"], skill_name="apple-notes")
        self.assertIn("[APP, Codex]", out_codex)

    def test_fleet_coordination_seats_table_preserved(self) -> None:
        out = specialize_from_monet(_coord(), SEATS["ag"], skill_name="fleet-coordination")
        self.assertIn("Antigravity / Gemini: `[AG]`", out)
        self.assertIn("Monet: `[MONET]`", out)
        self.assertIn("Cursor: `[CURSOR]`", out)
        self.assertIn("Codex: `[CODEX]`", out)
        self.assertNotIn("AG: `[AG]`", out)

    def test_specialize_universal(self) -> None:
        out_sess = specialize_universal(_session(), skill_name="session-start")
        self.assertIn("AGENT_TAG=<YOUR_TAG>", out_sess)
        self.assertIn("AGENT_SEAT=<YOUR_SEAT>", out_sess)
        self.assertIn("<seat>/<slug>", out_sess)
        self.assertNotIn("AGENT_SEAT=MONET", out_sess)

        out_not = specialize_universal(_notes(), skill_name="apple-notes")
        self.assertIn("[APP, Agent]", out_not)
        self.assertNotIn("[APP, Monet]", out_not)

    def test_codex_grok_renoir_deepseek_tags(self) -> None:
        mapping = {
            "codex": "CODEX",
            "grok": "GROK",
            "renoir": "RENOIR",
            "deepseek": "DSH",
            "minimax": "MM",
            "grok-build": "GROK-BUILD",
            "claude": "CLAUDE",
        }
        src = _session()
        for key, tag in mapping.items():
            out = specialize_from_monet(src, SEATS[key], skill_name="session-start")
            self.assertIn(f"AGENT_SEAT={tag}", out, key)
            self.assertNotIn("AGENT_SEAT=MONET", out, key)
        grok_bot = specialize_from_monet(
            src, SEATS["grok-bot"], skill_name="session-start"
        )
        self.assertIn("GB-CONDUCTOR", grok_bot)
        self.assertIn("GB-COMPILER", grok_bot)
        self.assertIn("GB-ORACLE", grok_bot)
        self.assertNotRegex(grok_bot, r"GB-COMPILE(?!R)")
        self.assertIn("[GB-<NAME>]", grok_bot)
        self.assertNotIn("AGENT_SEAT=MONET", grok_bot)
        self.assertNotIn("AGENT_SEAT=GROK-BOT", grok_bot)
        self.assertNotIn("You are **GROK-BOT**", grok_bot)
        self.assertNotIn("You are **CURSOR**", grok_bot)
        self.assertNotIn("You are **MONET**", grok_bot)

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
        self.assertFalse(any("/.renoir/" in dest or dest.endswith("/.renoir/skills") for dest in dests))
        out = specialize_from_monet(_session(), SEATS["renoir"], skill_name="session-start")
        self.assertIn("Inactive seat", out)
        self.assertIn("Do not install to `~/.renoir/skills`", out)

    def test_claude_shared_pin(self) -> None:
        out = specialize_from_monet(
            _session(), SEATS["claude_shared"], skill_name="session-start"
        )
        self.assertIn("Shared `~/.claude/skills`", out)
        self.assertIn("RENOIR", out)
        self.assertIn("MONET, CLAUDE, or RENOIR", out)

    def test_sentence_gap_keeps_protocol_name(self) -> None:
        with open(GAP, encoding="utf-8") as f:
            src = f.read()
        out = specialize_from_monet(src, SEATS["cursor"], skill_name="sentence-gap")
        self.assertTrue(
            "Monet portable" in out or "Monet's portable" in out,
            out[:600],
        )

    def test_quoted_description_folds_for_fx(self) -> None:
        out = specialize_from_monet(
            _session(), SEATS["codex"], skill_name="session-start"
        )
        self.assertIn("description: >-", out.split("---")[1])
        self.assertNotIn('description: Start', out.split("---")[1])
        self.assertIn("just a small fix.", out)

    def test_fx_seat_is_not_cursor(self) -> None:
        self.assertEqual(SEATS["fx"].tag, "FX")
        self.assertTrue(SEATS["fx"].dest.endswith("/.fx/skills") or "fx/skills" in SEATS["fx"].dest)
        out = specialize_from_monet(
            _session(), SEATS["fx"], skill_name="session-start"
        )
        self.assertIn("AGENT_SEAT=FX", out)
        self.assertNotIn("AGENT_SEAT=CURSOR", out)
        self.assertIn("[FX]", out)

    def test_fold_unwraps_quoted_yaml_string(self) -> None:
        from fleet_skill_identity import fold_yaml_description

        src = (
            "---\n"
            'name: cloudflare-one\n'
            'description: "Guides Cloudflare One \\"Zero Trust\\" work."\n'
            "---\n\n"
            "# Cloudflare One\n"
        )
        out = fold_yaml_description(src)
        self.assertIn("description: >-", out)
        self.assertIn('Guides Cloudflare One "Zero Trust" work.', out)
        self.assertNotIn('description: "', out)


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
                out = specialize_from_monet(
                    _load_skill(name), seat, skill_name=name
                )
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
                out = specialize_from_monet(
                    _load_skill(name), seat, skill_name=name
                ).lower()
                for fragment in banned:
                    if fragment in out:
                        hits.append(f"{key}/{name}:{fragment}")
        self.assertEqual(
            hits,
            [],
            "rendered skills must not ban DealDex GitHub-hosted macos-latest:\n"
            + "\n".join(hits),
        )


class PerSeatVoiceTests(unittest.TestCase):
    def test_each_named_seat_is_not_another_seat(self) -> None:
        names = catalog_skill_names(DOCS)
        named = [
            key
            for key, seat in SEATS.items()
            if seat.mode in {"exclusive", "grok_bot"}
        ]
        for key in named:
            seat = SEATS[key]
            for name in names:
                if not skill_allowed_for_seat(name, seat):
                    continue
                out = specialize_from_monet(
                    _load_skill(name), seat, skill_name=name
                )
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
            out = specialize_from_monet(
                _load_skill(name), SEATS["ag"], skill_name=name
            )
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
            out = specialize_from_monet(
                _load_skill(name), SEATS["codex"], skill_name=name
            )
            for phrase in forbidden:
                self.assertNotIn(phrase, out, f"codex/{name}: {phrase}")

    def test_cursor_skills_do_not_address_reader_as_monet(self) -> None:
        for name in catalog_skill_names(DOCS):
            out = specialize_from_monet(
                _load_skill(name), SEATS["cursor"], skill_name=name
            )
            self.assertNotIn("You are **MONET**", out, name)
            self.assertNotIn("AGENT_SEAT=MONET", out, name)
            self.assertNotIn("This install is for `MONET`", out, name)
            self.assertNotIn("This pack is for the **MONET** Claude account", out, name)

    def test_kimi_is_kimi_voiced(self) -> None:
        out = specialize_from_monet(
            _session(), SEATS["kimi"], skill_name="session-start"
        )
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
        out = specialize_from_monet(
            _session(), SEATS["claude_shared"], skill_name="session-start"
        )
        self.assertIn("Shared `~/.claude/skills`", out)
        self.assertNotIn("You are **MONET**", out)
        self.assertIn("MONET, CLAUDE, or RENOIR", out)


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
        self.assertTrue(is_grok_bot_tag("GB-ORACLE"))
        self.assertTrue(is_grok_bot_tag("GB-COMPILER"))
        self.assertFalse(is_grok_bot_tag("AFC"))
        self.assertTrue(head_has_fleet_wake("[MONET->FLEET] sync-1"))
        self.assertFalse(head_has_fleet_wake("[FLEET] sync-1"))
        self.assertFalse(head_has_fleet_wake("[AFC] sync-1"))

    def test_fleet_apps_acronym_is_afc(self) -> None:
        import json

        data = json.loads(
            Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8")
        )
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
        data = json.loads(
            Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8")
        )
        tags = {s["tag"]: s for s in data["seats"]}
        self.assertEqual(tags["MM"]["notesName"], "MiniMax")
        self.assertEqual(tags["DSH"]["notesName"], "DeepSeek Harness")
        self.assertNotIn("MINIMAX", tags)
        self.assertNotIn("DEEPSEEK", tags)
        text = Path(os.path.join(ROOT, "AGENT-SYNC.md")).read_text(encoding="utf-8")
        self.assertIn("| **MiniMax (`MM`)** |", text)
        self.assertIn("| **DeepSeek Harness (`DSH`)** |", text)
        self.assertIn("`[MM]`", text)
        self.assertIn("`[DSH]`", text)
        self.assertNotIn("| **MiniMax (`MINIMAX`)** |", text)
        self.assertNotIn("| **DeepSeek (`DEEPSEEK`)** |", text)
        coord = _coord()
        self.assertIn("MiniMax (MM): `[MM]`", coord)
        self.assertIn("DeepSeek Harness (DSH): `[DSH]`", coord)
        self.assertNotIn("MiniMax: `[MINIMAX]`", coord)
        self.assertNotIn("DeepSeek: `[DEEPSEEK]`", coord)

    def test_agent_sync_self_id_is_afc_not_fleet(self) -> None:
        text = Path(os.path.join(ROOT, "AGENT-SYNC.md")).read_text(encoding="utf-8")
        self.assertIn("| `AFC` |", text)
        self.assertIn("Simple-With-Us/AI-Fleet-Coordinator", text)
        self.assertIn("[AFC] sync-N", text)
        self.assertIn("every Grok Bot seat", text)
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
                self.assertIn("every Grok Bot seat", src)
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
                if name == "session-start" and key != "kimi":
                    self.assertIn("| AFC |", out, f"{key}/{name} lost AFC acronym")
                if name == "fleet-coordination":
                    self.assertIn("AFC", out, f"{key}/{name}")
                    self.assertIn("GB-COMPILER", out, f"{key}/{name}")
                    self.assertIn("GB-ORACLE", out, f"{key}/{name}")
                    self.assertNotRegex(out, r"GB-COMPILE(?!R)", msg=f"{key}/{name}")

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
        cursor_ss = Path(
            DOCS, "by-seat", "cursor", "session-start", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("| AFC |", cursor_ss)
        self.assertNotIn("This install is for `FLEET`", cursor_ss)


class RepoPlatformCopiesTests(unittest.TestCase):
    """The repo-tracked platform trees must equal a fresh render of the pack.

    Sessions opened in this repo load .claude/skills, .cursor/skills and
    .grok/skills directly, so a stale copy teaches the old lane commands.
    """

    FIX = "python3 scripts/install-fleet-skills.py --repo-only"

    def test_tracked_platform_copies_match_fresh_render(self) -> None:
        stale: list[str] = []
        for dest, seat in repo_platform_copies(ROOT):
            for name in catalog_skill_names(DOCS):
                if not skill_allowed_for_seat(name, seat):
                    continue
                path = Path(dest, name, "SKILL.md")
                want = specialize_from_monet(
                    _load_skill(name), seat, skill_name=name
                )
                have = path.read_text(encoding="utf-8") if path.is_file() else None
                if have != want:
                    stale.append(os.path.relpath(str(path), ROOT))
        self.assertEqual(
            stale, [], f"stale platform copies; re-render with: {self.FIX}"
        )

    def test_platform_copies_carry_no_never_install_skill(self) -> None:
        for dest, _seat in repo_platform_copies(ROOT):
            for name in NEVER_INSTALL:
                self.assertFalse(
                    Path(dest, name).exists(), f"{dest}/{name} must not exist"
                )

    def test_platform_copies_do_not_teach_retired_seat_flat_lane(self) -> None:
        """The old session-start text told readers to add a flat monet lane."""
        for dest, _seat in repo_platform_copies(ROOT):
            text = Path(dest, "session-start", "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertNotRegex(
                text, r"worktree add -b \S+ ~/apps/<prefix>-monet", dest
            )
            self.assertIn("lane new", text, dest)


def _load_installer():
    path = os.path.join(ROOT, "scripts", "install-fleet-skills.py")
    spec = importlib.util.spec_from_file_location("install_fleet_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
                want = specialize_from_monet(
                    _load_skill(name), seat, skill_name=name
                )
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

        data = json.loads(
            Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8")
        )
        row = next(r for r in data["seats"] if r["tag"] == tag)
        return row["branchPrefixes"][0]

    def test_coordination_skill_leads_with_registry_prefix_for_ag(self) -> None:
        prefix = self._registry_first_prefix("AG")
        self.assertEqual(prefix, "ag/")
        text = _load_skill("fleet-coordination")
        line = next(
            l for l in text.splitlines() if l.strip().startswith("- Antigravity / Gemini:")
        )
        self.assertIn(f"branch prefix `{prefix}`", line)
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

        cursor = para("lanes/trading/cursor-<slug>")
        self.assertIn("`cursor/<slug>`", cursor)
        # The new lane's branch is not the legacy standing lane's branch.
        self.assertNotIn("cursor-<slug>`), on its own branch (`agent/cursor`)", cursor)


class AgentSyncRosterTests(unittest.TestCase):
    """The availability roster must agree with the seat table and registry."""

    MARKER = "Retired, do not assign:"

    def _registry(self) -> list[dict]:
        import json

        data = json.loads(
            Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8")
        )
        return data["seats"]

    def _roster(self) -> str:
        text = Path(ROOT, "AGENT-SYNC.md").read_text(encoding="utf-8")
        start = text.index("## Agent availability")
        end = text.index("## CI Runner Infrastructure Policy")
        return text[start:end]

    def _available_paragraph(self) -> str:
        roster = self._roster()
        start = roster.index("**Available (normal):**")
        return roster[start : roster.index("\n\n", start)]

    def test_active_seats_sentence_lists_no_retired_seat(self) -> None:
        match = re.search(r"Active seats: ([A-Z, -]+)\.", self._roster())
        self.assertIsNotNone(match)
        active = {t.strip() for t in match.group(1).split(",")}
        retired = {r["tag"] for r in self._registry() if r.get("retired")}
        self.assertEqual(active & retired, set(), active)
        self.assertIn("CLUTCH", active)

    def test_available_list_has_no_retired_seat_and_names_clutch(self) -> None:
        para = self._available_paragraph()
        self.assertIn(self.MARKER, para)
        before, after = para.split(self.MARKER, 1)
        retired = [r["tag"] for r in self._registry() if r.get("retired")]
        for tag in retired:
            self.assertNotRegex(before, rf"\b{re.escape(tag)}\b", tag)
        self.assertRegex(before, r"\bCLUTCH\b")
        for tag in ("MONET", "RENOIR", "HARNESS"):
            self.assertRegex(after, rf"\b{tag}\b", tag)

    def test_roster_never_says_renoir_is_a_future_seat(self) -> None:
        self.assertNotIn("future third seat", self._roster())

    def test_seat_table_marks_the_same_seats_retired(self) -> None:
        text = Path(ROOT, "AGENT-SYNC.md").read_text(encoding="utf-8")
        for tag in ("MONET", "RENOIR", "HARNESS"):
            row = next(
                l for l in text.splitlines() if l.startswith(f"| **") and f"(`{tag}`)" in l
            )
            self.assertIn("Retired 2026-10-07", row, tag)


if __name__ == "__main__":
    unittest.main()


