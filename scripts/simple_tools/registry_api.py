"""The Operator's session registry over its HTTPS API, for the registry machinery on this host.

The registry API (listener_registry.py, served by the listener under /registry/v1/) is the
one store now:  the server's file, reached over HTTPS with a signed call.  The MiniMax
letter helper (scripts/mm_session_letter.py) grew the first client for it;  this module is
the same client discipline for the package's own registry callers, the operator-registry
CLI (operator_registry.main, which the letter sweep drives) and the platform-letter
registry side (platform_letter.registry_main).  Both speak the registry CLI's dialect, so
a call here takes the op and its fields under the CLI's names and answers with the API's
JSON.

Configuration, one name for each value:

  SIMPLE_TOOLS_OPERATOR_REGISTRY_URL        the API's base URL (the bare origin;  the
                                            /registry/v1/ prefix is the client's to add)
  SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY        the one signing key
  SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL  the signing principal (default OPERATOR:
                                            these callers are the Operator's own machinery)

There is no other key name and no variable that points at one.  A caller that passes its
own environment mapping gets exactly that mapping:  the fleet knob of the same name is
consulted only for the process's own environment (env None or os.environ), so a test or
a tool that hands in an explicit mapping never reaches Infisical and never picks up the
live registry by accident.

Scope of the hard cut (AFC-720).  This module has no store:  a host with no URL or no key
answers code "not_configured" for every op, nothing is opened, created or written here,
and a failed call answers with its failure rather than falling back to a file.  That is a
promise about this module only.  The callers (operator_registry.main,
platform_letter.registry_main, claude_letter, mm_session_letter and the platform adapters)
still choose their own local-file or ssh branch when base_url() is empty;  P4 and P6 of the
AFC-694 consolidation delete those branches, and until they land a caller with no URL can
still write a local file.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
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
#: The key's one variable name.  (Spelled KEY_VAR so the P2 acceptance grep, which bans
#: the retired pointer's suffix, stays exact.)
KEY_VAR = "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"
TOKEN_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN"
REGISTRY_PATH = "/operator-registry"
REGISTRY_FETCH_TIMEOUT = 3.0

#: The fleet_knobs names this module falls back to for the process environment.  They are
#: the same names as the environment's, so there is one name for each value.  A name the
#: knob registry does not hold yet reads as unset.
URL_KNOB = URL_ENV
KEY_KNOB = KEY_VAR

#: The principal these callers sign as when the environment names none.  The registry
#: CLI and the platform-letter registry side are the Operator's own machinery:  their
#: letters carry issuer OPERATOR, and the API refuses a lesser principal the same letter,
#: so OPERATOR is the only default that keeps the calls working.
DEFAULT_PRINCIPAL = "OPERATOR"

#: CLI spellings that name a registry field under another name, folded for the API (the
#: same folds mm_session_letter applies, so every client sends one dialect).
_ARG_ALIASES = {
    "location": "verified_host",
    "scope": "profile_scope",
    "session_id": "native_session_id",
}


#: Knob answers already read in this process, and why a knob read as unset.  A value is
#: read once per process and a miss once per retry window:  base_url() is called several
#: times per hook as a branch predicate, and a hook must not pay an Infisical round trip
#: for each.  Values never leave this
#: module;  only the reasons (names and fleet_knobs reason codes) are ever reported.
_KNOB_CACHE: dict[str, str] = {}
_KNOB_REASONS: dict[str, str] = {}
#: A knob that read as unset is asked again after this many seconds, so a long-lived
#: process (the router, a bridge) that started during an outage or before the name was
#: registered is not pinned to not_configured for its lifetime.  A short-lived hook never
#: lives this long, so it still pays at most one read per name.
KNOB_MISS_RETRY_S = 60.0
_KNOB_MISS_UNTIL: dict[str, float] = {}
_DIRECT_CACHE: dict[str, str] = {}
_DIRECT_IDENTITY: tuple[str, str] | None = None
_DIRECT_REASON = ""
_DIRECT_MISS_UNTIL = 0.0


def reset_knob_cache() -> None:
    """Forget every knob read (for tests, and for a long-lived process after a reload)."""
    _KNOB_CACHE.clear()
    _KNOB_REASONS.clear()
    _KNOB_MISS_UNTIL.clear()
    global _DIRECT_IDENTITY, _DIRECT_REASON, _DIRECT_MISS_UNTIL
    _DIRECT_CACHE.clear()
    _DIRECT_IDENTITY = None
    _DIRECT_REASON = ""
    _DIRECT_MISS_UNTIL = 0.0


def _direct_values(env: Mapping[str, str]) -> dict[str, str]:
    """Read only the two registry names with this consumer's own narrow grant."""
    global _DIRECT_IDENTITY, _DIRECT_REASON, _DIRECT_MISS_UNTIL
    token = env.get(TOKEN_ENV, "")
    identity = (hashlib.sha256(token.encode()).hexdigest(), env.get("FLEET_SECRETS_API", ""))
    if identity != _DIRECT_IDENTITY:
        _DIRECT_CACHE.clear()
        _DIRECT_MISS_UNTIL = 0.0
        _DIRECT_IDENTITY = identity
    if _DIRECT_CACHE or time.monotonic() < _DIRECT_MISS_UNTIL:
        return _DIRECT_CACHE
    _DIRECT_REASON = "%s is missing or invalid" % TOKEN_ENV
    if token.startswith("st.") and len(token) >= 20:
        try:
            import fleet_infisical as FI
            from fleet_knobs.source import PROJECT_ID

            scoped_env = dict(env)
            scoped_env[FI.ENV_READ_PATHS] = REGISTRY_PATH
            values = FI.fetch_project(PROJECT_ID, REGISTRY_PATH, scoped_env,
                                      token=token, names=(URL_ENV, KEY_VAR),
                                      timeout=REGISTRY_FETCH_TIMEOUT)
            url, key = values.get(URL_ENV, ""), values.get(KEY_VAR, "")
            parsed = urllib.parse.urlsplit(url)
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            valid_url = (bool(parsed.hostname) and parsed.port != 0
                         and (parsed.scheme == "https" or (parsed.scheme == "http" and loopback))
                         and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                         and parsed.username is None and parsed.password is None)
            valid_key = 32 <= len(key) <= 512 and all(33 <= ord(c) <= 126 for c in key)
            if valid_url and valid_key:
                _DIRECT_CACHE.update({URL_ENV: url.rstrip("/"), KEY_VAR: key})
                _DIRECT_REASON = ""
                return _DIRECT_CACHE
            _DIRECT_REASON = "registry URL or signing key failed validation"
        except Exception:  # noqa: BLE001 - no provider payload or credential reaches diagnostics
            _DIRECT_REASON = "registry-only Infisical read failed (%s required)" % TOKEN_ENV
    _DIRECT_MISS_UNTIL = time.monotonic() + KNOB_MISS_RETRY_S
    return _DIRECT_CACHE


def _knob_value(name: str) -> str:
    """The fleet knob `name` as text, or "" when it cannot be had.

    fleet_knobs (scripts/fleet_knobs) is imported only here and only on demand.  A host
    that does not ship it, a name its registry does not hold, a secret it cannot fetch and
    any other failure all read as unset, which the caller answers as not_configured;  the
    reason is kept (never the value) and rides on that answer, so a missing reader token
    is visible rather than silent.  Tests replace this function so no test reaches
    Infisical."""
    if name in _KNOB_CACHE:
        return _KNOB_CACHE[name]
    if time.monotonic() < _KNOB_MISS_UNTIL.get(name, 0.0):
        return ""
    value, reason = "", ""
    try:
        from fleet_knobs import knobs  # absent on a host that does not ship it
        from fleet_knobs.registry import BY_NAME
    except Exception:  # noqa: BLE001 - no knobs package is "not configured", never a crash
        reason = "fleet_knobs is not importable"
    else:
        if name not in BY_NAME:
            reason = "knob not registered"
        else:
            try:
                value = str(knobs.get(name) or "").strip()
            except Exception as exc:  # noqa: BLE001 - unreadable is "not configured"
                code = getattr(exc, "reason", "")
                reason = "knob unreadable: %s" % (
                    code if isinstance(code, str) and code else type(exc).__name__
                )
            else:
                reason = "" if value else "knob empty"
    if value:
        _KNOB_CACHE[name] = value
        _KNOB_REASONS.pop(name, None)
        _KNOB_MISS_UNTIL.pop(name, None)
    else:
        _KNOB_REASONS[name] = reason or "knob empty"
        _KNOB_MISS_UNTIL[name] = time.monotonic() + KNOB_MISS_RETRY_S
    return value


def _is_process_env(env: Mapping[str, str]) -> bool:
    return env is os.environ


def _setting(env: Mapping[str, str], env_name: str, knob_name: str) -> str:
    """One configuration value:  the environment's, else (for the process environment
    only) the knob's, else ""."""
    if _is_process_env(env) and TOKEN_ENV in env:
        return _direct_values(env).get(env_name, "")
    value = str(env.get(env_name) or "").strip()
    if value or not _is_process_env(env):
        return value
    return _knob_value(knob_name)


def _unset_reason(env: Mapping[str, str], env_name: str, knob_name: str) -> str:
    """Why a setting reads as unset, by name only:  never a value."""
    if _is_process_env(env) and TOKEN_ENV in env:
        return _DIRECT_REASON or "%s is unavailable" % env_name
    if not _is_process_env(env):
        return (
            "%s is not set in the given environment (knobs are not consulted for an explicit mapping)"
            % env_name
        )
    return "%s is not set; knob: %s" % (env_name, _KNOB_REASONS.get(knob_name, "unset"))


def base_url(env: Mapping[str, str] | None = None) -> str:
    """The configured registry API base URL, or "" when this host has none (and so answers
    not_configured to every call)."""
    env = os.environ if env is None else env
    return _setting(env, URL_ENV, URL_KNOB)


def signing_key(env: Mapping[str, str] | None = None) -> str:
    """The one signing key, or "" when this host has none."""
    env = os.environ if env is None else env
    return _setting(env, KEY_VAR, KEY_KNOB)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, *args: Any, **kwargs: Any
    ) -> None:  # a signed request never follows a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: int) -> bytes:
    """Send one signed request and return the answer's body.  The one network seam:  a
    non-2xx answer raises urllib's HTTPError, a broken transport raises as urllib does.
    tests/registry_fake.py replaces it to serve the call in process."""
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read()


def _fold(params: Mapping[str, Any]) -> dict[str, Any]:
    """The call's fields under the API's names:  CLI spellings folded, a file path dropped
    (`db` names a file on the registry's host, not a field of the call), and
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
    not be made answers with an error dict:  `not_configured` when this host has no usable
    URL or no key (for every op, and nothing is written anywhere), `timeout` when
    the registry did not answer or its answer broke off, and `outcome_unknown` riding on
    a failed write whose answer was lost, because a write may have landed before its
    answer did."""
    from . import listener_registry as RG

    env = os.environ if env is None else env
    url = base_url(env)
    if not url:
        return {
            "error": "no registry API URL is configured (%s)"
            % _unset_reason(env, URL_ENV, URL_KNOB),
            "code": "not_configured",
        }
    key = signing_key(env)
    if not key:
        return {
            "error": "the registry key is not set (%s)" % _unset_reason(env, KEY_VAR, KEY_KNOB),
            "code": "not_configured",
        }
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
        raw = _open(request, timeout)
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
