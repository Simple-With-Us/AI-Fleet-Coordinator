#!/usr/bin/env bash
# Mirror a PR's docs/EFFORT-LOG.md rows into the Mac effort ledger and append a
# note to matching MAC-LOCAL-PROCESSES.md rows.  Default mode is --dry-run.
# bash 3.2 and POSIX awk (no GNU date, no sed -i).  python3 is required.
set -euo pipefail

usage() {
  cat << 'EOF'
Usage: scripts/mac-ledger-mirror.sh [--dry-run|--apply] [--seat TAG] <repo-checkout-dir> <pr-number>

  --dry-run   Print a unified diff and do not write (default).
  --apply     Write changed files after copying each one to ARCHIVE_ROOT.
  --seat TAG  Seat tag for the process note.  Falls back to AGENT_TAG or SEAT.

Environment:
  LEDGER_FILE          Effort ledger (default /Users/jay/apps/TRADING-EFFORT-LOG.md)
  MAC_PROCESSES_FILE   Process table (default /Users/jay/apps/MAC-LOCAL-PROCESSES.md)
  ARCHIVE_ROOT         Backup directory (default $HOME/apps/_archive)
  MLM_REPO             owner/name.  Else `gh repo view`, else the origin remote.
  MLM_PR_JSON          PR JSON file.  Else `gh pr view` in the checkout.
  MLM_NOW              Epoch seconds for the note and backup names.
  TZ                   Timestamp zone (default America/Chicago).
EOF
}

mode=dry-run
seat="${AGENT_TAG:-${SEAT:-}}"
checkout=""
pr=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) mode=dry-run; shift ;;
    --apply) mode=apply; shift ;;
    --seat)
      [[ $# -ge 2 ]] || { echo "error: --seat needs a tag" >&2; exit 2; }
      seat=$2
      shift 2
      ;;
    --seat=*) seat=${1#*=}; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "error: unknown option: $1" >&2; exit 2 ;;
    *)
      if [[ -z "$checkout" ]]; then checkout=$1
      elif [[ -z "$pr" ]]; then pr=$1
      else echo "error: unexpected argument: $1" >&2; exit 2
      fi
      shift
      ;;
  esac
done

while [[ $# -gt 0 ]]; do
  if [[ -z "$checkout" ]]; then checkout=$1
  elif [[ -z "$pr" ]]; then pr=$1
  else echo "error: unexpected argument: $1" >&2; exit 2
  fi
  shift
done

if [[ -z "$checkout" || -z "$pr" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$pr" =~ ^[0-9]+$ ]]; then
  echo "error: pr number must be digits" >&2
  exit 2
fi
if [[ -z "$seat" ]]; then
  echo "error: seat tag required (--seat, AGENT_TAG, or SEAT)" >&2
  exit 1
fi
if [[ ! -d "$checkout" ]]; then
  echo "error: checkout dir not found: $checkout" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi
if ! command -v awk >/dev/null 2>&1; then
  echo "error: awk is required" >&2
  exit 1
fi

if [[ -z "${TZ:-}" ]]; then
  export TZ=America/Chicago
fi

LEDGER_FILE="${LEDGER_FILE:-/Users/jay/apps/TRADING-EFFORT-LOG.md}"
MAC_PROCESSES_FILE="${MAC_PROCESSES_FILE:-/Users/jay/apps/MAC-LOCAL-PROCESSES.md}"
ARCHIVE_ROOT="${ARCHIVE_ROOT:-${HOME}/apps/_archive}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

repo_from_url() {
  python3 - "$1" << 'PY'
import re, sys
url = sys.argv[1].strip()
match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
if not match:
    sys.exit(1)
print("%s/%s" % (match.group(1), match.group(2)))
PY
}

if [[ -z "${MLM_REPO:-}" ]]; then
  if command -v gh >/dev/null 2>&1; then
    if ! MLM_REPO=$(cd "$checkout" && gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null); then
      MLM_REPO=""
    fi
  fi
  if [[ -z "${MLM_REPO:-}" ]]; then
    if origin=$(git -C "$checkout" remote get-url origin 2>/dev/null); then
      if ! MLM_REPO=$(repo_from_url "$origin"); then
        MLM_REPO=""
      fi
    else
      origin=""
    fi
  fi
fi
if [[ -z "${MLM_REPO:-}" || "$MLM_REPO" != */* ]]; then
  echo "error: could not determine owner/repo (set MLM_REPO)" >&2
  exit 1
fi
short="${MLM_REPO##*/}"

if [[ -z "${MLM_PR_JSON:-}" ]]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "error: MLM_PR_JSON is unset and gh is not available" >&2
    exit 1
  fi
  MLM_PR_JSON="$work/pr.json"
  (cd "$checkout" && gh pr view "$pr" --json number,title,files,state,mergedAt) > "$MLM_PR_JSON"
fi
if [[ ! -f "$MLM_PR_JSON" ]]; then
  echo "error: PR JSON not found: $MLM_PR_JSON" >&2
  exit 1
fi

LC_ALL=C python3 - "$MLM_PR_JSON" "$work/stamp.txt" "$work/files.txt" "$work/note.txt" "$seat" "$short" "$pr" << 'PY'
import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

pr_json, stamp_path, files_path, note_path, seat, short, pr = sys.argv[1:]
import json
with open(pr_json, encoding="utf-8") as handle:
    data = json.load(handle)
title = data.get("title") or ""
title = title.replace("\r", " ").replace("\n", " ").strip()
title = title.replace("|", "\\|")
if title and not title.endswith("."):
    title = title + "."
elif not title:
    title = "(no title)."

tz = ZoneInfo(os.environ.get("TZ") or "America/Chicago")
raw = os.environ.get("MLM_NOW", "").strip()
if raw:
    dt = datetime.fromtimestamp(int(raw), tz)
else:
    dt = datetime.now(tz)
hour = dt.hour % 12
if hour == 0:
    hour = 12
ampm = "am" if dt.hour < 12 else "pm"
human = "%s, %s %d, %d %d:%s%s CT" % (
    dt.strftime("%a"), dt.strftime("%b"), dt.day, dt.year, hour, dt.strftime("%M"), ampm,
)
note = " **%s (%s, %s #%s):** %s" % (human, seat, short, pr, title)
with open(stamp_path, "w", encoding="utf-8") as handle:
    handle.write(dt.strftime("%Y-%m-%d") + "\n")
    handle.write(dt.strftime("%H%M%S") + "\n")
    handle.write(human + "\n")
paths = []
for item in data.get("files") or []:
    if isinstance(item, str):
        paths.append(item)
    elif isinstance(item, dict):
        path = item.get("path") or item.get("filename") or ""
        if path:
            paths.append(path)
with open(files_path, "w", encoding="utf-8") as handle:
    for path in paths:
        handle.write(path + "\n")
with open(note_path, "w", encoding="utf-8") as handle:
    handle.write(note + "\n")
PY

date_part=$(sed -n '1p' "$work/stamp.txt")
time_part=$(sed -n '2p' "$work/stamp.txt")

backup_file() {
  local src="$1"
  local base dest
  base=$(basename "$src")
  dest="$ARCHIVE_ROOT/$date_part/${base}.${time_part}.bak"
  if [[ -e "$dest" ]]; then
    local n=2
    while [[ -e "${dest%.bak}-${n}.bak" ]]; do
      n=$((n + 1))
    done
    dest="${dest%.bak}-${n}.bak"
  fi
  mkdir -p "$(dirname "$dest")"
  cp "$src" "$dest"
}

publish_state=unchanged
publish() {
  local orig="$1"
  local neu="$2"
  publish_state=unchanged
  if [[ ! -f "$orig" ]]; then
    echo "error: target file not found: $orig" >&2
    exit 1
  fi
  if cmp -s "$orig" "$neu"; then
    publish_state=unchanged
    return
  fi
  if [[ "$mode" == "dry-run" ]]; then
    diff -u "$orig" "$neu" || true
    publish_state=would-change
    return
  fi
  backup_file "$orig"
  local dir tmp
  dir=$(dirname "$orig")
  tmp=$(mktemp "$dir/.mlm.XXXXXX")
  cat "$neu" > "$tmp"
  mv "$tmp" "$orig"
  publish_state=updated
}

cat > "$work/extract.awk" << 'AWK'
function classify(h,    hl) {
  hl = tolower(h)
  if (hl ~ /in progress/) return "inprogress"
  if (hl ~ /deployed/) return "deployed"
  if (hl ~ /completed/) return "completed"
  if (hl ~ /planned/ || hl ~ /reserved/) return "planned"
  return "other"
}
function matches_pr(text, prnum,    re1, re2) {
  re1 = "(^|[^0-9])#" prnum "([^0-9]|$)"
  re2 = "/pull/" prnum "([^0-9]|$)"
  return (text ~ re1) || (text ~ re2)
}
function is_heading(s) { return s ~ /^##([ \t]|$)/ }
function is_bullet(s) { return s ~ /^- / }
function is_cont(s) { return s ~ /^[ \t]/ && s !~ /^[ \t]*$/ }
{
  n++
  line[n] = $0
}
END {
  sec = 0
  class = "other"
  i = 1
  while (i <= n) {
    if (is_heading(line[i])) {
      sec++
      class = classify(line[i])
      i++
      continue
    }
    if (is_bullet(line[i]) && sec > 0 && class != "other") {
      start = i
      j = i + 1
      while (j <= n && is_cont(line[j])) j++
      text = line[start]
      for (k = start + 1; k < j; k++) text = text "\n" line[k]
      first = line[start]
      if (matches_pr(text, pr) && !(first in seen)) {
        seen[first] = 1
        count = j - start
        print "@@ROW@@"
        print class
        print first
        print count
        for (k = start; k < j; k++) print line[k]
      }
      i = j
      continue
    }
    i++
  }
}
AWK

cat > "$work/ledger.awk" << 'AWK'
function classify(h,    hl) {
  hl = tolower(h)
  if (hl ~ /in progress/) return "inprogress"
  if (hl ~ /deployed/) return "deployed"
  if (hl ~ /completed/) return "completed"
  if (hl ~ /planned/ || hl ~ /reserved/) return "planned"
  return "other"
}
function matches_pr(text, prnum,    re1, re2) {
  re1 = "(^|[^0-9])#" prnum "([^0-9]|$)"
  re2 = "/pull/" prnum "([^0-9]|$)"
  return (text ~ re1) || (text ~ re2)
}
function is_heading(s) { return s ~ /^##([ \t]|$)/ }
function is_bullet(s) { return s ~ /^- / }
function is_cont(s) { return s ~ /^[ \t]/ && s !~ /^[ \t]*$/ }
function strip_marker(s) {
  while (match(s, /<!--[ \t]*mirror:[^>]*-->/)) {
    s = substr(s, 1, RSTART - 1) substr(s, RSTART + RLENGTH)
  }
  sub(/[ \t]+$/, "", s)
  return s
}
function norm_text(text,    nparts, i, parts, first, out) {
  nparts = split(text, parts, "\n")
  first = strip_marker(parts[1])
  out = first
  for (i = 2; i <= nparts; i++) out = out "\n" parts[i]
  return out
}
function with_marker(text,    nparts, i, parts, first, out, marker) {
  nparts = split(text, parts, "\n")
  first = strip_marker(parts[1])
  marker = "<!-- mirror:" repo "#" pr " -->"
  out = first " " marker
  for (i = 2; i <= nparts; i++) out = out "\n" parts[i]
  return out
}
function has_marker(text,    needle) {
  needle = "<!-- mirror:" repo "#" pr " -->"
  if (index(text, needle) > 0) return 1
  needle = "<!-- mirror:" repo "#" pr "-->"
  if (index(text, needle) > 0) return 1
  return 0
}
function is_candidate(text) {
  if (has_marker(text)) return 1
  if (short != "" && index(text, short) > 0 && matches_pr(text, pr)) return 1
  return 0
}
function canon(class) {
  if (class == "inprogress") return "## In Progress"
  if (class == "deployed") return "## Deployed"
  if (class == "completed") return "## Completed"
  if (class == "planned") return "## Planned"
  return "## Notes"
}
function emit_inserts(c,    k, nparts, t, parts, ii) {
  print ""
  for (k = 1; k <= nins[c]; k++) {
    t = ins_body[c, k]
    nparts = split(t, parts, "\n")
    for (ii = 1; ii <= nparts; ii++) print parts[ii]
    print ""
  }
}
BEGIN {
  pc = 0
  state = ""
  while ((getline rec < planfile) > 0) {
    if (rec == "@@ROW@@") { state = "class"; continue }
    if (state == "class") { pc++; pclass[pc] = rec; state = "first"; continue }
    if (state == "first") { pfirst[pc] = rec; state = "count"; continue }
    if (state == "count") {
      pleft[pc] = rec + 0
      pbody[pc] = ""
      pgot[pc] = 0
      state = "body"
      if (pleft[pc] == 0) state = ""
      continue
    }
    if (state == "body") {
      if (pgot[pc] == 0) pbody[pc] = rec
      else pbody[pc] = pbody[pc] "\n" rec
      pgot[pc]++
      if (pgot[pc] >= pleft[pc]) state = ""
      continue
    }
  }
  close(planfile)
}
{
  nlines++
  L[nlines] = $0
}
END {
  nsec = 0
  nrows = 0
  sec = 0
  i = 1
  while (i <= nlines) {
    if (is_heading(L[i])) {
      nsec++
      sec = nsec
      sec_at[nsec] = i
      sec_class[nsec] = classify(L[i])
      i++
      continue
    }
    if (is_bullet(L[i])) {
      nrows++
      row_start[nrows] = i
      j = i + 1
      while (j <= nlines && is_cont(L[j])) j++
      row_end[nrows] = j - 1
      row_sec[nrows] = sec
      text = L[i]
      for (k = i + 1; k < j; k++) text = text "\n" L[k]
      row_text[nrows] = text
      for (k = i; k < j; k++) line_row[k] = nrows
      i = j
      continue
    }
    i++
  }

  for (p = 1; p <= pc; p++) {
    action[p] = ""
    src = norm_text(pbody[p])
    for (r = 1; r <= nrows; r++) {
      if (consumed[r]) continue
      if (!is_candidate(row_text[r])) continue
      if (norm_text(row_text[r]) != src) continue
      consumed[r] = 1
      rowclass = "other"
      if (row_sec[r] > 0) rowclass = sec_class[row_sec[r]]
      if (rowclass == pclass[p]) action[p] = "noop"
      else {
        action[p] = "insert"
        deleted[r] = 1
      }
      break
    }
  }

  for (p = 1; p <= pc; p++) {
    if (action[p] != "") continue
    paired = 0
    for (r = 1; r <= nrows; r++) {
      if (consumed[r]) continue
      if (!is_candidate(row_text[r])) continue
      consumed[r] = 1
      deleted[r] = 1
      action[p] = "insert"
      paired = 1
      break
    }
    if (!paired) action[p] = "insert"
  }

  for (p = 1; p <= pc; p++) {
    if (action[p] != "insert") continue
    c = pclass[p]
    nins[c]++
    ins_body[c, nins[c]] = with_marker(pbody[p])
  }

  for (i = 1; i <= nlines; i++) {
    rid = line_row[i] + 0
    if (rid > 0 && deleted[rid]) continue
    print L[i]
    for (s = 1; s <= nsec; s++) {
      if (sec_at[s] != i) continue
      c = sec_class[s]
      if (!emitted[c] && nins[c] > 0) {
        emit_inserts(c)
        emitted[c] = 1
      }
    }
  }

  order[1] = "deployed"
  order[2] = "inprogress"
  order[3] = "completed"
  order[4] = "planned"
  for (oi = 1; oi <= 4; oi++) {
    c = order[oi]
    if (nins[c] > 0 && !emitted[c]) {
      print ""
      print canon(c)
      emit_inserts(c)
      emitted[c] = 1
    }
  }
}
AWK

cat > "$work/process.awk" << 'AWK'
function split_cells(line, cells,    s, i, c, cur, n, esc) {
  s = line
  if (substr(s, 1, 1) == "|") s = substr(s, 2)
  if (substr(s, length(s), 1) == "|") s = substr(s, 1, length(s) - 1)
  n = 0
  cur = ""
  esc = 0
  for (i = 1; i <= length(s); i++) {
    c = substr(s, i, 1)
    if (esc) { cur = cur c; esc = 0; continue }
    if (c == "\\") { cur = cur c; esc = 1; continue }
    if (c == "|") { n++; cells[n] = cur; cur = ""; continue }
    cur = cur c
  }
  n++
  cells[n] = cur
  return n
}
function is_sep(s,    n, i, c, cells) {
  if (s !~ /^\|/) return 0
  n = split_cells(s, cells)
  if (n < 2) return 0
  for (i = 1; i <= n; i++) {
    c = cells[i]
    gsub(/[ \t]/, "", c)
    if (c == "") continue
    if (c !~ /^:?-+:?$/) return 0
  }
  return 1
}
function find_what(header,    n, i, low, cells) {
  n = split_cells(header, cells)
  for (i = 1; i <= n; i++) {
    low = tolower(cells[i])
    if (index(low, "what it is") > 0) return i
  }
  return n
}
function path_after_ok(c) {
  if (c == "") return 1
  if (c ~ /[A-Za-z0-9_.\/~-]/) return 0
  return 1
}
function before_ok(pos, before) {
  if (pos == 1) return 1
  if (before == " " || before == "`" || before == "/" || before == "(") return 1
  return 0
}
function path_hit(line,    p, path, start, pos, before, after, rest) {
  for (p = 1; p <= npaths; p++) {
    path = paths[p]
    if (path == "") continue
    start = 1
    while (start <= length(line)) {
      rest = substr(line, start)
      pos = index(rest, path)
      if (pos == 0) break
      pos = start + pos - 1
      before = ""
      if (pos > 1) before = substr(line, pos - 1, 1)
      after = substr(line, pos + length(path), 1)
      if (before_ok(pos, before) && path_after_ok(after)) return 1
      start = pos + 1
    }
  }
  return 0
}
function annotate(line, what,    n, i, cells, out) {
  if (token != "" && index(line, token) > 0) return line
  if (!path_hit(line)) return line
  n = split_cells(line, cells)
  if (what < 1 || what > n) what = n
  cells[what] = cells[what] note
  out = "|"
  for (i = 1; i <= n; i++) out = out cells[i] "|"
  return out
}
BEGIN {
  npaths = 0
  while ((getline rec < pathsfile) > 0) {
    if (rec != "") {
      npaths++
      paths[npaths] = rec
    }
  }
  close(pathsfile)
  note = ""
  if ((getline rec < notefile) > 0) note = rec
  close(notefile)
}
{
  N++
  L[N] = $0
}
END {
  i = 1
  while (i <= N) {
    if (L[i] ~ /^\|/ && (i + 1) <= N && is_sep(L[i + 1])) {
      what = find_what(L[i])
      print L[i]
      print L[i + 1]
      i += 2
      while (i <= N && L[i] ~ /^\|/ && !is_sep(L[i])) {
        print annotate(L[i], what)
        i++
      }
      continue
    }
    print L[i]
    i++
  }
}
AWK

ledger_state=skipped
src="$checkout/docs/EFFORT-LOG.md"
if [[ ! -f "$src" ]]; then
  echo "note: no docs/EFFORT-LOG.md under ${checkout}; skipping effort-ledger mirror"
elif [[ ! -f "$LEDGER_FILE" ]]; then
  echo "error: ledger not found: $LEDGER_FILE" >&2
  exit 1
else
  awk -f "$work/extract.awk" -v pr="$pr" "$src" > "$work/plan.txt"
  if [[ ! -s "$work/plan.txt" ]]; then
    echo "note: no effort-log rows reference PR #${pr}; skipping effort-ledger mirror"
    ledger_state=skipped
  else
    awk -f "$work/ledger.awk" \
      -v planfile="$work/plan.txt" \
      -v repo="$MLM_REPO" \
      -v pr="$pr" \
      -v short="$short" \
      "$LEDGER_FILE" > "$work/ledger.new"
    publish "$LEDGER_FILE" "$work/ledger.new"
    ledger_state=$publish_state
  fi
fi

proc_state=skipped
if [[ ! -f "$MAC_PROCESSES_FILE" ]]; then
  echo "note: mac processes file not found: ${MAC_PROCESSES_FILE}; skipping process notes"
else
  token="${short} #${pr}):**"
  awk -f "$work/process.awk" \
    -v pathsfile="$work/files.txt" \
    -v notefile="$work/note.txt" \
    -v token="$token" \
    "$MAC_PROCESSES_FILE" > "$work/processes.new"
  publish "$MAC_PROCESSES_FILE" "$work/processes.new"
  proc_state=$publish_state
fi

echo "summary: effort-ledger ${ledger_state}; mac-processes ${proc_state}"
exit 0
