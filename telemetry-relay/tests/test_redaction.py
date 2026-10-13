"""Relay blocked_values stay in lockstep with scripts/agent_sync/secretscan.py."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from relay_common import SCRIPTS, block, config_text

sys.path.insert(0, str(SCRIPTS))
from agent_sync.secretscan import scan  # noqa: E402


def _canon(pattern: str) -> str:
    """Python raw-string vs YAML quote escaping (`\\"` vs `"`) is the same regex."""
    return pattern.replace('\\"', '"').replace("\\'", "'")


# Patterns that cannot be transcribed 1:1 into the collector's RE2
# blocked_values.  Each has a documented stand-in or a deliberate skip.
_DIVERGENCES = {
    r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b": "split into two RE2 alternatives",
    r"(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])": "lookarounds rewritten as (^|[^A-Za-z0-9])...([^A-Za-z0-9]|$)",
}


def _secretscan_raw_patterns() -> list[str]:
    src = (SCRIPTS / "agent_sync" / "secretscan.py").read_text(encoding="utf-8")
    chunk = src[src.index("_PATTERNS") : src.index("_ZULIP_SHAPED")]
    # File text, not eval: r"\b..." in the source is backslash + b, which is the pattern.
    found = re.findall(r're\.compile\(r("(?:\\.|[^"\\])*")\)', chunk)
    return [item[1:-1].replace('\\"', '"') for item in found]


def _yaml_single(quoted: str) -> str:
    """Decode a YAML single-quoted scalar (backslashes stay literal)."""
    if not (quoted.startswith("'") and quoted.endswith("'")):
        raise ValueError("expected a YAML single-quoted scalar")
    return quoted[1:-1].replace("''", "'")


def _blocked_values(cfg: str) -> list[str]:
    body = block(cfg, "redaction")
    values = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            values.append(_yaml_single(stripped[2:]))
    return values


class RedactionParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = config_text()
        cls.blocked = _blocked_values(cls.cfg)

    def test_every_secretscan_regex_is_transcribed_or_documented(self) -> None:
        raw = _secretscan_raw_patterns()
        self.assertGreaterEqual(len(raw), 15)
        blocked_set = {_canon(p) for p in self.blocked}
        for pattern in raw:
            if _canon(pattern) in blocked_set:
                continue
            self.assertIn(
                pattern,
                _DIVERGENCES,
                "secretscan pattern has no blocked_values row and no documented divergence: %s"
                % pattern,
            )

    def test_github_and_coolify_standins_are_present(self) -> None:
        self.assertIn(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", self.blocked)
        self.assertIn(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b", self.blocked)
        self.assertIn(
            r"(^|[^A-Za-z0-9])[0-9]{1,5}\|[A-Za-z0-9]{36,}([^A-Za-z0-9]|$)",
            self.blocked,
        )

    def test_zulip_heuristic_is_not_claimed_as_a_regex(self) -> None:
        # The Zulip-shaped 32-char mixed-case token needs code, not a regex.
        # The config comment must say so, and no 32-char class is in blocked_values.
        self.assertIn("Zulip-shaped", self.cfg)
        for value in self.blocked:
            self.assertNotIn("{32}", value)

    def test_prompt_text_without_a_secret_is_not_flagged(self) -> None:
        prompt = "Summarize the quarterly report in three bullets."
        self.assertIsNone(scan(prompt))
        for value in self.blocked:
            self.assertIsNone(re.search(value, prompt), value)

    def test_a_secret_inside_a_prompt_is_flagged_by_secretscan(self) -> None:
        # Values are built at runtime so this file never holds a key-shaped literal.
        filler = "abcdefghij0123456789klmnop"
        token = "sk-" + "ant-" + filler
        text = "here is a key %s in a prompt" % token
        self.assertEqual(scan(text), "Anthropic or OpenAI key")
        matching = [v for v in self.blocked if re.search(v, text)]
        self.assertTrue(matching, "blocked_values must catch an Anthropic-shaped key")

    def test_redaction_keeps_unknown_keys_so_prompts_survive(self) -> None:
        body = block(self.cfg, "redaction")
        self.assertIn("allow_all_keys: true", body)


class SecretscanSourceTests(unittest.TestCase):
    def test_relay_imports_the_repo_copy(self) -> None:
        path = Path(scan.__code__.co_filename).resolve()
        self.assertEqual(path, (SCRIPTS / "agent_sync" / "secretscan.py").resolve())


if __name__ == "__main__":
    unittest.main()
