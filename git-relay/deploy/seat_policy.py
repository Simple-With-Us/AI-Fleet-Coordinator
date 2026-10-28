#!/usr/bin/env python3
"""Render, install and check the reviewed git-relay seat policy (seat-policy.json).

    seat_policy.py render               print GIT_RELAY_SEATS_JSON as one compact line
    seat_policy.py install [--apply]    as root on the relay host:  diff the file against the
                                        running config;  with --apply, back the config up,
                                        write it, restart the relay and check its health
    seat_policy.py verify-apps          as root on the relay host:  every seat's own App
                                        mints a read-only token and reads one repository

The policy file holds no secret.  Every seat is `auth: shared`:  it signs in with its own
admin-minted ST-MCP bearer, which the relay checks through the Worker's verifier, and the
relay mints the seat's App token from the keys in Fleet-OPS /github-apps.

`install` loads the rendered policy with the DEPLOYED relay's own SeatPolicy before it
writes anything, so a policy the running code would refuse never reaches the unit, and it
puts the previous file back when the restarted relay does not come back healthy.

`verify-apps` runs the deployed relay's AppTokenMinter in a transient unit with the
relay's own credential binding (DynamicUser, LoadCredential of the apps token, the
unit's Infisical project), so it reads the App keys exactly as the service does.  For
each seat it mints a token with metadata and contents read for one repository, checks
that the key signs for the seat's own App (the minter checks the App id, this checks the
slug too) and reads that repository with it.  No token is printed, logged or kept.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "seat-policy.json"
DEFAULT_RELAY = "/opt/git-relay/git-relay"
DEFAULT_CONFIG = "/etc/git-relay/config.env"
DEFAULT_APPS_TOKEN = "/etc/git-relay/apps.infisical-token"
DEFAULT_UNIT = "git-relay.service"
DEFAULT_HEALTH = "http://127.0.0.1:8471/healthz"
SEATS_VAR = "GIT_RELAY_SEATS_JSON"
VERIFY_REPO = "Simple-With-Us/AI-Fleet-Coordinator"
# The unit environment the inner check needs, copied from the running unit (never a secret).
UNIT_ENV = ("PATH", "INFISICAL_FLEET_OPS_PROJECT_ID", "INFISICAL_ENVIRONMENT")


class PolicyFileError(ValueError):
    """The policy file is malformed;  the message names the field, never a value."""


def load(path: Path | str = DEFAULT_POLICY) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _profile(profiles: dict, name: str) -> dict:
    """A profile's three fields, with a field that names another profile taken from it."""
    if name not in profiles:
        raise PolicyFileError(f"unknown profile {name!r}")
    out = {}
    for field in ("actions", "mint_permissions", "repos"):
        value = profiles[name].get(field)
        if isinstance(value, str):
            source = profiles.get(value, {}).get(field)
            if value == name or not source or isinstance(source, str):
                raise PolicyFileError(f"profile {name!r}: {field} must name a concrete profile")
            value = source
        if not value:
            raise PolicyFileError(f"profile {name!r}: {field} is empty")
        out[field] = value
    return out


def render(doc: dict) -> dict:
    """The GIT_RELAY_SEATS_JSON object:  one shared-auth entry per seat, keyed by its App slug."""
    profiles = doc.get("profiles", {})
    seats: dict[str, dict] = {}
    identities: set[str] = set()
    for row in doc.get("seats", []):
        identity, slug, profile = row.get("identity"), row.get("app_slug"), row.get("profile")
        if not identity or not slug or not profile:
            raise PolicyFileError("every seat needs identity, app_slug and profile")
        if slug in seats:
            raise PolicyFileError(f"App {slug!r} is listed twice")
        if identity in identities:
            raise PolicyFileError(f"identity {identity!r} is listed twice")
        identities.add(identity)
        resolved = _profile(profiles, profile)
        seats[slug] = {
            "actions": list(resolved["actions"]),
            "app_slug": slug,
            "auth": "shared",
            "identity": identity,
            "mint_permissions": dict(resolved["mint_permissions"]),
            "repos": list(resolved["repos"]),
        }
    if not seats:
        raise PolicyFileError("the policy names no seat")
    return seats


def compact(seats: dict) -> str:
    """The one-line form config.env carries:  unquoted, compact separators, sorted keys."""
    return json.dumps(seats, separators=(",", ":"), sort_keys=True)


def validate_with(relay_dir: str, seats: dict) -> None:
    """Load the rendered policy with the relay code in relay_dir;  raises its PolicyError.

    Run it in a fresh process:  a git_relay already imported from elsewhere would be the
    one checked instead.
    """
    sys.path.insert(0, relay_dir)
    try:
        from git_relay.policy import SeatPolicy
    finally:
        sys.path.remove(relay_dir)

    class _NoVerifier:
        def parse_identity(self, token):
            return None

        def resolve(self, token):
            return None

    SeatPolicy(compact(seats), verifier=_NoVerifier())


def diff(old: dict, new: dict) -> list[str]:
    """Seat-level differences, with seat and field names only."""
    lines = []
    for slug in sorted(set(old) | set(new)):
        if slug not in old:
            lines.append(f"added    {slug} ({new[slug]['identity']})")
            continue
        if slug not in new:
            lines.append(f"removed  {slug} ({old[slug].get('identity')})")
            continue
        changed = []
        for field in sorted(set(old[slug]) | set(new[slug])):
            a, b = old[slug].get(field), new[slug].get(field)
            if field in ("actions", "repos"):
                plus = sorted(set(b or []) - set(a or []))
                minus = sorted(set(a or []) - set(b or []))
                if plus or minus:
                    changed.append(f"{field} +{plus} -{minus}")
            elif isinstance(a, dict) and isinstance(b, dict):
                keys = sorted(set(a) | set(b))
                moved = [f"{k}:{a.get(k)}->{b.get(k)}" for k in keys if a.get(k) != b.get(k)]
                if moved:
                    changed.append(f"{field} {', '.join(moved)}")
            elif a != b:
                changed.append(f"{field} {a!r} -> {b!r}")
        lines.append(f"changed  {slug}: {'; '.join(changed)}" if changed else f"same     {slug}")
    return lines


def _read_config(path: str) -> tuple[list[str], int, dict]:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    hits = [i for i, line in enumerate(lines) if line.startswith(SEATS_VAR + "=")]
    if len(hits) != 1:
        raise SystemExit(f"{path}: expected exactly one {SEATS_VAR} line, found {len(hits)}")
    current = json.loads(lines[hits[0]].split("=", 1)[1])
    if any("token" in cfg for cfg in current.values()):
        # A static-token seat cannot be expressed in a shared-only file;  never drop one silently.
        raise SystemExit(f"{path}: a running seat has a static token;  the file cannot replace it")
    return lines, hits[0], current


def _healthy(url: str, unit: str, wait_s: int) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(1)
        state = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        active = state.returncode == 0
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ok = resp.status == 200 and json.loads(resp.read(256)).get("ok") is True
        except Exception:
            ok = False
        if active and ok:
            return True
    return False


def _write_atomic(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".seat-policy.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def install(args) -> int:
    seats = render(load(args.policy))
    validate_with(args.relay, seats)
    lines, index, current = _read_config(args.config)
    report = diff(current, seats)
    print("\n".join(report))
    if any(line.startswith("removed") for line in report) and not args.allow_remove:
        print("refusing:  the file drops running seats (pass --allow-remove to mean it)")
        return 2
    if current == seats:
        print("the running policy already equals the file;  nothing to do")
        return 0
    if not args.apply:
        print("dry run;  pass --apply to install")
        return 0
    with open(args.config, encoding="utf-8") as fh:
        before = fh.read()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{args.config}.backup-seat-policy-{stamp}"
    _write_atomic(backup, before)
    lines[index] = f"{SEATS_VAR}={compact(seats)}"
    _write_atomic(args.config, "\n".join(lines) + "\n")
    subprocess.run(["systemctl", "restart", args.unit], check=True)
    if _healthy(args.health, args.unit, args.wait):
        print(f"installed {len(seats)} seats;  {args.unit} is active and healthy;  backup {backup}")
        return 0
    print(f"{args.unit} did not come back healthy;  restoring {backup}")
    _write_atomic(args.config, before)
    subprocess.run(["systemctl", "restart", args.unit], check=False)
    restored = _healthy(args.health, args.unit, args.wait)
    print("restored and healthy" if restored else "THE RESTORE IS NOT HEALTHY:  check the unit now")
    return 3


def _unit_env(unit: str) -> dict:
    out = subprocess.run(["systemctl", "show", "-p", "Environment", "--value", unit],
                         check=True, capture_output=True, text=True).stdout.split()
    env = dict(item.split("=", 1) for item in out if "=" in item)
    return {name: env[name] for name in UNIT_ENV if env.get(name)}


def verify_apps(args) -> int:
    """Copy this script and the rendered policy where a dynamic user can read them, then run
    the inner check in a transient unit that binds the relay's apps credential."""
    seats = render(load(args.policy))
    env = _unit_env(args.unit)
    missing = [name for name in UNIT_ENV if name not in env]
    if missing:
        print(f"{args.unit} sets no {', '.join(missing)};  cannot mirror its environment")
        return 2
    folder = tempfile.mkdtemp(prefix="seat-policy-verify-", dir="/run")
    try:
        os.chmod(folder, 0o755)
        script = os.path.join(folder, "seat_policy.py")
        policy = os.path.join(folder, "seats.json")
        shutil.copyfile(__file__, script)
        with open(policy, "w", encoding="utf-8") as fh:
            json.dump(seats, fh)
        os.chmod(script, 0o644)
        os.chmod(policy, 0o644)
        cmd = ["systemd-run", "--wait", "--pipe", "--collect", "--quiet",
               "-p", "DynamicUser=yes", "-p", "User=git-relay-verify", "-p", "PrivateTmp=yes",
               "-p", "ProtectHome=yes", "-p", "NoNewPrivileges=yes", "-p", "UMask=0077",
               "-p", f"LoadCredential=git-relay-apps-token:{args.apps_token}"]
        for name, value in env.items():
            cmd += ["-p", f"Environment={name}={value}"]
        cmd += ["/usr/bin/python3", "-I", script, "verify-inner",
                "--seats", policy, "--relay", args.relay]
        return subprocess.run(cmd, check=False).returncode
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def verify_inner(args) -> int:
    with open(args.seats, encoding="utf-8") as fh:
        seats = json.load(fh)
    sys.path.insert(0, args.relay)
    from git_relay.app_tokens import AppTokenMinter, FleetSecretsSource
    from git_relay.github_client import GitHubClient

    creds = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not creds:
        print("no CREDENTIALS_DIRECTORY:  run verify-apps, which binds the relay's apps credential")
        return 2
    # Every call below passes its own bearer, so this placeholder is never sent.
    client = GitHubClient("unused-placeholder")
    seen: dict[str, object] = {}

    def api(method, path, bearer, payload=None):
        out = client._call(method, path, payload, bearer=bearer)
        if method == "GET" and path == "/app" and isinstance(out, dict):
            seen["slug"] = out.get("slug")
        return out

    apps_token = os.path.join(creds, "git-relay-apps-token")
    source = FleetSecretsSource(path="/github-apps", credential_file=apps_token)
    minter = AppTokenMinter(source, api)
    read_only = {"metadata": "read", "contents": "read"}
    failures = 0
    for slug, cfg in seats.items():
        seen.clear()
        who = f"{cfg['identity']:<15}"
        try:
            token, _expiry = minter.token_for(slug, VERIFY_REPO, read_only)
            repo = client._call("GET", f"/repos/{VERIFY_REPO}", None, bearer=token)
            del token
            name = repo.get("full_name") if isinstance(repo, dict) else None
            ok = seen.get("slug") == slug and name == VERIFY_REPO
            print(f"{'ok  ' if ok else 'FAIL'} {who} App {seen.get('slug')}  read {name}")
        except Exception as exc:  # one seat's failure must not hide the others'
            ok = False
            print(f"FAIL {who} App {slug}:  {type(exc).__name__}: {str(exc)[:160]}")
        failures += 0 if ok else 1
    print(f"{len(seats) - failures} of {len(seats)} seats mint and read as their own App")
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["render", "install", "verify-apps", "verify-inner"])
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--relay", default=DEFAULT_RELAY, help="the deployed relay checkout")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--apps-token", default=DEFAULT_APPS_TOKEN)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--health", default=DEFAULT_HEALTH)
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for health")
    parser.add_argument("--seats", help="verify-inner only:  the rendered policy")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-remove", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "render":
        print(compact(render(load(args.policy))))
        return 0
    if args.command == "install":
        return install(args)
    if args.command == "verify-apps":
        return verify_apps(args)
    return verify_inner(args)


if __name__ == "__main__":
    sys.exit(main())
