#!/usr/bin/env bash
# Install reviewed code and nonsecret project metadata; never copy authentication material.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root on the intended Linux host.' >&2; exit 1; }
mode=${1:---install-only}
[[ "$mode" == --install-only || "$mode" == --enable ]] || { echo 'Use --install-only or --enable.' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
scripts=$(cd "$here/.." && pwd)
dest=/usr/local/lib/fleet-coding-memory
config=/etc/fleet-coding-memory
install -d -m 0700 "$dest" "$dest/settings" "$dest/settings/fleet_secrets" "$config"
install -m 0600 "$here/fleet_coding_memory.py" "$dest/fleet_coding_memory.py"
install -m 0700 "$scripts/fleet-secrets" "$dest/settings/fleet-secrets"
for source in "$scripts"/fleet_secrets/*.py; do
  install -m 0600 "$source" "$dest/settings/fleet_secrets/$(basename "$source")"
done
install -m 0700 "$scripts/fleet_secrets/linux-entrypoint.py" "$dest/settings/linux-entrypoint.py"
python3 - "$config/environment" <<'PY'
import os, sys, uuid
from pathlib import Path
metadata = Path('/home/jay/.agent-sync/fleet-ops-project-id').read_text().strip()
uuid.UUID(metadata)
target = Path(sys.argv[1])
temporary = target.with_suffix('.tmp')
temporary.write_text('INFISICAL_FLEET_OPS_PROJECT_ID=' + metadata + '\n'
                     'FLEET_SECRETS_CLI=/usr/local/lib/fleet-coding-memory/settings/fleet-secrets\n')
temporary.chmod(0o600)
os.replace(temporary, target)
PY
install -m 0644 "$here/fleet-coding-memory.service" /etc/systemd/system/fleet-coding-memory.service
systemctl daemon-reload
if [[ "$mode" == --enable ]]; then
  systemctl enable --now fleet-coding-memory.service
fi
if [[ -e "$config/exclude-units" ]]; then
  echo "Note: host-local exclude list present at $config/exclude-units (left untouched)."
fi
echo 'Coding memory code installed; authentication material was not copied.'
