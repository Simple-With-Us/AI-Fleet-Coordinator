#!/usr/bin/env bash
# Daily fleet backup: git repos + agent dotfolder skills/rules mirror.
# The two legs are independent: a failure in one must not skip the other.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
failures=""

/usr/bin/python3 "$HERE/backup-fleet-to-gdrive.py" "$@"
leg_repos=$?
if [ "$leg_repos" -ne 0 ]; then failures="$failures repo-backup(exit $leg_repos)"; fi

/usr/bin/python3 "$HERE/sync-fleet-agent-config-to-gdrive.py"
leg_config=$?
if [ "$leg_config" -ne 0 ]; then failures="$failures agent-config-sync(exit $leg_config)"; fi

if [ -n "$failures" ]; then
  echo "fleet-gdrive-backup: failed leg(s):$failures" >&2
  exit 1
fi
