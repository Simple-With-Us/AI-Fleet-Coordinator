"""Export the Apple Notes "Coding" folder to plain text, with an on-disk cache.

Notes.app is driven through osascript in a handful of bulk invocations (one for the metadata
of every note, then bodies in index ranges of ~100), never one call per note: the 500-note
archive exports in well under a minute cold and in about a second warm.  Nothing here activates
Notes.app, so the owner's focus is never stolen.

Cache: ~/apps/fleet-rag/cache/notes/<safe-id>.json keyed by the note's modification date.  A
note whose modification date is unchanged is served from the cache without touching Notes.
Only the SCRUBBED plain text is cached (never the raw HTML body): the directory is 0700 and
every file 0600, and `CACHE_VERSION` is bumped whenever the record shape changes so older
records are rewritten instead of trusted.  A note whose body did not come back from Notes is
not cached at all, so the next run asks for it again.

If Notes is unavailable (osascript missing, automation denied, folder absent) `export_notes`
raises NotesUnavailable; the ingest orchestrator logs it and skips the source.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from typing import Iterable

from .scrub import scrub

CACHE_DIR = pathlib.Path.home() / "apps" / "fleet-rag" / "cache" / "notes"
FOLDER = "Coding"
ACCOUNT = "iCloud"
US = "\x1f"   # unit separator between fields
RS = "\x1e"   # record separator between notes
BODY_BATCH = 100
OSA_TIMEOUT = 600
CACHE_VERSION = 2     # v1 records carried the raw "html" body and unscrubbed text; never served
CACHE_DIR_MODE = 0o700
CACHE_FILE_MODE = 0o600


class NotesUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------- AppleScript

_META_SCRIPT = """
set US to (ASCII character 31)
set RS to (ASCII character 30)
tell application "Notes"
  set f to folder "%(folder)s" of account "%(account)s"
  set ids to id of every note of f
  set names to name of every note of f
  set mods to modification date of every note of f
  set cres to creation date of every note of f
end tell
set out to ""
repeat with i from 1 to count of ids
  set m to (item i of mods)
  set c to (item i of cres)
  set ms to ""
  set cs to ""
  if m is not missing value then set ms to ((m as «class isot») as string)
  if c is not missing value then set cs to ((c as «class isot») as string)
  set out to out & (item i of ids) & US & (item i of names) & US & ms & US & cs & RS
end repeat
return out
"""

_RANGE_SCRIPT = """
set US to (ASCII character 31)
set RS to (ASCII character 30)
set startIdx to (system attribute "NOTE_START") as integer
set endIdx to (system attribute "NOTE_END") as integer
tell application "Notes"
  set f to folder "%(folder)s" of account "%(account)s"
  set n to count of notes of f
  if endIdx > n then set endIdx to n
  if startIdx > endIdx then return ""
  set ids to id of notes startIdx thru endIdx of f
  set bodies to body of notes startIdx thru endIdx of f
end tell
set out to ""
repeat with i from 1 to count of ids
  set b to item i of bodies
  if b is missing value then set b to ""
  set out to out & (item i of ids) & US & b & RS
end repeat
return out
"""

_BY_ID_SCRIPT = """
set US to (ASCII character 31)
set RS to (ASCII character 30)
set idPath to POSIX file (system attribute "NOTE_IDS")
set idText to read idPath as «class utf8»
set AppleScript's text item delimiters to linefeed
set idList to text items of idText
set out to ""
tell application "Notes"
  repeat with nid in idList
    if (length of nid) > 0 then
      set b to ""
      try
        set b to body of note id nid
      end try
      set out to out & nid & US & b & RS
    end if
  end repeat
end tell
return out
"""


def _osascript(script: str, env: dict[str, str] | None = None) -> str:
    exe = shutil.which("osascript")
    if not exe:
        raise NotesUnavailable("osascript not on PATH (not macOS?)")
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([exe, path], capture_output=True, timeout=OSA_TIMEOUT,
                              env={**os.environ, **(env or {})}, check=False)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise NotesUnavailable(f"osascript failed: {type(e).__name__}") from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise NotesUnavailable("osascript error: " + (err[-1][:200] if err else f"exit {proc.returncode}"))
    return proc.stdout.decode("utf-8", errors="replace")


def _records(raw: str) -> list[list[str]]:
    return [rec.split(US) for rec in raw.split(RS) if rec.strip()]


def list_notes(folder: str = FOLDER, account: str = ACCOUNT) -> list[dict]:
    """Metadata for every note in the folder, in Notes' own order: id, name, modified, created."""
    raw = _osascript(_META_SCRIPT % {"folder": folder, "account": account})
    out = []
    for rec in _records(raw):
        if len(rec) < 4:
            continue
        nid, name, mod, cre = rec[0].strip(), rec[1], rec[2].strip(), rec[3].strip()
        out.append({"id": nid, "name": name.strip(), "modified": mod, "created": cre})
    return out


def _fetch_bodies_range(start: int, end: int, folder: str, account: str) -> dict[str, str]:
    raw = _osascript(_RANGE_SCRIPT % {"folder": folder, "account": account},
                     {"NOTE_START": str(start), "NOTE_END": str(end)})
    return {rec[0].strip(): rec[1] for rec in _records(raw) if len(rec) >= 2}


def _fetch_bodies_by_id(ids: list[str]) -> dict[str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(ids))
        path = fh.name
    try:
        raw = _osascript(_BY_ID_SCRIPT, {"NOTE_IDS": path})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {rec[0].strip(): rec[1] for rec in _records(raw) if len(rec) >= 2}


# --------------------------------------------------------------------------- HTML -> text

_BLOCK_END = {"div", "p", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol",
              "table"}
_BREAK_START = {"br", "hr", "tr"}
_SKIP = {"style", "script", "head", "title"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP:
            self._skip += 1
            return
        if tag in ("ul", "ol"):
            self._list_depth += 1
        if tag == "li":
            self.parts.append("\n" + "  " * max(self._list_depth - 1, 0) + "- ")
        elif tag in _BREAK_START:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if tag in ("ul", "ol"):
            self._list_depth = max(self._list_depth - 1, 0)
        if tag in _BLOCK_END:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Notes HTML to plain text: block tags become line breaks, styles are dropped."""
    p = _Text()
    p.feed(markup)
    p.close()
    # convert_charrefs=True already decoded entities once; a second unescape would turn a
    # literal "&amp;amp;" in a note into "&".
    text = "".join(p.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.splitlines()]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


# --------------------------------------------------------------------------- cache + export

def safe_id(note_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", note_id.replace("x-coredata://", "")).strip("_")


def _cache_path(cache_dir: pathlib.Path, note_id: str) -> pathlib.Path:
    return cache_dir / f"{safe_id(note_id)}.json"


def _load_cached(cache_dir: pathlib.Path, meta: dict) -> dict | None:
    p = _cache_path(cache_dir, meta["id"])
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if rec.get("v") != CACHE_VERSION or rec.get("modified") != meta["modified"] or "text" not in rec:
        return None
    if "html" in rec:
        return None
    return rec


def _ensure_cache_dir(cache_dir: pathlib.Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cache_dir, CACHE_DIR_MODE)
    except OSError:
        pass


def _write_cache(path: pathlib.Path, rec: dict) -> None:
    """Write a cache record 0600 (create with the mode, chmod an existing file, atomic replace)."""
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(rec, ensure_ascii=False).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CACHE_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, CACHE_FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_record(meta: dict, body_html: str) -> dict:
    """The record that is cached and returned: metadata plus scrubbed plain text, no HTML."""
    text, kinds = scrub(html_to_text(body_html))
    rec = {"id": meta["id"], "name": meta["name"], "modified": meta["modified"],
           "created": meta["created"], "text": text, "v": CACHE_VERSION}
    if kinds:
        rec["scrubbed"] = kinds
    return rec


def export_notes(folder: str = FOLDER, account: str = ACCOUNT,
                 cache_dir: pathlib.Path | None = None, log=None) -> list[dict]:
    """Every note in the folder as {id, name, created, modified, text, v[, scrubbed]}.

    `text` is already scrubbed.  Bodies are fetched only for notes whose modification date
    differs from the cache (or whose cached record predates CACHE_VERSION).
    """
    cache_dir = cache_dir or CACHE_DIR
    _ensure_cache_dir(cache_dir)
    metas = list_notes(folder, account)
    result: list[dict] = []
    stale: list[int] = []
    for i, m in enumerate(metas):
        rec = _load_cached(cache_dir, m)
        if rec is None:
            stale.append(i)
            result.append(m)              # placeholder, filled below
        else:
            result.append(rec)
    if log:
        log(f"notes: {len(metas)} in '{folder}', {len(stale)} need export")
    if not stale:
        return result

    bodies: dict[str, str] = {}
    # Fetch bodies by index range covering the stale notes; Notes' order is stable within a
    # run, and every body comes back paired with its id so a shifted index is harmless.
    lo, hi = stale[0], stale[-1]
    start = lo
    while start <= hi:
        end = min(start + BODY_BATCH - 1, hi)
        if any(start <= s <= end for s in stale):
            bodies.update(_fetch_bodies_range(start + 1, end + 1, folder, account))
        start = end + 1
    missing = [metas[i]["id"] for i in stale if metas[i]["id"] not in bodies]
    for j in range(0, len(missing), 50):
        bodies.update(_fetch_bodies_by_id(missing[j:j + 50]))

    unfetched = 0
    for i in stale:
        m = metas[i]
        if m["id"] not in bodies:
            # Body never came back (deleted mid-run, AppleScript hiccup): return an empty text
            # placeholder but do NOT cache it, so the next run asks Notes again.
            unfetched += 1
            result[i] = {**m, "text": "", "v": CACHE_VERSION}
            continue
        rec = cache_record(m, bodies[m["id"]])
        try:
            _write_cache(_cache_path(cache_dir, m["id"]), rec)
        except OSError:
            pass
        result[i] = rec
    if log and unfetched:
        log(f"notes: {unfetched} bodies did not come back; left uncached")
    return result


def strip_title_line(text: str, title: str) -> str:
    """Notes repeats the title as the first body line; drop it so the chunk prefix is not doubled."""
    lines = text.split("\n", 1)
    if lines and lines[0].strip() == title.strip():
        return lines[1].lstrip("\n") if len(lines) > 1 else ""
    return text


def iter_cached(cache_dir: pathlib.Path | None = None) -> Iterable[dict]:
    """Yield cached note records without touching Notes.app (used when Notes is unavailable)."""
    cache_dir = cache_dir or CACHE_DIR
    if not cache_dir.exists():
        return
    for p in sorted(cache_dir.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict) or rec.get("v") != CACHE_VERSION or "html" in rec:
            continue              # pre-v2 record: raw HTML, unscrubbed; never serve it
        yield rec
