#!/usr/bin/env bash
set -euo pipefail

input="${AUTOFIX_EXECUTION_JSON:-${RUNNER_TEMP}/claude-execution-output.json}"
if [ ! -f "$input" ] || [ -L "$input" ]; then
  echo "::notice::Autofix execution log missing at $input; no provider error to surface"
  exit 0
fi

payload=$(
  jq -c '
    (if type == "array" then . else [.] end) as $msgs
    | ($msgs | map(select(.type == "result")) | last // {}) as $r
    | {
        is_error: ($r.is_error // false),
        subtype: ($r.subtype // "unknown"),
        duration_ms: ($r.duration_ms // null),
        reason: (
          if ($r | has("result")) and (($r.result | type) == "string") and (($r.result | length) > 0) then $r.result
          elif ($r | has("errors")) and (($r.errors | type) == "array") and (($r.errors | length) > 0) then ($r.errors[0] | tostring)
          else "" end
        ),
        assistant_error: (
          [$msgs[] | select(.type == "assistant") | .message.content[]? | select(.type == "text") | .text]
          | if length > 0 then .[-1] else "" end
        ),
        tool_error: (
          [$msgs[].message.content[]? | select(.type == "tool_result" and .is_error == true) | .content]
          | flatten
          | map(select(type == "string"))
          | if length > 0 then .[0] else "" end
        )
      }
  ' "$input" 2>/dev/null || echo '{}'
)

is_error=$(jq -r '.is_error // false' <<< "$payload")
if [ "$is_error" != "true" ] && [ "${CLAUDE_STEP_OUTCOME:-}" != "failure" ]; then
  exit 0
fi

reason=$(jq -r '.reason // empty' <<< "$payload")
assistant=$(jq -r '.assistant_error // empty' <<< "$payload")
tool_error=$(jq -r '.tool_error // empty' <<< "$payload")
subtype=$(jq -r '.subtype // "unknown"' <<< "$payload")
duration=$(jq -r '.duration_ms // empty' <<< "$payload")

sanitize() {
  local text="$1"
  text=$(printf '%s' "$text" | tr '\n' ' ' | sed -E 's/(Bearer|bearer) [A-Za-z0-9._-]+/[REDACTED]/g')
  text=$(printf '%s' "$text" | sed -E 's/(api[_-]?key|token|authorization)([=:])[[:space:]]*[^[:space:]]+/\1\2[REDACTED]/gi')
  text=$(printf '%s' "$text" | sed -E 's/sk-[A-Za-z0-9_-]+/[REDACTED]/g')
  printf '%s' "$text" | head -c 500
}

detail="$reason"
if [ -z "$detail" ] && [ -n "$tool_error" ]; then
  detail="$tool_error"
fi
if [ -z "$detail" ] && [ -n "$assistant" ]; then
  detail="$assistant"
fi
if [ -z "$detail" ]; then
  detail="Claude Code reported is_error with no provider message (subtype=$subtype, duration_ms=${duration:-unknown})"
fi
detail=$(sanitize "$detail")

route="${AUTOFIX_ROUTE:-unknown}"
model="${AUTOFIX_EFFECTIVE_MODEL:-unknown}"
base="${AUTOFIX_BASE_URL:-unknown}"

echo "::error::Autofix model run failed (route=$route base=$base model=$model subtype=$subtype duration_ms=${duration:-unknown}): $detail"
