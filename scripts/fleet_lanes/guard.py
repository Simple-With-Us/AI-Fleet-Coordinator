"""Temp-checkout guard: the core of a PreToolUse hook that stops fleet-repo checkouts in temp dirs.

The rule it enforces is narrow on purpose.  It is not "nothing in /tmp": agents legitimately use
temp for scratch files, build output, throwaway test repos and the Claude harness scratchpad.  It
is "no git checkout or worktree of a FLEET repo in a temp directory", because those checkouts are
invisible to `git worktree list` and the disk cleaners, they hold dropped work, and they keep
stale entries in the integration tree's .git/worktrees.

A fleet repo is any repo in the registry (fleet-apps.json plus layout.EXTRA_APPS), any GitHub URL
or OWNER/REPO whose owner is Simple-With-Us or jaywedgeworth22, and any local source path under
~/Code, ~/apps (lanes included) or a harness worktree location.  Temp means /tmp, /private/tmp,
/var/tmp, /private/var/tmp, the per-user macOS temp dir (/private/var/folders/*/*/T) and the
TMPDIR directory.  Third-party clones into temp are allowed, and a destination that cannot be
resolved from the command text is allowed: the guard denies only when the destination is
provably in temp.

Denied shapes (each only for a fleet source and a temp destination):
  git clone [opts] SRC [DEST]              DEST omitted means the repo name (even an unknown one,
                                           as in a loop) under the cwd or the last `cd`; --bare
                                           and --mirror included
  git [-C REPO] worktree add|move ...      the repo is -C, --git-dir or the tracked cwd
  gh repo clone OWNER/REPO [DEST]
  git archive TREE | tar -x -C TEMP        the whole tree: no pathspec, only excludes, or one that
                                           may be the whole tree (".", "..", ":/", "*"); also
                                           `git archive -o TEMP/...` and `> TEMP/...`
  curl|wget|gh api of a GitHub source tarball, saved into temp or extracted there
  git fetch|pull FLEET in a temp dir       FLEET is a URL, a local fleet path, or a remote that
                                           `git remote add` pointed at one (init plus fetch)
  git checkout-index --prefix=TEMP/        with -a/--all, or with no named files (--stdin)

Always allowed: anything whose destination is the harness scratchpad (payload `scratchpad_dir`,
or a `claude-<uid>` directory directly under a temp root, which is where Claude Code keeps it),
`git init` alone, `git worktree list|remove|prune`, `rm -rf` of temp dirs, reads of an existing
temp checkout (`git -C /tmp/x status`), and commands that merely mention /tmp.  Named files are
an extraction, not a checkout, so `git archive TREE -- PATH... | tar -x -C TEMP`,
`git checkout-index --prefix=TEMP/ FILE...` and `git show REV:PATH > TEMP/x` are allowed too.

How it reads a command.  The command arrives as a template: variables and $(...) are not
expanded.  A small shell-aware lexer (quotes, escapes, line continuations, $(...), backticks,
${...}, heredocs, redirections, subshell parentheses, comments; it never executes anything)
splits it into simple commands.  Within the ONE command string it tracks the working directory
(cd, pushd, popd, subshell scope) and simple variable assignments, so `d=$(mktemp -d)` followed by
`git clone ... $d/repo` resolves to a temp destination.  $TMPDIR, ${TMPDIR:-/tmp} and a bare
`mktemp -d` always mean temp.  `bash -c '...'`, `eval` and a heredoc fed to a shell are
evaluated recursively; a script file run with bash is not read.

Purity.  `evaluate` does string work only: no subprocess, no network, no realpath and no file
reads.  Everything that touches the filesystem (resolving the temp roots, loading the registry)
happens once in `make_context`.  It fails OPEN: a malformed payload, an unexpected shape or any
internal exception gives an allow.

Owner-only note (never shown to agents): setting FLEET_LANE_GUARD=off in the hook's environment
turns the guard off entirely; 0, false, no and disabled work too.  The deny reason never
mentions it.

Tests: fleet_lanes/tests/test_guard.py with the fixture table in fleet_lanes/tests/fixtures_guard.py.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import layout as L

__all__ = [
    "ALLOW", "DENY", "Decision", "GuardContext", "make_context", "evaluate", "redact_url",
    "RULE_CLONE", "RULE_GH_CLONE", "RULE_WORKTREE_ADD", "RULE_WORKTREE_MOVE", "RULE_ARCHIVE",
    "RULE_TARBALL", "RULE_INIT_FETCH", "RULE_EXPORT", "DENY_RULES", "FLEET_OWNERS",
    "ENV_GUARD", "MAX_COMMAND_CHARS",
]

ALLOW = "allow"
DENY = "deny"

RULE_CLONE = "clone-into-temp"                    # git clone (bare and mirror included)
RULE_GH_CLONE = "gh-clone-into-temp"              # gh repo clone
RULE_WORKTREE_ADD = "worktree-add-into-temp"      # git worktree add
RULE_WORKTREE_MOVE = "worktree-move-into-temp"    # git worktree move
RULE_ARCHIVE = "archive-into-temp"                # git archive written or extracted into temp
RULE_TARBALL = "tarball-into-temp"                # GitHub source tarball downloaded or extracted into temp
RULE_INIT_FETCH = "init-fetch-into-temp"          # git init + remote add + fetch/pull in temp
RULE_EXPORT = "checkout-index-into-temp"          # git checkout-index --prefix=TEMP/
DENY_RULES = (RULE_CLONE, RULE_GH_CLONE, RULE_WORKTREE_ADD, RULE_WORKTREE_MOVE, RULE_ARCHIVE,
              RULE_TARBALL, RULE_INIT_FETCH, RULE_EXPORT)

# Allow outcomes, so a caller (and the tests) can tell why a command passed.
_OK_NO_MATCH = "no-match"
_OK_NOT_SHELL = "not-applicable"
_OK_DISABLED = "disabled"
_OK_TOO_LARGE = "too-large"
_OK_FAIL_OPEN = "fail-open"

FLEET_OWNERS = ("Simple-With-Us", "jaywedgeworth22")
ENV_GUARD = "FLEET_LANE_GUARD"
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

# A command this long is not parsed (fail open).  20 KB parses in a few milliseconds.
MAX_COMMAND_CHARS = 256 * 1024
_MAX_DEPTH = 6     # nesting of $(...), bash -c, eval and heredoc-to-shell evaluation

# Words that must appear before any deny rule can fire.  A command without one is allowed
# without being parsed.
_TRIGGER = re.compile(r"clone|worktree|archive|codeload|tarball|zipball|fetch|pull|remote|checkout-index",
                      re.IGNORECASE)

_UNK = "\x00"      # text the guard cannot know (an unset variable, command output)
_META = frozenset(" \t\r\n;&|()<>")
_PLAIN_RE = re.compile(r"[^\s;&|()<>'\"\\$`]+")
_DQ_PLAIN_RE = re.compile(r"[^\"\\$`]+")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=")
_ASSIGN_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=$")
_PARAM_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])")
_BAL_RE = re.compile(r"[\\'\"`()<#\n]")
_DELIM_START_RE = re.compile(r"['\"\\]?[A-Za-z_]")
_CLAUDE_SCRATCH_RE = re.compile(r"claude-\d+")
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)/?([^/\s]+)/([^/\s]+?)/*$")
_ARCHIVE_EXT_RE = re.compile(r"(\.tar\.gz|\.tgz|\.tar|\.zip|\.tar\.bz2|\.tar\.xz|\.git)$", re.IGNORECASE)

_TARBALL_RES = (
    re.compile(r"^(?:https?://)?(?:[^/@\s]*@)?codeload\.github\.com/([^/\s]+)/([^/\s]+)/"
               r"(?:legacy\.)?(?:tar\.gz|tar|zip)(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"^(?:https?://)?(?:[^/@\s]*@)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/"
               r"(?:archive|tarball|zipball)(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"^(?:https?://)?(?:[^/@\s]*@)?api\.github\.com/repos/([^/\s]+)/([^/\s]+)/"
               r"(?:tarball|zipball)(?:[/?#]|$)", re.IGNORECASE),
)
_GH_API_TARBALL_RE = re.compile(r"^/?repos/([^/\s]+)/([^/\s]+)/(?:tarball|zipball)(?:[/?#]|$)",
                                re.IGNORECASE)

_SHELL_TOOL_HINTS = ("bash", "shell", "terminal", "run_command", "exec_command", "execute_command")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_RESERVED = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!",
                       "{", "}", "time", "esac", "coproc"})
_HEADERS = frozenset({"for", "case", "select", "function"})
_PASSTHRU = frozenset({"gunzip", "gzip", "zcat", "xz", "unxz", "xzcat", "bzip2", "bunzip2",
                       "bzcat", "zstd", "unzstd", "zstdcat", "cat", "pv"})
_SPECIAL_ZERO = frozenset({"$", "!", "?", "#", "-", "RANDOM", "BASHPID", "SECONDS",
                           "EPOCHSECONDS", "EPOCHREALTIME", "PPID", "UID", "EUID", "LINENO"})

_GIT_GLOBAL_VALUE = frozenset({"-c", "--namespace", "--super-prefix", "--config-env",
                               "--attr-source", "--list-cmds"})
_CLONE_VALUE = frozenset({"-b", "--branch", "-o", "--origin", "-u", "--upload-pack", "--reference",
                          "--reference-if-able", "--separate-git-dir", "--depth", "--shallow-since",
                          "--shallow-exclude", "-c", "--config", "--filter", "--server-option",
                          "-j", "--jobs", "--template", "--bundle-uri", "--ref-format", "--revision"})
_WT_ADD_VALUE = frozenset({"-b", "-B", "--reason"})
_ARCHIVE_VALUE = frozenset({"--format", "--prefix", "--exec", "--add-file", "--add-virtual-file", "--mtime"})
_TAR_SHORT_VALUE = frozenset("bCfFgHIKLNTVX")
_CURL_SHORT_VALUE = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
_CURL_LONG_VALUE = frozenset({
    "--output", "--output-dir", "--header", "--data", "--data-raw", "--data-binary",
    "--data-urlencode", "--data-ascii", "--user", "--request", "--user-agent", "--referer",
    "--cookie", "--cookie-jar", "--config", "--max-time", "--connect-timeout", "--retry",
    "--retry-delay", "--retry-max-time", "--proxy", "--proxy-user", "--cacert", "--capath",
    "--cert", "--key", "--form", "--form-string", "--upload-file", "--range", "--write-out",
    "--limit-rate", "--resolve", "--connect-to", "--interface", "--url", "--dump-header",
    "--oauth2-bearer", "--proto", "--proto-redir", "--continue-at", "--time-cond",
    "--max-filesize", "--netrc-file", "--trace", "--trace-ascii", "--stderr", "--variable",
    "--json", "--aws-sigv4", "--max-redirs", "--speed-limit", "--speed-time", "--unix-socket",
})
_WGET_SHORT_VALUE = frozenset("aABDeiIlLoOPQRtTUwX")
_WGET_LONG_VALUE = frozenset({"--output-document", "--directory-prefix", "--output-file",
                              "--append-output", "--user-agent", "--header", "--tries",
                              "--timeout", "--input-file", "--execute", "--user", "--password"})
_GH_API_VALUE = frozenset({"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F",
                           "--field", "-q", "--jq", "-t", "--template", "--hostname", "--input",
                           "--cache", "-p", "--preview"})


# --------------------------------------------------------------------------- public types

@dataclass(frozen=True)
class Decision:
    """The guard's answer.  `action` is "allow" or "deny"; `rule_id` names the rule that fired
    (one of DENY_RULES) or, for an allow, why it passed.  `destination` and `source` are display
    strings for a deny (credentials stripped); both are "" for an allow."""

    action: str
    reason: str = ""
    rule_id: str = ""
    destination: str = ""
    source: str = ""

    @property
    def denied(self) -> bool:
        return self.action == DENY


def redact_url(text: str) -> str:
    """Strip credentials from URLs: https://user:token@host becomes https://REDACTED@host.

    Any user info on an http(s) URL is redacted (a bare token is a credential too); on other
    schemes only user info that carries a password is, so ssh://git@github.com stays readable.
    """
    def fix(m: re.Match[str]) -> str:
        scheme, info = m.group(1), m.group(2)
        if scheme.lower().startswith("http") or ":" in info:
            return f"{scheme}REDACTED@"
        return m.group(0)
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@", fix, text)


class GuardContext:
    """Everything `evaluate` needs, resolved once.  Build it with `make_context`.

    The temp roots, the lanes and review roots and the harness locations come from
    `layout.make_guard_roots` and are already real paths; `evaluate` only compares strings
    against them.  `tmpdir` is what $TMPDIR and a bare `mktemp` resolve to.
    """

    def __init__(self, *, home: str, env: Mapping[str, str], registry: L.Registry, roots: L.Roots,
                 tmpdir: str) -> None:
        self.home = home
        self.env = env
        self.registry = registry
        self.roots = roots
        self.tmpdir = tmpdir
        self.case_insensitive = roots.case_insensitive
        self.fold = str.casefold if self.case_insensitive else (lambda s: s)
        fold = self.fold
        self.fleet_owners = frozenset(o.casefold() for o in (*FLEET_OWNERS, registry.owner) if o)
        self.owner_repos = frozenset(a.owner_repo.casefold() for a in registry.apps if a.owner_repo)
        home_f = fold(home)
        roots_f: list[str] = []
        for root in roots.tmp_roots:
            rf = fold(str(root))
            if rf in roots_f:
                continue
            if home_f == rf or home_f.startswith(rf + "/"):
                continue          # a home inside temp (a test home): that root is skipped
            roots_f.append(rf)
        self.tmp_roots_f = tuple(roots_f)
        self.tmp_globs_f = tuple(tuple(fold(g).split("/")) for g in roots.tmp_globs)
        self.home_parts_f = tuple(home_f.split("/"))
        self.fleet_roots_f = tuple(dict.fromkeys(
            fold(str(p)) for p in (roots.lanes_root, roots.code_root, roots.apps_root)))
        self.code_root_f = fold(str(roots.code_root))
        self.lanes_root_f = fold(str(roots.lanes_root))
        self.harness_f = tuple(
            tuple(fold(loc.glob_or_prefix).split("/"))
            for loc in roots.harness_locations if loc.name != "documents")
        names: list[tuple[str, L.App]] = []
        for app in registry.apps:
            for text in (app.prefix.lower(), *app.prefix_aliases()):
                if text:
                    names.append((text, app))
        names.sort(key=lambda t: -len(t[0]))
        self.app_names = tuple(names)

    def tmp_remainder(self, fk: str) -> str | None:
        """For a folded absolute path inside a temp root, the part after that root ("" for the
        root itself); None when the path is not in temp."""
        for root in self.tmp_roots_f:
            if fk == root:
                return ""
            if fk.startswith(root + "/"):
                return fk[len(root) + 1:]
        comps = fk.split("/")
        for pat in self.tmp_globs_f:
            k = len(pat)
            if len(comps) < k:
                continue
            if all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat)):
                if tuple(self.home_parts_f[:k]) == tuple(comps[:k]):
                    continue
                return "/".join(comps[k:])
        return None


def make_context(env: Mapping[str, str] | None = None, *, home: str | os.PathLike[str] | None = None,
                 registry: L.Registry | None = None, case_insensitive: bool | None = None) -> GuardContext:
    """Resolve the roots and load the registry once.  This is the only filesystem work.

    `env` defaults to the process environment and is read for HOME, TMPDIR, FLEET_LAYOUT,
    FLEET_LANES_ROOT, FLEET_APPS_JSON, AGENT_SEAT and FLEET_LANE_GUARD.  A registry that cannot
    be read falls back to the EXTRA_APPS rows; owner-based detection still works without it.
    `case_insensitive` defaults to true on macOS.
    """
    env = dict(os.environ if env is None else env)
    home_s = os.fspath(home) if home is not None else (env.get("HOME") or os.path.expanduser("~"))
    roots = L.make_guard_roots(home_s, env)
    if case_insensitive is not None:
        roots = dataclasses.replace(roots, case_insensitive=bool(case_insensitive))
    if registry is None:
        try:
            registry = L.load_registry(env=env)
        except (OSError, ValueError):
            registry = L.parse_registry({}, include_extras=True, source="fallback")
    raw_tmp = (env.get("TMPDIR") or "").strip()
    tmpdir = _norm(raw_tmp) if raw_tmp.startswith("/") else "/tmp"
    return GuardContext(home=str(roots.home), env=env, registry=registry, roots=roots, tmpdir=tmpdir)


def evaluate(payload: Any, ctx: GuardContext | None = None) -> Decision:
    """Judge one PreToolUse payload.  Pure string work; fails open on anything unexpected.

    The payload is the hook JSON: Claude Code and Codex send tool_name plus tool_input.command
    and cwd; Cursor's beforeShellExecution sends command, cwd and workspace_roots; Antigravity's
    run_command may send tool_input.CommandLine and Cwd.  `ctx` defaults to `make_context()`.
    """
    try:
        return _evaluate(payload, ctx)
    except Exception:
        return Decision(ALLOW, "", _OK_FAIL_OPEN)


# --------------------------------------------------------------------------- payload

def _extract(payload: Any) -> tuple[str, str, str | None] | None:
    """(command, cwd, scratchpad_dir) from a hook payload, or None when it is not a shell call."""
    if not isinstance(payload, Mapping):
        return None
    tool = payload.get("tool_name", payload.get("toolName"))
    if tool is not None:
        if not isinstance(tool, str):
            return None
        low = tool.strip().lower()
        if not any(h in low for h in _SHELL_TOOL_HINTS):
            return None
    ti = payload.get("tool_input", payload.get("toolInput"))
    if not isinstance(ti, Mapping):
        ti = {}
    cmd: Any = None
    for key in ("command", "cmd", "CommandLine", "commandLine", "script"):
        if ti.get(key) is not None:
            cmd = ti.get(key)
            break
    if cmd is None:
        cmd = payload.get("command")
    if isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd):
        if len(cmd) >= 3 and os.path.basename(cmd[0]) in _SHELLS and cmd[1].startswith("-") and "c" in cmd[1]:
            cmd = cmd[2]
        else:
            cmd = " ".join(shlex.quote(c) for c in cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    # The per-call directory (where this command runs) beats the session's top-level cwd.
    cwd = ""
    for value in (ti.get("workdir"), ti.get("cwd"), ti.get("Cwd"), ti.get("working_directory"),
                  payload.get("cwd")):
        if isinstance(value, str) and value.strip():
            cwd = value.strip()
            break
    if not cwd:
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and roots and isinstance(roots[0], str):
            cwd = roots[0]
    scratch = payload.get("scratchpad_dir", payload.get("scratchpadDir"))
    return cmd, cwd, scratch if isinstance(scratch, str) and scratch.startswith("/") else None


def _evaluate(payload: Any, ctx: GuardContext | None) -> Decision:
    env = ctx.env if ctx is not None else os.environ
    if (env.get(ENV_GUARD) or "").strip().lower() in _OFF_VALUES:
        return Decision(ALLOW, "", _OK_DISABLED)
    got = _extract(payload)
    if got is None:
        return Decision(ALLOW, "", _OK_NOT_SHELL)
    command, cwd, scratch = got
    if len(command) > MAX_COMMAND_CHARS:
        return Decision(ALLOW, "", _OK_TOO_LARGE)
    if not _TRIGGER.search(command):
        return Decision(ALLOW, "", _OK_NO_MATCH)
    if ctx is None:
        ctx = make_context()
    ev = _Evaluator(ctx, scratch)
    st = _State(_norm(cwd) if cwd.startswith("/") else _UNK)
    ev.run(command, st)
    if not ev.hits:
        return Decision(ALLOW, "", _OK_NO_MATCH)
    hit = ev.hits[0]
    return Decision(DENY, ev.reason(hit), hit.rule, ev.display(hit.target), hit.src.label)


# --------------------------------------------------------------------------- path strings

def _norm(path: str) -> str:
    out = os.path.normpath(path)
    if out.startswith("//"):
        out = "/" + out.lstrip("/")
    return out


def _join(base: str, path: str) -> str:
    """`path` against `base` as plain strings.  Unknown parts stay as _UNK; a relative path on an
    unknown base is wholly unknown."""
    if not path:
        return _UNK
    if path.startswith(_UNK):
        return _UNK
    if path.startswith("/"):
        return _norm(path)
    if not base.startswith("/"):
        return _UNK
    return _norm(base + "/" + path)


def _child(base: str, name: str) -> str:
    """`name`, which is always ONE path component (the directory git or gh picks for a clone, a
    mktemp basename), under `base`.  Unlike _join, an unknown name stays a child of a known base:
    it can never be absolute, so /tmp plus an unknown name is still in /tmp."""
    if not name or not base.startswith("/"):
        return _UNK
    return _norm(base + "/" + name)


_EXCLUDE_RE = re.compile(r":(?:[/!^]*[!^]|\([^)]*\bexclude\b)")


def _exclude_pathspec(spec: str) -> bool:
    """True for an exclude pathspec (":!x", ":^x", ":/!x", ":(exclude)x")."""
    return bool(_EXCLUDE_RE.match(spec))


def _whole_tree_pathspec(spec: str, base: str) -> bool:
    """True when a pathspec may select the whole tree as seen from `base`, the directory git runs
    in: empty, ".", a parent of `base`, any pathspec magic (":/", ":(top)", ":(glob)..."), or a
    glob whose fixed directory is one of those ("*", "*.ts", "../*").  A named file or directory
    below `base` is not.  A wholly unknown pathspec counts as a file, so the guard stays fail-open."""
    if spec.startswith(_UNK):
        return False
    if not spec or spec.startswith(":"):
        return True
    m = re.search(r"[*?\[]", spec)
    fixed = spec[:spec.rfind("/", 0, m.start()) + 1] if m else spec
    if base.startswith("/") and _UNK not in base:
        full = _join(base, fixed or ".")
        return full == base or base.startswith(full.rstrip("/") + "/")
    rel = os.path.normpath(fixed or ".")
    return rel in (".", "..") or rel.startswith("../")


def _humanish(repo: str, bare: bool) -> str:
    """The directory git (or gh) picks when the destination is omitted."""
    t = repo.rstrip("/")
    if t.endswith("/.git"):
        t = t[:-5]
    name = re.split(r"[/:]", t)[-1] if t else ""
    if name.endswith(".git"):
        name = name[:-4]
    name = name or "repo"
    return name + ".git" if bare else name


def _github_owner_repo(text: str) -> tuple[str, str] | None:
    """(owner, repo) for a GitHub URL in any common form, else None."""
    t = text.strip()
    if "://" in t:
        try:
            parts = urlsplit(t)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if "github" not in host:
            return None
        segs = [s for s in parts.path.split("/") if s]
        if host == "api.github.com" and segs[:1] == ["repos"]:
            segs = segs[1:]
        if len(segs) < 2:
            return None
        return segs[0], _strip_git(segs[1])
    m = _SCP_RE.match(t)
    if m and "github" in m.group(1).lower():
        return m.group(2), _strip_git(m.group(3))
    return None


def _strip_git(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


# --------------------------------------------------------------------------- lexer

class _Word:
    """One shell word as segments: ("lit", text), ("var", name, op, arg), ("cmd", inner), ("unk",).
    `tilde` is set when the word starts with an unquoted ~."""

    __slots__ = ("segs", "buf", "tilde")

    def __init__(self) -> None:
        self.segs: list[tuple] = []
        self.buf: list[str] = []
        self.tilde = False

    def lit(self, text: str) -> None:
        self.buf.append(text)

    def seg(self, seg: tuple) -> None:
        if self.buf:
            self.segs.append(("lit", "".join(self.buf)))
            self.buf = []
        self.segs.append(seg)

    def done(self) -> "_Word":
        if self.buf:
            self.segs.append(("lit", "".join(self.buf)))
            self.buf = []
        return self

    def empty(self) -> bool:
        return not self.segs and not self.buf

    def plain(self) -> str | None:
        """The text when the word is literal so far, else None."""
        return None if self.segs else "".join(self.buf)


def _read_delim(s: str, i: int) -> tuple[str, int]:
    """A heredoc delimiter word with its quotes removed."""
    n = len(s)
    out: list[str] = []
    while i < n and s[i] not in _META:
        c = s[i]
        if c in "'\"":
            j = s.find(c, i + 1)
            j = n if j < 0 else j
            out.append(s[i + 1:j])
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out), min(i, n)


def _heredoc_bodies(s: str, i: int, pending: list) -> int:
    """Consume the bodies of the pending heredocs starting at line start `i`.  A pending entry is
    (delimiter, strip_tabs, token or None); the body is stored in token[3]."""
    n = len(s)
    for delim, strip, tok in pending:
        lines: list[str] = []
        while i < n:
            j = s.find("\n", i)
            end = n if j < 0 else j
            line = s[i:end]
            i = n if j < 0 else j + 1
            cmp = line.lstrip("\t") if strip else line
            if cmp.rstrip("\r") == delim:
                break
            lines.append(line)
        if tok is not None:
            tok[3] = "\n".join(lines)
    return i


def _skip_dq(s: str, i: int) -> int:
    """Index just past the closing double quote; `i` is just after the opening one."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            _, i = _scan_balanced(s, i + 2)
            continue
        if c == "`":
            i = _skip_backtick(s, i + 1)
            continue
        i += 1
    return n


def _skip_backtick(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        i += 1
    return n


def _scan_balanced(s: str, i: int) -> tuple[str, int]:
    """Inner text of a $( ... ) or ( ... ) starting just after the "(", and the index past the
    matching ")".  Quotes, escapes, comments and heredoc bodies are skipped, so prose with
    apostrophes inside `$(cat <<'EOF' ... EOF)` cannot unbalance it.  Unbalanced input takes the
    rest of the string."""
    n = len(s)
    start = i
    depth = 1
    pending: list = []
    while i < n:
        m = _BAL_RE.search(s, i)
        if m is None:
            break
        i = m.start()
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i = _skip_dq(s, i + 1)
        elif c == "`":
            i = _skip_backtick(s, i + 1)
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
            i += 1
        elif c == "<":
            if s.startswith("<<", i) and not s.startswith("<<<", i):
                k = i + 2
                strip = k < n and s[k] == "-"
                k += 1 if strip else 0
                while k < n and s[k] in " \t":
                    k += 1
                if _DELIM_START_RE.match(s, k):
                    delim, k = _read_delim(s, k)
                    pending.append((delim, strip, None))
                i = k
            else:
                i += 1
        elif c == "#":
            if i == start or s[i - 1] in " \t\n;(|&":
                j = s.find("\n", i)
                i = n if j < 0 else j
            else:
                i += 1
        else:  # newline
            i += 1
            if pending:
                i = _heredoc_bodies(s, i, pending)
                pending = []
    return s[start:], n


def _scan_brace(s: str, i: int) -> int:
    """Index past the "}" closing a ${...} whose body starts at `i`."""
    n = len(s)
    depth = 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i = _skip_dq(s, i + 1)
            continue
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            _, i = _scan_balanced(s, i + 2)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _param_seg(inner: str) -> tuple:
    """Segment for the body of ${...}."""
    if not inner or (inner[0] in "#!" and len(inner) > 1):
        return ("unk",)            # ${#x} length, ${!x} indirection
    m = _PARAM_RE.match(inner)
    if m is None:
        return ("unk",)
    name, rest = m.group(1), inner[m.end():]
    if not rest:
        return ("var", name, None, None)
    for op in (":-", ":=", "-", "="):
        if rest.startswith(op):
            return ("var", name, "default", rest[len(op):])
    for op in (":?", "?"):
        if rest.startswith(op):
            return ("var", name, None, None)
    for op in ("%%", "%", "##", "#"):
        if rest.startswith(op):
            return ("var", name, op, rest[len(op):])
    return ("unk",)


def _read_backtick(s: str, i: int, w: _Word) -> int:
    j = _skip_backtick(s, i)
    end = j - 1 if j <= len(s) and j > i and s[j - 1] == "`" else j
    w.seg(("cmd", s[i:end].replace("\\`", "`")))
    return j


def _read_dollar(s: str, i: int, w: _Word, in_dq: bool) -> int:
    n = len(s)
    j = i + 1
    nx = s[j] if j < n else ""
    if nx == "(":
        if s.startswith("((", j):
            _, k = _scan_balanced(s, j + 1)
            w.seg(("unk",))
            return k
        inner, k = _scan_balanced(s, j + 1)
        w.seg(("cmd", inner))
        return k
    if nx == "{":
        k = _scan_brace(s, j + 1)
        inner = s[j + 1:k - 1] if k - 1 > j and s[k - 1] == "}" else s[j + 1:k]
        w.seg(_param_seg(inner))
        return k
    if nx == "'" and not in_dq:
        k = j + 1
        buf: list[str] = []
        while k < n and s[k] != "'":
            if s[k] == "\\" and k + 1 < n:
                buf.append(s[k + 1])
                k += 2
                continue
            buf.append(s[k])
            k += 1
        w.lit("".join(buf))
        return min(k + 1, n)
    if nx == '"' and not in_dq:
        return _read_dq(s, j + 1, w)
    m = _NAME_RE.match(s, j)
    if m:
        w.seg(("var", m.group(), None, None))
        return m.end()
    if nx and nx in "@*#?$!-0123456789":
        w.seg(("var", nx, None, None))
        return j + 1
    w.lit("$")
    return j


def _read_dq(s: str, i: int, w: _Word) -> int:
    """Body of a double-quoted string; `i` is just after the opening quote."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return i + 1
        if c == "\\":
            if i + 1 < n:
                nx = s[i + 1]
                if nx == "\n":
                    i += 2
                    continue
                if nx in '$`"\\':
                    w.lit(nx)
                    i += 2
                    continue
            w.lit("\\")
            i += 1
            continue
        if c == "$":
            i = _read_dollar(s, i, w, True)
            continue
        if c == "`":
            i = _read_backtick(s, i + 1, w)
            continue
        m = _DQ_PLAIN_RE.match(s, i)
        w.lit(m.group())
        i = m.end()
    return n


def _read_unit(s: str, i: int, w: _Word) -> int:
    """One piece of a word starting at `i`: a quoted span, an expansion, an escape or a plain run."""
    n = len(s)
    c = s[i]
    if c == "'":
        j = s.find("'", i + 1)
        if j < 0:
            w.lit(s[i + 1:])
            return n
        w.lit(s[i + 1:j])
        return j + 1
    if c == '"':
        return _read_dq(s, i + 1, w)
    if c == "\\":
        if i + 1 < n:
            if s[i + 1] == "\n":
                return i + 2
            w.lit(s[i + 1])
            return i + 2
        w.lit("\\")
        return i + 1
    if c == "$":
        return _read_dollar(s, i, w, False)
    if c == "`":
        return _read_backtick(s, i + 1, w)
    if c == "~" and w.empty():
        w.tilde = True
    m = _PLAIN_RE.match(s, i)
    if m:
        w.lit(m.group())
        return m.end()
    w.lit(c)
    return i + 1


def _lex_redirect(s: str, i: int, fd: str, toks: list, pending: list) -> int:
    n = len(s)
    if s.startswith("&>>", i):
        op, i = "&>>", i + 3
    elif s.startswith("&>", i):
        op, i = "&>", i + 2
    elif i + 1 < n and s[i + 1] == "(":
        inner, j = _scan_balanced(s, i + 2)       # process substitution <(...) or >(...)
        w = _Word()
        w.seg(("cmd", inner))
        toks.append(("w", w.done()))
        return j
    else:
        op = s[i]
        for cand in ("<<<", "<<-", "<<", "<>", "<&", ">>", ">|", ">&"):
            if s.startswith(cand, i):
                op = cand
                break
        i += len(op)
    while i < n and s[i] in " \t":
        i += 1
    if op in ("<<", "<<-"):
        if not _DELIM_START_RE.match(s, i):
            toks.append(["r", op, None, None, fd])
            return i
        delim, i = _read_delim(s, i)
        tok = ["r", op, None, None, fd]
        toks.append(tok)
        pending.append((delim, op == "<<-", tok))
        return i
    w = None
    while i < n and s[i] not in _META:
        if w is None:
            w = _Word()
        i = _read_unit(s, i, w)
    toks.append(["r", op, w.done() if w is not None else None, None, fd])
    return i


def _lex(s: str) -> list:
    """Tokens: ("w", _Word), ("op", text) for ; && || | & newline ( ) ;;, and redirections as
    ["r", op, target _Word or None, heredoc body or None, fd]."""
    toks: list = []
    n = len(s)
    i = 0
    word: _Word | None = None
    pending: list = []
    while i < n:
        c = s[i]
        if c in " \t\r":
            if word is not None:
                toks.append(("w", word.done()))
                word = None
            i += 1
            continue
        if c == "\n":
            if word is not None:
                toks.append(("w", word.done()))
                word = None
            toks.append(("op", "\n"))
            i += 1
            if pending:
                i = _heredoc_bodies(s, i, pending)
                pending = []
            continue
        if c == "\\" and i + 1 < n and s[i + 1] == "\n":
            i += 2
            continue
        if c == "#" and word is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "<>" or (c == "&" and s.startswith("&>", i)):
            fd = ""
            if word is not None:
                text = word.plain()
                if c in "<>" and text and text.isdigit():
                    fd = text
                else:
                    toks.append(("w", word.done()))
                word = None
            i = _lex_redirect(s, i, fd, toks, pending)
            continue
        if c in ";&|()":
            if c == "(" and word is not None:
                text = word.plain()
                if text is not None and _ASSIGN_PREFIX_RE.match(text):
                    inner, i = _scan_balanced(s, i + 1)     # array literal a=( ... )
                    word.lit("(" + inner + ")")
                    continue
            if word is not None:
                toks.append(("w", word.done()))
                word = None
            if c == ";":
                if s.startswith(";;&", i):
                    op, i = ";;", i + 3
                elif s.startswith(";;", i) or s.startswith(";&", i):
                    op, i = ";;", i + 2
                else:
                    op, i = ";", i + 1
            elif c == "&":
                op, i = ("&&", i + 2) if s.startswith("&&", i) else ("&", i + 1)
            elif c == "|":
                if s.startswith("||", i):
                    op, i = "||", i + 2
                elif s.startswith("|&", i):
                    op, i = "|", i + 2
                else:
                    op, i = "|", i + 1
            elif c == "(":
                j = i + 1
                while j < n and s[j] in " \t":
                    j += 1
                if j < n and s[j] == ")":
                    i = j + 1          # the () of a function definition
                    continue
                op, i = "(", i + 1
            else:
                op, i = ")", i + 1
            toks.append(("op", op))
            continue
        if word is None:
            word = _Word()
        i = _read_unit(s, i, word)
    if word is not None:
        toks.append(("w", word.done()))
    return toks


def _word_from_text(text: str) -> _Word:
    """A word built from raw text (a ${x:-default} default), with metacharacters kept literal."""
    w = _Word()
    i, n = 0, len(text)
    while i < n:
        if text[i] in _META:
            w.lit(text[i])
            i += 1
        else:
            i = _read_unit(text, i, w)
    return w.done()


# --------------------------------------------------------------------------- evaluation

class _State:
    """What one shell knows while the command runs.  `fleet_files` (downloaded fleet tarballs and
    archives) and `remotes` (temp dirs given a fleet remote) are filesystem effects, so a
    subshell shares them; cwd, the dir stack and variables are scoped."""

    __slots__ = ("cwd", "oldpwd", "dirstack", "vars", "fleet_files", "remotes")

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.oldpwd = _UNK
        self.dirstack: list[str] = []
        self.vars: dict[str, str] = {}
        self.fleet_files: dict[str, _Src] = {}
        self.remotes: dict[str, _Src] = {}

    def scoped_copy(self) -> "_State":
        c = _State.__new__(_State)
        c.cwd, c.oldpwd = self.cwd, self.oldpwd
        c.dirstack, c.vars = list(self.dirstack), dict(self.vars)
        c.fleet_files, c.remotes = self.fleet_files, self.remotes
        return c

    def restore(self, saved: "_State") -> None:
        self.cwd, self.oldpwd = saved.cwd, saved.oldpwd
        self.dirstack, self.vars = saved.dirstack, saved.vars


@dataclass(frozen=True)
class _Src:
    """A fleet source: `label` is safe to show (OWNER/REPO or a ~ path), `kind` is repo, archive
    or tarball."""

    label: str
    app: L.App | None = None
    prefix: str | None = None
    owner_repo: str = ""
    kind: str = "repo"


@dataclass(frozen=True)
class _Hit:
    rule: str
    target: str
    src: _Src
    what: str


def _derive_prefix(name: str) -> str | None:
    p = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return p or None


class _Evaluator:
    def __init__(self, ctx: GuardContext, scratch: str | None) -> None:
        self.ctx = ctx
        self.depth = 0
        self.hits: list[_Hit] = []
        fold = ctx.fold
        dirs: list[str] = []
        if scratch:
            sd = _norm(scratch)
            dirs.append(sd)
            for a, b in (("/private/tmp/", "/tmp/"), ("/tmp/", "/private/tmp/"),
                         ("/private/var/", "/var/"), ("/var/", "/private/var/")):
                if sd.startswith(a):
                    dirs.append(b + sd[len(a):])
        self.scratch_f = tuple(fold(d) for d in dirs)

    # ---- driving

    def run(self, text: str, st: _State) -> None:
        self._run_tokens(_lex(text), st)

    def _recurse(self, text: str, st: _State) -> None:
        if self.depth >= _MAX_DEPTH or not _TRIGGER.search(text):
            return
        self.depth += 1
        try:
            self._run_tokens(_lex(text), st)
        finally:
            self.depth -= 1

    def _run_tokens(self, toks: list, st: _State) -> None:
        words: list[_Word] = []
        redirs: list = []
        stack: list[tuple[_State, Any]] = []
        group_in: Any = None
        last_out: Any = None
        after_pipe = False
        for tok in toks:
            kind = tok[0]
            if kind == "w":
                words.append(tok[1])
                continue
            if kind == "r":
                redirs.append(tok)
                continue
            op = tok[1]
            if words or redirs:
                last_out = self._safe_command(words, redirs, st, last_out if after_pipe else group_in)
                words, redirs = [], []
                after_pipe = False
                if self.hits:
                    return
            if op == "|":
                after_pipe = True
            elif op == "(":
                stack.append((st.scoped_copy(), group_in))
                if after_pipe:
                    group_in = last_out
                after_pipe = False
                last_out = None
            elif op == ")":
                if stack:
                    saved, group_in = stack.pop()
                    st.restore(saved)
                after_pipe = False        # last_out stays: it is the group's output
            else:
                after_pipe = False
                last_out = None
        if words or redirs:
            self._safe_command(words, redirs, st, last_out if after_pipe else group_in)

    def _safe_command(self, words: list[_Word], redirs: list, st: _State, stdin: Any) -> Any:
        try:
            return self._command(words, redirs, st, stdin)
        except Exception:
            return None            # one odd segment never stops checks on the others

    # ---- expansion

    def _var(self, name: str, st: _State) -> str | None:
        if name in st.vars:
            return st.vars[name]
        if name == "HOME":
            return self.ctx.home
        if name == "TMPDIR":
            return self.ctx.tmpdir
        if name == "PWD":
            return st.cwd
        if name == "OLDPWD":
            return st.oldpwd
        if name in _SPECIAL_ZERO:
            return "0"
        value = self.ctx.env.get(name)
        return value if isinstance(value, str) else None

    def _param(self, seg: tuple, st: _State) -> str | None:
        _, name, op, arg = seg
        value = self._var(name, st)
        if op == "default":
            if value is None or value == "":
                value = self._expand(_word_from_text(arg or ""), st)
            return value
        if op in ("%", "%%", "#", "##"):
            if value is None:
                return None
            pat = self._expand(_word_from_text(arg or ""), st)
            if not pat or _UNK in pat or any(ch in pat for ch in "*?["):
                return None if pat else value
            if op[0] == "%":
                return value[:-len(pat)] if value.endswith(pat) else value
            return value[len(pat):] if value.startswith(pat) else value
        return value

    def _tilde(self, s: str, st: _State) -> str:
        m = re.match(r"~([^/]*)", s)
        if m is None:
            return s
        user, rest = m.group(1), s[m.end():]
        if user == "":
            base = self.ctx.home
        elif user == "+":
            base = st.cwd
        elif user == "-":
            base = st.oldpwd
        elif self.ctx.fold(user) == self.ctx.fold(os.path.basename(self.ctx.home)):
            base = self.ctx.home
        else:
            base = _UNK
        return base + rest

    def _expand(self, word: _Word, st: _State) -> str:
        parts: list[str] = []
        for seg in word.segs:
            k = seg[0]
            if k == "lit":
                parts.append(seg[1])
            elif k == "var":
                v = self._param(seg, st)
                parts.append(_UNK if v is None else v)
            elif k == "cmd":
                v = self._subst(seg[1], st)
                parts.append(_UNK if v is None else v)
            else:
                parts.append(_UNK)
        s = "".join(parts)
        return self._tilde(s, st) if word.tilde else s

    def _subst(self, inner: str, st: _State) -> str | None:
        """Evaluate $(inner) for denials, and return its output when the guard can know it
        (mktemp, pwd, echo, realpath)."""
        if self.depth >= _MAX_DEPTH:
            return None
        self.depth += 1
        try:
            toks = _lex(inner)
            if _TRIGGER.search(inner):
                self._run_tokens(toks, st.scoped_copy())
            first: list[_Word] = []
            for tok in toks:
                if tok[0] == "w":
                    first.append(tok[1])
                elif tok[0] == "op" and first:
                    break
            if not first:
                return None
            args = [self._expand(w, st) for w in first]
            name = os.path.basename(args[0])
            if name == "mktemp":
                return self._mktemp(args[1:], st)
            if name == "pwd":
                return st.cwd
            if name == "echo":
                return " ".join(a for a in args[1:] if not a.startswith("-"))
            if name in ("realpath", "readlink"):
                pos = [a for a in args[1:] if not a.startswith("-")]
                return self._path(pos[0], st) if pos else None
            return None
        finally:
            self.depth -= 1

    def _mktemp(self, args: list[str], st: _State) -> str:
        """Where `mktemp` would create its file or directory.  No template, -t, or --tmpdir means
        TMPDIR (or /tmp); -p DIR means DIR; an explicit template is a path."""
        use_tmp = False
        dir_: str | None = None
        template: str | None = None
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a == "--":
                if i < len(args):
                    template = args[i]
                break
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name == "--tmpdir":
                    if eq and val:
                        dir_ = val
                    else:
                        use_tmp = True
                elif name == "--suffix" and not eq:
                    i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                for k, ch in enumerate(a[1:], 1):
                    if ch == "t":
                        use_tmp = True
                    elif ch == "p":
                        tail = a[k + 1:]
                        dir_ = tail or (args[i] if i < len(args) else "")
                        if not tail:
                            i += 1
                        break
                continue
            template = a
        base_tmp = self._var("TMPDIR", st) or "/tmp"
        if dir_:
            return _child(self._path(dir_, st), os.path.basename(template) if template else "tmp.XXXXXXXXXX")
        if template is None:
            return _child(base_tmp, "tmp.XXXXXXXXXX")
        if use_tmp:
            return _child(base_tmp, os.path.basename(template))
        return self._path(template, st)

    def _path(self, text: str, st: _State) -> str:
        return _join(st.cwd, text)

    def _assignment(self, word: _Word, expanded: str, st: _State) -> tuple[str, str] | None:
        if not word.segs or word.segs[0][0] != "lit":
            return None
        first = word.segs[0][1]
        m = _ASSIGN_RE.match(first)
        if m is None:
            return None
        value = expanded[m.end():]
        if first[m.end():].startswith("~"):
            value = self._tilde(value, st)
        if m.group(2):
            value = (st.vars.get(m.group(1)) or "") + value
        return m.group(1), value

    # ---- classification

    def _where(self, path: str) -> str:
        """scratch, temp, other or unknown.  A partly unknown path is judged by its known leading
        directory, so /tmp/$X/repo is temp and $X/repo is unknown."""
        if not path or not path.startswith("/"):
            return "unknown"
        k = path.find(_UNK)
        if k >= 0:
            prefix = path[:k]
            known = prefix if prefix.endswith("/") else os.path.dirname(prefix)
            known = _norm(known) if known else "/"
        else:
            known = path
        fk = self.ctx.fold(known)
        for sd in self.scratch_f:
            if fk == sd or fk.startswith(sd + "/"):
                return "scratch"
        rem = self.ctx.tmp_remainder(fk)
        if rem is not None:
            if _CLAUDE_SCRATCH_RE.fullmatch(rem.split("/", 1)[0]):
                return "scratch"
            return "temp"
        return "other" if k < 0 else "unknown"

    def _app_from_name(self, name: str) -> L.App | None:
        if not name or _UNK in name:
            return None
        app = self.ctx.registry.app_by_name(name)
        if app is not None:
            return app
        low = name.lower()
        for text, cand in self.ctx.app_names:
            if low == text or low.startswith(text + "-"):
                return cand
        return None

    def _fleet_path_src(self, path: str) -> _Src | None:
        """A fleet source when `path` is under ~/Code/<child>, ~/apps/<child>, the lanes root or a
        harness worktree location."""
        if not path or not path.startswith("/"):
            return None
        fold = self.ctx.fold
        fp = fold(path)
        k = fp.find(_UNK)
        known = fp if k < 0 else fp[:k]
        for root in self.ctx.fleet_roots_f:
            if known.startswith(root + "/") or (k >= 0 and known == root + "/"):
                rel = known[len(root) + 1:]
                first = rel.split("/", 1)[0] if rel else ""
                if not first and k < 0:
                    continue
                app = None
                if first and (k < 0 or len(rel) > len(first)):
                    if root == self.ctx.lanes_root_f:
                        app = self.ctx.registry.app_by_prefix(first) or self._app_from_name(first)
                    else:
                        app = self._app_from_name(first)
                return _Src(self.display(path), app, app.prefix if app else None,
                             app.owner_repo if app else "")
        comps = known.split("/")
        for pat in self.ctx.harness_f:
            n = len(pat)
            if len(comps) > n and all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat)):
                app = None
                for comp in comps[n:]:            # below the harness root only (.botfleet is not BotFleet)
                    app = self._app_from_name(comp)
                    if app is not None:
                        break
                return _Src(self.display(path), app, app.prefix if app else None,
                            app.owner_repo if app else "")
        return None

    def _repo_src(self, owner: str, repo: str, kind: str = "repo") -> _Src | None:
        if _UNK in owner:
            return None
        owner_repo = f"{owner}/{repo}"
        if owner.casefold() not in self.ctx.fleet_owners and owner_repo.casefold() not in self.ctx.owner_repos:
            return None
        app = self._app_from_name(repo)
        label = owner_repo.replace(_UNK, "*")
        prefix = app.prefix if app else (_derive_prefix(repo) if _UNK not in repo else None)
        return _Src(label, app, prefix, label if _UNK not in repo else "", kind)

    def _source(self, repo: str, base: str) -> _Src | None:
        """Fleet source for a clone or remote URL, or a local path resolved against `base`."""
        if not repo or repo.startswith(_UNK):
            return None
        gh = _github_owner_repo(repo)
        if gh is not None:
            return self._repo_src(*gh)
        if repo.lower().startswith("file://"):
            return self._fleet_path_src(_join(base, repo[7:]))
        if "://" in repo or re.match(r"^[^/:]+:", repo):
            return None
        return self._fleet_path_src(_join(base, repo))

    def _tarball_src(self, url: str) -> _Src | None:
        for rx in _TARBALL_RES:
            m = rx.match(url)
            if m:
                return self._repo_src(m.group(1), _strip_git(m.group(2)), "tarball")
        return None

    def _check(self, rule: str, target: str, src: _Src, what: str) -> None:
        if self._where(target) == "temp":
            self.hits.append(_Hit(rule, target, src, what))

    def _stdout_target(self, redirs: list, st: _State) -> str | None:
        out = None
        for r in redirs:
            op, target, fd = r[1], r[2], r[4]
            if target is None:
                continue
            if (op in (">", ">>", ">|") and fd in ("", "1")) or op in ("&>", "&>>"):
                out = self._path(self._expand(target, st), st)
        return out

    # ---- commands

    def _command(self, words: list[_Word], redirs: list, st: _State, stdin: Any) -> Any:
        args = [self._expand(w, st) for w in words]
        n = len(args)
        i = 0
        while i < n and args[i] in _RESERVED:
            i += 1
        if i < n and args[i] in _HEADERS:
            return None
        assigns: list[tuple[str, str]] = []
        while i < n:
            a = self._assignment(words[i], args[i], st)
            if a is None:
                break
            assigns.append(a)
            i += 1
        if i >= n:
            for name, value in assigns:
                st.vars[name] = value
            return None
        i = self._skip_wrappers(args, i)
        if i >= n:
            return None
        name = os.path.basename(args[i])
        rest = args[i + 1:]
        if name in ("cd", "chdir"):
            self._cd(rest, st)
        elif name == "pushd":
            self._pushd(rest, st)
        elif name == "popd":
            st.oldpwd = st.cwd
            st.cwd = st.dirstack.pop() if st.dirstack else _UNK
        elif name in ("export", "declare", "typeset", "local", "readonly"):
            for a in rest:
                m = _ASSIGN_RE.match(a)
                if m and not m.group(2):
                    st.vars[m.group(1)] = a[m.end():]
        elif name == "git":
            return self._git(rest, redirs, st)
        elif name == "gh":
            return self._gh(rest, redirs, st)
        elif name == "curl":
            return self._curl(rest, redirs, st)
        elif name == "wget":
            return self._wget(rest, redirs, st)
        elif name in ("tar", "bsdtar", "gtar"):
            self._tar(rest, st, stdin)
        elif name == "unzip":
            self._unzip(rest, st)
        elif name in _SHELLS:
            self._shell(rest, redirs, st)
        elif name == "eval":
            self._recurse(" ".join(rest), st)
        elif name in _PASSTHRU:
            return stdin
        return None

    def _skip_wrappers(self, args: list[str], i: int) -> int:
        n = len(args)
        while i < n:
            name = os.path.basename(args[i])
            if name == "env":
                i += 1
                while i < n:
                    b = args[i]
                    if b in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                        i += 2
                    elif (b.startswith("-") and len(b) > 1) or b == "-" or _ASSIGN_RE.match(b):
                        i += 1
                    else:
                        break
            elif name in ("command", "builtin", "exec", "nohup", "noglob", "time"):
                i += 1
                while i < n and args[i].startswith("-") and len(args[i]) > 1:
                    i += 1
            elif name == "nice":
                i += 1
                if i < n and args[i] == "-n":
                    i += 2
                elif i < n and args[i].startswith("-"):
                    i += 1
            elif name == "timeout":
                i += 1
                while i < n and args[i].startswith("-"):
                    i += 2 if args[i] in ("-s", "-k") else 1
                i += 1             # the duration
            elif name == "sudo":
                i += 1
                while i < n and args[i].startswith("-"):
                    i += 2 if args[i] in ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T") else 1
            elif name == "caffeinate":
                i += 1
                while i < n and args[i].startswith("-"):
                    i += 2 if args[i] in ("-t", "-w") else 1
            else:
                break
        return i

    def _cd(self, args: list[str], st: _State) -> None:
        pos = [a for a in args if a not in ("-L", "-P", "-e", "-@", "--")]
        if not pos:
            target = self.ctx.home
        elif pos[0] == "-":
            target = st.oldpwd
        else:
            target = self._path(pos[0], st)
        st.oldpwd, st.cwd = st.cwd, target

    def _pushd(self, args: list[str], st: _State) -> None:
        pos = [a for a in args if a != "-n"]
        if not pos:
            if st.dirstack:
                top = st.dirstack.pop()
                st.dirstack.append(st.cwd)
                st.oldpwd, st.cwd = st.cwd, top
            return
        if pos[0][:1] in "+-" and pos[0][1:].isdigit():
            st.oldpwd, st.cwd = st.cwd, _UNK
            return
        st.dirstack.append(st.cwd)
        st.oldpwd, st.cwd = st.cwd, self._path(pos[0], st)

    def _shell(self, rest: list[str], redirs: list, st: _State) -> None:
        j = 0
        while j < len(rest):
            a = rest[j]
            if a == "--":
                j += 1
                break
            if a.startswith("-") and len(a) > 1 and not a.startswith("--"):
                if "c" in a[1:]:
                    if j + 1 < len(rest):
                        self._recurse(rest[j + 1], st.scoped_copy())
                    return
                j += 2 if a in ("-o", "+o", "-O", "+O") else 1
                continue
            if a.startswith("--") or a.startswith("+"):
                j += 2 if a in ("--rcfile", "--init-file") else 1
                continue
            break
        if j < len(rest) and rest[j] != "-":
            return                 # a script file: not read
        for r in redirs:
            if r[1] in ("<<", "<<-") and r[3]:
                self._recurse(r[3], st.scoped_copy())
            elif r[1] == "<<<" and r[2] is not None:
                self._recurse(self._expand(r[2], st), st.scoped_copy())

    # ---- git

    def _git(self, args: list[str], redirs: list, st: _State) -> Any:
        eff = st.cwd
        gitdir: str | None = None
        i = 0
        n = len(args)
        while i < n:
            a = args[i]
            if a == "-C":
                if i + 1 < n:
                    eff = _join(eff, args[i + 1])
                i += 2
            elif a in ("--git-dir", "--work-tree"):
                if a == "--git-dir" and i + 1 < n:
                    gitdir = args[i + 1]
                i += 2
            elif a.startswith("--git-dir="):
                gitdir = a.split("=", 1)[1]
                i += 1
            elif a in _GIT_GLOBAL_VALUE:
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                break
        if i >= n:
            return None
        sub, rest = args[i], args[i + 1:]
        repo_dir = _join(eff, gitdir) if gitdir else eff
        if sub == "clone":
            self._clone(rest, eff)
        elif sub == "worktree":
            self._worktree(rest, eff, repo_dir)
        elif sub == "archive":
            return self._archive(rest, eff, repo_dir, redirs, st)
        elif sub == "remote":
            self._remote(rest, eff, st)
        elif sub in ("fetch", "pull"):
            self._fetch(rest, eff, st)
        elif sub == "checkout-index":
            self._checkout_index(rest, eff, repo_dir)
        return None

    def _clone(self, rest: list[str], eff: str) -> None:
        pos: list[str] = []
        bare = False
        sep_dir: str | None = None
        i = 0
        while i < len(rest):
            a = rest[i]
            i += 1
            if a == "--":
                pos.extend(rest[i:])
                break
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name in ("--bare", "--mirror"):
                    bare = True
                elif name in _CLONE_VALUE and not eq:
                    val = rest[i] if i < len(rest) else ""
                    i += 1
                if name == "--separate-git-dir":
                    sep_dir = val
                continue
            if a.startswith("-") and len(a) > 1:
                if a in _CLONE_VALUE:
                    i += 1
                continue
            pos.append(a)
        if not pos:
            return
        src = self._source(pos[0], eff)
        if src is None:
            return
        target = _join(eff, pos[1]) if len(pos) > 1 else _child(eff, _humanish(pos[0], bare))
        self._check(RULE_CLONE, target, src, "a bare clone" if bare else "a clone")
        if sep_dir and not self.hits:
            self._check(RULE_CLONE, _join(eff, sep_dir), src, "the git directory of a clone")

    def _worktree(self, rest: list[str], eff: str, repo_dir: str) -> None:
        if not rest:
            return
        op, args = rest[0], rest[1:]
        if op == "add":
            pos: list[str] = []
            i = 0
            while i < len(args):
                a = args[i]
                i += 1
                if a == "--":
                    pos.extend(args[i:])
                    break
                if a in _WT_ADD_VALUE:
                    i += 1
                    continue
                if a.startswith("-") and len(a) > 1:
                    continue
                pos.append(a)
            if pos:
                src = self._fleet_path_src(repo_dir)
                if src is not None:
                    self._check(RULE_WORKTREE_ADD, _join(eff, pos[0]), src, "a worktree")
        elif op == "move":
            pos = [a for a in args if not (a.startswith("-") and len(a) > 1)]
            if len(pos) >= 2:
                src = self._fleet_path_src(repo_dir)
                if src is not None:
                    self._check(RULE_WORKTREE_MOVE, _join(eff, pos[1]), src, "a worktree")

    def _archive(self, rest: list[str], eff: str, repo_dir: str, redirs: list, st: _State) -> Any:
        out: str | None = None
        remote: str | None = None
        pos: list[str] = []
        i = 0
        while i < len(rest):
            a = rest[i]
            i += 1
            if a == "--":
                pos.extend(rest[i:])
                break
            if a in ("-o", "--output"):
                out = rest[i] if i < len(rest) else None
                i += 1
            elif a.startswith("--output="):
                out = a.split("=", 1)[1]
            elif a.startswith("-o") and len(a) > 2:
                out = a[2:]
            elif a == "--remote":
                remote = rest[i] if i < len(rest) else None
                i += 1
            elif a.startswith("--remote="):
                remote = a.split("=", 1)[1]
            elif a in _ARCHIVE_VALUE:
                i += 1
            elif not (a.startswith("-") and len(a) > 1):
                pos.append(a)
        # The tree-ish comes first; the rest are pathspecs.  Named files or directories are an
        # extraction, not a checkout (the update-botfleet.sh bootstrap pulls one script this way),
        # so nothing is denied or remembered.  Excludes alone, or a pathspec that may be the whole
        # tree (".", ":/", "*"), still count as the whole tree.
        include = [p for p in pos[1:] if not _exclude_pathspec(p)]
        if include and not any(_whole_tree_pathspec(p, eff) for p in include):
            return None
        src = self._source(remote, eff) if remote else self._fleet_path_src(repo_dir)
        if src is None:
            return None
        src = dataclasses.replace(src, kind="archive")
        target = _join(eff, out) if out else self._stdout_target(redirs, st)
        if target is None or out == "-":
            return src                     # streamed: a later `| tar -x` decides
        if self._where(target) == "temp":
            self.hits.append(_Hit(RULE_ARCHIVE, target, src, "an archive"))
        else:
            st.fleet_files[self.ctx.fold(target)] = src
        return None

    def _remote(self, rest: list[str], eff: str, st: _State) -> None:
        if not rest or rest[0] != "add":
            return
        fetch_now = False
        pos: list[str] = []
        args = rest[1:]
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a in ("-f", "--fetch"):
                fetch_now = True
            elif a in ("-t", "-m", "--track", "--master"):
                i += 1
            elif not a.startswith("-"):
                pos.append(a)
        if len(pos) < 2:
            return
        src = self._source(pos[1], eff)
        if src is None or self._where(eff) != "temp":
            return
        st.remotes[self.ctx.fold(eff)] = src
        if fetch_now:
            self.hits.append(_Hit(RULE_INIT_FETCH, eff, src, "a fetched checkout"))

    def _fetch(self, rest: list[str], eff: str, st: _State) -> None:
        if self._where(eff) != "temp":
            return
        src = st.remotes.get(self.ctx.fold(eff))
        if src is None:
            # A URL, or a local path (absolute once ~ is expanded).  Remote names and refspecs
            # like `origin` or `main` stay relative and are skipped.
            for a in rest:
                if not a.startswith("-") and ("://" in a or "@" in a or a.startswith("/")):
                    src = self._source(a, eff)
                    if src is not None:
                        break
        if src is not None:
            self.hits.append(_Hit(RULE_INIT_FETCH, eff, src, "a fetched checkout"))

    def _checkout_index(self, rest: list[str], eff: str, repo_dir: str) -> None:
        """Deny an export of the whole index: -a/--all, or no named files (`--stdin`, fed by
        `git ls-files`, is the usual full-export idiom).  Named files are an extraction."""
        prefix = None
        every = False
        paths: list[str] = []
        i = 0
        while i < len(rest):
            a = rest[i]
            i += 1
            if a == "--":
                paths.extend(rest[i:])
                break
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name == "--all":
                    every = True
                elif name in ("--prefix", "--stage"):
                    if not eq:
                        val = rest[i] if i < len(rest) else ""
                        i += 1
                    if name == "--prefix":
                        prefix = val
                continue
            if a.startswith("-") and len(a) > 1:
                every = every or "a" in a[1:]          # -a, also inside -fa or -uaf
                continue
            paths.append(a)
        if not prefix or (paths and not every):
            return
        src = self._fleet_path_src(repo_dir)
        if src is not None:
            self._check(RULE_EXPORT, _join(eff, prefix), src, "an exported tree")

    # ---- gh, curl, wget, tar, unzip

    def _gh(self, rest: list[str], redirs: list, st: _State) -> Any:
        if len(rest) >= 2 and rest[0] == "repo" and rest[1] == "clone":
            self._gh_clone(rest[2:], st)
            return None
        if rest and rest[0] == "api":
            return self._gh_api(rest[1:], redirs, st)
        return None

    def _gh_source(self, repo: str) -> _Src | None:
        if not repo or _UNK in repo.split("/")[0]:
            return None
        if "://" in repo or "@" in repo or ":" in repo:
            gh = _github_owner_repo(repo)
            return self._repo_src(*gh) if gh else None
        parts = [p for p in repo.split("/") if p]
        if len(parts) == 3 and "github" in parts[0].lower():
            parts = parts[1:]
        if len(parts) == 2:
            return self._repo_src(parts[0], _strip_git(parts[1]))
        if len(parts) == 1:
            app = self.ctx.registry.app_by_name(_strip_git(parts[0]))
            if app is not None:
                label = app.owner_repo or app.name
                return _Src(label, app, app.prefix, app.owner_repo)
        return None

    def _gh_clone(self, args: list[str], st: _State) -> None:
        pos: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a == "--":
                break
            if a in ("-u", "--upstream-remote-name"):
                i += 1
            elif not (a.startswith("-") and len(a) > 1):
                pos.append(a)
        if not pos:
            return
        src = self._gh_source(pos[0])
        if src is None:
            return
        target = self._path(pos[1], st) if len(pos) > 1 else _child(st.cwd, _humanish(pos[0], False))
        self._check(RULE_GH_CLONE, target, src, "a clone")

    def _download(self, src: _Src, target: str | None, st: _State) -> Any:
        """A fleet tarball saved to `target` (None means stdout, which a pipe may carry)."""
        if target is None:
            return src
        if self._where(target) == "temp":
            self.hits.append(_Hit(RULE_TARBALL, target, src, "a downloaded tarball"))
        else:
            st.fleet_files[self.ctx.fold(target)] = src
        return None

    def _gh_api(self, args: list[str], redirs: list, st: _State) -> Any:
        endpoint = None
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a in _GH_API_VALUE:
                i += 1
            elif not a.startswith("-") and endpoint is None:
                endpoint = a
        m = _GH_API_TARBALL_RE.match(endpoint or "")
        if m is None:
            return None
        src = self._repo_src(m.group(1), m.group(2), "tarball")
        if src is None:
            return None
        return self._download(src, self._stdout_target(redirs, st), st)

    def _curl(self, args: list[str], redirs: list, st: _State) -> Any:
        urls: list[str] = []
        outputs: list[str] = []
        remote_name = False
        outdir: str | None = None
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name in _CURL_LONG_VALUE:
                    if not eq:
                        val = args[i] if i < len(args) else ""
                        i += 1
                    if name == "--output":
                        outputs.append(val)
                    elif name == "--output-dir":
                        outdir = val
                    elif name == "--url":
                        urls.append(val)
                elif name in ("--remote-name", "--remote-name-all"):
                    remote_name = True
                continue
            if a.startswith("-") and len(a) > 1:
                for k, ch in enumerate(a[1:], 1):
                    if ch == "O":
                        remote_name = True
                    if ch in _CURL_SHORT_VALUE:
                        tail = a[k + 1:]
                        val = tail or (args[i] if i < len(args) else "")
                        if not tail:
                            i += 1
                        if ch == "o":
                            outputs.append(val)
                        break
                continue
            urls.append(a)
        base = self._path(outdir, st) if outdir else st.cwd
        stream = None
        for idx, url in enumerate(urls):
            src = self._tarball_src(url)
            if src is None:
                continue
            out = outputs[idx] if idx < len(outputs) else None
            if out == "-":
                target = None
            elif out:
                target = _join(base, out)
            elif remote_name:
                tail = urlsplit(url.replace(_UNK, "_")).path.rstrip("/").rsplit("/", 1)[-1] or "download"
                target = _join(base, tail)
            else:
                target = self._stdout_target(redirs, st)
            got = self._download(src, target, st)
            if self.hits:
                return None
            stream = stream or got
        return stream

    def _wget(self, args: list[str], redirs: list, st: _State) -> Any:
        urls: list[str] = []
        doc: str | None = None
        prefix: str | None = None
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name in _WGET_LONG_VALUE:
                    if not eq:
                        val = args[i] if i < len(args) else ""
                        i += 1
                    if name == "--output-document":
                        doc = val
                    elif name == "--directory-prefix":
                        prefix = val
                continue
            if a.startswith("-") and len(a) > 1:
                for k, ch in enumerate(a[1:], 1):
                    if ch in _WGET_SHORT_VALUE:
                        tail = a[k + 1:]
                        val = tail or (args[i] if i < len(args) else "")
                        if not tail:
                            i += 1
                        if ch == "O":
                            doc = val
                        elif ch == "P":
                            prefix = val
                        break
                continue
            urls.append(a)
        stream = None
        for url in urls:
            src = self._tarball_src(url)
            if src is None:
                continue
            if doc == "-":
                target = self._stdout_target(redirs, st)
            elif doc:
                target = self._path(doc, st)
            else:
                tail = urlsplit(url.replace(_UNK, "_")).path.rstrip("/").rsplit("/", 1)[-1] or "index.html"
                target = _join(self._path(prefix, st) if prefix else st.cwd, tail)
            got = self._download(src, target, st)
            if self.hits:
                return None
            stream = stream or got
        return stream

    def _extract_into(self, src: _Src | None, dest: str) -> None:
        if src is None:
            return
        if src.kind == "archive":
            self._check(RULE_ARCHIVE, dest, src, "an extracted archive")
        else:
            self._check(RULE_TARBALL, dest, src, "an extracted tarball")

    def _tar(self, args: list[str], st: _State, stdin: Any) -> None:
        extract = False
        dest: str | None = None
        infile: str | None = None
        i = 0
        if args and re.fullmatch(r"[A-Za-z]+", args[0]) and any(op in args[0] for op in "xtcru"):
            letters = args[0]                         # old style: tar xzf FILE
            i = 1
            for ch in letters:
                if ch == "x":
                    extract = True
                if ch in _TAR_SHORT_VALUE:
                    val = args[i] if i < len(args) else None
                    i += 1
                    if ch == "f":
                        infile = val
                    elif ch == "C":
                        dest = val
        while i < len(args):
            a = args[i]
            i += 1
            if a == "--":
                break
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if name in ("--extract", "--get"):
                    extract = True
                elif name in ("--file", "--directory"):
                    if not eq:
                        val = args[i] if i < len(args) else ""
                        i += 1
                    if name == "--file":
                        infile = val
                    else:
                        dest = val
                continue
            if a.startswith("-") and len(a) > 1:
                for k, ch in enumerate(a[1:], 1):
                    if ch == "x":
                        extract = True
                    if ch in _TAR_SHORT_VALUE:
                        tail = a[k + 1:]
                        val = tail or (args[i] if i < len(args) else "")
                        if not tail:
                            i += 1
                        if ch == "f":
                            infile = val
                        elif ch == "C":
                            dest = val
                        break
        if not extract:
            return
        if infile in (None, "-"):
            src = stdin if isinstance(stdin, _Src) else None
        else:
            src = st.fleet_files.get(self.ctx.fold(self._path(infile, st)))
        self._extract_into(src, self._path(dest, st) if dest else st.cwd)

    def _unzip(self, args: list[str], st: _State) -> None:
        infile: str | None = None
        dest: str | None = None
        i = 0
        while i < len(args):
            a = args[i]
            i += 1
            if a == "-d":
                dest = args[i] if i < len(args) else None
                i += 1
            elif a.startswith("-d") and len(a) > 2:
                dest = a[2:]
            elif a.startswith("-"):
                continue
            elif infile is None:
                infile = a
        if infile is None:
            return
        src = st.fleet_files.get(self.ctx.fold(self._path(infile, st)))
        self._extract_into(src, self._path(dest, st) if dest else st.cwd)

    # ---- the deny reason

    def display(self, path: str) -> str:
        s = path.replace(_UNK, "<unknown>")
        home = self.ctx.home
        fold = self.ctx.fold
        if fold(s) == fold(home):
            return "~"
        if fold(s).startswith(fold(home) + "/"):
            return "~/" + s[len(home) + 1:]
        return s

    def _seat_token(self) -> str | None:
        raw = (self.ctx.env.get("AGENT_SEAT") or "").strip()
        if not raw:
            return None
        seat = self.ctx.registry.seat_by_name(raw)
        token = seat.suffix if seat is not None else L.normalize_seat(raw, self.ctx.registry)
        return token if L.is_valid_slug(token) else None

    def _slug(self, target: str, app: L.App | None, prefix: str | None) -> str:
        base = target.rstrip("/").rsplit("/", 1)[-1].replace(_UNK, "")
        base = _ARCHIVE_EXT_RE.sub("", base)
        base = re.sub(r"X{3,}", "", base)
        slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
        names = [prefix] if prefix else []
        if app is not None:
            names.extend(app.prefix_aliases())
        for name in sorted({x for x in names if x}, key=len, reverse=True):
            if slug == name:
                slug = ""
                break
            if slug.startswith(name + "-"):
                slug = slug[len(name) + 1:]
                break
        slug = slug[:L.SLUG_MAX].strip("-")
        return slug if slug and slug not in ("tmp", "temp") and L.is_valid_slug(slug) else "work"

    def _lane(self, app: L.App | None, prefix: str | None, seat: str | None, slug: str) -> str:
        roots = self.ctx.roots
        if seat and prefix and L.is_valid_slug(prefix):
            try:
                return self.display(str(L.expected_lane_path(app or prefix, seat, slug, roots,
                                                             self.ctx.registry)))
            except L.LayoutError:
                pass
        p = prefix if prefix and L.is_valid_slug(prefix) else "<prefix>"
        s = seat or "<seat>"
        if roots.layout_mode == L.LAYOUT_FLAT:
            return self.display(f"{roots.apps_root}/{p}-{s}-{slug}")
        return self.display(f"{roots.lanes_root}/{p}/{s}-{slug}")

    def reason(self, hit: _Hit) -> str:
        src = hit.src
        app = src.app
        prefix = app.prefix if app is not None else src.prefix
        seat = self._seat_token()
        slug = self._slug(hit.target, app, prefix)
        lane = self._lane(app, prefix, seat, slug)
        branch = f"<seat>/{slug}"
        if seat:
            try:
                branch = L.branch_name(seat, slug, self.ctx.registry)
            except L.LayoutError:
                pass
        if app is not None and app.integration_dir_name:
            tree = self.display(f"{self.ctx.roots.code_root}/{app.integration_dir_name}")
            example = f"git -C {tree} worktree add -b {branch} {lane} origin/main"
        else:
            tree = "~/Code/<App>"
            example = f"gh repo clone {src.owner_repo} {lane}" if src.owner_repo else ""
        review = self.display(str(self.ctx.roots.review_root))
        where = f"Create it under the lanes root instead, at {lane}"
        if example:
            where += f" (for example `{example}`)"
        return "  ".join((
            f"Blocked: fleet-repo checkouts are not allowed in temp directories, and this command "
            f"would put {hit.what} of {src.label} at {self.display(hit.target)}.",
            where + ".",
            f"For a read-only PR check, use the review root {review}/ instead.",
            f"Do not do this work in the main integration tree {tree}; that tree is for the human.",
        ))
