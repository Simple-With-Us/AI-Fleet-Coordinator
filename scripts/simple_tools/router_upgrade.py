"""Guarded upgrade support for the existing user-unit installer. No daemon or replay."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

READONLY_BOOTSTRAP = 'import sys,os\ndef readonly_audit(event, args):\n    if event == "open":\n        _path, mode, flags = args\n        writing = (\n            bool(\n                flags\n                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)\n            )\n            if isinstance(flags, int)\n            else any(c in str(mode) for c in "wax+")\n        )\n        if writing:\n            raise RuntimeError("candidate attempted write outside isolated state")\n    if event in (\n        "os.remove",\n        "os.rename",\n        "os.rmdir",\n        "os.mkdir",\n        "os.chmod",\n        "os.chown",\n        "os.link",\n        "os.symlink",\n        "os.truncate",\n        "subprocess.Popen",\n        "os.system",\n        "os.exec",\n        "os.posix_spawn",\n    ):\n        raise RuntimeError("readonly candidate attempted mutation or subprocess")\n\nsys.dont_write_bytecode = True\nsys.addaudithook(readonly_audit)\n'


class UpgradeRefused(RuntimeError):
    pass


def _json(path):
    if path.is_symlink() or not path.is_file():
        raise UpgradeRefused("non-regular custody record")
    try:
        return json.loads(path.read_bytes())
    except (ValueError, OSError):
        raise UpgradeRefused("unreadable custody record") from None


def census(roots):
    """Complete raw queue/receipt/log inventory. No replay, pruning or repairs."""
    result = {"pending": {}, "receipts": {}, "logs": {}, "files": {}}
    for seat, raw in sorted(roots.items()):
        root = Path(raw)
        if root.is_symlink() or not root.is_dir():
            raise UpgradeRefused("missing or redirected seat state")
        for folder in ("pending", "done"):
            directory = root / folder
            if directory.is_symlink():
                raise UpgradeRefused("redirected custody directory")
            for path in sorted(directory.iterdir()) if directory.exists() else []:
                value = _json(path)
                if not path.name.endswith(".json") or not isinstance(value, dict):
                    raise UpgradeRefused("unknown custody record")
                key = seat + "/" + path.stem
                result["files"][seat + "/" + folder + "/" + path.name] = value
                if folder == "pending":
                    from simple_tools.wake_router import _loadable

                    if not _loadable(value, path.stem):
                        raise UpgradeRefused("unloadable pending record")
                    if value.get("in_flight") or value.get("running"):
                        raise UpgradeRefused("unsettled current delivery")
                    result["pending"][key] = value
                    receipt = value.get("receipt")
                    if receipt is None:
                        continue
                else:
                    receipt = value
                _record(result["receipts"], key, receipt)
        log = root / "wakes.jsonl"
        if log.exists():
            if log.is_symlink() or not log.is_file():
                raise UpgradeRefused("redirected receipt log")
            try:
                rows = [json.loads(line) for line in log.read_text().splitlines() if line]
            except (ValueError, OSError):
                raise UpgradeRefused("unreadable receipt log") from None
            result["logs"][seat] = rows
            for receipt in rows:
                if not isinstance(receipt, dict) or not isinstance(receipt.get("wake_id"), str):
                    raise UpgradeRefused("malformed receipt log")
                if receipt.get("test") is not True:
                    _record(result["receipts"], seat + "/" + receipt["wake_id"], receipt)
        else:
            result["logs"][seat] = []
    return result


def _record(records, key, receipt):
    if (
        not isinstance(receipt, dict)
        or receipt.get("wake_id") != key.split("/", 1)[1]
        or "delivered" not in receipt
        or (receipt["delivered"] is not None and type(receipt["delivered"]) is not bool)
        or not isinstance(receipt.get("status"), str)
    ):
        raise UpgradeRefused("receipt identity or shape mismatch")
    if key in records and records[key] != receipt:
        raise UpgradeRefused("conflicting receipt custody")
    records[key] = receipt


def uncertainty(records):
    """Baseline-relative evidence, never a request to rewrite a terminal receipt.

    A delivered/null/unknown handover is not native confirmation. An identical
    historical receipt is allowed in a later verified baseline; only a new or
    changed uncertain receipt refuses the current attempt. It will not gain a
    later acknowledgement here. After a pre-activation stopped refusal, preserve
    custody and explicitly recover the verified old service before another
    qualification. After activation was requested, candidate effects may exist:
    preserve final custody and require an explicit verified recovery decision,
    never an automatic rollback. Never replay, delete, or relabel a receipt as
    confirmed.
    """
    return {
        key: value
        for key, value in records["receipts"].items()
        if value.get("delivered") is None
        or value.get("status") == "unknown"
        or "interrupt" in str(value.get("code", ""))
    }


def compare(before, after):
    for key, value in before["receipts"].items():
        if after["receipts"].get(key) != value:
            raise UpgradeRefused("settled receipt disappeared or changed")
    for seat, rows in before["logs"].items():
        if after["logs"].get(seat, [])[: len(rows)] != rows:
            raise UpgradeRefused("receipt history disappeared or changed")
    for key, value in before["pending"].items():
        pending = after["pending"].get(key)
        if pending is not None:
            if any(pending.get(field) != value.get(field) for field in ("wake", "receipt_id")):
                raise UpgradeRefused("pending wake identity changed")
        elif key not in after["receipts"]:
            raise UpgradeRefused("unclaimed pending wake disappeared")
    old, new = uncertainty(before), uncertainty(after)
    if any(old.get(key) != value for key, value in new.items()):
        raise UpgradeRefused(
            "new delivery uncertainty; intake stays stopped, decide recovery, never blind replay"
        )


def tree_manifest(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise UpgradeRefused("release is missing or redirected")
    result = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise UpgradeRefused("release contains nonregular path")
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def state_paths(scripts, env):
    """Use the registered builders' actual state locators, without settings or workers."""
    import sys

    sys.path.insert(0, str(scripts))
    from simple_tools import wake_service as service

    result = {}
    for seat, builder in service.BUILDERS.items():
        module = importlib.import_module(builder.module)
        result[seat] = str(service._state(module, env, seat))
    return result


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{int(port)}/health", timeout=10) as response:
        if response.status != 200:
            raise UpgradeRefused("candidate health refused")
        return json.load(response)


def stopped(host, unit, proc_root):
    code, previous_pid, _ = host.systemctl("show", "--property=MainPID", "--value", unit)
    if code or not previous_pid.strip().isdigit():
        raise UpgradeRefused("cannot establish prior unit process")
    if host.systemctl("stop", unit)[0] != 0:
        raise UpgradeRefused("unit stop failed")
    code, pid, _ = host.systemctl("show", "--property=MainPID", "--value", unit)
    code2, status, _ = host.systemctl("is-active", unit)
    if (
        code
        or pid.strip() != "0"
        or code2 not in (3, 4)
        or status.strip() not in ("inactive", "failed")
    ):
        raise UpgradeRefused("unit not confirmed stopped")
    if previous_pid.strip() != "0" and (proc_root / previous_pid.strip()).exists():
        raise UpgradeRefused("previous unit process still exists")


class ControlledUpgrade:
    def __init__(self, host, base, unit_dir, proc_root, scripts, interpreter, port):
        self.host, self.base, self.proc_root, self.port = host, base, proc_root, port
        self.router, self.front = "afc-wake-router.service", "afc-wake-tls.service"
        self.current = os.readlink(base / "current")
        if not re.fullmatch(r"releases/[0-9a-f]{40}", self.current):
            raise UpgradeRefused("no immutable installed release")
        self.candidate_manifest = tree_manifest(scripts)
        self.old_manifest = tree_manifest(base / self.current / "scripts")
        if not self.old_manifest or not self.candidate_manifest:
            raise UpgradeRefused("empty release manifest")
        self.units = {name: (unit_dir / name).read_bytes() for name in (self.router, self.front)}
        code, pid, _ = host.systemctl("show", "--property=MainPID", "--value", self.router)
        if code or not re.fullmatch(r"[1-9][0-9]*", pid.strip()):
            raise UpgradeRefused("controlled upgrade requires running router")
        process = proc_root / pid.strip()
        argv = process.joinpath("cmdline").read_bytes().split(b"\0")
        expected = str(base / self.current / "scripts" / "simple-chat").encode()
        if expected not in argv:
            raise UpgradeRefused("running release does not match custody")
        env = dict(
            item.decode().split("=", 1)
            for item in process.joinpath("environ").read_bytes().split(b"\0")
            if b"=" in item
        )
        code = (
            READONLY_BOOTSTRAP
            + "\nimport sys,json,importlib;sys.path.insert(0,sys.argv[1]);from simple_tools import wake_service as s;e=json.load(sys.stdin);print(json.dumps({k:str(s._state(importlib.import_module(v.module),e,k)) for k,v in s.BUILDERS.items()}))"
        )
        mappings = []
        for source in (base / self.current / "scripts", scripts):
            status, output, _ = host.run(
                [interpreter, "-I", "-c", code, str(source)], stdin=json.dumps(env)
            )
            if status:
                raise UpgradeRefused("seat state locator failed")
            mappings.append(json.loads(output))
        if mappings[0] != mappings[1] or not mappings[0]:
            raise UpgradeRefused("candidate changes configured seat state locations")
        self.roots = mappings[0]
        if any(not Path(value).is_absolute() for value in self.roots.values()):
            raise UpgradeRefused("relative state location")
        self.before = census(self.roots)
        health(port)
        env["WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE"] = str(
            Path(env["HOME"]) / ".config/review-alerts/wake.key"
        )
        status, output, _ = host.run(
            [
                interpreter,
                "-I",
                "-c",
                READONLY_BOOTSTRAP
                + "\nimport sys,json,os;sys.path.insert(0,sys.argv[1]);from simple_tools.router_upgrade import qualify_candidate;print(json.dumps(qualify_candidate(json.load(sys.stdin))))",
                str(scripts),
            ],
            stdin=json.dumps(env),
        )
        if status:
            raise UpgradeRefused("non-delivering configured candidate startup canary failed")
        self.qualified = json.loads(output.strip().splitlines()[-1])
        if self.qualified.get("qualified") is not True:
            raise UpgradeRefused("candidate did not qualify")
        self.scripts = scripts
        self.unit_dir = unit_dir
        self.env = env
        compare(self.before, census(self.roots))
        self.snapshot = base / ("upgrade-custody-" + str(time.time_ns()))

    def quiesce(self):
        # Preserve unit/release/state-location custody before any outage.
        self.snapshot.mkdir(mode=0o700)
        # Persist the snapshot directory link before any possible outage.
        parent = os.open(self.snapshot.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        for name, value in self.units.items():
            durable_write(self.snapshot / name, value, 0o600)
        path = self.snapshot / "custody.json"
        durable_write(
            path,
            json.dumps(
                {
                    "release": self.current,
                    "states": self.roots,
                    "baseline": self.before,
                    "old_manifest": self.old_manifest,
                    "candidate_manifest": self.candidate_manifest,
                    "qualification": self.qualified,
                }
            ),
            0o600,
        )
        self.verify_old()
        compare(self.before, census(self.roots))
        self.stop_attempted = True
        stopped(self.host, self.front, self.proc_root)
        stopped(self.host, self.router, self.proc_root)
        self.after = census(self.roots)
        compare(self.before, self.after)
        after_path = self.snapshot / "stopped-census.json"
        durable_write(after_path, json.dumps(self.after), 0o600)

    def verify_old(self):
        if (
            os.readlink(self.base / "current") != self.current
            or tree_manifest(self.base / self.current / "scripts") != self.old_manifest
        ):
            raise UpgradeRefused("installed release changed")
        if any((self.unit_dir / name).read_bytes() != data for name, data in self.units.items()):
            raise UpgradeRefused("installed unit changed")

    def recover_stopped(self):
        # No automatic replay or rollback once delivery custody is in doubt.
        failures = []
        for unit in (self.front, self.router):
            try:
                stopped(self.host, unit, self.proc_root)
            except (UpgradeRefused, OSError):
                failures.append(unit)
        final = None
        try:
            final = census(self.roots)
            if self.snapshot.exists():
                durable_write(
                    self.snapshot / "recovery-census.json",
                    json.dumps(
                        {
                            "phase": "post-activation"
                            if getattr(self, "activation_requested", False)
                            else "pre-activation",
                            "census": final,
                            "unverified_units": failures,
                        }
                    ),
                    0o600,
                )
        except (UpgradeRefused, OSError):
            failures.append("custody-census")
        if failures:
            raise UpgradeRefused("recovery stop or custody unverified for " + ",".join(failures))
        if hasattr(self, "after"):
            compare(self.after, final)
        if getattr(self, "activation_requested", False):
            raise UpgradeRefused(
                "post-activation recovery preserved final custody; effects may have occurred, explicit recovery decision required"
            )

    def promote(self):
        if self.after != census(self.roots):
            raise UpgradeRefused("stopped custody changed before activation")
        current_scripts = self.base / os.readlink(self.base / "current") / "scripts"
        if tree_manifest(current_scripts) != self.candidate_manifest:
            raise UpgradeRefused("installed candidate differs from qualified bytes")
        unit = self.unit_dir / self.router
        text = unit.read_text()
        if text.count(" wake-router serve ") != 1:
            raise UpgradeRefused("unexpected candidate unit command")
        text = text.replace(" wake-router serve ", " wake-router serve --start-quiesced ")
        # The quiesced marker persists across an unexpected restart; only explicit activation runs workers.
        durable_write(unit, text, 0o644)
        if self.host.systemctl("daemon-reload")[0]:
            raise UpgradeRefused("quiesced unit reload failed")
        if self.host.systemctl("start", self.router)[0]:
            raise UpgradeRefused("candidate start failed; intake remains stopped")
        answer = wait_health(self.port, True)
        if {k: v for k, v in answer.items() if k != "upgrade_quiesced"} != self.qualified["health"]:
            raise UpgradeRefused("running health differs from qualified candidate readiness")
        if self.after != census(self.roots):
            raise UpgradeRefused("stopped custody changed before activation")
        code, pid, _ = self.host.systemctl("show", "--property=MainPID", "--value", self.router)
        argv = (
            (self.proc_root / pid.strip() / "cmdline").read_bytes().split(b"\0")
            if not code and pid.strip().isdigit()
            else []
        )
        if (
            b"--start-quiesced" not in argv
            or str(current_scripts / "simple-chat").encode() not in argv
        ):
            raise UpgradeRefused("candidate process identity or quiescence not proven")
        self.activation_requested = True
        if self.host.systemctl("kill", "--kill-whom=main", "--signal=USR1", self.router)[0]:
            raise UpgradeRefused("explicit candidate activation failed")
        wait_health(self.port, False)
        # Remove the temporary startup hold only after activation is acknowledged.
        durable_write(
            unit,
            text.replace(" wake-router serve --start-quiesced ", " wake-router serve "),
            0o644,
        )
        if self.host.systemctl("daemon-reload")[0]:
            raise UpgradeRefused("activated unit reload failed")
        if self.host.systemctl("start", self.front)[0]:
            raise UpgradeRefused("TLS intake start failed")


def qualify_candidate(env):
    """Actual startup/build and HTTP canary, isolated state, zero workers or real delivery."""
    import dataclasses
    import threading
    from http.server import ThreadingHTTPServer

    os.environ.clear()
    os.environ.update(env)

    exec(  # noqa: S102 - fixed local audit bootstrap shared with isolated children
        READONLY_BOOTSTRAP, {}
    )  # Auditing is a bounded Python guard, not an OS sandbox.
    from simple_tools import wake_router as router_module
    from simple_tools import wake_service as service
    from simple_tools import wake_settings as settings

    settings.preflight()
    built = service.build(env)
    code, readiness = built.seats.health()
    if code != 200 or readiness.get("healthy") is not True or built.seats.down:
        raise UpgradeRefused("configured candidate startup not ready")
    delivered = []

    def forbidden(*args, **kwargs):
        delivered.append(True)
        raise UpgradeRefused("candidate attempted delivery")

    temporary = "/nonexistent-router-upgrade-canary"
    with __import__("contextlib").nullcontext(temporary):
        clones = []
        for seat, original in built.seats.routers.items():
            platform = dataclasses.replace(
                original.platform,
                deliver=forbidden,
                deliver_turn=forbidden if original.platform.deliver_turn else None,
            )
            clone = router_module.Router(
                platform,
                original.env,
                original.config,
                Path(temporary) / seat,
                original._key,
                never_ran=original.never_ran,
                review_key=original._review_key,
            )
            # Even a handler regression cannot reserve a durable wake in this canary.
            clone.accept = forbidden
            clone._append_log = lambda receipt: (
                None
            )  # Test-only receipts have no durable side effect.
            clones.append(clone)
        seats = router_module.Seats(clones, built.seats.down, built.seats.not_installed)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), router_module.make_handler(seats))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(base + "/health", timeout=10) as response:
                if json.load(response) != readiness:
                    raise UpgradeRefused("candidate HTTP readiness mismatch")
            for clone in clones:
                key, _why = clone.review_key()
                if not key:
                    raise UpgradeRefused("candidate scoped credential not ready")
                payload = {
                    "contract": router_module.CONTRACT,
                    "test": True,
                    "seat": clone.platform.seat,
                    "wake_id": "ra-upgrade-probe",
                    "require_session": "00000000-0000-4000-8000-000000000000",
                    "require_letter": "ZZ",
                    "require_generation": 1,
                    "require_pr": "Simple-With-Us/AI-Fleet-Coordinator#2429",
                    "channel": "review-alerts",
                }
                request = urllib.request.Request(
                    base + "/wake/" + clone.platform.seat,
                    data=json.dumps(payload).encode(),
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                    },
                )
                with opener.open(request, timeout=30) as response:
                    answer = json.load(response)
                if (
                    answer.get("test") is not True
                    or answer.get("delivered") is not False
                    or not str(answer.get("reason", "")).startswith("required-session-")
                ):
                    raise UpgradeRefused("candidate exact-origin canary refused")
            if delivered or any(clone._entries or clone._threads for clone in clones):
                raise UpgradeRefused("candidate had delivery or worker state")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
    return {
        "qualified": True,
        "health": readiness,
        "seats": sorted(built.seats.routers),
    }


def wait_health(port, quiesced):
    deadline = time.monotonic() + 30
    while True:
        try:
            answer = health(port)
            if answer.get("upgrade_quiesced") is quiesced and answer.get("healthy") is True:
                return answer
        except (OSError, ValueError, UpgradeRefused):
            pass
        if time.monotonic() >= deadline:
            raise UpgradeRefused("candidate readiness/quiescence acknowledgement timed out")
        time.sleep(0.2)


def durable_write(path, text, mode):
    import secrets

    path = Path(path)
    temporary = path.with_name("." + path.name + "-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb" if isinstance(text, bytes) else "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)
