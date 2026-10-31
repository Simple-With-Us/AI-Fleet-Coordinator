#!/usr/bin/env python3
"""Tests for the fleet git tools CLI (scripts/git-tools/git), v2.

v2 contract under test:  tools/list runs at run time on this caller's own transport (bounded pages,
cursor-loop rejection, per-tool schema validation) and is the only tool authority;  read/write is the pinned
reviewed read set and an unknown action is a WRITE;  the actor is resolved for reads AND writes (declared,
explicit and server-reported seats must agree, mismatch refused before the target call);  the native ECHO
lane refuses any other seat before ANY RPC;  JSON is strict in both directions;  argument reads are bounded;
tools/list output is untrusted display data, never authority.

The transport is faked:  no network, no credentials, no custody.  The shared fleet secret scanner is not in
this repository, so a fake scanner stands in for argument-scanning tests and one test pins the fail-closed
behavior when the real scanner file is absent.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

IMPL = Path(__file__).resolve().parent / "git"
loader = importlib.machinery.SourceFileLoader("git_cli", str(IMPL))
spec = importlib.util.spec_from_loader("git_cli", loader)
git = importlib.util.module_from_spec(spec)
loader.exec_module(git)

BEARER = "b" * 40
ENV_KEYS = (
    "SIMPLE_TOOLS_BEARER",
    "SIMPLE_GIT_REMOTE",
    "SIMPLE_GIT_NATIVE_ECHO",
    "SIMPLE_GIT_ARGV0",
    "AGENT_SEAT",
    "AGENT_TAG",
    "AGENT_LAUNCHER",
    "AGENT_LAUNCH_SEAT",
)

READ_SCHEMA = {
    "type": "object",
    "required": ["owner"],
    "properties": {"owner": {"type": "string"}, "identity": {"type": "string"}},
}
WRITE_SCHEMA = {
    "type": "object",
    "required": ["title", "identity"],
    "properties": {"title": {"type": "string"}, "identity": {"type": "string"}},
}
PUT_SCHEMA = {
    "type": "object",
    "required": ["path", "content", "identity"],
    "properties": {
        "path": {"type": "string"},
        "content": "x",  # replaced below
    },
}
PUT_SCHEMA["properties"]["content"] = {"type": "string"}


def make_tool(name, schema=None, description="", title=""):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object", "properties": {}},
    }


DEFAULT_PAGE = [
    make_tool("gr_wf_repo_get", READ_SCHEMA, "get a repository"),
    make_tool("gr_wf_issue_create", WRITE_SCHEMA, "create an issue"),
    make_tool("gr_new_unknown", None, "not in the reviewed read set"),
    make_tool("notes_search", None, "a different family on the same endpoint"),
]


class FakeScanner:
    """Stands in for scripts/agent_sync/secretscan.py:  flags any string containing a known token."""

    def scan(self, value, known=()):
        for token in known:
            if token and token in value:
                return {"kind": "known_token"}
        return None


class FakeTransport:
    """Answers tools/list from `pages` and tools/call from `call_results`; records every tools/call.

    pages: list of either [tool, ...] (cursor chain generated) or ([tool, ...], nextCursor-or-None)."""

    def __init__(
        self,
        pages=None,
        seat="CODEX",
        call_results=None,
        status=200,
        auth_seat=None,
        default_seat=None,
        advertised=None,
        sc_override=None,
    ):
        self.pages = [DEFAULT_PAGE] if pages is None else pages
        self.seat = seat
        self.auth_seat = seat if auth_seat is None else auth_seat
        self.default_seat = seat if default_seat is None else default_seat
        self.advertised = advertised  # None -> [{"identity": <the usable seat>}]
        self.sc_override = sc_override
        self.call_results = call_results or {}
        self.status = status
        self.calls = []  # (name, arguments) for tools/call only
        self.list_requests = 0

    def _page(self, index):
        page = self.pages[index]
        if isinstance(page, tuple):
            return page
        nxt = "cursor-%d" % (index + 1) if index + 1 < len(self.pages) else None
        return page, nxt

    def post(self, url, headers, body, timeout):
        if url != git.REMOTE_MCP_URL:
            raise AssertionError("the endpoint is a fixed constant: " + url)
        if headers.get("Authorization") != "Bearer " + BEARER:
            raise AssertionError("missing or wrong Authorization header")
        req = json.loads(body.decode())
        if req["method"] == "tools/list":
            self.list_requests += 1
            cursor = req["params"].get("cursor")
            if cursor is None:
                index = 0
            elif cursor.startswith("cursor-"):
                index = int(cursor.split("-", 1)[1])
            else:
                index = -1
            try:
                tools, nxt = self._page(index)
            except IndexError:
                tools, nxt = [], None
            result = {"tools": tools}
            if nxt is not None:
                result["nextCursor"] = nxt
        elif req["method"] == "tools/call":
            name = req["params"]["name"]
            arguments = req["params"].get("arguments") or {}
            self.calls.append((name, arguments))
            if name == "identities":
                advertised = self.advertised
                if advertised is None:
                    usable = (
                        self.auth_seat
                        if git.SEAT_PATTERN.fullmatch(self.auth_seat or "")
                        else self.default_seat
                    )
                    advertised = [{"identity": usable}]
                structured = self.sc_override or {
                    "authenticated_seat": self.auth_seat,
                    "default_identity": self.default_seat,
                    "identities": advertised,
                }
                result = {
                    "structuredContent": structured,
                    "content": [{"type": "text", "text": self.auth_seat}],
                    "isError": False,
                }
            elif name in self.call_results:
                result = self.call_results[name]
            else:
                result = {
                    "structuredContent": {"ok": True},
                    "content": [{"type": "text", "text": "done"}],
                    "isError": False,
                }
        else:
            raise AssertionError("unexpected method " + req["method"])
        envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        return self.status, "application/json", envelope


def run_cli(argv, *, env=None, transport=None, stdin_text="", native_post=None, scanner="fake"):
    """Run main() fully wired:  no real network, clean seat/bearer environment, captured output."""
    clean_env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    clean_env.update(env or {})
    remote_post = transport.post if transport is not None else _no_network
    native = native_post if native_post is not None else _no_network_native
    if scanner == "fake":
        scanner_obj = FakeScanner()
    else:
        scanner_obj = (
            None  # None: the real load path (fail-closed test, or the real scanner when present)
        )
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        mock.patch.dict(os.environ, clean_env, clear=True),
        mock.patch.object(sys, "argv", ["simple-git"] + list(argv)),
        mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
        mock.patch.object(git, "_remote_post", remote_post),
        mock.patch.object(git, "_native_echo_post", native),
        mock.patch.object(git, "_SCANNER", scanner_obj),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            code = git.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _no_network(url, headers, body, timeout):
    raise AssertionError("no remote call may leave in this test")


def _no_network_native(body, write, timeout=None):
    raise AssertionError("no native call may leave in this test")


class FakeNative:
    """The custody adapter stand-in:  answers from fixtures, records every request body."""

    def __init__(self, pages=None, call_results=None):
        self.transport = FakeTransport(pages=pages, seat="ECHO", call_results=call_results)
        self.bodies = []
        self.timeouts = []

    def post(self, body, write, timeout=None):
        self.bodies.append(body)
        self.timeouts.append(timeout)
        req = json.loads(body.decode())
        # Route through the same fixture logic, without the Authorization assertion.
        if req["method"] == "tools/list":
            self.transport.list_requests += 1
            cursor = req["params"].get("cursor")
            index = 0 if cursor is None else int(cursor.split("-", 1)[1])
            try:
                tools, nxt = self.transport._page(index)
            except IndexError:
                tools, nxt = [], None
            result = {"tools": tools}
            if nxt is not None:
                result["nextCursor"] = nxt
        else:
            name = req["params"]["name"]
            self.transport.calls.append((name, req["params"].get("arguments") or {}))
            result = self.transport.call_results.get(name) or {
                "structuredContent": {"ok": True},
                "content": [{"type": "text", "text": "done"}],
                "isError": False,
            }
        envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        return 200, "application/json", envelope


class TestSmokeShape(unittest.TestCase):
    def test_help_exits_zero_without_any_credential_or_network(self):
        code, out, err = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("list", out)

    def test_usage_error_exits_two_before_any_credential_read(self):
        code, out, err = run_cli(["call"])
        self.assertEqual(code, 2)

    def test_list_without_bearer_exits_no_creds(self):
        code, out, err = run_cli(["list"])
        self.assertEqual(code, 4)
        self.assertIn("SIMPLE_TOOLS_BEARER", err)

    def test_whoami_without_bearer_exits_no_creds(self):
        code, out, err = run_cli(["whoami"])
        self.assertEqual(code, 4)

    def test_malformed_bearer_refused(self):
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": "not a bearer!"})
        self.assertEqual(code, 4)

    def test_abbreviated_long_option_rejected(self):
        code, out, err = run_cli(
            ["list", "--js"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertEqual(code, 2)

    def test_unknown_flag_rejected(self):
        code, out, err = run_cli(
            ["list", "--seat", "CODEX"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)


class TestDiscovery(unittest.TestCase):
    def test_list_uses_runtime_discovery_and_filters_non_git_tools(self):
        t = FakeTransport()
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 0)
        self.assertIn("gr_wf_repo_get", out)
        self.assertNotIn("notes_search", out)
        self.assertEqual(t.list_requests, 1)

    def test_read_write_labels_come_from_the_pinned_read_set(self):
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertIn("read  gr_wf_repo_get", out)
        self.assertIn("write  gr_wf_issue_create", out)

    def test_unknown_action_is_a_write(self):
        code, out, err = run_cli(
            ["list", "--writes"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertEqual(code, 0)
        self.assertIn("gr_new_unknown", out)
        self.assertNotIn("gr_wf_repo_get", out)

    def test_reads_filter(self):
        code, out, err = run_cli(
            ["list", "--reads"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertIn("gr_wf_repo_get", out)
        self.assertNotIn("gr_wf_issue_create", out)

    def test_multi_page_discovery(self):
        pages = [
            [make_tool("gr_wf_repo_get", READ_SCHEMA)],
            [make_tool("gr_wf_issue_create", WRITE_SCHEMA)],
        ]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport(pages)
        )
        self.assertEqual(code, 0)
        self.assertIn("gr_wf_issue_create", out)

    def test_cursor_loop_rejected(self):
        pages = [
            ([make_tool("gr_wf_repo_get", READ_SCHEMA)], "cursor-1"),
            ([make_tool("gr_wf_pr_get")], "cursor-1"),
        ]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport(pages)
        )
        self.assertEqual(code, 1)
        self.assertIn("cursor", err)

    def test_paging_past_the_page_cap_rejected(self):
        pages = [[make_tool("gr_wf_p%d" % i)] for i in range(git.DISCOVERY_MAX_PAGES + 1)]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport(pages)
        )
        self.assertEqual(code, 1)
        self.assertIn("paging", err)

    def test_tool_cap_rejected(self):
        page = [make_tool("gr_wf_a%d" % i) for i in range(3)]
        with mock.patch.object(git, "DISCOVERY_MAX_TOOLS", 2):
            code, out, err = run_cli(
                ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([page])
            )
        self.assertEqual(code, 1)
        self.assertIn("too many tools", err)

    def test_duplicate_tool_name_rejected(self):
        page = [make_tool("gr_wf_repo_get", READ_SCHEMA), make_tool("gr_wf_repo_get", READ_SCHEMA)]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([page])
        )
        self.assertEqual(code, 1)
        self.assertIn("twice", err)

    def test_oversized_cursor_rejected(self):
        pages = [([make_tool("gr_wf_repo_get", READ_SCHEMA)], "x" * 513)]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport(pages)
        )
        self.assertEqual(code, 1)

    def test_no_git_tools_for_this_caller_is_a_refusal(self):
        code, out, err = run_cli(
            ["list"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport([[make_tool("notes_search")]]),
        )
        self.assertEqual(code, 1)
        self.assertIn("no git tools", err)

    def test_unreadable_schema_rejected(self):
        page = [make_tool("gr_wf_repo_get", {"type": "array"})]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([page])
        )
        self.assertEqual(code, 1)
        self.assertIn("unreadable schema", err)

    def test_oversized_description_rejected(self):
        page = [make_tool("gr_wf_repo_get", READ_SCHEMA, "d" * 4097)]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([page])
        )
        self.assertEqual(code, 1)
        self.assertIn("oversized description", err)

    def test_show_unknown_tool(self):
        code, out, err = run_cli(
            ["show", "gr_nope"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown tool", err)

    def test_show_prints_schema(self):
        code, out, err = run_cli(
            ["show", "gr_wf_repo_get"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 0)
        self.assertIn('"owner"', out)

    def test_hosted_text_is_escaped_before_display(self):
        page = [make_tool("gr_wf_repo_get", READ_SCHEMA, "safe \x1b[31m red")]
        code, out, err = run_cli(
            ["show", "gr_wf_repo_get"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport([page]),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)

    def test_listing_is_not_execution_proof_footer(self):
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertIn("listing is not execution proof", err)


class TestActorResolution(unittest.TestCase):
    def test_read_injects_the_server_reported_seat(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(t.calls[0][0], "identities")
        self.assertEqual(t.calls[1], ("gr_wf_repo_get", {"owner": "o", "identity": "CODEX"}))

    def test_write_injects_the_server_reported_seat(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(t.calls[-1], ("gr_wf_issue_create", {"title": "t", "identity": "CODEX"}))

    def test_explicit_identity_matching_the_server_seat_passes(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o","identity":"CODEX"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)

    def test_explicit_identity_mismatch_refused_before_the_target_call(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o","identity":"GROK"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 6)
        self.assertEqual([c[0] for c in t.calls], ["identities"])  # probe only, no target call

    def test_declared_seat_matching_passes(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(t.calls[-1][1]["identity"], "CODEX")

    def test_declared_seat_mismatching_the_server_refused(self):
        t = FakeTransport(seat="ECHO")
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=t,
        )
        self.assertEqual(code, 6)
        self.assertEqual([c[0] for c in t.calls], ["identities"])

    def test_declared_and_explicit_disagree_refused(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o","identity":"GROK"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=t,
        )
        self.assertEqual(code, 2)
        self.assertEqual([c[0] for c in t.calls], ["identities"])  # the declared-seat probe only

    def test_conflicting_environment_declarations_refused(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX", "AGENT_TAG": "GROK"},
            transport=t,
        )
        self.assertEqual(code, 2)
        self.assertEqual(t.calls, [])

    def test_launcher_without_seat_refused(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_LAUNCHER": "claude"},
            transport=t,
        )
        self.assertEqual(code, 2)

    def test_bad_identity_format_refused(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o","identity":"codex"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 2)

    def test_whoami_prints_the_reported_seat(self):
        code, out, err = run_cli(
            ["whoami"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertEqual(code, 0)
        self.assertIn("authenticated_seat: CODEX", out)


class TestNativeEcho(unittest.TestCase):
    def test_declared_other_seat_refused_before_any_rpc(self):
        native = FakeNative()
        code, out, err = run_cli(
            ["list"],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1", "AGENT_SEAT": "CODEX"},
            native_post=native.post,
        )
        self.assertEqual(code, 2)
        self.assertEqual(native.bodies, [])  # not even discovery left

    def test_explicit_other_identity_refused_before_any_rpc(self):
        native = FakeNative()
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t","identity":"CODEX"}'],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
            native_post=native.post,
        )
        self.assertEqual(code, 2)
        self.assertEqual(native.bodies, [])

    def test_native_lane_needs_no_bearer_and_skips_the_probe(self):
        native = FakeNative()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1"},  # no SIMPLE_TOOLS_BEARER at all
            native_post=native.post,
        )
        self.assertEqual(code, 0, err)
        names = [c[0] for c in native.transport.calls]
        self.assertNotIn("identities", names)  # custody proves ECHO: no probe
        self.assertEqual(
            native.transport.calls[-1], ("gr_wf_repo_get", {"owner": "o", "identity": "ECHO"})
        )

    def test_native_declared_echo_passes(self):
        native = FakeNative()
        code, out, err = run_cli(
            ["list"],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1", "AGENT_SEAT": "ECHO"},
            native_post=native.post,
        )
        self.assertEqual(code, 0, err)

    def _error_result(self, code_in_content):
        text = json.dumps({"code": code_in_content}) if code_in_content else "plain failure text"
        return {"isError": True, "content": [{"type": "text", "text": text}]}

    def test_native_uncertain_transport_shape_keeps_outcome_unknown(self):
        native = FakeNative(
            call_results={"gr_wf_issue_create": self._error_result("outcome_unknown")}
        )
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
            native_post=native.post,
        )
        self.assertEqual(code, 1)
        self.assertIn("outcome_unknown", err)
        self.assertIn("may or may not have been applied", err)

    def test_native_genuine_refusal_keeps_its_code_and_is_not_uncertain(self):
        native = FakeNative(
            call_results={"gr_wf_issue_create": self._error_result("relay_refused")}
        )
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
            native_post=native.post,
        )
        self.assertEqual(code, 1)
        self.assertIn("relay_refused", err)
        self.assertNotIn("may or may not have been applied", err)

    def test_native_error_without_a_known_code_is_not_fabricated_as_unknown(self):
        native = FakeNative(call_results={"gr_wf_issue_create": self._error_result(None)})
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
            native_post=native.post,
        )
        self.assertEqual(code, 1)
        self.assertNotIn("outcome_unknown", err)
        self.assertNotIn("may or may not have been applied", err)


class TestNativeCustodyTransport(unittest.TestCase):
    """_native_echo_post itself: the fixed Linux custody route, root direct, ordinary shell via
    the exact already-authorized non-interactive elevation.  Linux-only, no fallback."""

    CUSTODY = "/usr/local/libexec/assigned-echo-mcp/linux-custody.py"

    def _run_transport(self, euid, platform="linux", isfile=True, returncode=0, write=False):
        answer = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        hello = {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "x"}}
        proc = types.SimpleNamespace(
            returncode=returncode,
            stdout=(json.dumps(hello) + "\n" + json.dumps(answer) + "\n").encode(),
        )
        with (
            mock.patch.object(git.os, "geteuid", return_value=euid),
            mock.patch.object(git.sys, "platform", platform),
            mock.patch.object(git.os.path, "isfile", return_value=isfile),
            mock.patch.object(git.subprocess, "run", return_value=proc) as run,
        ):
            try:
                result = git._native_echo_post(b'{"jsonrpc":"2.0","id":1}', write)
                return run, result, None
            except SystemExit as exc:
                return run, None, exc

    def test_ordinary_shell_uses_the_exact_authorized_elevation(self):
        run, result, exc = self._run_transport(euid=1000)
        self.assertIsNone(exc)
        self.assertEqual(result[0], 200)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", self.CUSTODY],
        )
        kwargs = run.call_args.kwargs
        self.assertNotIn("shell", kwargs)  # list argv, never a shell
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin"})  # no environment preservation
        self.assertEqual(kwargs["stderr"], git.subprocess.DEVNULL)  # no secret output channel
        self.assertTrue(kwargs["input"].endswith(b'{"jsonrpc":"2.0","id":1}\n'))

    def test_root_invocation_is_unchanged_without_sudo(self):
        run, result, exc = self._run_transport(euid=0)
        self.assertIsNone(exc)
        self.assertEqual(result[0], 200)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/python3", "-I", self.CUSTODY])

    def test_mac_refuses_for_ordinary_and_root_alike_no_fallback(self):
        for euid in (1000, 0):
            run, result, exc = self._run_transport(euid=euid, platform="darwin")
            self.assertIsNotNone(exc)
            self.assertEqual(exc.code, 6)  # EXIT_AUTH
            self.assertEqual(run.call_count, 0)

    def test_missing_custody_helper_refuses_before_any_spawn(self):
        run, result, exc = self._run_transport(euid=1000, isfile=False)
        self.assertIsNotNone(exc)
        self.assertEqual(exc.code, 6)
        self.assertEqual(run.call_count, 0)

    def test_sudo_refusal_fails_closed_once_no_retry(self):
        run, result, exc = self._run_transport(euid=1000, returncode=1, write=True)
        self.assertIsNotNone(exc)
        self.assertEqual(exc.code, 1)  # EXIT_ERROR
        self.assertEqual(run.call_count, 1)  # exactly one attempt, never retried


class TestStrictJsonAndBounds(unittest.TestCase):
    def test_duplicate_keys_in_arguments_refused(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"a","owner":"b"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        self.assertNotIn("owner", err)  # the parser's detail would echo the caller's duplicate key

    def test_non_finite_numbers_in_arguments_refused(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":NaN}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)

    def test_duplicate_keys_in_a_server_answer_make_it_unreadable(self):
        transport = FakeTransport()
        raw = b'{"jsonrpc":"2.0","id":1,"id":1,"result":{"tools":[]}}'
        transport.post = lambda url, headers, body, timeout: (200, "application/json", raw)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=transport)
        self.assertEqual(code, 1)
        self.assertIn("no readable answer", err)

    def test_stdin_over_the_cap_rejected(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", "-"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
            stdin_text=" " * (git.ARGS_MAX_BYTES + 2),
        )
        self.assertEqual(code, 2)
        self.assertIn("exceeds", err)

    def test_stdin_arguments_accepted(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", "-"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
            stdin_text='{"owner":"stdin"}',
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(t.calls[-1][1]["owner"], "stdin")

    def test_at_file_over_the_cap_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(" " * (git.ARGS_MAX_BYTES + 2))
            path = handle.name
        try:
            code, out, err = run_cli(
                ["call", "gr_wf_repo_get", "--args-json", "@" + path],
                env={"SIMPLE_TOOLS_BEARER": BEARER},
                transport=FakeTransport(),
            )
        finally:
            os.unlink(path)
        self.assertEqual(code, 2)
        self.assertIn("exceeds", err)

    def test_missing_at_file_rejected(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", "@/nonexistent/nope.json"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_arguments_must_be_an_object(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '["owner"]'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)


class TestValidationAndScanning(unittest.TestCase):
    def test_oversized_argument_refused_before_any_call(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "x" * 70000})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 2)
        self.assertIn("exceed", err)
        self.assertEqual(
            [c[0] for c in t.calls], ["identities"]
        )  # the principal probe only, no target call

    def test_per_action_limit_widens_where_the_worker_does(self):
        t = FakeTransport(pages=[[make_tool("gr_wf_file_put", PUT_SCHEMA)]])
        big = "x" * 100000
        code, out, err = run_cli(
            ["call", "gr_wf_file_put", "--args-json", json.dumps({"path": "a", "content": big})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)

    def test_schema_keys_enforced(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o","bogus":1}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 2)
        self.assertIn("schema", err)

    def test_secret_scan_refuses_before_sending(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "token " + BEARER})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 2)
        self.assertIn("secret_scan:title", err)
        self.assertNotIn(BEARER, err)  # the flagged text is never printed
        self.assertEqual([c[0] for c in t.calls], ["identities"])  # the target call never left

    def test_scanner_missing_fails_closed(self):
        # The real scripts/agent_sync/secretscan.py is not in this repository:  loading must fail closed.
        scanner_path = (
            Path(os.path.realpath(str(IMPL))).parent.parent / "agent_sync" / "secretscan.py"
        )
        if scanner_path.is_file():
            self.skipTest("the real scanner is present here; the fail-closed path is unreachable")
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
            scanner=None,
        )
        self.assertEqual(code, 1)
        self.assertIn("no unscanned route", err)
        self.assertEqual([c[0] for c in t.calls if c[0] != "identities"], [])


class TestOutcomes(unittest.TestCase):
    def test_5xx_on_a_write_is_uncertain(self):
        t = FakeTransport()
        inner = t.post

        def flaky(url, headers, body, timeout):
            req = json.loads(body.decode())
            if req["method"] == "tools/call" and req["params"]["name"] == "gr_wf_issue_create":
                return 500, "", b""
            return inner(url, headers, body, timeout)

        t.post = flaky
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)
        self.assertIn("may or may not have been applied", err)

    def test_5xx_on_a_read_is_not_uncertain(self):
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport(status=500)
        )
        self.assertEqual(code, 1)
        self.assertNotIn("may or may not have been applied", err)

    def test_dead_answer_after_sending_a_write_is_uncertain(self):
        t = FakeTransport()
        inner = t.post

        def boom(url, headers, body, timeout):
            req = json.loads(body.decode())
            if req["method"] == "tools/call" and req["params"]["name"] == "gr_wf_issue_create":
                raise git._TransportError(
                    True, "deadline"
                )  # the request left; the answer never came
            return inner(url, headers, body, timeout)

        t.post = boom
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("may or may not have been applied", err)

    def test_unsent_failure_has_no_uncertainty_wording(self):
        def boom(url, headers, body, timeout):
            raise git._TransportError(False, "unreachable")

        t = FakeTransport()
        t.post = boom
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", err)
        self.assertNotIn("may or may not have been applied", err)

    def test_server_message_text_is_never_copied(self):
        t = FakeTransport()
        evil = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "echo: SECRETINPUT"},
        }
        raw = json.dumps(evil).encode()
        t.post = lambda url, headers, body, timeout: (200, "application/json", raw)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 1)
        self.assertNotIn("SECRETINPUT", err)
        self.assertIn("refused", err)

    def test_outcome_unknown_on_a_write_carries_the_note(self):
        t = FakeTransport(
            call_results={
                "gr_wf_issue_create": {
                    "isError": True,
                    "structuredContent": {"code": "outcome_unknown"},
                    "content": [{"type": "text", "text": "lost"}],
                }
            }
        )
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("outcome_unknown", err)
        self.assertIn("may or may not have been applied", err)

    def test_outcome_unknown_on_a_read_has_no_write_note(self):
        t = FakeTransport(
            call_results={
                "gr_wf_repo_get": {
                    "isError": True,
                    "structuredContent": {"code": "outcome_unknown"},
                    "content": [{"type": "text", "text": "lost"}],
                }
            }
        )
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("outcome_unknown", err)
        self.assertNotIn("may or may not have been applied", err)

    def test_rate_limited_shows_bounded_retry_after(self):
        t = FakeTransport(
            call_results={
                "gr_wf_repo_get": {
                    "isError": True,
                    "structuredContent": {"code": "rate_limited", "retry_after_s": 30},
                    "content": [{"type": "text", "text": "slow down"}],
                }
            }
        )
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertIn("rate_limited, retry after 30s", err)

    def test_unbounded_retry_after_ignored(self):
        t = FakeTransport(
            call_results={
                "gr_wf_repo_get": {
                    "isError": True,
                    "structuredContent": {"code": "rate_limited", "retry_after_s": 99999999},
                    "content": [{"type": "text", "text": "slow down"}],
                }
            }
        )
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertIn("rate_limited", err)
        self.assertNotIn("retry after", err)

    def test_unknown_action_is_called_as_a_write(self):
        # gr_new_unknown is not in the reviewed read set:  its failures carry write uncertainty wording.
        t = FakeTransport(
            call_results={
                "gr_new_unknown": {
                    "isError": True,
                    "structuredContent": {"code": "outcome_unknown"},
                    "content": [{"type": "text", "text": "lost"}],
                }
            }
        )
        code, out, err = run_cli(
            ["call", "gr_new_unknown", "--args-json", "{}"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("may or may not have been applied", err)

    def test_call_unknown_tool(self):
        code, out, err = run_cli(
            ["call", "gr_nope"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport()
        )
        self.assertEqual(code, 2)

    def test_sse_answer_parsed(self):
        t = FakeTransport()
        inner = t.post

        def sse(url, headers, body, timeout):
            status, ctype, raw = inner(url, headers, body, timeout)
            return status, "text/event-stream", b"data: " + raw + b"\n\n"

        t.post = sse
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 0, err)

    def test_deep_nesting_rejected(self):
        t = FakeTransport()
        raw = ("{" * 100 + "}" * 100).encode()
        t.post = lambda url, headers, body, timeout: (200, "application/json", raw)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 1)
        self.assertIn("nests too deeply", err)

    def test_wrong_answer_id_ignored(self):
        t = FakeTransport()
        raw = json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"tools": []}}).encode()
        t.post = lambda url, headers, body, timeout: (200, "application/json", raw)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 1)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(
            git._NoRedirect().redirect_request(None, None, 302, None, {}, "https://evil.example")
        )

    def test_result_json_shape(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}', "--json"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        parsed = json.loads(out)
        self.assertIn("structured", parsed)
        self.assertIn("text", parsed)


class TestNoCallerEcho(unittest.TestCase):
    """Errors never carry caller or provider prose:  a sentinel must not appear in any error output."""

    SENTINEL = "ZXSENTINEL"

    def test_oversized_at_file_error_never_contains_contents(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"title": "' + self.SENTINEL + '"}' + " " * git.ARGS_MAX_BYTES)
            path = handle.name
        try:
            code, out, err = run_cli(
                ["call", "gr_wf_issue_create", "--args-json", "@" + path],
                env={"SIMPLE_TOOLS_BEARER": BEARER},
                transport=FakeTransport(),
            )
        finally:
            os.unlink(path)
        self.assertEqual(code, 2)
        self.assertIn("exceeds", err)
        self.assertNotIn(self.SENTINEL, err)

    def test_duplicate_key_name_never_echoed(self):
        code, out, err = run_cli(
            [
                "call",
                "gr_wf_repo_get",
                "--args-json",
                '{"' + self.SENTINEL + '":1,"' + self.SENTINEL + '":2}',
            ],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        self.assertNotIn(self.SENTINEL, err)

    def test_non_finite_value_never_echoed(self):
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"' + self.SENTINEL + '":Infinity}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 2)
        self.assertNotIn(self.SENTINEL, err)
        self.assertNotIn("Infinity", err)

    def test_deep_arguments_fail_clean_without_a_traceback(self):
        deep = "[" * 1500 + "]" * 1500
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", "-"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=FakeTransport(),
            stdin_text=deep,
        )
        self.assertEqual(code, 2)
        self.assertIn("nests too deeply", err)
        self.assertNotIn("Traceback", err)


class TestActorAssertionEveryVerb(unittest.TestCase):
    """The declared seat is asserted against the server-reported seat for list/show/whoami too,
    before discovery;  whoami refuses a mismatch instead of displaying it."""

    def test_list_with_matching_declared_seat(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"}, transport=t
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [c[0] for c in t.calls], ["identities"]
        )  # the assertion probe, before discovery

    def test_list_with_mismatching_declared_seat_refused_before_discovery(self):
        t = FakeTransport(seat="ECHO")
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"}, transport=t
        )
        self.assertEqual(code, 6)
        self.assertEqual([c[0] for c in t.calls], ["identities"])
        self.assertEqual(t.list_requests, 0)  # refused before discovery

    def test_show_with_mismatching_declared_seat_refused(self):
        t = FakeTransport(seat="ECHO")
        code, out, err = run_cli(
            ["show", "gr_wf_repo_get"],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=t,
        )
        self.assertEqual(code, 6)
        self.assertEqual(t.list_requests, 0)

    def test_whoami_mismatch_refuses_instead_of_displaying(self):
        t = FakeTransport(seat="ECHO")
        code, out, err = run_cli(
            ["whoami"], env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"}, transport=t
        )
        self.assertEqual(code, 6)
        self.assertNotIn("authenticated_seat", out)

    def test_whoami_matching_declared_seat_displays(self):
        code, out, err = run_cli(
            ["whoami"],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=FakeTransport(),
        )
        self.assertEqual(code, 0)
        self.assertIn("authenticated_seat: CODEX", out)

    def test_call_without_identity_argument_still_asserts_a_declared_seat(self):
        # gr_new_unknown's schema has no identity property:  a declared seat is still checked.
        t = FakeTransport(seat="ECHO")
        code, out, err = run_cli(
            ["call", "gr_new_unknown", "--args-json", "{}"],
            env={"SIMPLE_TOOLS_BEARER": BEARER, "AGENT_SEAT": "CODEX"},
            transport=t,
        )
        self.assertEqual(code, 6)
        self.assertEqual(
            [c[0] for c in t.calls], ["identities"]
        )  # main()'s assertion probe, no target call

    def test_call_without_identity_and_no_declaration_probes_once(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_new_unknown", "--args-json", "{}"],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual([c[0] for c in t.calls], ["identities", "gr_new_unknown"])
        self.assertNotIn(
            "identity", t.calls[-1][1]
        )  # nothing injected where the schema has no identity


class TestPrincipalEstablishment(unittest.TestCase):
    """One identities result establishes the actor for every bearer-mode verb;  auth/default must agree
    when both are usable, and the advertised identities must include the reported seat."""

    def test_contradictory_auth_and_default_refused(self):
        t = FakeTransport(seat="CODEX", default_seat="ECHO")
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("contradictory", err)
        self.assertEqual(t.list_requests, 0)

    def test_contradiction_refused_even_undeclared(self):
        t = FakeTransport(seat="CODEX", default_seat="ECHO")
        code, out, err = run_cli(["whoami"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)

    def _legacy_sc(self, auth):
        # The permitted legacy shape:  auth absent-or-null, a valid default, and the advertised array.
        sc = {"default_identity": "CODEX", "identities": [{"identity": "CODEX"}]}
        if auth != "absent":
            sc["authenticated_seat"] = auth
        return sc

    def test_absent_auth_falls_back_to_default(self):
        t = FakeTransport(sc_override=self._legacy_sc("absent"))
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 0, err)

    def test_null_auth_falls_back_to_default(self):
        t = FakeTransport(sc_override=self._legacy_sc(None))
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 0, err)

    def test_present_nonnull_malformed_auth_refused(self):
        t = FakeTransport(sc_override=self._legacy_sc("not a seat!"))
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("malformed", err)

    def test_present_nonnull_malformed_default_refused(self):
        sc = {
            "authenticated_seat": "CODEX",
            "default_identity": 42,
            "identities": [{"identity": "CODEX"}],
        }
        t = FakeTransport(sc_override=sc)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("malformed", err)

    def test_default_only_principal_is_the_injected_actor(self):
        t = FakeTransport(sc_override=self._legacy_sc("absent"))
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(t.calls[-1], ("gr_wf_repo_get", {"owner": "o", "identity": "CODEX"}))

    def test_neither_field_usable_refused(self):
        t = FakeTransport(sc_override={"identities": [{"identity": "CODEX"}]})
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("did not report a usable seat", err)

    def test_advertised_identities_must_include_the_seat(self):
        t = FakeTransport(seat="CODEX", advertised=[{"identity": "GROK"}])
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("do not include", err)

    def test_empty_advertised_list_refused(self):
        t = FakeTransport(seat="CODEX", advertised=[])
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)
        self.assertIn("no usable identities", err)

    def test_missing_advertised_list_refused(self):
        sc = {"authenticated_seat": "CODEX", "default_identity": "CODEX"}
        t = FakeTransport(sc_override=sc)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)

    def test_non_list_advertised_refused(self):
        sc = {"authenticated_seat": "CODEX", "identities": "CODEX"}
        t = FakeTransport(sc_override=sc)
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 6)

    def test_invalid_identity_objects_refused(self):
        for bad in (["CODEX"], [{"name": "CODEX"}], [{"identity": "not a seat!"}]):
            sc = {"authenticated_seat": "CODEX", "identities": bad}
            t = FakeTransport(sc_override=sc)
            code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
            self.assertEqual(code, 6, bad)

    def test_whoami_makes_exactly_one_request(self):
        t = FakeTransport()
        code, out, err = run_cli(["whoami"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 0)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0][0], "identities")

    def _native_whoami_result(self, auth, default, advertised=None):
        return {
            "structuredContent": {
                "authenticated_seat": auth,
                "default_identity": default,
                "identities": advertised if advertised is not None else [{"identity": auth}],
            },
            "content": [{"type": "text", "text": auth}],
            "isError": False,
        }

    def test_native_whoami_displays_a_consistent_echo_principal(self):
        native = FakeNative(call_results={"identities": self._native_whoami_result("ECHO", "ECHO")})
        code, out, err = run_cli(
            ["whoami"], env={"SIMPLE_GIT_NATIVE_ECHO": "1"}, native_post=native.post
        )
        self.assertEqual(code, 0, err)
        self.assertIn("authenticated_seat: ECHO", out)

    def test_native_whoami_refuses_a_contradictory_principal(self):
        native = FakeNative(
            call_results={"identities": self._native_whoami_result("ECHO", "CODEX")}
        )
        code, out, err = run_cli(
            ["whoami"], env={"SIMPLE_GIT_NATIVE_ECHO": "1"}, native_post=native.post
        )
        self.assertEqual(code, 6)
        self.assertNotIn("authenticated_seat", out)

    def test_native_whoami_refuses_a_non_echo_principal(self):
        native = FakeNative(
            call_results={"identities": self._native_whoami_result("CODEX", "CODEX")}
        )
        code, out, err = run_cli(
            ["whoami"], env={"SIMPLE_GIT_NATIVE_ECHO": "1"}, native_post=native.post
        )
        self.assertEqual(code, 6)
        self.assertNotIn("authenticated_seat: CODEX", out)


class TestSchemaFailClosed(unittest.TestCase):
    def test_missing_input_schema_is_a_refusal(self):
        entry = make_tool("gr_wf_repo_get", READ_SCHEMA)
        del entry["inputSchema"]
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([[entry]])
        )
        self.assertEqual(code, 1)
        self.assertIn("missing or unreadable schema", err)

    def test_non_object_input_schema_is_a_refusal(self):
        entry = make_tool("gr_wf_repo_get", READ_SCHEMA)
        entry["inputSchema"] = "not-a-schema"
        code, out, err = run_cli(
            ["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=FakeTransport([[entry]])
        )
        self.assertEqual(code, 1)
        self.assertIn("missing or unreadable schema", err)


def digest_of(pattern):
    import hashlib

    return hashlib.sha256(pattern.encode("utf-8")).hexdigest()


def with_known(*patterns):
    """Patch the reviewed-pattern set for a test:  production is exactly the 18;  tests may add their
    adversarial fixture so the sandbox kill path stays exercised."""
    return mock.patch.object(
        git, "_KNOWN_PATTERN_DIGESTS", git._KNOWN_PATTERN_DIGESTS | {digest_of(x) for x in patterns}
    )


CATALOG_SHA = r"^[0-9a-f]{40}$"  # a real catalog pattern for ordinary verdict tests


class TestReview1199(unittest.TestCase):
    """PR 1199 review fixes:  strict cursors, anyOf intersection, the overall budget, read1 guard,
    program-name escaping and recursive leaf-schema validation."""

    # 1. An empty nextCursor is malformed, never a terminator.
    def test_empty_cursor_is_malformed_not_a_terminator(self):
        t = FakeTransport(pages=[(DEFAULT_PAGE[:1], "")])
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 1)
        self.assertIn("malformed or repeating", err)

    def test_absent_and_null_cursor_end_the_list(self):
        for nxt in (None,):  # absent already covered everywhere;  null is explicit here
            t = FakeTransport(pages=[(DEFAULT_PAGE[:1], nxt)])
            code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
            self.assertEqual(code, 0, err)

    # 2. anyOf intersection:  root constraints AND one variant, variant-only keys known there only.
    ANYOF_SCHEMA = {
        "type": "object",
        "required": ["repo"],
        "properties": {"repo": {"type": "string"}},
        "anyOf": [
            {"required": ["mode"], "properties": {"mode": {"type": "string", "enum": ["a", "b"]}}},
            {"required": ["count"], "properties": {"count": {"type": "integer", "minimum": 1}}},
        ],
    }

    def _anyof_transport(self):
        return FakeTransport(pages=[[make_tool("gr_wf_repo_get", self.ANYOF_SCHEMA)]])

    def _anyof_call(self, args, t=None):
        return run_cli(
            ["call", "gr_wf_repo_get", "--args-json", json.dumps(args)],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t or self._anyof_transport(),
        )

    def test_anyof_variant_only_property_accepted(self):
        for args in ({"repo": "r", "mode": "a"}, {"repo": "r", "count": 2}):
            code, out, err = self._anyof_call(args)
            self.assertEqual(code, 0, err)

    def test_anyof_variant_constraints_enforced(self):
        for args in ({"repo": "r", "mode": "c"}, {"repo": "r", "count": 0}):
            code, out, err = self._anyof_call(args)
            self.assertEqual(code, 2, err)

    def test_anyof_unknown_key_still_refused(self):
        code, out, err = self._anyof_call({"repo": "r", "mode": "a", "bogus": 1})
        self.assertEqual(code, 2)
        self.assertIn("do not match any supported variant", err)

    def test_anyof_root_required_still_enforced(self):
        code, out, err = self._anyof_call({"mode": "a"})
        self.assertEqual(code, 2)
        code, out, err = self._anyof_call({"repo": "r"})
        self.assertEqual(code, 2)

    # 2b. Overlapping root/variant keys:  BOTH constraints apply (intersection, never replacement).
    OVERLAP_SCHEMA = {
        "type": "object",
        "required": ["repo"],
        "properties": {
            "repo": {"type": "string"},
            "x": {"type": "integer", "minimum": 10},
            "s": {"type": "string", "minLength": 2},
            "o": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5}}},
        },
        "anyOf": [
            {
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "s": {"type": "string", "enum": ["aa", "bb"]},
                    "o": {
                        "type": "object",
                        "properties": {"n": {"type": "integer", "maximum": 100}},
                    },
                }
            }
        ],
    }

    def _overlap_call(self, args):
        t = FakeTransport(pages=[[make_tool("gr_wf_repo_get", self.OVERLAP_SCHEMA)]])
        return run_cli(
            ["call", "gr_wf_repo_get", "--args-json", json.dumps(args)],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )

    def test_anyof_overlap_enforces_the_root_side(self):
        # root x minimum 10, variant minimum 0:  1 satisfies the variant but violates the root
        code, out, err = self._overlap_call({"repo": "r", "x": 1})
        self.assertEqual(code, 2)
        code, out, err = self._overlap_call({"repo": "r", "x": 15})
        self.assertEqual(code, 0, err)

    def test_anyof_overlap_enforces_both_string_sides(self):
        # root string minLength 2, variant enum [aa, bb]:  both apply
        code, out, err = self._overlap_call({"repo": "r", "s": "cc"})
        self.assertEqual(code, 2)  # enum refuses
        code, out, err = self._overlap_call({"repo": "r", "s": "a"})
        self.assertEqual(code, 2)  # enum AND root minLength refuse
        code, out, err = self._overlap_call({"repo": "r", "s": "aa"})
        self.assertEqual(code, 0, err)

    def test_anyof_overlap_nested_object_bounds_from_both_sides(self):
        # root nested minimum 5, variant nested maximum 100:  both bounds hold
        code, out, err = self._overlap_call({"repo": "r", "o": {"n": 1}})
        self.assertEqual(code, 2)  # root bound refuses
        code, out, err = self._overlap_call({"repo": "r", "o": {"n": 150}})
        self.assertEqual(code, 2)  # variant bound refuses
        code, out, err = self._overlap_call({"repo": "r", "o": {"n": 50}})
        self.assertEqual(code, 0, err)

    # 7. Untrusted patterns:  compile AND search run only in the isolated, deadline-bounded child
    # (Kody HIGH r4431100051).  These are real-timeout tests, not screen booleans.
    def test_patterns_match_definite_verdicts(self):
        end = time.monotonic() + 5
        self.assertTrue(git._patterns_match([(CATALOG_SHA, "a" * 40)], end))
        self.assertFalse(git._patterns_match([(CATALOG_SHA, "g" * 40)], end))
        self.assertTrue(git._patterns_match([], end))

    def test_unrecognized_pattern_fails_closed_without_compiling(self):
        # anything outside the exact reviewed 18:  no compile, no search, unverifiable -> refused
        start = time.monotonic()
        for pattern in (r"^[a-z]+$", r"^(a|aa){50}$", "a" * 600, r"(a?){1000000000}$", "["):
            self.assertIsNone(git._patterns_match([(pattern, "abc")], time.monotonic() + 5))
            self.assertFalse(git._pattern_known(pattern))
        self.assertLess(time.monotonic() - start, 2)

    def test_catastrophic_patterns_are_killed_inside_the_bound(self):
        # the host's actual counterexamples, run through the child via a test-extended known set:
        # each unbounded re.search exceeds 1.5s;  the child must be killed at the bound (unverifiable)
        patterns = [r"^(a|aa){50}$", r"^a*a*a*a*a*a*b$", r"^(a?){1000000000}$"]
        with with_known(*patterns):
            for pattern, value in (
                (r"^(a|aa){50}$", "a" * 49 + "!"),
                (r"^a*a*a*a*a*a*b$", "a" * 200 + "!"),
                (r"^(a?){1000000000}$", "a"),
            ):
                start = time.monotonic()
                verdict = git._patterns_match([(pattern, value)], start + 0.3)
                elapsed = time.monotonic() - start
                self.assertIsNone(verdict, pattern)
                self.assertLess(elapsed, 3, pattern)

    def test_invalid_pattern_is_a_clean_refusal_not_a_traceback(self):
        # a pattern that is (hypothetically) reviewed-known but does not compile: clean False
        with with_known("["):
            self.assertFalse(git._patterns_match([("[", "x")], time.monotonic() + 5))

    def test_the_real_catalog_patterns_compile_and_match_in_the_child(self):
        import json as _json
        from pathlib import Path as _Path

        catalog = _json.loads((_Path(__file__).resolve().parent / "catalog.json").read_text())
        tools = catalog if isinstance(catalog, list) else catalog.get("tools", catalog)
        patterns = set()

        def walk(node):
            if isinstance(node, dict):
                value = node.get("pattern")
                if isinstance(value, str):
                    patterns.add(value)
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(tools)
        self.assertEqual(len(patterns), 18)  # the actual capture: 18 distinct patterns
        # every real pattern compiles and matches a plausible value inside the child, fast
        samples = {
            "repo": "acme/widgets",
            "branch": "main",
            "path": "docs/readme.md",
        }
        # every pattern INDIVIDUALLY through the child (an all() mismatch must not short-circuit any
        # pattern out of being exercised):  each compiles and returns a definite verdict, fast
        start = time.monotonic()
        for pattern in patterns:
            verdict = git._patterns_match([(pattern, "x")], time.monotonic() + 5)
            self.assertIsNotNone(verdict, pattern)
        self.assertLess(time.monotonic() - start, 5)
        # and the known host shapes actually MATCH plausible values
        positive = {
            r"^(?!.*\.\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$": "acme/widgets",
            r"^(?![/-])(?!.*[.][.])(?!.*(?:/|[.]lock)$)[A-Za-z0-9._/-]+$": "main",
            r"^(?!/)(?!.*//)(?!.*(?:^|/)[.]{1,2}(?:/|$))(?!.*[/]$)[A-Za-z0-9._/+@~-]+$": "docs/a.md",
        }
        for pattern, value in positive.items():
            self.assertTrue(git._patterns_match([(pattern, value)], time.monotonic() + 5), pattern)

    def test_call_with_an_unrecognized_pattern_never_reaches_the_child(self):
        # an unreviewed pattern makes the schema unreadable at discovery: no fork, no target call
        schema = {
            "type": "object",
            "required": ["title", "identity"],
            "properties": {
                "title": {"type": "string", "pattern": r"^(a|aa){50}$"},
                "identity": {"type": "string"},
            },
        }
        t = FakeTransport(pages=[[make_tool("gr_wf_issue_create", schema)]])
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "a" * 49 + "!"})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 1)
        self.assertIn("missing or unreadable schema", err)
        self.assertNotIn("gr_wf_issue_create", [c[0] for c in t.calls])

    def test_call_with_a_normal_pattern_still_passes(self):
        schema = {
            "type": "object",
            "required": ["title", "identity"],
            "properties": {
                "title": {"type": "string", "pattern": CATALOG_SHA},
                "identity": {"type": "string"},
            },
        }
        t = FakeTransport(pages=[[make_tool("gr_wf_issue_create", schema)]])
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "a" * 40})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "g" * 40})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 2)  # a real non-match is a clean usage refusal

    def test_anyof_variant_without_properties_is_not_an_unboundlocalerror(self):
        # the host repro:  root object {title: string}, anyOf [{required:[title]}], args {title: x}
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "anyOf": [{"required": ["title"]}],
        }
        t = FakeTransport(pages=[[make_tool("gr_wf_repo_get", schema)]])
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"title":"x"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)

    def test_one_absolute_deadline_covers_every_variant(self):
        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "anyOf": [
                {"properties": {"title": {"type": "string", "pattern": r"^(a|aa){50}$"}}},
                {"properties": {"title": {"type": "string", "pattern": r"^(a|aa){60}$"}}},
                {"properties": {"title": {"type": "string", "pattern": r"^(a|aa){70}$"}}},
            ],
        }
        t = FakeTransport(pages=[[make_tool("gr_wf_issue_create", schema)]])
        start = time.monotonic()
        with (
            mock.patch.object(git, "_PATTERN_CHECK_MAX_S", 0.3),
            with_known(r"^(a|aa){50}$", r"^(a|aa){60}$", r"^(a|aa){70}$"),
        ):
            code, out, err = run_cli(
                [
                    "call",
                    "gr_wf_issue_create",
                    "--args-json",
                    json.dumps({"title": "a" * 49 + "!"}),
                ],
                env={"SIMPLE_TOOLS_BEARER": BEARER},
                transport=t,
            )
        # one shared 0.3s end - NOT a fresh cap per variant (3 x 0.3 would take ~1s)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(code, 1)
        self.assertIn("did not complete within the time budget", err)

    def test_pattern_work_never_starts_after_the_deadline(self):
        with mock.patch.object(
            git.os, "fork", side_effect=AssertionError("fork must not be reached")
        ):
            self.assertIsNone(git._patterns_match([(CATALOG_SHA, "a" * 40)], time.monotonic() - 1))

    def test_pipe_failure_fails_closed(self):
        with mock.patch.object(git.os, "pipe", side_effect=OSError("no fds")):
            self.assertIsNone(git._patterns_match([(CATALOG_SHA, "a" * 40)], time.monotonic() + 5))

    def test_fork_failure_closes_both_fds_and_fails_closed(self):
        real_pipe = os.pipe
        made = []

        def tracking_pipe():
            r, w = real_pipe()
            made.extend((r, w))
            return r, w

        with (
            mock.patch.object(git.os, "pipe", tracking_pipe),
            mock.patch.object(git.os, "fork", side_effect=OSError("no processes")),
        ):
            self.assertIsNone(git._patterns_match([(CATALOG_SHA, "a" * 40)], time.monotonic() + 5))
        for fd in made:
            with self.assertRaises(OSError):
                os.fstat(fd)  # both closed

    def test_missing_rlimits_fail_closed(self):
        with mock.patch.dict(sys.modules, {"resource": None}):
            self.assertIsNone(git._patterns_match([(CATALOG_SHA, "a" * 40)], time.monotonic() + 5))

    def test_as_limit_refusal_still_gives_a_verdict_but_cpu_refusal_fails_closed(self):
        import resource as _resource

        real = _resource.setrlimit
        for failing, expected in (
            (
                _resource.RLIMIT_AS,
                True,
            ),  # the actual Mac 3.15 failure: tolerated, AS is supplementary
            (_resource.RLIMIT_CPU, None),  # the CPU bound is required safety: no verdict without it
        ):

            def picky(which, limits, _failing=failing):
                if which == _failing:
                    raise ValueError("current limit exceeds maximum limit")
                return real(which, limits)

            with mock.patch("resource.setrlimit", picky):
                self.assertIs(
                    git._patterns_match([(CATALOG_SHA, "a" * 40)], time.monotonic() + 5), expected
                )

    def test_as_cap_is_clamped_to_the_platform_hard_limit(self):
        # the Mac failure shape: a fixed 1 GiB exceeds the platform hard limit -> clamp, never raise
        # (the child is a fork, so this is verified on the pure helper, not observed across the fork)

        class FakeResource:
            RLIMIT_AS = 9
            RLIM_INFINITY = -1

            def __init__(self, hard):
                self.hard = hard

            def getrlimit(self, which):
                return (64 << 20, self.hard)

        target, hard = git._as_limit_target(FakeResource(512 << 20))
        self.assertEqual((target, hard), (512 << 20, 512 << 20))  # clamped down to the hard limit
        target, hard = git._as_limit_target(FakeResource(-1))
        self.assertEqual((target, hard), (1 << 30, -1))  # unlimited: the cap, hard untouched
        target, hard = git._as_limit_target(FakeResource(4 << 30))
        self.assertEqual((target, hard), (1 << 30, 4 << 30))  # larger hard: the cap, hard untouched

    def test_unrecognized_pattern_is_an_unreadable_schema_at_discovery(self):
        # the exact contract:  anything outside the reviewed 18 fails closed, no compile anywhere
        for pattern in ("a" * 600, r"(a?){1000000000}$", r"^[a-z]+$", r"(a|aa){50}"):
            self._schema_refusal(
                {"type": "object", "properties": {"s": {"type": "string", "pattern": pattern}}}
            )

    def test_exceptional_parent_exit_kills_and_reaps_its_own_child(self):
        with with_known(r"^a*a*a*a*a*a*b$"):
            with mock.patch.object(git.os, "waitpid", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    git._patterns_match(
                        [(r"^a*a*a*a*a*a*b$", "a" * 200 + "!")], time.monotonic() + 60
                    )
        # the child was killed, not left burning CPU:  reap it here, then nothing remains
        for _ in range(100):
            done, _ = os.waitpid(-1, os.WNOHANG)
            if done == 0:
                break
            time.sleep(0.01)
        self.assertEqual(os.waitpid(-1, os.WNOHANG)[0], 0)

    def test_validation_never_starts_when_the_budget_is_gone(self):
        entry = {"name": "gr_x", "read": False, "schema": {"type": "object"}}
        with self.assertRaises(SystemExit) as ctx:
            git.validate_arguments(entry, {"a": 1}, time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, 1)

        # 3. One overall budget:  probe + pages + target;  an exhausted budget never starts the write.

    def test_budget_exhausted_before_target_write_is_reported_not_sent(self):
        t = FakeTransport()
        ticks = iter([1000.0, 1000.0, 1000.0, 1000.0 + 9999.0])
        clock = mock.patch.object(
            git.time, "monotonic", side_effect=lambda: next(ticks, 1000.0 + 9999.0)
        )
        with clock:
            code, out, err = run_cli(
                ["call", "gr_wf_issue_create", "--args-json", '{"title":"t"}'],
                env={"SIMPLE_TOOLS_BEARER": BEARER},
                transport=t,
            )
        self.assertEqual(code, 1)
        self.assertIn("the write was not sent", err)
        self.assertNotIn("may or may not have been applied", err)
        self.assertNotIn("gr_wf_issue_create", [c[0] for c in t.calls])

    def test_each_request_gets_the_remaining_budget(self):
        t = FakeTransport()
        timeouts = []
        inner = t.post

        def record(url, headers, body, timeout):
            timeouts.append(timeout)
            return inner(url, headers, body, timeout)

        t.post = record
        with mock.patch.object(git, "OVERALL_DEADLINE_S", 10):
            code, out, err = run_cli(
                ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
                env={"SIMPLE_TOOLS_BEARER": BEARER},
                transport=t,
            )
        self.assertEqual(code, 0, err)
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < x <= 10 for x in timeouts), timeouts)

    def test_native_path_gets_the_remaining_budget(self):
        native = FakeNative()
        with mock.patch.object(git, "OVERALL_DEADLINE_S", 10):
            code, out, err = run_cli(
                ["list"],
                env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
                native_post=native.post,
            )
        self.assertEqual(code, 0, err)
        self.assertTrue(native.timeouts)
        self.assertTrue(all(0 < x <= 10 for x in native.timeouts), native.timeouts)

    def test_native_timeout_is_the_actual_remaining_budget_without_a_floor(self):
        native = FakeNative()
        with mock.patch.object(git, "OVERALL_DEADLINE_S", 0.5):
            code, out, err = run_cli(
                ["list"],
                env={"SIMPLE_GIT_NATIVE_ECHO": "1"},
                native_post=native.post,
            )
        self.assertEqual(code, 0, err)
        self.assertTrue(native.timeouts)
        self.assertTrue(all(0 < x < 1 for x in native.timeouts), native.timeouts)

    # 4. read1 stays, but a response without it fails closed, not with a traceback.
    def test_response_without_read1_is_a_guarded_transport_failure(self):
        import types

        class FakeSock:
            def settimeout(self, value):
                self.value = value

        class NoRead1Response:
            status = 200
            headers = {"Content-Type": "application/json"}
            fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=FakeSock()))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                return NoRead1Response()

        with mock.patch.object(git.urllib.request, "build_opener", lambda *handlers: FakeOpener()):
            with self.assertRaises(git._TransportError):
                git._remote_post(git.REMOTE_MCP_URL, {}, b"{}", 5)

    # 5. The program name cannot carry newline/tab into stderr lines.
    def test_prog_escapes_newline_and_tab(self):
        with mock.patch.dict(os.environ, {"SIMPLE_GIT_ARGV0": "evil\nname\there"}):
            self.assertEqual(git.prog(), "evil\\nname\\there")

    # 6. Malformed leaf constraints:  clean refusal at discovery, clean False in _type_ok.
    def _schema_refusal(self, schema):
        t = FakeTransport(pages=[[make_tool("gr_wf_repo_get", schema)]])
        code, out, err = run_cli(["list"], env={"SIMPLE_TOOLS_BEARER": BEARER}, transport=t)
        self.assertEqual(code, 1)
        self.assertIn("missing or unreadable schema", err)

    def test_malformed_leaf_bounds_make_the_schema_unreadable(self):
        for bad in (
            {"type": "object", "properties": {"n": {"type": "integer", "minimum": "x"}}},
            {"type": "object", "properties": {"n": {"type": "integer", "minimum": True}}},
            {"type": "object", "properties": {"s": {"type": "string", "minLength": 1.5}}},
            {"type": "object", "properties": {"s": {"type": "string", "pattern": 5}}},
            {"type": "object", "properties": {"s": {"type": "string", "enum": "abc"}}},
            {"type": "object", "properties": {"a": {"type": "array", "items": "x"}}},
            {"type": "object", "anyOf": [{"properties": {"n": {"maximum": "x"}}}]},
        ):
            self._schema_refusal(bad)

    def test_type_ok_guards_return_false_without_a_traceback(self):
        self.assertFalse(git._type_ok({"type": "integer", "minimum": "x"}, 5))
        self.assertFalse(git._type_ok({"type": "integer", "maximum": None or "x"}, 5))
        self.assertFalse(git._type_ok({"type": "string", "minLength": "x"}, "abc"))
        self.assertFalse(git._type_ok({"type": "string", "maxLength": True}, "abc"))
        self.assertFalse(git._type_ok({"type": "string", "enum": "abc"}, "a"))
        self.assertFalse(git._type_ok({"type": "string", "pattern": 5}, "abc"))
        self.assertFalse(git._type_ok({"type": "array", "minItems": "x"}, []))
        # Well-formed leaves still pass.
        self.assertTrue(git._type_ok({"type": "integer", "minimum": 1, "maximum": 9}, 5))
        self.assertTrue(git._type_ok({"type": "string", "minLength": 1, "enum": ["a"]}, "a"))
        self.assertTrue(git._type_ok({"type": "array", "minItems": 0, "maxItems": 2}, [1]))


REAL_SCANNER = Path(os.path.realpath(str(IMPL))).parent.parent / "agent_sync" / "secretscan.py"


@unittest.skipUnless(REAL_SCANNER.is_file(), "scripts/agent_sync/secretscan.py not available here")
class TestRealSharedScanner(unittest.TestCase):
    """The actual fleet scanner, loaded exactly the way the implementation loads it."""

    def _scanner(self):
        loader = importlib.machinery.SourceFileLoader("_real_secretscan", str(REAL_SCANNER))
        spec = importlib.util.spec_from_loader("_real_secretscan", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_plain_text_passes(self):
        self.assertIsNone(self._scanner().scan("an ordinary commit message", known=[BEARER]))

    def test_known_token_is_flagged(self):
        self.assertIsNotNone(self._scanner().scan("the token is " + BEARER, known=[BEARER]))

    def test_cli_end_to_end_with_the_real_scanner(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_issue_create", "--args-json", json.dumps({"title": "token " + BEARER})],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
            scanner="real",
        )
        self.assertEqual(code, 2)
        self.assertIn("secret_scan:", err)
        self.assertNotIn(BEARER, err)
        self.assertEqual([c[0] for c in t.calls], ["identities"])

    def test_cli_clean_call_passes_with_the_real_scanner(self):
        t = FakeTransport()
        code, out, err = run_cli(
            ["call", "gr_wf_repo_get", "--args-json", '{"owner":"o"}'],
            env={"SIMPLE_TOOLS_BEARER": BEARER},
            transport=t,
            scanner="real",
        )
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
