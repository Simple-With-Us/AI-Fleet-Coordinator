"""Cursor Cloud existing-chat transport tests.  Uses only mocked API responses."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_sync.cursor_chat_transport import CursorCloudTransport  # noqa: E402


class FakeCursorApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.timeouts: list[int] = []
        self.list_pages: dict[str | None, dict] = {}
        self.agents: dict[str, dict] = {}
        self.conversations: dict[str, dict] = {}
        self.run_response: dict = {"run": {"id": "run-accepted"}}
        self.run_error: RuntimeError | None = None

    def load_api_key(self) -> str:
        return "test-key"

    def api_request(self, method: str, path: str, api_key: str, body=None, timeout=20):
        self.calls.append((method, path, body))
        self.timeouts.append(timeout)
        if method == "GET" and path.startswith("/v1/agents?"):
            query = parse_qs(urlsplit(path).query)
            self.assert_limit = query.get("limit") == ["100"]
            cursor = query.get("cursor", [None])[0]
            if cursor not in self.list_pages:
                raise RuntimeError("Cursor API unreachable: test page unavailable")
            return self.list_pages[cursor]
        if method == "GET" and path.endswith("/conversation"):
            agent_id = path.split("/")[-2]
            return self.conversations.get(agent_id, {"messages": []})
        if method == "GET" and path.startswith("/v1/agents/"):
            agent_id = path.rsplit("/", 1)[-1]
            if agent_id not in self.agents:
                raise RuntimeError(f"Cursor API 404 on GET {path}: not_found")
            return self.agents[agent_id]
        if method == "POST" and path.endswith("/runs"):
            if self.run_error:
                raise self.run_error
            return self.run_response
        raise AssertionError(f"unexpected API request: {method} {path}")


def agent(agent_id: str, status: str = "IDLE", repo: str | None = "https://github.com/org/repo") -> dict:
    return {
        "id": agent_id,
        "status": status,
        "name": "Existing task",
        "url": f"https://cursor.com/agents/{agent_id}",
        "repos": [{"url": repo}] if repo else [],
    }


def binding(agent_id: str = "bc-one", cwd: str | None = None) -> dict[str, str]:
    cwd = cwd or f"cursor-cloud://{agent_id}"
    return {"platform": "cursor-cloud", "host": "cursor-cloud", "session_id": agent_id,
            "cwd": cwd, "seat": "CURSOR"}


class DiscoverTests(unittest.TestCase):
    def test_paginates_at_100_and_returns_existing_idle_and_active_records(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {
            None: {"items": [{"id": "bc-one", "status": "IDLE"}, {"id": "bc-archived", "status": "ARCHIVED"}],
                  "nextCursor": "next-1"},
            "next-1": {"items": [{"id": "bc-two", "status": "ACTIVE"}]},
        }
        rows = CursorCloudTransport(api).discover()

        self.assertEqual([row["session_id"] for row in rows], ["bc-one", "bc-two"])
        self.assertEqual([row["state"] for row in rows], ["idle", "busy"])
        self.assertTrue(all(row["seat"] == "CURSOR" for row in rows))
        self.assertEqual(rows[0]["cwd"], "cursor-cloud://bc-one")
        self.assertTrue(api.assert_limit)
        self.assertTrue(all(timeout <= 5 for timeout in api.timeouts))
        self.assertFalse(any(path.startswith("/v1/agents/bc-") for _, path, _ in api.calls))

    def test_stops_if_cursor_repeats(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {None: {"items": [], "nextCursor": "same"}, "same": {"items": [], "nextCursor": "same"}}
        self.assertEqual(CursorCloudTransport(api).discover(), [])
        self.assertEqual(sum(path.startswith("/v1/agents?") for _, path, _ in api.calls), 2)

    def test_discovery_caps_results_and_page_fetches(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {
            None: {"items": [{"id": f"bc-{i}", "status": "IDLE"} for i in range(100)],
                  "nextCursor": "next"},
            "next": {"items": [{"id": f"bc-{i}", "status": "IDLE"} for i in range(100, 200)],
                     "nextCursor": "third"},
            "third": {"items": [{"id": "bc-extra", "status": "IDLE"}]},
        }
        transport = CursorCloudTransport(api)
        rows = transport.discover()
        self.assertEqual(len(rows), 200)
        self.assertEqual(rows[-1]["session_id"], "bc-199")
        self.assertEqual(sum(path.startswith("/v1/agents?") for _, path, _ in api.calls), 2)
        self.assertFalse(any(path.startswith("/v1/agents/bc-") for _, path, _ in api.calls))
        self.assertEqual(transport.last_discovery_status, "partial")

    def test_list_metadata_avoids_details_and_projects_updated_at(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {None: {"items": [{
            "id": "bc-one", "name": "Listed task", "status": "IDLE", "updatedAt": "2026-10-13T12:00:00Z",
            "repos": [{"url": "https://github.com/org/repo"}],
        }]}}
        rows = CursorCloudTransport(api).discover()
        self.assertEqual(rows[0]["title"], "Listed task")
        self.assertEqual(rows[0]["updated_at"], "2026-10-13T12:00:00Z")
        self.assertEqual(rows[0]["repo_url"], "https://github.com/org/repo")
        self.assertFalse(any(path.startswith("/v1/agents/bc-") for _, path, _ in api.calls))

    def test_failure_on_later_page_keeps_partial_results(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {None: {"items": [{"id": "bc-one", "status": "IDLE"}], "nextCursor": "missing"}}
        transport = CursorCloudTransport(api)
        rows = transport.discover()
        self.assertEqual([row["session_id"] for row in rows], ["bc-one"])
        self.assertEqual(transport.last_discovery_status, "partial")

    def test_agent_without_repo_uses_agent_specific_binding_sentinel(self) -> None:
        api = FakeCursorApi()
        api.list_pages = {None: {"items": [{"id": "bc-one", "status": "IDLE"}]}}
        self.assertEqual(CursorCloudTransport(api).discover()[0]["cwd"], "cursor-cloud://bc-one")

    def test_find_fetches_one_agent_by_id_without_listing(self) -> None:
        api = FakeCursorApi()
        api.agents = {"bc-old": agent("bc-old"), "bc-archived": agent("bc-archived", "ARCHIVED")}
        transport = CursorCloudTransport(api)
        row = transport.find("bc-old")
        self.assertIsNotNone(row)
        self.assertEqual(row["session_id"], "bc-old")
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["title"], "Existing task")
        self.assertEqual(row["cwd"], "cursor-cloud://bc-old")
        self.assertEqual(row["repo_url"], "https://github.com/org/repo")
        self.assertEqual(api.calls, [("GET", "/v1/agents/bc-old", None)])
        self.assertIsNone(transport.find("bc-archived"))
        self.assertIsNone(transport.find("bc-gone"))
        self.assertIsNone(transport.find("not-an-agent"))
        self.assertFalse(any(path.startswith("/v1/agents?") for _, path, _ in api.calls))


class PreflightTests(unittest.TestCase):
    def test_idle_agent_is_ready_and_active_is_busy(self) -> None:
        api = FakeCursorApi()
        api.agents = {"bc-one": agent("bc-one"), "bc-two": agent("bc-two", "ACTIVE")}
        transport = CursorCloudTransport(api)
        self.assertEqual(transport.preflight(binding("bc-one")), "ready")
        self.assertEqual(transport.preflight(binding("bc-two")), "busy")

    def test_archived_wrong_repo_and_unknown_id_are_unsupported(self) -> None:
        api = FakeCursorApi()
        api.agents = {"bc-one": agent("bc-one", "ARCHIVED")}
        transport = CursorCloudTransport(api)
        self.assertEqual(transport.preflight(binding("bc-one")), "unsupported")
        api.agents["bc-one"] = agent("bc-one")
        self.assertEqual(transport.preflight(binding("bc-one", "cursor-cloud://bc-other")), "unsupported")
        self.assertEqual(transport.preflight(binding("bc/no")), "unsupported")
        self.assertEqual(transport.preflight(binding("bc-gone")), "unsupported")
        self.assertEqual(transport.preflight({"platform": "other", "host": "cursor-cloud", "session_id": "bc-one"}),
                         "unsupported")

    def test_preflight_requires_agent_specific_sentinel_and_matching_detail_id(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        transport = CursorCloudTransport(api)
        self.assertEqual(transport.preflight(binding("bc-one", "cursor-cloud://bc-other")), "unsupported")
        self.assertFalse(api.calls)
        api.agents["bc-one"] = agent("bc-other")
        self.assertEqual(transport.preflight(binding("bc-one")), "unsupported")

    def test_unavailable_status_fails_closed_as_busy(self) -> None:
        api = FakeCursorApi()
        api.api_request = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down"))
        self.assertEqual(CursorCloudTransport(api).preflight(binding()), "busy")


class ExcerptAndReceiptTests(unittest.TestCase):
    def test_excerpt_is_limited_and_truncates_each_message(self) -> None:
        api = FakeCursorApi()
        api.conversations["bc-one"] = {"messages": [
            {"role": "user", "text": "old"},
            {"role": "assistant", "content": "x" * 2000},
            {"role": "user", "content": [{"type": "text", "text": "new"}]},
        ]}
        excerpt = CursorCloudTransport(api).excerpt("bc-one", limit=2)
        self.assertEqual([row["role"] for row in excerpt], ["assistant", "user"])
        self.assertEqual(len(excerpt[0]["text"]), 1200)
        self.assertEqual(excerpt[-1]["text"], "new")
        self.assertEqual(CursorCloudTransport(api).excerpt("bc-one", limit=0), [])

    def test_excerpt_ignores_untyped_content_blocks_and_non_chat_roles(self) -> None:
        api = FakeCursorApi()
        api.conversations["bc-one"] = {"messages": [
            {"role": "assistant", "content": [{"text": "tool payload"}]},
            {"role": "tool", "text": "tool output"},
            {"role": "assistant", "content": [{"type": "text", "text": "safe"}]},
        ]}
        excerpt = CursorCloudTransport(api).excerpt("bc-one")
        self.assertEqual([row["text"] for row in excerpt], ["", "", "safe"])

    def test_receipt_requires_one_exact_user_message(self) -> None:
        api = FakeCursorApi()
        api.conversations["bc-one"] = {"messages": [
            {"role": "assistant", "text": "wake text"},
            {"role": "user", "text": "wake text"},
        ]}
        transport = CursorCloudTransport(api)
        self.assertTrue(transport.receipt(binding(), "wake text"))
        api.conversations["bc-one"]["messages"].append({"role": "user", "text": "wake text"})
        self.assertFalse(transport.receipt(binding(), "wake text"))
        self.assertFalse(transport.receipt(binding(), "different"))


class SendTests(unittest.TestCase):
    def test_posts_only_a_follow_up_to_the_existing_agent_and_run_id_confirms_acceptance(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        transport = CursorCloudTransport(api)
        order: list[str] = []
        original = api.api_request

        def recording(method, path, key, body=None, timeout=20):
            order.append(method)
            return original(method, path, key, body, timeout)

        api.api_request = recording
        self.assertEqual(transport.send(binding(), "nudge", permit=lambda: order.append("permit") or True),
                         "accepted")
        post = [call for call in api.calls if call[0] == "POST"]
        self.assertEqual(post, [("POST", "/v1/agents/bc-one/runs", {"prompt": {"text": "nudge"}})])
        self.assertEqual(order, ["GET", "permit", "POST"])

    def test_permit_denial_prevents_post(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        self.assertEqual(CursorCloudTransport(api).send(binding(), "nudge", permit=lambda: False), "deferred")
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_busy_is_deferred_and_archived_is_failed_without_post(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one", "ACTIVE")
        transport = CursorCloudTransport(api)
        self.assertEqual(transport.send(binding(), "nudge"), "deferred")
        api.agents["bc-one"] = agent("bc-one", "ARCHIVED")
        self.assertEqual(transport.send(binding(), "nudge"), "failed")
        self.assertFalse(any(method == "POST" for method, _, _ in api.calls))

    def test_409_agent_busy_is_deferred(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        api.run_error = RuntimeError("Cursor API 409 on POST /v1/agents/bc-one/runs: agent_busy")
        self.assertEqual(CursorCloudTransport(api).send(binding(), "nudge"), "deferred")
        self.assertEqual(sum(method == "POST" for method, _, _ in api.calls), 1)

    def test_uncertain_post_is_never_retried_and_requires_exact_receipt(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        api.run_error = RuntimeError("Cursor API unreachable: timed out")
        transport = CursorCloudTransport(api)
        transport.receipt = lambda _binding, _text: False
        self.assertEqual(transport.send(binding(), "nudge"), "unknown")
        self.assertEqual(sum(method == "POST" for method, _, _ in api.calls), 1)

    def test_uncertain_post_with_unique_exact_user_text_has_strong_receipt(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        api.run_error = RuntimeError("Cursor API unreachable: timed out")
        api.conversations["bc-one"] = {"messages": [{"role": "user", "text": "nudge"}]}
        self.assertEqual(CursorCloudTransport(api).send(binding(), "nudge"), "accepted")
        self.assertEqual(sum(method == "POST" for method, _, _ in api.calls), 1)

    def test_missing_run_id_is_unknown_and_latest_run_id_is_not_used(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = {**agent("bc-one"), "latestRunId": "unrelated-old-run"}
        api.run_response = {"agent": {"latestRunId": "unrelated-old-run"}}
        transport = CursorCloudTransport(api)
        transport.receipt = lambda _binding, _text: False
        self.assertEqual(transport.send(binding(), "nudge"), "unknown")

    def test_run_id_for_different_agent_is_unknown(self) -> None:
        api = FakeCursorApi()
        api.agents["bc-one"] = agent("bc-one")
        api.run_response = {"run": {"id": "run-accepted", "agentId": "bc-other"}}
        self.assertEqual(CursorCloudTransport(api).send(binding(), "nudge"), "unknown")


if __name__ == "__main__":
    unittest.main()
