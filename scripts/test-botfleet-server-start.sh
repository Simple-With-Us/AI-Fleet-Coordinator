#!/bin/bash
# Contract: start wrapper exits 0 when :8799 is healthy, 1 when deps missing,
# and starts the harness with BOTFLEET_FLEET_SEAT_PROMPTS=1 (owner ruling
# 2026-10-11, seats for all BotFleet bots), in the script and in the plist.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WRAP="${ROOT}/botfleet-server-start.sh"
[ -f "$WRAP" ] || { echo "missing $WRAP" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$WRAP" "$tmp" <<'PY'
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text()
root = pathlib.Path(sys.argv[2])
src = src.replace("/usr/bin/curl", str(root / "curl"))
(root / "wrap.sh").write_text(src)
PY
chmod +x "${tmp}/wrap.sh"

# healthy port -> exit 0 even with no checkout
cat >"${tmp}/curl" <<'EOS'
#!/bin/bash
exit 0
EOS
chmod +x "${tmp}/curl"
if ! BOTFLEET_SERVER_ROOT="${tmp}/no-such" bash "${tmp}/wrap.sh" >"${tmp}/out" 2>"${tmp}/err"; then
  echo "FAIL expected exit 0 on healthy harness" >&2
  cat "${tmp}/err" >&2
  exit 1
fi
grep -q "already healthy" "${tmp}/out" || { echo "FAIL missing already-healthy log" >&2; exit 1; }

# The probe must hit /api/health.  BotFleet serves both /health and
# /api/health (server/index.ts answers 200 on either), so the stub proves the
# wrapper asks the one this contract names rather than the other.
cat >"${tmp}/curl" <<'EOS'
#!/bin/bash
case "$*" in
  *"/api/health"*) exit 0 ;;
  *) exit 22 ;;
esac
EOS
chmod +x "${tmp}/curl"
if ! BOTFLEET_SERVER_ROOT="${tmp}/no-such" bash "${tmp}/wrap.sh" >"${tmp}/out3" 2>"${tmp}/err3"; then
  echo "FAIL wrapper must probe /api/health, not /health" >&2
  cat "${tmp}/err3" >&2
  exit 1
fi
grep -q "already healthy" "${tmp}/out3" || { echo "FAIL /api/health probe: missing already-healthy log" >&2; exit 1; }

# unhealthy + missing node_modules -> exit 1
cat >"${tmp}/curl" <<'EOS'
#!/bin/bash
exit 22
EOS
chmod +x "${tmp}/curl"
if BOTFLEET_SERVER_ROOT="${tmp}/empty-root" bash "${tmp}/wrap.sh" >"${tmp}/out2" 2>"${tmp}/err2"; then
  echo "FAIL expected exit 1 when node_modules missing" >&2
  exit 1
fi
grep -q "missing" "${tmp}/err2" || { echo "FAIL missing node_modules message" >&2; cat "${tmp}/err2" >&2; exit 1; }

# Seat switch.  BotFleet is a public product and server/launch-identity.ts only
# gives bots a fleet seat when BOTFLEET_FLEET_SEAT_PROMPTS is exactly "1", so
# the Mac's launch config must set it.  Static: one export, with the literal
# value 1, before the exec that starts the harness.
want='export BOTFLEET_FLEET_SEAT_PROMPTS=1'
n="$(grep -c -x "$want" "$WRAP" || true)"
[ "$n" = "1" ] || { echo "FAIL $WRAP must contain exactly one line: $want (found $n)" >&2; exit 1; }
export_line="$(grep -n -x "$want" "$WRAP" | cut -d: -f1)"
exec_line="$(grep -n '^exec ' "$WRAP" | tail -1 | cut -d: -f1 || true)"
[ -n "$exec_line" ] && [ "$export_line" -lt "$exec_line" ] \
  || { echo "FAIL seat export (line $export_line) must come before exec (line ${exec_line:-none})" >&2; exit 1; }

# Behavioural: run the wrapper to its exec with a stub node that reports what
# it inherited.  Unhealthy port, a fake checkout, and the flag unset, then
# preset to the public default 0, which the wrapper must override.
mkdir -p "${tmp}/co/node_modules" "${tmp}/co/server"
: >"${tmp}/co/server/index.ts"
cat >"${tmp}/node" <<'EOS'
#!/bin/bash
echo "SEAT=${BOTFLEET_FLEET_SEAT_PROMPTS-unset}"
EOS
chmod +x "${tmp}/node"
for preset in unset 0; do
  if [ "$preset" = unset ]; then
    out="$(env -u BOTFLEET_FLEET_SEAT_PROMPTS BOTFLEET_SERVER_ROOT="${tmp}/co" BOTFLEET_NODE="${tmp}/node" bash "${tmp}/wrap.sh" 2>"${tmp}/err4")" || { echo "FAIL wrapper did not reach exec (flag $preset)" >&2; cat "${tmp}/err4" >&2; exit 1; }
  else
    out="$(BOTFLEET_FLEET_SEAT_PROMPTS=0 BOTFLEET_SERVER_ROOT="${tmp}/co" BOTFLEET_NODE="${tmp}/node" bash "${tmp}/wrap.sh" 2>"${tmp}/err4")" || { echo "FAIL wrapper did not reach exec (flag $preset)" >&2; cat "${tmp}/err4" >&2; exit 1; }
  fi
  [ "$out" = "SEAT=1" ] || { echo "FAIL harness must inherit BOTFLEET_FLEET_SEAT_PROMPTS=1 (flag $preset), got: $out" >&2; exit 1; }
done

# The tracked LaunchAgent template carries the same switch.
PLIST="${ROOT}/launchd/com.jay.botfleet-server.plist"
[ -f "$PLIST" ] || { echo "missing $PLIST" >&2; exit 1; }
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST" >/dev/null || { echo "FAIL plutil -lint $PLIST" >&2; exit 1; }
fi
python3 - "$PLIST" <<'PY' || { echo "FAIL $PLIST EnvironmentVariables must set BOTFLEET_FLEET_SEAT_PROMPTS to the string 1" >&2; exit 1; }
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    env = plistlib.load(fh).get("EnvironmentVariables", {})
sys.exit(0 if env.get("BOTFLEET_FLEET_SEAT_PROMPTS") == "1" else 1)
PY

echo OK
