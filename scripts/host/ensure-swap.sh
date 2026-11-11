#!/usr/bin/env bash
# Idempotent swap + swappiness for fleet-hetzner-nbg1.
#
# The 30GiB box has container RAM caps ~40-50% above physical (TEI 12GiB,
# Qdrant 12GiB, plus ST/CT/UM).  Swap is the overflow, not a second RAM
# tier.  Default Linux swappiness=60 will page TEI/Qdrant out just to
# keep file cache while MemAvailable is still ~20GiB — that is the
# wrong trigger.  We keep a 16GiB swapfile set and swappiness=20 so
# swap is used when cgroups actually oversubscribe, not when cache is
# hungry.
#
# Usage (root on the Coolify host):
#   bash scripts/host/ensure-swap.sh
#   bash scripts/host/ensure-swap.sh --dry-run
#   TARGET_SWAP_GIB=16 SWAPPINESS=20 bash scripts/host/ensure-swap.sh
#
# Does not print secrets.  Does not swapoff the existing /swapfile.
# Extra pages live in /swapfile.extra so a 4GiB in-use file stays up.
set -euo pipefail

TARGET_SWAP_GIB="${TARGET_SWAP_GIB:-16}"
SWAPPINESS="${SWAPPINESS:-20}"
EXTRA_FILE="${EXTRA_SWAPFILE:-/swapfile.extra}"
SYSCTL_FILE="${SYSCTL_FILE:-/etc/sysctl.d/99-fleet-swap.conf}"
FSTAB="${FSTAB:-/etc/fstab}"
DRY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 && "$DRY" -ne 1 ]]; then
  echo "run as root (or --dry-run)" >&2
  exit 1
fi

kib_to_gib() {
  # /proc/meminfo kB → integer GiB, floor.
  awk -v k="$1" 'BEGIN { printf "%d", int(k / 1024 / 1024) }'
}

swap_total_kib() {
  awk '/^SwapTotal:/ { print $2; exit }' /proc/meminfo
}

disk_avail_gib() {
  df -Pk / | awk 'NR==2 { printf "%d", int($4 / 1024 / 1024) }'
}

need_root_actions() {
  [[ "$DRY" -eq 1 ]] && return 1
  return 0
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ ! -r /proc/meminfo ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    log "dry-run: no /proc/meminfo (not Linux); would target ${TARGET_SWAP_GIB}GiB swappiness_target ${SWAPPINESS}"
    exit 0
  fi
  echo "linux /proc/meminfo required" >&2
  exit 1
fi

CURRENT_KIB="$(swap_total_kib)"
CURRENT_GIB="$(kib_to_gib "$CURRENT_KIB")"
AVAIL_GIB="$(disk_avail_gib)"
NEED_GIB=$(( TARGET_SWAP_GIB - CURRENT_GIB ))
if [[ "$NEED_GIB" -lt 0 ]]; then
  NEED_GIB=0
fi

log "swap now ${CURRENT_GIB}GiB (target ${TARGET_SWAP_GIB}GiB) disk_avail ${AVAIL_GIB}GiB swappiness_target ${SWAPPINESS}"

if [[ "$NEED_GIB" -gt 0 ]]; then
  # Keep 20GiB free after allocating the extra swapfile.
  HEADROOM=20
  if [[ "$AVAIL_GIB" -lt $(( NEED_GIB + HEADROOM )) ]]; then
    echo "not enough disk for +${NEED_GIB}GiB swap (avail ${AVAIL_GIB}GiB, want ${HEADROOM}GiB headroom)" >&2
    exit 1
  fi
  if [[ -e "$EXTRA_FILE" ]]; then
    extra_bytes="$(stat -c '%s' "$EXTRA_FILE" 2>/dev/null || echo 0)"
    extra_gib="$(awk -v b="$extra_bytes" 'BEGIN { printf "%d", int(b / 1024 / 1024 / 1024) }')"
    if [[ "$extra_gib" -ge "$NEED_GIB" ]]; then
      log "reuse $EXTRA_FILE (${extra_gib}GiB)"
    else
      echo "$EXTRA_FILE exists but is ${extra_gib}GiB < +${NEED_GIB}GiB; remove it by hand if you want a rebuild" >&2
      exit 1
    fi
  else
    log "create $EXTRA_FILE +${NEED_GIB}GiB"
    if need_root_actions; then
      fallocate -l "${NEED_GIB}G" "$EXTRA_FILE"
      chmod 600 "$EXTRA_FILE"
      mkswap "$EXTRA_FILE" >/dev/null
    fi
  fi
  if need_root_actions; then
    if ! swapon --show=NAME --noheadings | grep -qx "$EXTRA_FILE"; then
      swapon "$EXTRA_FILE"
    fi
    if ! grep -qE "^${EXTRA_FILE}[[:space:]]" "$FSTAB"; then
      printf '%s none swap sw 0 0\n' "$EXTRA_FILE" >> "$FSTAB"
    fi
  else
    log "dry-run: would swapon $EXTRA_FILE and fstab it"
  fi
fi

log "set vm.swappiness=${SWAPPINESS}"
if need_root_actions; then
  printf 'vm.swappiness=%s\n' "$SWAPPINESS" > "$SYSCTL_FILE"
  sysctl -w "vm.swappiness=${SWAPPINESS}" >/dev/null
else
  log "dry-run: would write $SYSCTL_FILE and sysctl -w vm.swappiness=${SWAPPINESS}"
fi

log "done SwapTotal=$(awk '/^SwapTotal:/ { print $2 }' /proc/meminfo)kB swappiness=$(cat /proc/sys/vm/swappiness)"
