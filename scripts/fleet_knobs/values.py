"""Parse, narrow and render knob values.  Pure functions, no I/O, no logging.

Typed values:  str -> str, int -> int, bool -> bool, enum -> str, list -> tuple of str,
caps -> dict of name -> int.

Narrowing is the whole safety story for local files:  ``narrow()`` returns the narrower of
the Infisical-side value and a local one, so a local file can lower a cap, shrink an
allowlist or force a mode down, and it can never raise, grow or enable anything.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from .registry import Spec

MAX_TEXT = 4096
MAX_LIST_ITEMS = 500
MAX_CAP_KEYS = 40
MAX_CAP_VALUE = 1_000_000

_LIST_ITEM_RE = re.compile(r"^[A-Za-z0-9._/@:+-]{1,120}$")
_CAP_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_INT_RE = re.compile(r"^[0-9]{1,9}$")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def parse(spec: Spec, raw: Any) -> Any:
    """The typed value for `raw` (a string from Infisical or a local file), or ValueError.

    The error text never contains the raw value."""
    if spec.kind == "bool" and isinstance(raw, bool):
        return raw
    if spec.kind == "int" and isinstance(raw, int) and not isinstance(raw, bool):
        return _check_int(spec, raw)
    if spec.kind == "list" and isinstance(raw, (list, tuple)):
        return _check_list(raw)
    if spec.kind == "caps" and isinstance(raw, dict):
        return _check_caps(raw)
    if not isinstance(raw, str):
        raise ValueError("not a string")
    text = raw.strip()
    if len(text) > MAX_TEXT:
        raise ValueError("too long")
    allowed = "\t\n\r" if spec.kind in ("list", "caps") else ""
    if any((ord(c) < 32 and c not in allowed) or ord(c) == 127 for c in text):
        raise ValueError("control character")
    if spec.kind == "str":
        if not text:
            raise ValueError("empty")
        if spec.pattern and not re.match(spec.pattern, text):
            raise ValueError("does not match the expected format")
        return text
    if spec.kind == "int":
        if not _INT_RE.match(text):
            raise ValueError("not a non-negative integer")
        return _check_int(spec, int(text))
    if spec.kind == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError("not a boolean")
    if spec.kind == "enum":
        low = text.lower()
        if low not in spec.choices:
            raise ValueError("not one of the allowed choices")
        return low
    if spec.kind == "list":
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError:
                raise ValueError("bad JSON list") from None
            if not isinstance(items, list):
                raise ValueError("bad JSON list")
            return _check_list(items)
        return _check_list([t for t in re.split(r"[,\s]+", text) if t])
    if spec.kind == "caps":
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                raise ValueError("bad JSON object") from None
            if not isinstance(obj, dict):
                raise ValueError("bad JSON object")
            return _check_caps(obj)
        pairs: Dict[str, Any] = {}
        for token in [t for t in re.split(r"[,\s]+", text) if t]:
            key, sep, val = token.partition("=")
            if not sep or key in pairs:
                raise ValueError("bad caps entry")
            pairs[key] = val
        return _check_caps(pairs)
    raise ValueError("unsupported kind")


def _check_int(spec: Spec, value: int) -> int:
    if value < spec.minimum or (spec.maximum is not None and value > spec.maximum):
        raise ValueError("out of range")
    return value


def _check_list(items: Any) -> Tuple[str, ...]:
    if len(items) > MAX_LIST_ITEMS:
        raise ValueError("too many items")
    out = []
    for item in items:
        if not isinstance(item, str) or not _LIST_ITEM_RE.match(item.strip()):
            raise ValueError("bad list item")
        item = item.strip()
        if item not in out:
            out.append(item)
    return tuple(out)


def _check_caps(obj: Dict[str, Any]) -> Dict[str, int]:
    if len(obj) > MAX_CAP_KEYS:
        raise ValueError("too many caps")
    out: Dict[str, int] = {}
    for key, val in obj.items():
        if not isinstance(key, str) or not _CAP_KEY_RE.match(key):
            raise ValueError("bad caps name")
        if isinstance(val, bool):
            raise ValueError("bad caps value")
        if isinstance(val, str):
            if not _INT_RE.match(val.strip()):
                raise ValueError("bad caps value")
            val = int(val.strip())
        if not isinstance(val, int) or val < 0 or val > MAX_CAP_VALUE:
            raise ValueError("bad caps value")
        out[key] = val
    return out


def default_value(spec: Spec) -> Any:
    """A fresh copy of the registry default (callers may mutate the result)."""
    if spec.kind == "caps":
        return dict(spec.default or {})
    if spec.kind == "list":
        return tuple(spec.default or ())
    return spec.default


def narrow(spec: Spec, base: Any, local: Any) -> Tuple[Any, bool, bool]:
    """(value, narrowed, ignored):  the narrower of `base` and `local`.

    `narrowed` is true when `local` changed the result;  `ignored` is true when part or all
    of `local` would have widened `base` and was dropped.  Both arguments are typed values."""
    if spec.local == "deny":
        return base, False, local != base
    if spec.kind == "bool":
        safe = spec.safe
        if base == safe:
            return base, False, local != safe
        if local == safe:
            return local, True, False
        return base, False, False
    if spec.kind == "enum":
        b, l = spec.choices.index(base), spec.choices.index(local)
        if l < b:
            return local, True, False
        return base, False, l > b
    if spec.kind == "int":
        better = local < base if spec.narrower == "min" else local > base
        if better:
            return local, True, False
        return base, False, local != base
    if spec.kind == "list":
        keep = tuple(x for x in base if x in set(local))
        extra = any(x not in base for x in local)
        return keep, keep != tuple(base), extra
    if spec.kind == "caps":
        out = dict(base)
        ignored = False
        for key, val in local.items():
            if key in out:
                if val < out[key]:
                    out[key] = val
                elif val > out[key]:
                    ignored = True
            else:
                out[key] = val      # base sets no cap here, so any local cap is narrower
        return out, out != base, ignored
    return base, False, local != base      # str


def most_conservative(spec: Spec, base: Any) -> Any:
    """The value a STOP file forces:  the narrowest value the knob can take."""
    if spec.local == "deny":
        return base
    if spec.kind == "bool":
        return spec.safe
    if spec.kind == "enum":
        return spec.choices[0]
    if spec.kind == "int":
        if spec.narrower == "min":
            return spec.minimum
        return spec.maximum if spec.maximum is not None else base
    if spec.kind == "list":
        return ()
    if spec.kind == "caps":
        return {k: 0 for k in base}
    return base


def render(spec: Spec, value: Any) -> str:
    """The value as text for a CLI answer or the INFISICAL.md default column."""
    if spec.kind == "bool":
        return "true" if value else "false"
    if spec.kind == "list":
        return ",".join(value)
    if spec.kind == "caps":
        return ",".join(f"{k}={v}" for k, v in sorted(value.items()))
    return str(value)


def lenient(spec: Spec, raw: Optional[Any]) -> Tuple[bool, Any]:
    """(ok, typed) for an optional raw value:  (False, None) when it is absent or invalid."""
    if raw is None:
        return False, None
    try:
        return True, parse(spec, raw)
    except ValueError:
        return False, None
