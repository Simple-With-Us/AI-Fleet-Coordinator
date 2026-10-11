#!/usr/bin/env bash
# scripts/telemetry/configure-agents.sh — mint producer configs for fleet-telemetry-relay.
#
# SHIPPED ONLY.  Never run this in CI, launchd, or any automation, and never run
# it against live $HOME dotfiles:  it writes producer config SNIPPETS (plus a
# README.txt with the manual install steps) into OUT_DIR only.  Jay copies the
# snippets into place by hand.  This script has no code path that writes under
# $HOME; a test pins that.
#
# Credentials come from Infisical by name (project "AI Fleet Coordinator", prod
# only) and are never printed:  stdout carries key names and set/empty flags
# only, snippet files are 0600.  Needs the Infisical CLI logged in (or
# INFISICAL_API_URL + machine identity in the environment) and python3.
#
# Usage:
#   INFISICAL_PROJECT_ID=<id, see INFISICAL.md> OUT_DIR=./agent-configs \
#     bash scripts/telemetry/configure-agents.sh [--relay-url https://<relay-host>]
set -euo pipefail

INFISICAL_PROJECT_ID="${INFISICAL_PROJECT_ID:?set INFISICAL_PROJECT_ID (the AI Fleet Coordinator project id, see INFISICAL.md)}"
OUT_DIR="${OUT_DIR:-./telemetry-relay-agent-configs}"
RELAY_URL="${RELAY_URL:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --relay-url) RELAY_URL="${2:?--relay-url needs a value}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?--out-dir needs a value}"; shift 2 ;;
    *) echo "usage: configure-agents.sh [--relay-url https://<relay-host>] [--out-dir DIR]" >&2; exit 2 ;;
  esac
done

command -v infisical >/dev/null 2>&1 || { echo "missing infisical CLI on PATH" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "missing python3 on PATH" >&2; exit 1; }

EXPORT_JSON="$(infisical export --env=prod --projectId="$INFISICAL_PROJECT_ID" --format=json)"
export EXPORT_JSON RELAY_URL

# NAMES the script reads (values stay in EXPORT_JSON, never echoed).
export NAMES="RELAY_BASE_URL RELAY_BEARER_CLAUDE_CODE RELAY_BEARER_CODEX RELAY_BEARER_GROK RELAY_BEARER_SHIM"

# Resolve: RELAY_BASE_URL prefers --relay-url/RELAY_URL, else the Infisical config
# of the same name.  Report set/empty per name; abort on any empty bearer.
# (Plain temp file, not mapfile/assoc arrays: the Mac ships bash 3.2.)
tmp_report="$(mktemp "${TMPDIR:-/tmp}/telemetry-relay-report.XXXXXX")"
trap 'rm -f "$tmp_report"' EXIT
python3 -c '
import json, os
names = os.environ["NAMES"].split()
store = json.loads(os.environ["EXPORT_JSON"])
override = os.environ.get("RELAY_URL", "")
for name in names:
    value = override if (name == "RELAY_BASE_URL" and override) else store.get(name, "")
    print("%s=%s" % (name, "set" if value else "empty"))
' > "$tmp_report"

missing=0
while IFS= read -r line; do
  name="${line%%=*}"
  state="${line##*=}"
  echo "$line"
  case "$name" in
    RELAY_BASE_URL) [ "$state" = "set" ] || { echo "RELAY_BASE_URL is empty: pass --relay-url or set it in Infisical" >&2; missing=1; } ;;
    *) [ "$state" = "set" ] || { echo "$name is empty in Infisical prod" >&2; missing=1; } ;;
  esac
done < "$tmp_report"
[ "$missing" -eq 0 ] || exit 1

umask 077
mkdir -p "$OUT_DIR"

# Write the snippets with python3 so values never pass through shell echo.
python3 - "$OUT_DIR" <<'PY'
import json, os, stat, sys

out_dir, export_json, relay_override = sys.argv[1], os.environ["EXPORT_JSON"], os.environ.get("RELAY_URL", "")
store = json.loads(export_json)
base = (relay_override or store["RELAY_BASE_URL"]).rstrip("/")
get = lambda name: store[name]

claude = {
    "env": {
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_ENDPOINT": base + ":4318",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + get("RELAY_BEARER_CLAUDE_CODE"),
        "OTEL_RESOURCE_ATTRIBUTES": "platform=claude-code,signal=agent",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
    }
}
codex = (
    "# Codex CLI native OTel (append to ~/.codex/config.toml by hand).\n"
    "# Field names per the Codex [otel] reference; re-check against the installed\n"
    "# Codex version before use.\n"
    "[otel]\n"
    'exporter = "otlp-http"\n'
    'endpoint = "' + base + ':4319"\n'
    'headers = { Authorization = "Bearer ' + get("RELAY_BEARER_CODEX") + '" }\n'
    "environment = \"platform=codex,signal=agent\"\n"
    "log_user_prompt = true\n"
)
grok = {
    "telemetry": {
        "otel_endpoint": base + ":4320",
        "otel_headers": {"Authorization": "Bearer " + get("RELAY_BEARER_GROK")},
        "otel_log_user_prompts": True,
        "otel_log_tool_content": True,
        "resource_attributes": {"platform": "grok-cli", "signal": "agent"},
    },
    "_note": "Field names per the Grok CLI telemetry.* reference; re-check against the installed Grok CLI before use.",
}
readme = """Install by hand (nothing here applied itself):
1. Claude Code: merge claude-settings.json `env` into ~/.claude/settings.json, then restart Claude Code.
2. Codex: append codex-config-otel.toml to ~/.codex/config.toml, then restart Codex.
3. Grok CLI: merge grok-telemetry.json `telemetry` into the Grok CLI config, then restart Grok CLI.
4. Hook shim producers (Cursor/Antigravity/Copilot): point the shim at the relay
   port 4321 with the RELAY_BEARER_SHIM token when the shim v2 task lands.
Keep these files private: they carry bearer tokens (mode 0600).
"""

files = {
    "claude-settings.json": json.dumps(claude, indent=2) + "\n",
    "codex-config-otel.toml": codex,
    "grok-telemetry.json": json.dumps(grok, indent=2) + "\n",
    "README.txt": readme,
}
for name, content in files.items():
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    print("wrote " + path)
PY

echo "snippets in $OUT_DIR (0600). Copy them into place by hand; see README.txt there."
