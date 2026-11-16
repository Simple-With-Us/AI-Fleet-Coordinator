#!/usr/bin/env python3
"""Seat self-test: prove a seat's own identity and git route with live reads.

    python3 scripts/simple_tools/selftest.py --seat CLAUDE [--json] [--strict]

Why it exists:  a fleet issue was closed three times with no live proof that the
seat could authenticate and reach the git relay, and a sweep then fell back to
`gh` as the owner.  This is the one command that proves or disproves it, from
the seat's own shell, before anyone falls back.

What it does, in order (each step reports PASS or FAIL, never a guess):

  1. bearer   reads the seat's own private bearer file (the same one every fleet
              tool reads) and checks it names the seat asked for
  2. env      strips the ambient identity hints (AGENT_SEAT, AGENT_TAG,
              AGENT_LAUNCH_SEAT) unless a launcher (AGENT_LAUNCHER) bound them,
              so a stale AGENT_SEAT left by another tool cannot fail or
              mislead the probe.  A launcher's hint is kept and must agree.
  3. whoami   the hosted gateway authenticates the bearer and names the seat
  4. gr read  one hosted git read (gr_wf_repo_get)
  5. policy   one seat-policy-gated read (gr_wf_pr_list_page)
  6. advisory reads that exist but need a relay policy grant (gr_wf_pr_search,
              gr_wf_repo_list):  reported, and fatal only with --strict

On a refusal it prints the exact cause, one of seat_not_in_policy,
bearer_rejected, verifier_unavailable, no_app_credential, seat_policy (the
relay's closed vocabulary), or a gateway cause (gateway_unavailable,
relay_refused_unclassified, ...).  It prints the Worker version the gateway
announces and a relay version only if one is exposed.  Exit code is 0 only when
every required step passed.

It never prints the bearer:  every printed value is a seat name, a closed code,
or a short version string, and the whole output is scrubbed of the bearer
before it leaves the process.  Read-only:  nothing is written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in (None, ""):  # run as a script:  make `simple_tools` importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from simple_tools import fleet_bearer as F  # type: ignore[no-redef]
    from simple_tools.mcp import bearer_gate as BG  # type: ignore[no-redef]
else:
    from . import fleet_bearer as F
    from .mcp import bearer_gate as BG

REPO = "Simple-With-Us/AI-Fleet-Coordinator"
HINT_VARS = ("AGENT_SEAT", "AGENT_TAG", "AGENT_LAUNCH_SEAT")
STALE_VARS = ("AGENT_SEAT", "AGENT_TAG")

# The relay's closed refusal vocabulary (scripts/simple-tools-mcp/src/gr-refusal.js).
RELAY_CAUSES = (
    "seat_not_in_policy",
    "bearer_rejected",
    "verifier_unavailable",
    "no_app_credential",
    "seat_policy",
)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,59}")
_VERSION_KEYS = ("worker_version", "relay_version")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")

# (step name, tool, arguments, required)
READS = (
    ("gr_read", "gr_wf_repo_get", {"repo": REPO}, True),
    ("policy_read", "gr_wf_pr_list_page", {"repo": REPO, "state": "open", "per_page": 1}, True),
    ("advisory_pr_search", "gr_wf_pr_search", {"repo": REPO, "per_page": 1}, False),
    ("advisory_repo_list", "gr_wf_repo_list", {"repo": REPO, "per_page": 1}, False),
)

Transport = Callable[[bytes, str, float], "tuple[int, str, bytes]"]


class Refusal(Exception):
    """A step failed.  `cause` is a closed code, `detail` is fixed prose."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(cause)
        self.cause = cause
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_transport(body: bytes, token: str, timeout: float) -> "tuple[int, str, bytes]":
    """POST to the fixed gateway.  No proxy, cookies, redirects or retries."""
    request = urllib.request.Request(
        F.ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "User-Agent": "fleet-seat-selftest/1",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(F.MAX_BYTES + 1)
            return response.status, response.headers.get("Content-Type", ""), raw
    except urllib.error.HTTPError as exc:
        return exc.code, "", b""


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the ambient AGENT_SEAT and AGENT_TAG unless a launcher bound them.

    AGENT_LAUNCH_SEAT is always kept:  fleet_bearer.Client treats it as a restriction even
    without a launcher, so the self-test must refuse the same conflicts the real client does."""
    out = dict(env)
    if not out.get("AGENT_LAUNCHER"):
        for name in STALE_VARS:
            out.pop(name, None)
    return out


def check_hints(env: Mapping[str, str], seat: str) -> None:
    """A launcher's hints may restrict, never select, a bearer (same rule as fleet_bearer.Client)."""
    for name in HINT_VARS:
        value = F.canonical_hint(env.get(name, ""))
        if value and value != seat:
            raise Refusal("launcher_seat_conflict", name + " names a different seat than the bearer")
    if env.get("AGENT_LAUNCHER") and not env.get("AGENT_LAUNCH_SEAT"):
        raise Refusal("launcher_seat_conflict", "the launcher did not bind a seat")


def _parse_reply(ctype: str, raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8")
    kind = ctype.split(";", 1)[0].strip()
    if kind == "text/event-stream":
        parts = [
            "\n".join(
                line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")
            )
            for event in text.replace("\r\n", "\n").split("\n\n")
        ]
    elif kind == "application/json":
        parts = [text]
    else:
        raise ValueError("content type")
    replies = [
        r
        for r in (F.bounded_json(p) for p in parts if p.strip())
        if isinstance(r, dict) and r.get("jsonrpc") == "2.0" and r.get("id") == 1
    ]
    if len(replies) != 1:
        raise ValueError("reply count")
    return replies[0]


def _error_cause(result: Mapping[str, Any]) -> Refusal:
    """The closed cause of a hosted tool error:  relay reason first, then code."""
    meta = result.get("_meta")
    err = meta.get("simple-tools/error") if isinstance(meta, dict) else None
    if not isinstance(err, dict):
        err = {}
    reason = err.get("reason")
    code = err.get("code")
    if isinstance(reason, str) and reason in RELAY_CAUSES:
        return Refusal(reason)
    if isinstance(code, str) and _CODE.fullmatch(code):
        if code == "relay_refused":
            if isinstance(reason, str) and _CODE.fullmatch(reason):
                return Refusal(reason)
            return Refusal("relay_refused_unclassified")
        if code in ("unavailable", "upstream_error"):
            return Refusal("gateway_unavailable" if code == "unavailable" else code)
        return Refusal(code)
    return Refusal("upstream_error")


class Probe:
    def __init__(self, token: str, transport: Transport, timeout: float) -> None:
        self.token = token
        self.transport = transport
        self.timeout = timeout

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, allow_nan=False
        ).encode()
        try:
            status, ctype, raw = self.transport(body, self.token, self.timeout)
        except Exception:  # OSError, ValueError, http.client.HTTPException (IncompleteRead, BadStatusLine)
            raise Refusal("gateway_unavailable", "the gateway could not be reached") from None
        if status in (401, 403):
            raise Refusal("bearer_rejected", "the gateway refused the bearer (HTTP %d)" % status)
        if status != 200:
            raise Refusal("gateway_unavailable", "gateway HTTP %d" % status)
        try:
            if len(raw) > F.MAX_BYTES or self.token.encode() in raw:
                raise ValueError("size or echo")
            reply = _parse_reply(ctype, raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Refusal("bad_gateway_reply", "the gateway reply was not valid") from None
        if "error" in reply or not isinstance(reply.get("result"), dict):
            raise Refusal("rpc_error", "the gateway returned a protocol error")
        result = reply["result"]
        if result.get("isError") is True:
            raise _error_cause(result)
        return result

    def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        sc = result.get("structuredContent")
        return sc if isinstance(sc, dict) else {}


def _version_fields(source: Any, relay_only: bool = False) -> dict[str, str]:
    """Top-level scalar version strings the gateway exposes, allowlisted by shape."""
    found: dict[str, str] = {}
    if not isinstance(source, dict):
        return found
    for key, value in source.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,40}", key):
            continue
        if key not in _VERSION_KEYS:
            continue
        if relay_only and key != "relay_version":
            continue
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value)
            if _VERSION.fullmatch(text):
                found[key] = text
    return found


def run(
    seat: str,
    *,
    env: Mapping[str, str],
    home: Path,
    transport: Transport = http_transport,
    bearer_file: str | None = None,
    strict: bool = False,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Run every step;  returns {"seat", "ok", "steps", "versions"}.  Never raises."""
    want = F.canonical_hint(seat)
    steps: list[dict[str, str]] = []
    versions: dict[str, str] = {"worker": "not exposed", "relay": "not exposed"}
    report: dict[str, Any] = {"seat": want, "ok": False, "steps": steps, "versions": versions}
    token = ""

    def add(name: str, status: str, detail: str = "", cause: str = "") -> None:
        row = {"step": name, "status": status}
        if cause:
            row["cause"] = cause
        if detail:
            row["detail"] = detail
        steps.append(row)

    environment = clean_env(env)
    # 1. bearer
    try:
        path = Path(bearer_file) if bearer_file else BG.bearer_path(home, want)
        bearer_seat, token = F.private_bearer(path)
        if bearer_seat != want:
            add("bearer", "FAIL", "the bearer file names a different seat", "seat_mismatch")
            return report
        add("bearer", "PASS", "own bearer file is private and names this seat")
    except Refusal as exc:
        add("bearer", "FAIL", exc.detail, exc.cause)
        return report
    except F.FleetError as exc:
        add("bearer", "FAIL", "bearer file is missing, unsafe or malformed", exc.code)
        return report
    except BG.Refused:
        add("bearer", "FAIL", "not a seat name this server can authenticate", "bad_seat_name")
        return report
    # 2. env
    removed = [n for n in HINT_VARS if env.get(n) and n not in environment]
    try:
        check_hints(environment, want)
        add("env", "PASS", "ignored ambient " + ", ".join(removed) if removed else "no stale hints")
    except Refusal as exc:
        add("env", "FAIL", exc.detail, exc.cause)
        return report

    probe = Probe(token, transport, timeout)
    ok = True
    # initialize:  the Worker announces its name and version here
    try:
        init = probe.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fleet-seat-selftest", "version": "1"},
            },
        )
        info = init.get("serverInfo")
        if isinstance(info, dict) and isinstance(info.get("version"), str):
            if _VERSION.fullmatch(info["version"]):
                versions["worker"] = info["version"]
    except Refusal:
        pass
    # 3. whoami
    try:
        who = probe.tool("whoami", {})
        if who.get("authenticated_seat") != want or who.get("seat") != want:
            add("whoami", "FAIL", "the gateway named a different seat", "seat_mismatch")
            report["ok"] = False
            _finish(report, steps)
            return report
        add("whoami", "PASS", "the gateway authenticated this bearer as " + want)
        for key, value in _version_fields(who).items():
            versions["relay" if key == "relay_version" else "worker"] = value
    except Refusal as exc:
        add("whoami", "FAIL", exc.detail, exc.cause)
        report["ok"] = False
        _finish(report, steps)
        return report
    # 4-6. reads
    for name, tool, args, required in READS:
        try:
            data = probe.tool(tool, args)
            for _key, value in _version_fields(data, relay_only=True).items():
                versions["relay"] = value
            add(name, "PASS", tool)
        except Refusal as exc:
            if required or strict:
                ok = False
                add(name, "FAIL", tool, exc.cause)
            else:
                add(name, "WARN", tool + " (needs a relay policy grant)", exc.cause)
    report["ok"] = ok
    _finish(report, steps)
    return report


def _finish(report: dict[str, Any], steps: list[dict[str, str]]) -> None:
    report["ok"] = bool(report["ok"]) and all(s["status"] != "FAIL" for s in steps)


def render(report: Mapping[str, Any], token: str = "") -> str:
    lines = ["seat: " + str(report["seat"])]
    for row in report["steps"]:
        text = "%-4s %-20s" % (row["status"], row["step"])
        if row.get("cause"):
            text += " cause=" + row["cause"]
        if row.get("detail"):
            text += "  " + row["detail"]
        lines.append(text)
    versions = report["versions"]
    lines.append("worker version: " + versions["worker"])
    lines.append("relay version: " + versions["relay"])
    lines.append("RESULT " + ("ok" if report["ok"] else "failed"))
    out = "\n".join(lines)
    return out.replace(token, "[bearer]") if token else out


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    transport: Transport = http_transport,
    out: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="Prove this seat's identity and git route live.")
    parser.add_argument("--seat", required=True, help="the seat to prove, e.g. CLAUDE")
    parser.add_argument("--bearer-file", help="explicit private bearer file (default ~/.secrets/fleet-tools/<seat>)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--strict", action="store_true", help="advisory reads must pass too")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    report = run(
        args.seat,
        env=env,
        home=home,
        transport=transport,
        bearer_file=args.bearer_file,
        strict=args.strict,
        timeout=args.timeout,
    )
    # Scrub with the bearer if it can be read again;  the report holds closed values only.
    token = ""
    try:
        path = Path(args.bearer_file) if args.bearer_file else BG.bearer_path(home, F.canonical_hint(args.seat))
        token = F.private_bearer(path)[1]
    except Exception:
        token = ""
    text = json.dumps(report, indent=2) if args.json else render(report)
    if token:
        text = text.replace(token, "[bearer]")
    print(text, file=stream)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
