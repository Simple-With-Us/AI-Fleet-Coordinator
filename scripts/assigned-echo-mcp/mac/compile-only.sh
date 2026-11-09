#!/bin/bash
# Nonsecret native source check only. No sign, install, item, ACL or storage effect.
set -euo pipefail
[ "$#" -eq 0 ] || exit 2
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
/usr/bin/clang -Wall -Wextra -Werror -Wno-deprecated-declarations -framework Security -framework CoreFoundation "$here/assigned-echo-mcp.c" -o "$tmp/assigned-echo-mcp"
/usr/bin/clang -Wall -Wextra -Werror -Wno-deprecated-declarations -framework Security -framework CoreFoundation "$here/acl-fixture.c" -o "$tmp/acl-fixture"
# Unsigned is EXPECTED here. Never codesign --sign in this harness.
/usr/bin/codesign --display --verbose=2 "$tmp/assigned-echo-mcp" 2>&1 || true
echo 'Native source compiled only. Signed fixed-path ACL and all custody effects remain unqualified.'
