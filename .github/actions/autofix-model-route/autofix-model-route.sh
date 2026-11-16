#!/usr/bin/env bash
set -euo pipefail

VAR_BASE="${VAR_BASE:-}"
VAR_MODEL="${VAR_MODEL:-}"
DEFAULT_MODEL="${DEFAULT_MODEL:-MiniMax-M3.1-Flash}"

repo_root="${GITHUB_WORKSPACE:-.}"
if [ -f "$repo_root/scripts/autofix_provider_model.sh" ]; then
  # shellcheck source=scripts/autofix_provider_model.sh
  source "$repo_root/scripts/autofix_provider_model.sh"
  VAR_BASE=$(autofix_trim "$VAR_BASE")
  VAR_MODEL=$(autofix_trim "$VAR_MODEL")
fi

route=
case "$VAR_BASE" in
  "" | https://api.minimax.io/anthropic) route=minimax ;;
  https://api.meta.ai) route=meta ;;
  *)
    echo "::error::SWU_AUTOFIX_MODEL_BASE_URL is not a supported autofix endpoint"
    exit 1
    ;;
esac

if [ "$route" = "minimax" ]; then
  effective="${VAR_MODEL:-$DEFAULT_MODEL}"
  {
    echo "route=minimax"
    echo "anthropic_api_key_mode=minimax"
    echo "effective_model=$effective"
  } >> "$GITHUB_OUTPUT"
  echo "ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic" >> "$GITHUB_ENV"
  echo "ANTHROPIC_MODEL=$effective" >> "$GITHUB_ENV"
  echo "ANTHROPIC_SMALL_FAST_MODEL=$effective" >> "$GITHUB_ENV"
  exit 0
fi

effective="${VAR_MODEL:-muse-spark-1.3}"
case "$effective" in
  muse-spark-1.3-contributor) effective=muse-spark-1.3 ;;
esac

if [[ ! "$effective" =~ ^muse- ]]; then
  echo "::error::SWU_AUTOFIX_MODEL must name a muse-* model id when SWU_AUTOFIX_MODEL_BASE_URL is https://api.meta.ai (got a non-muse id; use muse-spark-1.3 or muse-wonder-1.0)"
  exit 1
fi

mapped=no
if [ "${VAR_MODEL:-}" = "muse-spark-1.3-contributor" ] && [ "$effective" = "muse-spark-1.3" ]; then
  mapped=yes
fi

{
  echo "route=meta"
  echo "anthropic_api_key_mode=none"
  echo "effective_model=$effective"
} >> "$GITHUB_OUTPUT"

echo "ANTHROPIC_BASE_URL=https://api.meta.ai" >> "$GITHUB_ENV"
echo "ANTHROPIC_MODEL=$effective" >> "$GITHUB_ENV"
echo "ANTHROPIC_DEFAULT_OPUS_MODEL=$effective" >> "$GITHUB_ENV"
echo "ANTHROPIC_DEFAULT_SONNET_MODEL=$effective" >> "$GITHUB_ENV"
echo "ANTHROPIC_DEFAULT_HAIKU_MODEL=$effective" >> "$GITHUB_ENV"
echo "CLAUDE_CODE_SUBAGENT_MODEL=$effective" >> "$GITHUB_ENV"
echo "ENABLE_TOOL_SEARCH=true" >> "$GITHUB_ENV"
echo "ANTHROPIC_API_KEY=" >> "$GITHUB_ENV"

if [ "$mapped" = "yes" ]; then
  echo "::notice::SWU_AUTOFIX_MODEL muse-spark-1.3-contributor is an OpenRouter/Muse-local id; using muse-spark-1.3 on https://api.meta.ai"
fi
