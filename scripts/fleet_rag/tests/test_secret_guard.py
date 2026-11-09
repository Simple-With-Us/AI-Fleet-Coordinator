"""Tests for scripts/hooks/secret-guard-pretooluse.py.

    cd scripts && python3 -m unittest fleet_rag.tests.test_secret_guard -v

No live secrets are read.  Commands are templates only.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

HOOK = pathlib.Path(__file__).resolve().parents[2] / "hooks" / "secret-guard-pretooluse.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("secret_guard_pretooluse", HOOK)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


sg = _load_hook()


def bash_payload(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class IdentTests(unittest.TestCase):
    def test_secret_names(self):
        for name in (
            "SILICONFLOW_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "DB_PASSWORD",
            "POSTGRES_DSN",
            "KEY",
            "TOKEN",
            "MAC_COLLAB_TOKEN",
            "CF_ACCESS_CLIENT_SECRET",
            "OPENAI_API_KEY",
        ):
            self.assertTrue(sg.ident_is_secret(name), name)

    def test_non_secret_names(self):
        for name in (
            "HOME",
            "PATH",
            "PWD",
            "FILE",
            "KEYBOARD",
            "TOKENIZER",
            "COUNT",
            "LINES",
        ):
            self.assertFalse(sg.ident_is_secret(name), name)

    def test_length_expansion_is_not_a_value_ref(self):
        self.assertEqual(sg.secret_var_names('echo ${#SILICONFLOW_API_KEY}'), [])
        self.assertEqual(
            sg.secret_var_names('od -c <<< "$SILICONFLOW_API_KEY"'),
            ["SILICONFLOW_API_KEY"],
        )


class RuleETests(unittest.TestCase):
    """od/hexdump/xxd on a loaded key var -- the 2026-09-17 gap."""

    def test_incident_od_c_here_string(self):
        reason = sg.check_command('od -c <<< "$SILICONFLOW_API_KEY"')
        self.assertIsNotNone(reason)
        self.assertIn("SILICONFLOW_API_KEY", reason)
        self.assertRegex(reason, r"\bod\b")

    def test_od_unquoted_here_string(self):
        self.assertIsNotNone(sg.check_command("od -c <<< $GITHUB_TOKEN"))

    def test_od_braced(self):
        self.assertIsNotNone(sg.check_command('od -An -tx1 <<< "${DB_PASSWORD}"'))

    def test_printf_piped_to_od(self):
        self.assertIsNotNone(
            sg.check_command("printf '%s' \"$SILICONFLOW_API_KEY\" | od -c")
        )

    def test_echo_piped_to_xxd(self):
        self.assertIsNotNone(sg.check_command('echo "$OPENAI_API_KEY" | xxd'))

    def test_echo_piped_to_hexdump(self):
        self.assertIsNotNone(sg.check_command('echo "$MAC_COLLAB_TOKEN" | hexdump -C'))

    def test_xxd_here_string(self):
        self.assertIsNotNone(sg.check_command('xxd <<< "$AWS_SECRET_ACCESS_KEY"'))

    def test_hexdump_here_string(self):
        self.assertIsNotNone(sg.check_command('hexdump -C <<< "$POSTGRES_DSN"'))

    def test_strings_here_string(self):
        self.assertIsNotNone(sg.check_command('strings <<< "$DB_PASSWORD"'))

    def test_base64_here_string(self):
        self.assertIsNotNone(sg.check_command('base64 <<< "$GITHUB_TOKEN"'))

    def test_cut_c_here_string(self):
        reason = sg.check_command('cut -c1-4 <<< "$SILICONFLOW_API_KEY"')
        self.assertIsNotNone(reason)
        self.assertIn("cut -c", reason)

    def test_cut_characters_long_opt(self):
        self.assertIsNotNone(
            sg.check_command('cut --characters=1-8 <<< "$GITHUB_TOKEN"')
        )

    def test_printf_s_as_last_command(self):
        reason = sg.check_command("printf '%s' \"$SILICONFLOW_API_KEY\"")
        self.assertIsNotNone(reason)
        self.assertIn("printf", reason)

    def test_printf_q_as_last_command(self):
        self.assertIsNotNone(sg.check_command("printf '%q\\n' \"$GITHUB_TOKEN\""))

    def test_printf_unquoted_format(self):
        self.assertIsNotNone(sg.check_command('printf %s "$DB_PASSWORD"'))

    def test_od_inside_command_substitution(self):
        self.assertIsNotNone(
            sg.check_command('shape=$(od -c <<< "$SILICONFLOW_API_KEY")')
        )

    def test_substring_prefix_into_od(self):
        self.assertIsNotNone(
            sg.check_command('od -c <<< "${SILICONFLOW_API_KEY:0:4}"')
        )


class RuleEAllowTests(unittest.TestCase):
    def test_length_via_hash(self):
        self.assertIsNone(sg.check_command('echo ${#SILICONFLOW_API_KEY}'))

    def test_nonempty_test(self):
        self.assertIsNone(sg.check_command('[ -n "$SILICONFLOW_API_KEY" ] && echo set'))

    def test_quote_shape_test(self):
        self.assertIsNone(
            sg.check_command("""[[ $SILICONFLOW_API_KEY == *'\"'* ]] && echo quoted || echo clean""")
        )

    def test_printf_piped_to_wc_c(self):
        self.assertIsNone(
            sg.check_command("printf '%s' \"$SILICONFLOW_API_KEY\" | wc -c")
        )

    def test_printf_into_curl_config(self):
        # Allowed: keep the token out of argv.  printf is not the last stage.
        # Rule B still requires a redaction pipe when the template contains Bearer.
        self.assertIsNone(
            sg.check_command(
                'printf \'header = "Authorization: Bearer %s"\\n\' "$GITHUB_TOKEN" | '
                'curl -K - https://example.com/ 2>&1 | sed "s/${GITHUB_TOKEN}/[REDACTED]/g"'
            )
        )

    def test_printf_into_sed_redaction(self):
        self.assertIsNone(
            sg.check_command(
                'some-command --token "$GITHUB_TOKEN" 2>/dev/null | sed "s/${GITHUB_TOKEN}/[REDACTED]/g"'
            )
        )

    def test_extract_one_key_into_var(self):
        self.assertIsNone(
            sg.check_command(
                'TOKEN="$(grep -m1 \'^SOME_KEY=\' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d \'"\')"'
            )
        )

    def test_od_of_ordinary_file(self):
        self.assertIsNone(sg.check_command("od -c /tmp/readme.txt"))

    def test_xxd_of_binary(self):
        self.assertIsNone(sg.check_command("xxd ./logo.png | head -5"))

    def test_hexdump_hosts(self):
        self.assertIsNone(sg.check_command("hexdump -C /etc/hosts"))

    def test_od_of_home_var(self):
        self.assertIsNone(sg.check_command('od -c <<< "$HOME"'))

    def test_od_then_unrelated_key_assignment(self):
        self.assertIsNone(sg.check_command("od -c /tmp/x; TOKEN=abc true"))

    def test_cut_fields_extract_is_allowed(self):
        # cut -d= -f2- is the one-key extract, not a character dump.
        self.assertIsNone(
            sg.check_command(
                "grep -m1 '^SOME_KEY=' ~/.secrets/global-api-keys | cut -d= -f2-"
            )
        )

    def test_names_only_grep(self):
        self.assertIsNone(
            sg.check_command(
                "grep -oE '^[A-Z][A-Z0-9_]*' ~/.secrets/global-api-keys | sort -u"
            )
        )


class RuleATests(unittest.TestCase):
    def test_cat_handoff_denied(self):
        reason = sg.check_command("cat ~/.secrets/global-api-keys")
        self.assertIsNotNone(reason)
        self.assertIn(".secrets/", reason)

    def test_xxd_handoff_denied(self):
        reason = sg.check_command("xxd ~/.secrets/global-api-keys")
        self.assertIsNotNone(reason)
        self.assertIn(".secrets/", reason)

    def test_od_handoff_denied(self):
        self.assertIsNotNone(sg.check_command("od -c ~/.secrets/global-api-keys"))

    def test_hexdump_env_file_denied(self):
        self.assertIsNotNone(sg.check_command("hexdump -C /tmp/app.env"))

    def test_cut_c_handoff_denied(self):
        self.assertIsNotNone(
            sg.check_command("cut -c1-20 ~/.secrets/global-api-keys")
        )

    def test_head_unrelated_file_allowed(self):
        self.assertIsNone(sg.check_command("head -5 README.md"))

    def test_head_after_safe_command_does_not_false_positive(self):
        # ".secrets/" in a later comment/path must not trip a different segment's head.
        self.assertIsNone(
            sg.check_command("ls /tmp | head -5; echo looking at names not ~/.secrets/")
        )


class RuleCTests(unittest.TestCase):
    def test_ps_denied(self):
        self.assertIsNotNone(sg.check_command("ps aux"))

    def test_ps_in_command_substitution_denied(self):
        self.assertIsNotNone(sg.check_command("p=$(ps -o comm= -p $p)"))

    def test_pgrep_list_denied(self):
        self.assertIsNotNone(sg.check_command("pgrep -fl node"))

    def test_pgrep_f_allowed(self):
        self.assertIsNone(sg.check_command("pgrep -f mac-collab"))

    def test_pgrep_c_allowed(self):
        self.assertIsNone(sg.check_command("pgrep -c -f mac-collab"))


class InvokeSpellingTests(unittest.TestCase):
    """#261 matched bare `od`/`cat`/`ps` only.  Path and command/exec spellings leaked."""

    def test_usr_bin_od_here_string(self):
        reason = sg.check_command('/usr/bin/od -c <<< "$SILICONFLOW_API_KEY"')
        self.assertIsNotNone(reason)
        self.assertIn("SILICONFLOW_API_KEY", reason)

    def test_bin_od_here_string(self):
        self.assertIsNotNone(sg.check_command('/bin/od -c <<< "$GITHUB_TOKEN"'))

    def test_command_od(self):
        self.assertIsNotNone(
            sg.check_command('command od -c <<< "$SILICONFLOW_API_KEY"')
        )

    def test_command_p_od(self):
        self.assertIsNotNone(
            sg.check_command('command -p od -c <<< "$SILICONFLOW_API_KEY"')
        )

    def test_exec_xxd(self):
        self.assertIsNotNone(sg.check_command('exec xxd <<< "$AWS_SECRET_ACCESS_KEY"'))

    def test_backslash_od(self):
        self.assertIsNotNone(sg.check_command(r'\od -c <<< "$SILICONFLOW_API_KEY"'))

    def test_usr_bin_printf_last(self):
        self.assertIsNotNone(sg.check_command('/usr/bin/printf %s "$DB_PASSWORD"'))

    def test_usr_bin_cut_c(self):
        self.assertIsNotNone(
            sg.check_command('/usr/bin/cut -c1-4 <<< "$GITHUB_TOKEN"')
        )

    def test_bin_cat_handoff(self):
        reason = sg.check_command("/bin/cat ~/.secrets/global-api-keys")
        self.assertIsNotNone(reason)
        self.assertIn(".secrets/", reason)

    def test_usr_bin_xxd_handoff(self):
        self.assertIsNotNone(sg.check_command("/usr/bin/xxd ~/.secrets/global-api-keys"))

    def test_command_p_cat_handoff(self):
        self.assertIsNotNone(
            sg.check_command("command -p cat ~/.secrets/global-api-keys")
        )

    def test_bin_ps(self):
        self.assertIsNotNone(sg.check_command("/bin/ps aux"))

    def test_usr_bin_ps(self):
        self.assertIsNotNone(sg.check_command("/usr/bin/ps auxww"))

    def test_exec_ps(self):
        self.assertIsNotNone(sg.check_command("exec ps -ef"))

    def test_usr_bin_pgrep_list(self):
        self.assertIsNotNone(sg.check_command("/usr/bin/pgrep -fl node"))

    def test_command_pgrep_list(self):
        self.assertIsNotNone(sg.check_command("command pgrep -l ssh"))

    def test_path_od_of_ordinary_file_allowed(self):
        self.assertIsNone(sg.check_command("/usr/bin/od -c /tmp/readme.txt"))

    def test_mention_of_usr_bin_od_allowed(self):
        self.assertIsNone(sg.check_command("echo /usr/bin/od is a tool"))

    def test_path_pgrep_f_still_allowed(self):
        self.assertIsNone(sg.check_command("/usr/bin/pgrep -f mac-collab"))


class RuleBAndDTests(unittest.TestCase):
    def test_bearer_without_redaction_denied(self):
        self.assertIsNotNone(
            sg.check_command('curl -H "Authorization: Bearer abc" https://example.com/')
        )

    def test_bearer_with_sed_redaction_allowed(self):
        self.assertIsNone(
            sg.check_command(
                'curl -H "Authorization: Bearer $TOKEN" https://example.com/ 2>&1 | sed "s/${TOKEN}/[REDACTED]/g"'
            )
        )

    def test_secrets_path_with_stderr_merge_denied(self):
        self.assertIsNotNone(
            sg.check_command("set -a; . ~/.secrets/global-api-keys; set +a 2>&1")
        )


class ProtocolTests(unittest.TestCase):
    def _run(self, payload) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_deny_emits_pretooluse_json(self):
        proc = self._run(bash_payload('od -c <<< "$SILICONFLOW_API_KEY"'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertIn("SILICONFLOW_API_KEY", hook["permissionDecisionReason"])

    def test_allow_is_silent(self):
        proc = self._run(bash_payload("echo ${#SILICONFLOW_API_KEY}"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_non_bash_tool_is_ignored(self):
        proc = self._run(
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_malformed_json_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not-json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
