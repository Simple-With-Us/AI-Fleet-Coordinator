"""The F3 helpers, loaded by file path.

`agent_sync/textsafe.py`, `secretscan.py` and `identity.py` import nothing from their package, so this
tool loads them as plain files.  It never runs `import agent_sync`, which would pull in the Zulip
client and the whole CLI, and a stable copy (`~/apps/needs-owner`, run with `python3 -I`) holds only
these three files next to the `needs_owner` package.  In a checkout the same relative place is
`scripts/agent_sync/`.

    f3.textsafe.render_untrusted(text, cap, code_span=False)
    f3.secretscan.scan(text)
    f3.identity.resolve_seat(env), f3.identity.canonical_seat(raw), f3.identity.valid_seat(raw)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the directory holding needs_owner/ and agent_sync/
FILES = ("textsafe", "secretscan", "identity")


class F3Missing(RuntimeError):
    pass


def _load(name: str) -> ModuleType:
    path = os.path.join(ROOT, "agent_sync", name + ".py")
    if not os.path.isfile(path):
        raise F3Missing("needs-owner cannot find %s (the stable copy is incomplete;  run scripts/needs_owner/install.sh --apply)" % path)
    spec = importlib.util.spec_from_file_location("_needs_owner_f3_" + name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib refuses only a non-.py name
        raise F3Missing("cannot load " + path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


textsafe = _load("textsafe")
secretscan = _load("secretscan")
identity = _load("identity")
