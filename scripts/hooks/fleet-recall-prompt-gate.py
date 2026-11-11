#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge an agent to evaluate Fleet RAG search on trigger keywords.

Runs on UserPromptSubmit in Claude Code, Codex, and other supported platforms.
Inspects the user's prompt for recurring failure signals, infrastructure changes,
or investigative inquiries. When a trigger matches, it injects a high-priority
instruction to run `recall_search` before embarking on diagnostic loops.

Stdlib only; execution time < 10ms. Set FLEET_RECALL_HOOKS=0 to disable.
"""
from __future__ import annotations

import json
import os
import re
import sys

TRIGGER_PATTERN = re.compile(
    r"\b("
    # Errors and failures
    r"err(?:or)?|fail(?:ed|ure)?|50[0234]|40[134]|stall(?:ed)?|timeout|crash(?:ed)?|"
    r"traceback|sentry|exception|segfault|panic|"
    # Infrastructure, deployments, environment
    r"deploy(?:ed|ment)?|coolify|pm2|launchd|tailscale|cloudflare|tunnel|qdrant|"
    r"bge-m3|tei|redis|postgres|sqlite|secret|credential|token|"
    # Investigation & bug resolution
    r"investigate|debug|diagnose|why\s+(?:is|does|did|are)|broken"
    r")\b",
    re.IGNORECASE,
)

PROMPT_CONTEXT = (
    "[FLEET RAG GATE]: This prompt touches an error, infra, or troubleshooting trigger. "
    "Evaluate whether to run `recall_search` BEFORE inspecting code or running trial-and-error "
    "commands, to avoid re-deriving solutions another seat has already documented."
)


def extract_prompt_text(hook_data: dict) -> str:
    """Extract user prompt text from diverse hook payloads."""
    for key in ("prompt", "userPrompt", "text", "query", "message"):
        val = hook_data.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            for subkey in ("content", "text"):
                subval = val.get(subkey)
                if isinstance(subval, str) and subval.strip():
                    return subval
    return ""


def main() -> int:
    try:
        if os.environ.get("FLEET_RECALL_HOOKS", "1") == "0":
            return 0

        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return 0

        try:
            hook_data = json.loads(raw)
        except Exception:
            return 0

        if not isinstance(hook_data, dict):
            return 0

        text = extract_prompt_text(hook_data)
        if not text:
            return 0

        if TRIGGER_PATTERN.search(text):
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": PROMPT_CONTEXT,
                }
            }
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
