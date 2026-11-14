"""Fleet RAG setup health rows for `recall doctor` (not shipped in the server mirror)."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pathlib
import time
from collections.abc import Callable
from typing import Any

from . import core, recall_api
from .core import FleetRagError
from .doctor import DIRECT_PATH_BLOCKED_DETAIL, _finalize_rows, _row

NIGHTLY_MAX_AGE_H = 30 * 3600
INSTALL_BOARD = "linear-board/board"
INSTALL_SECRETSCAN = "simple_tools/secretscan.py"


def rag_install_root(
    home: pathlib.Path | None = None, env: dict[str, str] | None = None
) -> pathlib.Path:
    source = env if env is not None else os.environ
    home = home or pathlib.Path(os.path.expanduser("~"))
    raw = (source.get("FLEET_RAG_HOME") or "").strip()
    return pathlib.Path(raw).expanduser() if raw else home / "apps" / "fleet-rag"


def _token_file_path(env: dict[str, str]) -> pathlib.Path | None:
    return core._read_token_file_path(env)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# Probe path for the server unit drop-in.  Tests patch this so a real
# /etc/fleet/rag-nightly.env on the developer host cannot leak into setup_report.
NIGHTLY_UNIT_ENV = pathlib.Path("/etc/fleet/rag-nightly.env")


def nightly_applies(env: dict[str, str]) -> bool:
    if (env.get("FLEET_RAG_NIGHTLY") or "").strip() == "1":
        return True
    if (env.get("FLEET_RAG_NIGHTLY_SUMMARY") or "").strip():
        return True
    if NIGHTLY_UNIT_ENV.is_file():
        return True
    # Import here so a minimal `python3 -I mirror.py` copy is unaffected.
    from .nightly_job import summary_path

    return summary_path(env).is_file()


def _token_file_row(env: dict[str, str]) -> dict:
    hint = (
        f"configure this reader's authorized {core.INFISICAL_READ_TOKEN} "
        f"or {core.INFISICAL_READ_TOKEN_FILE} binding"
    )
    try:
        if core._process_read_token(env) is not None:
            return _row(
                "OK",
                "infisical:token-file",
                "own process binding configured; grant verification is separate",
            )
        if all((env.get(k) or "").strip() for k in core.READ_KEYS) and not core._read_token_bound(
            env
        ):
            return _row(
                "SKIP",
                "infisical:token-file",
                "not needed (all read keys already in environment)",
            )
        selected = core._open_read_token_file(env)
        if selected is None:
            return _row(
                "FAIL",
                "infisical:token-file",
                f"{core.INFISICAL_READ_TOKEN} / {core.INFISICAL_READ_TOKEN_FILE} is not configured",
                hint=hint,
            )
        _path, fd = selected
        os.close(fd)
        return _row(
            "OK",
            "infisical:token-file",
            "explicit recall bootstrap file present in a protected store; grant verification is separate",
        )
    except FleetRagError as exc:
        return _row("FAIL", "infisical:token-file", str(exc), hint=hint)


def _infisical_read_row(load_config: Callable[..., dict[str, str]]) -> dict:
    try:
        cfg = load_config(need_write=False)
    except FleetRagError as exc:
        return _row(
            "FAIL",
            "infisical:read-keys",
            str(exc)[:200],
            hint="fix the service-token scope or export QDRANT_* and TEI_* in the environment",
        )
    missing = [k for k in core.READ_KEYS if k not in cfg]
    if missing:
        return _row(
            "FAIL",
            "infisical:read-keys",
            "missing " + ", ".join(missing),
            hint="widen the recall reader token to prod / or set the keys in the environment",
        )
    return _row("OK", "infisical:read-keys", f"read ok ({len(core.READ_KEYS)} key names resolved)")


def _embedder_row(load_config: Callable[..., dict[str, str]]) -> dict:
    try:
        cfg = load_config(need_write=False)
    except FleetRagError:
        return _row(
            "FAIL", "tei:embedder", "credentials unavailable", hint="fix Infisical binding first"
        )
    healthy = core.embedder_healthy(cfg)
    return (
        _row("OK", "tei:embedder", "reachable")
        if healthy
        else _row(
            "FAIL",
            "tei:embedder",
            "unreachable",
            hint="check TEI_URL / TEI_API_KEY and Tailscale or recall-tunnel",
        )
    )


def _qdrant_collection_row(
    load_config: Callable[..., dict[str, str]],
    qdrant_factory: Callable[[], Any] | None,
    direct_path_blocked: bool,
) -> dict:
    if direct_path_blocked:
        return _row(
            "WARN",
            "qdrant:collection",
            DIRECT_PATH_BLOCKED_DETAIL,
            hint='run tailscale login or eval "$(recall-tunnel env)"',
        )
    try:
        cfg = load_config(need_write=False)
        q = qdrant_factory() if qdrant_factory else recall_api.Qdrant(cfg)
        if not q.healthz():
            return _row(
                "FAIL",
                "qdrant:collection",
                "healthz failed",
                hint="check QDRANT_URL and mesh reachability",
            )
        info = q.info()
        name = str(info.get("status", "?"))
        points = info.get("points_count", "?")
        return _row("OK", "qdrant:collection", f"{q.collection} status={name} points={points}")
    except FleetRagError as exc:
        return _row(
            "FAIL",
            "qdrant:collection",
            f"unreachable ({type(exc).__name__})",
            hint="verify QDRANT_URL, API key, and collection name",
        )
    except Exception as exc:  # noqa: BLE001
        return _row(
            "FAIL",
            "qdrant:collection",
            f"unreachable ({type(exc).__name__})",
            hint="verify QDRANT_URL, API key, and collection name",
        )


def _mirror_row(env: dict[str, str], now_s: float | None = None) -> dict:
    from . import mirror

    raw = (env.get(mirror.MIRROR_ENV) or "").strip()
    if not raw:
        return _row("SKIP", "mirror:freshness", "FLEET_RAG_MIRROR unset on this host")
    root = mirror.mirror_root(env)
    if root is None:
        return _row("SKIP", "mirror:freshness", "FLEET_RAG_MIRROR unset on this host")
    now_s = time.time() if now_s is None else now_s
    status = mirror.mirror_status(root, now_s, mirror.max_age_s(env))
    if status.fresh:
        age_h = (now_s - (status.at or int(now_s))) / 3600.0
        return _row("OK", "mirror:freshness", f".complete age_hours={age_h:.1f}")
    detail = f".complete {status.reason}"
    hint = "run the Mac mirror LaunchAgent or fleet-rag-mirror push; check FLEET_RAG_MIRROR path"
    return _row(
        "WARN" if status.reason in ("stale", "missing-stamp") else "FAIL",
        "mirror:freshness",
        detail,
        hint=hint,
    )


def _nightly_last_json_row(env: dict[str, str], now_s: float | None = None) -> dict:
    if not nightly_applies(env):
        return _row("SKIP", "nightly:last-json", "nightly job not configured on this host")
    from .nightly_job import summary_path

    path = summary_path(env)
    now_s = time.time() if now_s is None else now_s
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, ValueError, json.JSONDecodeError):
        return _row(
            "FAIL",
            "nightly:last-json",
            f"{path.name} absent or unreadable",
            hint="enable fleet-rag-nightly on the server and inspect journalctl -u fleet-rag-nightly",
        )
    finished = data.get("finished_at") or data.get("ended_at")
    if isinstance(finished, (int, float)):
        age_s = now_s - float(finished) / (1000.0 if finished > 1e12 else 1.0)
    else:
        age_s = now_s - mtime
    ok = bool(data.get("ok"))
    errors = data.get("errors") or []
    detail = f"age_hours={age_s / 3600:.1f} ok={'true' if ok else 'false'} errors={len(errors)}"
    if age_s > NIGHTLY_MAX_AGE_H:
        return _row(
            "FAIL",
            "nightly:last-json",
            f"{detail} (stale>{NIGHTLY_MAX_AGE_H // 3600}h)",
            hint="check fleet-rag-nightly.timer and ~/.local/state/fleet-rag-nightly/last.json",
        )
    if not ok:
        return _row(
            "FAIL",
            "nightly:last-json",
            detail,
            hint="read last.json and journalctl -u fleet-rag-nightly.service",
        )
    if errors:
        return _row("WARN", "nightly:last-json", detail, hint="review guard trips in last.json")
    return _row("OK", "nightly:last-json", detail)


def _install_row(root: pathlib.Path, rel: str, check: str) -> dict:
    path = root / rel
    if path.is_file():
        return _row("OK", check, f"{path.name} present")
    return _row(
        "FAIL",
        check,
        f"{rel} missing under install root",
        hint="run bash scripts/install-fleet-rag.sh from an AI-Fleet-Coordinator checkout",
    )


def _read_lock_pid(lock_path: pathlib.Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace").strip().split()
        return int(text[0]) if text else None
    except (OSError, ValueError):
        return None


def _ingest_lock_row(lock_path: pathlib.Path) -> dict:
    """Mirror ingest.IngestLock: flock decides whether a run is active; the file may linger."""
    if not lock_path.is_file():
        return _row("OK", "ingest:lock", "no lock file")
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError as exc:
        return _row(
            "FAIL",
            "ingest:lock",
            f"cannot open ({type(exc).__name__})",
            hint=f"fix permissions on {lock_path.name}",
        )
    held = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                held = True
            else:
                return _row(
                    "FAIL",
                    "ingest:lock",
                    f"flock probe failed ({type(exc).__name__})",
                    hint="check ingest.lock permissions",
                )
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if not held:
        return _row("OK", "ingest:lock", "unheld (lock file may remain after a run)")
    pid = _read_lock_pid(lock_path)
    if pid is not None and not _pid_alive(pid):
        return _row(
            "FAIL",
            "ingest:lock",
            f"stale flock (pid {pid} not running)",
            hint=f"rm {lock_path.name} after confirming no ingest is active",
        )
    detail = "held by another ingest"
    if pid is not None:
        detail += f" (pid {pid})"
    return _row("WARN", "ingest:lock", detail, hint="wait for ingest to finish")


def setup_report(
    home: pathlib.Path | str | None = None,
    env: dict[str, str] | None = None,
    load_config: Callable[..., dict[str, str]] | None = None,
    qdrant_factory: Callable[[], Any] | None = None,
    now_s: float | None = None,
    direct_path_blocked: bool = False,
) -> dict:
    """Fleet RAG setup health (`recall doctor`): token file, Infisical read, Qdrant, mirror,
    nightly summary, install layout, and ingest.lock.  Rows use OK / WARN / FAIL / SKIP; only FAIL
    fails the report.  Values are never printed."""
    home_path = pathlib.Path(home) if home else pathlib.Path(os.path.expanduser("~"))
    env_map = dict(os.environ if env is None else env)
    env_map.setdefault("HOME", str(home_path))
    loader = load_config or recall_api.load_config
    install_root = rag_install_root(home_path, env_map)
    lock_path = install_root / "state" / "ingest.lock"
    rows = [
        _token_file_row(env_map),
        _infisical_read_row(loader),
        _embedder_row(loader),
        _qdrant_collection_row(loader, qdrant_factory, direct_path_blocked),
        _mirror_row(env_map, now_s=now_s),
        _nightly_last_json_row(env_map, now_s=now_s),
        _install_row(install_root, INSTALL_BOARD, "install:linear-board"),
        _install_row(install_root, INSTALL_SECRETSCAN, "install:secretscan"),
        _ingest_lock_row(lock_path),
    ]
    return _finalize_rows(rows)


def format_platforms(report: dict) -> str:
    width = max((len(r["check"]) for r in report["rows"]), default=10)
    lines = [f"{'STATUS':<6} {'check':<{width}}  detail", f"{'------':<6} {'-' * width}  ------"]
    for r in report["rows"]:
        lines.append(f"{r['status']:<6} {r['check']:<{width}}  {r['detail']}")
    c = report["counts"]
    parts = [f"OK {c.get('OK', 0)}", f"WARN {c.get('WARN', 0)}", f"FAIL {c.get('FAIL', 0)}"]
    if c.get("SKIP"):
        parts.append(f"SKIP {c['SKIP']}")
    lines.append("")
    lines.append(f"overall: {'ok' if report['ok'] else 'PROBLEMS'}  ({', '.join(parts)})")
    return "\n".join(lines)
