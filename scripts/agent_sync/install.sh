#!/usr/bin/env bash
# Install the simple-tools CLIs for the current user.  Idempotent.
#
#   scripts/agent_sync/install.sh [--dry-run] [CHECKOUT]
#
# Symlinks ~/.local/bin/simple-chat, ~/.local/bin/simple-notes and ~/.local/bin/simple-board to
# CHECKOUT/scripts/, and creates ~/.agent-sync (mode 700, the state directory).  CHECKOUT defaults
# to the managed runtime checkout
# /Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime,
# a detached worktree on origin/main that LaunchAgent com.jay.agent-sync-runtime-sync keeps current.
# Never point it at the human integration tree ~/Code/AI-Fleet-Coordinator:  a daemon resets that tree,
# and a stuck merge there stops it updating.  With --dry-run it only prints what it would do.
#
# FleetLink's own CLI (fleetlink) ships from the FleetLink repo and is installed by its installer;
# nothing here installs it, and nothing here checks for it.
#
# ~/.local/bin/platform-letter (AFC-564, the session-letter hook and registry side for the platforms other than Claude
# and MiniMax) is linked the same way when the checkout has scripts/platform-letter.  The server gets it from the
# fleet-cli managed wrappers instead (scripts/fleet-cli-release.py).
#
# It touches nothing else: no pm2, no LaunchAgents, no Slack files, no credential files.
# Credentials come from Infisical Fleet-OPS /zulip through fleet-secrets; see README.md (AFC-359).
set -euo pipefail

DEFAULT_CHECKOUT="/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime"
dry_run=0
checkout=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

bin_dir="$HOME/.local/bin"
state_dir="$HOME/.agent-sync"
# The commands installed from this checkout, in the order they are linked.
commands="simple-chat simple-notes simple-board"

say() { if [ "$dry_run" -eq 1 ]; then echo "would: $*"; else echo "$*"; fi; }

for cmd in $commands; do
  if [ ! -f "$checkout/scripts/$cmd" ]; then
    echo "install.sh: $checkout/scripts/$cmd does not exist; pass the path of a checkout that has the simple-tools entry points" >&2
    exit 1
  fi
done

# Commands added after the first install (AFC-564: platform-letter, the session-letter hook and registry side for the
# non-Claude platforms).  They are linked when the checkout has them, so a checkout from before they existed still installs.
for cmd in platform-letter; do
  if [ -f "$checkout/scripts/$cmd" ]; then
    commands="$commands $cmd"
  fi
done

# 1. ~/.local/bin
if [ ! -d "$bin_dir" ]; then
  say "create $bin_dir"
  [ "$dry_run" -eq 1 ] || mkdir -p "$bin_dir"
fi

# 2. the symlinks (replace only our own link or a stale one; never overwrite a regular file)
for cmd in $commands; do
  target="$checkout/scripts/$cmd"
  link="$bin_dir/$cmd"

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
done

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

if [ "$dry_run" -eq 1 ]; then
  echo "dry run: nothing was changed"
else
  echo "done: run 'simple-chat --help', 'simple-notes --help' or 'simple-board --help'"
fi