"""`simple-notes create|update|pin|unpin|read|list|status|log|runner` and `simple-chat guide` (AFC-331, AFC-486).

The CLI twin of the notes_* tools:  one verb per tool and one option per argument (mcp/notes-tools.json is the
contract;  tests/test_notes_parity.py keeps them equal).  It runs the same code as the tools (notes_client.call), so
it works the same on every host:  on a Mac apple-notes-coding.sh runs directly and no credential is needed;
anywhere else the request goes to the Operator's notes queue and the command waits for the Mac runner's result
(up to --wait seconds, 300 by default).  No Zulip message is involved.  The body never reaches a command line of
ours:  it comes from standard input or --body-file (or --html for an HTML file), and is handed to the script on stdin.

`runner` is the Mac runner itself (notes_runner.py), run by the LaunchAgent com.jay.operator-notes-runner.

Exit codes follow the CLI:  2 usage, 3 credentials or configuration, 5 the note or the Operator refused, 6 network.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bodyfile
from . import identities as X
from . import identity as I
from . import notes as N
from . import notes_client as NC
from . import session_ids as SID
from . import zulip as Z
from .idem import IdemStore
from .secretscan import scan as scan_text
from .state import state_root

CLI_WAIT_DEFAULT = 300


def add_parsers(
    sub: Any, common: argparse.ArgumentParser, verbs: frozenset[str] | None = None
) -> None:
    """Register the `simple-notes` verbs, and the `guide` reader, on `sub`.

    The notes actions are top-level verbs of `simple-notes` (`simple-notes create ...`), not a
    `notes` group, so each one is added straight to `sub`.  `guide` prints the ST-MCP tools
    guide and stays on `simple-chat`; `verbs` lets each command keep the other's verbs out of its
    own `--help`.
    """

    # `status` and `read` are also verbs of `simple-chat` (the listener's status, a topic's history).  They are
    # registered only when the calling command's verb set asks for them, so the two never collide in one parser.
    shared = frozenset({"status", "read"})

    def add(parent: Any, name: str, summary: str) -> argparse.ArgumentParser:
        if name in shared and (verbs is None or name not in verbs):
            from .cli import _NullParser

            return _NullParser()
        return parent.add_parser(name, parents=[common], help=summary, description=summary)

    def identity_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--fleet-bearer-file",
            default=argparse.SUPPRESS,
            metavar="PATH",
            help="use this bot's private fleet bearer file via the hosted gateway; no direct fallback",
        )
        p.add_argument(
            "--identity",
            dest="as_seat",
            metavar="NAME",
            default=argparse.SUPPRESS,
            help="the bot this request acts as (the same as --as)",
        )

    def request_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--request-id",
            dest="request_id",
            metavar="ID",
            default=None,
            help="a unique id for this request (a retry with the same id never writes twice)",
        )
        p.add_argument(
            "--wait",
            dest="wait_seconds",
            type=int,
            metavar="SECONDS",
            default=None,
            help="off a Mac:  how long to wait for the Mac runner (default %d, 0 answers at once)"
            % CLI_WAIT_DEFAULT,
        )
        p.add_argument(
            "--no-wait",
            dest="no_wait",
            action="store_true",
            help="off a Mac:  answer queued at once (--wait 0)",
        )
        identity_opt(p)

    p = add(sub, "guide", "print the simple-tools guide, or one ## section of it")
    p.add_argument(
        "section", nargs="?", metavar="SECTION", help="a section heading (case does not matter)"
    )

    for action, summary in (
        (
            "create",
            "create a note (title [TEAM_KEY, Agent] Topic;  body from stdin, --body-file or --html)",
        ),
        (
            "update",
            "replace a note's body, any note in Coding (body from stdin, --body-file or --html)",
        ),
    ):
        q = add(sub, action, summary)
        q.add_argument("title", metavar="TITLE", help="the exact note title")
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
            help="the body is this HTML file (simple-notes read --format html gives one)",
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
        request_opts(q)
    for action in ("pin", "unpin"):
        q = add(sub, action, "%s the note with this exact title, any note in Coding" % action)
        q.add_argument("title", metavar="TITLE")
        request_opts(q)
    q = add(
        sub,
        "read",
        "print a note, any note in Coding (html is its content, ready for update --html)",
    )
    q.add_argument("title", metavar="TITLE")
    q.add_argument(
        "--format", choices=N.FORMATS, default=None, help="html (the default), text or raw"
    )
    request_opts(q)
    q = add(sub, "list", "list the notes in Coding, newest change first")
    q.add_argument(
        "--query", metavar="TEXT", default=None, help="only titles that contain this text"
    )
    q.add_argument(
        "--limit",
        type=int,
        metavar="N",
        default=None,
        help="at most this many (1 to %d, default %d)" % (N.LIST_MAX, N.LIST_DEFAULT),
    )
    request_opts(q)
    q = add(sub, "log", "the action log of apple-notes-coding.sh, newest first (never note text)")
    q.add_argument(
        "--since-hours",
        dest="since_hours",
        type=int,
        metavar="H",
        default=None,
        help="entries from the last H hours (default 24)",
    )
    q.add_argument(
        "--limit", type=int, metavar="N", default=None, help="at most N entries (default 50)"
    )
    q.add_argument(
        "--by-seat",
        dest="by_seat",
        metavar="SEAT",
        default=None,
        help="only the entries this seat made",
    )
    q.add_argument("--action", choices=N.ACTIONS, default=None, help="only this mode")
    q.add_argument(
        "--title", metavar="TEXT", default=None, help="only entries whose title contains this text"
    )
    q.add_argument(
        "--fleet-bearer-file",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="use this bot's private fleet bearer file; no direct fallback",
    )
    # No --identity:  the log is the whole fleet's (any seat may read every Coding note), so it acts as no one;
    # --by-seat filters it.
    q = add(
        sub, "runner", "the Mac runner for the Operator's notes queue (the LaunchAgent runs this)"
    )
    q.add_argument("--once", action="store_true", help="one pass, then exit")
    q.add_argument(
        "--claim-wait",
        dest="claim_wait",
        type=int,
        metavar="SECONDS",
        default=None,
        help="how long one claim waits for work (default 25)",
    )
    q = add(sub, "status", "where a request sent through the Operator stands")
    q.add_argument("request_id", metavar="REQUEST_ID")
    identity_opt(q)


def _fail(exc: N.NotesError) -> Z.AgentSyncError:
    if exc.code in ("invalid_argument", "session_required"):
        return Z.UsageError(exc.message)
    if exc.code in ("not_configured", "forbidden"):
        return Z.CredentialError(exc.message)
    # A lost enqueue is outcome_unknown, and its message keeps the network failure's own words.
    if exc.code in ("unavailable", "outcome_unknown") and "could not be reached" in exc.message:
        return Z.NetworkError(exc.message)
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
        # path checks no credential that could catch it.
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


def _action(args: argparse.Namespace) -> str:
    """The notes action: `simple-notes create` puts it in `command`; the MCP tools pass `notes_action`."""
    return getattr(args, "notes_action", None) or args.command


def _home(rt: Any) -> Path:
    return (
        Path(rt.home)
        if rt.home is not None
        else Path(rt.env.get("HOME") or os.path.expanduser("~"))
    )


def _host(rt: Any, identity: str | None, bearer_file: str | None = None) -> NC.Host:
    home = _home(rt)
    store = (
        IdemStore(str(state_root(rt.env, home) / identity / "mcp-idem.json")) if identity else None
    )
    return NC.Host(rt.env, home, store, caller="cli", bearer_file=bearer_file)


def _wait(args: argparse.Namespace) -> int:
    if getattr(args, "no_wait", False):
        return 0
    value = getattr(args, "wait_seconds", None)
    if value is None:
        return CLI_WAIT_DEFAULT
    if not 0 <= value <= CLI_WAIT_DEFAULT:
        raise Z.UsageError("--wait must be 0 to %d seconds" % CLI_WAIT_DEFAULT)
    return value


def _scan(request: dict[str, Any]) -> None:
    for field, text in N.text_fields(request).items():
        found = scan_text(text)
        if found:
            raise N.NotesError(
                "invalid_argument",
                "refusing to write:  the %s looks like it contains %s" % (field, found),
            )


def cmd_notes(rt: Any, args: argparse.Namespace) -> int:
    """Every `simple-notes` verb.  A write is audited exactly once, on every exit, in the `finally` below
    (identity_log, outcome only, never the body):  the branches only record in `audit` what happened.  A read, a list,
    a status check and the log leave no row."""
    from . import identity_log as AUDIT

    action = _action(args)
    if action == "runner":
        if getattr(args, "fleet_bearer_file", None) is not None:
            raise Z.UsageError(
                "runner is a backend service, not a fleet bearer action; no runner was started"
            )
        return _cmd_runner(rt, args)
    audit: dict[str, Any] = {}
    write = action in N.WRITE_ACTIONS
    try:
        return _cmd_notes(rt, args, audit)
    except (KeyboardInterrupt, SystemExit):
        if "outcome" not in audit:
            # Started:  the write may have landed.  Not started:  nothing was sent, and the attempt is still audited.
            audit["outcome"] = "interrupted" if audit.get("started") else "interrupted_before_send"
        raise
    except Exception as exc:
        if "outcome" not in audit:
            # The type name only.  Started:  the write may have landed.  Not started (its body or an option was
            # refused once the identity was known):  nothing ran, and the attempt is still audited.
            audit["outcome"] = (
                "outcome_unknown:%s" if audit.get("started") else "refused:%s"
            ) % type(exc).__name__
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
                    tool="notes " + action,
                    outcome=audit["outcome"],
                    message_id=None,
                )


def _cmd_notes(rt: Any, args: argparse.Namespace, audit: dict[str, Any]) -> int:
    action = _action(args)
    if (
        getattr(args, "fleet_bearer_file", None) is not None
        and not str(args.fleet_bearer_file).strip()
    ):
        raise Z.CredentialError("fleet bearer file path is empty; no fallback was used")
    use_json = bool(getattr(args, "json", False))
    if action == "log":
        host = _host(rt, None, getattr(args, "fleet_bearer_file", None))
        try:
            info = NC.log(
                host,
                {
                    name: getattr(args, name)
                    for name in ("since_hours", "limit", "by_seat", "action", "title")
                    if getattr(args, name, None) is not None
                },
            )
        except N.NotesError as exc:
            raise _fail(exc) from None
        _print_log(rt, info, use_json)
        return 0
    bearer_file = getattr(args, "fleet_bearer_file", None)
    if bearer_file is not None:
        from .fleet_bearer import Client, FleetError

        try:
            identity = Client(bearer_file, identity_env=rt.env).seat
        except FleetError as exc:
            raise Z.CredentialError(str(exc)) from None
        selected = getattr(args, "as_seat", None) or getattr(args, "default_seat", None)
        if selected and I.canonical_seat(selected) != identity:
            raise Z.UsageError("--identity/--as cannot override the fleet bearer identity")
    else:
        identity = _identity(rt, args)
    audit["identity"] = identity
    host = _host(rt, identity, bearer_file)
    if action == "status":
        try:
            info = NC.status(host, identity, args.request_id)
        except N.NotesError as exc:
            raise _fail(exc) from None
        _print(rt, info, use_json)
        return 0
    session = None
    markdown, flags = "", None
    if action in N.BODY_ACTIONS:
        body, is_html = _body(rt, args)
        markdown = body
        flags = {"html": is_html, "notify": args.notify, "needs_owner": args.needs_owner}
        if args.no_pin:
            flags["pin"] = False
        if args.summary:
            flags["summary"] = args.summary
        if args.pr:
            flags["pr"] = args.pr
        flags = {
            name: value
            for name, value in flags.items()
            if value not in (False, None, "") or name == "pin"
        }
        # AFC-361:  the session id, from the CLI's global --session ID or the environment, judged for the resolved
        # identity.
        try:
            session = SID.require(identity, getattr(args, "session", None), rt.env, "cli")
        except SID.SessionRequired as exc:
            audit["outcome"] = "session_required"
            raise Z.UsageError(str(exc)) from None
        except ValueError as exc:
            audit["outcome"] = (
                "invalid_argument"  # a refused write attempt is audited too, by its code only
            )
            raise Z.UsageError(str(exc)) from None
    wait = _wait(args)
    try:
        request = N.make_request(
            action,
            identity,
            request_id=args.request_id or N.new_request_id(),
            title=getattr(args, "title", None) if action != "list" else None,
            markdown=markdown,
            flags=flags,
            session=session,
            fmt=getattr(args, "format", None) if action == "read" else None,
            query=getattr(args, "query", None) if action == "list" else None,
            limit=getattr(args, "limit", None) if action == "list" else None,
        )
        if action in N.WRITE_ACTIONS:
            audit["started"] = True
        info = NC.call(host, identity, request, wait=wait, scan=_scan)
    except N.NotesError as exc:
        # A failed write is audited too, by its code only (never the body):  an outcome_unknown timeout is exactly
        # the row an owner looks for.
        audit["outcome"] = exc.code
        raise _fail(exc) from None
    audit["outcome"] = "ok" if info.get("status") == "done" else str(info.get("status"))
    if action == "read":
        _print_read(rt, info, use_json)
    elif action == "list":
        _print_list(rt, info, use_json)
    else:
        _print(rt, info, use_json)
    return 0


def _cmd_runner(rt: Any, args: argparse.Namespace) -> int:
    from . import notes_runner as RUN
    from . import notes_wire as W

    home = _home(rt)
    try:
        runner = RUN.runner_for(rt.env, home, log=lambda text: rt.err(text + "\n"))
    except N.NotesError as exc:
        raise _fail(exc) from None
    wait = args.claim_wait if args.claim_wait is not None else W.CLAIM_WAIT_MAX
    if not 0 <= wait <= W.CLAIM_WAIT_MAX:
        raise Z.UsageError("--claim-wait must be 0 to %d seconds" % W.CLAIM_WAIT_MAX)
    try:
        runner.acquire()
    except RUN.AlreadyRunning as exc:
        raise Z.UsageError(str(exc)) from None
    try:
        if args.once:
            try:
                runner.ship_log()
                handled = runner.once(wait)
            except N.NotesError as exc:
                raise _fail(exc) from None
            rt.out("notes runner:  %s\n" % ("handled one request" if handled else "nothing queued"))
            return 0
        stop = threading.Event()
        previous: dict[Any, Any] = {}
        try:
            # Installed inside the try, one at a time, so whatever was installed is restored on every exit path.
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            runner.loop(stop, wait)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return 0
    finally:
        runner.release()


def _when(value: Any) -> str:
    """An ISO time as the fleet writes it:  'Thu, Oct 22, 5:03pm' (America/Chicago)."""
    from .cli import format_time

    try:
        return format_time(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return str(value or "")


def _print(rt: Any, info: dict[str, Any], use_json: bool) -> None:
    if use_json:
        rt.out(json.dumps(info, ensure_ascii=False) + "\n")
        return
    line = "%s:  %s" % (info.get("request_id"), info.get("status"))
    if info.get("transport"):
        line += " (%s)" % info["transport"]
    if info.get("note_id"):
        line += ", note %s" % info["note_id"]
    if info.get("duplicate"):
        line += ", already done before"
    error = info.get("error")
    if isinstance(error, dict) and error.get("message"):
        line += ", %s:  %s" % (error.get("code"), error["message"])
    if info.get("pin_skipped"):
        line += ", pin skipped:  %s" % info["pin_skipped"]
    if info.get("poll_warning"):
        line += "; " + str(info["poll_warning"])
    if info.get("status") in ("queued", "running"):
        line += ";  the Mac runner has not finished it:  simple-notes status %s" % info.get(
            "request_id"
        )
    rt.out(line + "\n")


def _print_read(rt: Any, info: dict[str, Any], use_json: bool) -> None:
    note = info.get("note") if isinstance(info.get("note"), dict) else None
    if use_json or note is None:
        _print(rt, info, use_json)
        return
    body = str(note.get("body") or "")
    rt.out(body if body.endswith("\n") else body + "\n")
    if note.get("truncated"):
        rt.err("notes:  the note is longer than %d bytes;  the body was cut\n" % N.READ_MAX)


def _print_list(rt: Any, info: dict[str, Any], use_json: bool) -> None:
    if use_json or info.get("status") != "done":
        _print(rt, info, use_json)
        return
    for row in info.get("notes") or []:
        rt.out("%s  %s\n" % (_when(row.get("modified")), row.get("title")))
    shown, total = len(info.get("notes") or []), info.get("total", 0)
    if info.get("truncated"):
        rt.out("(%d of %d shown;  raise --limit or narrow --query)\n" % (shown, total))


def _print_log(rt: Any, info: dict[str, Any], use_json: bool) -> None:
    if use_json:
        rt.out(json.dumps(info, ensure_ascii=False) + "\n")
        return
    for entry in info.get("entries") or []:
        parts = [
            _when(entry.get("ts")),
            str(entry.get("mode") or "?"),
            str(entry.get("outcome") or entry.get("exit")),
            str(entry.get("seat") or "-"),
            str(entry.get("caller") or "-"),
        ]
        reformatted = entry.get("reformatted") if isinstance(entry.get("reformatted"), dict) else {}
        if reformatted.get("dates") or reformatted.get("gaps"):
            parts.append(
                "reformatted %s dates, %s gaps"
                % (reformatted.get("dates", 0), reformatted.get("gaps", 0))
            )
        rt.out("%s  %s\n" % ("  ".join(parts), entry.get("title") or ""))
    rt.out("(%d entries from the %s log)\n" % (info.get("count", 0), info.get("source")))
