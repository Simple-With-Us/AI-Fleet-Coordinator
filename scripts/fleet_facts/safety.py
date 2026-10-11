"""The F3 safety helpers, loaded by path.

`agent_sync/textsafe.py` (render_untrusted) and `agent_sync/secretscan.py` (redact_fields) import nothing
from their package, so they are loaded by file path from `<this package's parent>/agent_sync/`.  That is
`scripts/agent_sync/` in the repository and `<stable copy>/agent_sync/` in an installed copy (install.sh
copies exactly those two files plus an empty `__init__.py`).  A stable copy run with `python3 -I` therefore
needs nothing else, and there is no second copy of either function in this package.

If a helper cannot be found the gatherers refuse to produce output (`HelpersMissing`):  facts are never
printed unredacted.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

__all__ = ["HelpersMissing", "clean", "safe_ref", "safe_path", "safe_token", "redact", "render", "textsafe", "secretscan"]


class HelpersMissing(RuntimeError):
    pass


_loaded: dict[str, Any] = {}


def _helper_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "agent_sync"


def _load(name: str):
    if name in _loaded:
        return _loaded[name]
    path = _helper_dir() / (name + ".py")
    if not path.is_file():
        raise HelpersMissing("the F3 helper %s.py was not found next to this package (looked in %s)" % (name, _helper_dir().name))
    module_name = "_fleet_facts_" + name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HelpersMissing("the F3 helper %s.py could not be loaded" % name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken helper is a missing helper:  fail closed
        sys.modules.pop(module_name, None)
        raise HelpersMissing("the F3 helper %s.py failed to load (%s)" % (name, type(exc).__name__)) from exc
    _loaded[name] = module
    return module


def textsafe():
    return _load("textsafe")


def secretscan():
    return _load("secretscan")


def clean(value: Any, cap: int = 200) -> str:
    """Outside text as one inert plain line (render_untrusted without the code span), "" when empty.  Used
    for every free-text field before it goes into the facts;  the renderer wraps it in a code span again
    (render_untrusted is idempotent)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return ""
    return textsafe().render_untrusted(text, max(1, int(cap)), code_span=False)


def render(value: Any, cap: int = 200) -> str:
    """The code-span form for a message (what the renderer does at render time)."""
    return textsafe().render_untrusted(value, max(3, int(cap)))


_REF_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_PATH_OK = re.compile(r"^[A-Za-z0-9._/~+ -]{1,300}$")
_TOKEN_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}$")


def safe_ref(value: Any) -> tuple[str | None, bool]:
    """(branch name, True) when it is made only of characters a fleet branch uses, so it can be a join key
    exactly as git spelled it.  Anything else comes back cleaned and False (it will not match a join)."""
    if isinstance(value, str) and _REF_OK.match(value) and ".." not in value and "//" not in value:
        return value, True
    if value is None:
        return None, True
    return clean(value, 120), False


def safe_path(value: Any) -> tuple[str | None, bool]:
    """(path, True) when it is plain, so a tool can act on it exactly as written (`lane retire <path>`).  A path
    with a character that means something in a message (@, a backtick, brackets, a colon) comes back cleaned and
    False.  Cleaning can also drop a slash before `poll` or `todo`, which is why a plain path is not cleaned."""
    if isinstance(value, str) and _PATH_OK.match(value) and ".." not in value.split("/"):
        return value, True
    if value is None:
        return None, True
    return clean(value, 300), False


def safe_token(value: Any, cap: int = 64) -> str | None:
    """A login, a seat tag or a label-like word, exactly as given when it is plain, else cleaned."""
    if value is None:
        return None
    if isinstance(value, str) and _TOKEN_OK.match(value) and len(value) <= cap:
        return value
    return clean(value, cap) or None


def redact(obj: Any) -> tuple[Any, list[str]]:
    """(copy of `obj` with every secret-shaped string field withheld, the paths that were)."""
    return secretscan().redact_fields(obj)
