"""`agent-sync notes create|update|pin|unpin|status` and `agent-sync guide` (AFC-331).

The CLI twin of the notes_* and guide tools.  On a Mac the script runs directly and no Zulip credential is
needed;  with --relay (or off a Mac) the request goes to #notes-relay through the same code the MCP tool uses
(mcp/tools.py), so the envelope, the upload rule and the idempotency are one implementation.  The body never
reaches a command line of ours:  it comes from standard input or --body-file (or --html for an HTML file),
and is handed to the script on stdin.

Exit codes follow the CLI:  2 usage, 3 credentials, 5 the note or Zulip refused, 6 network.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from . import bodyfile
from . import config as C
from . import identities as X
from . import identity as I
from . import notes as N
from . import session_ids as SID
from . import zulip as Z
from .idem import IdemStore
from .secretscan import scan as scan_text
from .state import state_root


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    def add(parent: Any, name: str, summary: str) -> argparse.ArgumentParser:
        return parent.add_parser(name, parents=[common], help=summary, description=summary)

    def identity_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--identity",
            dest="as_seat",
            metavar="NAME",
            default=argparse.SUPPRESS,
            help="the bot this request acts as (the same as --as)",
        )

    p = add(sub, "guide", "print the Agent-Sync tools guide, or one ## section of it")
    p.add_argument(
        "section", nargs="?", metavar="SECTION", help="a section heading (case does not matter)"
    )

    p = add(
        sub,
        "notes",
        "write Apple Notes in the owner's Coding folder, directly on a Mac or through #notes-relay",
    )
    nsub = p.add_subparsers(dest="notes_action", metavar="ACTION", required=True)
    for action in ("create", "update"):
        q = add(nsub, action, "%s a note (body from stdin, --body-file or --html)" % action)
        q.add_argument("title", metavar="TITLE", help="[TEAM_KEY, SEAT_TAG] Topic")
        q.add_argument(
            "body",
            nargs="*",
            metavar="BODY",
            help="the body text, or - for stdin (default: stdin when piped)",
        )
        q.add_argument(
            "--body-file",
            dest="body_file",
            metavar="PATH",
            default=None,
            help="read the Markdown body from a private file (mode 0600 or 0400, at most %d KB)"
            % (bodyfile.MAX_BYTES // 1024),
        )
        q.add_argument(
            "--html",
            dest="html_file",
            metavar="PATH",
            default=None,
            help="the body is this HTML file",
        )
        q.add_argument("--no-pin", dest="no_pin", action="store_true", help="do not pin the note")
        q.add_argument(
            "--notify", action="store_true", help="also push a notification to the owner"
        )
        q.add_argument(
            "--needs-owner",
            dest="needs_owner",
            action="store_true",
            help="mark as needing the owner",
        )
        q.add_argument(
            "--summary", metavar="TEXT", default=None, help="one-line summary for the notification"
        )
        q.add_argument(
            "--pr", metavar="N", default=None, help="pull request numbers for the date line"
        )
        q.add_argument(
            "--relay", action="store_true", help="post a #notes-relay request even on a Mac"
        )
        q.add_argument(
            "--request-id",
            dest="request_id",
            metavar="ID",
            default=None,
            help="a unique id for this request",
        )
        identity_opt(q)
    for action in ("pin", "unpin"):
        q = add(nsub, action, "%s the note with this exact title" % action)
        q.add_argument("title", metavar="TITLE")
        q.add_argument(
            "--relay", action="store_true", help="post a #notes-relay request even on a Mac"
        )
        q.add_argument("--request-id", dest="request_id", metavar="ID", default=None)
        identity_opt(q)
    q = add(nsub, "status", "where a relayed request stands:  queued, done or refused")
    q.add_argument("request_id", metavar="REQUEST_ID")
    q.add_argument("--message-id", dest="message_id", type=int, default=None, metavar="ID")
    identity_opt(q)


def _fail(exc: N.NotesError) -> Z.AgentSyncError:
    if exc.code == "invalid_argument":
        return Z.UsageError(exc.message)
    return Z.ApiError(exc.message)


def cmd_guide(rt: Any, args: argparse.Namespace) -> int:
    from .mcp import guide as G

    try:
        text = G.read_text(rt.env)
        body, _section, _names = G.lookup(text, getattr(args, "section", None))
    except G.GuideError as exc:
        raise Z.UsageError(exc.message) from None
    rt.out(body if body.endswith("\n") else body + "\n")
    return 0


def _identity(rt: Any, args: argparse.Namespace) -> str:
    resolved = I.resolve_seat(
        rt.env, flag=getattr(args, "as_seat", None), default=getattr(args, "default_seat", None)
    )
    if resolved.problem:
        raise Z.CredentialError(resolved.problem)
    if not resolved.seat:
        raise Z.CredentialError(
            "no identity:  set AGENT_SEAT, or pass --identity NAME or --as NAME"
        )
    seat = Z.normalise_seat(resolved.seat)
    try:
        problem = X.agent_identity_problem(seat, registry=I.load_seat_registry(env=rt.env))
    except I.SeatRegistryError as exc:
        # Fail closed:  without the registry nothing proves this is a live agent identity, and the direct Mac
        # path checks no Zulip credential that could catch it.
        problem = (
            "cannot read the seat registry, so %s cannot be checked as an agent identity (%s)"
            % (seat, exc)
        )
    if problem:
        raise Z.CredentialError(problem)
    return seat


def _body(rt: Any, args: argparse.Namespace) -> tuple[str, bool]:
    """(body, is_html).  Exactly one source:  --html FILE, --body-file, BODY words or stdin.  A body longer than
    MARKDOWN_MAX characters is a usage error, never cut short."""
    body, is_html = _read_body(rt, args)
    if len(body) > N.MARKDOWN_MAX:
        raise Z.UsageError(N.BODY_TOO_LONG % N.MARKDOWN_MAX)
    return body, is_html


def _read_body(rt: Any, args: argparse.Namespace) -> tuple[str, bool]:
    sources = [args.html_file is not None, args.body_file is not None, bool(args.body)]
    if sum(sources) > 1:
        raise Z.UsageError(
            "give the body one way:  --html PATH, --body-file PATH, BODY, or standard input"
        )
    if args.html_file is not None:
        try:
            with open(args.html_file, encoding="utf-8") as fh:
                return fh.read(N.MARKDOWN_MAX + 1), True
        except (OSError, UnicodeDecodeError) as exc:
            raise Z.UsageError(
                "--html %s: cannot read it (%s)" % (args.html_file, type(exc).__name__)
            ) from None
    if args.body_file is not None:
        return bodyfile.read_body_file(args.body_file), False
    if args.body and args.body != ["-"]:
        return " ".join(args.body), False
    stdin = rt.stdin
    if args.body == ["-"] or not getattr(stdin, "isatty", lambda: False)():
        return stdin.read(N.MARKDOWN_MAX + 1), False
    return "", False


def cmd_notes(rt: Any, args: argparse.Namespace) -> int:
    """`notes create|update|pin|unpin|status`.  A write is audited exactly once, on every exit, in the `finally`
    below (identity_log, outcome only, never the body):  the branches only record in `audit` what happened.  A status
    check is a read and leaves no row."""
    from . import identity_log as AUDIT

    audit: dict[str, Any] = {}
    write = args.notes_action != "status"
    try:
        return _cmd_notes(rt, args, audit)
    except (KeyboardInterrupt, SystemExit):
        if audit.get("started") and "outcome" not in audit:
            audit["outcome"] = "interrupted"  # the write may have landed
        raise
    except Exception as exc:
        if audit.get("started") and "outcome" not in audit:
            audit["outcome"] = "outcome_unknown:%s" % type(exc).__name__  # the type name only
        raise
    finally:
        if write and audit.get("outcome") and audit.get("identity"):
            with contextlib.suppress(
                Exception
            ):  # an audit failure never changes the command's result
                AUDIT.append(
                    rt.env,
                    rt.home,
                    transport="cli",
                    default_identity=audit["identity"],
                    identity=audit["identity"],
                    tool="notes " + args.notes_action,
                    outcome=audit["outcome"],
                    message_id=audit.get("message_id")
                    if isinstance(audit.get("message_id"), int)
                    else None,
                )


def _cmd_notes(rt: Any, args: argparse.Namespace, audit: dict[str, Any]) -> int:
    from . import cli as CLI
    from .mcp import tools as T

    action = args.notes_action
    identity = _identity(rt, args)
    audit["identity"] = identity
    home = (
        Path(rt.home)
        if rt.home is not None
        else Path(rt.env.get("HOME") or os.path.expanduser("~"))
    )
    use_json = bool(getattr(args, "json", False))
    if action == "status":
        tool_name, call_args = "notes_status", {"request_id": args.request_id}
        if args.message_id is not None:
            call_args["message_id"] = args.message_id
    else:
        call_args = {"title": args.title, "transport": "relay" if args.relay else "auto"}
        if args.request_id:
            call_args["request_id"] = args.request_id
        if action in ("create", "update"):
            body, is_html = _body(rt, args)
            call_args.update(
                markdown=body,
                html=is_html,
                pin=not args.no_pin,
                notify=args.notify,
                needs_owner=args.needs_owner,
            )
            if args.summary:
                call_args["summary"] = args.summary
            if args.pr:
                call_args["pr"] = args.pr
            # AFC-361:  the session id, from the CLI's global --session ID or the environment, judged for the resolved
            # identity.  The relay path below hands it to the same tool code.
            try:
                session = SID.require(identity, getattr(args, "session", None), rt.env, "cli")
            except SID.SessionRequired as exc:
                audit["outcome"] = "session_required"
                raise Z.UsageError(str(exc)) from None
            except ValueError as exc:
                raise Z.UsageError(str(exc)) from None
            if session:
                call_args["session"] = session
        tool_name = "notes_" + action
    # Direct on a Mac:  no credential, no network.
    if action != "status" and not args.relay:
        script = N.direct_available(rt.env, home)
        if script is not None:
            audit["started"] = True
            try:
                envelope = N.make_envelope(
                    action,
                    args.title,
                    call_args.get("markdown", ""),
                    T.notes_flags(call_args, action),
                    call_args.get("request_id") or N.new_request_id(),
                    identity,
                    call_args.get("session"),
                )
                problem = N.title_problem(envelope["title"], identity, env=rt.env)
                if problem:
                    raise N.NotesError(
                        "invalid_argument",
                        "%s;  the Agent in the title must be %s" % (problem, identity),
                    )
                for field, text in N.text_fields(envelope).items():
                    found = scan_text(text)
                    if found:
                        raise N.NotesError(
                            "invalid_argument",
                            "refusing to write:  the %s looks like it contains %s" % (field, found),
                        )

                def send() -> dict[str, Any]:
                    done = N.run_direct(envelope, script, env=rt.env)
                    return {
                        "request_id": envelope["request_id"],
                        "action": action,
                        "status": "done",
                        "transport": "direct",
                        **({"note_id": done["note_id"]} if done.get("note_id") else {}),
                    }

                # The same persistent rows the MCP tools use (<state>/<SEAT>/mcp-idem.json):  a repeated --request-id
                # writes the note once, and a timed-out write is refused until the owner has checked the note.
                store = IdemStore(str(state_root(rt.env, home) / identity / "mcp-idem.json"))
                result = N.direct_once(store, envelope, send)
            except N.NotesError as exc:
                # A failed write is audited too, by its code only (never the body):  an outcome_unknown timeout
                # is exactly the row an owner looks for.
                audit["outcome"] = exc.code
                raise _fail(exc) from None
            audit["outcome"] = "ok"
            _print(rt, result, use_json)
            return 0
    # Relay (and status):  the MCP tool's own code over this identity's verified bot.
    agent = CLI.Agent(rt, args)
    CLI._check_agent_identity(rt, agent)
    cfg = C.load(str(state_root(rt.env, home)))
    consumer, consumer_label, _ = N.relay_consumer(cfg.notes_relay_seat)
    tools = T.Tools(
        agent,
        me=agent.me(),
        process_tag=agent.tag,
        session_source="flag",
        owner_user_id=cfg.owner_user_id,  # the pinned owner, or the OPERATOR consumer is never trusted
        notes_consumer=consumer,
        notes_consumer_label=consumer_label,
        sleep=rt.sleep,
        log=lambda text: None,
    )
    saved = (
        rt.stdout,
        rt.stderr,
        rt.stdin,
    )  # the tool layer swaps the runtime's streams for its own buffers
    audit["started"] = True  # from here a write may be in flight
    try:
        result = tools.call(tool_name, call_args)
    finally:
        rt.stdout, rt.stderr, rt.stdin = saved
    if result.get("isError"):
        error = json.loads(result["content"][0]["text"])
        message = str(error.get("message") or "the request failed")
        audit["outcome"] = str(error.get("code"))
        if error.get("code") == "invalid_argument":
            raise Z.UsageError(message)
        raise Z.ApiError(message)
    info = result.get("structuredContent") or {}
    audit["outcome"] = "ok"
    audit["message_id"] = info.get("message_id")
    _print(rt, info, use_json)
    return 0


def _print(rt: Any, info: dict[str, Any], use_json: bool) -> None:
    if use_json:
        rt.out(json.dumps(info, ensure_ascii=False) + "\n")
        return
    line = "%s:  %s" % (info.get("request_id"), info.get("status"))
    if info.get("transport"):
        line += " (%s)" % info["transport"]
    if info.get("message_id"):
        line += ", message %s" % info["message_id"]
    if info.get("reason"):
        line += ", reason:  %s" % info["reason"]
    rt.out(line + "\n")
