"""RFC 5545 helpers for fleet ICS feeds (stdlib only)."""
from __future__ import annotations


def ics_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _utf8_prefix(raw: bytes, max_octets: int) -> bytes:
    """Longest UTF-8-complete prefix of raw with len <= max_octets."""
    if len(raw) <= max_octets:
        return raw
    # Walk code points so we never split a multi-byte character
    text = raw.decode("utf-8")
    acc = ""
    for ch in text:
        trial = acc + ch
        if len(trial.encode("utf-8")) > max_octets:
            break
        acc = trial
    if not acc:
        # Single code point longer than budget (shouldn't happen for BMP+emoji)
        return raw[:max_octets]
    return acc.encode("utf-8")


def fold_line(line: str) -> str:
    """Fold at 75 *octets* (RFC 5545), never mid UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[bytes] = []
    first = _utf8_prefix(raw, 75)
    parts.append(first)
    rest = raw[len(first) :]
    while rest:
        # continuation lines: leading space + up to 74 octets of content
        chunk = _utf8_prefix(rest, 74)
        if not chunk:
            chunk = rest[:74]  # last-resort hard cut
        parts.append(b" " + chunk)
        rest = rest[len(chunk) :]
    return b"\r\n".join(parts).decode("utf-8")


def join_ics(lines: list[str]) -> str:
    """CRLF body with trailing CRLF (what Apple/Google expect)."""
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"
