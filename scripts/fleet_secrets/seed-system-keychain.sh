#!/usr/bin/env bash
# Seed the System keychain with the Infisical automation machine identity, so `fleet-secrets` can
# authenticate from pm2 and other boot-time jobs (OPS-818).  Run ONCE by the owner, with sudo.
#
#   { fleet-secrets get INFISICAL_AUTOMATION_CLIENT_ID; echo; \
#     fleet-secrets get INFISICAL_AUTOMATION_CLIENT_SECRET; echo; } \
#     | sudo bash scripts/fleet_secrets/seed-system-keychain.sh
#
# Why stdin:  under sudo, $HOME and the login session are different, so the script cannot reach
# Infisical itself.  The two `fleet-secrets get` calls run as you, in your GUI shell, and pipe the
# client id (line 1) and client secret (line 2) here.  The script reads both lines, writes two
# generic passwords into /Library/Keychains/System.keychain (service fleet-secrets, accounts
# infisical-client-id and infisical-client-secret), clears the variables, reads the items back, and
# prints only `match: true` or `match: false` for each.  It never prints a value.
#
# No value on any command line:  each item is added by feeding the `add-generic-password` command to
# `security -i` on its standard input, so the values never reach an argument list or the process
# list.  Because the command is one line of text, a value outside [A-Za-z0-9+/=_.:@-] (or starting
# with "-") is refused, naming the account only;  universal-auth ids and secrets are hex or base64.
# `security -i` exits 0 even when a command fails, so success is decided by the read-back below.
# `-A` lets any application read the items without a prompt, which is what a boot-time daemon needs
# (owner accepted, 2026-10-18);  anyone with an account on this Mac can read them with `security`.
# Rotate the machine identity in Infisical to revoke.  `-U` updates an existing item, so re-running
# is safe.
#
# Test hooks:  SECURITY_BIN (default /usr/bin/security) and FLEET_SECRETS_KEYCHAIN (default the
# System keychain;  a different path skips the root check).  Used by the unit tests only.
set -uo pipefail

SECURITY="${SECURITY_BIN:-/usr/bin/security}"
KEYCHAIN="${FLEET_SECRETS_KEYCHAIN:-/Library/Keychains/System.keychain}"
SERVICE="fleet-secrets"
ACCT_ID="infisical-client-id"
ACCT_SECRET="infisical-client-secret"

die() { echo "seed-system-keychain: $*" >&2; exit "${2:-1}"; }

if [ -z "${FLEET_SECRETS_KEYCHAIN:-}" ] && [ "$(id -u)" -ne 0 ]; then
  die "run it with sudo (it writes $KEYCHAIN)" 2
fi
[ -x "$SECURITY" ] || die "security tool not found" 2
if [ -t 0 ]; then
  die "pipe the client id and client secret in on stdin, one per line (see the header of this script)" 2
fi

client_id="" client_secret=""
IFS= read -r client_id || true
IFS= read -r client_secret || true
client_id="${client_id%$'\r'}"
client_secret="${client_secret%$'\r'}"

if [ -z "$client_id" ] || [ -z "$client_secret" ]; then
  client_id="" client_secret=""
  die "stdin must hold two non-empty lines:  the client id, then the client secret (a fleet-secrets get probably failed;  run fleet-secrets doctor)" 3
fi

safe_value() { # value:  one token that cannot break the `security -i` line
  [[ "$1" =~ ^[A-Za-z0-9+/=_.:@][A-Za-z0-9+/=_.:@-]*$ ]]
}
if ! safe_value "$client_id"; then
  client_id="" client_secret=""
  die "the value for $ACCT_ID has a character that is not allowed (whitespace, a quote, a backslash or another symbol);  nothing was written" 3
fi
if ! safe_value "$client_secret"; then
  client_id="" client_secret=""
  die "the value for $ACCT_SECRET has a character that is not allowed (whitespace, a quote, a backslash or another symbol);  nothing was written" 3
fi

add_item() { # account value:  the command goes to `security -i` on stdin, never on argv
  printf 'add-generic-password -U -A -s %s -a %s -w %s %s\n' "$SERVICE" "$1" "$2" "$KEYCHAIN" \
    | "$SECURITY" -i >/dev/null 2>&1
}

wrote=true
add_item "$ACCT_ID" "$client_id" || wrote=false
add_item "$ACCT_SECRET" "$client_secret" || wrote=false
if [ "$wrote" != true ]; then
  client_id="" client_secret=""
  die "security -i add-generic-password failed (is the System keychain unlocked and writable?)" 4
fi

read_item() { # account  -> value on stdout, empty when absent
  "$SECURITY" find-generic-password -s "$SERVICE" -a "$1" -w "$KEYCHAIN" 2>/dev/null
}

rc=0
back_id="$(read_item "$ACCT_ID")"
back_secret="$(read_item "$ACCT_SECRET")"
if [ -n "$back_id" ] && [ "$back_id" = "$client_id" ]; then echo "infisical-client-id match: true"; else echo "infisical-client-id match: false"; rc=5; fi
if [ -n "$back_secret" ] && [ "$back_secret" = "$client_secret" ]; then echo "infisical-client-secret match: true"; else echo "infisical-client-secret match: false"; rc=5; fi

# Also prove the invoking (non-root) user can read them, which is what pm2 does.
if [ -n "${SUDO_USER:-}" ] && [ "$(id -u)" -eq 0 ] && [ -z "${FLEET_SECRETS_KEYCHAIN:-}" ]; then
  u_id="$(sudo -u "$SUDO_USER" "$SECURITY" find-generic-password -s "$SERVICE" -a "$ACCT_ID" -w "$KEYCHAIN" 2>/dev/null)"
  u_secret="$(sudo -u "$SUDO_USER" "$SECURITY" find-generic-password -s "$SERVICE" -a "$ACCT_SECRET" -w "$KEYCHAIN" 2>/dev/null)"
  if [ -n "$u_id" ] && [ "$u_id" = "$client_id" ]; then echo "readable by $SUDO_USER (client id) match: true"; else echo "readable by $SUDO_USER (client id) match: false"; rc=5; fi
  if [ -n "$u_secret" ] && [ "$u_secret" = "$client_secret" ]; then echo "readable by $SUDO_USER (client secret) match: true"; else echo "readable by $SUDO_USER (client secret) match: false"; rc=5; fi
  u_id="" u_secret=""
fi

client_id="" client_secret="" back_id="" back_secret=""
if [ "$rc" -eq 0 ]; then echo "next: fleet-secrets doctor   (system_keychain_*_present should be true)"; fi
exit "$rc"
