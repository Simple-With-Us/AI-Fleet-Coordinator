"""The Operator's session registry over its HTTPS API, for the registry machinery on this host.

The registry API (listener_registry.py, served by the listener under /registry/v1/) is the
one store now:  the server's file, reached over HTTPS with a signed call.  The MiniMax
letter helper (scripts/mm_session_letter.py) grew the first client for it;  this module is
the same client discipline for the package's own registry callers, the operator-registry
CLI (operator_registry.main, which the letter sweep drives) and the platform-letter
registry side (platform_letter.registry_main).  Both speak the registry CLI's dialect, so
a call here takes the op and its fields under the CLI's names and answers with the API's
JSON, exactly as the local path's functions answer.

Configuration is the daemon's own, read from the caller's environment and nothing else:

  SIMPLE_TOOLS_OPERATOR_REGISTRY_URL        the API's base URL (the bare origin;  the
                                            /registry/v1/ prefix is the client's to add)
  SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL  the signing principal (default OPERATOR:
                                            these callers are the Operator's own machinery,
                                            and the local paths they replace act as it)
  SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY_ENV    names the env var holding the signing key
  SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY        that var's default name

With no URL set there is no API path:  base_url answers "" and every caller keeps its
local behaviour, byte for byte.  With a URL set the caller uses this transport instead of
the local file, never alongside it:  a call that fails answers with the failure, and no
caller falls back to writing the local store, because two stores that both take writes
are the split this cutover exists to end.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

URL_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL"
PRINCIPAL_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL"
KEY_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"
KEY_REF_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY_ENV"

#: The principal these callers sign as when the environment names none.  The registry
#: CLI and the platform-letter registry side are the Operator's own machinery:  the local
#: paths issue letters with issuer OPERATOR, and the API refuses a lesser principal the
#: same letter, so OPERATOR is the only default that keeps the calls working.
DEFAULT_PRINCIPAL = "OPERATOR"

#: CLI spellings that name a registry field under another name, folded for the API (the
#: same folds mm_session_letter applies, so every client sends one dialect).
_ARG_ALIASES = {
    "location": "verified_host",
    "scope": "profile_scope",
    "session_id": "native_session_id",
}


def base_url(env: Mapping[str, str] | None = None) -> str:
    """The configured registry API base URL, or "" when this host has no API path."""
    env = os.environ if env is None else env
    return str(env.get(URL_ENV) or "").strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, *args: Any, **kwargs: Any
    ) -> None:  # a signed request never follows a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fold(params: Mapping[str, Any]) -> dict[str, Any]:
    """The call's fields under the API's names:  CLI spellings folded, the local file's
    name dropped (it names a file on the registry's host, not a field of the call), and
    unset fields left out rather than sent as nulls."""
    out: dict[str, Any] = {}
    for name, value in params.items():
        if name == "db" or value is None:
            continue
        out[_ARG_ALIASES.get(name, name)] = value
    return out


def call(
    env: Mapping[str, str] | None,
    op: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: int = 25,
) -> dict[str, Any]:
    """One registry call over the HTTPS registry API, answered as its JSON.

    The answer is the API's own dict on success and on refusal alike.  A call that could
    not be made answers with an error dict in the same dialect the local transports use:
    `not_configured` when this host's configuration cannot sign the call, `timeout` when
    the registry did not answer or its answer broke off, and `outcome_unknown` riding on
    a failed write whose answer was lost, because a write may have landed before its
    answer did."""
    from . import listener_registry as RG

    env = os.environ if env is None else env
    url = base_url(env)
    if not url:
        return {"error": "no registry API URL is configured", "code": "not_configured"}
    if op not in RG.OPS:
        return {"error": "unknown registry op %r" % op, "code": "bad_op"}
    try:
        parts = urllib.parse.urlsplit(url)
        loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return {"error": "the registry URL is not usable", "code": "not_configured"}
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        return {"error": "the registry URL must be https", "code": "not_configured"}
    principal = str(env.get(PRINCIPAL_ENV) or "").strip().upper() or DEFAULT_PRINCIPAL
    key_env = str(env.get(KEY_REF_ENV) or "").strip() or KEY_ENV
    key = str(env.get(key_env) or "").strip()
    if not key:
        return {
            "error": "the registry key env %s is not set" % key_env,
            "code": "not_configured",
        }
    fields = _fold(params or {})
    # mark-exported names its ids positionally on the CLI;  the API names them in the body.
    positional = fields.pop("_args", [])
    if positional and op == "mark-exported":
        fields["outbox_ids"] = positional
    path = RG.PREFIX + op
    if op in RG.READ_OPS:
        method = "GET"
        query = urllib.parse.urlencode(
            {name: value for name, value in fields.items() if value is not True}
        )
        body = b""
    else:
        method = "POST"
        query = ""
        body = json.dumps(fields).encode("utf-8")
    full_url = url.rstrip("/") + path + (("?" + query) if query else "")
    request = urllib.request.Request(
        full_url, data=body if method == "POST" else None, method=method
    )
    # An explicit User-Agent:  the API's host is Cloudflare-fronted and Cloudflare
    # answers 1010 to Python's default urllib agent (the deploy-mapped-worker note),
    # so the default would never reach the listener.
    request.add_header("User-Agent", "simple-tools-registry/1.0")
    for name, value in RG.headers(key, method, path, query, principal, body, time.time()).items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            answer = None
        if isinstance(answer, dict):
            answer.setdefault("code", "http_%s" % exc.code)
            if method == "POST" and exc.code >= 500:
                # A server-side failure can surface after the write was applied, so a
                # write's outcome is unknown, as on the timeout path below.  A 4xx
                # answer is the API's own refusal:  the write was not applied.
                answer.setdefault("outcome", "outcome_unknown")
            return answer
        refusal: dict[str, Any] = {
            "error": "the registry answered HTTP %s" % exc.code,
            "code": "http_error",
        }
        if method == "POST" and exc.code >= 500:
            refusal["outcome"] = "outcome_unknown"
        return refusal
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        # The transport failed or the answer broke off mid-body (an HTTPException such
        # as a truncated read).  A write may have been applied before the answer was lost.
        answer = {"error": "the registry did not answer in time", "code": "timeout"}
        if method == "POST":
            answer["outcome"] = "outcome_unknown"
        return answer
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"error": "unreadable answer", "code": "bad_json", "outcome": "outcome_unknown"}
    if not isinstance(answer, dict):
        return {
            "error": "the registry answer was not an object",
            "code": "bad_shape",
            "outcome": "outcome_unknown",
        }
    return answer
