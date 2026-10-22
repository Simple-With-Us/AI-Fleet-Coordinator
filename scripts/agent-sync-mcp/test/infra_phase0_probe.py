#!/usr/bin/env python3
"""Exercise infra_phase0.py's tunnel/DNS cleanup against a fake Cloudflare API.

Driven by test/infra-phase0.test.mjs.  Stdlib only, no network:  the point is
to prove that a rule or CNAME belonging to the RETIRED host is still found and
removed after the hard cutover repointed HOST at the current endpoint.
Prints one JSON object; the node test asserts on it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import signal
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("infra_phase0", HERE.parent / "infra_phase0.py")
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)

TUNNEL_ID = "tunnel-1"
ZONES = {infra.ZONE_NAME: "zone-new", infra.RETIRED_ZONE_NAME: "zone-old"}
# Both hosts still carry a tunnel CNAME:  the retired one is the whole point.
RECORDS = {
    (infra.ZONE_NAME, infra.HOST): [
        {"id": "rec-new", "type": "CNAME", "name": infra.HOST, "content": "x.cfargotunnel.com"}
    ],
    (infra.RETIRED_ZONE_NAME, infra.RETIRED_HOST): [
        {
            "id": "rec-old",
            "type": "CNAME",
            "name": infra.RETIRED_HOST,
            "content": "y.cfargotunnel.com",
        }
    ],
}


class FakeCloudflare:
    def __init__(self):
        self.deleted = []
        self.puts = []
        self.queried = []
        self.config_reads = 0
        # When set, the tunnel answers with a shape this script must refuse to
        # interpret rather than crash on.
        self.malformed = None
        # Zones that answer "not on this account".  The retired zone is not
        # guaranteed to be on the account, and that must not abort `dns`.
        self.absent_zones: set[str] = set()
        # Zones whose lookup itself fails, the way `Cloudflare.ok` stops on a
        # rejected key or a 5xx.  That is not absence and must not read as it.
        self.failing_zones: set[str] = set()

    def ok(self, method, path, body=None, **params):
        if path == "/zones":
            zone = params["name"]
            if zone in self.failing_zones:
                raise SystemExit(
                    f"error: {method} {path} failed: "
                    + json.dumps([{"code": 10000, "message": "Authentication error"}])
                )
            if zone in self.absent_zones:
                return {"success": True, "result": []}
            return {
                "success": True,
                "result": [{"id": ZONES[zone], "account": {"id": infra.ACCOUNT_ID}}],
            }
        if path == f"/accounts/{infra.ACCOUNT_ID}/cfd_tunnel":
            return {"success": True, "result": [{"id": TUNNEL_ID, "name": infra.TUNNEL_NAME}]}
        if path.endswith("/configurations"):
            if method == "PUT":
                self.puts.append(body["config"]["ingress"])
                return {"success": True, "result": {}}
            self.config_reads += 1
            if self.malformed is not None:
                return {"success": True, "result": {"config": {"ingress": self.malformed}}}
            return {
                "success": True,
                "result": {
                    "config": {
                        "ingress": [
                            # The stale rule Kody flagged:  a public hostname
                            # aimed at the Mac's local dev port.
                            {"hostname": infra.RETIRED_HOST, "service": "http://localhost:8787"},
                            {"hostname": infra.HOST, "service": "http://localhost:8787"},
                            {"service": "http_status:404"},
                        ]
                    }
                },
            }
        if path.startswith("/zones/") and path.endswith("/dns_records"):
            zid = path.split("/")[2]
            zone = next(name for name, ident in ZONES.items() if ident == zid)
            # `check` sweeps the whole zone (no name filter) as well as each
            # host by name;  an absent filter returns every record in the zone.
            host = params.get("name")
            if host is None:
                return {
                    "success": True,
                    "result": [r for (z, _h), recs in RECORDS.items() if z == zone for r in recs],
                }
            self.queried.append((zone, host))
            return {"success": True, "result": RECORDS.get((zone, host), [])}
        if path.startswith("/zones/") and "/dns_records/" in path:
            self.deleted.append(path.rsplit("/", 1)[1])
            return {"success": True, "result": {}}
        if path == f"/accounts/{infra.ACCOUNT_ID}/access/apps":
            return {
                "success": True,
                "result": [{"name": infra.ACCESS_APP_NAME, "aud": "a" * 64, "destinations": []}],
            }
        if path == f"/accounts/{infra.ACCOUNT_ID}/storage/kv/namespaces":
            return {"success": True, "result": [{"id": "kv-live", "title": infra.KV_TITLE}]}
        raise AssertionError(f"unexpected API call: {method} {path} {params}")


class FakeSecretCloudflare:
    """Just the two things put_worker_secret reads off the Cloudflare handle."""

    _key = "cf-api-key-value-0123456789"
    _email = "operator@example.test"

    def wrangler_env(self):
        return {"CLOUDFLARE_API_KEY": self._key, "CLOUDFLARE_EMAIL": self._email}


class FakeChild:
    """A wrangler child that never exists, so the probe stays offline.

    Records how it was started and what it was signalled with, which is the
    whole of what these three checks are about:  the parent has to own the
    child's process group, and the child must not outlive an interrupted run.
    """

    def __init__(self, state, returncode=1, stdout="", stderr="", raise_on_communicate=None):
        self.state = state
        self.pid = 424242
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_on_communicate = raise_on_communicate
        self.kwargs = None
        self.signals: list[int] = []
        self.sigterm_during_run = None
        self.handler_after = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        return self

    def communicate(self, *args, **kwargs):
        if self.raise_on_communicate is not None:
            # What the parent sees while a signal handler is installed:  a
            # SIGTERM must reach the handler, and the handler must be undone
            # once the run is over.
            self.sigterm_during_run = signal.getsignal(signal.SIGTERM)
            raise self.raise_on_communicate
        return self.stdout, self.stderr

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def run_with_fake_child(child, secret=("a" * 64), which="wrangler"):
    """Drive put_worker_secret against a fake child and report what happened."""
    state: dict = {}
    real_popen, real_which, real_killpg = (
        subprocess.Popen,
        infra.shutil.which,
        os.killpg,
    )
    signals: list[tuple[int, int]] = []

    def fake_killpg(pid, sig):
        signals.append((pid, sig))

    class FakePopen:
        def __init__(self, argv, **kwargs):
            child.argv = argv
            child.kwargs = kwargs

    def fake_popen(argv, **kwargs):
        FakePopen(argv, **kwargs)
        return child

    infra.subprocess.Popen = fake_popen
    infra.shutil.which = lambda name: name if which else None
    os.killpg = fake_killpg
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    try:
        cf = FakeSecretCloudflare()
        error = None
        raised = None
        try:
            infra.put_worker_secret(cf, "ACCESS_AUD", secret)
        except SystemExit as exc:
            raised = "SystemExit"
            error = str(exc)
            state["exit_code"] = exc.code
        except BaseException as exc:  # an interrupt is a pass-through, not a crash
            raised = type(exc).__name__
    finally:
        subprocess.Popen = real_popen
        infra.shutil.which = real_which
        os.killpg = real_killpg
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    state.update(
        {
            "raised": raised,
            "error": error,
            "signals": signals,
            "child_returncode": child.returncode,
            "start_new_session": (child.kwargs or {}).get("start_new_session"),
            "child_cwd": str((child.kwargs or {}).get("cwd")),
            "child_env_has_key": "CLOUDFLARE_API_KEY" in ((child.kwargs or {}).get("env") or {}),
            "sigterm_handler_installed": child.sigterm_during_run not in (None, signal.SIG_DFL),
        }
    )
    return state


def secret_probes() -> dict:
    secret = "b" * 64
    key = FakeSecretCloudflare._key
    # A failing wrangler answer with everything sensitive in it:  the secret it
    # was fed, the API key that authenticated, and a response body echoing an
    # Authorization header.  Only the two documented scalars may survive.
    noisy = (
        "[ERROR] A request to the Cloudflare API failed.\n"
        f"  code: 10000\n"
        f"  request id: 8f14e45fceea167a5a36dedd4bea2543\n"
        f"  authorization: Bearer {key}\n"
        f"  echoed secret: {secret}\n"
        '{"result":null,"errors":[{"code":10000,"message":"Authentication error [10000]"}]}\n'
    )
    diagnostic = run_with_fake_child(
        FakeChild(None, returncode=1, stdout=noisy, stderr=noisy), secret=secret
    )
    quiet = run_with_fake_child(FakeChild(None, returncode=1, stdout="", stderr=""), secret=secret)

    def missing_executable():
        def boom(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "wrangler")

        state: dict = {
            "raised": None,
            "error": None,
            "exit_code": None,
            "signals": [],
            "child_returncode": None,
            "start_new_session": None,
            "child_cwd": "none",
            "child_env_has_key": False,
            "sigterm_handler_installed": False,
        }
        real_popen, real_which = subprocess.Popen, infra.shutil.which
        infra.subprocess.Popen = boom
        infra.shutil.which = lambda name: name
        try:
            try:
                infra.put_worker_secret(FakeSecretCloudflare(), "ACCESS_AUD", secret)
            except SystemExit as exc:
                state["raised"] = "SystemExit"
                state["error"] = str(exc)
                state["exit_code"] = exc.code
        finally:
            subprocess.Popen = real_popen
            infra.shutil.which = real_which
        return state

    # Nothing on PATH at all:  the same "command not found" status.
    no_wrangler = run_with_fake_child(FakeChild(None), secret=secret, which=None)
    child_missing = missing_executable()

    interrupted = run_with_fake_child(
        FakeChild(None, raise_on_communicate=KeyboardInterrupt()), secret=secret
    )

    # Wrangler labels the API key itself as the request id:  the whole detail is
    # withheld rather than relayed, so the key never reaches the error or CI logs.
    hex_key = "c" * 32
    real_key = FakeSecretCloudflare._key
    FakeSecretCloudflare._key = hex_key
    try:
        api_key_labelled = run_with_fake_child(
            FakeChild(
                None,
                returncode=1,
                stdout="",
                stderr=f"code: 10000\nrequest id: {hex_key}\n",
            ),
            secret=secret,
        )
    finally:
        FakeSecretCloudflare._key = real_key

    # A request id that is not Cloudflare's fixed 32-hex shape is not a request
    # id at all:  only the code survives.
    short_id = run_with_fake_child(
        FakeChild(None, returncode=1, stdout="", stderr="code: 10001\nrequest id: abc123\n"),
        secret=secret,
    )

    # The probe's own cleanup must restore the caller's SIGTERM handler, not
    # reset it to the default.
    real_handler = signal.getsignal(signal.SIGTERM)
    marker = lambda *args: None
    signal.signal(signal.SIGTERM, marker)
    try:
        run_with_fake_child(FakeChild(None, returncode=0, stdout="", stderr=""), secret=secret)
        sigterm_handler_restored = signal.getsignal(signal.SIGTERM) is marker
    finally:
        signal.signal(signal.SIGTERM, real_handler)

    return {
        "diagnostic": diagnostic,
        "quiet_failure": quiet,
        "no_wrangler_on_path": no_wrangler,
        "popen_missing": child_missing,
        "interrupted": interrupted,
        "api_key_labelled": api_key_labelled,
        "short_id": short_id,
        "sigterm_handler_restored": sigterm_handler_restored,
    }


def main() -> None:
    cf = FakeCloudflare()
    # Keep the real backup directory out of the way.
    infra.BACKUP_DIR = pathlib.Path(tempfile.mkdtemp())

    out = io.StringIO()
    lines = []

    def fake_say(text=""):
        lines.append(text)

    def fake_plan(apply, what):
        lines.append(("APPLY  " if apply else "DRY RUN  ") + what)
        return apply

    infra.say = fake_say
    infra.plan = fake_plan

    cf.config_reads = 0
    infra.cmd_tunnel(cf, True)
    tunnel_config_reads = cf.config_reads
    ingress_after = cf.puts[-1] if cf.puts else None

    infra.cmd_dns(cf, True)

    # The retired zone may not be on this account at all.  `dns --apply` runs
    # the served zone first, so a fatal retired-zone lookup would abort after
    # that deletion and leave the stale CNAME behind with a bare error.
    absent_zone = {"deleted": [], "raised": None, "output": []}
    cf_absent = FakeCloudflare()
    cf_absent.absent_zones = {infra.RETIRED_ZONE_NAME}
    cf_absent_lines: list[str] = []
    infra.say = lambda text="": cf_absent_lines.append(text)
    try:
        infra.cmd_dns(cf_absent, True)
    except BaseException as exc:  # noqa: BLE001 - the probe reports the kind
        absent_zone["raised"] = type(exc).__name__
    infra.say = fake_say
    absent_zone["deleted"] = cf_absent.deleted
    absent_zone["output"] = cf_absent_lines

    # A lookup that fails is not a zone that is absent.  Swallowing the failure
    # would print "nothing to clean there" for a rejected key and carry on.
    failed_lookup = {"deleted": [], "raised": None, "error": None, "output": []}
    cf_failing = FakeCloudflare()
    cf_failing.failing_zones = {infra.RETIRED_ZONE_NAME}
    cf_failing_lines: list[str] = []
    infra.say = lambda text="": cf_failing_lines.append(text)
    try:
        infra.cmd_dns(cf_failing, True)
    except BaseException as exc:  # noqa: BLE001 - the probe reports the kind
        failed_lookup["raised"] = type(exc).__name__
        failed_lookup["error"] = str(exc)
    infra.say = fake_say
    failed_lookup["deleted"] = cf_failing.deleted
    failed_lookup["output"] = cf_failing_lines

    # A third-party answer that is not the documented shape must be a precise
    # refusal, not an incidental AttributeError - in `check` that crash would
    # read as "could not verify" instead of "the answer was not understood".
    malformed_errors = {}
    for label, ingress in (
        ("entry_is_an_object", ["not-an-object"]),
        ("hostname_is_a_string", [{"hostname": 7, "service": "http_status:404"}]),
        ("service_is_a_string", [{"hostname": infra.HOST, "service": {"port": 8787}}]),
        ("ingress_is_a_list", {"hostname": infra.HOST}),
    ):
        cf.malformed = ingress
        try:
            infra.tunnel_ingress(cf, TUNNEL_ID)
            malformed_errors[label] = "ACCEPTED"
        except SystemExit as exc:
            malformed_errors[label] = str(exc)
    cf.malformed = None

    print(
        json.dumps(
            {
                "hosts_after_tunnel": [r.get("hostname") for r in (ingress_after or [])],
                "deleted_record_ids": cf.deleted,
                "queried": sorted(f"{zone}:{host}" for zone, host in cf.queried),
                "output": "\n".join(lines),
                "malformed_errors": malformed_errors,
                "tunnel_config_reads": tunnel_config_reads,
                "absent_zone": absent_zone,
                "failed_lookup": failed_lookup,
                "secrets": secret_probes(),
            }
        )
    )


if __name__ == "__main__":
    main()
