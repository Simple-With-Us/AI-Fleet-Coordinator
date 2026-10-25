"""Shared-bearer verifier (shared_bearer.py) and its policy integration.

Fakes only:  no network, no real key.  The transport and key source are
injected;  time is real but tokens/answers are synthetic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest

from git_relay.policy import PolicyError, SeatPolicy
from git_relay.shared_bearer import SIGNATURE_HEADER, SharedBearerVerifier

KEY = "k" * 48
URL = "https://agents.example/internal/gr-bearer-verify"
TOKEN = "gbb_fixer_" + "a" * 43
IDENTITY = "GB-FIXER"


class FakeKeySource:
    def __init__(self, values):
        self.values = values

    def secret(self, name):
        return self.values[name]


def make_transport(behavior):
    """behavior(body: bytes) -> (status, payload: bytes).  Records calls."""
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return behavior(body)

    transport.calls = calls
    return transport


def ok_answer(identity=IDENTITY, expires_in_s=3600):
    def behavior(body):
        return 200, json.dumps({
            "ok": True,
            "identity": identity,
            "expires_at": int((time.time() + expires_in_s) * 1000),
        }).encode()
    return behavior


def verifier_for(behavior, key=KEY):
    return SharedBearerVerifier(
        url=URL,
        key_name="GR_BEARER_VERIFY_KEY",
        key_source=FakeKeySource({"GR_BEARER_VERIFY_KEY": key}),
        transport=make_transport(behavior),
    )


class VerifierTest(unittest.TestCase):
    def test_resolves_a_live_bearer(self):
        v = verifier_for(ok_answer())
        self.assertEqual(v.resolve(TOKEN), IDENTITY)

    def test_sends_hash_only_and_signs_the_body(self):
        v = verifier_for(ok_answer())
        v.resolve(TOKEN)
        (call,) = v._transport.calls
        self.assertNotIn(TOKEN.encode(), call["body"])
        body = json.loads(call["body"])
        self.assertEqual(body["hash"], hashlib.sha256(TOKEN.encode()).hexdigest())
        self.assertEqual(body["identity"], IDENTITY)
        self.assertEqual(body["contract"], "gr-bearer-verify/1")
        expected = hmac.new(KEY.encode(), call["body"], hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"][SIGNATURE_HEADER], expected)

    def test_malformed_token_never_leaves_the_process(self):
        v = verifier_for(ok_answer())
        self.assertIsNone(v.resolve("not-a-bearer"))
        self.assertEqual(v._transport.calls, [])

    def test_answer_for_a_different_identity_is_refused(self):
        v = verifier_for(ok_answer(identity="GB-ORACLE"))
        self.assertIsNone(v.resolve(TOKEN))

    def test_expired_answer_is_refused(self):
        v = verifier_for(ok_answer(expires_in_s=-5))
        self.assertIsNone(v.resolve(TOKEN))

    def test_refusal_answer_is_refused(self):
        v = verifier_for(lambda body: (200, json.dumps({"ok": False, "reason": "mismatch"}).encode()))
        self.assertIsNone(v.resolve(TOKEN))

    def test_http_error_and_garbage_fail_closed(self):
        self.assertIsNone(verifier_for(lambda body: (500, b"")).resolve(TOKEN))
        self.assertIsNone(verifier_for(lambda body: (200, b"not json")).resolve(TOKEN))

    def test_transport_exception_fails_closed(self):
        def boom(body):
            raise TimeoutError("nope")
        self.assertIsNone(verifier_for(boom).resolve(TOKEN))

    def test_positive_answer_not_cached_by_default(self):
        # Immediate owner control:  a revoke or pause in /admin must take effect
        # on the next call, so the default re-verifies every call.
        v = verifier_for(ok_answer())
        self.assertEqual(v.resolve(TOKEN), IDENTITY)
        self.assertEqual(v.resolve(TOKEN), IDENTITY)
        self.assertEqual(len(v._transport.calls), 2)

    def test_positive_cache_only_with_operator_opt_in(self):
        import os
        os.environ["GIT_RELAY_VERIFY_CACHE_S"] = "30"
        try:
            v = verifier_for(ok_answer())
            self.assertEqual(v.resolve(TOKEN), IDENTITY)
            self.assertEqual(v.resolve(TOKEN), IDENTITY)
            self.assertEqual(len(v._transport.calls), 1)
        finally:
            del os.environ["GIT_RELAY_VERIFY_CACHE_S"]

    def test_cache_is_bounded_and_evicts_oldest(self):
        v = verifier_for(lambda body: (200, json.dumps({"ok": False, "reason": "mismatch"}).encode()))
        for i in range(1100):
            v.resolve("gbb_fixer_" + str(i % 36) * 43)
        from git_relay.shared_bearer import CACHE_MAX_ENTRIES
        self.assertLessEqual(len(v._cache), CACHE_MAX_ENTRIES)

    def test_negative_answer_caches_briefly(self):
        v = verifier_for(lambda body: (200, json.dumps({"ok": False, "reason": "mismatch"}).encode()))
        self.assertIsNone(v.resolve(TOKEN))
        self.assertIsNone(v.resolve(TOKEN))
        self.assertEqual(len(v._transport.calls), 1)

    def test_short_key_and_bad_url_are_startup_errors(self):
        with self.assertRaises(ValueError):
            SharedBearerVerifier(url=URL, key_name="K", key_source=FakeKeySource({"K": "short"}), transport=make_transport(ok_answer()))
        with self.assertRaises(ValueError):
            SharedBearerVerifier(url="http://insecure.example/x", key_name="K", key_source=FakeKeySource({"K": KEY}), transport=make_transport(ok_answer()))
        with self.assertRaises(KeyError):
            SharedBearerVerifier(url=URL, key_name="MISSING", key_source=FakeKeySource({}), transport=make_transport(ok_answer()))


SEAT_CFG = {
    "actions": ["open_pr"],
    "repos": ["Simple-With-Us/*"],
}


class PolicySharedAuthTest(unittest.TestCase):
    def test_shared_seat_resolves_through_the_verifier(self):
        v = verifier_for(ok_answer())
        policy = SeatPolicy(json.dumps({"swu-fixer": {"auth": "shared", "identity": IDENTITY, **SEAT_CFG}}), verifier=v)
        self.assertEqual(policy.authenticate(TOKEN), "swu-fixer")

    def test_shared_seat_without_verifier_is_unreachable(self):
        policy = SeatPolicy(json.dumps({"swu-fixer": {"auth": "shared", "identity": IDENTITY, **SEAT_CFG}}))
        self.assertIsNone(policy.authenticate(TOKEN))

    def test_verified_identity_without_a_matching_seat_is_denied(self):
        v = verifier_for(ok_answer(identity="GB-ORACLE"))
        policy = SeatPolicy(json.dumps({"swu-fixer": {"auth": "shared", "identity": IDENTITY, **SEAT_CFG}}), verifier=v)
        # GB-ORACLE verifies fine but no seat claims that identity.
        self.assertIsNone(policy.authenticate("gbb_oracle_" + "b" * 43))

    def test_shared_seat_must_not_carry_a_static_token(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps({"swu-fixer": {"auth": "shared", "identity": IDENTITY, "token": "x" * 20, **SEAT_CFG}}))

    def test_static_token_seats_still_work(self):
        policy = SeatPolicy(json.dumps({"swu-echo": {"token": "s" * 20, **SEAT_CFG}}), verifier=verifier_for(ok_answer()))
        self.assertEqual(policy.authenticate("s" * 20), "swu-echo")
        self.assertIsNone(policy.authenticate("wrong" + "s" * 20))

    def test_bad_auth_mode_is_a_config_error(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps({"swu-echo": {"auth": "bogus", **SEAT_CFG}}))

    def test_two_seats_cannot_claim_one_identity(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps({
                "swu-fixer": {"auth": "shared", "identity": IDENTITY, **SEAT_CFG},
                "swu-fixer-2": {"auth": "shared", "identity": IDENTITY, **SEAT_CFG},
            }))

class SeatFamilyTest(unittest.TestCase):
    SBT = "sbt_jet-chat_" + "c" * 43

    def test_seat_token_resolves_to_its_seat_name(self):
        v = verifier_for(ok_answer(identity="JET-CHAT"))
        self.assertEqual(v.parse_identity(self.SBT), "JET-CHAT")
        self.assertEqual(v.resolve(self.SBT), "JET-CHAT")

    def test_seat_identity_sent_verbatim(self):
        v = verifier_for(ok_answer(identity="JET-CHAT"))
        v.resolve(self.SBT)
        (call,) = v._transport.calls
        self.assertEqual(json.loads(call["body"])["identity"], "JET-CHAT")

    def test_shared_seat_policy_with_sbt_bearer(self):
        v = verifier_for(ok_answer(identity="JET-CHAT"))
        policy = SeatPolicy(json.dumps({"swu-jet-chat": {"auth": "shared", "identity": "JET-CHAT", **SEAT_CFG}}), verifier=v)
        self.assertEqual(policy.authenticate(self.SBT), "swu-jet-chat")


if __name__ == "__main__":
    unittest.main()
