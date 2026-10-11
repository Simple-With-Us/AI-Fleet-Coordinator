#!/usr/bin/env python3
"""fleet-work-backup:  back up the fleet's work that GitHub does not hold.

Two things are staged into one fixed directory and sent to an encrypted restic repository on
Backblaze B2 (prefix ``fleet-work-backup/`` in the bucket named by ``B2_FLEET_BUCKET_NAME``):

  (a) Git work.  For every checkout the fleet uses (lanes under ~/apps/lanes, flat ~/apps/<x>
      lanes, other clones under ~/apps, the human trees under ~/Code, and any linked worktree
      ``git worktree list`` names):  the commits no network remote has (``git bundle`` files, one per
      group of overlapping refs, each bounded by the remote's default branch so its prerequisites
      stay fetchable), every stash (as per-file patches), and per worktree the uncommitted work (one
      patch per tracked text file against HEAD, the raw new content of a tracked binary file, and the
      untracked files git does not ignore).  A checkout with nothing unpushed and nothing dirty
      adds nothing.
  (b) Non-git files under ~/apps that sit outside any checkout (protocol docs, effort logs,
      helper scripts, the board database, runtime configs).

Never staged:  secrets.  A name filter runs before anything is copied, a content scan runs over
the staged set (always the regex scan, which also reads binary files;  gitleaks too when it is
installed), and a match drops the file and logs its path only.  The gate fails closed:  with
gitleaks installed, a text file it has not cleared (budget spent, gitleaks failed) is left out of
this run and retried next run.  Infisical is the source of truth for secrets.

All git access is read-only (GIT_OPTIONAL_LOCKS=0, no fetch, no gc, no ref writes).

  fleet_work_backup.py run                  # one backup (what the LaunchAgent does)
  fleet_work_backup.py run --dry-run        # stage, list sizes, upload nothing, no lock, no gate
  fleet_work_backup.py init                 # restic init (explicit;  a scheduled run never inits)
  fleet_work_backup.py check-access         # can this process read /Volumes/External?
  fleet_work_backup.py check-access --launchd   # ask a one-shot launchd job (the scheduled job's own grants)
  fleet_work_backup.py image                # the executable macOS privacy settings see for this interpreter
  fleet_work_backup.py new-password         # generate the restic password into Infisical, never printed (once)
  fleet_work_backup.py snapshots            # restic snapshots for this job's tag
  fleet_work_backup.py restic -- <args>     # restic with this job's repo and credentials (restores)

Standard library only.  Test hooks are the Config fields and FLEET_WORK_BACKUP_* variables.
Docs:  scripts/fleet-work-backup/README.md and RESTORE.md.
"""
from __future__ import annotations

import argparse
import base64
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
import plistlib
import re
import secrets
import shutil
import signal
import sqlite3
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

VERSION = "2"
TAG = "fleet-work"
RETENTION = (("--keep-hourly", "24"), ("--keep-daily", "14"), ("--keep-weekly", "8"), ("--keep-monthly", "6"))
PRUNE_EVERY = 24 * 3600
CHECK_EVERY = 7 * 24 * 3600
SKIP_WARN_AFTER = 8  # consecutive skipped runs (about 24 hours at one run per 3 hours)
ALERT_EVERY = 24 * 3600     # at most one agent-sync alert a day while runs stay not ok
ALERT_TOPIC = "AFC 29c02bcb fleet-work-backup"

# Directory names never entered (git discovery and the non-git file walk).
SKIP_DIR_NAMES = {
    "node_modules", ".venv", "venv", "__pycache__", ".cache", "Caches", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "site-packages", "dist", "build", ".next", ".turbo", ".parcel-cache",
    "DerivedData", "Pods", ".gradle", ".idea", ".git", ".Trash", "logs", "coverage", ".dart_tool",
    # Downloaded or generated, never work:  Playwright browsers, Xcode index stores and local builds, SwiftPM.
    ".pw-browsers", "ms-playwright", "Index.noindex", "build-local", "build-local-ios", ".build", "SourcePackages",
}
# Extra names an owner adds without a code change:  FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS=name1,name2
SKIP_DIR_NAMES |= {n.strip() for n in os.environ.get("FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS", "").split(",") if n.strip()}
# File names never staged (logs, caches, build debris, live-DB sidecars).
SKIP_FILE_GLOBS = (
    "*.log", "*.log.*", "*.pyc", "*.pyo", ".DS_Store", "*.swp", "*.swo", "*.tmp", "*.sock", "*.pid",
    "*-wal", "*-shm", "*.db-journal", "*.sqlite-journal", "*.db-wal", "*.db-shm",
)
# Runtime homes and transcript stores that hold session text (tool output routinely carries credentials
# in shapes no scanner knows) and are rebuilt by the tool.  Matched as a run of path components under
# the walked roots.  FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS=1 turns the built-in list off.
SKIP_PATH_PARTS = (("acp-home", "sessions"), ("acp-home", "memtrace"), ("fleet-rag", "mined", "chat"))
if os.environ.get("FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS") == "1":
    SKIP_PATH_PARTS = ()
# Secret-looking names, matched case-insensitively against a file's base name.  STRICT names are key
# and credential material:  they are dropped everywhere, tracked-file patches included.  BROAD names
# (secret, token, credential, passwd) are dropped for untracked and non-git files;  for the patch of
# a tracked file they are not, because that file is source already in git (tokenizer.py, auth/
# credentials.ts) and the content scan still guards it.  Only code and prose extensions get that
# exemption (TRACKED_EXEMPT_EXTS);  a modified client_secret.json or service_token.txt is dropped.
SECRET_STRICT_GLOBS = (
    ".env", ".env.*", "*.env", "*.env.*", "*zuliprc*", "*.pem", "*.p8", "*.p12", "*.pfx", "*.key", "*.keystore",
    "*.jks", "*.keychain", "*.keychain-db", "*.kdbx", "*.pkcs12", "id_rsa*", "id_dsa*", "id_ecdsa*",
    "id_ed25519*", "id_xmss*", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".htpasswd", "global-api-keys*",
    "infisical-machine-identity*", "service-account*.json", "*serviceaccount*.json", "credentials.json",
    ".dev.vars", ".dev.vars.*", "auth.json", "*creds*.json", ".envrc", "*.tfstate", "*.tfstate.*", "*.tfvars",
    "*.tfvars.json", "*.mobileprovision", "*.ovpn",
)
# A path that ends like one of these is a secret file whatever its base name (a bare hosts.yml is an
# Ansible inventory, gh/hosts.yml holds the GitHub CLI token).
SECRET_PATH_SUFFIXES = ("gh/hosts.yml", "gh/hosts.yaml", ".config/gh/hosts.yml")
TRACKED_EXEMPT_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".swift", ".go", ".rs", ".rb", ".java",
                       ".kt", ".c", ".h", ".m", ".mm", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql",
                       ".md", ".mdx", ".rst")
SECRET_BROAD_GLOBS = ("*secret*", "*token*", "*credential*", "*passwd*")
# A name that matches a secret glob but is a documented template.  Content is still scanned.
SECRET_NAME_ALLOW = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
                     "*.env.example", "*.env.sample", "*.env.template")
# Directory components that mark a secret store.  Everything below one is dropped.
SECRET_DIR_NAMES = {".secrets", ".ssh", ".gnupg", ".aws", ".infisical", "Keychains", ".docker", ".kube"}
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")

# Content patterns (bytes).  High confidence only:  a match drops the whole file or patch.
CONTENT_PATTERNS: dict[str, re.Pattern[bytes]] = {
    # A header alone is a fixture or a doc example;  a key has a base64 body right after it (raw or as \n in JSON).
    "private-key-block": re.compile(
        rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?:\\n|\\r|\s)*(?:[A-Za-z0-9+/]{40,}|[A-Za-z0-9+/]{20,}=)"),
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
    # A JSON field that carries a credential under its usual name (an OAuth refresh token in a CLI's auth file).
    "json-secret-field": re.compile(
        rb'"(?:access_token|refresh_token|id_token|client_secret|api_key|apikey|secret_key|private_key|password)"'
        rb'\s*:\s*"([A-Za-z0-9._~+/=\-]{24,})"'),
}
ENTROPY_GATED = {"env-assignment", "json-secret-field"}
# Token-shaped matches that are examples in documentation do not count:  too few distinct characters,
# the word EXAMPLE, runs of x or 0, or an alphabet or digit run.
PLACEHOLDER_CHECKED = {"aws-access-key-id", "github-token", "slack-token", "stripe-key", "google-api-key",
                       "anthropic-key", "openai-key"}
PLACEHOLDER_MARKS = (b"example", b"xxxxxx", b"000000", b"abcdefgh", b"12345678", b"your", b"redacted", b"placeholder")


def is_placeholder(match: bytes) -> bool:
    low = match.lower()
    return len(set(match)) < 10 or any(m in low for m in PLACEHOLDER_MARKS)
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
    "json-secret-field": (b"_token", b"client_secret", b"api_key", b"apikey", b"secret_key", b"private_key", b"password"),
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
    bundle_scan_cap: int = 512 * 1024 ** 2
    host: str = "jay-mac"
    s3_endpoint: str = "s3.eu-central-003.backblazeb2.com"
    check_landed: bool = True
    git_timeout: int = 180
    bundle_timeout: int = 900
    max_runtime: int = 2 * 3600
    gitleaks_ignore_rules: tuple = ()
    gitleaks_budget_s: int = 1500
    repository: str = ""            # a restic repository to use instead of B2 (tests, a local mirror)
    launchctl_bin: str = "launchctl"
    check_wait_s: int = 90
    agent_sync_bin: str = ""        # '' is <home>/.local/bin/agent-sync (launchd's PATH does not have it)
    alert_seat: str = "CLAUDE"
    alert_topic: str = ALERT_TOPIC
    alert_to: tuple = ()            # seats or people to @-mention;  none means the post wakes nobody
    alert_after: int = SKIP_WARN_AFTER
    infisical_bin: str = "/opt/homebrew/bin/infisical"
    b2_http: Optional[Callable] = None   # (method, url, headers, body) -> (status, json):  tests answer offline
    load_fn: Optional[Callable[[], float]] = None
    swap_fn: Optional[Callable[[], Optional[float]]] = None
    pressure_fn: Optional[Callable[[], Optional[int]]] = None
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
    def check_plist_path(self) -> Path:
        return self.state_dir / "check-access.plist"

    @property
    def check_result_path(self) -> Path:
        return self.state_dir / "check-access.json"

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
    cfg.repository = os.environ.get("FLEET_WORK_BACKUP_REPOSITORY", cfg.repository)
    cfg.launchctl_bin = os.environ.get("FLEET_WORK_BACKUP_LAUNCHCTL", cfg.launchctl_bin)
    cfg.agent_sync_bin = os.environ.get("FLEET_WORK_BACKUP_AGENT_SYNC", cfg.agent_sync_bin)
    cfg.infisical_bin = os.environ.get("FLEET_WORK_BACKUP_INFISICAL", cfg.infisical_bin)
    cfg.alert_seat = os.environ.get("FLEET_WORK_BACKUP_ALERT_SEAT", cfg.alert_seat)
    cfg.alert_topic = os.environ.get("FLEET_WORK_BACKUP_ALERT_TOPIC", cfg.alert_topic)
    cfg.alert_to = tuple(t.strip() for t in os.environ.get("FLEET_WORK_BACKUP_ALERT_TO", "").split(",") if t.strip())
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
    ``tracked`` is the patch of a file git already tracks:  the broad names (secret, token, credential,
    passwd) are waived for source and prose files only;  the strict names always apply."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    base = parts[-1]
    # The broad-name exemption for a tracked file covers source and prose only.
    exempt = tracked and base.lower().endswith(TRACKED_EXEMPT_EXTS)
    for comp in parts[:-1]:
        if comp in SECRET_DIR_NAMES or (not exempt and "secret" in comp.lower()):
            return True
    if base in SECRET_DIR_NAMES:
        return True
    if _glob_hit(base, SECRET_NAME_ALLOW):
        return False
    low = "/".join(parts).lower()
    if any(low == suf or low.endswith("/" + suf) for suf in SECRET_PATH_SUFFIXES):
        return True
    if _glob_hit(base, SECRET_STRICT_GLOBS):
        return True
    return not exempt and _glob_hit(base, SECRET_BROAD_GLOBS)


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
        elif pid in PLACEHOLDER_CHECKED:
            if any(not is_placeholder(m.group(0)) for m in rx.finditer(data)):
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
            "-c", "gc.auto=0", "-c", "advice.detachedHead=false",
            "-c", "pack.threads=1")

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
    missing_volumes: list[str] = dataclasses.field(default_factory=list)   # symlinks onto an unmounted /Volumes disk


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
                        try:
                            if os.readlink(e.path).startswith("/Volumes/"):
                                d.missing_volumes.append(e.path)
                        except OSError:
                            pass
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


def linked_worktrees(git: Git, cwd: str) -> tuple[list[str], list[str]]:
    """Worktrees ``git worktree list`` names for the repository at ``cwd``:  (existing paths, missing paths).
    A linked worktree can live anywhere (~/.codex/worktrees), so discovery by folder walk is not enough."""
    rc, out, _ = git.run(cwd, "worktree", "list", "--porcelain", "-z")
    found: list[str] = []
    gone: list[str] = []
    if rc != 0:
        return found, gone
    for rec in out.decode("utf-8", "surrogateescape").split("\0\0"):
        fields = [f for f in rec.split("\0") if f]
        if not fields or not fields[0].startswith("worktree "):
            continue
        path = fields[0][len("worktree "):]
        if any(f == "bare" for f in fields):
            continue
        if any(f.startswith("prunable") for f in fields) or not os.path.isdir(path):
            gone.append(path)
        else:
            found.append(path)
    return found, gone


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
        self.dropped: list[dict] = []     # secret names and content hits:  never uploaded
        self.deferred: list[dict] = []    # not cleared by gitleaks this run:  left out, retried next run
        self.large: list[dict] = []
        self.notes: list[str] = []
        self.blocked: list[dict] = []
        self.allow: dict[str, str] = {}   # label -> content digest prefix the owner reviewed (allow.txt)
        self.allow_unpinned: list[str] = []
        self.allow_stale: list[str] = []
        self.allowed: list[str] = []

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
        digest = ""
        try:
            digest = file_digest(self.path(rel))[:12]
        except OSError:
            pass
        it = self.items.pop(rel, None)
        try:
            os.unlink(self.path(rel))
        except OSError:
            pass
        self.dropped.append({"path": it.src if it and it.src else rel, "stage": rel, "reason": reason, "digest": digest})

    def defer(self, rel: str, reason: str) -> None:
        it = self.items.pop(rel, None)
        try:
            os.unlink(self.path(rel))
        except OSError:
            pass
        self.deferred.append({"path": it.src if it and it.src else rel, "stage": rel, "reason": reason})

    def permit(self, label: str, digest: str) -> bool:
        """True when allow.txt pins ``label`` to this exact content (or ref tip).  An entry whose content
        changed since the owner reviewed it no longer applies and is reported."""
        want = self.allow.get(label)
        if not want:
            return False
        if digest and digest.startswith(want):
            if label not in self.allowed:
                self.allowed.append(label)
            return True
        if label not in self.allow_stale:
            self.allow_stale.append(label)
        return False

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


# A line of a text patch always starts with a space, + or -, so this marks a binary patch exactly.
BINARY_PATCH_RX = re.compile(rb"(?m)^GIT binary patch$")


def is_binary_file(path: str | Path) -> bool:
    """git's own test:  a NUL byte in the first 8000 bytes."""
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(8000)
    except OSError:
        return False


def _name_batches(names: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for name in names:
        if batch and (size + len(name) > 100_000 or len(batch) >= 1500):
            batches.append(batch)
            batch, size = [], 0
        batch.append(name)
        size += len(name) + 1
    if batch:
        batches.append(batch)
    return batches


def binary_names(git: Git, cwd: str, diff_args: list[str], names: list[str]) -> Optional[set[str]]:
    """Names git treats as binary in this diff (numstat prints '-' for both counts).  None on a git error."""
    found: set[str] = set()
    for b in _name_batches(names):
        rc, out, _ = git.run(cwd, "diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv", "--no-renames",
                             "--ignore-submodules=all", *diff_args, "--", *b)
        if rc != 0:
            return None
        for rec in out.decode("utf-8", "surrogateescape").split("\0"):
            if rec.startswith("-\t-\t"):
                found.add(rec[4:])
    return found


def blob_sizes(git: Git, cwd: str, rev: str, names: list[str]) -> dict[str, Optional[int]]:
    """Size of ``rev:path`` for each name;  None when the path is not in that tree (a deletion)."""
    sizes: dict[str, Optional[int]] = {}
    for b in _name_batches(names):
        stdin = "".join(f"{rev}:{n}\n" for n in b).encode("utf-8", "surrogateescape")
        rc, out, _ = git.run(cwd, "cat-file", "--batch-check=%(objectsize)", stdin=stdin)
        lines = out.decode("utf-8", "replace").splitlines() if rc == 0 else []
        for n, line in zip(b, lines):
            sizes[n] = int(line) if line.strip().isdigit() else None
    return sizes


def stage_patches(cfg: Config, git: Git, stage: Stage, cwd: str, diff_args: list[str], names: list[str],
                  rel_dir: str, src_label: str, check_size_in: Optional[str], tracked: bool = True,
                  post_rev: Optional[str] = None) -> int:
    """Stage the changes to ``names``.  Returns the number of files written.

    A text change becomes one patch file (``git diff --binary``), scanned as written.  A binary change
    is never staged as a patch:  git's binary patch is zlib-deflated base85, which neither scanner can
    read, so the raw new content is staged instead (``<rel_dir>/binary/<path>``, from the worktree or,
    for a stash, ``post_rev:<path>``) and the gate scans that.  A binary deletion is listed in
    ``binary-deleted.txt``.  A name that looks secret, or content over the size cap, is never read."""
    kept: list[str] = []
    for name in names:
        if "\n" in name:
            stage.notes.append(f"newline-in-name {src_label}")
            continue
        if is_secret_name(name, tracked):
            stage.dropped.append({"path": f"{src_label}:{name}", "stage": "", "reason": "secret-name"})
            continue
        kept.append(name)
    sizes: dict[str, Optional[int]] = blob_sizes(git, cwd, post_rev, kept) if post_rev else {}
    sized: list[str] = []
    for name in kept:
        if check_size_in:
            try:
                size = os.lstat(os.path.join(check_size_in, name)).st_size
            except OSError:
                size = 0
            label = os.path.join(check_size_in, name)
        else:
            size = sizes.get(name) or 0
            label = f"{src_label}:{name}"
        if size > cfg.max_file_bytes:
            stage.large.append({"path": label, "size": size})
            continue
        sized.append(name)
    if not sized:
        return 0
    bins = binary_names(git, cwd, diff_args, sized)
    if bins is None:
        stage.notes.append(f"diff-failed {src_label}")
        return 0
    written = 0
    deleted: list[str] = []
    for name in sorted(bins):
        rel = f"{rel_dir}/binary/{name}"
        label = f"{src_label}:{name}" if post_rev else os.path.join(cwd, name)
        dst = stage.path(rel)
        if post_rev:
            if name not in sizes:
                stage.notes.append(f"binary-size-unknown {label}")
                continue
            if sizes[name] is None:
                deleted.append(name)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(dst, "wb") as fh:
                    p = subprocess.run([cfg.git_bin, *Git.BASE, "-C", cwd, "cat-file", "blob", f"{post_rev}:{name}"],
                                       stdout=fh, stderr=subprocess.DEVNULL, env=git.env, timeout=cfg.git_timeout)
            except (OSError, subprocess.TimeoutExpired):
                stage.notes.append(f"binary-read-failed {label}")
                continue
            if p.returncode != 0:
                stage.notes.append(f"binary-read-failed {label}")
                try:
                    os.unlink(dst)
                except OSError:
                    pass
                continue
            stage.add(rel, label, dst.stat().st_size, "binary")
            written += 1
            continue
        src = os.path.join(cwd, name)
        try:
            st = os.lstat(src)
        except OSError:
            deleted.append(name)
            continue
        if not stat_mod.S_ISREG(st.st_mode):
            stage.notes.append(f"binary-not-a-file {label}")
            continue
        try:
            clone_or_copy(src, str(dst))
        except OSError as exc:
            stage.notes.append(f"copy-failed {src} ({errno.errorcode.get(exc.errno or 0, exc.errno)})")
            continue
        stage.add(rel, label, st.st_size, "binary")
        written += 1
    if deleted:
        stage.write(f"{rel_dir}/binary-deleted.txt", ("\n".join(sorted(deleted)) + "\n").encode("utf-8", "surrogateescape"),
                    "", "meta")
    text_names = [n for n in sized if n not in bins]
    # git diff takes no --pathspec-from-file, so the paths go on the command line in batches.
    out = b""
    for b in _name_batches(text_names):
        rc, part, err = git.run(cwd, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary",
                                "--ignore-submodules=all", *diff_args, "--", *b)
        if rc != 0:
            stage.notes.append(f"diff-failed {src_label}")
            return written
        out += part
    for idx, chunk in enumerate(split_diff(out), 1):
        cp = chunk_path(chunk)
        if cp and is_secret_name(cp, tracked):
            stage.dropped.append({"path": f"{src_label}:{cp}", "stage": "", "reason": "secret-name"})
            continue
        label = f"{src_label}:{cp or idx}"
        digest = hashlib.sha1(chunk).hexdigest()
        if BINARY_PATCH_RX.search(chunk):   # the file changed kind between two git calls:  cannot be read, so not kept
            stage.dropped.append({"path": label, "stage": "", "reason": "unscannable-binary-patch", "digest": digest[:12]})
            continue
        if len(chunk) > cfg.max_file_bytes:
            stage.large.append({"path": label, "size": len(chunk)})
            continue
        hit = scan_bytes(chunk)
        if hit and not stage.permit(label, digest):
            stage.dropped.append({"path": label, "stage": "", "reason": f"content:{hit}", "digest": digest[:12]})
            continue
        stage.write(f"{rel_dir}/{patch_name(chunk, idx)}", chunk, label, "patch")
        written += 1
    return written


def stage_untracked(cfg: Config, stage: Stage, root: str, names: list[str], rel_dir: str) -> int:
    count = 0
    for name in names:
        if is_skipped_file(os.path.basename(name)) or any(p in SKIP_DIR_NAMES for p in name.split("/")[:-1]):
            continue
        if is_secret_name(name):
            stage.dropped.append({"path": os.path.join(root, name), "stage": "", "reason": "secret-name"})
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
    bundles: int = 0
    head_bundles: int = 0
    anchor_refs: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.unpushed_refs or self.stashes or self.dirty_worktrees or self.head_bundles)


def repo_name(common_dir: str) -> str:
    p = Path(common_dir)
    return p.parent.name if p.name == ".git" else p.name


COMMIT_MARK = "@@fwb-commit@@"
SCAN_CHUNK = 4 * 1024 * 1024
COMMIT_MARK_RX = re.compile(rb"(?m)^@@fwb-commit@@([0-9a-f]{40})\n")


def scan_commits(git: Git, cwd: str, args: list[str], cap: int) -> tuple[dict[str, str], bool]:
    """One ``git log -p --cc`` pass over a range, scanned commit by commit:  returns ({commit: pattern} for every
    commit whose patch matches, truncated).  A hit is attributed to the commit that carries it, so which branches
    are affected is a reachability question afterwards, not another scan of the shared history per branch."""
    cmd = [git.cfg.git_bin, *Git.BASE, "-C", cwd, "log", "-p", "--cc", "--no-color", "--no-ext-diff", "--no-textconv",
           f"--format={COMMIT_MARK}%H", *args]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git.env)
    except OSError:
        return {}, False
    bad: dict[str, str] = {}
    state = {"cur": None, "tail": b""}

    def scan_segment(seg: bytes) -> None:
        if not seg:
            return
        cur = state["cur"]
        if cur is not None and cur not in bad:
            hit = scan_bytes(state["tail"] + seg)
            if hit:
                bad[cur] = hit
        state["tail"] = seg[-8192:]

    def feed(data: bytes) -> None:
        pos = 0
        for m in COMMIT_MARK_RX.finditer(data):
            scan_segment(data[pos:m.start()])
            state["cur"], state["tail"], pos = m.group(1).decode(), b"", m.end()
        scan_segment(data[pos:])

    read = 0
    carry = b""
    try:
        while read < cap:
            chunk = proc.stdout.read(SCAN_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            carry += chunk
            cut = carry.rfind(b"\n") + 1      # whole lines only, so a marker line is never split
            if cut:
                feed(carry[:cut])
                carry = carry[cut:]
        truncated = read >= cap and bool(proc.stdout.read(1))     # at the window's end with more to come
        if not truncated:
            feed(carry)
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
    return bad, truncated


def refs_containing(git: Git, cwd: str, commits: list[str], refs: set[str]) -> set[str]:
    """Which of ``refs`` can reach any of ``commits``  (git for-each-ref --contains, batched)."""
    found: set[str] = set()
    for i in range(0, len(commits), 1500):
        args: list[str] = []
        for sha in commits[i:i + 1500]:
            args += ["--contains", sha]
        rc, out, _ = git.run(cwd, "for-each-ref", "--format=%(refname)", *args, "refs/heads", "refs/tags")
        if rc == 0:
            found |= {r for r in out.decode("utf-8", "replace").split() if r in refs}
    return found


def is_network_url(url: str) -> bool:
    """True for a remote GitHub-like host.  A local path, file:// URL or bundle file is the same disk."""
    if re.match(r"^(?:https?|ssh|git|ftps?|git\+ssh|ssh\+git)://", url, re.I):
        return True
    if url.startswith(("file://", "/", "./", "../", "~")):
        return False
    return bool(re.match(r"^(?:[\w.\-]+@)?[\w.\-]+:(?!//)", url))


def remote_info(git: Git, cwd: str) -> tuple[list[str], list[str]]:
    """(``name url`` lines with credentials stripped, names of remotes whose configured URL is on the network).
    The URL is read from the config as written, so an ``insteadOf`` rewrite cannot turn a GitHub remote into a path."""
    rc, out, _ = git.run(cwd, "config", "-z", "--get-regexp", r"^remote\..*\.url$")
    meta: set[str] = set()
    urls: dict[str, str] = {}
    for rec in out.decode("utf-8", "replace").split("\0") if rc == 0 else []:
        key, _, val = rec.partition("\n")
        m = re.fullmatch(r"remote\.(.+)\.url", key, re.I)
        if not m or not val.strip():
            continue
        urls.setdefault(m.group(1), val.strip())
        meta.add(strip_url_credentials(f"{m.group(1)} {val.strip()}"))
    net = sorted((n for n, u in urls.items() if is_network_url(u)), key=lambda n: (n != "origin", n))
    return sorted(meta), net


def resolve_bound(git: Git, cwd: str, net: list[str]) -> tuple[str, str]:
    """The default branch of the first network remote:  (ref name, commit sha), or ('', '').  A bundle bounded
    by it has prerequisites that sit on that branch, which GitHub keeps for good;  bounded by every
    remote-tracking tip it would also need branches that are later deleted or force-pushed."""
    for r in net:
        cands = []
        head = git.text(cwd, "symbolic-ref", "-q", f"refs/remotes/{r}/HEAD")
        if head:
            cands.append(head)
        cands += [f"refs/remotes/{r}/main", f"refs/remotes/{r}/master"]
        for c in cands:
            sha = git.text(cwd, "rev-parse", "-q", "--verify", f"{c}^{{commit}}")
            if sha:
                return c, sha
    return "", ""


def proxy_bound(git: Git, cwd: str) -> Optional[dict]:
    """For a repository whose only remotes are local paths:  a commit GitHub holds, found through a local remote
    that is itself a checkout with a GitHub remote (a flat lane cloned from ~/Code/X).  That checkout's default
    branch tip is the bound when this clone has it;  a clone that has not fetched lately has an older tip, so the
    bound is the newest commit that its own tracking ref for the local remote and the checkout's GitHub branch
    share (the merge base, computed in the checkout, which has both).  That commit is on GitHub's default branch
    and an ancestor of what this clone holds."""
    rc, out, _ = git.run(cwd, "config", "-z", "--get-regexp", r"^remote\..*\.url$")
    for rec in out.decode("utf-8", "replace").split("\0") if rc == 0 else []:
        key, _, val = rec.partition("\n")
        m = re.fullmatch(r"remote\.(.+)\.url", key, re.I)
        val = val.strip()
        path = val[len("file://"):] if val.startswith("file://") else val
        if not m or not path.startswith("/") or not os.path.isdir(path):
            continue
        pmeta, pnet = remote_info(git, path)
        if not pnet:
            continue
        pref, psha = resolve_bound(git, path, pnet)
        if not psha:
            continue
        found = ""
        if git.run(cwd, "cat-file", "-e", f"{psha}^{{commit}}")[0] == 0:
            found = psha
        else:
            for cand in (f"refs/remotes/{m.group(1)}/HEAD", f"refs/remotes/{m.group(1)}/main", f"refs/remotes/{m.group(1)}/master"):
                sha = git.text(cwd, "rev-parse", "-q", "--verify", f"{cand}^{{commit}}")
                if not sha:
                    continue
                rc, mb, _ = git.run(path, "merge-base", sha, psha)
                base = mb.decode().strip() if rc == 0 else ""
                if base and git.run(cwd, "cat-file", "-e", f"{base}^{{commit}}")[0] == 0:
                    found = base
                    break
        if found:
            url = next((l.split(" ", 1)[1] for l in pmeta if l.split(" ", 1)[0] == pnet[0] and " " in l), "")
            return {"via": path, "ref": pref, "sha": found, "url": url}
    return None


def overlap_components(git: Git, cwd: str, refs: list[str], not_bound: list[str], limit: int = 200_000) -> list[list[str]]:
    """Group refs whose commit ranges share a commit, so a commit is bundled once per group instead of once
    per branch.  Independent branches get their own bundle:  a restore of one does not depend on another."""
    if not refs:
        return []
    parent = {r: r for r in refs}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, str] = {}
    for r in refs:
        rc, out, _ = git.run(cwd, "rev-list", r, *not_bound)
        commits = out.decode().split() if rc == 0 else []
        if len(commits) > limit:
            return [sorted(refs)]
        for c in commits:
            if c in owner:
                parent[find(r)] = find(owner[c])
            else:
                owner[c] = r
    comps: dict[str, list[str]] = {}
    for r in refs:
        comps.setdefault(find(r), []).append(r)
    return sorted((sorted(v) for v in comps.values()), key=lambda v: v[0])


def make_bundle(cfg: Config, git: Git, stage: Stage, cwd: str, rel: str, src: str, args: list[str]) -> bool:
    """git bundle create into the stage.  A bundle over the size cap is listed and not kept."""
    out_path = stage.path(rel)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = git.run(cwd, "bundle", "create", str(out_path), *args, timeout=cfg.bundle_timeout)
    if rc != 0 or not out_path.exists():
        stage.notes.append(f"bundle-failed {src}: {err.decode('utf-8', 'replace').strip()[:120]}")
        try:
            out_path.unlink()
        except OSError:
            pass
        return False
    size = out_path.stat().st_size
    if size > cfg.max_file_bytes:
        stage.large.append({"path": f"{src} bundle {rel.rsplit('/', 1)[-1]}", "size": size})
        out_path.unlink()
        return False
    stage.add(rel, src, size, "bundle")
    return True


ANCHOR_NS = "refs/fwb/"


def make_anchors(cfg: Config, git: Git, stage: Stage, cwd: str, common_dir: str, rel: str,
                 cands: list[tuple[str, str]], excl: list[str], scan_excl: list[str]) -> dict[str, dict]:
    """Bundle the commits that stash and worktree patches apply to, when nothing else in the backup holds them.

    ``cands`` is (name, sha):  a stash's base (its first parent, never the stash commit itself, whose index and
    untracked trees hold exactly what the gate dropped) or a dirty worktree's HEAD.  A candidate reachable from
    ``excl`` (the network remote's default branch and the tips the branch bundles hold) is covered.  The rest are
    scanned like unpushed commits, then cut into one bundle, ``refs/fwb/<name>``, bounded by ``excl``.  git bundles
    only refs, and the real repository is never written:  the refs live in a scratch bare repository whose
    alternates point at the real object store, and it is removed afterwards.  Returns {name: {sha, state}}."""
    out: dict[str, dict] = {}
    need: list[tuple[str, str]] = []
    for name, sha in cands:
        stdin = "".join([sha + "\n"] + [f"^{e}\n" for e in excl]).encode()
        rc, o, _ = git.run(cwd, "rev-list", "-1", "--stdin", stdin=stdin)
        if rc != 0:
            out[name] = {"sha": sha, "state": "unreadable"}
            stage.notes.append(f"anchor-unreadable {common_dir} {name}")
        elif not o.strip():
            out[name] = {"sha": sha, "state": "covered"}       # on the default branch, or in a branch bundle
        else:
            need.append((name, sha))
    if not need:
        return out
    shas = sorted({s for _, s in need})
    bad, truncated = scan_commits(git, cwd, [*shas, *(["--not", *scan_excl] if scan_excl else [])], cfg.bundle_scan_cap)
    keep: list[tuple[str, str]] = []
    for name, sha in need:
        hit = next((pat for c, pat in sorted(bad.items())
                    if c == sha or git.run(cwd, "merge-base", "--is-ancestor", c, sha)[0] == 0), "")
        label = f"{common_dir} anchor {name}"
        if hit and not stage.permit(label, sha):
            stage.dropped.append({"path": label, "stage": "", "reason": f"content:{hit}", "digest": sha[:12]})
            out[name] = {"sha": sha, "state": "dropped"}
        else:
            keep.append((name, sha))
    if not keep:
        return out
    if truncated:
        stage.notes.append(f"bundle-scan-partial {common_dir} (anchors)")
    scratch = tempfile.mkdtemp(prefix="objs-anchor-", dir=str(cfg.cache_dir))
    try:
        fmt = git.text(cwd, "rev-parse", "--show-object-format") or "sha1"
        rc, _, err = git.run(cfg.cache_dir, "init", "-q", "--bare", "--template=", f"--object-format={fmt}", scratch)
        ok = rc == 0
        if ok:
            info = Path(scratch) / "objects" / "info"
            info.mkdir(parents=True, exist_ok=True)
            (info / "alternates").write_text(os.path.join(common_dir, "objects") + "\n")
            if os.path.isfile(os.path.join(common_dir, "shallow")):
                shutil.copyfile(os.path.join(common_dir, "shallow"), os.path.join(scratch, "shallow"))
            lines = "".join(f"create {ANCHOR_NS}{name} {sha}\n" for name, sha in keep).encode()
            rc, _, err = git.run(scratch, "update-ref", "--stdin", stdin=lines)
            ok = rc == 0
        if not ok:
            stage.notes.append(f"anchor-failed {common_dir}: {err.decode('utf-8', 'replace').strip()[:120]}")
            made = False
        else:
            made = make_bundle(cfg, git, stage, scratch, rel, common_dir,
                               [*(ANCHOR_NS + n for n, _ in keep), *(["--not", *excl] if excl else [])])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for name, sha in keep:
        out[name] = {"sha": sha, "state": "bundled" if made else "failed", "ref": ANCHOR_NS + name}
    return out


def process_group(cfg: Config, git: Git, stage: Stage, common_dir: str, wts: list[Worktree]) -> GroupResult:
    name = repo_name(common_dir)
    slug = f"{slugify(name, 40)}-{short_hash(common_dir)}"
    res = GroupResult(slug=slug, name=name, common_dir=common_dir, worktrees=len(wts))
    base = f"git/{slug}"
    cwd = wts[0].path
    scratch = tempfile.mkdtemp(prefix="objs-", dir=str(cfg.cache_dir))
    try:
        # Pushed means "a network remote has it".  A remote that is a local path, a file:// URL or a bundle
        # file is the same disk, so what it holds is not backed up by it.
        remotes, net = remote_info(git, cwd)
        res.no_remote = not remotes
        net_args = [f"--remotes={n}" for n in net]
        not_net = ["--not", *net_args] if net_args else []
        bound_ref, bound_sha = resolve_bound(git, cwd, net) if net else ("", "")
        bound_args = [bound_sha] if bound_sha else net_args
        not_bound = ["--not", *bound_args] if bound_args else []
        if net and not bound_sha:
            stage.notes.append(f"bundle-bound-fallback {common_dir} (no default branch found;  bounded by every network remote ref)")
        proxy = None
        if remotes and not net:
            proxy = proxy_bound(git, cwd)
            if proxy:
                # Cloned from another checkout on this Mac that has a GitHub remote:  that checkout's default
                # branch is on GitHub, so it is the bound and the pushed line.
                bound_ref = bound_sha = proxy["sha"]
                bound_args, not_bound = [bound_sha], ["--not", bound_sha]
                not_net = ["--not", bound_sha]
                stage.notes.append(f"local-remote-proxied {common_dir} (bounded by {proxy['ref']} of {proxy['via']})")
            else:
                stage.notes.append(f"local-only-remotes {common_dir} (full history bundled)")
        # Unpushed refs:  tips that git rev-list finds outside every network remote's refs.
        rc, out, err = git.run(cwd, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(*objectname)",
                               "refs/heads", "refs/tags")
        refs: list[tuple[str, str]] = []
        if rc != 0:
            stage.notes.append(f"refs-unreadable {common_dir}: {err.decode('utf-8', 'replace').strip()[:100]}")
        for line in out.decode("utf-8", "replace").splitlines() if rc == 0 else []:
            parts = line.split("\0")
            if len(parts) >= 2:
                refs.append((parts[0], (parts[2] if len(parts) > 2 and parts[2] else parts[1])))
        unpushed: set[str] = set()
        if refs:
            rc, out, err = git.run(cwd, "rev-list", "--branches", "--tags", *not_net)
            if rc == 0:
                unpushed = set(out.decode().split())
            else:
                stage.notes.append(f"rev-list-failed {common_dir}: {err.decode('utf-8', 'replace').strip()[:100]}")
        pending = [(r, s) for r, s in refs if s in unpushed]
        # Branches whose merge into the default branch is a no-op are already on GitHub.
        landed = 0
        if pending and cfg.check_landed and len(pending) <= 400 and bound_ref:
            dtree = git.text(cwd, "rev-parse", f"{bound_ref}^{{tree}}")
            if dtree:
                objenv = {"GIT_OBJECT_DIRECTORY": os.path.join(scratch, "objects"),
                          "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(common_dir, "objects")}
                os.makedirs(objenv["GIT_OBJECT_DIRECTORY"], exist_ok=True)
                keep = []
                for ref, sha in pending:
                    if ref.startswith("refs/heads/"):
                        rc, out, _ = git.run(cwd, "merge-tree", "--write-tree", "--no-messages", bound_ref, ref,
                                             env=objenv, timeout=120)
                        if rc == 0 and out.split()[:1] == [dtree.encode()]:
                            landed += 1
                            continue
                    keep.append((ref, sha))
                pending = keep
        res.landed_refs = landed
        res.unpushed_refs = len(pending)
        group_meta: dict = {"tool": VERSION, "common_dir": common_dir, "name": name, "remotes": remotes,
                            "no_remote": res.no_remote, "network_remotes": net,
                            "bound": {"ref": bound_ref, "sha": bound_sha, **({"via": proxy["via"], "via_ref": proxy["ref"], "via_url": proxy["url"]} if proxy else {})},
                            "unpushed": [], "bundles": [],
                            "landed_skipped": landed, "worktrees": [], "stashes": []}
        # Bundles:  one per group of overlapping refs, shared by every worktree of the repository.  One pass
        # scans the commits no network remote has (a pushed commit is already on GitHub, so carrying it along
        # exposes nothing new), each hit is attributed to its commit, and the refs that can reach a hit commit
        # are dropped alone:  one key-shaped string in some branch costs that branch, not the repository.
        bundled_tips: list[str] = []
        if pending:
            tips = dict(pending)
            ref_names = [r for r, _ in pending]
            bad, truncated = scan_commits(git, cwd, [*ref_names, *not_net], cfg.bundle_scan_cap)
            if bad:
                dirty: dict[str, str] = {}
                by_pattern: dict[str, list[str]] = {}
                for sha, pat in bad.items():
                    by_pattern.setdefault(pat, []).append(sha)
                for pat, shas in by_pattern.items():
                    for ref in refs_containing(git, cwd, shas, set(tips)):
                        dirty.setdefault(ref, pat)
                clean: list[str] = []
                for ref in ref_names:
                    rl = f"{common_dir} ref {ref}"
                    if ref in dirty and not stage.permit(rl, tips[ref]):
                        stage.dropped.append({"path": rl, "stage": "", "reason": f"content:{dirty[ref]}", "digest": tips[ref][:12]})
                    else:
                        clean.append(ref)
                ref_names = clean
                pending = [(r, sha) for r, sha in pending if r in clean]
                res.unpushed_refs = len(pending)
            if ref_names and truncated:
                stage.notes.append(f"bundle-scan-partial {common_dir}")
            for comp in overlap_components(git, cwd, ref_names, not_bound):
                rel = f"{base}/bundles/{short_hash(chr(0).join(comp))}-{slugify(comp[0].rsplit('/', 1)[-1], 40)}.bundle"
                if make_bundle(cfg, git, stage, cwd, rel, common_dir, [*comp, *not_bound]):
                    res.bundle = True
                    res.bundles += 1
                    bundled_tips += [tips[r] for r in comp]
                    group_meta["bundles"].append({"file": rel.rsplit("/", 1)[-1], "refs": [{"ref": r, "sha": tips[r]} for r in comp]})
            group_meta["unpushed"] = [{"ref": r, "sha": s} for r, s in pending]
        # Every stash, as per-file patches (no refs are created in the repository).
        anchor_cands: list[tuple[str, str]] = []
        pending_meta: list[tuple[str, dict, str]] = []       # (stage rel, metadata, anchor name), written after the anchors
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
                                  f"{sdir}/tracked", f"{common_dir}#stash{n}", None, post_rev=sha)
            untracked_commit = git.text(cwd, "rev-parse", "-q", "--verify", f"{sha}^3")
            if untracked_commit:
                un_names = git.run(cwd, "ls-tree", "-r", "--name-only", "-z", untracked_commit)[1]
                wrote += stage_patches(cfg, git, stage, cwd, [empty_tree, untracked_commit],
                                       [p for p in un_names.decode("utf-8", "surrogateescape").split("\0") if p],
                                       f"{sdir}/untracked", f"{common_dir}#stash{n}", None, tracked=False,
                                       post_rev=untracked_commit)
            meta = {"index": n, "sha": sha, "base": base_sha, "subject": subject, "patches": wrote}
            aname = f"stash/{n:02d}"
            if wrote and base_sha:
                anchor_cands.append((aname, base_sha))
            pending_meta.append((f"{sdir}/stash.json", meta, aname))
            group_meta["stashes"].append(meta)
            res.stashes += 1
        # Per worktree:  uncommitted work and a detached HEAD no network remote has.
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
                rc, out, _ = git.run(wt.path, "rev-list", "-1", "HEAD", *not_net)
                if rc == 0 and out.strip():
                    label = f"{wt.path} (detached HEAD)"
                    hbad, htrunc = scan_commits(git, wt.path, ["HEAD", *not_net], cfg.bundle_scan_cap)
                    if hbad and stage.permit(label, wt.head):
                        hbad = {}
                    if hbad:
                        stage.dropped.append({"path": label, "stage": "", "reason": f"content:{next(iter(hbad.values()))}",
                                              "digest": wt.head[:12]})
                    elif make_bundle(cfg, git, stage, wt.path, f"{wdir}/head.bundle", wt.path, ["HEAD", *not_bound]):
                        if htrunc:
                            stage.notes.append(f"bundle-scan-partial {wt.path}")
                        wmeta["detached_head_bundle"] = True
                        res.head_bundles += 1
            if wrote or un or wmeta.get("detached_head_bundle"):
                wmeta["patches_written"] = wrote
                wmeta["untracked_written"] = un
                aname = f"worktree/{wslug}"
                if (wrote or un) and wt.head and not wmeta.get("detached_head_bundle"):
                    anchor_cands.append((aname, wt.head))
                pending_meta.append((f"{wdir}/worktree.json", wmeta, aname))
                group_meta["worktrees"].append(wmeta)
                if wrote or un:
                    res.dirty_worktrees += 1
                res.patch_files += wrote
                res.untracked_files += un
        # Anchors:  the commit a stash or a dirty worktree's patches apply to can be one GitHub never had, or one it
        # deletes later (a lane on a squash-landed branch, a stash on a rebased or deleted branch).  Those commits go
        # into anchors.bundle, bounded by the default branch and the tips the branch bundles above hold.
        if anchor_cands:
            excl = ([bound_sha] if bound_sha else []) + bundled_tips
            if not bound_sha and net:          # no default branch found:  every network remote tip bounds it instead
                rc, o, _ = git.run(cwd, "for-each-ref", "--format=%(objectname)", *[f"refs/remotes/{r}" for r in net])
                excl += o.decode().split() if rc == 0 else []
            scan_excl = (not_net[1:] if not_net else []) + bundled_tips
            anchors = make_anchors(cfg, git, stage, cwd, common_dir, f"{base}/anchors.bundle", anchor_cands,
                                   sorted(set(excl)), scan_excl)
            res.anchor_refs = sum(1 for a in anchors.values() if a["state"] == "bundled")
            group_meta["anchors"] = {"bundle": "anchors.bundle" if res.anchor_refs else "",
                                     "refs": {n: a for n, a in sorted(anchors.items())}}
            for _, meta, aname in pending_meta:
                if aname in anchors:
                    meta["anchor"] = {"ref": ANCHOR_NS + aname, **anchors[aname]}
        for rel_meta, meta, _ in pending_meta:
            stage.write(rel_meta, json.dumps(meta, indent=2).encode(), "", "meta")
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
                    # A checkout is backed up as git work;  a folder whose .git points nowhere (a pruned worktree)
                    # is not one, so its files are kept here as ordinary files.
                    if git_root_state(e.path) == "ok":
                        continue
                    parts = tuple(os.path.relpath(e.path, cfg.home).split(os.sep))
                    if any(parts[-len(sk):] == sk for sk in SKIP_PATH_PARTS):
                        stage.notes.append(f"skipped-by-default {e.path} (runtime session store)")
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
def file_digest(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_allow(cfg: Config) -> tuple[dict[str, str], list[str]]:
    """``allow.txt`` in the state folder:  one reviewed hit per line, ``<path> @ <digest>``, exactly as the
    detail log prints it (``allow-line``).  The digest pins the line to the content the owner looked at (a
    file's sha1, a branch's tip sha), so a real key pasted into an allowed file later is not waved through.
    Only content hits can be allowed;  a secret name never can.  A line with no ``@ digest`` is ignored and
    reported.  Returns ({label: digest}, unpinned lines)."""
    try:
        text = (cfg.state_dir / "allow.txt").read_text(encoding="utf-8")
    except OSError:
        return {}, []
    allow: dict[str, str] = {}
    unpinned: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, digest = line.rpartition(" @ ")
        digest = digest.strip().lower()
        if sep and label.strip() and re.fullmatch(r"[0-9a-f]{8,40}", digest):
            allow[label.strip()] = digest
        else:
            unpinned.append(line)
    return allow, unpinned


def scanner_fingerprint(cfg: Config) -> str:
    """Identifies the scanners that produced a cache entry:  the patterns, their prefilters and
    placeholder marks, this tool's version, and gitleaks' own version.  A change discards the cache,
    so a pattern added later runs over files an older pattern set had passed."""
    gl = ""
    if cfg.gitleaks_bin and shutil.which(cfg.gitleaks_bin):
        try:
            gl = subprocess.run([cfg.gitleaks_bin, "version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            gl = "?"
    blob = repr(({k: (v.pattern, v.flags) for k, v in CONTENT_PATTERNS.items()}, PREFILTER, PLACEHOLDER_MARKS,
                 sorted(PLACEHOLDER_CHECKED), sorted(ENTROPY_GATED), VERSION, gl, cfg.max_file_bytes))
    return hashlib.sha1(blob.encode()).hexdigest()


def load_scan_cache(cfg: Config, fingerprint: str) -> dict:
    try:
        data = json.loads((cfg.state_dir / "scan-cache.json").read_text())
        return data.get("files", {}) if data.get("v") == 2 and data.get("fp") == fingerprint else {}
    except (OSError, ValueError, AttributeError):
        return {}


def save_scan_cache(cfg: Config, files: dict, fingerprint: str) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = cfg.state_dir / f"scan-cache.json.tmp{os.getpid()}"
        tmp.write_text(json.dumps({"v": 2, "fp": fingerprint, "files": files}))
        os.replace(tmp, cfg.state_dir / "scan-cache.json")
    except OSError:
        pass


def gitleaks_batch(cfg: Config, stage: Stage, rels: list[str], deadline: float) -> tuple[Optional[dict], str]:
    """Run gitleaks over a batch of staged files (hard-linked into a scratch folder).  Returns
    ({rel: reason} for flagged files, '') or (None, error).  Every finding must map back to a file of
    the batch:  one that does not is an error, never a pass."""
    remaining = deadline - time.time()
    if remaining < 1:
        return None, "budget"
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
        # Throttled:  gitleaks is parallel and this Mac runs hot, so two threads at low priority.  The size
        # limit is the staging cap, so nothing that is staged is skipped for being large.
        limit_mb = max(1, cfg.max_file_bytes // (1024 * 1024)) + 1
        p = subprocess.run([cfg.gitleaks_bin, "dir", str(batch), "--no-banner", "--redact", "-f", "json",
                            "-r", str(report), "--exit-code", "0", "--log-level", "error",
                            "--max-target-megabytes", str(limit_mb)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=remaining,
                           cwd=str(cfg.cache_dir), env={**os.environ, "GOMAXPROCS": "2"},
                           preexec_fn=lambda: os.nice(10))
        if p.returncode != 0 or not report.exists():
            return None, f"rc={p.returncode}"
        findings = json.loads(report.read_text() or "[]")
        known = set(rels)
        base = os.path.realpath(batch)
        flagged: dict[str, str] = {}
        for f in findings if isinstance(findings, list) else []:
            try:
                rel = os.path.relpath(os.path.realpath(f.get("File") or ""), base)
            except ValueError:
                return None, "unmapped-finding"
            if rel not in known:
                return None, "unmapped-finding"
            if f.get("RuleID") in cfg.gitleaks_ignore_rules:
                continue
            flagged.setdefault(rel, f"gitleaks:{f.get('RuleID', '?')}")
        return flagged, ""
    except subprocess.TimeoutExpired:
        return None, "budget"
    except (OSError, ValueError) as exc:
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

    Every staged file gets the regex scan, which reads binary content too.  gitleaks runs as well when it
    is installed, and then the gate fails closed:  a text file gitleaks has not cleared (the wall-clock
    budget ran out, gitleaks failed on it) is taken out of this run and retried the next one.  Binary
    files (a NUL byte, SQLite) are the exception, because gitleaks does not read them;  the regex scan
    is their gate and the run log counts them (``regex_only``).  A file that passed is remembered by
    (path, size, mtime or content hash) in ``scan-cache.json``, so a steady-state run scans only what
    changed.  Dropped paths are recorded on the stage;  content is never echoed."""
    info = {"regex": True, "gitleaks": False, "gitleaks_error": "", "scanned": 0, "cached": 0,
            "gitleaks_files": 0, "gitleaks_pending": 0, "deferred": 0, "regex_only": 0, "gitleaks_unreadable": 0}
    fingerprint = scanner_fingerprint(cfg)
    cache = load_scan_cache(cfg, fingerprint)
    state: dict[str, dict] = {}
    todo: list[tuple[str, list]] = []
    for rel, item in list(stage.items.items()):
        if item.kind in ("bundle", "symlink", "meta"):
            continue
        try:
            st = os.stat(stage.path(rel))
            # A patch (or a binary read from git) is regenerated every run with a new mtime, so it is keyed by
            # content;  a cloned or snapshotted file keeps its mtime, so (size, mtime) is enough.
            key = [st.st_size, "h:" + file_digest(stage.path(rel))] if item.kind in ("patch", "binary") \
                else [st.st_size, st.st_mtime_ns]
        except OSError:
            continue
        ent = cache.get(rel)
        if isinstance(ent, list) and len(ent) == 4 and ent[:2] == key:
            state[rel] = {"key": key, "cacheable": True, "binary": bool(ent[3]), "gl": bool(ent[2])}
            info["cached"] += 1
        else:
            todo.append((rel, key))
    for rel, key in todo:
        path = stage.path(rel)
        item = stage.items[rel]
        hit = scan_file(path, cfg.scan_cap_bytes)
        info["scanned"] += 1
        cacheable = True
        if hit:
            if stage.permit(item.src, file_digest(path)):
                cacheable = False     # reviewed, so kept;  not cached, so taking the line out of allow.txt takes effect
            else:
                stage.drop(rel, f"content:{hit}")
                continue
        state[rel] = {"key": key, "cacheable": cacheable, "binary": item.kind == "sqlite" or is_binary_file(path),
                      "gl": False}
    if progress:
        progress(f"regex scan done:  {info['scanned']} scanned, {info['cached']} cached")
    gl = shutil.which(cfg.gitleaks_bin) if (use_gitleaks and cfg.gitleaks_bin) else None
    deferred_rels: set[str] = set()
    if gl:
        info["gitleaks"] = True
        info["regex_only"] = sum(1 for rel, s in state.items() if rel in stage.items and s["binary"])
        need = sorted(rel for rel, s in state.items() if rel in stage.items and not s["binary"] and not s["gl"])
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for rel in need:
            cur.append(rel)
            size += stage.items[rel].size
            if len(cur) >= 3000 or size >= 256 * 1024 * 1024:
                batches.append(cur)
                cur, size = [], 0
        if cur:
            batches.append(cur)
        deadline = time.time() + cfg.gitleaks_budget_s
        cleared: set[str] = set()
        flagged_all: dict[str, str] = {}
        failed: dict[str, str] = {}
        aborted = ""
        work = list(reversed(batches))
        while work:
            batch = work.pop()
            flagged, err = gitleaks_batch(cfg, stage, batch, deadline)
            if flagged is not None:
                cleared.update(batch)
                flagged_all.update(flagged)
                info["gitleaks_files"] += len(batch)
                if progress:
                    progress(f"gitleaks {info['gitleaks_files']}/{len(need)} files")
                continue
            if err in ("budget", "unmapped-finding"):
                aborted = err
                break
            if len(batch) == 1:       # gitleaks cannot read this one file:  it alone is left out
                failed[batch[0]] = err
                continue
            mid = len(batch) // 2     # bisect, so one bad file does not block every file after it
            work.append(batch[mid:])
            work.append(batch[:mid])
        for rel, reason in flagged_all.items():
            if rel not in stage.items:
                continue
            if stage.permit(stage.items[rel].src, file_digest(stage.path(rel))):
                state[rel]["cacheable"] = False
            else:
                stage.drop(rel, reason)
                state.pop(rel, None)
        for rel in cleared:
            if rel in state:
                state[rel]["gl"] = True
        for rel in need:
            if rel in stage.items and rel not in cleared:
                reason = "gitleaks-unreadable" if rel in failed else ("gitleaks-" + (aborted or "pending"))
                stage.defer(rel, reason)
                deferred_rels.add(rel)
        info["deferred"] = len(deferred_rels)
        info["gitleaks_pending"] = len(deferred_rels) if aborted == "budget" else 0
        info["gitleaks_unreadable"] = len(failed)
        info["gitleaks_error"] = aborted if aborted not in ("", "budget") else (f"{len(failed)}-files-unreadable" if failed else "")
    else:
        info["regex_only"] = sum(1 for rel in state if rel in stage.items)
    save_scan_cache(cfg, {rel: [s["key"][0], s["key"][1], s["gl"], s["binary"]] for rel, s in state.items()
                          if s["cacheable"] and (rel in stage.items or rel in deferred_rels)}, fingerprint)
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


def pressure_level(cfg: Config) -> Optional[int]:
    """macOS memory pressure:  1 normal, 2 warning, 4 critical.  None where the sysctl does not exist."""
    if cfg.pressure_fn:
        return cfg.pressure_fn()
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def pressure_reason(cfg: Config) -> Optional[str]:
    """Why this run should wait, or None.  Swap use alone is a poor signal on macOS (swap grows on demand and
    sat at 95% on an idle Mac), so a full swap counts only when memory pressure is at least warning.  Where
    the pressure sysctl does not exist, the swap rule stands alone."""
    la = load1(cfg)
    if la > cfg.load_limit:
        return f"load1={la:.1f}>{cfg.load_limit:g}"
    sw = swap_ratio(cfg)
    lvl = pressure_level(cfg)
    if lvl is not None and lvl >= 4:
        return f"memory-pressure={lvl}"
    swap_full = sw is not None and sw >= cfg.swap_limit
    if swap_full and (lvl is None or lvl >= 2):
        return f"swap={sw * 100:.0f}%>={cfg.swap_limit * 100:.0f}%" + (f",pressure={lvl}" if lvl is not None else "")
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
        try:
            if cfg.detail_log_path.stat().st_size > 8 * 1024 * 1024:   # keep the newest 4 MB
                data = cfg.detail_log_path.read_bytes()[-4 * 1024 * 1024:]
                cfg.detail_log_path.write_bytes(data[data.find(b"\n") + 1:])
        except OSError:
            pass
        with open(cfg.detail_log_path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {stamp(cfg.now())} ---\n")
            fh.writelines(l + "\n" for l in lines)
    except OSError:
        pass


# ------------------------------------------------------------------------------ removable volumes
CHECK_LABEL = "com.jay.fleet-work-backup.check"
JOB_LABEL = "com.jay.fleet-work-backup"
DEFAULT_INTERPRETER = "/usr/bin/python3"


def process_image() -> str:
    """The executable macOS privacy (TCC) sees for this process.  ``sys.executable`` is not it:  Homebrew's
    python3.14 is a small launcher that execs Python.app/Contents/MacOS/Python, and that is the image a
    Removable Volumes grant has to name."""
    if sys.platform == "darwin":
        try:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(4096)
            if lib.proc_pidpath(os.getpid(), buf, 4096) > 0:
                return os.fsdecode(buf.value)
        except (OSError, AttributeError):
            pass
    return os.path.realpath(sys.executable)


def app_bundle(path: str) -> str:
    """The innermost .app that contains ``path`` ('' when none):  System Settings lists a bundle by that name,
    and the helper app inside Xcode.app is its own bundle with its own grant."""
    m = re.match(r"^(.*\.app)(?:/|$)", path)
    return m.group(1) if m else ""


python_binary = process_image


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
    result = {"targets": [], "denied": 0, "missing": 0, "ok": 0, "binary": process_image(), "git": cfg.git_bin}
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


def removable_hint(cfg: Config, image: str = "") -> str:
    """The grant instruction.  ``image`` is the executable to name when it is not this process's own (the one-shot
    launchd job reports its own, which is what the schedule runs;  Terminal's interpreter can differ)."""
    img = image or process_image()
    app = app_bundle(img)
    who = f"{app} (the running executable is {img})" if app else img
    return (f"grant Removable Volumes (or Full Disk Access) to {who} in System Settings > Privacy & Security, "
            f"then run 'fleet_work_backup.py check-access --launchd' to see what a launchd job itself can read;  "
            f"git children inherit the grant, or set FLEET_WORK_BACKUP_GIT to a git binary that has it")


def job_command(cfg: Config) -> tuple[str, str]:
    """(interpreter, script) the scheduled job runs:  read from the installed LaunchAgent when there is one."""
    try:
        with open(cfg.home / "Library" / "LaunchAgents" / f"{JOB_LABEL}.plist", "rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments") or []
        if len(args) >= 2:
            return str(args[0]), str(args[1])
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    installed = cfg.install_dir / "fleet_work_backup.py"
    return DEFAULT_INTERPRETER, str(installed if installed.exists() else Path(__file__).resolve())


def launchctl(cfg: Config, *args: str) -> int:
    try:
        return subprocess.run([cfg.launchctl_bin, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 127


def check_via_launchd(cfg: Config) -> int:
    """Run ``check-access`` as a one-shot launchd job and report ITS result.  A command typed in Terminal
    inherits Terminal's privacy grants, so only a job launchd starts shows what the schedule can read.  The
    job uses a separate label, runs once, and is booted out again;  it never starts a backup."""
    if sys.platform != "darwin" and cfg.launchctl_bin == "launchctl":
        say("check-access --launchd is macOS only")
        return 2
    interp, script = job_command(cfg)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{CHECK_LABEL}"
    result = cfg.check_result_path
    out_log, err_log = cfg.state_dir / "check-access.out", cfg.state_dir / "check-access.err"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    for p in (result, out_log, err_log):
        try:
            p.unlink()
        except OSError:
            pass
    plist = {"Label": CHECK_LABEL, "ProgramArguments": [interp, script, "check-access", "--result", str(result)],
             "RunAtLoad": True, "ProcessType": "Standard", "StandardOutPath": str(out_log),
             "StandardErrorPath": str(err_log),
             "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                      "HOME": str(cfg.home)}}
    with open(cfg.check_plist_path, "wb") as fh:
        plistlib.dump(plist, fh)
    launchctl(cfg, "bootout", target)   # a leftover from an interrupted check
    say(f"starting a one-shot launchd job ({CHECK_LABEL}) with {interp}")
    rc = launchctl(cfg, "bootstrap", domain, str(cfg.check_plist_path))
    data = None
    if rc != 0:
        say(f"launchctl bootstrap failed (rc={rc})")
    else:
        deadline = time.time() + cfg.check_wait_s
        while time.time() < deadline:
            if result.exists() and result.stat().st_size:
                try:
                    data = json.loads(result.read_text())
                    break
                except ValueError:
                    pass
            time.sleep(0.5)
    launchctl(cfg, "bootout", target)
    try:
        cfg.check_plist_path.unlink()
    except OSError:
        pass
    if data is None:
        tail = ""
        try:
            tail = err_log.read_text().strip().splitlines()[-1][:160]
        except (OSError, IndexError):
            pass
        say(f"no result from the launchd job within {cfg.check_wait_s}s" + (f";  its stderr ends: {tail}" if tail else ""))
        return 2
    say(f"launchd job ran as: {data.get('binary')}")
    if data.get("image_app"):
        say(f"grant this bundle if a volume is denied: {data['image_app']}")
    for t in data.get("targets", []):
        say(f"{t['path']}: python={t['python']} git={t['git'] or '-'}")
    if not data.get("targets"):
        say("no lane symlinks point at /Volumes: nothing to check")
        return 0
    if data.get("denied"):
        say("DENIED under launchd: " + removable_hint(cfg, str(data.get("image") or data.get("binary") or "")))
        return 1
    if data.get("missing"):
        say("a volume is not mounted: its lanes will be skipped until it is")
        return 1
    say("ok under launchd:  the scheduled job can read the lanes")
    return 0


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


def restic_env(cfg: Config, allow_master: bool = False) -> tuple[Optional[dict], list[str]]:
    """Return (env for the restic subprocess, missing key NAMES).  Values never leave this process
    except in that env.  ``cfg.repository`` (FLEET_WORK_BACKUP_REPOSITORY) points restic at another
    repository, a local one for a test or a mirror, and then only the password is needed.

    The B2 master key is a fallback only for a restore (``restic`` and ``snapshots`` pass ``allow_master``) and
    only with FLEET_WORK_BACKUP_ALLOW_MASTER=1:  it has no file-name prefix, so it can delete the board's
    database replica in the same bucket, and a backup, an init or an install never uses it."""
    src = dict(read_handoff(cfg.handoff))
    for name in list(CRED_NAMES.values()) + list(MASTER_NAMES.values()):
        if os.environ.get(name):
            src[name] = os.environ[name]
    password = src.get(CRED_NAMES["password"], "")
    env = dict(os.environ)
    for var in ("RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY_FILE"):
        env.pop(var, None)
    if cfg.repository:
        if not password:
            return None, [CRED_NAMES["password"]]
        env.update({"RESTIC_REPOSITORY": cfg.repository, "RESTIC_PASSWORD": password})
        return env, []
    key_id = src.get(CRED_NAMES["key_id"], "")
    app_key = src.get(CRED_NAMES["app_key"], "")
    if (not key_id or not app_key) and allow_master and os.environ.get("FLEET_WORK_BACKUP_ALLOW_MASTER") == "1":
        key_id = key_id or src.get(MASTER_NAMES["key_id"], "")
        app_key = app_key or src.get(MASTER_NAMES["app_key"], "")
    bucket = src.get(CRED_NAMES["bucket"], "")
    missing = [CRED_NAMES[k] for k, v in (("bucket", bucket), ("key_id", key_id), ("app_key", app_key),
                                          ("password", password)) if not v]
    if missing:
        return None, missing
    region = re.sub(r"^s3\.|\.backblazeb2\.com$", "", cfg.s3_endpoint)
    env.update({
        "RESTIC_REPOSITORY": f"s3:https://{cfg.s3_endpoint}/{bucket}/fleet-work-backup",
        "RESTIC_PASSWORD": password,
        "AWS_ACCESS_KEY_ID": key_id,
        "AWS_SECRET_ACCESS_KEY": app_key,
        "AWS_DEFAULT_REGION": region,
    })
    return env, []


B2_API = "https://api.backblazeb2.com"
B2_PREFIX = "fleet-work-backup/"
B2_NEEDED_CAPS = {"listFiles", "readFiles", "writeFiles", "deleteFiles", "listBuckets"}
B2_TOO_STRONG_CAPS = {"writeKeys", "deleteKeys", "writeBuckets", "deleteBuckets", "bypassGovernance"}


def b2_call(cfg: Config, method: str, url: str, headers: dict, body: Optional[dict] = None) -> tuple[int, dict]:
    if cfg.b2_http:
        return cfg.b2_http(method, url, headers, body)
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, **({"Content-Type": "application/json"} if body is not None else {})},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def b2_key_problems(cfg: Config, key_id: str, app_key: str, bucket: str) -> list[tuple[bool, str]]:
    """What is wrong with the B2 key and bucket for this job, as (hard, text) pairs;  empty means ready.

    Hard:  the key is not limited to the one bucket and the ``fleet-work-backup/`` prefix (the bucket also holds the
    board's database replica), lacks a capability restic needs, holds one it must not (key or bucket admin), or a
    lifecycle rule hides files under the prefix by age (that would hide live restic packs).  Soft:  B2 could not be
    asked, or no lifecycle rule deletes hidden versions (``restic prune`` frees no space without one).  Read-only
    calls (b2_authorize_account and b2_list_buckets, API v4);  the texts name no key and no token."""
    basic = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
    try:
        st, auth = b2_call(cfg, "GET", f"{B2_API}/b2api/v4/b2_authorize_account", {"Authorization": "Basic " + basic})
    except (OSError, ValueError) as exc:
        return [(False, f"could not reach B2 to check the key ({type(exc).__name__})")]
    if st != 200:
        return [(st in (401, 403), f"B2 did not accept the key (status {st}, {str(auth.get('code', '?'))[:40]})")]
    storage = (auth.get("apiInfo") or {}).get("storageApi") or {}
    allowed = storage.get("allowed") or {}
    probs: list[tuple[bool, str]] = []
    prefix = allowed.get("namePrefix")
    if prefix != B2_PREFIX:
        probs.append((True, f"the key is not limited to the file-name prefix {B2_PREFIX} (its prefix is "
                            f"{'none' if not prefix else repr(prefix)});  create a bucket key with that prefix"))
    names = sorted(str(b.get("name")) for b in allowed.get("buckets") or [])
    if names != [bucket]:
        probs.append((True, f"the key is not limited to the one bucket {bucket} (it allows "
                            f"{', '.join(names) if names else 'every bucket'})"))
    caps = set(allowed.get("capabilities") or [])
    if B2_NEEDED_CAPS - caps:
        probs.append((True, "the key lacks " + ", ".join(sorted(B2_NEEDED_CAPS - caps))))
    if caps & B2_TOO_STRONG_CAPS:
        probs.append((True, "the key holds admin capabilities it must not have: " + ", ".join(sorted(caps & B2_TOO_STRONG_CAPS))))
    token, api = auth.get("authorizationToken"), storage.get("apiUrl")
    if "listBuckets" not in caps or not token or not api:
        return probs
    try:
        st, lb = b2_call(cfg, "POST", f"{api}/b2api/v4/b2_list_buckets", {"Authorization": token},
                         {"accountId": auth.get("accountId"), "bucketName": bucket})
    except (OSError, ValueError) as exc:
        return probs + [(False, f"could not read the bucket's lifecycle rules ({type(exc).__name__})")]
    found = (lb.get("buckets") or []) if st == 200 else []
    if st != 200 or not found:
        return probs + [(False, f"could not read the bucket's lifecycle rules (status {st})")]
    rules = [r for r in found[0].get("lifecycleRules") or [] if isinstance(r, dict)]
    covering = [r for r in rules if B2_PREFIX.startswith(r.get("fileNamePrefix") or "")]
    inside = [r for r in rules if (r.get("fileNamePrefix") or "").startswith(B2_PREFIX)]
    if any(r.get("daysFromUploadingToHiding") for r in covering + inside):
        probs.append((True, f"a lifecycle rule hides files under {B2_PREFIX} by age (daysFromUploadingToHiding):  it would "
                            f"hide live restic packs and break the repository;  remove that field"))
    if not any(int(r.get("daysFromHidingToDeleting") or 0) >= 1 for r in covering):
        probs.append((False, f"no lifecycle rule on {B2_PREFIX} deletes hidden versions (daysFromHidingToDeleting 7 to 30):  "
                             f"restic prune frees no space without it"))
    return probs


def check_b2_setup(cfg: Config) -> list[tuple[bool, str]]:
    """b2_key_problems for the key this job would use (never the master key).  A local repository needs none."""
    if cfg.repository:
        return []
    src = dict(read_handoff(cfg.handoff))
    for name in CRED_NAMES.values():
        if os.environ.get(name):
            src[name] = os.environ[name]
    key_id, app_key, bucket = (src.get(CRED_NAMES[k], "") for k in ("key_id", "app_key", "bucket"))
    if not (key_id and app_key and bucket):
        return [(True, "the bucket-scoped key or the bucket name is missing (" + ", ".join(
            CRED_NAMES[k] for k, v in (("key_id", key_id), ("app_key", app_key), ("bucket", bucket)) if not v) + ")")]
    return b2_key_problems(cfg, key_id, app_key, bucket)


def restic_key_count(cfg: Config, env: dict) -> Optional[int]:
    """How many keys open the repository (``restic key list --json``);  None when restic could not say."""
    try:
        p = run_restic(cfg, env, "key", "list", "--json", timeout=300)
        return len(json.loads(p.stdout or b"[]")) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


INFISICAL_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"     # "AI Fleet Coordinator";  prod is its only environment


def infisical_get(cfg: Config, name: str) -> tuple[int, str]:
    """(rc, value) of one Infisical secret.  The value stays in this process;  rc 0 with '' means it does not exist."""
    p = subprocess.run([cfg.infisical_bin, "secrets", "get", name, f"--projectId={INFISICAL_PROJECT}", "--env=prod",
                        "--plain", "--silent"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def cmd_new_password(cfg: Config, args) -> int:
    """Generate the repository password and write it to Infisical, never printing it.  Write-once:  it refuses when
    the handoff file, the environment or Infisical already has one (a replaced password locks every snapshot), and
    any read it cannot make is a refusal, never 'absent'.  The value reaches the CLI as a file path (``NAME=@file``,
    a 0600 file in a 0700 folder, removed afterwards), never in argv;  the CLI's output is discarded;  the value is
    read back and compared in memory, and only 'matched' and its length are reported."""
    name = CRED_NAMES["password"]
    if read_handoff(cfg.handoff).get(name) or os.environ.get(name):
        say(f"{name} is already in {cfg.handoff} or the environment:  the repository has its password;  "
            f"it is never replaced (README, Credentials).  Nothing written.")
        return 1
    if not os.access(cfg.infisical_bin, os.X_OK):
        say(f"the Infisical CLI is not at {cfg.infisical_bin} (FLEET_WORK_BACKUP_INFISICAL overrides)")
        return 2
    try:
        rc, cur = infisical_get(cfg, name)
    except (OSError, subprocess.TimeoutExpired) as exc:
        say(f"could not read Infisical ({type(exc).__name__});  nothing written")
        return 2
    if rc != 0:
        say(f"could not read {name} from Infisical (rc={rc});  is the CLI logged in?  Nothing written.")
        return 2
    if cur:
        say(f"{name} already exists in Infisical (length {len(cur)});  nothing written.  It is write-once:  a new "
            f"repository password would lock every snapshot (keys change only with restic key add and key remove).")
        return 1
    value = secrets.token_urlsafe(32)
    folder = tempfile.mkdtemp(prefix="fwb-pw-")
    try:
        path = os.path.join(folder, "value")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        p = subprocess.run([cfg.infisical_bin, "secrets", "set", f"{name}=@{path}", f"--projectId={INFISICAL_PROJECT}",
                            "--env=prod", "--type=shared", "--silent"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        say(f"the Infisical write did not run ({type(exc).__name__});  nothing written")
        return 2
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if p.returncode != 0:
        say(f"the Infisical write failed (rc={p.returncode});  nothing written")
        return 2
    try:
        rc, back = infisical_get(cfg, name)
    except (OSError, subprocess.TimeoutExpired):
        rc, back = -1, ""
    if rc != 0 or back != value:
        say(f"written, but the read-back did not match (rc={rc}, length {len(back)});  check {name} in the Infisical "
            f"UI before running init")
        return 3
    say(f"{name} written to Infisical (project AI Fleet Coordinator, prod) and read back:  matched, length {len(value)}.  "
        f"Never printed.  Next:  wait one infisical-secrets-sync tick, then init, then the recovery key (README, Install).")
    return 0


def restic_path(cfg: Config) -> Optional[str]:
    return shutil.which(cfg.restic_bin)


def _lower_priority() -> None:
    os.nice(10)


def run_restic(cfg: Config, env: dict, *args: str, timeout: int = 7200) -> subprocess.CompletedProcess:
    """restic at low CPU priority and two threads.  On a timeout, or when this process is interrupted, restic
    gets SIGINT first and a minute to release its repository lock;  a SIGKILL would leave a stale one."""
    env = {**env, "GOMAXPROCS": env.get("GOMAXPROCS", "2")}
    proc = subprocess.Popen([cfg.restic_bin, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=_lower_priority)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=60)
        except BaseException:
            proc.kill()
            proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def restic_backup(cfg: Config, env: dict, stage_root: Path, partial: bool = False) -> dict:
    tags = ["--tag", TAG] + (["--tag", "partial"] if partial else [])
    p = run_restic(cfg, env, "backup", "--json", *tags, "--host", cfg.host, "--ignore-inode",
                   "--ignore-ctime", str(stage_root))
    out = {"rc": p.returncode, "snapshot": "", "snapshot_full": "", "added": 0, "files": 0}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary":
            out["snapshot_full"] = str(msg.get("snapshot_id", ""))
            out["snapshot"] = out["snapshot_full"][:8]
            out["added"] = int(msg.get("data_added", 0) or 0)
            out["files"] = int(msg.get("total_files_processed", 0) or 0)
    if p.returncode != 0:
        out["error"] = (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["restic backup failed"])[-1][:160]
        out["wrong_password"] = b"wrong password" in p.stderr.lower()
    return out


def restic_maintenance(cfg: Config, env: dict, state: dict) -> dict:
    """forget on every run;  prune at most daily, check at most weekly (B2 transactions cost money).  Groups are
    host+tags, so partial snapshots (tagged ``partial``) are kept apart and never push complete ones out of
    the retention slots."""
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
        "bundle_files": sum(g.bundles + g.head_bundles + (1 if g.anchor_refs else 0) for g in groups),
        "anchor_refs": sum(g.anchor_refs for g in groups),
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
        "deferred": stage.deferred,
        "allowed": stage.allowed,
        "allow_stale": stage.allow_stale,
        "allow_unpinned": stage.allow_unpinned,
        "missing_volumes": disc.missing_volumes,
        "blocked": disc.blocked + stage.blocked,
        "notes": disc.notes + stage.notes,
        "gate": gate,
        "elapsed_s": round(time.time() - started, 1),
    }


def print_report(rep: dict) -> None:
    say("dry run report")
    print(f"  checkouts discovered     {rep['checkouts']}  (unreadable {rep['checkouts_unreadable']})")
    print(f"  repo groups (git dirs)   {rep['repo_groups']}  with work {rep['groups_with_work']}")
    print(f"  groups with unpushed     {rep['groups_with_unpushed']}  ({rep['unpushed_refs']} refs in {rep['bundle_files']} bundles;  "
          f"{rep['landed_refs_skipped']} landed refs skipped)")
    print(f"  groups with stashes      {rep['groups_with_stashes']}  ({rep['stashes']} stashes)")
    print(f"  anchor commits bundled   {rep['anchor_refs']}  (bases of stashes and dirty worktrees GitHub may not keep)")
    print(f"  dirty worktrees          {rep['worktrees_dirty']}  ({rep['patch_files']} patches or binary files, {rep['untracked_files']} untracked files)")
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
    print(f"  dropped by the gate      {len(rep['dropped'])} (paths only;  {len(rep['allowed'])} content hits allowed by allow.txt)")
    for d in rep["dropped"][:20]:
        print(f"    {d['reason']:<28} {d['path']}")
    print(f"  deferred (not cleared by gitleaks, retried next run)  {len(rep['deferred'])}")
    for d in rep["deferred"][:5]:
        print(f"    {d['reason']:<28} {d['path']}")
    if rep["allow_stale"] or rep["allow_unpinned"]:
        print(f"  allow.txt                {len(rep['allow_stale'])} entries stale (content changed), {len(rep['allow_unpinned'])} unpinned (ignored)")
    if rep["groups_no_remote"]:
        print(f"  repos with no remote (full history bundled): {', '.join(rep['groups_no_remote'][:10])}")
    if rep["missing_volumes"]:
        print(f"  lane links onto an unmounted volume: {len(rep['missing_volumes'])}")
    print(f"  blocked paths            {len(rep['blocked'])}")
    g = rep["gate"]
    print(f"  gate: regex scanned {g.get('scanned', 0)} files ({g.get('cached', 0)} cached);  gitleaks "
          f"{'on' if g.get('gitleaks') else 'off'}, {g.get('gitleaks_files', 0)} files cleared, {g.get('deferred', 0)} deferred, "
          f"{g.get('regex_only', 0)} regex-only {g.get('gitleaks_error', '')}")
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
    # A killed run can leave hard-linked batches and scratch object folders behind.
    # (Only ones over an hour old:  a dry run must not pull scratch out from under a live run.)
    for stale in list(cfg.cache_dir.glob("gl-*")) + list(cfg.cache_dir.glob("objs-*")):
        try:
            if time.time() - stale.stat().st_mtime < 3600:
                continue
            shutil.rmtree(stale, ignore_errors=True) if stale.is_dir() else stale.unlink(missing_ok=True)
        except OSError:
            pass
    stage = Stage(stage_root)
    stage.allow, stage.allow_unpinned = load_allow(cfg)
    git = Git(cfg)
    disc = discover(cfg)
    phase(f"discovered {len(disc.checkouts)} checkouts ({len(disc.blocked)} blocked)")
    with cf.ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        wts = list(pool.map(lambda p: inspect_worktree(cfg, git, p), disc.checkouts))
        # A linked worktree can live anywhere (~/.codex/worktrees);  git knows where, so ask it.
        known = {os.path.realpath(w.path) for w in wts}
        first_of: dict[str, str] = {}
        for w in wts:
            if not w.error and w.common_dir:
                first_of.setdefault(w.common_dir, w.path)
        extra: list[str] = []
        for common_dir, first in sorted(first_of.items()):
            found, gone = linked_worktrees(git, first)
            for path in gone:
                disc.notes.append(f"worktree-missing {path} (listed by {common_dir})")
            for path in found:
                real = os.path.realpath(path)
                if real not in known:
                    known.add(real)
                    extra.append(path)
        if extra:
            wts += list(pool.map(lambda p: inspect_worktree(cfg, git, p), extra))
            phase(f"found {len(extra)} linked worktrees outside the scanned folders")
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
    phase(f"gate done, {len(stage.dropped)} dropped, {len(stage.deferred)} deferred, {fmt_bytes(stage.total())} left")
    return stage, disc, wts, groups, file_count, gate, started


def cmd_run(cfg: Config, args) -> int:
    held: list[int] = []
    prev_alarm = signal.getsignal(signal.SIGALRM)
    try:
        return _run(cfg, args, held)
    except Exception as exc:  # never a silent run:  one log line, the stage removed, a traceback in the detail log
        if args.dry_run:
            raise
        log_not_ok(cfg, status="error", reason=f"exception:{type(exc).__name__}")
        detail_log(cfg, ["exception " + line for line in traceback.format_exc().splitlines()])
        shutil.rmtree(cfg.stage, ignore_errors=True)
        return 5
    finally:
        if cfg.max_runtime and not args.dry_run:
            signal.alarm(0)                      # a return path that never reached the restic step
            signal.signal(signal.SIGALRM, prev_alarm)
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass


def bump_not_ok(state: dict) -> int:
    """Count one more run with no complete snapshot (an error, a partial snapshot, a pressure or low-disk skip).
    Every such path goes through here, so the warning and the alert hang off one counter."""
    n = int(state.get("consecutive_not_ok", 0)) + 1
    state["consecutive_not_ok"] = n
    return n


def agent_sync_path(cfg: Config) -> str:
    return cfg.agent_sync_bin or str(cfg.home / ".local" / "bin" / "agent-sync")


def post_alert(cfg: Config, text: str) -> bool:
    """Post to the fleet's Zulip through agent-sync as this job's seat.  Never raises;  False when it did not post."""
    exe = agent_sync_path(cfg)
    if not os.access(exe, os.X_OK):
        return False
    cmd = [exe, "post", "--as", cfg.alert_seat, "--session", "fwbackup", "--topic", cfg.alert_topic]
    for who in cfg.alert_to:
        cmd += ["--to", who]
    cmd.append("-")
    try:
        p = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def alert_after_line(cfg: Config, state: dict, line: str) -> None:
    """After ``alert_after`` runs in a row with no complete snapshot, say so in Zulip, then at most once a day while
    it lasts;  the first good run after an alert says it is over.  The run line carries names and counts only."""
    n = int(state.get("consecutive_not_ok", 0))
    last = float(state.get("last_alert", 0) or 0)
    gap = "\u00a0 "              # Zulip renders Markdown:  a sentence gap is U+00A0 plus a space
    if n == 0:
        if last:
            post_alert(cfg, f"fleet-work-backup on {cfg.host} is back to a complete snapshot.{gap}`{line}`")
            state.pop("last_alert", None)
        return
    if n < cfg.alert_after or (last and cfg.now() - last < ALERT_EVERY):
        return
    text = (f"fleet-work-backup on {cfg.host}:  {n} runs in a row with no complete snapshot (last complete:  "
            f"{state.get('last_ok_text', 'never')}).{gap}Latest run line:  `{line}`.{gap}The detail log is "
            f"~/Library/Logs/fleet-work-backup.detail.log, and README.md in scripts/fleet-work-backup says what each "
            f"reason and warning means.")
    if post_alert(cfg, text):
        state["last_alert"] = cfg.now()
    else:
        detail_log(cfg, [f"alert-not-posted after {n} runs (agent-sync at {agent_sync_path(cfg)} missing or failed)"])


def log_not_ok(cfg: Config, **fields) -> str:
    """A log line for a run that produced no good snapshot, with the running count and the last good one."""
    state = read_state(cfg)
    n = bump_not_ok(state)
    warn = [w for w in str(fields.pop("warn", "") or "").split(",") if w]
    if n >= SKIP_WARN_AFTER:
        warn.append("no-complete-backup-for-about-24h")
    line = log_line(cfg, **fields, not_ok_in_a_row=n if n > 1 else "", warn=",".join(warn),
                    last_ok=state.get("last_ok_text", "never"))
    alert_after_line(cfg, state, line)
    try:
        write_state(cfg, state)
    except OSError:
        pass
    return line


def _run(cfg: Config, args, held: list[int]) -> int:
    t0 = time.time()
    dry = args.dry_run
    if getattr(args, "skip_gitleaks", False) and not dry:
        log_not_ok(cfg, status="error", reason="skip-gitleaks-is-for-a-dry-run")
        return 2
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
            bump_not_ok(state)
            warn = "no-backup-for-about-24h" if state["consecutive_skips"] >= SKIP_WARN_AFTER else ""
            line = log_line(cfg, status="skip", reason=why, consecutive=state["consecutive_skips"], warn=warn,
                            last_ok=state.get("last_ok_text", "never"))
            alert_after_line(cfg, state, line)
            write_state(cfg, state)
            return 0
        if not restic_path(cfg):
            log_not_ok(cfg, status="error", reason="restic-missing", hint="brew install restic")
            return 2
        env, missing = restic_env(cfg)
        if env is None:
            log_not_ok(cfg, status="error", reason="missing-credentials", names=",".join(missing))
            return 2
        if not disk_ok(cfg):
            state = read_state(cfg)
            n = bump_not_ok(state)
            line = log_line(cfg, status="skip", reason="low-disk", need=fmt_bytes(cfg.min_free_bytes),
                            not_ok_in_a_row=n if n > 1 else "", last_ok=state.get("last_ok_text", "never"))
            alert_after_line(cfg, state, line)
            write_state(cfg, state)
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
        shutil.rmtree(stage_root, ignore_errors=True)
        log_not_ok(cfg, status="error", reason="timeout", limit_s=cfg.max_runtime)
        return 3
    rep = build_report(cfg, disc, wts, groups, stage, file_count, gate, t0)
    manifest = {k: rep[k] for k in ("tool_version", "checkouts", "repo_groups", "groups_with_work", "unpushed_refs",
                                    "stashes", "patch_files", "untracked_files", "non_git_files", "staged_files",
                                    "staged_bytes", "by_kind")}
    manifest["created"] = stamp(cfg.now())
    manifest["dropped_count"] = len(stage.dropped)
    manifest["deferred_count"] = len(stage.deferred)
    (stage_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    (stage_root / "README-RESTORE.txt").write_text(
        "Restore runbook: scripts/fleet-work-backup/RESTORE.md in AI-Fleet-Coordinator.\n"
        "files/ mirrors absolute paths (copy files/Users/jay back to /Users/jay).  git/<repo>/ holds\n"
        "bundles/*.bundle, anchors.bundle (the commits the patches apply to), stashes/<n>/ and\n"
        "worktrees/<name>/ (patches/, binary/, untracked/, head.bundle).\n")
    detail = []
    for d in stage.dropped:
        detail.append(f"dropped {d['reason']} {d['path']}")
        if d.get("digest") and not d["reason"].startswith("secret-name"):
            detail.append(f"  allow-line {d['path']} @ {d['digest']}")
    detail += [f"deferred {d['reason']} {d['path']}" for d in stage.deferred]
    detail += [f"allow-stale {p} (its content changed since it was reviewed;  not applied)" for p in stage.allow_stale]
    detail += [f"allow-unpinned {l} (no '@ digest';  ignored)" for l in stage.allow_unpinned]
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
    deferred = len(stage.deferred)
    if not gate.get("gitleaks"):
        gate_name = "regex-only"
    else:
        gate_name = "gitleaks-error" if gate.get("gitleaks_error") else "gitleaks"
    common = dict(checkouts=rep["checkouts"], groups=rep["repo_groups"], work=rep["groups_with_work"],
                  unpushed=rep["unpushed_refs"], stashes=rep["stashes"], dirty=rep["worktrees_dirty"],
                  files=rep["staged_files"], staged=fmt_bytes(rep["staged_bytes"]), dropped=len(stage.dropped),
                  deferred=deferred, regex_only=gate.get("regex_only", 0), gate=gate_name,
                  large=len(stage.large), blocked=blocked)
    warns: list[str] = []
    grant = ""
    if blocked:
        if any(b.get("errno") in (errno.EPERM, errno.EACCES) for b in rep["blocked"]):
            warns.append("removable-volumes-denied")
            grant = process_image()
            detail.append("hint " + removable_hint(cfg))
        else:
            warns.append("paths-unreadable")
    if disc.missing_volumes:
        warns.append("volume-missing")
        detail.append(f"hint {len(disc.missing_volumes)} lane links point onto a volume that is not mounted:  "
                      f"{disc.missing_volumes[0]}, ...  Their work is not in this snapshot.")
    if deferred:
        warns.append("gitleaks-error" if gate.get("gitleaks_error") else "gitleaks-deferred")
    if gate_name == "regex-only":
        warns.append("gitleaks-missing")
    if any(n.startswith("bundle-scan-partial") for n in rep["notes"]):
        warns.append("bundle-scan-partial")
    if stage.allow_stale:
        warns.append("allow-entry-stale")
    if stage.allow_unpinned:
        warns.append("allow-entry-unpinned")
    partial = bool(blocked or disc.missing_volumes or deferred)
    detail_log(cfg, detail)
    try:
        res = restic_backup(cfg, env, stage_root, partial=partial)
    except (OSError, subprocess.TimeoutExpired, Timeout) as exc:
        log_not_ok(cfg, status="error", reason=f"restic-{type(exc).__name__}", **common)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 4
    finally:
        if cfg.max_runtime:
            signal.alarm(0)
    state = read_state(cfg)
    unreadable = res["rc"] == 3 and bool(res["snapshot"])   # restic made the snapshot but could not read some files
    if res["rc"] != 0 and not unreadable:
        log_not_ok(cfg, status="error", reason="wrong-password" if res.get("wrong_password") else "restic-backup-failed",
                   detail=res.get("error", "")[:120], **common)
        shutil.rmtree(stage_root, ignore_errors=True)
        return 4
    if unreadable:
        warns.append("restic-unreadable-files")
        if not partial:      # a snapshot restic could not fully read is partial too:  tag it so retention keeps it apart
            try:
                run_restic(cfg, env, "tag", "--add", "partial", res["snapshot_full"], timeout=300)
            except (OSError, subprocess.TimeoutExpired):
                pass
    status = "partial" if (partial or unreadable) else "ok"
    # Retention runs only after a complete snapshot:  a degraded one (a volume gone, a path denied, files deferred)
    # never decides what is forgotten or pruned, so a week of lane-less runs cannot age out the last good state.
    maint = {"forget": "skipped-partial", "prune": "-", "check": "-"}
    if status == "ok":
        try:
            maint = restic_maintenance(cfg, env, state)
        except (OSError, subprocess.TimeoutExpired):
            maint = {"forget": "fail", "prune": "-", "check": "-"}
    state["consecutive_skips"] = 0
    state["last_success"] = cfg.now()
    state["last_success_text"] = stamp(cfg.now())
    if status == "ok":
        state["consecutive_not_ok"] = 0
        state["last_ok"] = cfg.now()
        state["last_ok_text"] = stamp(cfg.now())
    else:
        if bump_not_ok(state) >= SKIP_WARN_AFTER:
            warns.append("no-complete-backup-for-about-24h")
    line = log_line(cfg, status=status, snapshot=res["snapshot"] or "?", added=fmt_bytes(res["added"]),
                    forget=maint["forget"], prune=maint["prune"], check=maint["check"],
                    elapsed=f"{time.time() - t0:.0f}s", warn=",".join(warns), grant=grant,
                    not_ok_in_a_row=state["consecutive_not_ok"] if state.get("consecutive_not_ok", 0) > 1 else "",
                    last_ok=(state.get("last_ok_text", "never") if status != "ok" else ""), **common)
    alert_after_line(cfg, state, line)
    write_state(cfg, state)
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
    probs = check_b2_setup(cfg)
    for hard, text in probs:
        say(("REFUSED:  " if hard else "warning:  ") + text)
    if any(hard for hard, _ in probs):
        say("not initialising:  fix the B2 key or bucket first (README, Where it goes)")
        return 2
    p = run_restic(cfg, env, "init", timeout=300)
    say("restic init " + ("ok" if p.returncode == 0 else "failed: " + (p.stderr.decode().strip().splitlines() or [""])[-1][:160]))
    return p.returncode


def cmd_snapshots(cfg: Config, args) -> int:
    env, missing = restic_env(cfg, allow_master=True)
    if env is None or not restic_path(cfg):
        say("restic or credentials missing: " + ", ".join(missing or ["restic"]))
        return 2
    p = run_restic(cfg, env, "snapshots", "--tag", TAG, "--compact", timeout=300)
    sys.stdout.write(p.stdout.decode("utf-8", "replace"))
    return p.returncode


def cmd_restic(cfg: Config, args) -> int:
    """Run restic with this job's repository and credentials (for restores and checks)."""
    env, missing = restic_env(cfg, allow_master=True)
    if env is None or not restic_path(cfg):
        say("restic or credentials missing: " + ", ".join(missing or ["restic"]))
        return 2
    rest = [a for a in args.restic_args if a != "--"]
    return subprocess.call([cfg.restic_bin, *rest], env=env)


def cmd_image(cfg: Config, args) -> int:
    """Print the executable macOS privacy settings see for this interpreter (what a grant has to name)."""
    img = process_image()
    print(img)
    app = app_bundle(img)
    if app:
        print(f"app bundle: {app}")
    print(f"sys.executable: {sys.executable}")
    return 0


def cmd_check_access(cfg: Config, args) -> int:
    if getattr(args, "launchd", False):
        return check_via_launchd(cfg)
    res = probe_external(cfg)
    res["image"] = process_image()
    res["image_app"] = app_bundle(res["image"])
    res["xpc_service"] = os.environ.get("XPC_SERVICE_NAME", "")
    if getattr(args, "result", None):
        try:
            tmp = Path(args.result + ".tmp")
            tmp.write_text(json.dumps(res))
            os.replace(tmp, args.result)
        except OSError:
            pass
    say(f"interpreter image: {res['image']}")
    say(f"git: {res['git']}")
    from_terminal = res["xpc_service"] in ("", "0") or sys.stdin.isatty()
    if from_terminal:
        say("note: this command runs with the privacy grants of the terminal you typed it in, so 'ok' here does not "
            "show what the scheduled job can read;  use 'check-access --launchd' for that")
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
    say("ok" + (" (as this terminal)" if from_terminal else ""))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Back up the fleet's non-GitHub work to restic on B2.")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="one backup (default)")
    run.add_argument("--dry-run", action="store_true", help="stage and report, upload nothing")
    run.add_argument("--json", help="write the report as JSON to this path")
    run.add_argument("--keep-stage", action="store_true", help="dry run: leave the staged tree in place")
    run.add_argument("--ignore-pressure", action="store_true", help="run even when load or swap is high")
    run.add_argument("--skip-gitleaks", action="store_true", help="dry run only:  regex scan alone (a faster report)")
    sub.add_parser("init", help="restic init (never done by a scheduled run)")
    ca = sub.add_parser("check-access", help="report whether /Volumes lanes are readable here")
    ca.add_argument("--launchd", action="store_true", help="run the check as a one-shot launchd job and report its result")
    ca.add_argument("--result", help=argparse.SUPPRESS)
    sub.add_parser("image", help="print the executable macOS privacy settings see for this interpreter")
    sub.add_parser("new-password", help="generate the repository password into Infisical, never printing it (once)")
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
            "restic": cmd_restic, "image": cmd_image, "new-password": cmd_new_password}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
