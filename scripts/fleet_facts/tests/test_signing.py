"""Signing and verifying:  the documented canonical form, tamper detection, and the two ways to get the key."""
import copy
import hashlib
import hmac
import json
import os
import sys
import unittest
from unittest import mock

from fleet_facts import signing
from fleet_knobs.tests._fake import SIGNING, KnobTestCase

KEY = "fixture-signing-key-0123456789abcdefABCDEF"
OTHER = "another-fixture-key-9876543210zyxwvuTSRQ"


def document():
    return {"schema": 1, "kind": "mac-facts", "signed_for": "mac-facts.json", "generated_at": "2026-10-11T12:00:00Z",
            "sources": {"lanes": {"status": "ok", "reason": None}}, "lanes": {"total": 3, "attention": [{"branch": "claude/x", "unpushed": 2}]},
            "note": "café — “quoted”"}


class SignAndVerify(unittest.TestCase):
    def test_a_signed_document_verifies(self):
        signed = signing.sign(document(), KEY)
        self.assertEqual(signed["sig_alg"], signing.ALG)
        self.assertRegex(signed["sig"], r"^[0-9a-f]{64}$")
        self.assertTrue(signing.verify(signed, KEY))
        self.assertTrue(signing.verify(signed, KEY, signed_for="mac-facts.json"))

    def test_the_input_is_not_changed(self):
        doc = document()
        before = copy.deepcopy(doc)
        signing.sign(doc, KEY)
        self.assertEqual(doc, before)

    def test_signing_twice_gives_the_same_signature_and_an_old_sig_is_replaced(self):
        once = signing.sign(document(), KEY)
        twice = signing.sign(once, KEY)
        self.assertEqual(once["sig"], twice["sig"])

    def test_any_change_breaks_it(self):
        signed = signing.sign(document(), KEY)
        mutations = [
            lambda d: d["lanes"]["attention"][0].update(unpushed=0),
            lambda d: d["lanes"].update(total=4),
            lambda d: d.update(generated_at="2026-10-11T13:00:00Z"),
            lambda d: d.update(extra="added"),
            lambda d: d.pop("kind"),
            lambda d: d.update(signed_for="gb/mac-facts.json"),
            lambda d: d["sources"]["lanes"].update(status="failed"),
            lambda d: d.update(note="cafe"),
        ]
        for mutate in mutations:
            tampered = copy.deepcopy(signed)
            mutate(tampered)
            verdict = signing.verify(tampered, KEY)
            self.assertFalse(verdict)
            self.assertEqual(verdict.reason, "the signature does not match")

    def test_another_key_fails_and_so_does_a_missing_or_odd_signature(self):
        signed = signing.sign(document(), KEY)
        self.assertFalse(signing.verify(signed, OTHER))
        for sig in (None, "", "abc", "A" * 64, 5):
            bad = dict(signed, sig=sig)
            self.assertFalse(signing.verify(bad, KEY))
        no_sig = {k: v for k, v in signed.items() if k != "sig"}
        self.assertEqual(signing.verify(no_sig, KEY).reason, "the file has no signature")
        self.assertFalse(signing.verify(dict(signed, sig_alg="hmac-sha1"), KEY))
        self.assertFalse(signing.verify([1, 2], KEY))

    def test_a_file_moved_to_another_path_fails_the_path_check(self):
        signed = signing.sign(document(), KEY)
        verdict = signing.verify(signed, KEY, signed_for="gb/mac-facts.json")
        self.assertFalse(verdict)
        self.assertIn("different path", verdict.reason)

    def test_key_order_and_pretty_printing_do_not_matter(self):
        signed = signing.sign(document(), KEY)
        shuffled = json.loads(json.dumps(dict(reversed(list(signed.items()))), indent=4, ensure_ascii=False))
        self.assertTrue(signing.verify(shuffled, KEY))

    def test_a_key_of_the_wrong_shape_cannot_sign_or_verify(self):
        for bad in ("", "short", "has space in it but is long enough to pass the length check", "é" * 40, None):
            with self.assertRaises(signing.KeyUnavailable):
                signing.sign(document(), bad)
            with self.assertRaises(signing.KeyUnavailable):
                signing.verify(signing.sign(document(), KEY), bad)

    def test_surrounding_whitespace_on_the_key_is_trimmed_on_both_sides(self):
        signed = signing.sign(document(), KEY + "\n")
        self.assertTrue(signing.verify(signed, "  " + KEY))

    def test_the_documented_snippet_is_what_signing_does(self):
        """The README tells a reader how to check a file without this package;  this is that text."""
        signed = signing.sign(document(), KEY)
        text = json.dumps(signed, indent=2)
        doc = json.loads(text)
        sig = doc.pop("sig")
        body = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        good = hmac.compare_digest(sig, hmac.new(KEY.strip().encode(), body, hashlib.sha256).hexdigest())
        self.assertTrue(good)

    def test_the_canonical_form_is_ascii_sorted_and_compact(self):
        body = signing.canonical({"b": 1, "a": {"z": "é", "y": [1, 2]}, "sig": "ignored"})
        self.assertEqual(body, b'{"a":{"y":[1,2],"z":"\\u00e9"},"b":1}')

    def test_not_a_number_cannot_be_signed(self):
        with self.assertRaises(ValueError):
            signing.canonical({"x": float("nan")})

    def test_sha256_hex(self):
        self.assertEqual(signing.sha256_hex(b"abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class KeyFromEnvironment(unittest.TestCase):
    def test_set_unset_and_malformed(self):
        self.assertEqual(signing.key_from_environ({signing.KEY_ENV: " %s\n" % KEY}), KEY)
        with self.assertRaises(signing.KeyUnavailable) as caught:
            signing.key_from_environ({})
        self.assertIn("not set", caught.exception.reason)
        with self.assertRaises(signing.KeyUnavailable) as caught:
            signing.key_from_environ({signing.KEY_ENV: "short"})
        self.assertNotIn("short", caught.exception.reason)


class KeyFromKnobs(KnobTestCase):
    def test_the_key_comes_from_infisical_through_k1(self):
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": SIGNING})
        self.assertEqual(signing.key_from_knobs(), SIGNING)
        self.assertEqual(len(self.fake.calls()), 1)

    def test_an_unset_key_is_unavailable(self):
        self.fake.set(secrets={})
        with self.assertRaises(signing.KeyUnavailable) as caught:
            signing.key_from_knobs()
        self.assertIn("unset", caught.exception.reason)

    def test_an_unreachable_infisical_is_unavailable_even_with_a_last_known_good_copy(self):
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": SIGNING, "FLEET_AUTOPUSH_MODE": "report"})
        signing.key_from_knobs()
        self.new_process()
        self.fake.set(mode="fail", leak=SIGNING)
        with self.assertRaises(signing.KeyUnavailable) as caught:
            signing.key_from_knobs()
        self.assertNotIn(SIGNING, caught.exception.reason)
        self.assertIn("unreachable", caught.exception.reason)

    def test_a_malformed_key_is_unavailable_and_never_echoed(self):
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": "short but secret"})
        with self.assertRaises(signing.KeyUnavailable) as caught:
            signing.key_from_knobs()
        self.assertNotIn("short but secret", caught.exception.reason)

    def test_a_refused_environment_is_unavailable_and_nothing_is_fetched(self):
        with mock.patch.dict(os.environ, {"INFISICAL_ENV": "staging"}):
            with self.assertRaises(signing.KeyUnavailable) as caught:
                signing.key_from_knobs()
        self.assertIn("prod is the only environment", caught.exception.reason)
        self.assertEqual(self.fake.calls(), [])

    def test_a_copy_without_fleet_knobs_says_how_to_fix_it(self):
        with mock.patch.dict(sys.modules, {"fleet_knobs": None}):
            with self.assertRaises(signing.KeyUnavailable) as caught:
                signing.key_from_knobs()
        self.assertIn("install.sh --apply", caught.exception.reason)

    def test_the_key_is_never_written_to_the_last_known_good_file(self):
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": SIGNING, "FLEET_AUTOPUSH_MODE": "report"})
        signing.key_from_knobs()
        knobs_module = sys.modules["fleet_knobs.knobs"]
        knobs_module.get("FLEET_AUTOPUSH_MODE")
        for path in (self.home).rglob("*"):
            if path.is_file():
                self.assertNotIn(SIGNING, path.read_text(encoding="utf-8", errors="replace"), str(path))


if __name__ == "__main__":
    unittest.main()
