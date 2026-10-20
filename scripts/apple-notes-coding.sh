#!/usr/bin/env bash
# Create (or update) an Apple Note in the iCloud "Coding" folder and try to pin it.
# Owner preference 2026-08-05: coding notes always go to Coding + pin to top.
# Owner 2026-08-09: title + timestamp shape (ALL apps, ALL agents):
#
#   Title:  "[TEAM_KEY, SEAT_TAG] Topic"  e.g. "[UM, MM] TestFlight first ship"
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
#   apple-notes-coding.sh --pin-only "Exact Note Title"
#   apple-notes-coding.sh --unpin-only "Exact Note Title"
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
# else $AGENT_SEAT, else the SEAT_TAG named in the title ("[TEAM_KEY, WISE] topic", how Instinct's bots call this over ssh);
# it is never a flag.  Data copy of fleet-apps.json `sessionIds`;  scripts/test_session_ids_parity.py keeps it equal.
# APPLE_NOTES_PRINT_HTML=1 prints the note HTML and exits before Notes is touched (the tests use it).
#
# Focus policy (owner 2026-08-10): create/update MUST NOT steal focus by default.
# Notes receives AppleScript body edits without activate/show.
#
# Pinning (owner + Claude 2026-08-10): fully headless by DEFAULT via the
# "Pin Coding Note" macOS Shortcut (Receive Text from Share Sheet → Find Note
# where Name contains Shortcut Input + Folder is Coding, limit 1 → Add Note to
# pinned notes). No window, no focus steal, no Accessibility. Unpin twin:
# "Unpin Coding Note" (same, with Remove). First run of each shortcut shows a
# one-time "Allow ... to share with Notes?" dialog — choose Always Allow.
# If the shortcut is missing, pin falls back to the legacy GUI menu-click path,
# which needs Accessibility and steals focus, and only runs with
# --pin / --pin-only / APPLE_NOTES_PIN=1.
# Canonical policy: /Users/jay/apps/AGENT-SYNC.md § Apple Notes.

set -euo pipefail

MODE=create
WANT_PIN=0
WANT_ACTIVATE=0
WANT_NOTIFY=0
NEEDS_OWNER=0
SUMMARY_TEXT=""
PR_NUMBERS="${PR_NUMBERS:-${APPLE_NOTES_PR:-}}"
HTML_PATH=""
TITLE=""
SESSION_ARG=""

# Env overrides (agents may set these explicitly).
[[ "${APPLE_NOTES_PIN:-0}" == "1" || "${APPLE_NOTES_PIN:-}" == "true" ]] && WANT_PIN=1
[[ "${APPLE_NOTES_ACTIVATE:-0}" == "1" || "${APPLE_NOTES_ACTIVATE:-}" == "true" ]] && WANT_ACTIVATE=1
[[ "${APPLE_NOTES_NOTIFY:-0}" == "1" || "${APPLE_NOTES_NOTIFY:-}" == "true" ]] && WANT_NOTIFY=1
[[ "${APPLE_NOTES_NEEDS_OWNER:-0}" == "1" || "${APPLE_NOTES_NEEDS_OWNER:-}" == "true" ]] && NEEDS_OWNER=1

# Robust argument parsing supporting flags in any order
while [[ $# -gt 0 ]]; do
  case "${1:-}" in
    --pin-only)
      MODE=pin
      WANT_PIN=1
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
    --pin)
      WANT_PIN=1
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
      echo "usage: $0 [--update] [--pin-only|--unpin-only|--pin|--activate|--notify|--needs-owner|--summary text|--pr \"18\"|--html path|--session id] \"Title\" [body]" >&2
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

if [[ -z "$TITLE" ]]; then
  echo "usage: $0 [--update] [--pin-only|--unpin-only|--pin|--activate|--notify|--needs-owner|--summary text|--pr \"18\"|--html path] \"Title\" [body]" >&2
  exit 2
fi

# Strip accidental backslash escaping on title brackets e.g. \[TEAM_KEY, SEAT_TAG\]
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
  case "$1" in ECHO|GROK-BOT|JET|MA|WISE|GB-*) return 0 ;; esac
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

if [[ "$MODE" == "pin" || "$MODE" == "unpin" ]]; then
  NOTE_ID=$(osascript -e "tell application \"Notes\" to get id of note \"$TITLE\" of folder \"Coding\" of account \"iCloud\"" 2>/dev/null || true)
  [[ -n "$NOTE_ID" ]] || { echo "$MODE-only: no note with that title in Coding" >&2; exit 3; }
  SKIP_BODY=1
else
  SKIP_BODY=0
fi

# Run a headless pin/unpin Shortcut with the note title as a text-file input.
# Returns 0 on success, 2 when the Shortcut is not installed, 1 when the run
# itself failed, 3 when the Shortcut list could not be read.  A failed list
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

_run_note_shortcut() {  # $1 = shortcut name
  local listing
  if ! listing="$(shortcuts list 2>/dev/null)"; then
    return 3
  fi
  printf '%s\n' "$listing" | grep -qxF "$1" || return 2
  local tmp attempt attempts
  tmp="$(mktemp).txt"
  printf '%s' "$TITLE" > "$tmp"
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
    if shortcuts run "$1" -i "$tmp" >/dev/null 2>&1; then
      rm -f "$tmp"
      return 0
    fi
    if [[ "$attempt" -ge 2 ]] || ! _pin_retry_on; then break; fi
    attempt=2
    sleep 3
  done
  rm -f "$tmp"
  return 1
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
    # Notes.app is an HTML renderer — two ASCII spaces collapse.
    # Sentence gap in HTML is &nbsp; + space.
    s = re.sub(r"([.!?]) {2,}", r"\1&nbsp; ", s)
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
# into `&nbsp; ` so sentence gaps survive.  Skip <pre>, <code>, <style>, <script>.
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
    out.append(re.sub(r"([.!?]) {2,}", r"\1&nbsp; ", part))
sys.stdout.write("".join(out))
'
}

BODY_HTML=""
if [[ "$SKIP_BODY" == "0" ]]; then
  if [[ -n "$HTML_PATH" ]]; then
    [[ -f "$HTML_PATH" ]] || { echo "missing --html file: $HTML_PATH" >&2; exit 2; }
    BODY_HTML=$(cat "$HTML_PATH" | IS_EXPLICIT_HTML=1 _convert_body_to_html)
  elif [[ -n "${1:-}" ]]; then
    BODY_TEXT="$1"
    BODY_HTML=$(printf '%s' "$BODY_TEXT" | _convert_body_to_html)
  elif [[ ! -t 0 ]]; then
    # Wait up to 0.5s for data to avoid hanging forever if called in an environment
    # with an open pipe but no data (e.g. task runners, cron).
    BODY_TEXT=$(/usr/bin/python3 -c 'import sys, select; r, _, _ = select.select([sys.stdin], [], [], 0.5); sys.stdout.write(sys.stdin.read()) if r else None' 2>/dev/null)
    BODY_HTML=$(printf '%s' "$BODY_TEXT" | _convert_body_to_html)
  else
    BODY_HTML="<div></div>"
  fi
  BODY_HTML=$(printf '%s' "$BODY_HTML" | _html_sentence_gaps)
fi

# Title shape check + ensure second-row timestamp (owner 2026-08-09).
# Title: "[TEAM_KEY, SEAT_TAG] Topic" — multi-app OK.  Body first line: "Sun, Aug 9, 3:52pm".
# Do NOT put a second markdown # Title in the body — that doubles the title in Notes.
if ! printf '%s' "$TITLE" | grep -Eq '^\[[^]]+\][[:space:]]+'; then
  echo "warning: title should start with [TEAM_KEY, SEAT_TAG] e.g. \"[UM, MM] Topic\"" >&2
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
BODY_HTML=$(printf '%s' "$BODY_HTML" | NOTE_TITLE="$TITLE" NEEDS_OWNER="$NEEDS_OWNER" SUMMARY_TEXT="$SUMMARY_TEXT" PR_NUMBERS="$PR_NUMBERS" NOTE_SESSION="$NOTE_SESSION" /usr/bin/python3 -c "
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
# stamped.  A row is an id-shaped token that ends its paragraph:  prose that merely starts "Session:" is not a row
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

def fleet_dates(markup):
    parts = re.split(r'(<[^>]+>)', markup)
    depth = 0
    for i, part in enumerate(parts):
        if part.startswith('<'):
            tag = re.match(r'</?\s*(pre|code)\b', part, re.I)
            if tag:
                depth += -1 if part.startswith('</') else 1
                depth = max(depth, 0)
        elif depth == 0:
            parts[i] = _one_space_gaps(_ISO.sub(_fleet_date, part))
    return ''.join(parts)

# Owner 2026-10-19:  a one-space sentence gap is widened too (the same rule as agent-sync's textfmt.single_gaps), so
# a note reads the same whoever wrote it.  Known abbreviations, dotted words, initials and list markers are skipped.
_ABBREV = frozenset('mr mrs ms dr st jr sr vs etc e.g i.e eg ie cf al approx dept est fig no nos vol ca mt ft sq'.split())
_GAP1 = re.compile(r'([.!?][\x22\x27”’)\]*_]*) (?=[A-Z0-9\x22“(*#])')
_WORD = re.compile(r'([A-Za-z][A-Za-z.]*)\Z')

def _one_space_gaps(segment):
    def repl(m):
        if m.group(1).startswith('.'):
            before = _WORD.search(segment[max(0, m.start() - 40):m.start()])
            word = before.group(1).casefold().rstrip('.') if before else ''
            if before and (word in _ABBREV or len(word) == 1 or '.' in word):
                return m.group(0)
            if re.fullmatch(r'\s*\d{1,9}', segment[:m.start()]):
                return m.group(0)
        _changes['gaps'] += 1
        return m.group(1) + '  '
    return _GAP1.sub(repl, segment)

inner = fleet_dates(inner)
if summary_text:
    summary_text = fleet_dates(summary_text)

# Tell the writer what was reformatted, on stderr, so the agent learns the fleet format (owner 2026-10-19).
if _changes['dates']:
    shown = ', '.join(a + ' to ' + b for a, b in _changes['dates'][:5]) + (' and more' if len(_changes['dates']) > 5 else '')
    sys.stderr.write('notes: reformatted:  dates rewritten in the fleet format (' + shown + ');  write dates that way' + chr(10))
if _changes['gaps']:
    n = _changes['gaps']
    sys.stderr.write('notes: reformatted:  ' + str(n) + ' sentence gap' + ('' if n == 1 else 's') + ' had one space and ' + ('was' if n == 1 else 'were') + ' widened;  put two spaces after a sentence' + chr(10))

extra_blocks = ''
if needs_owner:
    extra_blocks += '<div style=\"background-color: #FFF3CD; border-left: 5px solid #FFC107; color: #856404; padding: 10px 14px; margin-bottom: 12px; border-radius: 4px; font-family: -apple-system, sans-serif;\"><b>⚠️ NEEDS OWNER REVIEW / ACTION</b></div>'
if summary_text:
    extra_blocks += '<div style=\"background-color: #F8F9FA; border-left: 4px solid #0D6EFD; color: #212529; padding: 10px 14px; border-radius: 4px; margin-bottom: 12px; font-family: -apple-system, sans-serif;\"><b>📌 Mobile Quick View:</b> ' + html.escape(summary_text) + '</div>'

sys.stdout.write('<div>' + stamp_p + extra_blocks + inner + '</div>')
")

FULL_HTML=$(/usr/bin/python3 -c '
import html, sys
title = sys.argv[1]
body = sys.argv[2]
print("<h1>" + html.escape(title) + "</h1>" + body)
' "$TITLE" "$BODY_HTML")

if [[ "${APPLE_NOTES_PRINT_HTML:-0}" == "1" ]]; then
  printf '%s\n' "$FULL_HTML"
  exit 0
fi

TMP=$(mktemp /tmp/apple-note.XXXXXX)
printf '%s' "$FULL_HTML" >"$TMP"
TITLE_FILE=$(mktemp /tmp/apple-note-title.XXXXXX)
printf '%s' "$TITLE" >"$TITLE_FILE"
trap 'rm -f "$TMP" "$TITLE_FILE"' EXIT

if [[ "$MODE" == "update" ]]; then
  NOTE_ID=$(TITLE_FILE="$TITLE_FILE" TMP="$TMP" WANT_ACTIVATE="$WANT_ACTIVATE" osascript <<'EOF'
set titlePath to POSIX file (system attribute "TITLE_FILE")
set noteTitle to read titlePath as «class utf8»
set htmlPath to POSIX file (system attribute "TMP")
set htmlBody to read htmlPath as «class utf8»
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
  set targetNote to missing value
  try
    set targetNote to note noteTitle of codingFolder
  end try
  if targetNote is missing value then
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
EOF
)
  echo "updated note id=$NOTE_ID in folder Coding"
else
  NOTE_ID=$(WANT_ACTIVATE="$WANT_ACTIVATE" osascript <<EOF
set htmlPath to POSIX file "$TMP"
set htmlBody to read htmlPath as «class utf8»

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
EOF
)
  echo "created note id=$NOTE_ID in folder Coding"
fi
fi

if [[ "$WANT_NOTIFY" == "1" || "$NEEDS_OWNER" == "1" ]]; then
  _send_pushover_notification
fi

# Preferred pin path (2026-08-10): the "Pin Coding Note" Shortcut runs fully
# headless — no window, no focus steal, no Accessibility needed — so it is ON
# BY DEFAULT (owner preference: pin coding notes when able). The shortcut is:
#   Receive Text from Share Sheet → Find Note (Name contains Shortcut Input,
#   Folder is Coding, limit 1) → Add Note to pinned notes.
# Set APPLE_NOTES_PIN=0 to skip pinning entirely.
# _pin_rc: 0 pinned, 1 the run failed, 2 the Shortcut is not installed,
# 3 the Shortcut list could not be read, 4 pinning was disabled
# (APPLE_NOTES_PIN=0).  These are different problems and must not be reported
# as one (AFC-431).
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
  elif [[ "$_pin_rc" == "1" ]]; then
    echo "pinned: the 'Pin Coding Note' shortcut ran but did not complete; falling back to GUI path if requested" >&2
  elif [[ "$_pin_rc" == "2" ]]; then
    echo "pinned: the 'Pin Coding Note' shortcut is not installed; falling back to GUI path if requested" >&2
  else
    echo "pinned: the Shortcut list could not be read, so it is unknown whether 'Pin Coding Note' exists; falling back to GUI path if requested" >&2
  fi
fi

# Legacy fallback: GUI pin (needs Accessibility for System Events).
# Requires show+activate + menu click — steals focus. Skipped unless
# --pin / --pin-only / APPLE_NOTES_PIN=1.
if [[ "$WANT_PIN" != "1" ]]; then
  case "$_pin_rc" in
    2) echo "pinned: skipped ('Pin Coding Note' shortcut is not installed; pass --pin for GUI pin, which steals focus)" ;;
    3) echo "pinned: skipped (the Shortcut list could not be read, so it is unknown whether 'Pin Coding Note' exists; pass --pin for GUI pin, which steals focus)" ;;
    4) echo "pinned: skipped (pinning is disabled; APPLE_NOTES_PIN=0)" ;;
    *) echo "pinned: skipped ('Pin Coding Note' shortcut run failed; pass --pin for GUI pin, which steals focus)" ;;
  esac
  exit 0
fi

# Deterministic selection: re-`show` the exact note by id, then menu-click, with retries —
# the old version pinned whatever happened to be selected, which failed (or could pin the
# wrong note) whenever creation focus was lost (2026-08-08 fix).
PIN_RESULT=$(NOTE_ID="$NOTE_ID" osascript <<'EOF' 2>&1 || true
set noteId to system attribute "NOTE_ID"
set pinned to false
repeat with attempt from 1 to 3
  tell application "Notes"
    try
      show note id noteId
    end try
    activate
  end tell
  delay (0.5 * attempt + 0.4)
  tell application "System Events"
    tell process "Notes"
      repeat with menuName in {"File", "Note", "Edit"}
        try
          if exists menu item "Unpin Note" of menu menuName of menu bar 1 then
            set pinned to true
            exit repeat
          end if
        end try
        try
          set mi to menu item "Pin Note" of menu menuName of menu bar 1
          if enabled of mi then
            click mi
            set pinned to true
            exit repeat
          end if
        end try
      end repeat
    end tell
  end tell
  if pinned then exit repeat
end repeat
if pinned then
  return "pinned"
else
  return "pin-menu-not-found"
end if
EOF
)

if [[ "$PIN_RESULT" == "pinned" ]]; then
  echo "pinned: yes"
elif echo "$PIN_RESULT" | grep -qi 'assistive access\|not allowed'; then
  echo "pinned: no (grant Accessibility to Terminal/iTerm/osascript, or right-click note → Pin Note)"
else
  echo "pinned: no ($PIN_RESULT) — right-click note in list → Pin Note"
fi
