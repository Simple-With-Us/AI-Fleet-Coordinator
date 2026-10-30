"""Default-off Mac Muse bridge for local and server Agent-Sync inboxes.

This worker owns one decision ledger and the tested tool-less Muse runtime.  Server inboxes are
pulled through the existing ``coolify`` SSH alias; decisions return through a fixed server-local
command.  The server alone can invoke its configured routine.  The Mac never reads server keys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import adapters as A
from . import chat_catalog as CC
from . import config as C
from . import live as L
from . import muse_bridge as MB
from . import wakes as W

BRIDGE_VERSION = "muse-inbox-bridge-v2"
CONTRACT = Path(__file__).parent / "wake" / "muse-bridge-contract.md"
STATE_NAME = "muse-bridge-mac.json"
PIN_NAME = "muse-bridge-pin.json"
CANDIDATE_NAME = "muse-bridge-pin-candidate.json"
MAX_REMOTE_OUTPUT = 2_000_000
REMOTE_ALIAS = "coolify"


def _remote_command(remote_app: str) -> str:
    """The fixed server-local command, with the operator's own application id.

    The production application is private deployment inventory, so it comes
    from the operator's config and never from this source.
    """
    return (
        "set -eu; c=$(docker ps --filter name=" + remote_app + " --format '{{.Names}}'); "
        'test -n "$c"; test "$(printf \'%s\\n\' "$c" | wc -l)" -eq 1; '
        'docker exec -i -w /app/scripts "$c" python3 -m agent_sync.muse_bridge '
    )


class DeliveryRefused(ValueError):
    """A recorded decision can no longer be delivered for this message (stale, paused, loop guard,
    destination gone, or a server refusal).  Terminal for the item, unlike a transport failure."""


def resolve_bridge_muse(wake_path: str) -> str | None:
    """The real versioned Muse binary for the bridge pin, never the launcher shim.

    The bridge pin binds one versioned build so an upgrade holds new events
    until a fresh canary is pinned.  Falls back to the ordinary resolver when
    no versioned build is on the wake PATH.
    """
    found = A.newest_versioned_muse(wake_path)
    if found:
        return found
    return A.resolve_muse(None, wake_path)


def fingerprint() -> str | None:
    """Separate contract pin: deploying this bridge never invalidates CLAUDE's live Muse pin."""
    try:
        contract = CONTRACT.read_bytes()
        contract.decode("utf-8")
        schema = W.schema_text(strict=True)  # the schema the bridge's muse runs actually send
    except (OSError, UnicodeError, ValueError):
        return None
    data = json.dumps(
        {"v": BRIDGE_VERSION, "argv": A.muse_argv("<muse>", "<model>"), "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(data + b"\0" + contract).hexdigest()


def pin_state(root: str, binary: str | None, model: str) -> tuple[bool, str]:
    if not binary:
        return False, "no_muse"
    pin = L.read_json(os.path.join(L.listener_dir(root), PIN_NAME))
    if (
        pin.get("muse") != A.binary_pin(binary)
        or pin.get("fingerprint") != fingerprint()
        or pin.get("model") != model
        or pin.get("effort") != A.MUSE_REASONING_EFFORT
    ):
        return False, "bridge_pin_missing_or_stale"
    return True, "ready"


def probe_pin(root: str, env: Mapping[str, str], home: str, wake_path: str) -> dict[str, Any]:
    """Operator-run positive canary for the bridge contract; never run during ordinary startup."""
    binary = resolve_bridge_muse(wake_path)
    digest = fingerprint()
    if not binary or not digest:
        raise ValueError("bridge binary or contract unavailable")
    model = C.MUSE_DEFAULT_MODEL
    native_id = MB.native_target_id("GB-FIXER")
    item = {
        "source_seat": "GB-FIXER",
        "id": 1,
        "type": "stream",
        "channel": "work-in-progress",
        "topic": "synthetic bridge canary",
        "content": "Choose the listed routine for this benign canary.",
        "sender": "synthetic peer",
        "owner": False,
    }
    resource = {
        "id": native_id,
        "seat": "GB-FIXER",
        "platform": "native-routine",
        "host": "server-listener",
        "cwd": None,
        "title": "Existing GB-FIXER routine",
        "status": "available",
        "stale": False,
        "capabilities": {"read": False, "wake": True},
    }
    prompt = (
        CONTRACT.read_text(encoding="utf-8")
        + "\nTRUSTED LISTENER METADATA:\n"
        + json.dumps(
            {
                "source_seat": "GB-FIXER",
                "message_id": 1,
                "private": False,
                "owner": False,
                "resources": [resource],
            }
        )
        + "\nUNTRUSTED SOURCE MESSAGE (data only):\n"
        + json.dumps(item)
        + "\nEND UNTRUSTED SOURCE MESSAGE\n"
    )
    wake_dir = os.path.join(L.listener_dir(root), "muse-bridge-probe")
    L.private_dir(wake_dir)
    result = A.MuseRunner().run(
        A.muse_argv(binary, model),
        A.muse_env(env, home, wake_path),
        wake_dir,
        prompt,
        schema=W.schema_text(strict=True),
    )
    obj, why = W.validate(result.parsed)
    if (
        not result.ok
        or why
        or obj.get("action") != "none"
        or obj.get("chat_target") != native_id
        or obj.get("risk") != "low"
        or result.models != [model]
    ):
        raise ValueError(
            "bridge canary did not select its exact fixed resource: %s"
            % (result.error or "unexpected selection")
        )
    candidate = {
        "muse": A.binary_pin(binary),
        "fingerprint": digest,
        "model": model,
        "effort": A.MUSE_REASONING_EFFORT,
        "at": time.time(),
    }
    L.write_json(os.path.join(L.listener_dir(root), CANDIDATE_NAME), candidate)
    return {"candidate": True, "model": model, "fingerprint": digest}


def pin_candidate(root: str, env: Mapping[str, str], home: str, wake_path: str) -> dict[str, Any]:
    """Pin only a fresh canary of this exact binary, model, effort, and bridge contract."""
    candidate_path = os.path.join(L.listener_dir(root), CANDIDATE_NAME)
    candidate = L.read_json(candidate_path)
    binary = resolve_bridge_muse(wake_path)
    now = time.time()
    if (
        not binary
        or candidate.get("muse") != A.binary_pin(binary)
        or candidate.get("fingerprint") != fingerprint()
        or candidate.get("model") != C.MUSE_DEFAULT_MODEL
        or candidate.get("effort") != A.MUSE_REASONING_EFFORT
        or not isinstance(candidate.get("at"), (int, float))
        or not 0 <= now - candidate["at"] <= 86400
    ):
        raise ValueError("fresh bridge canary for this runtime is required")
    L.write_json(os.path.join(L.listener_dir(root), PIN_NAME), candidate)
    os.unlink(candidate_path)
    return {"pinned": True, "model": candidate["model"], "fingerprint": candidate["fingerprint"]}


class SSHExchange:
    """Two fixed remote commands; no inbox data or model value is interpolated into a shell."""

    def __init__(self, remote_app: str = "") -> None:
        self.remote_app = remote_app.strip()

    def request(self, command: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if command not in ("export", "submit"):
            raise ValueError("invalid bridge command")
        if not self.remote_app:
            raise RuntimeError(
                "the production application is not configured: set muse_bridge.remote_app "
                "in the operator's own listener config"
            )
        data = json.dumps(body, separators=(",", ":")).encode()
        if len(data) > MB.MAX_INPUT_BYTES:
            raise ValueError("bridge request too large")
        proc = subprocess.run(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                REMOTE_ALIAS,
                _remote_command(self.remote_app) + command,
            ],
            input=data,
            capture_output=True,
            timeout=25,
            check=False,
        )
        if proc.returncode != 0 or len(proc.stdout) > MAX_REMOTE_OUTPUT:
            raise RuntimeError("server bridge exchange unavailable")
        answer = json.loads(proc.stdout)
        if not isinstance(answer, dict):
            raise ValueError("invalid server bridge response")
        return answer


class MacBridge:
    """One serial model decision per source message with durable source-qualified receipts."""

    def __init__(self, daemon: Any, exchange: SSHExchange | None = None) -> None:
        self.daemon = daemon
        self.exchange = exchange or SSHExchange(
            getattr(getattr(daemon, "config", None), "muse_bridge_remote_app", "")
        )
        self.path = os.path.join(L.listener_dir(daemon.root), STATE_NAME)
        self.catalog = CC.ChatCatalog(home=daemon.home, scrub=daemon.scrub, max_records=120)

    def _state(self) -> dict[str, Any]:
        row = L.read_json(self.path)
        if not isinstance(row, dict) or (
            os.path.exists(self.path)
            and (
                not isinstance(row.get("cursors"), dict)
                or not isinstance(row.get("decisions"), dict)
            )
        ):
            raise ValueError("Mac bridge ledger is unreadable")
        return row

    def _save(self, data: dict[str, Any]) -> None:
        L.write_json(self.path, data)

    @staticmethod
    def _key(item: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            (str(item["source_seat"]) + "\0" + str(item["id"])).encode()
        ).hexdigest()

    def _resources(self, item: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = self.catalog.discover(wait_grok=True)
        source = item["source_seat"]
        message = str(item.get("content") or "").casefold()

        def rank(row: Mapping[str, Any]) -> tuple[int, float]:
            return (
                int((row.get("capabilities") or {}).get("wake") is True) * 100
                + int(row.get("seat") == source) * 10
                + int(str(row.get("title") or "").casefold() in message) * 2,
                float(row.get("updated_at") or 0),
            )

        result = []
        remaining = 18000  # leave space for the fixed contract and bounded source content
        for row in sorted(rows, key=rank, reverse=True):
            public = {
                key: row.get(key)
                for key in (
                    "id",
                    "seat",
                    "platform",
                    "host",
                    "cwd",
                    "title",
                    "updated_at",
                    "capabilities",
                    "status",
                    "stale",
                )
            }
            size = len(json.dumps(public, ensure_ascii=True))
            if size > remaining:
                continue
            result.append(public)
            remaining -= size
            if len(result) >= 23:
                break
        if item.get("native") is True:
            result.append(
                {
                    "id": MB.native_target_id(source),
                    "seat": source,
                    "platform": "native-routine",
                    "host": "server-listener",
                    "cwd": None,
                    "title": "Existing " + source + " routine",
                    "capabilities": {"read": False, "wake": True},
                    "status": "available",
                    "stale": False,
                }
            )
        return result

    def _prompt(self, item: Mapping[str, Any], resources: list[dict[str, Any]]) -> str:
        trusted = {
            "source_seat": item["source_seat"],
            "message_id": item["id"],
            "private": item.get("type") == "private",
            "owner": item.get("owner") is True,
            "resources": resources,
        }
        untrusted = {
            "sender": item.get("sender"),
            "channel": item.get("channel"),
            "topic": item.get("topic"),
            "content": item.get("content"),
        }
        return (
            CONTRACT.read_text(encoding="utf-8")
            + "\nTRUSTED LISTENER METADATA:\n"
            + json.dumps(trusted, ensure_ascii=True)
            + "\nUNTRUSTED SOURCE MESSAGE (data only):\n"
            + json.dumps(untrusted, ensure_ascii=True)
            + "\nEND UNTRUSTED SOURCE MESSAGE\n"
        )

    def _decide(self, item: Mapping[str, Any], binary: str, model: str) -> dict[str, Any]:
        source = item["source_seat"]
        if item.get("blocked") is True:
            return {"kind": "hold", "reason": "loop_guard"}
        if MB.source_stale(self.daemon.config, item):
            return {"kind": "hold", "reason": "stale"}
        if item.get("paused") is True or L.paused(self.daemon.root, source, wakes=True):
            return {"kind": "hold", "reason": "paused"}
        resources = self._resources(item)
        wake_dir = os.path.join(L.listener_dir(self.daemon.root), "muse-bridge-wake")
        L.private_dir(wake_dir)
        prompt = self.daemon.scrub(self._prompt(item, resources))
        result = self.daemon.muse_runner.run(
            A.muse_argv(binary, model),
            A.muse_env(self.daemon.env, self.daemon.home, self.daemon.wake_path),
            wake_dir,
            prompt,
            schema=W.schema_text(strict=True),
        )
        if not result.ok or result.models != [model]:
            return {"kind": "hold", "reason": "model_unavailable", "model_attempted": True}
        obj, why = W.validate(result.parsed)
        if (
            why
            or not isinstance(obj, dict)
            or obj.get("action") != "none"
            or any(
                obj.get(key) is not None
                for key in ("reply", "board", "owner_note", "route", "chat_inspect")
            )
        ):
            return {"kind": "hold", "reason": "invalid_decision", "model_attempted": True}
        target_id = obj.get("chat_target")
        if target_id is None:
            return {"kind": "hold", "reason": "no_target", "model_attempted": True}
        offered = next((row for row in resources if row.get("id") == target_id), None)
        if (
            not offered
            or offered.get("stale")
            or not (offered.get("capabilities") or {}).get("wake")
            or obj.get("risk") in ("high", "uncertain")
            or (not item.get("owner") and obj.get("risk") != "low")
            or (item.get("type") == "private" and offered.get("seat") != source)
        ):
            return {"kind": "hold", "reason": "selection_refused", "model_attempted": True}
        if offered.get("platform") == "native-routine":
            return {"kind": "native", "target": target_id, "model_attempted": True}
        try:
            self.catalog.discover(force=True, wait_grok=True)
            fresh = self.catalog.resolve(target_id)
        except Exception:
            fresh = None
        if (
            not fresh
            or fresh.get("stale")
            or not (fresh.get("capabilities") or {}).get("wake")
            or any(fresh.get(k) != offered.get(k) for k in ("seat", "platform", "host", "cwd"))
            or fresh.get("seat") not in ("GROK", "CURSOR", "CODEX")
            or (fresh.get("seat") == "CODEX" and fresh.get("platform") != "codex")
            or fresh.get("seat") not in self.daemon.seats
            or not self.daemon.seats[fresh["seat"]].cfg.enabled
        ):
            return {"kind": "hold", "reason": "destination_changed", "model_attempted": True}
        return {
            "kind": "chat",
            "target_id": target_id,
            "target": {k: fresh[k] for k in ("platform", "host", "session_id", "cwd")},
            "destination_seat": fresh["seat"],
            "model_attempted": True,
        }

    @staticmethod
    def _budget_reason(
        item: Mapping[str, Any], budget: Mapping[str, Any], decisions: Mapping[str, Any]
    ) -> str | None:
        if not isinstance(budget, dict) or any(
            k not in budget
            or isinstance(budget[k], bool)
            or not isinstance(budget[k], (int, float))
            or budget[k] < 0
            for k in C.BUDGET_DEFAULTS
        ):
            return "budget_unavailable"
        views = {}
        for key, row in decisions.items():
            if (
                not isinstance(row, dict)
                or row.get("source_seat") != item["source_seat"]
                or row.get("model_attempted") is not True
            ):
                continue
            views[key] = {
                "state": "done",
                "owner": row.get("owner") is True,
                "topic_key": row.get("topic_key"),
                "times": {"started": row.get("at")},
            }
        return W.budget_block(
            views,
            budget,
            owner=item.get("owner") is True,
            key=L.topic_key(str(item.get("channel") or ""), str(item.get("topic") or "")),
            now=time.time(),
            run_max=0.0,
        )

    def _deliver(
        self, item: Mapping[str, Any], decision: Mapping[str, Any], *, remote: bool
    ) -> None:
        kind = decision["kind"]
        if not remote and not MB.enabled(self.daemon.config, item["source_seat"]):
            raise DeliveryRefused("local source seat is no longer bridge enabled")
        # The item's blocked and paused flags are fresh from this export, so a decision recorded on
        # an earlier pass is refused here instead of being sent into a server refusal.
        if kind != "hold" and (
            item.get("blocked") is True
            or item.get("paused") is True
            or MB.source_stale(self.daemon.config, item)
            or L.paused(self.daemon.root, item["source_seat"], wakes=True)
        ):
            raise DeliveryRefused("source message is no longer eligible for delivery")
        if kind == "chat":
            # resolve() already refreshes from its bounded cache, so a known target needs no
            # Grok-blocking rescan; only a cache miss forces one before resolving again.
            fresh = self.catalog.resolve(decision.get("target_id"))
            if fresh is None:
                self.catalog.discover(force=True, wait_grok=True)
                fresh = self.catalog.resolve(decision.get("target_id"))
            if (
                not fresh
                or fresh.get("stale")
                or not (fresh.get("capabilities") or {}).get("wake")
                or fresh.get("seat") != decision.get("destination_seat")
                or fresh.get("seat") not in ("GROK", "CURSOR", "CODEX")
                or (fresh.get("seat") == "CODEX" and fresh.get("platform") != "codex")
                or fresh.get("seat") not in self.daemon.seats
                or not self.daemon.seats[fresh["seat"]].cfg.enabled
                or L.paused(self.daemon.root, fresh["seat"], wakes=True)
                or any(
                    fresh.get(k) != (decision.get("target") or {}).get(k)
                    for k in ("platform", "host", "session_id", "cwd")
                )
                or (item.get("type") == "private" and fresh.get("seat") != item["source_seat"])
                or L.paused(self.daemon.root, item["source_seat"], wakes=True)
            ):
                raise DeliveryRefused("selected chat is no longer an authorized destination")
        if remote:
            receipt = self.exchange.request(
                "submit",
                {
                    "source_seat": item["source_seat"],
                    "id": item["id"],
                    "seq": item["seq"],
                    "kind": kind,
                },
            )
            if receipt.get("refused") is not None:
                raise DeliveryRefused(
                    "server refused the decision: %s" % str(receipt["refused"])[:160]
                )
            if not isinstance(receipt.get("receipt"), str):
                raise ValueError("server did not return a durable receipt")
        elif kind == "native":
            raise DeliveryRefused("local native routine needs its own source runner")
        if kind == "chat":
            binding = dict(decision["target"])
            binding.update(
                channel=str(item.get("channel") or "direct message"),
                topic=str(item.get("topic") or "message %s" % item["id"]),
            )
            self.daemon.chat_queue(decision["destination_seat"]).enqueue_target(
                binding,
                item["id"],
                self.daemon.clock.time(),
                owner=item.get("owner") is True,
                stale=item.get("stale") is True,
                source_seat=item["source_seat"],
            )

    def _process_envelope(
        self,
        state: dict[str, Any],
        cursors: dict[str, Any],
        decisions: dict[str, Any],
        seat: str,
        envelope: Mapping[str, Any],
        *,
        remote: bool,
        binary: str,
        model: str,
    ) -> None:
        if envelope.get("gap"):
            self.daemon.log.write("muse-bridge-gap", seat=seat, oldest=envelope.get("oldest"))
            return
        for item in envelope.get("items", []):
            # A malformed item is skipped and reported, never retried:  raising here would leave
            # the cursor behind, and every later tick would re-export the same bad batch.
            if (
                not isinstance(item, dict)
                or item.get("source_seat") != seat
                or not isinstance(item.get("id"), int)
                or not isinstance(item.get("seq"), int)
            ):
                self.daemon.log.write(
                    "muse-bridge-bad-item", seat=seat, error="invalid source-qualified bridge item"
                )
                continue
            key = self._key(item)
            decision = decisions.get(key)
            if decision is None:
                budget_reason = self._budget_reason(item, envelope.get("budget"), decisions)
                if budget_reason:
                    decision = {"kind": "hold", "reason": budget_reason}
                else:
                    # Claim before model execution.  An interrupted call is held for review
                    # instead of spending another turn or fanning out on restart.
                    decisions[key] = {
                        "kind": "hold",
                        "reason": "model_interrupted",
                        "model_attempted": True,
                        "at": time.time(),
                        "source_seat": seat,
                        "id": item["id"],
                        "owner": item.get("owner") is True,
                        "topic_key": L.topic_key(
                            str(item.get("channel") or ""), str(item.get("topic") or "")
                        ),
                    }
                    self._save(state)
                    decision = self._decide(item, binary, model)
                decisions[key] = dict(
                    decision,
                    at=time.time(),
                    source_seat=seat,
                    id=item["id"],
                    owner=item.get("owner") is True,
                    topic_key=L.topic_key(
                        str(item.get("channel") or ""), str(item.get("topic") or "")
                    ),
                )
                self._save(state)  # persist before any remote or chat delivery
                decision = decisions[key]  # the stored row keeps the budget fields
            if decision.get("delivery") in ("done", "refused"):
                continue  # settled on an earlier pass:  never delivered twice, never delivered late
            try:
                self._deliver(item, decision, remote=remote)
            except DeliveryRefused as exc:
                # Terminal for this message.  Record it and move on so one item cannot hold the
                # cursor forever.  A transport failure still propagates and retries the batch;
                # server submit and the chat queue are both idempotent for a settled item.
                decisions[key] = dict(
                    decisions[key], delivery="refused", refused_reason=str(exc)[:200]
                )
                self._save(state)
                self.daemon.log.write(
                    "muse-bridge-delivery-refused", seat=seat, id=item["id"], kind=decision["kind"]
                )
                continue
            decisions[key] = dict(decisions[key], delivery="done")
            self._save(state)
            self.daemon.log.write(
                "muse-bridge-decision", seat=seat, id=item["id"], kind=decision["kind"]
            )
        cursors[seat] = envelope["next"]
        self._save(state)

    def run_once(self) -> None:
        cfg = self.daemon.config
        if cfg.instance != "mac" or not cfg.muse_bridge_enabled:
            return
        model = C.MUSE_DEFAULT_MODEL
        binary = resolve_bridge_muse(self.daemon.wake_path)
        pinned, why = pin_state(self.daemon.root, binary, model)
        if not pinned:
            if getattr(self, "_last_pin_reason", None) != why:
                self.daemon.log.write("muse-bridge-held", reason=why)
                self._last_pin_reason = why
            return
        self._last_pin_reason = None
        state = self._state()
        cursors = state.setdefault("cursors", {})
        decisions = state.setdefault("decisions", {})
        local = MB.export_local(self.daemon.root, cfg, cursors)
        for seat, envelope in local["seats"].items():
            try:
                self._process_envelope(
                    state,
                    cursors,
                    decisions,
                    seat,
                    envelope,
                    remote=False,
                    binary=binary,
                    model=model,
                )
            except Exception as exc:
                self.daemon.log.write(
                    "muse-bridge-source-error", seat=seat, error=type(exc).__name__
                )
        if cfg.muse_bridge_remote_seats:
            remote = self.exchange.request(
                "export",
                {"cursors": {seat: cursors.get(seat, 0) for seat in cfg.muse_bridge_remote_seats}},
            )
            seats = remote.get("seats")
            if not isinstance(seats, dict) or set(seats) != set(cfg.muse_bridge_remote_seats):
                raise ValueError("server bridge seat set changed")
            for seat, envelope in seats.items():
                try:
                    self._process_envelope(
                        state,
                        cursors,
                        decisions,
                        seat,
                        envelope,
                        remote=True,
                        binary=binary,
                        model=model,
                    )
                except Exception as exc:
                    self.daemon.log.write(
                        "muse-bridge-source-error", seat=seat, error=type(exc).__name__
                    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac Muse bridge runtime verification")
    parser.add_argument("command", choices=("status", "probe", "pin"))
    args = parser.parse_args(argv)
    env = os.environ
    root = L.state_root(env)
    home = env.get("HOME") or os.path.expanduser("~")
    wake_path = A.default_wake_path(home)
    if args.command == "probe":
        result = probe_pin(root, env, home, wake_path)
    elif args.command == "pin":
        result = pin_candidate(root, env, home, wake_path)
    else:
        result = dict(
            zip(
                ("pinned", "reason"),
                pin_state(root, resolve_bridge_muse(wake_path), C.MUSE_DEFAULT_MODEL),
            )
        )
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
