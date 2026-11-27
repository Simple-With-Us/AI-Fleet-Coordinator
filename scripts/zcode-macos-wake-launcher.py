#!/usr/bin/env python3
"""Launch the installed macOS ZCode CLI with its bundled provider resource.

The app's CLI bundle is outside the standalone package layout its relative
resource lookup expects. Supply only the actual bundled resource path. The CLI
still owns active-cache refresh and the existing personal provider config.
No login, model selection, session creation or wake retry happens here.
"""

import os
import sys
from pathlib import Path

NODE = "/opt/homebrew/bin/node"
CLI = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
BUILTIN = "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json"
BUILTIN_ENV = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"


def launch(args, env=None, *, exec_fn=os.execve, is_file=None):
    exists = is_file or (lambda path: Path(path).is_file())
    if not all(exists(path) for path in (NODE, CLI, BUILTIN)):
        raise RuntimeError("zcode-macos-launch-resource-unavailable")
    child = dict(os.environ if env is None else env)
    # Explicit supported caller configuration wins. Never inspect or copy its
    # contents and never replace the CLI's personal provider selection.
    if not child.get(BUILTIN_ENV, "").strip():
        child[BUILTIN_ENV] = BUILTIN
    exec_fn(NODE, [NODE, CLI, *args], child)


if __name__ == "__main__":
    try:
        launch(sys.argv[1:])
    except (OSError, RuntimeError):
        print("zcode-macos-launch-resource-unavailable", file=sys.stderr)
        sys.exit(1)
