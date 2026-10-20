#!/bin/bash
# Litestream needs the Backblaze master key pair.  They come from `fleet-secrets run` (OPS-818;  the old flat
# secrets file is retired, AFC-359), or are already in the environment.  The absolute path is
# because a LaunchAgent or pm2 PATH is minimal.
here="$(dirname "$0")"
if [ -n "${BACKBLAZE_MASTER_KEY_ID:-}" ] && [ -n "${BACKBLAZE_MASTER_APPLICATION_KEY:-}" ]; then
  exec litestream replicate -config "$here/litestream.yml"
fi
exec "${FLEET_SECRETS_BIN:-$HOME/.local/bin/fleet-secrets}" run \
  --only BACKBLAZE_MASTER_KEY_ID,BACKBLAZE_MASTER_APPLICATION_KEY -- \
  litestream replicate -config "$here/litestream.yml"
