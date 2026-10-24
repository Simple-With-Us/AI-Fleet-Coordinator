#!/usr/bin/env python3
"""Project one verified Infisical boolean into the listener's required offline TOML.

Setup-only: no credentials, fetching, daemon signals, counter resets or backlog replay.
The caller supplies the current raw Infisical secret object on stdin after a scoped read.
Infisical remains authoritative; this derived projection survives restart/outage without
silently re-enabling an explicitly disabled guard. Run again only after owner approval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path

KEY = "AGENT_SYNC_WAKE_LOOP_PREVENTION"
SETTING = "wake_loop_prevention"


def project(text: str, value: bool) -> str:
    raw = tomllib.loads(text)
    if not isinstance(raw.get("daemon"), dict):
        raise ValueError("missing daemon section")
    match = re.search(r"(?m)^\[daemon\]\s*(?:#.*)?$", text)
    if not match:
        raise ValueError("daemon header must be explicit")
    start = match.end()
    tail = re.search(r"(?m)^\s*\[", text[start:])
    end = start + tail.start() if tail else len(text)
    section = text[start:end]
    line = SETTING + " = " + str(value).lower()
    setting = re.compile(r"(?m)^\s*" + SETTING + r"\s*=.*$")
    section = (
        setting.sub("\n" + line, section) if setting.search(section) else "\n" + line + section
    )
    new = text[:start] + section + text[end:]
    check = tomllib.loads(new)
    before = dict(raw["daemon"])
    after = dict(check["daemon"])
    before.pop(SETTING, None)
    after.pop(SETTING, None)
    if before != after or {k: v for k, v in raw.items() if k != "daemon"} != {
        k: v for k, v in check.items() if k != "daemon"
    }:
        raise ValueError("projection would alter unrelated configuration")
    return new


def verified_value(obj: dict, project_id: str, environment: str, secret_path: str) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("secret"), dict):
        raise ValueError("source must contain a secret object")
    secret = obj["secret"]
    required = ("secretKey", "workspace", "environment", "secretPath", "secretValue")
    if any(not isinstance(secret.get(field), str) for field in required):
        raise ValueError("source fields must be strings")
    if (
        secret.get("secretKey") != KEY
        or secret.get("workspace") != project_id
        or secret.get("environment") != environment
        or secret.get("secretPath") != secret_path
    ):
        raise ValueError("source identity/path mismatch")
    value = secret.get("secretValue")
    if value not in ("true", "false"):
        raise ValueError("authoritative value must be true or false")
    return value == "true"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--environment", default="prod")
    p.add_argument("--secret-path", default="/zulip")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    path = Path(args.config)
    if path.is_symlink() or not path.is_file():
        raise ValueError("config must be a regular non-symlink file")
    value = verified_value(
        json.load(sys.stdin), args.project_id, args.environment, args.secret_path
    )
    old = path.read_text()
    new = project(old, value)
    if args.apply and old != new:
        # Same-directory atomic rename, preserving the original permissions and config verbatim
        # except this single typed setting. No secret value is written to the config or audit.
        fd, tmp = tempfile.mkstemp(prefix=".loop-projection-", dir=path.parent)
        try:
            os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
            with os.fdopen(fd, "w") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            if path.read_text() != old:
                raise ValueError("config changed during projection")
            os.replace(tmp, path)
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    print(
        json.dumps(
            {
                "key": KEY,
                "source": "Infisical",
                "project_id": args.project_id,
                "environment": args.environment,
                "secret_path": args.secret_path,
                "setting": SETTING,
                "enabled": value,
                "changed": old != new,
                "applied": args.apply,
                "daemon_reloaded": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        # Do not print input, full config or provider errors.
        print("projection refused: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
