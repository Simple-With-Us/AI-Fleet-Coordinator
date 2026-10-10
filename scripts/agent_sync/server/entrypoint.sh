#!/bin/sh
# Container entry for the agent-sync server instance.
#
# On first start it copies the sample config (scripts/agent_sync/server/listener.toml) onto the
# state volume as $AGENT_SYNC_CONFIG; it never overwrites one that exists, because
# `agent-sync daemon init` pins the owner in that copy.  Then it execs agent-sync with the
# container's arguments (default:  daemon run --wait-lock).
#
# Credentials:  when INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET and INFISICAL_PROJECT_ID are all
# set (a read-only Universal Auth machine identity), infisical_env.py loads the listener's
# variables from Infisical (prod, /zulip) and execs agent-sync with them, the identity removed.
# Otherwise agent-sync runs with the container's own environment, as before.  Either way exec
# keeps agent-sync as PID 1, and no value passes through this script or its log lines.
#
# `docker exec` gets the container's original environment, not the one PID 1 was started with.
# Run a command that needs the seats' credentials (daemon init, for one) through this script:
#   docker exec "$C" /app/scripts/agent_sync/server/entrypoint.sh daemon init --seat MA --yes
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
if [ -n "${INFISICAL_CLIENT_ID:-}" ] && [ -n "${INFISICAL_CLIENT_SECRET:-}" ] && [ -n "${INFISICAL_PROJECT_ID:-}" ]; then
    exec python3 "$here/infisical_env.py" "$here/../../agent-sync" "$@"
fi
if [ -n "${INFISICAL_CLIENT_ID:-}${INFISICAL_CLIENT_SECRET:-}${INFISICAL_PROJECT_ID:-}" ]; then
    echo "agent-sync: Infisical not used (INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET and INFISICAL_PROJECT_ID must all be set); starting with the container environment" >&2
fi
unset INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET
exec "$here/../../agent-sync" "$@"
