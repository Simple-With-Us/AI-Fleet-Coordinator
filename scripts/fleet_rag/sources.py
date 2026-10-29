"""Source generators for the fleet-agents corpus.

Each `iter_*` function yields `Doc` records (one per source document) that the ingest
orchestrator chunks, scrubs, embeds and upserts.  Generators never write anywhere and never
print; they raise on a hard failure so the orchestrator can record the source as errored.

Sources (payload `source` value):
  board          ~/apps/_retired/mac-collab-20261019/findings.db   (frozen read-only sqlite; findings + comments)
  linear         the live board: Linear issues through the installed `simple-board` CLI (read-only
                 GraphQL), so board work filed after the Oct-18 freeze reaches the corpus
  effort-log     ~/apps/*-EFFORT-LOG.md + EFFORT-LOG-PROTOCOL.md
  doc            markdown in fleet app repos (README/AGENTS/STATUS/CLAUDE + docs/**/*.md),
                 AI-Fleet-Coordinator, fleet-ops (its handbook/ pages too, private ones only when an
                 FLEET-PROTOCOL.md is ingested:  the public ones are the text of that file),
                 top-level ~/apps/*.md (not effort logs),
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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from .core import FleetRagError

HOME = pathlib.Path.home()
APPS_DIR = HOME / "apps"
CODE_DIR = HOME / "Code"
# THE BOARD was retired 2026-10-19 (AFC-363);  the database is frozen here and still ingested so the
# resolved findings stay searchable.  FLEET_RAG_BOARD_DB overrides the path.
BOARD_DB = pathlib.Path(
    os.environ.get("FLEET_RAG_BOARD_DB")
    or APPS_DIR / "_retired" / "mac-collab-20261019" / "findings.db"
)
# Every board row was imported into Linear;  the migration left board_id -> Linear URL in this TSV
# (columns board_id, source_key, team, linear_identifier, linear_url, linear_id).  FLEET_RAG_BOARD_MAP overrides.
BOARD_MAP = pathlib.Path(
    os.environ.get("FLEET_RAG_BOARD_MAP")
    or HOME / ".local" / "share" / "linear-migration" / "data" / "board-to-linear.tsv"
)
FLEET_REPO = CODE_DIR / "AI-Fleet-Coordinator"
FLEET_OPS_REPO = CODE_DIR / "fleet-ops"
GROK_HOME = HOME / ".grok"
GROK_SESSIONS = GROK_HOME / "sessions"
CURSOR_PROJECTS = HOME / ".cursor" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
GEMINI_HOME = HOME / ".gemini"
# The protocol document was renamed:  the live copy and the mirror each hold exactly one name now, and the old one
# is gone.  The fleet points at FLEET-PROTOCOL.md by its current name only.
PROTOCOL_DOC_NAMES = ("FLEET-PROTOCOL.md",)
EXTRA_DOCS = (
    APPS_DIR / "FLEET-PROTOCOL.md",
    APPS_DIR / "MAC-LOCAL-PROCESSES.md",
    APPS_DIR / "FLEET-UI-COPY.md",
    HOME / ".claude" / "CLAUDE.md",
)
SKILL_DIRS = (HOME / ".claude" / "skills", HOME / ".cursor" / "skills", GROK_HOME / "skills")
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_MEMORIES = HOME / ".codex" / "memories"
DOC_MAX_BYTES = 400 * 1024
CHAT_MAX_CHARS = 80_000
TURN_MAX_CHARS = 24_000
JSONL_LINE_MAX = 200_000

# Restricted walk for product app repos (fleet coordinator / fleet-ops keep a broader walk).
DOC_ROOT_NAMES = ("README.md", "AGENTS.md", "STATUS.md", "CLAUDE.md")
DOC_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "backups",
        "dist",
        "build",
        "vendor",
        ".secrets",
    }
)
DOC_APP_REPOS = (
    "Socratic.Trade",
    "Congress.Trade",
    "congress-trading-shared",
    "API-usage-monitor",
    "Usage-Monitor",
    "DealDex",
    "Personal-Site",
    "BotFleet",
    "BotFleet/openmausbot",
    "openmausbot",
    "ContactLogo",
    "AutoRotate",
    "Autorotate",
    "AI-Fleet-Coordinator",
    "fleet-ops",
    "agentic-trading",
)

# Doc-walk mirror rules (2026-09-02).  The same text used to be ingested several times over:
# the live ~/apps copy AND the AI-Fleet-Coordinator mirror of a protocol file, every by-seat /
# universal / dot-dir copy of a skill on top of the skill source, and each repo's
# docs/EFFORT-LOG.md mirror on top of the effort-log source, and (2026-10-11) the Fleet-OPS handbook/ pages that
# build the live FLEET-PROTOCOL.md.  mirror_skip_reason() names the
# rule that drops a file; iter_docs logs a per-rule summary through warn().
SKILL_TREE_DIRS = frozenset({".claude", ".cursor", ".grok"})
SKIP_APPS_MIRROR = "apps-mirror"  # (a) live ~/apps copy is canonical
SKIP_SKILL_COPY = "skill-copy"  # (b) covered by the skill source
SKIP_EFFORT_LOG = "effort-log-mirror"  # (c) covered by the effort-log source
SKIP_HANDBOOK_MIRROR = "handbook-mirror"  # (d) Fleet-OPS generated/ and public handbook pages: any ingested FLEET-PROTOCOL.md
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
    "api-usage-monitor": "usage-monitor",
    "usage-monitor": "usage-monitor",
    "um": "usage-monitor",
    "socratic-trade": "socratic-trade",
    "st": "socratic-trade",
    "congress-trade": "congress-trade",
    "ct": "congress-trade",
    "congress-shared": "congress-trading-shared",
    "congress-trading-shared": "congress-trading-shared",
    "cts": "congress-trading-shared",
    "fleet": "fleet",
    "fleet-infra": "fleet",
    "ai-fleet-coordinator": "fleet",
    "afl": "fleet",
    "fleet-ops": "fleet-ops",
    "botfleet": "botfleet",
    "bf": "botfleet",
    "openmausbot": "botfleet",
    "dealdex": "dealdex",
    "dd": "dealdex",
    "contactlogo": "contactlogo",
    "cl": "contactlogo",
    "autorotate": "autorotate",
    "ar": "autorotate",
    "personal-site": "personal-site",
    "ps": "personal-site",
    "trading": "trading",
    "agentic-trading": "trading",
    "trading-live": "trading",
    "clutch": "clutch",
    "ck": "clutch",
    "minimax-remote": "clutch",
    "harness": "clutch",
}

SEAT_ALIASES = {
    "grok": "GROK",
    "grok build": "GROK",
    "grok-build": "GROK",
    "monet": "MONET",
    "claude": "CLAUDE",
    "codex": "CODEX",
    "antigravity": "AG",
    "ag": "AG",
    "cursor": "CURSOR",
    "kimi": "KIMI",
    "owner": "OWNER",
    "jay": "OWNER",
    "gemini": "AG",
    "deepseek": "DSH",
    "dsh": "DSH",
    "deepseek harness": "DSH",
    "renoir": "RENOIR",
    "harness": "HARNESS",
    "fleet": "FLEET",
    "minimax": "MM",
    "mm": "MM",
    "clutch": "CLUTCH",
    "grok web": "GROK-WEB",
    "grok-web": "GROK-WEB",
    "grok bot": "GROK-BOT",
    "grok-bot": "GROK-BOT",
    "fx": "FX",
    "ma": "MA",
    "muse assist": "MA",
    "muse-assist": "MA",
    "mc": "MC",
    "muse code": "MC",
    "muse-code": "MC",
    "opencode": "OPENCODE",
    "echo": "ECHO",
    "wise": "WISE",
}
# The note title carries the agent itself (owner 2026-10-20:  `[TEAM_KEY, Agent] Topic`), so
# every live tag has to resolve here.  A tag missing from this table is read as a team key and the
# note loses its seat attribution entirely, which is worse than a wrong app.


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
        d = d.astimezone()  # naive stamps are local (Notes, git-less files)
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


_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


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
    return meta, md[m.end() :]


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
        p = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, timeout=60, check=False
        )
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
    head = (
        _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], pathlib.Path(top)) or ""
    ).strip()
    if head.startswith("origin/"):
        info["branch"] = head[len("origin/") :]
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
    def clean(v) -> str:
        # The effort-board writeback stamps issue bodies with `<!-- effort-key: <sha1> -->`;
        # it is machine metadata and gitleaks reads it as a credential, so drop it.
        return (
            _EFFORT_KEY.sub("", v).strip() if isinstance(v, str) else ("" if v is None else str(v))
        )

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
    for k, label in (
        ("source", "Source"),
        ("repo", "Repo"),
        ("location", "Location"),
        ("env", "Env"),
        ("source_url", "Link"),
    ):
        if clean(row.get(k)):
            where.append(f"{label}: {clean(row[k])}")
    if where:
        parts.append("- " + " · ".join(where))
    parts.append(f"- Board id: {clean(row.get('id'))}")
    for k, label in (
        ("description", "Description"),
        ("recommended_fix", "Recommended fix"),
        ("resolution", "Resolution"),
    ):
        if clean(row.get(k)):
            parts += ["", f"## {label}", "", clean(row[k])]
    if comments:
        parts += ["", "## Comments"]
        for c in comments:
            parts += [
                "",
                f"### {clean(c.get('author')) or '?'} — {clean(c.get('created_at'))}",
                "",
                clean(c.get("text")),
            ]
    return "\n".join(parts).rstrip() + "\n"


def load_board_url_map(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """board row id -> Linear issue URL, from the migration's TSV.  Empty when the file is missing or unreadable,
    so a board finding never gets a link at all rather than a dead one."""
    path = pathlib.Path(BOARD_MAP if path is None else path)
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            next(fh, None)  # header
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 5 and cols[0].strip() and cols[4].startswith("https://linear.app/"):
                    out[cols[0].strip()] = cols[4].strip()
    except OSError:
        return {}
    return out


def iter_board(
    db_path: pathlib.Path | str = BOARD_DB,
    limit: int | None = None,
    url_map: dict[str, str] | None = None,
) -> Iterator[Doc]:
    db_path = pathlib.Path(db_path)
    if url_map is None:
        url_map = load_board_url_map()
    if not db_path.exists():
        raise FileNotFoundError(f"board db not found: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        comments: dict[str, list[dict]] = {}
        for c in con.execute(
            "SELECT finding_id, author, text, created_at FROM comments ORDER BY created_at, rowid"
        ):
            comments.setdefault(c["finding_id"], []).append(dict(c))
        q = "SELECT * FROM findings ORDER BY created_at, rowid"
        if limit:
            q += f" LIMIT {int(limit)}"
        for r in con.execute(q):
            row = dict(r)
            cs = comments.get(row["id"], [])
            created = parse_ts_ms(row.get("created_at"))
            updated = max(
                [parse_ts_ms(row.get("updated_at"))] + [parse_ts_ms(c["created_at"]) for c in cs]
            )
            resolved = bool((row.get("resolution") or "").strip())
            app = app_slug(row.get("app"))
            yield Doc(
                doc_id=f"board/{row['id']}",
                title=(row.get("title") or "").strip() or f"Finding {row['id'][:8]}",
                text_markdown=_board_markdown(row, cs),
                source="board",
                app=app,
                category="lesson" if resolved else "finding",
                seat=seat_tag(row.get("addressed_by") or row.get("reported_by")),
                url=url_map.get(str(row["id"]), ""),
                path=str(db_path),
                created_at_ms=created or updated,
                updated_at_ms=updated or created,
                extra={
                    "status": row.get("status"),
                    "severity": row.get("severity"),
                    "source_kind": row.get("source_kind"),
                },
            )
    finally:
        con.close()


# --------------------------------------------------------------------------- effort logs


def iter_effort_logs(
    apps_dir: pathlib.Path | str = APPS_DIR, limit: int | None = None
) -> Iterator[Doc]:
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
        yield Doc(
            doc_id=f"effort-log/{key}",
            title=first_heading(md, p.name),
            text_markdown=md,
            source="effort-log",
            app=app,
            category="lesson",
            seat="FLEET",
            url="",
            path=str(p),
            created_at_ms=ts,
            updated_at_ms=ts,
        )


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
    return (
        name == "EFFORT-LOG.md"
        or name.endswith("-EFFORT-LOG.md")
        or name == "EFFORT-LOG-PROTOCOL.md"
    )


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


def _rel_under(p: pathlib.Path, roots: Iterable[pathlib.Path]) -> pathlib.PurePath | None:
    """`p` relative to the first of `roots` that holds it, both spelled the way the disk spells them (so a
    case-variant spelling of fleet-ops / Fleet-OPS on APFS still matches), or None."""
    try:
        cp = canonical_path(p)
    except OSError:
        return None
    for r in roots:
        try:
            return cp.relative_to(canonical_path(r))
        except (ValueError, OSError):
            continue
    return None


_HANDBOOK_ALWAYS_PRIVATE = frozenset({"private", "rulings"})


def handbook_page_is_public(p: pathlib.Path, rel: pathlib.PurePath) -> bool:
    """True for a Fleet-OPS handbook page whose text is in the generated FLEET-PROTOCOL.md.

    The generator's own predicate (scripts/handbook/build.mjs):  the front matter says exactly
    `visibility: public`, and the page is not under handbook/private/ or handbook/rulings/, whatever its front
    matter says.  Anything else (no front matter, handbook/README.md, any other value, an unreadable file) is
    kept, because only the public pages duplicate the live file.
    """
    if len(rel.parts) > 2 and rel.parts[1] in _HANDBOOK_ALWAYS_PRIVATE:
        return False
    try:
        with open(p, encoding="utf-8", errors="replace", newline="") as fh:
            lines = fh.read(8192).split("\n")
    except OSError:
        return False
    if not lines or lines[0] != "---":
        return False
    for line in lines[1:]:
        if line == "---":
            return False
        if line == "visibility: public":
            return True
    return False


def mirror_skip_reason(
    p: pathlib.Path,
    *,
    fleet_repos: Iterable[pathlib.Path] = (),
    live_basenames: Iterable[str] = (),
    ops_repos: Iterable[pathlib.Path] = (),
    agent_sync_ingested: bool | None = None,
) -> str | None:
    """Why the doc walk drops this file, or None to keep it.

    (a) SKIP_APPS_MIRROR: a file inside the fleet coordinator checkout whose basename is also a
        top-level ~/apps/*.md (FLEET-PROTOCOL.md, MAC-LOCAL-PROCESSES.md, FLEET-UI-COPY.md, ...).
        The live copy is canonical.  Generic root names (README.md, AGENTS.md, ...) are exempt.
    (b) SKIP_SKILL_COPY: any file under .claude/skills, .cursor/skills or .grok/skills, anything
        under docs/fleet-skills/by-seat, and any SKILL.md under a skills/ or fleet-skills/ dir.
        The skill source ingests ~/.claude/skills, ~/.cursor/skills and ~/.grok/skills.
    (c) SKIP_EFFORT_LOG: EFFORT-LOG.md, *-EFFORT-LOG.md and EFFORT-LOG-PROTOCOL.md anywhere.
        The effort-log source ingests the live boards.
    (d) SKIP_HANDBOOK_MIRROR: in a fleet-ops checkout, anything under generated/ and every public page under
        handbook/ (handbook_page_is_public), but only when a copy of FLEET-PROTOCOL.md is really ingested
        (`agent_sync_ingested`;  left as None it means the live file is among `live_basenames`).  The public pages are
        the text of that file, so a recall query for a handbook rule would otherwise return each rule twice:  once
        from the file (the live copy, or the AI-Fleet-Coordinator mirror when there is no live copy) and once from
        the page.  With no ingested copy at all (a fresh host, or both copies rejected by _skip_doc) the pages are the
        only text and stay.  Private pages (handbook/private/, handbook/rulings/, a page that is not
        `visibility: public`) and handbook/README.md are not in the file and are always kept.
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
    ops = list(ops_repos)
    if agent_sync_ingested is None:
        agent_sync_ingested = any(n in live for n in PROTOCOL_DOC_NAMES)
    if ops and agent_sync_ingested:
        rel = _rel_under(p, ops)
        if rel is not None and rel.parts:
            if rel.parts[0] == "generated":
                return SKIP_HANDBOOK_MIRROR
            if (
                rel.parts[0] == "handbook"
                and name.endswith(".md")
                and handbook_page_is_public(p, rel)
            ):
                return SKIP_HANDBOOK_MIRROR
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


def _collect_handbook_pages(repo: pathlib.Path) -> list[pathlib.Path]:
    """The Fleet-OPS handbook/ pages.  The root and docs/ walk does not enter handbook/, and the public pages are
    dropped again by mirror_skip_reason rule (d), so what survives is the private text and handbook/README.md."""
    pages = repo / "handbook"
    if not pages.is_dir():
        return []
    return sorted(pages.rglob("*.md"))


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


def _unique_repo_dirs(
    code_dir: pathlib.Path, names: Iterable[str] = DOC_APP_REPOS
) -> list[pathlib.Path]:
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


def _doc_files(
    fleet_repo: pathlib.Path,
    fleet_ops: pathlib.Path,
    extra: Iterable[pathlib.Path],
    code_dir: pathlib.Path | None = None,
    apps_dir: pathlib.Path | None = None,
    grok_home: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    """Every markdown file the doc source ingests, mirror rules applied (see mirror_skip_reason).

    Skipped files are appended to DOC_SKIPS as (rule, path); iter_docs turns them into one
    warning per rule.
    """
    files: list[pathlib.Path] = []
    files += _collect_repo_docs(fleet_repo, root_all_md=True)
    files += _collect_repo_docs(fleet_ops, root_all_md=True)
    files += _collect_handbook_pages(fleet_ops)
    fleet_repos = [fleet_repo]
    ops_repos = [fleet_ops]
    if code_dir is not None:
        code_dir = pathlib.Path(code_dir)
        fleet_repos.append(code_dir / "AI-Fleet-Coordinator")
        ops_repos.append(code_dir / "fleet-ops")
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
    # Only a live file that the walk will really ingest can stand in for its mirrors:  one that is missing, or
    # rejected by _skip_doc (over DOC_MAX_BYTES, in a skipped directory), would leave the mirror as the only copy.
    live_basenames = {
        p.name for p in live if p.is_file() and not _is_effort_log_name(p.name) and not _skip_doc(p)
    }

    # The handbook's public pages stand down for an FLEET-PROTOCOL.md that the walk will really ingest:  the live file, or
    # (on a host without one) the coordinator's mirror, which rule (a) keeps when there is no live copy.
    def _ingestable_mirror(f: pathlib.Path) -> bool:
        try:
            return (
                f.name in PROTOCOL_DOC_NAMES
                and f.is_file()
                and not _skip_doc(f)
                and _under(f, fleet_repos)
            )
        except (
            OSError
        ):  # unreadable:  _dedupe_docs drops it too, so it cannot stand in for the pages
            return False

    agent_sync_ingested = any(n in live_basenames for n in PROTOCOL_DOC_NAMES) or any(
        _ingestable_mirror(f) for f in files
    )
    kept: list[pathlib.Path] = []
    for f in _unique_paths(files):
        reason = mirror_skip_reason(
            f,
            fleet_repos=fleet_repos,
            live_basenames=live_basenames,
            ops_repos=ops_repos,
            agent_sync_ingested=agent_sync_ingested,
        )
        if reason:
            DOC_SKIPS.append((reason, str(f)))
            continue
        kept.append(f)
    return _dedupe_docs(kept)


def doc_from_file(
    p: pathlib.Path,
    source: str = "doc",
    category: str | None = None,
    app: str = "fleet",
    seat: str = "FLEET",
    doc_prefix: str = "doc",
) -> Doc:
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
    return Doc(
        doc_id=doc_id,
        title=first_heading(md, p.name),
        text_markdown=md,
        source=source,
        app=app,
        category=cat,
        seat=seat,
        url=url,
        path=str(p),
        created_at_ms=created,
        updated_at_ms=max(mtime_ms(p), created),
    )


def iter_docs(
    fleet_repo: pathlib.Path | str = FLEET_REPO,
    fleet_ops: pathlib.Path | str = FLEET_OPS_REPO,
    extra: Iterable[pathlib.Path] = EXTRA_DOCS,
    limit: int | None = None,
    code_dir: pathlib.Path | str | None = CODE_DIR,
    apps_dir: pathlib.Path | str | None = APPS_DIR,
    grok_home: pathlib.Path | str | None = GROK_HOME,
) -> Iterator[Doc]:
    ops = pathlib.Path(fleet_ops)
    DOC_SKIPS.clear()
    files = _doc_files(
        pathlib.Path(fleet_repo),
        ops,
        extra,
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
    for rule in (SKIP_APPS_MIRROR, SKIP_SKILL_COPY, SKIP_EFFORT_LOG, SKIP_HANDBOOK_MIRROR):
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


def iter_skills(
    skill_dirs: Iterable[pathlib.Path] = SKILL_DIRS, limit: int | None = None
) -> Iterator[Doc]:
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
            yield Doc(
                doc_id=f"skill/{tree}/{p.parent.name}",
                title=name,
                text_markdown=md,
                source="skill",
                app="fleet",
                category="runbook",
                seat="FLEET",
                url="",
                path=str(p),
                created_at_ms=ts,
                updated_at_ms=ts,
            )


# --------------------------------------------------------------------------- memory

_MEMORY_TYPES = {
    "user": "preference",
    "feedback": "preference",
    "project": "lesson",
    "reference": "infrastructure",
}


def project_slug(dirname: str) -> str:
    s = dirname
    for prefix in ("-Users-jay-", "-Users-jay"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "home"


def app_from_project(slug: str) -> str:
    for key in (
        "congress-trading-shared",
        "socratic-trade",
        "congress-trade",
        "usage-monitor",
        "api-usage-monitor",
        "botfleet",
        "openmausbot",
        "dealdex",
        "autorotate",
        "contactlogo",
        "personal-site",
        "agentic-trading",
        "trading-live",
        "trading",
        "ai-fleet-coordinator",
        "fleet-ops",
    ):
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
    return Doc(
        doc_id=doc_id,
        title=title,
        text_markdown=md,
        source="memory",
        app=app,
        category=cat,
        seat=seat,
        url="",
        path=str(p),
        created_at_ms=min(ts, upd) or ts,
        updated_at_ms=max(ts, upd),
    )


def iter_memory(
    claude_projects: pathlib.Path | str = CLAUDE_PROJECTS,
    codex_memories: pathlib.Path | str = CODEX_MEMORIES,
    limit: int | None = None,
) -> Iterator[Doc]:
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
                yield _memory_doc(
                    p, "CLAUDE", f"memory/claude/{slug}/{p.name}", app_from_project(slug)
                )
    cm = pathlib.Path(codex_memories)
    if cm.exists():
        for p in sorted(cm.glob("*.md")):
            if limit and n >= limit:
                return
            n += 1
            yield _memory_doc(p, "CODEX", f"memory/codex/{p.name}", "fleet")


# --------------------------------------------------------------------------- apple notes

_BRACKET = re.compile(r"^\s*\[([^\]]+)\]")


def _as_seat(token: str) -> str | None:
    """The canonical seat tag a bracket token names, or None when it names a team key."""
    low = token.lower()
    if re.match(r"^(gb|bf)-", low):
        return token.upper()
    return SEAT_ALIASES.get(low)


def parse_note_title(title: str) -> tuple[str, str]:
    """'[AFC, Claude] Topic' -> (app, seat).  The title convention puts the team keys first and the
    agent last, so the tokens are scanned from the right for a seat before anything is read as
    a team key.  Multi-team takes the first key.  Role tags (`GB-*`, `BF-*`) resolve by prefix."""
    m = _BRACKET.match(title or "")
    app, seat = "fleet", "FLEET"
    if not m:
        return app, seat
    toks = [t.strip() for t in re.split(r"[,/]", m.group(1)) if t.strip()]
    seat_idx = None
    for i in range(len(toks) - 1, -1, -1):
        found = _as_seat(toks[i])
        if found and found != "FLEET":
            seat, seat_idx = found, i
            break
    apps = [t for i, t in enumerate(toks) if i != seat_idx]
    if apps:
        app = app_slug(apps[0])
    return app, seat


def iter_apple_notes(
    records: Iterable[dict] | None = None, limit: int | None = None, log=None
) -> Iterator[Doc]:
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
            continue  # empty "New Note" shells
        if limit and n >= limit:
            return
        n += 1
        app, seat = parse_note_title(title)
        created = parse_ts_ms(rec.get("created"))
        updated = parse_ts_ms(rec.get("modified")) or created
        nid = rec.get("id", "")
        yield Doc(
            doc_id="note/" + nid.replace("x-coredata://", ""),
            title=title or "Untitled note",
            text_markdown=text,
            source="apple-note",
            app=app,
            category="note",
            seat=seat,
            url="",
            path="",
            created_at_ms=created or updated,
            updated_at_ms=updated,
        )


# --------------------------------------------------------------------------- chat logs

CHAT_SEATS = {
    "claude": "CLAUDE",
    "grok": "GROK",
    "cursor": "CURSOR",
    "codex": "CODEX",
    "gemini": "AG",
}
CHAT_SKIP_NAMES = frozenset(
    {
        "events.jsonl",
        "updates.jsonl",
        "rewind_points.jsonl",
        "signals.json",
        "journal.jsonl",
        "system_prompt.txt",
        "permission.toml",
        "permissions.toml",
        "summary.json",
        "prompt_context.json",
        "announcement_state.json",
        "resources_state.json",
        "transcript_full.jsonl",
    }
)
CHAT_SKIP_TYPES = frozenset(
    {
        "queue-operation",
        "mode",
        "permission-mode",
        "attachment",
        "progress",
        "system",
        "file-history-snapshot",
        "bridge-session",
        "last-prompt",
        "tool_result",
        "tool-result",
        "reasoning",
        "function_call",
        "function_call_output",
        "turn_context",
        "session_meta",
        "event_msg",
        "GENERIC",
    }
)
_TEXT_BLOCK_TYPES = frozenset(
    {
        "text",
        "input_text",
        "output_text",
        "inputtext",
        "outputtext",
    }
)
_SKIP_BLOCK_TYPES = frozenset(
    {
        "tool_use",
        "tool_result",
        "tool-result",
        "tool_call",
        "function_call",
        "function_call_output",
        "image",
        "image_url",
        "thinking",
        "reasoning",
        "redacted_thinking",
    }
)
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
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_MODE_ONLY_RE = re.compile(
    r"^(?:plan|default|acceptEdits|bypassPermissions|dontAsk|don't ask)$", re.IGNORECASE
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


def _docs_from_turns(
    turns: list[tuple[str, str]], *, platform: str, stable_id: str, path: pathlib.Path, app: str
) -> Iterator[Doc]:
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
            title=title,
            text_markdown=body + "\n",
            source="chat-log",
            app=app,
            category=cat,
            seat="OWNER",
            url="",
            path=str(path),
            created_at_ms=ts,
            updated_at_ms=ts,
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
            text = re.sub(r"<ADDITIONAL_METADATA>.*", "", text, flags=re.DOTALL)
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


def iter_chat_logs(
    claude_projects: pathlib.Path | str | None = CLAUDE_PROJECTS,
    grok_sessions: pathlib.Path | str | None = GROK_SESSIONS,
    cursor_projects: pathlib.Path | str | None = CURSOR_PROJECTS,
    codex_sessions: pathlib.Path | str | None = CODEX_SESSIONS,
    gemini_home: pathlib.Path | str | None = GEMINI_HOME,
    limit: int | None = None,
) -> Iterator[Doc]:
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
                yield from emit(
                    _docs_from_turns(turns, platform="grok", stable_id=stable, path=p, app=app)
                )
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


# --------------------------------------------------------------------------- linear (the live board)
#
# THE BOARD was retired 2026-10-19 (AFC-363);  the frozen findings.db keeps the old rows
# searchable, but nothing new can ever arrive from it, so the corpus has missed every board
# point since the freeze.  Linear is the live board now (docs/protocols/linear-migration.md).
# This generator reads Linear through the installed `simple-board` CLI - read verbs only,
# which need no bearer and run as the shared Fleet app - so credential lookup, token caching
# and rate limiting stay in the one sanctioned place instead of being duplicated here.
#
# What is yielded, so the frozen board docs are never duplicated and nothing is skipped:
#   - every issue the frozen corpus does NOT already cover, however old.  Coverage is decided
#     against the frozen findings.db, not the migration TSV alone:  the TSV also maps GitHub
#     and effort-log rows that were imported into Linear without ever becoming board findings
#     (measured 2026-10-28:  4,269 of 14,209 TSV rows have no frozen finding), so TSV
#     membership by itself would skip thousands of never-covered issues, and
#   - every covered issue touched at or after the freeze (LINEAR_FREEZE):  its frozen doc
#     is stale and this fresh copy is its update.
# The light scan (id + updatedAt only, one page per 250 issues) keeps the nightly cheap;  full
# bodies and comments are fetched only for the selected ids.  Point ids stay stable
# (doc_id linear/<identifier>) so a re-run re-embeds only what actually changed.

LINEAR_FREEZE = os.environ.get("FLEET_RAG_LINEAR_FREEZE", "2026-10-19T00:00:00.000Z")
SIMPLE_BOARD_CLI = os.environ.get("FLEET_RAG_SIMPLE_BOARD", "simple-board")
LINEAR_PAGE = 250
LINEAR_FULL_PAGE = 50  # full nodes are heavy (descriptions + comments);  keep each call modest
# simple-board caps --max-chars at 64000 and exits 1 past it, so oversized responses cannot be
# lifted away:  the fetch below halves its id chunk on overflow instead, and an issue that
# overflows even alone is SKIPPED (flagged) rather than ingested partial.
LINEAR_MAX_CHARS = 64_000
LINEAR_COMMENT_PAGE = 100
# First page plus up to five follow-ups.  Beyond the bound the issue is SKIPPED (with a run
# warning) rather than ingested truncated:  comments sort oldest-first, so a truncated doc
# would never pick up the omitted tail and its content hash would freeze the partial copy in
# place forever - including over an earlier complete one.
LINEAR_COMMENT_MAX_PAGES = 6
LINEAR_QUERY_TIMEOUT_S = 120

_LINEAR_LIGHT_FIELDS = "nodes { id updatedAt } pageInfo { hasNextPage endCursor }"
_LINEAR_FULL_FIELDS = (
    "nodes { id identifier title url description priority createdAt updatedAt "
    "state { name type } team { key } labels { nodes { name } } "
    "attachments(first: 20) { nodes { url } } "
    "comments(first: %d, orderBy: createdAt) { nodes { body createdAt user { displayName } "
    "externalUser { displayName } } pageInfo { hasNextPage endCursor } } } "
    "pageInfo { hasNextPage endCursor }" % LINEAR_COMMENT_PAGE
)

# Linear priority number -> the severity the board would have carried (README mapping:
# Urgent P0, High P1, Medium P2, Low P3/P4, none -).  A Severity/<P> label wins when present.
_LINEAR_PRIORITY_SEVERITY = {0: "", 1: "P0", 2: "P1", 3: "P2", 4: "P3"}


class LinearSourceError(FleetRagError):
    """The simple-board read failed;  the run must record the source as errored, not yield zero."""


def _linear_query(query: str, timeout_s: int = LINEAR_QUERY_TIMEOUT_S) -> dict:
    """One GraphQL read through the simple-board CLI.  Fail-closed:  any transport, exit-code or
    GraphQL error raises LinearSourceError so a broken read can never look like an empty board."""
    try:
        proc = subprocess.run(
            [SIMPLE_BOARD_CLI, "api", "query", "--max-chars", str(LINEAR_MAX_CHARS), query],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LinearSourceError(f"linear: simple-board api query failed to run: {e}") from None
    if proc.returncode != 0:
        raise LinearSourceError(
            f"linear: simple-board api query exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise LinearSourceError(f"linear: unreadable JSON from simple-board: {e}") from None
    if isinstance(data, dict) and data.get("errors"):
        raise LinearSourceError(f"linear: GraphQL errors: {str(data['errors'])[:300]}")
    if not isinstance(data, dict):
        raise LinearSourceError("linear: unexpected payload shape from simple-board")
    return data


def _linear_light_scan() -> dict:
    """Every issue's id -> updatedAt, one 250-wide page at a time, oldest update first."""
    out: dict = {}
    after = None
    while True:
        clause = f', after: "{after}"' if after else ""
        data = _linear_query(
            "query { issues(first: %d, orderBy: updatedAt%s) { %s } }"
            % (LINEAR_PAGE, clause, _LINEAR_LIGHT_FIELDS)
        )
        conn = data.get("issues") or {}
        for node in conn.get("nodes") or []:
            if node.get("id"):
                out[node["id"]] = node.get("updatedAt") or ""
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return out
        after = page.get("endCursor")
        if not after:
            raise LinearSourceError("linear: pageInfo said hasNextPage but gave no endCursor")


def load_linear_covered_ids(
    map_path: pathlib.Path | str | None = None,
    db_path: pathlib.Path | str | None = None,
) -> set:
    """Linear UUIDs whose content the frozen board corpus already covers:  TSV rows whose
    board_id exists in the frozen findings.db.  A Linear issue the migration merged several
    rows into is covered only when EVERY mapped row is covered.  Empty when either file is
    missing - the safe direction:  it re-ingests, it never skips."""
    map_path = pathlib.Path(BOARD_MAP if map_path is None else map_path)
    db_path = pathlib.Path(BOARD_DB if db_path is None else db_path)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            covered_board_ids = {r[0] for r in con.execute("SELECT id FROM findings")}
        finally:
            con.close()
        by_linear: dict[str, list[str]] = {}
        with map_path.open(encoding="utf-8") as fh:
            next(fh, None)  # header
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 6 and cols[5].strip():
                    by_linear.setdefault(cols[5].strip(), []).append(cols[0].strip())
    except OSError:
        return set()
    return {
        linear_id
        for linear_id, board_ids in by_linear.items()
        if board_ids and all(b in covered_board_ids for b in board_ids)
    }


def _linear_is_overflow(err: LinearSourceError) -> bool:
    """The CLI refused the response for size (it exits 1 with this stderr), not for a real read
    failure:  only this error may degrade to a smaller read or a skip, everything else raises."""
    return "exceeds --max-chars" in str(err)


def _linear_placeholder(issue_id: str) -> dict:
    """A skipped issue still needs a node so the missing-id check does not misfire on it."""
    try:
        data = _linear_query(
            "query { issues(first: 1, filter: { id: { in: [%s] } }) { nodes { id identifier } } }"
            % json.dumps(issue_id)
        )
        nodes = (data.get("issues") or {}).get("nodes") or []
        identifier = nodes[0].get("identifier") if nodes else None
    except LinearSourceError:
        identifier = None
    return {"id": issue_id, "identifier": identifier or issue_id, "_linear_truncated": True}


def _linear_fetch_chunk(chunk: list) -> list:
    """One id chunk's full nodes.  A response too large for the CLI halves the chunk;  an issue
    too large even alone degrades to a flagged placeholder (skipped downstream), never a partial
    doc.  Any other read failure raises - fail closed."""
    id_list = ", ".join(json.dumps(i) for i in chunk)
    after = None
    nodes: list = []
    try:
        while True:
            clause = f', after: "{after}"' if after else ""
            data = _linear_query(
                "query { issues(first: %d, filter: { id: { in: [%s] } }%s) { %s } }"
                % (LINEAR_FULL_PAGE, id_list, clause, _LINEAR_FULL_FIELDS)
            )
            conn = data.get("issues") or {}
            nodes.extend(conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                raise LinearSourceError("linear: full fetch lost its cursor mid-chunk")
    except LinearSourceError as e:
        if not _linear_is_overflow(e):
            raise
        if len(chunk) > 1:
            mid = len(chunk) // 2
            return _linear_fetch_chunk(chunk[:mid]) + _linear_fetch_chunk(chunk[mid:])
        warn(
            "linear: one issue exceeds the CLI size cap on its own;  skipped instead of "
            "ingesting a truncated copy (any earlier complete version stays current)"
        )
        return [_linear_placeholder(chunk[0])]
    if not nodes:
        raise LinearSourceError(
            "linear: a full fetch returned no nodes for a non-empty id chunk"
        )
    for node in nodes:
        _linear_complete_comments(node)
    return nodes


def _linear_fetch_full(ids: list) -> list:
    """Full nodes (bodies, labels, attachments, comments) for the selected ids, LINEAR_FULL_PAGE
    per call, halving on CLI size overflow."""
    nodes: list = []
    for start in range(0, len(ids), LINEAR_FULL_PAGE):
        nodes.extend(_linear_fetch_chunk(ids[start : start + LINEAR_FULL_PAGE]))
    found = {n.get("id") for n in nodes}
    missing = [i for i in ids if i not in found]
    if missing:
        raise LinearSourceError(
            f"linear: {len(missing)} selected id(s) came back without nodes (archived between "
            f"scan and fetch?): {missing[:3]}"
        )
    return nodes


def _linear_complete_comments(node: dict) -> None:
    """Page the comment connection past the first page when the issue has one.  Any truncation
    risk - the page bound, or a comment page too large for the CLI - flags the node so the run
    SKIPS it;  every other failure raises.  A truncated copy must never become the corpus's
    current version of the doc (comments sort oldest-first, so the omitted tail would never
    arrive and the partial hash would freeze in place)."""
    comments = node.get("comments") or {}
    page = comments.get("pageInfo") or {}
    seen = 1
    while page.get("hasNextPage") and seen < LINEAR_COMMENT_MAX_PAGES:
        after = page.get("endCursor")
        if not after:
            raise LinearSourceError(
                f"linear: comment pageInfo for {node.get('identifier')} lost its cursor"
            )
        try:
            data = _linear_query(
                'query { issue(id: "%s") { comments(first: %d, after: "%s", orderBy: createdAt) '
                "{ nodes { body createdAt user { displayName } externalUser { displayName } } "
                "pageInfo { hasNextPage endCursor } } } }"
                % (node.get("id"), LINEAR_COMMENT_PAGE, after)
            )
        except LinearSourceError as e:
            if not _linear_is_overflow(e):
                raise
            node["_linear_truncated"] = True
            return
        conn = (data.get("issue") or {}).get("comments") or {}
        comments.setdefault("nodes", []).extend(conn.get("nodes") or [])
        page = conn.get("pageInfo") or {}
        seen += 1
    if page.get("hasNextPage"):
        # See LINEAR_COMMENT_MAX_PAGES:  a truncated issue is skipped, never ingested partial.
        node["_linear_truncated"] = True
    comments["pageInfo"] = page


def _linear_severity(node: dict, label_names: list) -> str:
    for name in label_names:
        if name.startswith("Severity/"):
            return name.split("/", 1)[1]
    return _LINEAR_PRIORITY_SEVERITY.get(node.get("priority") or 0, "")


def _linear_seat(label_names: list) -> str:
    for prefix in ("seat:", "claimed:"):
        for name in label_names:
            if name.startswith(prefix):
                return seat_tag(name[len(prefix) :])
    return "FLEET"


def _linear_kind(label_names: list) -> str:
    for name in label_names:
        if name.startswith("Kind/"):
            return name.split("/", 1)[1]
    return ""


def _linear_markdown(node: dict, label_names: list) -> str:
    def clean(v) -> str:
        return (
            _EFFORT_KEY.sub("", v).strip() if isinstance(v, str) else ("" if v is None else str(v))
        )

    parts = [f"# {clean(node.get('title')) or 'Untitled issue'}", ""]
    meta = [f"State: {clean((node.get('state') or {}).get('name')) or 'unknown'}"]
    severity = _linear_severity(node, label_names)
    if severity:
        meta.append(f"Severity: {severity}")
    team = clean((node.get("team") or {}).get("key"))
    if team:
        meta.append(f"Team: {team}")
    kind = _linear_kind(label_names)
    if kind:
        meta.append(f"Kind: {kind}")
    seats = [n for n in label_names if n.startswith(("seat:", "claimed:"))]
    if seats:
        meta.append("Seats: " + ", ".join(sorted(seats)))
    parts.append("- " + " \u00b7 ".join(meta))
    parts.append(f"- Linear: {clean(node.get('identifier'))}  {clean(node.get('url'))}")
    description = clean(node.get("description"))
    if description:
        parts += ["", "## Description", "", description]
    attachments = [
        clean(a.get("url"))
        for a in (node.get("attachments") or {}).get("nodes") or []
        if clean(a.get("url"))
    ]
    if attachments:
        parts += ["", "## Attachments"] + [f"- {u}" for u in attachments]
    comments = (node.get("comments") or {}).get("nodes") or []
    if comments:
        ordered = sorted(comments, key=lambda c: c.get("createdAt") or "")
        parts += ["", "## Comments"]
        for c in ordered:
            author = clean((c.get("user") or {}).get("displayName")) or clean(
                (c.get("externalUser") or {}).get("displayName")
            )
            parts += [
                "",
                f"### {author or '?'} \u2014 {clean(c.get('createdAt'))}",
                "",
                clean(c.get("body")),
            ]
    return "\n".join(parts).rstrip() + "\n"


def iter_linear(
    limit: int | None = None,
    *,
    map_path: pathlib.Path | str | None = None,
    freeze: str | None = None,
    light_scan=None,
    fetch_full=None,
    covered_ids=None,
) -> Iterator[Doc]:
    """The live board:  Linear issues the frozen board corpus does not cover.  See the section
    comment above for the selection rule.  `light_scan`/`fetch_full` are test injection points."""
    freeze = freeze or LINEAR_FREEZE
    light_scan = light_scan or _linear_light_scan
    fetch_full = fetch_full or _linear_fetch_full
    imported = covered_ids if covered_ids is not None else load_linear_covered_ids(map_path)
    scan = light_scan()
    selected = [
        uid for uid, updated in scan.items() if uid not in imported or (updated or "") >= freeze
    ]
    selected.sort(key=lambda uid: scan.get(uid) or "", reverse=True)
    if limit:
        selected = selected[: int(limit)]
    if not selected:
        return
    for node in fetch_full(selected):
        if node.get("_linear_truncated"):
            warn(
                f"linear: {node.get('identifier')} has more than "
                f"{LINEAR_COMMENT_PAGE * LINEAR_COMMENT_MAX_PAGES} comments or exceeds the CLI "
                f"size cap;  skipped instead of ingesting a truncated copy (any earlier "
                f"complete version stays current)"
            )
            continue
        labels = [l.get("name") or "" for l in (node.get("labels") or {}).get("nodes") or []]
        team_key = (node.get("team") or {}).get("key")
        created = parse_ts_ms(node.get("createdAt"))
        updated = parse_ts_ms(node.get("updatedAt"))
        done = ((node.get("state") or {}).get("type") or "").lower() == "completed"
        yield Doc(
            doc_id=f"linear/{node.get('identifier')}",
            title=(node.get("title") or "").strip() or f"Issue {node.get('identifier')}",
            text_markdown=_linear_markdown(node, labels),
            source="linear",
            app=app_slug(team_key),
            category="lesson" if done else "finding",
            seat=_linear_seat(labels),
            url=node.get("url") or "",
            path="",
            created_at_ms=created or updated,
            updated_at_ms=updated or created,
            extra={
                "state": (node.get("state") or {}).get("name"),
                "severity": _linear_severity(node, labels),
                "source_kind": _linear_kind(labels) or None,
                "linear_id": node.get("id"),
            },
        )


# --------------------------------------------------------------------------- registry

GENERATORS: dict[str, Callable[..., Iterator[Doc]]] = {
    "board": iter_board,
    "linear": iter_linear,
    "effort-log": iter_effort_logs,
    "doc": iter_docs,
    "skill": iter_skills,
    "memory": iter_memory,
    "apple-note": iter_apple_notes,
    "chat-log": iter_chat_logs,
}
