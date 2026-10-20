#!/bin/sh
# Install the open-loops stable copy for the current user.  A plan by default;  --apply does it.  Idempotent.
#
#   scripts/open_loops/install.sh [--apply] [--c2-id ID]
#
# Stable copy:  $HOME/apps/open-loops/ holds
#   open-loops            the entry script          (python3 -I $HOME/apps/open-loops/open-loops --help)
#   open_loops/*.py       this package, without its tests
#   fleet_facts/*.py     the F6 gatherers it imports (without their tests and CI checker)
#   agent_sync/           an empty __init__.py plus the three F3 helpers loaded by path:
#                         textsafe.py (render_untrusted), secretscan.py (secret scan, redact_fields) and
#                         textfmt.py (sentence_gap, the same gap rule agent-sync applies on the way out)
#   c2-id                 only with --c2-id:  the C2 umbrella issue id (such as AFC-123) the heartbeat topic
#                         `<C2 ID> open-loops heartbeat` is named from.  A reinstall keeps the one already there.
# and $HOME/.local/bin/open-loops is a symlink to the entry script.
#
# The entry script's first line is rewritten to `#!<absolute python3> -I` (python 3.11 or newer, preferring
# /opt/homebrew/bin/python3), so a BotFleet routine's short PATH cannot pick an older Python and the link
# always runs isolated.  If no Python 3.11 is found the line is left as it is and a note says so.
#
# It touches nothing else:  no LaunchAgent, no pm2, no network, and no credential files.  Every path is
# built from $HOME, so a test can point HOME at a temp folder.  Run it from a checkout of the repository
# (a lane or the managed runtime checkout), never from the human tree under ~/Code.
set -eu

apply=0
c2=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) apply=1 ;;
    --c2-id)
      [ "$#" -ge 2 ] && [ -n "$2" ] || { echo "install.sh: --c2-id needs an issue id" >&2; exit 2; }
      c2="$2"; shift ;;
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -n "$c2" ]; then
  case "$c2" in
    *[!A-Z0-9-]*) echo "install.sh: --c2-id must look like AFC-123" >&2; exit 2 ;;   # also refuses a newline
  esac
  printf '%s\n' "$c2" | grep -Eq '^[A-Z][A-Z0-9]{0,7}-[0-9]{1,7}$' || { echo "install.sh: --c2-id must look like AFC-123" >&2; exit 2; }
fi

case "${HOME:-}" in
  /?*) ;;
  *) echo "install.sh: HOME must be an absolute path" >&2; exit 1 ;;
esac

here=$(cd "$(dirname "$0")" && pwd -P)
scripts_dir=$(dirname "$here")
dest="$HOME/apps/open-loops"
link_dir="$HOME/.local/bin"
link="$link_dir/open-loops"

for need in "$scripts_dir/open-loops" "$here/cli.py" "$scripts_dir/fleet_facts/gather.py" \
            "$scripts_dir/agent_sync/textsafe.py" "$scripts_dir/agent_sync/secretscan.py" "$scripts_dir/agent_sync/textfmt.py"; do
  [ -f "$need" ] || { echo "install.sh: missing $need (run this from a full checkout)" >&2; exit 1; }
done

say() { if [ "$apply" -eq 1 ]; then echo "$*"; else echo "would: $*"; fi; }

python=""
for candidate in /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    python="$candidate"; break
  fi
done

say "copy the package, the F6 gatherers it imports, the entry script and the three F3 helpers to $dest"
[ -z "$c2" ] || say "write the C2 id $c2 to $dest/c2-id"
if [ -z "$python" ]; then
  echo "note: no Python 3.11 or newer was found;  the entry script keeps '#!/usr/bin/env python3'"
fi
if [ "$apply" -eq 1 ]; then
  mkdir -p "$HOME/apps"
  stage="$HOME/apps/.open-loops.new.$$"
  trap 'rm -rf "$stage"' EXIT
  rm -rf "$stage"
  mkdir -p "$stage/open_loops" "$stage/fleet_facts" "$stage/agent_sync"
  for f in "$here"/*.py; do cp "$f" "$stage/open_loops/"; done
  for f in "$scripts_dir"/fleet_facts/*.py; do
    case "$(basename "$f")" in check_fixtures.py) continue ;; esac   # a CI tool, not part of the runtime
    cp "$f" "$stage/fleet_facts/"
  done
  if [ -n "$python" ]; then
    { printf '#!%s -I\n' "$python"; tail -n +2 "$scripts_dir/open-loops"; } > "$stage/open-loops"
  else
    cp "$scripts_dir/open-loops" "$stage/open-loops"
  fi
  cp "$scripts_dir/agent_sync/textsafe.py" "$scripts_dir/agent_sync/secretscan.py" "$scripts_dir/agent_sync/textfmt.py" "$stage/agent_sync/"
  : > "$stage/agent_sync/__init__.py"
  if [ -n "$c2" ]; then
    printf '%s\n' "$c2" > "$stage/c2-id"
  elif [ -f "$dest/c2-id" ]; then
    cp "$dest/c2-id" "$stage/c2-id"
  fi
  chmod 755 "$stage/open-loops"
  chmod 644 "$stage"/open_loops/*.py "$stage"/fleet_facts/*.py "$stage"/agent_sync/*.py
  [ ! -f "$stage/c2-id" ] || chmod 644 "$stage/c2-id"
  prev="$dest.prev"
  rm -rf "$prev"
  if [ -e "$dest" ]; then mv "$dest" "$prev"; fi
  mv "$stage" "$dest"
  rm -rf "$prev"
  trap - EXIT
fi

if [ -L "$link" ]; then
  if [ "$(readlink "$link")" = "$dest/open-loops" ]; then
    echo "symlink already correct: $link"
  else
    say "repoint $link -> $dest/open-loops"
    [ "$apply" -eq 0 ] || ln -sfn "$dest/open-loops" "$link"
  fi
elif [ -e "$link" ]; then
  echo "install.sh: $link exists and is not a symlink;  move it aside first" >&2
  exit 1
else
  say "link $link -> $dest/open-loops"
  if [ "$apply" -eq 1 ]; then mkdir -p "$link_dir"; ln -s "$dest/open-loops" "$link"; fi
fi

case ":${PATH:-}:" in
  *":$link_dir:"*) ;;
  *) echo "note: $link_dir is not on PATH;  add it to your shell profile" ;;
esac

if [ "$apply" -eq 1 ]; then
  [ -f "$dest/c2-id" ] || echo "note: no C2 id is set;  'open-loops send' refuses until you run this again with --c2-id AFC-123 (or pass --c2-id to send)"
  echo "done:  $dest/open-loops gather --no-mac --json"
else
  echo "plan only:  nothing was changed.  Re-run with --apply to install."
fi
