"""Server nightly Fleet RAG ingest (AFC-687).

Runs ``recall ingest --source linear,doc,skill,memory --prune`` after a
successful ``code-main-sync`` service, under its own lock, and reuses the
ingest prune guards.  Apple Notes are never part of this command.  Skill and
memory pruning stays off when the Mac mirror is missing or stale.

The process is disabled until ``FLEET_RAG_NIGHTLY=1``.  It reads Infisical
with service tokens only.  A machine-identity variable in the environment
refuses the run.  Secret values are never written to the summary or the log.

Exit 0 when the job is disabled, the lock is busy, or the ingest reports ok.
Exit 1 when code-main-sync fails, credentials cannot be read, or the ingest
reports a failure.  Zulip is posted only for a real run that failed or tripped
a prune guard.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_secrets.core import (
    ENV_API,
    SecretsError,
    check_service_token,
    fetch_project,
    infisical_project_id,
)
from simple_tools.secretscan import scan as scan_secrets

from fleet_rag import ingest, mirror
from fleet_rag.core import FleetRagError, infisical_api, infisical_project

# The command the timer runs.  --no-heartbeat keeps the process off fleet-secrets,
# which is the machine-identity reader.  Apple Notes are not in the source list.
INGEST_ARGV = ["--source", "linear,doc,skill,memory", "--prune", "--no-heartbeat"]
INGEST_SOURCES = ("linear", "doc", "skill", "memory")

RECALL_KEYS = (
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "QDRANT_FLEET_COLLECTION",
    "TEI_URL",
    "TEI_API_KEY",
)
LINEAR_KEYS = (
    "LINEAR_BF_ORACLE_CLIENT_ID",
    "LINEAR_BF_ORACLE_CLIENT_SECRET",
    "LINEAR_GB_ORACLE_CLIENT_ID",
    "LINEAR_GB_ORACLE_CLIENT_SECRET",
)
TOKEN_FILE_RECALL = "FLEET_RECALL_INFISICAL_TOKEN_FILE"
TOKEN_FILE_LINEAR = "FLEET_RAG_LINEAR_INFISICAL_TOKEN_FILE"
# The seat Linear app pairs live in AI Fleet Coordinator prod /linear, not in the shared
# project that holds the Qdrant and TEI keys.  The id is not committed:  it comes from this
# variable, else from the fleet_secrets AI Fleet Coordinator source (env or ~/.simple-tools).
LINEAR_PROJECT_ENV = "FLEET_RAG_LINEAR_INFISICAL_PROJECT"
RECALL_PROJECT_ENV = "FLEET_RAG_INFISICAL_PROJECT"
TOKEN_FILE_ZULIP = "FLEET_RAG_ZULIP_INFISICAL_TOKEN_FILE"
# The server file from AFC-677 (#1729).  It holds FLEET_RECALL_INFISICAL_TOKEN=...
# An export prefix and quotes are allowed.  TOKEN_FILE_RECALL overrides this path.
DEFAULT_RECALL_TOKEN_FILE = "/home/jay/.secrets/fleet-recall-infisical.env"
RECALL_TOKEN_KEY = "FLEET_RECALL_INFISICAL_TOKEN"

MACHINE_IDENTITY_ENV = (
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
    "INFISICAL_SHARED_CLIENT_ID",
    "INFISICAL_SHARED_CLIENT_SECRET",
)

ALERT_TAG = "[GB-ORACLE \U0001f4c1AFC]"
ALERT_STREAM = "Alerts"
ALERT_TOPIC = "fleet-rag-nightly"
GAP = "\u00a0 "


class NightlyError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def enabled(env: dict) -> bool:
    return (env.get("FLEET_RAG_NIGHTLY") or "").strip() == "1"


def machine_identity_name(env: dict) -> str | None:
    for name in MACHINE_IDENTITY_ENV:
        if (env.get(name) or "").strip():
            return name
    return None


def recall_token_path(env: dict) -> Path:
    """The recall reader file.  Unset uses the server file that is already in place."""
    named = (env.get(TOKEN_FILE_RECALL) or "").strip()
    return Path(named).expanduser() if named else Path(DEFAULT_RECALL_TOKEN_FILE)


def _service_token_text(text: str) -> str:
    """A raw token line, or the FLEET_RECALL_INFISICAL_TOKEN assignment in an env file."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == RECALL_TOKEN_KEY:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return text.strip()


def read_token_file(path: Path) -> str:
    """Return a service token from a regular 0600 or 0400 file.  Never log it.

    A recall env file (the #1729 shape) yields the FLEET_RECALL_INFISICAL_TOKEN
    value.  A Linear or Zulip file is the raw token on one line.
    """
    if path.is_symlink() or not path.is_file():
        raise NightlyError("token-file")
    if path.stat().st_mode & 0o077:
        raise NightlyError("token-file-mode")
    try:
        return check_service_token(_service_token_text(path.read_text(encoding="utf-8")))
    except (OSError, SecretsError, UnicodeError):
        raise NightlyError("token-invalid") from None


def _call_fetch(
    fetch: Callable, project: str, path: str, token: str, names: tuple[str, ...]
) -> dict:
    try:
        found = fetch(project, path, token, names)
    except SecretsError as exc:
        raise NightlyError("infisical-read-failed:" + exc.message) from None
    if not isinstance(found, dict):
        raise NightlyError("infisical-read-failed")
    return found


def _fetch(project: str, path: str, token: str, names: tuple[str, ...]) -> dict[str, str]:
    env = {ENV_API: infisical_api()}
    try:
        found = fetch_project(project, path, env, token=token, names=names)
    except SecretsError as exc:
        raise NightlyError("infisical-read-failed:" + exc.message) from None
    return {key: value for key, value in found.items() if key in names and value}


def linear_project_id(env: dict) -> str:
    """AI Fleet Coordinator project id for the Linear fetch.  Fails closed, without the value."""
    value = (env.get(LINEAR_PROJECT_ENV) or "").strip()
    if value:
        return value
    home = Path(env.get("HOME") or str(Path.home()))
    try:
        return infisical_project_id(env, home, "ai-fleet-coordinator")
    except SecretsError as exc:
        raise NightlyError("linear-project-unset") from exc


def load_credentials(
    env: dict, *, read_file: Callable[[Path], str], fetch: Callable
) -> dict[str, str]:
    """Read Qdrant, TEI and Linear through service tokens.  Zulip is optional.

    ``fetch(project, path, token, names) -> {name: value}``.  The token itself
    is not returned and is not placed in the result.
    """
    blocked = machine_identity_name(env)
    if blocked:
        raise NightlyError("machine-identity:" + blocked)
    linear_file = (env.get(TOKEN_FILE_LINEAR) or "").strip()
    if not linear_file:
        raise NightlyError("token-file-unset")
    project = (env.get(RECALL_PROJECT_ENV) or "").strip() or infisical_project()
    linear_project = linear_project_id(env)
    recall = _call_fetch(fetch, project, "/", read_file(recall_token_path(env)), RECALL_KEYS)
    linear = _call_fetch(
        fetch, linear_project, "/linear", read_file(Path(linear_file)), LINEAR_KEYS
    )
    missing = [name for name in RECALL_KEYS if not recall.get(name)]
    if missing:
        raise NightlyError("missing:" + ",".join(missing))
    pairs = []
    for seat in ("BF_ORACLE", "GB_ORACLE"):
        if linear.get(f"LINEAR_{seat}_CLIENT_ID") and linear.get(f"LINEAR_{seat}_CLIENT_SECRET"):
            pairs.append(seat)
    if not pairs:
        raise NightlyError("missing:linear-seat-pair")
    loaded = {**recall, **{key: value for key, value in linear.items() if value}}
    loaded.update(_zulip_credentials(env, read_file, fetch))
    return loaded


def _zulip_credentials(
    env: dict, read_file: Callable[[Path], str], fetch: Callable
) -> dict[str, str]:
    path_name = (env.get(TOKEN_FILE_ZULIP) or "").strip()
    if not path_name:
        return {}
    project = (env.get("FLEET_RAG_ZULIP_INFISICAL_PROJECT") or "").strip()
    if not project:
        raise NightlyError("missing:FLEET_RAG_ZULIP_INFISICAL_PROJECT")
    folder = (env.get("FLEET_RAG_ZULIP_INFISICAL_PATH") or "/zulip").strip() or "/zulip"
    site_name = (env.get("FLEET_RAG_ZULIP_SITE_NAME") or "ZULIP_SITE").strip()
    email_name = (env.get("FLEET_RAG_ZULIP_EMAIL_NAME") or "ZULIP_EMAIL").strip()
    key_name = (env.get("FLEET_RAG_ZULIP_API_KEY_NAME") or "ZULIP_API_KEY").strip()
    found = _call_fetch(
        fetch, project, folder, read_file(Path(path_name)), (site_name, email_name, key_name)
    )
    mapped = {
        "ZULIP_SITE": found.get(site_name, ""),
        "ZULIP_EMAIL": found.get(email_name, ""),
        "ZULIP_API_KEY": found.get(key_name, ""),
    }
    if not all(mapped.values()):
        raise NightlyError("missing:zulip")
    return mapped


def _public(text: str) -> str:
    if not text:
        return ""
    if scan_secrets(text):
        return "withheld"
    return text[:500]


def summary_path(env: dict) -> Path:
    raw = (env.get("FLEET_RAG_NIGHTLY_SUMMARY") or "").strip()
    if raw:
        return Path(raw)
    home = Path(env.get("HOME") or str(Path.home()))
    return home / ".local" / "state" / "fleet-rag-nightly" / "last.json"


def write_summary(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def alert_text(summary: dict, tag: str, host: str) -> str:
    if summary.get("ok"):
        lead = f"Fleet RAG nightly ingest on {host} tripped a prune guard."
    else:
        lead = f"Fleet RAG nightly ingest on {host} failed."
    parts = [lead]
    trips = summary.get("guard_trips") or []
    if trips:
        parts.append(f"{len(trips)} prune guard(s) tripped.")
    sync_exit = summary.get("code_sync_exit")
    if sync_exit not in (None, 0):
        parts.append(f"code-main-sync exited {sync_exit}.")
    errors = summary.get("errors") or []
    if errors:
        parts.append(f"{len(errors)} ingest error(s).")
    lines = [tag, "", GAP.join(parts), ""]
    for item in errors:
        lines.append(f"- {item}")
    for trip in summary.get("guard_trips") or []:
        source = trip.get("source") or "ingest"
        reason = trip.get("reason") or "guard"
        lines.append(f"- `{source}`: {reason}")
    return "\n".join(lines).rstrip() + "\n"


def zulip_post(site: str, email: str, api_key: str, fields: dict[str, str]) -> None:
    """POST one Zulip message.  Same https-only shape as code-main-sync."""
    parsed = urllib.parse.urlsplit(site.strip().rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc:
        raise NightlyError("zulip-site")
    auth = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    req = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}/api/v1/messages",
        data=urllib.parse.urlencode(fields).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=20) as resp:
        if resp.status != 200:
            raise NightlyError("zulip-http")


def _should_alert(summary: dict) -> bool:
    return bool(summary.get("ran") and (not summary.get("ok") or summary.get("guard_trips")))


def send_alert(summary: dict, env: dict, *, post: Callable | None = None) -> str:
    """Post when the run failed or a guard tripped.  Returns a short status word."""
    if not _should_alert(summary):
        return "quiet"
    site = (env.get("ZULIP_SITE") or "").strip()
    email = (env.get("ZULIP_EMAIL") or "").strip()
    api_key = (env.get("ZULIP_API_KEY") or "").strip()
    if not (site and email and api_key):
        return "unconfigured"
    host = os.uname().nodename
    body = alert_text(summary, (env.get("FLEET_RAG_ALERT_TAG") or ALERT_TAG), host)
    if scan_secrets(body):
        body = (
            (env.get("FLEET_RAG_ALERT_TAG") or ALERT_TAG)
            + "\n\n"
            + GAP.join(
                [
                    f"Fleet RAG nightly ingest on {host} failed.",
                    "The detail was withheld.",
                ]
            )
            + "\n"
        )
    fields = {
        "type": "stream",
        "to": env.get("FLEET_RAG_ALERT_STREAM") or ALERT_STREAM,
        "topic": env.get("FLEET_RAG_ALERT_TOPIC") or ALERT_TOPIC,
        "content": body,
    }
    try:
        (post or zulip_post)(site, email, api_key, fields)
    except Exception as exc:  # noqa: BLE001 - alerting must not hide the ingest result
        sys.stderr.write(f"alert: Zulip post failed ({type(exc).__name__})\n")
        return "failed"
    sys.stderr.write("alert: posted\n")
    return "posted"


# Set by fleet-rag-nightly.service, which pulls code-main-sync in with Wants= and After=.
# systemd has then already run the sync before this process starts, so the job only reads
# the result.  Starting a unit from a User= service needs polkit, which the job does not get.
SYNC_BY_UNIT_ENV = "FLEET_RAG_CODE_SYNC_BY_UNIT"
SYNC_UNIT = "code-main-sync.service"


def unit_sync_result(show: Callable[[list[str]], str] | None = None) -> int:
    """0 when the last code-main-sync run succeeded, else non-zero.  Read-only."""
    argv = ["systemctl", "show", SYNC_UNIT, "--property=Result", "--value"]
    try:
        if show is not None:
            out = show(argv)
        else:
            out = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, check=False
            ).stdout
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
    return 0 if out.strip() == "success" else 1


def code_sync(
    runner: Callable[[], int] | None = None, env: dict | None = None
) -> int:
    if runner is not None:
        return runner()
    if (env or {}).get(SYNC_BY_UNIT_ENV, "").strip() == "1":
        return unit_sync_result()
    try:
        proc = subprocess.run(
            ["systemctl", "start", "--wait", "code-main-sync.service"],
            capture_output=True,
            timeout=1800,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
    return proc.returncode


def run_ingest() -> tuple[int, dict]:
    buf = io.StringIO()
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = ingest.main(list(INGEST_ARGV))
    except Exception as exc:  # noqa: BLE001 - the summary records the type only
        return 1, {"ok": False, "errors": [type(exc).__name__], "guard_trips": [], "warnings": []}
    text = buf.getvalue().strip()
    report: dict = {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            report = parsed
    if not report:
        report = {"ok": code == 0, "errors": [], "guard_trips": [], "warnings": []}
    report.setdefault("errors", [])
    report.setdefault("guard_trips", [])
    report.setdefault("warnings", [])
    if code != 0 and report.get("ok") is not False and not report["errors"]:
        report["ok"] = False
        report["errors"] = [f"ingest-exit-{code}"]
    return code, report


def _base(enabled_flag: bool) -> dict:
    return {
        "enabled": enabled_flag,
        "ran": False,
        "ok": True,
        "code_sync_exit": None,
        "ingest_exit": None,
        "errors": [],
        "warnings": [],
        "guard_trips": [],
        "sources": list(INGEST_SOURCES),
        "mirror": None,
        "alert": "quiet",
    }


def _apply(updates: dict[str, str]) -> dict[str, str | None]:
    saved: dict[str, str | None] = {}
    for key, value in updates.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    return saved


def _restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def execute(
    env: dict,
    *,
    read_file: Callable[[Path], str] | None = None,
    fetch: Callable | None = None,
    sync: Callable[[], int] | None = None,
    ingest_runner: Callable[[], tuple[int, dict]] | None = None,
    post: Callable | None = None,
    now: float | None = None,
) -> dict:
    """One nightly attempt.  ``env`` is the job's configuration, not os.environ."""
    summary = _base(enabled(env))
    if not summary["enabled"]:
        summary["alert"] = "quiet"
        return summary
    blocked = machine_identity_name(env)
    if blocked:
        summary["ran"] = True
        summary["ok"] = False
        summary["errors"] = ["machine-identity:" + blocked]
        alert_env = dict(env)
        try:
            alert_env.update(_zulip_credentials(env, read_file or read_token_file, fetch or _fetch))
        except NightlyError:
            pass
        summary["alert"] = send_alert(summary, alert_env, post=post)
        return summary
    home = Path(env.get("HOME") or str(Path.home()))
    lock_path = Path(
        (env.get("FLEET_RAG_NIGHTLY_LOCK") or "").strip()
        or (home / ".local" / "state" / "fleet-rag-nightly" / "nightly.lock")
    )
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        summary["warnings"] = ["lock-busy"]
        return summary
    try:
        return _locked(
            summary,
            env,
            read_file=read_file or read_token_file,
            fetch=fetch or _fetch,
            sync=sync,
            ingest_runner=ingest_runner or run_ingest,
            post=post,
            now=time.time() if now is None else now,
        )
    finally:
        os.close(fd)


def _locked(summary, env, *, read_file, fetch, sync, ingest_runner, post, now) -> dict:
    summary["ran"] = True
    try:
        loaded = load_credentials(env, read_file=read_file, fetch=fetch)
    except NightlyError as exc:
        summary["ok"] = False
        summary["errors"] = [_public(exc.reason)]
        summary["alert"] = send_alert(summary, env, post=post)
        return summary
    except FleetRagError as exc:
        summary["ok"] = False
        summary["errors"] = [_public(str(exc))]
        summary["alert"] = send_alert(summary, env, post=post)
        return summary
    sync_exit = code_sync(sync, env)
    summary["code_sync_exit"] = sync_exit
    if sync_exit != 0:
        summary["ok"] = False
        summary["errors"] = [f"code-main-sync-exit-{sync_exit}"]
        summary["alert"] = send_alert(summary, {**env, **loaded}, post=post)
        return summary
    mirror_env = (env.get(mirror.MIRROR_ENV) or "").strip()
    updates = dict(loaded)
    if not mirror_env:
        updates[mirror.MIRROR_ENV] = str(summary_path(env).parent / "mirror-unconfigured")
        summary["guard_trips"] = [{"source": "mirror", "reason": "unconfigured"}]
        summary["mirror"] = {"fresh": False, "reason": "unconfigured"}
    else:
        status = mirror.mirror_status(Path(mirror_env), now, mirror.max_age_s(env))
        summary["mirror"] = {"fresh": status.fresh, "reason": status.reason, "at": status.at}
    updates["FLEET_RAG_REQUIRE_CODE_ROOT"] = "1"
    saved = _apply(updates)
    try:
        try:
            code, report = ingest_runner()
        except Exception as exc:  # noqa: BLE001 - the summary records the type only
            code, report = (
                1,
                {
                    "ok": False,
                    "errors": [type(exc).__name__],
                    "guard_trips": [],
                    "warnings": [],
                },
            )
    finally:
        _restore(saved)
    summary["ingest_exit"] = code
    errors = [_public(str(item)) for item in (report.get("errors") or [])]
    trips = []
    for trip in report.get("guard_trips") or []:
        if isinstance(trip, dict):
            trips.append(
                {
                    "source": _public(str(trip.get("source") or "")),
                    "reason": _public(str(trip.get("reason") or "")),
                }
            )
    if summary["guard_trips"]:
        trips = list(summary["guard_trips"]) + trips
    summary["errors"] = [item for item in errors if item]
    summary["warnings"] = [_public(str(item)) for item in (report.get("warnings") or [])]
    summary["guard_trips"] = trips
    summary["ok"] = bool(report.get("ok")) and code == 0 and sync_exit == 0
    # A guard trip keeps ok as the ingest reported it.  Unconfigured mirror
    # still ingests, and the alert fires because guard_trips is non-empty.
    if not summary["ok"] and not summary["errors"]:
        summary["errors"] = [f"ingest-exit-{code}"]
    alert_env = {**env, **loaded}
    summary["alert"] = send_alert(summary, alert_env, post=post)
    return summary


def main(argv: list[str] | None = None) -> int:
    del argv
    env = dict(os.environ)
    path = summary_path(env)
    held: dict = {"summary": _base(enabled(env))}

    def _on_term(signum, frame) -> None:
        del signum, frame
        stopped = held["summary"]
        stopped["ran"] = True
        stopped["ok"] = False
        stopped["errors"] = ["stopped"]
        try:
            write_summary(path, stopped)
        except OSError as exc:
            sys.stderr.write(f"nightly: summary ({type(exc).__name__})\n")
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _on_term)
    summary = execute(env)
    held["summary"] = summary
    try:
        write_summary(path, summary)
    except OSError as exc:
        sys.stderr.write(f"nightly: summary ({type(exc).__name__})\n")
    if not summary["enabled"] or summary.get("warnings") == ["lock-busy"]:
        return 0
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
