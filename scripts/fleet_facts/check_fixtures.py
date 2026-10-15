#!/usr/bin/env python3
"""CI check:  the fleet_facts test data is synthetic.

    python3 scripts/fleet_facts/check_fixtures.py [--root DIR]

Scans every text file under tests/ (the fixtures and the test modules, this folder's own test included) and
fails if one contains
  * a real path:  /Users/ or /Volumes/
  * a real Linear identifier:  a team key from scripts/linear-board/board TEAM_KEYS followed by a number
    (the tests use the team key FFX, which is not a team)
  * a UUID that is not synthetic:  the only allowed shape is 00000000-0000-4000-8000-<12 hex digits>, so a
    real Linear issue id, bot id or user id cannot slip in
  * an identity that belongs to the owner or the realm (a handful of fixed words)
  * anything the F3 secret scanner calls secret-shaped

Findings print as `file:line:rule`, never the text that matched.  Exit 0 clean, 1 findings, 2 usage.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FALLBACK_TEAM_KEYS = ("ST", "CT", "UM", "CTS", "DD", "AFC", "PS", "AR", "CL", "BF", "HH", "OPS", "CLUTCH", "FL", "CC", "SWU")
SYNTHETIC_UUID = re.compile(r"^00000000-0000-4000-8000-[0-9a-f]{12}$")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
BANNED_WORDS = ("jaywedgeworth", "wedgeworth", "jays.services", "simplewithus", "zulipchat")
PATHS = ("/Users/", "/Volumes/")


def real_team_keys() -> tuple[str, ...]:
    board = HERE.parent / "linear-board" / "board"
    try:
        match = re.search(r"^TEAM_KEYS\s*=\s*\(([^)]*)\)", board.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        match = None
    if match:
        keys = tuple(re.findall(r"\"([A-Z0-9]+)\"", match.group(1)))
        if keys:
            return keys
    return FALLBACK_TEAM_KEYS


def _scanner():
    path = HERE.parent / "agent_sync" / "secretscan.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_ff_check_secretscan", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.scan


def check_text(text: str, keys: tuple[str, ...], scan=None) -> list[tuple[int, str]]:
    """[(line number, rule)] for one file's text."""
    ident = re.compile(r"\b(?:%s)-\d+\b" % "|".join(re.escape(k) for k in keys))
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        lowered = line.lower()
        if any(p in line for p in PATHS):
            findings.append((number, "real path"))
        if ident.search(line):
            findings.append((number, "real Linear identifier"))
        if any(not SYNTHETIC_UUID.match(m.group(0).lower()) for m in UUID.finditer(line)):
            findings.append((number, "non-synthetic UUID"))
        if any(word in lowered for word in BANNED_WORDS):
            findings.append((number, "real identity"))
        if scan is not None and scan(line) is not None:
            findings.append((number, "secret-shaped text"))
    return findings


def check_tree(root: Path) -> list[tuple[str, int, str]]:
    keys = real_team_keys()
    scan = _scanner()
    findings: list[tuple[str, int, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, rule in check_text(text, keys, scan):
            findings.append((str(path.relative_to(root)), number, rule))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=str(HERE / "tests"), help="the folder to scan (default: this package's tests)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        sys.stderr.write("check_fixtures: %s is not a folder\n" % root)
        return 2
    findings = check_tree(root)
    for name, number, rule in findings:
        print("%s:%d:%s" % (name, number, rule))
    if findings:
        sys.stderr.write("check_fixtures: %d finding(s);  fixtures must be synthetic\n" % len(findings))
        return 1
    print("check_fixtures: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
