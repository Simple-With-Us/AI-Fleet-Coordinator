#!/bin/bash
# install-infisical-scoped.sh -- install the shared Infisical client for consumers that live
# outside this repository (the Mac-local tools under ~/apps).
#
# The shared reader CLI is retired.  A consumer that lives outside this repository still needs
# the same client this repository uses, so that one file is installed once, at a stable path,
# and those tools import it:
#
#   sys.path.insert(0, "/Users/jay/apps/infisical-scoped")
#   import fleet_infisical
#
# The reader next to it, `infisical-scoped.py`, is the one command a consumer outside
# this repository runs:  it speaks `get`, `names` and `doctor`, reads through the shared
# client, and uses the consumer's own scoped service token from the consumer's own store.
#
# It is a plain copy of scripts/fleet_infisical.py, not a symlink into a checkout: a symlink
# would break when the checkout moves, and a lane checkout is not a stable install location.
#
# The client is service-token only.  It holds no credential: the consumer's own scoped token
# comes from the consumer's own store (the owner's login keychain on the Mac).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/apps/infisical-scoped}"

install -d -m 0755 "$DEST"
install -m 0644 "$SRC_DIR/fleet_infisical.py" "$DEST/fleet_infisical.py"
install -m 0755 "$SRC_DIR/infisical-scoped.py" "$DEST/infisical-scoped.py"

python3 - "$DEST/fleet_infisical.py" <<'PY'
import ast, sys
path = sys.argv[1]
ast.parse(open(path, encoding="utf-8").read())
print("installed:", path)
PY
