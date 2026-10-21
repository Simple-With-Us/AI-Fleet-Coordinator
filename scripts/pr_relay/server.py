"""HTTP surface for the PR relay.

Endpoints (all JSON in, JSON out):
    GET  /healthz
    POST /v1/open_pr       {repo, base, head, title, body}
    POST /v1/edit_pr       {repo, number, title?, body?}
    POST /v1/reply_thread  {repo, number, comment_id, body}
    POST /v1/comment_pr    {repo, number, body}

Auth: "Authorization: Bearer <seat token>".  Every call, allowed or denied,
writes one audit row.  The GitHub token comes from GITHUB_TOKEN in the
environment at process start; it is never accepted over the wire, logged,
or returned.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import AuditLog
from .github_client import GitHubClient, RelayGitHubError
from .policy import PolicyError, SeatPolicy

MAX_REQUEST_BYTES = 64 * 1024
MAX_TEXT_FIELD = 32 * 1024

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8471


def _text(value, field: str, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"missing {field}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > MAX_TEXT_FIELD:
        raise ValueError(f"{field} exceeds the 32 KiB bound")
    return value


def _number(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


class PolicyDenied(Exception):
    """The seat authenticated but its policy refused the call."""


class Relay:
    """Wires policy, client and audit together; easy to drive without HTTP."""

    def __init__(self, policy: SeatPolicy, client: GitHubClient, audit: AuditLog):
        self.policy = policy
        self.client = client
        self.audit = audit

    def handle(self, token: str, action: str, args: dict) -> tuple[int, dict]:
        seat = self.policy.authenticate(token)
        if seat is None:
            return 401, {"error": "unauthenticated"}
        repo = str(args.get("repo") or "")
        try:
            result = self._dispatch(seat, action, args, repo)
        except PolicyDenied as exc:
            self.audit.record(seat, action, repo, "denied", {"reason": str(exc)})
            return 403, {"error": str(exc)}
        except ValueError as exc:
            self.audit.record(seat, action, repo, "bad_request", {"reason": str(exc)})
            return 400, {"error": str(exc)}
        except RelayGitHubError as exc:
            self.audit.record(
                seat, action, repo, "github_error", {"status": exc.status, "message": exc.message}
            )
            return 502, {"error": "github call failed", "github_status": exc.status}
        self.audit.record(seat, action, repo, "ok", {**args, **result})
        return 200, result

    def _dispatch(self, seat: str, action: str, args: dict, repo: str) -> dict:
        if action not in SeatPolicy.ALL_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        repo = _text(repo, "repo")
        denial = self.policy.authorize(seat, action, repo)
        if denial:
            raise PolicyDenied(denial)
        if action not in SeatPolicy.VALID_ACTIONS:
            # Enabled in policy but not implemented yet:  fail closed.  Tier 2/3
            # actions land only with their backing credential and implementation.
            raise ValueError(f"action {action!r} is not implemented yet")
        number = args.get("number")
        if action == "open_pr":
            return self.client.open_pr(
                repo,
                base=_text(args.get("base"), "base"),
                head=_text(args.get("head"), "head"),
                title=_text(args.get("title"), "title"),
                body=_text(args.get("body"), "body"),
            )
        if action == "edit_pr":
            return self.client.edit_pr(
                repo,
                _number(number, "number"),
                title=_text(args.get("title"), "title", required=False),
                body=_text(args.get("body"), "body", required=False),
            )
        if action == "reply_thread":
            return self.client.reply_thread(
                repo,
                _number(number, "number"),
                _number(args.get("comment_id"), "comment_id"),
                _text(args.get("body"), "body"),
            )
        return self.client.comment_pr(
            repo, _number(number, "number"), _text(args.get("body"), "body")
        )


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "swu-pr-relay/1"

        def log_message(self, fmt, *fmt_args):  # keep default stderr log out of the audit path
            return

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "v1":
                self._send(404, {"error": "not found"})
                return
            action = parts[1]
            auth = self.headers.get("Authorization") or ""
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"error": "bad Content-Length"})
                return
            if length < 1 or length > MAX_REQUEST_BYTES:
                self._send(400, {"error": "request body missing or over the 64 KiB bound"})
                return
            raw = self.rfile.read(length)
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                self._send(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(args, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return
            status, obj = relay.handle(token, action, args)
            self._send(status, obj)

    return Handler


def from_env() -> Relay:
    try:
        enabled = os.environ.get("PR_RELAY_ENABLED_ACTIONS", "")
        policy = SeatPolicy(
            os.environ.get("PR_RELAY_SEATS_JSON", ""),
            rate_per_hour=int(os.environ.get("PR_RELAY_RATE_PER_HOUR", "120")),
            enabled_actions=[a.strip() for a in enabled.split(",") if a.strip()] or None,
        )
    except PolicyError as exc:
        raise SystemExit(f"pr-relay config: {exc}") from exc
    client = GitHubClient(os.environ.get("GITHUB_TOKEN", ""))
    audit = AuditLog(os.environ.get("PR_RELAY_AUDIT_PATH", "pr-relay-audit.jsonl"))
    return Relay(policy, client, audit)


def main() -> None:
    relay = from_env()
    host = os.environ.get("PR_RELAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("PR_RELAY_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((host, port), make_handler(relay))
    print(f"pr-relay listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
