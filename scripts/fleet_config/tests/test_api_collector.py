"""Contract and adversarial tests for ``fleet_config.api_collector`` (AFC-441).

All tests are hermetic:  no real network, no real home, and no real credentials.

    cd scripts && python3 -m unittest fleet_config.tests.test_api_collector -v
"""

from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from fleet_config import aggregator, collectors, contract
from fleet_config.api_collector import (
    API_OBSERVATION_SCHEMA_VERSION,
    API_OBSERVER_CACHE_KEY,
    API_SIDECAR_CACHE_KEY,
    ApiObserverRuntime,
    ApiObserverTarget,
    ObserverClass,
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
    ObservedState,
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
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def __enter__(self) -> "_FakeHTTPResponse":
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
        allowed = frozenset({"observer.internal.example", "169.254.169.254", "10.0.0.5"})
        for bad in (
            "",
            "http://observer.internal.example/v1/obs",
            "file:///etc/passwd",
            "https://evil.example/v1/obs",
            "https://user:pass@observer.internal.example/v1/obs",
            "https://observer.internal.example/v1/obs#frag",
            "https://169.254.169.254/latest/meta-data",
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

    def test_http_fetch_refuses_dns_resolution_to_metadata_or_private_ip(self) -> None:
        with mock.patch(
            "fleet_config.api_collector.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
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

    def test_http_fetch_refuses_redirects_and_never_retries_auth_or_404(self) -> None:
        calls = {"n": 0}

        class _RedirectOpener(urllib.request.OpenerDirector):
            def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
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
                opener=_RedirectOpener(),
                resolve_dns=False,
            )
        self.assertIs(cm.exception.reason_code, ReasonCode.ERROR)
        self.assertEqual(calls["n"], 1)

        for code in (401, 403, 404):
            calls["n"] = 0

            class _StatusOpener(urllib.request.OpenerDirector):
                def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
                    calls["n"] += 1
                    raise urllib.error.HTTPError(
                        "https://observer.example.com/v1",
                        code,
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
                    opener=_StatusOpener(),
                    resolve_dns=False,
                )
            self.assertIs(cm2.exception.reason_code, ReasonCode.ERROR)
            self.assertEqual(calls["n"], 1)
            self.assertNotIn("RAW-UPSTREAM-ERROR-BODY", str(cm2.exception))

    def test_http_fetch_retries_once_on_429_and_5xx_and_rejects_oversized_and_timeouts(self) -> None:
        calls = {"n": 0}

        class _TransientThenOk(urllib.request.OpenerDirector):
            def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
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
            opener=_TransientThenOk(),
            resolve_dns=False,
        )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(data, b'{"ok":true}')

        class _OversizedOpener(urllib.request.OpenerDirector):
            def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
                return _FakeHTTPResponse(b"x" * 65)

        with self.assertRaises(CheckError) as cm_big:
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                5,
                64,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=_OversizedOpener(),
                resolve_dns=False,
            )
        self.assertIs(cm_big.exception.reason_code, ReasonCode.UNPARSEABLE)

        class _TimeoutOpener(urllib.request.OpenerDirector):
            def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
                raise TimeoutError("timed out")

        with self.assertRaises(CheckError) as cm_to:
            http_fetch_observer(
                "https://observer.example.com/v1",
                {},
                2,
                64,
                allowed_hosts=frozenset({"observer.example.com"}),
                opener=_TimeoutOpener(),
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


if __name__ == "__main__":
    unittest.main()
