"""Pure drift diff between the Hetzner manifest and a live root.

The live root is a directory the caller supplies.  Tests pass a temporary
directory.  Nothing here talks to a host, systemd, or cron.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from hetzner_gitops.manifest import FileSpec, ForbidSpec, HetznerManifest


class DriftKind(str, Enum):
    MISSING_LIVE = "missing_live"
    CONTENT_DRIFT = "content_drift"
    MISSING_SOURCE = "missing_source"
    LIVE_IS_DIRECTORY = "live_is_directory"
    PATH_ESCAPE = "path_escape"
    FORBIDDEN_PRESENT = "forbidden_present"


@dataclass(frozen=True)
class DriftItem:
    kind: DriftKind
    target: str
    detail: str


@dataclass
class DiffResult:
    drifts: list[DriftItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drifts

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "driftCount": len(self.drifts),
            "drifts": [
                {"kind": item.kind.value, "target": item.target, "detail": item.detail}
                for item in self.drifts
            ],
        }


@dataclass(frozen=True)
class LiveLayout:
    live_root: Path
    repo_root: Path


class PathEscape(ValueError):
    """A manifest path would leave the live root."""


def resolve_under(root: Path, relative: str) -> Path:
    """Return the absolute path of `relative` inside `root`, or raise."""
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise PathEscape(relative)
    root_real = root.resolve()
    dest = (root_real / rel).resolve()
    if dest != root_real and root_real not in dest.parents:
        raise PathEscape(relative)
    return dest


def diff_file(spec: FileSpec, layout: LiveLayout, out: list[DriftItem]) -> None:
    source = layout.repo_root / spec.source
    if not source.is_file():
        out.append(
            DriftItem(DriftKind.MISSING_SOURCE, spec.id, f"tracked source missing: {spec.source}")
        )
        return
    try:
        live = resolve_under(layout.live_root, spec.installed)
    except PathEscape:
        out.append(
            DriftItem(DriftKind.PATH_ESCAPE, spec.id, "installed path escapes the live root")
        )
        return
    if not live.exists():
        out.append(DriftItem(DriftKind.MISSING_LIVE, spec.id, f"absent: {spec.installed}"))
        return
    if live.is_dir():
        out.append(
            DriftItem(DriftKind.LIVE_IS_DIRECTORY, spec.id, f"live path is a directory: {spec.installed}")
        )
        return
    if source.read_bytes() != live.read_bytes():
        out.append(DriftItem(DriftKind.CONTENT_DRIFT, spec.id, "bytes differ"))


def diff_forbid(spec: ForbidSpec, layout: LiveLayout, out: list[DriftItem]) -> None:
    root = layout.live_root
    if not root.is_dir():
        return
    try:
        matches = sorted(path for path in root.glob(spec.glob) if path.is_file())
    except (OSError, ValueError):
        out.append(DriftItem(DriftKind.PATH_ESCAPE, spec.id, "forbid glob could not be read"))
        return
    for match in matches:
        try:
            match.resolve().relative_to(root.resolve())
        except ValueError:
            out.append(DriftItem(DriftKind.PATH_ESCAPE, spec.id, "forbid match escapes the live root"))
            continue
        rel = match.resolve().relative_to(root.resolve()).as_posix()
        out.append(
            DriftItem(
                DriftKind.FORBIDDEN_PRESENT,
                spec.id,
                f"present: {rel}",
            )
        )


def diff_manifest(manifest: HetznerManifest, layout: LiveLayout) -> DiffResult:
    drifts: list[DriftItem] = []
    for spec in manifest.files:
        diff_file(spec, layout, drifts)
    for spec in manifest.forbids:
        diff_forbid(spec, layout, drifts)
    return DiffResult(drifts=drifts)
