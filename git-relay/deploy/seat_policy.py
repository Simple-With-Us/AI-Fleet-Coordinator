#!/usr/bin/env python3
"""Render, install and check the reviewed git-relay seat policy (seat-policy.json).

    seat_policy.py render
        print GIT_RELAY_SEATS_JSON as one compact line
    seat_policy.py install --unit UNIT
        as root on the relay host:  validate the file with the deployed relay, show the
        diff against the running policy and the digest of the new one
    seat_policy.py install --unit UNIT --apply --expect DIGEST
        back the config up, write exactly that policy, restart the relay, check its health
    seat_policy.py verify-apps --unit UNIT
        as root on the relay host:  every seat's own App mints a read-only token and reads
        one repository

The policy file holds no secret.  Every seat is `auth: shared`:  it signs in with its own
admin-minted ST-MCP bearer, which the relay checks through the Worker's verifier, and the
relay mints the seat's App token from the App keys in Fleet-OPS.

Everything about the deployment is read from the relay's own systemd unit (UNIT):  its
working directory is the deployed relay code, its one EnvironmentFile holds the policy line,
its LoadCredential binds the apps token, and its environment names the health address and
the App-key folder.  No host path is written here.

As root it runs only from a root-owned copy that only root can write (README, "The reviewed
seat policy file"), and it prints each file's git blob id, so the copy can be matched to the
merged commit.  `install` loads the rendered policy with the DEPLOYED relay's own SeatPolicy
before it writes anything, so a policy the running code would refuse never reaches the unit.
`--apply` needs the digest the dry run printed, so it installs exactly the policy that was
shown, and it puts the previous file back when the restart fails, the relay does not come
back healthy, or the run is interrupted.

`verify-apps` runs the deployed relay's AppTokenMinter in a transient unit with the relay's
own credential binding (DynamicUser, the LoadCredential of the apps token, the unit's
Infisical project and the App-key folder the service reads), so it reads the App keys
exactly as the service does.  For each seat it mints a token with metadata and contents read
for one repository, checks that the key signs for the seat's own App (the minter checks the
App id, this checks the slug too) and reads that repository with it.  No token is printed,
logged or kept.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "seat-policy.json"
# The relay's own default for the App-key folder (server.py), used when nothing overrides it.
DEFAULT_SECRETS_PATH = "/github-apps"
APPS_CREDENTIAL = "git-relay-apps-token"
SEATS_VAR = "GIT_RELAY_SEATS_JSON"
SECRETS_PATH_VAR = "GIT_RELAY_SECRETS_PATH"
VERIFY_REPO = "Simple-With-Us/AI-Fleet-Coordinator"
# The unit environment the inner check needs, copied from the running unit (never a secret).
UNIT_ENV = ("PATH", "INFISICAL_FLEET_OPS_PROJECT_ID", "INFISICAL_ENVIRONMENT")
TOP_KEYS = {"//", "profiles", "seats"}
PROFILE_KEYS = {"//", "actions", "mint_permissions", "repos"}
SEAT_KEYS = {"identity", "app_slug", "profile"}


class PolicyFileError(ValueError):
    """The policy file is malformed;  the message names the field, never a value."""


def load(path: Path | str = DEFAULT_POLICY) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _unknown(where: str, keys, allowed: set) -> None:
    extra = sorted(set(keys) - allowed)
    if extra:
        raise PolicyFileError(f"{where}: unknown keys {extra}")


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
    """The GIT_RELAY_SEATS_JSON object:  one shared-auth entry per seat, keyed by its App slug.

    Every key is checked:  a field the renderer would not carry (a per-seat `repos` meant
    to narrow one seat, say) is refused rather than silently dropped.
    """
    _unknown("the file", doc, TOP_KEYS)
    profiles = doc.get("profiles", {})
    for name, profile in profiles.items():
        _unknown(f"profile {name!r}", profile, PROFILE_KEYS)
    seats: dict[str, dict] = {}
    identities: set[str] = set()
    for row in doc.get("seats", []):
        _unknown(f"seat {row.get('identity')!r}", row, SEAT_KEYS)
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
    """The one-line form the EnvironmentFile carries:  unquoted, compact, sorted keys."""
    return json.dumps(seats, separators=(",", ":"), sort_keys=True)


def digest(seats: dict) -> str:
    """The short digest `install --apply --expect` checks:  the rendered line, not the file."""
    return hashlib.sha256(compact(seats).encode()).hexdigest()[:16]


def blob_id(path: Path | str) -> str:
    """The file's git blob id, the value `git rev-parse <commit>:<path>` prints."""
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def require_root_owned(paths, lstat=os.lstat) -> None:
    """As root, refuse any file or folder on the way that is not root-owned, that a
    non-root account could write, or that is a symlink:  root must never run what
    another account can change."""
    for path in paths:
        current = Path(path).absolute()
        for part in [current, *current.parents]:
            st = lstat(part)
            if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
                raise SystemExit(
                    f"{part} is not root-owned and writable by root alone;  as root, run a "
                    "root-owned copy of the merged files (git-relay README, seat policy file)"
                )


@dataclass
class Unit:
    """What the relay's systemd unit says about its deployment."""

    name: str
    relay: str
    config: str
    apps_token: str
    environment: dict

    @property
    def health(self) -> str:
        """The relay's health URL, from the bind address the unit sets;  no assumed default."""
        host = self.environment.get("GIT_RELAY_HOST", "").strip()
        port = self.environment.get("GIT_RELAY_PORT", "").strip()
        if not host or not port:
            raise SystemExit(
                f"{self.name} sets no GIT_RELAY_HOST or GIT_RELAY_PORT;  no health URL"
            )
        return f"http://{host}:{port}/healthz"


def _systemctl(*args) -> str:
    return subprocess.run(["systemctl", *args], check=True, capture_output=True, text=True).stdout


def read_unit(name: str, show=None, cat=None) -> Unit:
    """The unit's working directory, its one EnvironmentFile, the apps credential it loads
    and its Environment=;  refuses when any of them is missing or ambiguous."""
    show = (
        show
        if show is not None
        else _systemctl(
            "show", name, "-p", "WorkingDirectory", "-p", "EnvironmentFiles", "-p", "Environment"
        )
    )
    cat = cat if cat is not None else _systemctl("cat", name)
    relay, files, env = "", [], {}
    for line in show.splitlines():
        key, _, value = line.partition("=")
        if key == "WorkingDirectory":
            relay = value.strip()
        elif key == "EnvironmentFiles" and value.strip():
            files.append(value.split(" (")[0].strip())
        elif key == "Environment":
            env.update(item.split("=", 1) for item in value.split() if "=" in item)
    tokens = []
    for line in cat.splitlines():
        if line.strip().startswith("LoadCredential="):
            ident, _, source = line.strip().split("=", 1)[1].partition(":")
            if ident == APPS_CREDENTIAL and source:
                tokens.append(source)
    if not relay or len(files) != 1 or len(tokens) != 1:
        raise SystemExit(
            f"{name}:  expected a WorkingDirectory, one EnvironmentFile and one "
            f"{APPS_CREDENTIAL} LoadCredential;  found {bool(relay)}, {len(files)}, {len(tokens)}"
        )
    return Unit(name=name, relay=relay, config=files[0], apps_token=tokens[0], environment=env)


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
    """Seat-level differences:  seat and field names, the actions and repositories added or
    removed, and the permission levels that moved.  The file holds no secret to print."""
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


def _config_value(lines: list[str], name: str) -> str | None:
    hits = [line.split("=", 1)[1] for line in lines if line.startswith(name + "=")]
    return hits[-1].strip().strip("'\"") if hits else None


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
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ok = resp.status == 200 and json.loads(resp.read(256)).get("ok") is True
        except Exception:
            ok = False
        if state.returncode == 0 and ok:
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


def _restore(unit: Unit, wait: int, before: str, backup: str) -> int:
    print(f"restoring {backup}")
    _write_atomic(unit.config, before)
    subprocess.run(["systemctl", "restart", unit.name], check=False)
    restored = _healthy(unit.health, unit.name, wait)
    print("restored and healthy" if restored else "THE RESTORE IS NOT HEALTHY:  check the unit now")
    return 3


def install(args, unit: Unit) -> int:
    seats = render(load(args.policy))
    validate_with(unit.relay, seats)
    _ = unit.health  # refuse now, before anything is written, if health cannot be checked
    lines, index, current = _read_config(unit.config)
    report = diff(current, seats)
    print("\n".join(report))
    print(f"policy digest {digest(seats)}")
    if any(line.startswith("removed") for line in report) and not args.allow_remove:
        print("refusing:  the file drops running seats (pass --allow-remove to mean it)")
        return 2
    if current == seats:
        print("the running policy already equals the file;  nothing to do")
        return 0
    if not args.apply:
        print(f"dry run;  install it with --apply --expect {digest(seats)}")
        return 0
    if args.expect != digest(seats):
        print("refusing:  --expect does not match this policy's digest;  run the dry run again")
        return 2
    with open(unit.config, encoding="utf-8") as fh:
        before = fh.read()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{unit.config}.backup-seat-policy-{stamp}"
    _write_atomic(backup, before)
    lines[index] = f"{SEATS_VAR}={compact(seats)}"
    _write_atomic(unit.config, "\n".join(lines) + "\n")
    try:
        restart = subprocess.run(["systemctl", "restart", unit.name], check=False)
        if restart.returncode == 0 and _healthy(unit.health, unit.name, args.wait):
            print(f"installed {len(seats)} seats;  {unit.name} is healthy;  backup {backup}")
            return 0
        print(f"{unit.name} did not come back healthy (restart exit {restart.returncode})")
    except BaseException:
        print("interrupted before the relay was confirmed healthy")
        _restore(unit, args.wait, before, backup)
        raise
    return _restore(unit, args.wait, before, backup)


def verify_apps(args, unit: Unit) -> int:
    """Copy this script and the rendered policy where a dynamic user can read them, then run
    the inner check in a transient unit that binds the relay's apps credential."""
    seats = render(load(args.policy))
    env = {name: unit.environment[name] for name in UNIT_ENV if unit.environment.get(name)}
    missing = [name for name in UNIT_ENV if name not in env]
    if missing:
        print(f"{unit.name} sets no {', '.join(missing)};  cannot mirror its environment")
        return 2
    # The App-key folder the service reads:  its EnvironmentFile wins over the unit's
    # Environment=, as it does for the service, and a set-but-empty value is an error.
    with open(unit.config, encoding="utf-8") as fh:
        secrets_path = _config_value(fh.read().splitlines(), SECRETS_PATH_VAR)
    if secrets_path is None:
        secrets_path = unit.environment.get(SECRETS_PATH_VAR, DEFAULT_SECRETS_PATH)
    if not secrets_path.strip():
        print(f"{SECRETS_PATH_VAR} is set but empty for {unit.name};  the service refuses that too")
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
        props = [
            "DynamicUser=yes",
            "User=git-relay-verify",
            "PrivateTmp=yes",
            "ProtectHome=yes",
            "NoNewPrivileges=yes",
            "UMask=0077",
            f"LoadCredential={APPS_CREDENTIAL}:{unit.apps_token}",
            *(f"Environment={name}={value}" for name, value in env.items()),
        ]
        cmd = ["systemd-run", "--wait", "--pipe", "--collect", "--quiet"]
        for prop in props:
            cmd += ["-p", prop]
        cmd += ["/usr/bin/python3", "-I", script, "verify-inner", "--seats", policy]
        cmd += ["--relay", unit.relay, "--secrets-path", secrets_path.strip()]
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

    apps_token = os.path.join(creds, APPS_CREDENTIAL)
    source = FleetSecretsSource(path=args.secrets_path, credential_file=apps_token)
    minter = AppTokenMinter(source, api)
    read_only = {"metadata": "read", "contents": "read"}
    print(f"App keys from {args.secrets_path}")
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
    parser.add_argument("--unit", help="install, verify-apps:  the relay's systemd unit")
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for health")
    parser.add_argument("--expect", help="install --apply:  the digest the dry run printed")
    parser.add_argument("--seats", help="verify-inner only:  the rendered policy")
    parser.add_argument("--relay", help="verify-inner only:  the deployed relay code")
    parser.add_argument("--secrets-path", default=DEFAULT_SECRETS_PATH, help="verify-inner only")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-remove", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "render":
        print(compact(render(load(args.policy))))
        return 0
    if args.command == "verify-inner":
        return verify_inner(args)
    if not args.unit:
        parser.error(f"{args.command} needs --unit, the relay's systemd unit")
    unit = read_unit(args.unit)
    if os.geteuid() == 0:
        require_root_owned([__file__, args.policy, unit.relay])
    for path in (__file__, args.policy):
        print(f"{os.path.basename(path)} blob {blob_id(path)}")
    if args.command == "install":
        return install(args, unit)
    return verify_apps(args, unit)


if __name__ == "__main__":
    sys.exit(main())
