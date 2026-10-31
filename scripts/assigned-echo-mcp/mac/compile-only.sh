#!/bin/bash
# Nonsecret native source check only. No sign, install, item, ACL or storage effect.
set -euo pipefail
[ "$#" -eq 0 ] || exit 2
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
SAN='-fsanitize=address,undefined'
# Pure-C protocol core: behavioral fixture (42 checks), plain and sanitized.
/usr/bin/clang -std=c99 -Wall -Wextra -Werror -I"$here" "$here/mac-fixture.c" -o "$tmp/mac-fixture"
"$tmp/mac-fixture"
/usr/bin/clang -std=c99 -Wall -Wextra -Werror $SAN -I"$here" "$here/mac-fixture.c" -o "$tmp/mac-fixture-san"
"$tmp/mac-fixture-san"
# Pure-C deadline-aware framing: synthetic-input fixture (22 checks), plain and sanitized.
/usr/bin/clang -std=c99 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Werror -I"$here" \
    "$here/mac-input-fixture.c" "$here/mac-input.c" -o "$tmp/mac-input-fixture"
"$tmp/mac-input-fixture"
/usr/bin/clang -std=c99 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Werror $SAN -I"$here" \
    "$here/mac-input-fixture.c" "$here/mac-input.c" -o "$tmp/mac-input-fixture-san"
"$tmp/mac-input-fixture-san"
# Hard process cap:  the alarm fixture must die with SIGALRM even when it was inherited
# ignored, blocked, or both.  Nonsensitive;  actually runs here.
/usr/bin/clang -std=c99 -Wall -Wextra -Werror "$here/mac-alarm-fixture.c" -o "$tmp/mac-alarm-fixture"
/usr/bin/clang -std=c99 -Wall -Wextra -Werror "$here/mac-alarm-fixture-driver.c" -o "$tmp/mac-alarm-driver"
"$tmp/mac-alarm-driver" "$tmp/mac-alarm-fixture"
# ObjC transport harness:  the NSURLProtocol stub intercepts every request, so no network
# is reachable.  Test hooks are compiled into the fixture build only (28 checks).
/usr/bin/clang -fobjc-arc -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -DMP_TRANSPORT_TEST_HOOKS -framework Foundation -I"$here" \
    "$here/mac-transport-fixture.m" "$here/mac-transport.m" -o "$tmp/mac-transport-fixture"
"$tmp/mac-transport-fixture"
/usr/bin/clang -fobjc-arc -Wall -Wextra -Werror -Wno-deprecated-declarations $SAN \
    -DMP_TRANSPORT_TEST_HOOKS -framework Foundation -I"$here" \
    "$here/mac-transport-fixture.m" "$here/mac-transport.m" -o "$tmp/mac-transport-fixture-san"
"$tmp/mac-transport-fixture-san"
# The synthetic ACL fixture and the production helper:  compile only.  The fixture runs
# exclusively in the isolated test Keychain flow of acl-fixture-plan.sh after approval;
# the helper is never signed, installed or run in this harness.
/usr/bin/clang -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -framework Security -framework CoreFoundation "$here/mac-acl-fixture.c" -o "$tmp/mac-acl-fixture"
/usr/bin/clang -fobjc-arc -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -framework Foundation -framework Security -I"$here" \
    "$here/mac-request.m" "$here/mac-transport.m" -o "$tmp/mac-request"
# Unsigned is EXPECTED here. Never codesign --sign in this harness.
/usr/bin/codesign --display --verbose=2 "$tmp/mac-request" 2>&1 || true
echo 'Native sources compiled and nonsensitive fixtures ran. Signed fixed-path ACL and all custody effects remain unqualified.'
