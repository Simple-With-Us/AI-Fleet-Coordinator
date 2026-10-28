from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_sync.idem import IdemStore
from agent_sync.seat_control import (
    CAPABILITY_MATRIX,
    REQUEST_TTL_SECONDS,
    SEAT_CONTROL_TOOL_SCHEMAS,
    SeatControlMCP,
    TOMBSTONE_EXPIRES_AT,
)


def row(**changes: Any) -> dict[str, Any]:
    value = {
        "id": "opaque-record-1",
        "host": "mac-fixture",
        "platform": "grok-leader",
        "session_id": "native-session-1",
        "seat": "GROK",
        "cwd": "/fixture/workspace",
        "title": "display title only",
        "status": "idle",
        "stale": False,
        "capabilities": {"read": True, "wake": True, "wake_status": "available"},
    }
    value.update(changes)
    return value


class FakeCatalog:
    def __init__(self, rows=None, excerpts=None):
        self.rows = list(rows if rows is not None else [row()])
        self.excerpts = excerpts if excerpts is not None else [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "hi"},
        ]
        self.excerpt_calls = []
        self.error = None

    def discover(self, **kwargs):
        if self.error:
            raise self.error
        return list(self.rows)

    def excerpt(self, record_id, limit=4):
        self.excerpt_calls.append((record_id, limit))
        return self.excerpts[:limit]


class FakeTransport:
    def __init__(self, outcome="accepted", preflight="ready"):
        self.outcome = outcome
        self.preflight_outcome = preflight
        self.preflights = []
        self.sent = []

    def preflight(self, binding):
        self.preflights.append(dict(binding))
        return self.preflight_outcome

    def send(self, binding, text, *, permit=None):
        self.sent.append((dict(binding), text))
        if permit is not None and not permit():
            return "deferred"
        return self.outcome


class FakeQueue:
    def __init__(self, items=None, fail=False):
        self.items = list(items or [])
        self.fail = fail

    def snapshot(self):
        if self.fail:
            raise OSError("fixture unreadable")
        return {"items": self.items}


class FailSentStore:
    """Fail one terminal accepted write while preserving the pending row."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.fail_sent_once = True

    def begin(self, *args, **kwargs):
        return self.wrapped.begin(*args, **kwargs)

    def row(self, *args, **kwargs):
        return self.wrapped.row(*args, **kwargs)

    def set(self, key, **changes):
        if changes.get("state") == "sent" and self.fail_sent_once:
            self.fail_sent_once = False
            raise OSError("fixture terminal write failure")
        return self.wrapped.set(key, **changes)

    def drop(self, *args, **kwargs):
        return self.wrapped.drop(*args, **kwargs)


class FailUnknownStore:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def begin(self, *args, **kwargs):
        return self.wrapped.begin(*args, **kwargs)

    def row(self, *args, **kwargs):
        return self.wrapped.row(*args, **kwargs)

    def set(self, key, **changes):
        if changes.get("state") == "unknown":
            raise OSError("fixture unknown-state write failure")
        return self.wrapped.set(key, **changes)

    def drop(self, *args, **kwargs):
        return self.wrapped.drop(*args, **kwargs)


class DelayRowVisibilityStore:
    """Simulate a concurrent writer appearing between read and begin."""

    def __init__(self, wrapped, hidden_reads=2):
        self.wrapped = wrapped
        self.hidden_reads = hidden_reads

    def begin(self, *args, **kwargs):
        return self.wrapped.begin(*args, **kwargs)

    def row(self, key):
        if self.hidden_reads:
            self.hidden_reads -= 1
            return None
        return self.wrapped.row(key)

    def set(self, key, **changes):
        return self.wrapped.set(key, **changes)

    def drop(self, *args, **kwargs):
        return self.wrapped.drop(*args, **kwargs)


class MissingDuplicateStore:
    def begin(self, *args, **kwargs):
        return "duplicate", None

    def row(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        pass

    def drop(self, *args, **kwargs):
        pass


class SeatControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit_rows = []
        self.catalog = FakeCatalog()
        self.transport = FakeTransport()
        self.allowed = True
        self.approved = True
        self.authorizations = []
        self.approvals = []

        def authorize(caller, operation, target):
            self.authorizations.append((caller, operation, target))
            return self.allowed

        def approve(caller, operation, target, request_id):
            self.approvals.append((caller, operation, target, request_id))
            return self.approved

        self.service = SeatControlMCP(
            caller_id="CODEX",
            local_scope="mac",
            catalog=self.catalog,
            transport=self.transport,
            nudge_queue=None,
            idempotency=IdemStore(str(Path(self.temp.name) / "idem.json")),
            authorize=authorize,
            approve=approve,
            audit=self.audit_rows.append,
            feature_enabled=True,
            mutations_enabled=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def target(*pairs, **changes):
        target = {
            "host_scope": "mac",
            "host": "mac-fixture",
            "platform": "grok-leader",
            "session_id": "native-session-1",
            "owner": "GROK",
        }
        if len(pairs) % 2:
            raise ValueError("target overrides must be key/value pairs")
        target.update(zip(pairs[::2], pairs[1::2]))
        target.update(changes)
        return target

    def prompt(self, request_id="req-1", prompt="hello world", target=None):
        return self.service.call_tool(
            "seat_control_prompt",
            {"target": target or self.target(), "request_id": request_id, "prompt": prompt},
        )

    def test_typed_schema_has_exact_targets_and_no_shell_or_process_kill(self):
        names = {tool["name"] for tool in SEAT_CONTROL_TOOL_SCHEMAS}
        self.assertIn("seat_control_prompt", names)
        self.assertIn("seat_control_cancel_turn", names)
        prompt = next(tool for tool in SEAT_CONTROL_TOOL_SCHEMAS if tool["name"] == "seat_control_prompt")
        self.assertEqual(prompt["inputSchema"]["required"], ["target", "request_id", "prompt"])
        target_props = prompt["inputSchema"]["properties"]["target"]["properties"]
        self.assertEqual(set(target_props), {"host_scope", "host", "platform", "session_id", "owner"})
        serialized = str(SEAT_CONTROL_TOOL_SCHEMAS).lower()
        self.assertNotIn("shell", serialized)
        self.assertNotIn("kill_process", serialized)
        self.assertNotIn("cwd", target_props)

    def test_runtime_tool_list_is_read_only_by_default_and_hides_unsupported_tools(self):
        self.service.mutations_enabled = False
        default_names = {tool["name"] for tool in self.service.list_tools()}
        self.assertNotIn("seat_control_prompt", default_names)
        self.assertNotIn("seat_control_start", default_names)
        self.assertNotIn("seat_control_cancel_turn", default_names)
        self.service.mutations_enabled = True
        enabled_names = {tool["name"] for tool in self.service.list_tools()}
        self.assertIn("seat_control_prompt", enabled_names)
        self.assertNotIn("seat_control_start", enabled_names)
        self.assertNotIn("seat_control_resume", enabled_names)
        self.assertNotIn("seat_control_cancel_turn", enabled_names)

    def test_feature_is_completely_disabled_by_default(self):
        self.service.feature_enabled = False
        self.assertEqual(self.service.list_tools(), [])
        result = self.prompt()
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(self.transport.sent, [])

    def test_capability_matrix_distinguishes_server_runtime_from_unsupported(self):
        server = CAPABILITY_MATRIX[("server", "grok-leader")]
        self.assertEqual(server["list"], "runtime_unverified")
        self.assertEqual(server["prompt"], "runtime_unverified")
        self.assertEqual(server["create"], "unsupported")
        self.assertEqual(server["cancel_turn"], "unsupported")
        self.assertEqual(CAPABILITY_MATRIX[("mac", "cursor-local")]["prompt"], "unsupported")

    def test_list_uses_exact_host_scope_and_titles_are_display_only(self):
        result = self.service.call_tool(
            "seat_control_list",
            {"host_scope": "mac", "host": "mac-fixture", "limit": 1},
        )
        self.assertEqual(result["status"], "ok")
        session = result["data"]["sessions"][0]
        self.assertEqual(session["target"]["session_id"], "native-session-1")
        self.assertEqual(session["title"], "display title only")
        self.assertNotIn("workspace", session)

    def test_list_requires_one_explicit_host_and_does_not_cross_hosts(self):
        missing_host = self.service.call_tool(
            "seat_control_list", {"host_scope": "mac"}
        )
        self.assertEqual(missing_host["status"], "invalid_request")
        self.catalog.rows.append(row(host="other-mac"))
        result = self.service.call_tool(
            "seat_control_list", {"host_scope": "mac", "host": "mac-fixture"}
        )
        self.assertEqual(len(result["data"]["sessions"]), 1)
        self.assertEqual(result["data"]["sessions"][0]["target"]["host"], "mac-fixture")

    def test_list_applies_result_limit_after_bounded_catalog_discovery(self):
        self.catalog.rows = [row(session_id=f"session-{index}") for index in range(6)]
        result = self.service.call_tool(
            "seat_control_list",
            {"host_scope": "mac", "host": "mac-fixture", "limit": 2},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["data"]["sessions"]), 2)
        # The production catalog itself owns bounded scans/caching. The
        # adapter independently limits each returned response.
        self.assertLessEqual(result["data"]["limit"], 24)

    def test_read_requires_host_platform_and_native_session_identity(self):
        wrong = self.service.call_tool(
            "seat_control_read",
            {"target": self.target("host", "server-fixture")},
        )
        self.assertEqual(wrong["status"], "wrong_host")
        self.assertEqual(self.catalog.excerpt_calls, [])

    def test_ambiguous_exact_identity_is_refused(self):
        self.catalog.rows.append(row(id="opaque-record-2"))
        result = self.service.call_tool("seat_control_status", {"target": self.target()})
        self.assertEqual(result["status"], "ambiguous")

    def test_same_native_id_on_another_host_does_not_cross_route(self):
        self.catalog.rows = [row(host="server-fixture")]
        result = self.service.call_tool("seat_control_status", {"target": self.target()})
        self.assertEqual(result["status"], "wrong_host")

    def test_wrong_owner_does_not_cross_route(self):
        result = self.service.call_tool(
            "seat_control_status", {"target": self.target("owner", "CODEX")}
        )
        self.assertEqual(result["status"], "wrong_owner")

    def test_stale_target_is_refused_and_never_replaced(self):
        self.catalog.rows = [row(status="stale", stale=True)]
        result = self.prompt()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.transport.sent, [])

    def test_unavailable_host_catalog_fails_closed(self):
        self.catalog.error = OSError("fixture host unavailable")
        result = self.service.call_tool("seat_control_status", {"target": self.target()})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.transport.sent, [])

    def test_unknown_session_does_not_fall_back_to_title_or_create(self):
        result = self.prompt(target=self.target("session_id", "missing-session"))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.transport.sent, [])

    def test_unsupported_platform_does_not_infer_control_from_discovery(self):
        self.catalog.rows = [row(platform="new-platform")]
        result = self.service.call_tool(
            "seat_control_read",
            {"target": self.target("platform", "new-platform")},
        )
        self.assertEqual(result["status"], "unsupported")

    def test_reads_are_caller_scoped(self):
        self.allowed = False
        result = self.service.call_tool("seat_control_read", {"target": self.target()})
        self.assertEqual(result["status"], "permission_refused")
        self.assertEqual(self.authorizations[-1][0], "CODEX")

    def test_mutations_are_disabled_by_default(self):
        self.service.mutations_enabled = False
        result = self.prompt()
        self.assertEqual(result["status"], "permission_refused")
        self.assertEqual(self.transport.sent, [])

    def test_per_request_approval_refusal_prevents_prompt(self):
        self.approved = False
        result = self.prompt()
        self.assertEqual(result["status"], "permission_refused")
        self.assertEqual(self.transport.sent, [])

    def test_accepted_duplicate_is_not_resent_and_not_called_completed(self):
        first = self.prompt()
        second = self.prompt()
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["data"]["picked_up"], False)
        self.assertEqual(first["data"]["completed"], False)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.transport.sent), 1)

    def test_accepted_duplicate_returns_receipt_if_session_disappears(self):
        first = self.prompt()
        self.catalog.rows.clear()
        second = self.prompt()
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.transport.sent), 1)

    def test_terminal_store_failure_returns_unknown_and_never_resends(self):
        store = FailSentStore(self.service.idempotency)
        self.service.idempotency = store
        first = self.prompt()
        second = self.prompt()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(len(self.transport.sent), 1)

    def test_terminal_audit_and_unknown_write_failures_still_return_unknown(self):
        self.service.idempotency = FailUnknownStore(self.service.idempotency)

        def audit(record):
            if record["outcome"] == "accepted":
                raise OSError("fixture terminal audit failure")
            self.audit_rows.append(record)

        self.service.audit = audit
        first = self.prompt()
        second = self.prompt()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.transport.sent), 1)

    def test_request_id_reuse_with_different_prompt_is_conflict(self):
        self.assertEqual(self.prompt(prompt="one")["status"], "accepted")
        result = self.prompt(prompt="two")
        self.assertEqual(result["status"], "request_conflict")
        self.assertEqual(len(self.transport.sent), 1)

    def test_held_request_is_not_replayed_when_target_becomes_ready(self):
        self.catalog.rows = [row(status="busy")]
        first = self.prompt()
        self.assertEqual(first["status"], "held")
        self.catalog.rows = [row(status="idle")]
        second = self.prompt()
        self.assertEqual(second["status"], "held")
        self.assertEqual(len(self.transport.sent), 0)

    def test_failed_request_receipt_is_not_replayed(self):
        self.transport.outcome = "rejected"
        first = self.prompt()
        self.assertEqual(first["status"], "failed")
        self.transport.outcome = "accepted"
        second = self.prompt()
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["data"]["status"], "failed")
        self.assertEqual(len(self.transport.sent), 1)

    def test_held_or_failed_request_id_reuse_with_changed_content_conflicts(self):
        self.catalog.rows = [row(status="busy")]
        self.assertEqual(self.prompt()["status"], "held")
        self.assertEqual(self.prompt(prompt="changed")["status"], "request_conflict")
        self.catalog.rows = [row(status="idle")]
        self.transport.outcome = "rejected"
        self.assertEqual(self.prompt(request_id="failed-id")["status"], "failed")
        self.assertEqual(
            self.prompt(request_id="failed-id", prompt="changed")["status"],
            "request_conflict",
        )
        self.assertEqual(len(self.transport.sent), 1)

    def test_terminal_receipt_survives_missing_target(self):
        self.catalog.rows = [row(status="busy")]
        first = self.prompt()
        self.catalog.rows.clear()
        second = self.prompt()
        self.assertEqual(first["status"], "held")
        self.assertEqual(second["status"], "held")
        self.assertEqual(len(self.transport.sent), 0)

    def test_hidden_receipt_race_cannot_reclaim_terminal_held_request(self):
        self.catalog.rows = [row(status="busy")]
        self.assertEqual(self.prompt()["status"], "held")
        self.catalog.rows = [row(status="idle")]
        self.service.idempotency = DelayRowVisibilityStore(
            self.service.idempotency, hidden_reads=2
        )
        raced = self.prompt()
        self.assertIn(raced["status"], {"held", "pending"})
        self.assertEqual(len(self.transport.sent), 0)

    def test_concurrent_request_id_collision_is_conflict(self):
        key = hashlib.sha256(b"CODEX\0req-1").hexdigest()
        self.service.idempotency.begin(
            key,
            600,
            {"fingerprint": "different-request", "result": {"status": "accepted"}},
        )
        self.service.idempotency.set(key, state="sent")
        self.service.idempotency = DelayRowVisibilityStore(self.service.idempotency)
        result = self.prompt()
        self.assertEqual(result["status"], "request_conflict")
        self.assertEqual(self.transport.sent, [])

    def test_duplicate_idempotency_state_without_receipt_fails_closed(self):
        self.service.idempotency = MissingDuplicateStore()
        result = self.prompt()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.transport.sent, [])

    def test_held_path_duplicate_without_receipt_fails_closed(self):
        self.catalog.rows = [row(status="unknown")]
        self.service.idempotency = MissingDuplicateStore()
        result = self.prompt()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.transport.sent, [])

    def test_uncertain_prompt_is_never_replayed(self):
        self.transport.outcome = "unknown"
        first = self.prompt()
        second = self.prompt()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(len(self.transport.sent), 1)

    def test_unknown_duplicate_stays_unknown_if_session_disappears(self):
        self.transport.outcome = "unknown"
        first = self.prompt()
        self.catalog.rows.clear()
        second = self.prompt()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(len(self.transport.sent), 1)

    def test_busy_target_is_held_without_dispatch(self):
        self.transport.preflight_outcome = "busy"
        result = self.prompt()
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.transport.sent, [])

    def test_active_or_permission_waiting_target_is_held_not_downgraded_to_unsupported(self):
        self.catalog.rows = [row(status="active", capabilities={"read": True, "wake": False})]
        result = self.prompt()
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.transport.sent, [])

    def test_unknown_busy_and_permission_waiting_catalog_targets_are_held(self):
        for request_id, changes in (
            ("unknown-state", {"status": "unknown"}),
            ("busy-state", {"status": "busy"}),
            ("active-state", {"status": "active"}),
            ("permission-state", {"pending_permission": True}),
            ("permission-status", {"status": "pending_permission"}),
        ):
            with self.subTest(status=request_id):
                self.catalog.rows = [row(**changes)]
                result = self.prompt(request_id=request_id)
                self.assertEqual(result["status"], "held")
        self.assertEqual(self.transport.sent, [])

    def test_prompt_requires_affirmative_catalog_wake_capability(self):
        cases = (
            ("missing wake", {"capabilities": {"read": True}}),
            ("false wake", {"capabilities": {"read": True, "wake": False}}),
            ("truthy nonboolean", {"capabilities": {"read": True, "wake": "true"}}),
            ("malformed capabilities", {"capabilities": ["read", "wake"]}),
            ("missing capabilities", {"capabilities": None}),
        )
        for request_id, changes in cases:
            with self.subTest(case=request_id):
                self.catalog.rows = [row(**changes)]
                result = self.prompt(request_id=request_id)
                self.assertEqual(result["status"], "unsupported")
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.transport.preflights, [])

    def test_runtime_unverified_server_prompt_never_reaches_transport(self):
        self.catalog.rows = [row(host="server-fixture")]
        self.service.local_scope = "server"
        self.service.authorize = lambda *_args: True
        result = self.prompt(target=self.target("host_scope", "server", "host", "server-fixture"))
        self.assertEqual(result["status"], "runtime_unverified")
        self.assertEqual(self.transport.sent, [])

    def test_preflight_unsupported_does_not_report_busy_or_dispatch(self):
        self.transport.preflight_outcome = "unsupported"
        result = self.prompt()
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(self.transport.sent, [])

    def test_unavailable_audit_sink_prevents_prompt_dispatch(self):
        def fail_audit(_record):
            raise OSError("fixture audit failure")

        self.service.audit = fail_audit
        result = self.prompt()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.transport.sent, [])

    def test_existing_nudge_queue_holds_exact_target_and_unreadable_queue_fails_closed(self):
        item = {"status": "unknown", "binding": self.target()}
        self.service.nudge_queue = FakeQueue([item])
        result = self.prompt()
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.transport.sent, [])
        self.service.nudge_queue = FakeQueue(fail=True)
        result = self.prompt(request_id="req-2")
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.transport.sent, [])

    def test_create_resume_and_cancel_turn_are_explicitly_unsupported(self):
        start = self.service.call_tool(
            "seat_control_start",
            {"host_scope": "mac", "host": "mac-fixture", "platform": "grok-leader", "request_id": "create-1"},
        )
        resume = self.service.call_tool(
            "seat_control_resume",
            {"target": self.target(), "request_id": "resume-1"},
        )
        cancel = self.service.call_tool(
            "seat_control_cancel_turn",
            {"target": self.target(), "request_id": "cancel-1"},
        )
        self.assertEqual(start["status"], "unsupported")
        self.assertEqual(resume["status"], "unsupported")
        self.assertEqual(cancel["status"], "unsupported")
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.transport.preflights, [])

    def test_output_is_bounded_to_user_and_assistant_text(self):
        self.catalog.excerpts = [
            {"role": "tool", "text": "must not surface"},
            {"role": "assistant", "text": "a" * 7000},
            {"role": "user", "text": "last"},
        ]
        result = self.service.call_tool(
            "seat_control_output", {"target": self.target(), "limit": 8}
        )
        self.assertEqual(result["status"], "ok")
        messages = result["data"]["messages"]
        self.assertEqual([m["role"] for m in messages], ["assistant", "user"])
        self.assertEqual(len(messages[0]["text"]), 2400)
        self.assertLessEqual(sum(len(m["text"]) for m in messages), 6000)

    def test_audit_omits_ids_hashes_platform_and_prompt_content(self):
        secret_prompt = "private prompt content"
        self.prompt(prompt=secret_prompt)
        audit_text = repr(self.audit_rows)
        identifiers = (
            "CODEX",
            "mac-fixture",
            "grok-leader",
            "native-session-1",
            "req-1",
            secret_prompt,
        )
        for identifier in identifiers:
            self.assertNotIn(identifier, audit_text)
            self.assertNotIn(hashlib.sha256(identifier.encode()).hexdigest(), audit_text)
        self.assertNotIn("actor_fingerprint", audit_text)
        self.assertNotIn("host_fingerprint", audit_text)
        self.assertNotIn("session_fingerprint", audit_text)
        self.assertNotIn("request_fingerprint", audit_text)
        self.assertNotIn("prompt_fingerprint", audit_text)
        self.assertTrue(
            any(record.get("delegation") == "approved" for record in self.audit_rows),
            "mutation delegation context must be audited without raw identifiers",
        )
        key = hashlib.sha256(b"CODEX\0req-1").hexdigest()
        row = self.service.idempotency.row(key)
        self.assertEqual(row["expires"], TOMBSTONE_EXPIRES_AT)

    def test_definite_auth_failure_does_not_pin_idempotency(self):
        self.approved = False
        result = self.prompt()
        self.assertEqual(result["status"], "permission_refused")
        key = hashlib.sha256(b"CODEX\0req-1").hexdigest()
        self.assertIsNone(self.service.idempotency.row(key))

    def test_prompt_secret_shape_is_refused_before_persisting(self):
        token = "sk-" + "ant-" + ("x" * 40)
        result = self.prompt(prompt=f"please use {token}")
        self.assertEqual(result["status"], "invalid_request")
        key = hashlib.sha256(b"CODEX\0req-1").hexdigest()
        self.assertIsNone(self.service.idempotency.row(key))

    def test_target_extra_identity_fields_are_rejected(self):
        target = self.target()
        target["display_title"] = "must not be accepted"
        result = self.service.call_tool(
            "seat_control_read", {"target": target}
        )
        self.assertEqual(result["status"], "invalid_request")

    def test_output_refuses_empty_catalog_record_id(self):
        self.catalog.rows = [row(id="")]
        result = self.service.call_tool(
            "seat_control_output", {"target": self.target(), "limit": 2}
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.catalog.excerpt_calls, [])

    def test_held_receipt_expires_within_request_ttl(self):
        import time

        self.catalog.rows = [row(status="busy")]
        self.prompt()
        key = hashlib.sha256(b"CODEX\0req-1").hexdigest()
        stored = self.service.idempotency.row(key)
        self.assertIsNotNone(stored)
        now = time.time()
        self.assertGreaterEqual(stored["expires"], now)
        self.assertLessEqual(stored["expires"], now + REQUEST_TTL_SECONDS + 5)
        self.assertNotEqual(stored["expires"], TOMBSTONE_EXPIRES_AT)

    def test_list_and_resolve_agree_on_agent_id_backed_sessions(self):
        self.catalog.rows = [
            row(session_id="", agent_id="native-session-1"),
        ]
        listed = self.service.call_tool(
            "seat_control_list",
            {"host_scope": "mac", "host": "mac-fixture", "limit": 4},
        )
        self.assertEqual(len(listed["data"]["sessions"]), 1)
        read = self.service.call_tool("seat_control_read", {"target": self.target()})
        self.assertEqual(read["status"], "ok")


if __name__ == "__main__":
    unittest.main()
