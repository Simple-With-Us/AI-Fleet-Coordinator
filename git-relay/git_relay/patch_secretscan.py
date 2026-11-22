"""A small secret scanner for text about to be posted to Zulip.

`post` and `reply` refuse text it flags, and the listener daemon refuses a wake reply it flags.
It knows common key prefixes, private-key blocks and Zulip-shaped API keys (32 mixed-case
letters and digits), and it always matches any key the caller hands it exactly (the bot keys
the process has loaded).  It reports the kind of match only, never the matched text.

The fleet's own services are covered by name (open-loops F3):  Linear API keys (`lin_api_`,
`lin_oauth_`, `lin_wh_`) and Linear client secrets (which have no prefix, so only an assignment
that names Linear catches them), Sentry auth tokens (`sntrys_`, `sntryu_`), xAI keys (`xai-`),
Infisical service tokens (`st.` plus three parts) and access tokens (a JWT), Cloudflare API
tokens (`cfut_`, `cfat_`, `cfk_`, or a 37 to 40 character key assigned to a name that says
Cloudflare), Coolify tokens (`N|` plus 36 or more letters and digits) and any ALL-CAPS name ending
SECRET, TOKEN, PASSWORD or API_KEY assigned a 16 character value.  `scripts/check-routine-prompts.py`
keeps a local fallback list of the main kinds (not `lin_wh_`, the JWT or the Infisical assignment) for
when it runs outside the repo and cannot load this file;  inside the repo this scanner is the one that
runs, and whatever the fallback flags, this flags too (test_secretscan_fleet pins it).
The hosted MCP Worker keeps a JavaScript copy of the list (`scripts/simple-tools-mcp/src/contract.js`);
`scripts/simple-tools-mcp/test/secretscan-parity.test.mjs` runs this suite's samples through both, so add a
kind to both in one change.

`redact_fields(obj)` is the per-field form for structured data that is about to be written
somewhere durable (the signed facts published to the private fleet-loops-data repo, a ledger):  it
replaces each secret-shaped string field with `[withheld: secret-shaped]` and says where.

Standard library only; structured-output and command imports stay inside their helpers.
There are no imports from this package, so it is safe on the hook fast path, and
`scripts/check-routine-prompts.py` loads it by file path.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Any

__all__ = ["WITHHELD", "basic_forms", "main", "redact_fields", "scan"]

WITHHELD = "[withheld: secret-shaped]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b")),
    # The fleet's own services (open-loops F3).  Specific kinds come before the generic assignment rows
    # below, so the answer names the service.
    ("Linear API key", re.compile(r"\blin_(api|oauth|wh)_[A-Za-z0-9]{20,}")),
    (
        "Linear client secret",
        re.compile(r"(?i)\blinear[\w -]{0,24}secret\s*[=:]\s*['\"]?(?P<value>[A-Za-z0-9]{24,})"),
    ),
    ("Sentry token", re.compile(r"\bsntry[su]_[A-Za-z0-9+/=_-]{20,}")),
    ("xAI key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    (
        "Infisical service token",
        re.compile(r"\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}"),
    ),
    (
        "Infisical credential",
        re.compile(
            r"(?i)\binfisical[\w .-]{0,40}?(token|secret)['\"]?\s*[=:]\s*['\"]?(?P<value>[A-Za-z0-9+/=._-]{24,})"
        ),
    ),
    (
        "access token (JWT)",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    ("Cloudflare token", re.compile(r"\bcf(ut|at|k)_[A-Za-z0-9]{30,}")),
    (
        "Cloudflare token",
        re.compile(
            r"(?i)\bcloudflare[\w ]{0,24}(token|key)\s*[=:]\s*['\"]?(?P<value>[A-Za-z0-9_-]{37,})"
        ),
    ),
    ("Coolify token", re.compile(r"(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])")),
    (
        "assigned credential",
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_?KEY)\s*=\s*['\"]?(?P<value>[A-Za-z0-9+/=._-]{16,})"
        ),
    ),
    ("bearer token", re.compile(r"(?i)\bauthorization:\s*(basic|bearer)\s+[A-Za-z0-9+/=._-]{16,}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?(?P<value>[A-Za-z0-9+/=._-]{16,})"
        ),
    ),
]
_ZULIP_SHAPED = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")


def _zulip_shaped(token: str) -> bool:
    return (
        any(c.isupper() for c in token)
        and any(c.islower() for c in token)
        and any(c.isdigit() for c in token)
    )


# Only complete, unquoted expressions after a source assignment can be nonliteral.
# Bare identifiers, quoted values, prose and bearer headers remain secret candidates.
# The separate routine checker fallback intentionally remains stricter.
_SOURCE_RHS = re.compile(
    r"(?:process\.env\.[A-Z][A-Z0-9_]*|import\.meta\.env\.[A-Z][A-Z0-9_]*)"
    r"(?= *[;,)\n]| *$)"
)


# A complete, unquoted dotted identifier chain (a property access or a call
# target) is a source expression, not a stored literal.  Bare runs, quoted
# values, env-prefixed forms and lowercase-only chains remain candidates.
_DOTTED_RHS = re.compile(r"[A-Za-z_$]+(?:\.[A-Za-z_$]+)+(?= *[;(),\n]| *$)")


def _source_assignment(text: str, match: re.Match[str]) -> bool:
    start = match.start("value")
    # An optional quote consumed by the detector means this is a literal, always.
    if start and text[start - 1] in "\"'":
        return False
    line = text[text.rfind("\n", 0, start) + 1 : start]
    # Existing `if (...) cfg.key =` lines use the assignment's exact suffix.
    assignment = re.search(
        r"(?:^|[;{} )])((?:(?:const|let|var) +)?[A-Za-z_$][A-Za-z0-9_$.]*) *= *$", line
    )
    labeled_arg = False
    if assignment is not None:
        # A source line containing quotes or template delimiters is conservatively scanned.
        if any(c in line for c in "\"'`"):
            return False
    else:
        # A labeled call argument (Swift/Kotlin) puts the keyword label right
        # after `(` or `,` while an earlier `(` is still unclosed, possibly
        # across lines.  Comma prose has no call paren, and quoted or
        # line-start colon pairs (JSON/YAML) never reach this path.  Paren
        # depth is tracked across the whole window so a closure inside the
        # call keeps the paren open and an earlier string argument is fine.
        # The separator must be the labeled-argument colon:  a `=` keyword
        # argument inside parens keeps the `=` path's stricter treatment.
        # Clearing is limited to call syntax; prose and quoted pairs still refuse.
        if ":" not in text[match.start() : start]:
            return False
        before = text[max(0, match.start() - 300) : match.start()]
        tail = re.search(r"[(,]\s*$", before)
        if tail is None:
            return False
        # The innermost open paren must be a call paren (right after an
        # identifier char). Balanced grouping pairs are popped when they
        # close, so they do not hide the call; parens inside string literals
        # are skipped, so prose or a quoted paren cannot open a call.
        stack = []
        quote = ""
        for i, ch in enumerate(before):
            if quote:
                if ch == quote and before[i - 1] != "\\":
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == "(":
                stack.append(i > 0 and (before[i - 1].isalnum() or before[i - 1] in "_$"))
            elif ch == ")" and stack:
                stack.pop()
        labeled_arg = bool(stack) and stack[-1] and not quote
        if not labeled_arg:
            return False
    if _SOURCE_RHS.match(text, start) is not None:
        return True
    chain = _DOTTED_RHS.match(text, start)
    if chain is None:
        return False
    value = chain.group(0)
    if value.startswith(("process.env.", "import.meta.env.")):
        return False
    if re.search(r"[a-z][A-Z]", value) is not None:
        return True
    # Lowercase-only chains stay candidates after `=`, but in a labeled call
    # argument a property chain is idiomatically lowercase (Swift/Kotlin).
    return labeled_arg


# Public configuration identifiers, not credential values. Keep exact bindings:
# an uppercase-looking string alone is not evidence that it names configuration.
_PUBLIC_PYTHON_ENV_BINDINGS = {
    "LISTENER_READ_TOKEN": "INFISICAL_LISTENER_READ_TOKEN",
    "OPERATOR_VOICE_TOKEN": "INFISICAL_OPERATOR_VOICE_TOKEN",
    "WAKE_ROUTER_TOKEN": "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN",
}


def _python_source_value_spans(text: str) -> frozenset[tuple[int, int]]:
    """Only generic-detector false positives proved by complete Python syntax.

    No source is executed. Quoted function names, comments, docstrings, bare
    names and unknown string constants cannot qualify. Every other match is
    still scanned independently, including literal arguments inside a call.
    """
    import ast

    if len(text) > 128 * 1024:
        return frozenset()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return frozenset()
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(node, end=False):
        line = node.end_lineno if end else node.lineno
        column = node.end_col_offset if end else node.col_offset
        # AST columns are UTF-8 bytes; scanner spans are Unicode characters.
        return starts[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))

    spans = set()
    for visited, node in enumerate(ast.walk(tree), 1):
        if visited > 10000:
            return frozenset()  # Budget failure never admits a partial analysis.
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and _PUBLIC_PYTHON_ENV_BINDINGS.get(node.targets[0].id) == node.value.value
        ):
            value = node.value
            left, right = offset(value), offset(value, True)
            # Require one ordinary quoted identifier, not an encoded,
            # concatenated or multiline expression that happens to decode alike.
            literal = text[left:right]
            if literal in ("'" + value.value + "'", '"' + value.value + '"'):
                spans.add((left + 1, right - 1))
        elif (
            isinstance(node, ast.keyword)
            and node.arg == "token"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_wake_router_token"
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Name)
            and node.value.args[0].id == "env"
            and not node.value.keywords
            and text[offset(node.value) : offset(node.value, True)] == "_wake_router_token(env)"
        ):
            # Only this public validator expression is classified here.
            # Other callable identifiers can themselves look like credentials
            # and retain the generic detector's conservative treatment.
            spans.add((offset(node.value.func), offset(node.value.func, True)))
    return frozenset(spans)


def _scan(text: str, known: Iterable[str], source_spans: frozenset[tuple[int, int]]) -> str | None:
    for value in known:
        if value and len(value) >= 8 and value in text:
            return "a loaded credential"
    for label, pattern in _PATTERNS:
        if "value" in pattern.groupindex:
            for match in pattern.finditer(text):
                if _source_assignment(text, match):
                    continue
                if (
                    label in ("assigned credential", "assigned secret")
                    and match.span("value") in source_spans
                ):
                    continue
                return label
        elif pattern.search(text):
            return label
    for match in _ZULIP_SHAPED.finditer(text):
        if _zulip_shaped(match.group()):
            return "a Zulip-shaped API key"
    return None


def scan(text: str, known: Iterable[str] = ()) -> str | None:
    """Raw text keeps its existing conservative policy, without source classification."""
    return _scan(text, known, frozenset())


# Exact generic-fixture approvals are release code, never caller input. Each
# entry requires immutable full-content review and a durable review record in
# the source change. See docs/security/reviewed-generic-fixtures.md.
_REVIEWED_GENERIC_FIXTURES: frozenset[tuple[str, str, str]] = frozenset(
    {
        (
            "Simple-With-Us/CodeCaps",
            "ios/CodeCapsCompanion/Widgets/WidgetPresentation.swift",
            "d6bd7e7ff0a69af9cfd4002f1e21db90f66b87315a4dc69cbd2fc1c585be0b2b",
        ),
        (
            "Simple-With-Us/CodeCaps",
            "ios/CodeCapsCompanion/App/Models/CompanionQuotaModel.swift",
            "1e44f5721cf37b067a3a1a2091fc316d2a9f95546effc22d756126eca687894b",
        ),
        (
            "Simple-With-Us/Usage-Monitor",
            "scripts/lib/run-session-token-collector.mjs",
            "e96f73542ff0a5923af4d53981f96693bb67f80444e54cf94a520737ac5418f4",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/linear-board/test_board.py",
            "9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "git-relay/git_relay/server.py",
            "82b53da38ce0a110a3f25b947fbcabcfcef031a8295795d72f46f48d55d79305",
        ),
    }
)
# Exact reviewed match spans, in decoded Unicode character offsets, not bytes.
# Full-file digest remains mandatory. Values are never stored in this registry.
# CT offsets come from the source owner's whole-file location-only review.
_REVIEWED_GENERIC_MATCHES: dict[tuple[str, str, str], frozenset[tuple[str, int, int, int, int]]] = {
    (
        "Simple-With-Us/AI-Fleet-Coordinator",
        "docs/security/reviewed-generic-fixtures.md",
        "825f87972a207b3ad52a99250f230db13ef55777be78eb566208d674d24419c6",
    ): frozenset(
        [
            ("assigned credential", 5876, 5917, 5893, 5917),
            ("assigned credential", 5935, 5972, 5952, 5972),
            ("assigned secret", 6085, 6115, 6093, 6115),
            ("assigned secret", 6278, 6312, 6286, 6312),
        ]
    ),
    (
        "Simple-With-Us/AI-Fleet-Coordinator",
        "git-relay/git_relay/server.py",
        "182f99d6c71bb9616e4ac9381d2fadfbe26e8760fc3dac873a6b3c58d2f2c752",
    ): frozenset({("assigned secret", 51282, 51325, 51290, 51325)}),
    (
        "Simple-With-Us/Congress.Trade",
        "app/src/security/__tests__/originLock.test.ts",
        "cc1f0a0d5565309e9268ecd4e37d15d09c5c2e85f4de3cd54638be87bee555c5",
    ): frozenset({("assigned secret", 547, 580, 557, 580)}),
    (
        "Simple-With-Us/Usage-Monitor",
        "upgrade-reviewed-hub.py",
        "b29e5a664f289d6bf11710d8130cfabf45f514d971f05b97fc5f7e641617c71e",
    ): frozenset({("assigned secret", 8115, 8143, 8123, 8143)}),
}

_GENERIC_ASSIGNMENTS = frozenset({"assigned credential", "assigned secret"})

# Immutable public package-integrity reviews, not a general SRI exemption.
# The complete files and public artifact bytes were independently verified.
# See docs/security/reviewed-public-integrities.md for provenance and authority.
_REVIEWED_PUBLIC_INTEGRITIES: frozenset[tuple[str, str, str]] = frozenset(
    {
        (
            "Simple-With-Us/Congress.Trade",
            "app/package-lock.json",
            "72462e4c39a4a9a2ba6927214072e0f607565db11c3aa1be034e31eb197af7b9",
        ),
        (
            "Simple-With-Us/Congress.Trade",
            "app/package-lock.json",
            "eb44b787f36b8149d5173d84e39d23d5c2b235721cc8c6eab9a6970d5ade5bd1",
        ),
        (
            "Simple-With-Us/Congress.Trade",
            "app/deno.lock",
            "faf4d3d6281fb4c94f0933043612483fd2f5463c469d0b5bb413365d8530a084",
        ),
        (
            "Simple-With-Us/Congress.Trade",
            "app/package-lock.json",
            "5ff0b0a6007a6df394d86060251d58a1a7ebd9b3e6330a3c5fc24021793b6932",
        ),
    }
)


def scan_file(repo: str, path: str, text: str, known: Iterable[str] = ()) -> str | None:
    """File-aware scan with exact reviewed subtypes kept as separate mechanisms.

    Valid bounded Python source can classify public environment-name bindings
    and the exact public token-validator expression, never arbitrary calls or values.
    Raw-message scan() remains conservative. No classification executes source.

    Match-pinned fixtures require the exact nonempty effective match census.
    Generic fixtures waive generic assignments only.  Public-integrity reviews
    waive only the Zulip-shape result for their two immutable files; all other
    patterns and every loaded value remain absolute, even if short.
    No basename/path prefix, caller approval flag or renamed/corrupt-file waiver.
    """
    import hashlib

    key = (repo, path, hashlib.sha256(text.encode("utf-8")).hexdigest())
    if key in _REVIEWED_PUBLIC_INTEGRITIES:
        known = tuple(known)
        for value in known:
            if value and value in text:
                return "a loaded credential"
        kind = scan(text, known)
        return None if kind == "a Zulip-shaped API key" else kind
    reviewed_matches = _REVIEWED_GENERIC_MATCHES.get(key)
    if key not in _REVIEWED_GENERIC_FIXTURES and reviewed_matches is None:
        # Language-aware classification, not a file/path or digest waiver.
        # Only validated Python syntax can contribute non-secret source spans.
        spans = _python_source_value_spans(text) if path.endswith(".py") else frozenset()
        return _scan(text, known, spans)
    for value in known:
        if value and value in text:
            return "a loaded credential"
    observed = set()
    for label, pattern in _PATTERNS:
        if label not in _GENERIC_ASSIGNMENTS:
            if pattern.search(text):
                return label
            continue
        if reviewed_matches is None:
            continue  # Legacy exact-file entry, unchanged by the match-pinned route.
        for match in pattern.finditer(text):
            if _source_assignment(text, match):
                continue
            identity = (label, *match.span(), *match.span("value"))
            if identity not in reviewed_matches:
                return label  # A digest update alone cannot admit another match.
            observed.add(identity)
    if reviewed_matches is not None and (not reviewed_matches or observed != set(reviewed_matches)):
        return "assigned secret"  # Missing/stale metadata is unverifiable, so refuse.
    if any(_zulip_shaped(match.group()) for match in _ZULIP_SHAPED.finditer(text)):
        return "a Zulip-shaped API key"
    return None


def basic_forms(email: str, key: str) -> list[str]:
    """The key and its base64 Basic-auth token, for `known`."""
    return [key, base64.b64encode(("%s:%s" % (email, key)).encode("utf-8")).decode("ascii")]


# --------------------------------------------------------------------------------------------
# Per-field redaction
# --------------------------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
MAX_DEPTH = 64


def _key_path(path: str, key: Any) -> str:
    """`path.name` for a dict key that is a plain identifier, else `path["key"]` (JSON quoted)."""
    if isinstance(key, str) and _IDENT_RE.fullmatch(key):
        return "%s.%s" % (path, key)
    import json  # lazy:  the hook fast path imports this module and never needs it

    return "%s[%s]" % (path, json.dumps(key, ensure_ascii=True, default=str))


def redact_fields(obj: Any, known: Iterable[str] = ()) -> tuple[Any, list[str]]:
    """(redacted copy of `obj`, the paths that were redacted).

    Every string that `scan` flags (with `known` as the exact values to refuse) is replaced whole by
    `[withheld: secret-shaped]`;  nothing else changes.  dicts, lists and tuples are walked, and a
    secret-shaped dict KEY is replaced too (then numbered, `[withheld: secret-shaped] #2`, if that
    would collide).  Anything else (numbers, None, bytes, sets, other objects) is returned as it is.
    A structure nested deeper than MAX_DEPTH is withheld whole (it may be cyclic), and the path says
    so.  `obj` itself is never changed.

    A path is `$` for the root, `.name` for a dict key that is a plain identifier, `["key"]` (JSON
    quoted) for any other key and `[n]` for a list or tuple index:  `$.items[2].title`.  A path never
    holds a secret, because a secret-shaped key is written as `["[withheld: secret-shaped]"]`.  The
    values it replaced are not returned, logged or kept."""
    known_list = [v for v in known if v]
    paths: list[str] = []

    def hit(value: str) -> bool:
        return scan(value, known_list) is not None

    def walk(node: Any, path: str, depth: int) -> Any:
        if isinstance(node, str):
            if hit(node):
                paths.append(path)
                return WITHHELD
            return node
        if not isinstance(node, (dict, list, tuple)):
            return node
        if depth >= MAX_DEPTH:
            paths.append(path)
            return WITHHELD
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for key, value in node.items():
                if isinstance(key, str) and hit(key):
                    new_key, n = WITHHELD, 1
                    while new_key in out:
                        n += 1
                        new_key = "%s #%d" % (WITHHELD, n)
                    child = _key_path(path, WITHHELD)
                    paths.append(child)
                    out[new_key] = walk(value, child, depth + 1)
                else:
                    out[key] = walk(value, _key_path(path, key), depth + 1)
            return out
        items = [walk(value, "%s[%d]" % (path, i), depth + 1) for i, value in enumerate(node)]
        return tuple(items) if isinstance(node, tuple) else items

    clean = walk(obj, "$", 0)
    return clean, list(dict.fromkeys(paths))


def main(argv=None, *, stdin=None, stdout=None) -> int:
    """Scan standard input and report only a field name and safe reason.

    The module is shipped with the shared fleet CLI release.  Call its absolute
    path with Python isolated mode; credential text belongs on stdin, never argv.
    """
    import argparse
    import json
    import sys

    class ScanParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError("invalid_arguments")

    parser = ScanParser(
        prog="python3 -I secretscan.py", description="Scan stdin without echoing its text"
    )
    parser.add_argument(
        "--field", default="text", help="allow-listed field identifier for the result"
    )
    output = sys.stdout if stdout is None else stdout
    try:
        args = parser.parse_args(argv)
    except ValueError:
        output.write(json.dumps({"ok": False, "code": "invalid_arguments"}) + "\n")
        return 2
    fields = {
        "text",
        "request",
        "request_source",
        "action_taken",
        "result",
        "body",
        "topic",
        "title",
        "description",
        "status_text",
        "filename",
        "url",
        "channel",
    }
    if args.field not in fields:
        output.write(json.dumps({"ok": False, "code": "invalid_field"}) + "\n")
        return 2
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin) if stdin is None else stdin
        raw = stream.read(256 * 1024 + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes):
            raise TypeError("invalid_input")
    except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
        output.write(json.dumps({"ok": False, "field": args.field, "code": "invalid_input"}) + "\n")
        return 2
    reason = None
    if not raw:
        output.write(json.dumps({"ok": False, "field": args.field, "code": "invalid_input"}) + "\n")
        return 2
    if len(raw) > 256 * 1024:
        reason = "input_too_large"
    else:
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeError:
            reason = "invalid_utf8"
    if reason:
        output.write(json.dumps({"ok": False, "field": args.field, "code": reason}) + "\n")
        return 2
    kind = scan(text)
    result = {
        "ok": kind is None,
        "field": args.field,
        "code": "refused_secret" if kind else "safe",
        "kind": kind,
    }
    output.write(json.dumps(result) + "\n")
    return 1 if kind else 0


if __name__ == "__main__":
    raise SystemExit(main())
