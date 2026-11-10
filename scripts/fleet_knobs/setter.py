"""``python3 -m fleet_knobs set KEY VALUE (--plan|--yes)``: change one NON-secret knob in Infisical.

The only write path for knobs.  What it promises:

  * Only a registered, non-secret knob, and only a value its ``Spec`` accepts (kind, choices,
    range, pattern), checked by the same ``values.parse`` every reader uses.  The value is
    written in its canonical text (``values.render``), so what lands in Infisical is exactly
    what a reader will parse back.
  * ``--plan`` shows the old value, the new one, the direction and the CLI argv, and writes
    nothing.  It refuses, before any approval, a write that could not run in this process (no
    write token), a value Infisical already holds and a reset of a knob Infisical does not
    hold.  ``--yes``
    writes.  With neither it refuses.
  * The write goes through the ``infisical`` CLI with an argv list (no shell) and a
    write-capable service token that lives only in this process's memory and in the child's
    environment as ``INFISICAL_TOKEN``.  The token comes only from ``FLEET_KNOBS_WRITE_TOKEN``
    and its grant metadata is checked before writing.  It is never printed, logged, written or
    put in an argv.  Without one the write is refused with
    ``no write token``.  The CLI's output is discarded: only its exit status is kept.
  * After a write the last-known-good copy is refreshed, so a reader that cannot reach
    Infisical sees the new value at once.  A live re-read refreshes it the usual way; when that
    read fails, the copy is patched for this one key and keeps its own age.
  * Every ``--yes`` appends one row to ``~/.simple-tools/listener/knobs.jsonl`` (0600): time,
    seat, key, old and new value and the result.

Every outside effect (the CLI, the live read, the copy) is a parameter, so the
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
WRITE_TIMEOUT = 30
MAX_TOKEN = 2048
# Variables that must never reach the write child: any other Infisical credential.
DROP_FROM_CHILD = (
    source.ENV_TOKEN,
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
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
    if spec.kind == "caps":
        # A cap name the knob does not have is a typo the readers would ignore, so the write
        # would change nothing the owner meant.  The names are the registry default's.
        known = set(spec.default or {})
        if not set(typed) <= known:
            raise Refused(
                f"{name}: a cap name is not one this knob has (it has {', '.join(sorted(known))})"
            )
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
    """The dedicated writer binding only; never query a local credential store."""
    token = (environ.get(ENV_WRITE_TOKEN) or "").strip()
    if (
        not token.startswith("st.")
        or len(token) < 20
        or len(token) > MAX_TOKEN
        or any(c.isspace() or ord(c) < 33 for c in token)
    ):
        return ""
    return token


def verify_writer(token: str, environ: dict[str, str]) -> None:
    """Require metadata to prove this project's prod root write grant before CLI use."""
    from fleet_secrets import core as secrets

    try:
        secrets.verify_service_token(token, source.PROJECT_ID, KNOB_PATH, environ, writer=True)
    except secrets.SecretsError:
        raise Refused(
            "write token does not prove the configured project, prod folder, and permissions"
        ) from None


def child_env(environ: dict[str, str], token: str) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in DROP_FROM_CHILD}
    env["INFISICAL_DISABLE_UPDATE_CHECK"] = "true"
    env[source.ENV_TOKEN] = token
    return env


def current(name: str) -> tuple[Any, str, bool, str | None]:
    """(typed value, source, stale, reason) from a fresh read: infisical, lkg or default."""
    knobs.refresh()
    knob = knobs.resolve(name)
    # The third field says whether Infisical was read, and the fourth why a default was
    # answered (unset, invalid or unreachable), so a "default" from a failed read or from a
    # value Infisical holds but the spec refuses is never taken as proof that it is unset.
    return knob.value, knob.source, knob.stale, knob.reason


def read_old(reader: Callable[[str], tuple], name: str) -> tuple[Any, str, bool | None, str | None]:
    """(value, source, stale, reason) from a reader that answers two to four fields.

    A reader that does not say whether its answer is stale, or why, answers None for it, and
    a ``default`` from it is never taken as proof that the knob is unset."""
    answer = reader(name)
    stale = bool(answer[2]) if len(answer) >= 3 else None
    reason = answer[3] if len(answer) >= 4 else None
    return answer[0], answer[1], stale, reason


def _peek(name: str) -> tuple[Any, bool]:
    """Another knob's (value, known) from the read this process already made.

    A default answered while Infisical could not be read, or a read that failed, is not
    known:  it says nothing about whether the other knob is set."""
    try:
        knob = knobs.resolve(name)
    except Exception:  # noqa: BLE001 - a dependency check is information only
        return None, False
    return knob.value, not (knob.source == "default" and knob.stale)


_DIRECTION_SENTENCES = {
    "wider": "This makes the fleet do more than it does now.",
    "narrower": "This makes the fleet do less than it does now.",
    "mixed": "This raises some limits and lowers others.",
}


def plan_notes(
    spec: Spec,
    typed: Any,
    text: str,
    old: Any,
    old_source: str | None,
    stale: bool | None = None,
) -> tuple[str, list[str]]:
    """(direction, warnings) for a change:  which way it moves the knob, and what the owner
    should read before approving it.  Only non-secret knobs get here.

    A ``default`` answered while Infisical could not be read is only what a job would fall
    back to, so it is not a current value:  the direction is unknown and the plan says so."""
    known = old_source in ("infisical", "lkg") or (old_source == "default" and stale is False)
    way = values.direction(spec, old, typed) if known else "unknown"
    warnings: list[str] = []
    if way in _DIRECTION_SENTENCES:
        warnings.append(_DIRECTION_SENTENCES[way])
    for value, sentence in spec.consequences:
        if value == text:
            warnings.append(sentence)
    for value, other in spec.requires:
        if value != text:
            continue
        other_value, other_known = _peek(other)
        if not other_known:
            warnings.append(f"{text} also needs {other}, and whether that is set is not known.")
        elif other_value in (None, "", False, (), {}):
            warnings.append(
                f"{text} also needs {other}, which is not set, so it will do less than its name says."
            )
    if spec.once and old not in (None, "") and old != typed:
        warnings.append(f"{spec.name} is meant to be set once, and it already holds a value.")
    if old_source == "lkg":
        warnings.append(
            "Infisical could not be read just now, so the current value is this machine's copy "
            "and the write may fail."
        )
    elif not known:
        warnings.append(
            "The current value could not be read, so the write may replace a value not shown."
        )
    return way, warnings


def render_default(spec: Spec) -> str:
    """The registry default as the engine writes it, or empty text for none."""
    default = values.default_value(spec)
    return "" if default is None else values.render(spec, default)


def describe_reach(spec: Spec) -> dict[str, Any]:
    """Where the change lands and who reads it, for the plan."""
    return {
        "stored": "infisical",
        "hosts": spec.hosts,
        "consumers": [plain for plain, _path in spec.consumers],
        "default": render_default(spec),
    }


def refresh_lkg(
    name: str, text: str, reader: Callable[[str], tuple], spec: Spec, typed: Any
) -> str:
    """Bring the last-known-good copy up to the new value.  Returns how: ``refreshed`` (a live
    read rewrote it), ``patched`` (the read failed, so this key was set in the copy and the copy
    kept its age), or ``not-refreshed:<note>``."""
    try:
        value, where, _stale, _reason = read_old(reader, name)
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
    return os.path.join(os.path.expanduser("~"), ".simple-tools", "listener", "knobs.jsonl")


def audit(path: str, row: dict[str, Any]) -> bool:
    """Append one JSON line, 0600, to a regular file of ours.  False when it cannot."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # A symlink in place of the directory would carry the audit row somewhere else:
        # the directory must be a real one of ours.
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return False
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


NO_TOKEN_PLAN = (
    "No write token is available here, so the write would be refused after approval.  "
    "Change the knob in Infisical instead, or run this where FLEET_KNOBS_WRITE_TOKEN is set."
)


def _refuse_a_plan_that_cannot_run(
    answer: dict[str, Any], environ: dict[str, str], platform: str, runner: Runner
) -> None:
    """Refuse a plan whose write would be refused for want of a token.

    The plan is the last step before the owner approves, so a write that cannot run is
    refused here rather than after the approval.  Only the token's shape is checked:  the
    value is held for this call and never printed."""
    present = bool(read_token(environ, platform, runner))
    answer["write_token"] = "present" if present else "missing"
    if not present:
        raise Refused(NO_TOKEN_PLAN)


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
    now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
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
        answer.update(describe_reach(spec))
        old_value: Any = None
        stale: bool | None = None
        try:
            old_value, old_source, stale, _reason = read_old(reader, name)
            answer["old"] = values.render(spec, old_value) if old_value is not None else None
            answer["old_source"] = old_source
        except Exception:  # noqa: BLE001 - the old value is information only
            answer["old_source"] = "unknown"
        if answer["old_source"] == "infisical" and old_value == typed:
            # Nothing would change, so nothing is asked for:  no approval and no audit row
            # for a write that writes what is already there.
            raise Refused(
                f"{name} already holds this value in Infisical, so there is nothing to write"
            )
        direction, warnings = plan_notes(spec, typed, text, old_value, answer["old_source"], stale)
        answer["direction"] = direction
        answer["warnings"] = warnings
        cli = find_cli()
        if not cli:
            raise Failed("the infisical CLI is not installed")
        argv = write_argv(cli, name, text)
        answer["commands"] = [["infisical"] + argv[1:]]
        if mode == "plan":
            _refuse_a_plan_that_cannot_run(answer, environ, platform, runner)
            detail = "nothing written"
            if warnings:
                detail += ".  " + "  ".join(warnings)
            answer.update(ok=True, result="planned", detail=detail)
            code = 0
        else:
            token = read_token(environ, platform, runner)
            if not token:
                raise Refused("no write token")
            verify_writer(token, environ)
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
        answer["audit"] = (
            "written"
            if audit(
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
            else "failed"
        )
    return answer, code


def delete_argv(cli: str, name: str) -> list[str]:
    return [
        cli,
        "secrets",
        "delete",
        name,
        f"--env={source.ENVIRONMENT}",
        f"--path={KNOB_PATH}",
        f"--projectId={source.PROJECT_ID}",
        "--type=shared",
        "--silent",
    ]


def check_unset(name: str) -> Spec:
    spec = BY_NAME.get(name)
    if spec is None:
        raise Refused(f"unknown knob {name}")
    if spec.secret:
        raise Refused(f"{name} is a secret and is never written here")
    return spec


def refresh_lkg_unset(name: str, reader) -> str:
    """Remove the knob from the last-known-good copy after a delete."""
    try:
        _value, where, _stale, _reason = read_old(reader, name)
    except Exception:  # noqa: BLE001
        where = "error"
    doc, note = lkg.read()
    if doc is None:
        return "refreshed" if where == "infisical" else f"not-refreshed:{note}"
    if name not in doc["knobs"]:
        return "refreshed"
    copy = dict(doc["knobs"])
    copy.pop(name, None)
    try:
        lkg.write(copy, now=float(doc["saved_epoch"]))
    except (OSError, ValueError) as exc:
        return f"not-refreshed:{type(exc).__name__}"
    return "patched"


def unset_knob(
    name,
    mode,
    *,
    environ=None,
    platform=None,
    runner=None,
    reader=None,
    find_cli=None,
    audit_file=None,
    now=None,
):
    """(answer, exit) for `unset`: Infisical delete first, then the local copy."""
    environ = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    runner = runner or run
    reader = reader or current
    find_cli = find_cli or source.find_cli
    now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
    answer = {
        "ok": False,
        "verb": "unset",
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
        spec = check_unset(name)
        reach = describe_reach(spec)
        answer.update(reach)
        # The value that applies once the override is gone is the registry default.
        answer["new"] = reach["default"]
        answer["new_source"] = "default"
        old_value: Any = None
        stale: bool | None = None
        reason: str | None = None
        try:
            old_value, old_source, stale, reason = read_old(reader, name)
            answer["old"] = values.render(spec, old_value) if old_value is not None else None
            answer["old_source"] = old_source
        except Exception:  # noqa: BLE001
            answer["old_source"] = "unknown"
        if answer["old_source"] == "default" and stale is False and reason == "unset":
            # Infisical was read and holds no value, so there is no override to remove.
            raise Refused(f"{name} is not set in Infisical, so it already holds its default")
        typed_default = values.default_value(spec)
        direction, warnings = plan_notes(
            spec, typed_default, answer["new"], old_value, answer["old_source"], stale
        )
        if spec.requires_explicit:
            warnings.append(
                "The rule that reads this treats the default as not set, so it will read unknown."
            )
        answer["direction"] = direction
        answer["warnings"] = warnings
        cli = find_cli()
        if not cli:
            raise Failed("the infisical CLI is not installed")
        argv = delete_argv(cli, name)
        answer["commands"] = [["infisical"] + argv[1:]]
        if mode == "plan":
            _refuse_a_plan_that_cannot_run(answer, environ, platform, runner)
            detail = "nothing written"
            if warnings:
                detail += ".  " + "  ".join(warnings)
            answer.update(ok=True, result="planned", detail=detail)
            code = 0
        else:
            token = read_token(environ, platform, runner)
            if not token:
                raise Refused("no write token")
            verify_writer(token, environ)
            env = child_env(environ, token)
            try:
                status, _ = runner(argv, timeout=WRITE_TIMEOUT, env=env)
            finally:
                env = {}
                token = ""
            if status != 0:
                raise Failed(f"infisical secrets delete exited {status}")
            answer["lkg"] = refresh_lkg_unset(name, reader)
            answer.update(ok=True, result="done", detail="deleted; the default applies")
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
        answer["audit"] = (
            "written"
            if audit(
                audit_file or audit_path(),
                {
                    "time": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "seat": (environ.get("AGENT_SEAT") or "owner").strip()[:32] or "owner",
                    "key": name if name in BY_NAME else "unknown",
                    "old": None if secret else answer["old"],
                    "new": None,
                    "result": answer["result"],
                },
            )
            else "failed"
        )
    return answer, code
