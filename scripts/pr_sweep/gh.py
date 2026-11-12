"""The one place pr-sweep talks to GitHub: `gh api graphql` queries and `gh api` GET calls, never a mutation.

Every core subcommand (inventory, plan, status, needs-work, audit) is read-only on GitHub.  `Gh.graphql` refuses
a document whose first operation is a mutation or subscription, and `Gh.rest` sends GET only.  The workers that
`pr-sweep run` launches do the writing, under the rules in rules.md.

Tests replace `exec_fn(argv, stdin_text) -> (rc, stdout, stderr)`, so no test reaches the network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

ExecFn = Callable[[Sequence[str], "str | None"], "tuple[int, str, str]"]

_OP = re.compile(r"^\s*(?:#[^\n]*\n\s*)*(query|mutation|subscription|fragment|\{)", re.DOTALL)
_REST_PATH = re.compile(r"^[A-Za-z0-9_.\-/]+(?:\?[A-Za-z0-9_.\-=&%,]*)?$")
RETRY_MARKERS = ("502", "503", "504", "timeout", "timed out", "connection reset", "EOF")
_CHILD_ENV = {
    "GH_PROMPT_DISABLED": "1",
    "GH_PAGER": "cat",
    "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}


class GhError(RuntimeError):
    """gh failed or answered with something unusable.  The message never carries a credential (gh prints none)."""


def default_exec(
    argv: Sequence[str], stdin_text: str | None, timeout: float = 120.0
) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update(_CHILD_ENV)
    try:
        proc = subprocess.run(
            list(argv),
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _first_operation(document: str) -> str:
    m = _OP.match(document)
    return m.group(1) if m else ""


class Gh:
    def __init__(
        self,
        exec_fn: ExecFn | None = None,
        *,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._exec = exec_fn or (lambda argv, stdin: default_exec(argv, stdin))
        self._attempts = max(1, attempts)
        self._sleep = sleep

    def _run(self, argv: list[str], stdin_text: str | None = None) -> str:
        last = ""
        for attempt in range(self._attempts):
            rc, out, err = self._exec(argv, stdin_text)
            if rc == 0:
                return out
            last = (err or out or f"exit {rc}").strip().splitlines()[-1:] or [f"exit {rc}"]
            last = last[0][:300]
            if not any(m in (err or "") for m in RETRY_MARKERS) or attempt == self._attempts - 1:
                break
            self._sleep(2.0 * (attempt + 1))
        raise GhError(f"gh {' '.join(argv[1:3])} failed: {last}")

    def graphql(self, document: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one read-only GraphQL document.  Variables go through -f (strings) and -F (numbers, booleans)."""
        op = _first_operation(document)
        if op not in ("query", "{", "fragment"):
            raise GhError(
                f"pr-sweep sends read-only queries only, not a {op or 'unrecognised'} document"
            )
        argv = ["gh", "api", "graphql", "-f", "query=" + document]
        for key, value in (variables or {}).items():
            if value is None:
                continue
            if isinstance(value, bool) or isinstance(value, int):
                argv += ["-F", f"{key}={json.dumps(value)}"]
            else:
                argv += ["-f", f"{key}={value}"]
        out = self._run(argv)
        try:
            body = json.loads(out)
        except ValueError as exc:
            raise GhError(f"gh api graphql returned no JSON: {exc}")
        if not isinstance(body, dict):
            raise GhError("gh api graphql returned a non-object")
        if body.get("errors") and not body.get("data"):
            first = (
                body["errors"][0]
                if isinstance(body["errors"], list) and body["errors"]
                else body["errors"]
            )
            raise GhError(
                f"GraphQL error: {str(first.get('message') if isinstance(first, dict) else first)[:300]}"
            )
        return body

    def rest(self, path: str, *, paginate: bool = False) -> Any:
        """GET one REST path (no leading slash), optionally all pages (gh --paginate --slurp)."""
        if not _REST_PATH.match(path) or ".." in path:
            raise GhError(f"refusing an odd REST path: {path[:80]!r}")
        argv = ["gh", "api", "-X", "GET", path]
        if paginate:
            argv[2:2] = ["--paginate", "--slurp"]
        out = self._run(argv)
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise GhError(f"gh api {path} returned no JSON: {exc}")
        if paginate and isinstance(data, list) and all(isinstance(page, list) for page in data):
            return [item for page in data for item in page]
        return data
