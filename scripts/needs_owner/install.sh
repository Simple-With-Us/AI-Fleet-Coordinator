#!/usr/bin/env bash
# Install needs-owner for the current user.  Idempotent.
#
#   scripts/needs_owner/install.sh            plan:  say what would change, change nothing
#   scripts/needs_owner/install.sh --apply    copy the stable copy and link it
#
# The stable copy is ~/apps/needs-owner:
#     needs-owner               the entry point (scripts/needs-owner)
#     needs_owner/              the package, without its tests
#     agent_sync/               only identity.py, secretscan.py and textsafe.py (the F3 helpers it loads by path)
#     state/                    untouched here (the fallback notifier keeps its files there)
# and ~/.local/bin/needs-owner is a symbolic link to ~/apps/needs-owner/needs-owner.
#
# Why a copy:  a tool run from a checkout moves with the branch, and a tool run with `python3 -I` cannot import
# a package it was not given.  So the copy carries every file the tool loads, and --apply ends by running
# `python3 -I ~/apps/needs-owner/needs-owner --help` and `--version` to prove it (both load the package and the three F3
# files before they parse anything, so a copy that lacks or has damaged one fails).  Run it from a lane or from the managed
# runtime checkout, never from the human integration tree ~/Code/AI-Fleet-Coordinator.
#
# It touches nothing else:  no LaunchAgents, no pm2, nothing under ~/.secrets.  It is an on-demand helper
# (docs/MAC-LOCAL-PROCESSES.md):  nothing stays running.
set -euo pipefail

apply=0
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    -h|--help) sed -n "2,21p" "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 2 ;;
  esac
done

here="$(cd "$(dirname "$0")" && pwd)"      # scripts/needs_owner
scripts="$(dirname "$here")"                # scripts
dest="$HOME/apps/needs-owner"
bin_dir="$HOME/.local/bin"
link="$bin_dir/needs-owner"
f3_files="identity.py secretscan.py textsafe.py"

for need in "$scripts/needs-owner" "$here/cli.py"; do
  [ -f "$need" ] || { echo "install.sh: $need is missing;  run this from a checkout of AI-Fleet-Coordinator" >&2; exit 1; }
done
for f in $f3_files; do
  [ -f "$scripts/agent_sync/$f" ] || { echo "install.sh: $scripts/agent_sync/$f is missing" >&2; exit 1; }
done

say() { if [ "$apply" -eq 1 ]; then echo "$*"; else echo "would: $*"; fi; }

# The list of (source, relative destination) pairs, one per line.
manifest() {
  echo "$scripts/needs-owner|needs-owner"
  for f in "$here"/*.py; do echo "$f|needs_owner/$(basename "$f")"; done
  for f in $f3_files; do echo "$scripts/agent_sync/$f|agent_sync/$f"; done
}

changed=0
while IFS='|' read -r src rel; do
  if [ -f "$dest/$rel" ] && cmp -s "$src" "$dest/$rel"; then
    continue
  fi
  changed=1
  if [ -f "$dest/$rel" ]; then say "update $dest/$rel"; else say "create $dest/$rel"; fi
done < <(manifest)

# Files in the stable copy that the source no longer has (a renamed module must not linger and shadow nothing).
stale=""
if [ -d "$dest" ]; then
  for sub in needs_owner agent_sync; do
    [ -d "$dest/$sub" ] || continue
    for f in "$dest/$sub"/*.py; do
      [ -e "$f" ] || continue
      rel="$sub/$(basename "$f")"
      if ! manifest | cut -d'|' -f2 | grep -qx "$rel"; then stale="$stale $f"; changed=1; say "remove $f (no longer in the source)"; fi
    done
  done
fi

# The link.
want_link=1
if [ -L "$link" ]; then
  if [ "$(readlink "$link")" = "$dest/needs-owner" ]; then want_link=0; echo "link already correct: $link -> $dest/needs-owner"; fi
elif [ -e "$link" ]; then
  echo "install.sh: $link exists and is not a symbolic link;  move it aside first" >&2
  exit 1
fi
[ "$want_link" -eq 1 ] && say "link $link -> $dest/needs-owner"

if [ "$changed" -eq 0 ] && [ "$want_link" -eq 0 ]; then
  echo "nothing to do:  $dest is current"
fi

if [ "$apply" -eq 0 ]; then
  [ "$changed" -eq 1 ] || [ "$want_link" -eq 1 ] && echo "plan only:  run again with --apply to make these changes"
  exit 0
fi

if [ "$changed" -eq 1 ]; then
  mkdir -p "$HOME/apps"
  stage="$(mktemp -d "$HOME/apps/.needs-owner-stage.XXXXXX")"
  trap 'rm -rf "$stage"' EXIT
  mkdir -p "$stage/needs_owner" "$stage/agent_sync"
  while IFS='|' read -r src rel; do
    cp "$src" "$stage/$rel"
    if [ "$rel" = "needs-owner" ]; then chmod 755 "$stage/$rel"; else chmod 644 "$stage/$rel"; fi
  done < <(manifest)
  mkdir -p "$dest"
  for name in needs_owner agent_sync; do
    rm -rf "$dest/$name.old"
    [ -e "$dest/$name" ] && mv "$dest/$name" "$dest/$name.old"
    mv "$stage/$name" "$dest/$name"
    rm -rf "$dest/$name.old"
  done
  mv -f "$stage/needs-owner" "$dest/needs-owner"
fi

if [ "$want_link" -eq 1 ]; then
  mkdir -p "$bin_dir"
  ln -sfn "$dest/needs-owner" "$link"
fi

# The proof:  the stable copy runs in isolated mode with only what it was given.  `--help` and `--version` import the
# package and load the three F3 files before argparse sees them, so a missing or damaged file fails here.
for proof in --help --version; do
  if ! PYTHONDONTWRITEBYTECODE=1 python3 -I "$dest/needs-owner" "$proof" >/dev/null; then
    echo "install.sh: the stable copy does not run with python3 -I ($proof);  it is incomplete" >&2
    exit 1
  fi
done
echo "installed:  $(PYTHONDONTWRITEBYTECODE=1 python3 -I "$dest/needs-owner" --version)  at $dest"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "note: $bin_dir is not on PATH;  add it to your shell profile" ;;
esac
