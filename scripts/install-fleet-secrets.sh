#!/usr/bin/env bash
# Install the fleet-secrets command on this Mac (OPS-818).  Idempotent.  Honors $HOME.
#
# Copies scripts/fleet_secrets/ (no tests, no __pycache__) and scripts/fleet-secrets from this
# checkout into $FLEET_SECRETS_HOME (default $HOME/.local/share/fleet-secrets/), then symlinks
# $HOME/.local/bin/fleet-secrets to the copied entry point.  The copy is on purpose:  a lane is
# retired after its PR merges, and a symlink into a retired lane would leave every migrated
# LaunchAgent and shell script without its secrets.  Re-run this after the package changes.
#
# It writes no secret and no project id.  The Fleet-OPS project id is read at run time from
# INFISICAL_FLEET_OPS_PROJECT_ID or ~/.agent-sync/fleet-ops-project-id (see INFISICAL.md).
set -euo pipefail

linux_bootstrap=0
if [ "${1:-}" = "--linux-private-bootstrap" ] && [ "$#" -eq 1 ]; then
  [ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] || { echo "install-fleet-secrets: bootstrap launcher requires Linux root" >&2; exit 1; }
  linux_bootstrap=1
elif [ "$#" -ne 0 ]; then
  echo "usage: install-fleet-secrets.sh [--linux-private-bootstrap]" >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${FLEET_SECRETS_HOME:-$HOME/.local/share/fleet-secrets}"
bin_dir="$HOME/.local/bin"

[ -d "$here/fleet_secrets" ] && [ -f "$here/fleet-secrets" ] || { echo "install-fleet-secrets: run it from a checkout of scripts/" >&2; exit 1; }

mkdir -p "$dest" "$bin_dir"
rm -rf "$dest/fleet_secrets"
mkdir -p "$dest/fleet_secrets"
for f in "$here"/fleet_secrets/*.py; do
  install -m 0644 "$f" "$dest/fleet_secrets/$(basename "$f")"
done
install -m 0755 "$here/fleet-secrets" "$dest/fleet-secrets"
launcher="$dest/fleet-secrets"
if [ "$linux_bootstrap" -eq 1 ]; then
  # Existing bootstrap stays in place. This only installs reviewed executable code.
  install -m 0755 "$here/fleet-secrets" "$dest/fleet-secrets-cli"
  install -m 0755 "$here/fleet_secrets/linux-entrypoint.py" "$dest/linux-entrypoint.py"
  launcher="$dest/linux-entrypoint.py"
fi
ln -sfn "$launcher" "$bin_dir/fleet-secrets"

echo "installed: $bin_dir/fleet-secrets -> $launcher"
echo "next:      fleet-secrets doctor"
