#!/bin/bash
# Contract tests for fleet-housekeep.sh.
# Zero zombies must be a single integer 0 (not 0\n0 from grep -c || echo 0).
# failed-units must be empty or real unit names, never ● table glyphs.
# Default IGNORE_FAILED_UNITS drops grub-initrd-fallback and cloud-init-hotplugd
# (with or without .service); those alone must not set level=warn.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${ROOT}/fleet-housekeep.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$SCRIPT" ] || fail "missing $SCRIPT"
bash -n "$SCRIPT" || fail "bash -n $SCRIPT"

if grep -nE "grep -c '\\^Z' \\|\\| echo 0" "$SCRIPT"; then
  fail "grep -c || echo 0 concatenates 0\\n0"
fi
if ! grep -q 'systemctl --failed --plain --no-legend' "$SCRIPT"; then
  fail "must list failed units with systemctl --failed --plain --no-legend"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export STATE_DIR="$tmp"
export WARN_USED_PCT=101
export CRIT_USED_PCT=101
export ZOMBIE_WARN=10
# Exercise the script default (grub-initrd-fallback|cloud-init-hotplugd).
unset IGNORE_FAILED_UNITS
export HOUSEKEEP_LIB_ONLY=1
# shellcheck disable=SC1090
source "$SCRIPT"

if [[ ! "$IGNORE_FAILED_UNITS" =~ grub-initrd-fallback ]]; then
  fail "default IGNORE_FAILED_UNITS must include grub-initrd-fallback"
fi
if [[ ! "$IGNORE_FAILED_UNITS" =~ cloud-init-hotplugd ]]; then
  fail "default IGNORE_FAILED_UNITS must include cloud-init-hotplugd"
fi

# --- count_zombie_stats: no match is one line, integer 0 ---
got=$(printf '%s\n' 'STAT' 'Ss' 'R+' 'S' | count_zombie_stats)
if [[ "$got" != "0" ]]; then
  fail "count_zombie_stats empty-match want 0 got $(printf '%q' "$got")"
fi
if [[ "$got" == *$'\n'* ]]; then
  fail "count_zombie_stats extra newline: $(printf '%q' "$got")"
fi
if ! [[ "$got" -ge "$ZOMBIE_WARN" || "$got" -lt "$ZOMBIE_WARN" ]]; then
  fail "count_zombie_stats=0 is not a usable integer"
fi
if [[ "$got" -ge "$ZOMBIE_WARN" ]]; then
  fail "zero zombies must not reach ZOMBIE_WARN"
fi

# The old pipeline is the bug: two zeros, -ge cannot parse.
buggy=$(printf '%s\n' 'STAT' 'Ss' | { grep -c '^Z' || echo 0; })
if [[ "$buggy" == "0" ]]; then
  fail "sanity: old grep -c || echo 0 should still emit 0\\n0"
fi
if [[ $(printf '%s\n' "$buggy" | wc -l | tr -d ' ') -lt 2 ]]; then
  fail "sanity: old pipeline should be two lines, got $(printf '%q' "$buggy")"
fi

got=$(printf '%s\n' 'STAT' 'Ss' 'Z' 'Z+' 'R' | count_zombie_stats)
if [[ "$got" != "2" ]]; then
  fail "count_zombie_stats want 2 zombies got $(printf '%q' "$got")"
fi

ps() { printf '%s\n' 'STAT' 'Ss' 'S+' 'Rl'; }
got=$(count_zombies)
if [[ "$got" != "0" ]]; then
  fail "count_zombies=0 want 0 got $(printf '%q' "$got")"
fi
if [[ "$(count_zombies | wc -l | tr -d ' ')" != "1" ]]; then
  fail "count_zombies=0 must be one line"
fi

# --- parse_failed_unit_names: table bullets vs --plain ---
table_out=$(printf '%s\n' \
  'UNIT LOAD ACTIVE SUB DESCRIPTION' \
  '● grub-initrd-fallback.service loaded failed failed GRUB' \
  '● cups.service loaded failed failed Printer' \
  | parse_failed_unit_names)
if [[ "$table_out" == *"●"* ]]; then
  fail "parse_failed_unit_names emitted ●: $(printf '%q' "$table_out")"
fi
if [[ "$table_out" != $'grub-initrd-fallback.service\ncups.service' ]]; then
  fail "parse table want unit names got $(printf '%q' "$table_out")"
fi

plain_out=$(printf '%s\n' \
  'grub-initrd-fallback.service loaded failed failed GRUB' \
  'cups.service loaded failed failed Printer' \
  | parse_failed_unit_names)
if [[ "$plain_out" == *"●"* ]]; then
  fail "parse --plain emitted ●: $(printf '%q' "$plain_out")"
fi
if [[ "$plain_out" != $'grub-initrd-fallback.service\ncups.service' ]]; then
  fail "parse --plain want unit names got $(printf '%q' "$plain_out")"
fi

empty_out=$(printf '' | parse_failed_unit_names)
if [[ -n "$empty_out" ]]; then
  fail "parse empty want empty got $(printf '%q' "$empty_out")"
fi

# --- list_actionable_failed_units uses --plain and IGNORE ---
systemctl() {
  if [[ " $* " != *" --failed "* ]]; then
    return 0
  fi
  if [[ " $* " != *" --plain "* ]]; then
    printf '%s\n' \
      '● grub-initrd-fallback.service loaded failed failed GRUB' \
      '● cups.service loaded failed failed Printer'
    return 0
  fi
  printf '%s\n' \
    'grub-initrd-fallback.service loaded failed failed GRUB' \
    'cloud-init-hotplugd.service loaded failed failed Hotplug' \
    'cloud-init-hotplugd loaded failed failed Hotplug' \
    'cups.service loaded failed failed Printer'
}

got=$(list_actionable_failed_units)
if [[ "$got" == *"●"* ]]; then
  fail "actionable units emitted ●: $(printf '%q' "$got")"
fi
if [[ "$got" == *"cloud-init-hotplugd"* ]]; then
  fail "actionable must ignore cloud-init-hotplugd, got $(printf '%q' "$got")"
fi
if [[ "$got" == *"grub-initrd-fallback"* ]]; then
  fail "actionable must ignore grub-initrd-fallback, got $(printf '%q' "$got")"
fi
if [[ "$got" != "cups.service" ]]; then
  fail "actionable want cups.service (ignored units dropped) got $(printf '%q' "$got")"
fi

# --- main: zero zombies + zero failed units => level=ok, not warn ---
ps() { printf '%s\n' 'STAT' 'Ss' 'S'; }
systemctl() { return 0; }
mkdir -p "$STATE_DIR"
main
status=$(cat "$STATE_DIR/last-status")
if [[ "$(wc -l <"$STATE_DIR/last-status" | tr -d ' ')" != "1" ]]; then
  fail "last-status must be one line, got: $status"
fi
if [[ "$status" != *"zombies=0 "* ]]; then
  fail "last-status want zombies=0 got: $status"
fi
if [[ "$status" != *"level=ok" ]]; then
  fail "zero zombies and zero failed units must not warn, got: $status"
fi
if [[ "$status" == *"●"* ]]; then
  fail "last-status must not contain ●: $status"
fi

# --- main: cloud-init-hotplugd.service alone => level=ok, not warn ---
ps() { printf '%s\n' 'STAT' 'Ss'; }
systemctl() {
  if [[ " $* " == *" --failed "* && " $* " == *" --plain "* ]]; then
    printf '%s\n' 'cloud-init-hotplugd.service loaded failed failed Hotplug'
    return 0
  fi
  printf '%s\n' '● cloud-init-hotplugd.service loaded failed failed Hotplug'
}
main
status=$(cat "$STATE_DIR/last-status")
if [[ "$status" != *"level=ok" ]]; then
  fail "cloud-init-hotplugd.service alone must not warn, got: $status"
fi
if [[ "$status" == *"level=warn"* ]]; then
  fail "cloud-init-hotplugd.service alone must not set level=warn, got: $status"
fi
if [[ "$status" == *"failed=cloud-init-hotplugd"* ]]; then
  fail "ignored cloud-init-hotplugd must not appear as failed=, got: $status"
fi

# --- main: cloud-init-hotplugd (no .service) alone => level=ok ---
systemctl() {
  if [[ " $* " == *" --failed "* && " $* " == *" --plain "* ]]; then
    printf '%s\n' 'cloud-init-hotplugd loaded failed failed Hotplug'
    return 0
  fi
}
main
status=$(cat "$STATE_DIR/last-status")
if [[ "$status" != *"level=ok" ]]; then
  fail "cloud-init-hotplugd alone must not warn, got: $status"
fi
if [[ "$status" == *"level=warn"* ]]; then
  fail "cloud-init-hotplugd alone must not set level=warn, got: $status"
fi

# --- main: real failed unit still warns (ignored units do not hide it) ---
ps() { printf '%s\n' 'STAT' 'Ss'; }
systemctl() {
  if [[ " $* " == *" --failed "* && " $* " == *" --plain "* ]]; then
    printf '%s\n' \
      'grub-initrd-fallback.service loaded failed failed GRUB' \
      'cloud-init-hotplugd.service loaded failed failed Hotplug' \
      'cups.service loaded failed failed Printer'
    return 0
  fi
  printf '%s\n' '● cups.service loaded failed failed Printer'
}
main
status=$(cat "$STATE_DIR/last-status")
if [[ "$status" == *"●"* ]]; then
  fail "last-status failed-units must not be ●: $status"
fi
if [[ "$status" != *"failed=cups.service"* ]]; then
  fail "last-status want failed=cups.service got: $status"
fi
if [[ "$status" == *"cloud-init-hotplugd"* ]]; then
  fail "last-status failed= must not include ignored cloud-init-hotplugd: $status"
fi
if [[ "$status" != *"level=warn" ]]; then
  fail "real failed unit should warn, got: $status"
fi
if [[ "$status" != *"zombies=0 "* ]]; then
  fail "last-status want zombies=0 next to failed unit, got: $status"
fi

echo "ok  housekeep $(bash -n "$SCRIPT" && echo 'bash -n clean')"
