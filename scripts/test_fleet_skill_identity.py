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
    _SEAT_PIN_SOURCE,
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

    def test_ag_suffix_is_antigravity(self) -> None:
        out = specialize_from_monet(_session(), SEATS["ag"], skill_name="session-start")
        self.assertIn(_arm("AG"), out)
        self.assertIn(_ordinary("Antigravity", "AG"), out)
        self.assertIn("ag/<slug>", out)
        self.assertIn("trading-antigravity", out)
        self.assertNotIn("trading-monet", out)

    def test_ag_notes_name_is_antigravity(self) -> None:
        # The canonical apple-notes text is seat-neutral (`[APP, Agent]`), so the
        # seat's Notes name comes from the install banner, never from Monet.
        out = specialize_from_monet(_notes(), SEATS["ag"], skill_name="apple-notes")
        self.assertIn("Notes `Antigravity`", out)
        self.assertNotIn("Notes `AG`", out)
        self.assertNotIn("[APP, Monet]", out)

    def test_cursor_and_grok_notes_names(self) -> None:
        for key, notes in (("cursor", "Cursor"), ("grok", "Grok"), ("codex", "Codex")):
            out = specialize_from_monet(_notes(), SEATS[key], skill_name="apple-notes")
            self.assertIn(f"Notes `{notes}`", out, key)
            self.assertNotIn("[APP, Monet]", out, key)

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
            if SEATS[key].retired:  # never anyone's default:  the arm fails
                self.assertIn(f'else SEAT="${{AGENT_SEAT:?{tag} is retired; take no work as {tag}}}"; fi', out, key)
            else:
                self.assertIn(_arm(tag), out, key)
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
        self.assertIn("Retired seat", out)
        self.assertNotIn("not yet active", out)
        self.assertIn("Do not install to `~/.renoir/skills`", out)

    def test_claude_shared_pin(self) -> None:
        out = specialize_from_monet(
            _session(), SEATS["claude_shared"], skill_name="session-start"
        )
        self.assertIn("Shared `~/.claude/skills`", out)
        self.assertIn(_arm("CLAUDE"), out)
        self.assertIn(_ordinary("Claude", "CLAUDE"), out)
        self.assertNotIn("MONET, CLAUDE, or RENOIR", out)

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
        self.assertIn(_arm("FX"), out)
        self.assertIn(_ordinary("Fx", "FX"), out)
        self.assertNotIn("AGENT_SEAT=CURSOR", out)
        self.assertIn("[FX·session8]", out)

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
        for name in catalog_skill_names(DOCS):
            if not skill_allowed_for_seat(name, SEATS["claude_shared"]):
                continue
            out = specialize_from_monet(
                _load_skill(name), SEATS["claude_shared"], skill_name=name
            )
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
        self.assertTrue(is_grok_bot_tag("GB-ORACLE"))
        self.assertTrue(is_grok_bot_tag("GB-COMPILER"))
        self.assertFalse(is_grok_bot_tag("AFC"))
        self.assertTrue(head_has_fleet_wake("[MONET->FLEET] sync-1"))
        self.assertTrue(head_has_fleet_wake("[CLAUDE·1a2b3c4d] @**all** HALT"))
        self.assertTrue(head_has_fleet_wake("[CLAUDE·1a2b3c4d] @*fleet* HALT"), "the retired group form is still read")
        self.assertFalse(head_has_fleet_wake("[CLAUDE·1a2b3c4d] @**Codex** HALT"))
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
        text = Path(os.path.join(ROOT, "AGENT-SYNC.md")).read_text(encoding="utf-8")
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
        "CLAUDE", "CODEX", "AG", "CURSOR", "GROK", "CLUTCH", "FX", "MM", "MC", "MA",
    }

    def test_every_zulip_identity_matches_the_guide_roster(self) -> None:
        roster = _guide_roster()
        self.assertTrue(roster, "guide roster table not found")
        seen: set[str] = set()
        for key, seat in SEATS.items():
            if not seat.zulip_bot:
                continue
            if seat.tag not in roster:
                # GROK-BUILD is an alias row that posts through GROK's bot.
                self.assertEqual(
                    (_bot_local(seat), seat.zulip_rc), roster["GROK"], key
                )
                continue
            seen.add(seat.tag)
            self.assertEqual(
                (_bot_local(seat), seat.zulip_rc), roster[seat.tag], key
            )
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
        self.assertFalse(tags & {"MONET", "RENOIR", "DSH", "KIMI", "CLUTCH"}, tags)

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

    def test_clutch_is_catalog_only_with_its_own_identity(self) -> None:
        seat = SEATS["clutch"]
        self.assertEqual((seat.tag, seat.notes, seat.prefix, seat.suffix),
                         ("CLUTCH", "Clutch", "clutch", "clutch"))
        self.assertFalse(seat.write_home)
        self.assertFalse(seat.retired)
        self.assertIn(seat, catalog_seats())
        self.assertTrue(skill_allowed_for_seat("mac-cleanup", seat))
        out = specialize_from_monet(_session(), seat, skill_name="session-start")
        self.assertIn(_arm("CLUTCH"), out)
        self.assertIn(_ordinary("Clutch", "CLUTCH"), out)
        self.assertIn("clutch/<slug>", out)
        self.assertIn("clutch-bot@simplewithus.zulipchat.com", out)
        self.assertIn("~/.secrets/Zulip/Clutch-zuliprc", out)
        self.assertIn("[CLUTCH·session8]", out)
        self.assertNotIn("AGENT_SEAT=MONET", out)
        self.assertNotIn("DSH·session8", out)

    def test_session_tag_and_board_by_are_the_readers_own(self) -> None:
        for key, seat in SEATS.items():
            if seat.retired or seat.mode == "grok_bot":
                continue
            ss = specialize_from_monet(_session(), seat, skill_name="session-start")
            board = specialize_from_monet(
                _load_skill("board-ops"), seat, skill_name="board-ops"
            )
            self.assertIn(f"`[{seat.tag}·session8]` tag", ss, key)
            self.assertNotIn("[MONET·session8", ss, key)
            self.assertIn(f"`--by` for this seat is your verified seat (`{seat.tag}` in an ordinary", board, key)
            self.assertIn('--by "$AGENT_SEAT"', board, key)
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
                if re.search(r"slack", line, re.I) and "retired" not in line.lower():
                    offenders.append(f"{label}:{n}: {line.strip()[:100]}")

        names = catalog_skill_names(DOCS)
        for key, seat in SEATS.items():
            for name in names:
                if skill_allowed_for_seat(name, seat):
                    scan(f"{key}/{name}", specialize_from_monet(_load_skill(name), seat, skill_name=name))
        for name in names:
            scan(f"universal/{name}", specialize_universal(_load_skill(name), skill_name=name))
            scan(f"source/{name}", _load_skill(name))
        scan("README-add-in-app.md", Path(DOCS, "README-add-in-app.md").read_text(encoding="utf-8"))
        for script in ("fleet_skill_identity.py", "install-fleet-skills.py"):
            scan(script, Path(ROOT, "scripts", script).read_text(encoding="utf-8"))
        self.assertEqual(offenders, [], "live Slack instruction survived:\n" + "\n".join(offenders))


class AgentSeatPinTests(unittest.TestCase):
    """The seat-precedence block (AGENT-SYNC § Identity Rules, owner 2026-10-09).

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
        match = re.search(r'(?ms)^if \[ -n "\$\{AGENT_LAUNCH_SEAT:-\}" \].*?^agent-sync whoami --as "\$SEAT"$', text)
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
                self.assertIn(f"{seat.tag} is retired and is no platform's default", out, key)
                continue
            self.assertIn(_arm(seat.tag), self._block(out).splitlines(), key)
            self.assertIn(_ordinary(seat.notes, seat.tag), out, key)
            self.assertIn(f"`<branch-prefix>` below is your seat's branch prefix:  `{seat.prefix}`", out, key)

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
            self.assertIn('--by "$AGENT_SEAT"', out, key)
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
            script = block.replace('agent-sync whoami --as "$SEAT"', 'printf "%s|%s" "$SEAT" "$AGENT_SEAT"')
            done = subprocess.run(["bash", "-c", script], env={**base, **env}, capture_output=True, text=True)
            return done.returncode, done.stdout, done.stderr

        for key, out in self._renders():
            if key == "kimi":
                continue
            block = self._block(out)
            check = subprocess.run(["bash", "-n", "-c", block], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, f"{key}: {check.stderr}")
            # A launcher's seat wins, and the AGENT_SEAT an engine's rules exported is left alone
            # (the CLI then refuses the mismatch).
            self.assertEqual(run(block, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER", AGENT_SEAT="CODEX"),
                             (0, "BF-PLUMBER|CODEX", ""), key)
            self.assertEqual(run(block, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER")[:2],
                             (0, "BF-PLUMBER|BF-PLUMBER"), key)
            # A launcher with no seat fails closed, before any default is read.
            code, stdout, stderr = run(block, AGENT_LAUNCHER="botfleet", AGENT_SEAT="CLAUDE")
            self.assertEqual((code, stdout), (3, ""), key)
            self.assertIn("no seat assigned by botfleet", stderr, key)
            # A seat already set is kept.
            self.assertEqual(run(block, AGENT_SEAT="BF-LAUNCHED")[:2], (0, "BF-LAUNCHED|BF-LAUNCHED"), key)
            # Nothing set:  the platform default, or a loud failure where there is none.
            code, stdout, stderr = run(block)
            seat = SEATS.get(key)
            if seat is not None and seat.mode == "exclusive" and not seat.retired:
                self.assertEqual((code, stdout), (0, "%s|%s" % (seat.tag, seat.tag)), key)
            else:
                self.assertNotEqual(code, 0, key)
                self.assertEqual(stdout, "", key)
            if key == "grok-bot":
                self.assertEqual(run(block, AGENT_TAG="GB-ORACLE")[:2], (0, "GB-ORACLE|GB-ORACLE"), key)


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
                if name in FROZEN_RENDERS or not skill_allowed_for_seat(name, seat):
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

    def test_default_mode_never_writes_retired_or_catalog_only_homes(self) -> None:
        # Make every one of those homes' parent exist, so only the seat table,
        # not a missing folder, can keep the installer out of them.
        for parent in (".deepseek", ".kimi", ".renoir", "Desktop", ".clutch"):
            (self.home / parent).mkdir(parents=True)
        self._run([])
        wrote = [
            f for f in self._home_files()
            if f.startswith((".deepseek/", ".kimi/", ".renoir/", "Desktop/", ".clutch/"))
        ]
        self.assertEqual(wrote, [])
        self.assertTrue(
            (self.home / ".claude" / "skills" / "session-start" / "SKILL.md").is_file()
        )
        shared = (self.home / ".claude" / "skills" / "session-start" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(_arm("CLAUDE"), shared)
        self.assertIn(_ordinary("Claude", "CLAUDE"), shared)
        self.assertNotIn("MONET, CLAUDE, or RENOIR", shared)

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

        cursor = para("lanes/trading/cursor-<slug>")
        self.assertIn("`cursor/<slug>`", cursor)
        # The new lane's branch is not the legacy standing lane's branch.
        self.assertNotIn("cursor-<slug>`), on its own branch (`agent/cursor`)", cursor)


class AgentSyncRosterTests(unittest.TestCase):
    """The availability roster must agree with the seat table and registry.

    The Oct 7 rewrite replaced the old "## Agent availability" section and the
    bold seat-table rows with "### Availability" (an Available bullet and a
    Retired bullet list) and the "### Active Seats" table.
    """

    def _registry(self) -> list[dict]:
        import json

        data = json.loads(
            Path(os.path.join(ROOT, "fleet-apps.json")).read_text(encoding="utf-8")
        )
        return data["seats"]

    def _text(self) -> str:
        return Path(ROOT, "AGENT-SYNC.md").read_text(encoding="utf-8")

    def _section(self, start_marker: str, end_marker: str) -> str:
        text = self._text()
        start = text.index(start_marker)
        return text[start : text.index(end_marker, start + len(start_marker))]

    def _roster(self) -> str:
        return self._section("\n### Availability\n", "\n## ")

    def _active_table_tags(self) -> set[str]:
        table = self._section("\n### Active Seats\n", "\n- **Tags.**")
        return {
            m.group(1)
            for m in re.finditer(r"(?m)^\| ([A-Z][A-Z-]*) \|", table)
        }

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


if __name__ == "__main__":
    unittest.main()


