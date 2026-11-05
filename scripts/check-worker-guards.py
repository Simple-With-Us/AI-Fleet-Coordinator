#!/usr/bin/env python3
"""Hold every tracked wrangler config to scripts/worker-registry.json.

    python3 scripts/check-worker-guards.py [--root DIR] [--registry FILE]
                                           [--repo OWNER/NAME] [--plan paid|free]

A config fails when it lacks limits.cpu_ms or limits.subrequests, a value is above
the registry, preview_urls is not false, its log sampling is implicit or above the
registry, its name is not in the registry, or its path is not listed under that name.
Each [env.*] block is held to the same rules with what it inherits, because a deploy
with --env runs those values, and under the Worker name it deploys as.

Downgrade switch:  the registry's account_plan says which plan the account is on.
"free" accepts a config with no limits.cpu_ms, because the Free plan's own 10 ms
applies, and caps limits.subrequests at the registry's free_plan_subrequests.  --plan
overrides the file for a rehearsal.  docs/cloudflare-workers.md has the checklist.

Reads files only;  it never talks to Cloudflare.  Exit 0 clean, 1 on any finding,
124 when git ls-files times out, 127 when git is missing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Includes named variants such as wrangler.prod.toml, which `wrangler deploy -c` can use.
CONFIG_NAME = re.compile(r"wrangler(?:[.-].+)?\.(?:toml|jsonc?)")
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
    return sorted(p for p in listing.split("\0") if CONFIG_NAME.fullmatch(Path(p).name))


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
            if (
                not isinstance(row.get("no_config_reason"), str)
                or not row["no_config_reason"].strip()
            ):
                findings.append(f"registry: {name}: a null config needs no_config_reason")
        elif not isinstance(config, str):
            findings.append(f"registry: {name}: config must be a path or null")
        extra = row.get("extra_configs", [])
        if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
            findings.append(f"registry: {name}: extra_configs must be a list of paths")
    return findings


def config_paths(row: dict) -> list[str]:
    extra = row.get("extra_configs")
    paths = [p for p in extra if isinstance(p, str)] if isinstance(extra, list) else []
    if isinstance(row.get("config"), str):
        paths.insert(0, row["config"])
    return paths


def check_config(path: str, config: dict, registry: dict, plan: str) -> list[str]:
    findings = check_block(path, path, config, registry, plan)
    environments = config.get("env")
    if environments is None:
        return findings
    if not isinstance(environments, dict):
        return findings + [f"{path}: env must be a table of environments"]
    base = {k: v for k, v in config.items() if k != "env"}
    for env_name, block in environments.items():
        block = block if isinstance(block, dict) else {}
        # Wrangler deploys an environment as <name>-<env> unless the block names it.
        merged = {**base, **block, "name": block.get("name") or f"{base.get('name')}-{env_name}"}
        findings.extend(check_block(path, f"{path} [env.{env_name}]", merged, registry, plan))
    return findings


def check_block(path: str, label: str, config: dict, registry: dict, plan: str) -> list[str]:
    name = config.get("name")
    workers = registry.get("workers") if isinstance(registry.get("workers"), dict) else {}
    row = workers.get(name) if isinstance(name, str) else None
    if not isinstance(row, dict):
        return [f"{label}: name {name!r} is not in the worker registry"]
    findings: list[str] = []
    if path not in config_paths(row):
        findings.append(f"{label}: not listed as a config of {name} in the worker registry")
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
            findings.append(f"{label}: limits.{key} is missing")
        elif not is_positive_int(value):
            findings.append(f"{label}: limits.{key} must be a positive integer")
        elif is_positive_int(top) and value > top:
            findings.append(
                f"{label}: limits.{key} {value} is above the allowed {top} on the {plan} plan"
            )

    if config.get("preview_urls") is not False:
        findings.append(f"{label}: preview_urls must be false")

    observability = (
        config.get("observability") if isinstance(config.get("observability"), dict) else {}
    )
    logs = observability.get("logs") if isinstance(observability.get("logs"), dict) else {}
    rates = [
        v
        for v in (observability.get("head_sampling_rate"), logs.get("head_sampling_rate"))
        if v is not None
    ]
    if not rates:
        findings.append(f"{label}: observability.head_sampling_rate is implicit")
    top_rate = row.get("head_sampling_rate")
    for rate in rates:
        if not is_rate(rate):
            findings.append(f"{label}: head_sampling_rate must be between 0 and 1")
        elif is_rate(top_rate) and rate > top_rate:
            findings.append(
                f"{label}: head_sampling_rate {rate} is above the registry's {top_rate}"
            )
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
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise  # main() gives these their own exit codes.
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
        if not isinstance(config, dict):
            findings.append(f"{path}: cannot parse: the top level must be an object")
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
    try:
        findings = run(args.root, registry_path, args.repo, args.plan)
    except subprocess.TimeoutExpired:
        print("git ls-files timed out, so no config was checked")
        return 124
    except FileNotFoundError:
        print("git executable not found, so no config was checked")
        return 127
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} worker guard finding(s);  see docs/cloudflare-workers.md")
        return 1
    print("worker guards ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
