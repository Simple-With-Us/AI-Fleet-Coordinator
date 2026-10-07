#!/bin/bash
set -euo pipefail

# setup-agent-lanes.sh — RETIRED (owner ruling 2026-10-07, docs/protocols/lane-map.md).
#
# The old script made one lane per agent in every repo (named after the repo folder, on an
# agent/<name> branch), which put idle checkouts where the Lane Map does not allow them.
# Lanes are now created per task.  This stub only prints that and exits 2, so an old link
# or habit fails loudly instead of creating anything.

cat >&2 <<'EOF'
setup-agent-lanes.sh is retired and does nothing.

Create a lane per task instead:

  export AGENT_SEAT=<your seat tag>          # for example CLAUDE, CODEX, AG, MM, CLUTCH
  ~/apps/lane new <app> <slug>               # lands at ~/apps/lanes/<prefix>/<seat>-<slug>
  ~/apps/lane new <app> --review --pr <n>    # read-only check of someone else's PR

Layout, rules and platform details: docs/protocols/lane-map.md
Every command and option:           scripts/fleet_lanes/README.md
EOF
exit 2
