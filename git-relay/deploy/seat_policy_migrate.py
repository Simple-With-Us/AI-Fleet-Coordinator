#!/usr/bin/env python3
"""Move the relay's seat policy from the file input onto the systemd credential input.

    seat_policy_migrate.py plan    --unit UNIT --backing FILE --journal-dir DIR --expect-digest D
        read only:  check every gate and print exactly what apply would change
    seat_policy_migrate.py apply   --unit UNIT --backing FILE --journal-dir DIR --expect-digest D
        journal first, then switch the unit, restart once, check, or put everything back
    seat_policy_migrate.py recover --unit UNIT --journal-dir DIR
        put the journaled before-images back and restart;  safe to run again, safe after a
        crash, a reboot or with the service stopped (it reads only the journal)

Source only until a separately released run.  It never adds, removes or edits a seat:  the
backing file is byte-identical to the policy the unit reads today, which is why --expect-digest
(the full canonical digest the reviewer saw) must match first.  Adding a seat is the separate,
proof-gated `seat_policy.py install-seat`, run after this.

What it changes, and nothing else:
  1. one new root-only backing file (--backing) holding the exact live policy bytes;
  2. the unit's one EnvironmentFile:  the single GIT_RELAY_SEATS_FILE=... line becomes
     GIT_RELAY_SEATS_CREDENTIAL=seat-policy, in the same place, every other line byte-preserved
     (EnvironmentFile= overrides Environment=, so it is the line itself that must change; a
     drop-in alone would leave both inputs set and the relay refuses that);
  3. one new drop-in with the single line LoadCredential=seat-policy:<backing>;  the unit's
     other LoadCredential lines (the apps token and any other) are untouched.
The old policy file is never modified or deleted, so it is itself a second before-image.

Order matters for crashes and reboots.  The backing file and the drop-in come FIRST:  an extra
credential next to the still-active file input is harmless (the relay's input selectors look
only at the GIT_RELAY_SEATS_* variables), so a restart or reboot at any point before the config
line changes still starts the relay on the old input.  The config line changes last;  after it
the unit has both halves, so a restart at that point also starts.  No journaled phase leaves a
unit whose next start fails.  Failure windows that remain are the restart itself and the health
and served-copy checks, and those restore.

Locking.  The migration holds, for the whole snapshot, edit, restart and check, the transaction
lock install-seat takes on the policy file (old path and backing path) plus its own journal
lock, so no install-seat writer can change the policy in between.  It also re-checks the old
policy file's digest before the restart and after the health gate and restores if it moved.

Before anything changes a root-only journal records, fsynced, the exact bytes and owner/mode of
the EnvironmentFile, the policy bytes, the drop-in state and the `systemctl cat` text, each
with its sha256, and the phase reached.  recover needs only that journal, which it checks field by field before it writes anything
(see _validate_journal for what that does and does not prove).  The journal is never deleted.  Not covered here:  the proof key and replay store the install-seat proof gate needs
are a separate prerequisite, and the reboot test is a separate released run on the real host.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("deploy_seat_policy", HERE / "seat_policy.py")
sp = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("deploy_seat_policy", sp)
_spec.loader.exec_module(sp)

JOURNAL = "migration-journal.json"
DROPIN_NAME = "seat-policy-credential.conf"
DROPIN_ROOT = "/etc/systemd/system"
CREDENTIALS_ROOT = "/run/credentials"
PHASES = (
    "prepared",
    "backing-written",
    "dropin-written",
    "config-edited",
    "restarted",
    "healthy",
    "restored",
)


class Refusal(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(
    path: str, data: bytes, mode: int, uid: int | None = None, gid: int | None = None
) -> None:
    """temp file in the target's directory, fsync, mode, owner, rename, fsync the directory."""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if uid is not None and os.geteuid() == 0:
            os.chown(tmp, uid, gid if gid is not None else -1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Host:
    """The only places this module touches the system, so tests can replace them."""

    def __init__(
        self,
        wait: int = 60,
        credentials_root: str = CREDENTIALS_ROOT,
        dropin_root: str = DROPIN_ROOT,
    ):
        self.wait = wait
        self.credentials_root = credentials_root
        self.dropin_root = dropin_root

    def systemctl(self, *args) -> int:
        return subprocess.run(["systemctl", *args], check=False).returncode

    def cat(self, unit: str) -> str:
        return sp._systemctl("cat", unit)

    def read_unit(self, unit: str) -> sp.Unit:
        return sp.read_unit(unit)

    def relay_credential_directory(self, unit: sp.Unit) -> str:
        """The one credential directory the relay accepts for the policy credential."""
        sys.path.insert(0, unit.relay)
        try:
            from git_relay import policy_input
        finally:
            sys.path.remove(unit.relay)
        return policy_input.SEAT_CREDENTIAL_DIRECTORY

    def healthy(self, unit: sp.Unit) -> bool:
        return sp._healthy(unit.health, unit.name, self.wait)


def _stat_info(path: str) -> dict:
    st = os.lstat(path)
    return {"uid": st.st_uid, "gid": st.st_gid, "mode": stat.S_IMODE(st.st_mode)}


def _trusted(uid: int) -> bool:
    return uid in (0, os.geteuid())


def _private_dir_refusal(path: str, label: str) -> str | None:
    try:
        st = os.lstat(path)
    except OSError:
        return f"{label} {path} is unreadable"
    if not stat.S_ISDIR(st.st_mode) or not _trusted(st.st_uid) or stat.S_IMODE(st.st_mode) & 0o077:
        return (
            f"{label} {path} must be a real directory, root-owned (or the running user), mode 0700"
        )
    return None


def _read_journal(journal_dir: str) -> dict | None:
    path = os.path.join(journal_dir, JOURNAL)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_journal(journal_dir: str, journal: dict) -> None:
    atomic_write(
        os.path.join(journal_dir, JOURNAL),
        (json.dumps(journal, sort_keys=True, indent=1) + "\n").encode(),
        0o600,
    )


def _config_edit(config_bytes: bytes) -> tuple[bytes, str]:
    """The EnvironmentFile with its one seats-file line swapped for the credential setting.

    Exactly one GIT_RELAY_SEATS_FILE= line, no inline or credential line already present;
    returns the new bytes and the absolute seats-file path the old line named."""
    text = config_bytes.decode("utf-8")
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(sp.SEATS_FILE_VAR + "=")]
    if len(hits) != 1:
        raise Refusal(f"expected exactly one {sp.SEATS_FILE_VAR} line, found {len(hits)}")
    for other in (sp.SEATS_VAR, sp.SEATS_CREDENTIAL_VAR):
        if any(line.startswith(other + "=") for line in lines):
            raise Refusal(
                f"the config already sets {other};  the policy inputs are mutually exclusive"
            )
    value = lines[hits[0]].split("=", 1)[1].strip().strip("'\"")
    if not os.path.isabs(value):
        raise Refusal(f"{sp.SEATS_FILE_VAR} must be an absolute path")
    lines[hits[0]] = f"{sp.SEATS_CREDENTIAL_VAR}={sp.SEATS_CREDENTIAL}"
    return "\n".join(lines).encode("utf-8"), value


def _dropin_path(host: Host, unit_name: str) -> str:
    return os.path.join(host.dropin_root, f"{unit_name}.d", DROPIN_NAME)


def _dropin_text(backing: str) -> bytes:
    return f"[Service]\nLoadCredential={sp.SEATS_CREDENTIAL}:{backing}\n".encode()


def _gates(host: Host, unit: sp.Unit, args) -> dict:
    """Every read-only check;  raises Refusal.  Returns what apply needs."""
    if os.geteuid() != 0 and not getattr(args, "allow_non_root", False):
        raise Refusal("run as root on the relay host")
    if unit.seat_credential:
        raise Refusal(
            f"{unit.name} already has a {sp.SEATS_CREDENTIAL} LoadCredential;  nothing to migrate"
        )
    if not os.path.isabs(args.backing):
        raise Refusal("--backing must be an absolute path")
    bad = _private_dir_refusal(args.journal_dir, "journal directory")
    if bad:
        raise Refusal(bad)
    backing_dir = os.path.dirname(args.backing)
    bad = _private_dir_refusal(backing_dir, "backing directory")
    if bad:
        raise Refusal(bad)
    if os.path.lexists(args.backing):
        raise Refusal(f"{args.backing} already exists;  refusing to overwrite or adopt it")
    with open(unit.config, "rb") as fh:
        config_bytes = fh.read()
    new_config, seats_path = _config_edit(config_bytes)
    # The state directory of a DynamicUser unit is not durable backup, and the backing file
    # must not sit where the service (or a path it can write) could replace it.
    if os.path.dirname(os.path.realpath(seats_path)) == os.path.realpath(backing_dir):
        raise Refusal("the backing directory must not be the directory of the current seats file")
    if os.path.realpath(backing_dir).startswith(os.path.realpath("/var/lib") + os.sep):
        raise Refusal(
            "the backing directory must not be under /var/lib (a service state directory)"
        )
    with open(seats_path, "rb") as fh:
        policy_bytes = fh.read()
    policy = json.loads(policy_bytes)
    if sp.canonical_digest(policy) != args.expect_digest:
        raise Refusal("the live policy is not the reviewed one (--expect-digest mismatch)")
    if any("token" in cfg for cfg in policy.values()):
        raise Refusal("a seat carries a static token;  the shared-only policy cannot hold it")
    sp.validate_with(unit.relay, policy)
    # systemd derives the credential directory from the unit name, and the relay accepts the
    # credential input only under one directory.  A unit named otherwise would pass every gate here
    # and then fail the relay's startup health check after two restarts, so refuse before writing.
    if f"{CREDENTIALS_ROOT}/{unit.name}" != host.relay_credential_directory(unit):
        raise Refusal(
            "the unit's systemd credential directory is not the one the relay accepts for the "
            "policy credential"
        )
    dropin = _dropin_path(host, unit.name)
    if os.path.lexists(dropin):
        raise Refusal(f"{dropin} already exists;  refusing to overwrite it")
    return {
        "config_bytes": config_bytes,
        "new_config": new_config,
        "seats_path": seats_path,
        "policy_bytes": policy_bytes,
        "dropin": dropin,
    }


def _effects(unit: sp.Unit, args, g: dict) -> list[str]:
    return [
        f"create   {args.backing}  root 0600, {len(g['policy_bytes'])} bytes, digest {args.expect_digest}",
        f"edit     {unit.config}:  {sp.SEATS_FILE_VAR}={g['seats_path']} -> {sp.SEATS_CREDENTIAL_VAR}={sp.SEATS_CREDENTIAL}",
        f"create   {g['dropin']}:  LoadCredential={sp.SEATS_CREDENTIAL}:{args.backing}",
        "restart  once (every seat is interrupted briefly), then health and served-copy digest check",
        f"keep     {g['seats_path']} untouched;  journal in {args.journal_dir}/{JOURNAL}",
    ]


def plan(args, host: Host | None = None) -> int:
    host = host or Host()
    unit = host.read_unit(args.unit)
    try:
        g = _gates(host, unit, args)
    except Refusal as exc:
        print(f"refusing:  {exc}")
        return 2
    print("\n".join(_effects(unit, args, g)))
    print("plan only;  nothing was written")
    return 0


def _served_copy_ok(host: Host, unit: sp.Unit, digest: str) -> bool:
    """The copy systemd handed the service (root can read it) holds the same policy."""
    path = os.path.join(host.credentials_root, unit.name, sp.SEATS_CREDENTIAL)
    try:
        with open(path, "rb") as fh:
            return sp.canonical_digest(json.loads(fh.read())) == digest
    except (OSError, ValueError):
        return False


def _restart_and_check(host: Host, unit: sp.Unit) -> bool:
    return host.systemctl("restart", unit.name) == 0 and host.healthy(unit)


def _take_lock(path: str):
    """sp._acquire_transaction_lock, with an unsafe lock path (a symlink, an unwritable
    directory) turned into a clean Refusal instead of an uncaught OSError.  Nothing has
    been written when it raises."""
    try:
        return sp._acquire_transaction_lock(path)
    except OSError:
        raise Refusal(
            f"the transaction lock file beside {path} cannot be opened safely;  nothing was written"
        ) from None


class _Locks:
    """The transaction locks, taken in a fixed order and released together.

    install-seat locks `<policy file>.transaction.lock`;  holding that lock for the old path
    and for the backing path (and our own journal lock) keeps every install-seat writer out
    for the whole migration.  None from acquire means one of them was busy."""

    def __init__(self, paths):
        self.paths = list(paths)
        self.fds = []

    def acquire(self) -> bool:
        for path in self.paths:
            try:
                fd = _take_lock(path)
            except Refusal:
                self.release()
                raise
            if fd is None:
                self.release()
                return False
            self.fds.append(fd)
        return True

    def release(self) -> None:
        while self.fds:
            os.close(self.fds.pop())


def _validate_journal(journal: dict, host: Host, unit_name: str) -> str | None:
    """Why a journal must not drive a restore, or None.

    The journal is a root-only file in a 0700 directory;  that is its only protection.  These
    checks catch corruption and a journal that belongs to another unit or host layout, not a
    forger who can already write as root.  Every field the restore writes or deletes is
    checked:  the config path must be the unit's own EnvironmentFile, the drop-in path the
    one this tool creates, the before-images must match their digests and be a real
    pre-migration shape, and modes and owners must be plain integers in range."""
    try:
        if journal.get("v") != 1 or journal.get("phase") not in PHASES + ("restore-unhealthy",):
            return "unknown journal version or phase"
        if journal.get("unit") != unit_name:
            return "the journal is for another unit"
        config, dropin, policy = journal["config"], journal["dropin"], journal["policy"]
        for entry in (config, policy):
            if sha(bytes.fromhex(entry["bytes_hex"])) != entry["sha256"]:
                return "a journal before-image fails its own digest"
        if sha(journal["unit_text"].encode()) != journal["unit_text_sha256"]:
            return "the journal's unit text fails its own digest"
        for key in ("uid", "gid", "mode"):
            if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] < 0:
                return f"journal config {key} is not a plain number"
        if config["mode"] & ~0o777:
            return "journal config mode has special bits"
        unit = host.read_unit(unit_name)
        if config["path"] != unit.config:
            return "the journal's config path is not the unit's EnvironmentFile"
        if dropin["path"] != _dropin_path(host, unit_name) or dropin["existed"] is not False:
            return "the journal's drop-in is not the one this tool creates"
        _edited, seats_path = _config_edit(bytes.fromhex(config["bytes_hex"]))
        if seats_path != policy["path"] or not os.path.isabs(journal["backing"]):
            return "the journal's policy or backing path does not match its config before-image"
    except (KeyError, TypeError, ValueError, SystemExit, Refusal) as exc:
        return f"the journal is malformed ({type(exc).__name__})"
    return None


def _restore_from(journal: dict, journal_dir: str, host: Host, unit_name: str) -> bool:
    """Put the journaled before-images back and restart.  Idempotent, needs only the journal.

    The config line goes back first, then the drop-in is removed, so no intermediate state
    leaves the credential setting without its LoadCredential."""
    config = journal["config"]
    atomic_write(
        config["path"],
        bytes.fromhex(config["bytes_hex"]),
        config["mode"],
        config["uid"],
        config["gid"],
    )
    dropin = journal["dropin"]
    if os.path.lexists(dropin["path"]):
        os.unlink(dropin["path"])
        _fsync_dir(os.path.dirname(dropin["path"]))
    host.systemctl("daemon-reload")
    host.systemctl("restart", unit_name)
    unit = host.read_unit(unit_name)
    ok = host.healthy(unit)
    journal["phase"] = "restored" if ok else "restore-unhealthy"
    _write_journal(journal_dir, journal)
    print("restored and healthy" if ok else "THE RESTORE IS NOT HEALTHY:  check the unit now")
    return ok


def _policy_unchanged(journal: dict) -> bool:
    try:
        with open(journal["policy"]["path"], "rb") as fh:
            return sha(fh.read()) == journal["policy"]["sha256"]
    except OSError:
        return False


def apply(args, host: Host | None = None, crash_after: str | None = None) -> int:
    """crash_after is a test hook:  raise right after the named phase is journaled."""
    host = host or Host()
    unit = host.read_unit(args.unit)
    try:
        journal_lock = _take_lock(os.path.join(args.journal_dir, "migrate"))
    except Refusal as exc:
        print(f"refusing:  {exc}")
        return 2
    if journal_lock is None:
        print("refusing:  another migration holds the journal lock")
        return 2
    locks = None
    try:
        try:
            with open(unit.config, "rb") as fh:
                _new, seats_path = _config_edit(fh.read())
        except (Refusal, OSError) as exc:
            print(f"refusing:  {exc}")
            return 2
        locks = _Locks([seats_path, args.backing])
        try:
            got = locks.acquire()
        except Refusal as exc:
            print(f"refusing:  {exc}")
            return 2
        if not got:
            print(
                "refusing:  an install-seat or migration transaction holds the policy lock;  retry when it finishes"
            )
            return 2
        existing = _read_journal(args.journal_dir)
        if existing is not None:
            if existing.get("phase") == "restored":
                print(
                    "refusing:  a journal from an earlier, restored attempt exists;  review it, "
                    "then move it aside (this tool never deletes a journal) before applying again"
                )
            else:
                print(
                    f"refusing:  a journal exists (phase {existing['phase']});  run recover first"
                )
            return 2
        try:
            g = _gates(host, unit, args)
        except Refusal as exc:
            print(f"refusing:  {exc}")
            return 2
        if g["seats_path"] != seats_path:
            print("refusing:  the config changed while taking the locks")
            return 2
        _ = unit.health
        cfg_info = _stat_info(unit.config)
        unit_text = host.cat(unit.name)
        journal = {
            "v": 1,
            "unit": unit.name,
            "phase": "prepared",
            "policy_digest": args.expect_digest,
            "backing": args.backing,
            "policy": {
                "path": g["seats_path"],
                "sha256": sha(g["policy_bytes"]),
                "bytes_hex": g["policy_bytes"].hex(),
            },
            "config": {
                "path": unit.config,
                "sha256": sha(g["config_bytes"]),
                "bytes_hex": g["config_bytes"].hex(),
                **cfg_info,
            },
            "dropin": {"path": g["dropin"], "existed": False},
            "unit_text_sha256": sha(unit_text.encode()),
            "unit_text": unit_text,
            "started_at": int(time.time()),
        }
        _write_journal(args.journal_dir, journal)

        def mark(phase: str) -> None:
            journal["phase"] = phase
            _write_journal(args.journal_dir, journal)
            if crash_after == phase:
                raise RuntimeError(f"injected crash after {phase}")

        try:
            # 1 and 2:  backing, then the drop-in.  Harmless while the file input is active.
            atomic_write(args.backing, g["policy_bytes"], 0o600, 0, 0)
            mark("backing-written")
            os.makedirs(os.path.dirname(g["dropin"]), exist_ok=True)
            atomic_write(g["dropin"], _dropin_text(args.backing), 0o644)
            host.systemctl("daemon-reload")
            mark("dropin-written")
            # 3:  the config line last;  now the unit has both halves.
            if not _policy_unchanged(journal):
                print("the live policy changed during the migration;  restoring")
                _restore_from(journal, args.journal_dir, host, unit.name)
                return 3
            atomic_write(
                unit.config,
                g["new_config"],
                cfg_info["mode"],
                cfg_info["uid"],
                cfg_info["gid"],
            )
            mark("config-edited")
            if not _restart_and_check(host, unit):
                print(f"{unit.name} did not come back healthy")
                _restore_from(journal, args.journal_dir, host, unit.name)
                return 3
            mark("restarted")
            loaded = host.read_unit(unit.name)
            if loaded.seat_credential != args.backing or not _served_copy_ok(
                host, unit, args.expect_digest
            ):
                print("the service does not serve the expected policy copy")
                _restore_from(journal, args.journal_dir, host, unit.name)
                return 3
            if not _policy_unchanged(journal):
                print("the live policy changed during the migration;  restoring")
                _restore_from(journal, args.journal_dir, host, unit.name)
                return 3
            mark("healthy")
        except BaseException:
            if journal["phase"] not in ("healthy", "restored"):
                print("interrupted before the relay was confirmed healthy;  restoring")
                _restore_from(journal, args.journal_dir, host, unit.name)
            raise
        print(
            f"migrated {unit.name} to the credential input;  policy digest {args.expect_digest};  journal {args.journal_dir}/{JOURNAL}"
        )
        return 0
    finally:
        if locks is not None:
            locks.release()
        os.close(journal_lock)


def recover(args, host: Host | None = None) -> int:
    host = host or Host()
    journal = _read_journal(args.journal_dir)
    if journal is None:
        print("refusing:  no journal in " + args.journal_dir)
        return 2
    bad = _validate_journal(journal, host, args.unit)
    if bad:
        print(f"refusing:  {bad}")
        return 2
    try:
        journal_lock = _take_lock(os.path.join(args.journal_dir, "migrate"))
    except Refusal as exc:
        print(f"refusing:  {exc}")
        return 2
    if journal_lock is None:
        print("refusing:  another transaction holds the lock")
        return 2
    locks = _Locks([journal["policy"]["path"], journal["backing"]])
    try:
        try:
            got = locks.acquire()
        except Refusal as exc:
            print(f"refusing:  {exc}")
            return 2
        if not got:
            print("refusing:  an install-seat or migration transaction holds the policy lock")
            return 2
        if journal["phase"] == "healthy":
            try:
                with open(journal["backing"], "rb") as fh:
                    unchanged = sha(fh.read()) == journal["policy"]["sha256"]
            except OSError:
                unchanged = False
            if not unchanged:
                print(
                    "refusing:  the backing policy changed after the migration (a later install-seat);  "
                    "rolling back would drop that change"
                )
                return 2
        return 0 if _restore_from(journal, args.journal_dir, host, args.unit) else 3
    finally:
        locks.release()
        os.close(journal_lock)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--unit", required=True)
        p.add_argument("--journal-dir", required=True)
        if name != "recover":
            p.add_argument("--backing", required=True)
            p.add_argument("--expect-digest", required=True)
        p.add_argument("--wait", type=int, default=60)
    args = ap.parse_args(argv)
    host = Host(wait=args.wait)
    return {"plan": plan, "apply": apply, "recover": recover}[args.cmd](args, host)


if __name__ == "__main__":
    sys.exit(main())
