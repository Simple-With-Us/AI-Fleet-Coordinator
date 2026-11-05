#!/usr/bin/env python3
"""Hold every tracked wrangler config to scripts/worker-registry.json.

    python3 scripts/check-worker-guards.py [--root DIR] [--registry FILE]
                                           [--repo OWNER/NAME] [--plan paid|free]

A config fails when it lacks limits.cpu_ms or limits.subrequests, a value is above
the registry, preview_urls is not false, its log sampling is implicit or above the
registry, its name is not in the registry, or its path is not listed under that name.

Downgrade switch:  the registry's account_plan says which plan the account is on.
"free" accepts a config with no limits.cpu_ms, because the Free plan's own 10 ms
applies, and caps limits.subrequests at the registry's free_plan_subrequests.  --plan
overrides the file for a rehearsal.  docs/cloudflare-workers.md has the checklist.

Reads files only;  it never talks to Cloudflare.  Exit 0 clean, 1 on any finding.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG_NAMES = {"wrangler.toml", "wrangler.json", "wrangler.jsonc"}
STATUSES = {"auto", "gated", "frozen", "retire"}
PLANS = {"paid", "free"}
THIS_REPO = "Simple-With-Us/AI-Fleet-Coordinator"
ROOT = Path(__file__).resolve().parent.parent


def strip_jsonc(text: str) -> str:
    """Drop comments and trailing commas, leaving string contents alone."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c in "}]":
            while out and out[-1].isspace():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
            out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def load_config(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        return tomllib.loads(text)
    return json.loads(strip_jsonc(text))


def tracked_configs(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    return sorted(p for p in listing.split("\0") if Path(p).name in CONFIG_NAMES)


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_rate(value: object) -> bool:
    return is_number(value) and 0 <= value <= 1


def is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def check_registry(registry: dict) -> list[str]:
    """The registry's own shape.  A bad registry cannot vouch for a config."""
    findings: list[str] = []
    if registry.get("account_plan") not in PLANS:
        findings.append(f"registry: account_plan must be one of {sorted(PLANS)}")
    if not is_positive_int(registry.get("free_plan_subrequests")):
        findings.append("registry: free_plan_subrequests must be a positive integer")
    workers = registry.get("workers")
    if not isinstance(workers, dict) or not workers:
        return findings + ["registry: workers must be a non-empty object"]
    for name, row in workers.items():
        if not isinstance(row, dict):
            findings.append(f"registry: {name}: row must be an object")
            continue
        if row.get("status") not in STATUSES:
            findings.append(f"registry: {name}: status must be one of {sorted(STATUSES)}")
        for key in ("repo", "health", "deploy"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                findings.append(f"registry: {name}: {key} must be a non-empty string")
        limits = row.get("limits")
        if not isinstance(limits, dict) or not all(
            is_positive_int(limits.get(k)) for k in ("cpu_ms", "subrequests")
        ):
            findings.append(f"registry: {name}: limits needs positive cpu_ms and subrequests")
        if not is_rate(row.get("head_sampling_rate")):
            findings.append(f"registry: {name}: head_sampling_rate must be between 0 and 1")
        config = row.get("config")
        if config is None:
            if not isinstance(row.get("no_config_reason"), str) or not row["no_config_reason"].strip():
                findings.append(f"registry: {name}: a null config needs no_config_reason")
        elif not isinstance(config, str):
            findings.append(f"registry: {name}: config must be a path or null")
    return findings


def config_paths(row: dict) -> list[str]:
    paths = list(row.get("extra_configs") or [])
    if isinstance(row.get("config"), str):
        paths.insert(0, row["config"])
    return paths


def check_config(path: str, config: dict, registry: dict, plan: str) -> list[str]:
    name = config.get("name")
    row = (registry.get("workers") or {}).get(name)
    if not isinstance(row, dict):
        return [f"{path}: name {name!r} is not in the worker registry"]
    findings: list[str] = []
    if path not in config_paths(row):
        findings.append(f"{path}: not listed as a config of {name} in the worker registry")
    ceiling = row.get("limits") if isinstance(row.get("limits"), dict) else {}
    limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}

    allowed = {"cpu_ms": ceiling.get("cpu_ms"), "subrequests": ceiling.get("subrequests")}
    if plan == "free" and is_positive_int(registry.get("free_plan_subrequests")):
        cap = registry["free_plan_subrequests"]
        have = allowed["subrequests"]
        allowed["subrequests"] = min(have, cap) if is_positive_int(have) else cap
    for key, top in allowed.items():
        value = limits.get(key)
        if value is None:
            if key == "cpu_ms" and plan == "free":
                continue
            findings.append(f"{path}: limits.{key} is missing")
        elif not is_positive_int(value):
            findings.append(f"{path}: limits.{key} must be a positive integer")
        elif is_positive_int(top) and value > top:
            findings.append(f"{path}: limits.{key} {value} is above the allowed {top} on the {plan} plan")

    if config.get("preview_urls") is not False:
        findings.append(f"{path}: preview_urls must be false")

    observability = config.get("observability") if isinstance(config.get("observability"), dict) else {}
    logs = observability.get("logs") if isinstance(observability.get("logs"), dict) else {}
    rates = [v for v in (observability.get("head_sampling_rate"), logs.get("head_sampling_rate")) if v is not None]
    if not rates:
        findings.append(f"{path}: observability.head_sampling_rate is implicit")
    top_rate = row.get("head_sampling_rate")
    for rate in rates:
        if not is_rate(rate):
            findings.append(f"{path}: head_sampling_rate must be between 0 and 1")
        elif is_rate(top_rate) and rate > top_rate:
            findings.append(f"{path}: head_sampling_rate {rate} is above the registry's {top_rate}")
    return findings


def run(root: Path, registry_path: Path, repo: str, plan: str | None) -> list[str]:
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return [f"registry: cannot read {registry_path}: {err}"]
    if not isinstance(registry, dict):
        return [f"registry: {registry_path} must hold an object"]
    findings = check_registry(registry)
    active_plan = plan or registry.get("account_plan")
    try:
        tracked = tracked_configs(root)
    except (OSError, subprocess.SubprocessError) as err:
        return findings + [f"cannot list tracked files under {root}: {err}"]
    if not tracked:
        # Fail closed:  an empty listing would otherwise pass every rule above.
        return findings + [f"no tracked wrangler config under {root}"]

    for path in tracked:
        try:
            config = load_config(root / path)
        except (OSError, ValueError) as err:
            findings.append(f"{path}: cannot parse: {err}")
            continue
        findings.extend(check_config(path, config, registry, active_plan))

    workers = registry.get("workers") if isinstance(registry.get("workers"), dict) else {}
    for name, row in workers.items():
        if not isinstance(row, dict) or row.get("repo") != repo:
            continue
        for path in config_paths(row):
            if path not in tracked:
                findings.append(f"registry: {name}: config {path} is not a tracked wrangler config")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--repo", default=THIS_REPO)
    parser.add_argument("--plan", choices=sorted(PLANS))
    args = parser.parse_args(argv)
    registry_path = args.registry or args.root / "scripts" / "worker-registry.json"
    findings = run(args.root, registry_path, args.repo, args.plan)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} worker guard finding(s);  see docs/cloudflare-workers.md")
        return 1
    print("worker guards ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
