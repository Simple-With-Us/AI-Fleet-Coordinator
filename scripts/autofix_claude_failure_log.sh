#!/usr/bin/env bash
# Emit sanitized Claude Code autofix failure context for GitHub Actions logs.
set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo "::warning::autofix failure log skipped (no execution file path)"
  exit 0
fi

exec_path=$1
if [ ! -f "$exec_path" ] || [ -L "$exec_path" ]; then
  echo "::warning::autofix failure log skipped (execution output missing)"
  exit 0
fi

route=${ROUTE:-unknown}
base=${BASE_URL:-unknown}
model=${MODEL_ID:-unknown}

echo "::notice::autofix route=$route base_url=$base model_id=$model"

if ! command -v jq >/dev/null 2>&1; then
  echo "::warning::jq unavailable; cannot summarize Claude execution output"
  exit 0
fi

result_json=$(jq -c '[.[] | select(.type == "result")] | last // empty' "$exec_path" 2>/dev/null || true)
if [ -z "$result_json" ]; then
  echo "::warning::Claude execution output has no result message"
  exit 0
fi

is_error=$(printf '%s' "$result_json" | jq -r '.is_error // empty')
subtype=$(printf '%s' "$result_json" | jq -r '.subtype // empty')
duration=$(printf '%s' "$result_json" | jq -r '.duration_ms // empty')
turns=$(printf '%s' "$result_json" | jq -r '.num_turns // empty')
echo "::notice::Claude result subtype=$subtype is_error=$is_error duration_ms=$duration num_turns=$turns"

# Any scalar string fields on the result object, redacted, help debug config/API faults.
while IFS= read -r line; do
  [ -n "$line" ] || continue
  key=${line%%=*}
  val=${line#*=}
  val=$(printf '%s' "$val" | tr '\n' ' ' | sed -E 's/(bearer|sk-|api[_-]?key)[^[:space:]]+/[REDACTED]/gi')
  val=${val:0:500}
  echo "::notice::Claude result field ${key}=${val}"
done < <(printf '%s' "$result_json" | jq -r '
  to_entries[]
  | select(.value | type == "string")
  | select(.key | test("^(type|subtype|is_error|duration_ms|num_turns|total_cost_usd|permission_denials_count)$") | not)
  | "\(.key)=\(.value)"
')

# Tool results flagged as errors sometimes carry the provider message.
errors=$(jq -r '
  [.[].message.content[]? | select(.type == "tool_result" and .is_error == true) | .content]
  | flatten
  | map(select(type == "string"))
  | .[0] // empty
' "$exec_path" 2>/dev/null || true)
if [ -n "$errors" ]; then
  errors=$(printf '%s' "$errors" | tr '\n' ' ' | sed -E 's/(bearer|sk-|api[_-]?key)[^[:space:]]+/[REDACTED]/gi')
  errors=${errors:0:500}
  echo "::notice::Claude tool error excerpt=${errors}"
fi

assistant_text=$(jq -r '
  [.[].message.content[]? | select(.type == "text") | .text]
  | join(" ")
  | .[0:500] // empty
' "$exec_path" 2>/dev/null || true)
if [ -n "$assistant_text" ]; then
  assistant_text=$(printf '%s' "$assistant_text" | sed -E 's/(bearer|sk-|api[_-]?key)[^[:space:]]+/[REDACTED]/gi')
  echo "::notice::Claude assistant text excerpt=${assistant_text}"
fi

if [ "$route" = meta ] && [ "$is_error" = true ] && ! printf '%s' "$model" | grep -q '/'; then
  echo "::error::meta route model_id=$model has no provider prefix; set SWU_AUTOFIX_MODEL to meta/<id> (for muse use meta/muse-spark-1.3-contributor)"
fi
