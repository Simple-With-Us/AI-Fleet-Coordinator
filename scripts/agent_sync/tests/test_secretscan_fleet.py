"""secretscan:  the fleet's own token kinds (open-loops F3) and per-field redaction.

Every token below is built when the test runs, from a prefix and random filler, so no line of this file
is a key-shaped literal (the gitleaks job runs on this repo, and so does the harness's own rule).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import secrets
import sys
import unittest
from pathlib import Path

from agent_sync import secretscan
from agent_sync.secretscan import WITHHELD, redact_fields, scan

SCRIPTS = Path(__file__).resolve().parents[2]


def hexs(n: int) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def alnum(n: int) -> str:
    """n letters and digits with at least one capital, one lower-case letter and one digit."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    body = "".join(secrets.choice(chars) for _ in range(n - 3))
    return body + "Aa7"


def b64(n: int) -> str:
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789-_") for _ in range(n))


def samples() -> list[tuple[str, str]]:
    """(expected kind, a text carrying one token of that kind)."""
    return [
        ("Linear API key", "key: " + "lin_" + "api_" + alnum(40)),
        ("Linear API key", "lin_" + "oauth_" + alnum(40)),
        ("Linear API key", "webhook " + "lin_" + "wh_" + alnum(36)),
        ("Linear client secret", "LINEAR_CLIENT_" + "SECRET=" + hexs(32)),
        ("Linear client secret", "Linear OAuth client secret: " + hexs(32)),
        ("Sentry token", "sntrys_" + b64(60)),
        ("Sentry token", "token sntryu_" + hexs(64)),
        ("xAI key", "xai-" + alnum(60)),
        ("Infisical service token", "st." + hexs(12) + "." + hexs(32) + "." + hexs(32)),
        ("Infisical credential", "INFISICAL_CLIENT_" + "SECRET=" + hexs(64)),
        ("Infisical credential", "Infisical token: " + alnum(40)),
        ("access token (JWT)", "Bearer-less " + "ey" + "J" + b64(20) + ".ey" + "J" + b64(30) + "." + b64(40)),
        ("Cloudflare token", "cf" + "ut_" + alnum(40)),
        ("Cloudflare token", "cf" + "at_" + alnum(40)),
        ("Cloudflare token", "Cloudflare API token = " + b64(40)),
        ("Cloudflare token", "CLOUDFLARE_GLOBAL_" + "KEY: " + hexs(37)),
        ("Coolify token", "coolify says 12|" + alnum(40)),
        ("Coolify token", "3|" + alnum(40)),
        ("assigned credential", "FLEET_FACTS_" + "TOKEN=" + alnum(24)),
        ("assigned credential", "SOME_SERVICE_PASS" + "WORD='" + alnum(20) + "'"),
    ]


class EveryKindIsFlaggedTests(unittest.TestCase):
    def test_each_sample_is_flagged_with_its_kind(self) -> None:
        for kind, text in samples():
            with self.subTest(kind=kind, shape=text[:12]):
                self.assertEqual(scan(text), kind)

    def test_the_kind_never_carries_the_text(self) -> None:
        for _, text in samples():
            found = scan(text) or ""
            for piece in text.replace("=", " ").replace(":", " ").split():
                if len(piece) >= 12:
                    self.assertNotIn(piece, found)

    def test_a_token_inside_prose_markdown_and_a_code_block_is_still_found(self) -> None:
        token = "lin_" + "api_" + alnum(40)
        for text in ("see %s for it" % token, "`%s`" % token, "```\nexport K=%s\n```" % token, "(%s)" % token,
                     "> %s" % token, "a\n\n%s\nb" % token):
            with self.subTest(text=text[:20]):
                self.assertEqual(scan(text), "Linear API key")

    def test_a_loaded_key_is_still_matched_exactly(self) -> None:
        loaded = hexs(24)
        self.assertEqual(scan("plain " + loaded, [loaded]), "a loaded credential")
        self.assertIsNone(scan("plain " + loaded))

    def test_the_older_kinds_still_answer_as_before(self) -> None:
        self.assertEqual(scan("AK" + "IA" + "ABCDEFGHIJ012345"), "AWS access key")
        self.assertEqual(scan("api_key=" + alnum(24)), "assigned secret")
        self.assertEqual(scan("sk-" + "ant-" + b64(40)), "Anthropic or OpenAI key")


def clean_texts() -> list[str]:
    """Text that only looks a little like a token and must not be flagged (the Worker's scanner is run over
    these too:  scripts/agent-sync-mcp/test/secretscan-parity.test.mjs)."""
    return [
        "git sha " + hexs(40), "uuid 9f1c2b3a-4d5e-6f70-8192-a3b4c5d6e7f8", "lin_api_ is the prefix of a Linear key",
        "lin_api_short", "xai-grok-bot-routines", "xai-" + "ab", "sntrys_ tokens start like that", "sntrys_abc",
        "Saint st.louis and ghost.example.com", "st.a.b.c", "| a | b |\n|---|---|\n| 3 | 4 |", "3 | 4", "12|short",
        "a 3|" + "x" * 10, "cfg_value and cfut_ short", "Linear secret handling is in the doc",
        "Infisical token rotation happens monthly", "see ~/.secrets/Zulip/Claude-zuliprc", "FLEET_AUTOPUSH_MODE=snapshot",
        "NEEDS_OWNER_CAPS=5", "OWNER_LINEAR_USER_ID=0b1c2d3e", "GITHUB_TOKEN=${{ secrets.GITHUB_TOKEN }}",
        "the Cloudflare token is stored in Infisical", "Linear client secret is in Infisical",
        "eyJ is how a JWT header starts", "eyJhbGciOi.short", "AFC-280 F3 agent-sync safety helpers",
    ]


class OrdinaryTextIsNotFlaggedTests(unittest.TestCase):
    def test_things_that_only_look_a_little_like_tokens(self) -> None:
        for text in clean_texts():
            with self.subTest(text=text[:40]):
                self.assertIsNone(scan(text))


class RoutineCheckerParityTests(unittest.TestCase):
    """scripts/check-routine-prompts.py keeps a local copy of these kinds for when it runs outside the repo.
    Whatever the copy flags, this module must flag too, with the same kind, so the duplicate stays harmless."""

    @classmethod
    def setUpClass(cls) -> None:
        path = SCRIPTS / "check-routine-prompts.py"
        if not path.exists():
            raise unittest.SkipTest("check-routine-prompts.py is not in this checkout")
        spec = importlib.util.spec_from_file_location("_parity_check_routine_prompts", str(path))
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module  # its dataclasses look their module up here
        cls.addClassCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(cls.module)

    def test_everything_the_checkers_copy_flags_is_flagged_here_too(self) -> None:
        local = self.module._LOCAL_PATTERNS
        seen = set()
        for kind, text in samples():
            for label, pattern in local:
                if pattern.search(text):
                    with self.subTest(kind=kind, local=label):
                        self.assertIsNotNone(scan(text))
                        if label != "assigned credential":  # this module may name an assignment more precisely
                            self.assertEqual(scan(text), label)
                    seen.add(label)
                    break
        self.assertGreaterEqual(len(seen), 8, "the samples should exercise most of the checker's copy")

    def test_the_checker_loads_this_module_by_path(self) -> None:
        self.assertIsNotNone(self.module._SCAN)
        self.assertEqual(self.module._SCAN("x " + "lin_" + "api_" + alnum(40)), "Linear API key")

    def test_json_is_imported_where_it_is_used_and_not_at_the_top(self) -> None:
        # the hook fast path imports this module on every tool call, so its top level stays small
        import ast

        tree = ast.parse((SCRIPTS / "agent_sync" / "secretscan.py").read_text(encoding="utf-8"))
        top = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("json", top)
        self.assertEqual(top, {"base64", "re"})

    def test_the_module_has_no_import_from_this_package(self) -> None:
        # the checker executes secretscan.py as a bare file, so a relative import would break it
        source = (SCRIPTS / "agent_sync" / "secretscan.py").read_text(encoding="utf-8")
        self.assertNotIn("from .", source)
        self.assertNotIn("import agent_sync", source)


class ScanIsFastTests(unittest.TestCase):
    def test_long_adversarial_inputs_do_not_blow_up(self) -> None:
        import time

        nasty = ("linear " * 3000, "cloudflare " + "a" * 60000, "infisical " + "_" * 60000, "st." + "a" * 60000,
                 "1|" + "a" * 60000, "eyJ" + "a" * 60000, "A" * 100000, "FOO_" * 20000 + "TOKEN =", "sntrys_" + "=" * 60000)
        start = time.monotonic()
        for text in nasty:
            scan(text)
        self.assertLess(time.monotonic() - start, 5.0)


class RedactFieldsTests(unittest.TestCase):
    def test_a_secret_shaped_field_is_replaced_whole_and_its_path_is_reported(self) -> None:
        token = "xai-" + alnum(60)
        doc = {"title": "ok", "note": "uses " + token + " today", "items": [{"id": 1, "text": "fine"}, {"id": 2, "text": token}]}
        clean, paths = redact_fields(doc)
        self.assertEqual(clean["note"], WITHHELD)
        self.assertEqual(clean["items"][1]["text"], WITHHELD)
        self.assertEqual(clean["title"], "ok")
        self.assertEqual(clean["items"][0], {"id": 1, "text": "fine"})
        self.assertEqual(paths, ["$.note", "$.items[1].text"])
        self.assertNotIn(token, json.dumps(clean))
        self.assertNotIn(token, json.dumps(paths))

    def test_the_input_is_never_changed(self) -> None:
        token = "lin_" + "api_" + alnum(40)
        doc = {"a": [token, {"b": token}], "c": (token, "x")}
        before = copy.deepcopy(doc)
        clean, _ = redact_fields(doc)
        self.assertEqual(doc, before)
        self.assertIsNot(clean, doc)
        self.assertIsNot(clean["a"], doc["a"])

    def test_nothing_secret_means_an_equal_copy_and_no_paths(self) -> None:
        doc = {"n": 5, "f": 1.5, "t": True, "z": None, "l": ["a", ["b"]], "d": {"k": "v"}, "tup": ("x", 2)}
        clean, paths = redact_fields(doc)
        self.assertEqual(clean, doc)
        self.assertEqual(paths, [])

    def test_lists_tuples_and_a_root_string(self) -> None:
        token = "sntrys_" + b64(60)
        clean, paths = redact_fields([token, ("a", token), "ok"])
        self.assertEqual(clean, [WITHHELD, ("a", WITHHELD), "ok"])
        self.assertIsInstance(clean[1], tuple)
        self.assertEqual(paths, ["$[0]", "$[1][1]"])
        self.assertEqual(redact_fields(token), (WITHHELD, ["$"]))
        self.assertEqual(redact_fields("fine"), ("fine", []))

    def test_a_secret_shaped_key_is_withheld_and_never_appears_in_a_path(self) -> None:
        token = "lin_" + "api_" + alnum(40)
        clean, paths = redact_fields({token: "value", "other": {token: token}})
        self.assertNotIn(token, json.dumps(clean))
        self.assertNotIn(token, json.dumps(paths))
        self.assertIn(WITHHELD, clean)
        self.assertEqual(clean[WITHHELD], "value")
        self.assertEqual(clean["other"], {WITHHELD: WITHHELD})
        self.assertEqual(paths, ['$["%s"]' % WITHHELD, '$.other["%s"]' % WITHHELD])

    def test_two_secret_shaped_keys_do_not_collide(self) -> None:
        a, b = "lin_" + "api_" + alnum(40), "lin_" + "api_" + alnum(40)
        clean, _ = redact_fields({a: 1, b: 2})
        self.assertEqual(sorted(clean.values()), [1, 2])
        self.assertEqual(len(clean), 2)

    def test_path_notation(self) -> None:
        token = "xai-" + alnum(60)
        clean, paths = redact_fields({"plain_name": token, "odd key.with dots": token, "a-b": token, "ué": token,
                                      "l": [[token]]})
        self.assertEqual(paths, ["$.plain_name", '$["odd key.with dots"]', "$.a-b", '$["u\\u00e9"]', "$.l[0][0]"])
        self.assertEqual(clean["l"], [[WITHHELD]])
        self.assertEqual([v for k, v in clean.items() if k != "l"], [WITHHELD] * 4)

    def test_known_values_are_redacted_too(self) -> None:
        loaded = hexs(20)
        clean, paths = redact_fields({"a": "has " + loaded, "b": "no"}, known=[loaded])
        self.assertEqual(clean, {"a": WITHHELD, "b": "no"})
        self.assertEqual(paths, ["$.a"])

    def test_values_that_are_not_text_are_left_alone(self) -> None:
        raw = b"lin_" + b"api_" + alnum(40).encode()
        clean, paths = redact_fields({"bytes": raw, "int": 12345678901234567890, "set": {1, 2}})
        self.assertEqual(clean["bytes"], raw)  # JSON-like data only:  documented, not walked
        self.assertEqual(paths, [])

    def test_a_structure_nested_too_deep_is_withheld_whole(self) -> None:
        deep: object = "x"
        for _ in range(secretscan.MAX_DEPTH + 5):
            deep = [deep]
        clean, paths = redact_fields({"deep": deep})
        self.assertTrue(paths and paths[0].startswith("$.deep[0]"))
        self.assertIn(WITHHELD, json.dumps(clean))
        cyclic: dict = {}
        cyclic["me"] = cyclic
        clean, paths = redact_fields(cyclic)  # must terminate
        self.assertTrue(paths)

    def test_the_paths_are_unique_and_ordered(self) -> None:
        token = "xai-" + alnum(60)
        _, paths = redact_fields({token: token})
        self.assertEqual(paths, ['$["%s"]' % WITHHELD])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
