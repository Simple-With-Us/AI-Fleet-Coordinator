"""Construct source fixtures to test syntax classification, not secret exceptions."""

import secrets
import string
import unittest
from unittest import mock

from git_relay import patch_secretscan as scanner
from git_relay.reviewed_fixtures import unreviewed_file_secret

REPO = "Example-Org/classifier-fixture"
PATH = "src/loader_fixture.py"
CALL = "_wake_router_token(env)"
BINDINGS = {
    "LISTENER_READ_TOKEN": "INFISICAL_LISTENER_READ_TOKEN",
    "OPERATOR_VOICE_TOKEN": "INFISICAL_OPERATOR_VOICE_TOKEN",
    "WAKE_ROUTER_TOKEN": "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN",
}


def synthetic():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(48))


def assignment_source(target, value, quote=repr):
    return f"{target} = {quote(value)}\n"


def keyword_source(expression):
    return f"fetch(token={expression})\n"


def public_source():
    return assignment_source("LISTENER_READ_TOKEN", "INFISICAL_LISTENER_READ_TOKEN")


class PythonSourceClassification(unittest.TestCase):
    def check_file(self, text, path=PATH, known=()):
        return unreviewed_file_secret(REPO, path, text.encode(), known)

    def test_exact_public_environment_name_bindings_are_not_values(self):
        self.assertEqual(scanner._PUBLIC_PYTHON_ENV_BINDINGS, BINDINGS)
        for name, value in BINDINGS.items():
            for quote in ("'", '"'):
                text = assignment_source(name, value, lambda value: quote + value + quote)
                with self.subTest(name=name, quote=quote):
                    self.assertIsNone(self.check_file(text))
                    self.assertEqual(scanner.scan(text), "assigned credential")

    def test_actual_keyword_call_is_source_not_a_literal(self):
        text = keyword_source(CALL)
        self.assertIsNone(self.check_file(text))
        self.assertEqual(scanner.scan(text), "assigned secret")

    def test_raw_messages_and_other_file_types_remain_conservative(self):
        for path in ("notes.md", "value.json", "source.js", "source.py.txt"):
            self.assertIsNotNone(self.check_file(public_source(), path))

    def test_known_values_are_absolute_even_when_they_are_public_names(self):
        for value in ("INFISICAL_LISTENER_READ_TOKEN", "TOKEN"):
            self.assertEqual(self.check_file(public_source(), known=[value]), "a loaded credential")

    def test_random_quoted_and_unquoted_assignments_still_refuse(self):
        for quote in (repr, str):
            self.assertIsNotNone(
                self.check_file(assignment_source("TEST_TOKEN", synthetic(), quote))
            )

    def test_unknown_uppercase_names_do_not_become_an_environment_exemption(self):
        self.assertIsNotNone(
            self.check_file(
                assignment_source("LISTENER_READ_TOKEN", "SOME_OTHER_PUBLIC_LOOKING_TOKEN")
            )
        )
        self.assertIsNotNone(
            self.check_file(assignment_source("OTHER_TOKEN", "INFISICAL_LISTENER_READ_TOKEN"))
        )

    def test_public_name_in_an_unrelated_binding_does_not_qualify(self):
        self.assertIsNotNone(
            self.check_file(assignment_source("WAKE_ROUTER_TOKEN", "INFISICAL_LISTENER_READ_TOKEN"))
        )

    def test_credential_shaped_callable_names_are_not_classified_as_source(self):
        for name in ("call_" + synthetic(), "unrelated_public_validator"):
            text = keyword_source(f"{name}(env)")
            self.assertEqual(scanner._python_source_value_spans(text), frozenset())
            self.assertEqual(self.check_file(text), "assigned secret")

    def test_nearby_validator_call_variants_get_no_semantic_admission(self):
        expressions = (
            "_wake_router_token(other_env)",
            "_wake_router_token()",
            "_wake_router_token(env, other)",
            "_wake_router_token(env=env)",
            "_wake_router_token(env, extra=True)",
            "_wake_router_token( env )",
            "other._wake_router_token(env)",
            "_wake_router_token(env) or fallback",
            "_wake_router_token(env) + suffix",
        )
        for expression in expressions:
            text = keyword_source(expression)
            with self.subTest(expression=expression):
                self.assertEqual(scanner._python_source_value_spans(text), frozenset())
                self.assertEqual(self.check_file(text), scanner.scan(text))
        text = f"fetch(api_key={CALL})\n"
        self.assertEqual(scanner._python_source_value_spans(text), frozenset())
        self.assertEqual(self.check_file(text), scanner.scan(text))

    def test_quoted_call_expression_is_not_source(self):
        self.assertIsNotNone(self.check_file(keyword_source(repr(CALL))))

    def test_bare_keyword_identifier_is_still_a_candidate(self):
        self.assertIsNotNone(self.check_file(keyword_source("some_credential_value")))

    def test_comments_and_docstrings_do_not_qualify(self):
        for text in (
            "# " + public_source(),
            repr(public_source()) + "\n",
            "# " + keyword_source(CALL),
        ):
            self.assertIsNotNone(self.check_file(text))

    def test_invalid_python_cannot_supply_source_evidence(self):
        self.assertIsNotNone(self.check_file(public_source() + "not valid python !!!\n"))

    def test_alternative_string_spellings_never_gain_new_admission(self):
        name, value = "LISTENER_READ_TOKEN", "INFISICAL_LISTENER_READ_TOKEN"
        for expression in ('r"' + value + '"', repr(value) + ' ""', '"""' + value + '"""'):
            text = assignment_source(name, expression, str)
            self.assertEqual(self.check_file(text), scanner.scan(text))

    def test_nested_literal_inside_call_is_scanned_independently(self):
        expression = f"_wake_router_token(env, password={synthetic()!r})"
        self.assertIsNotNone(self.check_file(keyword_source(expression)))

    def test_unrelated_literal_after_classified_source_is_still_refused(self):
        text = public_source() + assignment_source("PASSWORD", synthetic())
        self.assertIsNotNone(self.check_file(text))

    def test_dedicated_provider_shapes_are_never_waived(self):
        text = public_source() + assignment_source("value", "ghp_" + synthetic())
        self.assertIsNotNone(self.check_file(text))

    def test_known_runtime_value_is_not_waived_inside_a_call(self):
        self.assertEqual(
            self.check_file(keyword_source(CALL), known=["_wake_router_token"]),
            "a loaded credential",
        )

    def test_unicode_offsets_are_not_byte_offsets(self):
        text = 'title = "café"; ' + keyword_source(CALL)
        self.assertIsNone(self.check_file(text))
        self.assertIsNotNone(self.check_file(text + assignment_source("PASSWORD", synthetic())))

    def test_unicode_literal_separators_do_not_change_physical_lines(self):
        for separator in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            # Use the actual Unicode characters, not escaped spellings.
            prefix = 'banner = "' + separator + "é界😀" * 40 + '"\n'
            for tail in (public_source(), keyword_source(CALL)):
                with self.subTest(separator=ord(separator), tail_kind=tail.split("(")[0]):
                    text = prefix + tail
                    self.assertIsNone(self.check_file(text))
                    self.assertIsNotNone(
                        self.check_file(text + assignment_source("PASSWORD", synthetic()))
                    )

    def test_python_physical_newline_forms_keep_correct_offsets(self):
        for newline in ("\n", "\r\n", "\r"):
            text = ('banner = "é界😀"\n' + public_source() + keyword_source(CALL)).replace(
                "\n", newline
            )
            with self.subTest(newline=repr(newline)):
                self.assertIsNone(self.check_file(text))

    def test_invalid_ast_locations_invalidate_all_spans(self):
        import ast

        text = public_source() + public_source()
        for field, value in (
            ("lineno", 0),
            ("lineno", 9999),
            ("col_offset", -1),
            ("col_offset", None),
            ("end_col_offset", 1000000),
        ):
            tree = ast.parse(text)
            setattr(tree.body[1].value, field, value)
            with self.subTest(field=field, value=value), mock.patch("ast.parse", return_value=tree):
                self.assertEqual(scanner._python_source_value_spans(text), frozenset())
                self.assertIsNotNone(self.check_file(text))

    def test_undecodable_utf8_ast_column_fails_closed_not_exception(self):
        import ast

        text = public_source() + 'title="é"; ' + public_source()
        tree = ast.parse(text)
        tree.body[2].value.col_offset = len(b'title="') + 1
        with mock.patch("ast.parse", return_value=tree):
            self.assertEqual(scanner._python_source_value_spans(text), frozenset())
            self.assertIsNotNone(self.check_file(text))

    def test_surrogate_source_parse_fails_closed(self):
        self.assertEqual(
            scanner._python_source_value_spans("\ud800" + public_source()), frozenset()
        )

    def test_analysis_node_budget_does_not_admit_partial_evidence(self):
        text = public_source() + "x = 0\n" * 3000
        self.assertIsNotNone(self.check_file(text))

    def test_multiple_or_annotated_bindings_get_no_admission(self):
        value = "INFISICAL_LISTENER_READ_TOKEN"
        for target in ("LISTENER_READ_TOKEN = OTHER_TOKEN", "LISTENER_READ_TOKEN: str"):
            text = assignment_source(target, value)
            self.assertEqual(scanner._python_source_value_spans(text), frozenset())
            self.assertEqual(self.check_file(text), scanner.scan(text))

    def test_oversize_source_does_not_get_semantic_admission(self):
        self.assertIsNotNone(self.check_file("# padding\n" * 240000 + public_source()))


if __name__ == "__main__":
    unittest.main()
