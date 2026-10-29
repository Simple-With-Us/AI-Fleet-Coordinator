#!/usr/bin/env bash
# Create (or update) an Apple Note in the iCloud "Coding" folder and try to pin it.
# Owner preference 2026-08-05: coding notes always go to Coding + pin to top.
# Owner 2026-08-09: title + timestamp shape (ALL apps, ALL agents):
#
#   Title:  "[TEAM_KEY, Agent] Topic"  e.g. "[UM, Claude] TestFlight first ship"
#           Multi-app: "[ST, CT, MM] …"  Acronyms: UM ST CT CTS AFC (TEAM_KEY is the team key)
#   Row 2:  "Sun, Aug 9, 3:52pm"         local create/update stamp (auto-injected)
#
# Usage:
#   apple-notes-coding.sh "Title" "Plain body text or Markdown"
#   apple-notes-coding.sh "Title" --html /path/to/body.html
#   apple-notes-coding.sh --update "Title" "Body text"
#   apple-notes-coding.sh --update --pin --pr "46" "Title" "Body text"
#   echo "body" | apple-notes-coding.sh "Title"
#   apple-notes-coding.sh --session "$CLAUDE_CODE_SESSION_ID" "Title" "Body"   # create/update need a session id
#   apple-notes-coding.sh --update --existing-only "Title" "Body"   # never creates:  a missing note exits 3
#   apple-notes-coding.sh --pin-only "Exact Note Title"
#   apple-notes-coding.sh --unpin-only "Exact Note Title"
#   apple-notes-coding.sh --read "Exact Note Title"                 # or --read --title "Exact Note Title"
#   apple-notes-coding.sh --list [--query TEXT] [--limit N]         # N from 1 to 500, 100 when not given
#   APPLE_NOTES_PIN=0 apple-notes-coding.sh "Title" ...        # skip pinning
#   APPLE_NOTES_PIN_RETRY=0 apple-notes-coding.sh "Title" ...  # pin Shortcut: one attempt, no retry
#   APPLE_NOTES_ACTIVATE=1 apple-notes-coding.sh "Title" ... # bring Notes to front
#
# Session id (AFC-361, owner 2026-10-19):  create and update stamp the writer's session id into the note, so a
# headless resume can find a seat's session for a topic from the note (resume-wake design section 4).  The id is
# --session ID, else $AGENT_SESSION, else the seat's platform variable (Claude Code CLAUDE_CODE_SESSION_ID, Codex
# CODEX_THREAD_ID, Cursor CURSOR_CONVERSATION_ID, Grok GROK_SESSION_ID, OpenCode OPENCODE_SESSION_ID, MiniMax
# __MAVIS_RUNTIME_SESSION_ID).  With none the script exits 2 before it touches Notes.  A seat with one thread with the
# owner (Grok bots, MA/Rob, JET, WISE, ECHO) needs none.  The seat is $AGENT_LAUNCH_SEAT (with $AGENT_LAUNCHER),
# else $AGENT_SEAT, else the agent named in the title ("[TEAM_KEY, WISE] topic", how Instinct's bots call this over ssh);
# it is never a flag.  Data copy of fleet-apps.json `sessionIds`;  scripts/test_session_ids_parity.py keeps it equal.
# APPLE_NOTES_PRINT_HTML=1 prints the note HTML and exits before Notes is touched (the tests use it).
#
# Focus policy (owner 2026-08-10): create/update MUST NOT steal focus by default.
# Notes receives AppleScript body edits without activate/show.
#
# Pinning (owner + Claude 2026-08-10): fully headless, and ONLY headless.  The "Pin Coding Note" macOS
# Shortcut (Receive Text from Share Sheet → Find Note where Name contains Shortcut Input + Folder is
# Coding, limit 1 → Add Note to pinned notes) is the single way this helper pins.  No window, no focus
# steal, no Accessibility.  Unpin twin: "Unpin Coding Note" (same, with Remove).  First run of each
# shortcut shows a one-time "Allow ... to share with Notes?" dialog — choose Always Allow.
#
# There is NO GUI fallback (owner 2026-10-20:  something kept popping Notes open at 7:07pm;  the cause
# was the old menu-click path that brought Notes forward and clicked its Pin menu item whenever the
# Shortcut did not pin).  When the Shortcut does not pin, the helper writes
# "pin skipped: ... note saved unpinned" on stderr and carries on:  create and update still succeed
# (exit 0), and --pin-only exits 4.  Nothing in this script brings Notes forward except the explicit,
# opt-in --activate / --front / APPLE_NOTES_ACTIVATE=1.  --pin and APPLE_NOTES_PIN=1 are kept so old
# callers still parse;  they no longer change anything, because pinning through the Shortcut is
# already the default.
# One pin or unpin Shortcut runs at a time across processes (a mkdir lock, NOTES_PIN_LOCK_DIR), and a
# run that hit its time bound is never repeated:  a hang is a picker waiting on the owner's screen, not a
# race, and a second run only opens a second picker.
#
# Read and list (AFC-486, docs/protocols/operator-notes.md):  --read and --list need no session id, never write,
# and never bring Notes forward.  They ask Notes through JXA with the title and the query in the environment, never
# in the script source.  --read prints one JSON object on stdout:
#   {"title", "id", "created", "modified", "body", "plaintext"}
# where body is the note's HTML as Notes stores it, plaintext its text, and the dates ISO-8601 in UTC.  --list
# prints {"notes": [{"title", "id", "created", "modified"}, ...], "total": n, "truncated": bool}, newest change
# first;  --query keeps the titles that contain TEXT (case does not matter), total counts every note that matched,
# and truncated says the limit cut the list.  Both print plain-ASCII JSON (anything past ASCII is a \u escape).
# --existing-only (with --update only) makes an update of a missing note exit 3 instead of creating it.
#
# The action log (AFC-486):  every run that gets past argument parsing appends one JSON line to
# $APPLE_NOTES_ACTION_LOG (default ~/Library/Logs/fleet/apple-notes-actions.jsonl, "off" turns it off):  what was
# asked, how it ended and how long it took, never a note's text.  AGENT_NOTES_CALLER (default script),
# AGENT_NOTES_REQUEST_ID and AGENT_NOTES_REQUESTER name who asked;  the callers in scripts/agent_sync/notes.py set
# them.  A preview (APPLE_NOTES_PRINT_HTML=1 on a create or update) writes nothing to Notes and logs nothing.
#
# Exit codes:  0 done (a create/update whose pin was skipped is still 0), 2 usage or session error,
# 3 --pin-only / --unpin-only / --read / --update --existing-only found no note with that title, 4 --pin-only /
# --unpin-only did not pin or unpin the note.  Anything else is a failure the script did not plan for (an
# osascript error, say), and the code is the failing command's own.
# Canonical policy: /Users/jay/apps/FLEET-PROTOCOL.md § Apple Notes.

set -euo pipefail

MODE=create
WANT_ACTIVATE=0
WANT_NOTIFY=0
NEEDS_OWNER=0
SUMMARY_TEXT=""
PR_NUMBERS="${PR_NUMBERS:-${APPLE_NOTES_PR:-}}"
HTML_PATH=""
TITLE=""
SESSION_ARG=""
# How long an AppleEvent to Notes may run.  AppleScript's own default is two minutes, and a
# body the size of the Background Jobs Master List needs longer, so every "tell application
# \"Notes\"" below is wrapped in "with timeout of NOTES_APPLE_EVENT_TIMEOUT_S seconds".  A
# caller that runs this script must allow more than this, or it kills the write while
# osascript is still working and the later completion creates a duplicate note (AFC-436).
# The number is this long because that body failed with
# "AppleEvent timed out. (-1712)" before this timeout existed.
NOTES_APPLE_EVENT_TIMEOUT_S="${NOTES_APPLE_EVENT_TIMEOUT_S:-900}"
#  The value goes straight into the AppleScript source,  so it has to be a plain number of
#  seconds:  a typo ("900s") or a value carrying a quote makes osascript reject the program and
#  aborts the write with an opaque syntax error instead of a message about the override.
#  Anything that is not digits falls back to the default (AFC-436).
[[ "$NOTES_APPLE_EVENT_TIMEOUT_S" =~ ^[0-9]+$ ]] || NOTES_APPLE_EVENT_TIMEOUT_S=900
# How long one Shortcuts invocation may run.  The "Pin Coding Note" Shortcut can wait on a note
# picker on the owner's screen,  so an unbounded run can outlast the caller's cap:  the note is
# written and the caller is then told the outcome is unknown and refuses the retry (AFC-436).
NOTES_SHORTCUT_TIMEOUT_S="${NOTES_SHORTCUT_TIMEOUT_S:-30}"
[[ "$NOTES_SHORTCUT_TIMEOUT_S" =~ ^[0-9]+$ ]] || NOTES_SHORTCUT_TIMEOUT_S=30
# One pin or unpin Shortcut at a time across processes.  Every create and update ends in a pin, and
# several seats call this helper at once from separate processes (the chat relay serializes only
# its own calls).  Two overlapping runs of the Shortcut fight over the same Find Notes picker on the
# owner's screen and make each other fail, so a run takes this lock first.  macOS has no flock(1), so the
# lock is a directory made with mkdir, which is atomic, holding the owner's pid.  A lock whose pid is gone,
# or that is older than any live run could be (the pin phase is bounded by NOTES_SHORTCUT_TIMEOUT_S, see
# _pin_lock_stale), is stale and is taken over.  A caller that cannot get the lock in NOTES_PIN_LOCK_WAIT_S seconds skips the
# pin and says so, rather than queueing a second Shortcut behind the first.
NOTES_PIN_LOCK_DIR="${NOTES_PIN_LOCK_DIR:-${HOME:-/tmp}/Library/Caches/apple-notes-coding/pin.lock}"
NOTES_PIN_LOCK_WAIT_S="${NOTES_PIN_LOCK_WAIT_S:-20}"
[[ "$NOTES_PIN_LOCK_WAIT_S" =~ ^[0-9]+$ ]] || NOTES_PIN_LOCK_WAIT_S=20
_PIN_LOCK_HELD=0

# Env overrides (agents may set these explicitly).
# APPLE_NOTES_PIN=1 needs no handling:  pinning through the Shortcut is already the default (APPLE_NOTES_PIN=0 turns it off).
[[ "${APPLE_NOTES_ACTIVATE:-0}" == "1" || "${APPLE_NOTES_ACTIVATE:-}" == "true" ]] && WANT_ACTIVATE=1
[[ "${APPLE_NOTES_NOTIFY:-0}" == "1" || "${APPLE_NOTES_NOTIFY:-}" == "true" ]] && WANT_NOTIFY=1
[[ "${APPLE_NOTES_NEEDS_OWNER:-0}" == "1" || "${APPLE_NOTES_NEEDS_OWNER:-}" == "true" ]] && NEEDS_OWNER=1

EXISTING_ONLY=0
LIST_QUERY=""
LIST_LIMIT=""
_LIST_LIMIT_SET=0

# ---- The action log (AFC-486) ----------------------------------------------------------------------------------
# One JSON line per run, appended by _action_log_line from the EXIT trap, so a run that ends anywhere after the
# argument parsing (a usage or session refusal, a missing note, a failed pin, a success) is logged exactly once.  The
# line never holds a note's text:  the title, ids, sizes, a hash of the HTML written, counts and flags.  The writer is
# a short /usr/bin/python3 step:  the directory is made mode 700, the file mode 600, the append and the rotation
# (past 5 MB the file becomes .1, .1 becomes .2 and so on to .5, and the oldest is dropped) happen under one flock on
# a hidden lock file next to the log, and the whole step is bounded by a 5 s alarm.  It runs isolated (-I):  it needs
# the standard library only, and no PYTHONPATH or user site-packages can slow it or change it.  Whatever goes wrong
# there is dropped:  a log that cannot be written never changes the exit code or the output.
APPLE_NOTES_ACTION_LOG="${APPLE_NOTES_ACTION_LOG:-${HOME:-/tmp}/Library/Logs/fleet/apple-notes-actions.jsonl}"
#  The start time, for duration_ms.  bash 3.2 (macOS) has no EPOCHREALTIME, so perl's clock stands in for it, and
#  whole seconds when perl is missing too.
_LOG_T0="${EPOCHREALTIME:-}"
if [[ -z "$_LOG_T0" ]]; then
  _LOG_T0="$(perl -MTime::HiRes=time -e 'printf("%.6f", time)' 2>/dev/null || date +%s)"
fi
_LOG_INPUT_BYTES=""
_LOG_CHANGES_FILE=""
_LOG_LINKS_FILE=""
_NOTES_OUT=""
_TMP_MADE=0
_TITLE_REJECTED=0
NOTE_ID=""

#  The session a non-writing run came from, for the log only:  the same order the write check uses, never an error,
#  and nothing that is not shaped like a session id.
_log_session() {
  local s="${NOTE_SESSION:-}" v pat='^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$'
  if [[ -z "$s" ]]; then
    s="${SESSION_ARG:-${AGENT_SESSION:-}}"
    if [[ -z "$s" ]] && declare -F _session_envs >/dev/null; then
      for v in $(_session_envs "$(_session_seat)"); do
        if [[ -n "${!v:-}" ]]; then s="${!v}"; break; fi
      done
    fi
  fi
  [[ "$s" =~ $pat ]] || s=""
  printf '%s' "$s"
}

_action_log_line() {  # $1 = the exit code the script is leaving with
  local log="${APPLE_NOTES_ACTION_LOG:-}" outcome title="" seat="" pin="" note_id="${NOTE_ID:-}" body_file=""
  case "$log" in ""|[Oo][Ff][Ff]) return 0 ;; esac
  if [[ "${APPLE_NOTES_PRINT_HTML:-0}" == "1" ]] && [[ "$MODE" == "create" || "$MODE" == "update" ]]; then
    return 0
  fi
  [[ -x /usr/bin/python3 ]] || return 0
  case "$1" in
    0) outcome=done ;;
    2) outcome=usage ;;
    3) outcome=not_found ;;
    4) outcome=pin_failed ;;
    *) outcome=error ;;
  esac
  #  A title the secret scan refused is never written down, and neither is the seat read out of it.
  if [[ "$_TITLE_REJECTED" != "1" ]]; then
    title="$TITLE"
    declare -F _session_seat >/dev/null && seat="$(_session_seat)"
  fi
  [[ -n "$seat" ]] || seat="$(printf '%s' "${AGENT_SEAT:-}" | tr '[:lower:]' '[:upper:]')"
  #  _pin_rc is set by the pin block at the end:  0 pinned, 4 pinning switched off, anything else skipped.
  case "$MODE" in
    create|update|pin)
      case "${_pin_rc:-}" in
        "") ;;
        0) pin=pinned ;;
        4) pin=off ;;
        *) pin=skipped ;;
      esac
      ;;
  esac
  [[ "$note_id" == "__APPLE_NOTES_NOT_FOUND__" ]] && note_id=""
  [[ "$_TMP_MADE" == "1" ]] && body_file="${TMP:-}"
  _NL_LOG="$log" _NL_EXIT="$1" _NL_OUTCOME="$outcome" _NL_MODE="$MODE" _NL_TITLE="$title" \
  _NL_NOTE_ID="$note_id" _NL_T0="$_LOG_T0" _NL_SEAT="$seat" _NL_SESSION="$(_log_session)" \
  _NL_CALLER="${AGENT_NOTES_CALLER:-script}" _NL_REQUEST_ID="${AGENT_NOTES_REQUEST_ID:-}" \
  _NL_REQUESTER="${AGENT_NOTES_REQUESTER:-}" _NL_INPUT_BYTES="$_LOG_INPUT_BYTES" _NL_BODY_FILE="$body_file" \
  _NL_CHANGES_FILE="$_LOG_CHANGES_FILE" _NL_OUT_FILE="$_NOTES_OUT" _NL_PIN="$pin" _NL_NOTIFY="$WANT_NOTIFY" \
  _NL_NEEDS_OWNER="$NEEDS_OWNER" _NL_HTML="${HTML_PATH:+1}" _NL_EXISTING_ONLY="$EXISTING_ONLY" \
  _NL_SUMMARY="${SUMMARY_TEXT:+1}" _NL_PR="$PR_NUMBERS" _NL_LINKS_FILE="$_LOG_LINKS_FILE" \
  /usr/bin/python3 -I -c '
import fcntl, hashlib, json, os, signal, socket, time, uuid
from datetime import datetime

signal.alarm(5)
E = os.environ.get
ROTATE_AT = 5 * 1024 * 1024
KEEP = 5


def num(name):
    try:
        return int(str(E(name, "")).strip())
    except ValueError:
        return None


def short(name, cap):
    value = E(name, "")
    return value[:cap] if value else None


mode = E("_NL_MODE", "")
outcome = E("_NL_OUTCOME", "")
writes = mode in ("create", "update")
try:
    duration = max(0, int(round((time.time() - float(E("_NL_T0", "").replace(",", "."))) * 1000)))
except ValueError:
    duration = None
entry = {
    "v": 1,
    "id": uuid.uuid4().hex,
    "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
    "host": socket.gethostname().split(".")[0] or None,
    "mode": mode or None,
    "title": short("_NL_TITLE", 200),
    "note_id": short("_NL_NOTE_ID", 200),
    "exit": num("_NL_EXIT"),
    "outcome": outcome,
    "duration_ms": duration,
    "seat": short("_NL_SEAT", 64),
    "session": short("_NL_SESSION", 128),
    "caller": short("_NL_CALLER", 40) or "script",
    "request_id": short("_NL_REQUEST_ID", 128),
    "requester": short("_NL_REQUESTER", 64),
    "input_bytes": num("_NL_INPUT_BYTES") if writes else None,
    "body_bytes": None,
    "body_sha256": None,
    "reformatted": None,
    "pin": short("_NL_PIN", 16),
    "flags": {
        "notify": E("_NL_NOTIFY") == "1",
        "needs_owner": E("_NL_NEEDS_OWNER") == "1",
        "html": E("_NL_HTML") == "1",
        "existing_only": E("_NL_EXISTING_ONLY") == "1",
        "summary": E("_NL_SUMMARY") == "1",
        "pr": short("_NL_PR", 80),
    },
}
#  The size and hash of the HTML the script handed Notes:  enough to tell two writes apart, never the text.
if writes and E("_NL_BODY_FILE") and outcome not in ("usage", "not_found"):
    try:
        with open(E("_NL_BODY_FILE"), "rb") as fh:
            body = fh.read()
        entry["body_bytes"] = len(body)
        entry["body_sha256"] = hashlib.sha256(body).hexdigest()
    except OSError:
        pass
if writes and E("_NL_CHANGES_FILE"):
    try:
        with open(E("_NL_CHANGES_FILE")) as fh:
            dates, gaps = fh.read().split()
        entry["reformatted"] = {"dates": int(dates), "gaps": int(gaps)}
    except (OSError, ValueError):
        pass
if writes and E("_NL_LINKS_FILE"):
    try:
        with open(E("_NL_LINKS_FILE")) as fh:
            counts = fh.read().split()
        if not entry.get("reformatted"):
            entry["reformatted"] = {}
        #  The counts are the issues, the pull requests, the projects and the six newer kinds that
        #  became links.  A file written before a kind was counted carries fewer numbers, so a missing
        #  one past the first two is read as none rather than as a lost count.
        kinds = ("projects", "fleetlink", "swu", "repos", "pagerduty", "sentry", "cursor")
        links = {"issues": int(counts[0]), "prs": int(counts[1])}
        for i, kind in enumerate(kinds, start=2):
            links[kind] = int(counts[i]) if len(counts) > i else 0
        entry["reformatted"]["links"] = links
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        pass
if mode in ("read", "list"):
    key = "bytes" if mode == "read" else "count"
    entry[key] = None
    if outcome == "done" and E("_NL_OUT_FILE"):
        try:
            with open(E("_NL_OUT_FILE"), encoding="utf-8") as fh:
                data = json.load(fh)
            if mode == "read":
                entry["note_id"] = str(data.get("id") or "")[:200] or None
                entry[key] = len(str(data.get("body") or "").encode("utf-8"))
            else:
                entry[key] = len(data.get("notes") or [])
        except (OSError, ValueError, AttributeError):
            pass

path = os.path.abspath(os.path.expanduser(E("_NL_LOG", "")))
folder = os.path.dirname(path)
if not os.path.isdir(folder):
    os.makedirs(folder, mode=0o700)
    os.chmod(folder, 0o700)
line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
lock = os.open(os.path.join(folder, "." + os.path.basename(path) + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
try:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        size = os.stat(path).st_size
    except OSError:
        size = 0
    #  A line torn by an earlier short write (a full disk) or a crash is ended first, before any rotation, so it is
    #  whole in the copy it rotates into and this line starts on a line of its own:  joined to the torn part, it would
    #  read as one line that is no entry, and never be sent.  The mode is put back first (the owner reads and writes,
    #  as the open below sets it too), so a log left write-only can still be checked;  one this cannot read is left.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        rd = os.open(path, os.O_RDONLY)
        try:
            torn = size > 0 and os.pread(rd, 1, size - 1) != b"\n"
        finally:
            os.close(rd)
    except OSError:
        torn = False
    if size >= ROTATE_AT:
        if torn:
            end = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(end, b"\n")
            finally:
                os.close(end)
            torn = False
        for n in range(KEEP, 0, -1):
            older = path + "." + str(n - 1) if n > 1 else path
            if os.path.exists(older):
                os.replace(older, path + "." + str(n))
    if torn:
        line = b"\n" + line
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:  # a short write is finished, not left torn
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
finally:
    os.close(lock)
' </dev/null >/dev/null 2>&1 || true
}

#  The one EXIT trap:  the log line first (it reads the temp files), then the temp files go.  It leaves with the
#  code the script was leaving with, whatever the log did.
_on_exit() {
  local rc=$?
  set +eu
  _action_log_line "$rc" </dev/null >/dev/null 2>&1
  if [[ "${_TMP_MADE:-0}" == "1" ]]; then
    rm -f "$TMP" "$TITLE_FILE" 2>/dev/null
  fi
  [[ -z "${_LOG_CHANGES_FILE:-}" ]] || rm -f "$_LOG_CHANGES_FILE" 2>/dev/null
  [[ -z "${_LOG_LINKS_FILE:-}" ]] || rm -f "$_LOG_LINKS_FILE" 2>/dev/null
  [[ -z "${_NOTES_OUT:-}" ]] || rm -f "$_NOTES_OUT" 2>/dev/null
  exit "$rc"
}

# Robust argument parsing supporting flags in any order
while [[ $# -gt 0 ]]; do
  case "${1:-}" in
    --pin-only)
      MODE=pin
      shift || true
      ;;
    --unpin-only)
      MODE=unpin
      shift || true
      ;;
    --update)
      MODE=update
      shift || true
      ;;
    --read)
      MODE=read
      shift || true
      ;;
    --list)
      MODE=list
      shift || true
      ;;
    --existing-only)
      EXISTING_ONLY=1
      shift || true
      ;;
    --query)
      shift || true
      LIST_QUERY="${1:-}"
      shift || true
      ;;
    --limit)
      shift || true
      LIST_LIMIT="${1:-}"
      _LIST_LIMIT_SET=1
      shift || true
      ;;
    --pin)
      # Accepted for old callers;  pinning is already the default and there is no GUI path to switch on.
      shift || true
      ;;
    --activate|--front)
      WANT_ACTIVATE=1
      shift || true
      ;;
    --notify|--pushover)
      WANT_NOTIFY=1
      shift || true
      ;;
    --needs-owner|--action-required)
      NEEDS_OWNER=1
      shift || true
      ;;
    --summary)
      shift || true
      SUMMARY_TEXT="${1:-}"
      shift || true
      ;;
    --pr)
      shift || true
      PR_NUMBERS="${1:-}"
      shift || true
      ;;
    --html)
      shift || true
      HTML_PATH="${1:-}"
      shift || true
      ;;
    --title)
      shift || true
      TITLE="${1:-}"
      shift || true
      ;;
    --session)
      shift || true
      SESSION_ARG="${1:-}"
      shift || true
      ;;
    --)
      shift || true
      break
      ;;
    -*)
      echo "unknown flag: $1" >&2
      echo "usage: $0 [--update [--existing-only]] [--pin-only|--unpin-only|--pin|--activate|--notify|--needs-owner|--summary text|--pr \"18\"|--html path|--session id] \"Title\" [body]  |  $0 --read [--title] \"Title\"  |  $0 --list [--query text] [--limit 1-500]" >&2
      exit 2
      ;;
    *)
      if [[ -z "$TITLE" ]]; then
        TITLE="$1"
        shift || true
      else
        break
      fi
      ;;
  esac
done

# From here on every exit goes through the trap:  one action-log line, then the temp files go.
trap _on_exit EXIT

if [[ "$EXISTING_ONLY" == "1" && "$MODE" != "update" ]]; then
  echo "--existing-only goes with --update:  it makes an update of a missing note exit 3 instead of creating it" >&2
  exit 2
fi
if [[ "$MODE" != "list" ]] && [[ -n "$LIST_QUERY" || "$_LIST_LIMIT_SET" == "1" ]]; then
  echo "--query and --limit go with --list" >&2
  exit 2
fi
if [[ "$MODE" == "list" ]]; then
  _limit_re='^[1-9][0-9]{0,2}$'
  if [[ "$_LIST_LIMIT_SET" != "1" ]]; then
    LIST_LIMIT=100
  elif [[ ! "$LIST_LIMIT" =~ $_limit_re ]] || (( LIST_LIMIT > 500 )); then
    echo "--limit takes a whole number from 1 to 500" >&2
    exit 2
  fi
  if [[ -n "$TITLE" || $# -gt 0 ]]; then
    echo "--list takes no title:  --query keeps the titles that contain some text" >&2
    exit 2
  fi
fi
if [[ "$MODE" == "read" && $# -gt 0 ]]; then
  echo "--read takes the title and nothing else" >&2
  exit 2
fi

if [[ -z "$TITLE" && "$MODE" != "list" ]]; then
  echo "usage: $0 [--update [--existing-only]] [--pin-only|--unpin-only|--pin|--activate|--notify|--needs-owner|--summary text|--pr \"18\"|--html path|--session id] \"Title\" [body]  |  $0 --read [--title] \"Title\"  |  $0 --list [--query text] [--limit 1-500]" >&2
  exit 2
fi

# Strip accidental backslash escaping on title brackets e.g. \[TEAM_KEY, Agent\]
TITLE=$(printf '%s' "$TITLE" | sed -e 's/^\\\[/[/' -e 's/\\\]$/]/' -e 's/\\\[/[/g' -e 's/\\\]/]/g')

# Secret scan on the title (AFC-422).  The title reaches AppleScript source, the relay envelope, the
# owner notification and two stderr lines, so a credential pasted into it would leak four ways.  Reject
# it here, before any of those, and never echo the value back: a rejection that printed the title would
# put the credential in the very log it is protecting.
_title_looks_secret() {
  printf '%s' "$1" | grep -Eqi -e \
    '-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|(api[_-]?key|secret|password|passwd|token)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9_./+-]{12,}'
}
if _title_looks_secret "$TITLE"; then
  _TITLE_REJECTED=1
  echo "apple-notes-coding: field 'title' rejected: it looks like it holds a credential.  The value is not echoed." >&2
  exit 2
fi


# ---- Session id (AFC-361) ---------------------------------------------------------------------------------------
# Data copy of fleet-apps.json `sessionIds`;  scripts/test_session_ids_parity.py keeps it equal.
SESSION_ID_PATTERN='^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$'
SESSION_GENERIC_ENV="AGENT_SESSION"
SESSION_FALLBACK_ENV="CLAUDE_CODE_SESSION_ID CODEX_THREAD_ID CURSOR_CONVERSATION_ID CURSOR_AGENT_CHAT_ID GROK_SESSION_ID OPENCODE_SESSION_ID __MAVIS_RUNTIME_SESSION_ID"

_session_seat_token() {  # $1 = a seat or Notes name -> the canonical seat tag, upper case ("" when nothing usable)
  local t
  t="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | sed -E 's/^[^A-Z0-9]+//; s/[^A-Z0-9]+$//; s/[^A-Z0-9]+/-/g')"
  case "$t" in
    ROB|ROB-MUSE|MUSE-ASSIST) t=MA ;;
    INSTINCT|WISE-INSTINCT|ECHO-INSTINCT) t=WISE ;;
    GROK-BUILD) t=GROK ;;
    MINIMAX) t=MM ;;
  esac
  printf '%s' "$t"
}

_session_seat() {  # the resolved seat:  launcher's seat, else AGENT_SEAT, else the Agent in the title
  local raw="" agent
  if [[ -n "${AGENT_LAUNCHER:-}" && -n "${AGENT_LAUNCH_SEAT:-}" ]]; then
    raw="$AGENT_LAUNCH_SEAT"
  elif [[ -n "${AGENT_SEAT:-}" ]]; then
    raw="$AGENT_SEAT"
  else
    agent="$(printf '%s' "$TITLE" | sed -nE 's/^\[[^]]*, ?([^],]+)\].*$/\1/p')"
    case "$(printf '%s' "$agent" | tr '[:lower:]' '[:upper:]')" in
      JET|JET\ *) raw=JET ;;
      GROK\ BOT*|GROK-BOT*) raw=GROK-BOT ;;
      *) raw="$agent" ;;
    esac
  fi
  _session_seat_token "$raw"
}

_session_exempt() {  # $1 = seat tag:  one thread with the owner, so no session id to pass
  case "$1" in ECHO|GROK-BOT|JET|JET-CHAT|MA|WISE|GB-*) return 0 ;; esac
  return 1
}

_session_envs() {  # $1 = seat tag -> the platform variables to try, in order
  case "$1" in
    CLAUDE) echo CLAUDE_CODE_SESSION_ID ;;
    CODEX) echo CODEX_THREAD_ID ;;
    CURSOR) echo CURSOR_CONVERSATION_ID CURSOR_AGENT_CHAT_ID ;;
    GROK) echo GROK_SESSION_ID ;;
    MM) echo __MAVIS_RUNTIME_SESSION_ID ;;
    OPENCODE) echo OPENCODE_SESSION_ID ;;
    *) echo "$SESSION_FALLBACK_ENV" ;;
  esac
}

_session_own_var() {  # $1 = seat tag -> its own variable, for the refusal text
  case "$1" in
    CLAUDE) echo CLAUDE_CODE_SESSION_ID ;; CODEX) echo CODEX_THREAD_ID ;; CURSOR) echo CURSOR_CONVERSATION_ID ;;
    GROK) echo GROK_SESSION_ID ;; MM) echo __MAVIS_RUNTIME_SESSION_ID ;; OPENCODE) echo OPENCODE_SESSION_ID ;;
  esac
}

NOTE_SESSION=""
if [[ "$MODE" == "create" || "$MODE" == "update" ]]; then
  _seat="$(_session_seat)"
  if [[ -n "$SESSION_ARG" ]]; then
    NOTE_SESSION="$SESSION_ARG"
  elif [[ -n "${!SESSION_GENERIC_ENV:-}" ]]; then
    NOTE_SESSION="${!SESSION_GENERIC_ENV}"
  else
    for _var in $(_session_envs "$_seat"); do
      if [[ -n "${!_var:-}" ]]; then NOTE_SESSION="${!_var}"; break; fi
    done
  fi
  if [[ -n "$NOTE_SESSION" ]]; then
    if ! printf '%s' "$NOTE_SESSION" | grep -Eq "$SESSION_ID_PATTERN"; then
      echo "session id is not valid:  use 4 to 128 letters, digits, . _ : - starting with a letter or digit" >&2
      exit 2
    fi
    if printf '%s' "$NOTE_SESSION" | grep -Eiq '^session[0-9]*$'; then
      echo "session id is not valid:  \"$NOTE_SESSION\" is a chat tag (the [SEAT·session] label), not your platform's session id.  Pass the id your platform sets (${_seat:-your seat}:  $(_session_own_var "$_seat"))." >&2
      exit 2
    fi
  elif ! _session_exempt "$_seat"; then
    _own="$(_session_own_var "$_seat")"
    echo "no session id.  Notes record the session they come from, so a headless resume can find this seat's session for the topic.  Pass --session <id>, or export AGENT_SESSION=<id>${_own:+ ($_seat sets $_own;  it is empty here)}.  Grok bots, MA, JET, WISE and ECHO have one thread with the owner and need none.  (seat: ${_seat:-unknown})" >&2
    exit 2
  fi
fi

# ---- Read and list (AFC-486) -----------------------------------------------------------------------------------
# One JXA program serves both.  The heredoc is quoted, so nothing in it expands, and the title reaches it through
# the environment (APPLE_NOTES_TITLE), never through its source.  The lookup is the one the update block's
# `note noteTitle of codingFolder` makes, by exact name in the iCloud Coding folder.  Nothing here shows or
# activates Notes.  A list reads each property of the whole folder in one AppleEvent (246 notes take about 70 ms),
# and reads the ids again at the end:  a note written while the folder is read can reorder it, and then the four
# lists no longer line up, so a changed folder is read again (three tries).  The output is plain ASCII, so it does
# not hang on the encoding osascript writes in.
_NOTES_NOT_FOUND="__APPLE_NOTES_NOT_FOUND__"
_notes_jxa() {  # $1 = read or list, $2 = the title (read)
  APPLE_NOTES_JXA_MODE="$1" APPLE_NOTES_TITLE="${2:-}" osascript -l JavaScript <<'JXA'
ObjC.import('Foundation');

function env(name) {
  var value = ObjC.unwrap($.NSProcessInfo.processInfo.environment.objectForKey(name));
  return (value === undefined || value === null) ? '' : String(value);
}

function ascii(text) {
  return text.replace(/[^ -~]/g, function (c) {
    return '\\u' + ('000' + c.charCodeAt(0).toString(16)).slice(-4);
  });
}

function iso(when) {
  return when ? when.toISOString() : null;
}

function run() {
  var notFound = '__APPLE_NOTES_NOT_FOUND__';
  var folder = Application('Notes').accounts.byName('iCloud').folders.byName('Coding');
  if (env('APPLE_NOTES_JXA_MODE') === 'list') {
    if (!folder.exists()) {
      return '{"names":[],"ids":[],"created":[],"modified":[]}';
    }
    var notes = folder.notes;
    for (var attempt = 0; attempt < 3; attempt++) {
      var ids = notes.id();
      var names = notes.name();
      var created = notes.creationDate();
      var modified = notes.modificationDate();
      if (notes.id().join('\n') === ids.join('\n')) {
        return ascii(JSON.stringify({
          names: names, ids: ids, created: created.map(iso), modified: modified.map(iso)
        }));
      }
    }
    throw new Error('the Coding folder kept changing while it was read');
  }
  if (!folder.exists()) {
    return notFound;
  }
  var note = folder.notes.byName(env('APPLE_NOTES_TITLE'));
  if (!note.exists()) {
    return notFound;
  }
  return ascii(JSON.stringify({
    title: note.name(),
    id: note.id(),
    created: iso(note.creationDate()),
    modified: iso(note.modificationDate()),
    body: note.body(),
    plaintext: note.plaintext()
  }));
}
JXA
}

if [[ "$MODE" == "read" ]]; then
  _NOTES_OUT=$(mktemp /tmp/apple-note-read.XXXXXX)
  _jxa_rc=0
  _notes_jxa read "$TITLE" >"$_NOTES_OUT" || _jxa_rc=$?
  if [[ "$_jxa_rc" != "0" ]]; then
    echo "notes: the read did not complete (osascript exit $_jxa_rc)" >&2
    exit 1
  fi
  if grep -qxF "$_NOTES_NOT_FOUND" "$_NOTES_OUT"; then
    echo "no note with that title in the Coding folder" >&2
    exit 3
  fi
  cat "$_NOTES_OUT"
  exit 0
fi

if [[ "$MODE" == "list" ]]; then
  _NOTES_OUT=$(mktemp /tmp/apple-note-list.XXXXXX)
  _jxa_rc=0
  _notes_jxa list >"$_NOTES_OUT" || _jxa_rc=$?
  if [[ "$_jxa_rc" != "0" ]]; then
    echo "notes: the list did not complete (osascript exit $_jxa_rc)" >&2
    exit 1
  fi
  #  The query, the sort and the limit, in place in the same file (the action log counts the rows from it).
  _list_rc=0
  APPLE_NOTES_QUERY="$LIST_QUERY" APPLE_NOTES_LIMIT="$LIST_LIMIT" /usr/bin/python3 -I -c '
import json, os, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    raw = json.load(fh)
columns = [raw.get(key) or [] for key in ("names", "ids", "created", "modified")]
if len(set(len(column) for column in columns)) != 1:
    sys.exit("notes: the Coding folder changed while it was read;  run the list again")
query = os.environ.get("APPLE_NOTES_QUERY", "").casefold()
limit = int(os.environ.get("APPLE_NOTES_LIMIT") or 100)
rows = [
    {"title": title or "", "id": note_id or "", "created": created, "modified": modified}
    for title, note_id, created, modified in zip(*columns)
    if not query or query in (title or "").casefold()
]
rows.sort(key=lambda row: row["modified"] or "", reverse=True)
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"notes": rows[:limit], "total": len(rows), "truncated": len(rows) > limit}, fh)
    fh.write("\n")
' "$_NOTES_OUT" || _list_rc=$?
  if [[ "$_list_rc" != "0" ]]; then
    echo "notes: the list could not be read back (exit $_list_rc)" >&2
    exit 1
  fi
  cat "$_NOTES_OUT"
  exit 0
fi

if [[ "$MODE" == "pin" || "$MODE" == "unpin" ]]; then
  NOTE_ID=$(osascript -e "tell application \"Notes\" to get id of note \"$TITLE\" of folder \"Coding\" of account \"iCloud\"" 2>/dev/null || true)
  [[ -n "$NOTE_ID" ]] || { echo "$MODE-only: no note with that title in Coding" >&2; exit 3; }
  SKIP_BODY=1
else
  SKIP_BODY=0
fi

# Run a headless pin/unpin Shortcut with the note title as a text-file input.
# Returns 0 on success, 2 when the Shortcut is not installed, 1 when the run
# itself failed, 3 when the Shortcut list could not be read, 5 when the Shortcuts
# app could not be started.  A failed list
# lookup is its own case:  it is not proof that the Shortcut is missing, and
# sending an operator to recreate a Shortcut that exists is worse than saying
# the list did not read.  First-ever run of each shortcut shows a one-time
# "Allow ... to share with Notes?" dialog — choose Always Allow.
#
# Whether a failed pin or unpin Shortcut is retried is the APPLE_NOTES_PIN_RETRY fleet knob (Infisical,
# through scripts/fleet_knobs), resolved once per process through the package next to this script
# (a source checkout) or the managed stable copy at ~/apps/needs-owner (the live helper).  APPLE_NOTES_PIN_RETRY
# in the environment is only a narrowing override:  it can turn the retry off, never on.  With no fleet_knobs
# on this host, or an unreachable Infisical with no last-known-good copy, the registered default (on) applies,
# narrowed by that override.
_PIN_RETRY=""
_pin_retry_on() {
  #  A local value may only narrow, so it is read first:  an operator who sets it to off gets a
  #  decision without a knob lookup that can reach the network, and the lookup cannot widen it later.
  case "${APPLE_NOTES_PIN_RETRY:-}" in
    false|0|no|off) return 1 ;;
  esac
  if [[ -z "${_PIN_RETRY:-}" ]]; then
    local knob="" here candidate
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    for candidate in "$here" "$HOME/apps/needs-owner" "${AGENT_SYNC_RUNTIME:-$HOME/apps/lanes/_managed/fleet/agent-sync-runtime}/scripts"; do
      if [[ -n "$candidate" && -d "$candidate/fleet_knobs" ]] && command -v python3 >/dev/null 2>&1; then
        knob="$(cd "$candidate" && python3 -m fleet_knobs get APPLE_NOTES_PIN_RETRY 2>/dev/null)" || knob=""
        [[ -n "$knob" ]] && break
      fi
    done
    case "$knob" in
      false|0|no|off) _PIN_RETRY=0 ;;
      *) _PIN_RETRY=1 ;;
    esac
  fi
  [[ "$_PIN_RETRY" == "1" ]]
}

# The Shortcuts app has to be running before the shortcut runs.  Its "Find
# Notes" action is an app intent served by the Shortcuts app itself, and when
# that app is not running the intent cannot be dispatched: the shortcut fails
# with "The action 'Find Notes' could not run because an unknown error
# occurred", for every title, including one that does not exist.  Opening the
# app in the background costs nothing and does not steal focus.
_ensure_shortcuts_running() {
  pgrep -x Shortcuts >/dev/null 2>&1 && return 0
  open -g -a Shortcuts >/dev/null 2>&1 || return 1
  local waited=0
  while (( waited < 20 )); do
    pgrep -x Shortcuts >/dev/null 2>&1 && return 0
    sleep 0.25
    (( waited++ ))
  done
  return 1
}

# Run a Shortcuts command under a bound.  macOS has no GNU "timeout",  so the bound is perl's
# alarm,  the same idiom the runtime sync script uses for its git call.  The bound cannot rest on
# perl alone:  an unbounded run can outlast the caller's cap,  and the note is then written while
# the caller is told the outcome is unknown and refuses the retry (AFC-436).  python3 sets the same
# alarm and this script needs it for the HTML conversion anyway,  so it is the second way to bound
# the run;  without either one the run is refused rather than left unbounded.
_run_shortcuts_bounded() {  # $@ = the shortcuts command
  if command -v perl >/dev/null 2>&1; then
    perl -e 'alarm shift; exec @ARGV' "$NOTES_SHORTCUT_TIMEOUT_S" "$@"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import os,signal,sys; signal.alarm(int(sys.argv[1])); os.execvp(sys.argv[2], sys.argv[2:])' "$NOTES_SHORTCUT_TIMEOUT_S" "$@"
  else
    echo "notes: the Shortcut run cannot be bounded (no perl and no python3 on this host), so it was not attempted" >&2
    return 124
  fi
}

# Is process $1 running?  kill -0 fails both for a process that is gone and, with EPERM, for a live process
# owned by another user.  Only the first means the holder is dead, so the failure text decides:  "not
# permitted" is a live process, and a lock held by it is not taken over.
_pin_pid_alive() {
  local out
  out="$(LC_ALL=C kill -0 "$1" 2>&1)" && return 0
  [[ "$out" == *"not permitted"* ]]
}

# A lock is stale when its holder is gone, or when it is older than any live run could be.  The age is
# derived from NOTES_SHORTCUT_TIMEOUT_S, because a raised bound makes a live run last longer:  one bounded
# list lookup, one bounded run, a bounded retry and its 3 s pause, plus the 5 s app start and a minute of
# slack, rounded up to whole minutes for find -mmin.  The default 30 s gives 3 minutes.
_pin_lock_stale() {
  local holder stale_min
  holder="$(cat "$NOTES_PIN_LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$holder" =~ ^[0-9]+$ ]] && ! _pin_pid_alive "$holder"; then
    return 0
  fi
  stale_min=$(( (3 * NOTES_SHORTCUT_TIMEOUT_S + 3 + 5 + 60) / 60 + 1 ))
  (( stale_min >= 3 )) || stale_min=3
  [[ -n "$(find "$NOTES_PIN_LOCK_DIR" -maxdepth 0 -mmin "+$stale_min" 2>/dev/null)" ]]
}

# Take the pin lock.  Returns 0 when held, 1 when another run held it for the whole NOTES_PIN_LOCK_WAIT_S
# wait, 2 when the lock directory could not be made, 3 when it kept coming back stale.  A stale lock is
# moved aside with mv, which is atomic, before it is removed.  The check that calls a lock stale and the mv
# that replaces it are two steps, so two waiters that find the same stale lock at the same instant can
# still step on each other:  that needs a crashed holder and two waiters inside the same few
# milliseconds, and the worst it costs is two overlapping runs, which is what happened every time before
# this lock existed.  A second lock to guard the first would itself need a stale-lock rule, so it is not
# added.  A reused pid can likewise keep a dead holder's lock alive, but only until the age rule above
# takes it over.
_pin_lock_acquire() {
  local waited=0 takeovers=0
  mkdir -p -m 700 "$(dirname "$NOTES_PIN_LOCK_DIR")" 2>/dev/null || return 2
  until mkdir -m 700 "$NOTES_PIN_LOCK_DIR" 2>/dev/null; do
    #  Only a lock directory that exists is contention.  When mkdir failed and nothing is there, the cause is
    #  the filesystem (no permission, read-only, a vanished parent), and waiting would stall the full
    #  NOTES_PIN_LOCK_WAIT_S and then blame another run.  The holder may also have released the lock between
    #  the failed mkdir and this check, so one more try settles it before the directory is called unmakeable.
    if [[ ! -d "$NOTES_PIN_LOCK_DIR" ]]; then
      mkdir -m 700 "$NOTES_PIN_LOCK_DIR" 2>/dev/null && break
      [[ -d "$NOTES_PIN_LOCK_DIR" ]] || return 2
    fi
    if _pin_lock_stale && mv "$NOTES_PIN_LOCK_DIR" "$NOTES_PIN_LOCK_DIR.stale.$$" 2>/dev/null; then
      rm -rf "$NOTES_PIN_LOCK_DIR.stale.$$"
      #  A takeover takes no time, so it does not move the wait counter,  and the counter alone would
      #  never stop a lock that keeps coming back stale.  Takeovers have their own small cap:  a fourth
      #  one in a single call means something is recreating dead locks,  and the pin is skipped.
      takeovers=$((takeovers + 1))
      if (( takeovers > 3 )); then
        return 3
      fi
      continue
    fi
    if (( waited >= NOTES_PIN_LOCK_WAIT_S )); then
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  printf '%s\n' "$$" > "$NOTES_PIN_LOCK_DIR/pid" 2>/dev/null || true
  _PIN_LOCK_HELD=1
  return 0
}

# Give the lock back, but only if this process still holds it:  a run that outlived the stale age may
# have had the lock taken over, and removing the new holder's lock would reopen the overlap.
_pin_lock_release() {
  [[ "$_PIN_LOCK_HELD" == "1" ]] || return 0
  if [[ "$(cat "$NOTES_PIN_LOCK_DIR/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$NOTES_PIN_LOCK_DIR" 2>/dev/null || true
  fi
  _PIN_LOCK_HELD=0
  return 0
}

_run_note_shortcut_locked() {  # $1 = shortcut name;  the caller holds the pin lock
  local listing
  if ! listing="$(_run_shortcuts_bounded shortcuts list 2>/dev/null)"; then
    return 3
  fi
  printf '%s\n' "$listing" | grep -qxF "$1" || return 2
  #  The Shortcuts app must be up before the run:  its Find Notes action is an app intent
  #  served by that app,  and a closed app fails the run for every title (AFC-436).
  #  Code 5:  it could not be started,  which is a different fault from a failed run and needs
  #  its own message (AFC-431)  —  reporting it as "the run failed" hides the action that fixes it.
  _ensure_shortcuts_running || return 5
  local tmp attempt stripped run_rc
  tmp="$(mktemp).txt"
  printf '%s' "$TITLE" > "$tmp"
  #  A note whose user-given title starts with a non-alphanumeric run (an
  #  emoji pin glyph is the real case observed 2026-10-20, e.g. a title whose
  #  first char is U+2B50 U+FE0F instead of an alphanumeric) is the same
  #  note once that run is dropped, because the Find Notes action matches
  #  by *contains* against the note's name and the drop is a substring of
  #  the original.  Coding-note titles in this helper come out as
  #  `[TEAM_KEY, Agent] Topic`, so the bracketed prefix is its own pattern;
  #  matching Apple Notes' contains-match against the NOTE'S NAME succeeds
  #  when the retry passes the bare topic, which is the bracketed prefix
  #  and any leading non-alnum run stripped away.  Strip is a sed one-liner
  #  so it works on bash 3.2 (macOS) and POSIX sed alike;  it is no-op when
  #  nothing matches, so a retry only rewrites $tmp when there is something
  #  meaningful to fall back to.  An empty strip means the title was
  #  all-non-alnum, and that is its own failure mode that the caller
  #  surfaces through the regular `shortcut run failed` path rather than a
  #  third attempt that has nothing to pass.
  stripped="$(printf '%s' "$TITLE" | sed -E '
    s/^\[[^]]*\][[:space:]]+//
    s/^[^[:alnum:]]+//
  ')"
  # A just-written note may not be visible to the Shortcut's Find step yet, and
  # that miss fails the run (observed 2026-10-20).  Retry once before giving up,
  # so a timing race does not leave a coding note unpinned.  A miss can also pop
  # a note picker on the owner's screen, so the retry is gated by the
  # APPLE_NOTES_PIN_RETRY knob:  set it to false, or APPLE_NOTES_PIN_RETRY=0 in the
  # environment, for a single attempt, which is what a caller wants when it would
  # rather not risk a second Shortcut invocation.  The retry runs 3 s later than
  # the first attempt, so the note index has had more time to catch up and the
  # retry is the less likely of the two to miss it.
  attempt=1
  while :; do
    run_rc=0
    _run_shortcuts_bounded shortcuts run "$1" -i "$tmp" >/dev/null 2>&1 || run_rc=$?
    if [[ "$run_rc" == "0" ]]; then
      rm -f "$tmp"
      return 0
    fi
    #  A run that hit its time bound (killed by the alarm:  142, or 124 when it could not be bounded at
    #  all) is a hang, not an index race:  the Shortcut is waiting on a note picker on the owner's screen.
    #  A second run only opens a second picker, so it is never repeated.  Only a run that failed fast
    #  (the race the retry exists for) is tried again.
    if [[ "$run_rc" == "142" || "$run_rc" == "124" ]]; then break; fi
    if [[ "$attempt" -ge 2 ]] || ! _pin_retry_on; then break; fi
    attempt=2
    sleep 3
    #  On retry, fall back to the stripped-prefix form so a note whose title
    #  begins with an emoji or other non-alphanumeric run can still be found.
    #  Skip the rewrite when stripping produced an empty string or the same
    #  string (no leading run was present), so a title with no prefix keeps
    #  the same input on both attempts and the retry does not silently change
    #  what the Shortcut sees.
    if [[ -n "$stripped" && "$stripped" != "$TITLE" ]]; then
      printf '%s' "$stripped" > "$tmp"
    fi
  done
  rm -f "$tmp"
  return 1
}

# Run a note Shortcut under the pin lock.  Same return codes as above, plus the reasons the lock was not
# taken, so the run was skipped rather than stacked behind another:  6 another pin or unpin run held it
# for the whole wait, 7 its directory could not be made, 8 it kept coming back stale.
_run_note_shortcut() {  # $1 = shortcut name
  local rc=0 lock_rc=0
  _pin_lock_acquire || lock_rc=$?
  case "$lock_rc" in
    0) ;;
    2) return 7 ;;
    3) return 8 ;;
    *) return 6 ;;
  esac
  _run_note_shortcut_locked "$1" || rc=$?
  _pin_lock_release
  return "$rc"
}

if [[ "$MODE" == "unpin" ]]; then
  # Capture the code once:  $? read after a failed [[ ]] is the [[ ]]'s own 1,
  # so an elif chain on $? can never reach the 3 (list unreadable) arm (AFC-431).
  _unpin_rc=0
  _run_note_shortcut "Unpin Coding Note" || _unpin_rc=$?
  if [[ "$_unpin_rc" == "0" ]]; then
    echo "unpinned: yes (headless via 'Unpin Coding Note' shortcut)"
    exit 0
  elif [[ "$_unpin_rc" == "2" ]]; then
    echo "unpinned: no — 'Unpin Coding Note' shortcut not found. Create it: duplicate 'Pin Coding Note', rename, switch the last action's Add to Remove." >&2
    exit 4
  elif [[ "$_unpin_rc" == "3" ]]; then
    echo "unpinned: no — the Shortcut list could not be read, so it is unknown whether 'Unpin Coding Note' exists" >&2
    exit 4
  elif [[ "$_unpin_rc" == "5" ]]; then
    echo "unpinned: no — the Shortcuts app could not be started in the background, so the Shortcut could not run; open Shortcuts once and retry" >&2
    exit 4
  elif [[ "$_unpin_rc" == "6" ]]; then
    echo "unpinned: no — another pin or unpin run held the lock for ${NOTES_PIN_LOCK_WAIT_S}s, so this one was skipped; retry in a moment" >&2
    exit 4
  elif [[ "$_unpin_rc" == "7" ]]; then
    echo "unpinned: no — the pin lock directory ${NOTES_PIN_LOCK_DIR} could not be made, so the Shortcut was not run" >&2
    exit 4
  elif [[ "$_unpin_rc" == "8" ]]; then
    echo "unpinned: no — the pin lock kept coming back stale (more than 3 takeovers in one call), so the Shortcut was not run; remove ${NOTES_PIN_LOCK_DIR} if no other pin is running" >&2
    exit 4
  else
    echo "unpinned: no — 'Unpin Coding Note' shortcut run failed" >&2
    exit 4
  fi
fi

# Markdown & HTML converter for Notes.app.
# Notes does not render MD natively.
# Fixes:
# - Auto-detects if input is already HTML (prevents double-escaping raw HTML tags).
# - Strips invalid <div><br></div> spacers inside <ul>/<ol> (eliminates ghost bullets and number skips).
# - Recovers newlines from unescaped literal \n or single-line collapsed blocks.
# - Formats code blocks with SF Mono font and light styling.
# - Formats tables with clean border-collapse.
_convert_body_to_html() {
  local _md_py _md_dir _rc
  # A private temp DIRECTORY with the converter inside it.  macOS mktemp does not substitute an X
  # template that does not end the name, so `mktemp /tmp/apple-notes-md.XXXXXX.py` returns a FIXED
  # path:  two seats writing at once collide, and a stale file (a crash, or a cleanup the runtime's
  # recoverable-deletion wrapper refused) makes every LATER call fail with "File exists" and an
  # empty interpreter path.  A directory whose name ends in X's is substituted, so the file inside
  # it is unique per call and a stale one can never break the next call.
  if ! _md_dir=$(mktemp -d /tmp/apple-notes-md.XXXXXX); then
    echo "notes: cannot create a temp dir for the markdown converter" >&2
    return 1
  fi
  _md_py="${_md_dir}/convert.py"
  _rc=0
  cat >"${_md_py}" <<'PY'
import html, os, re, sys

SPACER = "<div><br></div>"

def is_html_content(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if re.match(r"^<(?:!DOCTYPE|!--|html|div|p|h[1-6]|ul|ol|table|section|article)\b", s, re.I):
        return True
    block_tags = ["div", "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "table", "pre"]
    open_c = sum(len(re.findall(rf"<{t}\b[^>]*>", s, re.I)) for t in block_tags)
    close_c = sum(len(re.findall(rf"</{t}>", s, re.I)) for t in block_tags)
    return open_c >= 1 and close_c >= 1

def clean_existing_html(text: str) -> str:
    s = text.strip()
    # Remove invalid spacers inside lists that turn into ghost bullets
    s = re.sub(r"(<[uo]l\b[^>]*>)\s*(?:<div><br\s*/?></div>|<br\s*/?>)+", r"\1", s, flags=re.I)
    s = re.sub(r"(?:<div><br\s*/?></div>|<br\s*/?>)+\s*(</[uo]l>)", r"\1", s, flags=re.I)
    s = re.sub(r"(</li>)\s*(?:<div><br\s*/?></div>|<br\s*/?>)+\s*(<li\b)", r"\1\2", s, flags=re.I)

    # Parse stray markdown bold/code/links in text nodes outside tags
    def fix_stray_md(match):
        chunk = match.group(0)
        chunk = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", chunk)
        chunk = re.sub(r"`([^`]+)`", r"<code style=\"background-color: #f0f2f5; padding: 2px 5px; border-radius: 4px; font-family: ui-monospace, Menlo, Monaco, Courier, monospace; font-size: 12px;\">\1</code>", chunk)
        chunk = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
            chunk,
        )
        return chunk
    s = re.sub(r">([^<]+)<", fix_stray_md, s)
    if not s.startswith("<div>"):
        s = "<div>" + s + "</div>"
    return s

def inline(s: str) -> str:
    s = html.escape(s)
    # A code span is content the author wants verbatim, so the sentence-gap conversion must
    # not touch it.  This pass runs BEFORE the backticks become code chips, so a code span
    # holding a sentence end used to get the entity inside the chip.  Hold the spans aside,
    # convert the prose around them, and put them back byte for byte.  (The whole-HTML pass
    # skips <code> regions the same way.)
    code_spans = []
    def hold_code_span(m):
        code_spans.append(m.group(0))
        return "\x00code" + str(len(code_spans) - 1) + "\x00"
    s = re.sub(r"`[^`]+`", hold_code_span, s)
    # Notes.app is an HTML renderer — two ASCII spaces collapse.
    # Sentence gap in HTML is the numeric reference `&#160; ` + space, the same U+00A0 plus a
    # normal space as every other surface.
    # Owner ruling 2026-10-23:  never the named `&nbsp;` entity.  The numeric reference adds no
    # raw non-ASCII byte to the note HTML.
    # Oct 29, 2026, measured through this writer:  Apple Notes stores the no-break space as an
    # ordinary space on import, so the note holds two ordinary spaces after a sentence end, and
    # Notes.app shows two ordinary spaces as a wide gap (the owner's screenshot of the gap
    # sample note).  A one-space gap is the thing that reads as no gap.  The AFC-552 premise that
    # the renderer collapses a run of ordinary spaces was never checked on screen.
    s = re.sub(r"([.!?]) {2,}", r"\1&#160; ", s)
    s = re.sub(r"\x00code(\d+)\x00", lambda m: code_spans[int(m.group(1))], s)
    # links [text](url) — after escape brackets still match
    s = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        s,
    )
    s = re.sub(r"`([^`]+)`", r"<code style=\"background-color: #f0f2f5; padding: 2px 5px; border-radius: 4px; font-family: ui-monospace, Menlo, Monaco, Courier, monospace; font-size: 12px;\">\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
    # Underscore italics only at word edges.
    s = re.sub(r"(?<![A-Za-z0-9_])_([^_]+)_(?![A-Za-z0-9_])", r"<i>\1</i>", s)
    return s

raw_input = sys.stdin.read()
is_explicit_html = os.environ.get("IS_EXPLICIT_HTML") == "1"

if is_explicit_html or is_html_content(raw_input):
    sys.stdout.write(clean_existing_html(raw_input))
    sys.exit(0)

text = raw_input

# Check if literal \n exists without actual newlines
if "\n" not in text and r"\n" in text:
    text = text.replace(r"\n", "\n")

# Check if collapsed single line with inline headings/bullets
if text.count("\n") < 3:
    text = re.sub(r"(?<=\S)\s+(#{1,4}\s+)", r"\n\n\1", text)
    text = re.sub(r"(?<=\S)\s+([-\*]\s+)", r"\n\1", text)

# normalize newlines
text = text.replace("\r\n", "\n").replace("\r", "\n")
lines = text.split("\n")
out = []
i = 0
in_ul = False
in_ol = False

def last_is_spacer():
    return bool(out) and out[-1] == SPACER

def add_spacer():
    if out and not last_is_spacer():
        out.append(SPACER)

def peek_stripped(j):
    while j < len(lines):
        s = lines[j].strip()
        if s:
            return s
        j += 1
    return ""

def close_lists():
    global in_ul, in_ol
    closed = False
    if in_ul:
        out.append("</ul>")
        in_ul = False
        closed = True
    if in_ol:
        out.append("</ol>")
        in_ol = False
        closed = True
    if closed:
        add_spacer()

while i < len(lines):
    line = lines[i]
    stripped = line.strip()

    # blank → section air
    if stripped == "":
        nxt = peek_stripped(i + 1)
        # If loose list (blank line between items in the same list), keep list open without spacer inside
        if (in_ul and re.match(r"^[-*+]\s+", nxt)) or (in_ol and re.match(r"^\d+[.)]\s+", nxt)):
            i += 1
            continue
        close_lists()
        add_spacer()
        i += 1
        continue

    # fenced code block
    if stripped.startswith("```"):
        close_lists()
        add_spacer()
        i += 1
        code_lines = []
        while i < len(lines) and not lines[i].strip().startswith("```"):
            code_lines.append(lines[i])
            i += 1
        if i < len(lines):
            i += 1  # closing fence
        out.append("<pre style=\"background-color: #f6f8fa; padding: 10px 12px; border-radius: 6px; font-family: ui-monospace, Menlo, Monaco, Courier, monospace; font-size: 12px; border: 1px solid #e1e4e8; overflow-x: auto;\"><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
        add_spacer()
        continue

    # hr
    if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
        close_lists()
        add_spacer()
        out.append("<hr>")
        add_spacer()
        i += 1
        continue

    # headings: map # and ## to h2, ### to h3, #### to h4 (prevents h1 clash with note title)
    m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
    if m:
        close_lists()
        add_spacer()
        h_len = len(m.group(1))
        level = 2 if h_len <= 2 else min(h_len, 4)
        out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
        add_spacer()
        i += 1
        continue

    # unordered list
    m = re.match(r"^[-*+]\s+(.*)$", stripped)
    if m:
        item = m.group(1).strip()
        if not item:
            i += 1
            continue
        if in_ol:
            out.append("</ol>")
            in_ol = False
            add_spacer()
        if not in_ul:
            out.append("<ul>")
            in_ul = True
        out.append(f"<li>{inline(item)}</li>")
        i += 1
        continue

    # ordered list
    m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
    if m:
        if in_ul:
            out.append("</ul>")
            in_ul = False
            add_spacer()
        if not in_ol:
            out.append("<ol>")
            in_ol = True
        out.append(f"<li>{inline(m.group(1))}</li>")
        i += 1
        continue

    # table
    if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|(?:\s*:?-+:?\s*\|)+\s*$", lines[i+1].strip()):
        close_lists()
        table_rows = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            table_rows.append(lines[i].strip())
            i += 1

        def split_cols(row_str):
            parts = row_str.split("|")
            if parts and parts[0] == "":
                parts = parts[1:]
            if parts and parts[-1] == "":
                parts = parts[:-1]
            return [p.strip() for p in parts]

        if len(table_rows) >= 2:
            th_cells = split_cols(table_rows[0])
            t_html = ["<table border=\"1\" cellpadding=\"4\" style=\"border-collapse: collapse; min-width: 100%; border: 1px solid #ccc;\">"]
            t_html.append("<thead><tr style=\"background-color: #f2f2f2;\">")
            for h in th_cells:
                t_html.append(f"<th style=\"border: 1px solid #ccc; padding: 6px 8px; text-align: left;\"><b>{inline(h)}</b></th>")
            t_html.append("</tr></thead><tbody>")
            for r in table_rows[2:]:
                cells = split_cols(r)
                t_html.append("<tr>")
                for c in cells:
                    t_html.append(f"<td style=\"border: 1px solid #ccc; padding: 5px 8px; vertical-align: top;\">{inline(c)}</td>")
                t_html.append("</tr>")
            t_html.append("</tbody></table>")
            out.append("".join(t_html))
            add_spacer()
            continue

    # paragraph (merge consecutive non-blank non-special lines)
    close_lists()
    para = [stripped]
    i += 1
    while i < len(lines):
        s2 = lines[i].strip()
        if s2 == "" or s2.startswith("#") or s2.startswith("```") or s2.startswith("|") or re.match(r"^[-*+]\s+", s2) or re.match(r"^\d+[.)]\s+", s2) or re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s2):
            break
        para.append(s2)
        i += 1
    out.append("<p>" + inline(" ".join(para)) + "</p>")
    add_spacer()

close_lists()
sys.stdout.write("<div>" + "".join(out) + "</div>")
PY
  # `|| _rc=$?` keeps `set -e` (line 46) from aborting here, before the cleanup below:  a converter
  # failure would otherwise skip the cleanup, leak the scratch directory, and surface as a bare
  # command failure instead of the note's own error.
  /usr/bin/python3 "${_md_py}" || _rc=$?
  # The scratch file and its directory go when they can:  a cleanup the runtime refuses must not
  # fail the note, and the next call gets a fresh directory either way.  Its output goes nowhere:
  # this runs inside the body capture, so a word on stdout would land in the note.
  rm -f "${_md_py}" >/dev/null 2>&1 || true
  rmdir "${_md_dir}" >/dev/null 2>&1 || true
  return ${_rc}
}

# Notes.app collapses ASCII double spaces.  Turn leftover `.  ` / `!  ` / `?  `
# into `&#160; ` so sentence gaps survive.  Skip <pre>, <code>, <style>, <script>.
# Owner ruling 2026-10-23:  never the named `&nbsp;` entity.  The numeric reference adds no
# raw non-ASCII byte to the note HTML.
# Notes stores the no-break space as an ordinary space on import and still shows the pair as a
# wide gap, so U+00A0 plus a normal space is the gap here too (measured Oct 29, 2026).
# A sentence that ends INSIDE inline markup puts the closing tag between the punctuation
# and the run of spaces (`Bold end.</b>  Next`), so the run never sits next to the
# punctuation and the gap was lost.  The optional tag group carries the closing tag
# through, so the gap lands after it.  (AFC-496)
_html_sentence_gaps() {
  /usr/bin/python3 -c '
import re, sys
s = sys.stdin.read()
parts = re.split(r"(<pre[\s\S]*?</pre>|<code[\s\S]*?</code>|<style[\s\S]*?</style>|<script[\s\S]*?</script>)", s, flags=re.I)
out = []
for i, part in enumerate(parts):
    if i % 2 == 1:
        out.append(part)
        continue
    out.append(re.sub(r"([.!?])(</[a-zA-Z][^>]*>)? {2,}", r"\1\2&#160; ", part))
sys.stdout.write("".join(out))
'
}

# ---- The linkifier (owner 2026-10-25) --------------------------------------------------------------------------
# A note body carries bare identifiers, so a reader cannot click through to the work they name.  The owner asked for
# links everywhere, Apple Notes included, in the house linkifier style:
#
#   [PR #957](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/957)
#
# The writer does the converting, so every seat gets it without every seat having to write it.  This is the third
# automatic rewrite the helper owns, with the fleet dates and the sentence gaps, and it behaves like them:  it runs on
# the HTML the note actually gets, it never touches code or an existing link, it counts what it changed, it says so on
# stderr, and the count reaches the action log.
#
# What becomes a link:
#
#   * A Linear issue identifier, OPS-1000 or AFC-547, becomes a link to that issue in the fleet workspace.  The team
#     key list is the Linear teams, so an identifier that is not a Linear issue (SRV-S07, MAC-P09) is left alone.
#   * A Linear project reference, P-BF-6, becomes a link to that project.  A project has no URL schema that can be
#     derived from its reference, so every project is coded here by hand, one line each, and a project reference that
#     is not in the table is left as plain text.  A project reference is never treated as an issue.
#   * A pull request reference, PR #957 or PR 957, becomes a link to the pull request, and the text is normalised to
#     the house form, PR #957.  A list of them, PRs #380, #386 and #389, becomes one link per number.
#   * A qualified reference, AFC#957 or BotFleet#1282, names its own repository and becomes a link to it.
#   * A fleet shortcut, fl/fleet-tools or fleetlink/zulip/zulip-fleet-guide.md, becomes a link to that page.  A path
#     never ends on punctuation, so the dot ending a sentence stays outside the link, and a shortcut inside a URL or
#     a longer path (foo/fl/x) is left alone.
#   * A site page, SWU/docs, becomes a link to that page.
#   * A repository reference, GH@user/repo, becomes a link to that repository.
#   * An incident reference, PagerDuty#384, becomes a link to that incident.  It wins over the qualified pull request
#     rule, which would otherwise read the pound sign as a pull request.
#   * A Sentry short id, BOTFLEET-16, becomes a link to that issue.  It is claimed before the issue rule runs, so the
#     number in it can never be read as a Linear issue number.
#   * A Cursor agent id, bc-a1b2c3d4-e5f6, becomes a link to that agent.
#
# Which repository a pull request links to.  The numbers collide across the fleet repositories:  PR #95 exists in a
# dozen of them, so a bare number is resolved from the block it sits in.  A paragraph and a table row are a block
# each, and the block is read in this order:
#
#   1. A repository named right next to the reference:  merged as AFC PR #318 links AI-Fleet-Coordinator.
#   2. The subject of a table row, which is what the row is about:  the com.jay.botfleet-server row links BotFleet.
#   3. The nearest repository named near the reference, within NOTES_LINKIFY_CONTEXT_CHARS characters.
#   4. The one repository the whole block talks about.
#   5. The team key in the note title, so a [ST, Claude] note links Socratic-Trade.
#   6. The fallback repository.
#
# A block that names several repositories, and a reference that names none of them, is left as plain text:  a link to
# the wrong repository is worse than no link.  So a note about one pull request in another repository names that
# repository, and a note about a pull request in the repository it is about just works.
#
# What is never touched:  a code span, a fenced block, a preformatted block, a style or script block, an existing
# link, and the inside of any tag.  A hand-written markdown link therefore does not get a second one.
#
# The overrides (a caller may set these):
#   NOTES_LINKIFY=0            turn the whole step off
#   NOTES_GITHUB_OWNER=...     the GitHub owner, Simple-With-Us by default
#   NOTES_LINKIFY_GITHUB_HOST=...  the GitHub host, github.com by default
#   NOTES_LINKIFY_LINEAR_HOST=...  the Linear host, linear.app by default
#   NOTES_LINKIFY_REPO=...     the fallback repository, AI-Fleet-Coordinator by default
#   NOTES_LINKIFY_FORCE_REPO=...  use this repository for every pull request in this run
#   NOTES_LINEAR_WORKSPACE=... the Linear workspace slug, simple-with-us by default
#   NOTES_LINEAR_TEAM_KEYS=...  the team keys that count, the sixteen Linear teams by default
#   NOTES_LINKIFY_CONTEXT_CHARS=...  how near a repository name must be to count, 60 by default
#
# The fleet shortcut host, the site host, the PagerDuty host, the Sentry host, the Cursor host and the gh lookup
# budget are constants and not overrides:  the owner rule is no environment overrides, so they are the same on every
# seat and in every run, and a caller cannot point the helper at another host or lift the cap on the lookups.
_html_autolink() {
  /usr/bin/python3 -c '
import html as _html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def _env(name, default):
    v = (os.environ.get(name) or "").strip()
    return v if v else default


owner = _env("NOTES_GITHUB_OWNER", "Simple-With-Us")
github_host = _env("NOTES_LINKIFY_GITHUB_HOST", "github.com")
linear_host = _env("NOTES_LINKIFY_LINEAR_HOST", "linear.app")
default_repo = _env("NOTES_LINKIFY_REPO", "AI-Fleet-Coordinator")
forced_repo = _env("NOTES_LINKIFY_FORCE_REPO", "")
workspace = _env("NOTES_LINEAR_WORKSPACE", "simple-with-us")
keys_raw = _env("NOTES_LINEAR_TEAM_KEYS", "AFC,OPS,ST,CT,UM,CTS,DD,PS,AR,CL,BF,HH,C,CC,FL,SWU")
keyset = set(k.strip() for k in keys_raw.split(",") if k.strip())
try:
    ctx_chars = int(_env("NOTES_LINKIFY_CONTEXT_CHARS", "60") or 60)
except ValueError:
    #  A bad value must never cost the note its body:  the helper would exit with a traceback and
    #  the linkified HTML would come back empty, so the body would be written blank.
    ctx_chars = 60
debug = _env("NOTES_LINKIFY_DEBUG", "")

#  The hosts of the five services the linkifier links to, and the gh lookup budget of one run.  They
#  are constants on purpose:  the owner rule is no environment overrides, so these names and this cap
#  are the same on every seat and in every run.
FLEETLINK_HOST = "fleetlink.online"
SWU_HOST = "simplewithus.com"
PAGERDUTY_HOST = "jays-services.pagerduty.com"
SENTRY_HOST = "simple-with-us.sentry.io"
CURSOR_HOST = "cursor.com"
GH_BUDGET_S = 8.0

#  A repository that is not under the default owner.
REPO_OWNER = {"Fleet-OPS": "jaywedgeworth22"}

TEAM_TO_REPO = {
    #  One Linear team per repository:  the team key names the repository that team works in.
    "AFC": "AI-Fleet-Coordinator",
    "ST": "Socratic-Trade",
    "CT": "Congress.Trade",
    "UM": "Usage-Monitor",
    "CTS": "congress-trading-shared",
    "DD": "DealDex",
    "PS": "Personal-Site",
    "AR": "Autorotate",
    "CL": "ContactLogo",
    "BF": "BotFleet",
    "HH": "HogHunter",
    "C": "Clutch",
    "CC": "CodeCaps",
    "FL": "FleetLink",
    "OPS": "Fleet-OPS",
    "SWU": "Simple-With-Us",
    "CK": "Clutch",
    "CAPS": "CodeCaps",
    "CLUTCH": "Clutch",
}

#  The Linear projects a note may name, each coded by hand.
#
#  A Linear project has no URL that can be derived from its reference.  An issue is TEAM-NUMBER and
#  every issue of a team lives under /issue/, so a team key and a number are enough to build the link.
#  A project is neither:  its path carries a slug and a hash, so nothing about the reference says what
#  the slug is.  The owner ruled on Oct 26, 2026 that a project reference must therefore be coded
#  individually, and the Zulip linkifier team found no schema either.
#
#  So each project is one line here:  the reference a writer types, and the path of that project
#  under the workspace.  A reference that is not in this table is left as plain text.  It is never
#  guessed at, because a project reference that links to the wrong thing is worse than one that does
#  not link, and it is never treated as an issue, because the number in it is a project number and
#  not an issue number.  The entries between the BEGIN and END lines are generated from Linear
#  (Project.identifier and slugId) by scripts/linear_project_linkifiers.py, which the Operator runs
#  on the Linear project webhook (AFC-565);  each path is the project id alone, which Linear opens
#  even after a rename.  The same run updates scripts/zulip_linkifiers.py TARGET, and the parity
#  test fails until both sides name the same projects.
PROJECT_PATHS = {
    # BEGIN linear-projects:  generated by scripts/linear_project_linkifiers.py;  do not edit by hand
    #  Muster: Fleet Admin Mac App, which the fleet calls P-AFC-7.
    "P-AFC-7": "9c28cf61dcc8",
    #  Fleet GitOps, which the fleet calls P-AFC-8.
    "P-AFC-8": "272cdd72e951",
    #  System Prompt Receipt Port (BF-1411), which the fleet calls P-BF-1.
    "P-BF-1": "cc08b4cf163b",
    #  BotFleet Native macOS Rewrite, which the fleet calls P-BF-6.
    "P-BF-6": "5afcbc055535",
    #  CTS Shared Dependency, which the fleet calls P-CTS-5.
    "P-CTS-5": "a04bf2274b2d",
    #  Mac Resource Mgmt, which the fleet calls P-OPS-2.
    "P-OPS-2": "22f992634f02",
    #  Fleet hygiene (cross-team), which the fleet calls P-OPS-4.
    "P-OPS-4": "32fd751b0b36",
    #  Public Websites, which the fleet calls P-SWU-3.
    "P-SWU-3": "6e26b4c70c72",
    #  Telemetry for AI Platforms, which the fleet calls P-UM-9.
    "P-UM-9": "dd28b9b2ee11",
    # END linear-projects
}

#  A team that owns more than one repository.  A CTS issue is implemented in the shared congress
#  repository and in the applications that use it, so a pull request next to a CTS-330 can be in any
#  of them and GitHub is asked which one holds that number.
TEAM_EXTRA_REPOS = {
    "CTS": ["Congress.Trade", "Socratic-Trade", "Usage-Monitor"],
}

#  Team keys too short to read as a repository on their own:  CT is Central Time in a timestamp and
#  the others turn up as ordinary words.  They still name a repository after a number (AFC-504) and
#  in a note title ([ST, Claude]).
AMBIGUOUS_TEAM_KEYS = {"ST", "CT", "UM", "DD", "PS", "AR", "CL", "CC", "FL", "C", "OPS"}

TOKEN_TO_REPO = {
    #  The repositories themselves.
    "ai-fleet-coordinator": "AI-Fleet-Coordinator",
    "autorotate": "Autorotate",
    "botfleet": "BotFleet",
    "clutch": "Clutch",
    "codecaps": "CodeCaps",
    "congress.trade": "Congress.Trade",
    "contactlogo": "ContactLogo",
    "dealdex": "DealDex",
    "fleetlink": "FleetLink",
    "hoghunter": "HogHunter",
    "personal-site": "Personal-Site",
    "socratic-trade": "Socratic-Trade",
    "usage-monitor": "Usage-Monitor",
    "congress-trading-shared": "congress-trading-shared",
    "fleet-ops": "Fleet-OPS",
    #  The installed helpers that live in one repository.
    "minimax-companion": "Clutch",
    "mac-cleanup": "AI-Fleet-Coordinator",
    "mac-auto-cleanup": "AI-Fleet-Coordinator",
    "disk-janitor": "AI-Fleet-Coordinator",
    "fleet-work-backup": "AI-Fleet-Coordinator",
    "fleet-recall": "AI-Fleet-Coordinator",
    "fleet-rag": "AI-Fleet-Coordinator",
    "lane-tools": "AI-Fleet-Coordinator",
    "github-outbox-bridge": "AI-Fleet-Coordinator",
    "sim-idle-reaper": "AI-Fleet-Coordinator",
    "mac-process-watch": "AI-Fleet-Coordinator",
}


def _pairs(text):
    """The KEY=value pairs in a comma separated list, as a dict."""
    out = {}
    for pair in text.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, value = pair.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                out[key] = value
    return out



#  A caller may code more projects without editing this file:  REF=URL,REF=URL.  A value there is a
#  whole URL, so a caller can name a project on another workspace or another host, while a project
#  in the table above keeps following the host and the workspace the note is written for.
_project_overrides = _pairs(_env("NOTES_LINEAR_PROJECTS", ""))
PROJECT_URLS = {
    ref: "https://" + linear_host + "/" + workspace + "/project/" + path
    for ref, path in PROJECT_PATHS.items()
}
PROJECT_URLS.update(_project_overrides)

#  The maps can be overridden, so a test does not have to carry the names of the repositories of
#  the fleet to prove that the resolution works.
#    NOTES_LINKIFY_TEAM_REPOS=KEY=repo,KEY=repo+other
#      KEY is a team key, and the names after it are the repositories that team works in.
#    NOTES_LINKIFY_REPO_OWNERS=repo=owner
#      A repository that does not live under the default owner.
_team_overrides = _pairs(_env("NOTES_LINKIFY_TEAM_REPOS", ""))
if _team_overrides:
    TEAM_TO_REPO = dict(TEAM_TO_REPO)
    TOKEN_TO_REPO = dict(TOKEN_TO_REPO)
    _extra = {}
    for _key, _value in _team_overrides.items():
        _names = [n.strip() for n in _value.split("+") if n.strip()]
        if not _names:
            continue
        _upper = _key.upper()
        if _upper in TEAM_TO_REPO:
            TEAM_TO_REPO[_upper] = _names[0]
            if len(_names) > 1:
                _extra[_upper] = _names[1:]
        else:
            TOKEN_TO_REPO[_key.lower()] = _names[0]
    if _extra:
        TEAM_EXTRA_REPOS = dict(TEAM_EXTRA_REPOS)
        TEAM_EXTRA_REPOS.update(_extra)

_owner_overrides = _pairs(_env("NOTES_LINKIFY_REPO_OWNERS", ""))
if _owner_overrides:
    REPO_OWNER = dict(REPO_OWNER)
    REPO_OWNER.update(_owner_overrides)

_counts = {
    "issues": 0,
    "prs": 0,
    "projects": 0,
    "fleetlink": 0,
    "swu": 0,
    "repos": 0,
    "pagerduty": 0,
    "sentry": 0,
    "cursor": 0,
}
_unresolved = []


def _a(url, text):
    return "<a href=\"" + _html.escape(url, quote=True) + "\">" + text + "</a>"


def _pull_url(repo, num):
    return (
        "https://" + github_host + "/" + REPO_OWNER.get(repo, owner) + "/" + repo + "/pull/" + num
    )


def _issue_url(repo, num):
    return (
        "https://" + github_host + "/" + REPO_OWNER.get(repo, owner) + "/" + repo + "/issues/" + num
    )


def _mentions(text):
    """Every repository mention in a block of text, as (start, end, repository, strength, team key).

    A repository name, an installed helper, or a team key on its own is a strong mention.  A team
    key with a number (AFC-504) is the house Linear form, so it is a weak mention:  it names the
    team, not the repository, and the two are not always the same thing.
    """
    out = []
    low = text.lower()
    for tok, repo in TOKEN_TO_REPO.items():
        for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", low):
            out.append([m.start(), m.end(), repo, "repo", ""])
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2,4})(?![A-Za-z0-9])(?!-\d)", text):
        repo = TEAM_TO_REPO.get(m.group(1))
        if repo and m.group(1) not in AMBIGUOUS_TEAM_KEYS:
            out.append([m.start(), m.end(), repo, "team", m.group(1)])
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]{2,4})-\d+", text):
        repo = TEAM_TO_REPO.get(m.group(1))
        if repo:
            out.append([m.start(), m.end(), repo, "teamnum", m.group(1)])
    out.sort(key=lambda t: (t[0], -t[1]))
    ded = []
    for t in out:
        if ded and t[0] == ded[-1][0] and t[1] == ded[-1][1]:
            continue
        ded.append(t)
    return ded


_gh_cache = {}
_gh_spent = 0.0
_gh_bin = False


def _gh_path():
    """The gh CLI to ask, found once per run:  the override, PATH, then the usual places."""
    global _gh_bin
    if _gh_bin is False:
        _gh_bin = None
        for cand in (
            _env("NOTES_LINKIFY_GH", ""),
            shutil.which("gh") or "",
            "/opt/homebrew/bin/gh",
            "/usr/local/bin/gh",
            "/usr/bin/gh",
        ):
            if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
                _gh_bin = cand
                break
    return _gh_bin


def _gh_has_pr(repo, num):
    """Does this pull request exist in this repository?

    True when it does, False when the repository says it does not, and None when it could not be
    asked:  a missing gh, a machine without a session, a network that is not there, or a run whose
    lookup budget is spent.  The two answers are kept apart because a pull request that is not
    there is a reason to leave the reference alone, while a failed question is not.
    """
    global _gh_spent
    if not num:
        return None
    if _env("NOTES_LINKIFY_LOOKUP", "1") == "0":
        return None
    key = (repo, num)
    if key in _gh_cache:
        return _gh_cache[key]
    result = None
    gh = None
    _remaining = GH_BUDGET_S - _gh_spent
    if _remaining > 0:
        gh = _gh_path()
    if gh:
        url = "repos/" + REPO_OWNER.get(repo, owner) + "/" + repo + "/pulls/" + num
        try:
            _timeout = float(_env("NOTES_LINKIFY_GH_TIMEOUT_S", "10") or 10)
        except ValueError:
            _timeout = 10.0
        #  The budget caps the call itself, not just the decision to call it.  A call that starts
        #  with little budget left must not block the full timeout, so it waits at most what is left.
        _timeout = min(_timeout, _remaining)
        try:
            with tempfile.TemporaryFile() as err:
                with open(os.devnull, "w") as devnull:
                    _start = time.monotonic()
                    try:
                        rc = subprocess.call(
                            [gh, "api", "--silent", url], stdout=devnull, stderr=err, timeout=_timeout
                        )
                    finally:
                        _gh_spent += time.monotonic() - _start
                err.seek(0)
                said = err.read().decode("utf-8", "replace")
            if rc == 0:
                result = True
            elif "HTTP 404" in said or ("Not Found" in said and re.search(r"\b404\b", said) is not None):
                result = False
            else:
                result = None
        except (OSError, subprocess.TimeoutExpired):
            #  A gh that hangs must not hang the note save:  the timeout above ends it, and a gh
            #  that cannot be run at all is the same answer, that the question was not answered.
            #  Either way the lookup budget of the run pays for the wait, so a hung network
            #  stalls one run by one budget, not by one timeout per reference.
            result = None
    _gh_cache[key] = result
    return result


def _team_repo(key, num):
    """The repository a team key names for this pull request, or None when it cannot be told.

    A team that owns one repository always names it.  A team that owns several (a CTS issue is
    implemented in the shared congress repository and in the applications that use it) is asked of
    GitHub, which repository holds that number.  When the answer is that none of them holds it,
    the reference is left alone:  a link to the wrong repository is worse than no link.
    """
    primary = TEAM_TO_REPO.get(key)
    if primary is None:
        return None
    asked = False
    for repo in [primary] + list(TEAM_EXTRA_REPOS.get(key, [])):
        found = _gh_has_pr(repo, num)
        if found is True:
            return repo
        if found is False:
            asked = True
    #  Nothing holds it that could be proven.  When GitHub could not be asked, the repository of
    #  that team is the best answer available.  When GitHub said no everywhere, there is no link.
    if asked:
        return None
    return primary


def _soft_team(soft):
    """The team key a block names, when it names exactly one of them.

    A block that names two teams (AFC-547 and OPS-1000) says nothing about where a pull request
    lives, so no team wins and the reference is resolved by something else.  A block that names
    one team (UM-1594) is about the repository of that team.
    """
    keys = set(m[4] for m in soft)
    if len(keys) == 1:
        return keys.pop()
    return ""


def _resolve(pos, mentions, is_row, title_repo, num=""):
    """The repository a pull request reference belongs to, or None when it cannot be told.

    A reference is resolved in this order:  a repository named right next to it, the subject of a
    table row, the nearest repository named near it, the one repository the block talks about, the
    team key in the note title, and the default repository.  A block that names several
    repositories and a reference that names none of them is left alone:  a link to the wrong
    repository is worse than no link.

    A team key with a number (AFC-504) is the house Linear form.  It names a team, not a
    repository, so it only counts when it sits right next to the reference:  UM-1594 PRs #1694 is a
    Usage-Monitor pull request, but OPS-1000 is a Linear issue in the Fleet-OPS team and says
    nothing at all about where a pull request lives.  A team that owns more than one repository is
    asked of GitHub which of them holds the number.
    """
    hard = [m for m in mentions if m[3] != "teamnum"]
    soft = [m for m in mentions if m[3] == "teamnum"]
    hard_before = [m for m in hard if m[1] <= pos]
    soft_before = [m for m in soft if m[1] <= pos]
    if hard_before and pos - hard_before[-1][1] <= 16:
        return hard_before[-1][2]
    if is_row:
        #  A table row is about one thing, and its first mention names that thing.
        if hard:
            return hard[0][2]
        if soft:
            key = _soft_team(soft)
            if key:
                return _team_repo(key, num)
    if soft_before and pos - soft_before[-1][1] <= 16:
        #  Only a mention that comes before the reference can describe it:  a team key further
        #  down the block says nothing about what came before it.
        near = [m for m in soft if 0 <= pos - m[1] <= ctx_chars]
        key = _soft_team(near or soft_before)
        if key:
            return _team_repo(key, num)
    if hard_before and pos - hard_before[-1][1] <= ctx_chars:
        return hard_before[-1][2]
    hard_repos = set(m[2] for m in hard)
    if len(hard_repos) == 1:
        return next(iter(hard_repos))
    soft_repos = set(m[2] for m in soft)
    soft_keys = set(m[4] for m in soft)
    if not hard_repos and len(soft_repos) == 1:
        if len(soft_keys) == 1:
            #  One team, named a long way from the reference:  it is asked of GitHub which of the
            #  repositories that team works in holds the number, exactly as the near and the row
            #  paths do, so a team with several repositories is not decided by distance alone.
            return _team_repo(next(iter(soft_keys)), num)
        return next(iter(soft_repos))
    if title_repo:
        return title_repo
    if not hard_repos and not soft_repos:
        return default_repo
    return None


#  A pull request reference:  PR #957, PR 957, PRs #94/#95, PRs 439, 440 and 445, and the qualified
#  form AFC#957.  A qualified reference names its own repository and never depends on the context.
PR_RE = re.compile(
    r"(?<![A-Za-z0-9/_-])(?P<qual>[A-Za-z][A-Za-z0-9._-]{0,30})#(?P<qnum>\d+)(?![0-9])"
    r"|(?<![A-Za-z0-9/])(?P<kw>PRs?)(?P<gap>\s*#?\s*)"
    r"(?P<nums>\d+(?:\s*(?:/|,)\s*#?\s*\d+|\s+and\s+#?\s*\d+)*)"
)
LINEAR_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])(?P<key>[A-Za-z0-9]{1,6})-(?P<num>\d+)(?![A-Za-z0-9_-])"
)

#  A project reference:  P-BF-6, or P-6 where the team is named elsewhere.  The P is the marker and
#  the rest is the reference, so the whole thing is matched here and never offered to the issue rule.
#  The team segment is optional because a writer may leave it out, and because a project reference
#  that is left out of the team segment is still a project reference.  Matching it here is what
#  keeps it out of the issue rule, so it is matched whether or not it is one of the coded projects.
PROJECT_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])(?P<ref>P-(?:[A-Za-z0-9]{1,6}-)?\d+)(?![A-Za-z0-9_-])"
)

#  The live Zulip set, same shapes (AFC-562).  Each rule carries the house lookbehind, so it never
#  matches inside a URL or a longer path, and each claims its span before the pull request and the
#  issue rules run, so PagerDuty#384 is an incident and not a pull request, and a Sentry short id
#  is never read as a Linear issue.
FL_PATH_RE = r"[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*"
FL_RE = re.compile(r"(?<![A-Za-z0-9_/-])(?:fl|fleetlink)/(?P<path>" + FL_PATH_RE + r")")
SWU_RE = re.compile(r"(?<![A-Za-z0-9_/-])SWU/(?P<slug>[A-Za-z0-9_-]+)")
GH_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])GH@(?P<user>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]*[A-Za-z0-9_-])"
)
PAGERDUTY_RE = re.compile(r"(?<![A-Za-z0-9_/-])PagerDuty#(?P<id>[A-Z0-9]+)(?![A-Za-z0-9_-])")
CURSOR_RE = re.compile(r"(?<![A-Za-z0-9_/-])bc-(?P<id>[0-9a-f]{8}[A-Za-z0-9_-]*)")

#  Sentry projects in org simple-with-us, the thirteen slugs of the live rule.  A short id is the
#  upper-cased slug, a dash and a base-32 counter.
SENTRY_PROJECTS = (
    "AGENT-SESSIONS",
    "AUTOROTATE",
    "BOTFLEET",
    "CODECAPS",
    "CONGRESS-TRADE",
    "CONTACTLOGO",
    "DEALDEX",
    "FLEET-INFRA",
    "FLEETLINK",
    "HOGHUNTER",
    "PERSONAL-SITE",
    "SOCRATIC-TRADE",
    "USAGE-MONITOR",
)
SENTRY_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])(?P<id>(?:" + "|".join(SENTRY_PROJECTS) + r")-[0-9A-Z]+)"
    r"(?![A-Za-z0-9_-])"
)


LIST_RE = re.compile(r"(#\s*)?(\d+)|\band\b|([/,])")


def _list_items(nums_text, plural):
    """The pull request numbers in a list, and nothing else.

    PRs 439, 440 and 445 is three pull requests and the 22 files that follow it are not a fourth.
    PR #12, 34 files is one pull request:  a singular PR takes a list only when the numbers are
    written as PR #94/#95 or PR #95 and #96.

    Returns the numbers and the offset in nums_text just past the last one, so the caller can
    replace the list alone and leave whatever followed it, such as ", 22 files", where it was.
    """
    items = []
    last_sep = ""
    seen_and = False
    end = 0
    for m in LIST_RE.finditer(nums_text):
        text = m.group(0)
        if m.group(3):
            sep = m.group(3)
            if sep == "," and seen_and:
                break
            last_sep = sep
            continue
        if text == "and":
            seen_and = True
            last_sep = "and"
            continue
        hashed = m.group(1) is not None
        if items and last_sep in (",", "and") and not plural and not hashed:
            break
        items.append(m.group(2))
        end = m.end()
        last_sep = ""
    return items, end


def _pr_replacement(items, repo):
    """The house form: PR #957 for one number, PRs #94, #95 and #96 for a list.

    Returns the text and the number of links in it.
    """
    if not items:
        return None, 0
    if len(items) == 1:
        return _a(_pull_url(repo, items[0]), "PR #" + items[0]), 1
    parts = []
    for i, num in enumerate(items):
        if i:
            parts.append(" and " if i == len(items) - 1 else ", ")
        parts.append(_a(_pull_url(repo, num), "#" + num))
    return "PRs " + "".join(parts), len(items)


def _process_unit(unit_html, is_row, title_repo):
    """Rewrite one block of HTML.  The block is the context:  a table row, or a paragraph."""
    segs = re.split(
        r"(<pre[\s\S]*?</pre>|<code[\s\S]*?</code>|<style[\s\S]*?</style>|<script[\s\S]*?</script>|<a\b[^>]*>[\s\S]*?</a>)",
        unit_html,
        flags=re.I,
    )
    plain = []
    maps = []
    base = 0
    for idx, seg in enumerate(segs):
        if idx % 2 == 1:
            body = re.sub(r"<[^>]+>", " ", seg)
            plain.append(body)
            base += len(body)
            continue
        runs = re.split(r"(<[^>]+>)", seg)
        seg_plain = []
        offsets = []
        for r, run in enumerate(runs):
            if r % 2 == 1:
                seg_plain.append(" ")
                offsets.append((-1, 0))
            else:
                offsets.append((len("".join(seg_plain)), len(run)))
                seg_plain.append(run)
        joined = "".join(seg_plain)
        plain.append(joined)
        maps.append((base, offsets, runs))
        base += len(joined)
    context = "".join(plain)
    mentions = _mentions(context)
    out = []
    tcount = -1
    for idx, seg in enumerate(segs):
        if idx % 2 == 1:
            out.append(seg)
            continue
        tcount += 1
        seg_base, offsets, runs = maps[tcount]
        new_runs = list(runs)
        edits = []
        for r, run in enumerate(runs):
            if r % 2 == 1 or not run:
                continue
            run_plain_start, run_len = offsets[r]
            #  The live Zulip set first, each span claimed:  a PagerDuty incident is never offered
            #  to the qualified pull request rule, and a Sentry short id is never offered to the
            #  issue rule.
            claimed = []
            for rx, kind, url_of in (
                (FL_RE, "fleetlink", lambda m: "https://" + FLEETLINK_HOST + "/" + m.group("path")),
                (SWU_RE, "swu", lambda m: "https://" + SWU_HOST + "/" + m.group("slug")),
                (
                    GH_RE,
                    "repos",
                    lambda m: "https://" + github_host + "/" + m.group("user") + "/" + m.group("repo"),
                ),
                (
                    PAGERDUTY_RE,
                    "pagerduty",
                    lambda m: "https://" + PAGERDUTY_HOST + "/incidents/" + m.group("id"),
                ),
                (
                    SENTRY_RE,
                    "sentry",
                    lambda m: "https://" + SENTRY_HOST + "/issues/?query=" + m.group("id"),
                ),
                (
                    CURSOR_RE,
                    "cursor",
                    lambda m: "https://" + CURSOR_HOST + "/agents/bc-" + m.group("id"),
                ),
            ):
                for m in rx.finditer(run):
                    #  The first rule wins a span:  a path may carry a dot (fl/a.SWU/x), and without
                    #  this the rule inside it would write a second, overlapping link.
                    if any(m.start() < b and m.end() > a for a, b in claimed):
                        continue
                    claimed.append((m.start(), m.end()))
                    _counts[kind] += 1
                    edits.append((r, m.start(), m.end(), _a(url_of(m), m.group(0))))
            for m in PR_RE.finditer(run):
                if any(m.start() < b and m.end() > a for a, b in claimed):
                    continue
                qual = m.group("qual")
                if qual:
                    repo = TOKEN_TO_REPO.get(qual.lower())
                    if repo is None:
                        repo = TEAM_TO_REPO.get(qual.upper())
                    if repo:
                        _counts["prs"] += 1
                        #  A pound sign names a pull request, so the link is the pull request and
                        #  not the issue that shares its number.
                        edits.append((r, m.start(), m.end(), _a(_pull_url(repo, m.group("qnum")), qual + "#" + m.group("qnum"))))
                        continue
                    if qual.upper() != "PR":
                        continue
                    #  PR#957 with no space is the keyword form, not the qualified one
                    pos = seg_base + run_plain_start + m.start()
                    repo = forced_repo or _resolve(pos, mentions, is_row, title_repo, m.group("qnum"))
                    if repo is None:
                        _unresolved.append(m.group(0))
                        continue
                    _counts["prs"] += 1
                    edits.append(
                        (r, m.start(), m.end(), _a(_pull_url(repo, m.group("qnum")), "PR #" + m.group("qnum")))
                    )
                    continue
                items, end = _list_items(m.group("nums"), m.group("kw").lower() == "prs")
                if not items:
                    continue
                pos = seg_base + run_plain_start + m.start()
                repo = forced_repo or _resolve(pos, mentions, is_row, title_repo, items[0])
                if debug:
                    sys.stderr.write(
                        "notes: linkify:  " + m.group(0)[:16] + " -> " + str(repo)
                        + "  " + context[max(0, pos - 50):pos + 20].replace("\n", " ") + "\n"
                    )
                if repo is None:
                    _unresolved.append(m.group(0))
                    continue
                repl, count = _pr_replacement(items, repo)
                if repl:
                    _counts["prs"] += count
                    #  replace the list alone:  what follows it, such as ", 22 files", stays put
                    edits.append((r, m.start(), m.start("nums") + end, repl))
            #  A project reference is claimed here, before the issue rule runs, so the number in it
            #  can never be read as an issue number.  A project that is not coded stays plain text.
            project_spans = []
            for m in PROJECT_RE.finditer(run):
                #  A project shape inside a claimed span (fl/a.P-1) belongs to that rule.
                if any(m.start() < b and m.end() > a for a, b in claimed):
                    continue
                ref = m.group("ref")
                #  The span is claimed whether the project is coded or not:  a project reference
                #  that names no known project stays plain text, and the number inside it is a
                #  project number that no issue rule may ever read.
                project_spans.append((m.start(), m.end()))
                url = PROJECT_URLS.get(ref)
                if url is None:
                    _unresolved.append(ref)
                    continue
                _counts["projects"] += 1
                edits.append((r, m.start(), m.end(), _a(url, ref)))
            for m in LINEAR_RE.finditer(run):
                #  A reference inside a project reference is the project number and not an issue number.
                #  A reference inside a claimed span (a Sentry short id, a Cursor agent id, a fleet
                #  shortcut) belongs to that rule and is never read as an issue either.
                if any(m.start() >= a and m.end() <= b for a, b in project_spans + claimed):
                    continue
                key = m.group("key")
                if key not in keyset:
                    continue
                _counts["issues"] += 1
                ident = key + "-" + m.group("num")
                edits.append((r, m.start(), m.end(), _a("https://" + linear_host + "/" + workspace + "/issue/" + ident, ident)))
        if edits:
            by_run = {}
            for r, s, e, t in edits:
                by_run.setdefault(r, []).append((s, e, t))
            for r, items in by_run.items():
                text = new_runs[r]
                for s, e, t in sorted(items, key=lambda x: x[0], reverse=True):
                    text = text[:s] + t + text[e:]
                new_runs[r] = text
        out.append("".join(new_runs))
    return "".join(out)


UNIT_RE = re.compile(
    r"(<(?P<t>p|tr|li|h[1-6]|blockquote|pre)\b[^>]*>[\s\S]*?</(?P=t)>)"
)


def linkify(s, title=""):
    m = re.match(r"\[\s*([A-Za-z0-9]{1,6})\b", title)
    title_repo = TEAM_TO_REPO.get(m.group(1).upper()) if m else None
    out = []
    last = 0
    for um in UNIT_RE.finditer(s):
        if um.start() > last:
            out.append(_process_unit(s[last:um.start()], False, title_repo))
        out.append(_process_unit(um.group(0), um.group("t") == "tr", title_repo))
        last = um.end()
    if last < len(s):
        out.append(_process_unit(s[last:], False, title_repo))
    return "".join(out)


def _n(word, count):
    return str(count) + " " + word + ("" if count == 1 else "s")


def main():
    s = sys.stdin.read()
    if _env("NOTES_LINKIFY", "1") == "0":
        sys.stdout.write(s)
        return
    result = linkify(s, _env("NOTE_TITLE", ""))
    sys.stdout.write(result)
    if any(_counts.values()):
        sys.stderr.write(
            "notes: linkified:  "
            + _n("issue reference", _counts["issues"])
            + ", " + _n("project reference", _counts["projects"])
            + ", " + _n("fleetlink reference", _counts["fleetlink"])
            + ", " + _n("SWU reference", _counts["swu"])
            + ", " + _n("repository reference", _counts["repos"])
            + ", " + _n("PagerDuty reference", _counts["pagerduty"])
            + ", " + _n("Sentry reference", _counts["sentry"])
            + ", " + _n("Cursor agent reference", _counts["cursor"])
            + " and " + _n("pull request reference", _counts["prs"])
            + " became links\n"
        )
    if _unresolved:
        sys.stderr.write(
            "notes: left as text:  " + ", ".join(sorted(set(_unresolved))) + "\n"
        )
    if os.environ.get("NOTES_LINK_COUNTS_FILE"):
        try:
            with open(os.environ["NOTES_LINK_COUNTS_FILE"], "w") as _fh:
                _fh.write(
                    " ".join(
                        str(_counts[kind])
                        for kind in (
                            "issues",
                            "prs",
                            "projects",
                            "fleetlink",
                            "swu",
                            "repos",
                            "pagerduty",
                            "sentry",
                            "cursor",
                        )
                    )
                    + chr(10)
                )
        except OSError:
            pass


if __name__ == "__main__":
    main()
'
}

BODY_HTML=""
if [[ "$SKIP_BODY" == "0" ]]; then
  #  _LOG_INPUT_BYTES is the size of the body as it arrived, for the action log;  working it out can never fail
  #  the write.
  if [[ -n "$HTML_PATH" ]]; then
    [[ -f "$HTML_PATH" ]] || { echo "missing --html file: $HTML_PATH" >&2; exit 2; }
    _LOG_INPUT_BYTES=$(wc -c <"$HTML_PATH" 2>/dev/null | tr -d ' ') || _LOG_INPUT_BYTES=""
    BODY_HTML=$(cat "$HTML_PATH" | IS_EXPLICIT_HTML=1 _convert_body_to_html)
  elif [[ -n "${1:-}" ]]; then
    BODY_TEXT="$1"
    _LOG_INPUT_BYTES=$(printf '%s' "$BODY_TEXT" | wc -c | tr -d ' ') || _LOG_INPUT_BYTES=""
    BODY_HTML=$(printf '%s' "$BODY_TEXT" | _convert_body_to_html)
  elif [[ ! -t 0 ]]; then
    # Wait up to 0.5s for data to avoid hanging forever if called in an environment
    # with an open pipe but no data (e.g. task runners, cron).
    BODY_TEXT=$(/usr/bin/python3 -c 'import sys, select; r, _, _ = select.select([sys.stdin], [], [], 0.5); sys.stdout.write(sys.stdin.read()) if r else None' 2>/dev/null)
    _LOG_INPUT_BYTES=$(printf '%s' "$BODY_TEXT" | wc -c | tr -d ' ') || _LOG_INPUT_BYTES=""
    BODY_HTML=$(printf '%s' "$BODY_TEXT" | _convert_body_to_html)
  else
    _LOG_INPUT_BYTES=0
    BODY_HTML="<div></div>"
  fi
  BODY_HTML=$(printf '%s' "$BODY_HTML" | _html_sentence_gaps)
fi

# Title shape check + ensure second-row timestamp (owner 2026-08-09).
# Title: "[TEAM_KEY, Agent] Topic" — multi-app OK.  Body first line: "Sun, Aug 9, 3:52pm".
# Do NOT put a second markdown # Title in the body — that doubles the title in Notes.
if ! printf '%s' "$TITLE" | grep -Eq '^\[[^]]+\][[:space:]]+'; then
  echo "warning: title should start with [TEAM_KEY, Agent] e.g. \"[UM, Claude] Topic\"" >&2
fi
if printf '%s' "$TITLE" | grep -Eiq 'session'; then
  echo "warning: do not put 'session' in Apple Note titles" >&2
fi

# One secret on stdout:  the environment value, else `fleet-secrets get NAME`, else nothing.
_pushover_secret() {
  local name="$1" value="${!1:-}" bin
  if [[ -z "$value" ]]; then
    bin="$(command -v fleet-secrets 2>/dev/null || true)"
    [[ -z "$bin" && -x "${HOME}/.local/bin/fleet-secrets" ]] && bin="${HOME}/.local/bin/fleet-secrets"
    [[ -n "$bin" ]] && value="$("$bin" get "$name" 2>/dev/null || true)"
  fi
  printf '%s' "$value"
}

_send_pushover_notification() {
  local user_key token
  # Environment first, then `fleet-secrets get` (OPS-818;  the old flat secrets file is retired, AFC-359).
  user_key=$(_pushover_secret PUSHOVER_USER_KEY)
  token=$(_pushover_secret PUSHOVER_USAGE_API_TOKEN)
  [[ -z "$token" ]] && token=$(_pushover_secret PUSHOVER_ST_API_TOKEN)
  [[ -z "$token" ]] && token=$(_pushover_secret PUSHOVER_CT_API_TOKEN)

  local msg="${SUMMARY_TEXT:-Apple Note updated in Coding folder}"
  if [[ "$NEEDS_OWNER" == "1" ]]; then
    msg="⚠️ [NEEDS OWNER REVIEW] ${msg}"
  fi

  if [[ -n "${user_key:-}" && -n "${token:-}" ]]; then
    curl -s \
      --form-string "token=${token}" \
      --form-string "user=${user_key}" \
      --form-string "title=${TITLE}" \
      --form-string "message=${msg}" \
      --form-string "sound=pushover" \
      https://api.pushover.net/1/messages.json >/dev/null 2>&1 || true
    echo "notification: sent via Pushover push alert to owner"
  elif command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$msg\" with title \"$TITLE\"" 2>/dev/null || true
    echo "notification: sent via macOS notification"
  fi
}

if [[ "$SKIP_BODY" == "0" ]]; then
#  The counts of what the step below reformatted reach the action log through this file:  its stdout is the note.
_LOG_CHANGES_FILE=$(mktemp /tmp/apple-notes-changes.XXXXXX 2>/dev/null) || _LOG_CHANGES_FILE=""
BODY_HTML=$(printf '%s' "$BODY_HTML" | NOTE_TITLE="$TITLE" NEEDS_OWNER="$NEEDS_OWNER" SUMMARY_TEXT="$SUMMARY_TEXT" PR_NUMBERS="$PR_NUMBERS" NOTE_SESSION="$NOTE_SESSION" NOTES_CHANGES_FILE="$_LOG_CHANGES_FILE" /usr/bin/python3 -c "
import sys, re, html, os
from datetime import datetime
body = sys.stdin.read()
title = os.environ.get('NOTE_TITLE', '').strip()
needs_owner = os.environ.get('NEEDS_OWNER') == '1'
summary_text = os.environ.get('SUMMARY_TEXT', '').strip()
pr_numbers = os.environ.get('PR_NUMBERS', '').strip()
note_session = os.environ.get('NOTE_SESSION', '').strip()
now = datetime.now()
# Sun, Aug 9, 3:52pm — no leading zero on day/hour (portable; avoid %-I)
_h = now.hour % 12 or 12
stamp = (
    now.strftime('%a, %b ')
    + str(now.day)
    + ', '
    + str(_h)
    + ':'
    + now.strftime('%M')
    + now.strftime('%p').lower()
)

def strip_outer_div(s):
    s = s.strip()
    if s.startswith('<div>') and s.endswith('</div>'):
        return s[5:-6]
    return s

inner = strip_outer_div(body).lstrip()

# Deduplicate title if body begins with matching heading
if title:
    clean_title = re.sub(r'^[\[\(][^\]\)]+[\]\)]\s*', '', title).strip()
    escaped_titles = [re.escape(title), re.escape(clean_title), re.escape(html.escape(title)), re.escape(html.escape(clean_title))]
    pattern = r'^(?:<h[12][^>]*>\s*(?:' + '|'.join(escaped_titles) + r')\s*</h[12]>\s*(?:<div><br\s*/?></div>)?)\s*'
    inner = re.sub(pattern, '', inner, flags=re.I).lstrip()

m = re.match(
    r'^(?:<p>)?\s*'
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), '
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) '
    r'\d{1,2}, \d{1,2}:\d{2}[ap]m'
    r'(?:\s*[·|—|-]\s*(?:PR\s*#?\s*[\d\s,#]+)+)?'
    r'\s*(?:</p>)?\s*',
    inner,
    re.I,
)

pr_suffix = ''
if pr_numbers:
    parts = [p.strip() for p in re.split(r'[,|&]', pr_numbers) if p.strip()]
    formatted_prs = []
    for p in parts:
        clean_p = re.sub(r'^(?:PR\s*#?|#)', '', p, flags=re.I).strip()
        if clean_p:
            formatted_prs.append('PR #' + clean_p)
    if formatted_prs:
        pr_suffix = ' · ' + ', '.join(formatted_prs)

if m:
    matched_text = m.group(0)
    inner = inner[m.end():]
    if not pr_suffix:
        # Preserve existing PR divider if found in existing timestamp line
        pr_match = re.search(r'[·|—|-]\s*(PR\s*#?\s*[\d\s,#]+)', matched_text, re.I)
        if pr_match:
            pr_suffix = ' · ' + pr_match.group(1).strip()

# AFC-361, owner 2026-10-19:  the writer's session id is the small row under the timestamp, with no second date.
# An update rewrites the whole body, so a Session row the caller echoed back from the old body is dropped
# wherever it sat (under the timestamp, or at the end, where a version of 2026-10-19 put it) and the current one is
# stamped.  A row is an id-shaped token that ends its paragraph:  prose that merely starts \"Session:\" is not a row
# and stays.  A spacer after the row belongs to it, so a stale row at the end goes with every spacer it left behind
# (an old note can end on more than one of them, so the row must not have to be the last characters of the body).
_SESSION_ROW = (
    r'(?:<p>\s*)?(?:<span[^>]*>\s*)?\s*Session:\s*'
    r'[A-Za-z0-9][A-Za-z0-9._:-]{3,127}'
    # A row from before Oct 19 carried a second date after the id (' - Mon, Oct 19, 9:40pm').  Only that exact
    # shape is taken, so prose that merely begins with 'Session:' ('Session: claude-code resumed') stays.
    r'(?:\s+-\s+[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}(?:, \d{4})?(?:, \d{1,2}:\d{2}[ap]m)?)?'
    r'\s*(?:</span>\s*)?</p>'
)
_SPACER = r'(?:<div><br\s*/?></div>\s*)*'
inner = re.sub(r'^' + _SESSION_ROW + r'\s*' + _SPACER + r'\s*', '', inner, flags=re.I)
inner = re.sub(r'(?:^|\n|(?<=>))[>\s]*' + _SPACER + r'\s*' + _SESSION_ROW + r'\s*' + _SPACER + r'\s*$', '', inner, flags=re.I)
session_p = ''
if note_session:
    session_p = '<p><span style=\"font-size: 11px;\">' + html.escape('Session: ' + note_session) + '</span></p>'
stamp += pr_suffix
stamp_p = '<p>' + html.escape(stamp) + '</p>' + session_p + '<div><br></div>'

# Fleet dates (owner 2026-10-19):  an ISO date in the prose (2026-10-19, or 2026-10-20T02:03:00Z) becomes the fleet
# form, Mon, Oct 19 (or Mon, Oct 19, 9:03pm), in Central, with the year only when it is not this year.  Code, link
# targets and names that merely contain a date (backup-2026-10-19.tar) keep theirs.
from zoneinfo import ZoneInfo
_CENTRAL = ZoneInfo('America/Chicago')
_ISO = re.compile(
    #  A date is a token of its own:  a name or a query string that merely contains one (backup-2026-10-19.tar,
    #  ?d=2026-10-19) keeps it.  A dot after the date is the end of a sentence, so only a dot followed by a word
    #  character keeps the date whole.
    r'(?<![\w/.:=?&-])(\d{4})-(\d{2})-(\d{2})'
    # A +HH:MM / -HH:MM offset is only an offset on the ISO T form.  On the space form (2026-10-19 10:30) a bare
    # -11:00 after the minutes is the end of a time range, not a UTC offset, and taking it as one moves the hour
    # and drops the range end.  That range end is a second local time, so it is captured and rendered in the
    # fleet form too:  a note must not keep a 24-hour clock it rewrote everything else away from.
    r'(?:T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?'
    r'| (\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z)?(?:-(\d{2}):(\d{2}))?)?'
    r'(?![\w/-]|\.[A-Za-z0-9])'
)

def _fleet_day(d):
    out = d.strftime('%a, %b ') + str(d.day)
    return out + (', ' + str(d.year) if d.year != now.year else '')

def _fleet_clock(when):
    return str(when.hour % 12 or 12) + when.strftime(':%M') + when.strftime('%p').lower()

_changes = {'dates': [], 'gaps': 0}

def _fleet_date(m):
    new = _fleet_date_text(m)
    if new != m.group(0):
        _changes['dates'].append((m.group(0), new))
    return new

def _fleet_date_text(m):
    y, mo, d, t_hh, t_mm, t_zone, hh, mm, zone, r_hh, r_mm = m.groups()
    if t_hh is not None:
        hh, mm, zone = t_hh, t_mm, t_zone
    try:
        if hh is None:
            return _fleet_day(datetime(int(y), int(mo), int(d)))
        when = datetime(int(y), int(mo), int(d), int(hh), int(mm))
        if zone:
            offset = '+00:00' if zone == 'Z' else (zone if ':' in zone else zone[:3] + ':' + zone[3:])
            when = datetime.fromisoformat(when.isoformat() + offset).astimezone(_CENTRAL)
        out = _fleet_day(when) + ', ' + _fleet_clock(when)
        if r_hh is not None:
            end = datetime(int(y), int(mo), int(d), int(r_hh), int(r_mm))
            if zone:  # the range end is in the same zone as its start, so it converts the same way
                end = datetime.fromisoformat(end.isoformat() + offset).astimezone(_CENTRAL)
            out += '-' + _fleet_clock(end)
        return out
    except ValueError:
        return m.group(0)

def fleet_dates(markup, gap_entity=False):
    parts = re.split(r'(<[^>]+>)', markup)
    depth = 0
    for i, part in enumerate(parts):
        if part.startswith('<'):
            tag = re.match(r'</?\s*(pre|code)\b', part, re.I)
            if tag:
                depth += -1 if part.startswith('</') else 1
                depth = max(depth, 0)
        elif depth == 0:
            parts[i] = _one_space_gaps(_ISO.sub(_fleet_date, part), gap_entity)
    return ''.join(parts)

# Owner 2026-10-19:  a one-space sentence gap is widened too (the same rule as scripts/agent_sync/textfmt.py
# single_gaps), so
# a note reads the same whoever wrote it.  Known abbreviations, dotted words, initials and list markers are skipped.
_ABBREV = frozenset('mr mrs ms dr st jr sr vs etc e.g i.e eg ie cf al approx dept est fig no nos vol ca mt ft sq'.split())
_GAP1 = re.compile(r'([.!?][\x22\x27”’)\]*_]*) (?=[A-Z0-9\x22“(*#])')
_WORD = re.compile(r'([A-Za-z][A-Za-z.]*)\Z')

def _one_space_gaps(segment, gap_entity=False):
    def repl(m):
        if m.group(1).startswith('.'):
            before = _WORD.search(segment[max(0, m.start() - 40):m.start()])
            word = before.group(1).casefold().rstrip('.') if before else ''
            if before and (word in _ABBREV or len(word) == 1 or '.' in word):
                return m.group(0)
            if re.fullmatch(r'\s*\d{1,9}', segment[:m.start()]):
                return m.group(0)
        _changes['gaps'] += 1
        #  The note body is HTML, so the gap is the numeric reference for a no-break space,
        #  like the two HTML passes and with no raw non-ASCII byte in the note HTML.
        #  The mobile quick view is plain text that is embedded with html.escape, so
        #  a reference there would reach the owner as the literal five characters.
        #  It carries the no-break space itself.
        return m.group(1) + ('&#160; ' if gap_entity else '\xa0 ')
    return _GAP1.sub(repl, segment)

inner = fleet_dates(inner, gap_entity=True)
if summary_text:
    summary_text = fleet_dates(summary_text)

# Tell the writer what was reformatted, on stderr, so the agent learns the fleet format (owner 2026-10-19).
if _changes['dates']:
    shown = ', '.join(a + ' to ' + b for a, b in _changes['dates'][:5]) + (' and more' if len(_changes['dates']) > 5 else '')
    sys.stderr.write('notes: reformatted:  dates rewritten in the fleet format (' + shown + ');  write dates that way' + chr(10))
if _changes['gaps']:
    n = _changes['gaps']
    sys.stderr.write('notes: reformatted:  ' + str(n) + ' sentence gap' + ('' if n == 1 else 's') + ' had one space and ' + ('was' if n == 1 else 'were') + ' widened;  put two spaces after a sentence' + chr(10))
# The same counts for the action log (AFC-486), as two numbers in a file of their own.  A file that cannot be
# written costs the log its reformatted field and nothing else.
if os.environ.get('NOTES_CHANGES_FILE'):
    try:
        with open(os.environ['NOTES_CHANGES_FILE'], 'w') as _fh:
            _fh.write(str(len(_changes['dates'])) + ' ' + str(_changes['gaps']) + chr(10))
    except OSError:
        pass

extra_blocks = ''
if needs_owner:
    extra_blocks += '<div style=\"background-color: #FFF3CD; border-left: 5px solid #FFC107; color: #856404; padding: 10px 14px; margin-bottom: 12px; border-radius: 4px; font-family: -apple-system, sans-serif;\"><b>⚠️ NEEDS OWNER REVIEW / ACTION</b></div>'
if summary_text:
    extra_blocks += '<div style=\"background-color: #F8F9FA; border-left: 4px solid #0D6EFD; color: #212529; padding: 10px 14px; border-radius: 4px; margin-bottom: 12px; font-family: -apple-system, sans-serif;\"><b>📌 Mobile Quick View:</b> ' + html.escape(summary_text) + '</div>'

sys.stdout.write('<div>' + stamp_p + extra_blocks + inner + '</div>')
")

#  The body goes in on stdin and only the title travels as an argument.  macOS gives a process
#  1048576 bytes for the whole command line, the arguments and the environment together, and the
#  Background Jobs Master List is about 1 MB of HTML, so an argument body did not fit:  it died
#  with "Argument list too long" (exit 126) before the linkifier ran and before the note was
#  written, so the pinned master list silently went stale.  The Notes write itself was never at
#  risk, because osascript reads the HTML from a file.
FULL_HTML=$(printf '%s' "$BODY_HTML" | NOTE_TITLE="$TITLE" /usr/bin/python3 -c '
import html, os, sys
title = os.environ.get("NOTE_TITLE", "")
body = sys.stdin.read()
sys.stdout.write("<h1>" + html.escape(title) + "</h1>" + body)
')

#  The linkifier runs on the whole note, so the date row and its pull request suffix are covered as well as the body.
if [[ "$SKIP_BODY" == "0" ]]; then
  _LOG_LINKS_FILE=$(mktemp /tmp/apple-notes-links.XXXXXX 2>/dev/null) || _LOG_LINKS_FILE=""
  FULL_HTML=$(printf '%s' "$FULL_HTML" | NOTE_TITLE="$TITLE" NOTES_LINK_COUNTS_FILE="$_LOG_LINKS_FILE" _html_autolink)
fi

if [[ "${APPLE_NOTES_PRINT_HTML:-0}" == "1" ]]; then
  printf '%s\n' "$FULL_HTML"
  exit 0
fi

TMP=$(mktemp /tmp/apple-note.XXXXXX)
printf '%s' "$FULL_HTML" >"$TMP"
TITLE_FILE=$(mktemp /tmp/apple-note-title.XXXXXX)
printf '%s' "$TITLE" >"$TITLE_FILE"
#  _on_exit (the one EXIT trap, set after the argument parsing) removes both files, after the action log has read
#  the size and hash of the HTML from $TMP.
_TMP_MADE=1

if [[ "$MODE" == "update" ]]; then
  #  Unquoted so ${NOTES_APPLE_EVENT_TIMEOUT_S} expands:  in a quoted heredoc osascript would
  #  receive the literal text and the "with timeout of" clause would not compile.  The block still
  #  reads its paths through "system attribute",  so nothing else here expands.
  #  --existing-only (AFC-486):  a missing note, or a missing Coding folder, answers the not-found sentinel instead
  #  of being made, inside the same AppleEvent that would have written it, so no other write can land in between.
  NOTE_ID=$(TITLE_FILE="$TITLE_FILE" TMP="$TMP" WANT_ACTIVATE="$WANT_ACTIVATE" EXISTING_ONLY="$EXISTING_ONLY" osascript <<EOF
set titlePath to POSIX file (system attribute "TITLE_FILE")
set noteTitle to read titlePath as «class utf8»
set htmlPath to POSIX file (system attribute "TMP")
set htmlBody to read htmlPath as «class utf8»
--- A large body takes longer than the default two-minute AppleEvent timeout:
--- the master list is a quarter of a megabyte of markdown, and the write timed
--- out with "AppleEvent timed out. (-1712)" before this.
with timeout of ${NOTES_APPLE_EVENT_TIMEOUT_S} seconds
tell application "Notes"
  set codingFolder to missing value
  try
    set codingFolder to folder "Coding" of account "iCloud"
  end try
  if codingFolder is missing value then
    if (system attribute "EXISTING_ONLY") is "1" then return "__APPLE_NOTES_NOT_FOUND__"
    tell account "iCloud"
      set codingFolder to make new folder with properties {name:"Coding"}
    end tell
  end if
  set targetNote to missing value
  try
    set targetNote to note noteTitle of codingFolder
  end try
  if targetNote is missing value then
    if (system attribute "EXISTING_ONLY") is "1" then return "__APPLE_NOTES_NOT_FOUND__"
    -- create if missing
    set targetNote to make new note at codingFolder with properties {body:htmlBody}
  else
    set body of targetNote to htmlBody
  end if
  -- Do NOT show/activate by default (steals owner focus). Optional via env.
  if (system attribute "WANT_ACTIVATE") is "1" then
    show targetNote
    activate
  end if
  return id of targetNote
end tell
end timeout
EOF
)
  if [[ "$NOTE_ID" == "$_NOTES_NOT_FOUND" ]]; then
    NOTE_ID=""
    echo "no note with that title in the Coding folder" >&2
    exit 3
  fi
  echo "updated note id=$NOTE_ID in folder Coding"
else
  NOTE_ID=$(WANT_ACTIVATE="$WANT_ACTIVATE" osascript <<EOF
set htmlPath to POSIX file "$TMP"
set htmlBody to read htmlPath as «class utf8»

with timeout of ${NOTES_APPLE_EVENT_TIMEOUT_S} seconds
tell application "Notes"
  set codingFolder to missing value
  try
    set codingFolder to folder "Coding" of account "iCloud"
  end try
  if codingFolder is missing value then
    tell account "iCloud"
      set codingFolder to make new folder with properties {name:"Coding"}
    end tell
  end if

  set newNote to make new note at codingFolder with properties {body:htmlBody}
  if (system attribute "WANT_ACTIVATE") is "1" then
    show newNote
    activate
  end if
  return id of newNote
end tell
end timeout
EOF
)
  echo "created note id=$NOTE_ID in folder Coding"
fi
fi

if [[ "$WANT_NOTIFY" == "1" || "$NEEDS_OWNER" == "1" ]]; then
  _send_pushover_notification
fi

# The pin (2026-08-10, no GUI fallback since 2026-10-20): the "Pin Coding Note" Shortcut runs fully
# headless — no window, no focus steal, no Accessibility needed — so it is ON BY DEFAULT (owner
# preference: pin coding notes when able). The shortcut is:
#   Receive Text from Share Sheet → Find Note (Name contains Shortcut Input,
#   Folder is Coding, limit 1) → Add Note to pinned notes.
# Set APPLE_NOTES_PIN=0 to skip pinning entirely.
# _pin_rc: 0 pinned, 1 the run failed, 2 the Shortcut is not installed,
# 3 the Shortcut list could not be read, 4 pinning was disabled
# (APPLE_NOTES_PIN=0), 5 the Shortcuts app could not be started (AFC-436),
# 6 another pin run held the lock for the whole wait, 7 the lock directory could not be made, 8 the lock
# kept coming back stale.
# These are different problems and must not be reported
# as one (AFC-431).
#
# When the pin does not happen there is nothing else to try:  the helper never reaches for the Notes
# window.  A create or update still succeeded, so it warns and exits 0 with the note saved unpinned;
# --pin-only was asked to pin and nothing else, so it exits 4.
_pin_rc=4
if [[ "${APPLE_NOTES_PIN:-}" != "0" ]]; then
  # Settle delay: a just-created/updated note may not be visible to the
  # Shortcut's Find yet (index race) — a miss pops a note picker on the
  # owner's screen. Two seconds reliably clears it (observed 2026-08-10).
  [[ "$MODE" == "create" || "$MODE" == "update" ]] && sleep 2
  _pin_rc=0
  _run_note_shortcut "Pin Coding Note" || _pin_rc=$?
  if [[ "$_pin_rc" == "0" ]]; then
    echo "pinned: yes (headless via 'Pin Coding Note' shortcut)"
    exit 0
  fi
fi

if [[ "$MODE" == "pin" ]]; then
  _pin_outcome="nothing was pinned"
else
  _pin_outcome="note saved unpinned"
fi
#  The "pin skipped:" line is read back by agent_sync.notes, which keeps the first 200 characters of the
#  reason.  Each reason ends with $_pin_outcome, which tells a caller the note was saved, so every reason
#  has to fit in 200 characters with a long lock path or that tail is cut off
#  (scripts/test_apple_notes_pin.py checks the length of each one).
case "$_pin_rc" in
  1)
    echo "pinned: skipped ('Pin Coding Note' shortcut run failed)"
    echo "pin skipped: Pin Coding Note Shortcut failed; $_pin_outcome" >&2
    ;;
  2)
    echo "pinned: skipped ('Pin Coding Note' shortcut is not installed)"
    echo "pin skipped: the 'Pin Coding Note' Shortcut is not installed; $_pin_outcome" >&2
    ;;
  3)
    echo "pinned: skipped (the Shortcut list could not be read, so it is unknown whether 'Pin Coding Note' exists)"
    echo "pin skipped: the Shortcut list could not be read, so it is unknown whether 'Pin Coding Note' exists; $_pin_outcome" >&2
    ;;
  4)
    echo "pinned: skipped (pinning is disabled; APPLE_NOTES_PIN=0)"
    if [[ "$MODE" == "pin" ]]; then
      echo "pin skipped: pinning is disabled (APPLE_NOTES_PIN=0); $_pin_outcome" >&2
    fi
    ;;
  5)
    echo "pinned: skipped (the Shortcuts app could not be started in the background, so the Shortcut could not run; open Shortcuts once and retry)"
    echo "pin skipped: the Shortcuts app could not be started in the background, so the Shortcut could not run; open Shortcuts once and retry; $_pin_outcome" >&2
    ;;
  7)
    echo "pinned: skipped (the pin lock directory could not be made)"
    echo "pin skipped: the pin lock directory ${NOTES_PIN_LOCK_DIR} could not be made; $_pin_outcome" >&2
    ;;
  8)
    echo "pinned: skipped (the pin lock kept coming back stale)"
    echo "pin skipped: the pin lock kept coming back stale; remove ${NOTES_PIN_LOCK_DIR} if no pin is running; $_pin_outcome" >&2
    ;;
  *)
    echo "pinned: skipped (another pin or unpin run held the lock for ${NOTES_PIN_LOCK_WAIT_S}s)"
    echo "pin skipped: another pin or unpin run held the lock for ${NOTES_PIN_LOCK_WAIT_S}s; $_pin_outcome" >&2
    ;;
esac
if [[ "$MODE" == "pin" ]]; then
  exit 4
fi
exit 0
