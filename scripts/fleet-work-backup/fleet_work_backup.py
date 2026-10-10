#!/usr/bin/env python3
"""fleet-work-backup:  back up the fleet's work that GitHub does not hold.

Two things are staged into one fixed directory and sent to an encrypted restic repository on
Backblaze B2 (prefix ``fleet-work-backup/`` in the bucket named by ``B2_FLEET_BUCKET_NAME``):

  (a) Git work.  For every checkout the fleet uses (lanes under ~/apps/lanes, flat ~/apps/<x>
      lanes, other clones under ~/apps, and the human trees under ~/Code):  the commits that no
      remote has (one ``git bundle`` per repository, shared by all of its worktrees), every
      stash (as per-file patches), and per worktree the uncommitted work (one patch per tracked
      file against HEAD, plus the untracked files git does not ignore).  A checkout with nothing
      unpushed and nothing dirty adds nothing.
  (b) Non-git files under ~/apps that sit outside any checkout (protocol docs, effort logs,
      helper scripts, the board database, runtime configs).

Never staged:  secrets.  A name filter runs before anything is copied, a content scan (gitleaks
when installed, always a regex scan) runs over the staged set, and a match drops the file and
logs its path only.  Infisical is the source of truth for secrets.

All git access is read-only (GIT_OPTIONAL_LOCKS=0, no fetch, no gc, no ref writes).

  fleet_work_backup.py run                  # one backup (what the LaunchAgent does)
  fleet_work_backup.py run --dry-run        # stage, list sizes, upload nothing, no lock, no gate
  fleet_work_backup.py init                 # restic init (explicit;  a scheduled run never inits)
  fleet_work_backup.py check-access         # can this process read /Volumes/External?
  fleet_work_backup.py snapshots            # restic snapshots for this job's tag
  fleet_work_backup.py restic -- <args>     # restic with this job's repo and credentials (restores)

Standard library only.  Test hooks are the Config fields and FLEET_WORK_BACKUP_* variables.
Docs:  scripts/fleet-work-backup/README.md and RESTORE.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import ctypes
import dataclasses
import errno
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

VERSION = "1"
TAG = "fleet-work"
RETENTION = (("--keep-hourly", "24"), ("--keep-daily", "14"), ("--keep-weekly", "8"), ("--keep-monthly", "6"))
PRUNE_EVERY = 24 * 3600
CHECK_EVERY = 7 * 24 * 3600
SKIP_WARN_AFTER = 8  # consecutive skipped runs (about 24 hours at one run per 3 hours)

# Directory names never entered (git discovery and the non-git file walk).
SKIP_DIR_NAMES = {
    "node_modules", ".venv", "venv", "__pycache__", ".cache", "Caches", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "site-packages", "dist", "build", ".next", ".turbo", ".parcel-cache",
    "DerivedData", "Pods", ".gradle", ".idea", ".git", ".Trash", "logs", "coverage", ".dart_tool",
}
# File names never staged (logs, caches, build debris, live-DB sidecars).
SKIP_FILE_GLOBS = (
    "*.log", "*.log.*", "*.pyc", "*.pyo", ".DS_Store", "*.swp", "*.swo", "*.tmp", "*.sock", "*.pid",
    "*-wal", "*-shm", "*.db-journal", "*.sqlite-journal", "*.db-wal", "*.db-shm",
)
# Secret-looking names, matched case-insensitively against a file's base name.  STRICT names are key
# and credential material:  they are dropped everywhere, tracked-file patches included.  BROAD names
# (secret, token, credential, passwd) are dropped for untracked and non-git files;  for the patch of
# a tracked file they are not, because that file is source already in git (tokenizer.py, auth/
# credentials.ts) and the content scan still guards it.
SECRET_STRICT_GLOBS = (
    ".env", ".env.*", "*.env", "*.env.*", "*zuliprc*", "*.pem", "*.p8", "*.p12", "*.pfx", "*.key", "*.keystore",
    "*.jks", "*.keychain", "*.keychain-db", "*.kdbx", "*.pkcs12", "id_rsa*", "id_dsa*", "id_ecdsa*",
    "id_ed25519*", "id_xmss*", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd", "global-api-keys*",
    "infisical-machine-identity*", "service-account*.json", "*serviceaccount*.json", "credentials.json",
)
SECRET_BROAD_GLOBS = ("*secret*", "*token*", "*credential*", "*passwd*")
# A name that matches a secret glob but is a documented template.  Content is still scanned.
SECRET_NAME_ALLOW = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
                     "*.env.example", "*.env.sample", "*.env.template")
# Directory components that mark a secret store.  Everything below one is dropped.
SECRET_DIR_NAMES = {".secrets", ".ssh", ".gnupg", ".aws", ".infisical", "Keychains", ".docker", ".kube"}
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")

# Content patterns (bytes).  High confidence only:  a match drops the whole file or patch.
CONTENT_PATTERNS: dict[str, re.Pattern[bytes]] = {
    "private-key-block": re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    "aws-access-key-id": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "slack-token": re.compile(rb"\bxox[abprso]-[A-Za-z0-9-]{10,}"),
    "stripe-key": re.compile(rb"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "google-api-key": re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "anthropic-key": re.compile(rb"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai-key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "bearer-header": re.compile(rb"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{24,}"),
    # Environment-style names are upper case by convention, which keeps this fast and quiet.
    "env-assignment": re.compile(
        rb"(?m)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*[ \t]*=[ \t]*"
        rb"['\"]?([A-Za-z0-9/+=_\-.]{20,})['\"]?[ \t]*$"),
}
ENTROPY_GATED = {"env-assignment"}
# A pattern runs only when one of its literals is present:  a substring test is orders of magnitude
# cheaper than a regex over gigabytes.
PREFILTER: dict[str, tuple[bytes, ...]] = {
    "private-key-block": (b"PRIVATE KEY",),
    "aws-access-key-id": (b"AKIA", b"ASIA"),
    "github-token": (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_"),
    "slack-token": (b"xox",),
    "stripe-key": (b"k_live_", b"k_test_"),
    "google-api-key": (b"AIza",),
    "anthropic-key": (b"sk-ant-",),
    "openai-key": (b"sk-",),
    "jwt": (b"eyJ",),
    "bearer-header": (b"earer", b"EARER"),
    "env-assignment": (b"KEY", b"SECRET", b"TOKEN", b"PASSWORD", b"PASSWD"),
}

# macOS clonefile(2), loaded lazily.  A clone is instant, free until written and a point-in-time
# copy, so what the gate scans is what restic reads.
_CLONEFILE = None


def say(msg: str) -> None:
    print(f"fleet-work-backup: {msg}", flush=True)


# ------------------------------------------------------------------------------------------ config
@dataclasses.dataclass
class Config:
    home: Path
    git_bin: str = "git"
    restic_bin: str = "restic"
    gitleaks_bin: Optional[str] = "gitleaks"
    max_file_bytes: int = 200 * 1024 * 1024
    max_untracked_files: int = 20000
    load_limit: float = 60.0
    swap_limit: float = 0.95
    min_free_bytes: int = 5 * 1024 ** 3
    workers: int = 4
    scan_cap_bytes: int = 256 * 1024 ** 2
    bundle_scan_cap: int = 128 * 1024 ** 2
    host: str = "jay-mac"
    s3_endpoint: str = "s3.eu-central-003.backblazeb2.com"
    check_landed: bool = True
    git_timeout: int = 180
    bundle_timeout: int = 900
    max_runtime: int = 2 * 3600
    gitleaks_ignore_rules: tuple = ()
    gitleaks_budget_s: int = 1500
    load_fn: Optional[Callable[[], float]] = None
    swap_fn: Optional[Callable[[], Optional[float]]] = None
    now_fn: Optional[Callable[[], float]] = None

    @property
    def apps(self) -> Path:
        return self.home / "apps"

    @property
    def code(self) -> Path:
        return self.home / "Code"

    @property
    def lanes(self) -> Path:
        return self.apps / "lanes"

    @property
    def install_dir(self) -> Path:
        return self.apps / "fleet-work-backup"

    @property
    def cache_dir(self) -> Path:
        return self.home / "Library" / "Caches" / "fleet-work-backup"

    @property
    def stage(self) -> Path:
        return self.cache_dir / "stage"

    @property
    def dry_stage(self) -> Path:
        return self.cache_dir / "stage-dry"

    @property
    def state_dir(self) -> Path:
        return self.home / "Library" / "Application Support" / "fleet-work-backup"

    @property
    def log_path(self) -> Path:
        return self.home / "Library" / "Logs" / "fleet-work-backup.log"

    @property
    def detail_log_path(self) -> Path:
        return self.home / "Library" / "Logs" / "fleet-work-backup.detail.log"

    @property
    def handoff(self) -> Path:
        return self.home / ".secrets" / "global-api-keys"

    def now(self) -> float:
        return self.now_fn() if self.now_fn else time.time()


def config_from_env() -> Config:
    home = Path(os.environ.get("FLEET_WORK_BACKUP_HOME") or os.path.expanduser("~"))
    cfg = Config(home=home)
    cfg.git_bin = os.environ.get("FLEET_WORK_BACKUP_GIT", cfg.git_bin)
    cfg.restic_bin = os.environ.get("FLEET_WORK_BACKUP_RESTIC", cfg.restic_bin)
    gl = os.environ.get("FLEET_WORK_BACKUP_GITLEAKS", cfg.gitleaks_bin or "")
    cfg.gitleaks_bin = gl or None
    cfg.host = os.environ.get("FLEET_WORK_BACKUP_HOST", cfg.host)
    cfg.s3_endpoint = os.environ.get("FLEET_WORK_BACKUP_S3_ENDPOINT", cfg.s3_endpoint)
    for env, attr, cast in (("FLEET_WORK_BACKUP_LOAD_LIMIT", "load_limit", float),
                            ("FLEET_WORK_BACKUP_SWAP_LIMIT", "swap_limit", float),
                            ("FLEET_WORK_BACKUP_WORKERS", "workers", int),
                            ("FLEET_WORK_BACKUP_MAX_FILE_MB", "max_file_bytes", lambda v: int(float(v) * 1024 * 1024))):
        if os.environ.get(env):
            try:
                setattr(cfg, attr, cast(os.environ[env]))
            except ValueError:
                pass
    return cfg


# --------------------------------------------------------------------------------------- utilities
def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def stamp(ts: Optional[float] = None) -> str:
    """12-hour local time, no zone name: 2026-10-10 3:55pm."""
    t = time.localtime(ts if ts is not None else time.time())
    hour = t.tm_hour % 12 or 12
    return f"{time.strftime('%Y-%m-%d', t)} {hour}:{t.tm_min:02d}{'am' if t.tm_hour < 12 else 'pm'}"


def slugify(text: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:limit] or "x"


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def strip_url_credentials(url: str) -> str:
    return re.sub(r"(?<=://)[^/@\s]+@", "", url)


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts: dict[int, int] = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    total = len(data)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def _glob_hit(name: str, globs: Iterable[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, g.lower()) for g in globs)


def is_secret_name(rel: str, tracked: bool = False) -> bool:
    """True when a relative path looks like a secret by name.  Runs before anything is copied.
    ``tracked`` is the patch of a file git already tracks:  only the strict names apply."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    for comp in parts[:-1]:
        if comp in SECRET_DIR_NAMES or (not tracked and "secret" in comp.lower()):
            return True
    base = parts[-1]
    if base in SECRET_DIR_NAMES:
        return True
    if _glob_hit(base, SECRET_NAME_ALLOW):
        return False
    if _glob_hit(base, SECRET_STRICT_GLOBS):
        return True
    return not tracked and _glob_hit(base, SECRET_BROAD_GLOBS)


def is_skipped_file(name: str) -> bool:
    return _glob_hit(name, SKIP_FILE_GLOBS)


def scan_bytes(data: bytes) -> Optional[str]:
    """Return the id of the first content pattern that matches, else None."""
    for pid, rx in CONTENT_PATTERNS.items():
        if not any(lit in data for lit in PREFILTER[pid]):
            continue
        if pid in ENTROPY_GATED:
            for m in rx.finditer(data):
                val = m.group(1)
                if entropy(val) >= 3.3 and any(48 <= c <= 57 for c in val) and not val.lower().startswith(b"your"):
                    return pid
        elif rx.search(data):
            return pid
    return None


def scan_file(path: Path, cap: int) -> Optional[str]:
    """Scan a file in overlapping chunks (binary too:  a SQLite page holds plain text).
    A file over ``cap`` is scanned up to the cap."""
    chunk = 8 * 1024 * 1024
    overlap = 8192
    read = 0
    tail = b""
    try:
        with open(path, "rb") as fh:
            while read < cap:
                buf = fh.read(chunk)
                if not buf:
                    break
                read += len(buf)
                hit = scan_bytes(tail + buf)
                if hit:
                    return hit
                tail = buf[-overlap:]
    except OSError:
        return None
    return None


def clone_or_copy(src: str, dst: str) -> str:
    """Copy a regular file keeping its mtime.  Returns 'clone' or 'copy'."""
    global _CLONEFILE
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if sys.platform == "darwin":
        try:
            if _CLONEFILE is None:
                _CLONEFILE = ctypes.CDLL(None, use_errno=True).clonefile
                _CLONEFILE.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
            if _CLONEFILE(os.fsencode(src), os.fsencode(dst), 0) == 0:
                st = os.stat(src)
                os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
                return "clone"
        except (AttributeError, OSError):
            pass
    shutil.copy2(src, dst)
    return "copy"


# ------------------------------------------------------------------------------------------- git
class Git:
    """Read-only git runner.  Never writes a ref, fetches, or garbage-collects."""

    BASE = ("-c", "safe.directory=*", "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
            "-c", "gc.auto=0", "-c", "advice.detachedHead=false")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.env = dict(os.environ)
        self.env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
                         "LC_ALL": "C", "GIT_LITERAL_PATHSPECS": "1"})
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF"):
            self.env.pop(var, None)

    def run(self, cwd: str | Path, *args: str, stdin: bytes | None = None, timeout: int | None = None,
            env: dict | None = None) -> tuple[int, bytes, bytes]:
        cmd = [self.cfg.git_bin, *self.BASE, "-C", str(cwd), *args]
        e = self.env if env is None else {**self.env, **env}
        try:
            p = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout or self.cfg.git_timeout, env=e)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, b"", b"timeout"
        except OSError as exc:
            return 127, b"", str(exc).encode()

    def text(self, cwd, *args, **kw) -> Optional[str]:
        rc, out, _ = self.run(cwd, *args, **kw)
        return out.decode("utf-8", "replace").strip() if rc == 0 else None


def is_denied(stderr: bytes) -> bool:
    low = stderr.lower()
    return b"operation not permitted" in low or b"permission denied" in low


# ------------------------------------------------------------------------------------- discovery
@dataclasses.dataclass
class Discovery:
    checkouts: list[str] = dataclasses.field(default_factory=list)
    blocked: list[dict] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


def git_root_state(path: str) -> str:
    """'ok' for a checkout, 'dangling' for a .git file whose gitdir is gone, '' for neither."""
    dotgit = os.path.join(path, ".git")
    if os.path.isdir(dotgit):
        return "ok"
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, "r", encoding="utf-8", errors="replace") as fh:
                line = fh.readline().strip()
        except OSError:
            return "dangling"
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            if not os.path.isabs(target):
                target = os.path.join(path, target)
            return "ok" if os.path.isdir(target) else "dangling"
        return "dangling"
    return ""


def discover(cfg: Config) -> Discovery:
    """Find git checkouts.  Lanes can sit at depth 4 (_codex/<slug>/<Repo>), flat clones at 3."""
    d = Discovery()
    seen: set[str] = set()
    roots = [(cfg.lanes, 4), (cfg.apps, 3), (cfg.code, 2)]
    skip_real = {os.path.realpath(p) for p in (cfg.lanes, cfg.install_dir, cfg.cache_dir)}

    def walk(root: Path, max_depth: int) -> None:
        stack: list[tuple[str, int]] = [(str(root), 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                real = os.path.realpath(cur)
            except OSError:
                continue
            if real in seen:
                continue
            if depth > 0 and real in skip_real and root != cfg.lanes:
                continue
            seen.add(real)
            if depth > 0:
                state = git_root_state(cur)
                if state == "ok":
                    d.checkouts.append(cur)
                    continue
                if state == "dangling":
                    d.notes.append(f"dangling-gitdir {cur}")
                    continue
            if depth >= max_depth:
                continue
            try:
                with os.scandir(cur) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except PermissionError as exc:
                d.blocked.append({"path": cur, "errno": exc.errno, "via": "scandir"})
                continue
            except OSError as exc:
                d.notes.append(f"unreadable {cur} ({errno.errorcode.get(exc.errno or 0, exc.errno)})")
                continue
            children = []
            for e in entries:
                if e.name in SKIP_DIR_NAMES:
                    continue
                try:
                    if e.is_dir(follow_symlinks=True):
                        children.append(e.path)
                    elif e.is_symlink() and not os.path.exists(e.path):
                        d.notes.append(f"dangling-symlink {e.path}")
                except PermissionError as exc:
                    d.blocked.append({"path": e.path, "errno": exc.errno, "via": "stat"})
                except OSError:
                    pass
            for child in reversed(children):
                stack.append((child, depth + 1))

    for root, depth in roots:
        if root.is_dir():
            walk(root, depth)
    d.checkouts = sorted(set(d.checkouts))
    return d


# ---------------------------------------------------------------------------------- git inspection
@dataclasses.dataclass
class Worktree:
    path: str
    common_dir: str = ""
    head: str = ""
    branch: str = ""
    changed: list[str] = dataclasses.field(default_factory=list)
    untracked: list[str] = dataclasses.field(default_factory=list)
    error: str = ""
    denied: bool = False

    @property
    def dirty(self) -> bool:
        return bool(self.changed or self.untracked)


def inspect_worktree(cfg: Config, git: Git, path: str) -> Worktree:
    wt = Worktree(path=path)
    rc, out, err = git.run(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc != 0:
        wt.error = (err.decode("utf-8", "replace").strip().splitlines() or ["rev-parse failed"])[0]
        wt.denied = is_denied(err)
        return wt
    wt.common_dir = os.path.realpath(out.decode("utf-8", "replace").strip())
    rc, out, _ = git.run(path, "rev-parse", "-q", "--verify", "HEAD")
    wt.head = out.decode().strip() if rc == 0 else ""
    wt.branch = git.text(path, "symbolic-ref", "-q", "--short", "HEAD") or ""
    if wt.head:
        rc, out, err = git.run(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                               "--ignore-submodules=all", "--name-only", "-z", "HEAD", "--")
        if rc != 0:
            wt.error = "diff failed"
            wt.denied = is_denied(err)
            return wt
        wt.changed = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    rc, out, err = git.run(path, "ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0:
        wt.error = "ls-files failed"
        wt.denied = is_denied(err)
        return wt
    wt.untracked = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p and not p.endswith("/")]
    if not wt.head:
        rc, out, _ = git.run(path, "ls-files", "-z")
        wt.untracked += [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    return wt


# ------------------------------------------------------------------------------------- the stage
@dataclasses.dataclass
class Item:
    rel: str
    src: str
    size: int
    kind: str  # bundle patch untracked file sqlite symlink meta


class Stage:
    def __init__(self, root: Path):
        self.root = root
        self.items: dict[str, Item] = {}
        self.dropped: list[dict] = []
        self.large: list[dict] = []
        self.notes: list[str] = []
        self.blocked: list[dict] = []

    def path(self, rel: str) -> Path:
        return self.root / rel

    def add(self, rel: str, src: str, size: int, kind: str) -> None:
        self.items[rel] = Item(rel, src, size, kind)

    def write(self, rel: str, data: bytes, src: str, kind: str) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.add(rel, src, len(data), kind)

    def drop(self, rel: str, reason: str) -> None:
        it = self.items.pop(rel, None)
        try:
            os.unlink(self.path(rel))
        except OSError:
            pass
        self.dropped.append({"path": it.src if it and it.src else rel, "stage": rel, "reason": reason})

    def total(self) -> int:
        return sum(i.size for i in self.items.values())


def split_diff(data: bytes) -> list[bytes]:
    return [p for p in re.split(rb"(?m)^(?=diff --git )", data) if p.strip()]


def patch_name(chunk: bytes, idx: int) -> str:
    m = re.match(rb'diff --git a/(.+?) b/(.+?)\n', chunk + b"\n")
    path = m.group(2).decode("utf-8", "replace") if m else f"unnamed-{idx}"
    return f"{idx:04d}-{slugify(path.replace('/', '__'), 110)}.patch"


def chunk_path(chunk: bytes) -> str:
    m = re.match(rb'diff --git a/(.+?) b/(.+?)\n', chunk + b"\n")
    return m.group(2).decode("utf-8", "replace") if m else ""


def stage_patches(cfg: Config, git: Git, stage: Stage, cwd: str, diff_args: list[str], names: list[str],
                  rel_dir: str, src_label: str, check_size_in: Optional[str], tracked: bool = True) -> int:
    """Write one patch file per changed path.  Returns the number written.  Names that look secret,
    or that are bigger than the size cap, are never diffed;  a chunk whose content matches a
    pattern is dropped alone."""
    allowed: list[str] = []
    for name in names:
        if is_secret_name(name, tracked):
            stage.dropped.append({"path": f"{src_label}:{name}", "stage": "", "reason": "secret-name"})
            continue
        if check_size_in:
            try:
                size = os.lstat(os.path.join(check_size_in, name)).st_size
            except OSError:
                size = 0
            if size > cfg.max_file_bytes:
                stage.large.append({"path": os.path.join(check_size_in, name), "size": size})
                continue
        allowed.append(name)
    if not allowed:
        return 0
    # git diff takes no --pathspec-from-file, so the paths go on the command line in batches.
    out = b""
    batch: list[str] = []
    size = 0
    batches: list[list[str]] = []
    for name in allowed:
        if batch and (size + len(name) > 100_000 or len(batch) >= 1500):
            batches.append(batch)
            batch, size = [], 0
        batch.append(name)
        size += len(name) + 1
    if batch:
        batches.append(batch)
    for b in batches:
        rc, part, err = git.run(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary",
                                "--ignore-submodules=all", *diff_args, "--", *b)
        if rc != 0:
            stage.notes.append(f"diff-failed {src_label}")
            return 0
        out += part
    written = 0
    for idx, chunk in enumerate(split_diff(out), 1):
        cp = chunk_path(chunk)
        if cp and is_secret_name(cp, tracked):
            stage.dropped.append({"path": f"{src_label}:{cp}", "stage": "", "reason": "secret-name"})
            continue
        hit = scan_bytes(chunk)
        if hit:
            stage.dropped.append({"path": f"{src_label}:{cp or idx}", "stage": "", "reason": f"content:{hit}"})
            continue
        stage.write(f"{rel_dir}/{patch_name(chunk, idx)}", chunk, f"{src_label}:{cp}", "patch")
        written += 1
    return written


def stage_untracked(cfg: Config, stage: Stage, root: str, names: list[str], rel_dir: str) -> int:
    count = 0
    for name in names:
        if is_secret_name(name):
            stage.dropped.append({"path": os.path.join(root, name), "stage": "", "reason": "secret-name"})
            continue
        if is_skipped_file(os.path.basename(name)) or any(p in SKIP_DIR_NAMES for p in name.split("/")[:-1]):
            continue
        src = os.path.join(root, name)
        try:
            st = os.lstat(src)
        except OSError:
            continue
        if count >= cfg.max_untracked_files:
            stage.notes.append(f"untracked-truncated {root} at {cfg.max_untracked_files} files")
            break
        rel = f"{rel_dir}/{name}"
        dst = stage.path(rel)
        if os.path.islink(src):
            os.makedirs(dst.parent, exist_ok=True)
            os.symlink(os.readlink(src), dst)
            stage.add(rel, src, 0, "symlink")
            count += 1
            continue
        if not os.path.isfile(src):
            continue
        if st.st_size > cfg.max_file_bytes:
            stage.large.append({"path": src, "size": st.st_size})
            continue
        try:
            clone_or_copy(src, str(dst))
        except OSError as exc:
            stage.notes.append(f"copy-failed {src} ({errno.errorcode.get(exc.errno or 0, exc.errno)})")
            continue
        stage.add(rel, src, st.st_size, "untracked")
        count += 1
    return count


@dataclasses.dataclass
class GroupResult:
    slug: str
    name: str
    common_dir: str
    worktrees: int
    unpushed_refs: int = 0
    landed_refs: int = 0
    no_remote: bool = False
    stashes: int = 0
    dirty_worktrees: int = 0
    untracked_files: int = 0
    patch_files: int = 0
    bundle: bool = False
    head_bundles: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.unpushed_refs or self.stashes or self.dirty_worktrees or self.head_bundles)


def repo_name(common_dir: str) -> str:
    p = Path(common_dir)
    return p.parent.name if p.name == ".git" else p.name


def stream_scan(git: Git, cwd: str, args: list[str], cap: int) -> tuple[Optional[str], bool]:
    """Scan ``git log -p`` output for secret patterns.  Returns (hit, truncated)."""
    cmd = [git.cfg.git_bin, *Git.BASE, "-C", cwd, "log", "-p", "--no-color", "--no-ext-diff", "--no-textconv",
           "--format=%H", *args]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git.env)
    except OSError:
        return None, False
    read, tail, hit = 0, b"", None
    try:
        while read < cap:
            buf = proc.stdout.read(4 * 1024 * 1024)
            if not buf:
                break
            read += len(buf)
            hit = scan_bytes(tail + buf)
            if hit:
                break
            tail = buf[-8192:]
    finally:
        proc.kill()
        proc.wait()
    return hit, read >= cap and hit is None


def process_group(cfg: Config, git: Git, stage: Stage, common_dir: str, wts: list[Worktree]) -> GroupResult:
    name = repo_name(common_dir)
    slug = f"{slugify(name, 40)}-{short_hash(common_dir)}"
    res = GroupResult(slug=slug, name=name, common_dir=common_dir, worktrees=len(wts))
    base = f"git/{slug}"
    cwd = wts[0].path
    scratch = tempfile.mkdtemp(prefix="fwb-objs-")
    try:
        # Remotes (credentials stripped from URLs) and the no-remote flag.
        rc, out, _ = git.run(cwd, "remote", "-v")
        remotes = sorted({strip_url_credentials(" ".join(line.split()[:2]))
                          for line in out.decode("utf-8", "replace").splitlines() if line.strip()}) if rc == 0 else []
        res.no_remote = not remotes
        # Unpushed refs:  tips that git rev-list finds outside every remote-tracking ref.
        rc, out, _ = git.run(cwd, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(*objectname)",
                             "refs/heads", "refs/tags")
        refs: list[tuple[str, str]] = []
        for line in out.decode("utf-8", "replace").splitlines() if rc == 0 else []:
            parts = line.split("\0")
            if len(parts) >= 2:
                refs.append((parts[0], (parts[2] if len(parts) > 2 and parts[2] else parts[1])))
        unpushed: set[str] = set()
        if refs:
            rc, out, _ = git.run(cwd, "rev-list", "--branches", "--tags", "--not", "--remotes")
            if rc == 0:
                unpushed = set(out.decode().split())
        pending = [(r, s) for r, s in refs if s in unpushed]
        # Branches whose merge into the default branch is a no-op are already on GitHub.
        landed = 0
        if pending and cfg.check_landed and len(pending) <= 400:
            default = git.text(cwd, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
            if not default:
                for cand in ("refs/remotes/origin/main", "refs/remotes/origin/master"):
                    if git.run(cwd, "rev-parse", "-q", "--verify", cand)[0] == 0:
                        default = cand
                        break
            dtree = git.text(cwd, "rev-parse", f"{default}^{{tree}}") if default else None
            if default and dtree:
                objenv = {"GIT_OBJECT_DIRECTORY": os.path.join(scratch, "objects"),
                          "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(common_dir, "objects")}
                os.makedirs(objenv["GIT_OBJECT_DIRECTORY"], exist_ok=True)
                keep = []
                for ref, sha in pending:
                    if ref.startswith("refs/heads/"):
                        rc, out, _ = git.run(cwd, "merge-tree", "--write-tree", "--no-messages", default, ref,
                                             env=objenv, timeout=120)
                        if rc == 0 and out.split()[:1] == [dtree.encode()]:
                            landed += 1
                            continue
                    keep.append((ref, sha))
                pending = keep
        res.landed_refs = landed
        res.unpushed_refs = len(pending)
        group_meta: dict = {"tool": VERSION, "common_dir": common_dir, "name": name, "remotes": remotes,
                            "no_remote": res.no_remote, "unpushed": [], "landed_skipped": landed,
                            "worktrees": [], "stashes": []}
        # One bundle for the repository, shared by every worktree.
        if pending:
            ref_names = [r for r, _ in pending]
            hit, truncated = stream_scan(git, cwd, [*ref_names, "--not", "--remotes"], cfg.bundle_scan_cap)
            if hit:
                stage.dropped.append({"path": f"{common_dir} (unpushed commits)", "stage": "", "reason": f"content:{hit}"})
            else:
                if truncated:
                    stage.notes.append(f"bundle-scan-partial {common_dir}")
                out_path = stage.path(f"{base}/unpushed.bundle")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                rc, _, err = git.run(cwd, "bundle", "create", str(out_path), *ref_names, "--not", "--remotes",
                                     timeout=cfg.bundle_timeout)
                if rc == 0 and out_path.exists():
                    stage.add(f"{base}/unpushed.bundle", common_dir, out_path.stat().st_size, "bundle")
                    res.bundle = True
                    group_meta["unpushed"] = [{"ref": r, "sha": s} for r, s in pending]
                else:
                    stage.notes.append(f"bundle-failed {common_dir}: {err.decode('utf-8', 'replace').strip()[:120]}")
        # Every stash, as per-file patches (no refs are created in the repository).
        rc, out, _ = git.run(cwd, "stash", "list", "--format=%H%x00%gs")
        empty_tree = git.text(cwd, "hash-object", "-t", "tree", "/dev/null") or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        for n, line in enumerate(out.decode("utf-8", "replace").splitlines() if rc == 0 else []):
            sha, _, subject = line.partition("\0")
            if not sha:
                continue
            base_sha = git.text(cwd, "rev-parse", f"{sha}^1") or ""
            sdir = f"{base}/stashes/{n:02d}"
            tracked_names = git.run(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                                    base_sha, sha, "--")[1].decode("utf-8", "surrogateescape").split("\0")
            wrote = stage_patches(cfg, git, stage, cwd, [base_sha, sha], [p for p in tracked_names if p],
                                  f"{sdir}/tracked", f"{common_dir}#stash{n}", None)
            untracked_commit = git.text(cwd, "rev-parse", "-q", "--verify", f"{sha}^3")
            if untracked_commit:
                un_names = git.run(cwd, "ls-tree", "-r", "--name-only", "-z", untracked_commit)[1]
                wrote += stage_patches(cfg, git, stage, cwd, [empty_tree, untracked_commit],
                                       [p for p in un_names.decode("utf-8", "surrogateescape").split("\0") if p],
                                       f"{sdir}/untracked", f"{common_dir}#stash{n}", None, tracked=False)
            meta = {"index": n, "sha": sha, "base": base_sha, "subject": subject, "patches": wrote}
            stage.write(f"{sdir}/stash.json", json.dumps(meta, indent=2).encode(), "", "meta")
            group_meta["stashes"].append(meta)
            res.stashes += 1
        # Per worktree:  uncommitted work and a detached HEAD no remote has.
        for wt in wts:
            wslug = f"{slugify(os.path.basename(wt.path), 40)}-{short_hash(os.path.realpath(wt.path))}"
            wdir = f"{base}/worktrees/{wslug}"
            wmeta = {"path": wt.path, "head": wt.head, "branch": wt.branch, "changed": len(wt.changed),
                     "untracked": len(wt.untracked)}
            wrote = 0
            if wt.changed:
                wrote = stage_patches(cfg, git, stage, wt.path, ["HEAD"], wt.changed, f"{wdir}/patches", wt.path, wt.path)
            un = stage_untracked(cfg, stage, wt.path, wt.untracked, f"{wdir}/untracked") if wt.untracked else 0
            if wt.head and not wt.branch:
                rc, out, _ = git.run(wt.path, "rev-list", "-1", "HEAD", "--not", "--remotes")
                if rc == 0 and out.strip():
                    hp = stage.path(f"{wdir}/head.bundle")
                    hp.parent.mkdir(parents=True, exist_ok=True)
                    rc, _, _ = git.run(wt.path, "bundle", "create", str(hp), "HEAD", "--not", "--remotes",
                                       timeout=cfg.bundle_timeout)
                    if rc == 0 and hp.exists():
                        stage.add(f"{wdir}/head.bundle", wt.path, hp.stat().st_size, "bundle")
                        wmeta["detached_head_bundle"] = True
                        res.head_bundles += 1
            if wrote or un or wmeta.get("detached_head_bundle"):
                wmeta["patches_written"] = wrote
                wmeta["untracked_written"] = un
                stage.write(f"{wdir}/worktree.json", json.dumps(wmeta, indent=2).encode(), "", "meta")
                group_meta["worktrees"].append(wmeta)
                if wrote or un:
                    res.dirty_worktrees += 1
                res.patch_files += wrote
                res.untracked_files += un
        if any(k.startswith(base + "/") for k in list(stage.items)):
            stage.write(f"{base}/group.json", json.dumps(group_meta, indent=2).encode(), "", "meta")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return res


# ---------------------------------------------------------------------------- non-git files (b)
def snapshot_sqlite(src: str, dst: str) -> bool:
    """Copy a SQLite database with the online backup API (read-only open), so a live WAL-mode
    file is never torn.  mtime follows the newest of the file and its -wal."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
        try:
            out = sqlite3.connect(dst)
            try:
                con.backup(out)
            finally:
                out.close()
        finally:
            con.close()
    except sqlite3.Error:
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False
    mt = os.stat(src).st_mtime_ns
    try:
        mt = max(mt, os.stat(src + "-wal").st_mtime_ns)
    except OSError:
        pass
    os.utime(dst, ns=(mt, mt))
    return True


def collect_files(cfg: Config, stage: Stage) -> int:
    """Stage non-git files under ~/apps (outside any checkout) and the loose files at the top of ~/Code."""
    count = 0
    skip_real = {os.path.realpath(p) for p in (cfg.lanes, cfg.install_dir, cfg.cache_dir)}

    def stage_one(src: str, name: str, rel_to_home: str) -> None:
        nonlocal count
        if is_skipped_file(name):
            return
        if is_secret_name(rel_to_home):
            stage.dropped.append({"path": src, "stage": "", "reason": "secret-name"})
            return
        try:
            st = os.lstat(src)
        except OSError:
            return
        rel = f"files/{rel_to_home}"
        dst = str(stage.path(rel))
        import stat as _stat
        if _stat.S_ISLNK(st.st_mode):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.symlink(os.readlink(src), dst)
            except OSError:
                return
            stage.add(rel, src, 0, "symlink")
            count += 1
            return
        if not _stat.S_ISREG(st.st_mode):
            return
        if st.st_size > cfg.max_file_bytes:
            stage.large.append({"path": src, "size": st.st_size})
            return
        try:
            if name.lower().endswith(SQLITE_SUFFIXES) and snapshot_sqlite(src, dst):
                stage.add(rel, src, os.stat(dst).st_size, "sqlite")
            else:
                clone_or_copy(src, dst)
                stage.add(rel, src, st.st_size, "file")
            count += 1
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                stage.blocked.append({"path": src, "errno": exc.errno, "via": "copy"})
            else:
                stage.notes.append(f"copy-failed {src} ({errno.errorcode.get(exc.errno or 0, exc.errno)})")

    def walk(top: Path) -> None:
        stack = [str(top)]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except PermissionError as exc:
                stage.blocked.append({"path": cur, "errno": exc.errno, "via": "scandir"})
                continue
            except OSError:
                continue
            for e in entries:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if e.name in SKIP_DIR_NAMES:
                        continue
                    if os.path.realpath(e.path) in skip_real:
                        continue
                    if git_root_state(e.path):
                        continue
                    stack.append(e.path)
                else:
                    stage_one(e.path, e.name, os.path.relpath(e.path, cfg.home).replace(os.sep, "/"))

    if cfg.apps.is_dir():
        walk(cfg.apps)
    if cfg.code.is_dir():
        try:
            for e in sorted(os.scandir(cfg.code), key=lambda e: e.name):
                if e.is_file(follow_symlinks=False):
                    stage_one(e.path, e.name, os.path.relpath(e.path, cfg.home).replace(os.sep, "/"))
        except PermissionError as exc:
            stage.blocked.append({"path": str(cfg.code), "errno": exc.errno, "via": "scandir"})
    return count


# --------------------------------------------------------------------------------- secret gate
def load_scan_cache(cfg: Config) -> dict:
    try:
        data = json.loads((cfg.state_dir / "scan-cache.json").read_text())
        return data.get("files", {}) if data.get("v") == 1 else {}
    except (OSError, ValueError, AttributeError):
        return {}


def save_scan_cache(cfg: Config, files: dict) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = cfg.state_dir / f"scan-cache.json.tmp{os.getpid()}"
        tmp.write_text(json.dumps({"v": 1, "files": files}))
        os.replace(tmp, cfg.state_dir / "scan-cache.json")
    except OSError:
        pass


def gitleaks_batch(cfg: Config, stage: Stage, rels: list[str]) -> tuple[Optional[dict], str]:
    """Run gitleaks over a batch of staged files (hard-linked into a scratch folder).  Returns
    ({rel: reason} for flagged files, '') or (None, error)."""
    batch = Path(tempfile.mkdtemp(prefix="gl-", dir=str(cfg.cache_dir)))
    report = batch.with_suffix(".json")
    try:
        for rel in rels:
            dst = batch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(stage.path(rel), dst)
            except OSError:
                shutil.copy2(stage.path(rel), dst)
        # Throttled:  gitleaks is parallel and this Mac runs hot, so two threads at low priority.
        p = subprocess.run([cfg.gitleaks_bin, "dir", str(batch), "--no-banner", "--redact", "-f", "json",
                            "-r", str(report), "--exit-code", "0", "--log-level", "error",
                            "--max-target-megabytes", "50"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=cfg.gitleaks_budget_s,
                           cwd=str(cfg.cache_dir), env={**os.environ, "GOMAXPROCS": "2"},
                           preexec_fn=lambda: os.nice(10))
        if p.returncode != 0 or not report.exists():
            return None, f"rc={p.returncode}"
        findings = json.loads(report.read_text() or "[]")
        flagged: dict[str, str] = {}
        for f in findings if isinstance(findings, list) else []:
            if f.get("RuleID") in cfg.gitleaks_ignore_rules:
                continue
            try:
                rel = os.path.relpath(f.get("File") or "", batch)
            except ValueError:
                continue
            flagged.setdefault(rel, f"gitleaks:{f.get('RuleID', '?')}")
        return flagged, ""
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return None, type(exc).__name__
    finally:
        shutil.rmtree(batch, ignore_errors=True)
        try:
            report.unlink()
        except OSError:
            pass


def run_gate(cfg: Config, stage: Stage, progress: Optional[Callable[[str], None]] = None,
             use_gitleaks: bool = True) -> dict:
    """Content scan over the staged set.  Bundles were scanned before they were made.

    Every staged file gets the regex scan;  gitleaks runs too when installed, in batches within a
    wall-clock budget.  A file that passed is remembered by (path, size, mtime) in
    ``scan-cache.json``, so a steady-state run scans only what changed.  Dropped paths are recorded
    on the stage;  content is never echoed."""
    info = {"regex": True, "gitleaks": False, "gitleaks_error": "", "scanned": 0, "cached": 0,
            "gitleaks_files": 0, "gitleaks_pending": 0}
    cache = load_scan_cache(cfg)
    new_cache: dict = {}
    regex_todo: list[tuple[str, list]] = []
    for rel, item in list(stage.items.items()):
        if item.kind in ("bundle", "symlink", "meta"):
            continue
        try:
            st = os.stat(stage.path(rel))
        except OSError:
            continue
        key = [st.st_size, st.st_mtime_ns]
        ent = cache.get(rel)
        if ent and ent[:2] == key:
            new_cache[rel] = ent
            info["cached"] += 1
        else:
            regex_todo.append((rel, key))
    for rel, key in regex_todo:
        hit = scan_file(stage.path(rel), cfg.scan_cap_bytes)
        info["scanned"] += 1
        if hit:
            stage.drop(rel, f"content:{hit}")
        else:
            new_cache[rel] = [key[0], key[1], False]
    if progress:
        progress(f"regex scan done:  {info['scanned']} scanned, {info['cached']} cached")
    gl = shutil.which(cfg.gitleaks_bin) if (use_gitleaks and cfg.gitleaks_bin) else None
    if gl:
        info["gitleaks"] = True
        pending = sorted(rel for rel, ent in new_cache.items() if not ent[2] and rel in stage.items)
        deadline = time.time() + cfg.gitleaks_budget_s
        i = 0
        while i < len(pending):
            if time.time() >= deadline:
                break
            batch, size = [], 0
            while i < len(pending) and len(batch) < 3000 and size < 256 * 1024 * 1024:
                batch.append(pending[i])
                size += stage.items[pending[i]].size
                i += 1
            flagged, err = gitleaks_batch(cfg, stage, batch)
            if flagged is None:
                info["gitleaks_error"] = err
                i -= len(batch)
                break
            for rel in batch:
                if rel in flagged:
                    stage.drop(rel, flagged[rel])
                    new_cache.pop(rel, None)
                elif rel in new_cache:
                    new_cache[rel][2] = True
            info["gitleaks_files"] += len(batch)
            if progress:
                progress(f"gitleaks {info['gitleaks_files']}/{len(pending)} files")
        info["gitleaks_pending"] = len(pending) - i
    save_scan_cache(cfg, {rel: ent for rel, ent in new_cache.items() if rel in stage.items})
    return info


# --------------------------------------------------------------------------------- safety gates
def load1(cfg: Config) -> float:
    if cfg.load_fn:
        return cfg.load_fn()
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def swap_ratio(cfg: Config) -> Optional[float]:
    if cfg.swap_fn:
        return cfg.swap_fn()
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", out)
    if not m or float(m.group(1)) <= 0:
        return None
    return float(m.group(2)) / float(m.group(1))


def pressure_reason(cfg: Config) -> Optional[str]:
    la = load1(cfg)
    if la > cfg.load_limit:
        return f"load1={la:.1f}>{cfg.load_limit:g}"
    sw = swap_ratio(cfg)
    if sw is not None and sw >= cfg.swap_limit:
        return f"swap={sw * 100:.0f}%>={cfg.swap_limit * 100:.0f}%"
    return None


def read_state(cfg: Config) -> dict:
    try:
        return json.loads((cfg.state_dir / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def write_state(cfg: Config, state: dict) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_dir / f"state.json.tmp{os.getpid()}"
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, cfg.state_dir / "state.json")


def log_line(cfg: Config, **fields) -> str:
    parts = [stamp(cfg.now())]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    line = " ".join(parts)
    try:
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return line


def detail_log(cfg: Config, lines: list[str]) -> None:
    if not lines:
        return
    try:
        cfg.detail_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.detail_log_path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp(cfg.now())} ---\n")
            fh.writelines(l + "\n" for l in lines)
    except OSError:
        pass


# ------------------------------------------------------------------------------ removable volumes
def python_binary() -> str:
    return os.path.realpath(sys.executable)


def first_checkout_under(root: Path) -> str:
    try:
        for a in sorted(os.listdir(root)):
            for b in sorted(os.listdir(root / a)) if (root / a).is_dir() else []:
                if (root / a / b / ".git").exists():
                    return str(root / a / b)
    except OSError:
        pass
    return ""


def probe_external(cfg: Config) -> dict:
    """Can this process read the volumes the lane symlinks point at?  Reports per volume path."""
    targets: dict[str, str] = {}
    try:
        for e in os.scandir(cfg.lanes):
            if e.is_symlink():
                tgt = os.readlink(e.path)
                if tgt.startswith("/Volumes/"):
                    targets[tgt] = e.name
    except OSError:
        pass
    result = {"targets": [], "denied": 0, "missing": 0, "ok": 0, "binary": python_binary(), "git": cfg.git_bin}
    for tgt in sorted(targets):
        entry = {"path": tgt, "python": "", "git": ""}
        try:
            os.listdir(tgt)
            entry["python"] = "ok"
        except PermissionError:
            entry["python"] = "denied"
        except OSError:
            entry["python"] = "missing"
        if entry["python"] == "ok":
            first = first_checkout_under(Path(tgt))
            if first:
                rc, _, err = Git(cfg).run(first, "rev-parse", "--git-dir")
                entry["git"] = "ok" if rc == 0 else ("denied" if is_denied(err) else "error")
        result["targets"].append(entry)
        if "denied" in (entry["python"], entry["git"]):
            result["denied"] += 1
        elif entry["python"] == "missing":
            result["missing"] += 1
        else:
            result["ok"] += 1
    return result


def removable_hint(cfg: Config) -> str:
    return (f"grant Removable Volumes (or Full Disk Access) to {python_binary()} in System Settings > Privacy & "
            f"Security, then rerun 'fleet_work_backup.py check-access';  git children inherit the grant, or set "
            f"FLEET_WORK_BACKUP_GIT to a git binary that has it")


# ------------------------------------------------------------------------------------------ restic
CRED_NAMES = {"bucket": "B2_FLEET_BUCKET_NAME", "key_id": "B2_FLEET_WORK_KEY_ID",
              "app_key": "B2_FLEET_WORK_APPLICATION_KEY", "password": "RESTIC_FLEET_WORK_PASSWORD"}
MASTER_NAMES = {"key_id": "BACKBLAZE_MASTER_KEY_ID", "app_key": "BACKBLAZE_MASTER_APPLICATION_KEY"}


def read_handoff(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", k.strip()):
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def restic_env(cfg: Config) -> tuple[Optional[dict], list[str]]:
    """Return (env for the restic subprocess, missing key NAMES).  Values never leave this process
    except in that env."""
    src = dict(read_handoff(cfg.handoff))
    for name in list(CRED_NAMES.values()) + list(MASTER_NAMES.values()):
        if os.environ.get(name):
            src[name] = os.environ[name]
    key_id = src.get(CRED_NAMES["key_id"], "")
    app_key = src.get(CRED_NAMES["app_key"], "")
    if (not key_id or not app_key) and os.environ.get("FLEET_WORK_BACKUP_ALLOW_MASTER") == "1":
        key_id = key_id or src.get(MASTER_NAMES["key_id"], "")
        app_key = app_key or src.get(MASTER_NAMES["app_key"], "")
    bucket = src.get(CRED_NAMES["bucket"], "")
    password = src.get(CRED_NAMES["password"], "")
    missing = [CRED_NAMES[k] for k, v in (("bucket", bucket), ("key_id", key_id), ("app_key", app_key),
                                          ("password", password)) if not v]
    if missing:
        return None, missing
    region = re.sub(r"^s3\.|\.backblazeb2\.com$", "", cfg.s3_endpoint)
    env = dict(os.environ)
    env.update({
        "RESTIC_REPOSITORY": f"s3:https://{cfg.s3_endpoint}/{bucket}/fleet-work-backup",
        "RESTIC_PASSWORD": password,
        "AWS_ACCESS_KEY_ID": key_id,
        "AWS_SECRET_ACCESS_KEY": app_key,
        "AWS_DEFAULT_REGION": region,
    })
    for var in ("RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY_FILE"):
        env.pop(var, None)
    return env, []


def restic_path(cfg: Config) -> Optional[str]:
    return shutil.which(cfg.restic_bin)


def run_restic(cfg: Config, env: dict, *args: str, timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run([cfg.restic_bin, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)


def restic_backup(cfg: Config, env: dict, stage_root: Path) -> dict:
    p = run_restic(cfg, env, "backup", "--json", "--tag", TAG, "--host", cfg.host, "--ignore-inode",
                   "--ignore-ctime", str(stage_root))
    out = {"rc": p.returncode, "snapshot": "", "added": 0, "files": 0}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary":
            out["snapshot"] = str(msg.get("snapshot_id", ""))[:8]
            out["added"] = int(msg.get("data_added", 0) or 0)
            out["files"] = int(msg.get("total_files_processed", 0) or 0)
    if p.returncode != 0:
        out["error"] = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["restic backup failed"])[-1][:160]
    return out


def restic_maintenance(cfg: Config, env: dict, state: dict) -> dict:
    """forget on every run;  prune at most daily, check at most weekly (B2 transactions cost money)."""
    now = cfg.now()
    res = {"forget": "ok", "prune": "-", "check": "-"}
    args = ["forget", "--tag", TAG, "--group-by", "host,tags", *[a for kv in RETENTION for a in kv]]
    prune = now - float(state.get("last_prune", 0)) >= PRUNE_EVERY
    if prune:
        args.append("--prune")
    p = run_restic(cfg, env, *args)
    if p.returncode != 0:
        res["forget"] = "fail"
        res["error"] = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["forget failed"])[-1][:160]
        return res
    if prune:
        state["last_prune"] = now
        res["prune"] = "ok"
    if now - float(state.get("last_check", 0)) >= CHECK_EVERY:
        c = run_restic(cfg, env, "check")
        res["check"] = "ok" if c.returncode == 0 else "fail"
        if c.returncode == 0:
            state["last_check"] = now
    return res


# ------------------------------------------------------------------------------------------ report
def build_report(cfg: Config, disc: Discovery, wts: list[Worktree], groups: list[GroupResult], stage: Stage,
                 file_count: int, gate: dict, started: float) -> dict:
    by_kind: dict[str, dict] = {}
    for it in stage.items.values():
        k = by_kind.setdefault(it.kind, {"files": 0, "bytes": 0})
        k["files"] += 1
        k["bytes"] += it.size
    top_files = sorted(stage.items.values(), key=lambda i: i.size, reverse=True)[:15]
    units: dict[str, int] = {}
    for it in stage.items.values():
        parts = it.rel.split("/")
        if parts[0] == "git":
            key = "/".join(parts[:2])
        else:
            key = "/".join(parts[:5]) if len(parts) > 5 else "/".join(parts[:-1])
        units[key] = units.get(key, 0) + it.size
    return {
        "tool_version": VERSION,
        "checkouts": len(disc.checkouts),
        "checkouts_unreadable": sum(1 for w in wts if w.error),
        "repo_groups": len(groups),
        "groups_with_unpushed": sum(1 for g in groups if g.unpushed_refs),
        "groups_with_stashes": sum(1 for g in groups if g.stashes),
        "worktrees_dirty": sum(g.dirty_worktrees for g in groups),
        "checkouts_with_work": len({w.path for w in wts if w.dirty}) ,
        "groups_with_work": sum(1 for g in groups if g.has_work),
        "groups_no_remote": [g.name for g in groups if g.no_remote],
        "unpushed_refs": sum(g.unpushed_refs for g in groups),
        "landed_refs_skipped": sum(g.landed_refs for g in groups),
        "stashes": sum(g.stashes for g in groups),
        "patch_files": sum(g.patch_files for g in groups),
        "untracked_files": sum(g.untracked_files for g in groups),
        "non_git_files": file_count,
        "staged_files": len(stage.items),
        "staged_bytes": stage.total(),
        "by_kind": by_kind,
        "top_files": [{"rel": i.rel, "size": i.size, "src": i.src} for i in top_files],
        "top_units": sorted(({"unit": k, "size": v} for k, v in units.items()), key=lambda d: d["size"], reverse=True)[:15],
        "large_skipped": stage.large,
        "dropped": stage.dropped,
        "blocked": disc.blocked + stage.blocked,
        "notes": disc.notes + stage.notes,
        "gate": gate,
        "elapsed_s": round(time.time() - started, 1),
    }


def print_report(rep: dict) -> None:
    say("dry run report")
    print(f"  checkouts discovered     {rep['checkouts']}  (unreadable {rep['checkouts_unreadable']})")
    print(f"  repo groups (git dirs)   {rep['repo_groups']}  with work {rep['groups_with_work']}")
    print(f"  groups with unpushed     {rep['groups_with_unpushed']}  ({rep['unpushed_refs']} refs;  "
          f"{rep['landed_refs_skipped']} landed refs skipped)")
    print(f"  groups with stashes      {rep['groups_with_stashes']}  ({rep['stashes']} stashes)")
    print(f"  dirty worktrees          {rep['worktrees_dirty']}  ({rep['patch_files']} patches, {rep['untracked_files']} untracked files)")
    print(f"  non-git files            {rep['non_git_files']}")
    print(f"  staged                   {rep['staged_files']} files, {fmt_bytes(rep['staged_bytes'])}")
    for kind, v in sorted(rep["by_kind"].items()):
        print(f"    {kind:<10} {v['files']:>7} files {fmt_bytes(v['bytes']):>10}")
    print("  top units:")
    for u in rep["top_units"][:10]:
        print(f"    {fmt_bytes(u['size']):>10}  {u['unit']}")
    print("  top files:")
    for f in rep["top_files"][:10]:
        print(f"    {fmt_bytes(f['size']):>10}  {f['rel']}")
    print(f"  large files skipped      {len(rep['large_skipped'])}")
    for f in rep["large_skipped"][:10]:
        print(f"    {fmt_bytes(f['size']):>10}  {f['path']}")
    print(f"  dropped by the gate      {len(rep['dropped'])} (paths only)")
    for d in rep["dropped"][:20]:
        print(f"    {d['reason']:<28} {d['path']}")
    if rep["groups_no_remote"]:
        print(f"  repos with no remote (full history bundled): {', '.join(rep['groups_no_remote'][:10])}")
    print(f"  blocked paths            {len(rep['blocked'])}")
    g = rep["gate"]
    print(f"  gate: regex scanned {g.get('scanned', 0)} files ({g.get('cached', 0)} cached);  gitleaks "
          f"{'on' if g.get('gitleaks') else 'off'}, {g.get('gitleaks_files', 0)} files, {g.get('gitleaks_pending', 0)} pending "
          f"{g.get('gitleaks_error', '')}")
    print(f"  elapsed                  {rep['elapsed_s']}s")


# ------------------------------------------------------------------------------------------ the run
class Timeout(Exception):
    pass


def disk_ok(cfg: Config) -> bool:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(cfg.cache_dir).free >= cfg.min_free_bytes


def build_stage(cfg: Config, stage_root: Path, progress: Optional[Callable[[str], None]] = None,
                use_gitleaks: bool = True):
    started = time.time()

    def phase(msg: str) -> None:
        if progress:
            progress(f"{time.time() - started:6.0f}s  {msg}")

    if stage_root.exists():
        shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    stage = Stage(stage_root)
    git = Git(cfg)
    disc = discover(cfg)
    phase(f"discovered {len(disc.checkouts)} checkouts ({len(disc.blocked)} blocked)")
    with cf.ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        wts = list(pool.map(lambda p: inspect_worktree(cfg, git, p), disc.checkouts))
    phase(f"inspected {len(wts)} worktrees, {sum(1 for w in wts if w.dirty)} dirty")
    for w in wts:
        if w.error:
            if w.denied:
                disc.blocked.append({"path": w.path, "errno": errno.EPERM, "via": "git"})
            else:
                disc.notes.append(f"git-error {w.path}: {w.error}")
    by_common: dict[str, list[Worktree]] = {}
    for w in wts:
        if not w.error and w.common_dir:
            by_common.setdefault(w.common_dir, []).append(w)
    # A group with no remote-unpushed work, no stash and no dirty worktree still needs its refs read,
    # so every group is processed;  one with nothing leaves nothing behind.
    with cf.ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        groups = list(pool.map(lambda kv: process_group(cfg, git, stage, kv[0], sorted(kv[1], key=lambda w: w.path)),
                               sorted(by_common.items())))
    phase(f"processed {len(groups)} repository groups, {sum(1 for g in groups if g.has_work)} with work, "
          f"{fmt_bytes(stage.total())} staged so far")
    file_count = collect_files(cfg, stage)
    phase(f"staged {file_count} non-git files, {fmt_bytes(stage.total())} total")
    gate = run_gate(cfg, stage, progress, use_gitleaks)
    phase(f"gate done, {len(stage.dropped)} dropped, {fmt_bytes(stage.total())} left")
    return stage, disc, wts, groups, file_count, gate, started


def cmd_run(cfg: Config, args) -> int:
    held: list[int] = []
    try:
        return _run(cfg, args, held)
    finally:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass


def _run(cfg: Config, args, held: list[int]) -> int:
    t0 = time.time()
    dry = args.dry_run
    if not dry:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(cfg.state_dir / "lock"), os.O_CREAT | os.O_RDWR, 0o600)
        held.append(lock_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_line(cfg, status="skip", reason="lock-held")
            return 0
        state = read_state(cfg)
        why = pressure_reason(cfg) if not args.ignore_pressure else None
        if why:
            state["consecutive_skips"] = int(state.get("consecutive_skips", 0)) + 1
            write_state(cfg, state)
            warn = "no-backup-for-about-24h" if state["consecutive_skips"] >= SKIP_WARN_AFTER else ""
            log_line(cfg, status="skip", reason=why, consecutive=state["consecutive_skips"], warn=warn,
                     last_success=state.get("last_success_text", "never"))
            return 0
        if not restic_path(cfg):
            log_line(cfg, status="error", reason="restic-missing", hint="brew install restic")
            return 2
        env, missing = restic_env(cfg)
        if env is None:
            log_line(cfg, status="error", reason="missing-credentials", names=",".join(missing))
            return 2
        if not disk_ok(cfg):
            log_line(cfg, status="skip", reason="low-disk", need=fmt_bytes(cfg.min_free_bytes))
            return 0
        if cfg.max_runtime:
            def _alarm(*_):
                raise Timeout()
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(cfg.max_runtime)
    stage_root = cfg.dry_stage if dry else cfg.stage
    try:
        stage, disc, wts, groups, file_count, gate, started = build_stage(
            cfg, stage_root, progress=(lambda m: say(m)) if dry else None,
            use_gitleaks=not getattr(args, "skip_gitleaks", False))
    except Timeout:
        log_line(cfg, status="error", reason="timeout", limit_s=cfg.max_runtime)
        return 3
    rep = build_report(cfg, disc, wts, groups, stage, file_count, gate, t0)
    manifest = {k: rep[k] for k in ("tool_version", "checkouts", "repo_groups", "groups_with_work", "unpushed_refs",
                                    "stashes", "patch_files", "untracked_files", "non_git_files", "staged_files",
                                    "staged_bytes", "by_kind")}
    manifest["created"] = stamp(cfg.now())
    manifest["dropped_count"] = len(stage.dropped)
    (stage_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    (stage_root / "README-RESTORE.txt").write_text(
        "Restore runbook: scripts/fleet-work-backup/RESTORE.md in AI-Fleet-Coordinator.\n"
        "files/ mirrors absolute paths (copy files/Users/jay back to /Users/jay).  git/<repo>/ holds\n"
        "unpushed.bundle, stashes/<n>/ and worktrees/<name>/ (patches/, untracked/, head.bundle).\n")
    detail = [f"dropped {d['reason']} {d['path']}" for d in stage.dropped]
    detail += [f"large {f['size']} {f['path']}" for f in stage.large]
    detail += [f"blocked errno={b.get('errno')} {b['via']} {b['path']}" for b in rep["blocked"]]
    detail += [f"note {n}" for n in rep["notes"]]
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2))
    if dry:
        print_report(rep)
        if not args.keep_stage:
            shutil.rmtree(stage_root, ignore_errors=True)
        else:
            say(f"stage kept at {stage_root}")
        return 0
    blocked = len(rep["blocked"])
    common = dict(checkouts=rep["checkouts"], groups=rep["repo_groups"], work=rep["groups_with_work"],
                  unpushed=rep["unpushed_refs"], stashes=rep["stashes"], dirty=rep["worktrees_dirty"],
                  files=rep["staged_files"], staged=fmt_bytes(rep["staged_bytes"]), dropped=len(stage.dropped),
                  large=len(stage.large), blocked=blocked)
    warn, grant = "", ""
    if blocked:
        if any(b.get("errno") in (errno.EPERM, errno.EACCES) for b in rep["blocked"]):
            warn, grant = "removable-volumes-denied", python_binary()
            detail.append("hint " + removable_hint(cfg))
        else:
            warn = "paths-unreadable"
    detail_log(cfg, detail)
    try:
        res = restic_backup(cfg, env, stage_root)
    except (OSError, subprocess.TimeoutExpired, Timeout) as exc:
        log_line(cfg, status="error", reason=f"restic-{type(exc).__name__}", **common)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 4
    finally:
        if cfg.max_runtime:
            signal.alarm(0)
    state = read_state(cfg)
    if res["rc"] != 0:
        log_line(cfg, status="error", reason="restic-backup-failed", detail=res.get("error", "")[:120], **common)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 4
    maint = {"forget": "-", "prune": "-", "check": "-"}
    try:
        maint = restic_maintenance(cfg, env, state)
    except (OSError, subprocess.TimeoutExpired):
        maint["forget"] = "fail"
    state["consecutive_skips"] = 0
    state["last_success"] = cfg.now()
    state["last_success_text"] = stamp(cfg.now())
    write_state(cfg, state)
    status = "partial" if blocked else "ok"
    log_line(cfg, status=status, snapshot=res["snapshot"] or "?", added=fmt_bytes(res["added"]),
             forget=maint["forget"], prune=maint["prune"], check=maint["check"], elapsed=f"{time.time() - t0:.0f}s",
             warn=warn, grant=grant, **common)
    shutil.rmtree(stage_root, ignore_errors=True)
    return 0


def cmd_init(cfg: Config, args) -> int:
    if not restic_path(cfg):
        say("restic is not installed (brew install restic)")
        return 2
    env, missing = restic_env(cfg)
    if env is None:
        say(f"missing credentials in {cfg.handoff} or the environment: {', '.join(missing)}")
        return 2
    p = run_restic(cfg, env, "init", timeout=300)
    say("restic init " + ("ok" if p.returncode == 0 else "failed: " + (p.stderr.decode().strip().splitlines() or [""])[-1][:160]))
    return p.returncode


def cmd_snapshots(cfg: Config, args) -> int:
    env, missing = restic_env(cfg)
    if env is None or not restic_path(cfg):
        say("restic or credentials missing: " + ", ".join(missing or ["restic"]))
        return 2
    p = run_restic(cfg, env, "snapshots", "--tag", TAG, "--compact", timeout=300)
    sys.stdout.write(p.stdout.decode("utf-8", "replace"))
    return p.returncode


def cmd_restic(cfg: Config, args) -> int:
    """Run restic with this job's repository and credentials (for restores and checks)."""
    env, missing = restic_env(cfg)
    if env is None or not restic_path(cfg):
        say("restic or credentials missing: " + ", ".join(missing or ["restic"]))
        return 2
    rest = [a for a in args.restic_args if a != "--"]
    return subprocess.call([cfg.restic_bin, *rest], env=env)


def cmd_check_access(cfg: Config, args) -> int:
    res = probe_external(cfg)
    say(f"interpreter: {res['binary']}")
    say(f"git: {res['git']}")
    if not res["targets"]:
        say("no lane symlinks point at /Volumes: nothing to check")
        return 0
    for t in res["targets"]:
        say(f"{t['path']}: python={t['python']} git={t['git'] or '-'}")
    if res["denied"]:
        say("DENIED: " + removable_hint(cfg))
        return 1
    if res["missing"]:
        say("a volume is not mounted: its lanes will be skipped until it is")
        return 1
    say("ok")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Back up the fleet's non-GitHub work to restic on B2.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="one backup (default)")
    run.add_argument("--dry-run", action="store_true", help="stage and report, upload nothing")
    run.add_argument("--json", help="write the report as JSON to this path")
    run.add_argument("--keep-stage", action="store_true", help="dry run: leave the staged tree in place")
    run.add_argument("--ignore-pressure", action="store_true", help="run even when load or swap is high")
    run.add_argument("--skip-gitleaks", action="store_true", help="regex scan only (a faster dry run)")
    sub.add_parser("init", help="restic init (never done by a scheduled run)")
    sub.add_parser("check-access", help="report whether /Volumes lanes are readable here")
    sub.add_parser("snapshots", help="list this job's snapshots")
    rs = sub.add_parser("restic", help="run restic against this job's repository (restores, checks)")
    rs.add_argument("restic_args", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cfg = config_from_env()
    if args.cmd in (None, "run"):
        if args.cmd is None:
            args = ap.parse_args(["run"])
        return cmd_run(cfg, args)
    return {"init": cmd_init, "check-access": cmd_check_access, "snapshots": cmd_snapshots,
            "restic": cmd_restic}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
