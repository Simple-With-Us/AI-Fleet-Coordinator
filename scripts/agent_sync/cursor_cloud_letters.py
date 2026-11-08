"""Operator session letters for Cursor Cloud agents, and a DM to the Grok Bot a chat is named for (owner request,
Nov 7, 2026).

    python3 -m agent_sync.cursor_cloud_letters sweep [--dry-run] [--remote DEST]

A Cloud agent runs its hooks on Cursor's own workers, which cannot reach the Operator's registry and drop the context a
hook gives at the start, so `platform_letter_cursor` leaves Cloud agents alone and this sweep gives them their letters
instead.  Each run:

1.  Reads the open Cloud agents (IDLE or ACTIVE) from the Cursor API (`CursorCloudTransport.discover`), with the key
    that module already uses (CURSOR_SYNC_API_KEY from Infisical Fleet-OPS, or CURSOR_API_KEY).  Only an agent updated
    within the last 36 hours goes on:  Cursor keeps a finished agent IDLE until it is archived.
2.  Registers each one in the `cursor` letter namespace at location `cloud`, under its own source, so the hook's local
    chats and these never sweep each other.  A registration also marks the agent seen, so a letter stays while the agent
    is open and the registry's 36 hour sweep frees it once the agent is gone.  The title is sent without any letter
    already on it.
3.  Tells the bot a chat is named for.  An agent whose title is "GB-Director: words" (or "XX: GB-Director: words", once
    it carries its letter) belongs to that Grok Bot, so the bot gets a Zulip DM with the chat's link, its letter, and the
    title to rename it to, "XX: GB-Director: words".  The owner allowed the wake (Nov 7, 2026):  the bot renames the
    chat.  A bot is told once per letter:  again only when the chat's letter or generation changes, never while the
    title already carries the letter, and never for a name that is not a GB bot's (`simple-chat dm` refuses an unknown
    name before anything is sent).  A DM whose outcome is unknown counts as sent, so a bot is never told twice.

The registry is reached the way the hooks reach it:  in process where the registry is (the Mac, or wherever the
registry API URL is configured), else over ssh to the destination in SESSION_LETTER_REMOTE or --remote.  The DM goes
out from the seat this runs as (AGENT_SEAT), through `simple-chat dm`.  What it told each bot is kept in
`<state>/CURSOR/cloud-letters.json`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import live as L
from . import platform_letter as PL
from . import platform_letter_cursor as PC

SOURCE = "cursor-cloud-letters"
LOCATION = "cloud"
SPEC = dataclasses.replace(PC.SPEC, source=SOURCE)
STATE_FILE = "cloud-letters.json"
DM_TIMEOUT = 30
# "GB-Director: words", with or without a letter in front:  the bot's name, then its words.
BOT_TITLE = re.compile(r"^(?:[A-Z]{1,2}: )?(GB-[A-Za-z][A-Za-z0-9]{0,31})\s*:\s*(\S.*)$")
# The marks Zulip reads as markup, a mention, a stream link or an emoji, kept out of a title quoted in a DM.
MARKUP = re.compile(r"[`*_~@#\[\]<>|$:!\\]")
# A Cloud agent's link, as the Cursor API gives it.
CURSOR_LINK = re.compile(r"https://cursor\.com/[A-Za-z0-9/?=&._-]{1,200}")
# The whole sweep's budget for DMs:  past it, the rest wait for the next run, so one slow `simple-chat` cannot hold the
# run past the timer's next start.  A registration still happens for every agent.
DM_BUDGET = 180
# Cursor keeps a finished Cloud agent IDLE until it is archived, so the open list holds agents untouched for weeks.  Only
# one updated within the registry's own stale window takes a letter (and wakes its bot);  an older one would hold a
# letter it never uses and wake a bot about a dead chat.  An agent with no readable updated_at is skipped too.
MAX_AGE_HOURS = 36
# A page of 100 agents takes longer than the transport's 5 second default;  30 is the transport's own ceiling.
API_TIMEOUT = 30

Runner = Callable[..., Any]
Transport = Callable[[list[str]], Mapping[str, Any]]


def bot_of(title: str) -> tuple[str, str]:
    """(the GB bot a title names, the words after its name), or ("", "") for a title that names none."""
    match = BOT_TITLE.match(PL.one_line(title))
    return (match.group(1), match.group(2).strip()) if match else ("", "")


def state_path(env: Mapping[str, str]) -> str:
    return os.path.join(L.state_root(env), PC.SEAT, STATE_FILE)


def read_state(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        with open(state_path(env), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(env: Mapping[str, str], state: Mapping[str, Any]) -> None:
    path = state_path(env)
    folder = os.path.dirname(path)
    if os.path.islink(folder):
        raise ValueError("the state folder is a symlink")  # a link could put private state anywhere
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".cloud-letters-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)  # the rename itself survives a crash
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def local_registry(env: Mapping[str, str]) -> bool:
    """Whether the registry answers in process here:  the Mac keeps the file, and a configured API URL is the store."""
    from . import registry_api

    return sys.platform == "darwin" or bool(registry_api.base_url(env))


def in_process(env: Mapping[str, str]) -> Transport:
    def call(argv: list[str]) -> dict[str, Any]:
        out = io.StringIO()
        PL.registry_main(list(argv), env=env, stdout=out)
        return PL._last_json(out.getvalue()) or {"error": "the registry gave no answer"}

    return call


def register(
    env: Mapping[str, str],
    agent: Mapping[str, Any],
    *,
    runner: Runner,
    now: float,
    remote: str,
    transport: Transport | None,
) -> dict[str, Any]:
    """Register one Cloud agent and answer with the registry's JSON (an `error` key when it failed)."""
    if transport is None and local_registry(env):
        transport = in_process(env)
    chat = PL.Chat(
        SPEC,
        env,
        LOCATION,
        str(agent["session_id"]),
        runner=runner,
        now=now,
        remote=remote,
        transport=transport,
        seat=PC.SEAT,
    )
    return chat.registry("register", title=PL.bare(agent.get("title") or ""))


def message(bot: str, agent: Mapping[str, Any], label: str, generation: int, words: str) -> str:
    """The DM to the bot a chat is named for.  Rendered copy:  a sentence gap is U+00A0 plus a space."""
    gen = " (gen %d)" % generation if generation > 1 else ""
    url = str(agent.get("url") or "")
    link = url if CURSOR_LINK.fullmatch(url) else str(agent.get("session_id"))
    title, words = plain(agent.get("title")), plain(words)
    return (
        'Your Cursor Cloud chat %s ("%s") holds Operator letter %s%s, and it is addressed as "@**Cursor** %s".  '
        'Please rename it to "%s: %s: %s" so its title carries the letter.'
        % (link, title, label, gen, label, label, bot, words)
    )


def plain(text: object) -> str:
    """A chat title as plain words for a Zulip DM:  one line with the marks Zulip reads as markup or a mention taken
    out, so a title can neither format the message nor @-mention anyone."""
    return PL.one_line(MARKUP.sub("", PL.one_line(text)))[:200]


def send_dm(bot: str, text: str, env: Mapping[str, str], runner: Runner) -> str:
    """Send one DM through `simple-chat dm`.  "sent", "refused" (it answered and sent nothing, such as for an unknown
    name), "unknown" (it may have sent), or "not-started" (it could not run, so nothing went out)."""
    try:
        proc = runner(
            ["simple-chat", "dm", "--to", bot, "--", text],
            capture_output=True,
            text=True,
            timeout=DM_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "not-started"
    if proc.returncode == 0:
        return "sent"
    said = (str(proc.stdout or "") + str(proc.stderr or "")).lower()
    # Only an answer that names the bot and refuses it is a refusal;  any other failure may have sent.
    named = bot.lower() in said and ("refus" in said or "unknown" in said or "ambiguous" in said)
    return "refused" if named else "unknown"


def recent(agent: Mapping[str, Any], now: float, max_age_hours: float = MAX_AGE_HOURS) -> bool:
    """Whether the agent's updatedAt is within `max_age_hours` of `now`.  A missing or unreadable time is not recent."""
    import datetime as dt

    stamp = str(agent.get("updated_at") or "").strip()
    if not stamp:
        return False
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    return now - when.timestamp() <= max_age_hours * 3600


def sweep(
    env: Mapping[str, str],
    *,
    discover: Callable[[], list[dict[str, Any]]] | None = None,
    complete: Callable[[], bool] | None = None,
    runner: Runner = subprocess.run,
    transport: Transport | None = None,
    remote: str = "",
    dry_run: bool = False,
    now: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    budget: float = DM_BUDGET,
) -> dict[str, Any]:
    """One pass over the open Cloud agents.  Answers counts and one row per agent, with no secrets in it."""
    now = time.time() if now is None else now
    started = clock()
    if discover is None:
        from .cursor_chat_transport import CursorCloudTransport

        cloud = CursorCloudTransport(timeout_seconds=API_TIMEOUT)
        discover = cloud.discover

        def complete() -> bool:
            return getattr(cloud, "last_discovery_status", "") == "complete"

    found = discover()
    # Whether the discovery saw every open agent:  only then is an agent missing from it gone.
    whole = complete() if complete is not None else True
    agents = [a for a in found if recent(a, now)]
    state = read_state(env)
    rows = []
    for agent in agents:
        sid = str(agent.get("session_id") or "")
        if not sid:
            continue
        row: dict[str, Any] = {"session_id": sid}
        bot, words = bot_of(str(agent.get("title") or ""))
        if bot:
            row["bot"] = bot
        if dry_run:
            rows.append(row)
            continue
        answer = register(env, agent, runner=runner, now=now, remote=remote, transport=transport)
        label = str(answer.get("label") or "")
        if "error" in answer or not label:
            row["error"] = str(answer.get("error") or "no letter")[:200]
            rows.append(row)
            continue
        generation = int(answer.get("generation") or 1)
        row["label"] = label
        told = state.get(sid) if isinstance(state.get(sid), dict) else {}
        current = PL.one_line(agent.get("title"))
        wanted = {"bot": bot, "label": label, "generation": generation}
        if (
            bot
            and not current.startswith(label + ": ")
            and {k: told.get(k) for k in wanted} != wanted
        ):
            if clock() - started > budget:
                row["dm"] = "deferred"  # the next run sends it
            else:
                outcome = send_dm(bot, message(bot, agent, label, generation, words), env, runner)
                row["dm"] = outcome
                # A sent DM is done, and so is one whose outcome is unknown:  it may have reached Zulip, and a bot is
                # never woken twice for one letter.  A refusal or a DM that never started sent nothing, so the next
                # run tries again.
                if outcome in ("sent", "unknown"):
                    state[sid] = dict(wanted, outcome=outcome, at=now)
        rows.append(row)
    if not dry_run:
        if whole:
            # Only a discovery that saw every open agent says one is gone:  a partial list (a page that failed, or
            # more agents than the cap) would forget what a bot was told, and tell it again next time.
            open_ids = {str(a.get("session_id") or "") for a in agents}
            for sid in [k for k in state if k not in open_ids]:
                del state[sid]  # the agent is gone; its letter is the registry's to free
        write_state(env, state)
    return {
        "agents": len(agents),
        "stale": len(found) - len(agents),
        "complete": whole,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cursor_cloud_letters")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sweep", help="give open Cursor Cloud agents letters and tell their GB bots")
    s.add_argument(
        "--dry-run", action="store_true", help="read the agents and print what it would do"
    )
    s.add_argument("--remote", default="", help="the ssh destination that holds the registry")
    args = parser.parse_args(argv)
    env = os.environ
    remote = args.remote or PL.remote_destination(env)
    try:
        out = sweep(env, remote=remote, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - one line for the log, never a traceback with a key in it
        # The Cursor API failure carries only an HTTP status and a code, never the response text.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "status": getattr(exc, "status", None),
                    "code": getattr(exc, "code", None),
                }
            )
        )
        return 1
    print(json.dumps(out, sort_keys=True))
    return 1 if any("error" in row for row in out["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
