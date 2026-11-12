"""Load deploy/hetzner/manifest.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    UNIT = "unit"
    TIMER = "timer"
    CRON = "cron"
    DROP_IN = "drop-in"


class ManifestError(ValueError):
    """The manifest is not safe to reconcile."""


@dataclass(frozen=True)
class FileSpec:
    id: str
    kind: FileKind
    source: str
    installed: str
    inventory: str
    script_source: str = ""


@dataclass(frozen=True)
class ExcludedUnit:
    id: str
    category: str
    source: str
    reason: str


@dataclass(frozen=True)
class Gap:
    id: str
    kind: str
    inventory: str
    note: str


@dataclass(frozen=True)
class ForbidSpec:
    id: str
    glob: str
    inventory: str
    note: str


@dataclass(frozen=True)
class HetznerManifest:
    schema_version: int
    issue: str
    files: tuple[FileSpec, ...]
    excluded_units: tuple[ExcludedUnit, ...]
    excluded_notes: tuple[tuple[str, str], ...]
    gaps: tuple[Gap, ...]
    forbids: tuple[ForbidSpec, ...]
    path: Path

    @property
    def desired_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.files)

    @property
    def excluded_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.excluded_units)


def default_manifest_path(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / "deploy" / "hetzner" / "manifest.toml"


def _relative_path(value: str, label: str) -> str:
    text = value.strip()
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"{label} must be a relative path without '..': {value!r}")
    return text


def _kind(value: str) -> FileKind:
    try:
        return FileKind(value)
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in FileKind)
        raise ManifestError(f"unknown file kind {value!r}; expected {allowed}") from exc


def _require_id(row: dict, table: str) -> str:
    raw = row.get("id")
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(f"{table} row is missing id")
    return raw.strip()


def load_manifest(path: Path) -> HetznerManifest:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    files: list[FileSpec] = []
    seen_ids: set[str] = set()
    seen_installed: set[str] = set()
    seen_sources: set[str] = set()
    for row in data.get("file") or []:
        if not isinstance(row, dict):
            raise ManifestError("file row must be a table")
        item_id = _require_id(row, "file")
        installed = _relative_path(str(row.get("installed", "")), f"{item_id} installed")
        source = _relative_path(str(row.get("source", "")), f"{item_id} source")
        script_source = str(row.get("script_source", "") or "")
        if script_source:
            script_source = _relative_path(script_source, f"{item_id} script_source")
        if item_id in seen_ids:
            raise ManifestError(f"duplicate id {item_id}")
        if installed in seen_installed:
            raise ManifestError(f"duplicate installed path {installed}")
        if source in seen_sources:
            raise ManifestError(f"duplicate source {source}")
        seen_ids.add(item_id)
        seen_installed.add(installed)
        seen_sources.add(source)
        files.append(
            FileSpec(
                id=item_id,
                kind=_kind(str(row.get("kind", ""))),
                source=source,
                installed=installed,
                inventory=str(row.get("inventory", "")),
                script_source=script_source,
            )
        )
    excluded: list[ExcludedUnit] = []
    excl_root = data.get("excluded") if isinstance(data.get("excluded"), dict) else {}
    for row in excl_root.get("unit") or []:
        if not isinstance(row, dict):
            raise ManifestError("excluded unit row must be a table")
        item_id = _require_id(row, "excluded.unit")
        source = _relative_path(str(row.get("source", "")), f"excluded {item_id}")
        if item_id in seen_ids or source in seen_sources:
            raise ManifestError(f"excluded unit {item_id} overlaps desired state")
        seen_ids.add(item_id)
        excluded.append(
            ExcludedUnit(
                id=item_id,
                category=str(row.get("category", "")),
                source=source,
                reason=str(row.get("reason", "")),
            )
        )
    notes: list[tuple[str, str]] = []
    for row in excl_root.get("note") or []:
        if isinstance(row, dict):
            notes.append((str(row.get("category", "")), str(row.get("reason", ""))))
    gaps: list[Gap] = []
    for row in data.get("gap") or []:
        if not isinstance(row, dict):
            raise ManifestError("gap row must be a table")
        gaps.append(
            Gap(
                id=_require_id(row, "gap"),
                kind=str(row.get("kind", "")),
                inventory=str(row.get("inventory", "")),
                note=str(row.get("note", "")),
            )
        )
    forbids: list[ForbidSpec] = []
    for row in data.get("forbid") or []:
        if not isinstance(row, dict):
            raise ManifestError("forbid row must be a table")
        item_id = _require_id(row, "forbid")
        pattern = _relative_path(str(row.get("glob", "")), f"{item_id} glob")
        if "**" in pattern:
            raise ManifestError(f"{item_id} glob must not be recursive")
        forbids.append(
            ForbidSpec(
                id=item_id,
                glob=pattern,
                inventory=str(row.get("inventory", "")),
                note=str(row.get("note", "")),
            )
        )
    return HetznerManifest(
        schema_version=int(data.get("schema_version", 0)),
        issue=str(data.get("issue", "")),
        files=tuple(files),
        excluded_units=tuple(excluded),
        excluded_notes=tuple(notes),
        gaps=tuple(gaps),
        forbids=tuple(forbids),
        path=path.resolve(),
    )
