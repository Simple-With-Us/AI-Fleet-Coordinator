"""``python3 -m fleet_knobs set KEY VALUE (--plan|--yes)``: change one NON-secret knob in Infisical.

The only write path for knobs.  What it promises:

  * Only a registered, non-secret knob, and only a value its ``Spec`` accepts (kind, choices,
    range, pattern), checked by the same ``values.parse`` every reader uses.  The value is
    written in its canonical text (``values.render``), so what lands in Infisical is exactly
    what a reader will parse back.
  * ``--plan`` shows the old value, the new one and the CLI argv, and writes nothing.  ``--yes``
    writes.  With neither it refuses.
  * The write goes through the ``infisical`` CLI with an argv list (no shell) and a
    write-capable service token that lives only in this process's memory and in the child's
    environment as ``INFISICAL_TOKEN``.  The token comes from ``FLEET_KNOBS_WRITE_TOKEN`` or, on
    the Mac, the login keychain item ``fleet-knobs-write`` (account ``INFISICAL_TOKEN``).  It is
    never printed, logged, written or put in an argv.  Without one the write is refused with
    ``no write token``.  The CLI's output is discarded: only its exit status is kept.
  * After a write the last-known-good copy is refreshed, so a reader that cannot reach
    Infisical sees the new value at once.  A live re-read refreshes it the usual way; when that
    read fails, the copy is patched for this one key and keeps its own age.
  * Every ``--yes`` appends one row to ``~/.agent-sync/listener/knobs.jsonl`` (0600): time,
    seat, key, old and new value and the result.

Every outside effect (the CLI, the keychain, the live read, the copy) is a parameter, so the
tests run none of them.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from . import knobs, lkg, source, values
from .registry import BY_NAME, Spec

# The knobs live at the root of the AI Fleet Coordinator project (source.argv_for reads "/").
KNOB_PATH = "/"
ENV_WRITE_TOKEN = "FLEET_KNOBS_WRITE_TOKEN"
KEYCHAIN_SERVICE = "fleet-knobs-write"
KEYCHAIN_ACCOUNT = "INFISICAL_TOKEN"
SECURITY = "/usr/bin/security"
WRITE_TIMEOUT = 30
KEYCHAIN_TIMEOUT = 10
MAX_TOKEN = 4096
# Variables that must never reach the write child: any other Infisical credential.
DROP_FROM_CHILD = (
    source.ENV_TOKEN,
    source.ENV_AUTOMATION_ID,
    source.ENV_AUTOMATION_SECRET,
    ENV_WRITE_TOKEN,
)


class Refused(Exception):
    """The write is not allowed.  The text names a key or a reason, never a token."""


class Failed(Exception):
    """The write was allowed and did not succeed."""


Runner = Callable[..., tuple[int, bytes]]


def run(
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    want_stdout: bool = False,
) -> tuple[int, bytes]:
    """(exit status, stdout).  An argv list without a shell; stderr is always discarded."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise Refused("internal: not an argv list")
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if want_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            cwd="/",
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return 124, b""
    except OSError:
        return 127, b""
    return proc.returncode, ((proc.stdout or b"") if want_stdout else b"")


def check(name: str, raw: str) -> tuple[Spec, Any, str]:
    """(spec, typed value, canonical text) for a request, or Refused.  Never names the value."""
    spec = BY_NAME.get(name)
    if spec is None:
        raise Refused(f"unknown knob {name}")
    if spec.secret:
        raise Refused(f"{name} is a secret and is never written here")
    try:
        typed = values.parse(spec, raw)
    except ValueError as exc:
        raise Refused(f"{name}: the value is not accepted ({exc})") from None
    text = values.render(spec, typed)
    # The CLI reads KEY=@file as a file name on some versions: never let a value start so.
    if text.startswith("@"):
        raise Refused(f"{name}: a value may not start with @")
    if values.parse(spec, text) != typed:
        raise Refused(f"{name}: the value does not survive a round trip")
    return spec, typed, text


def write_argv(cli: str, name: str, text: str) -> list[str]:
    return [
        cli,
        "secrets",
        "set",
        f"{name}={text}",
        f"--env={source.ENVIRONMENT}",
        f"--path={KNOB_PATH}",
        f"--projectId={source.PROJECT_ID}",
        "--type=shared",
        "--silent",
    ]


def read_token(environ: dict[str, str], platform: str, runner: Runner) -> str:
    """The write token from the environment, else the Mac keychain, else ""."""
    token = (environ.get(ENV_WRITE_TOKEN) or "").strip()
    if not token and platform.startswith("darwin"):
        status, out = runner(
            [
                SECURITY,
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            timeout=KEYCHAIN_TIMEOUT,
            want_stdout=True,
        )
        token = out.decode("utf-8", "replace").strip() if status == 0 else ""
        out = b""
    if len(token) > MAX_TOKEN or any(c.isspace() or ord(c) < 33 for c in token):
        return ""
    return token


def child_env(environ: dict[str, str], token: str) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in DROP_FROM_CHILD}
    env["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
    env[source.ENV_TOKEN] = token
    return env


def current(name: str) -> tuple[Any, str]:
    """(typed value, source) from a fresh read: infisical, lkg or default."""
    knobs.refresh()
    knob = knobs.resolve(name)
    return knob.value, knob.source


def refresh_lkg(
    name: str, text: str, reader: Callable[[str], tuple[Any, str]], spec: Spec, typed: Any
) -> str:
    """Bring the last-known-good copy up to the new value.  Returns how: ``refreshed`` (a live
    read rewrote it), ``patched`` (the read failed, so this key was set in the copy and the copy
    kept its age), or ``not-refreshed:<note>``."""
    try:
        value, where = reader(name)
    except Exception:  # noqa: BLE001 - the copy is a convenience
        value, where = None, "error"
    if where == "infisical" and value == typed:
        return "refreshed"
    doc, note = lkg.read()
    if doc is None:
        return f"not-refreshed:{note}"
    copy = dict(doc["knobs"])
    copy[name] = text
    try:
        lkg.write(copy, now=float(doc["saved_epoch"]))
    except (OSError, ValueError) as exc:
        return f"not-refreshed:{type(exc).__name__}"
    return "patched"


def audit_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".agent-sync", "listener", "knobs.jsonl")


def audit(path: str, row: dict[str, Any]) -> bool:
    """Append one JSON line, 0600, to a regular file of ours.  False when it cannot."""
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def set_knob(
    name: str,
    raw: str,
    mode: str | None,
    *,
    environ: dict[str, str] | None = None,
    platform: str | None = None,
    runner: Runner | None = None,
    reader: Callable[[str], tuple[Any, str]] | None = None,
    find_cli: Callable[[], str | None] | None = None,
    audit_file: str | None = None,
    now: Callable[[], datetime.datetime] | None = None,
) -> tuple[dict[str, Any], int]:
    """(answer, exit status) for one request.  ``mode`` is ``plan``, ``yes`` or None.

    The answer is ``{ok, verb, target, old, new, commands, result, detail}`` plus ``lkg`` after
    a write; ``result`` is planned, done, refused or failed; the exit status is 0, 0, 2 or 1."""
    environ = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    runner = runner or run
    reader = reader or current
    find_cli = find_cli or source.find_cli
    now = now or (lambda: datetime.datetime.now(datetime.UTC))
    answer: dict[str, Any] = {
        "ok": False,
        "verb": "set",
        "target": name,
        "old": None,
        "new": None,
        "commands": [],
        "result": "refused",
        "detail": "",
    }
    spec = None
    code = 2
    token = ""
    try:
        if mode not in ("plan", "yes"):
            raise Refused("pass --plan to see the write or --yes to make it")
        refusal = source.environment_refusal()
        if refusal:
            raise Refused(refusal)
        spec, typed, text = check(name, raw)
        answer["new"] = text
        try:
            old_value, old_source = reader(name)
            answer["old"] = values.render(spec, old_value) if old_value is not None else None
            answer["old_source"] = old_source
        except Exception:  # noqa: BLE001 - the old value is information only
            answer["old_source"] = "unknown"
        cli = find_cli()
        if not cli:
            raise Failed("the infisical CLI is not installed")
        argv = write_argv(cli, name, text)
        answer["commands"] = [["infisical"] + argv[1:]]
        if mode == "plan":
            answer.update(ok=True, result="planned", detail="nothing written")
            code = 0
        else:
            token = read_token(environ, platform, runner)
            if not token:
                raise Refused("no write token")
            env = child_env(environ, token)
            try:
                status, _ = runner(argv, timeout=WRITE_TIMEOUT, env=env)
            finally:
                env = {}
                token = ""
            if status != 0:
                raise Failed(f"infisical secrets set exited {status}")
            answer["lkg"] = refresh_lkg(name, text, reader, spec, typed)
            answer.update(ok=True, result="done", detail="written")
            code = 0
    except Refused as exc:
        answer.update(ok=False, result="refused", detail=str(exc))
        code = 2
    except Failed as exc:
        answer.update(ok=False, result="failed", detail=str(exc))
        code = 1
    finally:
        token = ""
    if mode == "yes":
        secret = spec is None or spec.secret
        audit(
            audit_file or audit_path(),
            {
                "time": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "seat": (environ.get("AGENT_SEAT") or "owner").strip()[:32] or "owner",
                "key": name if name in BY_NAME else "unknown",
                # A value is only ever recorded for a registered non-secret knob.
                "old": None if secret else answer["old"],
                "new": None if secret else answer["new"],
                "result": answer["result"],
            },
        )
    return answer, code
