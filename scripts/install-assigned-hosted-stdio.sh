#!/usr/bin/env bash
# Code only. The host's supported private credential mechanism injects ASSIGNED_MCP_BEARER.
set -euo pipefail
[ "$#" -eq 0 ] || { echo 'usage: install-assigned-hosted-stdio.sh' >&2; exit 2; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/.local/share/assigned-hosted-stdio"
mkdir -p "$dest" "$HOME/.local/bin"
install -m 0755 "$here/mcp-servers/assigned-hosted-stdio.py" "$dest/assigned-hosted-stdio.py"
ln -sfn "$dest/assigned-hosted-stdio.py" "$HOME/.local/bin/assigned-hosted-stdio"
echo 'Installed assigned-hosted-stdio code. Host owner must bind only the assigned bearer through supported private custody.'
