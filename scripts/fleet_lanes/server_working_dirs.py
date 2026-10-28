#!/usr/bin/env python3
"""Point every agent tool's session working directory at the lanes root.

Owner policy, Oct 22, 2026:  on a server host the project mirrors live on a
volume the host names in the environment and the lanes live beside them, and
`~/Code` and `~/apps` resolve to them, so
the policy path `~/apps/lanes/<Repo>/<seat>-<slug>` still holds.  Every tool
session starts in the lanes root, so work lands in a lane and never in an
integration tree.

The knobs differ per tool, because the tools differ.  Codex and the desktop apps
have a location setting; the CLI tools have none, so their working directory
comes from the launcher that starts them.  This script owns those launchers and
settings on a server host.  The Claude desktop app reads `worktree.location` from
this host's `~/.claude/settings.json` for the worktrees of its SSH sessions, and
without it files them inside the project (`<repo>/.claude/worktrees`), so that key
points at the lanes root too.

It is idempotent.  `--apply` writes only what differs, leaves a timestamped
backup next to every file it touches, and returns nonzero when a knob is still
off afterwards, so automation can tell that the policy did not take.  `--verify`
reports each knob and exits nonzero when one of them is off.

A knob the running account cannot write is skipped, not fatal:  the apply
reports it, carries on with the knobs after it, and still ends with the verify
that says which knobs are off.  A knob that could not be written is off, so the
verify reports it as off and the apply still returns what the verify returns.

An apply never rewrites the identity of a unit the operator set up:  the account
the unit runs as, the command it runs and the environment that account reads are
the operator's own, and the template only fills in what they left out.

Usage:

    python3 scripts/fleet_lanes/server_working_dirs.py --apply
    python3 scripts/fleet_lanes/server_working_dirs.py --verify
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/root")).expanduser()

# The volume a server host keeps its integration trees and its apps folder on.
# The path is not in this repository:  the host names it in the environment, and a
# host that names none has no volume, so the copy under the home is the fallback.
ENV_SERVER_VOLUME = "FLEET_SERVER_VOLUME"

# Where the systemd units live.  Overridable so a test can write them somewhere
# harmless, and so a host that keeps its units elsewhere can point at it.
SYSTEMD_DIR = Path(os.environ.get("FLEET_SYSTEMD_DIR", "/etc/systemd/system"))

# A host or port that ends up inside a root-owned unit file.  Anything outside
# this set (whitespace, a newline, a shell metacharacter) could add arguments to
# ExecStart, so it is refused rather than interpolated.
HOST_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,255}\Z")
PORT_RE = re.compile(r"\A[0-9]{1,5}\Z")


def _resolve(candidate: str) -> Path:
    """A configured root read the way `layout.py` reads one.

    `~` is expanded against the home and a relative path is joined onto it, so
    `FLEET_LANES_ROOT` selects the same directory here that `lane new` uses.
    """
    path = Path(candidate).expanduser()
    return path if path.is_absolute() else HOME / path


def server_volume() -> Path | None:
    """The volume a server host keeps its trees on, when the host names one.

    The path is read from the environment, so it is not in this repository.  A host
    that names no volume has none, and the copy under the home is the only fallback
    there is.
    """
    raw = (os.environ.get(ENV_SERVER_VOLUME) or "").strip()
    if not raw:
        return None
    path = _resolve(raw)
    return path if path.is_dir() else None


def lanes_root() -> Path:
    volume = server_volume()
    candidates = [
        os.environ.get("FLEET_LANES_ROOT"),
        str(HOME / "apps" / "lanes"),
    ]
    if volume is not None:
        candidates.append(str(volume / "apps" / "lanes"))
    for candidate in candidates:
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "apps" / "lanes"


def apps_root() -> Path:
    volume = server_volume()
    candidates = [
        os.environ.get("FLEET_APPS_ROOT"),
        str(HOME / "apps"),
    ]
    if volume is not None:
        candidates.append(str(volume / "apps"))
    for candidate in candidates:
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "apps"


def code_root() -> Path:
    """Where the integration trees live:  FLEET_CODE_ROOT, then ~/Code, then the volume."""
    volume = server_volume()
    candidates = [
        os.environ.get("FLEET_CODE_ROOT"),
        str(HOME / "Code"),
    ]
    if volume is not None:
        candidates.append(str(volume / "Code"))
    for candidate in candidates:
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "Code"


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def sync_directory(path: Path) -> None:
    """Flush a directory's entries, so a rename into it survives a crash.

    The rename itself is atomic, but the directory entry that names the new
    file is not durable until the directory is flushed.  A filesystem that
    refuses the flush is not a reason to fail a write that already happened.
    """
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def backup(path: Path) -> None:
    """Copy the file beside itself, so a rewrite can be undone.

    The copy is staged, flushed and moved into place, so a copy that fails
    partway leaves no half-written backup behind for an operator to restore
    from, and a crash cannot leave the backup without its contents.
    """
    if not path.exists():
        return
    target = Path(f"{path}.bak-{stamp()}")
    temporary = Path(f"{target}.tmp")
    try:
        shutil.copy2(path, temporary)
        with temporary.open("rb") as staged:
            os.fsync(staged.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
    except BaseException:
        discard(temporary)
        raise


def discard(path: Path) -> None:
    """Remove a staged file, and never let the cleanup mask the real failure."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def write(path: Path, text: str) -> None:
    """Write a file, replacing it in one step.

    Every knob write goes through the atomic replace, so a write that fails
    partway leaves the file it had exactly as it was:  a knob is never left
    half written.  A knob whose staged file cannot be created is refused with
    a `PermissionError` instead of being written in place, so the file it had
    survives that refusal too.
    """
    write_atomic(path, text)


def staged_directory(path: Path) -> Path:
    """The directory the staged replacement of a knob file is created in.

    A symlinked knob is written through, so the staged file lives in the
    target's own directory rather than beside the link.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    return target.parent


def prepare_staged_directory(path: Path) -> Path:
    """Make sure the staged replacement of a knob file can be created.

    A missing directory is created, the way the non-symlink path creates it, so
    a knob whose target directory does not exist yet is not refused with a
    permission message that hides the real reason.  A directory that is there
    but is neither writable nor searchable is refused with a `PermissionError`
    that names it, before anything is written, so the file the link points at
    cannot be truncated.

    Every apply calls this before it takes a backup, so a knob that can never
    be applied does not drop another copy beside the link on every run.
    """
    directory = staged_directory(path)
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() and not os.access(directory, os.W_OK | os.X_OK):
        raise PermissionError(
            f"the staged replacement of {path} needs write and search access to"
            f" {directory}"
        )
    return directory


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a failure cannot leave it half written.

    The content goes to a temporary file in the same directory, is flushed and
    synced, and only then is moved over the target, so a reader sees either the
    old file or the new one and never a truncated one.  The directory entry is
    flushed after the rename, so a crash cannot lose the new file.

    A symlinked knob is written through:  the link stays a link and the file it
    points at is the one that changes.  The staged file then lives in the
    target's own directory, so that directory has to be writable and
    searchable.  When it is not, the write fails closed with a
    `PermissionError` instead of falling back to an in-place write, because an
    in-place write is not atomic:  it truncates the target before writing, so
    an interrupted write could leave the file corrupt.  The knob is then
    skipped and reported like any other knob the account cannot write, and the
    file it had is untouched.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    prepare_staged_directory(path)
    owner = target.stat() if target.exists() else None
    mode = owner.st_mode & 0o777 if owner is not None else 0o644
    handle = tempfile.NamedTemporaryFile(
        "w", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
        delete=False, encoding="utf-8",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if owner is not None:
            # The replacement carries the file's own owner and group, so a unit
            # the operator owns keeps its owner after the rewrite.
            try:
                os.chown(temporary, owner.st_uid, owner.st_gid)
            except OSError:
                # A refused owner change leaves the group to try on its own.
                try:
                    os.chown(temporary, -1, owner.st_gid)
                except OSError as exc:
                    print(
                        f"  warning  {path}: could not keep the group of the file"
                        f" ({exc.strerror or exc})"
                    )
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        sync_directory(target.parent)
    except BaseException:
        discard(temporary)
        raise


def which(name: str) -> str:
    """The absolute path of a tool binary, searched where the units put it.

    A root operator's PATH normally holds neither `~/.opencode/bin` nor
    `~/.local/bin`, the two directories the generated units declare, so falling
    back to a fixed `/usr/local/bin/<name>` wrote an ExecStart that names a
    binary which does not exist and systemd refused the unit with status=203/EXEC.
    Search where the units say the tools live and fail loudly rather than invent
    a path.
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in (HOME / ".opencode" / "bin", HOME / ".local" / "bin", Path("/usr/local/bin")):
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise SystemExit(
        f"{name} is not in PATH, ~/.opencode/bin, ~/.local/bin or /usr/local/bin: "
        "install it, or put it on PATH, before applying the working directories"
    )


# --------------------------------------------------------------------------- #
# The knobs
# --------------------------------------------------------------------------- #

BASHRC_BEGIN = "# --- fleet: agent working directory"
BASHRC_END = "# --- end fleet working directory"

BASHRC_BLOCK = """
{begin} (owner policy, Oct 22 2026) ----------------
# Every agent tool session on this host starts in the lanes root, so work lands in
# a lane ({lanes}/<Repo>/<seat>-<slug>) and never in an integration tree.
# The roots come from the environment because they live on a volume, not in the
# home directory:  a lane tool that resolved ~/Code or ~/apps would look in a
# folder that is not there and would find no integration tree and no repo.
# The values below are shell-quoted, because they are read from the environment
# and a path that carried a metacharacter would otherwise run a command
# substitution in every shell that sources this file.
{exports}if [ -d {lanes_q} ] && [ "$PWD" = "$HOME" ]; then
  cd {lanes_q}
fi
{end} ----------------------------------------------
"""


def bashrc_exports(code: Path, apps: Path, lanes: Path) -> str:
    """The export lines of the fleet block, with every value shell-quoted.

    The values are read from the environment, so a path that carried a shell
    metacharacter would otherwise be written between double quotes and run a
    command substitution in every shell that sources the file.
    """
    lines = [
        f"export FLEET_CODE_ROOT={shlex.quote(str(code))}",
        f"export FLEET_APPS_ROOT={shlex.quote(str(apps))}",
        f"export FLEET_LANES_ROOT={shlex.quote(str(lanes))}",
    ]
    volume = server_volume()
    if volume is not None:
        lines.append(f"export FLEET_SERVER_VOLUME={shlex.quote(str(volume))}")
    return "".join(line + "\n" for line in lines)


def systemd_value(text: str) -> str:
    """A value quoted the way systemd reads an Environment= line.

    systemd splits the line on whitespace unless the value is quoted, so a path
    with a space in it would otherwise arrive as two values.
    """
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_environments(root: Path) -> dict[str, str]:
    """The Environment= lines that give a unit-launched session the canonical roots.

    A unit reads no shell profile, so the roots are declared in the unit itself:  a
    session that fell back to ~/Code and ~/apps would find no integration tree and
    no repo, which is the failure this script exists to prevent.
    """
    volume = server_volume()
    return {
        "code_env": f"Environment={systemd_value('FLEET_CODE_ROOT=' + str(code_root()))}",
        "apps_env": f"Environment={systemd_value('FLEET_APPS_ROOT=' + str(apps_root()))}",
        "lanes_env": f"Environment={systemd_value('FLEET_LANES_ROOT=' + str(root))}",
        "volume_env": "" if volume is None else
        f"Environment={systemd_value('FLEET_SERVER_VOLUME=' + str(volume))}\n",
    }


OPENCODE_UNIT = """[Unit]
Description=OpenCode server (agent working directory: the lanes root)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={lanes}
Environment=HOME={home}
Environment=PATH={home}/.opencode/bin:{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The canonical roots are declared here as well as in the shell profile, because a
# unit-launched session reads no profile:  a tool that fell back to ~/Code and
# ~/apps would find no integration tree and no repo.
{code_env}
{apps_env}
{lanes_env}
{volume_env}# The config's server block is deprecated in v2 and the port is ignored, so the
# host and port the fleet's clients use are passed explicitly.
ExecStart={opencode} serve --service{flags}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""

CLAUDE_UNIT = """[Unit]
Description=Claude Code remote control (agent working directory: the lanes root)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={lanes}
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The canonical roots are declared here as well as in the shell profile, because a
# unit-launched session reads no profile:  a tool that fell back to ~/Code and
# ~/apps would find no integration tree and no repo.
{code_env}
{apps_env}
{lanes_env}
{volume_env}# The OAuth token is read into memory once and never refreshed, so the process
# must not outlive the 8 hour token life.  RuntimeMaxSec recycles it, which is
# the systemd-native form of the Mac's capped launcher.
ExecStart={claude} remote-control
RuntimeMaxSec=21600
Restart=always
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


def opencode_server_flags() -> str:
    """The host and port the fleet's clients use, from the OpenCode config.

    The `server` block is deprecated in OpenCode v2 and the port is ignored when
    the server starts, so the flags carry it instead.  A config that no longer
    names them yields no flags, and the caller keeps whatever the running unit
    already has, so an apply never drops a working `--hostname`/`--port` pair.
    """
    config = HOME / ".config" / "opencode" / "opencode.json"
    try:
        data = json.loads(read(config) or "{}")
    except json.JSONDecodeError:
        return ""
    server = data.get("server") or {}
    host, port = server.get("host"), server.get("port")
    if not host or not port:
        return ""
    if not HOST_RE.match(str(host)):
        raise SystemExit(f"refusing to write an opencode host that is not a hostname: {host!r}")
    if not PORT_RE.match(str(port)) or not 1 <= int(port) <= 65535:
        raise SystemExit(f"refusing to write an opencode port that is not a port number: {port!r}")
    return f" --hostname {host} --port {port}"


def unit_execstart(text: str) -> str:
    """The ExecStart command of a unit, or "" when it has none.

    A line the unit continues is one line here, so a flag on a continuation line
    is read as part of the command.
    """
    for line in unit_logical_lines(text or ""):
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            return stripped[len("ExecStart="):].strip()
    return ""


# The host and port pair the fleet's clients use, as it appears in a command.
FLAG_PAIR = re.compile(r"\s--hostname\s+\S+\s+--port\s+\S+")


def command_flags(command: str) -> str:
    """The `--hostname`/`--port` pair a command carries, or "" when it carries none."""
    match = FLAG_PAIR.search(command or "")
    return match.group(0) if match else ""


def unit_execstart_flags(text: str) -> str:
    """The `--hostname`/`--port` pair a unit already carries, or ""."""
    return command_flags(unit_execstart(text))


# The tmux session's own `-c`, anchored to the new-session command so a Codex
# `-c key=value` override elsewhere in the launcher is never mistaken for it.
TMUX_CWD = re.compile(r"\btmux\s+new-session\b[^\n]*?(?<![\w-])-c\s+(\S+)")
TMUX_NEW_SESSION = re.compile(r"\btmux\s+new-session\b")


def _is_dir_token(token: str) -> bool:
    """True when a `-c` token is a directory and not an option or a key=value.

    Codex overrides look like `-c sandbox_mode=workspace-write`, so the `=` and a
    leading dash separate the two spellings without guessing.
    """
    return bool(token) and not token.startswith("-") and "=" not in token


def tmux_cwd(text: str) -> str | None:
    """The directory the tmux session is created in, or None when there is none."""
    for match in TMUX_CWD.finditer(text):
        if _is_dir_token(match.group(1)):
            return match.group(1)
    return None


def cwd_expr(root: Path) -> str:
    """The `cwd=` expression the ACP runtime gets.

    A blank or whitespace-only FLEET_LANES_ROOT falls back to the resolved lanes
    root, exactly as `layout.py` treats it, because `subprocess.Popen(cwd="")`
    raises FileNotFoundError before the CLI ever starts.
    """
    literal = json.dumps(str(root))
    return f'(os.environ.get("FLEET_LANES_ROOT") or {literal}).strip() or {literal}'


# --------------------------------------------------------------------------- #
# Python-source helpers, for the ACP runtimes
# --------------------------------------------------------------------------- #

def _module(text: str) -> ast.Module | None:
    """The parsed module, or None when the text is not valid Python."""
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _popen_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `subprocess.Popen(...)` / `Popen(...)` call in the module."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "Popen":
            calls.append(node)
    return calls


def _same_expr(node: ast.AST, source: str) -> bool:
    """True when a node is already exactly the expression `source` parses to."""
    try:
        wanted = ast.parse(source, mode="eval")
    except SyntaxError:
        return False
    return ast.dump(node) == ast.dump(wanted.body)


def _has_cwd_expr(call: ast.Call, source: str) -> bool:
    """True when the call's `cwd=` already is exactly this expression."""
    return any(kw.arg == "cwd" and _same_expr(kw.value, source) for kw in call.keywords)


def _binds_os(tree: ast.AST) -> bool:
    """True when the module imports `os` somewhere, so `os.environ` resolves."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any((alias.asname or alias.name.split(".")[0]) == "os" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if any((alias.asname or alias.name) == "os" for alias in node.names):
                return True
    return False


def _line_offsets(text: str) -> list[int]:
    offsets, position = [0], 0
    for line in text.splitlines(keepends=True):
        position += len(line)
        offsets.append(position)
    return offsets


def _span(text: str, offsets: list[int], node: ast.AST) -> tuple[int, int]:
    return (
        offsets[node.lineno - 1] + node.col_offset,
        offsets[node.end_lineno - 1] + node.end_col_offset,
    )


def _add_cwd(text: str, call: ast.Call, expr: str) -> str | None:
    """Add a `cwd=` keyword to a call that has none, keeping its own-line style.

    `col_offset` is relative to that node's own line, so the parenthesis offsets
    are resolved through the line table rather than used as absolute indexes.
    """
    offsets = _line_offsets(text)
    open_at = offsets[call.func.end_lineno - 1] + call.func.end_col_offset
    close_at = offsets[call.end_lineno - 1] + call.end_col_offset - 1
    if open_at >= len(text) or text[open_at] != "(" or close_at >= len(text) or text[close_at] != ")":
        return None
    if "\n" in text[open_at:close_at]:
        line_start = text.rfind("\n", 0, close_at) + 1
        prefix = text[line_start:close_at]
        indent = " " * (len(prefix) + 4) if not prefix.strip() else " " * 4
        lines = text.splitlines(keepends=True)
        lines.insert(text.count("\n", 0, close_at), f"{indent}cwd={expr},\n")
        return "".join(lines)
    head = text[:close_at].rstrip()
    if head.endswith(","):
        return f"{head} cwd={expr}," + text[close_at:]
    return f"{head}, cwd={expr}" + text[close_at:]


def _replace_cwd(text: str, call: ast.Call, expr: str) -> str | None:
    """Rewrite the fallback of a call's existing `cwd=` keyword."""
    node = next((kw.value for kw in call.keywords if kw.arg == "cwd"), None)
    if node is None:
        return None
    start, end = _span(text, _line_offsets(text), node)
    return text[:start] + expr + text[end:]


def _insert_import_os(text: str, tree: ast.Module) -> str:
    """Add `import os` after the last top-level import, keeping the shebang first."""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, node.end_lineno or node.lineno)
    if last:
        lines = text.splitlines(keepends=True)
        lines.insert(last, "import os\n")
        return "".join(lines)
    lines = text.splitlines(keepends=True)
    at = 1 if lines and lines[0].startswith("#!") else 0
    if ast.get_docstring(tree, clean=False) is not None and tree.body:
        at = tree.body[0].end_lineno
    lines.insert(at, "import os\n")
    return "".join(lines)


def same_dir(left: str | Path | None, right: Path) -> bool:
    """True when both paths are the same directory.

    The lanes root has two spellings on a server host (`~/apps/lanes` and the
    volume path), and they are the same directory, so compare the resolved paths
    and never the strings.
    """
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def knob_codex_tmux_boot(root: Path) -> tuple[bool, str]:
    path = HOME / ".local" / "bin" / "codex-tmux-boot"
    if not path.exists():
        return True, "not installed (no codex-tmux-boot)"
    cwd = tmux_cwd(read(path))
    if same_dir(cwd, root):
        return True, f"tmux session cwd is {cwd}"
    return False, f"tmux session cwd is {cwd or 'unset'}, not {root}"


def knob_codex_tmux_autostart(root: Path) -> tuple[bool, str]:
    path = HOME / ".local" / "bin" / "codex-tmux-autostart"
    if not path.exists():
        return True, "not installed (no codex-tmux-autostart)"
    cwd = tmux_cwd(read(path))
    if same_dir(cwd, root):
        return True, f"tmux session cwd is {cwd}"
    return False, f"tmux session cwd is {cwd or 'unset'}, not {root}"


def knob_acp_runtime(path: Path, root: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, f"not installed ({path.name})"
    tree = _module(read(path))
    calls = _popen_calls(tree) if tree is not None else []
    wanted = cwd_expr(root)
    for call in calls:
        for keyword in call.keywords:
            if keyword.arg != "cwd":
                continue
            if _same_expr(keyword.value, wanted):
                return True, f"the CLI starts in {root}"
            return False, f"the CLI's cwd is not {root}"
    return False, f"the CLI does not start in {root}"


def bashrc_block(text: str) -> str:
    """The fleet block inside a .bashrc, or "" when it has none."""
    if BASHRC_BEGIN not in text:
        return ""
    rest = text.split(BASHRC_BEGIN, 1)[1]
    if BASHRC_END not in rest:
        return ""
    return BASHRC_BEGIN + rest.split(BASHRC_END, 1)[0]


def shell_value(text: str) -> str:
    """The value of a shell word list, as the shell would read it.

    The fleet block writes its values shell-quoted, so the check reads them back
    the way a shell would, and not as the raw text of the line.
    """
    try:
        parts = shlex.split(text.strip())
    except ValueError:
        return text.strip()
    return parts[0] if parts else text.strip()


def knob_bashrc(root: Path) -> tuple[bool, str]:
    path = HOME / ".bashrc"
    if not path.exists():
        return True, "not installed (no .bashrc)"
    block = bashrc_block(read(path))
    target = re.search(r"^\s*cd\s+(.+)$", block, flags=re.MULTILINE)
    directory = shell_value(target.group(1)) if target else ""
    if not (target and same_dir(directory, root)):
        return False, f"interactive shells start in {directory or 'nowhere'}, not {root}"
    missing = []
    for var, want in (("FLEET_CODE_ROOT", code_root()),
                      ("FLEET_APPS_ROOT", apps_root()),
                      ("FLEET_LANES_ROOT", root)):
        found = re.search(rf"^\s*export\s+{var}=(.+)$", block, flags=re.MULTILINE)
        if not found:
            missing.append(var)
        elif not same_dir(shell_value(found.group(1)), want):
            missing.append(f"{var}={shell_value(found.group(1))}")
    if missing:
        return False, "the bashrc block does not set " + ", ".join(missing)
    return True, f"interactive shells start in {directory}"


def claude_settings(path: Path) -> dict | None:
    """The host's Claude user settings as a dict, or None when they cannot be read as one."""
    try:
        data = json.loads(read(path) or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def knob_claude_worktrees(path: Path, root: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, f"not installed (no {path.name})"
    data = claude_settings(path)
    if data is None:
        return False, f"{path} is not a JSON object"
    section = data.get("worktree")
    location = section.get("location") if isinstance(section, dict) else None
    if isinstance(location, str) and location and same_dir(_resolve(location), root):
        return True, f"desktop worktrees go to {location}"
    return False, f"desktop worktrees go to {location or '<repo>/.claude/worktrees'}, not {root}"


def knob_unit(path: Path, root: Path, flags: str = "") -> tuple[bool, str]:
    if not path.exists():
        return True, f"not installed ({path.name})"
    text = read(path)
    match = re.search(r"^WorkingDirectory=(\S+)", text, flags=re.MULTILINE)
    wd = match.group(1) if match else None
    if not same_dir(wd, root):
        return False, f"WorkingDirectory is {wd or 'unset'}, not {root}"
    for var, want in (("FLEET_CODE_ROOT", code_root()),
                      ("FLEET_APPS_ROOT", apps_root()),
                      ("FLEET_LANES_ROOT", root)):
        found = re.search(rf'^Environment="{var}=([^"]*)"', text, flags=re.MULTILINE)
        if not found:
            return False, f"{path.name} sets no {var}"
        if not same_dir(found.group(1), want):
            return False, f"{var} is {found.group(1)}, not {want}"
    if flags and unit_execstart_flags(text) != flags:
        return False, f"ExecStart has no {flags.strip()}"
    return True, f"WorkingDirectory is {wd}"


def knobs(root: Path) -> list[tuple[str, Path, str]]:
    """(label, path, kind) for every knob this host can have."""
    apps = apps_root()
    return [
        ("codex-tmux-boot", HOME / ".local" / "bin" / "codex-tmux-boot", "codex-tmux-boot"),
        ("codex-tmux-autostart", HOME / ".local" / "bin" / "codex-tmux-autostart", "codex-tmux-autostart"),
        ("minimax acp runtime", apps / "muse-acp-runtime" / "minimax-shellular-acp.py", "acp"),
        ("muse acp runtime", apps / "muse-acp-runtime" / "muse-shellular-acp.py", "acp"),
        ("bashrc", HOME / ".bashrc", "bashrc"),
        ("claude desktop worktrees", HOME / ".claude" / "settings.json", "claude-settings"),
        ("opencode-server.service", SYSTEMD_DIR / "opencode-server.service", "unit"),
        ("claude-remote-control.service", SYSTEMD_DIR / "claude-remote-control.service", "unit"),
    ]


def check(root: Path, skipped: dict[str, str] | None = None) -> list[tuple[str, bool, str]]:
    """(label, ok, note) for every knob, with the skipped ones reported as off.

    A knob the apply could not write is off:  it is reported as such here, so
    the verify says the policy did not take for it.
    """
    skipped = skipped or {}
    out: list[tuple[str, bool, str]] = []
    for label, path, kind in knobs(root):
        if label in skipped:
            out.append((label, False, f"not applied: {skipped[label]}"))
            continue
        if kind == "codex-tmux-boot":
            ok, note = knob_codex_tmux_boot(root)
        elif kind == "codex-tmux-autostart":
            ok, note = knob_codex_tmux_autostart(root)
        elif kind == "acp":
            ok, note = knob_acp_runtime(path, root)
        elif kind == "bashrc":
            ok, note = knob_bashrc(root)
        elif kind == "claude-settings":
            ok, note = knob_claude_worktrees(path, root)
        else:
            flags = ""
            if label.startswith("opencode"):
                try:
                    flags = opencode_server_flags()
                except SystemExit as exc:
                    out.append((label, False, str(exc)))
                    continue
            ok, note = knob_unit(path, root, flags)
        out.append((label, ok, note))
    return out


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def apply_codex(path: Path, root: Path) -> bool:
    """Point the tmux session at the lanes root."""
    text = read(path)
    match = next((m for m in TMUX_CWD.finditer(text) if _is_dir_token(m.group(1))), None)
    if match is not None:
        cwd = match.group(1)
        if same_dir(cwd, root):
            return False
        # Rewrite the captured token by span, so a Codex `-c key=value` earlier in
        # the launcher is never the text that changes.
        start, end = match.span(1)
        updated = text[:start] + str(root) + text[end:]
    else:
        anchor = TMUX_NEW_SESSION.search(text)
        if anchor is None:
            return False
        # No directory named yet: name the lanes root for the new session.
        updated = text[: anchor.end()] + f" -c {root}" + text[anchor.end() :]
    if updated == text:
        return False
    prepare_staged_directory(path)
    backup(path)
    write(path, updated)
    return True


def apply_acp(path: Path, root: Path) -> bool:
    """Make the ACP runtime spawn its CLI in the lanes root.

    The edit is structural:  the `subprocess.Popen` call that starts the CLI
    gains a `cwd=` keyword that reads FLEET_LANES_ROOT and falls back to the
    resolved lanes root, `os` is imported when the file does not import it, and a
    stale baked-in fallback is rewritten to the current root.  The rewritten file
    is parsed before it is written, so a runtime is never left broken.
    """
    text = read(path)
    tree = _module(text)
    if tree is None:
        return False
    calls = _popen_calls(tree)
    if not calls:
        return False
    target = next((c for c in calls if any(kw.arg == "cwd" for kw in c.keywords)), calls[0])
    wanted = cwd_expr(root)
    if _has_cwd_expr(target, wanted):
        return False
    if any(kw.arg == "cwd" for kw in target.keywords):
        updated = _replace_cwd(text, target, wanted)
    else:
        updated = _add_cwd(text, target, wanted)
    if updated is None:
        return False
    rewritten = _module(updated)
    if rewritten is None:
        return False
    if not _binds_os(rewritten):
        updated = _insert_import_os(updated, rewritten)
        rewritten = _module(updated)
        if rewritten is None or not _binds_os(rewritten):
            return False
    if updated == text:
        return False
    prepare_staged_directory(path)
    backup(path)
    write(path, updated)
    return True


def apply_bashrc(root: Path) -> bool:
    path = HOME / ".bashrc"
    text = read(path)
    if bashrc_block(text) and knob_bashrc(root)[0]:
        return False
    prepare_staged_directory(path)
    backup(path)
    new_block = BASHRC_BLOCK.format(
        begin=BASHRC_BEGIN, end=BASHRC_END, lanes=root, lanes_root=root,
        lanes_q=shlex.quote(str(root)),
        exports=bashrc_exports(code_root(), apps_root(), root),
    ).strip("\n")
    if bashrc_block(text):
        start = text.index(BASHRC_BEGIN)
        end = text.index(BASHRC_END) + len(BASHRC_END)
        # The old block's closing marker is a whole line, so cut past the end of
        # that line:  the new block renders its own closing line, and keeping the
        # tail of the old one would write the marker twice.
        line_end = text.find("\n", end)
        end = len(text) if line_end == -1 else line_end + 1
        new_text = text[:start] + new_block + "\n" + text[end:]
    else:
        new_text = text.rstrip("\n") + "\n\n" + new_block + "\n"
    write_atomic(path, new_text)
    return True


def apply_claude_settings(path: Path, root: Path) -> bool:
    """Point `worktree.location` at the lanes root, keeping every other setting.

    The value is the resolved root, so on a host whose `~/apps` is a symlink into
    a volume the setting names the volume path that git and the desktop app record.
    A file that is not a JSON object is left alone and the knob stays off.
    """
    if knob_claude_worktrees(path, root)[0]:
        return False
    data = claude_settings(path)
    if data is None:
        return False
    section = data.get("worktree")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        return False
    section["location"] = str(root.resolve())
    data["worktree"] = section
    prepare_staged_directory(path)
    backup(path)
    write(path, json.dumps(data, indent=2) + "\n")
    return True


def environment_assignments(line: str) -> list[tuple[str, str]]:
    """The variables an `Environment=` line sets, each with its original text.

    systemd reads the payload as space-separated assignments, and a value may be
    quoted, so the payload is split on the whitespace that is outside quotes.
    The original text of every assignment is kept, so a line that is carried
    over keeps the quoting the operator wrote.
    """
    payload = line.strip()[len("Environment="):]
    segments: list[str] = []
    current = ""
    quote = ""
    index = 0
    while index < len(payload):
        char = payload[index]
        if quote:
            current += char
            if char == "\\" and quote == '"' and index + 1 < len(payload):
                current += payload[index + 1]
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current += char
        elif char.isspace():
            if current:
                segments.append(current)
                current = ""
        else:
            current += char
        index += 1
    if current:
        segments.append(current)
    assignments: list[tuple[str, str]] = []
    for segment in segments:
        name = segment
        if name[:1] in "\"'":
            name = name[1:]
        if name.endswith('"') and not name.endswith('\\"'):
            name = name[:-1]
        name = name.split("=", 1)[0].strip()
        if name:
            assignments.append((name, segment))
    return assignments


# The variables the script owns in a unit:  the canonical roots.  An operator's own
# value for one of them is dropped, because the rewrite exists to point the unit at
# the fleet's roots and not at a tree that is no longer there.
OWNED_ENVIRONMENT = (
    "FLEET_CODE_ROOT", "FLEET_APPS_ROOT", "FLEET_LANES_ROOT", "FLEET_SERVER_VOLUME"
)

# The directives that carry the identity of a unit:  the account it runs as and the
# command it runs.  The template carries a default for each of them, and an operator
# who set one keeps their own value:  a unit that runs as another user, or runs
# another binary, is not the unit the operator set up.
IDENTITY_DIRECTIVES = ("User", "Group", "ExecStart")

# The environment the account reads.  Like the identity directives, the operator's own
# value wins over the template's default.
IDENTITY_ENVIRONMENT = ("HOME", "PATH")

# The placeholder the template carries where the binary goes.  It is resolved only
# when the template's own ExecStart survives the merge, so a unit that names its own
# command never needs the template's binary to exist on the host.
BINARY_MARKER = "@@fleet-binary@@"


def unit_logical_lines(text: str) -> list[str]:
    """The lines of a unit, with a continued line joined to the line it continues.

    systemd continues a line that ends with a backslash, so the command of a
    directive is the line and the lines it continues.  A directive that is spliced
    back into a unit must not keep a trailing backslash, or systemd reads the line
    after it as part of the command.
    """
    joined: list[str] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if joined and joined[-1].rstrip().endswith("\\"):
            joined[-1] = joined[-1].rstrip()[:-1].rstrip() + " " + stripped
            continue
        joined.append(raw)
    return joined


def unit_directives(text: str, name: str) -> list[str]:
    """The lines of a unit that set `name`, in the order they appear.

    A line the unit continues is one line here, so the command keeps its whole
    value when it is spliced back into a unit.
    """
    prefix = name + "="
    return [line.strip() for line in unit_logical_lines(text) if line.strip().startswith(prefix)]


def unit_environment(text: str) -> dict[str, list[str]]:
    """The environment variables a unit sets, each with the assignments it wrote."""
    found: dict[str, list[str]] = {}
    for line in unit_logical_lines(text):
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        for name, raw in environment_assignments(stripped):
            found.setdefault(name, []).append(raw)
    return found


def service_end(lines: list[str]) -> int:
    """The index of the first line after the [Service] section of a unit."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == "[Service]":
                start = index
            continue
        if stripped.startswith("["):
            return index
    return len(lines)


def replace_identity_environment(lines: list[str], existing: str) -> tuple[list[str], set[str]]:
    """The body lines with the operator's own HOME and PATH in place of the template's.

    The template's line for a variable the operator set is replaced and not kept next
    to it, so the unit carries one value for it and the template's default is gone.
    The variables placed this way are returned, so they are not carried over twice.
    """
    operator = unit_environment(existing)
    placed: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            out.append(line)
            continue
        assignments = environment_assignments(stripped)
        replaced = {
            name for name, _ in assignments
            if name in IDENTITY_ENVIRONMENT and operator.get(name)
        }
        if not replaced:
            out.append(line)
            continue
        placed.update(replaced)
        payload = [
            operator[name][-1] if name in replaced else raw
            for name, raw in assignments
        ]
        out.append("Environment=" + " ".join(payload))
    return out, placed


def carried_directives(text: str, body: str, placed: set[str] | None = None) -> list[str]:
    """The unit directives this script does not own, taken from the unit it replaces.

    An `EnvironmentFile=` the operator added, and an `Environment=` the template does
    not set, belong to the operator, so they are carried over instead of silently
    dropped by the rewrite.

    A root the script owns is the script's:  systemd takes the last assignment for a
    variable, and the carried line lands after the template's, so a line that still
    sets it would run the unit with the older value.  That assignment is dropped, and
    the rest of the line is carried over.  A variable that was placed in place of the
    template's own line is not carried over a second time.
    """
    if not text:
        return []
    owned = set(OWNED_ENVIRONMENT)
    done = set(placed or ())
    carried: list[str] = []
    for line in unit_logical_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("EnvironmentFile="):
            carried.append(stripped)
            continue
        if not stripped.startswith("Environment=") or stripped in body:
            continue
        assignments = environment_assignments(stripped)
        if not assignments:
            carried.append(stripped)
            continue
        keep = [(name, raw) for name, raw in assignments if name not in owned and name not in done]
        if not keep:
            continue
        if len(keep) == len(assignments):
            carried.append(stripped)
            continue
        carried.append("Environment=" + " ".join(raw for _, raw in keep))
    return carried


def reconcile_execstart(line: str, flags: str) -> str:
    """The operator's ExecStart line with the knob's flags in its command.

    The operator's binary, its argv and its account are the operator's, but the host
    and the port the fleet's clients use are the knob's policy, so the pair in the
    command is the configured one:  a stale pair is replaced and a command that
    carries no pair gains one.  A command with no configured pair is left exactly as
    it is.
    """
    prefix = "ExecStart="
    if not flags or not line.startswith(prefix):
        return line
    command = line[len(prefix):]
    stale = command_flags(command)
    command = command.replace(stale, flags, 1) if stale else command + flags
    return prefix + command


def merge_unit(body: str, existing: str, flags: str = "") -> str:
    """The template body with the operator's own settings kept.

    The operator's identity directives win over the template's defaults:  the account
    the unit runs as, the command it runs and the environment that account reads.  A
    directive the operator did not set keeps the template's value.

    The operator's ExecStart lines replace the template's line and never sit next to
    it:  systemd runs every ExecStart line of a unit, so a second one would run the
    command twice.

    The canonical roots are the script's own:  an operator's stale value for one of
    them is dropped, because the rewrite exists to point the unit at the fleet's roots.
    """
    lines = body.splitlines()
    for name in IDENTITY_DIRECTIVES:
        theirs = unit_directives(existing, name)
        if not theirs:
            continue
        if name == "ExecStart" and flags:
            # The knob reads the first ExecStart line, so only that one carries the
            # host and the port the fleet's clients use.
            theirs = [reconcile_execstart(theirs[0], flags)] + theirs[1:]
        indexes = [i for i, line in enumerate(lines) if line.strip().startswith(name + "=")]
        if indexes:
            first = indexes[0]
            for index in reversed(indexes):
                del lines[index]
            lines[first:first] = theirs
        else:
            at = service_end(lines)
            lines[at:at] = theirs
    lines, placed = replace_identity_environment(lines, existing)
    carried = carried_directives(existing, body, placed)
    if carried:
        after = [
            i for i, line in enumerate(lines)
            if line.startswith(("Environment=", "EnvironmentFile="))
        ]
        at = max(after) + 1 if after else len(lines)
        lines[at:at] = carried
    return "\n".join(lines) + "\n"


def apply_unit(path: Path, body: str, tool: str = "", flags: str = "") -> bool:
    """Replace a unit with the template, keeping the operator's own settings.

    The operator's identity directives win over the template's defaults, so an apply
    never rewrites the account a unit runs as, the command it runs or the environment
    that account reads.

    `tool` names the binary the template's own ExecStart runs.  It is resolved only
    when that line survives the merge:  a unit that already names its command keeps
    it, and the host need not carry the template's binary at all.
    """
    text = read(path)
    merged = merge_unit(body, text, flags)
    if tool and BINARY_MARKER in merged:
        merged = merged.replace(BINARY_MARKER, which(tool))
    if text == merged:
        return False
    prepare_staged_directory(path)
    backup(path)
    write(path, merged)
    return True


def apply_knob(label: str, path: Path, kind: str, root: Path) -> tuple[bool, str]:
    """Apply one knob and name the service a change to it needs restarted.

    The second value is the service to restart, or "" when the knob needs none.
    An OSError raised here means the running account cannot write the knob:  the
    caller skips it and carries on with the knobs after it.
    """
    if kind in ("codex-tmux-boot", "codex-tmux-autostart"):
        return (path.exists() and apply_codex(path, root)), ""
    if kind == "acp":
        return (path.exists() and apply_acp(path, root)), ""
    if kind == "bashrc":
        return apply_bashrc(root), ""
    if kind == "claude-settings":
        return (path.exists() and apply_claude_settings(path, root)), ""
    if label.startswith("opencode"):
        flags = opencode_server_flags() or unit_execstart_flags(read(path))
        body = OPENCODE_UNIT.format(
            lanes=root, home=HOME, opencode=BINARY_MARKER, flags=flags,
            **unit_environments(root),
        )
        touched = apply_unit(path, body, "opencode", flags)
        return touched, "opencode-server.service" if touched else ""
    body = CLAUDE_UNIT.format(
        lanes=root, home=HOME, claude=BINARY_MARKER,
        **unit_environments(root),
    )
    touched = apply_unit(path, body, "claude")
    return touched, "claude-remote-control.service" if touched else ""


def skip_reason(exc: OSError) -> str:
    """Why a knob was skipped, in the words the skip line carries."""
    if isinstance(exc, PermissionError):
        # A refusal the tool raises itself names the object that needs the
        # permission, because the file itself can be writable while the
        # directory the staged file has to be created in is not, and only the
        # directory can be fixed.  A permission the operating system raises
        # says nothing more than the account cannot write the file, so the
        # skip line stays the short one it has always been.
        if exc.errno is None:
            detail = str(exc).strip()
            if detail:
                return f"not writable by this account: {detail}"
        return "not writable by this account"
    return f"not written: {exc.strerror or exc}"


def apply(root: Path) -> int:
    changed = 0
    restart: list[str] = []
    skipped: dict[str, str] = {}
    refused: SystemExit | None = None
    for label, path, kind in knobs(root):
        if kind == "unit" and not path.parent.is_dir():
            print(f"  skipped  {label:26} ({path.parent} does not exist on this host)")
            continue
        try:
            touched, service = apply_knob(label, path, kind, root)
        except OSError as exc:
            # A knob the running account cannot write is skipped, not fatal:  the
            # knobs after it still apply, and the verify at the end still runs.
            reason = skip_reason(exc)
            print(f"  skipped  {label:26} ({reason})")
            skipped[label] = reason
            continue
        except SystemExit as exc:
            # A knob the tool refuses to write (a host or port it will not
            # interpolate, a binary it will not invent a path for) is reported
            # and the knobs after it still apply.  The refusal is raised once
            # the verify has run, so the run still ends on it.
            reason = f"refused: {exc}"
            print(f"  skipped  {label:26} ({reason})")
            skipped[label] = reason
            if refused is None:
                refused = exc
            continue
        changed += 1 if touched else 0
        if touched and service:
            restart.append(service)
        print(f"  {'changed' if touched else 'ok     '}  {label}")
    if changed:
        print("\n  Restart what the change affects:")
        print("    systemctl daemon-reload")
        if restart:
            print(f"    systemctl restart {' '.join(restart)}")
        print("    (the Codex tmux session picks the new cwd up when it is next created)")
        print("    (the Claude desktop app reads worktree.location when it makes its next worktree)")
    # A knob that could not be applied is still off, so the apply reports it the
    # way --verify does instead of printing `ok` and exiting zero.
    failed = verify(root, skipped)
    if refused is not None:
        raise refused
    return failed


def verify(root: Path, skipped: dict[str, str] | None = None) -> int:
    failed = 0
    for label, ok, note in check(root, skipped):
        print(f"  {'PASS' if ok else 'FAIL'}  {label:26} {note}")
        failed += 0 if ok else 1
    if failed:
        print(f"\n  {failed} knob(s) are off.  Run with --apply to set them.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the working directories")
    parser.add_argument("--verify", action="store_true", help="report the working directories")
    args = parser.parse_args(argv)

    root = lanes_root()
    print(f"lanes root: {root}")
    if not root.is_dir():
        print("  the lanes root does not exist on this host")
        return 2

    if args.apply:
        return apply(root)
    return verify(root)


if __name__ == "__main__":
    sys.exit(main())