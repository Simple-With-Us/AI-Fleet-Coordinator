"""Source generators for the fleet-agents corpus.

Each `iter_*` function yields `Doc` records (one per source document) that the ingest
orchestrator chunks, scrubs, embeds and upserts.  Generators never write anywhere and never
print; they raise on a hard failure so the orchestrator can record the source as errored.

Sources (payload `source` value):
  board          ~/apps/mac-collab/findings.db   (read-only sqlite; findings + comments)
  effort-log     ~/apps/*-EFFORT-LOG.md + EFFORT-LOG-PROTOCOL.md
  doc            markdown in fleet app repos (README/AGENTS/STATUS/CLAUDE + docs/**/*.md),
                 AI-Fleet-Coordinator, fleet-ops, top-level ~/apps/*.md (not effort logs),
                 ~/.claude/CLAUDE.md, ~/.grok/docs/**/*.md, ~/.grok/skills/**/SKILL.md
  skill          ~/.claude/skills/*/SKILL.md and ~/.cursor/skills/*/SKILL.md
                 (doc_id skill/<tree>/<name>; byte-identical copies collapse to the first tree)
  memory         ~/.claude/projects/*/memory/*.md and ~/.codex/memories/*.md
  apple-note     the iCloud "Coding" folder via notes_export (falls back to the on-disk cache
                 when Notes.app is unavailable and records a warning via `take_warnings`)
  chat-log       parsed agent transcripts (user + assistant text only) from Claude, Grok,
                 Cursor, Codex, and Gemini jsonl; doc_id chat/<platform>/<id>[#partN]

Generators cannot reach the ingest report directly, so a non-fatal degradation (the notes
fallback) is appended to the module-level `WARNINGS` list; the orchestrator drains it with
`take_warnings()` after each source and copies the messages into the run report.
"""
from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

HOME = pathlib.Path.home()
APPS_DIR = HOME / "apps"
CODE_DIR = HOME / "Code"
BOARD_DB = APPS_DIR / "mac-collab" / "findings.db"
BOARD_URL = "https://mac.jays.services/board"
FLEET_REPO = CODE_DIR / "AI-Fleet-Coordinator"
FLEET_OPS_REPO = CODE_DIR / "fleet-ops"
GROK_HOME = HOME / ".grok"
GROK_SESSIONS = GROK_HOME / "sessions"
CURSOR_PROJECTS = HOME / ".cursor" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
GEMINI_HOME = HOME / ".gemini"
EXTRA_DOCS = (APPS_DIR / "AGENT-SYNC.md", APPS_DIR / "MAC-LOCAL-PROCESSES.md",
              APPS_DIR / "FLEET-UI-COPY.md", HOME / ".claude" / "CLAUDE.md")
SKILL_DIRS = (HOME / ".claude" / "skills", HOME / ".cursor" / "skills", GROK_HOME / "skills")
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_MEMORIES = HOME / ".codex" / "memories"
DOC_MAX_BYTES = 400 * 1024
CHAT_MAX_CHARS = 80_000
TURN_MAX_CHARS = 24_000
JSONL_LINE_MAX = 200_000

# Restricted walk for product app repos (fleet coordinator / fleet-ops keep a broader walk).
DOC_ROOT_NAMES = ("README.md", "AGENTS.md", "STATUS.md", "CLAUDE.md")
DOC_SKIP_DIR_NAMES = frozenset({
    "node_modules", ".git", "backups", "dist", "build", "vendor", ".secrets",
})
DOC_APP_REPOS = (
    "Socratic.Trade", "Congress.Trade", "congress-trading-shared",
    "API-usage-monitor", "Usage-Monitor", "DealDex", "Personal-Site",
    "BotFleet", "BotFleet/openmausbot", "openmausbot",
    "ContactLogo", "AutoRotate", "Autorotate",
    "AI-Fleet-Coordinator", "fleet-ops", "agentic-trading",
)

# Doc-walk mirror rules (2026-09-02).  The same text used to be ingested several times over:
# the live ~/apps copy AND the AI-Fleet-Coordinator mirror of a protocol file, every by-seat /
# universal / dot-dir copy of a skill on top of the skill source, and each repo's
# docs/EFFORT-LOG.md mirror on top of the effort-log source.  mirror_skip_reason() names the
# rule that drops a file; iter_docs logs a per-rule summary through warn().
SKILL_TREE_DIRS = frozenset({".claude", ".cursor", ".grok"})
SKIP_APPS_MIRROR = "apps-mirror"            # (a) live ~/apps copy is canonical
SKIP_SKILL_COPY = "skill-copy"              # (b) covered by the skill source
SKIP_EFFORT_LOG = "effort-log-mirror"       # (c) covered by the effort-log source
DOC_SKIPS: list[tuple[str, str]] = []


def take_doc_skips() -> list[tuple[str, str]]:
    """Return and clear the (rule, path) pairs the last doc walk skipped."""
    out = list(DOC_SKIPS)
    DOC_SKIPS.clear()
    return out


SOURCES = ("board", "effort-log", "doc", "skill", "memory", "apple-note", "chat-log")
# Nightly / `ingest --all`.  chat-log is an infrequent owner-policy scan, not a lesson dump
# (owner 2026-09-02).  Pass `--source chat-log` explicitly.
NIGHTLY_SOURCES = ("board", "effort-log", "doc", "skill", "memory", "apple-note")

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def take_warnings() -> list[str]:
    """Return and clear the warnings accumulated since the last call."""
    out = list(WARNINGS)
    WARNINGS.clear()
    return out

# App slug normalisation shared by every source.
APP_ALIASES = {
    "api-usage-monitor": "usage-monitor", "usage-monitor": "usage-monitor", "um": "usage-monitor",
    "socratic-trade": "socratic-trade", "st": "socratic-trade",
    "congress-trade": "congress-trade", "ct": "congress-trade",
    "congress-shared": "congress-trading-shared", "congress-trading-shared": "congress-trading-shared",
    "cts": "congress-trading-shared",
    "fleet": "fleet", "fleet-infra": "fleet", "ai-fleet-coordinator": "fleet", "afl": "fleet",
    "fleet-ops": "fleet-ops",
    "botfleet": "botfleet", "bf": "botfleet", "openmausbot": "botfleet",
    "dealdex": "dealdex", "dd": "dealdex",
    "contactlogo": "contactlogo", "cl": "contactlogo",
    "autorotate": "autorotate", "ar": "autorotate",
    "personal-site": "personal-site", "ps": "personal-site",
    "trading": "trading", "agentic-trading": "trading", "trading-live": "trading",
    "clutch": "clutch", "ck": "clutch", "minimax-remote": "clutch", "harness": "clutch",
}

SEAT_ALIASES = {
    "grok": "GROK", "monet": "MONET", "claude": "CLAUDE", "codex": "CODEX", "antigravity": "AG",
    "ag": "AG", "cursor": "CURSOR", "kimi": "KIMI", "owner": "OWNER", "jay": "OWNER",
    "gemini": "AG", "deepseek": "DSH", "dsh": "DSH", "renoir": "RENOIR", "fleet": "FLEET",
    "minimax": "MM", "mm": "MM",
}


@dataclass
class Doc:
    doc_id: str
    title: str
    text_markdown: str
    source: str
    app: str
    category: str
    seat: str
    url: str = ""
    path: str = ""
    created_at_ms: int = 0
    updated_at_ms: int = 0
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers

def app_slug(raw: str | None, default: str = "fleet") -> str:
    if not raw:
        return default
    key = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not key:
        return default
    return APP_ALIASES.get(key, key)


def seat_tag(raw: str | None, default: str = "FLEET") -> str:
    if not raw:
        return default
    tok = re.split(r"[\s(/,]+", raw.strip())[0]
    tok = re.sub(r"[^A-Za-z0-9_\-]", "", tok)
    if not tok:
        return default
    return SEAT_ALIASES.get(tok.lower(), tok.upper())


def parse_ts_ms(value: str | None) -> int:
    """ISO-8601 (with or without zone, with or without fractional seconds) to epoch ms, else 0."""
    if not value:
        return 0
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(v)
    except ValueError:
        return 0
    if d.tzinfo is None:
        d = d.astimezone()          # naive stamps are local (Notes, git-less files)
    return int(d.timestamp() * 1000)


def mtime_ms(path: pathlib.Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def first_heading(md: str, fallback: str) -> str:
    for line in md.splitlines()[:40]:
        m = re.match(r"^#{1,3}\s+(.*?)\s*#*\s*$", line)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return fallback


_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def frontmatter(md: str) -> tuple[dict[str, str], str]:
    """Very small YAML-ish frontmatter reader: flat `key: value` lines at any indent."""
    m = _FRONT.match(md)
    if not m:
        return {}, md
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_\-]*):\s*(.*?)\s*$", line)
        if km and km.group(2):
            meta.setdefault(km.group(1).lower(), km.group(2).strip().strip('"').strip("'"))
    return meta, md[m.end():]


# --------------------------------------------------------------------------- git helpers

_git_cache: dict[str, dict] = {}


# Filesystem identity helpers (2026-09-02).  On a case-insensitive volume (APFS default) the
# spellings AutoRotate and Autorotate name ONE directory, but pathlib.resolve() keeps whatever
# spelling it was given, so path-keyed dedupe walked the checkout twice and doc_from_file could
# not make the file relative to git's (canonical) toplevel.  Dedupe on (st_dev, st_ino) and
# spell every repo path the way the disk does.

def fs_key(p: pathlib.Path) -> tuple:
    """Identity of the file behind `p`: (st_dev, st_ino), falling back to the resolved path.

    Raises OSError when the path does not exist (callers decide whether to skip it).
    """
    try:
        st = os.stat(p)
        if st.st_ino:
            return (st.st_dev, st.st_ino)
    except OSError:
        pass
    return ("path", os.fspath(p.resolve(strict=True)))


@functools.lru_cache(maxsize=8192)
def _dir_names(d: str) -> tuple[str, ...]:
    try:
        return tuple(os.listdir(d))
    except OSError:
        return ()


def _canonical_name(parent: str, name: str) -> str:
    """The on-disk spelling of `name` inside `parent` (case-insensitive match verified by samefile)."""
    names = _dir_names(parent)
    if name in names or not names:
        return name
    target = os.path.join(parent, name)
    fold = name.casefold()
    for n in names:
        if n.casefold() == fold:
            try:
                if os.path.samefile(os.path.join(parent, n), target):
                    return n
            except OSError:
                continue
    return name


@functools.lru_cache(maxsize=8192)
def _canonical_dir(d: str) -> str:
    parent, name = os.path.split(d)
    if not name or parent == d:
        return d
    cparent = _canonical_dir(parent)
    return os.path.join(cparent, _canonical_name(cparent, name))


def canonical_path(p: pathlib.Path | str, *, resolve: bool = True) -> pathlib.Path:
    """`p` with every component spelled the way the disk spells it on a case-insensitive
    volume (AutoRotate/README.md -> Autorotate/README.md).  Symlinks are resolved unless
    resolve=False, which only fixes the spelling of the absolute path as given."""
    rp = pathlib.Path(p).resolve() if resolve else pathlib.Path(os.path.abspath(p))
    parent, name = os.path.split(os.fspath(rp))
    if not name:
        return rp
    cparent = _canonical_dir(parent)
    return pathlib.Path(cparent) / _canonical_name(cparent, name)


def _git(args: list[str], cwd: pathlib.Path) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", errors="replace")


def repo_info(path: pathlib.Path) -> dict | None:
    """{"top", "owner", "repo", "branch", "dates": {relpath: epoch_s}} for the repo holding path."""
    start = path if path.is_dir() else path.parent
    top = _git(["rev-parse", "--show-toplevel"], start)
    if not top:
        return None
    # Canonical on-disk spelling, so a case-variant `path` (AutoRotate vs Autorotate on APFS)
    # keys one cache entry and doc_from_file can make it relative to this top.
    top = os.fspath(canonical_path(top.strip()))
    if top in _git_cache:
        return _git_cache[top]
    info: dict = {"top": top, "owner": "", "repo": "", "branch": "main", "dates": {}}
    origin = (_git(["remote", "get-url", "origin"], pathlib.Path(top)) or "").strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", origin)
    if m:
        info["owner"], info["repo"] = m.group(1), m.group(2)
    head = (_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], pathlib.Path(top)) or "").strip()
    if head.startswith("origin/"):
        info["branch"] = head[len("origin/"):]
    # One log walk gives the latest commit time of every path (first occurrence wins).
    log = _git(["log", "--format=%ct", "--name-only", "--", "."], pathlib.Path(top)) or ""
    cur = 0
    for line in log.splitlines():
        if not line.strip():
            continue
        if re.fullmatch(r"\d+", line.strip()):
            cur = int(line.strip())
        elif line not in info["dates"]:
            info["dates"][line] = cur
    _git_cache[top] = info
    return info


def blob_url(info: dict | None, rel: str) -> str:
    if not info or not info["owner"]:
        return ""
    return f"https://github.com/{info['owner']}/{info['repo']}/blob/{info['branch']}/{rel}"


# --------------------------------------------------------------------------- board

_EFFORT_KEY = re.compile(r"<!--\s*effort-key:[^>]*-->\s*")


def _board_markdown(row: dict, comments: list[dict]) -> str:
    def clean(v) -> str:  # noqa: ANN001
        # The effort-board writeback stamps issue bodies with `<!-- effort-key: <sha1> -->`;
        # it is machine metadata and gitleaks reads it as a credential, so drop it.
        return _EFFORT_KEY.sub("", v).strip() if isinstance(v, str) else ("" if v is None else str(v))

    parts = [f"# {clean(row.get('title')) or 'Untitled finding'}", ""]
    meta = [f"Status: {clean(row.get('status')) or 'open'}"]
    if clean(row.get("severity")):
        meta.append(f"Severity: {clean(row['severity'])}")
    meta.append(f"App: {clean(row.get('app'))}")
    if clean(row.get("source_kind")):
        meta.append(f"Kind: {clean(row['source_kind'])}")
    if clean(row.get("category")):
        meta.append(f"Category: {clean(row['category'])}")
    if clean(row.get("surface")):
        meta.append(f"Surface: {clean(row['surface'])}")
    parts.append("- " + " · ".join(meta))
    people = []
    if clean(row.get("reported_by")):
        people.append(f"Reported by: {clean(row['reported_by'])}")
    if clean(row.get("addressed_by")):
        people.append(f"Addressed by: {clean(row['addressed_by'])}")
    if people:
        parts.append("- " + " · ".join(people))
    where = []
    for k, label in (("source", "Source"), ("repo", "Repo"), ("location", "Location"),
                     ("env", "Env"), ("source_url", "Link")):
        if clean(row.get(k)):
            where.append(f"{label}: {clean(row[k])}")
    if where:
        parts.append("- " + " · ".join(where))
    parts.append(f"- Board id: {clean(row.get('id'))}")
    for k, label in (("description", "Description"), ("recommended_fix", "Recommended fix"),
                     ("resolution", "Resolution")):
        if clean(row.get(k)):
            parts += ["", f"## {label}", "", clean(row[k])]
    if comments:
        parts += ["", "## Comments"]
        for c in comments:
            parts += ["", f"### {clean(c.get('author')) or '?'} — {clean(c.get('created_at'))}", "",
                      clean(c.get("text"))]
    return "\n".join(parts).rstrip() + "\n"


def iter_board(db_path: pathlib.Path | str = BOARD_DB, limit: int | None = None) -> Iterator[Doc]:
    db_path = pathlib.Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"board db not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        comments: dict[str, list[dict]] = {}
        for c in con.execute("SELECT finding_id, author, text, created_at FROM comments "
                             "ORDER BY created_at, rowid"):
            comments.setdefault(c["finding_id"], []).append(dict(c))
        q = "SELECT * FROM findings ORDER BY created_at, rowid"
        if limit:
            q += f" LIMIT {int(limit)}"
        for r in con.execute(q):
            row = dict(r)
            cs = comments.get(row["id"], [])
            created = parse_ts_ms(row.get("created_at"))
            updated = max([parse_ts_ms(row.get("updated_at"))] + [parse_ts_ms(c["created_at"]) for c in cs])
            resolved = bool((row.get("resolution") or "").strip())
            app = app_slug(row.get("app"))
            yield Doc(
                doc_id=f"board/{row['id']}",
                title=(row.get("title") or "").strip() or f"Finding {row['id'][:8]}",
                text_markdown=_board_markdown(row, cs),
                source="board", app=app,
                category="lesson" if resolved else "finding",
                seat=seat_tag(row.get("addressed_by") or row.get("reported_by")),
                url=f"{BOARD_URL}?app={app}",
                path=str(db_path),
                created_at_ms=created or updated,
                updated_at_ms=updated or created,
                extra={"status": row.get("status"), "severity": row.get("severity"),
                       "source_kind": row.get("source_kind")},
            )
    finally:
        con.close()


# --------------------------------------------------------------------------- effort logs

def iter_effort_logs(apps_dir: pathlib.Path | str = APPS_DIR, limit: int | None = None) -> Iterator[Doc]:
    apps_dir = pathlib.Path(apps_dir)
    files = sorted(apps_dir.glob("*-EFFORT-LOG.md"))
    proto = apps_dir / "EFFORT-LOG-PROTOCOL.md"
    if proto.exists():
        files.append(proto)
    n = 0
    for p in files:
        if limit and n >= limit:
            break
        stem = p.stem
        if stem == "EFFORT-LOG-PROTOCOL":
            key, app = "PROTOCOL", "fleet"
        else:
            key = stem[: -len("-EFFORT-LOG")]
            app = app_slug(key)
        md = read_text(p)
        ts = mtime_ms(p)
        n += 1
        yield Doc(doc_id=f"effort-log/{key}", title=first_heading(md, p.name), text_markdown=md,
                  source="effort-log", app=app, category="lesson", seat="FLEET", url="",
                  path=str(p), created_at_ms=ts, updated_at_ms=ts)


# --------------------------------------------------------------------------- docs

def _skip_doc(p: pathlib.Path) -> bool:
    parts = set(p.parts)
    if parts & DOC_SKIP_DIR_NAMES:
        return True
    if "reviews" in p.parts and "raw" in p.name.lower():
        return True
    try:
        if p.stat().st_size > DOC_MAX_BYTES:
            return True
    except OSError:
        return True
    return False


def _is_effort_log_name(name: str) -> bool:
    return name == "EFFORT-LOG.md" or name.endswith("-EFFORT-LOG.md") or name == "EFFORT-LOG-PROTOCOL.md"


def _under(p: pathlib.Path, roots: Iterable[pathlib.Path]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            rp.relative_to(pathlib.Path(r).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def mirror_skip_reason(p: pathlib.Path, *, fleet_repos: Iterable[pathlib.Path] = (),
                       live_basenames: Iterable[str] = ()) -> str | None:
    """Why the doc walk drops this file, or None to keep it.

    (a) SKIP_APPS_MIRROR: a file inside the fleet coordinator checkout whose basename is also a
        top-level ~/apps/*.md (AGENT-SYNC.md, MAC-LOCAL-PROCESSES.md, FLEET-UI-COPY.md, ...).
        The live copy is canonical.  Generic root names (README.md, AGENTS.md, ...) are exempt.
    (b) SKIP_SKILL_COPY: any file under .claude/skills, .cursor/skills or .grok/skills, anything
        under docs/fleet-skills/by-seat, and any SKILL.md under a skills/ or fleet-skills/ dir.
        The skill source ingests ~/.claude/skills, ~/.cursor/skills and ~/.grok/skills.
    (c) SKIP_EFFORT_LOG: EFFORT-LOG.md, *-EFFORT-LOG.md and EFFORT-LOG-PROTOCOL.md anywhere.
        The effort-log source ingests the live boards.
    """
    name = p.name
    parts = p.parts
    if _is_effort_log_name(name):
        return SKIP_EFFORT_LOG
    dirs = parts[:-1]
    for i, part in enumerate(dirs[:-1]):
        if part in SKILL_TREE_DIRS and dirs[i + 1] == "skills":
            return SKIP_SKILL_COPY
    if "fleet-skills" in dirs and "by-seat" in dirs:
        return SKIP_SKILL_COPY
    if name == "SKILL.md" and ("skills" in dirs or "fleet-skills" in dirs):
        return SKIP_SKILL_COPY
    live = set(live_basenames)
    if live and name in live and name not in DOC_ROOT_NAMES and _under(p, fleet_repos):
        return SKIP_APPS_MIRROR
    return None


def _collect_repo_docs(repo: pathlib.Path, *, root_all_md: bool) -> list[pathlib.Path]:
    """Markdown from one checkout.  `root_all_md` keeps the coordinator/ops broad walk.

    Skill trees (.claude/skills, skills/) are deliberately not walked here: the skill source
    owns SKILL.md content (see mirror_skip_reason rule b).
    """
    if not repo.exists() or not repo.is_dir():
        return []
    files: list[pathlib.Path] = []
    if root_all_md:
        files += sorted(repo.glob("*.md"))
        files += sorted((repo / "docs").rglob("*.md"))
        return files
    for name in DOC_ROOT_NAMES:
        cand = repo / name
        if cand.is_file():
            files.append(cand)
    docs_dir = repo / "docs"
    if docs_dir.is_dir():
        files += sorted(docs_dir.rglob("*.md"))
    return files


def _unique_paths(files: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    """First occurrence of each file by filesystem identity (fs_key): the coordinator is walked
    twice (as fleet_repo and as a DOC_APP_REPOS entry) and a case-variant spelling of one
    directory on a case-insensitive volume is the same file."""
    seen: set[tuple] = set()
    out: list[pathlib.Path] = []
    for f in files:
        try:
            key = fs_key(f)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _dedupe_docs(files: Iterable[pathlib.Path]) -> list[pathlib.Path]:
    seen: set[tuple] = set()
    out: list[pathlib.Path] = []
    for f in files:
        try:
            if not f.is_file() or _skip_doc(f):
                continue
            key = fs_key(f)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _unique_repo_dirs(code_dir: pathlib.Path,
                      names: Iterable[str] = DOC_APP_REPOS) -> list[pathlib.Path]:
    """The distinct checkouts named by `names`, in order, each spelled as on disk.

    Two entries that are one directory (os.path.samefile: AutoRotate / Autorotate on APFS, or
    a symlink) walk once.  Missing entries are dropped.
    """
    out: list[pathlib.Path] = []
    for name in names:
        cand = code_dir / name
        if not cand.is_dir():
            continue
        if any(_samefile(cand, prev) for prev in out):
            continue
        out.append(canonical_path(cand, resolve=False))
    return out


def _samefile(a: pathlib.Path, b: pathlib.Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _doc_files(fleet_repo: pathlib.Path, fleet_ops: pathlib.Path,
               extra: Iterable[pathlib.Path],
               code_dir: pathlib.Path | None = None,
               apps_dir: pathlib.Path | None = None,
               grok_home: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every markdown file the doc source ingests, mirror rules applied (see mirror_skip_reason).

    Skipped files are appended to DOC_SKIPS as (rule, path); iter_docs turns them into one
    warning per rule.
    """
    files: list[pathlib.Path] = []
    files += _collect_repo_docs(fleet_repo, root_all_md=True)
    files += _collect_repo_docs(fleet_ops, root_all_md=True)
    fleet_repos = [fleet_repo]
    if code_dir is not None:
        code_dir = pathlib.Path(code_dir)
        fleet_repos.append(code_dir / "AI-Fleet-Coordinator")
        for repo_dir in _unique_repo_dirs(code_dir):
            files += _collect_repo_docs(repo_dir, root_all_md=False)
    live: list[pathlib.Path] = [pathlib.Path(x) for x in extra if pathlib.Path(x).exists()]
    if apps_dir is not None:
        apps = pathlib.Path(apps_dir)
        if apps.is_dir():
            live += sorted(apps.glob("*.md"))
    files += live
    if grok_home is not None:
        grok = pathlib.Path(grok_home)
        docs = grok / "docs"
        if docs.is_dir():
            files += sorted(docs.rglob("*.md"))
    live_basenames = {p.name for p in live if p.is_file() and not _is_effort_log_name(p.name)}
    kept: list[pathlib.Path] = []
    for f in _unique_paths(files):
        reason = mirror_skip_reason(f, fleet_repos=fleet_repos, live_basenames=live_basenames)
        if reason:
            DOC_SKIPS.append((reason, str(f)))
            continue
        kept.append(f)
    return _dedupe_docs(kept)


def doc_from_file(p: pathlib.Path, source: str = "doc", category: str | None = None,
                  app: str = "fleet", seat: str = "FLEET", doc_prefix: str = "doc") -> Doc:
    info = repo_info(p)
    rel = ""
    if info:
        try:
            rel = str(canonical_path(p).relative_to(info["top"]))
        except (ValueError, OSError):
            rel = ""
    if info and rel:
        doc_id = f"{doc_prefix}/{info['repo'] or pathlib.Path(info['top']).name}/{rel}"
        url = blob_url(info, rel)
        created = info["dates"].get(rel, 0) * 1000 or mtime_ms(p)
    else:
        try:
            home_rel = str(p.resolve().relative_to(HOME))
        except ValueError:
            home_rel = str(p.resolve()).lstrip("/")
        doc_id = f"{doc_prefix}/local/{home_rel}"
        url = ""
        created = mtime_ms(p)
    md = read_text(p)
    cat = category or ("runbook" if p.name == "SKILL.md" else "doc")
    return Doc(doc_id=doc_id, title=first_heading(md, p.name), text_markdown=md, source=source,
               app=app, category=cat, seat=seat, url=url, path=str(p),
               created_at_ms=created, updated_at_ms=max(mtime_ms(p), created))


def iter_docs(fleet_repo: pathlib.Path | str = FLEET_REPO, fleet_ops: pathlib.Path | str = FLEET_OPS_REPO,
              extra: Iterable[pathlib.Path] = EXTRA_DOCS, limit: int | None = None,
              code_dir: pathlib.Path | str | None = CODE_DIR,
              apps_dir: pathlib.Path | str | None = APPS_DIR,
              grok_home: pathlib.Path | str | None = GROK_HOME) -> Iterator[Doc]:
    ops = pathlib.Path(fleet_ops)
    DOC_SKIPS.clear()
    files = _doc_files(
        pathlib.Path(fleet_repo), ops, extra,
        code_dir=None if code_dir is None else pathlib.Path(code_dir),
        apps_dir=None if apps_dir is None else pathlib.Path(apps_dir),
        grok_home=None if grok_home is None else pathlib.Path(grok_home),
    )
    _log_doc_skips()
    for i, p in enumerate(files):
        if limit and i >= limit:
            break
        yield doc_from_file(p, app=app_from_path(p, fleet_ops=ops))


def _log_doc_skips() -> None:
    """One warning per mirror rule: count plus up to three example paths (home-relative)."""
    by_rule: dict[str, list[str]] = {}
    for rule, path in DOC_SKIPS:
        by_rule.setdefault(rule, []).append(path)
    for rule in (SKIP_APPS_MIRROR, SKIP_SKILL_COPY, SKIP_EFFORT_LOG):
        paths = by_rule.get(rule)
        if not paths:
            continue
        examples = ", ".join(_home_rel(x) for x in paths[:3])
        more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
        warn(f"doc: skipped {len(paths)} {rule} file(s): {examples}{more}")


def _home_rel(path: str) -> str:
    try:
        return "~/" + str(pathlib.Path(path).relative_to(HOME))
    except ValueError:
        return path


# --------------------------------------------------------------------------- skills

def skill_tree(skill_dir: pathlib.Path) -> str:
    """'~/.claude/skills' -> 'claude', '~/.cursor/skills' -> 'cursor', '/x/claude' -> 'claude'."""
    d = pathlib.Path(skill_dir)
    name = d.parent.name if d.name == "skills" and d.parent.name else d.name
    tree = re.sub(r"[^A-Za-z0-9_\-]+", "-", name.lstrip(".")).strip("-").lower()
    return tree or "local"


def iter_skills(skill_dirs: Iterable[pathlib.Path] = SKILL_DIRS, limit: int | None = None) -> Iterator[Doc]:
    """One Doc per SKILL.md.  doc_id is unique per tree (skill/claude/<name>, skill/cursor/<name>);
    a byte-identical copy in a later tree is skipped (the first tree wins) so the same content is
    not chunked twice under two ids."""
    seen_hash: set[str] = set()
    n = 0
    for d in skill_dirs:
        d = pathlib.Path(d)
        if not d.exists():
            continue
        tree = skill_tree(d)
        for p in sorted(d.glob("*/SKILL.md")):
            if limit and n >= limit:
                return
            md = read_text(p)
            h = hashlib.sha256(md.encode("utf-8")).hexdigest()
            if h in seen_hash:
                continue
            seen_hash.add(h)
            meta, _ = frontmatter(md)
            name = meta.get("name") or p.parent.name
            ts = mtime_ms(p)
            n += 1
            yield Doc(doc_id=f"skill/{tree}/{p.parent.name}", title=name, text_markdown=md, source="skill",
                      app="fleet", category="runbook", seat="FLEET", url="", path=str(p),
                      created_at_ms=ts, updated_at_ms=ts)


# --------------------------------------------------------------------------- memory

_MEMORY_TYPES = {"user": "preference", "feedback": "preference", "project": "lesson",
                 "reference": "infrastructure"}


def project_slug(dirname: str) -> str:
    s = dirname
    for prefix in ("-Users-jay-", "-Users-jay"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "home"


def app_from_project(slug: str) -> str:
    for key in ("congress-trading-shared", "socratic-trade", "congress-trade", "usage-monitor",
                "api-usage-monitor", "botfleet", "openmausbot", "dealdex", "autorotate", "contactlogo",
                "personal-site", "agentic-trading", "trading-live", "trading", "ai-fleet-coordinator",
                "fleet-ops"):
        if key in slug:
            return app_slug(key)
    return "fleet"


def app_from_path(path: pathlib.Path, fleet_ops: pathlib.Path | str | None = None) -> str:
    """Infer an app slug from a filesystem path (repo folder, Claude project dir, session cwd)."""
    try:
        resolved = pathlib.Path(path).resolve()
    except OSError:
        resolved = pathlib.Path(path)
    if fleet_ops:
        try:
            ops = pathlib.Path(fleet_ops).resolve()
            if resolved == ops or ops in resolved.parents:
                return "fleet-ops"
        except OSError:
            if "fleet-ops" in resolved.parts:
                return "fleet-ops"
    slug = project_slug("-".join(resolved.parts))
    return app_from_project(slug)


def _memory_doc(p: pathlib.Path, seat: str, doc_id: str, app: str) -> Doc:
    md = read_text(p)
    meta, _ = frontmatter(md)
    cat = _MEMORY_TYPES.get((meta.get("type") or "").lower(), "lesson")
    title = meta.get("name") or first_heading(md, p.stem)
    ts = mtime_ms(p)
    upd = parse_ts_ms(meta.get("modified")) or ts
    return Doc(doc_id=doc_id, title=title, text_markdown=md, source="memory", app=app, category=cat,
               seat=seat, url="", path=str(p), created_at_ms=min(ts, upd) or ts, updated_at_ms=max(ts, upd))


def iter_memory(claude_projects: pathlib.Path | str = CLAUDE_PROJECTS,
                codex_memories: pathlib.Path | str = CODEX_MEMORIES, limit: int | None = None) -> Iterator[Doc]:
    n = 0
    cp = pathlib.Path(claude_projects)
    if cp.exists():
        for mem_dir in sorted(cp.glob("*/memory")):
            slug = project_slug(mem_dir.parent.name)
            for p in sorted(mem_dir.glob("*.md")):
                if p.name == "MEMORY.md":
                    continue
                if limit and n >= limit:
                    return
                n += 1
                yield _memory_doc(p, "CLAUDE", f"memory/claude/{slug}/{p.name}", app_from_project(slug))
    cm = pathlib.Path(codex_memories)
    if cm.exists():
        for p in sorted(cm.glob("*.md")):
            if limit and n >= limit:
                return
            n += 1
            yield _memory_doc(p, "CODEX", f"memory/codex/{p.name}", "fleet")


# --------------------------------------------------------------------------- apple notes

_BRACKET = re.compile(r"^\s*\[([^\]]+)\]")


def parse_note_title(title: str) -> tuple[str, str]:
    """'[UM, ST, Grok] topic' -> (app, seat).  Multi-app takes the first app."""
    m = _BRACKET.match(title or "")
    app, seat = "fleet", "FLEET"
    if not m:
        return app, seat
    toks = [t.strip() for t in re.split(r"[,/]", m.group(1)) if t.strip()]
    apps: list[str] = []
    for t in toks:
        low = t.lower()
        if low in SEAT_ALIASES and low != "fleet":
            seat = SEAT_ALIASES[low]
        elif re.match(r"^gb-", low):
            seat = t.upper()
        else:
            apps.append(t)
    if apps:
        app = app_slug(apps[0])
    return app, seat


def iter_apple_notes(records: Iterable[dict] | None = None, limit: int | None = None, log=None) -> Iterator[Doc]:
    """Docs from `notes_export.export_notes()` (or pre-fetched records for tests).

    When Notes.app cannot be driven (`NotesUnavailable`) the cached records are served instead;
    the error is logged and recorded as a run warning so the report shows the degradation.
    """
    if records is None:
        from . import notes_export
        from .core import eprint
        log = log or eprint
        try:
            records = notes_export.export_notes(log=log)
        except notes_export.NotesUnavailable as e:
            records = list(notes_export.iter_cached())
            msg = f"apple-note: Notes unavailable ({e}); served {len(records)} cached notes"
            log(f"[apple-note] WARNING {msg}")
            warn(msg)
    from .notes_export import strip_title_line
    n = 0
    for rec in records:
        title = (rec.get("name") or "").strip()
        text = strip_title_line(rec.get("text") or "", title)
        if len(text.strip()) < 20:
            continue                      # empty "New Note" shells
        if limit and n >= limit:
            return
        n += 1
        app, seat = parse_note_title(title)
        created = parse_ts_ms(rec.get("created"))
        updated = parse_ts_ms(rec.get("modified")) or created
        nid = rec.get("id", "")
        yield Doc(doc_id="note/" + nid.replace("x-coredata://", ""), title=title or "Untitled note",
                  text_markdown=text, source="apple-note", app=app, category="note", seat=seat,
                  url="", path="", created_at_ms=created or updated, updated_at_ms=updated)


# --------------------------------------------------------------------------- chat logs

CHAT_SEATS = {
    "claude": "CLAUDE", "grok": "GROK", "cursor": "CURSOR", "codex": "CODEX",
    "gemini": "AG",
}
CHAT_SKIP_NAMES = frozenset({
    "events.jsonl", "updates.jsonl", "rewind_points.jsonl", "signals.json",
    "journal.jsonl", "system_prompt.txt", "permission.toml", "permissions.toml",
    "summary.json", "prompt_context.json", "announcement_state.json",
    "resources_state.json", "transcript_full.jsonl",
})
CHAT_SKIP_TYPES = frozenset({
    "queue-operation", "mode", "permission-mode", "attachment", "progress",
    "system", "file-history-snapshot", "bridge-session", "last-prompt",
    "tool_result", "tool-result", "reasoning", "function_call",
    "function_call_output", "turn_context", "session_meta", "event_msg",
    "GENERIC",
})
_TEXT_BLOCK_TYPES = frozenset({
    "text", "input_text", "output_text", "inputtext", "outputtext",
})
_SKIP_BLOCK_TYPES = frozenset({
    "tool_use", "tool_result", "tool-result", "tool_call", "function_call",
    "function_call_output", "image", "image_url", "thinking", "reasoning",
    "redacted_thinking",
})
_SECRET_TOKEN_RE = re.compile(r"(ghp_|github_pat_|sk-ant-|xoxb-|xoxp-|xoxa-)")
_OWNER_RULING_RE = re.compile(
    r"(?i)\b(?:owner\s+(?:ruling|preference|directive)|owner\s+prefers|"
    r"owner\s+said|jay\s+(?:said|wants)|binding\s+for\s+every|"
    r"do\s+not\s+treat\s+this\s+as|from\s+now\s+on|canonical:)\b"
)
_INFRA_POLICY_RE = re.compile(
    r"(?i)\b(?:qdrant|tei-bge|coolify|infisical|tailscale|cx43|"
    r"fleet-agents|embed(?:ding)?\s+space|ingest\.lock)\b"
)
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_MODE_ONLY_RE = re.compile(
    r"^(?:plan|default|acceptEdits|bypassPermissions|dontAsk|don't ask)$", re.I
)
_GROK_MD_HEAD = re.compile(
    r"(?im)^(?:#{1,3}\s*)?(?:\*\*)?(user|assistant|human|grok|owner)(?:\*\*)?\s*:?\s*$"
)


def _secret_path(path: pathlib.Path) -> bool:
    return ".secrets" in path.parts or path.name.endswith(".lock")


def _drop_secret_lines(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not _SECRET_TOKEN_RE.search(ln))


def _text_from_content(content: object, *, budget: int = TURN_MAX_CHARS) -> str:
    """Pull user-visible text from str | list[blocks] | dict.  Skip tool payloads."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        t = str(content.get("type") or "").lower()
        if t in _SKIP_BLOCK_TYPES or t.endswith("tool_result") or t.endswith("tool_use"):
            return ""
        text = content.get("text")
        if isinstance(text, str) and (not t or t in _TEXT_BLOCK_TYPES):
            return text.strip()
        if "content" in content:
            return _text_from_content(content["content"], budget=budget)
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        size = 0
        for item in content:
            piece = _text_from_content(item, budget=budget)
            if not piece:
                continue
            parts.append(piece)
            size += len(piece)
            if size > budget:
                break
        return "\n".join(parts).strip()
    return ""


def _is_noise_turn(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if _MODE_ONLY_RE.fullmatch(t):
        return True
    if t.startswith("<command-name>") or t.startswith("<local-command"):
        return True
    if t.startswith("Caveat: The messages below were generated"):
        return True
    return False


def _looks_like_ruling(text: str) -> bool:
    return bool(_OWNER_RULING_RE.search(text))


def _iter_jsonl(path: pathlib.Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(line) > JSONL_LINE_MAX:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _add_turn(turns: list[tuple[str, str]], role: str, text: str) -> None:
    text = _drop_secret_lines(text).strip()
    if _is_noise_turn(text) or len(text) > TURN_MAX_CHARS:
        return
    turns.append((role, text))


def _docs_from_turns(turns: list[tuple[str, str]], *, platform: str, stable_id: str,
                     path: pathlib.Path, app: str) -> Iterator[Doc]:
    """Owner 2026-09-02: chat mining is a rare infra/policy scan, not a lesson dump.

    Only user turns that look like an owner ruling or infra/policy shift become Docs.
    Agents contribute lessons themselves via recall_contribute; transcripts hide the
    token-waste that made the lesson.
    """
    if not turns:
        return
    ts = mtime_ms(path)
    n = 0
    for role, text in turns:
        if role != "user":
            continue
        if not _looks_like_ruling(text):
            continue
        body = text.strip()
        if len(body) < 20:
            continue
        n += 1
        cat = "infrastructure" if _INFRA_POLICY_RE.search(body) else "preference"
        title = first_heading(body, f"{platform} owner ruling")
        if len(title) > 80:
            title = title[:77].rstrip() + "..."
        yield Doc(
            doc_id=f"chat/{platform}/{stable_id}#ruling{n}",
            title=title, text_markdown=body + "\n", source="chat-log", app=app,
            category=cat, seat="OWNER", url="", path=str(path),
            created_at_ms=ts, updated_at_ms=ts,
            extra={"platform": platform, "ruling": n},
        )


def _parse_claude_jsonl(path: pathlib.Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for obj in _iter_jsonl(path):
        t = str(obj.get("type") or "")
        if t in CHAT_SKIP_TYPES:
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        role = str(msg.get("role") or t or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = _text_from_content(msg.get("content"))
        _add_turn(turns, role, text)
    return turns


def _parse_grok_jsonl(path: pathlib.Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for obj in _iter_jsonl(path):
        t = str(obj.get("type") or "").lower()
        if t in CHAT_SKIP_TYPES or t in {"reasoning", "tool_result", "system"}:
            continue
        if t not in ("user", "assistant"):
            continue
        text = _text_from_content(obj.get("content"))
        _add_turn(turns, t, text)
    return turns


def _parse_grok_transcript_md(path: pathlib.Path) -> list[tuple[str, str]]:
    try:
        raw = read_text(path)
    except OSError:
        return []
    turns: list[tuple[str, str]] = []
    role = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal role, buf
        if role and buf:
            mapped = "user" if role in ("user", "human", "owner") else "assistant"
            _add_turn(turns, mapped, "\n".join(buf))
        role, buf = "", []

    for line in raw.splitlines():
        m = _GROK_MD_HEAD.match(line)
        if m:
            flush()
            role = m.group(1).lower()
            continue
        if role:
            buf.append(line)
    flush()
    return turns


def _parse_cursor_jsonl(path: pathlib.Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for obj in _iter_jsonl(path):
        role = str(obj.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        text = _text_from_content(msg.get("content") if isinstance(msg, dict) else None)
        _add_turn(turns, role, text)
    return turns


def _parse_codex_jsonl(path: pathlib.Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for obj in _iter_jsonl(path):
        if str(obj.get("type") or "") != "response_item":
            continue
        pl = obj.get("payload")
        if not isinstance(pl, dict):
            continue
        if str(pl.get("type") or "") not in ("message", ""):
            continue
        role = str(pl.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        text = _text_from_content(pl.get("content"))
        _add_turn(turns, role, text)
    return turns


def _parse_gemini_jsonl(path: pathlib.Path) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    for obj in _iter_jsonl(path):
        typ = str(obj.get("type") or obj.get("role") or "")
        if typ in CHAT_SKIP_TYPES:
            continue
        if typ in ("USER_INPUT", "user"):
            raw = obj.get("content")
            text = raw if isinstance(raw, str) else _text_from_content(raw)
            m = _USER_REQUEST_RE.search(text)
            if m:
                text = m.group(1)
            text = re.sub(r"<ADDITIONAL_METADATA>.*", "", text, flags=re.S)
            _add_turn(turns, "user", text)
        elif typ in ("PLANNER_RESPONSE", "MODEL_RESPONSE", "assistant", "ASSISTANT"):
            raw = obj.get("content")
            if not raw:
                continue
            text = raw if isinstance(raw, str) else _text_from_content(raw)
            text = text.replace("&nbsp;", " ")
            _add_turn(turns, "assistant", text)
        else:
            role = str(obj.get("role") or "").lower()
            if role in ("user", "assistant"):
                _add_turn(turns, role, _text_from_content(obj.get("content") or obj.get("message")))
    return turns


def _yield_chat(path: pathlib.Path, platform: str, stable_id: str, app: str) -> Iterator[Doc]:
    if _secret_path(path) or path.name in CHAT_SKIP_NAMES:
        return
    parsers = {
        "claude": _parse_claude_jsonl,
        "grok": _parse_grok_jsonl,
        "cursor": _parse_cursor_jsonl,
        "codex": _parse_codex_jsonl,
        "gemini": _parse_gemini_jsonl,
    }
    parse = parsers.get(platform)
    if parse is None:
        return
    turns = parse(path)
    yield from _docs_from_turns(turns, platform=platform, stable_id=stable_id, path=path, app=app)


def _claude_chat_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*.jsonl")):
        if p.name in CHAT_SKIP_NAMES or _secret_path(p):
            continue
        if "memory" in p.parts:
            continue
        yield p


def _grok_chat_files(root: pathlib.Path) -> Iterator[tuple[pathlib.Path, str]]:
    if not root.exists():
        return
    seen: set[pathlib.Path] = set()
    for hist in sorted(root.rglob("chat_history.jsonl")):
        if _secret_path(hist) or hist.name in CHAT_SKIP_NAMES:
            continue
        seen.add(hist.parent)
        yield hist, hist.parent.name
    for md in sorted(root.rglob("transcript.md")):
        if _secret_path(md) or md.parent in seen:
            continue
        yield md, md.parent.name


def _cursor_chat_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*.jsonl")):
        if "agent-transcripts" not in p.parts:
            continue
        if p.name in CHAT_SKIP_NAMES or _secret_path(p):
            continue
        yield p


def _codex_chat_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    if not root.exists():
        return
    for p in sorted(root.rglob("*.jsonl")):
        if p.name in CHAT_SKIP_NAMES or _secret_path(p):
            continue
        yield p


def _gemini_chat_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    if not root.exists():
        return
    seen: set[pathlib.Path] = set()
    for p in sorted(root.rglob("*.jsonl")):
        if "chunks" in p.parts or p.name in CHAT_SKIP_NAMES or _secret_path(p):
            continue
        if p.name == "history.jsonl":
            continue
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        # Prefer transcript.jsonl over other logs in the same folder.
        if p.name != "transcript.jsonl" and (p.parent / "transcript.jsonl").is_file():
            continue
        seen.add(rp)
        yield p


def _gemini_stable_id(path: pathlib.Path) -> str:
    parts = path.parts
    if "brain" in parts:
        i = parts.index("brain")
        if i + 1 < len(parts):
            return parts[i + 1]
    if "conversations" in parts:
        return path.stem
    return path.stem


def iter_chat_logs(claude_projects: pathlib.Path | str | None = CLAUDE_PROJECTS,
                   grok_sessions: pathlib.Path | str | None = GROK_SESSIONS,
                   cursor_projects: pathlib.Path | str | None = CURSOR_PROJECTS,
                   codex_sessions: pathlib.Path | str | None = CODEX_SESSIONS,
                   gemini_home: pathlib.Path | str | None = GEMINI_HOME,
                   limit: int | None = None) -> Iterator[Doc]:
    """User + assistant turns from local agent transcripts.  Roots are injectable for tests."""
    n = 0

    def emit(docs: Iterable[Doc]) -> Iterator[Doc]:
        nonlocal n
        for d in docs:
            if limit and n >= limit:
                return
            n += 1
            yield d

    if claude_projects is not None:
        root = pathlib.Path(claude_projects)
        for p in _claude_chat_files(root):
            if limit and n >= limit:
                return
            app = app_from_path(p)
            yield from emit(_yield_chat(p, "claude", p.stem, app))
    if grok_sessions is not None:
        root = pathlib.Path(grok_sessions)
        for p, stable in _grok_chat_files(root):
            if limit and n >= limit:
                return
            app = app_from_path(p)
            if p.suffix == ".md":
                turns = _parse_grok_transcript_md(p)
                yield from emit(_docs_from_turns(
                    turns, platform="grok", stable_id=stable, path=p, app=app))
            else:
                yield from emit(_yield_chat(p, "grok", stable, app))
    if cursor_projects is not None:
        root = pathlib.Path(cursor_projects)
        for p in _cursor_chat_files(root):
            if limit and n >= limit:
                return
            app = app_from_path(p)
            yield from emit(_yield_chat(p, "cursor", p.stem, app))
    if codex_sessions is not None:
        root = pathlib.Path(codex_sessions)
        for p in _codex_chat_files(root):
            if limit and n >= limit:
                return
            app = app_from_path(p)
            yield from emit(_yield_chat(p, "codex", p.stem, app))
    if gemini_home is not None:
        root = pathlib.Path(gemini_home)
        for p in _gemini_chat_files(root):
            if limit and n >= limit:
                return
            app = app_from_path(p)
            yield from emit(_yield_chat(p, "gemini", _gemini_stable_id(p), app))


# --------------------------------------------------------------------------- registry

GENERATORS: dict[str, Callable[..., Iterator[Doc]]] = {
    "board": iter_board,
    "effort-log": iter_effort_logs,
    "doc": iter_docs,
    "skill": iter_skills,
    "memory": iter_memory,
    "apple-note": iter_apple_notes,
    "chat-log": iter_chat_logs,
}
