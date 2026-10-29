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

if ! declare -F _fleet_secrets_uname_s >/dev/null 2>&1; then
  _fleet_secrets_uname_s() {
    /usr/bin/uname -s
  }
fi

if ! declare -F _fleet_secrets_uid >/dev/null 2>&1; then
  _fleet_secrets_uid() {
    /usr/bin/id -u
  }
fi

_linux_private_bootstrap_allowed() {
  [ "$(_fleet_secrets_uname_s 2>/dev/null || true)" = Linux ] \
    && [ "$(_fleet_secrets_uid 2>/dev/null || true)" = 0 ]
}

install_fleet_secrets_main() {
  local linux_bootstrap=0
  if [ "${1:-}" = "--linux-private-bootstrap" ] && [ "$#" -eq 1 ]; then
    _linux_private_bootstrap_allowed || {
      echo "install-fleet-secrets: bootstrap launcher requires Linux root" >&2
      return 1
    }
    linux_bootstrap=1
  elif [ "$#" -ne 0 ]; then
    echo "usage: install-fleet-secrets.sh [--linux-private-bootstrap]" >&2
    return 2
  fi

  local here dest bin_dir launcher f
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  dest="${FLEET_SECRETS_HOME:-$HOME/.local/share/fleet-secrets}"
  bin_dir="$HOME/.local/bin"

  [ -d "$here/fleet_secrets" ] && [ -f "$here/fleet-secrets" ] || {
    echo "install-fleet-secrets: run it from a checkout of scripts/" >&2
    return 1
  }

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
  else
    rm -f "$dest/linux-entrypoint.py" "$dest/fleet-secrets-cli"
  fi
  ln -sfn "$launcher" "$bin_dir/fleet-secrets"

  echo "installed: $bin_dir/fleet-secrets -> $launcher"
  echo "next:      fleet-secrets doctor"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  install_fleet_secrets_main "$@"
fi
