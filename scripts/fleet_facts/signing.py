"""Signing and verifying the Mac facts files (open-loops F7).

A signed file is a JSON object with two extra fields:

    "sig_alg": "hmac-sha256/canonical-json-v1"      part of what is signed
    "sig":     "<64 lowercase hex digits>"           the HMAC-SHA256 of the canonical form, not part of it

The canonical form is the object WITHOUT its `sig` field, serialized as

    json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")

(the Python defaults for everything else), and the HMAC key is the text of FLEET_FACTS_SIGNING_KEY, trimmed of
surrounding whitespace, encoded as UTF-8.  So any Python can check a file with

    import hashlib, hmac, json, os
    doc = json.load(open("mac-facts.json")); sig = doc.pop("sig")
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    ok = hmac.compare_digest(sig, hmac.new(os.environ["FLEET_FACTS_SIGNING_KEY"].strip().encode(), body, hashlib.sha256).hexdigest())

and `fleet-facts verify FILE` does exactly that.  The file on disk is pretty-printed;  only the parsed object is
signed, so formatting does not matter.  `signed_for` (the path in the repository) is signed too, so a file
cannot be moved to a path it was not made for, and the freshness check is the consumer's (`generated_at`).

Nothing here prints, logs or returns the key.  The publisher gets it through K1 (`fleet_knobs`, imported only
when asked, so `verify` works from a copy that has no Infisical);  `verify` reads it from the environment,
because the hosts that check a file (the cloud and Grok Bot) have no Infisical.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping

__all__ = ["ALG", "KEY_ENV", "KeyUnavailable", "Verdict", "canonical", "sign", "verify", "key_from_environ", "key_from_knobs", "sha256_hex"]

ALG = "hmac-sha256/canonical-json-v1"
KEY_ENV = "FLEET_FACTS_SIGNING_KEY"
# The same shape K1 accepts for the secret (32 random bytes as hex, base64 or urlsafe base64).
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9+/=_-]{32,256}\Z")
_SIG_SHAPE = re.compile(r"^[0-9a-f]{64}\Z")


class KeyUnavailable(Exception):
    """The signing key cannot be had.  `reason` is a short token or sentence that never holds the key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Verdict:
    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str | None = None) -> None:
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(document: Mapping[str, Any]) -> bytes:
    """The bytes that are signed:  the object without `sig`, sorted keys, no spaces, ASCII only."""
    body = {key: value for key, value in document.items() if key != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _key_bytes(key: str) -> bytes:
    text = key.strip() if isinstance(key, str) else ""
    if not _KEY_SHAPE.match(text):
        raise KeyUnavailable("the signing key does not have the expected shape (32 to 256 letters, digits, + / = _ -)")
    return text.encode("utf-8")


def sign(document: Mapping[str, Any], key: str) -> dict:
    """A copy of `document` with `sig_alg` and `sig` set.  Raises KeyUnavailable for a key of the wrong shape."""
    signed = {k: v for k, v in document.items() if k != "sig"}
    signed["sig_alg"] = ALG
    digest = hmac.new(_key_bytes(key), canonical(signed), hashlib.sha256).hexdigest()
    signed["sig"] = digest
    return signed


def verify(document: Any, key: str, *, signed_for: str | None = None) -> Verdict:
    """Does `document` carry a good signature under `key`?  With `signed_for`, also that it was made for that path.

    Raises KeyUnavailable (the caller cannot check at all) for a missing or malformed key;  every other failure is
    a Verdict(False, reason)."""
    secret = _key_bytes(key)
    if not isinstance(document, dict):
        return Verdict(False, "the file is not a JSON object")
    sig = document.get("sig")
    if not isinstance(sig, str) or not _SIG_SHAPE.match(sig):
        return Verdict(False, "the file has no signature")
    if document.get("sig_alg") != ALG:
        return Verdict(False, "the signature algorithm is not %s" % ALG)
    try:
        body = canonical(document)
    except (TypeError, ValueError):
        return Verdict(False, "the file cannot be put in canonical form")
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return Verdict(False, "the signature does not match")
    if signed_for is not None and document.get("signed_for") != signed_for:
        return Verdict(False, "the file was signed for a different path")
    return Verdict(True)


def key_from_environ(environ: Mapping[str, str]) -> str:
    """The key for `verify`, from the environment.  Raises KeyUnavailable when it is unset or malformed."""
    value = (environ.get(KEY_ENV) or "").strip()
    if not value:
        raise KeyUnavailable("%s is not set" % KEY_ENV)
    _key_bytes(value)
    return value


def key_from_knobs() -> str:
    """The key for `publish`, through K1 (Infisical).  Never cached on disk and never defaulted:  a job that cannot
    fetch it stops.  `fleet_knobs` is imported here and nowhere else in this package."""
    try:
        from fleet_knobs import knobs  # noqa: PLC0415 - only the publisher needs it
    except ImportError:
        raise KeyUnavailable("fleet_knobs is missing from this copy of fleet-facts;  run scripts/fleet_facts/install.sh --apply") from None
    try:
        value = knobs.get(KEY_ENV)
    except knobs.SecretUnavailable as exc:
        raise KeyUnavailable("%s is unavailable (%s)" % (KEY_ENV, exc.reason)) from None
    except knobs.KnobError as exc:
        raise KeyUnavailable("Infisical cannot be read (%s)" % str(exc).splitlines()[0][:160]) from None
    _key_bytes(value)
    return value
