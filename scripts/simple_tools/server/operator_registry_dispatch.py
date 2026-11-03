#!/usr/bin/env python3
"""operator-registry-dispatch: the reviewed host-side registry route into the app container.

OWNERSHIP / INSTALL CONTRACT.  This command belongs to the registry route only.  The managed
wrappers `simple-chat` and `platform-letter` carry this module (the fleet-cli sync ships it)
and route EXACTLY the registry argv shapes to main_if_configured, passing every other command
through untouched - this dispatcher never replaces or intercepts unrelated commands.  Install:
no hand edit of a synced wrapper (the timer would revert it) and no separate binary;  the only
host-installed piece is the config at ~/.config/fleet-cli/operator-registry-dispatch.json
(0600, owned by the host user).  The host user is the
existing dispatch user (in the docker group); no root, no new credential:  the route needs no
Zulip or Infisical credential because registry ops are local DB work INSIDE the container.

WHAT IT DOES.  A Mac (or other client) hop lands on the registry host as the dispatch user
with argv `letter registry <op> ...` (the client's LETTER_REGISTRY_REMOTE ssh call).  This
dispatcher validates that shape, resolves the ONE managed app container (never a hardcoded
ephemeral id), validates it (running, expected image, the state volume mounted at /data), and
execs the registry verb inside it as the NUMERIC container uid 10001 - the only identity the
canonical store /data/operator/registry.sqlite3 permits.  argv passes through as a list (no
shell re-parse anywhere after ssh), stdin is closed (clients send none), stdout/stderr and the
exit code propagate unchanged, and a timeout kills the call.  Any failure prints one JSON line
{"error": ...} with a short reason token - never config values, never container internals -
and exits nonzero, so the client fails closed instead of falling back to a local store.

NO ENV OVERRIDE.  The deployed image's default state dir already resolves the registry to
/data/operator/registry.sqlite3, so this dispatcher sets no registry-path variable at all.
The deployed image predates the SIMPLE_TOOLS_* variable names;  do not shim the retired
prefix.  A future cutover image keeps the same default, which this route validates via the
/data mount check.

ROUTES (each fails closed on any other shape), bound to the REAL installed parsers:
  letter registry <register|title|touch|close> ...        ->  <container_cli> letter registry ...
      (Claude's parser has exactly these four ops - it has no "show" op)
  letter show ...                                         ->  <container_cli> letter show ...
  platform-letter registry <register|title|touch|close|show> --platform P ...
                                                          ->  python3 -m <platform_module> registry ...
      (the platform parser REQUIRES --platform and adds "show")
  platform-letter show --platform P                       ->  python3 -m <platform_module> show ...
  operator-registry <verb> ...                            ->  python3 -m <registry_module> <verb> ...
      the real module CLI's verbs:  register relabel resolve update close sweep touch
             history pending-outbox mark-exported wake-target
  Together these are the registry writer classes that route over the hop:  the letter hooks
  (client side), the management flows (alloc/sweep run INSIDE the close op of these same
  registry mains), and the sheets exporter's pending-outbox/mark-exported.  The daemon's own
  registration already runs inside the container (canonical by construction);  MM's transport
  is PR1255's separate route.  The module names are config fields, pinned per installed
  image:  the deployed image predates the simple_tools layout, so the host sets what the
  installed image actually provides (verified today only for `simple-chat letter registry`).

CONFIG (metadata only, no secrets;  every field required unless noted):
  {"schema": 1, "container_name_regex": "...",
   "image_id": "sha256:<the reviewed image id>",
   "volume_name": "<expected named volume>",
   "registry_module": "simple_tools.operator_registry",
   "platform_module": "simple_tools.platform_letter"}
Verification is MANDATORY and exact:  the one matched container must run the pinned image
id (tags are mutable and never trusted), must mount the NAMED volume (a bind mount is a
refusal) at /data, and the exec runs as the fixed numeric uid 10001 with the fixed CLI -
none of those is configurable, so a config cannot widen them.  Resolution fails closed when
zero or more than one running container matches.

TIME BUDGET vs THE CLIENT.  The client's whole ssh round trip times out at 8s, so the
dispatcher spends at most ~1.5s resolving, ~1.5s inspecting and 5s in the exec (worst case
just under the client budget).  A timeout is NOT cancellation:  docker exec may still
complete the registry write after the client is gone.  On timeout this prints
{"error", "outcome": "unknown", "retry": ...} - "safe" only for the read-only verbs and
the proven-idempotent writes (register/touch/close/mark-exported), "read-back-first" for
everything else (relabel --force double-applies;  unproven writes may):  reconcile those by
reading the registry (resolve/history/show) before deciding, never by blind retry, and
never by assuming failure or success.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

CONFIG_PARTS = (".config", "fleet-cli", "operator-registry-dispatch.json")
CONTAINER_UID = 10001  # the only identity the canonical store permits;  never configurable
CANONICAL_DB = "/data/operator/registry.sqlite3"
CONTAINER_CLI = "/usr/local/bin/simple-chat"
CONTAINER_WORKDIR = "/app/scripts"
MOUNT_DESTINATION = "/data"
RESOLVE_TIMEOUT = 1.5
EXEC_TIMEOUT = 5.0  # the client's ssh budget is 8s for the whole round trip
ALLOWED_REGISTRY_OPS = frozenset({"register", "title", "touch", "close"})
ALLOWED_PLATFORM_OPS = frozenset({"register", "title", "touch", "close", "show"})
ALLOWED_MODULE_VERBS = frozenset({
    "register", "relabel", "resolve", "update", "close", "sweep", "touch",
    "history", "pending-outbox", "mark-exported", "wake-target", "reopen", "held",
})
#: Verbs where an identical-argv retry after an uncertain outcome is PROVEN safe:
#: the read-only verbs, and the writes the independent review verified idempotent
#: (same-record/same-letter/no-second-effect).  Anything else - relabel --force increments
#: the generation again, an unproven write may double-apply - reconciles by READING the
#: registry first (resolve/history/show), never by blind retry.
READ_ONLY_VERBS = frozenset({"pending-outbox", "wake-target", "resolve", "history", "held"})
PROVEN_IDEMPOTENT_WRITES = frozenset({"register", "touch", "close", "mark-exported", "reopen"})
# reopen is proven idempotent by the registry's own contract:  an already-open session comes
# back with the same letter and nothing written, and a retry of a write that never landed
# performs the same reopen.  A refusal (space_lost, label_exhausted) repeats as the same refusal.
MAX_TIMEOUT = 60


def _fail(reason: str) -> int:
    sys.stdout.write(json.dumps({"error": "operator registry dispatch: " + reason}) + "\n")
    return 2


def _load_config(home: str) -> dict:
    """The dispatch config, custody-checked exactly like the notes reader's:  owner-only
    directories and a regular single-link 0600 file, no symlink followed, non-blocking."""
    fd = None
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            # Home custody we cannot trust still means "no config" when the config simply
            # is not there (lstat only - never an open, so no FIFO can block this check);
            # a config that EXISTS under unsafe custody refuses.
            probe = home
            missing = False
            for part in CONFIG_PARTS:
                probe = os.path.join(probe, part)
                if not os.path.lexists(probe):
                    missing = True
                    break
            if missing:
                raise FileNotFoundError
            raise ValueError("custody")
        for index, part in enumerate(CONFIG_PARTS):
            final = index == len(CONFIG_PARTS) - 1
            child = os.open(
                part,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if final else os.O_DIRECTORY),
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("custody")
            if final and (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16384
            ):
                raise ValueError("custody")
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ValueError("invalid")
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("schema") != 1:
            raise ValueError("invalid")
        name_re = str(config["container_name_regex"])
        re.compile(name_re)
        image_id = config["image_id"]
        volume = config["volume_name"]  # mandatory:  the named state volume must be checked
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or not isinstance(volume, str)
            or not volume
        ):
            raise ValueError("invalid")
        defaults = {
            "registry_module": "simple_tools.operator_registry",
            "platform_module": "simple_tools.platform_letter",
        }
        for field, default in defaults.items():
            module = str(config.get(field) or default)
            if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", module):
                raise ValueError("invalid")
            config[field] = module
        if "container_user" in config or "container_cli" in config or "container_workdir" in config:
            raise ValueError("invalid")  # fixed by the route;  a config must not widen them
        return config
    except (OSError, KeyError, TypeError, ValueError, re.error) as exc:
        raise ValueError("custody" if str(exc) == "custody" else "invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def _resolve_container(config: dict) -> str:
    """The one running managed app container's id, or ValueError(<reason>)."""
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
        capture_output=True, text=True, timeout=RESOLVE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise ValueError("docker-unavailable")
    name_re = re.compile(str(config["container_name_regex"]))
    matches = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and name_re.search(parts[1]):
            matches.append(parts[0])
    if not matches:
        raise ValueError("container-missing")
    if len(matches) > 1:
        raise ValueError("container-ambiguous")
    cid = matches[0]
    inspect = subprocess.run(
        ["docker", "inspect", "--format", "{{.Image}} {{json .Mounts}}", cid],
        capture_output=True, text=True, timeout=RESOLVE_TIMEOUT,
    )
    if inspect.returncode != 0:
        raise ValueError("docker-unavailable")
    image_id, _, raw_mounts = inspect.stdout.strip().partition(" ")
    if image_id != config["image_id"]:  # the reviewed image id exactly;  tags never trusted
        raise ValueError("image-mismatch")
    try:
        mounts = json.loads(raw_mounts)
    except ValueError:
        raise ValueError("docker-unavailable") from None
    ok = any(
        isinstance(m, dict)
        and m.get("Type") == "volume"  # a bind mount is not the canonical store
        and m.get("Destination") == MOUNT_DESTINATION
        and m.get("Name") == config["volume_name"]
        for m in (mounts or [])
    )
    if not ok:
        raise ValueError("volume-mismatch")
    return cid


def _flag_value(argv: list[str], name: str) -> str | None:
    for i, word in enumerate(argv):
        if word == name and i + 1 < len(argv):
            return argv[i + 1]
        if word.startswith(name + "="):
            return word[len(name) + 1:]
    return None


def _redirects_db(argv: list[str]) -> bool:
    """True when any argument could steer the command at another store.

    argparse accepts unambiguous prefixes and equals forms, so exact "--db" matching is not
    enough:  --d, --db, --db=PATH and a duplicated flag all redirect.  Only --db starts with
    "--d" among these CLIs' flags, so any "--d..." token argparse could resolve is refused.
    """
    for word in argv:
        if word.startswith("--") and "--db".startswith(word.split("=", 1)[0]) and len(word) >= 3:
            return True
    return False


def _container_command(config: dict, cid: str, argv: list[str]) -> list[str] | None:
    """The in-container argv for an allowed route, else None."""
    if _redirects_db(argv):
        return None  # the canonical store is pinned here;  no caller picks another
    if len(argv) >= 3 and argv[0] == "letter" and argv[1] == "registry" and argv[2] in ALLOWED_REGISTRY_OPS:
        return [CONTAINER_CLI] + argv
    if len(argv) >= 2 and argv[0] == "letter" and argv[1] == "show":
        return [CONTAINER_CLI] + argv
    if len(argv) >= 2 and argv[0] == "platform-letter" and argv[1] in ("registry", "show"):
        platform = _flag_value(argv[2:], "--platform") or ""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", platform):
            return None  # the real parser requires --platform;  refuse to guess it
        if argv[1] == "registry" and not (
            len(argv) >= 3 and argv[2] in ALLOWED_PLATFORM_OPS
        ):
            return None
        return ["python3", "-m", str(config["platform_module"])] + argv[1:]
    if len(argv) >= 2 and argv[0] == "operator-registry" and argv[1] in ALLOWED_MODULE_VERBS:
        # Every module verb takes --db (two require it):  the dispatcher pins the canonical
        # store itself rather than trusting the in-container default or any caller value.
        return [
            "python3", "-m", str(config["registry_module"]),
            argv[1], "--db", CANONICAL_DB,
        ] + argv[2:]
    return None


def main_if_configured(argv: list[str], home: str | None = None) -> int | None:
    """The managed-wrapper gate:  None when this host holds no dispatch config, so the caller's
    own handling continues untouched.  When a config exists the dispatcher owns the call:  a
    config that fails custody or validation, and an argv shape outside the registry routes, is
    a refusal from main - never a fall back to a local store.  A path whose existence cannot be
    decided is treated as present, so an unreadable home fails closed instead of silently
    writing a second store.
    """
    home = os.environ.get("HOME") or "" if home is None else home
    if not home:
        return None
    # lstat, not lexists:  lexists answers False for ANY OSError, so a config under an
    # unreadable directory would silently fall back to a local store.  Only a genuine
    # absence (FileNotFoundError) leaves the caller untouched;  a permission or other
    # error means "cannot prove absent", which fails closed into the dispatcher's own
    # refusal instead of writing a second store.
    try:
        os.lstat(os.path.join(home, *CONFIG_PARTS))
    except FileNotFoundError:
        return None
    except OSError:
        pass
    return main(argv, home=home)


def main_with_home(argv: list[str], home: str) -> int:
    """main with an explicit home (tests);  the process env's HOME is the default."""
    return main(argv, home=home)


def main(argv: list[str], home: str | None = None) -> int:
    home = os.environ.get("HOME") or "" if home is None else home
    try:
        config = _load_config(home)
    except ValueError as exc:
        return _fail("config-" + str(exc))
    try:
        cid = _resolve_container(config)
    except ValueError as exc:
        return _fail(str(exc))
    except (OSError, subprocess.SubprocessError):
        return _fail("docker-unavailable")
    inner = _container_command(config, cid, argv)
    if inner is None:
        return _fail("not-a-registry-route")
    cmd = [
        "docker", "exec", "-i",
        "-u", str(CONTAINER_UID),
        "-w", CONTAINER_WORKDIR,
        cid,
    ] + inner
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=EXEC_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # NOT proof of failure and NOT proof of success:  the exec may still land the
        # write.  Retry is safe only for read-only verbs and proven-idempotent writes;
        # anything else must be reconciled by reading the registry first.
        retry = "safe" if (
            argv[:2] == ["letter", "show"]
            or (len(argv) >= 2 and argv[0] == "platform-letter" and argv[1] == "show")
            or (len(argv) >= 2 and argv[0] == "operator-registry"
                and argv[1] in READ_ONLY_VERBS | PROVEN_IDEMPOTENT_WRITES)
            or (len(argv) >= 3 and argv[:2] == ["letter", "registry"]
                and argv[2] in ("register", "touch", "close"))
            or (len(argv) >= 3 and argv[:2] == ["platform-letter", "registry"]
                and argv[2] in ("register", "touch", "close", "show"))
        ) else "read-back-first"
        sys.stdout.write(json.dumps({
            "error": "operator registry dispatch: timeout",
            "outcome": "unknown",
            "retry": retry,
        }) + "\n")
        return 3
    except (OSError, subprocess.SubprocessError):
        return _fail("docker-unavailable")
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
