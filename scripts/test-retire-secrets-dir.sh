#!/usr/bin/env bash
# Hermetic test of scripts/retire-secrets-dir.sh:  a temp HOME, stub launchctl / lsof / pm2, a throwaway git
# repo as ~/Code/<repo> with clones under ~/apps, and LaunchAgent plists.  No real folder, no network, no secret, no passphrase prompt (archive needs a
# terminal and a real hdiutil;  only its refusal path runs here).
#
#   bash scripts/test-retire-secrets-dir.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/retire-secrets-dir.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0
check() { local name="$1"; shift; if "$@"; then echo "ok   $name"; else echo "FAIL $name"; fail=1; fi; }

export RETIRE_HOME="$tmp/home" RETIRE_CODE_DIR="$tmp/home/Code" RETIRE_ALLOW="$tmp/allow" RETIRE_UID=501 RETIRE_STAMP=T1
mkdir -p "$RETIRE_HOME/.secrets" "$RETIRE_CODE_DIR/demo" "$RETIRE_HOME/apps" "$RETIRE_HOME/Library/LaunchAgents"
printf 'DUMMY=not-a-real-value\n' > "$RETIRE_HOME/.secrets/dummy.env"
printf '^demo:docs/HANDOFF-\n' > "$RETIRE_ALLOW"

cat > "$tmp/launchctl" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LC"
case "$1" in
  print) [ -f "$STUB_LOADED" ] ;;
  bootout) rm -f "$STUB_LOADED" ;;
  *) : ;;
esac
STUB
cat > "$tmp/lsof" <<'STUB'
#!/usr/bin/env bash
[ -f "$STUB_OPEN" ] && printf 'p4242\ncfakeproc\n'
exit 0
STUB
cat > "$tmp/pm2empty" <<'STUB'
#!/usr/bin/env bash
printf '[PM2] Spawning PM2 daemon\n[]\n'
STUB
chmod +x "$tmp/launchctl" "$tmp/lsof"
export RETIRE_PM2="bash $tmp/pm2empty"
export RETIRE_LAUNCHCTL="$tmp/launchctl" RETIRE_LSOF="$tmp/lsof" STUB_LC="$tmp/lc.log" STUB_LOADED="$tmp/loaded" STUB_OPEN="$tmp/open"
touch "$STUB_LOADED"

repo="$RETIRE_CODE_DIR/demo"
git -C "$repo" init -q && git -C "$repo" config user.email t@example.invalid && git -C "$repo" config user.name t
mkdir -p "$repo/docs"
printf 'source ~/.secrets/dummy.env\n' > "$repo/reader.sh"
printf 'history mentions ~/.secrets\n' > "$repo/docs/HANDOFF-old.md"
git -C "$repo" add -A && git -C "$repo" commit -q -m init

out="$(bash "$script" check)"; rc=$?
check "check fails while a reader exists" test "$rc" -ne 0
check "check names the reader file and line, not its text" bash -c "printf '%s' '$out' | grep -q 'demo:reader.sh:1' && ! printf '%s' '$out' | grep -q 'dummy.env'"
check "check skips the allowlisted history doc" bash -c "! printf '%s' '$out' | grep -q 'HANDOFF-old'"
check "check reports the writer as loaded" bash -c "printf '%s' '$out' | grep -q 'is LOADED'"
rm -f "$STUB_LOADED"  # the readers-only tests below run with the writer already stopped

git -C "$repo" rm -q reader.sh && git -C "$repo" commit -q -m clean
printf 'x = 1\n' > "$RETIRE_HOME/apps/ok.py"
out="$(bash "$script" check)"; rc=$?
check "check passes when the readers are gone" test "$rc" -eq 0

printf 'The old folder was ~/.secrets.\n' > "$repo/README.md"
git -C "$repo" add -A && git -C "$repo" commit -q -m doc
out="$(bash "$script" check)"; rc=$?
check "a prose mention in a document is counted but does not fail the check" bash -c "test $rc -eq 0 && printf '%s' '$out' | grep -q 'documents that still mention the folder: 1 files'"

printf 'import os; os.path.join(home, ".secrets")\n' > "$RETIRE_HOME/apps/bad.py"
out="$(bash "$script" check)"; rc=$?
check "check catches a Mac reader outside git" bash -c "test $rc -ne 0 && printf '%s' '$out' | grep -q 'mac:.*apps/bad.py:1'"
rm -f "$RETIRE_HOME/apps/bad.py"
printf 'const s = body.secrets; const t = data?.secrets; x[0].secrets; f().secrets; this.secrets_x = 1\n' > "$RETIRE_HOME/apps/prop.js"
out="$(bash "$script" check)"; rc=$?
check "a property access such as body.secrets is not the folder and does not match" bash -c "test $rc -eq 0 && ! printf '%s' '$out' | grep -q 'prop.js'"
rm -f "$RETIRE_HOME/apps/prop.js"
for form in 'source ~/.secrets/a' 'p="$HOME/.secrets/a"' 'p = "/.secrets"' "p = '.secrets'" 'join(home, ".secrets", "x")' 'Path.home() / ".secrets"' 'SECRETS_DIR=x' 'secrets_dir = 1'; do
  printf '%s\n' "$form" > "$RETIRE_HOME/apps/form.txt.sh"
  out="$(bash "$script" check)"
  check "the pattern matches: $form" bash -c "printf '%s' '$out' | grep -q 'form.txt.sh:1'"
done
rm -f "$RETIRE_HOME/apps/form.txt.sh"

mkdir -p "$RETIRE_HOME/.appstoreconnect/private_keys"
ln -s "$RETIRE_HOME/.secrets/dummy.env" "$RETIRE_HOME/.appstoreconnect/private_keys/AuthKey_X.p8"
out="$(bash "$script" check)"; rc=$?
check "check fails on a symlink that points into the folder and names it" bash -c "test $rc -ne 0 && printf '%s' '$out' | grep -q 'link:.*AuthKey_X.p8'"
rm -f "$RETIRE_HOME/.appstoreconnect/private_keys/AuthKey_X.p8"

touch "$STUB_OPEN"
out="$(bash "$script" check)"; rc=$?
check "check fails while a process holds a file open and names it" bash -c "test $rc -ne 0 && printf '%s' '$out' | grep -q '4242 fakeproc'"
rm -f "$STUB_OPEN"

touch "$STUB_LOADED"
out="$(bash "$script" check)"; rc=$?
check "check FAILS while the writer is loaded, even with no readers left" bash -c "test $rc -ne 0 && printf '%s' '$out' | grep -q 'the writer com.jay.infisical-secrets-sync is still loaded'"
mkdir -p "$RETIRE_HOME/bin" "$RETIRE_HOME/apps/infisical-sync"
printf 'x\n' > "$RETIRE_HOME/Library/LaunchAgents/com.jay.infisical-secrets-sync.plist"
printf 'x\n' > "$RETIRE_HOME/bin/infisical-secrets-sync.sh"
printf 'x\n' > "$RETIRE_HOME/apps/infisical-sync/sync.sh"
check "archive refuses while the writer is loaded" bash -c "bash '$script' archive 2>&1 | grep -q 'still loaded'"
bash "$script" bootout >/dev/null
check "bootout moves the writer's plist, script and seeder to _retired with a rollback note" bash -c "test ! -e '$RETIRE_HOME/bin/infisical-secrets-sync.sh' && test ! -e '$RETIRE_HOME/Library/LaunchAgents/com.jay.infisical-secrets-sync.plist' && test ! -e '$RETIRE_HOME/apps/infisical-sync' && test -f '$RETIRE_HOME'/apps/_retired/infisical-secrets-sync-T1/ROLLBACK.txt && test -f '$RETIRE_HOME'/apps/_retired/infisical-secrets-sync-T1/infisical-secrets-sync.sh"
check "bootout boots out and disables the writer" bash -c "grep -q 'bootout gui/501/com.jay.infisical-secrets-sync' '$STUB_LC' && grep -q 'disable gui/501/com.jay.infisical-secrets-sync' '$STUB_LC'"

mv "$RETIRE_HOME/.secrets" "$RETIRE_HOME/.secrets.retired-T0"
bash "$script" rollback >/dev/null
check "rollback restores the retired folder" test -f "$RETIRE_HOME/.secrets/dummy.env"
check "rollback also restores the writer's files" bash -c "test -f '$RETIRE_HOME/bin/infisical-secrets-sync.sh' && test -f '$RETIRE_HOME/Library/LaunchAgents/com.jay.infisical-secrets-sync.plist' && test -d '$RETIRE_HOME/apps/infisical-sync'"

# ---- git checkouts under ~/apps:  runtime (a plist, pm2 job or PATH entry uses it) versus idle ---------------------
has() { printf '%s\n' "$out" | grep -q -- "$1"; }
hasnt() { ! has "$1"; }
run_check() { out="$(bash "$script" check "$@" 2>/dev/null)"; rc=$?; }
commit_demo() { git -C "$repo" add -A && git -C "$repo" commit -q -m "$1"; }
write_plist() { # $1 = file name, $2 = label, $3 = program path, $4 = optional working directory
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>%s</string>' "$2"
    printf '<key>ProgramArguments</key><array><string>/bin/bash</string><string>%s</string></array>' "$3"
    [ -n "${4:-}" ] && printf '<key>WorkingDirectory</key><string>%s</string>' "$4"
    printf '<key>EnvironmentVariables</key><dict><key>TOKEN</key><string>SHOULD_NOT_PRINT</string></dict></dict></plist>\n'
  } > "$RETIRE_HOME/Library/LaunchAgents/$1"
}

# A:  origin/main holds a reader.  behindco is cloned at A, then B removes it and becomes origin/main.
printf 'source ~/.secrets/dummy.env\n' > "$repo/old-reader.sh"
printf 'echo tracked\n' > "$repo/tracked.sh"
commit_demo A
git clone -q "$repo" "$RETIRE_HOME/apps/behindco"
git -C "$repo" rm -q old-reader.sh && commit_demo B
git -C "$repo" update-ref refs/remotes/origin/main HEAD
git -C "$RETIRE_HOME/apps/behindco" fetch -q origin
git clone -q "$repo" "$RETIRE_HOME/apps/cleanco"
git clone -q "$repo" "$RETIRE_HOME/apps/dirtyco"
printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/dirtyco/tracked.sh"
printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/dirtyco/new.sh"
printf 'const s = body.secrets;\n' > "$RETIRE_HOME/apps/dirtyco/prop.js"
# Task worktrees and retired trees are skipped even with a reader inside (lanes/x is also a git checkout).
mkdir -p "$RETIRE_HOME/apps/lanes/x" "$RETIRE_HOME/apps/_retired/y" "$RETIRE_HOME/apps/_archive/z"
git -C "$RETIRE_HOME/apps/lanes/x" init -q
for f in lanes/x/bad.sh _retired/y/bad.sh _archive/z/bad.sh; do printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/$f"; done
# dirtyco and cleanco are RUNTIME checkouts:  a LaunchAgent runs a file in each.  behindco is idle.
write_plist rt-dirty.plist com.test.rtdirty "$RETIRE_HOME/apps/dirtyco/tracked.sh"
write_plist rt-clean.plist com.test.rtclean "$RETIRE_HOME/apps/cleanco/tracked.sh"

run_check
check "a runtime checkout with a dirty tracked reader is scanned and the reader is named" has 'checkout:dirtyco:tracked.sh:1'
check "an untracked reader in a runtime checkout is named" has 'checkout:dirtyco:new.sh:1'
check "a property access in a checkout does not match" hasnt 'prop.js'
check "the runtime status line carries the dirty count and the runtime marker" has 'scanned:dirtyco:demo:dirty=3:equals-origin-main:runtime'
check "the program file a job runs is listed with its reader hits" has 'runs:dirtyco:com.test.rtdirty:tracked.sh:reader-hits=1'
check "a clean runtime checkout equal to origin/main is covered" has 'covered:cleanco:demo:clean:equals-origin-main:runtime'
check "its program file is listed clean" has 'runs:cleanco:com.test.rtclean:tracked.sh:clean'
check "a covered checkout is not scanned" hasnt 'checkout:cleanco'
check "an idle checkout behind origin/main is reported as idle with its state" has 'idle:behindco:demo:clean:behind-main'
check "idle hits are not listed without --idle" hasnt 'idle-hit:behindco'
check "the idle warning counts the checkout and its hit" has 'WARN idle copies.*1 checkouts, 1 reader hits'
check "an idle checkout is not scanned as runtime" hasnt 'checkout:behindco'
check "lanes, _retired and _archive are skipped" bash -c "! printf '%s' \"\$1\" | grep -q -e 'lanes/x' -e '_retired/y' -e '_archive/z' -e 'checkout:x:'" _ "$out"
check "runtime reader hits fail the check" test "$rc" -ne 0
check "the output holds no line of a file" hasnt 'dummy.env'
run_check --idle
check "--idle lists the idle hits" has 'idle-hit:behindco:old-reader.sh:1'

printf '^checkout:dirtyco:new\\.sh$\n' >> "$RETIRE_ALLOW"
run_check
check "the allowlist key checkout:<dir>:<path> silences a checkout hit" bash -c "! printf '%s' \"\$1\" | grep -q 'new.sh' && printf '%s' \"\$1\" | grep -q 'tracked.sh'" _ "$out"

git -C "$RETIRE_HOME/apps/dirtyco" checkout -q -- tracked.sh
rm -f "$RETIRE_HOME/apps/dirtyco/new.sh" "$RETIRE_HOME/apps/dirtyco/prop.js"
run_check
check "an idle checkout with a reader warns and the check passes" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'CHECK PASSED' && printf '%s' \"\$1\" | grep -q 'WARN idle copies.*1 reader hits'" _ "$out"
RETIRE_PATH="$RETIRE_HOME/apps/behindco:/usr/bin" run_check
check "a checkout on PATH is a runtime checkout and its reader fails the check" bash -c "test $rc -ne 0 && printf '%s' \"\$1\" | grep -q 'checkout:behindco:old-reader.sh:1'" _ "$out"
git -C "$RETIRE_HOME/apps/behindco" merge -q --ff-only "origin/$(git -C "$repo" rev-parse --abbrev-ref HEAD)"
run_check
check "check passes once every checkout holds no reader" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'CHECK PASSED' && printf '%s' \"\$1\" | grep -q 'checkouts: 3 covered, 0 runtime and scanned, 0 idle'" _ "$out"
check "the program paths section is present with nothing unscanned" has 'unscanned program paths: 0'
rm -f "$RETIRE_HOME/Library/LaunchAgents/rt-dirty.plist" "$RETIRE_HOME/Library/LaunchAgents/rt-clean.plist"

# The real allowlist:  every group hides what it should and nothing more.
mkdir -p "$RETIRE_HOME/.cursor/extensions/x" "$RETIRE_HOME/apps/ios-fleet/.backup-x" "$RETIRE_HOME/bin" "$RETIRE_HOME/apps/dirtyco/tests"
for f in .cursor/extensions/x/out.js apps/ios-fleet/.backup-x/a.sh apps/AGENT-SYNC.md.orig apps/AGENT-SYNC.md.pre-sync-1 \
  apps/output.json apps/.monitor-mac/findings.json bin/infisical-secrets-sync.sh apps/upstream/v/a.ts apps/handbook-sync/allowlist.json; do
  mkdir -p "$(dirname "$RETIRE_HOME/$f")"; printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/$f"
done
printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/dirtyco/tests/t.py"
printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/dirtyco/real.py"
printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/real-reader.py"
mkdir -p "$RETIRE_HOME/apps/tests" && printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/apps/tests/mac-test.py"
write_plist rt-dirty.plist com.test.rtdirty "$RETIRE_HOME/apps/dirtyco/real.py"
out="$(RETIRE_ALLOW="$here/retire-secrets-dir.allow" bash "$script" check 2>/dev/null)"; rc=$?
for f in extensions/x/out.js .backup-x/a.sh AGENT-SYNC.md.orig AGENT-SYNC.md.pre-sync-1 apps/output.json .monitor-mac/findings.json upstream/v/a.ts handbook-sync/allowlist.json dirtyco:tests/t.py; do
  check "the real allowlist hides $f" hasnt "$f"
done
mkdir -p "$RETIRE_HOME/bin" && printf 'source ~/.secrets/dummy.env\n' > "$RETIRE_HOME/bin/infisical-secrets-sync.sh"
out2="$(RETIRE_ALLOW="$here/retire-secrets-dir.allow" bash "$script" check 2>/dev/null)"
check "the real allowlist does NOT hide the writer script (it must be retired, not exempted)" bash -c "printf '%s' \"\$1\" | grep -q 'mac:.*bin/infisical-secrets-sync.sh:1'" _ "$out2"
check "the real allowlist hides a test dir in a checkout but not a mac path named tests" bash -c "printf '%s' \"\$1\" | grep -q 'mac:.*apps/tests/mac-test.py:1'" _ "$out"
check "the real allowlist still reports a real reader on the Mac and in a runtime checkout" bash -c "printf '%s' \"\$1\" | grep -q 'mac:.*apps/real-reader.py:1' && printf '%s' \"\$1\" | grep -q 'checkout:dirtyco:real.py:1'" _ "$out"
rm -f "$RETIRE_HOME/Library/LaunchAgents/rt-dirty.plist" "$RETIRE_HOME/apps/dirtyco/real.py" "$RETIRE_HOME/apps/real-reader.py"
rm -rf "$RETIRE_HOME/apps/tests" "$RETIRE_HOME/apps/dirtyco/tests" "$RETIRE_HOME/.cursor" "$RETIRE_HOME/apps/ios-fleet" "$RETIRE_HOME/apps/upstream" "$RETIRE_HOME/apps/handbook-sync" "$RETIRE_HOME/apps/.monitor-mac"
rm -f "$RETIRE_HOME/apps/AGENT-SYNC.md.orig" "$RETIRE_HOME/apps/AGENT-SYNC.md.pre-sync-1" "$RETIRE_HOME/apps/output.json" "$RETIRE_HOME/bin/infisical-secrets-sync.sh"

# LaunchAgent plists.  Environment values carry a canary that must never be printed.
mkdir -p "$RETIRE_HOME/opt/tool" "$RETIRE_HOME/apps/lanes/_managed/AFC"
printf '#!/bin/sh\n' > "$RETIRE_HOME/opt/tool/run.sh"
printf '#!/bin/sh\n' > "$RETIRE_HOME/apps/lanes/_managed/AFC/run.sh"
write_plist outside.plist com.test.outside "$RETIRE_HOME/opt/tool/run.sh"
run_check
check "a LaunchAgent program outside every scanned root is UNSCANNED and fails the check" bash -c "test $rc -ne 0 && printf '%s' \"\$1\" | grep -q 'UNSCANNED:com.test.outside:.*opt/tool/run.sh'" _ "$out"
check "system programs and EnvironmentVariables are not printed" bash -c "! printf '%s' \"\$1\" | grep -q -e '/bin/bash' -e SHOULD_NOT_PRINT" _ "$out"
rm -f "$RETIRE_HOME/Library/LaunchAgents/outside.plist"

write_plist inside.plist com.test.inside "$RETIRE_HOME/apps/ok.py" "$RETIRE_HOME/apps/cleanco"
write_plist managed.plist com.test.managed "$RETIRE_HOME/apps/lanes/_managed/AFC/run.sh"
run_check
check "a program under a scanned root is scanned" has 'scanned:com.test.inside:.*apps/ok.py'
check "a working directory inside a covered checkout is scanned" has 'scanned:com.test.inside:.*apps/cleanco'
check "a program under lanes/_managed is covered-managed" has 'covered-managed:com.test.managed:.*lanes/_managed/AFC/run.sh'
check "scanned and managed programs pass the check" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'CHECK PASSED'" _ "$out"
write_plist lane.plist com.test.lane "$RETIRE_HOME/apps/lanes/x/bad.sh"
run_check
check "a program that runs from a task worktree is UNSCANNED" bash -c "test $rc -ne 0 && printf '%s' \"\$1\" | grep -q 'UNSCANNED:com.test.lane:.*lanes/x/bad.sh'" _ "$out"
rm -f "$RETIRE_HOME/Library/LaunchAgents/lane.plist" "$RETIRE_HOME/Library/LaunchAgents/inside.plist" "$RETIRE_HOME/Library/LaunchAgents/managed.plist"

# A working directory that is only the home folder is not code;  a helper in a scanned root of its own is scanned.
mkdir -p "$RETIRE_HOME/.claude-merge-shepherd"
printf '#!/bin/sh\n' > "$RETIRE_HOME/.claude-merge-shepherd/run.sh"
write_plist cwd.plist com.test.cwd "$RETIRE_HOME/.claude-merge-shepherd/run.sh" "$RETIRE_HOME"
run_check
check "the home folder as a working directory is cwd-only and does not fail" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'cwd-only:com.test.cwd:' && printf '%s' \"\$1\" | grep -q 'scanned:com.test.cwd:.*merge-shepherd/run.sh'" _ "$out"
write_plist cwd2.plist com.test.cwd2 "$RETIRE_HOME/.claude-merge-shepherd/run.sh" "$RETIRE_HOME/apps/lanes"
run_check
check "the folder of task worktrees as a working directory is cwd-only too" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'cwd-only:com.test.cwd2:'" _ "$out"
rm -f "$RETIRE_HOME/Library/LaunchAgents/cwd.plist" "$RETIRE_HOME/Library/LaunchAgents/cwd2.plist"

# pm2 stubs print a banner, then JSON with an env block that must never be printed.
pm2_stub() { # $1 = file, $2 = exec path, $3 = cwd
  printf '#!/usr/bin/env bash\nprintf "[PM2] Spawning PM2 daemon\\n"\nprintf %%s %q\n' \
    "[{\"name\":\"w\",\"pm2_env\":{\"pm_exec_path\":\"$2\",\"pm_cwd\":\"$3\",\"env\":{\"TOKEN\":\"SHOULD_NOT_PRINT\"}}}]" > "$1"
}
pm2_stub "$tmp/pm2in" "$RETIRE_HOME/apps/ok.py" "$RETIRE_HOME/apps"
pm2_stub "$tmp/pm2out" "$RETIRE_HOME/opt/tool/svc.js" "$RETIRE_HOME/opt/tool"
export RETIRE_PM2="bash $tmp/pm2in"
run_check
check "a pm2 process in a scanned dir passes and is listed by name and path" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'scanned:pm2/w:.*apps/ok.py' && ! printf '%s' \"\$1\" | grep -q SHOULD_NOT_PRINT" _ "$out"
export RETIRE_PM2="bash $tmp/pm2out"
run_check
check "a pm2 process outside every scanned root is UNSCANNED and fails" bash -c "test $rc -ne 0 && printf '%s' \"\$1\" | grep -q 'UNSCANNED:pm2/w:.*opt/tool/svc.js'" _ "$out"
export RETIRE_PM2="pm2-not-installed-here"
run_check
check "a missing pm2 is a note and the check carries on" bash -c "test $rc -eq 0 && printf '%s' \"\$1\" | grep -q 'note:pm2 not found'" _ "$out"
export RETIRE_PM2="bash $tmp/pm2empty"


check "the script never deletes (no rm, rmdir, unlink, shred or find -delete)" bash -c "! grep -v '^ *#' '$script' | grep -v 'say \"  rm -rf' | grep -E '(^|[^a-z_])(rm|rmdir|unlink|shred)( |\$)|-delete'"
check "the script prints no file contents (no cat, head or tail)" bash -c "! grep -v '^ *#' '$script' | grep -E '(^|[^a-z])(cat|head|tail)( |\$)'"

exit $fail
