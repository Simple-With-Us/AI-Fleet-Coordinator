"""Load deploy/mac/manifest.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HelperPair:
    repo: str
    installed: str


@dataclass(frozen=True)
class LaunchAgentSpec:
    label: str
    plist_template: str
    helpers: tuple[HelperPair, ...] = ()


@dataclass(frozen=True)
class ExcludedLaunchAgent:
    label: str
    category: str
    reason: str


@dataclass(frozen=True)
class Pm2Spec:
    ecosystem_template: str
    ecosystem_installed: str
    app_names: tuple[str, ...]


@dataclass(frozen=True)
class MacManifest:
    schema_version: int
    issue: str
    launch_agents: tuple[LaunchAgentSpec, ...]
    excluded_launch_agents: tuple[ExcludedLaunchAgent, ...]
    excluded_notes: tuple[tuple[str, str], ...]
    pm2: Pm2Spec
    gaps: tuple[dict[str, str], ...]
    path: Path

    @property
    def excluded_labels(self) -> frozenset[str]:
        return frozenset(e.label for e in self.excluded_launch_agents)

    @property
    def desired_labels(self) -> frozenset[str]:
        return frozenset(a.label for a in self.launch_agents)


def default_manifest_path(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / "deploy" / "mac" / "manifest.toml"


def load_manifest(path: Path) -> MacManifest:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    agents: list[LaunchAgentSpec] = []
    for row in data.get("launch_agent") or []:
        helpers: list[HelperPair] = []
        for h in row.get("helpers") or []:
            if isinstance(h, dict) and h.get("repo") and h.get("installed"):
                helpers.append(HelperPair(str(h["repo"]), str(h["installed"])))
        agents.append(
            LaunchAgentSpec(
                label=str(row["label"]),
                plist_template=str(row["plist_template"]),
                helpers=tuple(helpers),
            )
        )
    excluded: list[ExcludedLaunchAgent] = []
    excl_root = data.get("excluded") if isinstance(data.get("excluded"), dict) else {}
    for row in excl_root.get("launch_agent") or []:
        excluded.append(
            ExcludedLaunchAgent(
                label=str(row["label"]),
                category=str(row.get("category", "")),
                reason=str(row.get("reason", "")),
            )
        )
    notes: list[tuple[str, str]] = []
    for row in excl_root.get("note") or []:
        notes.append((str(row.get("category", "")), str(row.get("reason", ""))))
    pm2_block = data.get("pm2") if isinstance(data.get("pm2"), dict) else {}
    app_names = tuple(
        str(a["name"])
        for a in (pm2_block.get("app") or [])
        if isinstance(a, dict) and a.get("name")
    )
    pm2 = Pm2Spec(
        ecosystem_template=str(pm2_block.get("ecosystem_template", "")),
        ecosystem_installed=str(pm2_block.get("ecosystem_installed", "")),
        app_names=app_names,
    )
    gaps = tuple(dict(g) for g in data.get("gap") or [])
    return MacManifest(
        schema_version=int(data.get("schema_version", 0)),
        issue=str(data.get("issue", "")),
        launch_agents=tuple(agents),
        excluded_launch_agents=tuple(excluded),
        excluded_notes=tuple(notes),
        pm2=pm2,
        gaps=gaps,
        path=path.resolve(),
    )
