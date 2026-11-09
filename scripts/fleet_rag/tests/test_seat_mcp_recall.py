"""seat-mcp recall bridge:  registration, schemas, and guardrails.  No network.

Run from scripts/:  python3 -m unittest fleet_rag.tests.test_seat_mcp_recall -v

The tracked seat_mcp package is imported from scripts/seat-mcp.  recall_api is stubbed with the
TOOL CONTRACT so the test does not depend on the live corpus or on the other builder's module.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS / "seat-mcp"))

from fleet_rag import public_fallback  # noqa: E402
from seat_mcp import recall_bridge  # noqa: E402
from seat_mcp.seats import SeatError  # noqa: E402
from seat_mcp.tools import TOOL_IMPL, tool_schemas  # noqa: E402

RECALL_TOOLS = ("recall_search", "recall_stats", "recall_contribute", "recall_correct", "recall_retract")
LEGACY_TOOLS = (
    "seat_launch", "seat_status", "seat_reply", "seat_result",
    "grok_sessions_list", "grok_session_peek", "grok_session_tail",
    "grok_session_prompt", "grok_session_await", "grok_session_cancel",
)


def _stub_api() -> types.ModuleType:
    """A fake fleet_rag.recall_api with exactly the contract signatures; records calls."""
    mod = types.ModuleType("fleet_rag.recall_api")
    mod.calls = []  # type: ignore[attr-defined]

    def recall_search(query, limit=5, category=None, app=None, source=None, seat=None, since_days=None,
                      include_retracted=False):
        mod.calls.append(("search", dict(query=query, limit=limit, category=category, app=app,
                                         source=source, seat=seat, since_days=since_days,
                                         include_retracted=include_retracted)))
        return {"hits": [{"score": 0.9, "text": "t", "source": "doc", "app": "fleet", "category": "lesson",
                          "seat": "CLAUDE", "doc_id": "doc/x", "chunk_index": 0, "heading": "", "title": "x",
                          "url": "", "path": "", "created_at": 1}], "mode": "hybrid"}

    def recall_stats():
        mod.calls.append(("stats", {}))
        return {"collection": "fleet-agents", "status": "green", "points": 8, "embedder_healthy": True,
                "by_source": {"doc": 8}, "by_app": {"fleet": 8}}

    def recall_contribute(text, category, app="fleet", seat=None, title=None, url=None):
        mod.calls.append(("contribute", dict(text=text, category=category, app=app, seat=seat, title=title, url=url)))
        if len(text.strip()) < 40:
            raise ValueError("text must be 40..4000 chars")
        return {"id": "uuid", "doc_id": f"contrib/{seat}/2026-09-01/abcdef12", "scrubbed": [], "status": "completed"}

    mod.recall_search = recall_search  # type: ignore[attr-defined]
    mod.recall_stats = recall_stats  # type: ignore[attr-defined]
    def recall_correct(doc_id, text, reason, seat=None, title=None, url=None, expected_revision=None):
        mod.calls.append(("correct", dict(doc_id=doc_id, text=text, reason=reason, seat=seat,
                                          title=title, url=url, expected_revision=expected_revision)))
        return {"id": "uuid", "doc_id": doc_id, "seat": seat, "revision": 2, "status": "completed", "corrected_at": 1, "corrected_by": seat, "previous_content_hash": "old", "content_hash": "new", "scrubbed": []}

    def recall_retract(doc_id, reason, seat=None, expected_revision=None):
        mod.calls.append(("retract", dict(doc_id=doc_id, reason=reason, seat=seat,
                                          expected_revision=expected_revision)))
        return {"id": "uuid", "doc_id": doc_id, "seat": seat, "revision": 2, "retracted": True, "status": "completed", "retracted_at": 1, "retracted_by": seat, "retraction_reason": reason}

    mod.recall_contribute = recall_contribute  # type: ignore[attr-defined]
    mod.recall_correct = recall_correct  # type: ignore[attr-defined]
    mod.recall_retract = recall_retract  # type: ignore[attr-defined]
    return mod


class RegistrationTests(unittest.TestCase):
    def test_recall_tools_registered(self) -> None:
        for name in RECALL_TOOLS:
            self.assertIn(name, TOOL_IMPL)
            self.assertTrue(callable(TOOL_IMPL[name]))

    def test_existing_tools_untouched(self) -> None:
        for name in LEGACY_TOOLS:
            self.assertIn(name, TOOL_IMPL)
        self.assertEqual(len(TOOL_IMPL), len(LEGACY_TOOLS) + len(RECALL_TOOLS))

    def test_schemas_present_and_match_impl(self) -> None:
        schemas = {s["name"]: s for s in tool_schemas()}
        self.assertEqual(set(schemas), set(TOOL_IMPL))
        for name in RECALL_TOOLS:
            s = schemas[name]
            self.assertTrue(s["description"])
            self.assertEqual(s["inputSchema"]["type"], "object")
        self.assertEqual(schemas["recall_search"]["inputSchema"]["required"], ["query"])
        self.assertEqual(set(schemas["recall_contribute"]["inputSchema"]["required"]), {"text", "category", "seat"})
        self.assertEqual(
            set(schemas["recall_contribute"]["inputSchema"]["properties"]["category"]["enum"]),
            {"lesson", "preference", "infrastructure", "decision", "runbook"},
        )
        for key in ("category", "app", "source", "seat", "since_days", "limit", "include_retracted"):
            self.assertIn(key, schemas["recall_search"]["inputSchema"]["properties"])
        self.assertEqual(set(schemas["recall_correct"]["inputSchema"]["required"]), {"doc_id", "text", "reason", "seat"})
        self.assertEqual(set(schemas["recall_retract"]["inputSchema"]["required"]), {"doc_id", "reason", "seat"})


class StubbedBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _stub_api()
        recall_bridge._reset_api_cache()
        self._p = patch.object(recall_bridge, "_load_api", return_value=self.api)
        self._p.start()
        self._mutation = patch.object(
            public_fallback, "call_mutation", lambda name, args, run_local: run_local()
        )
        self._mutation.start()

    def tearDown(self) -> None:
        self._mutation.stop()
        self._p.stop()
        recall_bridge._reset_api_cache()

    def test_search_passes_contract_kwargs(self) -> None:
        out = TOOL_IMPL["recall_search"]({"query": "grep trap", "limit": "3", "category": "Lesson",
                                          "app": "Fleet", "seat": "claude", "since_days": 7})
        self.assertEqual(out["mode"], "hybrid")
        self.assertEqual(len(out["hits"]), 1)
        kind, kw = self.api.calls[-1]
        self.assertEqual(kind, "search")
        self.assertEqual(kw, dict(query="grep trap", limit=3, category="lesson", app="fleet",
                                  source=None, seat="CLAUDE", since_days=7, include_retracted=False))
        TOOL_IMPL["recall_search"]({"query": "grep trap", "include_retracted": True})
        self.assertTrue(self.api.calls[-1][1]["include_retracted"])

    def test_search_requires_query(self) -> None:
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_search"]({})
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_search"]({"query": "   "})

    def test_search_limit_bounds(self) -> None:
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_search"]({"query": "x", "limit": 0})
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_search"]({"query": "x", "limit": 999})

    def test_stats(self) -> None:
        out = TOOL_IMPL["recall_stats"]({})
        self.assertEqual(out["collection"], "fleet-agents")
        self.assertIn("by_source", out)

    def test_contribute_requires_seat(self) -> None:
        with patch.dict(os.environ, {"AGENT_SEAT": "CLAUDE"}):
            with self.assertRaises(SeatError) as ctx:
                TOOL_IMPL["recall_contribute"]({"text": "x" * 60, "category": "lesson"})
        self.assertIn("seat is required", str(ctx.exception))
        self.assertEqual(self.api.calls, [])  # never reached the API

    def test_contribute_validates_category(self) -> None:
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_contribute"]({"text": "x" * 60, "category": "finding", "seat": "GROK"})
        with self.assertRaises(SeatError):
            TOOL_IMPL["recall_contribute"]({"text": "x" * 60, "seat": "GROK"})

    def test_contribute_happy_path(self) -> None:
        out = TOOL_IMPL["recall_contribute"]({"text": "Never grep the handoff file for KEY=value lines; names only.",
                                              "category": "Lesson", "seat": "grok", "title": "grep trap",
                                              "url": "https://example.test/x"})
        self.assertTrue(out["doc_id"].startswith("contrib/GROK/"))
        kind, kw = self.api.calls[-1]
        self.assertEqual(kind, "contribute")
        self.assertEqual(kw["seat"], "GROK")
        self.assertEqual(kw["category"], "lesson")
        self.assertEqual(kw["app"], "fleet")
        self.assertEqual(kw["title"], "grep trap")

    def test_correct_and_retract_require_seat_and_pass_contract(self) -> None:
        doc = "contrib/GROK/2026-11-05/472fc36e"
        for name, args in (("recall_correct", {"doc_id": doc, "text": "x" * 60, "reason": "current"}),
                           ("recall_retract", {"doc_id": doc, "reason": "obsolete"})):
            with self.subTest(name), self.assertRaises(SeatError):
                TOOL_IMPL[name](args)
        out = TOOL_IMPL["recall_correct"]({"doc_id": doc, "text": "x" * 60, "reason": "current",
                                           "seat": "grok", "expected_revision": 2})
        self.assertEqual(out["revision"], 2)
        self.assertEqual(self.api.calls[-1][1]["seat"], "GROK")
        self.assertEqual(self.api.calls[-1][1]["expected_revision"], 2)
        with self.assertRaisesRegex(SeatError, "shared bearer does not prove the owner identity"):
            TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "obsolete", "seat": "OWNER"})
        self.assertNotEqual(self.api.calls[-1][0], "retract")
        out = TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "obsolete", "seat": "grok"})
        self.assertTrue(out["retracted"])
        self.assertEqual(self.api.calls[-1], ("retract", {"doc_id": doc, "reason": "obsolete",
                                                           "seat": "GROK", "expected_revision": None}))

    def test_mutation_arguments_and_response_are_fail_closed(self) -> None:
        doc = "contrib/GROK/2026-11-05/472fc36e"
        with self.assertRaisesRegex(SeatError, "doc_id must"):
            TOOL_IMPL["recall_retract"]({"doc_id": "docs/not-a-contribution", "reason": "obsolete",
                                          "seat": "GROK"})
        with self.assertRaisesRegex(SeatError, "reason must"):
            TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "x" * 501, "seat": "GROK"})

        deep = {}
        cursor = deep
        for _ in range(17):
            cursor["x"] = {}
            cursor = cursor["x"]
        with self.assertRaisesRegex(SeatError, "depth"):
            TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "obsolete", "seat": "GROK",
                                          "extra": deep})
        wide = {"expected_revision": {str(i): i for i in range(300)}}
        with self.assertRaisesRegex(SeatError, "node"):
            TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "obsolete", "seat": "GROK", **wide})

        self.api.recall_retract = lambda **kwargs: {"id": "uuid", "doc_id": doc}  # type: ignore[method-assign]
        with self.assertRaisesRegex(SeatError, "invalid response field"):
            TOOL_IMPL["recall_retract"]({"doc_id": doc, "reason": "obsolete", "seat": "GROK"})

    def test_api_value_error_becomes_seat_error(self) -> None:
        with self.assertRaises(SeatError) as ctx:
            TOOL_IMPL["recall_contribute"]({"text": "too short", "category": "lesson", "seat": "GROK"})
        self.assertIn("40..4000", str(ctx.exception))


class MissingPackageTests(unittest.TestCase):
    def test_missing_package_is_clear_seat_error(self) -> None:
        recall_bridge._reset_api_cache()
        with tempfile.TemporaryDirectory() as empty:
            with patch.dict(os.environ, {"FLEET_RAG_HOME": empty}):
                with self.assertRaises(SeatError) as ctx:
                    TOOL_IMPL["recall_stats"]({})
        msg = str(ctx.exception)
        self.assertIn("install-fleet-rag.sh", msg)
        self.assertIn(empty, msg)
        self.assertNotIn(empty, sys.path)  # guarded insert: nothing added when the package is absent
        recall_bridge._reset_api_cache()

    def test_installed_location_is_used(self) -> None:
        recall_bridge._reset_api_cache()
        with tempfile.TemporaryDirectory() as home:
            pkg = Path(home) / "fleet_rag"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "public_fallback.py").write_text("def call_mutation(name, kwargs, run_local):\n    return run_local()\n")
            (pkg / "recall_api.py").write_text(
                "def recall_search(query, limit=5, category=None, app=None, source=None, seat=None, since_days=None):\n"
                "    return {'hits': [], 'mode': 'dense'}\n"
                "def recall_stats():\n"
                "    return {'collection': 'c', 'status': 'green', 'points': 0, 'embedder_healthy': False,\n"
                "            'by_source': {}, 'by_app': {}}\n"
                "def recall_contribute(text, category, app='fleet', seat=None, title=None, url=None):\n"
                "    return {'id': 'i', 'doc_id': 'd', 'scrubbed': [], 'status': 'ok'}\n"
                "def recall_correct(doc_id, text, reason, seat=None, title=None, url=None, expected_revision=None):\n"
                "    return {'id': 'i', 'doc_id': doc_id, 'status': 'ok'}\n"
                "def recall_retract(doc_id, reason, seat=None, expected_revision=None):\n"
                "    return {'id': 'i', 'doc_id': doc_id, 'retracted': True, 'status': 'ok'}\n"
            )
            saved = {k: v for k, v in sys.modules.items() if k == "fleet_rag" or k.startswith("fleet_rag.")}
            for k in saved:
                del sys.modules[k]
            try:
                with patch.dict(os.environ, {"FLEET_RAG_HOME": home}):
                    out = TOOL_IMPL["recall_stats"]({})
                self.assertEqual(out["points"], 0)
                self.assertIn(home, sys.path)
            finally:
                for k in [k for k in sys.modules if k == "fleet_rag" or k.startswith("fleet_rag.")]:
                    del sys.modules[k]
                sys.modules.update(saved)
                if home in sys.path:
                    sys.path.remove(home)
                recall_bridge._reset_api_cache()


if __name__ == "__main__":
    unittest.main()
