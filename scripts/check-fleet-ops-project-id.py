#!/usr/bin/env python3
"""Fail if the live Fleet-OPS Infisical project UUID is committed to the repo.

The project id is private inventory (INFISICAL.md):  it is configured through
``INFISICAL_FLEET_OPS_PROJECT_ID`` and ``~/.simple-tools/fleet-ops-project-id``,
never in public source.  This lint compares the SHA-256 digest of every
RFC-4122 UUID-shaped token against a pinned digest, so the checker itself
never embeds the literal id.

Usage (from an AI-Fleet-Coordinator checkout; exit 0 clean, 1 findings):

    python3 scripts/check-fleet-ops-project-id.py
    python3 scripts/check-fleet-ops-project-id.py PATH [PATH...]
    python3 scripts/check-fleet-ops-project-id.py --digest <sha256-hex> PATH
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Default: SHA-256 of the Fleet-OPS Infisical project UUID (lowercase, no braces).  The literal is not stored here.
DEFAULT_BANNED_PROJECT_ID_SHA256 = "98523bf1fe1594f019e654a36f5fec6331bc9863270199b39c4c0977198e83c2"

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    "DerivedData",
    "Pods",
    ".build",
    "vendor",
}
MAX_BYTES = 2 * 1024 * 1024
SELF = Path(__file__).resolve()


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def digest_for_uuid(token: str) -> str:
    return hashlib.sha256(token.lower().encode()).hexdigest()


def scan_text(path: str, text: str, *, banned_digest: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in UUID_RE.finditer(line):
            token = match.group(0)
            if digest_for_uuid(token) == banned_digest:
                findings.append(
                    Finding(
                        path,
                        line_no,
                        "committed Fleet-OPS Infisical project id (use INFISICAL_FLEET_OPS_PROJECT_ID instead)",
                    )
                )
    return findings


def scan_file(path: Path, root: Path, *, banned_digest: str) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > MAX_BYTES:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return scan_text(rel, text, banned_digest=banned_digest)


def scan_tree(root: Path, *, banned_digest: str) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        findings.extend(scan_file(path, root, banned_digest=banned_digest))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--digest",
        default=DEFAULT_BANNED_PROJECT_ID_SHA256,
        metavar="SHA256",
        help="flag any RFC-4122 UUID whose lowercase SHA-256 matches this digest (default: Fleet-OPS prod project)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="directories to scan (default: the repo that contains this script)",
    )
    args = parser.parse_args(argv)
    digest = (args.digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        print("check-fleet-ops-project-id: --digest must be a 64-character hex SHA-256", file=sys.stderr)
        return 2
    roots = args.paths or [SELF.parent.parent]
    findings: list[Finding] = []
    for root in roots:
        root = root.resolve()
        if not root.is_dir():
            print(f"check-fleet-ops-project-id: not a directory: {root}", file=sys.stderr)
            return 2
        findings.extend(scan_tree(root, banned_digest=digest))
    if findings:
        for item in findings:
            print(item.render())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
