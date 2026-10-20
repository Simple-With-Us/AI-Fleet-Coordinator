"""Fixtures for the ``fleet_config`` tests:  a temp home,  a fake manifest and fake checks.

Nothing here touches the real home,  the real repo or a real installer.  Every test gets
its own temporary directory,  its own registry and manifest files,  and its own fake check
functions,  so a test can never write to Jay's Mac.

Stdlib only,  and Python 3.9 safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from collections.abc import Sequence

from fleet_config.checks import CheckContext, CheckRegistry, build_result
from fleet_config.types import (
    CheckResult,
    CheckSpec,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    Manifest,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
    SeatManifest,
)

__all__ = [
    "FleetTestCase",
    "make_manifest",
    "make_private_dir",
    "make_rule",
    "manifest_json",
    "registry_json",
    "registry_with",
    "result_check",
    "snapshot_tree",
]


def make_private_dir(path: str) -> str:
    """Create a directory the test owns,  with owner-only permissions.

    ``os.makedirs`` would use the permissive default mode bits,  and it follows a
    symlink:  a link that points somewhere else would be written through.  So the path
    is refused when it is a symlink,  and the mode is set explicitly afterwards,  which
    the umask cannot loosen.
    """
    if os.path.islink(path):
        raise ValueError("the directory %s is a symlink,  so it is not created" % path)
    if os.path.exists(path) and not os.path.isdir(path):
        raise ValueError("the path %s exists and is not a directory" % path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def make_rule(
    rule_id: str,
    kind: ConfigKind,
    platform: str = "minimax",
    *,
    seat: str = "MM",
    check_type: CheckType = CheckType.FILE_HASH,
    target: str = "~/.minimax/memory/user.md",
    detail: str = "",
    autofix: bool = True,
    proof: ProofLevel = ProofLevel.L0,
    owner: str = "MM",
    collector: Collector = Collector.MAC,
    ttl: int = 900,
    expected: ExpectedState | None = None,
) -> Rule:
    """One rule for a fake manifest."""
    return Rule(
        id=rule_id,
        kind=kind,
        platforms=(platform,),
        check=CheckSpec(type=check_type, target=target, detail=detail),
        expected=expected if expected is not None else ExpectedState(description="the rule holds"),
        proof_level=proof,
        owner_seat=owner,
        autofix_allowed=autofix,
        seat=seat,
        collector=collector,
        ttl_seconds=ttl,
        description="a fake rule",
    )


def make_manifest(
    rules: Sequence[Rule], *, seat: str = "MM", platforms: Sequence[str] = ("minimax",)
) -> Manifest:
    """A manifest with one seat and the given rules."""
    return Manifest(
        schema_version=1,
        seats=(SeatManifest(seat=seat, platforms=tuple(platforms), rules=tuple(rules)),),
        source_path="<fake manifest>",
    )


def manifest_json(manifest: Manifest) -> dict[str, object]:
    """The manifest as the JSON a loader reads."""
    seats: dict[str, object] = {}
    for entry in manifest.seats:
        rules: list[dict[str, object]] = []
        for rule in entry.rules:
            rules.append(
                {
                    "id": rule.id,
                    "kind": rule.kind.value,
                    "platforms": list(rule.platforms),
                    "check": {
                        "type": rule.check.type.value,
                        "target": rule.check.target,
                        "detail": rule.check.detail,
                    },
                    "expected": {
                        "description": rule.expected.description,
                        "present": rule.expected.present,
                        "sha256": rule.expected.sha256,
                        "names": list(rule.expected.names),
                        "mode": rule.expected.mode,
                        "timeoutSeconds": rule.expected.timeout_seconds,
                    },
                    "proofLevel": rule.proof_level.value,
                    "ownerSeat": rule.owner_seat,
                    "autofixAllowed": rule.autofix_allowed,
                    "collector": rule.collector.value,
                    "ttlSeconds": rule.ttl_seconds,
                    "description": rule.description,
                }
            )
        seats[entry.seat] = {
            "platforms": list(entry.platforms),
            "rules": rules,
            "notApplicable": [],
        }
    return {"schemaVersion": 1, "seats": seats}


def registry_json(tags: Sequence[str]) -> dict[str, object]:
    """A fleet registry holding exactly these live seat tags."""
    return {"seats": [{"tag": tag, "app": tag.lower()} for tag in tags]}


def result_check(
    state: CheckState,
    reason: str = "the fake check says so",
    reason_code: ReasonCode = ReasonCode.OK,
    observed: ObservedState | None = None,
    proof: ProofLevel | None = None,
):
    """A check function that always answers with this one result."""

    def check(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
        return build_result(
            rule,
            platform,
            state,
            reason,
            reason_code,
            observed if observed is not None else ObservedState(),
            ctx,
            proof_level_achieved=proof,
        )

    return check


def registry_with(checks: dict[CheckType, object]) -> CheckRegistry:
    """A registry holding exactly these check functions."""
    registry = CheckRegistry()
    for check_type, function in checks.items():
        registry.register(check_type)(function)
    return registry


def snapshot_tree(root: pathlib.Path) -> dict[str, tuple[int, int, str]]:
    """A snapshot of every file under `root`:  path to (mtime_ns, size, sha256).

    The mtime and the hash are both kept,  because a write that keeps the bytes and moves
    the mtime is still a write.
    """
    snapshot: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        data = path.read_bytes()
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (
            stat.st_mtime_ns,
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
    return snapshot


class FleetTestCase(unittest.TestCase):
    """A temporary home,  a temporary registry and manifest,  and no real installer."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.home = self.root / "home"
        make_private_dir(str(self.home))
        self.registry_path = self.root / "fleet-apps.json"
        self.manifest_path = self.root / "fleet-seats.json"

    def write_registry(self, tags: Sequence[str]) -> pathlib.Path:
        """Write a fake registry holding these seat tags."""
        self.registry_path.write_text(json.dumps(registry_json(tags), indent=1), encoding="utf-8")
        return self.registry_path

    def write_manifest(self, manifest: Manifest) -> pathlib.Path:
        """Write a fake manifest file."""
        self.manifest_path.write_text(
            json.dumps(manifest_json(manifest), indent=1), encoding="utf-8"
        )
        return self.manifest_path
