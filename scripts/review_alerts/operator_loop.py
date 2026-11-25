"""Review loop hosted by the existing canonical Operator process.

No signer or ledger access crosses the service boundary. Receipts come through
relay's scoped service-read lane; binding uses this Operator's existing in-process
RegistryService and transactional store. No alternate registry is opened here.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time

from . import events, github_read, messages, service, wakes
from .operator_origin_bridge import RegistryOriginBinder
from .operator_pr_origin import Held


class ProtectedReceipts:
    def __init__(self, reader, repos):
        self.reader = reader
        self.repos = tuple(repos)
        # Conservative until every repo answers with an explicit complete
        # marker:  a missing or unrecognized marker is never complete, and
        # partial coverage never stops confirmed-row processing.
        #
        # Coverage belongs to a SCAN, not to the latest single read:  a scan
        # accumulates every census read it makes (enumeration and later
        # binder lookups alike), a complete read never repairs a partial one
        # within the same scan, and a failed or malformed read marks the
        # scan partial instead of leaving a prior complete standing.  Only
        # begin_scan() opens a fresh scan.
        self._scan_partial = False
        self.coverage = "partial"

    def begin_scan(self):
        """Open a new scan:  no failures proven yet, and the coverage word
        stays partial until a fully complete census proves it."""
        self._scan_partial = False
        self.coverage = "partial"

    def rows(self):
        out = []
        try:
            partial = False
            for repo in self.repos:
                answer = self.reader._read_full(dict(repo=repo, verb="creation_receipts"))
                if not isinstance(answer, dict):
                    raise Held("receipt-census-invalid")
                if answer.get("coverage") != "complete":
                    partial = True
                rows = answer.get("body")
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise Held("receipt-census-invalid")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or row.get("repo") != repo
                        or type(row.get("number")) is not int
                        or row.get("confirmed") is not True
                        or row.get("source") != "open_pr"
                    ):
                        raise Held("receipt-invalid")
                    out.append(row)
        except Exception:
            # A failed read never leaves an earlier complete standing for
            # the scan it happened in.
            self._scan_partial = True
            self.coverage = "partial"
            raise
        self._scan_partial = self._scan_partial or partial
        self.coverage = "partial" if self._scan_partial else "complete"
        return out

    def lookup(self, repo, number):
        rows = [r for r in self.rows() if r["repo"] == repo and r["number"] == number]
        return dict(rows[0]) if len(rows) == 1 else None


def start(env, registry, stop, log, *, operator_send=None, destination_for_origin=None):
    """Return no thread when not configured; active errors surface, never disable other wakes."""
    from .config import configured

    env = configured(env)
    if str(env.get("RA_ENABLED", "")).lower() not in ("1", "true", "yes"):
        return None
    if registry is None:
        raise Held("canonical-operator-registry-required")
    if not callable(getattr(registry, "_op_bind_pr", None)):
        raise Held("canonical-pr-binding-capability-required")
    repos = {s.strip() for s in str(env.get("RA_REPOS", "")).split(",") if s.strip()}
    seat_map = service.seat_map_from(env)
    platforms = json.loads(env.get("RA_PLATFORM_MAP_JSON") or "{}")
    state = str(env.get("RA_STATE_DIR") or "")
    if (
        not repos
        or not seat_map
        or not isinstance(platforms, dict)
        or not state
        or not os.path.isabs(state)
    ):
        raise Held("review-service-config-incomplete")
    if not callable(operator_send) or not callable(destination_for_origin):
        raise Held("review-service-message-callbacks-required")
    messenger = messages.OperatorMessages(operator_send, destination_for_origin)
    token_file = env.get("RA_SERVICE_TOKEN_FILE", "")
    wake_file = env.get("RA_WAKE_KEY_FILE", "")
    if not service._credential_from_file(token_file) or not service._credential_from_file(
        wake_file
    ):
        raise Held("review-service-credential-missing")

    class StopAwareRead(github_read.ServiceReadClient):
        def _read_full(self, payload):
            if stop.is_set():
                raise Held("operator-stopping")
            return super()._read_full(payload)

    reader = StopAwareRead(
        env.get("RA_RELAY_URL", "http://127.0.0.1:8471"),
        lambda: service._credential_from_file(token_file),
    )
    receipts = ProtectedReceipts(reader, repos)

    def registry_call(op, payload):
        # Existing service object supplies ownership checks and the one database.
        if stop.is_set():
            raise Held("operator-stopping")
        status, answer = getattr(registry, "_op_" + op.replace("-", "_"))("OPERATOR", payload)
        if status != 200:
            raise Held("registry-operation-refused")
        return answer

    binder = RegistryOriginBinder(receipts, registry_call, seat_map, platforms)

    class StopAwareRouter(wakes.WakeClient):
        def submit(self, *args, **kwargs):
            if stop.is_set():
                raise Held("operator-stopping")
            return super().submit(*args, **kwargs)

    router = StopAwareRouter(
        env.get("RA_ROUTER_URL", "http://127.0.0.1:8794"),
        lambda: service._credential_from_file(wake_file),
    )
    gate = str(env.get("RA_SELFTEST_SEAT") or "")
    if gate not in platforms:
        raise Held("review-service-gate-unconfigured")
    os.makedirs(state, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(state, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise Held("review-service-already-running")
    try:
        runner = service.AlertService(
            events.EventStore(os.path.join(state, "events.jsonl")),
            reader,
            router,
            "",
            seat_map,
            repos,
            gate_seat=gate,
            owner_provider=receipts.rows,
            coverage_provider=lambda: receipts.coverage,
            coverage_reset=receipts.begin_scan,
            origin_binder=binder.bind,
            cancelled=stop.is_set,
            message_poster=messenger.post,
        )
        interval = max(60, float(env.get("RA_INTERVAL_S", "900")))
        # Post-repair recovery selection:  explicit exact attempts or none.
        # A malformed entry is a configuration refusal, never a guess.
        recovery_selection = service._recovery_selection(env)
    except ValueError as exc:
        os.close(lock_fd)
        raise Held("review-service-recovery-selection-invalid") from exc
    except Exception:
        os.close(lock_fd)
        raise

    def loop():
        qualified = False
        try:
            while not stop.is_set():
                try:
                    if not qualified:
                        router.check_required_session_support(gate, probe_id=str(time.time_ns()))
                        qualified = True
                        if recovery_selection:
                            # Post-repair recovery:  one explicitly
                            # selected, bounded pass at process start,
                            # after the capability gate, before the first
                            # cycle, under the single-writer lock.  No
                            # selection, no pass.
                            log(
                                "review-alert-recovery",
                                **runner.recover_held(recovery_selection),
                            )
                    if stop.is_set():
                        break
                    counts = runner.cycle()
                    service._write_status(state, runner.status_snapshot())
                    log("review-alert-cycle", **counts)
                except Exception as exc:
                    log("review-alert-error", reason=type(exc).__name__)
                stop.wait(interval)
        finally:
            os.close(lock_fd)

    thread = threading.Thread(target=loop, name="review-alerts", daemon=True)
    try:
        thread.start()
    except Exception:
        os.close(lock_fd)
        raise
    return thread
