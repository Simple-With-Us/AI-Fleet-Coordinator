#!/usr/bin/env bash
# Install the agent-sync CLI for the current user.  Idempotent.
#
#   scripts/agent_sync/install.sh [--dry-run] [CHECKOUT]
#
# Symlinks ~/.local/bin/agent-sync to CHECKOUT/scripts/agent-sync and creates ~/.agent-sync
# (mode 700, the state directory).  CHECKOUT defaults to /Users/jay/Code/AI-Fleet-Coordinator,
# the integration tree that tracks origin/main.  With --dry-run it only prints what it would do.
#
# It touches nothing else: no pm2, no LaunchAgents, no Slack files, nothing under ~/.secrets.
# Credentials are the owner's job; see README.md for the zuliprc location and the file mode.
set -euo pipefail

DEFAULT_CHECKOUT="/Users/jay/Code/AI-Fleet-Coordinator"
dry_run=0
checkout=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
    *) if [ -n "$checkout" ]; then echo "install.sh: only one checkout path is allowed" >&2; exit 2; fi
       checkout="$arg" ;;
  esac
done
checkout="${checkout:-$DEFAULT_CHECKOUT}"

# The link target must be absolute: a relative one resolves against ~/.local/bin and dangles.
# `pwd` (not `pwd -P`) keeps the path as given, so a checkout reached through a symlink stays that path.
if ! resolved="$(cd "$checkout" 2>/dev/null && pwd)"; then
  echo "install.sh: checkout path not found or not a directory: $checkout" >&2
  exit 1
fi
checkout="$resolved"

target="$checkout/scripts/agent-sync"
bin_dir="$HOME/.local/bin"
link="$bin_dir/agent-sync"
state_dir="$HOME/.agent-sync"

if [ ! -f "$target" ]; then
  echo "install.sh: $target does not exist; pass the path of a checkout that has scripts/agent-sync" >&2
  exit 1
fi

say() { if [ "$dry_run" -eq 1 ]; then echo "would: $*"; else echo "$*"; fi; }

# 1. ~/.local/bin
if [ ! -d "$bin_dir" ]; then
  say "create $bin_dir"
  [ "$dry_run" -eq 1 ] || mkdir -p "$bin_dir"
fi

# 2. the symlink (replace only our own link or a stale one; never overwrite a regular file)
if [ -L "$link" ]; then
  current="$(readlink "$link")"
  if [ "$current" = "$target" ]; then
    echo "symlink already correct: $link -> $target"
  else
    say "repoint $link: $current -> $target"
    [ "$dry_run" -eq 1 ] || ln -sfn "$target" "$link"
  fi
elif [ -e "$link" ]; then
  echo "install.sh: $link exists and is not a symlink; move it aside first" >&2
  exit 1
else
  say "link $link -> $target"
  [ "$dry_run" -eq 1 ] || ln -s "$target" "$link"
fi

# 3. the executable bit (git keeps it, but a copied tree may not)
if [ ! -x "$target" ]; then
  say "chmod +x $target"
  [ "$dry_run" -eq 1 ] || chmod +x "$target"
fi

# 4. the state directory
if [ -d "$state_dir" ]; then
  mode="$(stat -c '%a' "$state_dir" 2>/dev/null || stat -f '%Lp' "$state_dir")"
  if [ "$mode" != "700" ]; then
    say "chmod 700 $state_dir (was $mode)"
    [ "$dry_run" -eq 1 ] || chmod 700 "$state_dir"
  else
    echo "state directory already present: $state_dir (mode 700)"
  fi
else
  say "create $state_dir (mode 700)"
  if [ "$dry_run" -eq 0 ]; then mkdir -p "$state_dir"; chmod 700 "$state_dir"; fi
fi

# 5. PATH hint
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "note: $bin_dir is not on PATH; add it to your shell profile" ;;
esac

if [ "$dry_run" -eq 1 ]; then echo "dry run: nothing was changed"; else echo "done: run 'agent-sync --help'"; fi
