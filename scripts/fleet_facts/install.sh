#!/bin/sh
# Install the fleet-facts stable copy for the current user.  A plan by default;  --apply does it.  Idempotent.
#
#   scripts/fleet_facts/install.sh [--apply]
#
# Stable copy:  $HOME/apps/fleet-facts/ holds
#   fleet-facts           the entry script          (python3 -I $HOME/apps/fleet-facts/fleet-facts --help)
#   mac-facts-publish     the launcher the LaunchAgent com.jay.mac-facts-publish runs (it does not load anything)
#   fleet_facts/*.py      the package, without its tests
#   agent_sync/           an empty __init__.py plus the two F3 helpers the package loads by path:
#                         textsafe.py (render_untrusted) and secretscan.py (redact_fields)
#   fleet_knobs/*.py      the K1 knob reader, without its tests:  `publish` and `publish-mac` fetch the signing key
#                         and the allowed-apps list through it (imported only then)
# and $HOME/.local/bin/fleet-facts is a symlink to the entry script.
#
# It touches nothing else:  no LaunchAgent, no pm2, no network, and nothing under ~/.secrets.  Every path is
# built from $HOME, so a test can point HOME at a temp folder.  Run it from a checkout of the repository
# (a lane or the managed runtime checkout), never from the human tree under ~/Code.
set -eu

apply=0
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
  esac
done

case "${HOME:-}" in
  /?*) ;;
  *) echo "install.sh: HOME must be an absolute path" >&2; exit 1 ;;
esac

here=$(cd "$(dirname "$0")" && pwd -P)
scripts_dir=$(dirname "$here")
dest="$HOME/apps/fleet-facts"
link_dir="$HOME/.local/bin"
link="$link_dir/fleet-facts"

for need in "$scripts_dir/fleet-facts" "$here/mac-facts-publish" "$here/cli.py" \
            "$scripts_dir/agent_sync/textsafe.py" "$scripts_dir/agent_sync/secretscan.py" "$scripts_dir/fleet_knobs/knobs.py"; do
  [ -f "$need" ] || { echo "install.sh: missing $need (run this from a full checkout)" >&2; exit 1; }
done

say() { if [ "$apply" -eq 1 ]; then echo "$*"; else echo "would: $*"; fi; }

say "copy the package, the entry script, the launcher, the two F3 helpers and fleet_knobs to $dest"
if [ "$apply" -eq 1 ]; then
  mkdir -p "$HOME/apps"
  stage="$HOME/apps/.fleet-facts.new.$$"
  trap 'rm -rf "$stage"' EXIT
  rm -rf "$stage"
  mkdir -p "$stage/fleet_facts" "$stage/agent_sync" "$stage/fleet_knobs"
  for f in "$here"/*.py; do
    case "$(basename "$f")" in check_fixtures.py) continue ;; esac   # a CI tool, not part of the runtime
    cp "$f" "$stage/fleet_facts/"
  done
  cp "$scripts_dir/fleet-facts" "$stage/fleet-facts"
  cp "$here/mac-facts-publish" "$stage/mac-facts-publish"
  cp "$scripts_dir/agent_sync/textsafe.py" "$scripts_dir/agent_sync/secretscan.py" "$stage/agent_sync/"
  cp "$scripts_dir"/fleet_knobs/*.py "$stage/fleet_knobs/"
  : > "$stage/agent_sync/__init__.py"
  chmod 755 "$stage/fleet-facts" "$stage/mac-facts-publish"
  chmod 644 "$stage"/fleet_facts/*.py "$stage"/agent_sync/*.py "$stage"/fleet_knobs/*.py
  prev="$dest.prev"
  rm -rf "$prev"
  if [ -e "$dest" ]; then mv "$dest" "$prev"; fi
  mv "$stage" "$dest"
  rm -rf "$prev"
  trap - EXIT
fi

if [ -L "$link" ]; then
  if [ "$(readlink "$link")" = "$dest/fleet-facts" ]; then
    echo "symlink already correct: $link"
  else
    say "repoint $link -> $dest/fleet-facts"
    [ "$apply" -eq 0 ] || ln -sfn "$dest/fleet-facts" "$link"
  fi
elif [ -e "$link" ]; then
  echo "install.sh: $link exists and is not a symlink;  move it aside first" >&2
  exit 1
else
  say "link $link -> $dest/fleet-facts"
  if [ "$apply" -eq 1 ]; then mkdir -p "$link_dir"; ln -s "$dest/fleet-facts" "$link"; fi
fi

case ":${PATH:-}:" in
  *":$link_dir:"*) ;;
  *) echo "note: $link_dir is not on PATH;  add it to your shell profile" ;;
esac

if [ "$apply" -eq 1 ]; then
  echo "done:  python3 -I $dest/fleet-facts gather --no-mac --json"
else
  echo "plan only:  nothing was changed.  Re-run with --apply to install."
fi
