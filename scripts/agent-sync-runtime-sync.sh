#!/bin/bash
# The Mac's runtime-sync script is scripts/simple-tools-runtime-sync.sh (renamed with its LaunchAgent,
# com.jay.simple-tools-runtime-sync, on Nov 2, 2026).  This old name stays only because CI's
# `bash -n scripts/agent-sync-runtime-sync.sh` line lives in a workflow file the seat that renamed the
# script cannot change;  delete this file in the same change that points that line at the new name.
# Nothing installs or runs this copy.  If something does, it runs the real script.
exec /bin/bash "$(dirname "$0")/simple-tools-runtime-sync.sh" "$@"
