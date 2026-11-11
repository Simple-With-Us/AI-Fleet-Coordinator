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
import fcntl
import hashlib
import json
import os
import pwd
import re
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
# The systemd credential input (policy_input.py):  the unit sets GIT_RELAY_SEATS_CREDENTIAL=seat-policy and
# carries LoadCredential=seat-policy:<backing file>.  A DynamicUser unit takes the policy this way.
SEATS_CREDENTIAL_VAR = "GIT_RELAY_SEATS_CREDENTIAL"
SEATS_CREDENTIAL = "seat-policy"
SECRETS_PATH_VAR = "GIT_RELAY_SECRETS_PATH"
VERIFY_REPO = "Simple-With-Us/AI-Fleet-Coordinator"
# The unit environment the inner check needs, copied from the running unit (never a secret).
UNIT_ENV = ("PATH", "INFISICAL_FLEET_OPS_PROJECT_ID", "INFISICAL_ENVIRONMENT")
# One EnvironmentFiles entry as `systemctl show` prints it:  "<path> (ignore_errors=no)".
ENV_FILE = re.compile(r"(\S+) \(ignore_errors=(?:yes|no)\)")
TOP_KEYS = {"//", "profiles", "seats"}
PROFILE_KEYS = {"//", "actions", "mint_permissions", "repos"}
SEAT_KEYS = {"identity", "app_slug", "profile", "oauth_assert"}


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
        # oauth_assert is a per-seat opt-in to the Worker's hosted-OAuth
        # assertion path (git_relay/assertion.py):  present it must be true,
        # and it renders only when true, so the deployed JSON names exactly
        # the seats that accept assertions.
        if "oauth_assert" in row:
            if row["oauth_assert"] is not True:
                raise PolicyFileError(f"seat {identity!r}: oauth_assert must be true when present")
            seats[slug]["oauth_assert"] = True
    if not seats:
        raise PolicyFileError("the policy names no seat")
    return seats


def compact(seats: dict) -> str:
    """The one-line form the EnvironmentFile carries:  unquoted, compact, sorted keys."""
    return json.dumps(seats, separators=(",", ":"), sort_keys=True)


def digest(seats: dict) -> str:
    """The short digest `install --apply --expect` checks:  the rendered line, not the file."""
    return hashlib.sha256(compact(seats).encode()).hexdigest()[:16]


def canonical_digest(policy: dict) -> str:
    """The full canonical digest the additive transaction checks (seat_add_policy.digest).

    Unlike digest() (the env installer's short line digest), this commits to the
    whole rendered policy object:  it is the CAS value on the live seats file and
    the receipt's transaction binding.
    """
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


SEATS_FILE_VAR = "GIT_RELAY_SEATS_FILE"
PROOF_KEY_FILE_VAR = "GIT_RELAY_PROOF_KEY_FILE"
# server.py's default audit path, relative to the unit's WorkingDirectory.
DEFAULT_AUDIT = "git-relay-audit.jsonl"
# The durable accepted-proof store (deploy_proof.ReplayStore), same directory.
REPLAY_STORE = "git-relay-proofs.json"


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
    user: str = ""
    dynamic_user: bool = False
    # The backing file of LoadCredential=seat-policy:<file>;  empty when the unit has none.
    seat_credential: str = ""

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
            "show",
            name,
            "-p",
            "WorkingDirectory",
            "-p",
            "EnvironmentFiles",
            "-p",
            "Environment",
            "-p",
            "User",
            "-p",
            "DynamicUser",
        )
    )
    cat = cat if cat is not None else _systemctl("cat", name)
    relay, files, env = "", [], {}
    user, dynamic = "", False
    for line in show.splitlines():
        key, _, value = line.partition("=")
        if key == "WorkingDirectory":
            relay = value.strip()
        elif key == "EnvironmentFiles" and value.strip():
            # One line per file or several space-joined on one line:  count every path.
            files.extend(ENV_FILE.findall(value))
        elif key == "Environment":
            env.update(item.split("=", 1) for item in value.split() if "=" in item)
        elif key == "User":
            user = value.strip()
        elif key == "DynamicUser":
            dynamic = value.strip() == "yes"
    tokens, policies = [], []
    for line in cat.splitlines():
        if line.strip().startswith("LoadCredential="):
            ident, _, source = line.strip().split("=", 1)[1].partition(":")
            if ident == APPS_CREDENTIAL and source:
                tokens.append(source)
            elif ident == SEATS_CREDENTIAL and source:
                policies.append(source)
    if len(policies) > 1:
        raise SystemExit(
            f"{name}:  more than one {SEATS_CREDENTIAL} LoadCredential;  refusing as ambiguous"
        )
    if not relay or len(files) != 1 or len(tokens) != 1:
        raise SystemExit(
            f"{name}:  expected a WorkingDirectory, one EnvironmentFile and one "
            f"{APPS_CREDENTIAL} LoadCredential;  found {bool(relay)}, {len(files)}, {len(tokens)}"
        )
    return Unit(
        name=name,
        relay=relay,
        config=files[0],
        apps_token=tokens[0],
        environment=env,
        user=user,
        dynamic_user=dynamic,
        seat_credential=policies[0] if policies else "",
    )


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


def _seats_file(unit: Unit) -> str:
    """The live seats file:  exactly one GIT_RELAY_SEATS_FILE line, an absolute path.

    The file input is mutually exclusive with the inline one (policy_input.py), so
    a unit that sets both is refused rather than silently migrated.  A duplicate
    line is refused as ambiguous:  the transaction must know exactly which setting
    the service reads, not guess a winner.
    """
    with open(unit.config, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    hits = [line for line in lines if line.startswith(SEATS_FILE_VAR + "=")]
    if len(hits) != 1:
        raise SystemExit(
            f"{unit.config}:  expected exactly one {SEATS_FILE_VAR} line, found {len(hits)}"
        )
    value = hits[0].split("=", 1)[1].strip().strip("'\"")
    if not os.path.isabs(value):
        raise SystemExit(f"{unit.config}:  {SEATS_FILE_VAR} must be an absolute path")
    if _config_value(lines, SEATS_VAR) is not None:
        raise SystemExit(f"{unit.config}:  {SEATS_VAR} and {SEATS_FILE_VAR} are mutually exclusive")
    return value


def _seat_policy_source(unit: Unit) -> tuple[str, bool]:
    """The live policy file and whether the service reads it through the systemd credential.

    A unit that sets GIT_RELAY_SEATS_CREDENTIAL=seat-policy reads its policy from a
    LoadCredential copy of one root-owned backing file, which is the only input a
    DynamicUser unit can use.  The transaction then replaces that backing file, and
    the restart makes systemd hand the new copy to the service.  The credential input
    is mutually exclusive with the file and inline inputs (policy_input.py), so a unit
    that sets any of them next to it is refused rather than guessed at, and so is a
    unit whose LoadCredential for the policy is missing or not an absolute path.
    Without the credential setting this is exactly the file input, unchanged.
    """
    with open(unit.config, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    hits = [line for line in lines if line.startswith(SEATS_CREDENTIAL_VAR + "=")]
    if not hits and SEATS_CREDENTIAL_VAR not in unit.environment:
        return _seats_file(unit), False
    if len(hits) > 1:
        raise SystemExit(
            f"{unit.config}:  expected exactly one {SEATS_CREDENTIAL_VAR} line, found {len(hits)}"
        )
    value = (
        hits[0].split("=", 1)[1].strip().strip("'\"")
        if hits
        else unit.environment.get(SEATS_CREDENTIAL_VAR, "")
    )
    if hits and SEATS_CREDENTIAL_VAR in unit.environment:
        raise SystemExit(
            f"{unit.config}:  {SEATS_CREDENTIAL_VAR} is set twice;  refusing as ambiguous"
        )
    if value != SEATS_CREDENTIAL:
        raise SystemExit(f"{unit.config}:  {SEATS_CREDENTIAL_VAR} must name {SEATS_CREDENTIAL}")
    if (
        _config_value(lines, SEATS_VAR) is not None
        or _config_value(lines, SEATS_FILE_VAR) is not None
        or SEATS_VAR in unit.environment
        or SEATS_FILE_VAR in unit.environment
    ):
        raise SystemExit(f"{unit.config}:  the seat policy inputs are mutually exclusive")
    if not unit.seat_credential or not os.path.isabs(unit.seat_credential):
        raise SystemExit(
            f"{unit.name}:  expected one absolute LoadCredential={SEATS_CREDENTIAL}:<file>;  "
            f"found {unit.seat_credential!r}"
        )
    return unit.seat_credential, True


def _credential_backing_refusal(path: str) -> str | None:
    """Why a credential backing file is unsafe to replace, or None.

    It is read by systemd as root and handed to a service uid that no host file can own,
    so it must be a plain private file nobody else can write or swap:  not a symlink,
    one link, owned by the user running this (root when run as root, as policy_input.py
    accepts), mode 0400 or 0600, in a directory nobody else can write.  A non-root
    operator is refused a root-owned file here, because it could neither read nor replace
    it."""
    try:
        info = os.lstat(path)
    except OSError:
        return "the credential backing file is unreadable"
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
    ):
        return "the credential backing file must be a private regular file (0400 or 0600, not a symlink)"
    try:
        parent = os.lstat(os.path.dirname(path))
    except OSError:
        return "the credential backing directory is unreadable"
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid not in (0, os.geteuid())
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        return "the credential backing directory must be private to its owner (no group or other write)"
    return None


def _audit_path(unit: Unit) -> str:
    """The relay's ACTUAL audit log:  its GIT_RELAY_AUDIT_PATH (Environment= or the
    EnvironmentFile), resolved like from_env does, else the default beside the code."""
    value = unit.environment.get("GIT_RELAY_AUDIT_PATH")
    if not value:
        with open(unit.config, encoding="utf-8") as fh:
            value = _config_value(fh.read().splitlines(), "GIT_RELAY_AUDIT_PATH")
    if not value:
        return os.path.join(unit.relay, DEFAULT_AUDIT)
    return value if os.path.isabs(value) else os.path.join(unit.relay, value)


TRANSACTION_LOCK_WAIT_S = 10


def _acquire_transaction_lock(seats_path: str):
    """Exclusive install transaction, on the adopt-lock pattern (server.py):

    flock(LOCK_EX|LOCK_NB) on a fixed sibling of the seats file, retried until the
    deadline;  the lock file is never unlinked, so there is no stale-inode race.
    The lock spans verify, receipt consume, write, health gate and any recovery:
    a second operator never interleaves a CAS, a store write or a replace.  None
    on timeout (caller refuses the transaction);  the fd releases on close.
    """
    lock_path = seats_path + ".transaction.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + TRANSACTION_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _service_uid(unit: Unit, credential: bool = False) -> int | None:
    """The uid the service reads the seats file as;  None when that cannot work.

    A DynamicUser gets a fresh uid per start, so no host file's ownership can
    match it:  that deployment must use the systemd credential input instead.
    On that input the service never reads the host file itself;  systemd reads
    it as root, so the file stays root's and the answer is 0."""
    if credential:
        return 0
    if unit.dynamic_user:
        return None
    if not unit.user or unit.user in ("root", "0"):
        return 0
    try:
        return pwd.getpwnam(unit.user).pw_uid
    except KeyError:
        return None


def _write_atomic_as(path: str, text: str, st) -> None:
    """_write_atomic that preserves the replaced file's owner, group and mode.

    A root 0600 replace under a non-root service would leave the unit unable to
    read its own policy at the next start;  the restore path needs the same
    preservation, so both go through here.  chown is only attempted as root."""
    fd, tmp = tempfile.mkstemp(prefix=".seat-policy.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.geteuid() == 0 and (st.st_uid, st.st_gid) != (0, 0):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _restore_file(unit: Unit, wait: int, before: str, path: str, backup: str, st) -> int:
    print(f"restoring {backup}")
    _write_atomic_as(path, before, st)
    subprocess.run(["systemctl", "restart", unit.name], check=False)
    restored = _healthy(unit.health, unit.name, wait)
    print("restored and healthy" if restored else "THE RESTORE IS NOT HEALTHY:  check the unit now")
    return 3


def _proof_key_dir_trust(path):
    """The key's directory chain must be untamperable by anyone but its operator.

    As root - where install-seat and proof-key run in production - the FULL
    ancestor chain is checked with the same rule require_root_owned applies
    to the policy file:  every ancestor directory root-owned, writable by
    root alone, never a symlink (a foreign-owned 0755 parent, or a symlinked
    one, lets another account substitute the key file).  Non-root is the
    dev/fixture path:  only the immediate directory is checked, since test
    trees legitimately live under per-user (and on macOS symlinked) roots.
    """
    parent = os.path.dirname(os.path.abspath(path)) or "/"
    if os.geteuid() == 0:
        try:
            require_root_owned([parent])
        except SystemExit as exc:
            raise ValueError(f"proof-key directory chain:  {exc}")
        return
    try:
        pst = os.stat(parent)
    except OSError:
        raise ValueError("proof-key directory unreadable")
    if pst.st_mode & 0o022:
        raise ValueError("proof-key directory must not be group/world-writable")


def _load_proof_key(path):
    """Read the operator proof key from a custody-checked file;  never logs it.

    The directory chain is checked first (_proof_key_dir_trust).  The file
    itself is opened O_NOFOLLOW | O_NONBLOCK (a FIFO must refuse, not hang)
    and verified by fstat on the OPENED descriptor - a regular file of at
    most 64 KiB, owned by the current euid (root-owned in production), 0600
    when running as root - so a symlink swap or a replace between check and
    open lands on the descriptor that is actually read, never a second
    lookup.  Any deviation refuses closed:  a writable or substituted key
    silently forges every receipt gate downstream of it.
    """
    _proof_key_dir_trust(path)
    try:
        # O_NONBLOCK with the O_NOFOLLOW:  a FIFO at the path must refuse,
        # never hang the install transaction on open.
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError:
        raise ValueError("proof-key file unreadable (missing, or a symlink)")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("proof-key file must be a regular file")
        if st.st_uid != os.geteuid():
            raise ValueError("proof-key file must be owned by its operator")
        if os.geteuid() == 0 and st.st_mode & 0o077:
            raise ValueError("proof-key file must be private (0600)")
        if st.st_size > 1 << 16:
            raise ValueError("proof-key file larger than 64 KiB is not a key")
        data = os.read(fd, 1 << 16)
    finally:
        os.close(fd)
    key = data.strip()
    if not key:
        raise ValueError("proof-key file empty")
    return key


def proof_key(args) -> int:
    """Mint the operator proof key into a root-owned 0600 file;  never prints it.

    Custody rule (a proposal requiring the host owner's approval to enact):
    one 0600 file owned by root on the relay host, named by
    GIT_RELAY_PROOF_KEY_FILE, in a root-owned directory that is not group- or
    world-writable.  Any offline backup - for example the Fleet-OPS
    /git-relay-owner folder - is a separate, explicitly authorized
    operational step, never part of this command.  A non-root caller is
    refused (the file must be root-owned), a symlinked path is refused, and
    an existing key is never overwritten:  rotating the key would orphan
    outstanding receipts.
    """
    path = os.environ.get(PROOF_KEY_FILE_VAR) or args.key_file
    if not path:
        print(f"refusing:  {PROOF_KEY_FILE_VAR} names no proof-key file")
        return 2
    if os.geteuid() != 0:
        print("refusing:  proof-key runs as root so the key file is root-owned 0600")
        return 2
    try:
        _proof_key_dir_trust(path)
    except ValueError as exc:
        print(f"refusing:  {exc}")
        return 2
    material = os.urandom(32).hex() + "\n"
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        print(f"refusing:  {path} exists;  never overwrite the proof key, receipts bind to it")
        return 2
    except OSError:
        print(f"refusing:  could not create {path} exclusively")
        return 2
    try:
        # One ownership rule:  fd stays this process's raw descriptor until a
        # successful os.fdopen hands it to the file object;  the except path
        # closes it only while it is still ours, and every failure path
        # unlinks the partial file this invocation created.
        st = os.fstat(fd)
        if st.st_uid != 0 or st.st_mode & 0o777 != 0o600:
            os.close(fd)
            fd = None
            try:
                os.unlink(path)
            except OSError:
                pass
            print(f"refusing:  {path} is not root-owned 0600 after creation")
            return 2
        fh = os.fdopen(fd, "w", encoding="utf-8")
        fd = None
        with fh:
            fh.write(material)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(path)
        except OSError:
            pass
        print(f"refusing:  could not write {path}")
        return 2
    print(f"proof key written to {path} (root-owned 0600;  the key is never printed)")
    return 0


def install_seat(args, unit: Unit) -> int:
    """Add exactly one seat to a file-backed policy under one exclusive transaction.

    The transaction lock (adopt-lock pattern) spans verify, receipt consume,
    write, health gate and any recovery:  two operators can never interleave a
    CAS, a replay-store write or a replace.  Every other gate runs before
    anything is written:  the service uid must be able to read the result, the
    live file must BE the reviewed policy (CAS on the full canonical digest),
    the additive result must match --expect-after, and the proof receipt must
    verify for exactly this seat and this before/after pair against the
    operator key, the durable replay store and the relay's ACTUAL audit log
    (its configured GIT_RELAY_AUDIT_PATH, not an assumed default).  The dry run
    checks the same gates without consuming the receipt;  --apply verifies it
    again and records it.  No policy is ever trimmed, reordered or regenerated
    here.
    """
    sys.path.insert(0, unit.relay)
    try:
        from git_relay import deploy_proof, policy_input, seat_add_policy
    finally:
        sys.path.remove(unit.relay)
    path, credential = _seat_policy_source(unit)
    if credential:
        # The relay accepts the credential input only under one systemd credential
        # directory, which systemd derives from the unit name.  A unit named otherwise
        # would pass every gate here and then fail the health gate at startup, so it is
        # refused before anything is written.
        if f"/run/credentials/{unit.name}" != policy_input.SEAT_CREDENTIAL_DIRECTORY:
            print(
                "refusing:  the unit's systemd credential directory is not the one the relay "
                "accepts for the policy credential"
            )
            return 2
        # Validate the backing file and its directory BEFORE the transaction lock creates
        # a file beside it as root.  The same check runs again under the lock.
        refusal = _credential_backing_refusal(path)
        if refusal:
            print(f"refusing:  {refusal}")
            return 2
    try:
        lock_fd = _acquire_transaction_lock(path)
    except OSError:
        print("refusing:  the transaction lock file cannot be opened safely;  nothing was written")
        return 2
    if lock_fd is None:
        print("refusing:  another install-seat transaction holds the lock;  retry when it finishes")
        return 2
    try:
        return _install_seat_locked(args, unit, path, deploy_proof, seat_add_policy, credential)
    finally:
        os.close(lock_fd)


def _install_seat_locked(
    args, unit: Unit, path: str, deploy_proof, seat_add_policy, credential: bool = False
) -> int:
    uid = _service_uid(unit, credential)
    if credential:
        refusal = _credential_backing_refusal(path)
        if refusal:
            print(f"refusing:  {refusal}")
            return 2
    if uid is None:
        print(
            "refusing:  the unit's User=/DynamicUser= cannot own a readable seats file;\n"
            "a DynamicUser deployment takes the systemd credential input instead"
        )
        return 2
    info = os.stat(path)
    if uid != 0 and info.st_uid != uid:
        print(
            "refusing:  the service user does not own the seats file;  a root replace would be unreadable"
        )
        return 2
    with open(path, encoding="utf-8") as fh:
        before_text = fh.read()
    try:
        current = json.loads(before_text)
    except ValueError:
        print("refusing:  the live policy is not valid JSON;  nothing was written")
        return 2
    if canonical_digest(current) != args.expect_before:
        print("refusing:  the live policy is not the reviewed one (--expect-before mismatch)")
        return 2
    with open(args.row_json, encoding="utf-8") as fh:
        row = json.load(fh)
    if not isinstance(row, dict):
        print("refusing:  the seat row file is not a JSON object")
        return 2
    try:
        result = seat_add_policy.prepare(current, args.seat, row, args.expect_before)
    except ValueError as exc:
        print(f"refusing:  {exc}")
        return 2
    after = canonical_digest(result)
    if after != args.expect_after:
        print("refusing:  --expect-after does not match the additive result;  re-render and review")
        return 2
    key_path = os.environ.get(PROOF_KEY_FILE_VAR)
    if not key_path:
        print(f"refusing:  {PROOF_KEY_FILE_VAR} names no proof-key file;  the proof gate stays on")
        return 2
    try:
        key = _load_proof_key(key_path)
    except ValueError as exc:
        print(f"refusing:  {exc}")
        return 2
    bootstrap = getattr(args, "bootstrap_receipt", None)
    with open(args.receipt or bootstrap, encoding="utf-8") as fh:
        receipt = json.load(fh)
    try:
        audit_rows = deploy_proof.read_audit_rows(_audit_path(unit))
        store = deploy_proof.ReplayStore(os.path.join(unit.relay, REPLAY_STORE))
        if bootstrap:
            # First-seat bootstrap:  the receipt must bind the relay's
            # verifier-confirmed identity to THIS row's configured identity,
            # so the proven-denied bearer is exactly the seat being added.
            # A row without an explicit identity cannot make that claim.
            claimed = row.get("identity")
            if not isinstance(claimed, str) or not claimed:
                print("refusing:  the seat row names no identity for a bootstrap receipt to bind")
                return 2
            deploy_proof.verify_bootstrap_receipt(
                key,
                receipt,
                seat=args.seat,
                identity=claimed,
                now=int(time.time()),
                store=store,
                policy_before=args.expect_before,
                policy_after=args.expect_after,
                audit_rows=audit_rows,
                store_id=args.apply,
            )
        else:
            deploy_proof.verify_receipt(
                key,
                receipt,
                seat=args.seat,
                now=int(time.time()),
                store=store,
                policy_before=args.expect_before,
                policy_after=args.expect_after,
                audit_rows=audit_rows,
                store_id=args.apply,
            )
    except ValueError as exc:
        print(f"refusing:  {exc}")
        return 2
    except OSError:
        print("refusing:  the audit log or replay store is unreadable;  refusing closed")
        return 2
    print(f"additive policy digest {after} (new seat:  {args.seat})")
    if not args.apply:
        print(
            "dry run;  install it with --apply and the same --expect-before/--expect-after/--receipt"
        )
        return 0
    _ = unit.health  # refuse before writing if health cannot be checked
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{path}.backup-seat-add-{stamp}"
    _write_atomic(backup, before_text)
    _write_atomic_as(path, json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", info)
    try:
        restart = subprocess.run(["systemctl", "restart", unit.name], check=False)
        if restart.returncode == 0 and _healthy(unit.health, unit.name, args.wait):
            print(f"installed seat {args.seat};  {unit.name} is healthy;  backup {backup}")
            return 0
        print(f"{unit.name} did not come back healthy (restart exit {restart.returncode})")
    except BaseException:
        print("interrupted before the relay was confirmed healthy")
        _restore_file(unit, args.wait, before_text, path, backup, info)
        raise
    return _restore_file(unit, args.wait, before_text, path, backup, info)


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
    parser.add_argument(
        "command",
        choices=["render", "install", "install-seat", "verify-apps", "verify-inner", "proof-key"],
    )
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument(
        "--unit", help="install, install-seat, verify-apps:  the relay's systemd unit"
    )
    parser.add_argument("--wait", type=int, default=30, help="seconds to wait for health")
    parser.add_argument("--expect", help="install --apply:  the digest the dry run printed")
    parser.add_argument("--seat", help="install-seat:  the one seat to add")
    parser.add_argument("--row-json", help="install-seat:  the reviewed rendered seat row")
    parser.add_argument(
        "--expect-before", help="install-seat:  full canonical digest of the live policy"
    )
    parser.add_argument(
        "--expect-after", help="install-seat:  full canonical digest of live-plus-one-seat"
    )
    parser.add_argument("--receipt", help="install-seat:  the signed external-proof receipt")
    parser.add_argument(
        "--bootstrap-receipt",
        help="install-seat:  the signed bootstrap-proof receipt (first seat for an identity)",
    )
    parser.add_argument("--key-file", help="proof-key:  key path when the env var is unset")
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
    if args.command == "proof-key":
        return proof_key(args)
    if not args.unit:
        parser.error(f"{args.command} needs --unit, the relay's systemd unit")
    unit = read_unit(args.unit)
    if os.geteuid() == 0:
        require_root_owned([__file__, args.policy, unit.relay])
    for path in (__file__, args.policy):
        print(f"{os.path.basename(path)} blob {blob_id(path)}")
    if args.command == "install":
        return install(args, unit)
    if args.command == "install-seat":
        for flag in ("seat", "row_json", "expect_before", "expect_after"):
            if not getattr(args, flag):
                parser.error(f"install-seat needs --{flag.replace('_', '-')}")
        if bool(args.receipt) == bool(args.bootstrap_receipt):
            parser.error("install-seat needs exactly one of --receipt or --bootstrap-receipt")
        if os.geteuid() == 0:
            require_root_owned([args.row_json, args.receipt or args.bootstrap_receipt])
        return install_seat(args, unit)
    return verify_apps(args, unit)


if __name__ == "__main__":
    sys.exit(main())
