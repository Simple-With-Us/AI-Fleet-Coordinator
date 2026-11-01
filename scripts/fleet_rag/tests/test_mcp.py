"""End-to-end test of fleet-recall-mcp.py over stdio with FLEET_RECALL_FAKE=1.

    cd scripts && python3 -m unittest fleet_rag.tests.test_mcp -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from fleet_rag import recall_api
from fleet_rag.core import build_point

SERVER = pathlib.Path(__file__).resolve().parents[2] / "fleet-recall-mcp.py"


def run_server(messages: list, env_extra: dict | None = None) -> tuple[list, str]:
    env = {**os.environ, "FLEET_RECALL_FAKE": "1", "AGENT_SEAT": "TESTSEAT", **(env_extra or {})}
    stdin = "\n".join(json.dumps(m) if not isinstance(m, str) else m for m in messages) + "\n"
    proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True, text=True,
                          env=env, timeout=60)
    out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return out, proc.stderr


class McpServerTests(unittest.TestCase):
    def test_handshake_list_call_and_errors(self):
        good = ("pm2 start does not re-read env from the ecosystem file; restart with --update-env "
                "so the cached PATH is replaced.")
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "recall_stats"}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "recall_search",
                        "arguments": {"query": "leaking credentials handoff", "limit": "2", "app": "fleet"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "recall_contribute", "arguments": {"text": good, "category": "lesson"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "recall_search", "arguments": {"query": "update-env", "source": "agent-contribution"}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "recall_contribute", "arguments": {"text": "too short", "category": "lesson"}}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 10, "method": "resources/list"},
            "this is not json",
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "recall_search", "arguments": {"query": "x", "bogus": 1}}},
        ]
        out, stderr = run_server(msgs)
        self.assertIn("FAKE backend", stderr)
        by_id = {m.get("id"): m for m in out}

        init = by_id[1]["result"]
        self.assertEqual(init["protocolVersion"], "2024-11-05")   # client version accepted
        self.assertEqual(init["serverInfo"]["name"], "fleet-recall")
        self.assertIn("tools", init["capabilities"])

        self.assertEqual(by_id[2]["result"], {})

        tools = by_id[3]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["recall_search", "recall_stats", "recall_contribute"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertIn("description", t)
        self.assertEqual(tools[0]["inputSchema"]["required"], ["query"])
        self.assertEqual(tools[2]["inputSchema"]["required"], ["text", "category"])
        self.assertEqual(tools[2]["inputSchema"]["properties"]["force"]["type"], "boolean")
        self.assertNotIn("default", tools[2]["inputSchema"]["properties"]["force"])
        self.assertIn("duplicate", tools[2]["description"])

        stats = json.loads(by_id[4]["result"]["content"][0]["text"])
        self.assertFalse(by_id[4]["result"]["isError"])
        self.assertEqual(stats["points"], 3)
        self.assertEqual(stats["by_source"]["doc"], 3)

        search = by_id[5]["result"]["structuredContent"]
        self.assertEqual(search["mode"], "hybrid")
        self.assertEqual(len(search["hits"]), 2)
        self.assertIn("global-api-keys", search["hits"][0]["text"])

        contrib = json.loads(by_id[6]["result"]["content"][0]["text"])
        self.assertFalse(by_id[6]["result"]["isError"])
        self.assertTrue(contrib["doc_id"].startswith("contrib/TESTSEAT/"))
        self.assertEqual(contrib["status"], "completed")

        again = by_id[7]["result"]["structuredContent"]
        self.assertEqual(len(again["hits"]), 1)
        self.assertEqual(again["hits"][0]["source"], "agent-contribution")
        self.assertEqual(again["hits"][0]["seat"], "TESTSEAT")

        short = by_id[8]["result"]
        self.assertTrue(short["isError"])
        self.assertIn("too short", short["content"][0]["text"])

        self.assertEqual(by_id[9]["error"]["code"], -32602)
        self.assertEqual(by_id[10]["error"]["code"], -32601)
        self.assertEqual(by_id[None]["error"]["code"], -32700)
        self.assertTrue(by_id[11]["result"]["isError"])
        self.assertIn("bogus", by_id[11]["result"]["content"][0]["text"])

        # The notification produced no response line.
        self.assertEqual(len(out), 12)
        self.assertTrue(all(m["jsonrpc"] == "2.0" for m in out))

    def test_tools_list_without_credentials(self):
        # No fake backend, no env keys, unreadable HOME: initialize + tools/list must still answer.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("QDRANT_", "TEI_"))}
        env.update({"HOME": "/nonexistent-home-for-test", "FLEET_RECALL_FAKE": "0"})
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
        proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True, text=True,
                              env=env, timeout=60)
        out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(out[1]["result"]["tools"]), 3)

    def test_missing_seat_is_tool_error_not_crash(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "recall_contribute",
                        "arguments": {"text": "a" * 60, "category": "decision"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        env = {k: v for k, v in os.environ.items() if k != "AGENT_SEAT"}
        env["FLEET_RECALL_FAKE"] = "1"
        stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
        proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True, text=True,
                              env=env, timeout=60)
        out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(out[0]["result"]["isError"])
        self.assertIn("seat", out[0]["result"]["content"][0]["text"])
        self.assertEqual(out[1]["result"], {})


class McpRobustnessTests(unittest.TestCase):
    """Malformed params and backend failures must never terminate the server loop."""

    def test_bad_params_then_backend_error_then_tools_list_in_one_process(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "recall_stats"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": ["recall_stats"]},
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": 42},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": "nope"},   # ignored
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "recall_search", "arguments": {"query": "x", "category": ["doc"]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "recall_search", "arguments": {"query": "x", "source": "meta"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": None}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "recall_stats"}},
        ]
        out, stderr = run_server(msgs)
        by_id = {m.get("id"): m for m in out}
        for rid in (1, 2, 3):
            self.assertEqual(by_id[rid]["error"]["code"], -32602, rid)
            self.assertIn("params must be an object", by_id[rid]["error"]["message"])
        self.assertNotIn("nope", "".join(json.dumps(m) for m in out))
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertIn("category must be a string", by_id[4]["result"]["content"][0]["text"])
        self.assertTrue(by_id[5]["result"]["isError"])
        self.assertIn("source must be one of", by_id[5]["result"]["content"][0]["text"])
        self.assertEqual(by_id[6]["error"]["code"], -32602)
        self.assertEqual(len(by_id[7]["result"]["tools"]), 3)                 # loop still alive
        stats = json.loads(by_id[8]["result"]["content"][0]["text"])
        self.assertEqual(stats["points"], 3)
        self.assertEqual(stats["by_app"], {"fleet": 3, "other": 0})
        self.assertEqual(len(out), 8)                                          # notification: no line

    def test_backend_exception_and_handler_crash_do_not_end_the_loop(self):
        path = str(SERVER)
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_under_test", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.FAKE_CORPUS = True  # these tests install the in-process fake corpus themselves
        saved = {k: getattr(recall_api, k) for k in
                 ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
                 "gitleaks_available")}
        recall_api.install_fake_backend()
        try:
            class BoomQdrant(recall_api.FakeQdrant):
                def info(self):
                    raise RuntimeError("qdrant exploded with a body that must not be echoed")

                def query_hybrid(self, *a, **k):
                    raise ConnectionResetError("peer reset")

            recall_api.Qdrant = BoomQdrant
            real_handle = mod.handle

            def flaky_handle(msg):
                if msg.get("id") == 3:
                    raise TypeError("handler bug")
                if msg.get("id") == 4:
                    raise ValueError("notification bug")
                return real_handle(msg)

            mod.handle = flaky_handle
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "recall_stats"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "recall_search", "arguments": {"query": "leaking credentials"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                {"jsonrpc": "2.0", "id": 4, "method": "ping"},
                [{"jsonrpc": "2.0", "id": 5, "method": "ping"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"}],
                {"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
            ]
            stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
            stdout, stderr = io.StringIO(), io.StringIO()
            real_stderr, sys.stderr = sys.stderr, stderr
            try:
                mod.serve(stdin, stdout)
            finally:
                sys.stderr = real_stderr
        finally:
            for k, v in saved.items():
                setattr(recall_api, k, v)
            recall_api.reset_config_cache()
        lines = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
        flat = []
        for ln in lines:
            flat.extend(ln if isinstance(ln, list) else [ln])
        by_id = {m.get("id"): m for m in flat}
        # Backend exceptions inside a tool call: tool error naming the class only.
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertEqual(by_id[1]["result"]["content"][0]["text"], "recall_stats failed: RuntimeError")
        self.assertNotIn("exploded", stdout.getvalue())
        self.assertTrue(by_id[2]["result"]["isError"])
        self.assertIn("ConnectionResetError", by_id[2]["result"]["content"][0]["text"])
        # A handler crash: -32603 for the request, the loop continues, batches included.
        self.assertEqual(by_id[3]["error"]["code"], -32603)
        self.assertIn("TypeError", by_id[3]["error"]["message"])
        self.assertEqual(by_id[4]["error"]["code"], -32603)
        self.assertEqual(by_id[5]["result"], {})
        batch = [ln for ln in lines if isinstance(ln, list)]
        self.assertEqual(len(batch), 1)
        self.assertEqual([m["id"] for m in batch[0]], [5, 3])
        self.assertEqual(len(by_id[6]["result"]["tools"]), 3)                 # still serving
        self.assertEqual(len(flat), 7)
        err = stderr.getvalue()
        self.assertIn("RuntimeError", err)
        self.assertIn("TypeError", err)
        self.assertNotIn("exploded", err)
        self.assertNotIn("handler bug", err)

    def test_handler_crash_on_notification_is_logged_without_a_response(self):
        path = str(SERVER)
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_under_test2", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.FAKE_CORPUS = True  # these tests install the in-process fake corpus themselves

        def crash(msg):
            if "id" not in msg:
                raise KeyError("boom")
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

        mod.handle = crash
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/x"}) + "\n"
                            + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        real_stderr, sys.stderr = sys.stderr, stderr
        try:
            mod.serve(stdin, stdout)
        finally:
            sys.stderr = real_stderr
        lines = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
        self.assertEqual(lines, [{"jsonrpc": "2.0", "id": 9, "result": {}}])
        self.assertIn("KeyError", stderr.getvalue())


class SearchOptionsTests(unittest.TestCase):
    """per_doc / rerank / prefer_lessons are declared in the schema and passed straight through."""

    SEAMS = ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
                 "gitleaks_available")

    def load(self):
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_options_test", str(SERVER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.FAKE_CORPUS = True  # these tests install the in-process fake corpus themselves
        return mod

    def setUp(self):
        self.saved = {k: getattr(recall_api, k) for k in self.SEAMS}
        recall_api.install_fake_backend()
        self.mod = self.load()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()

    def search(self, args: dict) -> dict:
        resp = self.mod.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "recall_search", "arguments": args}})
        return resp["result"]

    def test_schema_declares_the_options(self):
        props = self.mod.TOOLS[0]["inputSchema"]["properties"]
        self.assertEqual((props["per_doc"]["type"], props["per_doc"]["minimum"], props["per_doc"]["maximum"],
                          props["per_doc"]["default"]), ("integer", 1, recall_api.PER_DOC_MAX, 1))
        self.assertEqual(recall_api.PER_DOC_MAX, 3)
        for key in ("rerank", "prefer_lessons"):
            self.assertEqual(props[key]["type"], "boolean")
            self.assertIs(props[key]["default"], True)
            self.assertIn("description", props[key])
        self.assertEqual(self.mod.TOOLS[0]["inputSchema"]["required"], ["query"])
        self.assertIn("per_doc", self.mod.TOOLS[0]["description"])

    def test_options_pass_through_to_recall_search(self):
        seen: list[dict] = []

        def spy(query, **kw):
            seen.append({"query": query, **kw})
            return {"hits": [], "mode": "hybrid"}

        with mock.patch.object(recall_api, "recall_search", spy):
            res = self.search({"query": "q", "per_doc": "2", "rerank": False, "prefer_lessons": False})
            self.assertFalse(res["isError"])
            self.assertEqual(seen[-1], {"query": "q", "per_doc": 2, "rerank": False, "prefer_lessons": False})
            self.search({"query": "q"})
            self.assertEqual(seen[-1], {"query": "q"})                      # defaults left to recall_search
            self.search({"query": "q", "per_doc": None, "rerank": None, "prefer_lessons": None})
            self.assertEqual(seen[-1], {"query": "q"})                      # null means default
            self.search({"query": "q", "per_doc": 3, "rerank": True, "prefer_lessons": True, "limit": 7})
            self.assertEqual(seen[-1], {"query": "q", "per_doc": 3, "rerank": True, "prefer_lessons": True,
                                        "limit": 7})

    def test_option_validation_is_a_tool_error(self):
        for args, needle in (
            ({"query": "q", "rerank": "yes"}, "rerank must be a boolean"),
            ({"query": "q", "prefer_lessons": 1}, "prefer_lessons must be a boolean"),
            ({"query": "q", "per_doc": "two"}, "per_doc must be an integer"),
            ({"query": "q", "per_doc": 4}, f"per_doc must be an integer between 1 and {recall_api.PER_DOC_MAX}"),
            ({"query": "q", "per_doc": 0}, "per_doc must be an integer between 1 and"),
        ):
            res = self.search(args)
            self.assertTrue(res["isError"], args)
            self.assertIn(needle, res["content"][0]["text"])
        self.assertEqual(recall_api.FakeQdrant.calls, [])                  # nothing reached the backend

    def test_per_doc_returns_extra_chunks_of_one_document(self):
        for i in range(3):
            pt = build_point(f"pm2 update-env chunk {i}: the ecosystem file env is cached until restart.",
                             {"source": "doc", "app": "fleet", "category": "runbook", "seat": "CLAUDE",
                              "doc_id": "doc/pm2-env", "chunk_index": i, "chunk_count": 3, "heading": "",
                              "title": "pm2 env", "url": "", "path": "", "created_at": 1756684800000,
                              "updated_at": 1756684800000, "ingest_run": "fake"})
            recall_api.FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})
        one = self.search({"query": "pm2 update-env", "limit": 5})["structuredContent"]
        self.assertEqual([h["doc_id"] for h in one["hits"]].count("doc/pm2-env"), 1)
        two = self.search({"query": "pm2 update-env", "limit": 5, "per_doc": 2,
                                      "prefer_lessons": False, "rerank": False})["structuredContent"]
        self.assertEqual([h["doc_id"] for h in two["hits"]].count("doc/pm2-env"), 2)
        self.assertEqual(two["mode"], "hybrid")
        self.assertTrue(all(h["group_hits"] == 3 for h in two["hits"] if h["doc_id"] == "doc/pm2-env"))


class ContributeFallbackKwargsTests(unittest.TestCase):
    """force is local-only; fallback kwargs must not retain it after contribute_with_guard."""

    def load(self):
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_fallback_test", str(SERVER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.FAKE_CORPUS = True  # these tests install the in-process fake corpus themselves
        return mod

    def test_force_stripped_from_shared_work_before_fallback(self):
        mod = self.load()
        snapshots = {}

        def fake_call_with_fallback(name, kwargs, run_local):
            snapshots["before_local"] = dict(kwargs)
            out = run_local()
            snapshots["after_local"] = dict(kwargs)
            return out

        good = "x" * 60
        with mock.patch.dict(os.environ, {"AGENT_SEAT": "TESTSEAT", "FLEET_RECALL_FAKE": "1"}):
            recall_api.install_fake_backend()
            with mock.patch.object(mod.public_fallback, "call_with_fallback", fake_call_with_fallback):
                mod.call_tool(
                    "recall_contribute",
                    {"text": good, "category": "lesson", "force": False},
                )
        self.assertIn("force", snapshots["before_local"])
        self.assertNotIn("force", snapshots["after_local"])


class DuplicateGuardTests(unittest.TestCase):
    """recall_contribute answers {"status": "duplicate"} for a near-duplicate unless force=true."""

    def load(self):
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_guard_test", str(SERVER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        mod.FAKE_CORPUS = True  # these tests install the in-process fake corpus themselves
        return mod

    def setUp(self):
        self.saved = {k: getattr(recall_api, k) for k in
                      ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
                 "gitleaks_available")}
        recall_api.install_fake_backend()
        self.env = mock.patch.dict(os.environ, {"AGENT_SEAT": "TESTSEAT"})
        self.env.start()
        existing = ("pm2 start does not re-read env from the ecosystem file; restart with --update-env "
                    "so the cached PATH is replaced.")
        pt = build_point(existing, {"source": "agent-contribution", "app": "fleet", "category": "lesson",
                                    "seat": "GROK", "doc_id": "contrib/GROK/2026-09-01/abcd1234", "chunk_index": 0,
                                    "chunk_count": 1, "heading": "", "title": "pm2 env", "url": "", "path": "",
                                    "created_at": 1756684800000, "updated_at": 1756684800000, "ingest_run": "c"})
        recall_api.FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})
        score = {"value": 0.97}

        class Scored(recall_api.FakeQdrant):
            def search_dense(self, vector, limit=5, flt=None):
                hits = super().search_dense(vector, limit, flt)
                for h in hits:
                    h["score"] = score["value"]
                return hits

        self.score = score
        recall_api.Qdrant = Scored
        recall_api.reset_config_cache()

    def tearDown(self):
        self.env.stop()
        for k, v in self.saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()

    def call(self, mod, args: dict) -> dict:
        resp = mod.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "recall_contribute", "arguments": args}})
        return resp["result"]

    def test_duplicate_then_force(self):
        mod = self.load()
        text = "pm2 start ignores env changes from the ecosystem file; restart with --update-env."
        res = self.call(mod, {"text": text, "category": "lesson"})
        self.assertFalse(res["isError"])
        body = json.loads(res["content"][0]["text"])
        self.assertEqual(body["status"], "duplicate")
        self.assertEqual(body["existing"]["doc_id"], "contrib/GROK/2026-09-01/abcd1234")
        self.assertEqual(body["existing"]["seat"], "GROK")
        self.assertEqual(body["existing"]["score"], 0.97)
        self.assertEqual(body["threshold"], 0.92)
        self.assertIn("force=true", body["message"])
        self.assertEqual(recall_api.FakeQdrant.upserts, [])                # nothing stored

        res = self.call(mod, {"text": text, "category": "lesson", "force": True})
        body = json.loads(res["content"][0]["text"])
        self.assertTrue(body["doc_id"].startswith("contrib/TESTSEAT/"))
        self.assertEqual(body["status"], "completed")
        self.assertNotIn("nearest", body)
        self.assertEqual(len(recall_api.FakeQdrant.upserts), 1)

    def test_below_threshold_stores_and_reports_nearest(self):
        mod = self.load()
        self.score["value"] = 0.5
        res = self.call(mod, {"text": "A different lesson about Coolify deploy stalls and their cause.",
                              "category": "lesson", "force": False})
        body = json.loads(res["content"][0]["text"])
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["nearest"]["doc_id"], "contrib/GROK/2026-09-01/abcd1234")
        self.assertEqual(body["nearest"]["score"], 0.5)

    def test_force_must_be_boolean_and_short_text_skips_guard(self):
        mod = self.load()
        res = self.call(mod, {"text": "x" * 60, "category": "lesson", "force": "yes"})
        self.assertTrue(res["isError"])
        self.assertIn("force must be a boolean", res["content"][0]["text"])
        res = self.call(mod, {"text": "too short", "category": "lesson"})
        self.assertTrue(res["isError"])
        self.assertIn("too short", res["content"][0]["text"])
        self.assertEqual(recall_api.FakeQdrant.calls, [])                 # guard never searched


if __name__ == "__main__":
    unittest.main()
