"""install_rules tests: the per-platform Lane Map rules installer.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_install_rules -v

Every test works in a throwaway fake home (tempfile.TemporaryDirectory) and never touches the real
home.  The sample rules files copy the SHAPE of the real ones (headings, a lane table, comment
markers, an em dash) and never their content.  The fake home sits in the macOS per-user temp
directory, which is a symlinked path (/var -> /private/var), so every test also exercises the
realpath comparison against --home.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import install_rules as IR
from fleet_lanes import layout as L

_APPS = [
    ("Socratic-Trade", "ST", "trading", "Socratic-Trade"),
    ("Congress.Trade", "CT", "congress", "Congress.Trade"),
    ("Usage-Monitor", "UM", "usage", "Usage-Monitor"),
    ("congress-trading-shared", "CTS", "cts", "congress-trading-shared"),
    ("DealDex", "DD", "dealdex", "DealDex"),
    ("AI-Fleet-Coordinator", "AFC", "fleet", "AI-Fleet-Coordinator"),
    ("ContactLogo", "CL", "contactlogo", "ContactLogo"),
    ("fleet-ops", "OPS", "fleet-ops", "fleet-ops"),
]
_SEATS = [
    ("CLAUDE", "claude", ["claude/", "agent/claude"], False),
    ("MONET", "monet", ["monet/"], True),
    ("CODEX", "codex", ["codex/"], False),
    ("AG", "antigravity", ["ag/", "agent/antigravity"], False),
    ("CURSOR", "cursor", ["cursor/"], False),
    ("GROK", "grok", ["grok/"], False),
    ("GROK-BUILD", "grok-build", ["grok-build/"], False),
    ("MM", "minimax", ["minimax/"], False),
    ("FX", "fx", ["fx/"], False),
]
REGISTRY = L.parse_registry({
    "owner": "Simple-With-Us",
    "apps": [{"repo": r, "acronym": a, "worktreePrefix": p, "codeDir": c} for r, a, p, c in _APPS],
    "seats": [{"tag": t, "worktreeSuffix": s, "branchPrefixes": b, **({"retired": True} if ret else {})}
              for t, s, b, ret in _SEATS],
})

# Sample files: the shape of the real rules files, none of their text.
CLAUDE_SAMPLE = """# Global Claude Code instructions

Canonical protocol lives elsewhere — read it first.

## Seat identity — pin it, never infer it

Notes name is Title Case, branches are `<seat>/<slug>`, lanes are `~/apps/<prefix>-<seat>`.

| App | Prefix | Example lane |
|---|---|---|
| Socratic.Trade | `trading` | `~/apps/trading-claude` |
| Congress.Trade | `congress` | `~/apps/congress-claude` |
| Usage-Monitor | `usage` | `~/apps/usage-claude` |

## Secrets

Handoff file: a path, never chat text.

Nothing to see here.
"""

CODEX_SAMPLE = """# Codex rules

Work in an owned worktree off `origin/main`.

| App | Prefix | Example lane |
|---|---|---|
| Socratic.Trade | `trading` | `~/apps/trading-codex` |
| ai-fleet-coordinator | `fleet` | `~/apps/fleet-codex` |

Use a worktree only when parallel writers need it.
"""

MINIMAX_SAMPLE = """<!-- mavis-personalization:start -->
# User profile

## More about you

Prefers short answers.
<!-- mavis-personalization:end -->
<!-- mem-append-reason: owner standing rule -->
### Delegation economics (2026-09-04)
Delegate by default.
"""

REPLACED_BODY = "## Lane Map: changed wording for the replace test (owner 2026-10-07)\n\nSecond line of the changed rule.\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: str) -> dict[str, tuple]:
    """Every dir and file under root with content hash, mode and mtime, so any write shows up."""
    snap: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            p = os.path.join(dirpath, name)
            snap[p] = ("dir", os.lstat(p).st_mode) if not os.path.islink(p) else ("dirlink", os.readlink(p))
        for name in filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                snap[p] = ("link", os.readlink(p))
            else:
                snap[p] = ("file", sha(Path(p).read_bytes()), st.st_mode, st.st_mtime_ns)
    return snap


def clock_at(start: str, step_s: int = 0):
    """An injectable clock: returns `start` plus `step_s` seconds more on every call."""
    state = {"t": dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)}

    def now() -> dt.datetime:
        t = state["t"]
        state["t"] = t + dt.timedelta(seconds=step_s)
        return t
    return now


class HomeCase(unittest.TestCase):
    """A fake home plus helpers to fill it and to run the CLI against it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name

    def path(self, rel: str) -> str:
        return os.path.join(self.home, rel)

    def write(self, rel: str, text: str | bytes, mode: int | None = None) -> str:
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        Path(p).write_bytes(data)
        if mode is not None:
            os.chmod(p, mode)
        return p

    def read(self, rel: str) -> str:
        return Path(self.path(rel)).read_text(encoding="utf-8")

    def run_cli(self, *argv: str, clock=None, registry=REGISTRY, home: bool = True):
        out, err = io.StringIO(), io.StringIO()
        args = list(argv) + (["--home", self.home] if home else [])
        code = IR.main(args, clock=clock or clock_at("2026-10-07T14:30:00"), out=out, err=err, registry=registry)
        return code, out.getvalue(), err.getvalue()

    def files_in(self, rel_dir: str) -> list[str]:
        return sorted(os.listdir(self.path(rel_dir)))


# --------------------------------------------------------------------------- templates

class TemplateTests(unittest.TestCase):
    def texts(self) -> dict[str, str]:
        out = {v: (IR.RULES_DIR / f).read_text(encoding="utf-8") for v, f in IR.TEMPLATE_FILES.items()}
        out.update({ph: (IR.RULES_DIR / f).read_text(encoding="utf-8") for ph, f in IR.PART_FILES.items()})
        return out

    def test_templates_exist_and_are_plain_ascii(self) -> None:
        for variant, text in self.texts().items():
            with self.subTest(variant=variant):
                self.assertTrue(text.strip())
                text.encode("ascii")

    def test_block_is_wrapped_in_the_markers(self) -> None:
        for variant in IR.TEMPLATE_FILES:
            with self.subTest(variant=variant):
                lines = IR.block_lines(variant)
                self.assertEqual(lines[0], f"<!-- fleet-lane-map:begin v{IR.BLOCK_VERSION} -->")
                self.assertEqual(IR.BLOCK_VERSION, 2, "layout v2 text is block version 2")
                self.assertEqual(lines[-1], "<!-- fleet-lane-map:end -->")
                self.assertEqual(IR.block_text(variant), "\n".join(lines) + "\n")

    def test_minimal_variant_is_three_lines(self) -> None:
        body = IR.body_text("minimal").split("\n")
        self.assertEqual(len(body), 3)
        self.assertTrue(all(body))

    def test_cursor_frontmatter_comes_first_and_body_matches_full(self) -> None:
        fm = IR.frontmatter_lines("cursor")
        self.assertEqual(fm[0], "---")
        self.assertEqual(fm[-1], "---")
        self.assertIn("alwaysApply: true", fm)
        self.assertTrue(any(ln.startswith("description: ") and len(ln) > 20 for ln in fm))
        self.assertEqual(IR.body_text("cursor"), IR.body_text("full"))
        self.assertTrue(IR.compose(None, "cursor").new_text.startswith("---\n"))
        self.assertIsNone(IR.frontmatter_problem(IR.compose(None, "cursor").new_text))

    def test_two_spaces_between_sentences(self) -> None:
        for variant, text in self.texts().items():
            with self.subTest(variant=variant):
                self.assertIsNone(re.search(r"[.!?] [A-Za-z]", text), re.findall(r".{12}[.!?] [A-Za-z].{8}", text))

    def test_every_variant_carries_the_rule(self) -> None:
        for variant in ("full", "minimal"):
            body = IR.body_text(variant)
            with self.subTest(variant=variant):
                for needle in ("docs/protocols/lane-map.md", "~/Code/<Repo>", "~/apps/lanes/<Repo>/", "review-pr-<n>",
                               "~/apps/lane new <app> <slug>", "AGENT_SEAT", "/tmp", "/private/tmp", "/var/tmp",
                               "$TMPDIR", "/var/folders", "throwaway", "whole", "~/apps/<prefix>-<seat>",
                               "_managed", "_review", "2026-10-09"):
                    self.assertIn(needle, body)

    def test_the_full_text_names_every_v2_home(self) -> None:
        full = IR.body_text("full")
        for needle in ("~/apps/lanes/<Repo>/<seat>-<slug>", "~/apps/lanes/<Repo>/review-pr-<n>",
                       "~/apps/lanes/<Repo>/<slug>-<hex>", "~/apps/lanes/_codex/<slug>/<Repo>", "<repo>/.claude/worktrees",
                       "Congress.Trade", "AI-Fleet-Coordinator"):
            self.assertIn(needle, full)
        self.assertNotIn("~/apps/lanes/_review/<prefix>", full, "the pre-v2 review path is gone from the rule")
        self.assertNotIn("lanes/<prefix>/<seat>", full)

    def test_a_v1_block_is_replaced_in_place_by_the_current_one(self) -> None:
        v1 = "# Rules\n\n" + IR.BEGIN_PREFIX + "1 -->\n~/apps/lanes/<prefix>/<seat>-<slug>\n" + IR.END_LINE + "\n\ntail\n"
        res = IR.compose(v1, "full")
        self.assertEqual(res.action, "replace")
        self.assertTrue(res.new_text.startswith("# Rules\n\n" + IR.BEGIN_PREFIX + f"{IR.BLOCK_VERSION} -->\n"))
        self.assertTrue(res.new_text.endswith("\ntail\n"))
        self.assertNotIn("<prefix>/<seat>", res.new_text.split(IR.END_LINE)[0].replace(
            "~/apps/lanes/<prefix>/ such as", ""))

    def test_branch_is_prefix_plus_slug_not_the_folder_seat_name(self) -> None:
        # AG: folder antigravity-<slug>, branch ag/<slug>.  The draft's "<seat>/<slug>" was wrong for it.
        full = IR.body_text("full")
        self.assertIn("ag/fix-thing", full)
        self.assertIn("branch prefix", full)
        self.assertNotIn("branch <seat>/<slug>", full)
        self.assertIn("branch minimax/<slug>", IR.body_text("minimal"))

    def test_banned_words_stay_out(self) -> None:
        for variant, text in self.texts().items():
            with self.subTest(variant=variant):
                low = text.lower()
                self.assertNotIn("s" + "lack", low)               # chat moved off the old tool (owner 2026-10-07)
                # Two words that must never sit near each other in our text; built apart so this file obeys it too.
                first, second = "iso" + "lation", "work" + "tree"
                for a, b in ((first, second), (second, first)):
                    self.assertNotRegex(low, a + r".{0,40}" + b)

    def test_redactor_leaves_our_own_text_alone(self) -> None:
        for variant in IR.TEMPLATE_FILES:
            for line in IR.block_lines(variant):
                with self.subTest(variant=variant, line=line[:40]):
                    self.assertEqual(IR.redact(line), line)


# --------------------------------------------------------------------------- markers and compose

class MarkerTests(unittest.TestCase):
    B = "<!-- fleet-lane-map:begin v1 -->"
    E = "<!-- fleet-lane-map:end -->"

    def test_no_markers(self) -> None:
        self.assertIsNone(IR.find_block("hello\nworld\n"))

    def test_one_block_and_its_version(self) -> None:
        loc = IR.find_block(f"a\n{self.B}\nx\n{self.E}\nz\n")
        self.assertEqual((loc.begin, loc.end, loc.version), (1, 3, 1))
        self.assertEqual(IR.find_block("<!-- fleet-lane-map:begin v7 -->\n<!-- fleet-lane-map:end -->").version, 7)

    def test_crlf_and_indented_markers(self) -> None:
        loc = IR.find_block(f"a\r\n  {self.B} \r\nx\r\n{self.E}\r\n")
        self.assertEqual((loc.begin, loc.end), (1, 3))

    def test_unbalanced_or_repeated_markers_raise(self) -> None:
        for text in (f"{self.B}\nx\n", f"x\n{self.E}\n", f"{self.E}\nx\n{self.B}\n",
                     f"{self.B}\n{self.E}\n{self.B}\n{self.E}\n", f"{self.B}\n{self.B}\n{self.E}\n"):
            with self.subTest(text=text), self.assertRaises(IR.MarkerError):
                IR.find_block(text)


class ComposeTests(unittest.TestCase):
    def test_missing_file_is_created_with_just_the_block(self) -> None:
        comp = IR.compose(None, "full")
        self.assertEqual((comp.action, comp.new_text), ("create", IR.block_text("full")))

    def test_missing_cursor_file_gets_frontmatter_then_block(self) -> None:
        text = IR.compose(None, "cursor").new_text
        self.assertTrue(text.startswith("---\n"))
        self.assertLess(text.index("alwaysApply: true"), text.index(IR.BEGIN_PREFIX))

    def test_empty_file_gets_the_block_only(self) -> None:
        comp = IR.compose("", "full")
        self.assertEqual((comp.action, comp.new_text), ("append", IR.block_text("full")))

    def test_append_keeps_every_original_byte_and_adds_one_blank_line(self) -> None:
        for original in ("# Rules\n\nSome text.\n", "# Rules\n\nNo trailing newline", "# Rules\n\n"):
            with self.subTest(original=original):
                comp = IR.compose(original, "full")
                self.assertEqual(comp.action, "append")
                self.assertTrue(comp.new_text.startswith(original))
                tail = comp.new_text[len(original):]
                self.assertTrue(tail.endswith(IR.block_text("full")))
                self.assertEqual((original + tail).count("\n\n\n"), 0)
                self.assertIn("\n\n" + IR.BEGIN_PREFIX, comp.new_text)

    def test_second_compose_is_a_no_op(self) -> None:
        once = IR.compose("# Rules\n", "full").new_text
        again = IR.compose(once, "full")
        self.assertEqual((again.action, again.new_text), ("none", once))

    def test_replace_changes_only_the_block(self) -> None:
        stale = "# Top\n\nkeep me\n\n" + IR.BEGIN_PREFIX + "1 -->\nold rule\n" + IR.END_LINE + "\n\n## After\nkeep too\n"
        comp = IR.compose(stale, "full")
        self.assertEqual(comp.action, "replace")
        self.assertTrue(comp.new_text.startswith("# Top\n\nkeep me\n\n"))
        self.assertTrue(comp.new_text.endswith("\n\n## After\nkeep too\n"))
        self.assertNotIn("old rule", comp.new_text)
        self.assertEqual(IR.compose(comp.new_text, "full").action, "none")

    def test_crlf_files_stay_crlf(self) -> None:
        crlf = "# Rules\r\n\r\ntext\r\n"
        out = IR.compose(crlf, "minimal").new_text
        self.assertTrue(out.startswith(crlf))
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertEqual(IR.compose(out, "minimal").action, "none")

    def test_end_marker_at_end_of_file_without_newline_stays_that_way(self) -> None:
        stale = "top\n" + IR.BEGIN_PREFIX + "1 -->\nold\n" + IR.END_LINE
        out = IR.compose(stale, "minimal").new_text
        self.assertTrue(out.endswith(IR.END_LINE))
        self.assertTrue(out.startswith("top\n"))

    def test_older_version_marker_is_replaced_and_newer_is_refused(self) -> None:
        old = "<!-- fleet-lane-map:begin v0 -->\nx\n" + IR.END_LINE + "\n"
        self.assertEqual(IR.compose(old, "full").action, "replace")
        newer = f"<!-- fleet-lane-map:begin v{IR.BLOCK_VERSION + 1} -->\nx\n" + IR.END_LINE + "\n"
        with self.assertRaises(IR.MarkerError):
            IR.compose(newer, "full")


# --------------------------------------------------------------------------- credentials

class RedactTests(unittest.TestCase):
    def test_key_value_pairs(self) -> None:
        # Made-up key=value lines, stored reversed so a secret scanner has no name-and-value
        # literal to match in the source; each is un-reversed here before use.
        reversed_lines = ("654fed321cba=YEK_IPA", "'laerton-ks'=YEK_IPA_IANEPO tropxe", "2retnuh :drowssap",
                          '"fedcba" = nekot', "t3rc3s=terces_tneilc")
        for line in (s[::-1] for s in reversed_lines):
            with self.subTest(line=line):
                out = IR.redact(line)
                self.assertIn("[REDACTED]", out)
                for leak in ("abc123def456", "notreal", "hunter2", "abcdef", "s3cr3t"):
                    self.assertNotIn(leak, out)

    def test_token_shapes(self) -> None:
        samples = ["sk-" + "a1b2c3d4e5f6g7h8i9j0", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2", "xoxb-" + "1234567890-abcdefghij",
                   "AKIA" + "ABCDEFGHIJKLMNOP", "Bearer " + "abcdefghijklmnop1234567890",
                   "eyJhbGciOiJI" + ".eyJzdWIiOiIx" + ".abcdefghij"]
        for tok in samples:
            with self.subTest(tok=tok[:8]):
                out = IR.redact(f"see {tok} here")
                self.assertNotIn(tok, out)
                self.assertIn("[REDACTED]", out)

    def test_url_credentials_use_the_doctor_redactor(self) -> None:
        out = IR.redact("clone https://jay:" + "s3cretpw" + "@github.com/o/r.git now")
        self.assertIn("https://REDACTED@github.com/o/r.git", out)
        self.assertNotIn("s3cretpw", out)

    def test_long_mixed_tokens_but_not_paths_or_words(self) -> None:
        self.assertIn("[REDACTED]", IR.redact("id " + "a1" * 25))
        for plain in ("~/apps/lanes/trading/claude-fix-the-very-long-slug-name-for-the-thing-here",
                      "a" * 60, "a plain sentence about the lane map and where checkouts live"):
            self.assertEqual(IR.redact(plain), plain)


# --------------------------------------------------------------------------- contradictions

class ContradictionTests(unittest.TestCase):
    def kinds(self, text: str) -> list[tuple[int, str]]:
        return [(c.line, c.kind) for c in IR.find_contradictions(text, registry=REGISTRY)]

    def test_flat_lane_paths_in_every_spelling(self) -> None:
        text = "\n".join([
            "| Socratic.Trade | `trading` | `~/apps/trading-claude` |",
            "see $HOME/apps/usage-codex for the lane",
            "or /Users/jay/apps/fleet-ag-fix-thing.",
            "old ~/apps/contactlogo-mm lane",
            "retired ~/apps/trading-monet lane",
            "ops lane ~/apps/fleet-ops-claude",
            "lanes are `~/apps/<prefix>-<seat>`.",
            "also the pattern <app>-<seat>",
        ])
        self.assertEqual(self.kinds(text), [(i, "flat-lane") for i in range(1, 9)])

    def test_v2_lanes_and_tools_are_not_contradictions(self) -> None:
        text = "\n".join([
            "~/apps/lanes/Socratic-Trade/claude-fix-thing",
            "~/apps/lanes/Congress.Trade/review-pr-12",
            "~/apps/lanes/_codex/fix/BotFleet and ~/apps/lanes/BotFleet/active-engines-display-e380b8",
            "/Users/jay/apps/lanes/AI-Fleet-Coordinator/claude-x and ~/apps/lanes/congress-trading-shared/cursor-y",
            "~/apps/lanes/homebrew-tap/codex-x",
            "Printer: ~/apps/fleet-mode and ~/apps/agent-sync/consumer.mjs",
            "run ~/apps/lane new fleet my-slug",
            "~/apps/scratch/claude/topic",
            "~/Code/Socratic-Trade is the integration tree",
            "branches are `<seat>/<slug>`",
            "~/apps/apple-notes-coding.sh and ~/apps/AGENT-SYNC.md",
        ])
        self.assertEqual(self.kinds(text), [])

    def test_the_pre_v2_lane_folders_are_reported(self) -> None:
        text = "\n".join([
            "~/apps/lanes/trading/claude-fix-thing",                   # 1: an old prefix folder
            "see $HOME/apps/lanes/fleet/ for the lanes",               # 2
            "~/apps/lanes/_review/fleet/pr-12",                        # 3: the old review root
            "Files go in ~/apps/lanes/_managed/codex.",                # 4
            "lanes are `~/apps/lanes/<prefix>/<seat>-<slug>`.",        # 5: the placeholder
            "/Users/jay/apps/lanes/contactlogo/claude-x",              # 6: the case-only old spelling
            "~/apps/lanes/_notes/x and ~/apps/lanes/dealdex/y",        # 7
            "~/apps/lanes/ContactLogo/claude-x",                       # fine
            "~/apps/lanes/AI-Fleet-Coordinator/claude-x",              # fine
        ])
        self.assertEqual(self.kinds(text), [(i, "old-lane-layout") for i in range(1, 8)])
        snippets = [c.snippet for c in IR.find_contradictions(text, registry=REGISTRY)]
        self.assertEqual(snippets[0], "~/apps/lanes/trading")
        self.assertEqual(snippets[2], "~/apps/lanes/_review")

    def test_old_lane_layout_with_the_real_registry(self) -> None:
        real = L.load_registry(env={})
        text = "\n".join(["~/apps/lanes/botfleet/claude-x", "~/apps/lanes/fleet-ops/minimax-x",
                          "~/apps/lanes/BotFleet/claude-x", "~/apps/lanes/Fleet-OPS/minimax-x",
                          "~/apps/lanes/homebrew-tap/codex-x", "~/apps/lanes/congress-trading-shared/cursor-x"])
        self.assertEqual([(c.line, c.kind) for c in IR.find_contradictions(text, registry=real)],
                         [(1, "old-lane-layout"), (2, "old-lane-layout")])

    def test_old_lane_layout_without_a_registry(self) -> None:
        got = IR.find_contradictions("~/apps/lanes/trading/claude-x and ~/apps/lanes/BotFleet/claude-y\n", registry=None)
        self.assertEqual([(c.kind, c.snippet) for c in got], [("old-lane-layout", "~/apps/lanes/trading")])

    def test_a_file_with_the_old_text_outside_the_block_is_reported_by_plan(self) -> None:
        text = "| Socratic.Trade | `trading` | `~/apps/lanes/trading/claude-x` |\n"
        (c,) = IR.find_contradictions(text, registry=REGISTRY)
        self.assertEqual(c.kind, "old-lane-layout")

    def test_temp_checkout_advice(self) -> None:
        text = "\n".join([
            "git worktree add /tmp/x origin/main",
            "clone it into $TMPDIR/repo and build",
            "mktemp -d, then git clone the repo",
            "checkout a fresh copy under /private/tmp/work",
            "Never clone into /tmp",
            "Do not put a worktree in /var/folders",
            "Scratch output goes in /private/tmp/claude-501/scratchpad",
            "use mktemp for download buffers",
        ])
        self.assertEqual(self.kinds(text), [(i, "tmp-checkout") for i in (1, 2, 3, 4)])

    def test_legacy_agent_prefix_is_a_neutral_mention(self) -> None:
        text = "branches: `agent/claude` and agent/antigravity\nthe user-agent/1.0 header\nsee apps/agent/foo\n"
        self.assertEqual(self.kinds(text), [(1, "legacy-agent-prefix-mention")])

    def test_line_numbers_snippets_and_redaction(self) -> None:
        text = "intro\n\nexport SOME_TOKEN=" + "abcd1234efgh " + "and ~/apps/trading-claude\n"
        (c,) = IR.find_contradictions(text, registry=REGISTRY)
        self.assertEqual((c.line, c.kind), (3, "flat-lane"))
        self.assertEqual(c.snippet, "~/apps/trading-claude")      # the matched text only: the token beside it is not echoed
        self.assertNotIn("abcd1234efgh", c.snippet)
        self.assertNotIn("SOME_TOKEN", c.snippet)
        many = " ".join(f"~/apps/trading-claude-{i}" for i in range(30))
        (c2,) = IR.find_contradictions(many, registry=REGISTRY)
        self.assertLessEqual(len(c2.snippet), 110)
        self.assertTrue(c2.snippet.endswith("..."))
        # the redactor still runs over what is printed
        self.assertNotIn("sk-" + "a1b2c3d4e5f6g7h8i9j0", IR._snippet(["sk-" + "a1b2c3d4e5f6g7h8i9j0"]))

    def test_our_own_block_is_never_reported(self) -> None:
        text = "# Rules\n\n" + IR.block_text("full") + "\n~/apps/trading-claude is mentioned after the block\n"
        loc = IR.find_block(text)
        got = IR.find_contradictions(text, registry=REGISTRY, skip=loc)
        self.assertEqual([c.line for c in got], [text.count("\n", 0, text.index("is mentioned")) + 1])
        # without `skip` the block's own placeholder line would be reported
        self.assertGreater(len(IR.find_contradictions(text, registry=REGISTRY)), len(got))

    def test_works_without_a_registry(self) -> None:
        got = IR.find_contradictions("lane ~/apps/trading-claude\n", registry=None)
        self.assertEqual([c.kind for c in got], ["flat-lane"])


# --------------------------------------------------------------------------- plan

class PlanTests(HomeCase):
    def test_missing_file_without_create_is_refused_but_previewed(self) -> None:
        os.makedirs(self.path(".codex"))
        code, out, _ = self.run_cli("plan", "codex")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("exists: no", out)
        self.assertIn("apply would: create", out)
        self.assertIn("pass --create", out)
        self.assertIn("+" + IR.BEGIN_PREFIX + f"{IR.BLOCK_VERSION} -->", out)
        self.assertIn("--- /dev/null", out)

    def test_missing_file_and_missing_platform_dir_is_refused_even_with_create(self) -> None:
        code, out, _ = self.run_cli("plan", "codex", "--create")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("does not look installed", out)

    def test_missing_file_with_create_has_nothing_to_complain_about(self) -> None:
        os.makedirs(self.path(".fx"))
        _, out, _ = self.run_cli("plan", "fx", "--create")
        self.assertIn("apply would: create", out)
        self.assertNotIn("REFUSED", out)

    def test_empty_file(self) -> None:
        self.write(".fx/AGENTS.md", "")
        _, out, _ = self.run_cli("plan", "fx")
        self.assertIn("exists: yes   size: 0 bytes", out)
        self.assertIn("marker block: absent", out)
        self.assertIn("apply would: append", out)
        self.assertNotIn("REFUSED", out)

    def test_populated_file_diff_is_exactly_the_block(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["codex"], self.home, registry=REGISTRY)
        self.assertEqual(entry.action, "append")
        added = [ln[1:] for ln in entry.diff[2:] if ln.startswith("+")]
        removed = [ln for ln in entry.diff[2:] if ln.startswith("-")]
        self.assertEqual(removed, [])
        self.assertEqual([ln for ln in added if ln], [ln for ln in IR.block_lines("full") if ln])
        self.assertTrue(entry.diff[0].startswith("--- a/") and entry.diff[1].startswith("+++ b/"))
        self.assertTrue(entry.diff[2].startswith("@@"))

    def test_already_installed_file_reports_current_and_no_diff(self) -> None:
        self.write(".codex/AGENTS.md", "# Codex rules\n\nNo lane paths in here.\n")
        self.assertEqual(self.run_cli("apply", "codex")[0], IR.EXIT_OK)
        code, out, _ = self.run_cli("plan", "codex")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("marker block: current", out)
        self.assertIn("apply would change: nothing", out)
        self.assertNotIn("    diff:", out)
        self.assertIn("none found", out)      # our own block mentions ~/apps/<prefix>-<seat> and is not reported

    def test_lines_outside_an_installed_block_are_still_reported(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        self.assertEqual(self.run_cli("apply", "codex")[0], IR.EXIT_OK)
        _, out, _ = self.run_cli("plan", "codex")
        self.assertIn("marker block: current", out)
        self.assertEqual(len(re.findall(r"\[flat-lane\]", out)), 2)         # the two table rows, not our block

    def test_stale_block_diff_shows_the_old_and_new_lines(self) -> None:
        self.write(".grok/GROK.md", "# Grok\n\n" + IR.BEGIN_PREFIX + "1 -->\nthe OLD rule line\n" + IR.END_LINE + "\n\ntail\n")
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["grok"], self.home, registry=REGISTRY)
        self.assertEqual((entry.action, entry.block), ("replace", "stale"))
        self.assertIn("-the OLD rule line", entry.diff)
        self.assertTrue(any(ln.startswith("+## Lane Map") for ln in entry.diff))

    def test_plan_never_writes(self) -> None:
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        self.write(".fx/AGENTS.md", "")
        self.write(".minimax/memory/user.md", MINIMAX_SAMPLE)
        os.makedirs(self.path(".cursor/rules"))
        os.makedirs(self.path(".grok"))
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(outside.name, "other.md").write_text("elsewhere\n")
        os.symlink(os.path.join(outside.name, "other.md"), self.path(".gemini-link"))
        before = snapshot(self.home)
        for argv in (("plan",), ("plan", "--create", "--i-own-this-file"), ("plan", "claude", "--i-own-this-file"),
                     ("apply", "--dry-run", "codex", "cursor", "--create"), ("verify", "codex"), ()):
            with self.subTest(argv=argv):
                self.run_cli(*argv)
                self.assertEqual(snapshot(self.home), before)

    def test_claude_is_skipped_in_a_default_plan_and_refused_when_named(self) -> None:
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("SKIPPED", out)
        self.assertIn("--i-own-this-file", out)
        self.assertNotIn("trading-claude", out)          # the file was not even read
        code, out, _ = self.run_cli("plan", "claude")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertNotIn("trading-claude", out)
        code, out, _ = self.run_cli("plan", "claude", "--i-own-this-file")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("apply would: append", out)

    def test_contradictions_section_lists_line_numbers_and_snippets(self) -> None:
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        _, out, _ = self.run_cli("plan", "claude", "--i-own-this-file")
        self.assertIn("CONTRADICTIONS (report only; never edited automatically):", out)
        lines = CLAUDE_SAMPLE.split("\n")
        for number, needle in ((7, "<prefix>-<seat>"), (11, "trading-claude"), (12, "congress-claude"), (13, "usage-claude")):
            self.assertIn(needle, lines[number - 1])
            self.assertRegex(out, rf"line {number}  \[flat-lane\]  .*{re.escape(needle)}")
        self.assertEqual(len(re.findall(r"\[flat-lane\]", out)), 4)

    def test_clean_file_has_no_contradictions(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n\nNothing here.\n")
        _, out, _ = self.run_cli("plan", "fx")
        self.assertIn("CONTRADICTIONS", out)
        self.assertIn("none found", out)

    def test_diff_context_is_redacted(self) -> None:
        secret = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3"
        self.write(".fx/AGENTS.md", f"# Fx\n\nOPENAI_API_KEY={secret}\nSERVICE_TOKEN: {'abc' * 10}\n")
        _, out, _ = self.run_cli("plan", "fx")
        self.assertNotIn(secret, out)
        self.assertNotIn("abc" * 10, out)
        self.assertNotIn("OPENAI_API_KEY", out)                 # the line is not echoed at all, redacted or not
        self.assertNotIn("SERVICE_TOKEN", out)
        self.assertIn("unchanged line", out)

    def test_codex_size_warning_and_cap(self) -> None:
        fill = "x" * 79 + "\n"
        near = fill * 380                                   # 30,400 bytes: new size passes 30 KiB
        self.write(".codex/AGENTS.md", near)
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["codex"], self.home, registry=REGISTRY)
        self.assertEqual(entry.status, "ok")
        self.assertTrue(any("30720" in w and "32 KiB" in w for w in entry.warnings), entry.warnings)
        _, out, _ = self.run_cli("plan", "codex")
        self.assertIn("WARNING", out)
        small = fill * 100
        self.write(".codex/AGENTS.md", small)
        self.assertEqual(IR.build_entry(IR.PLATFORM_BY_NAME["codex"], self.home).warnings, [])
        over = fill * 410                                   # 32,800 bytes already: any block busts the cap
        self.write(".codex/AGENTS.md", over)
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["codex"], self.home, registry=REGISTRY)
        self.assertEqual(entry.status, "refused")
        self.assertTrue(any("32768-byte cap" in r for r in entry.refusals))

    def test_only_codex_and_clutch_have_a_cap(self) -> None:
        caps = {p.name: p.size_cap for p in IR.PLATFORMS}
        self.assertEqual(caps["codex"], 32 * 1024)
        self.assertEqual(caps["clutch"], 64 * 1024)        # the engine caps the whole baseline at 64 KiB
        self.assertEqual([n for n, c in caps.items() if c is not None], ["codex", "clutch"])

    def test_cursor_create_path(self) -> None:
        os.makedirs(self.path(".cursor"))
        _, out, _ = self.run_cli("plan", "cursor")
        self.assertIn("pass --create", out)
        _, out, _ = self.run_cli("plan", "cursor", "--create")
        self.assertIn("apply would: create", out)
        self.assertIn("+alwaysApply: true", out)
        self.assertNotIn("REFUSED", out)

    def test_existing_cursor_file_with_bad_frontmatter_warns(self) -> None:
        self.write(".cursor/rules/fleet-lane-map.mdc", "---\ndescription: x\nalwaysApply: false\n---\n\nold\n")
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["cursor"], self.home)
        self.assertTrue(any("alwaysApply: true" in w for w in entry.warnings))

    def test_muse_code_is_listed_as_unsupported(self) -> None:
        code, out, _ = self.run_cli("plan")
        self.assertIn("== muse-code", out)
        self.assertIn("UNSUPPORTED: no known user-level rules file (UNVERIFIED)", out)
        self.assertEqual(code, IR.EXIT_OK)

    def test_symlink_outside_home_is_refused_and_never_read(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name, "private.md")
        target.write_text("TOP-SECRET-LINE-12345\n")
        os.makedirs(self.path(".fx"))
        os.symlink(target, self.path(".fx/AGENTS.md"))
        _, out, _ = self.run_cli("plan", "fx")
        self.assertIn("REFUSED", out)
        self.assertIn("outside", out)
        self.assertNotIn("TOP-SECRET", out)

    def test_inspect_file_does_not_read_through_a_symlink_outside_home(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name, "private.md")
        target.write_text("not ours\n")
        os.makedirs(self.path(".fx"))
        os.symlink(target, self.path(".fx/AGENTS.md"))
        state = IR.inspect_file(IR.PLATFORM_BY_NAME["fx"], self.home)
        self.assertTrue(state.refusals)
        self.assertIsNone(state.text)
        self.assertIsNone(state.raw)
        result = IR.verify_platform(IR.PLATFORM_BY_NAME["fx"], self.home)
        self.assertFalse(result.ok)

    def test_the_home_level_agents_md_is_a_platform_and_an_owner_file(self) -> None:
        # ~/AGENTS.md carried a hand-copied block that no platform row refreshed
        p = IR.PLATFORM_BY_NAME["home-agents"]
        self.assertEqual((p.rel_path, p.variant, p.owner_file, p.root_dir), ("AGENTS.md", "full", True, ""))
        self.write("AGENTS.md", "# Home\n\n" + IR.BEGIN_PREFIX + "1 -->\nold v1 text\n" + IR.END_LINE + "\n\ntail\n")
        code, out, err = self.run_cli("plan", "home-agents")
        self.assertIn("owner's own file", out + err, "plan refuses it without the ownership flag")
        code, out, err = self.run_cli("plan", "home-agents", "--i-own-this-file")
        self.assertEqual(code, 0, out + err)
        self.assertIn("replace", out)
        code, out, err = self.run_cli("apply", "home-agents", "--i-own-this-file")
        self.assertEqual(code, 0, out + err)
        text = Path(self.path("AGENTS.md")).read_text(encoding="utf-8")
        self.assertIn(IR.BEGIN_PREFIX + f"{IR.BLOCK_VERSION} -->", text)
        self.assertNotIn("old v1 text", text)
        self.assertTrue(text.endswith("\ntail\n"))
        self.assertEqual(self.run_cli("verify", "home-agents")[0], 0)

    def test_default_plan_covers_every_platform(self) -> None:
        _, out, _ = self.run_cli("plan")
        for p in IR.PLATFORMS:
            self.assertIn(f"== {p.name}", out)
        self.assertIn("Summary:", out)

    def test_realistic_home_end_to_end_plan(self) -> None:
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        self.write(".minimax/memory/user.md", MINIMAX_SAMPLE)
        _, out, _ = self.run_cli("plan", "codex", "minimax")
        self.assertIn("line 7  [flat-lane]  ~/apps/trading-codex", out)          # the matched path, not the table row
        self.assertIn("line 8  [flat-lane]  ~/apps/fleet-codex", out)
        self.assertNotIn("| Socratic.Trade", out)
        self.assertNotIn("| ai-fleet-coordinator", out)
        self.assertIn("2 would change", out)


# --------------------------------------------------------------------------- apply

class ApplyTests(HomeCase):
    def test_apply_with_no_platform_names_is_a_usage_error(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        before = snapshot(self.home)
        for argv in (("apply",), ("apply", "--create", "--i-own-this-file")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, IR.EXIT_USAGE)
            self.assertIn("never defaults to all", err)
            self.assertEqual(out, "")
        self.assertEqual(snapshot(self.home), before)

    def test_unknown_platform_and_bad_home_are_usage_errors(self) -> None:
        self.assertEqual(self.run_cli("apply", "vim")[0], IR.EXIT_USAGE)
        err = io.StringIO()
        code = IR.main(["plan", "--home", self.path("nope")], out=io.StringIO(), err=err)
        self.assertEqual(code, IR.EXIT_USAGE)
        self.assertIn("not a directory", err.getvalue())

    def test_claude_needs_the_explicit_flag(self) -> None:
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        before = snapshot(self.home)
        code, out, _ = self.run_cli("apply", "claude")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("REFUSED", out)
        self.assertEqual(snapshot(self.home), before)
        code, out, _ = self.run_cli("apply", "claude", "--i-own-this-file")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("CHANGED", out)
        self.assertTrue(self.read(".claude/CLAUDE.md").startswith(CLAUDE_SAMPLE))
        self.assertTrue(self.read(".claude/CLAUDE.md").endswith(IR.block_text("full")))

    def test_append_writes_a_verified_backup_and_keeps_the_mode(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE, mode=0o640)
        code, out, _ = self.run_cli("apply", "codex", clock=clock_at("2026-10-07T14:30:05"))
        self.assertEqual(code, IR.EXIT_OK)
        names = self.files_in(".codex")
        self.assertEqual(names, ["AGENTS.md", "AGENTS.md.bak-lane-map-20261007-143005"])
        backup = self.path(".codex/AGENTS.md.bak-lane-map-20261007-143005")
        self.assertEqual(Path(backup).read_text(encoding="utf-8"), CODEX_SAMPLE)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.path(".codex/AGENTS.md")).st_mode), 0o640)
        self.assertEqual(self.read(".codex/AGENTS.md"), CODEX_SAMPLE + "\n" + IR.block_text("full"))
        self.assertIn("backup", out)

    def test_second_apply_is_a_no_op_with_no_new_backup(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE, mode=0o600)
        self.assertEqual(self.run_cli("apply", "codex")[0], IR.EXIT_OK)
        after_first = snapshot(self.home)
        self.assertEqual(len(self.files_in(".codex")), 2)
        code, out, _ = self.run_cli("apply", "codex", clock=clock_at("2027-01-01T00:00:00"))
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("UNCHANGED", out)
        self.assertIn("no backup", out)
        self.assertEqual(snapshot(self.home), after_first)         # bytes, mode and mtime all identical
        self.assertEqual(stat.S_IMODE(os.stat(self.path(".codex/AGENTS.md")).st_mode), 0o600)

    def test_block_text_change_is_replaced_in_place_with_a_new_backup(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n\nkeep this head\n")
        self.assertEqual(self.run_cli("apply", "fx", clock=clock_at("2026-10-07T14:30:00"))[0], IR.EXIT_OK)
        installed = self.read(".fx/AGENTS.md")
        self.write(".fx/AGENTS.md", installed + "\n## Appended later\nkeep this tail\n")
        edited = self.read(".fx/AGENTS.md")
        rules = Path(self._tmp.name + "-rules")
        shutil.copytree(IR.RULES_DIR, rules)
        self.addCleanup(shutil.rmtree, rules, True)
        (rules / "lane-map.full.md").write_text(REPLACED_BODY, encoding="utf-8")
        with mock.patch.object(IR, "RULES_DIR", rules):
            plan = self.run_cli("plan", "fx")[1]
            code, out, _ = self.run_cli("apply", "fx", clock=clock_at("2026-10-07T14:31:00"))
        self.assertIn("marker block: stale", plan)
        self.assertEqual(code, IR.EXIT_OK)
        now = self.read(".fx/AGENTS.md")
        self.assertIn("changed wording for the replace test", now)
        self.assertEqual(now.count(IR.BEGIN_PREFIX), 1)
        self.assertTrue(now.startswith("# Fx\n\nkeep this head\n\n" + IR.BEGIN_PREFIX))
        self.assertTrue(now.endswith(IR.END_LINE + "\n\n## Appended later\nkeep this tail\n"))
        self.assertNotIn("Old flat lanes", now)
        backups = [n for n in self.files_in(".fx") if ".bak-lane-map-" in n]
        self.assertEqual(backups, ["AGENTS.md.bak-lane-map-20261007-143000", "AGENTS.md.bak-lane-map-20261007-143100"])
        self.assertEqual(Path(self.path(".fx/" + backups[1])).read_text(encoding="utf-8"), edited)

    def test_backup_in_the_same_second_gets_a_numeric_suffix(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        frozen = clock_at("2026-10-07T14:30:00")
        self.run_cli("apply", "fx", clock=frozen)
        rules = Path(self._tmp.name + "-rules2")
        shutil.copytree(IR.RULES_DIR, rules)
        self.addCleanup(shutil.rmtree, rules, True)
        (rules / "lane-map.full.md").write_text(REPLACED_BODY, encoding="utf-8")
        with mock.patch.object(IR, "RULES_DIR", rules):
            self.run_cli("apply", "fx", clock=frozen)
        self.assertEqual([n for n in self.files_in(".fx") if ".bak-" in n],
                         ["AGENTS.md.bak-lane-map-20261007-143000", "AGENTS.md.bak-lane-map-20261007-143000-2"])

    def test_failed_rename_leaves_the_original_and_cleans_up(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE, mode=0o640)
        before = snapshot(self.home)
        with mock.patch.object(IR.os, "replace", side_effect=OSError("simulated rename failure")):
            code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_FAILED)
        self.assertIn("FAILED", out)
        self.assertIn("original left as it was", out)
        self.assertEqual(snapshot(self.home), before)              # no temp file, no stray backup
        self.assertEqual(self.files_in(".codex"), ["AGENTS.md"])

    def test_failed_write_before_the_rename_leaves_the_original(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        before = snapshot(self.home)
        real_fsync = os.fsync
        calls = {"n": 0}

        def flaky_fsync(fd):
            calls["n"] += 1
            if calls["n"] == 2:                  # call 1 is the backup, call 2 the temp file
                raise OSError("simulated disk failure")
            return real_fsync(fd)
        with mock.patch.object(IR.os, "fsync", side_effect=flaky_fsync):
            code, _, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_FAILED)
        self.assertEqual(snapshot(self.home), before)

    def test_atomic_write_removes_its_temp_file_on_any_failure(self) -> None:
        target = self.write("f.md", "original\n")
        with mock.patch.object(IR.os, "chmod", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                IR.atomic_write(target, b"new\n", 0o644)
        self.assertEqual(self.files_in("."), ["f.md"])
        self.assertEqual(Path(target).read_text(), "original\n")

    def test_symlink_outside_home_is_refused_and_left_alone(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name, "shared.md")
        target.write_text("shared\n")
        os.makedirs(self.path(".fx"))
        os.symlink(target, self.path(".fx/AGENTS.md"))
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("outside", out)
        self.assertEqual(target.read_text(), "shared\n")
        self.assertTrue(os.path.islink(self.path(".fx/AGENTS.md")))
        self.assertEqual(self.files_in(".fx"), ["AGENTS.md"])

    def test_symlink_inside_home_is_written_through_and_stays_a_symlink(self) -> None:
        real = self.write("dotfiles/fx-agents.md", "# Shared\n")
        os.makedirs(self.path(".fx"))
        os.symlink(real, self.path(".fx/AGENTS.md"))
        code, out, _ = self.run_cli("apply", "fx", clock=clock_at("2026-10-07T14:30:00"))
        self.assertEqual(code, IR.EXIT_OK)
        self.assertTrue(os.path.islink(self.path(".fx/AGENTS.md")))
        self.assertEqual(os.path.realpath(self.path(".fx/AGENTS.md")), os.path.realpath(real))
        self.assertTrue(self.read("dotfiles/fx-agents.md").startswith("# Shared\n"))
        self.assertIn(IR.BEGIN_PREFIX, self.read("dotfiles/fx-agents.md"))
        self.assertEqual(self.files_in("dotfiles"), ["fx-agents.md", "fx-agents.md.bak-lane-map-20261007-143000"])
        self.assertEqual(self.files_in(".fx"), ["AGENTS.md"])

    def test_two_platforms_sharing_one_target_apply_it_once(self) -> None:
        real = self.write("dotfiles/shared.md", "# Shared\n")
        for rel in (".fx", ".grok"):
            os.makedirs(self.path(rel))
        os.symlink(real, self.path(".fx/AGENTS.md"))
        os.symlink(real, self.path(".grok/GROK.md"))
        code, out, _ = self.run_cli("apply", "fx", "grok")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("FX: CHANGED".lower(), out.lower())
        self.assertIn("grok: unchanged", out.lower())
        self.assertEqual(self.read("dotfiles/shared.md").count(IR.BEGIN_PREFIX), 1)

    def test_dangling_and_directory_symlinks_are_refused(self) -> None:
        os.makedirs(self.path(".fx"))
        os.symlink(self.path("missing.md"), self.path(".fx/AGENTS.md"))
        code, out, _ = self.run_cli("apply", "fx", "--create")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("cannot be followed", out)
        os.unlink(self.path(".fx/AGENTS.md"))
        os.makedirs(self.path("somedir"))
        os.symlink(self.path("somedir"), self.path(".fx/AGENTS.md"))
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("not a regular file", out)

    def test_a_symlinked_platform_directory_pointing_outside_home_is_refused(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(outside.name, "AGENTS.md").write_text("# Fx\n")
        os.symlink(outside.name, self.path(".fx"))
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertEqual(Path(outside.name, "AGENTS.md").read_text(), "# Fx\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read anything")
    def test_unreadable_file_is_refused(self) -> None:
        p = self.write(".fx/AGENTS.md", "# Fx\n", mode=0o000)
        self.addCleanup(os.chmod, p, 0o644)
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("unreadable", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits")
    def test_read_only_file_and_read_only_directory_are_refused(self) -> None:
        p = self.write(".fx/AGENTS.md", "# Fx\n", mode=0o444)
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("read-only", out)
        self.assertEqual(self.read(".fx/AGENTS.md"), "# Fx\n")
        os.chmod(p, 0o644)
        os.chmod(self.path(".fx"), 0o555)
        self.addCleanup(os.chmod, self.path(".fx"), 0o755)
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("not writable", out)
        self.assertEqual(self.read(".fx/AGENTS.md"), "# Fx\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits")
    def test_verify_works_on_a_read_only_file_that_is_current(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        self.run_cli("apply", "fx")
        os.chmod(self.path(".fx/AGENTS.md"), 0o444)
        os.chmod(self.path(".fx"), 0o555)
        self.addCleanup(os.chmod, self.path(".fx"), 0o755)
        self.assertEqual(self.run_cli("verify", "fx")[0], IR.EXIT_OK)
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_OK)                    # nothing to change, so nothing to refuse
        self.assertIn("UNCHANGED", out)

    def test_binary_and_non_utf8_files_are_refused(self) -> None:
        for data, word in ((b"\xff\xfe\x00bad", "NUL"), (b"caf\xe9 not utf8\n", "UTF-8")):
            with self.subTest(word=word):
                self.write(".fx/AGENTS.md", data)
                code, out, _ = self.run_cli("apply", "fx")
                self.assertEqual(code, IR.EXIT_REFUSED)
                self.assertIn(word, out)
                self.assertEqual(Path(self.path(".fx/AGENTS.md")).read_bytes(), data)
                self.assertEqual(self.files_in(".fx"), ["AGENTS.md"])

    def test_malformed_or_newer_markers_are_refused_and_nothing_changes(self) -> None:
        for text in ("# Fx\n" + IR.BEGIN_PREFIX + "1 -->\nno end marker\n",
                     "# Fx\n" + IR.END_LINE + "\n",
                     "# Fx\n" + IR.BEGIN_PREFIX + "9 -->\nfuture\n" + IR.END_LINE + "\n"):
            with self.subTest(text=text[-30:]):
                self.write(".fx/AGENTS.md", text)
                before = snapshot(self.home)
                code, out, _ = self.run_cli("apply", "fx")
                self.assertEqual(code, IR.EXIT_REFUSED)
                self.assertEqual(snapshot(self.home), before)

    def test_codex_result_over_the_cap_is_refused_with_the_file_untouched(self) -> None:
        self.write(".codex/AGENTS.md", ("x" * 79 + "\n") * 410)
        before = snapshot(self.home)
        code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("32768-byte cap", out)
        self.assertEqual(snapshot(self.home), before)

    def test_codex_warning_is_printed_by_a_successful_apply(self) -> None:
        self.write(".codex/AGENTS.md", ("x" * 79 + "\n") * 380)
        code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("warning:", out)

    def test_missing_file_needs_create_and_gets_no_backup(self) -> None:
        os.makedirs(self.path(".fx"))
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertEqual(self.files_in(".fx"), [])
        code, out, _ = self.run_cli("apply", "fx", "--create")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("CREATED", out)
        self.assertIn("no backup", out)
        self.assertEqual(self.files_in(".fx"), ["AGENTS.md"])
        self.assertEqual(self.read(".fx/AGENTS.md"), IR.block_text("full"))
        self.assertEqual(stat.S_IMODE(os.stat(self.path(".fx/AGENTS.md")).st_mode), 0o644)

    def test_create_refuses_when_the_platform_does_not_look_installed(self) -> None:
        code, out, _ = self.run_cli("apply", "fx", "--create")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.path(".fx")))

    def test_cursor_file_is_created_only_with_create_then_stays_current(self) -> None:
        os.makedirs(self.path(".cursor"))
        code, _, _ = self.run_cli("apply", "cursor")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.path(".cursor/rules")))
        code, out, _ = self.run_cli("apply", "cursor", "--create")
        self.assertEqual(code, IR.EXIT_OK)
        text = self.read(".cursor/rules/fleet-lane-map.mdc")
        self.assertTrue(text.startswith("---\ndescription: "))
        self.assertIn("alwaysApply: true\n---\n\n" + IR.BEGIN_PREFIX, text)
        self.assertEqual(self.files_in(".cursor/rules"), ["fleet-lane-map.mdc"])
        code, out, _ = self.run_cli("apply", "cursor", "--create")
        self.assertIn("UNCHANGED", out)
        self.assertEqual(self.files_in(".cursor/rules"), ["fleet-lane-map.mdc"])

    def test_minimax_gets_the_minimal_block_and_other_platforms_the_full_one(self) -> None:
        self.write(".minimax/memory/user.md", MINIMAX_SAMPLE)
        self.write(".grok/GROK.md", "# Grok\n")
        self.assertEqual(self.run_cli("apply", "minimax", "grok")[0], IR.EXIT_OK)
        mm, grok = self.read(".minimax/memory/user.md"), self.read(".grok/GROK.md")
        self.assertTrue(mm.startswith(MINIMAX_SAMPLE))
        self.assertTrue(mm.endswith(IR.block_text("minimal")))
        self.assertNotIn("## Lane Map", mm)
        self.assertTrue(grok.endswith(IR.block_text("full")))

    def test_platform_aliases(self) -> None:
        self.write(".gemini/config/AGENTS.md", "# AG\n")
        self.write(".grok/GROK.md", "# Grok\n")
        self.write(".minimax/memory/user.md", "# MM\n")
        code, out, _ = self.run_cli("apply", "ag", "grok-build", "mm")
        self.assertEqual(code, IR.EXIT_OK)
        for rel in (".gemini/config/AGENTS.md", ".grok/GROK.md", ".minimax/memory/user.md"):
            self.assertIn(IR.BEGIN_PREFIX, self.read(rel))

    def test_one_refusal_does_not_stop_the_others(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        code, out, _ = self.run_cli("apply", "claude", "fx")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertEqual(self.read(".claude/CLAUDE.md"), CLAUDE_SAMPLE)
        self.assertIn(IR.BEGIN_PREFIX, self.read(".fx/AGENTS.md"))

    def test_apply_muse_code_is_refused(self) -> None:
        code, out, _ = self.run_cli("apply", "muse-code")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("unsupported", out)

    def test_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        before = snapshot(self.home)
        code, out, _ = self.run_cli("apply", "codex", "--dry-run")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("apply would: append", out)
        self.assertEqual(snapshot(self.home), before)

    def test_bytes_outside_the_block_survive_an_install_and_a_refresh(self) -> None:
        original = "﻿# Top — with a BOM and an em dash\r\n\r\nline\r\n"
        self.write(".fx/AGENTS.md", original.encode("utf-8"))
        self.run_cli("apply", "fx")
        installed = Path(self.path(".fx/AGENTS.md")).read_bytes()
        self.assertTrue(installed.startswith(original.encode("utf-8")))
        self.assertNotIn(b"\n", installed.replace(b"\r\n", b""))      # CRLF file stays CRLF


# --------------------------------------------------------------------------- verify

class VerifyTests(HomeCase):
    def test_verify_needs_platform_names(self) -> None:
        code, _, err = self.run_cli("verify")
        self.assertEqual(code, IR.EXIT_USAGE)
        self.assertIn("never defaults to all", err)

    def test_verify_passes_after_apply(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        self.write(".claude/CLAUDE.md", CLAUDE_SAMPLE)
        self.run_cli("apply", "codex", "claude", "--i-own-this-file")
        code, out, _ = self.run_cli("verify", "codex", "claude")          # verify needs no owner flag
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("codex: OK", out)
        self.assertIn("claude: OK", out)

    def test_verify_fails_for_absent_stale_malformed_and_missing(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        self.write(".grok/GROK.md", "# Grok\n" + IR.BEGIN_PREFIX + "1 -->\nold text\n" + IR.END_LINE + "\n")
        self.write(".fx/AGENTS.md", "# Fx\n" + IR.BEGIN_PREFIX + "1 -->\nno end\n")
        cases = {"codex": "no Lane Map block", "grok": "stale", "fx": "malformed", "antigravity": "does not exist"}
        for name, reason in cases.items():
            with self.subTest(name=name):
                code, out, _ = self.run_cli("verify", name)
                self.assertEqual(code, IR.EXIT_FAILED)
                self.assertIn(f"{name}: FAIL", out)
                self.assertIn(reason, out)

    def test_verify_checks_the_cursor_frontmatter(self) -> None:
        os.makedirs(self.path(".cursor"))
        self.run_cli("apply", "cursor", "--create")
        self.assertEqual(self.run_cli("verify", "cursor")[0], IR.EXIT_OK)
        text = self.read(".cursor/rules/fleet-lane-map.mdc").replace("alwaysApply: true", "alwaysApply: false")
        self.write(".cursor/rules/fleet-lane-map.mdc", text)
        code, out, _ = self.run_cli("verify", "cursor")
        self.assertEqual(code, IR.EXIT_FAILED)
        self.assertIn("alwaysApply: true", out)

    def test_verify_reads_the_file_again_after_a_change(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        self.run_cli("apply", "fx")
        self.assertEqual(self.run_cli("verify", "fx")[0], IR.EXIT_OK)
        self.write(".fx/AGENTS.md", self.read(".fx/AGENTS.md").replace("never guess your seat", "guess"))
        self.assertEqual(self.run_cli("verify", "fx")[0], IR.EXIT_FAILED)

    def test_verify_of_an_unsupported_platform_fails(self) -> None:
        self.assertEqual(self.run_cli("verify", "muse-code")[0], IR.EXIT_FAILED)

    def test_verify_never_writes(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        before = snapshot(self.home)
        self.run_cli("verify", "fx", "codex")
        self.assertEqual(snapshot(self.home), before)


# --------------------------------------------------------------------------- fleet basics (more harnesses)

def _load_seat_block_checker():
    """scripts/check-seat-blocks.py has a hyphen in its name, so load it by path."""
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "check-seat-blocks.py"
    spec = importlib.util.spec_from_file_location("check_seat_blocks", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FleetBasicsTests(HomeCase):
    """Clutch, Kimi, Vibe and Copilot CLI get the Fleet Basics ahead of the Lane Map in one marker block."""

    NEW = {"clutch": (".clutch/dsh/AGENTS.md", "clutch"), "kimi": (".kimi-code/AGENTS.md", "nodefault"),
           "vibe": (".vibe/AGENTS.md", "nodefault"), "copilot": (".copilot/copilot-instructions.md", "nodefault")}

    def test_the_new_platforms_name_their_file_and_variant(self) -> None:
        for name, (rel, variant) in self.NEW.items():
            with self.subTest(name=name):
                p = IR.PLATFORM_BY_NAME[name]
                self.assertEqual((p.rel_path, p.variant), (rel, variant))
                self.assertFalse(p.owner_file, "these files do not exist yet: nothing of the owner's to protect")

    def test_each_block_carries_basics_then_the_lane_map(self) -> None:
        for variant in ("clutch", "nodefault"):
            body = IR.body_text(variant)
            with self.subTest(variant=variant):
                self.assertTrue(body.startswith("## Fleet Basics\n"))
                for needle in ("/Users/jay/apps/AGENT-SYNC.md", "### Seat Identity", "## Lane Map: where every checkout lives",
                               "~/apps/lanes/<Repo>/<seat>-<slug>", "### Coordination", "### Land Your Work",
                               "### Secrets", "### Writing Anything a Human Reads", "sentence-gap", "`ps`",
                               "Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`"):
                    self.assertIn(needle, body)
                self.assertLess(body.index("### Seat Identity"), body.index("## Lane Map"))
                self.assertLess(body.index("## Lane Map"), body.index("### Secrets"))
                self.assertNotIn("{{", body, "every placeholder is filled")
                self.assertTrue(IR.body_text("full") in body, "the Lane Map text is the shared full text")

    def test_clutch_has_a_fixed_default_and_a_launcher_that_wins(self) -> None:
        body = IR.body_text("clutch")
        self.assertIn("Your default seat is CLUTCH", body)
        self.assertIn("then a seat a launcher assigned", body)
        self.assertIn("which beats this file whatever model you are", body)
        self.assertIn("clutch/<slug>", body)
        self.assertNotIn("no default seat", body.lower())

    def test_the_no_default_text_never_assigns_a_seat(self) -> None:
        body = IR.body_text("nodefault")
        self.assertIn("This tool has no default seat.", body)
        self.assertIn("ask Jay which seat you are", body)
        self.assertIn("never infer one from the folder, the branch, the model, a skill or another tool's rules file", body)
        for tag in ("CLAUDE", "CODEX", "CURSOR", "CLUTCH", "MONET", "RENOIR", "GROK", "AG", "MM", "FX", "MC"):
            self.assertNotRegex(body, r"\b" + tag + r"\b", f"{tag} must not appear as anyone's seat")
        self.assertNotIn("default seat is", body)

    def test_every_new_block_passes_the_seat_block_checker(self) -> None:
        checker = _load_seat_block_checker()
        for variant in ("clutch", "nodefault"):
            text = IR.block_text(variant)
            with self.subTest(variant=variant):
                self.assertEqual(checker.check_text(f"<{variant}>", text, needs_clause=True), [])
                self.assertIn("AGENT_SEAT", text, "the checker only demands the clause of a file that names AGENT_SEAT")

    def test_apply_creates_each_file_once_and_verify_passes(self) -> None:
        for name, (rel, _variant) in self.NEW.items():
            root = IR.PLATFORM_BY_NAME[name].root_dir
            os.makedirs(self.path(root), exist_ok=True)
            with self.subTest(name=name):
                code, out, err = self.run_cli("plan", name)
                self.assertIn("pass --create", out, out + err)
                code, out, err = self.run_cli("apply", name, "--create")
                self.assertEqual(code, IR.EXIT_OK, out + err)
                self.assertIn(f"{name}: CREATED", out)
                self.assertTrue(self.read(rel).startswith(IR.BEGIN_PREFIX + f"{IR.BLOCK_VERSION} -->\n## Fleet Basics\n"))
                self.assertEqual(self.run_cli("verify", name)[0], IR.EXIT_OK)
                before = snapshot(self.home)
                code, out, _ = self.run_cli("apply", name, "--create")
                self.assertEqual(code, IR.EXIT_OK)
                self.assertIn(f"{name}: UNCHANGED", out)
                self.assertEqual(snapshot(self.home), before, "a second apply writes nothing and makes no backup")

    def test_a_tool_that_is_not_installed_is_not_given_a_folder(self) -> None:
        for name in self.NEW:
            with self.subTest(name=name):
                code, out, _ = self.run_cli("apply", name, "--create")
                self.assertEqual(code, IR.EXIT_REFUSED)
                self.assertIn("does not look installed", out)
        self.assertEqual(os.listdir(self.home), [])
        # ~/.config is shared, so it proves nothing about OpenCode: its own folder has to be there
        self.assertEqual(IR.PLATFORM_BY_NAME["opencode"].root_dir, ".config/opencode")
        os.makedirs(self.path(".config/muse"))
        code, out, _ = self.run_cli("apply", "opencode", "--create")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("~/.config/opencode does not exist", out)
        self.assertFalse(os.path.exists(self.path(".config/opencode")))

    def test_the_owners_own_text_survives_and_a_backup_is_made(self) -> None:
        owner = "# My Vibe notes\n\nKeep answers short.\n"
        self.write(".vibe/AGENTS.md", owner)
        code, out, err = self.run_cli("apply", "vibe")
        self.assertEqual(code, IR.EXIT_OK, out + err)
        text = self.read(".vibe/AGENTS.md")
        self.assertTrue(text.startswith(owner + "\n" + IR.BEGIN_PREFIX))
        backups = [n for n in self.files_in(".vibe") if ".bak-lane-map-" in n]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.read(".vibe/" + backups[0]), owner)

    def test_clutch_is_capped_at_64_kib_and_a_big_file_is_refused(self) -> None:
        self.write(".clutch/dsh/AGENTS.md", "x" * (64 * 1024 + 10))
        code, out, _ = self.run_cli("apply", "clutch")
        self.assertEqual(code, IR.EXIT_REFUSED)
        self.assertIn("65536-byte cap", out)

    def test_clutch_block_is_small(self) -> None:
        self.assertLess(len(IR.block_text("clutch").encode("utf-8")), 8 * 1024,
                        "the engine drops the broadest file first when the 64 KiB baseline fills")

    def test_copilot_prints_its_unverified_caveat(self) -> None:
        os.makedirs(self.path(".copilot"))
        _, out, _ = self.run_cli("plan", "copilot", "--create")
        self.assertIn("WARNING: UNVERIFIED on this Mac", out)
        code, out, _ = self.run_cli("apply", "copilot", "--create")
        self.assertEqual(code, IR.EXIT_OK)
        self.assertIn("warning: UNVERIFIED on this Mac", out)

    def test_opencode_prints_its_double_load_caveat(self) -> None:
        os.makedirs(self.path(".config/opencode"))
        _, out, _ = self.run_cli("plan", "opencode", "--create")
        self.assertIn("WARNING: UNVERIFIED: OpenCode also walks AGENTS.md up", out)
        self.assertIn("the same Lane Map twice", out)

    def test_opencode_gets_a_fixed_seat_identity_then_the_lane_map(self) -> None:
        """OpenCode is the OPENCODE seat (owner, Sat, Oct 10, 2026), so its block names that default, puts a launcher's
        seat first and says a headless run or Conductor has no default.  It is not the whole Fleet Basics:  the live
        file is a whole copy of ~/AGENTS.md, which already holds those sections."""
        p = IR.PLATFORM_BY_NAME["opencode"]
        self.assertEqual((p.rel_path, p.variant), (".config/opencode/AGENTS.md", "opencode"))
        body = IR.body_text(p.variant)
        self.assertIn("Your default seat is OPENCODE", body)
        self.assertIn("opencode-bot@", body)
        self.assertIn("`opencode/`", body)
        self.assertIn("which beats this file whatever model you are", body)
        self.assertIn("Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`", body)
        self.assertIn("have no default", body)
        self.assertIn("Conductor", body)
        self.assertNotIn("## Fleet Basics", body)
        self.assertNotIn("### Coordination", body)
        self.assertNotIn("This tool has no default seat", body)
        self.assertNotIn("not a seat", body)
        self.assertIn("## Lane Map", body)
        self.assertLess(body.index("Your default seat is OPENCODE"), body.index("## Lane Map"))
        self.assertLess(len(IR.block_text("opencode").encode("utf-8")), 8 * 1024)
        os.makedirs(self.path(".config/opencode"))
        code, out, err = self.run_cli("apply", "opencode", "--create")
        self.assertEqual(code, IR.EXIT_OK, out + err)
        text = self.read(".config/opencode/AGENTS.md")
        self.assertIn("Your default seat is OPENCODE", text)
        self.assertIn("## Lane Map", text)
        self.assertNotIn("## Fleet Basics", text)
        self.assertEqual(self.run_cli("verify", "opencode")[0], IR.EXIT_OK)
        self.assertEqual(self.run_cli("apply", "opencode")[0], IR.EXIT_OK)
        self.assertEqual(self.read(".config/opencode/AGENTS.md"), text, "a second apply changes nothing")

    def test_opencode_seat_text_names_no_other_seat_as_its_own(self) -> None:
        """The check-seat-blocks rules for a rules file:  no bare export, no launcher write, a launcher clause."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "check_seat_blocks", os.path.join(os.path.dirname(__file__), "..", "..", "check-seat-blocks.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.check_text("opencode", IR.block_text("opencode"), needs_clause=True), [])
        self.assertEqual(mod.check_text("clutch", IR.block_text("clutch"), needs_clause=True), [])

    def test_no_default_text_does_not_list_opencode(self) -> None:
        """OpenCode may become a seat (board 87ca50fa), so the text that says what is not a seat must not name it."""
        self.assertNotIn("OpenCode", IR.body_text("nodefault"))

    def test_conductor_and_muse_code_are_explained_not_written(self) -> None:
        for name, needle in (("conductor", "Prompts"),
                             ("muse-code", "~/.claude/CLAUDE.md")):
            with self.subTest(name=name):
                code, out, _ = self.run_cli("plan", name)
                self.assertEqual(code, IR.EXIT_OK)
                self.assertIn(f"== {name}", out)
                self.assertIn("UNSUPPORTED:", out)
                self.assertIn(needle, out)
                code, out, _ = self.run_cli("apply", name)
                self.assertEqual(code, IR.EXIT_REFUSED)
                self.assertEqual(os.listdir(self.home), [])

    def test_short_names_resolve(self) -> None:
        names = [p.name for p in IR.resolve_platform_names(["mc", "muse", "kimi-code", "mistral-vibe", "copilot-cli", "dsh"])]
        self.assertEqual(names, ["muse-code", "kimi", "vibe", "copilot", "clutch"])


# --------------------------------------------------------------------------- command line

class CliTests(HomeCase):
    def test_no_command_means_plan(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        before = snapshot(self.home)
        for argv in ((), ("--create",)):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, IR.EXIT_OK)
            self.assertIn("read-only, nothing is written", out)
        self.assertEqual(snapshot(self.home), before)

    def test_exit_codes_are_distinct(self) -> None:
        self.assertEqual({IR.EXIT_OK, IR.EXIT_FAILED, IR.EXIT_REFUSED, IR.EXIT_USAGE}, {0, 1, 2, 64})

    def test_options_may_come_before_or_after_the_platforms(self) -> None:
        self.write(".fx/AGENTS.md", "# Fx\n")
        for argv in (["plan", "--home", self.home, "fx"], ["plan", "fx", "--home", self.home]):
            out = io.StringIO()
            self.assertEqual(IR.main(argv, out=out, err=io.StringIO(), registry=REGISTRY), IR.EXIT_OK)
            self.assertIn("== fx", out.getvalue())
            self.assertNotIn("== codex", out.getvalue())

    def test_unknown_option_is_a_usage_error_not_a_refusal(self) -> None:
        code, _, err = self.run_cli("plan", "--no-such-flag")
        self.assertEqual(code, IR.EXIT_USAGE)
        self.assertIn("install_rules:", err)

    def test_default_registry_loads_or_is_none(self) -> None:
        reg = IR.default_registry()
        self.assertTrue(reg is None or isinstance(reg, L.Registry))

    def test_module_is_stdlib_only(self) -> None:
        src = Path(IR.__file__).read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:from|import) ([\w.]+)", src, re.M))
        stdlib = {"__future__", "argparse", "datetime", "difflib", "os", "re", "stat", "sys", "tempfile",
                  "dataclasses", "pathlib", "typing"}
        self.assertEqual(imports - stdlib, {".", ".doctor"}, imports - stdlib)


# --------------------------------------------------------------------------- review round 2: confirmed findings
#
# Values that look like credentials are fakes assembled from pieces, so no literal here looks live.

STRIPE_FAKE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
STRIPE_RESTRICTED_FAKE = "rk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
GITLAB_FAKE = "glpat-" + "abcdefghij0123456789"
AWS_SECRET_FAKE = "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
TWILIO_FAKE = "AC" + "0123456789abcdef" + "0123456789abcdef"
JSON_KEY_LINE = '{"api_key": "' + 'abc123' + '"}'


class HomeArgumentTests(HomeCase):
    """An empty --home is a usage error, never the real home."""

    def setUp(self) -> None:
        super().setUp()
        self.real = self.path("realhome")                 # stands in for the real home: HOME points here
        self.write("realhome/.fx/AGENTS.md", "# Fx\n")

    def test_an_empty_home_is_a_usage_error_and_writes_nothing(self) -> None:
        before = snapshot(self.real)
        with mock.patch.dict(os.environ, {"HOME": self.real}):
            for argv in (("apply", "fx"), ("apply", "fx", "--create"), ("plan", "fx"), ("plan",), ("verify", "fx"),
                         ("apply", "grok", "--create")):
                with self.subTest(argv=argv):
                    code, out, err = self.run_cli(*argv, "--home", "", home=False)
                    self.assertEqual(code, IR.EXIT_USAGE, (out, err))
                    self.assertIn("--home", err)
        self.assertEqual(snapshot(self.real), before)

    def test_an_empty_home_variable_is_not_the_filesystem_root(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": ""}):
            for argv in (("plan",), ("verify", "fx")):
                with self.subTest(argv=argv):
                    code, out, err = self.run_cli(*argv, home=False)
                    self.assertEqual(code, IR.EXIT_USAGE, (out, err))

    def test_the_default_still_comes_from_the_home_variable(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": self.real}):
            code, out, err = self.run_cli("plan", "fx", home=False)
        self.assertEqual((code, err), (IR.EXIT_OK, ""), out)
        self.assertIn(self.real, out)


class CodexCapTests(HomeCase):
    """The block must END inside the bytes Codex reads; a current block past the cap is not installed."""

    CAP = 32 * 1024

    def block_len(self) -> int:
        return len(IR.block_text("full").encode("utf-8"))

    def file_with_block_at(self, prefix_bytes: int) -> str:
        prefix = "x" * (prefix_bytes - 1) + "\n" if prefix_bytes else ""
        self.write(".codex/AGENTS.md", prefix + IR.block_text("full"))
        return prefix

    def test_verify_fails_when_the_block_lies_past_the_cap(self) -> None:
        self.file_with_block_at(33 * 1024)
        code, out, _ = self.run_cli("verify", "codex")
        self.assertEqual(code, IR.EXIT_FAILED, out)
        self.assertIn("codex: FAIL", out)
        self.assertIn("32768", out)
        self.assertIn("beyond", out)

    def test_verify_fails_when_the_block_straddles_the_cap(self) -> None:
        self.file_with_block_at(self.CAP - self.block_len() // 2)
        code, out, _ = self.run_cli("verify", "codex")
        self.assertEqual(code, IR.EXIT_FAILED, out)
        self.assertIn("beyond", out)

    def test_the_block_may_end_exactly_on_the_cap(self) -> None:
        self.file_with_block_at(self.CAP - self.block_len() + 1)            # end marker's last byte is byte 32768
        self.assertEqual(self.run_cli("verify", "codex")[0], IR.EXIT_OK)
        self.file_with_block_at(self.CAP - self.block_len() + 2)            # one byte later
        self.assertEqual(self.run_cli("verify", "codex")[0], IR.EXIT_FAILED)

    def test_the_offset_counts_bytes_not_characters(self) -> None:
        self.write(".codex/AGENTS.md", "é" * 17000 + "\n" + IR.block_text("full"))       # 17,000 characters, 34,000 bytes
        code, out, _ = self.run_cli("verify", "codex")
        self.assertEqual(code, IR.EXIT_FAILED, out)

    def test_a_crlf_file_counts_its_carriage_returns(self) -> None:
        text = ("x" * 78 + "\r\n") * 400 + IR.block_text("full").replace("\n", "\r\n")        # 32,000 bytes of CRLF lines first
        self.write(".codex/AGENTS.md", text)
        code, out, _ = self.run_cli("verify", "codex")
        self.assertEqual(code, IR.EXIT_FAILED, out)

    def test_other_platforms_have_no_cap(self) -> None:
        self.write(".fx/AGENTS.md", "x" * 100000 + "\n" + IR.block_text("full"))
        self.assertEqual(self.run_cli("verify", "fx")[0], IR.EXIT_OK)

    def test_apply_does_not_call_a_block_past_the_cap_unchanged(self) -> None:
        self.file_with_block_at(33 * 1024)
        before = snapshot(self.home)
        code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_REFUSED, out)
        self.assertIn("REFUSED", out)
        self.assertNotIn("UNCHANGED", out)
        self.assertIn("32768-byte cap", out)
        self.assertEqual(snapshot(self.home), before)
        code, out, _ = self.run_cli("plan", "codex")
        self.assertIn("REFUSED", out)
        self.assertIn("32768-byte cap", out)

    def test_a_stale_block_near_the_top_of_a_long_file_can_be_refreshed(self) -> None:
        text = "# top\n\n" + IR.BEGIN_PREFIX + "1 -->\nthe OLD rule line\n" + IR.END_LINE + "\n\n" + ("x" * 79 + "\n") * 420
        self.assertGreater(len(text.encode("utf-8")), self.CAP)
        self.write(".codex/AGENTS.md", text)
        code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_OK, out)
        self.assertIn("warning:", out)
        self.assertEqual(self.run_cli("verify", "codex")[0], IR.EXIT_OK)
        self.assertNotIn("the OLD rule line", self.read(".codex/AGENTS.md"))
        self.assertTrue(self.read(".codex/AGENTS.md").endswith(("x" * 79 + "\n") * 420))     # bytes outside the block are intact

    def test_a_stale_block_past_the_cap_is_still_refused(self) -> None:
        text = ("x" * 79 + "\n") * 420 + IR.BEGIN_PREFIX + "1 -->\nthe OLD rule line\n" + IR.END_LINE + "\n"
        self.write(".codex/AGENTS.md", text)
        before = snapshot(self.home)
        code, out, _ = self.run_cli("apply", "codex")
        self.assertEqual(code, IR.EXIT_REFUSED, out)
        self.assertEqual(snapshot(self.home), before)


class PlanEchoTests(HomeCase):
    """plan prints none of the owner's lines, only our own added lines and the matched text of a contradiction."""

    def planted(self) -> list[str]:
        return ["intro line", "live key " + STRIPE_FAKE, STRIPE_RESTRICTED_FAKE + " restricted", AWS_SECRET_FAKE,
                "twilio " + TWILIO_FAKE, JSON_KEY_LINE, "gitlab " + GITLAB_FAKE,
                "see ~/apps/trading-claude with SERVICE_TOKEN=" + "abcd1234efgh" + " beside it", "last line before the append"]

    def test_plan_prints_no_line_of_the_users_file(self) -> None:
        self.write(".codex/AGENTS.md", "\n".join(self.planted()) + "\n")
        code, out, _ = self.run_cli("plan", "codex")
        self.assertEqual(code, IR.EXIT_OK, out)
        for secret in (STRIPE_FAKE, STRIPE_RESTRICTED_FAKE, AWS_SECRET_FAKE, TWILIO_FAKE, GITLAB_FAKE, "abc123", "abcd1234efgh",
                       "K7MDENG", "4eC39Hq"):
            self.assertNotIn(secret, out)
        for words in ("intro line", "live key", "restricted", "last line before the append", "beside it", "api_key"):
            self.assertNotIn(words, out)
        self.assertIn("unchanged line", out)
        self.assertIn("+" + IR.BEGIN_PREFIX + f"{IR.BLOCK_VERSION} -->", out)  # our own added lines are still shown
        self.assertRegex(out, r"line 8  \[flat-lane\]")                     # the contradiction is still located

    def test_a_contradiction_shows_the_matched_text_and_nothing_else(self) -> None:
        self.write(".fx/AGENTS.md", "export API_TOKEN=" + "abcd1234efgh" + " then cd ~/apps/trading-claude && ls\n"
                   "git clone the repo into /tmp/work\nuse agent/claude here\nlanes are ~/apps/<prefix>-<seat>\n")
        _, out, _ = self.run_cli("plan", "fx")
        self.assertIn("line 1  [flat-lane]  ~/apps/trading-claude", out)
        self.assertNotIn("abcd1234efgh", out)
        self.assertNotIn("export", out)
        self.assertRegex(out, r"line 2  \[tmp-checkout\]  /tmp \+ git clone")
        self.assertRegex(out, r"line 3  \[legacy-agent-prefix-mention\]  agent/claude")
        self.assertRegex(out, r"line 4  \[flat-lane\]  <prefix>-<seat>")

    def test_the_diff_keeps_hunk_headers_and_collapses_context(self) -> None:
        self.write(".codex/AGENTS.md", CODEX_SAMPLE)
        entry = IR.build_entry(IR.PLATFORM_BY_NAME["codex"], self.home, registry=REGISTRY)
        body = entry.diff[2:]
        self.assertTrue(body[0].startswith("@@"))
        for ln in body:
            self.assertTrue(ln.startswith(("@@", "+", "-", "   (")), ln)
        self.assertTrue(any("unchanged line" in ln for ln in body), body)
        for original in CODEX_SAMPLE.splitlines():
            if original.strip():
                self.assertFalse(any(original in ln for ln in body), original)

    def test_redact_covers_the_shapes_that_used_to_leak(self) -> None:
        for name, text, leak in (
                ("stripe", "x " + STRIPE_FAKE + " y", "4eC39Hq"), ("stripe restricted", "x " + STRIPE_RESTRICTED_FAKE, "4eC39Hq"),
                ("gitlab", "x " + GITLAB_FAKE + " y", "abcdefghij0123456789"), ("twilio", "x " + TWILIO_FAKE + " y", "0123456789abcdef"),
                ("aws 40 char", "key " + AWS_SECRET_FAKE + " end", "K7MDENG"),
                ("aws env", "AWS_SECRET_ACCESS_KEY=" + AWS_SECRET_FAKE, "K7MDENG"),
                ("json short value", JSON_KEY_LINE, "abc123"), ("json secret", '  "client_secret": "' + 'xyz' + '",', "xyz"),
                ("json token", '"auth_token": "' + 'q9' + '"', "q9"),
                ("short password", "DB_PASSWORD=" + "hunt3r2", "hunt3r2")):
            with self.subTest(name):
                out = IR.redact(text)
                self.assertNotIn(leak, out)
                self.assertIn("[REDACTED]", out)

    def test_redact_still_leaves_paths_words_and_our_own_text_alone(self) -> None:
        for plain in ("/usr/local/share/man/man1/something", "~/apps/lanes/trading/claude-fix-thing",
                      "the quick brown fox jumps over the lazy dog", "scripts/fleet_lanes/tests/test_install_rules.py"):
            self.assertEqual(IR.redact(plain), plain)
        for variant in ("full", "minimal", "cursor"):
            for ln in IR.block_lines(variant):
                self.assertEqual(IR.redact(ln), ln, ln)


if __name__ == "__main__":
    unittest.main()
