#!/bin/sh
# Container entry for the Operator's server instance.
#
# On first start it copies the sample config (scripts/simple_tools/server/listener.toml) onto the
# state volume as $SIMPLE_TOOLS_CONFIG; it never overwrites one that exists, because
# `simple-chat daemon init` pins the owner in that copy.  Then it execs simple-chat with the
# container's arguments (default:  daemon run --wait-lock).
#
# Credentials:  INFISICAL_TOKEN and INFISICAL_PROJECT_ID are required.  infisical_env.py loads
# the listener's variables from Infisical (prod, /zulip) with the scoped service token, then
# execs simple-chat with the loaded values and without the token.  Missing credentials or a
# failed fetch stop startup; the listener never falls back to container copies.
#
# `docker exec` gets the container's original environment, not the one PID 1 was started with.
# Run a command that needs the seats' credentials (daemon init, for one) through this script:
#   docker exec "$C" /app/scripts/simple_tools/server/entrypoint.sh daemon init --seat MA --yes
set -eu
here=$(dirname "$0")
: "${SIMPLE_TOOLS_STATE_DIR:=/data}"
: "${SIMPLE_TOOLS_CONFIG:=${SIMPLE_TOOLS_STATE_DIR}/listener.toml}"
export SIMPLE_TOOLS_STATE_DIR SIMPLE_TOOLS_CONFIG
umask 077
mkdir -p "$SIMPLE_TOOLS_STATE_DIR"
if [ ! -e "$SIMPLE_TOOLS_CONFIG" ]; then
    cp "$here/listener.toml" "$SIMPLE_TOOLS_CONFIG"
    echo "simple-chat: seeded $SIMPLE_TOOLS_CONFIG from the image's sample; run simple-chat daemon init to pin the owner" >&2
fi
exec python3 "$here/infisical_env.py" "$here/../../simple-chat" "$@"
