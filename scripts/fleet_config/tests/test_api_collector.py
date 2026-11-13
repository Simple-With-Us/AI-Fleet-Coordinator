"""Contract and adversarial tests for ``fleet_config.api_collector`` (AFC-441).

All tests are hermetic:  no real network, no real home, and no real credentials.

    cd scripts && python3 -m unittest fleet_config.tests.test_api_collector -v
"""

from __future__ import annotations

import builtins
import io
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from fleet_config import aggregator, collectors, contract
from fleet_config import manifest as manifest_mod
from fleet_config.api_collector import (
    _API_BATCH_CACHE_KEY,
    API_OBSERVATION_SCHEMA_VERSION,
    API_OBSERVER_CACHE_KEY,
    API_SIDECAR_CACHE_KEY,
    DEFAULT_USER_AGENT,
    ApiObserverRuntime,
    ApiObserverTarget,
    ObserverClass,
    _build_safe_opener,
    api_collect,
    build_first_cloud_seat_rules,
    evaluate_api_observation,
    http_fetch_observer,
    scrub_secrets,
    validate_observer_url,
)
from fleet_config.checks import CheckContext, CheckError
from fleet_config.types import (
    SEAT_CONFIG_KINDS,
    CheckSpec,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    Manifest,
    ProofLevel,
    ReasonCode,
    Rule,
    SeatManifest,
)


def _cloud_rule(
    rule_id: str = "grok-bot-cloud-1-mcp",
    *,
    kind: ConfigKind = ConfigKind.MCP,
    check_type: CheckType = CheckType.MCP_SERVERS_REGISTERED,
    expected: ExpectedState | None = None,
    proof_level: ProofLevel = ProofLevel.L0,
    ttl_seconds: int = 900,
) -> Rule:
    return Rule(
        id=rule_id,
        kind=kind,
        platforms=("cursor-cloud",),
        check=CheckSpec(type=check_type, target="cloud://registered-subject/mcp"),
        expected=expected or ExpectedState(description="allowlisted mcp", names=("simple-chat",)),
        proof_level=proof_level,
        owner_seat="CODEX",
        autofix_allowed=False,
        collector=Collector.API,
        ttl_seconds=ttl_seconds,
        seat="GROK-BOT",
        source="AFC-441 test",
    )


def _payload(
    *,
    nonce: str = "nonce-123",
    rule_id: str = "grok-bot-cloud-1-mcp",
    seat: str = "GROK-BOT",
    platform: str = "cursor-cloud",
    host_id: str = "cloud-host-1",
    observer_class: str = "owner_observer",
    observed_at: str = "2026-11-12T20:00:00Z",
    expires_at: str = "2026-11-12T20:15:00Z",
    observed: dict | None = None,
    proof: dict | None = None,
    schema_version: object = API_OBSERVATION_SCHEMA_VERSION,
    extra_rows: list | None = None,
) -> bytes:
    row: dict = {
        "observationId": "obs-001",
        "ruleId": rule_id,
        "seat": seat,
        "platform": platform,
        "hostId": host_id,
        "subjectSession": "AA.1.C",
        "observerId": "hetzner-ro-observer-1",
        "observerClass": observer_class,
        "sourceRevision": "aaedc1790",
        "observedAt": observed_at,
        "expiresAt": expires_at,
        "scope": ["cloud"],
        "evidenceRef": "receipt:obs-001",
        "observed": observed if observed is not None else {"present": True, "names": ["simple-chat"]},
    }
    if proof is not None:
        row["proof"] = proof
    rows = [row]
    if extra_rows:
        rows.extend(extra_rows)
    return json.dumps(
        {
            "schemaVersion": schema_version,
            "nonce": nonce,
            "rows": rows,
        }
    ).encode("utf-8")


class _FakeHTTPResponse:
    code = 200
    msg = "OK"

    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.headers: dict[str, str] = {}

    def info(self) -> dict[str, str]:
        return self.headers

    def geturl(self) -> str:
        return "https://observer.example.com/v1"

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class ApiCollectorSecurityTests(unittest.TestCase):
    """Adversarial URL, redirect, retry, schema, identity, replay and secret checks."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="fleet-api-test-")
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 12, 20, 5, 0, tzinfo=timezone.utc)
        self.ctx = CheckContext(home=Path(self.tmp.name), now=self.now, timeout=5)

    def test_unconfigured_api_collector_is_honest_unsupported(self) -> None:
        rule = _cloud_rule()
        res = collectors.collect(rule, "cursor-cloud", self.ctx)
        self.assertIs(res.state, CheckState.UNSUPPORTED)
        self.assertIs(res.reason_code, ReasonCode.UNSUPPORTED)
        self.assertIn("AFC-398", res.reason)

    def test_validate_observer_url_refuses_ssrf_and_insecure_schemes(self) -> None:
        allowed = frozenset(
            {"observer.internal.example", "169.254.169.254", "10.0.0.5", "127.0.0.1"}
        )
        for bad in (
            "",
            "http://observer.internal.example/v1/obs",
            "file:///etc/passwd",
            "https://evil.example/v1/obs",
            "https://user:pass@observer.internal.example/v1/obs",
            "https://observer.internal.example/v1/obs#frag",
            "https://169.254.169.254/latest/meta-data",
            "https://10.0.0.5/v1/obs",
            "https://127.0.0.1/v1/obs",
        ):
            with self.subTest(bad=bad), self.assertRaises(CheckError) as cm:
                validate_observer_url(bad, allowed_hosts=allowed)
            self.assertIs(cm.exception.reason_code, ReasonCode.ERROR)

        # Explicit allowlisted https host succeeds.
        self.assertEqual(
            validate_observer_url(
                "https://observer.internal.example/v1/obs",
                allowed_hosts=allowed,
            ),
            "https://observer.internal.example/v1/obs",
        )
        # Private and loopback targets require explicit opt-in flags.
        self.assertEqual(
            validate_observer_url(
                "https://10.0.0.5/v1/obs",
                allowed_hosts=allowed,
                allow_private_ips=True,
            ),
            "https://10.0.0.5/v1/obs",
        )
        self.assertEqual(
            validate_observer_url(
                "http://127.0.0.1/v1/obs",
                allowed_hosts=allowed,
                allow_insecure_loopback=True,
            ),
            "http://127.0.0.1/v1/obs",
        )

    def test_http_fetch_refuses_dns_resolution_to_metadata_or_private_ip(self) -> None:
        for ip_addr in ("169.254.169.254", "10.0.0.5", "127.0.0.1"):
            with (
                self.subTest(ip_addr=ip_addr),
                mock.patch(
                    "fleet_config.api_collector.socket.getaddrinfo",
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip_addr, 443))],
                ),
            ):
                with self.assertRaises(CheckError) as cm:
                    http_fetch_observer(
                        "https://observer.example.com/v1/obs",
                        {},
                        5,
                        1024,
                        allowed_hosts=frozenset({"observer.example.com"}),
                    )
                self.assertIs(cm.exception.reason_code, ReasonCode.ERROR)

    def _handler_opener(self, handler_fn: Any) -> urllib.request.OpenerDirector:
        class _StubHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req: urllib.request.Request) -> Any:  # noqa: N802
                return handler_fn(req)

        return urllib.request.build_opener(_StubHTTPSHandler())

    def test_http_fetch_refuses_redirects_and_never_retries_auth_or_404(self) -> None:
        calls = {"n": 0}

        def _redirect_handler(_req: urllib.request.Request) -> Any:
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "https://observer.example.com/v1",
                302,
                "Found",
                None,
                io.BytesIO(b"secret-body-should-not-leak"),
            )

        with self.assertRaises(CheckError) as cm:
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                5,
                1024,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=self._handler_opener(_redirect_handler),
                resolve_dns=False,
            )
        self.assertIs(cm.exception.reason_code, ReasonCode.ERROR)
        self.assertEqual(calls["n"], 1)

        for code in (401, 403, 404):
            calls["n"] = 0

            def _status_handler(_req: urllib.request.Request, _code: int = code) -> Any:
                calls["n"] += 1
                raise urllib.error.HTTPError(
                    "https://observer.example.com/v1",
                    _code,
                    "err",
                    None,
                    io.BytesIO(b"RAW-UPSTREAM-ERROR-BODY-DO-NOT-COPY"),
                )

            with self.subTest(code=code), self.assertRaises(CheckError) as cm2:
                http_fetch_observer(
                    "https://observer.example.com/v1",
                    {},
                    5,
                    1024,
                    allowed_hosts=frozenset({"observer.example.com"}),
                    opener=self._handler_opener(_status_handler),
                    resolve_dns=False,
                )
            self.assertIs(cm2.exception.reason_code, ReasonCode.ERROR)
            self.assertEqual(calls["n"], 1)
            self.assertNotIn("RAW-UPSTREAM-ERROR-BODY", str(cm2.exception))

    def test_http_fetch_retries_once_on_429_and_5xx_and_rejects_oversized_and_timeouts(self) -> None:
        calls = {"n": 0}

        def _transient_then_ok(_req: urllib.request.Request) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("https://observer.example.com/v1", 503, "busy", None, None)
            return _FakeHTTPResponse(b'{"ok":true}')

        data = http_fetch_observer(
            "https://observer.example.com/v1",
            {},
            5,
            1024,
            allowed_hosts=frozenset({"observer.example.com"}),
            opener=self._handler_opener(_transient_then_ok),
            resolve_dns=False,
        )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(data, b'{"ok":true}')

        with self.assertRaises(CheckError) as cm_big:
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                5,
                64,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=self._handler_opener(lambda _req: _FakeHTTPResponse(b"x" * 65)),
                resolve_dns=False,
            )
        self.assertIs(cm_big.exception.reason_code, ReasonCode.UNPARSEABLE)

        def _timeout_handler(_req: urllib.request.Request) -> Any:
            raise TimeoutError("timed out")

        with self.assertRaises(CheckError) as cm_to:
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                2,
                64,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=self._handler_opener(_timeout_handler),
                resolve_dns=False,
            )
        self.assertIs(cm_to.exception.reason_code, ReasonCode.TIMEOUT)

    def test_self_report_never_produces_pass_or_l1_proof(self) -> None:
        rule = _cloud_rule(proof_level=ProofLevel.L1, kind=ConfigKind.HOOKS, check_type=CheckType.HOOK_REGISTERED)
        raw = _payload(
            observer_class="self_report",
            observed={"present": True, "names": ["simple-chat"]},
            proof={"status": "passed", "level": "L1"},
        )
        result, sidecar = evaluate_api_observation(
            rule,
            "cursor-cloud",
            raw,
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.ERROR)
        self.assertIsNone(sidecar)
        self.assertTrue(contract.is_failing(result))

    def test_replay_nonce_mismatch_duplicate_rows_spoof_and_schema_refusal(self) -> None:
        rule = _cloud_rule()
        # Wrong nonce -> ERROR
        res_nonce, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(nonce="wrong-nonce"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_nonce.state, CheckState.UNKNOWN)
        self.assertIs(res_nonce.reason_code, ReasonCode.ERROR)

        # Unsupported schemaVersion -> UNPARSEABLE
        res_schema, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(schema_version=99),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_schema.state, CheckState.UNKNOWN)
        self.assertIs(res_schema.reason_code, ReasonCode.UNPARSEABLE)

        # NaN constant -> UNPARSEABLE
        res_nan, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            b'{"schemaVersion": 1, "nonce": "nonce-123", "rows": [], "extra": NaN}',
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_nan.state, CheckState.UNKNOWN)
        self.assertIs(res_nan.reason_code, ReasonCode.UNPARSEABLE)

        # Duplicate identity rows -> UNPARSEABLE
        dup_row = json.loads(_payload().decode("utf-8"))["rows"][0]
        res_dup, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(extra_rows=[dup_row]),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_dup.state, CheckState.UNKNOWN)
        self.assertIs(res_dup.reason_code, ReasonCode.UNPARSEABLE)

        # Spoofed hostId (missing matching row) -> UNKNOWN/error, NEVER MISSING
        res_spoof, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(host_id="other-host"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_spoof.state, CheckState.UNKNOWN)
        self.assertIs(res_spoof.reason_code, ReasonCode.ERROR)

    def test_stale_and_future_timestamps_fail_closed_preserving_checked_at(self) -> None:
        rule = _cloud_rule(ttl_seconds=300)
        # Observed 10 minutes ago, local ttl=300 => expired even if remote claims longer expiry.
        res_stale, sidecar = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(
                observed_at="2026-11-12T19:55:00Z",
                expires_at="2026-11-12T20:30:00Z",
            ),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_stale.state, CheckState.UNKNOWN)
        self.assertIs(res_stale.reason_code, ReasonCode.STALE)
        self.assertTrue(res_stale.stale)
        self.assertEqual(res_stale.checked_at, "2026-11-12T19:55:00Z")
        self.assertEqual(res_stale.expires_at, "2026-11-12T20:00:00Z")
        self.assertIsNotNone(sidecar)

        # Future observedAt (> skew) -> ERROR
        future = self.now + timedelta(hours=1)
        res_future, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(
                observed_at=future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                expires_at=(future + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_future.state, CheckState.UNKNOWN)
        self.assertIs(res_future.reason_code, ReasonCode.ERROR)

    def test_secret_canary_is_scrubbed_from_all_outputs(self) -> None:
        canary = "SECRET-CANARY-XYZ-9988"
        rule = _cloud_rule()
        raw = _payload(
            observed={
                "present": True,
                "names": ["simple-chat", f"token={canary}"],
                "sha256": f"Bearer {canary}",
            }
        )
        target = ApiObserverTarget(
            service_id="afc-398-observer",
            base_url="https://observer.example.com/v1/observations",
            allowed_hosts=frozenset({"observer.example.com"}),
            expected_host_id="cloud-host-1",
        )
        runtime = ApiObserverRuntime(
            target=target,
            fetcher=lambda *_args: raw,
            nonce_factory=lambda: "nonce-123",
            secret_canaries=(canary,),
            manifest_digest="a" * 64,
        )
        ctx = CheckContext(
            home=Path(self.tmp.name),
            now=self.now,
            timeout=5,
            cache={API_OBSERVER_CACHE_KEY: runtime},
        )
        result = api_collect(rule, "cursor-cloud", ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        serialized = json.dumps(contract.build_status_envelope(
            aggregator.run_status(
                Manifest(
                    schema_version=2,
                    seats=(SeatManifest(seat="GROK-BOT", platforms=("cursor-cloud",), rules=(rule,)),),
                    compliance_rules=(),
                    source_path="",
                ),
                ctx=ctx,
            )
        ))
        self.assertNotIn(canary, serialized)
        sidecars = ctx.cache[API_SIDECAR_CACHE_KEY]
        self.assertTrue(sidecars)
        self.assertNotIn(canary, json.dumps([s.to_dict() for s in sidecars]))
        self.assertEqual(contract.SCHEMA_VERSION, 1)
        self.assertIn("[REDACTED]", scrub_secrets(f"Bearer {canary}", (canary,)))

    def test_first_cloud_seat_rules_and_aggregator_integration(self) -> None:
        digest = "b" * 64
        rules = build_first_cloud_seat_rules(
            seat="GROK-BOT",
            platform="cursor-cloud",
            host_id="cloud-host-1",
            owner_seat="CODEX",
            rules_target="cloud://registered-subject/AGENTS.md",
            rules_sha256=digest,
            hooks_target="cloud://registered-subject/hooks.json",
            hook_names=("lane-guard",),
            mcp_target="cloud://registered-subject/mcp.json",
            mcp_names=("simple-chat",),
            skills_target="cloud://registered-subject/skills",
            skill_names=("fleet-guard",),
            subagents_target="cloud://registered-subject/subagents",
            subagent_names=("research",),
            permissions_target="cloud://registered-subject/permissions.json",
            permission_names=("read-only",),
            hook_proof_level=ProofLevel.L1,
        )
        self.assertEqual({r.kind for r in rules}, set(SEAT_CONFIG_KINDS))
        for r in rules:
            self.assertIs(r.collector, Collector.API)
            self.assertFalse(r.autofix_allowed)

        # Subset semantics for skills passes with extra installed skills, fails when missing one.
        skills_rule = next(r for r in rules if r.kind is ConfigKind.SKILLS)
        hooks_rule = next(r for r in rules if r.kind is ConfigKind.HOOKS)
        responses = {
            skills_rule.id: _payload(
                rule_id=skills_rule.id,
                observer_class=ObserverClass.PROVIDER_API.value,
                observed={"present": True, "names": ["fleet-guard", "extra-skill"]},
            ),
            # Hooks L1 without proof receipt -> UNPROVEN (and never runs local Mac proof.py).
            hooks_rule.id: _payload(
                rule_id=hooks_rule.id,
                observer_class=ObserverClass.OWNER_OBSERVER.value,
                observed={"present": True, "names": ["lane-guard"]},
            ),
        }

        def fake_fetch(url: str, *_args: object) -> bytes:
            for rid, payload in responses.items():
                if rid in url:
                    return payload
            raise AssertionError("unexpected url: " + url)

        target = ApiObserverTarget(
            service_id="afc-398-observer",
            base_url="https://observer.example.com/v1/obs",
            allowed_hosts=frozenset({"observer.example.com"}),
            expected_host_id="cloud-host-1",
        )
        runtime = ApiObserverRuntime(
            target=target,
            fetcher=fake_fetch,
            nonce_factory=lambda: "nonce-123",
        )
        ctx = CheckContext(
            home=Path(self.tmp.name),
            now=self.now,
            timeout=5,
            cache={API_OBSERVER_CACHE_KEY: runtime},
        )
        manifest = Manifest(
            schema_version=2,
            seats=(SeatManifest(seat="GROK-BOT", platforms=("cursor-cloud",), rules=(skills_rule, hooks_rule)),),
            compliance_rules=(),
            source_path="",
        )
        with mock.patch("fleet_config.aggregator._run_proof", side_effect=AssertionError("mac proof must not run")):
            report = aggregator.run_status(manifest, ctx=ctx)
        by_id = {res.rule_id: res for res in report.results}
        self.assertIs(by_id[skills_rule.id].state, CheckState.PASS)
        self.assertIs(by_id[hooks_rule.id].state, CheckState.UNPROVEN)
        self.assertIs(by_id[hooks_rule.id].reason_code, ReasonCode.PROOF_NOT_RUN)
        # collectors.collect routes configured Collector.API through api_collector too.
        self.assertIs(collectors.collect(skills_rule, "cursor-cloud", ctx).state, CheckState.PASS)

    def test_missing_observed_and_unconstrained_rules_never_pass(self) -> None:
        # Missing or non-mapping observed block -> UNKNOWN / UNPARSEABLE, even with empty constraints.
        empty_rule = _cloud_rule(
            rule_id="grok-bot-cloud-1-mcp-empty",
            expected=ExpectedState(description="No constraints set"),
        )
        raw_obj = json.loads(_payload(rule_id=empty_rule.id).decode("utf-8"))
        del raw_obj["rows"][0]["observed"]
        res_missing, _ = evaluate_api_observation(
            empty_rule,
            "cursor-cloud",
            json.dumps(raw_obj).encode("utf-8"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_missing.state, CheckState.UNKNOWN)
        self.assertIs(res_missing.reason_code, ReasonCode.UNPARSEABLE)

        # Present observed mapping with unconstrained ExpectedState -> DRIFT (never defaults to PASS).
        res_unconstrained, _ = evaluate_api_observation(
            empty_rule,
            "cursor-cloud",
            _payload(rule_id=empty_rule.id, observed={"present": True}),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_unconstrained.state, CheckState.DRIFT)
        self.assertIs(res_unconstrained.reason_code, ReasonCode.DRIFT)

        # expected.present=True with observed.present missing (None) -> DRIFT, never PASS.
        present_rule = _cloud_rule(
            rule_id="grok-bot-cloud-1-hooks-present",
            expected=ExpectedState(description="Expected present=True", present=True),
        )
        res_unconfirmed, _ = evaluate_api_observation(
            present_rule,
            "cursor-cloud",
            _payload(rule_id=present_rule.id, observed={}),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_unconfirmed.state, CheckState.DRIFT)
        self.assertIs(res_unconfirmed.reason_code, ReasonCode.DRIFT)

    def test_custom_opener_strips_redirect_handler_and_sets_user_agent(self) -> None:
        observed_headers: list[str | None] = []

        class _RedirectOrOkHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib BaseHTTPRequestHandler method
                observed_headers.append(self.headers.get("User-Agent"))
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "https://evil.example/steal")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args: object) -> None:
                return None

        server = HTTPServer(("127.0.0.1", 0), _RedirectOrOkHandler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            custom = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
            with self.assertRaises(CheckError) as cm:
                http_fetch_observer(
                    f"http://127.0.0.1:{port}/redir",
                    {},
                    5,
                    1024,
                    allowed_hosts=frozenset({"127.0.0.1"}),
                    allow_insecure_loopback=True,
                    opener=custom,
                    resolve_dns=False,
                )
            self.assertIs(cm.exception.reason_code, ReasonCode.ERROR)
            self.assertIn("redirect", str(cm.exception).lower())

            body = http_fetch_observer(
                f"http://127.0.0.1:{port}/ok",
                {},
                5,
                1024,
                allowed_hosts=frozenset({"127.0.0.1"}),
                allow_insecure_loopback=True,
                opener=custom,
                resolve_dns=False,
            )
            self.assertEqual(body, b'{"ok":true}')
            self.assertEqual(observed_headers, [DEFAULT_USER_AGENT, DEFAULT_USER_AGENT])

            custom_body = http_fetch_observer(
                f"http://127.0.0.1:{port}/ok",
                {"User-Agent": "custom-client/2.0"},
                5,
                1024,
                allowed_hosts=frozenset({"127.0.0.1"}),
                allow_insecure_loopback=True,
                resolve_dns=False,
            )
            self.assertEqual(custom_body, b'{"ok":true}')
            self.assertEqual(observed_headers[-1], "custom-client/2.0")
        finally:
            server.shutdown()
            server.server_close()

    def test_dns_pinning_prevents_rebinding_between_verify_and_connect(self) -> None:
        seen_hosts: list[str | None] = []

        class _OkHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen_hosts.append(self.headers.get("Host"))
                self.send_response(200)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"pinned":true}')

            def log_message(self, *_args: object) -> None:
                return None

        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_getaddrinfo = socket.getaddrinfo
        hostname_calls = {"n": 0}

        def _rebinding_getaddrinfo(host, port_arg, family=0, type=0, proto=0, flags=0):  # noqa: A002, ANN001
            if host == "localhost" and not (flags & socket.AI_NUMERICHOST):
                hostname_calls["n"] += 1
                if hostname_calls["n"] == 1:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
            return real_getaddrinfo(host, port_arg, family, type, proto, flags)

        try:
            with mock.patch("fleet_config.api_collector.socket.getaddrinfo", side_effect=_rebinding_getaddrinfo):
                body = http_fetch_observer(
                    f"http://localhost:{port}/v1/obs",
                    {},
                    5,
                    1024,
                    allowed_hosts=frozenset({"localhost"}),
                    allow_insecure_loopback=True,
                    resolve_dns=True,
                )
            self.assertEqual(body, b'{"pinned":true}')
            self.assertEqual(hostname_calls["n"], 1)
            self.assertEqual(seen_hosts, [f"localhost:{port}"])
        finally:
            server.shutdown()
            server.server_close()

    def test_compliance_empty_seat_row_accepted_and_missing_seat_rejected(self) -> None:
        compliance_rule = Rule(
            id="compliance-cloud-seat-api-observer",
            kind=ConfigKind.MCP,
            platforms=("cursor-cloud",),
            check=CheckSpec(type=CheckType.MCP_SERVERS_REGISTERED, target="cloud://observer"),
            expected=ExpectedState(description="compliance observer", names=("simple-chat",)),
            proof_level=ProofLevel.L0,
            owner_seat="CODEX",
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=900,
            seat="",
            scope=("cloud",),
            source="AFC-441",
        )
        res_ok, sidecar = evaluate_api_observation(
            compliance_rule,
            "cursor-cloud",
            _payload(rule_id=compliance_rule.id, seat=""),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_ok.state, CheckState.PASS)
        self.assertIsNotNone(sidecar)

        missing_seat_doc = json.loads(_payload(rule_id=compliance_rule.id, seat="").decode("utf-8"))
        del missing_seat_doc["rows"][0]["seat"]
        res_missing_key, _ = evaluate_api_observation(
            compliance_rule,
            "cursor-cloud",
            json.dumps(missing_seat_doc).encode("utf-8"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
        )
        self.assertIs(res_missing_key.state, CheckState.UNKNOWN)
        self.assertIs(res_missing_key.reason_code, ReasonCode.UNPARSEABLE)

    def test_canaries_scrubbed_on_bad_schema_unknown_observer_and_sidecar_scope(self) -> None:
        canary = "SECRET-TOKEN-9911"
        rule = _cloud_rule()

        res_schema, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(schema_version=f"v-{canary}"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
            secret_canaries=(canary,),
        )
        self.assertIs(res_schema.reason_code, ReasonCode.UNPARSEABLE)
        self.assertNotIn(canary, res_schema.reason)

        res_obs, _ = evaluate_api_observation(
            rule,
            "cursor-cloud",
            _payload(observer_class=f"custom-{canary}"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
            secret_canaries=(canary,),
        )
        self.assertIs(res_obs.reason_code, ReasonCode.ERROR)
        self.assertNotIn(canary, res_obs.reason)

        doc = json.loads(_payload().decode("utf-8"))
        doc["rows"][0]["scope"] = [f"cloud-{canary}"]
        _, sidecar = evaluate_api_observation(
            rule,
            "cursor-cloud",
            json.dumps(doc).encode("utf-8"),
            expected_nonce="nonce-123",
            expected_host_id="cloud-host-1",
            ctx=self.ctx,
            secret_canaries=(canary,),
        )
        self.assertIsNotNone(sidecar)
        assert sidecar is not None
        self.assertNotIn(canary, json.dumps(sidecar.to_dict()))

    def test_l2_hook_rule_clamps_ttl_and_passes_manifest_validation(self) -> None:
        rules = build_first_cloud_seat_rules(
            seat="GROK-BOT",
            platform="cursor-cloud",
            host_id="cloud-host-1",
            owner_seat="CODEX",
            rules_target="cloud://registered-subject/AGENTS.md",
            rules_sha256="b" * 64,
            hooks_target="cloud://registered-subject/hooks.json",
            hook_names=("lane-guard",),
            mcp_target="cloud://registered-subject/mcp.json",
            mcp_names=("simple-chat",),
            skills_target="cloud://registered-subject/skills",
            skill_names=("fleet-guard",),
            subagents_target="cloud://registered-subject/subagents",
            subagent_names=("research",),
            permissions_target="cloud://registered-subject/permissions.json",
            permission_names=("read-only",),
            hook_proof_level=ProofLevel.L2,
            ttl_seconds=900,
        )
        hooks_rule = next(r for r in rules if r.kind is ConfigKind.HOOKS)
        self.assertGreaterEqual(hooks_rule.ttl_seconds, 86400)
        manifest = Manifest(
            schema_version=2,
            seats=(SeatManifest(seat="GROK-BOT", platforms=("cursor-cloud",), rules=rules),),
            compliance_rules=(),
            source_path="",
        )
        manifest_mod.validate_rules(manifest)

    def test_api_collect_batches_multi_row_host_and_caches_host_failures(self) -> None:
        rules = build_first_cloud_seat_rules(
            seat="GROK-BOT",
            platform="cursor-cloud",
            host_id="cloud-host-1",
            owner_seat="CODEX",
            rules_target="cloud://registered-subject/AGENTS.md",
            rules_sha256="b" * 64,
            hooks_target="cloud://registered-subject/hooks.json",
            hook_names=("lane-guard",),
            mcp_target="cloud://registered-subject/mcp.json",
            mcp_names=("simple-chat",),
            skills_target="cloud://registered-subject/skills",
            skill_names=("fleet-guard",),
            subagents_target="cloud://registered-subject/subagents",
            subagent_names=("research",),
            permissions_target="cloud://registered-subject/permissions.json",
            permission_names=("read-only",),
        )
        base_rows = [
            {
                "observationId": f"obs-{i}",
                "ruleId": r.id,
                "seat": "GROK-BOT",
                "platform": "cursor-cloud",
                "hostId": "cloud-host-1",
                "observerId": "hetzner-ro-observer-1",
                "observerClass": "owner_observer",
                "observedAt": "2026-11-12T20:00:00Z",
                "expiresAt": "2026-11-12T20:15:00Z",
                "observed": {
                    "present": True,
                    "sha256": r.expected.sha256,
                    "names": list(r.expected.names),
                },
            }
            for i, r in enumerate(rules)
        ]
        fetch_calls = {"n": 0}

        def batch_fetch(url: str, *_args: object) -> bytes:
            fetch_calls["n"] += 1
            self.assertNotIn("ruleId=", url)
            return json.dumps(
                {"schemaVersion": 1, "nonce": "nonce-123", "rows": base_rows}
            ).encode("utf-8")

        target = ApiObserverTarget(
            service_id="afc-398-observer",
            base_url="https://observer.example.com/v1/obs",
            allowed_hosts=frozenset({"observer.example.com"}),
            expected_host_id="cloud-host-1",
            batch_by_host=True,
        )
        runtime = ApiObserverRuntime(
            target=target,
            fetcher=batch_fetch,
            nonce_factory=lambda: "nonce-123",
        )
        ctx = CheckContext(
            home=Path(self.tmp.name),
            now=self.now,
            timeout=5,
            cache={API_OBSERVER_CACHE_KEY: runtime},
        )
        manifest = Manifest(
            schema_version=2,
            seats=(SeatManifest(seat="GROK-BOT", platforms=("cursor-cloud",), rules=rules),),
            compliance_rules=(),
            source_path="",
        )
        report = aggregator.run_status(manifest, ctx=ctx)
        self.assertEqual(fetch_calls["n"], 1)
        self.assertTrue(all(res.state is CheckState.PASS for res in report.results))

        # Host failure short-circuits remaining rules in the same run after one fetch.
        err_calls = {"n": 0}

        def failing_fetch(*_args: object) -> bytes:
            err_calls["n"] += 1
            raise CheckError("timeout", ReasonCode.TIMEOUT)

        err_runtime = ApiObserverRuntime(
            target=target,
            fetcher=failing_fetch,
            nonce_factory=lambda: "nonce-123",
        )
        err_ctx = CheckContext(
            home=Path(self.tmp.name),
            now=self.now,
            timeout=5,
            cache={API_OBSERVER_CACHE_KEY: err_runtime},
        )
        err_report = aggregator.run_status(manifest, ctx=err_ctx)
        self.assertEqual(err_calls["n"], 1)
        self.assertTrue(all(res.reason_code is ReasonCode.TIMEOUT for res in err_report.results))

    def test_aggregator_catches_api_collector_import_error(self) -> None:
        rule = _cloud_rule()
        real_import = builtins.__import__

        def _fail_api_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002, ANN001
            if "api_collector" in name or (fromlist and "api_collector" in fromlist):
                raise ImportError("simulated broken api_collector import")
            return real_import(name, globals, locals, fromlist, level)

        manifest = Manifest(
            schema_version=2,
            seats=(SeatManifest(seat="GROK-BOT", platforms=("cursor-cloud",), rules=(rule,)),),
            compliance_rules=(),
            source_path="",
        )
        with mock.patch("builtins.__import__", side_effect=_fail_api_import):
            report = aggregator.run_status(manifest, ctx=self.ctx)
        self.assertEqual(len(report.results), 1)
        self.assertIs(report.results[0].state, CheckState.UNKNOWN)
        self.assertIs(report.results[0].reason_code, ReasonCode.ERROR)

    def test_custom_opener_open_override_rejected_and_ambient_proxies_neutralized(self) -> None:
        class _CustomOpenOpener(urllib.request.OpenerDirector):
            def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
                raise AssertionError("custom open override must be refused")

        with self.assertRaises(TypeError):
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                5,
                1024,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=_CustomOpenOpener(),
                resolve_dns=False,
            )

        with mock.patch.dict(
            os.environ,
            {"https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"},
            clear=False,
        ):
            unpinned = _build_safe_opener(None, pinned_host="observer.example.com", pinned_ips=())
            self.assertFalse(
                any(isinstance(h, urllib.request.ProxyHandler) for h in unpinned.handlers)
            )
            self.assertFalse(
                any(
                    isinstance(h, urllib.request.ProxyHandler)
                    for h in unpinned.process_request.get("https", [])
                )
            )
            self.assertFalse(
                any(
                    isinstance(h, urllib.request.ProxyHandler)
                    for h in unpinned.process_request.get("http", [])
                )
            )

            copied = _build_safe_opener(
                urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://127.0.0.1:9"})),
                pinned_host="observer.example.com",
                pinned_ips=("93.184.216.34",),
            )
            self.assertFalse(
                any(isinstance(h, urllib.request.ProxyHandler) for h in copied.handlers)
            )

    def test_non_batched_api_collect_skips_host_batch_payload_caching_and_dns_timeout_fails_closed(self) -> None:
        rule = _cloud_rule()
        target = ApiObserverTarget(
            service_id="obs",
            base_url="https://observer.example.com/v1/obs",
            expected_host_id="cloud-host-1",
            allowed_hosts=frozenset({"observer.example.com"}),
            batch_by_host=False,
        )
        raw = _payload(rule_id=rule.id, observed={"present": True, "names": ["simple-chat"]})
        runtime = ApiObserverRuntime(
            target=target,
            fetcher=lambda *_args: raw,
            nonce_factory=lambda: "nonce-123",
        )
        ctx = CheckContext(
            home=Path(self.tmp.name),
            now=self.now,
            timeout=5,
            cache={API_OBSERVER_CACHE_KEY: runtime},
        )
        res = api_collect(rule, "cursor-cloud", ctx)
        self.assertIs(res.state, CheckState.PASS)
        batch_map = ctx.cache.get(_API_BATCH_CACHE_KEY, {})
        self.assertTrue(all(not batch.entries for batch in batch_map.values()))

        gate = threading.Event()

        def _slow_getaddrinfo(*_args: object, **_kwargs: object) -> list[Any]:
            gate.wait(2.0)
            return []

        try:
            with mock.patch("fleet_config.api_collector.socket.getaddrinfo", side_effect=_slow_getaddrinfo):
                with self.assertRaises(CheckError) as cm:
                    http_fetch_observer(
                        "https://observer.example.com/v1/obs",
                        {},
                        timeout=0.05,
                        max_bytes=1024,
                        allowed_hosts=frozenset({"observer.example.com"}),
                        resolve_dns=True,
                    )
            self.assertIs(cm.exception.reason_code, ReasonCode.TIMEOUT)
        finally:
            gate.set()


if __name__ == "__main__":
    unittest.main()
