"""Pure drift diff between manifest desired state and a simulated or live Mac layout."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from mac_gitops.manifest import LaunchAgentSpec, MacManifest, Pm2Spec
from mac_gitops.plist_norm import plist_view, plist_views_equal, sha256_file


class DriftKind(str, Enum):
    PLIST_MISSING_INSTALLED = "plist_missing_installed"
    PLIST_DRIFT = "plist_drift"
    HELPER_DRIFT = "helper_drift"
    HELPER_MISSING_INSTALLED = "helper_missing_installed"
    PM2_ECOSYSTEM_DRIFT = "pm2_ecosystem_drift"
    PM2_APP_MISSING = "pm2_app_missing"
    PM2_APP_UNEXPECTED = "pm2_app_unexpected"
    EXCLUDED_PRESENT = "excluded_present"


@dataclass
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
                {"kind": d.kind.value, "target": d.target, "detail": d.detail} for d in self.drifts
            ],
        }


@dataclass
class MacLayout:
    """Filesystem and pm2 snapshot the reconciler compares against."""

    home: Path
    launch_agents_dir: Path
    repo_root: Path
    pm2_app_names: frozenset[str] | None = None
    pm2_ecosystem_installed: Path | None = None


def installed_plist_path(layout: MacLayout, label: str) -> Path:
    return layout.launch_agents_dir / f"{label}.plist"


def diff_launch_agent(
    spec: LaunchAgentSpec,
    layout: MacLayout,
    out: list[DriftItem],
) -> None:
    template = layout.repo_root / spec.plist_template
    installed = installed_plist_path(layout, spec.label)
    home = str(layout.home)
    want = plist_view(template, home) if template.is_file() else None
    have = plist_view(installed, home) if installed.is_file() else None
    if want is None:
        out.append(
            DriftItem(
                DriftKind.PLIST_DRIFT,
                spec.label,
                f"manifest template missing: {spec.plist_template}",
            )
        )
        return
    if have is None:
        out.append(
            DriftItem(DriftKind.PLIST_MISSING_INSTALLED, spec.label, "installed plist absent")
        )
    elif not plist_views_equal(want, have):
        out.append(
            DriftItem(
                DriftKind.PLIST_DRIFT,
                spec.label,
                "installed plist does not match tracked template",
            )
        )
    for helper in spec.helpers:
        repo_path = layout.repo_root / helper.repo
        live_path = layout.home / helper.installed
        if not live_path.is_file():
            out.append(
                DriftItem(
                    DriftKind.HELPER_MISSING_INSTALLED,
                    spec.label,
                    f"helper missing: {helper.installed}",
                )
            )
            continue
        repo_hash = sha256_file(repo_path)
        live_hash = sha256_file(live_path)
        if repo_hash is None:
            out.append(
                DriftItem(
                    DriftKind.HELPER_DRIFT,
                    spec.label,
                    f"helper template missing: {helper.repo}",
                )
            )
        elif live_hash != repo_hash:
            out.append(
                DriftItem(
                    DriftKind.HELPER_DRIFT,
                    spec.label,
                    f"helper bytes differ: {helper.installed}",
                )
            )


def diff_pm2(pm2: Pm2Spec, layout: MacLayout, out: list[DriftItem]) -> None:
    template = layout.repo_root / pm2.ecosystem_template
    installed = layout.pm2_ecosystem_installed or (layout.home / pm2.ecosystem_installed)
    t_hash = sha256_file(template)
    i_hash = sha256_file(installed) if installed.is_file() else None
    if t_hash is None:
        out.append(
            DriftItem(DriftKind.PM2_ECOSYSTEM_DRIFT, "pm2-ecosystem", "tracked ecosystem file missing")
        )
    elif i_hash is None:
        out.append(
            DriftItem(
                DriftKind.PM2_ECOSYSTEM_DRIFT,
                "pm2-ecosystem",
                f"installed ecosystem absent: {pm2.ecosystem_installed}",
            )
        )
    elif t_hash != i_hash:
        out.append(
            DriftItem(
                DriftKind.PM2_ECOSYSTEM_DRIFT,
                "pm2-ecosystem",
                "installed pm2 ecosystem differs from scripts/pm2-ecosystem.config.cjs",
            )
        )
    if layout.pm2_app_names is None:
        return
    desired = frozenset(pm2.app_names)
    live = layout.pm2_app_names
    for name in sorted(desired - live):
        out.append(DriftItem(DriftKind.PM2_APP_MISSING, name, "not listed in pm2 jlist"))
    for name in sorted(live - desired):
        if name.startswith("pm2-"):
            continue
        out.append(DriftItem(DriftKind.PM2_APP_UNEXPECTED, name, "pm2 app not in manifest"))


def diff_excluded(manifest: MacManifest, layout: MacLayout, out: list[DriftItem]) -> None:
    for label in manifest.excluded_labels:
        if installed_plist_path(layout, label).is_file():
            out.append(
                DriftItem(
                    DriftKind.EXCLUDED_PRESENT,
                    label,
                    "excluded label has an installed plist (informational for check-only)",
                )
            )


def diff_manifest(manifest: MacManifest, layout: MacLayout, *, report_excluded: bool = True) -> DiffResult:
    drifts: list[DriftItem] = []
    for spec in manifest.launch_agents:
        diff_launch_agent(spec, layout, drifts)
    diff_pm2(manifest.pm2, layout, drifts)
    if report_excluded:
        diff_excluded(manifest, layout, drifts)
    return DiffResult(drifts=drifts)


def parse_pm2_jlist(stdout: str) -> frozenset[str]:
    try:
        processes = json.loads(stdout)
    except json.JSONDecodeError:
        return frozenset()
    names: set[str] = set()
    if not isinstance(processes, list):
        return frozenset()
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        name = proc.get("name")
        if isinstance(name, str) and name:
            names.add(name)
            continue
        env = proc.get("pm2_env") or {}
        if isinstance(env, dict):
            n = env.get("name")
            if isinstance(n, str) and n:
                names.add(n)
    return frozenset(names)
