#!/usr/bin/env bash
# Code-only ECHO installer. Never provisions a credential or starts a service.
set -euo pipefail
[ "$#" -eq 0 ] || { echo 'No seat or path selectors are supported.' >&2; exit 2; }
[ "$(/usr/bin/uname -s)" = Linux ] && [ "$(/usr/bin/id -u)" = 0 ] || { echo 'Linux root installer required.' >&2; exit 3; }
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
adapter="$here/../mcp-servers/assigned-hosted-stdio.py"
[ -f "$adapter" ] && [ -f "$here/linux-custody.py" ] || { echo 'Reviewed assigned hosted adapter and custody source must both be present.' >&2; exit 3; }
# Refuse existing symlink targets or writable/unowned ancestors. No arbitrary override.
/usr/bin/python3 -I - <<'PY'
from pathlib import Path
import stat
for path in (Path('/usr/local'), Path('/usr/local/libexec')):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Unsafe fixed code ancestor')
path = Path('/usr/local/libexec/assigned-echo-mcp')
if path.exists() or path.is_symlink():
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Unsafe fixed code directory')
    for name in ('linux-custody.py', 'assigned-hosted-stdio.py'):
        entry = path / name
        if entry.exists() or entry.is_symlink():
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SystemExit('Unsafe installed code')
PY
/usr/bin/install -d -o root -g root -m 0755 /usr/local/libexec/assigned-echo-mcp
/usr/bin/install -o root -g root -m 0755 "$adapter" /usr/local/libexec/assigned-echo-mcp/assigned-hosted-stdio.py
/usr/bin/install -o root -g root -m 0755 "$here/linux-custody.py" /usr/local/libexec/assigned-echo-mcp/linux-custody.py
echo 'Installed fixed ECHO code only. No credential, host key or service was created.'
