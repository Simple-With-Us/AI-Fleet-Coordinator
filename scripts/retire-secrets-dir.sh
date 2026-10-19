#!/usr/bin/env bash
# retire-secrets-dir.sh -- AFC-359.  Gets ~/.secrets ready to be deleted by Jay.  It NEVER deletes.
#
#   retire-secrets-dir.sh check [--docs] [--idle]   no reader is left and no process holds a file open (read-only)
#   retire-secrets-dir.sh bootout            stop and disable the one writer, com.jay.infisical-secrets-sync, and MOVE its
#                                            plist, script and the one-time seeder to ~/apps/_retired/ (rollback undoes it)
#   retire-secrets-dir.sh archive [--rename] copy ~/.secrets into an AES-256 encrypted disk image (you type the
#                                            passphrase), verify it, and with --rename also rename ~/.secrets to
#                                            ~/.secrets.retired-<stamp> (a reversible canary:  if anything breaks,
#                                            `retire-secrets-dir.sh rollback` puts it back)
#   retire-secrets-dir.sh rollback           rename the retired folder back and re-enable the writer
#
# Order:  check, bootout, check again, archive --rename, live with it for a day, then delete the archived
# folder yourself.  This script prints the delete command and never runs it.
#
# What `check` covers:  the origin/main of every repo under ~/Code;  plain files on this Mac (home dotfiles, ~/bin,
# ~/.local/bin, ~/apps and so on);  every git checkout directly under ~/apps (scanned unless it is clean and equal to its
# repo's origin/main);  symlinks into the folder;  and every program path a LaunchAgent or pm2 runs (each must lie
# inside something scanned).  A checkout that no LaunchAgent, pm2 job or PATH entry uses is an idle copy:  its hits
# are reported as idle-hit (a warning, listed with `check --idle`) and never fail the check.  Task worktrees (~/apps/lanes, ~/apps/_retired, ~/apps/_archive) are skipped:  their code
# is covered through origin/main.
#
# Output carries file paths, line numbers, process names and counts only:  never a line of a file, never a value.
# Hermetic test hooks (scripts/test-retire-secrets-dir.sh):  RETIRE_HOME, RETIRE_CODE_DIR, RETIRE_ALLOW,
# RETIRE_LAUNCHCTL, RETIRE_HDIUTIL, RETIRE_LSOF, RETIRE_UID, RETIRE_STAMP, RETIRE_ARCHIVE_DIR,
# RETIRE_PLIST_DIR (default ~/Library/LaunchAgents), RETIRE_PM2 (a command that prints `pm2 jlist` JSON).
set -u
HOME_DIR="${RETIRE_HOME:-$HOME}"
SECRETS="$HOME_DIR/.secrets"
CODE_DIR="${RETIRE_CODE_DIR:-$HOME_DIR/Code}"
here="$(cd "$(dirname "$0")" && pwd)"
ALLOW="${RETIRE_ALLOW:-$here/retire-secrets-dir.allow}"
LAUNCHCTL="${RETIRE_LAUNCHCTL:-launchctl}"
HDIUTIL="${RETIRE_HDIUTIL:-hdiutil}"
LSOF="${RETIRE_LSOF:-lsof}"
UID_NUM="${RETIRE_UID:-$(id -u)}"
STAMP="${RETIRE_STAMP:-$(date +%Y%m%d-%H%M%S)}"
ARCHIVE_DIR="${RETIRE_ARCHIVE_DIR:-$HOME_DIR/Documents}"
VOLUMES_DIR="${RETIRE_VOLUMES:-/Volumes}"
TTY_IN="${RETIRE_TTY_IN:-/dev/tty}"
TTY_OUT="${RETIRE_TTY_OUT:-/dev/tty}"
PROG_RAW=""
PLIST_DIR="${RETIRE_PLIST_DIR:-$HOME_DIR/Library/LaunchAgents}"
PM2_CMD="${RETIRE_PM2:-pm2 jlist}"
WRITER_LABEL="com.jay.infisical-secrets-sync"
WRITER_PLIST="$HOME_DIR/Library/LaunchAgents/$WRITER_LABEL.plist"
WRITER_SCRIPT="$HOME_DIR/bin/infisical-secrets-sync.sh"
SEEDER_DIR="$HOME_DIR/apps/infisical-sync"
RETIRED_DIR="${RETIRE_RETIRED_DIR:-$HOME_DIR/apps/_retired}"
# What counts as a reader:  the folder name as a path component (~/.secrets, $HOME/.secrets, "/.secrets", ".secrets" or
# '.secrets', a path join with a .secrets component) or the variable names code uses to build the path.  A property
# access such as body.secrets, data.secrets or x?.secrets (an API response field) is not the folder and does not match:
# the dot must not follow a letter, digit, underscore, closing paren or bracket, or a question mark.
PATTERN='(^|[^]A-Za-z0-9_)?])\.secrets([^A-Za-z0-9_]|$)|SECRETS_DIR|secrets_dir'
# Directory names the plain-file scan never enters, at any depth (logs, caches, transcripts, big runtime trees).
SKIP_DIRS="lanes node_modules .git .venv venv projects sessions logs file-history shell-snapshots todos transcripts telemetry backups worktrees cache _archive _retired .pm2 fleet-rag st-mm-postmortem scratch retired plugins sqlite"
# Vendored trees inside a scanned project:  a program inside one still belongs to the project that is scanned.
VENDOR_DIRS="node_modules .git .venv venv"
# Where the plain-file scan looks.
MAC_ROOTS=("$HOME_DIR/apps" "$HOME_DIR/bin" "$HOME_DIR/.local/bin" "$HOME_DIR/.local/libexec" \
  "$HOME_DIR/Library/LaunchAgents" "$HOME_DIR/vision-worker" "$HOME_DIR/.agent-sync" "$HOME_DIR/.claude" \
  "$HOME_DIR/.codex" "$HOME_DIR/.cursor" "$HOME_DIR/.config" "$HOME_DIR/.claude-merge-shepherd" \
  "$HOME_DIR/Library/Application Support/ShellularWatchdog" \
  "$HOME_DIR/.zshrc" "$HOME_DIR/.zprofile" "$HOME_DIR/.zshenv" "$HOME_DIR/.bash_profile" "$HOME_DIR/.bashrc")

say() { printf '%s\n' "$*"; }
die() { printf 'retire-secrets-dir: %s\n' "$*" >&2; exit 1; }

# The allowlist is joined into one regex once, so a hit costs no process.
ALLOW_RE=""
if [ -f "$ALLOW" ]; then
  while IFS= read -r _re; do
    case "$_re" in ''|'#'*) continue ;; esac
    ALLOW_RE="${ALLOW_RE:+$ALLOW_RE|}($_re)"
  done < "$ALLOW"
fi
if [ -n "$ALLOW_RE" ]; then
  [[ x =~ $ALLOW_RE ]]
  [ $? -ne 2 ] || die "an allowlist line in $ALLOW is not a valid extended regex"
fi

allowed() { # $1 = "<where>:<path>";  true when an allowlist regex matches
  [ -n "$ALLOW_RE" ] || return 1
  [[ $1 =~ $ALLOW_RE ]]
}

# Documentation and records (prose, not code):  counted and listed on request (--docs), never fail the check.
is_doc() { case "$1" in *.md|*.mdx|*.mdc|*.txt|*.ics|*.html|*EFFORT-LOG*|*/docs/*|docs/*) return 0 ;; *) return 1 ;; esac; }

# Print "<where>:<path>:<line>" for every non-allowlisted hit.  Hits only:  matched text is never printed.
scan_repos() {
  local d ref where
  [ -d "$CODE_DIR" ] || return 0
  for d in "$CODE_DIR"/*/; do
    d="${d%/}"
    [ -e "$d/.git" ] || continue
    [ -L "$d" ] && continue # a symlink alias of another repo
    where="$(basename "$d")"
    if git -C "$d" rev-parse --verify -q origin/main >/dev/null 2>&1; then ref="origin/main"; else ref="HEAD"; fi
    git -C "$d" grep -n -I -E -e "$PATTERN" "$ref" -- . 2>/dev/null | while IFS=: read -r _ path line _; do
      [ -n "$path" ] || continue
      if allowed "$where:$path"; then continue; fi
      if is_doc "$path"; then printf 'doc:%s:%s:%s\n' "$where" "$path" "$line"; else printf '%s:%s:%s\n' "$where" "$path" "$line"; fi
    done
  done
}

# The top-level ~/apps directories that are git checkouts (a .git file or directory).  Task worktrees and retired
# trees are not listed:  their code is covered through origin/main.
list_checkouts() {
  local d
  for d in "$HOME_DIR"/apps/*/; do
    d="${d%/}"
    case "$(basename "$d")" in lanes|_retired|_archive) continue ;; esac
    if [ -e "$d/.git" ]; then printf '%s\n' "$d"; fi
  done
}

scan_mac() {
  local root f line name g
  local common=() apps_skip=()
  for g in $SKIP_DIRS; do common+=("--exclude-dir=$g"); done
  # Git checkouts under ~/apps are handled by scan_checkouts (git grep), so the plain scan of ~/apps steps over them.
  while IFS= read -r g; do name="$(basename "$g")"; apps_skip+=("--exclude-dir=$name"); done < <(list_checkouts)
  for root in "${MAC_ROOTS[@]}"; do
    [ -e "$root" ] || continue
    if [ "$root" = "$HOME_DIR/apps" ]; then
      grep -rIn -E ${apps_skip[@]+"${apps_skip[@]}"} "${common[@]}" \
        --exclude='*.log' --exclude='*.jsonl' --exclude='*.jsonl.*' --exclude='*.bak*' --exclude='*bak-afc359*' \
        -e "$PATTERN" "$root" 2>/dev/null
    else
      grep -rIn -E "${common[@]}" \
        --exclude='*.log' --exclude='*.jsonl' --exclude='*.jsonl.*' --exclude='*.bak*' --exclude='*bak-afc359*' \
        -e "$PATTERN" "$root" 2>/dev/null
    fi | while IFS=: read -r f line _; do
        if allowed "mac:$f"; then continue; fi
        if is_doc "$f"; then printf 'doc:mac:%s:%s\n' "$f" "$line"; else printf 'mac:%s:%s\n' "$f" "$line"; fi
      done
  done
  # crontab entries
  if crontab -l 2>/dev/null | grep -Eq -e "$PATTERN"; then printf 'mac:crontab:-\n'; fi
}

# Sets CK_REPO, CK_HEAD, CK_DIRTY (count of changed and untracked paths) and CK_REL (HEAD against origin/main of the
# SAME repo in $CODE_DIR:  equals-origin-main, behind-main, ahead-of-main, diverged, unknown-main when the checkout
# lacks that object, or no-origin-main).
checkout_state() { # $1 = checkout dir
  local d="$1" url main r
  CK_REPO=""; CK_HEAD=""; CK_DIRTY=0; CK_REL="no-origin-main"
  url="$(git -C "$d" remote get-url origin 2>/dev/null </dev/null)"
  CK_REPO="$(basename "${url:-none}" .git)"
  CK_HEAD="$(git -C "$d" rev-parse -q --verify HEAD 2>/dev/null </dev/null)"
  CK_DIRTY="$(git -C "$d" status --porcelain 2>/dev/null </dev/null | wc -l | tr -d ' ')"
  main=""
  if [ -n "$url" ]; then
    # Repo folder names under ~/Code and remote names differ in case;  match them case-blind.
    for r in "$CODE_DIR"/*/; do
      r="${r%/}"
      if [ "$(basename "$r" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$CK_REPO" | tr '[:upper:]' '[:lower:]')" ]; then
        main="$(git -C "$r" rev-parse -q --verify origin/main 2>/dev/null </dev/null)"
        break
      fi
    done
  fi
  if [ -z "$main" ] || [ -z "$CK_HEAD" ]; then CK_REL="no-origin-main"
  elif [ "$main" = "$CK_HEAD" ]; then CK_REL="equals-origin-main"
  elif ! git -C "$d" cat-file -e "$main^{commit}" 2>/dev/null </dev/null; then CK_REL="unknown-main"
  elif git -C "$d" merge-base --is-ancestor "$CK_HEAD" "$main" 2>/dev/null </dev/null; then CK_REL="behind-main"
  elif git -C "$d" merge-base --is-ancestor "$main" "$CK_HEAD" 2>/dev/null </dev/null; then CK_REL="ahead-of-main"
  else CK_REL="diverged"; fi
}

# True when a LOADED LaunchAgent plist or a pm2 job runs something inside the checkout (program path, WorkingDirectory or
# pm_cwd, from $PROG_RAW) or its directory is on PATH.  Such a checkout is a RUNTIME checkout:  reader hits fail the
# check.  Any other checkout is an idle copy of someone's work.
is_runtime_checkout() { # $1 = checkout dir
  local d="$1" p r pe
  local l
  while IFS=$'\t' read -r l p; do
    [ -n "$p" ] || continue
    case "$p" in
      "$d"|"$d/"*)
        # A pm2 job counts as running.  A LaunchAgent counts only while it is loaded:  one that is disabled and booted
        # out (BotFleet is deliberately off) runs nothing, so its checkout is an idle copy.
        case "$l" in pm2/*) return 0 ;; esac
        "$LAUNCHCTL" print "gui/$UID_NUM/$l" >/dev/null 2>&1 && return 0 ;;
    esac
  done <<< "$PROG_RAW"
  IFS=: read -r -a pe <<< "${RETIRE_PATH:-$PATH}"
  for r in ${pe[@]+"${pe[@]}"}; do
    case "$r" in "$d"|"$d/"*) return 0 ;; esac
  done
  return 1
}

# Every git checkout directly under ~/apps is either covered (clean and HEAD equals origin/main of its repo, which
# scan_repos reads) or scanned here:  tracked files as they are in the working tree plus untracked files git does not
# ignore.  Prints "@status:..." lines (shown in their own section) and hits:  "checkout:<dir>:<path>:<line>" for a
# runtime checkout (fails the check), "idle-hit:<dir>:<path>:<line>" for an idle one (a warning).
scan_checkouts() {
  local d name path line state kind rt label p rel n hits
  while IFS= read -r d; do
    name="$(basename "$d")"
    checkout_state "$d"
    rt=0; is_runtime_checkout "$d" && rt=1
    state="clean"; [ "$CK_DIRTY" -eq 0 ] || state="dirty=$CK_DIRTY"
    hits=""
    if [ "$CK_DIRTY" -eq 0 ] && [ "$CK_REL" = "equals-origin-main" ]; then
      printf '@status:covered:%s:%s:clean:equals-origin-main%s\n' "$name" "$CK_REPO" "$([ "$rt" -eq 1 ] && printf ':runtime')"
    else
      if [ "$rt" -eq 1 ]; then kind="checkout"; printf '@status:scanned:%s:%s:%s:%s:runtime\n' "$name" "$CK_REPO" "$state" "$CK_REL"
      else kind="idle-hit"; printf '@status:idle:%s:%s:%s:%s\n' "$name" "$CK_REPO" "$state" "$CK_REL"; fi
      hits="$(git -C "$d" grep -n -I -E --untracked -e "$PATTERN" -- . 2>/dev/null </dev/null | while IFS=: read -r path line _; do
        [ -n "$path" ] || continue
        if allowed "checkout:$name:$path"; then continue; fi
        printf '%s\t%s\n' "$path" "$line"
      done)"
      while IFS=$'\t' read -r path line; do
        [ -n "$path" ] || continue
        if is_doc "$path"; then printf 'doc:%s:%s:%s:%s\n' "$kind" "$name" "$path" "$line"; else printf '%s:%s:%s:%s\n' "$kind" "$name" "$path" "$line"; fi
      done <<< "$hits"
    fi
    # For a runtime checkout, list what the jobs run from it and whether those files hold a reader.
    if [ "$rt" -eq 1 ]; then
      while IFS=$'\t' read -r label p; do
        case "$p" in "$d"|"$d/"*) ;; *) continue ;; esac
        rel="${p#"$d"}"; rel="${rel#/}"
        if [ -f "$p" ]; then
          n="$(printf '%s\n' "$hits" | awk -F'\t' -v r="$rel" '$1 == r' | wc -l | tr -d ' ')"
          if [ "$n" -eq 0 ]; then printf '@status:runs:%s:%s:%s:clean\n' "$name" "$label" "$rel"
          else printf '@status:runs:%s:%s:%s:reader-hits=%s\n' "$name" "$label" "$rel" "$n"; fi
        else
          printf '@status:runs:%s:%s:cwd=%s\n' "$name" "$label" "${rel:-.}"
        fi
      done <<< "$PROG_RAW"
    fi
  done < <(list_checkouts)
}

# Symlinks anywhere under $HOME (outside the folder) that point into it:  they would dangle after the archive.
scan_symlinks() {
  local root roots=("$HOME_DIR/.appstoreconnect" "$HOME_DIR/.local" "$HOME_DIR/bin" "$HOME_DIR/.agent-sync" "$HOME_DIR/.config" \
    "$HOME_DIR/.claude" "$HOME_DIR/.codex" "$HOME_DIR/.cursor" "$HOME_DIR/.ssh" "$HOME_DIR/Library/LaunchAgents")
  for root in "${roots[@]}"; do
    [ -e "$root" ] || continue
    find "$root" -maxdepth 6 \( -path '*/node_modules' -o -path '*/.git' -o -path '*/projects' -o -path '*/sessions' \) -prune \
      -o -type l \( -lname "$SECRETS" -o -lname "$SECRETS/*" -o -lname '*/.secrets' -o -lname '*/.secrets/*' -o -lname '~/.secrets/*' \) -print 2>/dev/null
  done | while IFS= read -r f; do allowed "link:$f" || printf 'link:%s\n' "$f"; done
  # ~/apps top level only (its lanes and big runtime dirs are not walked)
  find "$HOME_DIR/apps" -maxdepth 2 -type l \( -lname "$SECRETS/*" -o -lname '*/.secrets/*' \) -print 2>/dev/null |
    while IFS= read -r f; do allowed "link:$f" || printf 'link:%s\n' "$f"; done
}

# Program paths:  every file a LaunchAgent or pm2 job runs (and the directory it runs in) must lie inside something
# this check scans, or be a system location.  Python reads the plist JSON (plutil converts it) and the pm2 JSON and
# prints only the label or name and the paths:  never an EnvironmentVariables or env entry.
PY_PLISTS='
import json, os, re, subprocess, sys
d = sys.argv[1]
def found(obj):
    out = []
    cands = []
    if isinstance(obj.get("Program"), str):
        cands.append(obj["Program"])
    cands += [a for a in (obj.get("ProgramArguments") or []) if isinstance(a, str)]
    for a in cands:
        if a.startswith("/") and os.path.isfile(a):
            out.append(a)
        elif re.search(r"\s", a):
            for t in re.findall(r"/[^\s\"\x27;|&()<>$`]+", a):
                if os.path.isfile(t):
                    out.append(t)
    wd = obj.get("WorkingDirectory")
    if isinstance(wd, str) and os.path.isdir(wd):
        out.append(wd)
    more = [os.path.realpath(x) for x in out if os.path.islink(x)]
    return out + more
for name in sorted(os.listdir(d)):
    if not name.endswith(".plist"):
        continue
    f = os.path.join(d, name)
    try:
        r = subprocess.run(["plutil", "-convert", "json", "-o", "-", f], capture_output=True, text=True, check=False)
        obj = json.loads(r.stdout)
    except Exception:
        continue
    label = str(obj.get("Label") or name[:-6])
    for x in sorted(set(found(obj))):
        print(label + "\t" + x)
'
PY_PM2='
import json, re, sys
s = sys.stdin.read()
m = re.search(r"\[\s*(\{|\])", s)
if not m:
    sys.exit(0)
procs, _ = json.JSONDecoder().raw_decode(s[m.start():])
for p in procs:
    e = p.get("pm2_env") or {}
    name = str(p.get("name") or e.get("name") or "?")
    for key in ("pm_exec_path", "pm_cwd"):
        v = e.get(key) or p.get(key)
        if isinstance(v, str) and v.startswith("/"):
            print("pm2/" + name + "\t" + v)
'

# True when a directory name on the way (path relative to a scan root) is one the plain-file scan skips.
skipped_component() {
  local rest="$1" part g vendor
  while [ -n "$rest" ]; do
    part="${rest%%/*}"
    case "$rest" in */*) rest="${rest#*/}" ;; *) rest="" ;; esac
    vendor=0
    for g in $VENDOR_DIRS; do [ "$part" = "$g" ] && vendor=1; done
    [ "$vendor" -eq 1 ] && continue
    for g in $SKIP_DIRS; do [ "$part" = "$g" ] && return 0; done
  done
  return 1
}

# Prints one word for an absolute program path:  scanned, scanned-checkout, covered-repo, covered-managed, cwd-only,
# UNSCANNED, or nothing for a system location (or a vendor app under ~/Applications).
program_verdict() { # $1 = absolute path
  local p="$1" rel top root
  case "$p" in
    # A working directory that only holds other work (the home folder, the folder of task worktrees) is not code.
    "$HOME_DIR"|"$HOME_DIR/apps/lanes") echo "cwd-only"; return ;;
    "$HOME_DIR/apps/lanes/_managed"|"$HOME_DIR/apps/lanes/_managed/"*) echo "covered-managed"; return ;;
    "$HOME_DIR/apps/lanes"|"$HOME_DIR/apps/lanes/"*|"$HOME_DIR/apps/_retired"|"$HOME_DIR/apps/_retired/"*| \
    "$HOME_DIR/apps/_archive"|"$HOME_DIR/apps/_archive/"*|/Volumes/External/Lanes|/Volumes/External/Lanes/*) echo "UNSCANNED"; return ;;
    "$HOME_DIR/Applications"|"$HOME_DIR/Applications/"*|"$HOME_DIR/Library/Application Support/Google"|"$HOME_DIR/Library/Application Support/Google/"*) return ;;
    "$CODE_DIR/"*)
      rel="${p#"$CODE_DIR"/}"; top="${rel%%/*}"
      if [ -e "$CODE_DIR/$top/.git" ]; then echo "covered-repo"; else echo "UNSCANNED"; fi
      return ;;
    "$HOME_DIR/apps/"*)
      rel="${p#"$HOME_DIR"/apps/}"; top="${rel%%/*}"
      if [ -e "$HOME_DIR/apps/$top/.git" ]; then echo "scanned-checkout"; return; fi
      if skipped_component "$rel"; then echo "UNSCANNED"; else echo "scanned"; fi
      return ;;
  esac
  for root in "${MAC_ROOTS[@]}"; do
    case "$p" in
      "$root") echo "scanned"; return ;;
      "$root/"*) if skipped_component "${p#"$root"/}"; then echo "UNSCANNED"; else echo "scanned"; fi; return ;;
    esac
  done
  case "$p" in
    "$HOME_DIR"|"$HOME_DIR/"*) echo "UNSCANNED"; return ;;
    /usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/opt/homebrew|/opt/homebrew/*|/System|/System/*|/Library|/Library/*|/Applications|/Applications/*| \
    /private/var|/private/var/*|/private/etc|/private/etc/*|/var|/var/*|/etc|/etc/*|/dev|/dev/*) return ;;
  esac
  echo "UNSCANNED"
}

# "<label><TAB><path>" lines from the LaunchAgents and from pm2 (also kept in $PROG_RAW for the checkout scan).
collect_programs() {
  local pm2_bin have_plutil=0 have_pm2=0
  command -v plutil >/dev/null 2>&1 && have_plutil=1
  pm2_bin="${PM2_CMD%% *}"
  command -v "$pm2_bin" >/dev/null 2>&1 && have_pm2=1
  {
    if [ -d "$PLIST_DIR" ] && [ "$have_plutil" -eq 1 ]; then python3 -I -c "$PY_PLISTS" "$PLIST_DIR" 2>/dev/null; fi
    if [ "$have_pm2" -eq 1 ]; then bash -c "$PM2_CMD" 2>/dev/null </dev/null | python3 -I -c "$PY_PM2" 2>/dev/null; fi
  } | sort -u
}

# Classifies the paths in $PROG_RAW;  prints "<verdict>:<label>:<path>" (and "note:" lines).
scan_programs() {
  local label p v
  command -v plutil >/dev/null 2>&1 || { [ -d "$PLIST_DIR" ] && say "note:plutil not found;  LaunchAgent program paths not checked"; }
  command -v "${PM2_CMD%% *}" >/dev/null 2>&1 || say "note:pm2 not found;  pm2 program paths not checked"
  while IFS=$'\t' read -r label p; do
    [ -n "$p" ] || continue
    v="$(program_verdict "$p")"
    [ -n "$v" ] || continue
    [ "$v" = "scanned-checkout" ] && v="scanned"
    printf '%s:%s:%s\n' "$v" "$label" "$p"
  done <<< "$PROG_RAW"
}

scan_open_files() { # "<pid> <command>" per process that holds a file open under ~/.secrets
  [ -d "$SECRETS" ] || return 0
  "$LSOF" -F pc +D "$SECRETS" 2>/dev/null | awk '/^p/ {pid=substr($0,2)} /^c/ {print pid, substr($0,2)}' | sort -u
}

writer_loaded() { "$LAUNCHCTL" print "gui/$UID_NUM/$WRITER_LABEL" >/dev/null 2>&1; }

cmd_check() {
  local n=0 hits open all docs d status progs u c k i idle idle_n idle_c show_docs=0 show_idle=0 arg
  for arg in "$@"; do
    case "$arg" in --docs) show_docs=1 ;; --idle) show_idle=1 ;; esac
  done
  [ -d "$SECRETS" ] || say "note:  $SECRETS does not exist (already archived or renamed)"
  # What the LaunchAgents and pm2 run decides which checkouts are runtime and which are idle copies.
  PROG_RAW="$(collect_programs)"
  say "== readers in repos (origin/main), git checkouts under ~/apps and files on this Mac, and symlinks into the folder, minus the allowlist ($ALLOW)"
  all="$( { scan_repos; scan_checkouts; scan_mac; scan_symlinks; } | sort -u)"
  status="$(printf '%s\n' "$all" | grep '^@status:' | sed 's/^@status://')"
  hits="$(printf '%s\n' "$all" | grep -v -e '^doc:' -e '^@status:' -e '^idle-hit:' | grep -v '^$')"
  idle="$(printf '%s\n' "$all" | grep '^idle-hit:')"
  docs="$(printf '%s\n' "$all" | grep '^doc:')"
  if [ -n "$hits" ]; then printf '%s\n' "$hits"; n=$(printf '%s\n' "$hits" | wc -l | tr -d ' '); fi
  say "readers left (code, config, symlinks): $n"
  d=0
  [ -n "$docs" ] && d=$(printf '%s\n' "$docs" | awk -F: '{ if ($2 == "checkout" || $2 == "idle-hit") print $2 ":" $3 ":" $4; else print $2 ":" $3 }' | sort -u | wc -l | tr -d ' ')
  say "documents that still mention the folder: $d files (informational;  run with --docs to list them)"
  if [ "$show_docs" -eq 1 ] && [ -n "$docs" ]; then printf '%s\n' "$docs"; fi
  idle_n=0; [ -n "$idle" ] && idle_n=$(printf '%s\n' "$idle" | wc -l | tr -d ' ')
  idle_c=0; [ -n "$status" ] && idle_c=$(printf '%s\n' "$status" | grep -c '^idle:')
  say "WARN idle copies (no LaunchAgent, pm2 job or PATH entry uses them):  $idle_c checkouts, $idle_n reader hits (does not fail the check;  run with --idle to list the hits)"
  if [ "$show_idle" -eq 1 ] && [ -n "$idle" ]; then printf '%s\n' "$idle"; fi
  say "== git checkouts under ~/apps (lanes, _retired and _archive are skipped;  covered = clean and equal to origin/main of its repo;  runtime = a LaunchAgent, pm2 job or PATH entry uses it;  idle = nothing does)"
  if [ -n "$status" ]; then printf '%s\n' "$status"; fi
  c=0; k=0; i=0
  [ -n "$status" ] && c=$(printf '%s\n' "$status" | grep -c '^covered:')
  [ -n "$status" ] && k=$(printf '%s\n' "$status" | grep -c '^scanned:')
  [ -n "$status" ] && i=$(printf '%s\n' "$status" | grep -c '^idle:')
  say "checkouts: $c covered, $k runtime and scanned, $i idle"
  say "== program paths (LaunchAgents in $PLIST_DIR, and pm2)"
  progs="$(scan_programs)"
  if [ -n "$progs" ]; then printf '%s\n' "$progs"; fi
  u=0; [ -n "$progs" ] && u=$(printf '%s\n' "$progs" | grep -c '^UNSCANNED:')
  say "unscanned program paths: $u"
  say "== processes holding a file open under $SECRETS"
  open="$(scan_open_files)"
  if [ -n "$open" ]; then printf '%s\n' "$open"; fi
  local o=0; [ -n "$open" ] && o=$(printf '%s\n' "$open" | wc -l | tr -d ' ')
  say "open-file holders: $o"
  say "== writer"
  if writer_loaded; then say "$WRITER_LABEL is LOADED  (run: retire-secrets-dir.sh bootout)"; else say "$WRITER_LABEL is not loaded"; fi
  local w=0
  if writer_loaded; then w=1; say "FAIL:  the writer $WRITER_LABEL is still loaded;  it would recreate the folder and refill it with plaintext"; fi
  if [ "$n" -eq 0 ] && [ "$o" -eq 0 ] && [ "$u" -eq 0 ] && [ "$w" -eq 0 ]; then say "CHECK PASSED"; return 0; fi
  say "CHECK FAILED"
  return 1
}

has_writer_files() { [ -f "$WRITER_PLIST" ] || [ -f "$WRITER_SCRIPT" ] || [ -d "$SEEDER_DIR" ]; }

cmd_bootout() {
  local dest="$RETIRED_DIR/infisical-secrets-sync-$STAMP" note
  if ! writer_loaded && ! has_writer_files; then
    say "$WRITER_LABEL is already retired (not loaded, no plist, script or seeder left to move);  nothing to do."
    return 0
  fi
  if writer_loaded; then
    "$LAUNCHCTL" bootout "gui/$UID_NUM/$WRITER_LABEL" || die "bootout failed"
  else
    say "$WRITER_LABEL was not loaded"
  fi
  "$LAUNCHCTL" disable "gui/$UID_NUM/$WRITER_LABEL" || die "disable failed"
  if writer_loaded; then die "$WRITER_LABEL is still loaded"; fi
  # Retire the writer's files (moved, never deleted) so nothing reinstalls or reruns it.  The directory exists only
  # when there is something to put in it.
  if has_writer_files; then
    mkdir -p "$dest"
    note="$dest/ROLLBACK.txt"
    : > "$note"
    if [ -f "$WRITER_PLIST" ]; then mv "$WRITER_PLIST" "$dest/" && printf 'mv %s/%s.plist %s\n' "$dest" "$WRITER_LABEL" "$WRITER_PLIST" >> "$note"; fi
    if [ -f "$WRITER_SCRIPT" ]; then mv "$WRITER_SCRIPT" "$dest/" && printf 'mv %s/infisical-secrets-sync.sh %s\n' "$dest" "$WRITER_SCRIPT" >> "$note"; fi
    if [ -d "$SEEDER_DIR" ]; then mv "$SEEDER_DIR" "$dest/" && printf 'mv %s/infisical-sync %s\n' "$dest" "$SEEDER_DIR" >> "$note"; fi
    printf 'launchctl enable gui/%s/%s && launchctl bootstrap gui/%s %s\n' "$UID_NUM" "$WRITER_LABEL" "$UID_NUM" "$WRITER_PLIST" >> "$note"
    say "$WRITER_LABEL booted out, disabled and retired (plist, script and the one-time seeder moved to $dest)."
    say "Rollback:  retire-secrets-dir.sh rollback   (or run the lines in $note)"
  else
    say "$WRITER_LABEL booted out and disabled (its files were already moved)."
  fi
}

cmd_archive() {
  local rename=0 pw pw2 out vol mnt diffs drc n_src n_arc
  [ "${1:-}" = "--rename" ] && rename=1
  [ -d "$SECRETS" ] || die "$SECRETS does not exist"
  if writer_loaded; then die "$WRITER_LABEL is still loaded and writes into the folder:  run bootout first"; fi
  cmd_check >/dev/null || die "check does not pass:  run 'retire-secrets-dir.sh check' and fix what it lists"
  mkdir -p "$ARCHIVE_DIR"
  out="$ARCHIVE_DIR/secrets-archive-$STAMP.dmg"
  [ -e "$out" ] && die "$out already exists"
  printf 'Passphrase for the encrypted archive (not shown):  ' > "$TTY_OUT"
  IFS= read -rs pw < "$TTY_IN"; printf '\n' > "$TTY_OUT"
  printf 'Again:  ' > "$TTY_OUT"
  IFS= read -rs pw2 < "$TTY_IN"; printf '\n' > "$TTY_OUT"
  [ -n "$pw" ] && [ "$pw" = "$pw2" ] || die "empty or different passphrases"
  vol="secrets-archive-$STAMP"
  printf '%s' "$pw" | "$HDIUTIL" create -encryption AES-256 -stdinpass -srcfolder "$SECRETS" -volname "$vol" \
    -fs APFS -format UDZO -quiet "$out" || die "hdiutil create failed"
  chmod 600 "$out"
  # Verify:  mount read-only, prove the mount is there and readable, then compare bytes (diff -rq) and the file count.
  # Any trouble fails the archive and leaves the folder untouched.  diff's stderr is NOT hidden, and its exit status is
  # used:  0 identical, 1 differences, 2 trouble (an unreadable file, a missing path).
  printf '%s' "$pw" | "$HDIUTIL" attach "$out" -stdinpass -readonly -nobrowse -quiet || die "could not mount the archive to verify it"
  mnt="$VOLUMES_DIR/$vol"
  pw=""; pw2=""
  if [ ! -d "$mnt" ] || [ ! -r "$mnt" ]; then
    "$HDIUTIL" detach "$mnt" -quiet >/dev/null 2>&1 || true
    die "the archive mounted but $mnt is not a readable directory;  nothing was renamed"
  fi
  diffs="$(diff -rq -x .fseventsd -x .Spotlight-V100 -x .Trashes "$SECRETS" "$mnt")"
  drc=$?
  n_src="$(find "$SECRETS" -type f | wc -l | tr -d ' ')"
  n_arc="$(find "$mnt" -type f -not -path '*/.fseventsd/*' -not -path '*/.Spotlight-V100/*' -not -path '*/.Trashes/*' | wc -l | tr -d ' ')"
  "$HDIUTIL" detach "$mnt" -quiet >/dev/null 2>&1 || true
  if [ "$drc" -ne 0 ]; then
    [ -n "$diffs" ] && printf '%s\n' "$diffs" | awk 'NR <= 20'
    die "comparing the archive with the folder failed (diff exit $drc: 1 = differences, 2 = trouble);  nothing was renamed"
  fi
  if [ "$n_src" != "$n_arc" ]; then
    die "file count differs: folder $n_src, archive $n_arc;  nothing was renamed"
  fi
  say "verified:  $n_src files, byte-for-byte identical"
  shasum -a 256 "$out" > "$out.sha256"
  say "archive verified:  $out  (AES-256;  sha256 in $out.sha256)"
  if [ "$rename" -eq 1 ]; then
    mv "$SECRETS" "$HOME_DIR/.secrets.retired-$STAMP" || die "rename failed"
    say "renamed $SECRETS -> $HOME_DIR/.secrets.retired-$STAMP  (reversible:  retire-secrets-dir.sh rollback)"
  fi
  say ""
  say "When you are satisfied (a day with the folder renamed is a good canary), YOU delete it.  This script never does:"
  say "  rm -rf \"$HOME_DIR/.secrets.retired-$STAMP\"     # if you used --rename"
  say "  rm -rf \"$SECRETS\"                              # if you did not"
}

cmd_rollback() {
  local r wd d
  r="$(ls -d "$HOME_DIR"/.secrets.retired-* 2>/dev/null | sort -r | sed -n 1p)"
  if [ -n "$r" ] && [ -e "$SECRETS" ]; then
    # Both a renamed folder and a live one exist:  do not guess, and do NOT re-enable the writer (it would write into a
    # folder that may be the wrong one).
    say "NOT rolled back:  $SECRETS exists AND $r exists."
    say "Merge by hand (compare them, move what you need into $SECRETS), or move one aside, then run rollback again."
    say "The writer was left as it is."
    return 1
  fi
  if [ -n "$r" ]; then
    mv "$r" "$SECRETS" || die "could not rename $r back"
    say "restored $r -> $SECRETS"
  else
    say "no renamed folder to restore"
  fi
  # The newest retired-writer directory that actually holds one of its files (an empty one is skipped).
  wd=""
  for d in $(ls -d "$RETIRED_DIR"/infisical-secrets-sync-* 2>/dev/null | sort -r); do
    if [ -f "$d/$WRITER_LABEL.plist" ] || [ -f "$d/infisical-secrets-sync.sh" ] || [ -d "$d/infisical-sync" ]; then wd="$d"; break; fi
  done
  if [ -n "$wd" ]; then
    [ -f "$wd/$WRITER_LABEL.plist" ] && [ ! -e "$WRITER_PLIST" ] && mv "$wd/$WRITER_LABEL.plist" "$WRITER_PLIST"
    [ -f "$wd/infisical-secrets-sync.sh" ] && [ ! -e "$WRITER_SCRIPT" ] && mv "$wd/infisical-secrets-sync.sh" "$WRITER_SCRIPT"
    [ -d "$wd/infisical-sync" ] && [ ! -e "$SEEDER_DIR" ] && mv "$wd/infisical-sync" "$SEEDER_DIR"
  fi
  "$LAUNCHCTL" enable "gui/$UID_NUM/$WRITER_LABEL" 2>/dev/null || true
  if [ -f "$WRITER_PLIST" ]; then "$LAUNCHCTL" bootstrap "gui/$UID_NUM" "$WRITER_PLIST" 2>/dev/null || true; fi
  say "writer re-enabled (if its plist is installed)"
}

case "${1:-}" in
  check) shift; cmd_check "$@" ;;
  bootout) cmd_bootout ;;
  archive) shift; cmd_archive "$@" ;;
  rollback) cmd_rollback; exit $? ;;
  *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
