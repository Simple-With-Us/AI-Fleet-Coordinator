"""`open-loops send --dm-file PATH --facts PATH`:  the one way BF-Deployer's digest reaches the owner.

In order, stopping at the first refusal (nothing is sent after one):

  1. `agent-sync whoami --json` must say this is BF-DEPLOYER, verified;  anything else is refused (exit 3);
  2. the DM is linted against the facts (lint.py);  a problem is exit 4 and nothing is sent;
  3. the heartbeat topic needs the C2 umbrella id (`<C2 ID> open-loops heartbeat`);  without one the send is
     refused before anything goes out (exit 2), because a digest with no heartbeat hides a missing twin;
  4. `agent-sync dm-read --with owner --json --limit 30`:  if a message THIS bot sent already carries this
     facts id (or another id minted the same day, unless --again), the DM is not sent again;  if dm-read fails
     the send stops (exit 5), because "could not check" must not become "send twice";
  5. `agent-sync dm --owner --body-file F` sends the linted text, from a private copy (0600, in a 0700
     folder) so the bytes that were checked are the bytes that are sent;
  6. a no-mention heartbeat in topic `<C2 ID> open-loops heartbeat` (facts id, characters, sources ok,
     missing) with `--session openloop` and `--body-file`.  For a duplicate DM the heartbeat is posted only if
     the topic does not already carry this facts id.

Exit status:  0 sent (or already sent) and the heartbeat is in place;  2 usage or a missing C2 id;  3 refused
(wrong seat, no agent-sync, unsafe helper);  4 lint failed;  5 could not check for a duplicate, or the send
failed (a network failure after the request left may mean it was delivered:  it is not retried, check
`agent-sync dm-read --with owner`);  6 the DM is out but the heartbeat is not.

Every `agent-sync` command line is built by a function in this module so a test can run it through
`agent_sync.cli.build_parser()`.  The command lines never contain the DM text.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Any, Mapping, TextIO

from fleet_facts import safety
from fleet_facts.runner import Runner, which
from fleet_facts.runner import run as default_run

from . import lint as lintmod
from .files import EXIT_USAGE, FACTS_ID, Refusal, load_facts, read_private_file

__all__ = ["SEAT", "SESSION", "EXIT_USAGE", "EXIT_REFUSED", "EXIT_LINT", "EXIT_SEND", "EXIT_HEARTBEAT",
           "run_send", "load_facts", "read_private_file", "Refusal", "heartbeat_topic", "heartbeat_body",
           "argv_whoami", "argv_dm_read", "argv_dm", "argv_post", "argv_read_topic", "C2_ID"]

SEAT = "BF-DEPLOYER"
SESSION = "openloop"
EXIT_OK, EXIT_REFUSED, EXIT_LINT, EXIT_SEND, EXIT_HEARTBEAT = 0, 3, 4, 5, 6
DM_READ_LIMIT = 30
TIMEOUT_S = 60
C2_ID = re.compile(r"^[A-Z][A-Z0-9]{0,7}-\d{1,7}$")
_FACTS_ID = FACTS_ID


# ---- the agent-sync command lines ---------------------------------------------------------------

def argv_whoami(exe: str) -> list[str]:
    return [exe, "whoami", "--json"]


def argv_dm_read(exe: str) -> list[str]:
    return [exe, "dm-read", "--with", "owner", "--json", "--limit", str(DM_READ_LIMIT)]


def argv_dm(exe: str, body_file: str) -> list[str]:
    return [exe, "dm", "--owner", "--session", SESSION, "--json", "--body-file", body_file]


def heartbeat_topic(c2_id: str) -> str:
    return "%s open-loops heartbeat" % c2_id


def argv_post(exe: str, topic: str, body_file: str) -> list[str]:
    return [exe, "post", "--session", SESSION, "--topic", topic, "--json", "--body-file", body_file]


def argv_read_topic(exe: str, topic: str) -> list[str]:
    return [exe, "read", "--session", SESSION, "--topic", topic, "--json", "--limit", "10", "--include-self"]


class _Private:
    """A 0700 folder holding 0600 files, removed on exit."""

    def __enter__(self) -> "_Private":
        self.folder = tempfile.mkdtemp(prefix="open-loops-send-")
        os.chmod(self.folder, 0o700)
        return self

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.folder, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


# ---- agent-sync -------------------------------------------------------------------------------------

def _first_line(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return safety.clean(lines[0], 160) if lines else "no message"


def _json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            out.append(value)
    return out


def _whoami(run: Runner, exe: str) -> dict:
    result = run(argv_whoami(exe), timeout=TIMEOUT_S)
    if result.reason:
        raise Refusal(EXIT_REFUSED, "agent-sync whoami %s" % result.reason)
    try:
        info = json.loads(result.out)
    except ValueError:
        info = None
    if result.rc != 0 or not isinstance(info, dict):
        raise Refusal(EXIT_REFUSED, "agent-sync whoami failed (exit %s):  %s" % (result.rc, _first_line(result.err)))
    if info.get("seat") != SEAT or info.get("verified") is not True or info.get("bot_seat") not in (None, SEAT):
        raise Refusal(EXIT_REFUSED, "this is not %s (seat %s, bot seat %s, verified %s);  only BF-Deployer sends the digest"
                      % (SEAT, safety.clean(info.get("seat"), 40), safety.clean(info.get("bot_seat"), 40), info.get("verified")))
    user_id = info.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise Refusal(EXIT_REFUSED, "agent-sync whoami has no user id, so a duplicate cannot be told from the owner's own messages")
    return info


def _already_sent(run: Runner, exe: str, me: int, facts_id: str, again: bool) -> str | None:
    """"exact", "same-day" or None.  Raises Refusal(EXIT_SEND) when the conversation cannot be read."""
    result = run(argv_dm_read(exe), timeout=TIMEOUT_S)
    if result.reason or result.rc != 0:
        raise Refusal(EXIT_SEND, "could not check for a duplicate (agent-sync dm-read %s);  nothing was sent"
                      % (result.reason or "exited %s" % result.rc))
    try:
        messages = _json_lines(result.out)
    except ValueError:
        raise Refusal(EXIT_SEND, "could not check for a duplicate (dm-read printed something that is not JSON lines);  nothing was sent") from None
    day = _FACTS_ID.match(facts_id).group(1)
    same_day = re.compile(r"\bol-%s-\d{4}-[a-z0-9-]+" % day)
    verdict = None
    for message in messages:
        if message.get("sender_id") != me:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if facts_id in content:
            return "exact"
        if not again and same_day.search(content):
            verdict = "same-day"
    return verdict


def heartbeat_body(facts: Mapping[str, Any], characters: int) -> str:
    ok = [safety.clean(n, 30) for n, s in facts["sources"].items() if s.get("status") == "ok"]
    missing = [m.get("source") for m in facts["missing"] if isinstance(m, dict)]
    text = "Heartbeat for %s.  The DM is %d characters.  Sources ok:  %s.  Missing:  %s." % (
        facts["facts_id"], characters, ", ".join(ok) or "none", ", ".join(safety.clean(m, 30) for m in missing) or "none")
    return lintmod.prepare(text)


def _heartbeat_present(run: Runner, exe: str, topic: str, facts_id: str) -> bool:
    result = run(argv_read_topic(exe, topic), timeout=TIMEOUT_S)
    if result.reason or result.rc != 0:
        raise Refusal(EXIT_HEARTBEAT, "could not check the heartbeat topic (agent-sync read %s)" % (result.reason or "exited %s" % result.rc))
    try:
        return any(facts_id in str(m.get("content") or "") for m in _json_lines(result.out))
    except ValueError:
        raise Refusal(EXIT_HEARTBEAT, "could not check the heartbeat topic (read printed something that is not JSON lines)") from None


# ---- the sender -------------------------------------------------------------------------------------

def run_send(dm_file: str, facts_file: str, *, c2_id: str | None, dry_run: bool = False, again: bool = False,
             run: Runner = default_run, out: TextIO | None = None, err: TextIO | None = None) -> int:
    import sys
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        return _send(dm_file, facts_file, c2_id, dry_run, again, run, out)
    except Refusal as refusal:
        err.write("open-loops send: %s\n" % refusal)
        return refusal.code
    except safety.HelpersMissing as exc:
        err.write("open-loops send: %s;  nothing was sent\n" % exc)
        return EXIT_REFUSED


def _send(dm_file: str, facts_file: str, c2_id: str | None, dry_run: bool, again: bool, run: Runner, out: TextIO) -> int:
    facts = load_facts(facts_file)
    text = read_private_file(dm_file, "the DM file")
    exe = which("simple-chat")
    if exe is None:
        raise Refusal(EXIT_REFUSED, "agent-sync is not installed")
    me = _whoami(run, exe)["user_id"]
    problems = lintmod.lint(text, facts)
    if problems:
        for problem in problems:
            out.write("lint:  %s\n" % problem)
        raise Refusal(EXIT_LINT, "the DM did not pass %d check%s;  nothing was sent" % (len(problems), "" if len(problems) == 1 else "s"))
    if not c2_id or not C2_ID.match(c2_id):
        raise Refusal(EXIT_USAGE, "no C2 id for the heartbeat topic:  pass --c2-id, set OPEN_LOOPS_C2_ID, or install with "
                                  "--c2-id (the umbrella issue's id, such as AFC-123);  nothing was sent")
    ready = lintmod.prepare(text).strip() + "\n"
    topic = heartbeat_topic(c2_id)
    duplicate = _already_sent(run, exe, me, facts["facts_id"], again)
    if duplicate:
        out.write("already sent:  a DM from this bot carries %s;  not sent again\n" % ("this facts id" if duplicate == "exact" else "today's facts id (use --again to send another)"))
    if dry_run:
        out.write("dry run:  the DM passes the checks (%d characters)%s;  nothing was sent\n" % (
            len(ready), " and would not be sent again (duplicate)" if duplicate else ""))
        return EXIT_OK
    with _Private() as folder:
        if not duplicate:
            path = folder.write("dm.md", ready)
            sent = run(argv_dm(exe, path), timeout=TIMEOUT_S)
            if sent.reason or sent.rc != 0:
                maybe = sent.reason is not None or sent.rc == 6
                raise Refusal(EXIT_SEND, "the DM was not sent (agent-sync dm %s:  %s)%s" % (
                    sent.reason or "exited %s" % sent.rc, _first_line(sent.err),
                    ";  it may have been delivered, so it was not retried (check agent-sync dm-read --with owner)" if maybe else ""))
            try:
                info = json.loads(sent.out)
                out.write("sent id %s to the owner (%d characters)\n" % (info.get("id"), len(ready)))
            except ValueError:
                out.write("sent to the owner (%d characters)\n" % len(ready))
        beat = folder.write("heartbeat.md", heartbeat_body(facts, len(ready)))
        if duplicate and _heartbeat_present(run, exe, topic, facts["facts_id"]):
            out.write("heartbeat already in %s\n" % topic)
            return EXIT_OK
        posted = run(argv_post(exe, topic, beat), timeout=TIMEOUT_S)
        if posted.reason or posted.rc != 0:
            raise Refusal(EXIT_HEARTBEAT, "the DM is out but the heartbeat was not posted (agent-sync post %s:  %s)" % (
                posted.reason or "exited %s" % posted.rc, _first_line(posted.err)))
        out.write("heartbeat posted in %s\n" % topic)
    return EXIT_OK
