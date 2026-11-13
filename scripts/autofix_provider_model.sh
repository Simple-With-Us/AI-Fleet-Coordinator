#!/usr/bin/env bash
# Resolve autofix provider route, base URL, and model id for Claude Code CI.
# Sourced by workflow steps; also exercised by scripts/test_autofix_provider_model.py.
set -euo pipefail

autofix_trim() {
  local value=$1
  printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

autofix_route_for_base() {
  local base
  base=$(autofix_trim "$1")
  case "$base" in
    "" | https://api.minimax.io/anthropic) printf '%s' minimax ;;
    https://api.meta.ai) printf '%s' meta ;;
    *) printf '%s' invalid ;;
  esac
}

autofix_model_is_minimax() {
  case "$1" in [Mm][Ii][Nn][Ii][Mm][Aa][Xx]*) return 0 ;; esac
  return 1
}

# Meta muse models are registered as meta/<id> on OpenRouter and Meta routes.
autofix_resolve_model_for_route() {
  local route=$1
  local configured=$2
  local fallback=$3
  local effective
  effective=${configured:-$fallback}
  effective=$(autofix_trim "$effective")
  if [ "$route" = meta ] && [ -n "$effective" ] && ! printf '%s' "$effective" | grep -q '/'; then
    case "$effective" in
      muse-* | Muse-*)
        effective="meta/$effective"
        ;;
    esac
  fi
  printf '%s' "$effective"
}

autofix_default_base_for_route() {
  case "$1" in
    meta) printf '%s' 'https://api.meta.ai' ;;
    *) printf '%s' 'https://api.minimax.io/anthropic' ;;
  esac
}
