#!/bin/sh
# Container entry for the agent-sync server instance.
#
# On first start it copies the sample config (scripts/agent_sync/server/listener.toml) onto the
# state volume as $AGENT_SYNC_CONFIG; it never overwrites one that exists, because
# `agent-sync daemon init` pins the owner in that copy.  Then it execs agent-sync with the
# container's arguments (default:  daemon run --wait-lock).  No secrets pass through here.
set -eu
here=$(dirname "$0")
: "${AGENT_SYNC_STATE_DIR:=/data}"
: "${AGENT_SYNC_CONFIG:=${AGENT_SYNC_STATE_DIR}/listener.toml}"
export AGENT_SYNC_STATE_DIR AGENT_SYNC_CONFIG
umask 077
mkdir -p "$AGENT_SYNC_STATE_DIR"
if [ ! -e "$AGENT_SYNC_CONFIG" ]; then
    cp "$here/listener.toml" "$AGENT_SYNC_CONFIG"
    echo "agent-sync: seeded $AGENT_SYNC_CONFIG from the image's sample; run agent-sync daemon init to pin the owner" >&2
fi
exec "$here/../../agent-sync" "$@"
