#!/usr/bin/env bash
# onboard-new-agent.sh — register a new seat in fleet-apps.json and print the manual steps.
#
# Mechanical half of docs/ONBOARDING-NEW-AGENT.md.  Does not create lanes (a lane is
# made per task with `~/apps/lane new`), does not create the seat's Zulip bot (only Jay
# does, in his own terminal), and does not configure MCP, or platform rules files and hooks.
#
# Usage:
#   ./scripts/onboard-new-agent.sh --tag GROK --notes-name Grok \
#       --worktree-suffix grok --branch-prefix grok/ --zulip-short grok-build \
#       --app grok-terminal
#   ./scripts/onboard-new-agent.sh --tag NEWSEAT --dry-run
#
# --zulip-short is the short name Jay will type for the bot.  Zulip appends -bot itself,
# so the default (the lowercase tag) gives <tag>-bot@.  Never end it in -bot.
#
# --app is the product the seat runs in (claude-code, codex, grok-terminal, grok-web ...), written to
# fleet-apps.json seatApps.bySeat.  It is never guessed from the tag, because two seats on one model are
# two apps and a wrong guess would count them as one.  Without --app a terminal asks (naming the apps
# already there);  a script or a pipe stops.  Only --dry-run, a seat that already has an app in
# seatApps.bySeat, or a retired seat may leave it out.

set -euo pipefail

TAG=""
NOTES_NAME=""
SUFFIX=""
PREFIX=""
ZULIP_SHORT=""
APP=""
DRY_RUN=0
here="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --notes-name) NOTES_NAME="${2:-}"; shift 2 ;;
    --worktree-suffix) SUFFIX="${2:-}"; shift 2 ;;
    --branch-prefix) PREFIX="${2:-}"; shift 2 ;;
    --zulip-short) ZULIP_SHORT="${2:-}"; shift 2 ;;
    --app) APP="${2:-}"; shift 2 ;;
    --apps|--include-fleet)
      # Accepted so older command lines keep working.  These used to pick which
      # integration trees got a lane; no lane is created here any more.
      echo "note: $1 is ignored; lanes are created per task with 'lane new'" >&2
      # --apps used to take a value; older command lines still pass one, but the flag is
      # documented as a no-op, so only take a value when the next argument is not a flag.
      if [ "$1" = "--apps" ] && [ "$#" -gt 1 ] && [ "${2#--}" = "$2" ]; then shift 2; else shift; fi ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

if [ -z "$TAG" ]; then
  echo "required: --tag" >&2
  usage 1
fi
# Registry keys are upper case (parse_seat_registry upper-cases every seatApps key), so
# canonicalise the tag here or a lower-case --tag writes a duplicate lower-case key.
TAG="$(printf '%s' "$TAG" | tr '[:lower:]' '[:upper:]')"   # not ${TAG^^}:  macOS /bin/bash is 3.2
NOTES_NAME="${NOTES_NAME:-$TAG}"
SUFFIX="${SUFFIX:-$(echo "$TAG" | tr '[:upper:]' '[:lower:]')}"
PREFIX="${PREFIX:-$SUFFIX/}"
ZULIP_SHORT="${ZULIP_SHORT:-$(echo "$TAG" | tr '[:upper:]' '[:lower:]')}"

# The seat's app (fleet-apps.json seatApps.bySeat).  A person at a terminal is asked;  anything else
# must pass --app.  The apps already in use are listed so a new seat on an existing app reuses its name.
known_apps() {
  python3 - "$here/fleet-apps.json" <<'PY'
import json, sys
apps = json.load(open(sys.argv[1])).get("seatApps", {})
names = set(apps.get("bySeat", {}).values()) | set(apps.get("extraSeats", {}).values())
names |= {p.get("app") for p in apps.get("patterns", []) if isinstance(p, dict) and p.get("app")}
print(", ".join(sorted(names)))
PY
}
# Needs --app unless the seat already has one, or is a retired row (a retired seat has no app).
needs_app() {
  python3 - "$here/fleet-apps.json" "$TAG" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
tag = sys.argv[2]
if tag in data.get("seatApps", {}).get("bySeat", {}):
    sys.exit(1)
if any(s.get("tag") == tag and s.get("retired") for s in data.get("seats", [])):
    sys.exit(1)
sys.exit(0)
PY
}
if [ -z "$APP" ] && [ "$DRY_RUN" -eq 0 ] && needs_app; then
  if [ -t 0 ]; then
    echo "Which app does $TAG run in?  Apps in use:  $(known_apps)" >&2
    echo "Reuse a name above only when $TAG runs in that same app;  a different product gets its own name." >&2
    read -r -p "app (lowercase, hyphens):  " APP
  fi
  if [ -z "$APP" ]; then
    echo "required: --app <name>  (apps in use:  $(known_apps))" >&2
    exit 1
  fi
fi
if [ -n "$APP" ] && ! printf '%s' "$APP" | grep -Eq '^[a-z0-9]+(-[a-z0-9]+)*$'; then
  echo "--app '$APP' must be lowercase letters, digits and single hyphens (claude-code, grok-web)" >&2
  exit 1
fi

# The credential file code comes from the rule the simple-chat CLI itself uses, so the
# file this script names is the file the CLI will look for.  The bot email is the short
# name plus -bot@ plus the realm host (the registry's zulipRealm).
zulip_ids="$(python3 - "$here" "$TAG" "$ZULIP_SHORT" <<'PY'
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

here, tag, short = sys.argv[1:4]
sys.path.insert(0, str(Path(here) / "scripts"))
from agent_sync.zulip import UsageError, credential_file_name, normalise_seat  # noqa: E402

try:
    seat = normalise_seat(tag)
except UsageError as err:
    sys.exit(f"--tag: {err}")
if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", short) or short.endswith("-bot"):
    sys.exit(f"--zulip-short {short!r} must be lowercase letters, digits and single hyphens, and must "
             "not end in -bot (Zulip appends -bot itself)")
realm = json.loads((Path(here) / "fleet-apps.json").read_text())["zulipRealm"]
print(credential_file_name(seat).removesuffix("-zuliprc"), f"{short}-bot@{urlparse(realm).hostname}")
PY
)"
FILE_CODE="${zulip_ids%% *}"
BOT_EMAIL="${zulip_ids##* }"

echo "== seat onboard: tag=$TAG notes=$NOTES_NAME suffix=$SUFFIX prefix=$PREFIX app=${APP:-(not set)}"
BOT_KEY_PREFIX="$(printf '%s' "$FILE_CODE" | tr 'a-z-' 'A-Z_')"
echo "   zulip bot=$BOT_EMAIL  credentials=Infisical Fleet-OPS /zulip (keys ${BOT_KEY_PREFIX}_ZULIP_EMAIL, _KEY, _SITE)"

# Record the seat in fleet-apps.json if missing, and its app in seatApps.bySeat.
if [ "$DRY_RUN" -eq 0 ]; then
  python3 - "$here/fleet-apps.json" "$TAG" "$NOTES_NAME" "$SUFFIX" "$PREFIX" "$BOT_EMAIL" "$FILE_CODE" "$APP" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
tag, notes, suffix, prefix, bot_email, file_code, app = sys.argv[2:9]
data = json.loads(path.read_text())
seats = data.setdefault("seats", [])
changed = False
if any(s.get("tag") == tag for s in seats):
    print(f"fleet-apps.json already has seat {tag}")
else:
    seats.append({
        "tag": tag,
        "notesName": notes,
        "worktreeSuffix": suffix,
        "branchPrefixes": [prefix],
        "zulipBotEmail": bot_email,
        "zulipFileCode": file_code,
    })
    changed = True
    print(f"appended seat {tag} to fleet-apps.json")
by_seat = data.get("seatApps", {}).get("bySeat")
if by_seat is None:
    sys.exit("fleet-apps.json has no seatApps.bySeat;  run this from a lane at origin/main")
if any(s.get("tag") == tag and s.get("retired") for s in seats):
    print(f"seat {tag} is retired, so it gets no app in seatApps.bySeat")
elif tag in by_seat:
    print(f"seatApps.bySeat already gives {tag} the app {by_seat[tag]}")
else:
    by_seat[tag] = app
    changed = True
    print(f"set seatApps.bySeat[{tag}] = {app}")
if changed:
    path.write_text(json.dumps(data, indent=2) + "\n")
PY
else
  echo "DRY: would record seat $TAG in fleet-apps.json, with app ${APP:-(none given)} in seatApps.bySeat"
fi

echo
if [ "$DRY_RUN" -eq 1 ]; then recorded="would be recorded"; else recorded="is recorded"; fi
echo "Seat $TAG $recorded.  This script creates NO lanes: a lane is made per task, with"
echo "  AGENT_SEAT=$TAG ~/apps/lane new <app> <slug>"
echo "which lands at ~/apps/lanes/<Repo>/$SUFFIX-<slug> on branch ${PREFIX}<slug>."
echo "(Layout and rules: docs/protocols/lane-map.md.  Folder names use the whole suffix.)"
echo
echo "Still do by hand (the owner approves each live install; read every plan first):"
echo "  1. PR the new fleet-apps.json row and its seatApps.bySeat app (python3 scripts/check-fleet-registry.py"
echo "     fails without it).  'lane' refuses an unknown seat until the row has"
echo "     merged and the stable copy is refreshed:"
echo "       cd scripts && python3 -m fleet_lanes.install_tools plan tools"
echo "       cd scripts && python3 -m fleet_lanes.install_tools apply tools"
echo "  2. Rules file and deny hook are installed per PLATFORM, not per seat.  If this seat runs"
echo "     on a covered platform (rules: claude codex fx grok antigravity minimax cursor;"
echo "     hook: claude codex grok antigravity cursor minimax muse clutch kimi; minimax is in 'all', and muse,"
echo "     clutch and kimi are opt-in), confirm with:"
echo "       cd scripts && python3 -m fleet_lanes.install_rules verify <platform>"
echo "       cd scripts && python3 -m fleet_lanes.install_tools verify <platform>"
echo "     and, if either is missing, plan then apply it:"
echo "       python3 -m fleet_lanes.install_rules plan <platform>"
echo "       python3 -m fleet_lanes.install_rules apply <platform>   (add --create for a new file;"
echo "         --i-own-this-file for claude)"
echo "       python3 -m fleet_lanes.install_tools plan <platform>"
echo "       python3 -m fleet_lanes.install_tools apply <platform>"
echo "     A new platform needs an entry in scripts/fleet_lanes/install_rules.py and install_tools.py"
echo "     first; this script does not add one."
echo "  3. AGENT_SEAT=$TAG if the platform shares an account"
echo "  4. Zulip bot.  Only Jay creates bots; an agent never does, and nobody pastes, prints or cats a key."
echo "       a. Add a row to ROSTER in scripts/zulip_provision_bots.py:"
echo "            (\"$NOTES_NAME\", \"$ZULIP_SHORT\", \"$FILE_CODE\")"
echo "          The display name is Jay's choice; the short name and file code are not."
echo "          Zulip appends -bot to the short name, so this makes $BOT_EMAIL."
echo "       b. Jay runs it in his own terminal (it signs in as the realm-admin bot from Infisical /zulipadmin and asks y/N before it writes):"
echo "            python3 scripts/zulip_provision_bots.py --dry-run --only \"<display name in that ROSTER row>\""
echo "            python3 scripts/zulip_provision_bots.py --only \"<display name in that ROSTER row>\""
echo "          --only matches the full display name, so use the one Jay chose.  Without --only it covers the whole roster."
echo "          Settings > Bots in Zulip works too; enter only the short name there."
echo "          The script writes ~/.agent-sync/provisioned/$FILE_CODE-zuliprc with mode 600 and never prints the key."
echo "          It then prints the Infisical /zulip key names to store (upper case, hyphens as underscores)."
echo "          An existing bot keeps its key unless Jay passes --regenerate, which invalidates the old key."
echo "          A cloud seat gets env ZULIP_EMAIL, ZULIP_API_KEY and ZULIP_SITE instead of a file."
echo "       c. Jay stores the three values in Infisical /zulip and deletes ~/.agent-sync/provisioned/."
echo "          Check the file mode (expect 600) before that and the identity (it must show $BOT_EMAIL) after."
echo "          Neither prints the key:"
echo "            stat -f '%Lp' ~/.agent-sync/provisioned/$FILE_CODE-zuliprc"
echo "            AGENT_SEAT=$TAG simple-chat whoami"
echo "       d. Subscribe at setup, not ad hoc (a typo cannot create a channel with --must-exist):"
echo "            AGENT_SEAT=$TAG simple-chat subscribe --channel work-in-progress --channel builds --must-exist"
echo "  5. Intro, once per session, before the first claim:"
echo "       AGENT_SEAT=$TAG simple-chat post --topic \"roll call\" \"online  |  <platform>  |  cadence:  <listen|wait|per-turn read>"
echo "       can:  <what this session can do>\""
echo "     The CLI writes the [${TAG}·session8] tag itself.  Read the room first:  simple-chat inbox"
echo "  6. Listener: docs/protocols/agent-sync-partition.toml must list $TAG as mac or server, in the"
echo "     PR that enables it and with Jay's OK.  A listener refuses a seat the file does not give to"
echo "     its own instance, and a seat must never be in both."
echo "  7. Add the seat to the Who's Here roster in docs/protocols/zulip-fleet-guide.md (mention, bot"
echo "     email, file code), and a row to the Active Seats table if this is a standing seat:  that table is in"
echo "     Fleet-OPS handbook/core/seats-and-identity.md, changed by pull request, never by editing FLEET-PROTOCOL.md"
echo "  8. See docs/ONBOARDING-NEW-AGENT.md"
