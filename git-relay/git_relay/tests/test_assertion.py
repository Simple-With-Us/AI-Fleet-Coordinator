"""Hosted-OAuth assertion verifier (assertion.py) and its policy integration.

Fakes only:  no network, no real key.  The transport and key source are
injected;  time is real but tokens are synthetic.  The Worker-minted
cross-language vector pins the exact claims format
scripts/agent-sync-mcp/src/gr-assert.js must keep producing (its
gr-assert.test.mjs pins the same token).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest
import unittest.mock

from git_relay.assertion import GrAssertionVerifier, parse_claims
from git_relay.policy import PolicyError, SeatPolicy
from git_relay.shared_bearer import SharedBearerVerifier

KEY = "k" * 48
SEAT = "CODEX"
VERIFY_URL = "https://agents.example/internal/gr-assert-verify"
BEARER_VERIFY_URL = "https://agents.example/internal/gr-bearer-verify"
BODY = json.dumps({"repo": "example/x"}, separators=(",", ":"))
BODY_SHA = hashlib.sha256(BODY.encode()).hexdigest()

# Pinned cross-language vector (gr-assert.test.mjs pins the same token):
# key "test-assert-key-0123456789abcdef0123", seat CODEX, action open_pr,
# body sha256 of '{"repo":"example/x"}', iat 1800000000, exp 1800000090,
# jti "AbCdEfGhIjKlMnOp", epoch 7, approved_at 1799999000000, phase 2,
# grant_ref "0123456789ab", scopes ["zulip:read", "zulip:write"].
VECTOR_KEY = "test-assert-key-0123456789abcdef0123"
VECTOR_BODY_SHA = "3552dcd2b3e97a13af54e33d560bafd9f79836dfecb0bd03766627077fe1f259"
VECTOR_TOKEN = (
    "gra2_eyJ2IjoiZ3ItYXNzZXJ0LzIiLCJhdWQiOiJnaXQtcmVsYXkiLCJzZWF0IjoiQ09ERVgiLCJpYXQiOjE4MDAwMDAwMDAsImV4cCI6MTgwMDAwMDA5"
    "MCwianRpIjoiQWJDZEVmR2hJaktsTW5PcCIsImFjdGlvbiI6Im9wZW5fcHIiLCJib2R5IjoiMzU1MmRjZDJiM2U5N2ExM2FmNTRlMzNkNTYwYmFmZDlmN"
    "zk4MzZkZmVjYjBiZDAzNzY2NjI3MDc3ZmUxZjI1OSIsInNjb3BlcyI6WyJ6dWxpcDpyZWFkIiwienVsaXA6d3JpdGUiXSwiZXBvY2giOjcsImFwcHJvdm"
    "VkX2F0IjoxNzk5OTk5MDAwMDAwLCJwaGFzZSI6MiwiZ3JhbnRfcmVmIjoiMDEyMzQ1Njc4OWFiIn0"
    "_f2f6326fe364c4ee341414eb7435cbd7b902ca34b62c8753ba985e20b71dd500"
)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint(
    key=KEY,
    seat=SEAT,
    iat=None,
    ttl=90,
    jti="0123456789abcdef",
    action="open_pr",
    body_sha=BODY_SHA,
    scopes=("zulip:read", "zulip:write"),
    epoch=7,
    approved_at=None,
    phase=2,
    grant_ref="0123456789ab",
):
    """A synthetic gra2_ token, claims in the Worker's canonical key order."""
    iat = int(time.time()) if iat is None else iat
    claims = {
        "v": "gr-assert/2",
        "aud": "git-relay",
        "seat": seat,
        "iat": iat,
        "exp": iat + ttl,
        "jti": jti,
        "action": action,
        "body": body_sha,
        "scopes": list(scopes),
        "epoch": epoch,
        "approved_at": int(time.time() * 1000) if approved_at is None else approved_at,
        "phase": phase,
        "grant_ref": grant_ref,
    }
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"gra2_{payload}_{sig}", claims


def proof(claims, **over):
    p = {"action": claims["action"], "body_sha256": claims["body"]}
    p.update(over)
    return p


class FakeKeySource:
    def __init__(self, values):
        self.values = values

    def secret(self, name):
        return self.values[name]


def make_transport(behavior):
    """behavior(body: bytes) -> (status, payload).  Records calls."""
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return behavior(body)

    transport.calls = calls
    return transport


def ok_answer(seat=SEAT):
    def behavior(body):
        return 200, json.dumps({"ok": True, "seat": seat}).encode()

    return behavior


def verifier(behavior=None, key=KEY):
    return GrAssertionVerifier(
        url=VERIFY_URL,
        key_name="GR_BEARER_VERIFY_KEY",
        key_source=FakeKeySource({"GR_BEARER_VERIFY_KEY": key}),
        transport=make_transport(behavior or ok_answer()),
    )


class ParseTest(unittest.TestCase):
    def test_pinned_worker_vector_parses_to_the_exact_claims(self):
        # The vector's window is 2027-01-15 +/-:  parse inside it.
        with unittest.mock.patch("git_relay.assertion.time") as clock:
            clock.time.return_value = 1800000045.0
            claims = parse_claims(VECTOR_TOKEN)
        self.assertIsNotNone(claims)
        self.assertEqual(
            claims,
            {
                "v": "gr-assert/2",
                "aud": "git-relay",
                "seat": "CODEX",
                "iat": 1800000000,
                "exp": 1800000090,
                "jti": "AbCdEfGhIjKlMnOp",
                "action": "open_pr",
                "body": VECTOR_BODY_SHA,
                "scopes": ["zulip:read", "zulip:write"],
                "epoch": 7,
                "approved_at": 1799999000000,
                "phase": 2,
                "grant_ref": "0123456789ab",
            },
        )
        # Canonical cross-language signing:  the Worker's signature over the
        # exact base64url payload is reproducible byte for byte.  (Production
        # never verifies here - the relay holds no key - this pins the format.)
        payload = VECTOR_TOKEN.split("_")[1]
        expected = hmac.new(VECTOR_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(VECTOR_TOKEN.endswith("_" + expected))

    def test_window_rules(self):
        now = int(time.time())
        old, _ = mint(iat=now - 500, ttl=90)
        self.assertIsNone(parse_claims(old))
        future, _ = mint(iat=now + 120)
        self.assertIsNone(parse_claims(future))
        long_lived, _ = mint(ttl=600)
        self.assertIsNone(parse_claims(long_lived))
        zero, _ = mint(ttl=0)
        self.assertIsNone(parse_claims(zero))

    def test_out_of_contract_claims_and_shapes_fail_closed(self):
        good, claims = mint()
        variants = [
            {**claims, "v": "gr-assert/1"},
            {**claims, "aud": "elsewhere"},
            {**claims, "seat": "codex"},
            {**claims, "jti": "short"},
            {**claims, "action": "Open_Pr"},
            {**claims, "body": "not-hex"},
            {**claims, "scopes": ["zulip:write", "admin"]},
            {**claims, "scopes": "zulip:write"},
            {**claims, "epoch": True},
            {**claims, "phase": 1.5},
            {**claims, "approved_at": "yesterday"},
            {**claims, "grant_ref": "xyz"},
            {**claims, "extra": 1},
        ]
        # parse_claims tolerates an extra key?  No:  buildClaims round-trip on
        # the Worker side is strict, and the relay mirrors that - unknown keys
        # mean a non-canonical token this Worker never minted.
        for v in variants:
            payload = b64url(json.dumps(v, separators=(",", ":")).encode())
            sig = hmac.new(KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
            self.assertIsNone(parse_claims(f"gra2_{payload}_{sig}"), v)
        for junk in (
            "",
            "gra2_",
            "gbb_fixer_" + "a" * 43,
            "sbt_codex_" + "a" * 43,
            "gra1_codex_1800000000_1800000090_AbCdEfGhIjKlMnOp_" + "0" * 64,
            "not-a-token",
        ):
            self.assertIsNone(parse_claims(junk))

    def test_a_tampered_payload_is_not_assertion_shaped(self):
        good, _ = mint()
        # A corrupted payload segment fails the base64/JSON/shape parse.  (A
        # tampered SIGNATURE still parses structurally - the HMAC check is the
        # Worker's, and its refusal drives resolve() to None.)
        self.assertIsNone(parse_claims(good[:10] + "!" + good[11:]))
        mangled = good.split("_")
        mangled[1] = mangled[1][:-4] + "AAAA"
        self.assertIsNone(parse_claims("_".join(mangled)))


class ResolveTest(unittest.TestCase):
    def test_a_bound_assertion_resolves_to_its_seat_and_claims(self):
        token, claims = mint()
        v = verifier()
        seat, resolved = v.resolve(token, proof(claims))
        self.assertEqual(seat, SEAT)
        self.assertEqual(resolved, claims)
        # Exactly one callback, carrying the token, action and body hash,
        # signed with the channel key.
        self.assertEqual(len(v._transport.calls), 1)
        call = v._transport.calls[0]
        self.assertEqual(call["url"], VERIFY_URL)
        sent = json.loads(call["body"])
        self.assertEqual(sent["contract"], "gr-assert-verify/1")
        self.assertEqual(sent["token"], token)
        self.assertEqual(sent["action"], "open_pr")
        self.assertEqual(sent["body_sha256"], BODY_SHA)
        expected_sig = hmac.new(KEY.encode(), call["body"], hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"]["X-Agent-Sync-Signature"], expected_sig)

    def test_the_request_binding_is_enforced_before_any_callback(self):
        token, claims = mint()
        v = verifier()
        self.assertIsNone(v.resolve(token, proof(claims, action="merge_pr")))
        self.assertIsNone(v.resolve(token, proof(claims, body_sha256="0" * 64)))
        self.assertIsNone(v.resolve(token, None))
        self.assertEqual(v._transport.calls, [])

    def test_wrong_seat_answer_and_refusal_fail_closed(self):
        token, claims = mint()
        self.assertIsNone(verifier(ok_answer(seat="ECHO")).resolve(token, proof(claims)))
        self.assertIsNone(
            verifier(lambda body: (200, b'{"ok": false, "reason": "replayed"}')).resolve(
                token, proof(claims)
            )
        )
        self.assertIsNone(verifier(lambda body: (401, b"")).resolve(token, proof(claims)))
        self.assertIsNone(verifier(lambda body: (200, b"not json")).resolve(token, proof(claims)))

    def test_transport_exceptions_fail_closed(self):
        def boom(body):
            raise OSError("worker unreachable")

        token, claims = mint()
        self.assertIsNone(verifier(boom).resolve(token, proof(claims)))

    def test_refusals_cache_briefly_but_positives_never_cache(self):
        token, claims = mint()
        v = verifier(lambda body: (200, b'{"ok": false, "reason": "bad_signature"}'))
        self.assertIsNone(v.resolve(token, proof(claims)))
        self.assertIsNone(v.resolve(token, proof(claims)))
        self.assertEqual(len(v._transport.calls), 1)  # the refusal was cached

        v2 = verifier(ok_answer())
        self.assertEqual(v2.resolve(token, proof(claims))[0], SEAT)
        self.assertEqual(v2.resolve(token, proof(claims))[0], SEAT)
        # Both presentations went to the Worker:  an assertion is one-use, so
        # a cached positive would hide the Worker's replay refusal.
        self.assertEqual(len(v2._transport.calls), 2)

    def test_short_key_and_missing_key_are_startup_errors(self):
        with self.assertRaises(ValueError):
            GrAssertionVerifier(
                url=VERIFY_URL, key_name="K", key_source=FakeKeySource({"K": "short"})
            )
        with self.assertRaises(KeyError):
            GrAssertionVerifier(url=VERIFY_URL, key_name="MISSING", key_source=FakeKeySource({}))
        with self.assertRaises(ValueError):
            GrAssertionVerifier(
                url="http://agents.example/x", key_name="K", key_source=FakeKeySource({"K": KEY})
            )

    def test_expired_assertion_never_reaches_the_callback(self):
        token, claims = mint(iat=int(time.time()) - 500, ttl=90)
        v = verifier()
        self.assertIsNone(v.resolve(token, proof(claims)))
        self.assertEqual(v._transport.calls, [])


SEAT_CFG = {
    "actions": ["open_pr"],
    "repos": ["Simple-With-Us/*"],
}


class PolicyAssertionTest(unittest.TestCase):
    def policy(self, seat_cfg=None, with_verifier=True, behavior=None):
        cfg = (
            seat_cfg
            if seat_cfg is not None
            else {"auth": "shared", "identity": SEAT, "oauth_assert": True, **SEAT_CFG}
        )
        return SeatPolicy(
            json.dumps({"swu-codex": cfg}),
            assertion_verifier=verifier(behavior) if with_verifier else None,
        )

    def test_flagged_shared_seat_accepts_a_valid_bound_assertion(self):
        token, claims = mint()
        self.assertEqual(self.policy().authenticate(token, proof(claims)), "swu-codex")

    def test_a_write_with_read_scopes_only_is_refused(self):
        token, claims = mint(scopes=("zulip:read",))
        # open_pr is a write action:  the signed claims lack zulip:write.
        self.assertIsNone(self.policy().authenticate(token, proof(claims)))

    def test_a_read_action_needs_no_write_scope(self):
        cfg = {
            "auth": "shared",
            "identity": SEAT,
            "oauth_assert": True,
            "app_slug": "swu-codex",
            "mint_permissions": {"contents": "read"},
            "actions": ["wf_repo_variable_get"],
            "repos": ["Simple-With-Us/*"],
        }
        token, claims = mint(action="wf_repo_variable_get", scopes=("zulip:read",))
        self.assertEqual(self.policy(cfg).authenticate(token, proof(claims)), "swu-codex")

    def test_unflagged_seat_refuses_a_valid_assertion(self):
        token, claims = mint()
        policy = self.policy({"auth": "shared", "identity": SEAT, **SEAT_CFG})
        self.assertIsNone(policy.authenticate(token, proof(claims)))

    def test_assertion_without_verifier_is_unreachable(self):
        token, claims = mint()
        self.assertIsNone(self.policy(with_verifier=False).authenticate(token, proof(claims)))

    def test_verified_identity_without_a_matching_seat_is_denied(self):
        token, claims = mint(seat="ECHO")
        self.assertIsNone(self.policy().authenticate(token, proof(claims)))

    def test_a_worker_refusal_never_falls_through_to_the_bearer_path(self):
        token, claims = mint()
        policy = self.policy(behavior=lambda body: (200, b'{"ok": false, "reason": "replayed"}'))
        self.assertIsNone(policy.authenticate(token, proof(claims)))

    def test_a_missing_proof_refuses(self):
        token, _ = mint()
        self.assertIsNone(self.policy().authenticate(token))

    def test_oauth_assert_flag_validation(self):
        with self.assertRaises(PolicyError):
            # non-boolean flag
            self.policy({"auth": "shared", "identity": SEAT, "oauth_assert": "yes", **SEAT_CFG})
        with self.assertRaises(PolicyError):
            # a static-token seat may not opt into assertions
            self.policy(
                {
                    "auth": "token",
                    "token": "t" * 20,
                    "identity": SEAT,
                    "oauth_assert": True,
                    **SEAT_CFG,
                }
            )

    def test_static_and_shared_bearer_seats_still_work(self):
        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-echo": {"token": "s" * 20, **SEAT_CFG},
                    "swu-codex": {
                        "auth": "shared",
                        "identity": SEAT,
                        "oauth_assert": True,
                        **SEAT_CFG,
                    },
                }
            ),
            assertion_verifier=verifier(),
        )
        self.assertEqual(policy.authenticate("s" * 20), "swu-echo")
        self.assertIsNone(policy.authenticate("wrong" + "s" * 20))
        # A gbb_-family guess still takes the shared-verifier path and fails
        # there (no bearer verifier configured here), not the assertion path.
        self.assertIsNone(policy.authenticate("gbb_fixer_" + "a" * 43))


SBT_TOKEN = "sbt_codex_" + "a" * 43
GBB_TOKEN = "gbb_fixer_" + "b" * 43


def bearer_ok_answer(identity="CODEX", expires_in_s=3600):
    def behavior(body):
        return 200, json.dumps(
            {
                "ok": True,
                "identity": identity,
                "expires_at": int((time.time() + expires_in_s) * 1000),
            }
        ).encode()

    return behavior


def shared_verifier(behavior):
    return SharedBearerVerifier(
        url=BEARER_VERIFY_URL,
        key_name="GR_BEARER_VERIFY_KEY",
        key_source=FakeKeySource({"GR_BEARER_VERIFY_KEY": KEY}),
        transport=make_transport(behavior),
    )


class AuthConfusionTest(unittest.TestCase):
    """Cross-family confusion and downgrade, against the REAL shared-bearer
    verifier and the REAL assertion verifier wired into one SeatPolicy."""

    def policy(self, behavior=None, assert_behavior=None, **seat_over):
        cfg = {"auth": "shared", "identity": SEAT, "oauth_assert": True, **SEAT_CFG}
        cfg.update(seat_over)
        return SeatPolicy(
            json.dumps({"swu-codex": cfg}),
            verifier=shared_verifier(behavior or bearer_ok_answer()),
            assertion_verifier=verifier(assert_behavior or ok_answer()),
        )

    def test_an_assertion_never_reaches_the_bearer_verify_route(self):
        policy = self.policy()
        token, claims = mint()
        self.assertEqual(policy.authenticate(token, proof(claims)), "swu-codex")
        # Zero bearer-route calls:  the assertion went to its own route only.
        self.assertEqual(policy._verifier._transport.calls, [])
        self.assertEqual(len(policy._assertion_verifier._transport.calls), 1)

    def test_a_seat_bearer_still_takes_the_remote_path_untouched(self):
        policy = self.policy()
        self.assertEqual(policy.authenticate(SBT_TOKEN), "swu-codex")
        self.assertEqual(len(policy._verifier._transport.calls), 1)
        self.assertEqual(policy._assertion_verifier._transport.calls, [])

    def test_a_persona_bearer_is_not_an_assertion(self):
        policy = self.policy()
        # GB-FIXER names a family the assertion verifier never claims, and no
        # configured seat here claims GB-FIXER, so policy refuses it locally -
        # zero remote calls, by the existing shared-path pre-check.
        self.assertIsNone(policy.authenticate(GBB_TOKEN))
        self.assertEqual(policy._verifier._transport.calls, [])
        self.assertIsNone(parse_claims(GBB_TOKEN))

    def test_assertion_verifier_rejects_every_bearer_family(self):
        v = verifier()
        for token in (SBT_TOKEN, GBB_TOKEN):
            self.assertIsNone(v.parse_identity(token))
            self.assertIsNone(v.resolve(token, {"action": "open_pr", "body_sha256": BODY_SHA}))
        self.assertEqual(v._transport.calls, [])

    def test_downgrade_to_unflagged_seat_refuses_even_with_verifiers(self):
        policy = self.policy(oauth_assert=False)
        token, claims = mint()
        self.assertIsNone(policy.authenticate(token, proof(claims)))

    def test_a_locally_invalid_assertion_costs_no_remote_call(self):
        policy = self.policy()
        expired, claims = mint(iat=int(time.time()) - 500, ttl=90)
        self.assertIsNone(policy.authenticate(expired, proof(claims)))
        self.assertEqual(policy._verifier._transport.calls, [])
        self.assertEqual(policy._assertion_verifier._transport.calls, [])

    def test_bearer_and_assertion_paths_give_the_same_seat(self):
        # One seat, two live credentials:  both resolve to the same seat key,
        # so the relay's allowlist, rate limits and audit see one identity.
        policy = self.policy()
        token, claims = mint()
        self.assertEqual(policy.authenticate(SBT_TOKEN), "swu-codex")
        self.assertEqual(policy.authenticate(token, proof(claims)), "swu-codex")


if __name__ == "__main__":
    unittest.main()
