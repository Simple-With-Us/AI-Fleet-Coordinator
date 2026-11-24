"""Linux driver bridge candidate, no automatic activation.

Uses an interactive child protocol so registry/session custody is revalidated
immediately before native subscribe/send, not frozen at process launch.
"""

import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path

from .native_custody import fresh as custody_fresh
from .native_evidence import alert_id, persist
from .native_store import reconcile
from .zcode_native import NativeDelivery, digest


class Driver:
    def __init__(self, state, activation_epoch, node, home):
        self.state, self.activation_epoch = Path(state), activation_epoch
        self.node, self.home = node, home
        self.context = threading.local()
        self.journal = NativeDelivery(self.state / "native-commands", self.transport, self.verify)

    def verify(self, binding):
        return bool(getattr(self.context, "verify", None) and self.context.verify(binding))

    def __call__(self, binding, wake_key, text, *, fresh=False, verify, target=None):
        # Router's own predispatch pending record is authoritative, not fresh kwarg.
        self.context.verify = verify
        try:
            if not self.verify(binding):
                return {"outcome": "refused", "code": "native-binding-changed"}
            authoritative = bool(
                target
                and target.session_id == binding["session_id"]
                and target.host == binding["host"]
                and target.generation == binding["generation"]
                and custody_fresh(self.state, wake_key, target, self.activation_epoch)
            )
            result = self.journal.deliver(binding, wake_key, text, fresh=authoritative)
            if self.verify(binding):
                try:
                    result["evidence"] = reconcile(
                        Path(self.home) / ".zcode/cli/db/db.sqlite",
                        binding,
                        "app-control-echo-review-" + digest({"wake_key": wake_key}),
                        text,
                        alert_id(self.state, wake_key),
                    )
                except Exception:
                    result["evidence"] = {"native_input": "unverified"}
            if "evidence" in result:
                result["evidence"]["admission"] = (
                    "admitted"
                    if result.get("code") == "native-admitted-not-confirmed"
                    or result["evidence"].get("native_input") == "persisted"
                    else "unverified"
                )
                try:
                    persist(self.state, binding, wake_key, result["evidence"])
                except Exception:
                    result["evidence_persistence"] = "unverified"
            return result
        finally:
            self.context.verify = None

    def reconcile_only(self, binding, wake_key, text, *, verify):
        return self(binding, wake_key, text, fresh=False, verify=verify)

    def transport(self, request):
        command = [self.node, str(Path(__file__).with_name("native_driver.cjs"))]
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            start_new_session=True,
            bufsize=0,
        )
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        try:
            child.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
            child.stdin.flush()
            deadline = time.monotonic() + 35
            buffer = b""
            os.set_blocking(child.stdout.fileno(), False)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError()
                chunk = os.read(child.stdout.fileno(), 32769)
                if not chunk:
                    raise ValueError("native-driver-lost")
                buffer += chunk
                if len(buffer) > 32768:
                    raise ValueError("native-driver-budget")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if "verify" in message:
                        ok = message.get("binding") == request["binding"] and self.verify(
                            request["binding"]
                        )
                        child.stdin.write(
                            (json.dumps({"verify": message["verify"], "ok": ok}) + "\n").encode()
                        )
                        child.stdin.flush()
                    elif set(message) == {"result"} and isinstance(message["result"], dict):
                        return message["result"]
                    else:
                        raise ValueError("native-driver-unverified")
            raise TimeoutError()
        finally:
            selector.close()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()


def freshness(state, target, wake_id, activation_epoch):
    return custody_fresh(Path(state), wake_id, target, activation_epoch)
