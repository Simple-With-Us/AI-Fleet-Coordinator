#!/usr/bin/env bash
# Contract tests for scripts/mac-ledger-mirror.sh.  Fixtures are copied into a
# temp directory and every path override is set, including HOME and ARCHIVE_ROOT.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/mac-ledger-mirror.sh"
FIX="$ROOT/scripts/fixtures/mac-ledger-mirror"
NOW=1791179520
NOTE='**Mon, Oct 5, 2026 12:52am CT (GROK-BOT, AI-Fleet-Coordinator #340):** feat(kody): add defer flow.'
MARKER='<!-- mirror:Simple-With-Us/AI-Fleet-Coordinator#340 -->'

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "PASS: $*"
}

[[ -f "$SCRIPT" ]] || fail "missing $SCRIPT"
bash -n "$SCRIPT" || fail "bash -n mac-ledger-mirror.sh"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$SCRIPT" || fail "shellcheck"
  pass "shellcheck"
else
  pass "shellcheck skipped"
fi

bak_count() {
  find "$1" -type f 2>/dev/null | wc -l | tr -d ' '
}

setup() {
  tmp=$(mktemp -d)
  mkdir -p "$tmp/checkout/docs" "$tmp/home" "$tmp/archive"
  cp "$FIX/effort-log.md" "$tmp/checkout/docs/EFFORT-LOG.md"
  cp "$FIX/ledger.md" "$tmp/ledger.md"
  cp "$FIX/processes.md" "$tmp/processes.md"
  cp "$FIX/pr.json" "$tmp/pr.json"
}

run_mirror() {
  # shellcheck disable=SC2068
  env -u AGENT_TAG -u SEAT \
    HOME="$tmp/home" \
    ARCHIVE_ROOT="$tmp/archive" \
    LEDGER_FILE="$tmp/ledger.md" \
    MAC_PROCESSES_FILE="$tmp/processes.md" \
    MLM_REPO="Simple-With-Us/AI-Fleet-Coordinator" \
    MLM_PR_JSON="$tmp/pr.json" \
    MLM_NOW="$NOW" \
    TZ=America/Chicago \
    bash "$SCRIPT" "$@"
}

section_has() {
  local file="$1" heading="$2" needle="$3"
  python3 - "$file" "$heading" "$needle" << 'PY'
import sys
path, heading, needle = sys.argv[1:]
text = open(path, encoding="utf-8").read().splitlines()
current = "preamble"
buf = []
found = None
for line in text:
    if line.startswith("## "):
        if current == heading:
            found = "\n".join(buf)
        current = line[3:].strip()
        buf = []
    else:
        buf.append(line)
if current == heading:
    found = "\n".join(buf)
if found is None:
    sys.exit(2)
sys.exit(0 if needle in found else 1)
PY
}

# --- dry-run prints a diff and changes nothing ---
setup
before_l=$(cksum "$tmp/ledger.md")
before_p=$(cksum "$tmp/processes.md")
dry_out=$(run_mirror --dry-run --seat GROK-BOT "$tmp/checkout" 340)
printf '%s\n' "$dry_out" | grep -q 'Kody defer flow' || fail "dry-run diff missing effort row"
printf '%s\n' "$dry_out" | grep -q '12:52am CT' || fail "dry-run diff missing process note"
printf '%s\n' "$dry_out" | grep -q 'summary: effort-ledger would-change; mac-processes would-change' || fail "dry-run summary: $dry_out"
[[ "$(cksum "$tmp/ledger.md")" == "$before_l" ]] || fail "dry-run changed the ledger"
[[ "$(cksum "$tmp/processes.md")" == "$before_p" ]] || fail "dry-run changed processes"
[[ "$(bak_count "$tmp/archive")" == "0" ]] || fail "dry-run wrote a backup"
pass "dry-run"

# --- apply inserts the row, the note, and backups ---
setup
apply_out=$(run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340)
printf '%s\n' "$apply_out" | grep -q 'summary: effort-ledger updated; mac-processes updated' || fail "apply summary: $apply_out"
grep -q -F "$MARKER" "$tmp/ledger.md" || fail "marker missing"
grep -q 'Continuation stays with the row.' "$tmp/ledger.md" || fail "continuation dropped"
section_has "$tmp/ledger.md" "In Progress" "$MARKER" || fail "row not under In Progress"
section_has "$tmp/ledger.md" "In Progress" "Continuation stays with the row." || fail "continuation not in In Progress"
if section_has "$tmp/ledger.md" "Completed" "Kody defer flow"; then
  fail "new row landed in Completed"
fi
grep -q 'Unclassified noise' "$tmp/ledger.md" && fail "unclassified source row was mirrored"
grep -q 'IN PR #312' "$tmp/ledger.md" || fail "unrelated ledger row disappeared"
[[ -f "$tmp/archive/2026-10-05/ledger.md.005200.bak" ]] || fail "ledger backup missing"
[[ -f "$tmp/archive/2026-10-05/processes.md.005200.bak" ]] || fail "processes backup missing"
python3 - "$tmp/processes.md" "$NOTE" << 'PY' || fail "process note placement"
import sys
path, note = sys.argv[1:]
text = open(path, encoding="utf-8").read().splitlines()

def cells(line):
    body = line
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    out = []
    cur = ""
    esc = False
    for ch in body:
        if esc:
            cur += ch
            esc = False
            continue
        if ch == "\\":
            cur += ch
            esc = True
            continue
        if ch == "|":
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return out

def boundary(line, path):
    start = 0
    while True:
        pos = line.find(path, start)
        if pos < 0:
            return False
        before = "" if pos == 0 else line[pos - 1]
        after = line[pos + len(path):pos + len(path) + 1]
        before_ok = pos == 0 or before in " `/("
        after_ok = after == "" or after not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./~-"
        if before_ok and after_ok:
            return True
        start = pos + 1

header = None
matched = 0
for line in text:
    if line.startswith("| Name |"):
        header = cells(line)
        continue
    if header is None or not line.startswith("|") or set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
        continue
    if line.startswith("|---"):
        continue
    cols = cells(line)
    what = cols[2]
    live = cols[3]
    full = "scripts/pm2-ecosystem.config.cjs"
    if boundary(line, full):
        matched += 1
        if note not in what:
            sys.stderr.write("missing note in What it is: %s\n" % line[:80])
            sys.exit(1)
        if note in live:
            sys.stderr.write("note leaked into Live now\n")
            sys.exit(1)
    else:
        if "12:52am CT" in line:
            sys.stderr.write("note on a non-matching row: %s\n" % line[:80])
            sys.exit(1)
if matched != 2:
    sys.stderr.write("expected 2 matching rows, got %d\n" % matched)
    sys.exit(1)
PY
note_hits=$(grep -F -c 'AI-Fleet-Coordinator #340):**' "$tmp/processes.md" || true)
[[ "$note_hits" == "2" ]] || fail "expected the note twice, got ${note_hits}"
pass "apply"

# --- re-run apply is a no-op and does not add a backup ---
first_baks=$(bak_count "$tmp/archive")
rerun_out=$(run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340)
printf '%s\n' "$rerun_out" | grep -q 'summary: effort-ledger unchanged; mac-processes unchanged' || fail "re-run summary: $rerun_out"
second_baks=$(bak_count "$tmp/archive")
[[ "$first_baks" == "$second_baks" ]] || fail "re-run created a backup ($first_baks -> $second_baks)"
note_hits=$(grep -F -c 'AI-Fleet-Coordinator #340):**' "$tmp/processes.md" || true)
[[ "$note_hits" == "2" ]] || fail "re-run duplicated the process note"
pass "re-run no-op"

# --- lifecycle move: In Progress text changes and the section changes ---
cat > "$tmp/checkout/docs/EFFORT-LOG.md" << 'EOF'
## Completed

- **2026-10-05 — GROK-BOT — MERGED #340 — Kody defer flow landed.**  Shipped.

## In Progress

- **2026-10-01 — CLAUDE — IN PR #312 — Other work.**  Not this PR.
EOF
life_out=$(run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340)
printf '%s\n' "$life_out" | grep -q 'effort-ledger updated' || fail "lifecycle summary: $life_out"
section_has "$tmp/ledger.md" "Completed" "Kody defer flow landed." || fail "updated row not in Completed"
section_has "$tmp/ledger.md" "Completed" "$MARKER" || fail "marker not on the moved row"
if section_has "$tmp/ledger.md" "In Progress" "#340"; then
  fail "old #340 row still in In Progress"
fi
grep -q 'Building the defer workflow.' "$tmp/ledger.md" && fail "old row text still present"
life_baks=$(bak_count "$tmp/archive")
[[ "$life_baks" == "$((second_baks + 1))" ]] || fail "lifecycle should backup only the ledger ($second_baks -> $life_baks)"
pass "lifecycle move"

# --- missing section is created ---
setup
cp "$FIX/ledger-no-progress.md" "$tmp/ledger.md"
run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340 >/dev/null
section_has "$tmp/ledger.md" "In Progress" "$MARKER" || fail "created section missing the row"
section_has "$tmp/ledger.md" "Completed" "MERGED #247" || fail "existing completed row dropped"
pass "create section"

# --- no matching PR leaves both files alone ---
setup
cp "$FIX/pr-nomatch.json" "$tmp/pr.json"
before_l=$(cksum "$tmp/ledger.md")
before_p=$(cksum "$tmp/processes.md")
none_out=$(run_mirror --apply --seat GROK-BOT "$tmp/checkout" 12345)
printf '%s\n' "$none_out" | grep -q 'no effort-log rows reference PR #12345' || fail "missing no-match note: $none_out"
printf '%s\n' "$none_out" | grep -q 'summary: effort-ledger skipped; mac-processes unchanged' || fail "no-match summary: $none_out"
[[ "$(cksum "$tmp/ledger.md")" == "$before_l" ]] || fail "no-match changed the ledger"
[[ "$(cksum "$tmp/processes.md")" == "$before_p" ]] || fail "no-match changed processes"
[[ "$(bak_count "$tmp/archive")" == "0" ]] || fail "no-match wrote a backup"
pass "no-match"

# --- a pipe in the title is escaped and stays in the What it is cell ---
setup
cp "$FIX/pr-pipe.json" "$tmp/pr.json"
run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340 >/dev/null
python3 - "$tmp/processes.md" << 'PY' || fail "pipe in title broke the table"
import sys
path = sys.argv[1]
line = next(l for l in open(path, encoding="utf-8") if "botfleet-mcp" in l).rstrip("\n")
if "feat(kody): add defer \\| sweep." not in line:
    sys.stderr.write(line + "\n")
    sys.exit(1)
body = line[1:-1] if line.endswith("|") else line[1:]
parts = []
cur = ""
esc = False
for ch in body:
    if esc:
        cur += ch
        esc = False
        continue
    if ch == "\\":
        cur += ch
        esc = True
        continue
    if ch == "|":
        parts.append(cur)
        cur = ""
        continue
    cur += ch
parts.append(cur)
if len(parts) != 4:
    sys.stderr.write("cells=%d\n" % len(parts))
    sys.exit(1)
if "\\|" not in parts[2]:
    sys.exit(1)
PY
pass "escaped pipe"

# --- /pull/N is a PR reference ---
setup
cat > "$tmp/checkout/docs/EFFORT-LOG.md" << 'EOF'
## In Progress

- **2026-10-04 — GROK-BOT — follow-up https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/340 for the defer flow.**
EOF
run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340 >/dev/null
grep -q -F "$MARKER" "$tmp/ledger.md" || fail "/pull/340 row was not mirrored"
section_has "$tmp/ledger.md" "In Progress" "pull/340" || fail "/pull/340 row in the wrong section"
pass "pull url"

# --- a legacy row that names the repo and the PR is replaced, not duplicated ---
setup
cat > "$tmp/ledger.md" << 'EOF'
## In Progress

- **2026-10-02 — GROK-BOT — IN PR #340 — stale wording about AI-Fleet-Coordinator.**  Old text.

## Completed

- **2026-09-17 — GROK — MERGED #247 — Ledger digest.**
EOF
run_mirror --apply --seat GROK-BOT "$tmp/checkout" 340 >/dev/null
marker_hits=$(grep -F -c "$MARKER" "$tmp/ledger.md" || true)
[[ "$marker_hits" == "1" ]] || fail "fallback match duplicated the row ($marker_hits)"
grep -q 'stale wording' "$tmp/ledger.md" && fail "stale fallback row was left in place"
section_has "$tmp/ledger.md" "In Progress" "Kody defer flow" || fail "replacement row missing"
pass "fallback match"

# --- missing seat is an error and writes nothing ---
setup
set +e
seat_out=$(env -u AGENT_TAG -u SEAT \
  HOME="$tmp/home" \
  ARCHIVE_ROOT="$tmp/archive" \
  LEDGER_FILE="$tmp/ledger.md" \
  MAC_PROCESSES_FILE="$tmp/processes.md" \
  MLM_REPO="Simple-With-Us/AI-Fleet-Coordinator" \
  MLM_PR_JSON="$tmp/pr.json" \
  MLM_NOW="$NOW" \
  TZ=America/Chicago \
  bash "$SCRIPT" --dry-run "$tmp/checkout" 340 2>&1)
seat_code=$?
set -e
[[ "$seat_code" -ne 0 ]] || fail "missing seat exited 0"
printf '%s\n' "$seat_out" | grep -qi 'seat' || fail "missing seat message: $seat_out"
[[ "$(bak_count "$tmp/archive")" == "0" ]] || fail "missing seat wrote a backup"
pass "missing seat"

echo "OK"
