#!/usr/bin/env python3
"""Refuse delete when a note was not written through the fleet notes tool (AFC-728).

apple-notes-coding.sh calls this on stdin JSON from delete_fetch.  Exit 5 matches the script's
not-deletable code.  Re-execs with Homebrew python3 when /usr/bin/python3 is only 3.9 (same pattern
as scripts/simple-notes).
"""
from __future__ import annotations

import json
import os
import sys

if sys.version_info < (3, 11):
    for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.access(candidate, os.X_OK) and os.path.realpath(candidate) != os.path.realpath(
            sys.executable
        ):
            os.execv(candidate, [candidate, os.path.realpath(__file__)] + sys.argv[1:])
    sys.stderr.write(
        "notes: delete guard needs Python 3.11+ (found %d.%d)\n" % sys.version_info[:2]
    )
    sys.exit(1)

_SCRIPTS = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _SCRIPTS)

from simple_tools.notes import is_tool_created_note  # noqa: E402


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.stderr.write("notes: delete guard got invalid JSON\n")
        return 1
    title = str(data.get("title") or "")
    body = str(data.get("body") or "")
    plaintext = str(data.get("plaintext") or "")
    if is_tool_created_note(title, body, plaintext):
        return 0
    sys.stderr.write(
        "this note was not written through the fleet notes tool, so delete was refused\n"
    )
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
