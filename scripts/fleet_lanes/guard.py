"""Checkout placement guard: stops fleet-repo task checkouts in temp and integration trees.

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

Fleet worktree add/move destinations at or below ~/Code are also denied.  Clones into ~/Code
are denied except for an exact registered canonical integration destination, which supports
onboarding.  These checks use the host account home, so the same rule applies on Linux.

Denied temp shapes (each only for a fleet source and a temp destination):
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

Widened placement rules (AFC-618).  Beyond temp and ~/Code, a fleet clone or worktree must land in the lanes root
(a full clone only under a `_`-prefixed tool root), the exact integration tree, a sanctioned harness folder, the
scratchpad or a row of placement-exceptions.json.  Anything else falls in a rule class: `home`, `apps`,
`clone-in-lanes` or `elsewhere`.  These classes obey the mode file (placement.py): detect allows and records, block
denies, class by class.  The temp and ~/Code rules above keep denying whatever the mode file says.  An in-date exception
row passes a destination only as far as its class allows (placement.row_excuses):  here, on the hook, that means a service
or backup row, because a tool cache filling itself never comes through a hook and an agent's own command is not that.

Isolation (AFC-618).  A Claude `isolation: "worktree"` spawn, `EnterWorktree`, or a Muse `worktree_isolation` spawn
from inside the integration tree creates a worktree there, in no shell command at all.  `isolation` in the mode file
decides: off by default, then detect, then block.

This is the friendly message, not the wall.  It reads shell text, so a script file, a Python subprocess or a git
alias stays out of its sight; the git shim (git_shim.py) and the doctor sweep cover those.

Tests: fleet_lanes/tests/test_guard.py with the fixture table in fleet_lanes/tests/fixtures_guard.py, and
fleet_lanes/tests/test_guard_placement.py for the widened rules.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import layout as L
from . import placement as P

__all__ = [
    "ALLOW",
    "DENY",
    "DENY_RULES",
    "ENV_GUARD",
    "FLEET_OWNERS",
    "MAX_COMMAND_CHARS",
    "RULE_ARCHIVE",
    "RULE_CLONE",
    "RULE_CLONE_CODE",
    "RULE_EXPORT",
    "RULE_GH_CLONE",
    "RULE_GH_CLONE_CODE",
    "RULE_INIT_FETCH",
    "RULE_ISOLATION",
    "RULE_PLACEMENT_APPS",
    "RULE_PLACEMENT_CLONE_IN_LANES",
    "RULE_PLACEMENT_ELSEWHERE",
    "RULE_PLACEMENT_HOME",
    "RULE_TARBALL",
    "RULE_WORKTREE_ADD",
    "RULE_WORKTREE_ADD_CODE",
    "RULE_WORKTREE_MOVE",
    "RULE_WORKTREE_MOVE_CODE",
    "Decision",
    "GuardContext",
    "evaluate",
    "evaluate_git_argv",
    "evaluate_shell_text",
    "lane_name_reads_back",
    "make_context",
    "redact_url",
    "seat_owns_folder",
]

ALLOW = "allow"
DENY = "deny"

RULE_CLONE = "clone-into-temp"  # git clone (bare and mirror included)
RULE_GH_CLONE = "gh-clone-into-temp"  # gh repo clone
RULE_WORKTREE_ADD = "worktree-add-into-temp"  # git worktree add
RULE_WORKTREE_MOVE = "worktree-move-into-temp"  # git worktree move
RULE_CLONE_CODE = "clone-into-code"  # git clone into the integration tree
RULE_GH_CLONE_CODE = "gh-clone-into-code"  # gh repo clone into the integration tree
RULE_WORKTREE_ADD_CODE = "worktree-add-into-code"  # git worktree add into the integration tree
RULE_WORKTREE_MOVE_CODE = "worktree-move-into-code"  # git worktree move into the integration tree
RULE_ARCHIVE = "archive-into-temp"  # git archive written or extracted into temp
RULE_TARBALL = "tarball-into-temp"  # GitHub source tarball downloaded or extracted into temp
RULE_INIT_FETCH = "init-fetch-into-temp"  # git init + remote add + fetch/pull in temp
RULE_EXPORT = "checkout-index-into-temp"  # git checkout-index --prefix=TEMP/
RULE_PLACEMENT_HOME = "placement-home"  # a fleet checkout directly under or below the home folder
RULE_PLACEMENT_APPS = "placement-apps"  # a fleet checkout under ~/apps but outside the lanes root
RULE_PLACEMENT_CLONE_IN_LANES = "placement-clone-in-lanes"  # a full clone inside a lane folder
RULE_PLACEMENT_ELSEWHERE = "placement-elsewhere"  # a fleet checkout anywhere else on the disk
RULE_ISOLATION = "isolation-from-integration-tree"  # a harness worktree spawned from the integration tree
PLACEMENT_RULES = {
    P.CLASS_HOME: RULE_PLACEMENT_HOME,
    P.CLASS_APPS: RULE_PLACEMENT_APPS,
    P.CLASS_CLONE_IN_LANES: RULE_PLACEMENT_CLONE_IN_LANES,
    P.CLASS_ELSEWHERE: RULE_PLACEMENT_ELSEWHERE,
}
DENY_RULES = (
    RULE_CLONE,
    RULE_GH_CLONE,
    RULE_WORKTREE_ADD,
    RULE_WORKTREE_MOVE,
    RULE_CLONE_CODE,
    RULE_GH_CLONE_CODE,
    RULE_WORKTREE_ADD_CODE,
    RULE_WORKTREE_MOVE_CODE,
    RULE_ARCHIVE,
    RULE_TARBALL,
    RULE_INIT_FETCH,
    RULE_EXPORT,
    RULE_PLACEMENT_HOME,
    RULE_PLACEMENT_APPS,
    RULE_PLACEMENT_CLONE_IN_LANES,
    RULE_PLACEMENT_ELSEWHERE,
    RULE_ISOLATION,
)

# Allow outcomes, so a caller (and the tests) can tell why a command passed.
_PLACEABLE_RULES = frozenset({RULE_CLONE, RULE_GH_CLONE, RULE_WORKTREE_ADD, RULE_WORKTREE_MOVE})
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
_MAX_DEPTH = 6  # nesting of $(...), bash -c, eval and heredoc-to-shell evaluation

# Words that must appear before any deny rule can fire.  A command without one is allowed
# without being parsed.
_TRIGGER = re.compile(
    r"clone|worktree|archive|codeload|tarball|zipball|fetch|pull|remote|checkout-index",
    re.IGNORECASE,
)

_UNK = "\x00"  # text the guard cannot know (an unset variable, command output)
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
_ARCHIVE_EXT_RE = re.compile(
    r"(\.tar\.gz|\.tgz|\.tar|\.zip|\.tar\.bz2|\.tar\.xz|\.git)$", re.IGNORECASE
)

_TARBALL_RES = (
    re.compile(
        r"^(?:https?://)?(?:[^/@\s]*@)?codeload\.github\.com/([^/\s]+)/([^/\s]+)/"
        r"(?:legacy\.)?(?:tar\.gz|tar|zip)(?:[/?#]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:https?://)?(?:[^/@\s]*@)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/"
        r"(?:archive|tarball|zipball)(?:[/?#]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:https?://)?(?:[^/@\s]*@)?api\.github\.com/repos/([^/\s]+)/([^/\s]+)/"
        r"(?:tarball|zipball)(?:[/?#]|$)",
        re.IGNORECASE,
    ),
)
_GH_API_TARBALL_RE = re.compile(
    r"^/?repos/([^/\s]+)/([^/\s]+)/(?:tarball|zipball)(?:[/?#]|$)", re.IGNORECASE
)

_SHELL_TOOL_HINTS = ("bash", "shell", "terminal", "run_command", "exec_command", "execute_command", "start_process")
_ISOLATION_TOOLS = frozenset({"enterworktree"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_RESERVED = frozenset(
    {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "!",
        "{",
        "}",
        "time",
        "esac",
        "coproc",
    }
)
_HEADERS = frozenset({"for", "case", "select", "function"})
_PASSTHRU = frozenset(
    {
        "gunzip",
        "gzip",
        "zcat",
        "xz",
        "unxz",
        "xzcat",
        "bzip2",
        "bunzip2",
        "bzcat",
        "zstd",
        "unzstd",
        "zstdcat",
        "cat",
        "pv",
    }
)
_SPECIAL_ZERO = frozenset(
    {
        "$",
        "!",
        "?",
        "#",
        "-",
        "RANDOM",
        "BASHPID",
        "SECONDS",
        "EPOCHSECONDS",
        "EPOCHREALTIME",
        "PPID",
        "UID",
        "EUID",
        "LINENO",
    }
)

_GIT_GLOBAL_VALUE = frozenset(
    {"-c", "--namespace", "--super-prefix", "--config-env", "--attr-source", "--list-cmds", "--shallow-file"}
)
_CLONE_VALUE = frozenset(
    {
        "-b",
        "--branch",
        "-o",
        "--origin",
        "-u",
        "--upload-pack",
        "--reference",
        "--reference-if-able",
        "--separate-git-dir",
        "--depth",
        "--shallow-since",
        "--shallow-exclude",
        "-c",
        "--config",
        "--filter",
        "--server-option",
        "-j",
        "--jobs",
        "--template",
        "--bundle-uri",
        "--ref-format",
        "--revision",
    }
)
# Every long option of `git clone`, because git takes any unique prefix of one and a prefix of a value option swallows the next
# argument.  `_CLONE_VALUE` says which take a value.
_CLONE_LONG = frozenset(
    {
        "--verbose", "--quiet", "--progress", "--reject-shallow", "--no-checkout", "--checkout", "--bare", "--mirror",
        "--local", "--no-hardlinks", "--hardlinks", "--shared", "--recurse-submodules", "--recursive", "--jobs", "--template",
        "--reference", "--reference-if-able", "--dissociate", "--origin", "--branch", "--revision", "--upload-pack", "--depth",
        "--shallow-since", "--shallow-exclude", "--single-branch", "--no-tags", "--tags", "--shallow-submodules",
        "--remote-submodules", "--separate-git-dir", "--ref-format", "--config", "--server-option", "--ipv4", "--ipv6",
        "--filter", "--also-filter-submodules", "--sparse", "--bundle-uri",
    }
)
_CLONE_SHORT_VALUE = frozenset("bouj" "c")
_WT_ADD_VALUE = frozenset({"-b", "-B", "--reason"})
_WT_ADD_LONG = frozenset(
    {
        "--force", "--detach", "--checkout", "--no-checkout", "--lock", "--reason", "--orphan", "--track", "--no-track",
        "--guess-remote", "--no-guess-remote", "--relative-paths", "--no-relative-paths", "--quiet",
    }
)
_WT_ADD_SHORT_VALUE = frozenset("bB")
# The only `_`-prefixed folders in the lanes root the Lane Map places (layout._location_class).  Any other is unsanctioned.
_LANES_TOOL_ROOTS = frozenset({"_codex", "_conductor", "_managed", "_review"})
# These tools run where their own process runs, so the session's folder says nothing about a relative path.
_NO_SESSION_CWD_HINTS = ("start_process", "run_in_terminal")
_ARCHIVE_VALUE = frozenset(
    {"--format", "--prefix", "--exec", "--add-file", "--add-virtual-file", "--mtime"}
)
_TAR_SHORT_VALUE = frozenset("bCfFgHIKLNTVX")
_CURL_SHORT_VALUE = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
_CURL_LONG_VALUE = frozenset(
    {
        "--output",
        "--output-dir",
        "--header",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--data-ascii",
        "--user",
        "--request",
        "--user-agent",
        "--referer",
        "--cookie",
        "--cookie-jar",
        "--config",
        "--max-time",
        "--connect-timeout",
        "--retry",
        "--retry-delay",
        "--retry-max-time",
        "--proxy",
        "--proxy-user",
        "--cacert",
        "--capath",
        "--cert",
        "--key",
        "--form",
        "--form-string",
        "--upload-file",
        "--range",
        "--write-out",
        "--limit-rate",
        "--resolve",
        "--connect-to",
        "--interface",
        "--url",
        "--dump-header",
        "--oauth2-bearer",
        "--proto",
        "--proto-redir",
        "--continue-at",
        "--time-cond",
        "--max-filesize",
        "--netrc-file",
        "--trace",
        "--trace-ascii",
        "--stderr",
        "--variable",
        "--json",
        "--aws-sigv4",
        "--max-redirs",
        "--speed-limit",
        "--speed-time",
        "--unix-socket",
    }
)
_WGET_SHORT_VALUE = frozenset("aABDeiIlLoOPQRtTUwX")
_WGET_LONG_VALUE = frozenset(
    {
        "--output-document",
        "--directory-prefix",
        "--output-file",
        "--append-output",
        "--user-agent",
        "--header",
        "--tries",
        "--timeout",
        "--input-file",
        "--execute",
        "--user",
        "--password",
    }
)
_GH_API_VALUE = frozenset(
    {
        "-X",
        "--method",
        "-H",
        "--header",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--hostname",
        "--input",
        "--cache",
        "-p",
        "--preview",
    }
)


# --------------------------------------------------------------------------- public types


@dataclass(frozen=True)
class Decision:
    """The guard's answer.  `action` is "allow" or "deny"; `rule_id` names the rule that fired
    (one of DENY_RULES) or, for an allow, why it passed.  `destination` and `source` are display
    strings for a deny (credentials stripped); both are "" for an allow.  `detected`, on an allow, names the
    placement rule class that would have denied under block, and `detected_detail` holds what to log for it."""

    action: str
    reason: str = ""
    rule_id: str = ""
    destination: str = ""
    source: str = ""
    detected: str = ""
    detected_detail: Mapping[str, str] | None = None

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

    def __init__(
        self,
        *,
        home: str,
        env: Mapping[str, str],
        registry: L.Registry,
        roots: L.Roots,
        tmpdir: str,
        exceptions: tuple[P.ExceptionRow, ...] = (),
        mode: P.Mode | None = None,
        host: str = P.HOST_MAC,
        exceptions_problem: str | None = None,
    ) -> None:
        self.exceptions_problem = exceptions_problem
        self.home = home
        self.env = env
        self.registry = registry
        self.roots = roots
        self.tmpdir = tmpdir
        self.exceptions = exceptions
        self.mode = mode if mode is not None else P.Mode()
        self.host = host
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
            roots_f.append(rf)
        self.tmp_roots_f = tuple(roots_f)
        self.tmp_globs_f = tuple(tuple(fold(g).split("/")) for g in roots.tmp_globs)
        self.home_parts_f = tuple(home_f.split("/"))
        # A real home is never inside temp.  A test home made with tempfile is, and then only the
        # home's own subtree is exempt: its siblings (on Linux /tmp/<repo> beside /tmp/tmpAbC123)
        # stay temp, so a temp-dir home cannot switch a whole temp root off.
        self.home_f = home_f
        self.home_in_temp = self._raw_remainder(home_f) is not None
        # Lanes stored on the external disk (lanes/<Repo> is a symlink there) are lanes: a clone of one into
        # temp is as much a fleet clone as a clone of the same lane at its ~/apps/lanes spelling.
        self.fleet_roots_f = tuple(
            dict.fromkeys(
                fold(str(p))
                for p in (
                    roots.lanes_root,
                    roots.code_root,
                    roots.apps_root,
                    *roots.external_lanes_roots,
                )
            )
        )
        self.code_root_f = fold(str(roots.code_root))
        self.lanes_root_f = fold(str(roots.lanes_root))
        self.lanes_roots_f = tuple(
            dict.fromkeys(fold(str(p)) for p in (roots.lanes_root, *roots.external_lanes_roots))
        )
        self.harness_f = tuple(
            tuple(fold(loc.glob_or_prefix).split("/"))
            for loc in roots.harness_locations
            if loc.name != "documents"
        )
        # Harness folders a tool picks itself and the map accepts.  The repo-local ones sit in ~/Code, which has its own
        # rule, and an unsanctioned one (scratch, workspaces) is exactly what the widened rules are for.
        self.harness_ok_f = tuple(
            tuple(fold(loc.glob_or_prefix).split("/"))
            for loc in roots.harness_locations
            if loc.sanctioned and loc.name not in ("claude-repo", "muse-repo")
        )
        self.harness_repo_f = tuple(
            tuple(fold(loc.glob_or_prefix).split("/"))
            for loc in roots.harness_locations
            if loc.name in ("claude-repo", "muse-repo")
        )
        self.apps_root_f = fold(str(roots.apps_root))
        # The same roots as a person types them.  On a host whose ~/apps or ~/Code is a symlink, the guard's real roots
        # do not textually hold ~/apps/lanes/x, and a dangling lanes symlink must still read as the lanes root.
        spelled = {
            os.path.join(home, "apps", "lanes"): str(roots.lanes_root),
            os.path.join(home, "apps"): str(roots.apps_root),
            os.path.join(home, "Code"): str(roots.code_root),
        }
        for key, real in (
            (L.ENV_LANES_ROOT, str(roots.lanes_root)),
            (L.ENV_APPS_ROOT, str(roots.apps_root)),
            (L.ENV_CODE_ROOT, str(roots.code_root)),
        ):
            raw = (env.get(key) or "").strip()
            if raw.startswith("/"):
                spelled[_norm(raw)] = real
        self.spelled_f = tuple(
            sorted(
                ((fold(a), real) for a, real in spelled.items() if fold(a) != fold(real)),
                key=lambda t: -len(t[0]),
            )
        )
        names: list[tuple[str, L.App]] = []
        for app in registry.apps:
            for text in (app.prefix.lower(), *app.prefix_aliases()):
                if text:
                    names.append((text, app))
        names.sort(key=lambda t: -len(t[0]))
        self.app_names = tuple(names)

    def _raw_remainder(self, fk: str) -> str | None:
        for root in self.tmp_roots_f:
            if fk == root:
                return ""
            if fk.startswith(root + "/"):
                return fk[len(root) + 1 :]
        comps = fk.split("/")
        for pat in self.tmp_globs_f:
            k = len(pat)
            if len(comps) < k:
                continue
            if all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat)):
                return "/".join(comps[k:])
        return None

    def tmp_remainder(self, fk: str) -> str | None:
        """For a folded absolute path inside a temp root, the part after that root ("" for the
        root itself); None when the path is not in temp.  When the home itself sits in temp (a
        test home), paths inside the home are not temp."""
        rem = self._raw_remainder(fk)
        if (
            rem is not None
            and self.home_in_temp
            and (fk == self.home_f or fk.startswith(self.home_f + "/"))
        ):
            return None
        return rem

    def respelled(self, path: str) -> str:
        """`path` with a typed spelling of a root replaced by the real root."""
        fk = self.fold(path)
        for alias, real in self.spelled_f:
            if fk == alias:
                return real
            if fk.startswith(alias + "/"):
                return real + path[len(alias):]
        return path

    def in_date_exception(self, path: str) -> P.ExceptionRow | None:
        """The exception row that excuses `path`, if one is in date for this host."""
        if not self.exceptions:
            return None
        spellings = (path, self.respelled(path), L.to_logical(path, self.roots))
        hit = P.find_exception(spellings, self.roots, self.exceptions, host=self.host)
        return hit.row if hit is not None and not hit.expired else None


# --------------------------------------------------------------------------- lane naming, shared with `lane`
#
# `lane` (lane.py) calls these two as well, so the deny reason can never name a lane that `lane new`
# refuses.  They would sit best in layout.py; they live here because the hook import path must stay
# layout, guard, hook (no doctor, no lane), and lane can import guard cheaply.


def seat_owns_folder(seat: L.Seat, registry: L.Registry) -> bool:
    """True when the folder name `seat.suffix` belongs to `seat`.

    Two registry rows can share a worktreeSuffix: CURSOR and GROK-BOT both use `cursor` (GROK-BOT had
    CURSOR's cursor/ branch prefix too, until F2 removed it).  The name belongs to the seat
    `Registry.seat_by_suffix` picks (the first live row), and no other seat gets a lane under it,
    because that folder and branch would be the owner's.  `lane` refuses such a seat, and the deny
    reason shows a placeholder.
    """
    owner = registry.seat_by_suffix(seat.suffix)
    return owner is not None and owner.name.lower() == seat.name.lower()


def lane_name_reads_back(
    prefix: str, seat: str, slug: str, registry: L.Registry, *, flat: bool = False
) -> bool:
    """True when the folder `lane new` makes for (prefix, seat, slug) reads back as that very app
    prefix, seat and slug.

    It does not when the slug's first word joins the seat into another seat's name: seat grok with
    slug build makes grok-build, which is GROK-BUILD's, and grok-bot reads as the cursor seat.  The
    folder is <seat>-<slug> under lanes/<Repo>/ (nested) or <prefix>-<seat>-<slug> under ~/apps
    (flat).  It is judged the way `layout.explain_lane_name` judges a lane path, but on the bare
    name, so nothing touches the filesystem (`evaluate` must stay pure).  `seat` is the canonical
    folder token (worktreeSuffix) and `prefix` a registry app prefix; an unknown prefix fails.  In
    layout v2 the folder above the name is the app's repo folder (`App.lane_dir`), found from `prefix`.
    """
    try:
        name = L.lane_dir_name(prefix, seat, slug) if flat else L.nested_dir_name(seat, slug)
    except L.LayoutError:
        return False
    parent = None
    if not flat:
        owner = registry.app_by_prefix(prefix)
        if owner is None or owner.prefix != prefix:
            return False
        parent = owner.lane_dir
    verdict, _reasons, app, hit, got = L._eval_name(name, registry, nested_prefix_dir=parent)
    return (
        verdict == L.NameVerdict.CONFORMING
        and app is not None
        and app.prefix == prefix
        and hit is not None
        and hit.seat == seat
        and got == slug
    )


def make_context(
    env: Mapping[str, str] | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
    registry: L.Registry | None = None,
    case_insensitive: bool | None = None,
) -> GuardContext:
    """Resolve the roots and load the registry once.  This is the only filesystem work.

    `env` defaults to the process environment and is read for HOME, TMPDIR, FLEET_LAYOUT,
    FLEET_LANES_ROOT, FLEET_LANES_EXTERNAL_ROOT, FLEET_APPS_JSON, AGENT_SEAT and FLEET_LANE_GUARD.  A registry that cannot
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
    rows, exceptions_problem = P.load_exceptions_reporting(env=env)
    return GuardContext(
        home=str(roots.home),
        env=env,
        registry=registry,
        roots=roots,
        tmpdir=tmpdir,
        exceptions=rows,
        mode=P.read_mode(str(roots.home), env),
        host=P.host_kind(env),
        exceptions_problem=exceptions_problem,
    )


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
        if (
            len(cmd) >= 3
            and os.path.basename(cmd[0]) in _SHELLS
            and cmd[1].startswith("-")
            and "c" in cmd[1]
        ):
            cmd = cmd[2]
        else:
            cmd = " ".join(shlex.quote(c) for c in cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    # The per-call directory (where this command runs) beats the session's top-level cwd.  Desktop Commander's start_process
    # takes no directory and a terminal may not be given one, so for those a relative path is unknown, not session-relative.
    per_call_only = isinstance(tool, str) and any(h in tool.lower() for h in _NO_SESSION_CWD_HINTS)
    cwd = ""
    for value in (
        ti.get("workdir"),
        ti.get("cwd"),
        ti.get("Cwd"),
        ti.get("working_directory"),
        None if per_call_only else payload.get("cwd"),
    ):
        if isinstance(value, str) and value.strip():
            cwd = value.strip()
            break
    if not cwd and not per_call_only:
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and roots and isinstance(roots[0], str):
            cwd = roots[0]
    scratch = payload.get("scratchpad_dir", payload.get("scratchpadDir"))
    return cmd, cwd, scratch if isinstance(scratch, str) and scratch.startswith("/") else None


def _evaluate(payload: Any, ctx: GuardContext | None) -> Decision:
    env = ctx.env if ctx is not None else os.environ
    if (env.get(ENV_GUARD) or "").strip().lower() in _OFF_VALUES:
        return Decision(ALLOW, "", _OK_DISABLED)
    isolation = _evaluate_isolation(payload, ctx) if _isolation_request(payload) else None
    if isolation is not None and (isolation.denied or isolation.detected):
        return isolation
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
    if ev.hits:
        hit = ev.hits[0]
        return Decision(DENY, ev.reason(hit), hit.rule, ev.display(hit.target), hit.src.label)
    if ev.placement:
        # A compound command can name several destinations, and any class the mode blocks denies the whole command.
        for cls, hit in ev.placement:
            if ctx.mode.blocks(cls):
                return Decision(DENY, ev.reason(hit), hit.rule, ev.display(hit.target), hit.src.label)
        cls, hit = ev.placement[0]
        if ctx.mode.mode != P.MODE_OFF:
            return Decision(ALLOW, "", _OK_NO_MATCH, detected=cls, detected_detail=_detail(ev, hit, cls))
    if ev.excused and ctx.mode.mode != P.MODE_OFF:
        row, target, src = ev.excused[0]
        return Decision(ALLOW, "", _OK_NO_MATCH, detected_detail={
            "action": CLASS_EXCUSED, "class": CLASS_EXCUSED, "rule": row.id, "dest": ev.display(target), "source": src.label,
        })
    return Decision(ALLOW, "", _OK_NO_MATCH)


def _detail(ev: _Evaluator, hit: _Hit, cls: str) -> dict[str, str]:
    return {"class": cls, "rule": hit.rule, "dest": ev.display(hit.target), "source": hit.src.label}


def _rule_class(rule: str) -> str:
    """The mode file's word for a rule."""
    for cls, name in PLACEMENT_RULES.items():
        if name == rule:
            return cls
    if rule in (RULE_CLONE_CODE, RULE_GH_CLONE_CODE, RULE_WORKTREE_ADD_CODE, RULE_WORKTREE_MOVE_CODE):
        return P.CLASS_CODE
    return P.CLASS_TEMP


CLASS_EXCUSED = "excused"  # what the shim gets back for a destination an exception row covers;  never a block


_MAX_ORIGIN_HOPS = 3


def _venv_src_spelling(target: str) -> str | None:
    """`pip install -e git+...` clones into <venv>/src/<name>, whatever the venv is called, and a pattern cannot say "any folder
    holding a pyvenv.cfg".  So a real venv is spelled `.venv` here for the row to match.  Only the shim calls this:  it reads the disk."""
    parent = os.path.dirname(target)
    venv = os.path.dirname(parent)
    if os.path.basename(parent) != "src" or not os.path.isfile(os.path.join(venv, "pyvenv.cfg")):
        return None
    return os.path.join(os.path.dirname(venv), ".venv", "src", os.path.basename(target))


def evaluate_git_argv(
    args: list[str],
    cwd: str,
    ctx: GuardContext,
    *,
    origin_of: Callable[[str], Any] | None = None,
    others: list[tuple[str, Decision]] | None = None,
) -> tuple[str, Decision] | None:
    """The shim's question: judge one real `git` argv, already past alias expansion.

    Returns (rule class, a DENY Decision whose reason is ready to print) when this checkout would be misplaced,
    (CLASS_EXCUSED, an ALLOW Decision naming the row) when an exception row covers it, and None when it is fine.  The caller
    applies the mode, because for the shim even temp and ~/Code obey it.  `origin_of` reads a repo's origin URL:  the shim
    may touch the disk, where the hook may not, so here a repo is fleet by its origin and not only by where it sits.  `origin_of`
    answers with one URL or a list of them.  The answer is the one to act on, and any other verdict is added to `others`.
    """
    ev = _Evaluator(ctx, None, via="shim", origin_of=origin_of)
    ev._git(list(args), [], _State(_norm(cwd) if cwd.startswith("/") else _UNK))
    return _shim_verdict(ev, others)


def evaluate_shell_text(
    command: str,
    cwd: str,
    ctx: GuardContext,
    *,
    args: Sequence[str] = (),
    origin_of: Callable[[str], Any] | None = None,
    others: list[tuple[str, Decision]] | None = None,
) -> tuple[str, Decision] | None:
    """The same question for a shell command, the body of a `!` git alias.  `args` are the alias's arguments, which git hands the
    shell as $1 and so on."""
    ev = _Evaluator(ctx, None, via="shim", origin_of=origin_of)
    st = _State(_norm(cwd) if cwd.startswith("/") else _UNK)
    for i, arg in enumerate(args, 1):
        st.vars[str(i)] = arg
    ev.run(command, st)
    return _shim_verdict(ev, others)


def _shim_verdict(ev: _Evaluator, others: list[tuple[str, Decision]] | None = None) -> tuple[str, Decision] | None:
    """A partial block has to see every checkout in a `!` alias, so a class it blocks counts wherever it comes in the alias, and one
    that only detects never hides it.  A harness worktree the `isolation` line leaves alone comes last, because it logs nothing."""
    rows = [(P.CLASS_ISOLATION if ev.is_harness_isolation(hit) else _rule_class(hit.rule), hit) for hit in ev.hits]
    rows += ev.placement
    if not rows:
        if not ev.excused:
            return None
        row, target, src = ev.excused[0]
        return CLASS_EXCUSED, Decision(ALLOW, "", row.id, ev.display(target), src.label)
    mode = ev.ctx.mode
    rows.sort(key=lambda r: (not mode.blocks(r[0]), r[0] == P.CLASS_ISOLATION and mode.isolation == P.MODE_OFF))
    verdicts = [(cls, Decision(DENY, ev.reason(hit), hit.rule, ev.display(hit.target), hit.src.label)) for cls, hit in rows]
    if others is not None:
        others.extend(verdicts[1:])
    return verdicts[0]


def _isolation_request(payload: Any) -> bool:
    """Whether this tool call asks a harness to make its own worktree."""
    if not isinstance(payload, Mapping):
        return False
    tool = payload.get("tool_name", payload.get("toolName"))
    ti = payload.get("tool_input", payload.get("toolInput"))
    if isinstance(tool, str) and tool.strip().lower() in _ISOLATION_TOOLS:
        # EnterWorktree with a path moves into a worktree that exists and creates nothing, which is how a session started in
        # ~/Code reaches the lane this rule tells it to make.
        return not (isinstance(ti, Mapping) and ti.get("path"))
    if not isinstance(ti, Mapping):
        return False
    return ti.get("isolation") == "worktree" or ti.get("worktree_isolation") not in (None, False, "", "none", "off")


def _evaluate_isolation(payload: Mapping, ctx: GuardContext | None) -> Decision:
    if ctx is None:
        ctx = make_context()
    mode = ctx.mode.isolation
    cwd = payload.get("cwd")
    if mode == P.MODE_OFF or not isinstance(cwd, str) or not cwd.startswith("/"):
        return Decision(ALLOW, "", _OK_NOT_SHELL)
    fk = ctx.fold(ctx.respelled(_norm(cwd)))
    if not fk.startswith(ctx.code_root_f + "/"):
        return Decision(ALLOW, "", _OK_NOT_SHELL)
    repo = ctx.respelled(_norm(cwd))[len(ctx.code_root_f) + 1 :].split("/", 1)[0]
    # A repo the registry does not hold is someone's own, and `lane new` would refuse it.
    if not any(
        app.integration_dir_name and ctx.fold(app.integration_dir_name) == ctx.fold(repo) for app in ctx.registry.apps
    ):
        return Decision(ALLOW, "", _OK_NOT_SHELL)
    if mode != P.MODE_BLOCK:
        return Decision(ALLOW, "", _OK_NOT_SHELL, detected="isolation",
                        detected_detail={"class": "isolation", "rule": RULE_ISOLATION, "dest": cwd, "source": repo})
    shim = os.path.join(str(ctx.roots.apps_root), "lane")
    reason = (
        f"Blocked: this would create a harness worktree inside the integration tree {cwd}, which is for the human.  "
        f"Run `{shim} new {repo} <slug>`, then work in the lane it prints, where this kind of worktree is allowed."
    )
    return Decision(DENY, reason, RULE_ISOLATION, cwd, repo)


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
    fixed = spec[: spec.rfind("/", 0, m.start()) + 1] if m else spec
    if base.startswith("/") and _UNK not in base:
        full = _join(base, fixed or ".")
        return full == base or base.startswith(full.rstrip("/") + "/")
    rel = os.path.normpath(fixed or ".")
    return rel in (".", "..") or rel.startswith("../")


def _humanish(repo: str, bare: bool) -> str:
    """The directory git (or gh) picks when the destination is omitted."""
    t = repo.rstrip("/")
    t = t.removesuffix("/.git")
    name = re.split(r"[/:]", t)[-1] if t else ""
    name = name.removesuffix(".git")
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
    return name.removesuffix(".git")


# --------------------------------------------------------------------------- lexer


class _Word:
    """One shell word as segments: ("lit", text), ("var", name, op, arg), ("cmd", inner), ("unk",).
    `tilde` is set when the word starts with an unquoted ~."""

    __slots__ = ("buf", "segs", "tilde")

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

    def done(self) -> _Word:
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
            out.append(s[i + 1 : j])
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
        return ("unk",)  # ${#x} length, ${!x} indirection
    m = _PARAM_RE.match(inner)
    if m is None:
        return ("unk",)
    name, rest = m.group(1), inner[m.end() :]
    if not rest:
        return ("var", name, None, None)
    for op in (":-", ":=", "-", "="):
        if rest.startswith(op):
            return ("var", name, "default", rest[len(op) :])
    for op in (":?", "?"):
        if rest.startswith(op):
            return ("var", name, None, None)
    for op in ("%%", "%", "##", "#"):
        if rest.startswith(op):
            return ("var", name, op, rest[len(op) :])
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
        inner = s[j + 1 : k - 1] if k - 1 > j and s[k - 1] == "}" else s[j + 1 : k]
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
            w.lit(s[i + 1 :])
            return n
        w.lit(s[i + 1 : j])
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
        inner, j = _scan_balanced(s, i + 2)  # process substitution <(...) or >(...)
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
                    inner, i = _scan_balanced(s, i + 1)  # array literal a=( ... )
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
                    i = j + 1  # the () of a function definition:  the name ends here, and the body is read as commands
                    if word is not None:
                        toks.append(("w", word))
                        word = None
                    toks.append(("op", ";"))
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

    __slots__ = ("cwd", "dirstack", "fleet_files", "oldpwd", "remotes", "vars")

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.oldpwd = _UNK
        self.dirstack: list[str] = []
        self.vars: dict[str, str] = {}
        self.fleet_files: dict[str, _Src] = {}
        self.remotes: dict[str, _Src] = {}

    def scoped_copy(self) -> _State:
        c = _State.__new__(_State)
        c.cwd, c.oldpwd = self.cwd, self.oldpwd
        c.dirstack, c.vars = list(self.dirstack), dict(self.vars)
        c.fleet_files, c.remotes = self.fleet_files, self.remotes
        return c

    def restore(self, saved: _State) -> None:
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
    loose: bool = False  # the app came only from a folder named <prefix>-<something>, which anyone's repo can be


@dataclass(frozen=True)
class _Hit:
    rule: str
    target: str
    src: _Src
    what: str


def _derive_prefix(name: str) -> str | None:
    p = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return p or None


def _parse_options(
    args: Sequence[str], longs: frozenset[str], value_opts: frozenset[str], short_value: frozenset[str]
) -> tuple[list[str], dict[str, str]]:
    """(positional arguments, long options given) the way git reads them:  a unique prefix of a long option counts, and short
    options cluster, so `-qb main` and `--branc main` both take `main` as a value.  An ambiguous prefix is git's own error, and
    here it takes no value."""
    pos: list[str] = []
    seen: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            pos.extend(args[i:])
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in longs:
                candidates = [o for o in longs if o.startswith(name)]
                name = candidates[0] if len(candidates) == 1 else name
            if name in value_opts and not eq:
                val = args[i] if i < len(args) else ""
                i += 1
            seen[name] = val
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch in short_value:
                    if k == len(a) - 1:
                        i += 1
                    break
        else:
            pos.append(a)
    return pos, seen


def _landing(path: str) -> str | None:
    """Where a destination really lands when a symlink in it points away, or None when nothing moves it.  A dangling link is the
    unmounted disk, which must never read as misplaced, so it answers None too."""
    probe = path
    tail: list[str] = []
    while probe not in ("", "/") and not os.path.exists(probe):
        if os.path.islink(probe):
            return None
        probe, name = os.path.split(probe)
        tail.insert(0, name)
    real = os.path.realpath(probe)
    return os.path.join(real, *tail) if real != probe else None


class _Evaluator:
    def __init__(
        self,
        ctx: GuardContext,
        scratch: str | None,
        *,
        via: str = "hook",
        origin_of: Callable[[str], str | None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.depth = 0
        self.hits: list[_Hit] = []
        self.placement: list[tuple[str, _Hit]] = []  # widened rule class and hit; the mode decides what they do
        self.excused: list[tuple[P.ExceptionRow, str, _Src]] = []  # rows that covered a destination, for the shim's log
        self.via = via
        self.origin_of = origin_of
        self.mirror_or_local = False  # the shape of the clone being judged, which a build row asks about
        fold = ctx.fold
        dirs: list[str] = []
        if scratch:
            sd = _norm(scratch)
            dirs.append(sd)
            for a, b in (
                ("/private/tmp/", "/tmp/"),
                ("/tmp/", "/private/tmp/"),
                ("/private/var/", "/var/"),
                ("/var/", "/private/var/"),
            ):
                if sd.startswith(a):
                    dirs.append(b + sd[len(a) :])
        self.scratch_f = tuple(fold(d) for d in dirs)

    # ---- driving

    def _stop(self) -> bool:
        """The hook denies at the first hit.  The shim goes on, because the mode file can block a later class and not the first."""
        return bool(self.hits) and self.via != "shim"

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
                last_out = self._safe_command(
                    words, redirs, st, last_out if after_pipe else group_in
                )
                words, redirs = [], []
                after_pipe = False
                if self._stop():
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
                after_pipe = False  # last_out stays: it is the group's output
            else:
                after_pipe = False
                last_out = None
        if words or redirs:
            self._safe_command(words, redirs, st, last_out if after_pipe else group_in)

    def _safe_command(self, words: list[_Word], redirs: list, st: _State, stdin: Any) -> Any:
        try:
            return self._command(words, redirs, st, stdin)
        except Exception:
            return None  # one odd segment never stops checks on the others

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
                return value.removesuffix(pat)
            return value.removeprefix(pat)
        return value

    def _tilde(self, s: str, st: _State) -> str:
        m = re.match(r"~([^/]*)", s)
        if m is None:
            return s
        user, rest = m.group(1), s[m.end() :]
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
                        tail = a[k + 1 :]
                        dir_ = tail or (args[i] if i < len(args) else "")
                        if not tail:
                            i += 1
                        break
                continue
            template = a
        base_tmp = self._var("TMPDIR", st) or "/tmp"
        if dir_:
            return _child(
                self._path(dir_, st), os.path.basename(template) if template else "tmp.XXXXXXXXXX"
            )
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
        value = expanded[m.end() :]
        if first[m.end() :].startswith("~"):
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

    def _in_code(self, path: str) -> bool:
        """Whether a resolved destination is at or below ~/Code, including a known prefix
        followed by an unresolved shell variable."""
        if not path or not path.startswith("/"):
            return False
        k = path.find(_UNK)
        if k < 0:
            known = path
        else:
            prefix = path[:k]
            known = prefix if prefix.endswith("/") else os.path.dirname(prefix)
        known = _norm(known.rstrip("/")) if known else "/"
        folded = self.ctx.fold(known)
        return folded == self.ctx.code_root_f or folded.startswith(self.ctx.code_root_f + "/")

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

    def _only_prefix_named(self, name: str) -> bool:
        """Whether an app matched this folder only because its name starts with the app's.  `<prefix>-<seat>` is a flat lane, so it
        stays the app's;  `<prefix>-tools` may be anyone's."""
        reg = self.ctx.registry
        if reg.app_by_name(name) is not None:
            return False
        low = name.lower()
        for text, _cand in self.ctx.app_names:
            if low.startswith(text + "-"):
                head = low[len(text) + 1 :].split("-", 1)[0]
                return reg.seat_by_suffix(head) is None and reg.seat_by_name(head) is None
        return False

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
                rel = known[len(root) + 1 :]
                first = rel.split("/", 1)[0] if rel else ""
                if not first and k < 0:
                    continue
                app = None
                if first and (k < 0 or len(rel) > len(first)):
                    if root in self.ctx.lanes_roots_f:
                        reg = self.ctx.registry
                        app = (
                            reg.app_by_lane_dir(first, case_insensitive=self.ctx.case_insensitive)
                            or reg.app_by_prefix(first)
                            or self._app_from_name(first)
                        )
                    else:
                        app = self._app_from_name(first)
                return _Src(
                    self.display(path),
                    app,
                    app.prefix if app else None,
                    app.owner_repo if app else "",
                    loose=app is not None and root not in self.ctx.lanes_roots_f and self._only_prefix_named(first),
                )
        comps = known.split("/")
        for pat in self.ctx.harness_f:
            n = len(pat)
            if len(comps) > n and all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat)):
                app = None
                for comp in comps[n:]:  # below the harness root only (.botfleet is not BotFleet)
                    app = self._app_from_name(comp)
                    if app is not None:
                        break
                return _Src(
                    self.display(path),
                    app,
                    app.prefix if app else None,
                    app.owner_repo if app else "",
                )
        return None

    def _repo_src(self, owner: str, repo: str, kind: str = "repo") -> _Src | None:
        if _UNK in owner:
            return None
        owner_repo = f"{owner}/{repo}"
        if (
            owner.casefold() not in self.ctx.fleet_owners
            and owner_repo.casefold() not in self.ctx.owner_repos
        ):
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
            return self._local_src(_join(base, repo[7:]))
        if "://" in repo or re.match(r"^[^/:]+:", repo):
            return None
        return self._local_src(_join(base, repo))

    def _local_src(self, path: str, hops: int = 0) -> _Src | None:
        """The fleet source a local repo stands for.  The hook goes by where it sits, since it must not touch the disk.  The shim
        asks the repo's origin, so a fleet checkout that got outside the roots is still fleet, and a repo under a root whose origin is
        someone else's, or that has no remote, is not.  An origin that is a local path is followed, since a clone of a clone is still
        fleet, and a repo with no `origin` is asked about the remotes it does have.  A folder name that looks like an app decides
        nothing here."""
        src = self._fleet_path_src(path)
        if self.origin_of is None:
            return src
        answer = self.origin_of(path) if path.startswith("/") and _UNK not in path else None
        for url in [answer] if isinstance(answer, str) else (answer or ()):
            gh = _github_owner_repo(url)
            if gh is not None:
                by_origin = self._repo_src(*gh)
            else:
                by_origin = self._local_origin_src(url, path, hops)
            if by_origin is not None:
                return src if src is not None and src.app is not None else by_origin
        return None

    def _local_origin_src(self, url: str, path: str, hops: int) -> _Src | None:
        """The fleet source behind an origin that is a local path, a few hops at most.  A folder that is gone counts by where it was."""
        if hops >= _MAX_ORIGIN_HOPS:
            return None
        local = url[7:] if url.lower().startswith("file://") else url
        if "://" in local or re.match(r"^[^/:]+:", local):
            return None  # another host, which _github_owner_repo has already said is not GitHub
        if local.startswith("~/"):
            local = _join(self.ctx.home, local[2:])
        local = _join(path, local)
        if os.path.isdir(local):
            return self._local_src(local, hops + 1)
        found = self._fleet_path_src(local)
        return found if found is not None and found.app is not None else None

    def _tarball_src(self, url: str) -> _Src | None:
        for rx in _TARBALL_RES:
            m = rx.match(url)
            if m:
                return self._repo_src(m.group(1), _strip_git(m.group(2)), "tarball")
        return None

    def _excused(self, rule: str, target: str, src: _Src) -> bool:
        """Whether an in-date row covers this command.  See placement.row_excuses for what each class allows."""
        if not target.startswith("/") or _UNK in target:
            return False
        row = self.ctx.in_date_exception(target)
        if row is None and self.origin_of is not None:
            spelled = _venv_src_spelling(target)
            row = self.ctx.in_date_exception(spelled) if spelled else None
        if row is None:
            return False
        if not P.row_excuses(
            row,
            via=self.via,
            clone=rule in (RULE_CLONE, RULE_GH_CLONE),
            in_temp=self._where(target) == "temp",
            in_code=self._in_code(target),
            mirror_or_local=self.mirror_or_local,
        ):
            return False
        self.excused.append((row, target, src))
        return True

    def is_harness_isolation(self, hit: _Hit) -> bool:
        """A worktree add into a repo-local harness folder, which is what `isolation: "worktree"` runs.  The `isolation` line of the
        mode file rules on that, so turning on the `code` class must not rule on it too."""
        if hit.rule != RULE_WORKTREE_ADD_CODE:
            return False
        comps = self.ctx.fold(self.ctx.respelled(hit.target)).split("/")
        return any(
            len(comps) > len(pat) and all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat))
            for pat in self.ctx.harness_repo_f
        )

    def _check(self, rule: str, target: str, src: _Src, what: str) -> None:
        if self._excused(rule, target, src):
            return
        where = self._where(target)
        if where == "temp":
            self.hits.append(_Hit(rule, target, src, what))
            return
        if not self._in_code(target):
            self._check_placement(rule, target, src, what, where)
            return
        code_rule = {
            RULE_CLONE: RULE_CLONE_CODE,
            RULE_GH_CLONE: RULE_GH_CLONE_CODE,
            RULE_WORKTREE_ADD: RULE_WORKTREE_ADD_CODE,
            RULE_WORKTREE_MOVE: RULE_WORKTREE_MOVE_CODE,
        }.get(rule)
        if code_rule is None:
            return
        # A clone of a registered app into its exact canonical integration directory is the
        # onboarding path.  Worktrees, nested destinations, and cross-app clones stay denied.
        app = src.app
        canonical = (
            code_rule in (RULE_CLONE_CODE, RULE_GH_CLONE_CODE)
            and app is not None
            and app.registered
            and app.integration_dir_name
            # the source repository must be this app's own repo, not a name that merely starts with it
            and bool(app.owner_repo)
            and src.owner_repo.casefold() == app.owner_repo.casefold()
            and self.ctx.fold(_norm(target))
            == self.ctx.fold(_join(str(self.ctx.roots.code_root), app.integration_dir_name))
        )
        if not canonical:
            self.hits.append(_Hit(code_rule, target, src, what))

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
        rest = args[i + 1 :]
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
                    st.vars[m.group(1)] = a[m.end() :]
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
                i += 1  # the duration
            elif name == "sudo":
                i += 1
                while i < n and args[i].startswith("-"):
                    i += (
                        2
                        if args[i] in ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T")
                        else 1
                    )
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
            return  # a script file: not read
        for r in redirs:
            if r[1] in ("<<", "<<-") and r[3]:
                self._recurse(r[3], st.scoped_copy())
            elif r[1] == "<<<" and r[2] is not None:
                self._recurse(self._expand(r[2], st), st.scoped_copy())

    # ---- git

    def _check_placement(self, rule: str, target: str, src: _Src, what: str, where: str) -> None:
        """The widened rule: a destination outside temp and ~/Code must be in the map.  It only ever records."""
        if where != "other" or rule not in _PLACEABLE_RULES:
            return
        # Being under a root is not being a fleet repo.  A folder in ~/apps or ~/Code the registry cannot name, with no fleet
        # owner either, may be anyone's, and these rules are new:  only the older temp and ~/Code rules go by place alone.
        if src.app is None and not src.owner_repo:
            return
        # The hook cannot read an origin, so a folder name alone does not make a fleet repo under these rules.  The shim asked the origin.
        if src.loose and self.origin_of is None:
            return
        cls = self._placement_class(rule, self.ctx.respelled(target), src)
        if cls is None and self.origin_of is not None:
            # The shim can see the disk, so a symlink in the lanes root is judged where it lands.
            landed = _landing(target)
            if landed is not None:
                cls = self._placement_class(rule, landed, src)
                target = landed if cls is not None else target
        if cls is not None:
            self.placement.append((cls, _Hit(PLACEMENT_RULES[cls], target, src, what)))

    def _placement_class(self, rule: str, target: str, src: _Src) -> str | None:
        ctx = self.ctx
        fk = ctx.fold(target)
        for root in ctx.lanes_roots_f:
            if fk == root or fk.startswith(root + "/"):
                first = fk[len(root) + 1 :].split("/", 1)[0]
                clone = rule in (RULE_CLONE, RULE_GH_CLONE)
                if first.startswith("_") and first not in _LANES_TOOL_ROOTS:
                    return P.CLASS_CLONE_IN_LANES if clone else P.CLASS_ELSEWHERE
                return P.CLASS_CLONE_IN_LANES if clone and not first.startswith("_") else None
        if fk == ctx.code_root_f or fk.startswith(ctx.code_root_f + "/"):
            # Reached only through a typed spelling of the root (a symlinked ~/Code).  Same answer as the code rule.
            app = src.app
            clone = rule in (RULE_CLONE, RULE_GH_CLONE)
            exact = (
                clone
                and app is not None
                and app.registered
                and bool(app.integration_dir_name)
                and fk == ctx.fold(_join(str(ctx.roots.code_root), app.integration_dir_name))
            )
            return None if exact else P.CLASS_ELSEWHERE
        comps = fk.split("/")
        for pat in ctx.harness_ok_f:
            k = len(pat)
            if len(comps) >= k and all(fnmatch.fnmatchcase(c, p) for c, p in zip(comps, pat)):
                return None
        if fk == ctx.apps_root_f or fk.startswith(ctx.apps_root_f + "/"):
            return P.CLASS_APPS
        if fk == ctx.home_f or fk.startswith(ctx.home_f + "/"):
            return P.CLASS_HOME
        return P.CLASS_ELSEWHERE

    def _git(self, args: list[str], redirs: list, st: _State) -> Any:
        eff = st.cwd
        gitdir: str | None = None
        worktree: str | None = None
        i = 0
        n = len(args)
        while i < n:
            a = args[i]
            if a == "-C":
                if i + 1 < n:
                    eff = _join(eff, args[i + 1])
                i += 2
            elif a in ("--git-dir", "--work-tree"):
                if i + 1 < n:
                    if a == "--git-dir":
                        gitdir = args[i + 1]
                    else:
                        worktree = args[i + 1]
                i += 2
            elif a.startswith("--git-dir="):
                gitdir = a.split("=", 1)[1]
                i += 1
            elif a.startswith("--work-tree="):
                worktree = a.split("=", 1)[1]
                i += 1
            elif a in _GIT_GLOBAL_VALUE:
                i += 2
            elif a.startswith("-"):
                i += 1
            else:
                break
        if i >= n:
            return None
        sub, rest = args[i], args[i + 1 :]
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
            # What this writes is the repo it names, not the folder it runs in:  a fetch touches the git dir, and a pull its work tree too.
            written = _join(eff, worktree) if sub == "pull" and worktree else repo_dir
            self._fetch(rest, written, st)
        elif sub == "checkout-index":
            self._checkout_index(rest, eff, repo_dir)
        return None

    def _clone(self, rest: list[str], eff: str) -> None:
        pos, longs = _parse_options(rest, _CLONE_LONG, _CLONE_VALUE, _CLONE_SHORT_VALUE)
        bare = "--bare" in longs or "--mirror" in longs
        sep_dir = longs.get("--separate-git-dir")
        if not pos:
            return
        self.mirror_or_local = bare or "://" not in pos[0] and not re.match(r"^[^/:]+:", pos[0])
        src = self._source(pos[0], eff)
        if src is None:
            return
        target = _join(eff, pos[1]) if len(pos) > 1 else _child(eff, _humanish(pos[0], bare))
        self._check(RULE_CLONE, target, src, "a bare clone" if bare else "a clone")
        if sep_dir and not self._stop():
            self._check(RULE_CLONE, _join(eff, sep_dir), src, "the git directory of a clone")

    def _worktree(self, rest: list[str], eff: str, repo_dir: str) -> None:
        if not rest:
            return
        op, args = rest[0], rest[1:]
        if op == "add":
            pos, _longs = _parse_options(args, _WT_ADD_LONG, _WT_ADD_VALUE, _WT_ADD_SHORT_VALUE)
            if pos:
                src = self._local_src(repo_dir)
                if src is not None:
                    self._check(RULE_WORKTREE_ADD, _join(eff, pos[0]), src, "a worktree")
        elif op == "move":
            pos = [a for a in args if not (a.startswith("-") and len(a) > 1)]
            if len(pos) >= 2:
                src = self._local_src(repo_dir)
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
        src = self._source(remote, eff) if remote else self._local_src(repo_dir)
        if src is None:
            return None
        src = dataclasses.replace(src, kind="archive")
        target = _join(eff, out) if out else self._stdout_target(redirs, st)
        if target is None or out == "-":
            return src  # streamed: a later `| tar -x` decides
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
                every = every or "a" in a[1:]  # -a, also inside -fa or -uaf
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
        target = (
            self._path(pos[1], st) if len(pos) > 1 else _child(st.cwd, _humanish(pos[0], False))
        )
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
                        tail = a[k + 1 :]
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
                tail = (
                    urlsplit(url.replace(_UNK, "_")).path.rstrip("/").rsplit("/", 1)[-1]
                    or "download"
                )
                target = _join(base, tail)
            else:
                target = self._stdout_target(redirs, st)
            got = self._download(src, target, st)
            if self._stop():
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
                        tail = a[k + 1 :]
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
                tail = (
                    urlsplit(url.replace(_UNK, "_")).path.rstrip("/").rsplit("/", 1)[-1]
                    or "index.html"
                )
                target = _join(self._path(prefix, st) if prefix else st.cwd, tail)
            got = self._download(src, target, st)
            if self._stop():
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
            letters = args[0]  # old style: tar xzf FILE
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
                        tail = a[k + 1 :]
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
            return "~/" + s[len(home) + 1 :]
        return s

    def _seat_token(self) -> str | None:
        raw = (self.ctx.env.get("AGENT_SEAT") or "").strip()
        if not raw:
            return None
        seat = self.ctx.registry.seat_by_name(raw)
        if seat is not None and not seat_owns_folder(seat, self.ctx.registry):
            return None  # GROK-BOT: the folder name is CURSOR's
        token = seat.suffix if seat is not None else L.normalize_seat(raw, self.ctx.registry)
        return token if L.is_valid_slug(token) else None

    def _slug(self, target: str, app: L.App | None, prefix: str | None, seat: str | None) -> str:
        """The slug for the suggested lane: the target's own name with the app's prefix taken off,
        or `work`.  A slug whose folder would read as another seat's (grok plus build is
        grok-build) is replaced by `work`, because `lane new` refuses it.  `seat` is the folder
        token shown in the reason; with no seat shown (a placeholder), the slug must read back for
        every seat `lane new` accepts, so the command works once AGENT_SEAT is set."""
        base = target.rstrip("/").rsplit("/", 1)[-1].replace(_UNK, "")
        base = _ARCHIVE_EXT_RE.sub("", base)
        base = re.sub(r"X{3,}", "", base)
        slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
        if seat and slug.startswith(seat.lower() + "-"):
            slug = slug[len(seat) + 1 :]  # the folder already names the seat:  claude-x is the slug x
        names = [prefix] if prefix else []
        if app is not None:
            names.extend(app.prefix_aliases())
        for name in sorted({x for x in names if x}, key=len, reverse=True):
            if slug == name:
                slug = ""
                break
            if slug.startswith(name + "-"):
                slug = slug[len(name) + 1 :]
                break
        slug = slug[: L.SLUG_MAX].strip("-")
        if not slug or slug in ("tmp", "temp") or not L.is_valid_slug(slug):
            return "work"
        registry = self.ctx.registry
        if not prefix or registry.app_by_prefix(prefix) is None:
            return slug  # an unknown app: no folder rule to check against
        flat = self.ctx.roots.layout_mode == L.LAYOUT_FLAT
        seats = [seat] if seat else [s.suffix for s in registry.seats if self._lane_new_accepts(s)]
        if all(lane_name_reads_back(prefix, s, slug, registry, flat=flat) for s in seats):
            return slug
        return "work"

    def _repo_dir(self, app: L.App | None, owner_repo: str) -> str | None:
        """The folder under the lanes root for this source (layout v2): the app's repo folder, else the
        GitHub repo name as written (Kodus-Config, never the lowercase prefix), else None."""
        if app is not None and L.is_valid_repo_dir(app.lane_dir):
            return app.lane_dir
        tail = owner_repo.rsplit("/", 1)[-1] if owner_repo else ""
        return tail if L.is_valid_repo_dir(tail) else None

    def _lane(
        self,
        app: L.App | None,
        repo_dir: str | None,
        prefix: str | None,
        seat: str | None,
        slug: str,
    ) -> str:
        roots = self.ctx.roots
        flat = roots.layout_mode == L.LAYOUT_FLAT
        if (
            seat
            and L.is_valid_slug(seat)
            and (repo_dir if not flat else (prefix if prefix and L.is_valid_slug(prefix) else None))
        ):
            try:
                return self.display(
                    str(
                        L.expected_lane_path(
                            app or (prefix if flat else repo_dir),
                            seat,
                            slug,
                            roots,
                            self.ctx.registry,
                        )
                    )
                )
            except L.LayoutError:
                pass
        s = seat or "<seat>"
        if flat:
            p = prefix if prefix and L.is_valid_slug(prefix) else "<prefix>"
            return self.display(f"{roots.apps_root}/{p}-{s}-{slug}")
        return self.display(f"{roots.lanes_root}/{repo_dir or '<Repo>'}/{s}-{slug}")

    def _lane_new_accepts(self, seat: L.Seat) -> bool:
        """The same seat checks `lane.resolve_seat` makes: live, a usable folder name it owns, and
        a branch prefix."""
        return bool(
            not seat.retired
            and seat.primary_branch_prefix()
            and L.is_valid_slug(seat.suffix)
            and seat_owns_folder(seat, self.ctx.registry)
        )

    def _seat_usable_for_lane_new(self) -> bool:
        """True when AGENT_SEAT names a live seat that `lane new` would accept: a registry tag that is
        not retired, has a branch prefix and owns its folder name.  A whole name (antigravity), an
        unknown value, a retired tag or a tag that shares another seat's folder name (GROK-BOT) is
        refused by the CLI, so the reason must not promise a lane for it."""
        raw = (self.ctx.env.get("AGENT_SEAT") or "").strip()
        seat = self.ctx.registry.seat_by_name(raw) if raw else None
        return seat is not None and self._lane_new_accepts(seat)

    def reason(self, hit: _Hit) -> str:
        text = self._reason_text(hit)
        problem = self.ctx.exceptions_problem
        if problem:
            text += (f"  The placement exceptions file cannot be used ({problem}), so no exception was applied;  "
                     "if this path has a row, repair the file instead of moving the work.")
        return text

    def _reason_text(self, hit: _Hit) -> str:
        src = hit.src
        app = src.app
        prefix = app.prefix if app is not None else src.prefix
        seat = self._seat_token()
        lane_new_works = bool(app is not None and app.registered and app.integration_dir_name)
        usable = self._seat_usable_for_lane_new()
        shown_seat = (seat if usable else None) if lane_new_works else seat
        slug = self._slug(hit.target, app, prefix, shown_seat)
        repo_dir = self._repo_dir(app, src.owner_repo)
        review = self.display(f"{self.ctx.roots.lanes_root}/{repo_dir or '<Repo>'}/review-pr-<n>")
        is_code_rule = hit.rule in (
            RULE_CLONE_CODE,
            RULE_GH_CLONE_CODE,
            RULE_WORKTREE_ADD_CODE,
            RULE_WORKTREE_MOVE_CODE,
        )
        location = "inside the ~/Code integration tree" if is_code_rule else "in temp directories"
        if hit.rule == RULE_PLACEMENT_CLONE_IN_LANES:
            location = "as full clones in a lane folder, because a lane is a linked worktree of the integration tree"
        elif hit.rule in PLACEMENT_RULES.values():
            location = "outside the lanes folder"
        blocked = (
            f"Blocked: fleet-repo checkouts are not allowed {location}, and this command "
            f"would put {hit.what} of {src.label} at {self.display(hit.target)}."
        )

        def branch_for(token: str | None) -> str:
            if token:
                try:
                    return L.branch_name(token, slug, self.ctx.registry)
                except L.LayoutError:
                    pass
            return f"<seat>/{slug}"

        if lane_new_works:
            # `lane new` works for this app, so it is the way: it makes the branch and the lane at the
            # path below in one step.  The CLI takes the seat only from AGENT_SEAT and refuses an unset,
            # unknown, retired or folder-sharing one, so in those cases say so up front and show a
            # placeholder.
            token = shown_seat
            tree = self.display(f"{self.ctx.roots.code_root}/{app.integration_dir_name}")
            shim = self.display(f"{self.ctx.roots.apps_root}/lane")
            run = f"run `{shim} new {app.name} {slug}` instead"
            run = (
                (run[0].upper() + run[1:])
                if usable
                else f"Set AGENT_SEAT to your seat tag, then {run}"
            )
            return "  ".join(
                (
                    blocked,
                    f"{run}; it creates branch {branch_for(token)} off origin/main and the lane at "
                    f"{self._lane(app, repo_dir, prefix, token, slug)}.",
                    f"For a read-only PR check, run `{shim} new {app.name} --review --pr <n>` instead; it makes "
                    f"a detached checkout at {review}.",
                    f"Do not do this work in the main integration tree {tree}; that tree is for the human.",
                )
            )
        # No registered integration tree to hang a worktree on (or an unknown repo): `lane new` would
        # refuse, so keep pointing at the lane path itself.
        lane = self._lane(app, repo_dir, prefix, seat, slug)
        if hit.rule == RULE_PLACEMENT_CLONE_IN_LANES and not (app is not None and app.integration_dir_name):
            # That path is the folder just refused, so there is no clone to suggest.  The way in is a worktree off a clone that
            # already exists, or a place in the map for the repo.
            return "  ".join(
                (
                    blocked,
                    f"This repo has no integration tree under {self.display(str(self.ctx.roots.code_root))}, so a lane has "
                    f"nothing to hang from.  If a clone of it exists, run `git -C <that clone> worktree add -b "
                    f"{branch_for(seat)} {lane}`.",
                    "Otherwise give the repo an integration tree in fleet-apps.json, or record why it is an exception in "
                    "scripts/fleet_lanes/placement-exceptions.json.",
                )
            )
        if app is not None and app.integration_dir_name:
            tree = self.display(f"{self.ctx.roots.code_root}/{app.integration_dir_name}")
            example = f"git -C {tree} worktree add -b {branch_for(seat)} {lane} origin/main"
        else:
            tree = "~/Code/<App>"
            # A clone in a lane folder is what `clone-in-lanes` refuses, so never suggest one while that class blocks.
            clone_ok = not self.ctx.mode.blocks(P.CLASS_CLONE_IN_LANES)
            example = f"gh repo clone {src.owner_repo} {lane}" if src.owner_repo and clone_ok else ""
        where = f"Create it under the lanes root instead, at {lane}"
        if example:
            where += f" (for example `{example}`)"
        return "  ".join(
            (
                blocked,
                where + ".",
                f"For a read-only PR check, use a detached checkout at {review} instead.",
                f"Do not do this work in the main integration tree {tree}; that tree is for the human.",
            )
        )
