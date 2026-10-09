"""Lane layout core: where fleet checkouts may live, what they are called, and what they are.

Everything here is pure.  There is no subprocess and no network.  The only filesystem access is
`os.path.realpath` (symlink resolution) and one `.git` existence probe for direct children of
~/Code, and both work from an explicit home path carried by `Roots`, so tests can use a fake home.

Sanctioned places for checkouts:
  1. Integration trees at ~/Code/<App> (main only, human use).  Nothing else belongs at the top of
     ~/Code.
  2. Lanes under ~/apps.  The layout is chosen by the FLEET_LAYOUT environment variable:
       nested (default)  ~/apps/lanes/<prefix>/<seat>-<slug>
                         ~/apps/lanes/_managed/<harness>/   harness-chosen layouts
                         ~/apps/lanes/_review/              short-lived PR verify checkouts
       flat              ~/apps/<prefix>-<seat>-<slug>
     In nested mode a flat top-level checkout under ~/apps is LANE_FLAT_LEGACY (allowed while the
     fleet transitions).  In flat mode it is LANE_FLAT.
  3. Harness-managed worktree locations (~/.codex/worktrees, ~/Code/<App>/.muse/worktrees and
     friends).  Sanctioned but tracked.

Forbidden for any checkout: /tmp, /private/tmp, /var/tmp, /private/var/tmp, the per-user macOS
temp dir (/private/var/folders/*/*/T) and the TMPDIR directory.  macOS /tmp is a symlink to
/private/tmp, so every comparison is made on resolved real paths.

Two platform facts shape the comparisons.  APFS is case-insensitive, so `Roots.case_insensitive`
(true on macOS by default) folds case before any prefix test.  And ~/Code/Socratic.Trade is a
symlink to ~/Code/Socratic-Trade, so `real_key` and `dedupe_paths` collapse aliases of one
directory into one key.

Tests: fleet_lanes/tests/test_layout.py.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

try:
    from enum import StrEnum
except ImportError:  # Python 3.9 and 3.10, including macOS /usr/bin/python3
    # The temp-dir guard hook imports this module under whatever `python3` its PATH finds, and the
    # hook treats any exception as an allow.  A missing StrEnum would switch the guard off without
    # a sound, so layout.py carries the one behavior it needs: members are strs, and str(), format()
    # and json all give the plain value.
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        def __str__(self) -> str:
            return str(self.value)

        __format__ = str.__format__

__all__ = [
    "DEFAULT_LAYOUT", "LAYOUT_FLAT", "LAYOUT_NESTED", "LayoutError",
    "LocationClass", "NameVerdict", "BranchVerdict",
    "App", "Seat", "Registry", "EXTRA_APPS", "APP_PREFIX_ALIASES",
    "default_registry_path", "load_registry", "parse_registry",
    "CANONICAL_SEATS", "SEAT_ALIASES", "ROLE_TOKENS",
    "seat_alias_map", "canonical_seat_set", "normalize_seat", "is_alias_only", "is_known_seat",
    "HarnessLocation", "Roots", "make_roots", "make_guard_roots", "make_tmp_roots", "is_forbidden_tmp",
    "resolve_path", "real_key", "dedupe_paths", "classify_location", "lane_root",
    "validate_slug", "is_valid_slug", "lane_dir_name", "nested_dir_name", "branch_name",
    "expected_lane_path", "LaneNameResult", "explain_lane_name", "check_lane_name",
    "check_branch_name", "seat_from_branch", "upgrade_with_branch",
]

DEFAULT_OWNER = "Simple-With-Us"

LAYOUT_NESTED = "nested"
LAYOUT_FLAT = "flat"
DEFAULT_LAYOUT = LAYOUT_NESTED

ENV_LAYOUT = "FLEET_LAYOUT"
ENV_LANES_ROOT = "FLEET_LANES_ROOT"
ENV_APPS_JSON = "FLEET_APPS_JSON"

MANAGED_DIR = "_managed"
REVIEW_DIR = "_review"

SLUG_MAX = 40
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Temp locations that must never hold a checkout.  The two globs are the per-user macOS temp dir
# (what TMPDIR normally points at); /var is a symlink to /private/var there.
_STATIC_TMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")
_TMP_GLOBS = ("/private/var/folders/*/*/T", "/var/folders/*/*/T")


class LayoutError(ValueError):
    """A name, slug or setting that the lane layout rules reject."""


class LocationClass(StrEnum):
    """Where a path sits relative to the sanctioned and forbidden roots."""

    INTEGRATION_TREE = "INTEGRATION_TREE"            # ~/Code/<registered App>
    LANE_NESTED = "LANE_NESTED"                      # lanes/<prefix>/<dir>
    LANE_FLAT_LEGACY = "LANE_FLAT_LEGACY"            # ~/apps/<dir> while the mode is nested
    LANE_FLAT = "LANE_FLAT"                          # ~/apps/<dir> while the mode is flat
    REVIEW = "REVIEW"                                # lanes/_review/...
    MANAGED = "MANAGED"                              # harness-chosen layouts, sanctioned but tracked
    FORBIDDEN_TMP = "FORBIDDEN_TMP"                  # /tmp and friends
    FORBIDDEN_CODE_TOPLEVEL = "FORBIDDEN_CODE_TOPLEVEL"  # a checkout directly under ~/Code, not a registered tree
    UNSANCTIONED = "UNSANCTIONED"                    # anywhere else: reported, not forbidden


class NameVerdict(StrEnum):
    """Result of a lane directory name check.  Compares equal to the plain strings."""

    CONFORMING = "CONFORMING"
    ALIAS_ONLY = "ALIAS-ONLY"
    NAME_DRIFT = "NAME-DRIFT"
    NON_LANE = "NON-LANE"


class BranchVerdict(StrEnum):
    """Result of a branch name check.

    The first three members are the contract.  DEFAULT_BRANCH (main or master) and DETACHED (no
    branch, or HEAD) are extra members so a caller can tell an integration tree on main from a
    lane on a stray name; a caller that only knows the three treats them as non-conforming.
    """

    CONFORMING = "CONFORMING"
    LEGACY_BRANCH = "LEGACY-BRANCH"
    NON_CONFORMING = "NON-CONFORMING"
    DEFAULT_BRANCH = "DEFAULT-BRANCH"
    DETACHED = "DETACHED"


# --------------------------------------------------------------------------- registry

def _derive_prefix(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")


def _dot_to_dash(name: str) -> tuple[str, ...]:
    """`Congress.Trade` is also written `Congress-Trade` in lane names."""
    return tuple(sorted({name, name.replace(".", "-")}))


def _loose(name: str) -> str:
    """Lowercase with dots and dashes removed: Socratic.Trade, Socratic-Trade and socratictrade agree."""
    return re.sub(r"[.\-]", "", name.lower())


# Alternate spellings of an app that show up at the start of lane directory names.  Keyed by the
# lowercased repo name; the repo name, code dir and acronym are added automatically.
APP_PREFIX_ALIASES: dict[str, tuple[str, ...]] = {
    "ai-fleet-coordinator": ("ai-fleet-coordinator", "afl", "afc"),
    "socratic-trade": ("socratic-trade", "socratic.trade", "st"),
    "usage-monitor": ("usage-monitor", "um"),
    "congress.trade": ("congress.trade", "congress-trade", "ct"),
    "congress-trading-shared": ("congress-trading-shared", "cts"),
    "botfleet": ("botfleet", "bf"),
    "hoghunter": ("hoghunter", "hh"),
    "personal-site": ("personal-site", "ps"),
    "contactlogo": ("contactlogo", "cl"),
    "dealdex": ("dealdex", "dd"),
    "autorotate": ("autorotate", "ar"),
    "fleet-ops": ("fleet-ops", "ops"),
    "clutch": ("clutch", "ck", "harness"),
    "simple-with-us": ("simple-with-us", "simplewithus", "swu"),
}


@dataclass(frozen=True)
class App:
    """One fleet repo.

    `prefix` is the lane name prefix (worktreePrefix in fleet-apps.json).  `integration_dir_name`
    is the directory under ~/Code that holds the main-only tree, or "" when the app has none.
    `registered` is False for apps that have lanes on disk but no row in fleet-apps.json.
    `owner_repo` is "" when the GitHub repo could not be confirmed.
    """

    name: str
    owner_repo: str
    prefix: str
    integration_dir_name: str
    registered: bool = True
    acronym: str = ""
    display_name: str = ""
    kind: str = ""

    def prefix_aliases(self) -> tuple[str, ...]:
        """Other spellings that appear at the front of lane names, longest first, prefix excluded."""
        found: set[str] = set()
        for raw in (self.name, self.integration_dir_name, self.acronym):
            if raw:
                found.update(_dot_to_dash(raw.lower()))
        found.update(APP_PREFIX_ALIASES.get(self.name.lower(), ()))
        found.discard(self.prefix.lower())
        found.discard("")
        return tuple(sorted(found, key=lambda s: (-len(s), s)))

    def integration_names(self) -> tuple[str, ...]:
        """Directory names under ~/Code that count as this app's integration tree.  Classification
        compares them ignoring dots and dashes, so ~/Code/Socratic.Trade counts as Socratic-Trade."""
        return (self.integration_dir_name,) if self.integration_dir_name else ()


@dataclass(frozen=True)
class Seat:
    """One coding seat.  `name` is the registry tag (AG), `suffix` the lane token (antigravity)."""

    name: str
    suffix: str
    branch_prefixes: tuple[str, ...] = ()
    retired: bool = False
    notes_name: str = ""

    def primary_branch_prefix(self) -> str:
        """First slash-terminated branch prefix, without the slash ("" when there is none)."""
        for bp in self.branch_prefixes:
            if bp.endswith("/") and bp.count("/") == 1:
                return bp[:-1]
        return ""


# Apps with lanes on disk but no fleet-apps.json row.  CodeCaps has a row now;  its entry here stays
# only as the fallback for an installed copy of the registry that predates the row, and a registry row
# wins.  owner_repo and prefix come from the git remotes and lane names seen in the 2026-10-07 sweep.
# mmx-acp had no remote (owner_repo ""), and Kodus-Config, upptime-status and mmx-acp have no ~/Code
# tree (their clone lives in ~/apps), so their integration_dir_name is "".  Simple-With-Us appears on
# disk as simple-with-us-*, simplewithus-* and Simple-With-Us-*; the hyphenated lowercase form is the
# prefix.
EXTRA_APPS: tuple[App, ...] = (
    App("CodeCaps", f"{DEFAULT_OWNER}/codecaps", "codecaps", "CodeCaps", registered=False),
    App("FleetLink", f"{DEFAULT_OWNER}/FleetLink", "fleetlink", "FleetLink", registered=False),
    App("Simple-With-Us", f"{DEFAULT_OWNER}/Simple-With-Us", "simple-with-us", "Simple-With-Us",
        registered=False),
    App("Kodus-Config", f"{DEFAULT_OWNER}/Kodus-Config", "kodus-config", "", registered=False),
    App("upptime-status", f"{DEFAULT_OWNER}/upptime-status", "upptime-status", "", registered=False),
    App("mmx-acp", "", "mmx-acp", "", registered=False),
    App("homebrew-tap", f"{DEFAULT_OWNER}/homebrew-tap", "homebrew-tap", "homebrew-tap", registered=False),
)


@dataclass(frozen=True)
class Registry:
    """The parsed fleet-apps.json plus the EXTRA_APPS rows."""

    owner: str
    apps: tuple[App, ...]
    seats: tuple[Seat, ...]
    code_root: str = ""
    apps_root: str = ""
    source: str = ""

    @property
    def registered_apps(self) -> tuple[App, ...]:
        return tuple(a for a in self.apps if a.registered)

    def prefixes(self) -> tuple[str, ...]:
        return tuple(a.prefix for a in self.apps)

    def app_by_prefix(self, prefix: str) -> App | None:
        want = prefix.strip().lower()
        for app in self.apps:
            if app.prefix.lower() == want:
                return app
        return None

    def app_by_name(self, name: str) -> App | None:
        """Look up by repo name or integration dir, ignoring case, dots and dashes."""
        want = _loose(name.strip())
        for app in self.apps:
            if want in {_loose(app.name), *(_loose(n) for n in app.integration_names())}:
                return app
        return None

    def seat_by_name(self, tag: str) -> Seat | None:
        want = tag.strip().lower()
        for seat in self.seats:
            if seat.name.lower() == want:
                return seat
        return None

    def seats_by_suffix(self, suffix: str) -> tuple[Seat, ...]:
        want = suffix.strip().lower()
        return tuple(s for s in self.seats if s.suffix.lower() == want)

    def seat_by_suffix(self, suffix: str) -> Seat | None:
        """Several tags can share a suffix (CURSOR and GROK-BOT both use `cursor`).

        The winner is deterministic: the first live seat in registry order, else the first
        retired one.  So `cursor` is always the CURSOR seat.
        """
        matches = self.seats_by_suffix(suffix)
        for seat in matches:
            if not seat.retired:
                return seat
        return matches[0] if matches else None

    def integration_names(self) -> tuple[str, ...]:
        out: list[str] = []
        for app in self.apps:
            for n in app.integration_names():
                if n not in out:
                    out.append(n)
        return tuple(out)


def _as_str(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def parse_registry(data: Mapping[str, object], *, include_extras: bool = True, source: str = "") -> Registry:
    """Build a Registry from already-loaded JSON.  Tolerates missing and extra fields."""
    owner = _as_str(data.get("owner")) or DEFAULT_OWNER
    apps: list[App] = []
    raw_apps = data.get("apps")
    for item in raw_apps if isinstance(raw_apps, list) else []:
        if not isinstance(item, dict):
            continue
        repo = _as_str(item.get("repo")) or _as_str(item.get("name"))
        if not repo:
            continue
        apps.append(App(
            name=repo,
            owner_repo=_as_str(item.get("ownerRepo")) or f"{owner}/{repo}",
            prefix=_as_str(item.get("worktreePrefix")) or _derive_prefix(repo),
            integration_dir_name=_as_str(item.get("codeDir")) or repo,
            registered=True,
            acronym=_as_str(item.get("acronym")),
            display_name=_as_str(item.get("displayName")),
            kind=_as_str(item.get("kind")),
        ))
    if include_extras:
        have_names = {a.name.lower() for a in apps}
        have_prefixes = {a.prefix.lower() for a in apps}
        for extra in EXTRA_APPS:
            if extra.name.lower() in have_names or extra.prefix.lower() in have_prefixes:
                continue
            apps.append(extra)
    seats: list[Seat] = []
    raw_seats = data.get("seats")
    for item in raw_seats if isinstance(raw_seats, list) else []:
        if not isinstance(item, dict):
            continue
        tag = _as_str(item.get("tag"))
        if not tag:
            continue
        bps = item.get("branchPrefixes")
        seats.append(Seat(
            name=tag,
            suffix=(_as_str(item.get("worktreeSuffix")) or tag).lower(),
            branch_prefixes=tuple(b for b in bps if isinstance(b, str) and b) if isinstance(bps, list) else (),
            retired=bool(item.get("retired")),
            notes_name=_as_str(item.get("notesName")),
        ))
    return Registry(
        owner=owner, apps=tuple(apps), seats=tuple(seats),
        code_root=_as_str(data.get("codeRoot")), apps_root=_as_str(data.get("appsRoot")), source=source,
    )


def default_registry_path(env: Mapping[str, str] | None = None) -> Path:
    """FLEET_APPS_JSON when set, else fleet-apps.json at the repo root (two levels above scripts/)."""
    env = os.environ if env is None else env
    override = (env.get(ENV_APPS_JSON) or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "fleet-apps.json"


def load_registry(path: str | os.PathLike[str] | None = None, *, include_extras: bool = True,
                  env: Mapping[str, str] | None = None) -> Registry:
    """Read fleet-apps.json.  Raises OSError or ValueError when the file is missing or not JSON."""
    target = Path(path) if path is not None else default_registry_path(env)
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{target}: expected a JSON object at the top level")
    return parse_registry(data, include_extras=include_extras, source=str(target))


# --------------------------------------------------------------------------- seats

# Lane-name tokens for every seat in the registry, kept here so a registry that fails to load
# still names the seats.  The registry adds to this set.
CANONICAL_SEATS: tuple[str, ...] = (
    "claude", "monet", "codex", "antigravity", "cursor", "grok", "grok-build", "deepseek",
    "harness", "renoir", "fx", "kimi", "minimax", "muse-assist", "muse-code",
)

# Short forms in wide use on disk.  `gb` and `grok-bot` map to `cursor` because the registry gives
# GROK-BOT the cursor suffix and the cursor/ branch prefix.
SEAT_ALIASES: dict[str, str] = {
    "ag": "antigravity",
    "mm": "minimax",
    "ma": "muse-assist",
    "muse": "muse-assist",
    "mc": "muse-code",
    "dsh": "deepseek",
    "gb": "cursor",
    "grok-bot": "cursor",
}

# BotFleet bot roles show up in lane names and branch prefixes.  They are not seats.
ROLE_TOKENS = frozenset({
    "kody", "fixer", "designer", "compiler", "plumber", "publisher", "deployer", "director",
    "housekeeper", "oracle", "nurse", "accountant", "conductor", "bf",
})


def canonical_seat_set(registry: Registry | None = None) -> frozenset[str]:
    out = set(CANONICAL_SEATS)
    if registry is not None:
        out.update(s.suffix.lower() for s in registry.seats)
    return frozenset(out)


def seat_alias_map(registry: Registry | None = None) -> dict[str, str]:
    """Alias token -> canonical token: the hand table plus, from the registry, each tag whose
    lowercase differs from its suffix (ag, mm, dsh, ...) and each single-segment branch prefix."""
    out = dict(SEAT_ALIASES)
    if registry is None:
        return out
    canon = canonical_seat_set(registry)
    for seat in registry.seats:
        target = seat.suffix.lower()
        tokens = [seat.name.lower()]
        tokens += [bp[:-1].lower() for bp in seat.branch_prefixes if bp.endswith("/") and bp.count("/") == 1]
        for tok in tokens:
            if tok and tok != target and tok not in canon and tok not in out:
                out[tok] = target
    return out


def normalize_seat(token: str, registry: Registry | None = None) -> str:
    """Canonical lane token for a seat: ag -> antigravity, mm -> minimax, gb -> cursor.

    An unknown token comes back lowercased and unchanged, so callers can still report it.
    """
    t = token.strip().lower()
    if t in canonical_seat_set(registry):
        return t
    return seat_alias_map(registry).get(t, t)


def is_alias_only(token: str, registry: Registry | None = None) -> bool:
    """True for a short form (ag, mm, ...) and False for a canonical token or an unknown one."""
    t = token.strip().lower()
    return t not in canonical_seat_set(registry) and t in seat_alias_map(registry)


def is_known_seat(token: str, registry: Registry | None = None) -> bool:
    return normalize_seat(token, registry) in canonical_seat_set(registry)


# --------------------------------------------------------------------------- roots

@dataclass(frozen=True)
class HarnessLocation:
    """A place a harness creates checkouts on its own.

    `glob_or_prefix` is an absolute path that may contain glob wildcards in whole path components
    (Code/*/.claude/worktrees).  A path under it, or equal to it, matches.  `sanctioned` False
    marks a place that is reported as UNSANCTIONED instead of MANAGED.
    """

    name: str
    glob_or_prefix: str
    sanctioned: bool = True


# (name, path relative to home, sanctioned)
_HARNESS_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("claude-repo", "Code/*/.claude/worktrees", True),
    ("muse-repo", "Code/*/.muse/worktrees", True),
    ("codex", ".codex/worktrees", True),
    ("cursor", ".cursor/worktrees", True),
    ("grok", ".grok/worktrees", True),
    ("antigravity", ".gemini/antigravity/worktrees", True),
    ("antigravity-ag", ".ag/worktrees", True),
    ("botfleet", ".botfleet/worktrees", True),
    ("codecaps-pages", "codecaps-pages", True),
    ("antigravity-scratch", ".gemini/antigravity/scratch", False),
    ("botfleet-workspaces", ".botfleet/workspaces", False),
    ("documents", "Documents", False),
)


@dataclass(frozen=True)
class Roots:
    """Every root the classifier needs, resolved to real paths.  Build one with `make_roots`."""

    home: Path
    code_root: Path
    apps_root: Path
    lanes_root: Path
    managed_root: Path
    review_root: Path
    harness_locations: tuple[HarnessLocation, ...]
    tmp_roots: tuple[Path, ...]
    tmp_globs: tuple[str, ...]
    layout_mode: str = DEFAULT_LAYOUT
    case_insensitive: bool = False
    integration_names: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


def make_tmp_roots(env: Mapping[str, str] | None = None,
                   home: str | os.PathLike[str] | None = None) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Forbidden temp roots and temp globs.  Parses no layout and loads no registry, so a guard
    can call it with nothing else set up.  Each root is listed as written and as resolved."""
    env = os.environ if env is None else env
    seen: list[str] = []

    def add(raw: str) -> None:
        for cand in (os.path.normpath(raw), _real(raw)):
            if cand not in seen and cand != os.sep:
                seen.append(cand)

    for static in _STATIC_TMP_ROOTS:
        add(static)
    tmpdir = (env.get("TMPDIR") or "").strip()
    if tmpdir.startswith("~") and home is not None:
        tmpdir = os.path.join(os.fspath(home), tmpdir[2:])
    if tmpdir and os.path.isabs(tmpdir):
        add(tmpdir)
    return tuple(Path(s) for s in seen), _TMP_GLOBS


def _pattern_under(home: str, rel: str) -> str:
    """Absolute glob for `rel` under home, with the wildcard-free leading part resolved."""
    comps = rel.split("/")
    literal: list[str] = []
    for comp in comps:
        if glob.has_magic(comp):
            break
        literal.append(comp)
    base = _real(os.path.join(home, *literal))
    rest = comps[len(literal):]
    return "/".join([glob.escape(base), *rest]) if rest else glob.escape(base)


def make_roots(home: str | os.PathLike[str], env: Mapping[str, str] | None = None, *,
               registry: Registry | None = None, case_insensitive: bool | None = None) -> Roots:
    """Resolve every root under `home`.

    `env` is read for FLEET_LAYOUT, FLEET_LANES_ROOT and TMPDIR only; pass an explicit dict in
    tests so the process environment never leaks in.  FLEET_LAYOUT other than nested or flat falls
    back to nested and is recorded in `warnings` (it never raises, so forbidden-tmp detection stays
    on).  `registry` defaults to `load_registry(env=env)`, which can raise if the file is
    unreadable.  `case_insensitive` defaults to true on macOS.
    """
    env = os.environ if env is None else env
    real_home = _real(home)
    warnings: list[str] = []

    raw_mode = (env.get(ENV_LAYOUT) or "").strip().lower()
    if raw_mode in (LAYOUT_NESTED, LAYOUT_FLAT):
        mode = raw_mode
    else:
        mode = DEFAULT_LAYOUT
        if raw_mode:
            warnings.append(f"unrecognized {ENV_LAYOUT} {raw_mode!r}; using {DEFAULT_LAYOUT}")

    code_root = _real(os.path.join(real_home, "Code"))
    apps_root = _real(os.path.join(real_home, "apps"))
    raw_lanes = (env.get(ENV_LANES_ROOT) or "").strip()
    if raw_lanes == "~" or raw_lanes.startswith("~/"):
        lanes = os.path.join(real_home, raw_lanes[2:])
    elif raw_lanes and os.path.isabs(raw_lanes):
        lanes = raw_lanes
    elif raw_lanes:
        lanes = os.path.join(real_home, raw_lanes)
    else:
        lanes = os.path.join(apps_root, "lanes")
    lanes_root = _real(lanes)

    if registry is None:
        registry = load_registry(env=env)
    tmp_roots, tmp_globs = make_tmp_roots(env, real_home)
    return Roots(
        home=Path(real_home),
        code_root=Path(code_root),
        apps_root=Path(apps_root),
        lanes_root=Path(lanes_root),
        managed_root=Path(lanes_root) / MANAGED_DIR,
        review_root=Path(lanes_root) / REVIEW_DIR,
        harness_locations=tuple(
            HarnessLocation(name, _pattern_under(real_home, rel), sanctioned)
            for name, rel, sanctioned in _HARNESS_SPECS
        ),
        tmp_roots=tmp_roots,
        tmp_globs=tmp_globs,
        layout_mode=mode,
        case_insensitive=(sys.platform == "darwin") if case_insensitive is None else bool(case_insensitive),
        integration_names=registry.integration_names(),
        warnings=tuple(warnings),
    )


def make_guard_roots(home: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Roots:
    """Roots for a forbidden-tmp guard: reads no file and cannot fail on a bad registry or layout.

    The registry is empty, so integration trees are not recognised (INTEGRATION_TREE and
    FORBIDDEN_CODE_TOPLEVEL are not meaningful from these roots), but `is_forbidden_tmp` and the
    FORBIDDEN_TMP class work exactly as with `make_roots`.
    """
    return make_roots(home, env, registry=Registry(owner=DEFAULT_OWNER, apps=(), seats=()))


# --------------------------------------------------------------------------- path helpers

def _fold(roots: Roots):
    return str.casefold if roots.case_insensitive else (lambda s: s)


def _parts(path: str | os.PathLike[str], roots: Roots) -> tuple[str, ...]:
    fold = _fold(roots)
    return tuple(fold(p) for p in Path(os.fspath(path)).parts)


def _under(parts: tuple[str, ...], root_parts: tuple[str, ...]) -> bool:
    return len(parts) >= len(root_parts) and parts[:len(root_parts)] == root_parts


def _glob_match(parts: tuple[str, ...], pattern: str, roots: Roots) -> int:
    """Number of components in `pattern` when `parts` starts with it (wildcards per component),
    else 0.  A path under the pattern, or equal to it, matches."""
    pat = _parts(pattern, roots)
    if len(parts) < len(pat):
        return 0
    for part, pcomp in zip(parts, pat):
        if not fnmatch.fnmatchcase(part, pcomp):
            return 0
    return len(pat)


def resolve_path(path: str | os.PathLike[str], roots: Roots) -> str:
    """Absolute real path.  `~` and `~/x` expand against `roots.home`; symlinks are resolved, so
    /tmp/x becomes /private/tmp/x on macOS.  Relative paths resolve against the process cwd."""
    s = os.fspath(path)
    if s == "~" or s.startswith("~/"):
        s = os.path.join(os.fspath(roots.home), s[2:])
    return _real(s)


def real_key(path: str | os.PathLike[str], roots: Roots) -> str:
    """Identity of the directory a path names: resolved, and case-folded on a case-insensitive
    volume.  ~/Code/Socratic.Trade and ~/Code/Socratic-Trade share one key."""
    return _fold(roots)(resolve_path(path, roots))


def dedupe_paths(paths: Iterable[str | os.PathLike[str]], roots: Roots) -> list[Path]:
    """First path seen for each distinct `real_key`, in input order."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = real_key(p, roots)
        if key not in seen:
            seen.add(key)
            out.append(Path(os.fspath(p)))
    return out


def _tmp_hit(parts: tuple[str, ...], roots: Roots) -> bool:
    """True when `parts` is inside a forbidden temp location.

    A temp location that contains the home exempts only the home's own subtree, never its
    siblings.  A real home is never inside a temp dir; a test home made with tempfile is, and
    without the exemption every sanctioned path under that home would read as forbidden.  Paths
    next to the home (on Linux, /tmp/dealdex-work beside /tmp/tmpAbC123) stay forbidden, so a
    temp-dir test home cannot switch the whole /tmp root off.
    """
    home_parts = _parts(roots.home, roots)
    in_home = _under(parts, home_parts)
    for root in roots.tmp_roots:
        rp = _parts(root, roots)
        if _under(parts, rp) and not (in_home and _under(home_parts, rp)):
            return True
    for pattern in roots.tmp_globs:
        n = _glob_match(parts, pattern, roots)
        if n and not (in_home and home_parts[:n] == parts[:n]):
            return True
    return False


def is_forbidden_tmp(path: str | os.PathLike[str], roots: Roots) -> bool:
    """True when the resolved path is inside /tmp, /var/tmp, the macOS user temp dir or TMPDIR."""
    return _tmp_hit(_parts(resolve_path(path, roots), roots), roots)


def _loose_exact(name: str) -> str:
    """Drop dots and dashes but keep case, so a case-sensitive volume still tells Fleet-OPS from fleet-ops."""
    return re.sub(r"[.\-]", "", name)


def _has_git_entry(directory: str) -> bool:
    """A checkout has a `.git` directory (clone) or a `.git` file (linked worktree)."""
    return os.path.lexists(os.path.join(directory, ".git"))


# --------------------------------------------------------------------------- classification

def _classify(real: str, roots: Roots, is_checkout: bool | None) -> tuple[LocationClass, str | None]:
    """(class, lane directory or None) for an already-resolved path."""
    orig = Path(real).parts
    parts = _parts(real, roots)
    fold = _fold(roots)

    if _tmp_hit(parts, roots):
        return LocationClass.FORBIDDEN_TMP, None

    best: tuple[int, HarnessLocation] | None = None
    for loc in roots.harness_locations:
        n = _glob_match(parts, loc.glob_or_prefix, roots)
        if n and (best is None or n > best[0]):
            best = (n, loc)
    if best is not None:
        return (LocationClass.MANAGED if best[1].sanctioned else LocationClass.UNSANCTIONED), None

    lanes_p = _parts(roots.lanes_root, roots)
    if _under(parts, lanes_p):
        if _under(parts, _parts(roots.managed_root, roots)):
            return LocationClass.MANAGED, None
        if _under(parts, _parts(roots.review_root, roots)):
            return LocationClass.REVIEW, None
        rel = parts[len(lanes_p):]
        if roots.layout_mode == LAYOUT_NESTED and len(rel) >= 2 and not rel[0].startswith("_"):
            return LocationClass.LANE_NESTED, os.path.join(*orig[:len(lanes_p) + 2])
        return LocationClass.UNSANCTIONED, None

    code_p = _parts(roots.code_root, roots)
    if _under(parts, code_p):
        rel = parts[len(code_p):]
        if not rel:
            return LocationClass.UNSANCTIONED, None
        if _loose_exact(rel[0]) in {_loose_exact(fold(n)) for n in roots.integration_names}:
            return LocationClass.INTEGRATION_TREE, None
        top = os.path.join(*orig[:len(code_p) + 1])
        checkout = _has_git_entry(top) if is_checkout is None else is_checkout
        return (LocationClass.FORBIDDEN_CODE_TOPLEVEL if checkout else LocationClass.UNSANCTIONED), None

    apps_p = _parts(roots.apps_root, roots)
    if _under(parts, apps_p):
        rel = parts[len(apps_p):]
        if not rel or rel[0].startswith(("_", ".")):
            return LocationClass.UNSANCTIONED, None
        cls = LocationClass.LANE_FLAT if roots.layout_mode == LAYOUT_FLAT else LocationClass.LANE_FLAT_LEGACY
        return cls, os.path.join(*orig[:len(apps_p) + 1])

    return LocationClass.UNSANCTIONED, None


def classify_location(path: str | os.PathLike[str], roots: Roots, *,
                      is_checkout: bool | None = None) -> LocationClass:
    """Classify where `path` sits.

    Symlinks are resolved first and prefixes are compared case-insensitively when
    `roots.case_insensitive` is set.  The order is: forbidden temp, harness locations (so
    ~/Code/<App>/.claude/worktrees/x is MANAGED, not INTEGRATION_TREE), the lanes root, ~/Code,
    ~/apps, then UNSANCTIONED.  A direct child of ~/Code that is not a registered integration tree
    is FORBIDDEN_CODE_TOPLEVEL only if it is a checkout; `is_checkout` overrides the `.git` probe.
    The Claude harness scratchpad under /private/tmp/claude-<uid> is not special-cased here: the
    guard decides that, so this returns FORBIDDEN_TMP for it like any other temp path.
    """
    return _classify(resolve_path(path, roots), roots, is_checkout)[0]


def lane_root(path: str | os.PathLike[str], roots: Roots) -> Path | None:
    """The lane directory a path belongs to (~/apps/<dir> or lanes/<prefix>/<dir>), else None.
    A checkout whose path differs from its lane root is nested inside the lane."""
    found = _classify(resolve_path(path, roots), roots, None)[1]
    return Path(found) if found else None


# --------------------------------------------------------------------------- naming

def is_valid_slug(slug: object) -> bool:
    return isinstance(slug, str) and 1 <= len(slug) <= SLUG_MAX and bool(_KEBAB_RE.match(slug))


def validate_slug(slug: str) -> str:
    """Lowercase kebab, 1..40 chars, no leading, trailing or doubled dash.  Returns the slug."""
    if not isinstance(slug, str):
        raise LayoutError(f"slug must be a string, got {type(slug).__name__}")
    if not 1 <= len(slug) <= SLUG_MAX:
        raise LayoutError(f"slug must be 1..{SLUG_MAX} chars, got {len(slug)}: {slug!r}")
    if not _KEBAB_RE.match(slug):
        raise LayoutError(f"slug must be lowercase kebab (a-z, 0-9, single dashes inside): {slug!r}")
    return slug


def _token(value: str, what: str) -> str:
    if not isinstance(value, str) or not _KEBAB_RE.match(value):
        raise LayoutError(f"{what} must be lowercase kebab, got {value!r}")
    return value


def lane_dir_name(prefix: str, seat: str, slug: str | None = None) -> str:
    """Flat lane directory name: <prefix>-<seat>[-<slug>].  The seat is used as given."""
    name = f"{_token(prefix, 'prefix')}-{_token(seat, 'seat')}"
    return f"{name}-{validate_slug(slug)}" if slug is not None else name


def nested_dir_name(seat: str, slug: str | None = None) -> str:
    """Directory name inside lanes/<prefix>/: <seat>[-<slug>]."""
    name = _token(seat, "seat")
    return f"{name}-{validate_slug(slug)}" if slug is not None else name


def branch_name(seat: str, slug: str, registry: Registry | None = None) -> str:
    """<seat>/<slug>.  With a registry the seat is mapped to its primary branch prefix (antigravity
    gives ag/<slug>, minimax gives minimax/<slug>); without one the seat is used as given."""
    validate_slug(slug)
    if registry is not None:
        match = registry.seat_by_suffix(normalize_seat(seat, registry))
        if match is not None and match.primary_branch_prefix():
            return f"{match.primary_branch_prefix()}/{slug}"
    return f"{_token(seat.strip().lower(), 'seat')}/{slug}"


def expected_lane_path(app: App | str, seat: str, slug: str | None, roots: Roots,
                       registry: Registry | None = None) -> Path:
    """Where a new lane belongs under the active layout mode.

    nested: lanes_root/<prefix>/<seat>[-<slug>];  flat: apps_root/<prefix>-<seat>[-<slug>].
    `app` is an App or a prefix string.  The seat is normalized (ag becomes antigravity).  A
    missing slug names the seat's home lane for that app.
    """
    prefix = app.prefix if isinstance(app, App) else str(app).strip().lower()
    _token(prefix, "prefix")
    canon = normalize_seat(seat, registry)
    if roots.layout_mode == LAYOUT_FLAT:
        return roots.apps_root / lane_dir_name(prefix, canon, slug)
    return roots.lanes_root / prefix / nested_dir_name(canon, slug)


@dataclass(frozen=True)
class _SeatHit:
    kind: str       # canonical | alias | role
    seat: str       # canonical token, or BF-<ROLE> for a role
    ntokens: int
    text: str       # the tokens as written, joined with dashes


def _seat_at(tokens: Sequence[str], i: int, canon: frozenset[str], aliases: Mapping[str, str]) -> _SeatHit | None:
    """Seat at token position i.  Two-token seats (grok-build, muse-code) win over one-token ones."""
    for n in (2, 1):
        if i + n <= len(tokens):
            cand = "-".join(tokens[i:i + n])
            if cand in canon:
                return _SeatHit("canonical", cand, n, cand)
            if cand in aliases:
                return _SeatHit("alias", aliases[cand], n, cand)
    tok = tokens[i] if i < len(tokens) else ""
    if tok == "bf":
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if nxt in ROLE_TOKENS and nxt != "bf":
            return _SeatHit("role", "BF-" + nxt.upper(), 2, f"bf-{nxt}")
        return _SeatHit("role", "BF-BOT", 1, "bf")
    if tok in ROLE_TOKENS:
        return _SeatHit("role", "BF-" + tok.upper(), 1, tok)
    return None


def _seat_anywhere(tokens: Sequence[str], canon: frozenset[str], aliases: Mapping[str, str]) -> tuple[int, _SeatHit] | None:
    for i in range(len(tokens)):
        hit = _seat_at(tokens, i, canon, aliases)
        if hit is not None:
            return i, hit
    return None


def _match_app(lname: str, registry: Registry) -> tuple[App, str, str] | None:
    """The app a lowercased name starts with, at token 0 only: (app, matched text, prefix|alias).

    The longest match wins, so fleet-ops-minimax is the fleet-ops app and not `fleet` plus a slug.
    At equal length an exact prefix beats an alias, then registry order decides.
    """
    cands: list[tuple[int, int, int, str, App, str]] = []
    for idx, app in enumerate(registry.apps):
        cands.append((-len(app.prefix), 0, idx, app.prefix.lower(), app, "prefix"))
        for alias in app.prefix_aliases():
            cands.append((-len(alias), 1, idx, alias, app, "alias"))
    for _, _, _, text, app, kind in sorted(cands, key=lambda c: c[:3]):
        if lname == text or lname.startswith(text + "-"):
            return app, text, kind
    return None


@dataclass(frozen=True)
class LaneNameResult:
    """Why a lane directory name got its verdict.

    `seat` is the canonical seat token (or BF-<ROLE>), `seat_token` the text as written.
    `layout` is `flat` or `nested` for names in a lane location and None otherwise.
    """

    verdict: NameVerdict
    reasons: tuple[str, ...]
    location: LocationClass
    app: App | None = None
    seat: str | None = None
    seat_token: str | None = None
    slug: str | None = None
    layout: str | None = None


def _eval_name(name: str, registry: Registry, *, nested_prefix_dir: str | None) -> tuple[NameVerdict, list[str], App | None, _SeatHit | None, str | None]:
    """Judge one lane directory name.  `nested_prefix_dir` is the lanes/<prefix> directory name for
    a nested lane (the name then holds only <seat>[-<slug>]), or None for a flat lane."""
    canon = canonical_seat_set(registry)
    aliases = seat_alias_map(registry)
    lname = name.lower()
    reasons: list[str] = []
    kebab = bool(_KEBAB_RE.match(name))
    if name != lname:
        reasons.append("case")
    if not _KEBAB_RE.match(lname):
        reasons.append("not-kebab")

    app: App | None
    exact = False
    alias_prefix = False
    if nested_prefix_dir is None:
        m = _match_app(lname, registry)
        if m is None:
            toks = lname.split("-")
            anywhere = _seat_anywhere(toks, canon, aliases)
            reasons.append("no-app-prefix")
            if anywhere is not None:
                return NameVerdict.NAME_DRIFT, reasons, None, anywhere[1], None
            return NameVerdict.NON_LANE, reasons, None, None, None
        app, text, kind = m
        exact = kind == "prefix" and name[:len(text)] == text
        if kind == "alias":
            alias_prefix = True
            reasons.append(f"alias-prefix:{text}")
        elif not exact:
            reasons.append("prefix-case")
        rest = lname[len(text) + 1:] if len(lname) > len(text) else ""
        has_rest = bool(rest)
    else:
        app = registry.app_by_prefix(nested_prefix_dir)
        exact = app is not None and nested_prefix_dir == app.prefix
        if app is None:
            m = _match_app(nested_prefix_dir.lower(), registry)
            if m is not None and m[1] == nested_prefix_dir.lower():
                app = m[0]
                reasons.append(f"prefix-dir-alias:{nested_prefix_dir}")
            else:
                reasons.append(f"unknown-prefix-dir:{nested_prefix_dir}")
        elif not exact:
            reasons.append(f"prefix-dir-case:{nested_prefix_dir}")
        rest = lname
        has_rest = bool(rest)

    toks = rest.split("-") if rest else []
    first = _seat_at(toks, 0, canon, aliases) if toks else None
    if first is not None and first.kind in ("canonical", "alias"):
        slug = "-".join(toks[first.ntokens:]) or None
        if slug is not None and len(slug) > SLUG_MAX:
            reasons.append(f"slug-over-{SLUG_MAX}")
        if exact and kebab:
            if first.kind == "canonical":
                reasons.append("seat-token-ok:" + first.text)
                return NameVerdict.CONFORMING, reasons, app, first, slug
            reasons.append("seat-alias:" + first.text)
            return NameVerdict.ALIAS_ONLY, reasons, app, first, slug
        return NameVerdict.NAME_DRIFT, reasons, app, first, slug
    if first is not None:
        reasons.append("bot-role-not-seat:" + toks[0])
        return NameVerdict.NAME_DRIFT, reasons, app, first, None
    anywhere = _seat_anywhere(toks, canon, aliases)
    if anywhere is not None:
        reasons.append("seat-not-adjacent-to-prefix:" + toks[anywhere[0]])
        return NameVerdict.NAME_DRIFT, reasons, app, anywhere[1], None
    if alias_prefix and has_rest:
        reasons.append("alias-prefix-without-seat")
        return NameVerdict.NAME_DRIFT, reasons, app, None, None
    reasons.append("no-seat-token")
    return NameVerdict.NON_LANE, reasons, app, None, None


def explain_lane_name(path: str | os.PathLike[str], registry: Registry, roots: Roots) -> LaneNameResult:
    """Judge the lane directory name of `path` and say why.

    The app is inferred from the name (longest match across registered prefixes and aliases, at the
    first token only), because there is no git remote to read here.  Verdicts:
      CONFORMING  exact prefix, canonical seat token, kebab slug
      ALIAS-ONLY  exact prefix and slug, but the seat is a short form (ag, mm, gb, ...)
      NAME-DRIFT  lane-shaped but wrong: alias or mis-cased prefix, bot role, seat out of place
      NON-LANE    nothing in the name says it is a lane (botfleet-server, research)
    Paths outside a lane location (integration trees, managed, review, tmp) are NON-LANE with the
    location named in the reasons.  The name is judged from the resolved path.
    """
    real = resolve_path(path, roots)
    cls, lane_dir = _classify(real, roots, None)
    if lane_dir is None or cls not in (LocationClass.LANE_NESTED, LocationClass.LANE_FLAT,
                                       LocationClass.LANE_FLAT_LEGACY):
        return LaneNameResult(NameVerdict.NON_LANE, (f"location:{cls}",), cls)
    lane = Path(lane_dir)
    if cls is LocationClass.LANE_NESTED:
        verdict, reasons, app, hit, slug = _eval_name(lane.name, registry, nested_prefix_dir=lane.parent.name)
        layout = LAYOUT_NESTED
    else:
        verdict, reasons, app, hit, slug = _eval_name(lane.name, registry, nested_prefix_dir=None)
        layout = LAYOUT_FLAT
    return LaneNameResult(verdict, tuple(reasons), cls, app, hit.seat if hit else None,
                          hit.text if hit else None, slug, layout)


def check_lane_name(path: str | os.PathLike[str], registry: Registry, roots: Roots) -> NameVerdict:
    """CONFORMING, ALIAS-ONLY, NAME-DRIFT or NON-LANE.  See `explain_lane_name` for the reasons."""
    return explain_lane_name(path, registry, roots).verdict


def _branch_prefixes(registry: Registry) -> tuple[list[str], list[str]]:
    current: list[str] = []
    legacy: list[str] = []
    for seat in registry.seats:
        for bp in seat.branch_prefixes:
            (current if bp.endswith("/") else legacy).append(bp)
    return current, legacy


def check_branch_name(branch: str | None, registry: Registry) -> BranchVerdict:
    """CONFORMING for <registered prefix>/<slug>; LEGACY-BRANCH for the old forms (agent/...,
    registry prefixes without a trailing slash, a registered prefix in the wrong case, and a seat
    spelled another way such as mm/ or antigravity/); NON-CONFORMING for the rest.  main and
    master give DEFAULT_BRANCH, and no branch or HEAD gives DETACHED."""
    if branch is None or not branch.strip():
        return BranchVerdict.DETACHED
    b = branch.strip()
    if b.startswith("refs/heads/"):
        b = b[len("refs/heads/"):]
    if b == "HEAD":
        return BranchVerdict.DETACHED
    if b in ("main", "master"):
        return BranchVerdict.DEFAULT_BRANCH
    current, legacy = _branch_prefixes(registry)
    for bp in current:
        if b.startswith(bp) and len(b) > len(bp):
            return BranchVerdict.CONFORMING
    if b.startswith("agent/") or any(b.startswith(bp) for bp in legacy):
        return BranchVerdict.LEGACY_BRANCH
    for bp in current:
        if b.lower().startswith(bp.lower()) and len(b) > len(bp):
            return BranchVerdict.LEGACY_BRANCH
    head, _, tail = b.partition("/")
    if tail and is_known_seat(head, registry):
        return BranchVerdict.LEGACY_BRANCH
    return BranchVerdict.NON_CONFORMING


def seat_from_branch(branch: str | None, registry: Registry) -> str | None:
    """Canonical seat token a branch name points at, or None.  Covers conforming and legacy forms
    (ag/x and agent/antigravity-x both give antigravity)."""
    if not branch:
        return None
    b = branch.strip()
    if b.startswith("refs/heads/"):
        b = b[len("refs/heads/"):]
    for seat in registry.seats:
        for bp in seat.branch_prefixes:
            if b.startswith(bp) and len(b) > len(bp):
                return seat.suffix.lower()
    if b.startswith("agent/"):
        tail = b[len("agent/"):].split("/", 1)[0].split("-", 1)[0]
        return normalize_seat(tail, registry) if is_known_seat(tail, registry) else None
    head, _, tail = b.partition("/")
    return normalize_seat(head, registry) if tail and is_known_seat(head, registry) else None


def upgrade_with_branch(verdict: NameVerdict, branch: str | None, registry: Registry) -> NameVerdict:
    """Fold branch evidence into a name verdict.

    A name alone can say NON-LANE for a directory that is plainly a lane by its branch (a clone
    named HogHunter on grok/cpu-mach-timebase).  When the verdict is NON-LANE and the branch names
    a seat, this returns NAME-DRIFT.  Every other verdict is returned unchanged.
    """
    if verdict == NameVerdict.NON_LANE and seat_from_branch(branch, registry) is not None:
        return NameVerdict.NAME_DRIFT
    return verdict
