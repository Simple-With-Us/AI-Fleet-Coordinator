#!/usr/bin/env python3
"""Point every agent tool's session working directory at the lanes root.

Owner policy, Oct 22, 2026:  on a server host the project mirrors live on the
fleet-extra volume (`/mnt/fleet-extra/Code`) and the lanes live beside them
(`/mnt/fleet-extra/apps/lanes`), and `~/Code` and `~/apps` resolve to them, so
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
import shutil
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/root")).expanduser()

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


def lanes_root() -> Path:
    for candidate in (
        os.environ.get("FLEET_LANES_ROOT"),
        str(HOME / "apps" / "lanes"),
        "/mnt/fleet-extra/apps/lanes",
    ):
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "apps" / "lanes"


def apps_root() -> Path:
    for candidate in (
        os.environ.get("FLEET_APPS_ROOT"),
        str(HOME / "apps"),
        "/mnt/fleet-extra/apps",
    ):
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "apps"


def code_root() -> Path:
    """Where the integration trees live:  FLEET_CODE_ROOT, then ~/Code, then the volume."""
    for candidate in (
        os.environ.get("FLEET_CODE_ROOT"),
        str(HOME / "Code"),
        "/mnt/fleet-extra/Code",
    ):
        if not candidate:
            continue
        path = _resolve(candidate)
        if path.is_dir():
            return path
    return HOME / "Code"


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, f"{path}.bak-{stamp()}")


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


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
export FLEET_CODE_ROOT="{code}"
export FLEET_APPS_ROOT="{apps}"
export FLEET_LANES_ROOT="{lanes}"
if [ -d "{lanes}" ] && [ "$PWD" = "$HOME" ]; then
  cd "{lanes}"
fi
{end} ----------------------------------------------
"""

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
# The config's server block is deprecated in v2 and the port is ignored, so the
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
# The OAuth token is read into memory once and never refreshed, so the process
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
    """The ExecStart line of a unit, or "" when it has none."""
    match = re.search(r"^ExecStart=(.*)$", text or "", flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def unit_execstart_flags(text: str) -> str:
    """The `--hostname`/`--port` pair a unit already carries, or ""."""
    match = re.search(r"\s--hostname\s+\S+\s+--port\s+\S+", unit_execstart(text))
    return match.group(0) if match else ""


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


def knob_bashrc(root: Path) -> tuple[bool, str]:
    path = HOME / ".bashrc"
    if not path.exists():
        return True, "not installed (no .bashrc)"
    block = bashrc_block(read(path))
    target = re.search(r'^\s*cd\s+"([^"]+)"', block, flags=re.MULTILINE)
    if not (target and same_dir(target.group(1), root)):
        return False, f"interactive shells start in {target.group(1) if target else 'nowhere'}, not {root}"
    missing = []
    for var, want in (("FLEET_CODE_ROOT", code_root()),
                      ("FLEET_APPS_ROOT", apps_root()),
                      ("FLEET_LANES_ROOT", root)):
        found = re.search(rf'^\s*export\s+{var}="([^"]+)"', block, flags=re.MULTILINE)
        if not found:
            missing.append(var)
        elif not same_dir(found.group(1), want):
            missing.append(f"{var}={found.group(1)}")
    if missing:
        return False, "the bashrc block does not set " + ", ".join(missing)
    return True, f"interactive shells start in {target.group(1)}"


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


def check(root: Path) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    for label, path, kind in knobs(root):
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
    backup(path)
    write(path, updated)
    return True


def apply_bashrc(root: Path) -> bool:
    path = HOME / ".bashrc"
    text = read(path)
    if bashrc_block(text) and knob_bashrc(root)[0]:
        return False
    backup(path)
    new_block = BASHRC_BLOCK.format(
        begin=BASHRC_BEGIN, end=BASHRC_END, lanes=root, lanes_root=root,
        code=code_root(), apps=apps_root(),
    ).strip("\n")
    if bashrc_block(text):
        start = text.index(BASHRC_BEGIN)
        end = text.index(BASHRC_END) + len(BASHRC_END)
        new_text = text[:start] + new_block + text[end:]
    else:
        new_text = text.rstrip("\n") + "\n\n" + new_block + "\n"
    with path.open("w") as handle:
        handle.write(new_text)
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
    backup(path)
    write(path, json.dumps(data, indent=2) + "\n")
    return True


def carried_directives(text: str, body: str) -> list[str]:
    """The unit directives this script does not own, taken from the unit it replaces.

    The template owns WorkingDirectory, ExecStart, the HOME/PATH environment and
    the restart policy.  An `EnvironmentFile=` the operator added, and an
    `Environment=` the template does not set, belong to the operator, so they are
    carried over instead of silently dropped by the rewrite.
    """
    if not text:
        return []
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("EnvironmentFile=")
        or (line.strip().startswith("Environment=") and line.strip() not in body)
    ]


def apply_unit(path: Path, body: str) -> bool:
    text = read(path)
    carried = carried_directives(text, body)
    if carried:
        lines = body.splitlines()
        after = [i for i, line in enumerate(lines) if line.startswith(("Environment=", "EnvironmentFile="))]
        lines[max(after) + 1 : max(after) + 1] = carried
        body = "\n".join(lines) + "\n"
    if text == body:
        return False
    backup(path)
    write(path, body)
    return True


def apply(root: Path) -> int:
    changed = 0
    restart: list[str] = []
    for label, path, kind in knobs(root):
        if kind in ("codex-tmux-boot", "codex-tmux-autostart"):
            touched = path.exists() and apply_codex(path, root)
        elif kind == "acp":
            touched = path.exists() and apply_acp(path, root)
        elif kind == "bashrc":
            touched = apply_bashrc(root)
        elif kind == "claude-settings":
            touched = path.exists() and apply_claude_settings(path, root)
        elif not path.parent.is_dir():
            print(f"  skipped  {label:26} ({path.parent} does not exist on this host)")
            continue
        elif label.startswith("opencode"):
            flags = opencode_server_flags() or unit_execstart_flags(read(path))
            body = OPENCODE_UNIT.format(
                lanes=root, home=HOME, opencode=which("opencode"), flags=flags
            )
            touched = apply_unit(path, body)
            if touched:
                restart.append("opencode-server.service")
        else:
            body = CLAUDE_UNIT.format(lanes=root, home=HOME, claude=which("claude"))
            touched = apply_unit(path, body)
            if touched:
                restart.append("claude-remote-control.service")
        changed += 1 if touched else 0
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
    return verify(root)


def verify(root: Path) -> int:
    failed = 0
    for label, ok, note in check(root):
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