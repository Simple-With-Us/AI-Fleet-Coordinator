"""Source generator tests: a temp findings.db with the real schema shape, files on disk, notes."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from fleet_rag import notes_export, sources
from fleet_rag.notes_export import html_to_text, strip_title_line

FINDINGS_DDL = """
CREATE TABLE findings (
    id TEXT PRIMARY KEY, app TEXT NOT NULL, external_uid TEXT, source TEXT, title TEXT NOT NULL,
    severity TEXT, category TEXT, surface TEXT, description TEXT, recommended_fix TEXT,
    status TEXT NOT NULL DEFAULT 'open', addressed_by TEXT, resolution TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'review-finding', source_url TEXT, repo TEXT, reported_by TEXT,
    location TEXT, env TEXT, writeback_at TEXT, UNIQUE(app, external_uid));
CREATE TABLE comments (
    id TEXT PRIMARY KEY, finding_id TEXT NOT NULL REFERENCES findings(id), author TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL, location TEXT, env TEXT);
"""


def make_db(path: pathlib.Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(FINDINGS_DDL)
    con.execute(
        "INSERT INTO findings (id, app, title, severity, description, recommended_fix, status, "
        "created_at, updated_at, source_kind, reported_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "aaaa1111",
            "congress-trade",
            "No account deletion path",
            "P0",
            "Users cannot delete.",
            "Add delete_account.",
            "open",
            "2026-08-20T01:57:12.791030+00:00",
            "2026-08-20T01:57:12.791030+00:00",
            "review-finding",
            "Claude (Mac)",
        ),
    )
    con.execute(
        "INSERT INTO findings (id, app, title, severity, description, status, addressed_by, resolution, "
        "created_at, updated_at, source_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "bbbb2222",
            "API-Usage-Monitor",
            "Snapshot scheduler late",
            "P1",
            "Rows scheduled in the past.",
            "completed",
            "GROK",
            "Landed in #123.",
            "2026-08-21T03:00:00+00:00",
            "2026-08-22T08:42:11.038241+00:00",
            "effort-row",
        ),
    )
    con.execute(
        "INSERT INTO findings (id, app, title, description, status, created_at, updated_at, source_kind) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "cccc3333",
            "dealdex",
            "Issue with effort key",
            "Body line.\n\n<!-- effort-key: " + FAKE_EFFORT_HASH + " -->\nMore.",
            "open",
            "2026-08-24T00:00:00+00:00",
            "2026-08-24T00:00:00+00:00",
            "github-issue",
        ),
    )
    con.execute(
        "INSERT INTO comments (id, finding_id, author, text, created_at) VALUES (?,?,?,?,?)",
        ("c2", "aaaa1111", "GROK", "Second comment.", "2026-08-23T10:00:00+00:00"),
    )
    con.execute(
        "INSERT INTO comments (id, finding_id, author, text, created_at) VALUES (?,?,?,?,?)",
        ("c1", "aaaa1111", "CLAUDE", "First comment.", "2026-08-22T10:00:00+00:00"),
    )
    con.commit()
    con.close()


# Made-up fixtures, built at run time so no source line looks like a credential to gitleaks.
FAKE_EFFORT_HASH = "0123456789" * 4  # 40 hex characters
FAKE_SLACK = "xoxb-" + "1" * 12 + "-abcdefghijkl"


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "findings.db"
        make_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_docs(self) -> None:
        docs = {d.doc_id: d for d in sources.iter_board(self.db)}
        self.assertEqual(set(docs), {"board/aaaa1111", "board/bbbb2222", "board/cccc3333"})
        c = docs["board/cccc3333"]
        self.assertNotIn("effort-key", c.text_markdown)
        self.assertIn("Body line.\n\nMore.", c.text_markdown)
        self.assertEqual(c.seat, "FLEET")
        a = docs["board/aaaa1111"]
        self.assertEqual(a.source, "board")
        self.assertEqual(a.category, "finding")
        self.assertEqual(a.app, "congress-trade")
        self.assertEqual(a.seat, "CLAUDE")
        self.assertEqual(a.title, "No account deletion path")
        self.assertEqual(a.url, "")  # no mapping given: never a dead link
        self.assertEqual(a.created_at_ms, sources.parse_ts_ms("2026-08-20T01:57:12.791030+00:00"))
        # updated_at is the last comment, and comments come in chronological order
        self.assertEqual(a.updated_at_ms, sources.parse_ts_ms("2026-08-23T10:00:00+00:00"))
        self.assertLess(
            a.text_markdown.index("First comment."), a.text_markdown.index("Second comment.")
        )
        for needle in (
            "# No account deletion path",
            "Severity: P0",
            "## Description",
            "Users cannot delete.",
            "## Recommended fix",
            "### CLAUDE — 2026-08-22T10:00:00+00:00",
            "Board id: aaaa1111",
        ):
            self.assertIn(needle, a.text_markdown)
        b = docs["board/bbbb2222"]
        self.assertEqual(b.category, "lesson")
        self.assertEqual(b.app, "usage-monitor")
        self.assertEqual(b.seat, "GROK")
        self.assertIn("## Resolution\n\nLanded in #123.", b.text_markdown)
        self.assertEqual(b.updated_at_ms, sources.parse_ts_ms("2026-08-22T08:42:11.038241+00:00"))

    def test_url_is_the_linear_issue_from_the_migration_map(self) -> None:
        tsv = pathlib.Path(self.tmp.name) / "board-to-linear.tsv"
        tsv.write_text(
            "board_id\tsource_key\tteam\tlinear_identifier\tlinear_url\tlinear_id\n"
            "\teffortlog:x:1\tST\tST-1\thttps://linear.app/simple-with-us/issue/ST-1/x\tu1\n"
            "aaaa1111\tboard:aaaa1111\tCTS\tCTS-5\thttps://linear.app/simple-with-us/issue/CTS-5/y\tu2\n",
            encoding="utf-8",
        )
        urls = sources.load_board_url_map(tsv)
        self.assertEqual(urls, {"aaaa1111": "https://linear.app/simple-with-us/issue/CTS-5/y"})
        docs = {d.doc_id: d for d in sources.iter_board(self.db, url_map=urls)}
        self.assertEqual(
            docs["board/aaaa1111"].url, "https://linear.app/simple-with-us/issue/CTS-5/y"
        )
        self.assertEqual(
            docs["board/bbbb2222"].url, ""
        )  # unmapped row: empty, not mac.jays.services
        self.assertEqual(
            sources.load_board_url_map(pathlib.Path(self.tmp.name) / "missing.tsv"), {}
        )
        for d in docs.values():
            self.assertNotIn("mac.jays.services", d.url)

    def test_default_paths_point_at_the_retired_database(self) -> None:
        self.assertEqual(
            sources.BOARD_DB.parts[-3:], ("_retired", "mac-collab-20261019", "findings.db")
        ) if "FLEET_RAG_BOARD_DB" not in os.environ else None

    def test_limit_and_missing_db(self) -> None:
        self.assertEqual(len(list(sources.iter_board(self.db, limit=2))), 2)
        with self.assertRaises(FileNotFoundError):
            list(sources.iter_board(self.db.with_name("nope.db")))


class FileSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_effort_logs(self) -> None:
        (self.root / "SOCRATIC-TRADE-EFFORT-LOG.md").write_text("# ST log\n\n## Deployed\n- x\n")
        (self.root / "API-USAGE-MONITOR-EFFORT-LOG.md").write_text("# UM log\n")
        (self.root / "EFFORT-LOG-PROTOCOL.md").write_text("# Protocol\n")
        docs = {d.doc_id: d for d in sources.iter_effort_logs(self.root)}
        self.assertEqual(
            set(docs),
            {"effort-log/SOCRATIC-TRADE", "effort-log/API-USAGE-MONITOR", "effort-log/PROTOCOL"},
        )
        self.assertEqual(docs["effort-log/SOCRATIC-TRADE"].app, "socratic-trade")
        self.assertEqual(docs["effort-log/API-USAGE-MONITOR"].app, "usage-monitor")
        self.assertEqual(docs["effort-log/PROTOCOL"].app, "fleet")
        d = docs["effort-log/SOCRATIC-TRADE"]
        self.assertEqual(
            (d.source, d.category, d.seat, d.title), ("effort-log", "lesson", "FLEET", "ST log")
        )
        self.assertGreater(d.created_at_ms, 0)

    def test_docs_filters(self) -> None:
        repo = self.root / "repo"
        (repo / "docs" / "reviews").mkdir(parents=True)
        (repo / "docs" / "backups").mkdir()
        (repo / "node_modules" / "x").mkdir(parents=True)
        (repo / ".claude" / "skills" / "s1").mkdir(parents=True)
        (repo / "README.md").write_text("# Fleet coordinator\n\nhello\n")
        (repo / "docs" / "A.md").write_text("# A\n\nbody\n")
        (repo / "docs" / "reviews" / "2026-raw-dump.md").write_text("# raw\n")
        (repo / "docs" / "reviews" / "2026-review.md").write_text("# review\n")
        (repo / "docs" / "backups" / "old.md").write_text("# old\n")
        (repo / "docs" / "big.md").write_text("x" * (sources.DOC_MAX_BYTES + 1))
        (repo / "node_modules" / "x" / "README.md").write_text("# nm\n")
        (repo / ".claude" / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\n---\n# S1\n")
        extra = self.root / "FLEET-PROTOCOL.md"
        extra.write_text("# Agent sync\n")
        (repo / "docs" / "dist").mkdir()
        (repo / "docs" / "dist" / "out.md").write_text("# dist\n")
        (repo / "docs" / "vendor").mkdir()
        (repo / "docs" / "vendor" / "lib.md").write_text("# vendor\n")
        (repo / "docs" / "build").mkdir()
        (repo / "docs" / "build" / "gen.md").write_text("# build\n")
        docs = {
            d.path: d
            for d in sources.iter_docs(
                repo,
                self.root / "missing-ops",
                [extra],
                code_dir=None,
                apps_dir=None,
                grok_home=None,
            )
        }
        names = {pathlib.Path(p).name for p in docs}
        # the repo's .claude/skills copy is the skill source's job (mirror rule b)
        self.assertEqual(names, {"README.md", "A.md", "2026-review.md", "FLEET-PROTOCOL.md"})
        self.assertEqual(docs[str(repo / "docs" / "A.md")].category, "doc")
        # not a git repo -> no url, local doc id, mtime dates
        a = docs[str(repo / "docs" / "A.md")]
        self.assertEqual(a.url, "")
        self.assertTrue(a.doc_id.startswith("doc/local/"))
        self.assertGreater(a.created_at_ms, 0)
        self.assertEqual(a.title, "A")

    def test_skills_dedupe(self) -> None:
        for d in ("claude", "cursor"):
            (self.root / d / "same").mkdir(parents=True)
            (self.root / d / "same" / "SKILL.md").write_text("---\nname: same\n---\n# Same skill\n")
        (self.root / "cursor" / "only").mkdir()
        (self.root / "cursor" / "only" / "SKILL.md").write_text("# Only\n")
        docs = list(sources.iter_skills([self.root / "claude", self.root / "cursor"]))
        self.assertEqual(sorted(d.doc_id for d in docs), ["skill/claude/same", "skill/cursor/only"])
        self.assertTrue(all(d.category == "runbook" and d.seat == "FLEET" for d in docs))
        self.assertEqual(docs[0].title, "same")
        # same directory name, different content -> two docs with tree-unique ids (no collision)
        (self.root / "cursor" / "same" / "SKILL.md").write_text(
            "---\nname: same\n---\n# Same skill, edited\n"
        )
        docs = list(sources.iter_skills([self.root / "claude", self.root / "cursor"]))
        self.assertEqual(
            sorted(d.doc_id for d in docs),
            ["skill/claude/same", "skill/cursor/only", "skill/cursor/same"],
        )
        self.assertEqual(len({d.doc_id for d in docs}), len(docs))
        # the real trees map to claude / cursor
        self.assertEqual(sources.skill_tree(pathlib.Path.home() / ".claude" / "skills"), "claude")
        self.assertEqual(sources.skill_tree(pathlib.Path.home() / ".cursor" / "skills"), "cursor")
        self.assertEqual(sources.skill_tree(self.root / "claude"), "claude")

    def test_memory(self) -> None:
        proj = self.root / "projects" / "-Users-jay-Code-Socratic-Trade" / "memory"
        proj.mkdir(parents=True)
        (proj / "MEMORY.md").write_text("# index\n")
        (proj / "pref.md").write_text(
            "---\nname: pref\nmetadata:\n  type: feedback\n---\n\nOwner wants X.\n"
        )
        (proj / "infra.md").write_text(
            "---\nname: infra\ntype: reference\n---\n\nQdrant lives at Y.\n"
        )
        (proj / "plain.md").write_text("# Plain\n\nno frontmatter\n")
        codex = self.root / "codex"
        codex.mkdir()
        (codex / "MEMORY.md").write_text("# Task group\n\nscope: x\n")
        docs = {d.doc_id: d for d in sources.iter_memory(self.root / "projects", codex)}
        self.assertEqual(
            set(docs),
            {
                "memory/claude/code-socratic-trade/pref.md",
                "memory/claude/code-socratic-trade/infra.md",
                "memory/claude/code-socratic-trade/plain.md",
                "memory/codex/MEMORY.md",
            },
        )
        self.assertEqual(docs["memory/claude/code-socratic-trade/pref.md"].category, "preference")
        self.assertEqual(
            docs["memory/claude/code-socratic-trade/infra.md"].category, "infrastructure"
        )
        self.assertEqual(docs["memory/claude/code-socratic-trade/plain.md"].category, "lesson")
        self.assertEqual(docs["memory/claude/code-socratic-trade/pref.md"].app, "socratic-trade")
        self.assertEqual(docs["memory/claude/code-socratic-trade/pref.md"].seat, "CLAUDE")
        self.assertEqual(docs["memory/codex/MEMORY.md"].seat, "CODEX")
        self.assertEqual(docs["memory/claude/code-socratic-trade/plain.md"].title, "Plain")


class NoteTests(unittest.TestCase):
    def test_title_parse(self) -> None:
        self.assertEqual(
            sources.parse_note_title("[UM, Grok] TestFlight"), ("usage-monitor", "GROK")
        )
        self.assertEqual(
            sources.parse_note_title("[ST, CT, Monet] digests"), ("socratic-trade", "MONET")
        )
        self.assertEqual(sources.parse_note_title("[FLEET, Claude] review"), ("fleet", "CLAUDE"))
        self.assertEqual(
            sources.parse_note_title("[CTS, Antigravity] x"), ("congress-trading-shared", "AG")
        )
        self.assertEqual(sources.parse_note_title("[BF, AG] y"), ("botfleet", "AG"))
        self.assertEqual(sources.parse_note_title("[DD, Cursor] z"), ("dealdex", "CURSOR"))
        self.assertEqual(sources.parse_note_title("[Codex] z"), ("fleet", "CODEX"))
        self.assertEqual(sources.parse_note_title("New Note"), ("fleet", "FLEET"))

    def test_title_parse_resolves_the_seat_tag_the_new_convention_requires(self) -> None:
        """AFC-422:  the title is `[TEAM_KEY, Agent] Topic`, so the seat arrives as the agent name.
        Before the fix only a few lowercase aliases resolved;  `GROK-WEB`, `MA`, `FX` and the rest
        were read as team keys and the note lost its seat attribution entirely."""
        for title, expected in (
            ("[AFC, GROK-WEB] Topic", ("afc", "GROK-WEB")),
            ("[AFC, MM] Notes", ("afc", "MM")),
            ("[AFC, CLAUDE] x", ("afc", "CLAUDE")),
            ("[AFC, MA] x", ("afc", "MA")),
            ("[AFC, MC] x", ("afc", "MC")),
            ("[AFC, FX] x", ("afc", "FX")),
            ("[AFC, WISE] x", ("afc", "WISE")),
            ("[AFC, ECHO] x", ("afc", "ECHO")),
            ("[AFC, OPENCODE] x", ("afc", "OPENCODE")),
            ("[AFC, CLUTCH] x", ("afc", "CLUTCH")),
            ("[AFC, GROK-BOT] x", ("afc", "GROK-BOT")),
            ("[AFC, Grok Web] x", ("afc", "GROK-WEB")),
            ("[AFC, MiniMax] x", ("afc", "MM")),
            ("[AFC, Muse Assist] x", ("afc", "MA")),
            ("[AFC, GB-Fixer] x", ("afc", "GB-FIXER")),
            ("[AFC, BF-Deployer] x", ("afc", "BF-DEPLOYER")),
            ("[AFC, CT, MM] x", ("afc", "MM")),
            ("[AFC] no seat", ("afc", "FLEET")),
        ):
            with self.subTest(title):
                self.assertEqual(sources.parse_note_title(title), expected)

    def test_grok_build_is_one_seat_with_grok(self) -> None:
        """Owner 2026-10-10:  the Mac Grok TUI and Grok Build are one seat and everything signs GROK."""
        self.assertEqual(sources.parse_note_title("[AFC, GROK-BUILD] x"), ("afc", "GROK"))
        self.assertEqual(sources.parse_note_title("[AFC, Grok Build] x"), ("afc", "GROK"))

    def test_html_to_text(self) -> None:
        html = (
            '<div><b><span style="font-size: 24px">[UM, Grok] Title</span></b></div>'
            "<style>.x{}</style><div>Tue, Sep 1, 3:01pm</div><div><br></div>"
            "<div>Line one &amp; two.</div><ul><li>alpha</li><li>beta</li></ul><p>End.</p>"
        )
        text = html_to_text(html)
        self.assertIn("[UM, Grok] Title\nTue, Sep 1, 3:01pm", text)
        self.assertIn("Line one & two.", text)
        self.assertIn("- alpha\n- beta", text)
        self.assertNotIn(".x{}", text)
        self.assertEqual(
            strip_title_line(text, "[UM, Grok] Title").split("\n")[0], "Tue, Sep 1, 3:01pm"
        )
        # entities are decoded exactly once: a literal "&amp;amp;" in a note is "&amp;" as text
        self.assertEqual(html_to_text("<div>a &amp;amp; b &lt;tag&gt;</div>"), "a &amp; b <tag>")

    def test_iter_apple_notes(self) -> None:
        recs = [
            {
                "id": "x-coredata://ABC/ICNote/p1",
                "name": "[ST, Monet] Plan",
                "created": "2026-08-09T15:52:00",
                "modified": "2026-08-10T09:00:00",
                "text": "[ST, Monet] Plan\nSun, Aug 9, 3:52pm\n\nBody text long enough.",
            },
            {
                "id": "x-coredata://ABC/ICNote/p2",
                "name": "New Note",
                "created": "",
                "modified": "",
                "text": "New Note",
            },
        ]
        docs = list(sources.iter_apple_notes(recs))
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual(d.doc_id, "note/ABC/ICNote/p1")
        self.assertEqual(
            (d.source, d.category, d.app, d.seat), ("apple-note", "note", "socratic-trade", "MONET")
        )
        self.assertTrue(d.text_markdown.startswith("Sun, Aug 9"))
        self.assertGreater(d.updated_at_ms, d.created_at_ms)


class NotesCacheTests(unittest.TestCase):
    META = [
        {
            "id": "x-coredata://ABC/ICNote/p1",
            "name": "[ST, Monet] Plan",
            "modified": "2026-08-10T09:00:00",
            "created": "2026-08-09T15:52:00",
        },
        {
            "id": "x-coredata://ABC/ICNote/p2",
            "name": "Gone",
            "modified": "2026-08-11T09:00:00",
            "created": "2026-08-11T09:00:00",
        },
    ]
    BODY = (
        "<div>[ST, Monet] Plan</div><div>Sun, Aug 9, 3:52pm</div><div>Deploy with "
        "SLACK_TOKEN=" + FAKE_SLACK + " and go.</div>"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = pathlib.Path(self.tmp.name) / "cache" / "notes"
        self.range_calls: list[tuple[int, int]] = []
        self.byid_calls: list[list[str]] = []

        def fake_range(start, end, folder, account):
            self.range_calls.append((start, end))
            return {self.META[0]["id"]: self.BODY}  # p2's body never comes back

        def fake_by_id(ids):
            self.byid_calls.append(list(ids))
            return {}

        self.patches = [
            mock.patch.object(
                notes_export, "list_notes", lambda folder, account: [dict(m) for m in self.META]
            ),
            mock.patch.object(notes_export, "_fetch_bodies_range", fake_range),
            mock.patch.object(notes_export, "_fetch_bodies_by_id", fake_by_id),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_cache_holds_only_scrubbed_text_with_private_modes(self) -> None:
        logs: list[str] = []
        recs = notes_export.export_notes(cache_dir=self.cache, log=logs.append)
        self.assertEqual(len(recs), 2)
        rec = recs[0]
        self.assertEqual(rec["v"], notes_export.CACHE_VERSION)
        self.assertNotIn("html", rec)
        self.assertNotIn("xoxb-", rec["text"])
        self.assertIn("[REDACTED:slack-token]", rec["text"])
        self.assertIn("slack-token", rec["scrubbed"])
        self.assertTrue(rec["text"].startswith("[ST, Monet] Plan\nSun, Aug 9"))
        self.assertEqual(set(rec), {"id", "name", "modified", "created", "text", "v", "scrubbed"})
        # on disk: dir 0700, file 0600, and the raw body never touched the disk
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)
        files = sorted(self.cache.glob("*.json"))
        self.assertEqual(len(files), 1)  # p2 (no body) is NOT cached
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        raw = files[0].read_text()
        self.assertNotIn("xoxb-", raw)
        self.assertNotIn("<div>", raw)
        self.assertEqual(json.loads(raw), rec)
        self.assertEqual(recs[1]["text"], "")
        self.assertTrue(any("did not come back" in l for l in logs))
        self.assertEqual(self.byid_calls, [[self.META[1]["id"]]])
        # second export: p1 served from cache, p2 asked for again
        self.range_calls.clear()
        recs2 = notes_export.export_notes(cache_dir=self.cache)
        self.assertEqual(recs2[0], rec)
        self.assertEqual(self.range_calls, [(2, 2)])

    def test_old_cache_records_are_invalidated_and_rewritten(self) -> None:
        self.cache.mkdir(parents=True)
        old = self.cache / f"{notes_export.safe_id(self.META[0]['id'])}.json"
        old.write_text(
            json.dumps(
                {
                    **self.META[0],
                    "html": self.BODY,
                    "text": "raw unscrubbed " + FAKE_SLACK + "",
                }
            )
        )
        os.chmod(old, 0o644)
        self.assertEqual(list(notes_export.iter_cached(self.cache)), [])  # v1 never served
        recs = notes_export.export_notes(cache_dir=self.cache)
        self.assertEqual(self.range_calls, [(1, 2)])  # re-fetched (p2 never cached)
        self.assertEqual(recs[0]["v"], notes_export.CACHE_VERSION)
        self.assertNotIn("html", json.loads(old.read_text()))
        self.assertEqual(stat.S_IMODE(old.stat().st_mode), 0o600)
        self.assertEqual(
            [r["id"] for r in notes_export.iter_cached(self.cache)], [self.META[0]["id"]]
        )

    def test_iter_apple_notes_falls_back_to_cache(self) -> None:
        notes_export.export_notes(cache_dir=self.cache)
        logs: list[str] = []
        sources.take_warnings()

        def unavailable(log=None):
            raise notes_export.NotesUnavailable("automation denied")

        with (
            mock.patch.object(notes_export, "export_notes", unavailable),
            mock.patch.object(notes_export, "CACHE_DIR", self.cache),
        ):
            docs = list(sources.iter_apple_notes(log=logs.append))
        self.assertEqual([d.doc_id for d in docs], ["note/ABC/ICNote/p1"])
        self.assertEqual(docs[0].seat, "MONET")
        self.assertNotIn("xoxb-", docs[0].text_markdown)
        self.assertTrue(docs[0].text_markdown.startswith("Sun, Aug 9"))
        warnings = sources.take_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("automation denied", warnings[0])
        self.assertIn("served 1 cached notes", warnings[0])
        self.assertTrue(any("WARNING" in l and "automation denied" in l for l in logs))
        self.assertEqual(sources.take_warnings(), [])


class HelperTests(unittest.TestCase):
    def test_slugs(self) -> None:
        self.assertEqual(sources.app_slug("Congress-Trade"), "congress-trade")
        self.assertEqual(sources.app_slug("CONGRESS-SHARED"), "congress-trading-shared")
        self.assertEqual(sources.app_slug(""), "fleet")
        self.assertEqual(sources.seat_tag("monet"), "MONET")
        self.assertEqual(sources.seat_tag("GB-CONDUCTOR"), "GB-CONDUCTOR")
        self.assertEqual(sources.seat_tag(None), "FLEET")
        self.assertEqual(
            sources.project_slug("-Users-jay-Code-AI-Fleet-Coordinator"),
            "code-ai-fleet-coordinator",
        )
        self.assertEqual(
            sources.app_from_project("code-congress-trading-shared"), "congress-trading-shared"
        )
        self.assertEqual(sources.app_from_project("code-congress-trade-app"), "congress-trade")
        self.assertEqual(
            sources.app_from_path(pathlib.Path("/Users/jay/Code/Socratic.Trade/README.md")),
            "socratic-trade",
        )
        self.assertEqual(
            sources.app_from_path(pathlib.Path("/Users/jay/Code/Usage-Monitor/docs/a.md")),
            "usage-monitor",
        )
        self.assertEqual(
            sources.app_from_path(
                pathlib.Path("/Users/jay/Code/fleet-ops/ATTACK-MAP.md"),
                fleet_ops=pathlib.Path("/Users/jay/Code/fleet-ops"),
            ),
            "fleet-ops",
        )

    def test_parse_ts(self) -> None:
        self.assertEqual(sources.parse_ts_ms("2026-08-31T22:00:00Z"), 1788213600000)
        self.assertEqual(sources.parse_ts_ms("2026-08-31T22:00:00+00:00"), 1788213600000)
        self.assertEqual(sources.parse_ts_ms("garbage"), 0)
        self.assertEqual(sources.parse_ts_ms(None), 0)


def _jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class ExtraDocWalkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.code = self.root / "Code"
        self.apps = self.root / "apps"
        self.grok = self.root / "grok"
        self.fleet = self.code / "AI-Fleet-Coordinator"
        self.ops = self.code / "fleet-ops"
        (self.fleet / "docs").mkdir(parents=True)
        (self.fleet / "README.md").write_text("# Fleet coordinator\n")
        (self.ops).mkdir(parents=True)
        (self.ops / "ATTACK-MAP.md").write_text("# Ops\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_include_and_exclude(self) -> None:
        st = self.code / "Socratic.Trade"
        (st / "docs" / "reviews").mkdir(parents=True)
        (st / "node_modules" / "pkg").mkdir(parents=True)
        (st / "docs" / "dist").mkdir()
        (st / "docs" / "build").mkdir()
        (st / "docs" / "vendor").mkdir()
        (st / "README.md").write_text("# Socratic.Trade\n\nhello\n")
        (st / "AGENTS.md").write_text("# Agents\n")
        (st / "STATUS.md").write_text("# Status\n")
        (st / "CLAUDE.md").write_text("# Claude\n")
        (st / "SECRET-NOTES.md").write_text("# not a root allowlist file\n")
        (st / "docs" / "runbook.md").write_text("# Runbook\n")
        (st / "docs" / "reviews" / "2026-raw-dump.md").write_text("# raw\n")
        (st / "docs" / "reviews" / "2026-review.md").write_text("# review\n")
        (st / "node_modules" / "pkg" / "README.md").write_text("# nm\n")
        (st / "docs" / "dist" / "out.md").write_text("# dist\n")
        (st / "docs" / "build" / "gen.md").write_text("# build\n")
        (st / "docs" / "vendor" / "lib.md").write_text("# vendor\n")
        (st / "docs" / "big.md").write_text("x" * (sources.DOC_MAX_BYTES + 1))

        um = self.code / "Usage-Monitor"
        um.mkdir()
        (um / "README.md").write_text("# Usage Monitor\n")

        self.apps.mkdir()
        (self.apps / "FLEET-PROTOCOL.md").write_text("# Agent sync protocol\n")
        (self.apps / "COOLIFY.md").write_text("# Coolify\n")
        (self.apps / "SOCRATIC-TRADE-EFFORT-LOG.md").write_text("# ST effort\n")
        (self.apps / "EFFORT-LOG-PROTOCOL.md").write_text("# Protocol\n")

        (self.grok / "docs" / "user-guide").mkdir(parents=True)
        (self.grok / "docs" / "user-guide" / "sessions.md").write_text("# Grok sessions\n")
        (self.grok / "skills" / "board-ops").mkdir(parents=True)
        (self.grok / "skills" / "board-ops" / "SKILL.md").write_text("# Board ops\n")

        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        by_name = {pathlib.Path(d.path).name: d for d in docs}
        self.assertIn("README.md", by_name)
        self.assertEqual(
            {pathlib.Path(d.path).name for d in docs if "Socratic.Trade" in d.path},
            {"README.md", "AGENTS.md", "STATUS.md", "CLAUDE.md", "runbook.md", "2026-review.md"},
        )
        names = {pathlib.Path(d.path).name for d in docs}
        self.assertNotIn("SECRET-NOTES.md", names)
        self.assertNotIn("SOCRATIC-TRADE-EFFORT-LOG.md", names)
        self.assertNotIn("EFFORT-LOG-PROTOCOL.md", names)
        self.assertIn("COOLIFY.md", names)
        self.assertIn("FLEET-PROTOCOL.md", names)
        self.assertIn("sessions.md", names)
        self.assertNotIn("SKILL.md", names)  # ~/.grok/skills is a skill-source tree now
        self.assertNotIn("out.md", names)
        self.assertNotIn("gen.md", names)
        self.assertNotIn("lib.md", names)
        self.assertNotIn("2026-raw-dump.md", names)
        st_readme = next(d for d in docs if d.path.endswith("Socratic.Trade/README.md"))
        self.assertEqual(st_readme.app, "socratic-trade")
        um_readme = next(d for d in docs if "Usage-Monitor" in d.path)
        self.assertEqual(um_readme.app, "usage-monitor")
        ops = next(d for d in docs if d.path.endswith("ATTACK-MAP.md"))
        self.assertEqual(ops.app, "fleet-ops")
        skipped = dict((path, rule) for rule, path in sources.take_doc_skips())
        # grok skills are not walked by the doc source at all (they are a SKILL_DIRS tree)
        self.assertNotIn(str(self.grok / "skills" / "board-ops" / "SKILL.md"), skipped)
        self.assertEqual(
            skipped[str(self.apps / "SOCRATIC-TRADE-EFFORT-LOG.md")], sources.SKIP_EFFORT_LOG
        )
        self.assertEqual(
            skipped[str(self.apps / "EFFORT-LOG-PROTOCOL.md")], sources.SKIP_EFFORT_LOG
        )
        # resolved-path dedup: walking coordinator both as fleet_repo and as a DOC_APP_REPO
        paths = [d.path for d in docs]
        self.assertEqual(len(paths), len(set(pathlib.Path(p).resolve() for p in paths)))


class MirrorSkipTests(unittest.TestCase):
    """Doc-walk dedupe at the source: live ~/apps copy wins, skill copies and effort-log mirrors
    are left to the skill / effort-log sources, and every skip is logged once per rule."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.code = self.root / "Code"
        self.apps = self.root / "apps"
        self.grok = self.root / ".grok"
        self.fleet = self.code / "AI-Fleet-Coordinator"
        self.ops = self.code / "fleet-ops"
        self.st = self.code / "Socratic.Trade"
        for d in (self.fleet / "docs", self.ops, self.st / "docs", self.apps, self.grok / "skills"):
            d.mkdir(parents=True)
        sources.take_doc_skips()
        sources.take_warnings()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _w(self, path: pathlib.Path, text: str = "# x\n\nbody\n") -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _walk(self, extra=()) -> tuple[set[str], dict[str, str]]:
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=extra,
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        kept = {d.path for d in docs}
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        self.assertFalse(kept & set(skipped))
        return kept, skipped

    def test_rule_a_live_apps_copy_beats_the_repo_mirror(self):
        live_sync = self._w(self.apps / "FLEET-PROTOCOL.md", "# Agent sync (live)\n")
        live_mlp = self._w(self.apps / "MAC-LOCAL-PROCESSES.md", "# Mac local processes (live)\n")
        live_ui = self._w(self.apps / "FLEET-UI-COPY.md")
        self._w(self.apps / "README.md", "# apps readme\n")
        mirror_sync = self._w(self.fleet / "FLEET-PROTOCOL.md", "# Agent sync (mirror)\n")
        mirror_mlp = self._w(self.fleet / "docs" / "MAC-LOCAL-PROCESSES.md", "# mirror\n")
        mirror_ui = self._w(self.fleet / "FLEET-UI-COPY.md")
        fleet_readme = self._w(self.fleet / "README.md", "# Fleet coordinator\n")
        fleet_only = self._w(self.fleet / "docs" / "RAG-FLEET-INFRA.md")
        st_sync = self._w(self.st / "docs" / "FLEET-PROTOCOL.md", "# ST's own copy\n")
        kept, skipped = self._walk()
        for live in (live_sync, live_mlp, live_ui, fleet_readme, fleet_only, st_sync):
            self.assertIn(str(live), kept, live)
        for mirror in (mirror_sync, mirror_mlp, mirror_ui):
            self.assertEqual(skipped.get(str(mirror)), sources.SKIP_APPS_MIRROR, mirror)
        # README.md is a generic root name: both copies stay even though the basenames match
        self.assertIn(str(self.apps / "README.md"), kept)

    def test_rule_a_only_when_the_live_copy_exists(self):
        mirror = self._w(self.fleet / "FLEET-PROTOCOL.md")
        kept, skipped = self._walk()
        self.assertIn(str(mirror), kept)
        self.assertEqual(skipped, {})

    def test_rule_a_counts_extra_docs_as_live(self):
        extra = self._w(self.root / "elsewhere" / "FLEET-UI-COPY.md")
        mirror = self._w(self.fleet / "FLEET-UI-COPY.md")
        kept, skipped = self._walk(extra=[extra])
        self.assertIn(str(extra), kept)
        self.assertEqual(skipped.get(str(mirror)), sources.SKIP_APPS_MIRROR)

    def test_rule_b_skill_copies(self):
        # under docs/: walked, then dropped by the rule (and logged)
        walked = [
            self._w(
                self.fleet / "docs" / "fleet-skills" / "by-seat" / "ag" / "board-ops" / "SKILL.md"
            ),
            self._w(
                self.fleet / "docs" / "fleet-skills" / "by-seat" / "ag" / "board-ops" / "README.md"
            ),
            self._w(self.fleet / "docs" / "fleet-skills" / "board-ops" / "SKILL.md"),
            self._w(self.st / "docs" / "skills" / "deploy" / "SKILL.md"),
        ]
        # skill trees the doc walk no longer enters at all (the skill source owns them)
        unwalked = [
            self._w(self.fleet / "skills" / "board-ops" / "SKILL.md"),
            self._w(self.fleet / ".claude" / "skills" / "board-ops" / "SKILL.md"),
            self._w(self.fleet / ".cursor" / "skills" / "board-ops" / "notes.md"),
            self._w(self.fleet / ".grok" / "skills" / "board-ops" / "SKILL.md"),
            self._w(self.grok / "skills" / "board-ops" / "SKILL.md"),
        ]
        index = self._w(self.fleet / "docs" / "fleet-skills" / "README.md", "# skills index\n")
        guide = self._w(self.grok / "docs" / "user-guide" / "sessions.md")
        kept, skipped = self._walk()
        for c in walked:
            self.assertEqual(skipped.get(str(c)), sources.SKIP_SKILL_COPY, c)
        for c in unwalked:
            self.assertNotIn(str(c), kept, c)
            self.assertNotIn(str(c), skipped, c)
        self.assertIn(str(index), kept)
        self.assertIn(str(guide), kept)

    def test_rule_c_effort_log_mirrors(self):
        mirrors = [
            self._w(self.fleet / "docs" / "EFFORT-LOG.md"),
            self._w(self.st / "docs" / "EFFORT-LOG.md"),
            self._w(self.fleet / "EFFORT-LOG-PROTOCOL.md"),
            self._w(self.apps / "SOCRATIC-TRADE-EFFORT-LOG.md"),
            self._w(self.apps / "EFFORT-LOG-PROTOCOL.md"),
        ]
        other = self._w(self.st / "docs" / "runbook.md")
        kept, skipped = self._walk()
        for m in mirrors:
            self.assertEqual(skipped.get(str(m)), sources.SKIP_EFFORT_LOG, m)
        self.assertIn(str(other), kept)

    def test_skips_are_logged_once_per_rule(self):
        self._w(self.apps / "FLEET-PROTOCOL.md")
        self._w(self.fleet / "FLEET-PROTOCOL.md")
        for i in range(5):
            self._w(
                self.fleet / "docs" / "fleet-skills" / "by-seat" / f"seat{i}" / "x" / "SKILL.md"
            )
        self._w(self.st / "docs" / "EFFORT-LOG.md")
        list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        warnings = sources.take_warnings()
        self.assertEqual(len(warnings), 3)
        by_rule = {w.split()[3]: w for w in warnings}
        self.assertEqual(
            set(by_rule),
            {sources.SKIP_APPS_MIRROR, sources.SKIP_SKILL_COPY, sources.SKIP_EFFORT_LOG},
        )
        self.assertTrue(
            by_rule[sources.SKIP_APPS_MIRROR].startswith("doc: skipped 1 apps-mirror file(s): ")
        )
        self.assertIn("FLEET-PROTOCOL.md", by_rule[sources.SKIP_APPS_MIRROR])
        self.assertTrue(
            by_rule[sources.SKIP_SKILL_COPY].startswith("doc: skipped 5 skill-copy file(s): ")
        )
        self.assertTrue(by_rule[sources.SKIP_SKILL_COPY].endswith("(+2 more)"))
        self.assertIn("EFFORT-LOG.md", by_rule[sources.SKIP_EFFORT_LOG])
        # the (rule, path) pairs stay available until consumed, then the list is empty
        self.assertEqual(len(sources.take_doc_skips()), 7)
        self.assertEqual(sources.take_doc_skips(), [])
        # a walk with nothing to skip logs nothing
        for f in list(self.fleet.rglob("*.md")) + list(self.st.rglob("*.md")):
            f.unlink()
        list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        self.assertEqual(sources.take_warnings(), [])

    def test_mirror_skip_reason_unit(self):
        r = sources.mirror_skip_reason
        fleet = pathlib.Path("/x/Code/AI-Fleet-Coordinator")
        self.assertEqual(
            r(pathlib.Path("/x/Code/Socratic.Trade/docs/EFFORT-LOG.md")), sources.SKIP_EFFORT_LOG
        )
        self.assertEqual(r(pathlib.Path("/x/apps/BOTFLEET-EFFORT-LOG.md")), sources.SKIP_EFFORT_LOG)
        self.assertEqual(r(pathlib.Path("/x/.claude/skills/a/SKILL.md")), sources.SKIP_SKILL_COPY)
        self.assertEqual(r(pathlib.Path("/x/.cursor/skills/a/README.md")), sources.SKIP_SKILL_COPY)
        self.assertEqual(r(pathlib.Path("/x/.grok/skills/a/SKILL.md")), sources.SKIP_SKILL_COPY)
        self.assertEqual(
            r(fleet / "docs/fleet-skills/by-seat/fx/a/anything.md"), sources.SKIP_SKILL_COPY
        )
        self.assertEqual(r(fleet / "docs/fleet-skills/a/SKILL.md"), sources.SKIP_SKILL_COPY)
        self.assertEqual(r(fleet / "skills/a/SKILL.md"), sources.SKIP_SKILL_COPY)
        self.assertIsNone(r(fleet / "docs/fleet-skills/README.md"))
        self.assertIsNone(r(pathlib.Path("/x/.claude/CLAUDE.md")))
        self.assertIsNone(r(pathlib.Path("/x/Code/Socratic.Trade/docs/skills.md")))
        # rule (a) needs a real checkout to resolve against; use the temp fleet dir
        live = {"FLEET-PROTOCOL.md", "README.md"}
        mirror = self._w(self.fleet / "FLEET-PROTOCOL.md")
        readme = self._w(self.fleet / "README.md")
        outside = self._w(self.st / "FLEET-PROTOCOL.md")
        self.assertEqual(
            r(mirror, fleet_repos=[self.fleet], live_basenames=live), sources.SKIP_APPS_MIRROR
        )
        self.assertIsNone(r(readme, fleet_repos=[self.fleet], live_basenames=live))
        self.assertIsNone(r(outside, fleet_repos=[self.fleet], live_basenames=live))
        self.assertIsNone(r(mirror, fleet_repos=[self.fleet], live_basenames=()))

    # ---- rule (d): the Fleet-OPS handbook.  Its public pages and generated/ are the text of the live FLEET-PROTOCOL.md.

    PUBLIC = (
        "---\ntitle: {t}\norder: {o}\nvisibility: public\nsection: {s}\n---\n## {t}\n\n{body}\n"
    )
    PRIVATE = (
        "---\ntitle: {t}\norder: {o}\nvisibility: private\nsection: {s}\n---\n## {t}\n\n{body}\n"
    )
    RULE = "Never idle-watch a PR:  a PR that is not merging is waiting on an action, not on time."

    def _handbook(self):
        """The shapes build.mjs makes: public pages, a private page, a ruling log whose front matter says public by
        mistake, the README, and generated/FLEET-PROTOCOL.md; plus the live file and the AFC mirror that hold the same text."""
        live = self._w(self.apps / "FLEET-PROTOCOL.md", f"# Agent sync (live)\n\n{self.RULE}\n")
        mirror = self._w(self.fleet / "FLEET-PROTOCOL.md", f"# Agent sync (mirror)\n\n{self.RULE}\n")
        generated = self._w(
            self.ops / "generated" / "FLEET-PROTOCOL.md", f"# Agent sync (generated)\n\n{self.RULE}\n"
        )
        h = self.ops / "handbook"
        public = [
            self._w(
                h / "work" / "landing.md",
                self.PUBLIC.format(t="Landing", o="0400", s="work", body=self.RULE),
            ),
            self._w(
                h / "core" / "how-to-use.md",
                self.PUBLIC.format(t="How", o="0100", s="core", body="Read it."),
            ),
            self._w(
                h / "00-preamble.md",
                self.PUBLIC.format(t="Preamble", o="0010", s="core", body="Scope."),
            ),
        ]
        private = [
            self._w(
                h / "private" / "pointers.md",
                self.PRIVATE.format(t="Pointers", o="9000", s="private", body="See ATTACK-MAP."),
            ),
            self._w(
                h / "rulings" / "2026-10.md",
                self.PUBLIC.format(t="Rulings", o="9100", s="rulings", body="A ruling."),
            ),
            self._w(
                h / "ops" / "held-back.md",
                self.PRIVATE.format(t="Held", o="8000", s="ops", body="Not yet public."),
            ),
            self._w(h / "README.md", "# Fleet Handbook\n\nEditing rules.\n"),
        ]
        return live, mirror, generated, public, private

    def test_rule_d_public_handbook_pages_are_one_hit_through_the_live_file(self):
        live, mirror, generated, public, private = self._handbook()
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        kept = {d.path for d in docs}
        # the handbook rule appears once, in the live file
        hits = [d.path for d in docs if self.RULE in d.text_markdown]
        self.assertEqual(hits, [str(live)])
        for page in public:
            self.assertEqual(skipped.get(str(page)), sources.SKIP_HANDBOOK_MIRROR, page)
        for page in private:
            self.assertIn(str(page), kept, page)
        self.assertEqual(skipped.get(str(mirror)), sources.SKIP_APPS_MIRROR)
        self.assertNotIn(str(generated), kept)  # the walk never enters generated/
        self.assertNotIn(str(generated), skipped)
        # the private pages are ingested as fleet-ops docs
        by_path = {d.path: d for d in docs}
        pointers = by_path[str(self.ops / "handbook" / "private" / "pointers.md")]
        self.assertEqual(
            (pointers.source, pointers.app, pointers.title), ("doc", "fleet-ops", "Pointers")
        )

    def test_rule_d_stands_down_to_the_afc_mirror_when_there_is_no_live_file(self):
        live, mirror, generated, public, private = self._handbook()
        live.unlink()  # a fresh host:  no ~/apps/FLEET-PROTOCOL.md
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        kept = {d.path for d in docs}
        # the mirror is the one FLEET-PROTOCOL.md the walk ingests, so the public pages are dropped for it, as they are
        # for the live file:  a handbook rule is one hit, which is what it was before the handbook was walked at all
        self.assertIn(str(mirror), kept)
        for page in public:
            self.assertEqual(skipped.get(str(page)), sources.SKIP_HANDBOOK_MIRROR, page)
        for page in private:
            self.assertIn(str(page), kept, page)
        hits = [d.path for d in docs if self.RULE in d.text_markdown]
        self.assertEqual(hits, [str(mirror)])

    def test_rule_d_keeps_the_public_pages_when_no_agent_sync_copy_is_ingested(self):
        live, mirror, generated, public, private = self._handbook()
        live.unlink()
        mirror.unlink()  # nothing else holds the text:  the public pages are the only copy
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        kept = {d.path for d in docs}
        for page in public + private:
            self.assertIn(str(page), kept, page)
        self.assertEqual(skipped, {})
        self.assertEqual(len([d for d in docs if self.RULE in d.text_markdown]), 1)

    def test_rule_d_is_not_satisfied_by_another_repos_agent_sync_copy(self):
        live, mirror, generated, public, private = self._handbook()
        live.unlink()
        mirror.unlink()
        self._w(
            self.st / "docs" / "FLEET-PROTOCOL.md", f"# ST's own copy\n\n{self.RULE}\n"
        )  # not the coordinator's mirror
        kept, skipped = self._walk()
        for page in public:
            self.assertIn(str(page), kept, page)
        self.assertEqual(skipped, {})

    def test_a_live_file_the_walk_rejects_does_not_hide_its_mirrors(self):
        live, mirror, generated, public, private = self._handbook()
        live.write_text(
            f"# Agent sync (live)\n\n{self.RULE}\n" + "x" * (sources.DOC_MAX_BYTES + 10)
        )
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        kept = {d.path for d in docs}
        self.assertNotIn(str(live), kept)  # too big for the doc source
        self.assertIn(str(mirror), kept)  # so the mirror is not dropped for it (rule a)
        for page in public:  # and the mirror, which is ingested, stands in for
            self.assertEqual(
                skipped.get(str(page)), sources.SKIP_HANDBOOK_MIRROR, page
            )  # the pages (rule d)
        self.assertNotIn(str(mirror), skipped)
        self.assertEqual([d.path for d in docs if self.RULE in d.text_markdown], [str(mirror)])

    def test_rule_d_survives_an_unreadable_mirror_and_keeps_the_pages(self):
        live, mirror, generated, public, private = self._handbook()
        live.unlink()
        real = sources._skip_doc

        def unreadable(p):
            if p == mirror:
                raise PermissionError(13, "Permission denied", str(p))
            return real(p)

        with mock.patch.object(sources, "_skip_doc", unreadable):
            docs = list(
                sources.iter_docs(
                    self.fleet,
                    self.ops,
                    extra=[],
                    code_dir=self.code,
                    apps_dir=self.apps,
                    grok_home=self.grok,
                )
            )
        kept = {d.path for d in docs}
        # the walk finishes, and since no AGENT-SYNC.md could be read the public pages are the only copy
        self.assertNotIn(str(mirror), kept)
        for page in public:
            self.assertIn(str(page), kept, page)

    def test_a_live_file_and_mirror_the_walk_both_reject_do_not_hide_the_pages(self):
        live, mirror, generated, public, private = self._handbook()
        big = "x" * (sources.DOC_MAX_BYTES + 10)
        live.write_text(f"# Agent sync (live)\n\n{self.RULE}\n{big}")
        mirror.write_text(f"# Agent sync (mirror)\n\n{self.RULE}\n{big}")
        docs = list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        skipped = {path: rule for rule, path in sources.take_doc_skips()}
        kept = {d.path for d in docs}
        for page in public:
            self.assertIn(str(page), kept, page)
        self.assertEqual(skipped, {})

    def test_rule_d_is_logged_once(self):
        self._handbook()
        list(
            sources.iter_docs(
                self.fleet,
                self.ops,
                extra=[],
                code_dir=self.code,
                apps_dir=self.apps,
                grok_home=self.grok,
            )
        )
        by_rule = {w.split()[3]: w for w in sources.take_warnings()}
        self.assertIn(sources.SKIP_HANDBOOK_MIRROR, by_rule)
        self.assertTrue(
            by_rule[sources.SKIP_HANDBOOK_MIRROR].startswith(
                "doc: skipped 3 handbook-mirror file(s): "
            )
        )

    def test_rule_d_unit(self):
        r = sources.mirror_skip_reason
        h = self.ops / "handbook"
        pub = self._w(
            h / "chat" / "posting.md", self.PUBLIC.format(t="P", o="0700", s="chat", body="x")
        )
        priv = self._w(
            h / "private" / "p.md", self.PRIVATE.format(t="P", o="9000", s="private", body="x")
        )
        ruling = self._w(
            h / "rulings" / "2026-10.md", self.PUBLIC.format(t="R", o="9100", s="rulings", body="x")
        )
        quoted = self._w(
            h / "chat" / "quoted.md", '---\ntitle: Q\nvisibility: "public"\n---\n## Q\n'
        )
        nofm = self._w(h / "README.md", "# readme\n")
        crlf = self._w(
            h / "chat" / "crlf.md", "---\r\ntitle: C\r\nvisibility: public\r\n---\n## C\n"
        )
        late = self._w(
            h / "chat" / "late.md", "## No front matter\n\n---\nvisibility: public\n---\n"
        )
        gen = self._w(self.ops / "generated" / "FLEET-PROTOCOL.md")
        docs_gen = self._w(self.ops / "docs" / "generated" / "x.md")
        elsewhere = self._w(
            self.st / "handbook" / "chat" / "posting.md",
            self.PUBLIC.format(t="P", o="1", s="chat", body="x"),
        )
        ops = [self.ops]
        live = {"FLEET-PROTOCOL.md"}
        self.assertEqual(r(pub, ops_repos=ops, live_basenames=live), sources.SKIP_HANDBOOK_MIRROR)
        self.assertEqual(r(gen, ops_repos=ops, live_basenames=live), sources.SKIP_HANDBOOK_MIRROR)
        for kept in (priv, ruling, quoted, nofm, crlf, late, docs_gen):
            self.assertIsNone(r(kept, ops_repos=ops, live_basenames=live), kept)
        # a handbook/ folder in another repo is not Fleet-OPS's
        self.assertIsNone(r(elsewhere, ops_repos=ops, live_basenames=live))
        # without an ops repo the rule is off, so existing callers are unchanged
        self.assertIsNone(r(pub, live_basenames=live))
        # without an ingested AGENT-SYNC.md the public pages and generated/ are the only copy, so they stay
        self.assertIsNone(r(pub, ops_repos=ops))
        self.assertIsNone(r(gen, ops_repos=ops, live_basenames={"MAC-LOCAL-PROCESSES.md"}))
        # the caller may say a copy is ingested that is not live (the coordinator's mirror), or that none is
        self.assertEqual(
            r(pub, ops_repos=ops, agent_sync_ingested=True), sources.SKIP_HANDBOOK_MIRROR
        )
        self.assertEqual(
            r(gen, ops_repos=ops, agent_sync_ingested=True), sources.SKIP_HANDBOOK_MIRROR
        )
        self.assertIsNone(r(pub, ops_repos=ops, live_basenames=live, agent_sync_ingested=False))

    def test_rule_d_public_page_reads_only_the_front_matter(self):
        rel = pathlib.PurePath("handbook/chat/posting.md")
        page = self._w(
            self.ops / "handbook" / "chat" / "posting.md",
            "---\ntitle: P\nvisibility: private\n---\n## P\n\nvisibility: public\n",
        )
        self.assertFalse(sources.handbook_page_is_public(page, rel))
        self.assertFalse(sources.handbook_page_is_public(self.ops / "handbook" / "missing.md", rel))

    def test_grok_skills_are_a_skill_source_tree(self):
        self.assertIn(sources.GROK_HOME / "skills", sources.SKILL_DIRS)
        self._w(
            self.grok / "skills" / "housekeeper" / "SKILL.md",
            "---\nname: housekeeper\n---\n# HK (GROK)\n",
        )
        self._w(self.grok / "skills" / "README.md", "# index, not a skill\n")
        docs = list(sources.iter_skills([self.grok / "skills"]))
        self.assertEqual([d.doc_id for d in docs], ["skill/grok/housekeeper"])
        self.assertEqual(
            (docs[0].source, docs[0].category, docs[0].title), ("skill", "runbook", "housekeeper")
        )


class ChatLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.none = dict(
            claude_projects=None,
            grok_sessions=None,
            cursor_projects=None,
            codex_sessions=None,
            gemini_home=None,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _docs(self, **roots: pathlib.Path) -> list:
        kw = dict(self.none)
        kw.update(roots)
        return list(sources.iter_chat_logs(**kw))

    def test_grok_transcript_md_fallback(self) -> None:
        sess = self.root / "grok" / "cwd" / "mdonly"
        sess.mkdir(parents=True)
        (sess / "transcript.md").write_text(
            "# User\n\nOwner ruling: default UI theme is light.\n\n# Assistant\n\nHi back.\n"
        )
        (sess / "events.jsonl").write_text(
            json.dumps({"type": "assistant", "content": "spam"}) + "\n"
        )
        docs = self._docs(grok_sessions=self.root / "grok")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].doc_id, "chat/grok/mdonly#ruling1")
        self.assertEqual(docs[0].seat, "OWNER")
        self.assertIn("Owner ruling", docs[0].text_markdown)
        self.assertNotIn("Hi back.", docs[0].text_markdown)
        self.assertNotIn("spam", docs[0].text_markdown)

    def test_skip_tool_only_jsonl(self) -> None:
        claude = self.root / "claude" / "proj"
        _jsonl(
            claude / "tool-only.jsonl",
            [
                {"type": "queue-operation", "operation": "init"},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "1", "content": "ok"},
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                        ],
                    },
                },
                {"type": "mode", "mode": "plan"},
            ],
        )
        grok_sess = self.root / "grok" / "cwd" / "sid"
        _jsonl(
            grok_sess / "events.jsonl",
            [
                {"type": "assistant", "content": "should not be ingested from events.jsonl"},
            ],
        )
        _jsonl(
            grok_sess / "chat_history.jsonl",
            [
                {"type": "system", "content": "You are Grok."},
                {"type": "tool_result", "content": "huge tool blob " * 20},
                {"type": "reasoning", "summary": "thinking"},
                {"type": "assistant", "content": "", "tool_calls": [{"name": "x"}]},
            ],
        )
        docs = self._docs(claude_projects=self.root / "claude", grok_sessions=self.root / "grok")
        self.assertEqual(docs, [])

    def test_extract_user_and_assistant(self) -> None:
        claude = self.root / "claude" / "-Users-jay-Code-Socratic-Trade"
        _jsonl(
            claude / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
            [
                {"type": "queue-operation", "operation": "start"},
                {"type": "permission-mode"},
                {"type": "user", "message": {"role": "user", "content": "Ship the ST patch."}},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "ignore me"},
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Landed the ST patch on main."},
                            {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                        ],
                    },
                },
            ],
        )
        grok_sess = self.root / "grok" / "cwd" / "01abc"
        _jsonl(grok_sess / "events.jsonl", [{"type": "assistant", "content": "events spam"}])
        _jsonl(
            grok_sess / "chat_history.jsonl",
            [
                {
                    "type": "user",
                    "content": [
                        {"type": "text", "text": "Owner ruling: default UI theme is light."}
                    ],
                },
                {"type": "assistant", "content": "Recorded the light-theme default."},
                {"type": "tool_result", "content": "not a turn"},
            ],
        )
        (grok_sess / "system_prompt.txt").write_text("skip me")
        (grok_sess / "chat_history.jsonl.lock").write_text("lock")
        cur = self.root / "cursor" / "Users-jay-Code-Socratic-Trade" / "agent-transcripts" / "tid"
        _jsonl(
            cur / "tid.jsonl",
            [
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "Cursor user turn."}]},
                },
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {}},
                            {"type": "text", "text": "Cursor assistant turn."},
                        ]
                    },
                },
            ],
        )
        _jsonl(
            self.root / "codex" / "rollout-1.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "1"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "sys"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Codex user."}],
                    },
                },
                {"type": "response_item", "payload": {"type": "reasoning", "summary": "nope"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Codex assistant."}],
                    },
                },
            ],
        )
        gem = self.root / "gemini" / "brain" / "g1" / ".system_generated" / "logs"
        _jsonl(
            gem / "transcript.jsonl",
            [
                {
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST> Gemini hello </USER_REQUEST>"
                    "<ADDITIONAL_METADATA> cwd=/tmp </ADDITIONAL_METADATA>",
                },
                {"type": "GENERIC", "content": "Created At: ignore"},
                {"type": "PLANNER_RESPONSE", "content": "Gemini reply.&nbsp; Done."},
            ],
        )
        _jsonl(
            gem / "chunks" / "transcript" / "000.jsonl",
            [
                {"type": "USER_INPUT", "content": "<USER_REQUEST> duplicate chunk </USER_REQUEST>"},
            ],
        )
        _jsonl(
            gem / "transcript_full.jsonl",
            [
                {"type": "USER_INPUT", "content": "<USER_REQUEST> duplicate full </USER_REQUEST>"},
            ],
        )

        docs = {
            d.doc_id: d
            for d in self._docs(
                claude_projects=self.root / "claude",
                grok_sessions=self.root / "grok",
                cursor_projects=self.root / "cursor",
                codex_sessions=self.root / "codex",
                gemini_home=self.root / "gemini",
            )
        }
        # Owner 2026-09-02: only owner-ruling / infra-policy user turns, not full sessions.
        self.assertEqual(set(docs), {"chat/grok/01abc#ruling1"})
        grok = docs["chat/grok/01abc#ruling1"]
        self.assertEqual(grok.source, "chat-log")
        self.assertEqual(grok.seat, "OWNER")
        self.assertEqual(grok.category, "preference")
        self.assertIn("Owner ruling", grok.text_markdown)
        self.assertNotIn("Recorded the light-theme default.", grok.text_markdown)
        self.assertNotIn("events spam", grok.text_markdown)
        self.assertNotIn("not a turn", grok.text_markdown)
        self.assertNotIn("Ship the ST patch.", grok.text_markdown)
        self.assertNotIn("Cursor user turn.", grok.text_markdown)

    def test_secret_lines_dropped_on_ruling(self) -> None:
        claude = self.root / "claude" / "proj"
        _jsonl(
            claude / "sess.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Owner ruling: keep this line\nexport GH=ghp_notarealtoken\nmore keep",
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "ok\nsk-ant-dummy\nxoxb-dummy\nstill ok",
                    },
                },
            ],
        )
        docs = self._docs(claude_projects=self.root / "claude")
        self.assertEqual(len(docs), 1)
        text = docs[0].text_markdown
        self.assertEqual(docs[0].seat, "OWNER")
        self.assertIn("keep this line", text)
        self.assertIn("more keep", text)
        self.assertNotIn("still ok", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("sk-ant-", text)
        self.assertNotIn("xoxb-", text)

    def test_two_rulings_are_separate_docs(self) -> None:
        claude = self.root / "claude" / "proj"
        _jsonl(
            claude / "big.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Owner ruling: never mix OpenRouter vectors into fleet-agents.",
                    },
                },
                {"type": "assistant", "message": {"role": "assistant", "content": "noted"}},
                {
                    "type": "user",
                    "message": {"role": "user", "content": "Owner said default UI theme is light."},
                },
                {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
            ],
        )
        split = {d.doc_id: d for d in self._docs(claude_projects=self.root / "claude")}
        self.assertEqual(
            set(split),
            {
                "chat/claude/big#ruling1",
                "chat/claude/big#ruling2",
            },
        )
        self.assertEqual(split["chat/claude/big#ruling1"].category, "infrastructure")
        self.assertEqual(split["chat/claude/big#ruling2"].category, "preference")

    def test_nightly_sources_exclude_chat_log(self) -> None:
        self.assertNotIn("chat-log", sources.NIGHTLY_SOURCES)
        self.assertIn("chat-log", sources.SOURCES)
        self.assertIn("chat-log", sources.GENERATORS)



class ProtocolDocRename(unittest.TestCase):
    """The protocol document was renamed;  readers know the current name only."""

    def test_only_the_current_name_is_known_and_the_extra_docs_list_it(self) -> None:
        self.assertEqual(set(sources.PROTOCOL_DOC_NAMES), {"FLEET-PROTOCOL.md"})
        names = {p.name for p in sources.EXTRA_DOCS}
        self.assertTrue({"FLEET-PROTOCOL.md"} <= names)
        self.assertNotIn("AGENT-SYNC.md", names)

if __name__ == "__main__":
    unittest.main()


class CaseVariantRepoTests(unittest.TestCase):
    """DOC_APP_REPOS lists both AutoRotate and Autorotate; on a case-insensitive volume (APFS)
    they are one directory.  Dedupe on filesystem identity, walk the checkout once, and spell
    the doc_id the way the disk does (doc/Autorotate/...)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.code = self.root / "Code"
        self.repo = self.code / "Autorotate"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "README.md").write_text("# Autorotate\n\nbody\n")
        (self.repo / "docs" / "A.md").write_text("# A\n\nbody\n")
        self.variant = self.code / "AutoRotate"
        try:
            self.case_insensitive = os.path.samefile(self.variant, self.repo)
        except OSError:
            self.case_insensitive = False
        sources._git_cache.clear()
        sources.take_doc_skips()

    def tearDown(self) -> None:
        sources._git_cache.clear()
        self.tmp.cleanup()

    def _git_init(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git not installed")
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        for args in (
            ["init", "-q", "-b", "main"],
            ["remote", "add", "origin", "git@github.com:owner/Autorotate.git"],
            ["add", "."],
            ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        ):
            subprocess.run(["git", *args], cwd=self.repo, check=True, env=env, capture_output=True)

    def test_fs_key_is_filesystem_identity(self) -> None:
        a = self.repo / "README.md"
        link = self.repo / "README-link.md"
        os.link(a, link)  # hard link: another name for the same inode
        self.assertEqual(sources.fs_key(a), sources.fs_key(link))
        self.assertNotEqual(sources.fs_key(a), sources.fs_key(self.repo / "docs" / "A.md"))
        with self.assertRaises(OSError):
            sources.fs_key(self.repo / "missing.md")
        self.assertEqual(sources._unique_paths([a, link, a]), [a])
        self.assertEqual(sources._dedupe_docs([link, a]), [link])

    def test_unique_paths_case_variant_spellings(self) -> None:
        if not self.case_insensitive:
            self.skipTest("case-sensitive filesystem: the two spellings are different paths")
        a, b = self.variant / "README.md", self.repo / "README.md"
        self.assertEqual(sources._unique_paths([a, b]), [a])
        self.assertEqual(sources._dedupe_docs([a, b]), [a])
        self.assertEqual(sources.canonical_path(self.variant / "readme.md").name, "README.md")
        self.assertEqual(sources.canonical_path(self.variant, resolve=False), self.repo)

    def test_unique_repo_dirs_walks_one_checkout(self) -> None:
        if not self.case_insensitive:
            self.skipTest("case-sensitive filesystem: the two spellings are different paths")
        dirs = sources._unique_repo_dirs(self.code, ("AutoRotate", "Autorotate", "missing"))
        self.assertEqual(dirs, [self.repo])  # on-disk spelling, symlinks untouched
        # a case-sensitive-safe check too: the same entry twice is walked once
        self.assertEqual(
            sources._unique_repo_dirs(self.code, ("Autorotate", "Autorotate")), [self.repo]
        )

    def test_doc_walk_yields_each_file_once_with_canonical_doc_id(self) -> None:
        if not self.case_insensitive:
            self.skipTest("case-sensitive filesystem: the two spellings are different paths")
        self._git_init()
        with mock.patch.object(sources, "DOC_APP_REPOS", ("AutoRotate", "Autorotate")):
            docs = list(
                sources.iter_docs(
                    self.root / "no-fleet",
                    self.root / "no-ops",
                    extra=(),
                    code_dir=self.code,
                    apps_dir=None,
                    grok_home=None,
                )
            )
        self.assertEqual(
            sorted(d.doc_id for d in docs), ["doc/Autorotate/README.md", "doc/Autorotate/docs/A.md"]
        )
        self.assertTrue(
            all(d.path.startswith(str(self.repo) + os.sep) for d in docs), [d.path for d in docs]
        )
        self.assertTrue(
            all(d.url.startswith("https://github.com/owner/Autorotate/blob/main/") for d in docs)
        )

    def test_repo_info_and_doc_id_from_variant_spelling(self) -> None:
        if not self.case_insensitive:
            self.skipTest("case-sensitive filesystem: the two spellings are different paths")
        self._git_init()
        canonical = sources.canonical_path(self.repo)
        info = sources.repo_info(self.code / "AUTOROTATE" / "docs" / "A.md")
        self.assertEqual(pathlib.Path(info["top"]), canonical)
        self.assertIs(sources.repo_info(self.repo), info)  # one cache entry for both spellings
        self.assertEqual(len(sources._git_cache), 1)
        d = sources.doc_from_file(self.code / "AUTOROTATE" / "docs" / "A.md")
        self.assertEqual(d.doc_id, "doc/Autorotate/docs/A.md")
        self.assertEqual(d.url, "https://github.com/owner/Autorotate/blob/main/docs/A.md")
        self.assertGreater(d.created_at_ms, 0)

    def test_repo_info_canonical_spelling_without_case_variance(self) -> None:
        self._git_init()
        d = sources.doc_from_file(self.repo / "README.md")
        self.assertEqual(d.doc_id, "doc/Autorotate/README.md")


# --------------------------------------------------------------------------- linear source


def _linear_node(
    uid="uuid-1",
    identifier="AFC-999",
    title="Board row filed after the freeze",
    state=("Done", "completed"),
    team="AFC",
    labels=("Kind/agent-report", "Severity/P2", "seat:CLAUDE"),
    description="The body of the issue.",
    comments=(),
    created="2026-10-20T01:00:00.000Z",
    updated="2026-10-21T01:00:00.000Z",
    priority=3,
):
    return {
        "id": uid,
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/simple-with-us/issue/{identifier}/slug",
        "description": description,
        "priority": priority,
        "createdAt": created,
        "updatedAt": updated,
        "state": {"name": state[0], "type": state[1]},
        "team": {"key": team},
        "labels": {"nodes": [{"name": n} for n in labels]},
        "attachments": {"nodes": []},
        "comments": {
            "nodes": [
                {
                    "body": body,
                    "createdAt": ts,
                    "user": {"displayName": author} if author else None,
                    "externalUser": None,
                }
                for author, body, ts in comments
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


class LinearSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # keep the source's state files inside the test tmpdir, never the real install paths
        env = mock.patch.dict(
            os.environ,
            {
                "FLEET_RAG_LINEAR_STATE": str(pathlib.Path(self.tmp) / "ingest-state.json"),
                "FLEET_RAG_LINEAR_REFRESH_STATE": str(
                    pathlib.Path(self.tmp) / "linear-source-refresh.json"
                ),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.map_path = pathlib.Path(self.tmp) / "board-to-linear.tsv"
        self.map_path.write_text(
            "board_id\tsource_key\tteam\tlinear_identifier\tlinear_url\tlinear_id\n"
            "aaaa1111\tsk\tAFC\tAFC-1\thttps://linear.app/x/AFC-1\tuuid-imported-old\n"
            "bbbb2222\tsk\tAFC\tAFC-2\thttps://linear.app/x/AFC-2\tuuid-imported-active\n",
            encoding="utf-8",
        )

    def test_selection_skips_imported_untouched_but_keeps_new_and_updated(self) -> None:
        scan = {
            "uuid-imported-old": "2026-10-11T00:00:00.000Z",  # in TSV, untouched -> skip
            "uuid-imported-active": "2026-10-25T00:00:00.000Z",  # in TSV, post-freeze -> yield
            "uuid-new": "2026-10-12T00:00:00.000Z",  # not in TSV -> yield however old
        }
        nodes = [
            _linear_node(uid="uuid-imported-active", identifier="AFC-2"),
            _linear_node(uid="uuid-new", identifier="AFC-3", title="Filed straight into Linear"),
        ]
        seen_fetch = {}

        def fetch(ids):
            seen_fetch["ids"] = list(ids)
            return nodes

        docs = list(
            sources.iter_linear(
                covered_ids={"uuid-imported-old", "uuid-imported-active"},
                light_scan=lambda: scan,
                fetch_full=fetch,
            )
        )
        self.assertEqual(sorted(seen_fetch["ids"]), ["uuid-imported-active", "uuid-new"])
        # doc_id is the stable issue UUID, not the identifier (team moves rename identifiers)
        self.assertEqual(
            [d.doc_id for d in docs], ["linear/uuid-imported-active", "linear/uuid-new"]
        )
        self.assertTrue(all(d.source == "linear" for d in docs))

    def test_mapping_category_seat_url_markdown_and_comment_order(self) -> None:
        node = _linear_node(
            comments=[
                ("Codex", "second", "2026-10-21T02:00:00.000Z"),
                ("Claude", "first", "2026-10-20T02:00:00.000Z"),
            ],
            description="Body with marker.\n\n<!-- effort-key: deadbeef -->",
        )
        docs = list(
            sources.iter_linear(
                covered_ids=set(),
                light_scan=lambda: {"uuid-1": "2026-10-21T01:00:00.000Z"},
                fetch_full=lambda ids: [node],
            )
        )
        (doc,) = docs
        self.assertEqual(doc.doc_id, "linear/uuid-1")
        self.assertEqual(doc.category, "lesson")  # state type completed
        self.assertEqual(doc.seat, "CLAUDE")
        self.assertEqual(doc.app, "fleet")  # AFC team maps to the fleet app, not "afc"
        self.assertTrue(doc.url.startswith("https://linear.app/"))
        self.assertEqual(doc.extra["severity"], "P2")  # Severity label beats priority number
        self.assertIn("## Description", doc.text_markdown)
        self.assertNotIn("effort-key", doc.text_markdown)  # machine marker stripped
        self.assertLess(doc.text_markdown.index("first"), doc.text_markdown.index("second"))
        self.assertIn("## Comments", doc.text_markdown)
        self.assertEqual(doc.created_at_ms, sources.parse_ts_ms("2026-10-20T01:00:00.000Z"))
        self.assertEqual(doc.updated_at_ms, sources.parse_ts_ms("2026-10-21T01:00:00.000Z"))

    def test_open_state_is_a_finding_and_seat_falls_back_to_claimed(self) -> None:
        node = _linear_node(state=("In Progress", "started"), labels=("claimed:WISE",))
        (doc,) = sources.iter_linear(
            covered_ids=set(),
            light_scan=lambda: {"uuid-1": "2026-10-21T01:00:00.000Z"},
            fetch_full=lambda ids: [node],
        )
        self.assertEqual(doc.category, "finding")
        self.assertEqual(doc.seat, "WISE")
        self.assertEqual(doc.extra["severity"], "P2")  # priority 3 -> P2 without a label

    def test_comment_pages_are_followed_and_the_bound_flags_truncation(self) -> None:
        def fake_query(query, timeout_s=0):
            if "issue(id:" in query:
                return {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "body": "extra page body",
                                    "createdAt": "2026-10-22T00:00:00.000Z",
                                    "user": None,
                                    "externalUser": {"displayName": "Relay"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                        }
                    }
                }
            raise AssertionError("unexpected query")

        node = _linear_node(comments=[("Claude", "first page body", "2026-10-20T00:00:00.000Z")])
        node["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c0"}
        with mock.patch.object(sources, "_linear_query", side_effect=fake_query):
            sources._linear_complete_comments(node)
        bodies = [c["body"] for c in node["comments"]["nodes"]]
        # one original page plus the follow-up pages up to the bound
        self.assertEqual(len(bodies), sources.LINEAR_COMMENT_MAX_PAGES)
        self.assertIn("extra page body", bodies)
        # past the bound the node is flagged, not warned: iter_linear decides to skip it
        self.assertTrue(node.get("_linear_truncated"))

    def test_overlong_thread_is_skipped_not_ingested_partial(self) -> None:
        node = _linear_node()
        node["_linear_truncated"] = True
        sources.take_warnings()  # drain anything earlier tests left
        docs = list(
            sources.iter_linear(
                covered_ids=set(),
                light_scan=lambda: {"uuid-1": "2026-10-21T01:00:00.000Z"},
                fetch_full=lambda ids: [node],
            )
        )
        self.assertEqual([], docs)
        warnings = sources.take_warnings()
        self.assertTrue(
            any("skipped instead of ingesting a truncated copy" in w for w in warnings),
            warnings,
        )

    def test_scan_and_fetch_fail_closed(self) -> None:
        with mock.patch.object(
            sources.subprocess,
            "run",
            side_effect=OSError("no simple-board here"),
        ):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_light_scan()

        def ok_proc(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout='{"errors": [{"message": "boom"}]}', stderr="")

        with mock.patch.object(sources.subprocess, "run", side_effect=ok_proc):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_query("query { x }")

        def bad_rc(*a, **k):
            return subprocess.CompletedProcess(a, 4, stdout="", stderr="no Fleet credentials")

        with mock.patch.object(sources.subprocess, "run", side_effect=bad_rc):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_query("query { x }")

    def test_oversized_chunk_halves_until_it_fits(self) -> None:
        """A response past the CLI size cap is retried as smaller chunks, not a failure."""
        def fake_query(query, timeout_s=0):
            if "filter: { id: { in: [" in query and query.count("uuid-") > 1:
                raise sources.LinearSourceError(
                    "linear: simple-board api query exited 1: response exceeds the CLI size cap",
                    overflow=True,
                )
            for uid in ("uuid-a", "uuid-b"):
                if uid in query:
                    return {"issues": {"nodes": [_linear_node(uid=uid)], "pageInfo": {}}}
            raise AssertionError("unexpected query")

        with mock.patch.object(sources, "_linear_query", side_effect=fake_query):
            nodes = sources._linear_fetch_full(["uuid-a", "uuid-b"])
        self.assertEqual(["uuid-a", "uuid-b"], [n["id"] for n in nodes])

    def test_single_issue_too_large_is_skipped_with_a_placeholder(self) -> None:
        """An issue that overflows even alone degrades to a flagged placeholder, so the run
        skips it (warning) instead of dying or ingesting a partial copy."""
        def fake_query(query, timeout_s=0):
            if "title url description" in query:
                raise sources.LinearSourceError(
                    "linear: simple-board api query exited 1: response exceeds the CLI size cap",
                    overflow=True,
                )
            return {"issues": {"nodes": [{"id": "uuid-huge", "identifier": "AFC-777"}]}}

        sources.take_warnings()  # drain
        with mock.patch.object(sources, "_linear_query", side_effect=fake_query):
            docs = list(
                sources.iter_linear(
                    covered_ids=set(),
                    light_scan=lambda: {"uuid-huge": "2026-10-26T00:00:00.000Z"},
                )
            )
        self.assertEqual([], docs)
        warnings = sources.take_warnings()
        self.assertTrue(any("exceeds the CLI size cap" in w for w in warnings), warnings)
        self.assertTrue(
            any("skipped instead of ingesting a truncated copy" in w for w in warnings), warnings
        )

    def test_oversized_comment_page_flags_the_node_not_raises(self) -> None:
        """A comment page too large for the CLI flags the node for skipping;  it must not kill
        the run, and must not let a partial copy through."""
        def fake_query(query, timeout_s=0):
            if "issue(id:" in query:
                raise sources.LinearSourceError(
                    "linear: simple-board api query exited 1: response exceeds the CLI size cap",
                    overflow=True,
                )
            raise AssertionError("unexpected query")

        node = _linear_node(comments=[("Claude", "first page body", "2026-10-20T00:00:00.000Z")])
        node["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c0"}
        with mock.patch.object(sources, "_linear_query", side_effect=fake_query):
            sources._linear_complete_comments(node)
        self.assertTrue(node.get("_linear_truncated"))

    def test_selection_advances_from_the_ingest_state(self) -> None:
        """An issue already ingested at its current updatedAt is not refetched;  a changed one is."""
        state_path = pathlib.Path(self.tmp) / "ingest-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "linear/uuid-1": {
                        "source": "linear",
                        "updated_at": sources.parse_ts_ms("2026-10-21T01:00:00.000Z"),
                    },
                    "board/aaaa1111": {"source": "board", "updated_at": 1},
                }
            ),
            encoding="utf-8",
        )
        refresh_path = pathlib.Path(self.tmp) / "linear-source-refresh.json"
        refresh_path.write_text(json.dumps({"linear/uuid-1": sources.now_ms()}), encoding="utf-8")
        scan = {
            "uuid-1": {"identifier": "AFC-999", "updatedAt": "2026-10-21T01:00:00.000Z"},
            "uuid-2": {"identifier": "AFC-1000", "updatedAt": "2026-10-22T00:00:00.000Z"},
        }
        seen = {}

        def fetch(ids):
            seen["ids"] = list(ids)
            return [_linear_node(uid="uuid-2", identifier="AFC-1000")]

        env = {
            "FLEET_RAG_LINEAR_STATE": str(state_path),
            "FLEET_RAG_LINEAR_REFRESH_STATE": str(refresh_path),
        }
        with mock.patch.dict(os.environ, env):
            docs = list(
                sources.iter_linear(covered_ids=set(), light_scan=lambda: scan, fetch_full=fetch)
            )
        self.assertEqual(["uuid-2"], seen["ids"])
        self.assertEqual(["linear/uuid-2"], [d.doc_id for d in docs])

    def test_full_env_refetches_despite_state(self) -> None:
        state_path = pathlib.Path(self.tmp) / "ingest-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "linear/uuid-1": {
                        "source": "linear",
                        "updated_at": sources.parse_ts_ms("2026-10-21T01:00:00.000Z"),
                    }
                }
            ),
            encoding="utf-8",
        )
        scan = {"uuid-1": {"identifier": "AFC-999", "updatedAt": "2026-10-21T01:00:00.000Z"}}
        seen = {}

        def fetch(ids):
            seen["ids"] = list(ids)
            return [_linear_node()]

        env = {"FLEET_RAG_LINEAR_STATE": str(state_path), "FLEET_RAG_LINEAR_FULL": "1"}
        with mock.patch.dict(os.environ, env):
            docs = list(
                sources.iter_linear(covered_ids=set(), light_scan=lambda: scan, fetch_full=fetch)
            )
        self.assertEqual(["uuid-1"], seen["ids"])
        self.assertEqual(["linear/uuid-1"], [d.doc_id for d in docs])

    def test_missing_or_corrupt_state_fetches_all(self) -> None:
        scan = {"uuid-1": {"identifier": "AFC-999", "updatedAt": "2026-10-21T01:00:00.000Z"}}
        seen = {}

        def fetch(ids):
            seen["ids"] = list(ids)
            return [_linear_node()]

        missing = str(pathlib.Path(self.tmp) / "nope.json")
        with mock.patch.dict(os.environ, {"FLEET_RAG_LINEAR_STATE": missing}):
            docs = list(
                sources.iter_linear(covered_ids=set(), light_scan=lambda: scan, fetch_full=fetch)
            )
        self.assertEqual(["uuid-1"], seen["ids"])
        self.assertEqual(1, len(docs))

    def test_app_label_wins_and_team_fallbacks_match_the_frozen_board(self) -> None:
        """The migration's app:<slug> label preserves the original board app for merged rows;
        team fallbacks follow the frozen board's own evidence."""
        node = _linear_node(team="AFC", labels=("app:codecaps", "seat:CLAUDE"))
        (doc,) = sources.iter_linear(
            covered_ids=set(),
            light_scan=lambda: {"uuid-1": "2026-10-21T01:00:00.000Z"},
            fetch_full=lambda ids: [node],
        )
        self.assertEqual(doc.app, "codecaps")
        for team, expected in (
            ("AFC", "fleet"),
            ("OPS", "fleet-ops"),
            ("C", "clutch"),
            ("CK", "clutch"),
            ("HH", "hoghunter"),
            ("CC", "codecaps"),
            ("ST", "socratic-trade"),  # lowercase alias path
        ):
            plain = _linear_node(team=team, labels=())
            (doc,) = sources.iter_linear(
                covered_ids=set(),
                light_scan=lambda: {"uuid-1": "2026-10-21T01:00:00.000Z"},
                fetch_full=lambda ids, n=plain: [n],
            )
            self.assertEqual(doc.app, expected, team)

    def test_stale_refresh_mark_forces_refetch_despite_covering_state(self) -> None:
        """An unchanged updatedAt only skips the fetch inside the refresh window:  past it the
        doc is refetched, so a comment edit that never bumped updatedAt cannot hide forever."""
        state_path = pathlib.Path(self.tmp) / "ingest-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "linear/uuid-1": {
                        "source": "linear",
                        "updated_at": sources.parse_ts_ms("2026-10-21T01:00:00.000Z"),
                    }
                }
            ),
            encoding="utf-8",
        )
        refresh_path = pathlib.Path(self.tmp) / "linear-source-refresh.json"
        stale = sources.now_ms() - 8 * 24 * 3600 * 1000  # beyond the 7-day default window
        refresh_path.write_text(json.dumps({"linear/uuid-1": stale}), encoding="utf-8")
        scan = {"uuid-1": {"identifier": "AFC-999", "updatedAt": "2026-10-21T01:00:00.000Z"}}
        seen = {}

        def fetch(ids):
            seen["ids"] = list(ids)
            return [_linear_node()]

        env = {
            "FLEET_RAG_LINEAR_STATE": str(state_path),
            "FLEET_RAG_LINEAR_REFRESH_STATE": str(refresh_path),
        }
        with mock.patch.dict(os.environ, env):
            docs = list(
                sources.iter_linear(covered_ids=set(), light_scan=lambda: scan, fetch_full=fetch)
            )
        self.assertEqual(["uuid-1"], seen["ids"])
        self.assertEqual(1, len(docs))
        # the fetch refreshed the mark
        marks = json.loads(refresh_path.read_text(encoding="utf-8"))
        self.assertGreater(marks["linear/uuid-1"], stale)

    def test_coverage_files_missing_fails_closed(self) -> None:
        missing = pathlib.Path(self.tmp) / "nope"
        with self.assertRaises(sources.LinearSourceError) as ctx:
            sources.load_linear_covered_ids(missing / "map.tsv", missing / "db.sqlite")
        self.assertIn("db.sqlite", str(ctx.exception))
        db = pathlib.Path(self.tmp) / "findings.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE findings (id TEXT PRIMARY KEY)")
        con.commit()
        con.close()
        with self.assertRaises(sources.LinearSourceError) as ctx:
            sources.load_linear_covered_ids(missing / "map.tsv", db)
        self.assertIn("map.tsv", str(ctx.exception))

    def test_empty_scan_is_a_broken_read_not_an_empty_board(self) -> None:
        def empty_page(*a, **k):
            return subprocess.CompletedProcess(
                a, 0, stdout='{"issues": {"nodes": [], "pageInfo": {"hasNextPage": false}}}', stderr=""
            )

        with mock.patch.object(sources.subprocess, "run", side_effect=empty_page):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_light_scan()

        def no_connection(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout="{}", stderr="")

        with mock.patch.object(sources.subprocess, "run", side_effect=no_connection):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_light_scan()

    def test_full_node_shape_is_validated_before_use(self) -> None:
        node = _linear_node()
        del node["comments"]
        with self.assertRaises(sources.LinearSourceError):
            sources._linear_validate_full(node)
        node2 = _linear_node()
        node2["id"] = 42
        with self.assertRaises(sources.LinearSourceError):
            sources._linear_validate_full(node2)

    def test_shared_deadline_stops_pagination(self) -> None:
        past = sources.time.monotonic() - 1
        with self.assertRaises(sources.LinearSourceError) as ctx:
            sources._linear_remaining(past)
        self.assertIn("deadline", str(ctx.exception))
        with mock.patch.object(
            sources, "_linear_query", return_value={"issues": {"nodes": [], "pageInfo": {}}}
        ) as q:
            try:
                sources._linear_light_scan(deadline=past)
            except sources.LinearSourceError:
                pass
        q.assert_not_called()

    def test_missing_ids_after_fetch_raise(self) -> None:
        with mock.patch.object(
            sources, "_linear_query", return_value={"issues": {"nodes": [], "pageInfo": {}}}
        ):
            with self.assertRaises(sources.LinearSourceError):
                sources._linear_fetch_full(["uuid-gone"])

    def test_limit_picks_the_most_recently_updated(self) -> None:
        scan = {"a": "2026-10-20T00:00:00.000Z", "b": "2026-10-26T00:00:00.000Z"}
        seen = {}

        def fetch(ids):
            seen["ids"] = ids
            return [_linear_node(uid="b", identifier="AFC-7")]

        docs = list(
            sources.iter_linear(
                limit=1,
                covered_ids=set(),
                light_scan=lambda: scan,
                fetch_full=fetch,
            )
        )
        self.assertEqual(seen["ids"], ["b"])
        self.assertEqual([d.doc_id for d in docs], ["linear/b"])

    def test_covered_ids_come_from_actual_frozen_coverage_not_tsv_membership(self) -> None:
        db = pathlib.Path(self.tmp) / "findings.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE findings (id TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO findings VALUES (?)", [("aaaa1111",), ("bbbb2222",)])
        con.commit()
        con.close()
        covered = sources.load_linear_covered_ids(self.map_path, db)
        # uuid-imported-old maps aaaa1111 (in the frozen db) -> covered
        self.assertIn("uuid-imported-old", covered)
        # uuid-imported-active maps bbbb2222 (in the frozen db) -> covered too
        self.assertIn("uuid-imported-active", covered)
        # a TSV row whose board_id never became a finding is NOT skipped
        self.map_path.write_text(
            self.map_path.read_text(encoding="utf-8")
            + "cccc3333\tgh:Simple-With-Us/X#1\tAFC\tAFC-3\thttps://linear.app/x/AFC-3\tuuid-gh-only\n",
            encoding="utf-8",
        )
        covered = sources.load_linear_covered_ids(self.map_path, db)
        self.assertNotIn("uuid-gh-only", covered)

    def test_merged_issue_is_covered_only_when_every_mapped_row_is(self) -> None:
        db = pathlib.Path(self.tmp) / "findings.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE findings (id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO findings VALUES ('aaaa1111')")
        con.commit()
        con.close()
        self.map_path.write_text(
            self.map_path.read_text(encoding="utf-8")
            + "dddd4444\tgh:x\tAFC\tAFC-2\thttps://linear.app/x/AFC-2\tuuid-imported-active\n",
            encoding="utf-8",
        )
        covered = sources.load_linear_covered_ids(self.map_path, db)
        # uuid-imported-active now maps bbbb2222 (missing) and dddd4444 (missing) -> not covered
        self.assertNotIn("uuid-imported-active", covered)
        self.assertIn("uuid-imported-old", covered)

    def test_rereading_a_changed_issue_keeps_the_doc_id_and_changes_the_text(self) -> None:
        scan = {"uuid-1": "2026-10-21T01:00:00.000Z"}
        first = list(
            sources.iter_linear(
                covered_ids=set(),
                light_scan=lambda: scan,
                fetch_full=lambda ids: [_linear_node(description="version one")],
            )
        )
        second = list(
            sources.iter_linear(
                covered_ids=set(),
                light_scan=lambda: {"uuid-1": "2026-10-22T01:00:00.000Z"},
                fetch_full=lambda ids: [_linear_node(description="version two", updated="2026-10-22T01:00:00.000Z")],
            )
        )
        self.assertEqual(first[0].doc_id, second[0].doc_id)
        self.assertNotEqual(first[0].text_markdown, second[0].text_markdown)
        self.assertLess(first[0].updated_at_ms, second[0].updated_at_ms)
