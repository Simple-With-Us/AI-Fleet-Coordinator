"""Fall back to the public recall.jays.services service when the Tailscale-only Qdrant/TEI
path is unreachable from this Mac.

The Mac-side surfaces (the `recall` CLI and the stdio `fleet-recall-mcp.py`) call
`fleet_rag.recall_api` functions directly against TEI_URL / QDRANT_URL, which are Tailscale
mesh addresses (see docs/RAG-FLEET-INFRA.md).  When Tailscale is down on this Mac -- logged
out, a captive/corporate network blocking the control plane, or simply not running -- every
call to those addresses fails as a connection-level error (refused / reset / timeout).
`core.http_json` retries such an error 4 times with exponential backoff before giving up, so
the failure surfaces slowly and as an opaque `ConnectionError` / `RemoteDisconnected`
traceback.

This module gives those two surfaces a fast, actionable alternative:

  1. A cheap up-front check (`tailscale_status_text`, ~0.1-3s) reads `tailscale status` so an
     already-known-down Tailscale skips the slow local retry storm entirely and goes straight
     to the public path.  A machine with no Tailscale.app (Linux, CI, a relocated binary)
     reports "unknown", which is treated as "assume up" -- this module only ever *skips* the
     local path on positive evidence, never on the absence of the binary.  macOS GUI IPC
     failure is positive evidence: `tailscale status` from a LaunchAgent / no-Aqua session
     tries to start Tailscale.app and returns `CLIError 3` instead of "logged out"; that is
     treated as down so `recall stats` does not wait on private Qdrant's 120s per-call
     timeout.  `RECALL_SKIP_PRIVATE=1` skips the Tailscale binary entirely (no GUI start)
     and skips the private path even when QDRANT_URL/TEI_URL are set.  On macOS, when the
     status check itself is unknown (no Tailscale.app at all), `private_route_bypasses_
     tailscale` reads the OS routing table for the configured Qdrant host as a second,
     independent signal -- a route that resolves to a non-"utun" interface is positive
     evidence the private hop is gone, so the same fast public path is taken instead of
     hanging on this network's transparent TCP proxy until the 120s read timeout.  Both
     signals are combined in `direct_path_blocked`, shared by `call_with_fallback` and
     `recall doctor --platforms`.
  2. Otherwise the local path still runs first -- Tailscale can flap, and "believed up" is a
     hint, not a guarantee -- but a CONNECTION-LEVEL failure from it (never an HTTP 4xx/5xx,
     which is a real answer from a reachable server) is caught and retried once against
     https://recall.jays.services's REST twin (`scripts/fleet-recall-service`).
  3. The public retry needs three names: `RECALL_API_TOKEN` and `CF_ACCESS_CLIENT_ID` /
     `CF_ACCESS_CLIENT_SECRET` (the Cloudflare Access service token) -- see
     docs/RECALL-ACCESS-CHECK.md.  A value already in the environment wins; otherwise each is
     read straight from its handoff file (PUBLIC_ENV_FILES: RECALL_API_TOKEN from
     ~/.secrets/global-api-keys, the Access pair from
     ~/.secrets/agents-jays-services-access-service-token.env) so this works from the stock
     `python3 fleet-recall-mcp.py` MCP registration with no shell wrapper.  The request also
     carries a real `User-Agent` (core.http_json sends none, and urllib's default trips
     Cloudflare's bot management -- 403, "error code: 1010").  A missing name, or a failure on
     the public path too (a rejected bearer, Access rejecting the service token, the box itself
     down), produces one plain-English, actionable line -- never a second opaque exception.

Only recall_search / recall_stats / recall_contribute are covered (the shared tool contract).
The public service's recall_search route now accepts every recall_search() keyword, including
`per_doc`, `rerank`, and `prefer_lessons` (see `scripts/fleet-recall-service/server.py` TOOLS).
The CLI-only `--force` dedup-guard override has no public twin -- it is silently dropped when a
call actually falls back, and the near-duplicate contribute guard
(a local-only nicety, not part of the shared tool contract) is skipped rather than run against
an unreachable backend.

The Hetzner-side `fleet-recall-service` imports `recall_api` directly and never goes through
this module: it already runs on the box next to Qdrant/TEI and has no Tailscale hop to lose.

Callers must never engage the network path while a test has swapped `recall_api.Qdrant` for
`FakeQdrant` (`recall_api.install_fake_backend()`) -- see `using_fake_backend`.
"""
from __future__ import annotations

import ipaddress
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from typing import Any, Callable

from . import __version__, core, recall_api
from .core import FleetRagError

TAILSCALE_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TAILSCALE_STATUS_TIMEOUT = 3.0
# BotFleet LaunchAgent / other GUI-less callers set this so `recall stats --json` never
# starts Tailscale.app and never waits 120s on private Qdrant.
SKIP_PRIVATE_ENV = "RECALL_SKIP_PRIVATE"

# macOS-only corroborating signal for when tailscale_status_text() itself returns None (no
# Tailscale.app to ask).  Tailscale's mesh addresses are all inside this CGNAT range
# (https://tailscale.com/kb/1015/100.x-addresses); a route to a host in it that resolves to a
# non-"utun" interface is positive evidence the private hop is gone, not silence to read as
# "assume up".
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# The private Qdrant host documented in docs/RAG-FLEET-INFRA.md, used only as a fallback when
# QDRANT_URL is not set in the environment (e.g. before recall_api.load_config() has run).
DEFAULT_QDRANT_URL = "http://100.69.77.26:6333"
ROUTE_BIN = "/sbin/route"
ROUTE_TIMEOUT = 2.0

PUBLIC_BASE = "https://recall.jays.services"
PUBLIC_ENV_KEYS = ("RECALL_API_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
PUBLIC_TIMEOUT = 15
PUBLIC_RETRIES = 1
# `urllib.request`'s default User-Agent ("Python-urllib/3.x") trips Cloudflare's bot management
# in front of recall.jays.services (403, "error code: 1010" -- documented in
# docs/RECALL-ACCESS-CHECK.md's troubleshooting table).  curl and browsers are unaffected
# because they send their own real UA; core.http_json sends none, so this module must.
USER_AGENT = f"fleet-recall-fallback/{__version__} (+https://github.com/Simple-With-Us/AI-Fleet-Coordinator)"

# Every MCP client on this Mac registers `fleet-recall` as a plain `python3 fleet-recall-mcp.py`
# (no shell wrapper, no env sourcing -- see install-fleet-rag.sh), so these three names are
# rarely already in os.environ.  Mirror core.py's own env-then-handoff-file pattern instead of
# requiring every MCP config to be rewritten as a `sh -c 'set -a; . ...; exec ...'` wrapper:
# RECALL_API_TOKEN lives in the same handoff file as TEI_URL / QDRANT_URL (~/.secrets/global-
# api-keys); the Cloudflare Access service token is a separate file per docs/RECALL-ACCESS-
# CHECK.md (~/.secrets/agents-jays-services-access-service-token.env, shared with agents.jays.
# services).  A value already in the environment always wins.
CF_ACCESS_FILE = pathlib.Path.home() / ".secrets" / "agents-jays-services-access-service-token.env"
PUBLIC_ENV_FILES = {
    "RECALL_API_TOKEN": core.HANDOFF,
    "CF_ACCESS_CLIENT_ID": CF_ACCESS_FILE,
    "CF_ACCESS_CLIENT_SECRET": CF_ACCESS_FILE,
}


def _read_named_line(path: pathlib.Path, name: str) -> "str | None":
    """The value of one `NAME=value` line in a chmod-600 handoff file, or None.  Same quote-
    stripping as core._identity(); never logs the path's other contents."""
    try:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(name)}=(.*)$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")

REST_PATH = {"recall_search": "/recall/search", "recall_stats": "/recall/stats",
             "recall_contribute": "/recall/contribute"}
REST_METHOD = {"recall_search": "POST", "recall_stats": "GET", "recall_contribute": "POST"}
# The public tool contract mirrors the local one (scripts/fleet-recall-service/server.py TOOLS)
# for recall_search -- every recall_search() keyword argument has a public route.  recall_
# contribute stays a deliberate subset: `force` is a local-only dedup-guard nicety (see the
# module docstring) with no public twin.
PUBLIC_ALLOWED_ARGS = {
    "recall_search": {"query", "limit", "category", "app", "source", "seat", "since_days",
                      "per_doc", "rerank", "prefer_lessons"},
    "recall_stats": set(),
    "recall_contribute": {"text", "category", "app", "seat", "title", "url"},
}

RunLocal = Callable[[], dict]
StatusProbe = Callable[[], "str | None"]


# --------------------------------------------------------------------------- Tailscale status

def skip_private_requested() -> bool:
    """True when the caller asked to skip Tailscale.app and the private Qdrant/TEI path."""
    v = (os.environ.get(SKIP_PRIVATE_ENV) or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def tailscale_status_text(run: Callable[..., Any] | None = None,
                          timeout: float = TAILSCALE_STATUS_TIMEOUT) -> "str | None":
    """`tailscale status` output (stdout+stderr), or None when it could not be determined.

    None covers a missing/relocated binary, a timeout, or any other OSError -- callers must
    treat None as "unknown", never as "down", so a machine with no Tailscale.app keeps today's
    behavior instead of every call being routed to the public fallback.  `run` is injectable
    (defaults to `subprocess.run`) so tests never shell out for real.

    `RECALL_SKIP_PRIVATE` never invokes the Tailscale.app binary (LaunchAgent plist env would
    otherwise try to start the GUI).  The probe also sets TAILSCALE_BE_CLI=1 so a GUI-less
    parent does not flip the macOS binary into GUI-launch mode.
    """
    if skip_private_requested():
        return "RECALL_SKIP_PRIVATE: Tailscale probe skipped"
    runner = run or subprocess.run
    ts_env = os.environ.copy()
    ts_env["TAILSCALE_BE_CLI"] = "1"
    ts_env.setdefault("TERM", "dumb")
    try:
        proc = runner([TAILSCALE_BIN, "status"], capture_output=True, text=True,
                      timeout=timeout, env=ts_env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def tailscale_believed_down(status_text: "str | None") -> bool:
    """True on positive evidence Tailscale cannot carry the private Qdrant path.

    None / empty (unknown) is never "down" -- Linux and CI have no Tailscale.app and still
    use local Qdrant.  `RECALL_SKIP_PRIVATE` is an explicit skip.  macOS `CLIError 3` /
    "GUI failed to start" is the LaunchAgent case: the binary exists but cannot talk to
    the GUI, and treating that as unknown used to wait 120s on private Qdrant.
    """
    if skip_private_requested():
        return True
    if not status_text:
        return False
    low = status_text.lower()
    if "tailscale is stopped" in low or "logged out" in low:
        return True
    if "clierror" in low and "error 3" in low:
        return True
    if "the tailscale gui failed to start" in low:
        return True
    return False


def _qdrant_url_for_route_check() -> str:
    """QDRANT_URL from the environment, or the documented default private host -- only used to
    pick which host to ask the routing table about, never to make a network call itself."""
    return os.environ.get("QDRANT_URL", "").strip() or DEFAULT_QDRANT_URL


def private_route_bypasses_tailscale(url: "str | None" = None,
                                     run: Callable[..., Any] | None = None) -> bool:
    """True on positive evidence from the macOS routing table that the private Qdrant host is
    NOT reachable through a Tailscale (utun) interface.

    This exists for the case `tailscale_status_text()` cannot resolve on its own: no
    `Tailscale.app` at all (Tailscale.app removed from the Mac, or a relocated/CI binary).
    `tailscale_believed_down(None)` is "unknown", read as "assume up" by design -- correct for
    a Linux/CI box that never had Tailscale.app -- but on a Mac this network's transparent TCP
    proxy makes a direct connect to a dead private address "succeed" in ~0.2s and then hang
    until `core.DEFAULT_TIMEOUT` (120s) on the read, instead of failing fast.  A `route -n get`
    lookup is a local, offline, sub-second check that settles the ambiguity independently of
    Tailscale's own binary.

    Returns True only when ALL of:
      - `sys.platform == "darwin"` (this signal is macOS-specific; every other platform, CI
        included, returns False unconditionally -- unchanged behavior there)
      - the configured (or documented-default) `QDRANT_URL` host is an IPv4 literal inside
        Tailscale's CGNAT range (`TAILSCALE_CGNAT`, 100.64.0.0/10) -- an operator's `Run your
        own` deployment on a normal host is never treated as Tailscale-routed
      - `route -n get <ip>` (via `run`, injectable, defaults to `subprocess.run`) completes
        within `ROUTE_TIMEOUT` and its output parses an `interface:` line
      - that interface does NOT start with `"utun"` (Tailscale's interface family on macOS)

    Everything else -- a non-Darwin platform, a non-CGNAT/non-IP host, a `route` call that
    fails, times out, or produces unparseable output, or an interface that DOES start with
    `"utun"` -- returns False.  This helper only ever adds a fast "blocked" verdict on top of
    today's "unknown = assume up" default; it never claims the private path IS reachable, so it
    can only make a hang shorter, never mask a real outage as healthy.
    """
    if sys.platform != "darwin":
        return False
    host = urllib.parse.urlparse(url or _qdrant_url_for_route_check()).hostname
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.version != 4 or addr not in TAILSCALE_CGNAT:
        return False
    runner = run or subprocess.run
    try:
        proc = runner([ROUTE_BIN, "-n", "get", str(addr)], capture_output=True, text=True,
                      timeout=ROUTE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    m = re.search(r"^\s*interface:\s*(\S+)", proc.stdout or "", re.MULTILINE)
    if not m:
        return False
    return not m.group(1).startswith("utun")


def direct_path_blocked(status_text: "str | None") -> bool:
    """The one shared verdict `call_with_fallback` and `recall doctor --platforms` both use to
    decide the direct Qdrant/TEI path is not worth attempting: positive Tailscale evidence
    (`tailscale_believed_down`), OR -- only when `status_text` is unknown (None), i.e. there was
    no Tailscale.app to ask -- positive evidence from the macOS routing table
    (`private_route_bypasses_tailscale`).  Callers still gate this on `using_fake_backend()` /
    `local_override_active()` / `skip_private_requested()` themselves (as they already do);
    keeping the "unknown status" corroboration in one place means the two call sites can never
    quietly drift apart on what "blocked" means.
    """
    if tailscale_believed_down(status_text):
        return True
    if status_text is None:
        return private_route_bypasses_tailscale()
    return False


# --------------------------------------------------------------------------- connection errors

def is_connection_error(err: FleetRagError) -> bool:
    """True for the two message shapes `core.http_json` raises on a connection-level failure
    (never for an HTTP status error, a validation error, or a "missing credentials" error --
    those are real answers or configuration problems, not a Tailscale-shaped outage)."""
    msg = str(err)
    return " reaching " in msg or msg.startswith("request failed:")


def guard_may_run() -> bool:
    """Whether it is worth attempting the local near-duplicate contribute guard (a Qdrant call
    the public service never makes, so it gets no fallback of its own): always yes against a
    fake backend or with an operator override active (local_override_active()), and yes against
    a real one unless Tailscale is believed down -- in which case skip the guard and let the
    contribute call itself decide whether to fall back."""
    if using_fake_backend():
        return True
    if skip_private_requested():
        return False
    if local_override_active():
        return True
    return not tailscale_believed_down(tailscale_status_text())


def local_override_active() -> bool:
    """True when an operator has pointed this Mac at Qdrant/TEI directly -- both QDRANT_URL and
    TEI_URL are non-empty in the environment (e.g. the SSH tunnel opened by `recall-tunnel up`
    and `eval "$(recall-tunnel env)"`, or a `Run your own` deployment).  `call_with_fallback` and
    `guard_may_run` then try the local path first regardless of what `tailscale status` says --
    the override is a deliberate operator choice, not a guess, so it always wins over Tailscale
    evidence.  A connection-level failure from that local path still falls back exactly as it
    always has."""
    return bool(os.environ.get("QDRANT_URL", "").strip()) and bool(os.environ.get("TEI_URL", "").strip())


def require_direct_path(what: str) -> None:
    """Raise immediately when `recall <what>` needs the direct Qdrant/TEI path and there is
    positive evidence Tailscale is down with no operator override in place.

    `recall_search` / `recall_stats` / `recall_contribute` have a public REST twin
    (`call_with_fallback`); `ingest`, `eval`, and the doctor sentinel do not -- the public twin
    has no ingest route and drops the rerank/per_doc knobs eval needs -- so without this check
    those three retry the unreachable Tailscale addresses for several minutes and finally
    surface an opaque `RemoteDisconnected` traceback instead of one actionable line.
    """
    if using_fake_backend() or local_override_active():
        return
    if skip_private_requested() or tailscale_believed_down(tailscale_status_text()):
        raise FleetRagError(
            f"Tailscale is logged out on this Mac and `recall {what}` needs the direct Qdrant/TEI "
            "path (no public fallback for it).  Either run `tailscale login`, or open the SSH "
            'tunnel with `recall-tunnel up` and rerun with `eval "$(recall-tunnel env)"`.')


def using_fake_backend() -> bool:
    """True when a test has swapped the backend to the in-process fake -- either the whole
    module seam (`recall_api.install_fake_backend()`, used by the CLI/recall_api test suite,
    including a test-local subclass of FakeQdrant that overrides one method to misbehave) or
    the `FLEET_RECALL_FAKE=1` environment flag (used by the stdio-server subprocess tests).  A
    fake-backed call must never reach out to the real public network."""
    if os.environ.get("FLEET_RECALL_FAKE") == "1":
        return True
    qdrant = recall_api.Qdrant
    return isinstance(qdrant, type) and issubclass(qdrant, recall_api.FakeQdrant)


# --------------------------------------------------------------------------- public REST call

def public_credentials() -> "dict[str, str] | None":
    """The three names the public REST twin needs, or None if any is missing/blank.

    Environment first (a value there always wins, and is all the unit tests ever set); when a
    name is not in the environment, read it from its handoff file (PUBLIC_ENV_FILES) the same
    way core.load_config() falls back to Infisical for TEI_URL / QDRANT_URL -- so the fallback
    works from the stock `python3 fleet-recall-mcp.py` MCP registration with no shell wrapper
    and no change to any client's MCP config.
    """
    vals = _resolve_public_env()
    return vals if all(vals.values()) else None


def _resolve_public_env() -> dict[str, str]:
    """Every PUBLIC_ENV_KEYS name resolved env-then-file; a name found nowhere maps to "" so
    the caller can still tell which one(s) are missing."""
    vals = {}
    for k in PUBLIC_ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if not v:
            v = _read_named_line(PUBLIC_ENV_FILES[k], k) or ""
        vals[k] = v
    return vals


def _public_headers(creds: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {creds['RECALL_API_TOKEN']}",
        "CF-Access-Client-Id": creds["CF_ACCESS_CLIENT_ID"],
        "CF-Access-Client-Secret": creds["CF_ACCESS_CLIENT_SECRET"],
        "User-Agent": USER_AGENT,
    }


def call_public(name: str, kwargs: dict) -> dict:
    """Call the public REST twin for one of the three tools.  Raises FleetRagError describing
    ONLY the public-side failure (a missing credential name, or the HTTP/connection error) --
    `call_with_fallback` prefixes it with why the local path was skipped."""
    if name not in REST_PATH:
        raise FleetRagError(f"no public REST route for {name!r}")
    creds = public_credentials()
    if creds is None:
        resolved = _resolve_public_env()
        missing = [k for k in PUBLIC_ENV_KEYS if not resolved[k]]
        raise FleetRagError(
            "public fallback (recall.jays.services) needs " + ", ".join(missing)
            + " -- set them in the environment, or see docs/RECALL-ACCESS-CHECK.md for the"
            " handoff files that normally supply them")
    url = PUBLIC_BASE + REST_PATH[name]
    method = REST_METHOD[name]
    body = dict(kwargs) if method == "POST" else None
    res = core.http_json(url, body, _public_headers(creds), method=method,
                         timeout=PUBLIC_TIMEOUT, retries=PUBLIC_RETRIES)
    if isinstance(res, dict) and res.get("ok") is False:
        raise FleetRagError(f"public fallback ({url}) rejected the request: {res.get('error', '?')}")
    if isinstance(res, dict):
        res = {k: v for k, v in res.items() if k != "ok"}
    return res


# --------------------------------------------------------------------------- orchestration

def _fallback(name: str, kwargs: dict, why: str) -> dict:
    allowed = PUBLIC_ALLOWED_ARGS.get(name, set())
    public_kwargs = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    try:
        return call_public(name, public_kwargs)
    except FleetRagError as pub_err:
        raise FleetRagError(
            f"{why}; {pub_err}; run `tailscale login` on this Mac to restore the local path."
        ) from None


def call_with_fallback(name: str, kwargs: dict, run_local: RunLocal,
                       status_probe: "StatusProbe | None" = None) -> dict:
    """Run `run_local()`; fall back to the public REST twin on a Tailscale-shaped failure.

    `name` is one of recall_search / recall_stats / recall_contribute (used to pick the public
    route and filter `kwargs` to the arguments that route accepts).  `kwargs` is the same
    argument set `run_local` was built from -- it is never passed to `run_local` itself, only
    used to build the public request if the local call is skipped or fails.  `status_probe`
    (default `tailscale_status_text`) is injectable for tests.

    An operator override (`local_override_active()` -- both QDRANT_URL and TEI_URL set, e.g. by
    the SSH tunnel from `recall-tunnel up`) skips the Tailscale-evidence check entirely and goes
    straight to `run_local()`; a connection-level failure from it still falls back exactly as it
    always has.
    """
    if using_fake_backend():
        return run_local()
    if skip_private_requested():
        return _fallback(
            name, kwargs,
            "RECALL_SKIP_PRIVATE is set; skipping Tailscale and the private Qdrant path")
    if not local_override_active():
        probe = status_probe or tailscale_status_text
        status_text = probe()
        if direct_path_blocked(status_text):
            why = ("Tailscale is logged out on this Mac" if tailscale_believed_down(status_text)
                   else "the routing table shows no Tailscale interface for the private Qdrant host")
            return _fallback(name, kwargs, why)
    try:
        return run_local()
    except FleetRagError as e:
        if not is_connection_error(e):
            raise
        return _fallback(name, kwargs, f"the Tailscale TEI/Qdrant path is unreachable ({e})")
