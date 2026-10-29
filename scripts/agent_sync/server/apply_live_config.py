#!/usr/bin/env python3
"""Bring the live config's JET and Grok Bot sections in line with the image's sample, and add the settings
sections a live volume lacks.

Run it inside the container (the Coolify terminal, or docker exec):

    python3 /app/scripts/agent_sync/server/apply_live_config.py            # show what would change
    python3 /app/scripts/agent_sync/server/apply_live_config.py --apply    # back up, then write

The entrypoint seeds /data/listener.toml from the sample only once, so a seat section changed in the
sample never reaches a live volume by itself.  This copies exactly the sample's [seat.JET] and every
[seat.GB-*] section into the live file:  a section that differs is replaced where it stands, with the
comment lines just above it, and a missing one is appended.  [listener_api], [notes_queue] (AFC-486) and
[linear_projects] (AFC-565) are appended the same way when the live file has none, but one that is there is
never changed:  its `enabled` (and the notes queue's seat lists) are the operator's choice on that volume,
and the sample ships all three off.  [daemon]
(owner_user_id and eligible_user_ids, pinned by `simple-chat daemon init`) and every other section stay byte
for byte.

Before writing, the result is checked with the listener's own config parser and the seat partition;
any problem refuses the write.  --apply first copies the live file to <file>.bak-<UTC stamp> (mode 600),
then replaces it atomically with the same mode and owner.  A second run changes nothing and writes no
backup.  The file holds variable names, never values, so the diff prints the keys as they are.

Afterwards restart the app if a section names a variable the container did not start with (always the
case for a newly enabled persona); otherwise `simple-chat daemon reload` is enough.

Exit codes:  0 done (or nothing to do), 1 a file is missing or unreadable, 2 the result would be refused.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

# scripts/, so `agent_sync` imports (the image keeps the repo layout).  Only this sys.path change comes before the
# import, which ruff's E402 allows, so the import needs no noqa (a newer ruff drops it as unused).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent_sync import config as C

HERE = Path(__file__).resolve().parent

_HEADER_RE = re.compile(r"^\[([^\[\]]+)\]\s*(#.*)?$")
# Settings sections copied from the sample only when the live file has none, and never replaced.
ADD_ONLY = ("listener_api", "notes_queue", "linear_projects")


def target(seat: str) -> bool:
    return seat == "JET" or seat.startswith("GB-")


def blocks(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(preamble lines, [(table name, lines)]).  A block owns its header, the lines up to the next block,
    and the comment and blank lines directly above its header.  Joining everything back gives the text."""
    lines = text.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines) if _HEADER_RE.match(line.strip())]
    starts = []
    for n, head in enumerate(heads):
        floor = heads[n - 1] if n else -1
        start = head
        while start - 1 > floor and (
            not lines[start - 1].strip() or lines[start - 1].lstrip().startswith("#")
        ):
            start -= 1
        starts.append(start)
    preamble = lines[: starts[0]] if heads else lines
    out = []
    for n, head in enumerate(heads):
        end = starts[n + 1] if n + 1 < len(heads) else len(lines)
        out.append((_HEADER_RE.match(lines[head].strip()).group(1).strip(), lines[starts[n] : end]))
    return preamble, out


def _seat(name: str) -> str | None:
    return name[len("seat.") :] if name.startswith("seat.") else None


def _flat(prefix: str, value: object, out: dict[str, object]) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            _flat("%s.%s" % (prefix, key) if prefix else key, value[key], out)
    else:
        out[prefix] = value


def section_diff(old: str | None, new: str) -> list[str]:
    """Key-level differences between two blocks (`added` when old is None)."""
    if old is None:
        return ["added"]
    a: dict[str, object] = {}
    b: dict[str, object] = {}
    _flat("", tomllib.loads(old), a)
    _flat("", tomllib.loads(new), b)
    changes = []
    for key in sorted(set(a) | set(b)):
        if a.get(key, "<unset>") != b.get(key, "<unset>"):
            changes.append(
                "%s: %r -> %r"
                % (key.split(".", 2)[-1], a.get(key, "<unset>"), b.get(key, "<unset>"))
            )
    return changes or ["comments only"]


def merge(live: str, sample: str) -> tuple[str, dict[str, list[str]]]:
    """(the new live text, {table name: changes}) for every target seat that differs and every ADD_ONLY section the
    live file lacks.  A table name is as its header writes it:  `seat.JET`, `notes_queue`."""
    _, sample_blocks = blocks(sample)
    replace = {name: body for name, body in sample_blocks if _seat(name) and target(_seat(name))}
    add_only = {name: body for name, body in sample_blocks if name in ADD_ONLY}
    preamble, live_blocks = blocks(live)
    changes: dict[str, list[str]] = {}
    seen: set[str] = set()
    out = list(preamble)
    for name, body in live_blocks:
        if name in add_only:
            seen.add(name)  # already there:  the live values stand
        elif name in replace:
            seen.add(name)
            new = replace[name]
            if "".join(body).strip() != "".join(new).strip():
                changes[name] = section_diff("".join(body), "".join(new))
                body = list(new)
                if not body[-1].endswith("\n"):
                    body[-1] += "\n"
        out.extend(body)
    for name, new in sample_blocks:
        if name in seen or (name not in replace and name not in add_only):
            continue
        changes[name] = section_diff(None, "".join(new))
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        block = list(new)
        if block and block[0].strip():
            block.insert(0, "\n")
        if not block[-1].endswith("\n"):
            block[-1] += "\n"
        out.extend(block)
    return "".join(out), changes


def problems(text: str) -> list[str]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return ["the result is not valid TOML: %s" % exc]
    cfg = C.from_dict(raw)
    partition, error = C.load_partition(C.partition_path(os.environ))
    found = list(cfg.errors)
    if error:
        found.append(error)
    found += C.partition_errors(cfg, partition, env_instance="server")
    return found


def daemon_block(text: str) -> str:
    return "".join("".join(body) for name, body in blocks(text)[1] if name == "daemon")


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path: Path, text: str, stamp: str) -> Path:
    st = path.stat()
    backup = path.with_name("%s.bak-%s" % (path.name, stamp))
    backup_fd, backup_tmp = tempfile.mkstemp(
        prefix=".%s." % backup.name, dir=str(path.parent)
    )
    try:
        with os.fdopen(backup_fd, "wb") as bfh:
            with path.open("rb") as source:
                shutil.copyfileobj(source, bfh)
            os.fchmod(bfh.fileno(), 0o600)
            bfh.flush()
            os.fsync(bfh.fileno())
        os.replace(backup_tmp, backup)
        _fsync_dir(path.parent)
    except BaseException:
        if os.path.exists(backup_tmp):
            os.unlink(backup_tmp)
        raise
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            os.fchmod(fh.fileno(), st.st_mode & 0o777)
            try:
                os.fchown(fh.fileno(), st.st_uid, st.st_gid)
            except PermissionError:
                pass
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--live",
        default=os.environ.get("AGENT_SYNC_CONFIG") or "/data/listener.toml",
        help="the live config (default: $AGENT_SYNC_CONFIG, else /data/listener.toml)",
    )
    ap.add_argument(
        "--sample",
        default=str(HERE / "listener.toml"),
        help="the sample (default: the image's copy)",
    )
    ap.add_argument(
        "--apply", action="store_true", help="back up the live file, then write the change"
    )
    args = ap.parse_args(argv)
    live_path, sample_path = Path(args.live), Path(args.sample)
    try:
        live, sample = (
            live_path.read_text(encoding="utf-8"),
            sample_path.read_text(encoding="utf-8"),
        )
    except OSError as exc:
        print("cannot read %s: %s" % (exc.filename, exc.strerror), file=out)
        return 1
    try:
        new, changes = merge(live, sample)
    except tomllib.TOMLDecodeError as exc:
        print("a section does not parse: %s" % exc, file=out)
        return 2
    if not changes:
        print(
            "no changes:  %s already matches the sample's JET and GB sections and has [%s]"
            % (live_path, "] and [".join(ADD_ONLY)),
            file=out,
        )
        return 0
    for name in sorted(changes):
        print("[%s]" % name, file=out)
        for line in changes[name]:
            print("    %s" % line, file=out)
    if daemon_block(new) != daemon_block(live):
        print("refused:  [daemon] would change (it must stay byte for byte)", file=out)
        return 2
    found = problems(new)
    if found:
        print("refused:  the result would not load:", file=out)
        for line in found:
            print("    %s" % line, file=out)
        return 2
    if not args.apply:
        print("dry run:  nothing written; run again with --apply", file=out)
        return 0
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = write(live_path, new, stamp)
    print(
        "wrote %s (backup %s).  Restart the app if a section names a new variable; else simple-chat daemon reload."
        % (live_path, backup),
        file=out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
