"""Tests for the `--from-muster` entry: muster_approval.py, muster_agent/muster_trust_files.py and the helper.

They check `server-route-spec.md` version 5 (section 16's vectors, in `fixtures/muster-vectors-v5.json`,
public data only) through BOTH the module (`muster_approval.handle_request` with a fake backend) and the
helper's own entry (`fleet-ops-control.py --from-muster`, in process and as a real subprocess fed over
standard input), then attack it: oversize, truncated, duplicate, unknown, non-UTF-8, control and huge
input, odd signatures, replays, torn and corrupt ledgers, symbolic links and hard links, unsafe modes,
approvals for another action, host, target, plan or device, the clock at every edge (the window and the
kernel's own state), the rate limits, faults before and after the nonce is spent, races between
processes, and a clock that steps.

The helper's planner (`plan_request`) is the one planner, and section 4 of the spec is written from
it: `PlannerBytesTests` pins its own bytes and hashes, and the vector tests check the vectors'
signature logic over the vectors' own bytes.

Nothing here touches a real key, a real service, `/etc/muster` or the account's real state directory:
every file lives in a temporary directory and every key is a test key derived from a label, as the spec
says.  The entry loads its modules only from a folder that no group or other can write, which a source
checkout on a shared server may not be, so the tests run it from a private copy (`make_checkout`); the
two tests that run the production entry with no hook change two path constants of that copy and nothing
else.

  cd scripts && python3 test_muster_approval.py

The trust-list chain verb (`muster trust-list chain --yes`, spec 5.3 sections 5, 8 and 12, P3 and C1 to
C5) is tested from `ChainVectorTests` on: every chain vector of section 16 from the fixture's
`frozenExpected` (a Milestone 1 to 4 helper refuses every rule C entry `reopen_unavailable`), then the
parse, the rate step, the stored list, the lock, the write, a crash at each step of it and chains at once.
"""

import ast
import atexit
import base64
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("cryptography") is None:
    # The route's modules import `cryptography` (the system package on the server).  A machine
    # without it, such as a CI image that installs only PyYAML, skips this file and stays green;
    # a machine that has it runs everything.
    if __name__ == "__main__":
        print("test_muster_approval skipped: cryptography is not installed")
        raise SystemExit(0)
    raise unittest.SkipTest("cryptography is not installed")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixtures" / "muster-vectors-v5.json").read_text(encoding="utf-8"))
# The spec, in the repository: the tests read it and fail when it is not there.
SPEC_PATH = HERE.parent / "docs" / "muster-ios" / "server-route-spec.md"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ma = load("muster_approval_under_test", "muster_approval.py")
mt = load("muster_trust_files_under_test", "muster_agent/muster_trust_files.py")
foc = load("foc_under_test", "fleet-ops-control.py")
make = load("make_muster_vectors_under_test", "make_muster_vectors.py")

N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
UID = os.getuid()
COMMAND = "server restart fleet-server-status.timer --yes"
PLAN_COMMAND = "server restart fleet-server-status.timer --plan"
NOW = "2026-11-07T21:00:30Z"
NOW_EPOCH = 1_794_085_230  # NOW as epoch seconds, written out so that no helper builds it
BASE_APPROVAL = {
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "host": "server",
    "action": "restart",
    "target": "fleet-server-status.timer",
    "planSHA256": "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
    "issuedAt": "2026-11-07T21:00:00Z",
    "expiresAt": "2026-11-07T21:02:00Z",
}
JOB = {
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
}
DEVICE_B = "d-629e62343180ad883d8f7a47"
ILLUSTRATIVE_PLAN = FIXTURE["illustrativePlan"].encode("ascii")
OTHER_PLAN = b"a different plan"


# MARK: - Test keys and builders, independent of the code under test


def derive(label):
    digest = hashlib.sha256(("muster test key " + label).encode()).digest()
    return ec.derive_private_key(int.from_bytes(digest, "big") % (N - 1) + 1, ec.SECP256R1())


KEYS = {label: derive(label) for label in FIXTURE["keys"]}
OWNER = KEYS["owner"]
HELPER = KEYS["server helper"]
DEVICE_A = KEYS["device A approval"]


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def spki(key):
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def sign(key, data):
    return key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))


def text_of(approval):
    fields = ("jobId", "jobNonce", "approvalNonce", "host", "action", "target", "planSHA256")
    fields += ("deviceId", "issuedAt", "expiresAt")
    return "\n".join(["muster-approve/1"] + [approval[name] for name in fields]).encode("ascii")


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def approval(signer=DEVICE_A, **overrides):
    result = dict(BASE_APPROVAL, **overrides)
    result["signature"] = b64u(sign(signer, text_of(result)))
    return result


def stdin_of(approval_dict):
    return compact({"protocol": "muster-helper/1", "approval": approval_dict})


def job_stdin(job=None):
    return compact({"protocol": "muster-helper/1", "job": dict(JOB if job is None else job)})


CURRENT_COMMAND = "muster trust-list current --plan"
CURRENT_STDIN = compact({"protocol": "muster-helper/1"})


def resign(body, signer=OWNER, prefix=b"muster-trust-list/1\n"):
    return {
        "body": body,
        "ownerSignature": b64u(sign(signer, prefix + compact(body))),
        "countersignatures": [],
    }


def ledger_line(nonce, expires="2026-11-07T21:02:00Z", recorded="2026-11-07T21:00:00Z"):
    row = {
        "approvalNonce": nonce,
        "deviceId": BASE_APPROVAL["deviceId"],
        "expiresAt": expires,
        "jobId": BASE_APPROVAL["jobId"],
        "recordedAt": recorded,
    }
    return compact(row) + b"\n"


def epoch(stamp):
    return ma.parse_time(stamp)


def calendar_epoch(year, month, day, hour, minute, second):
    """Seconds since the epoch of a UTC date, from days and seconds alone (no library)."""
    days = sum(365 + (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) for y in range(1970, year))
    lengths = [31, 28 + (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)), 31, 30, 31, 30]
    lengths += [31, 31, 30, 31, 30, 31]
    days += sum(lengths[: month - 1]) + day - 1
    return days * 86400 + hour * 3600 + minute * 60 + second


def later(seconds):
    """A clock reading `seconds` after NOW, for a second request that must not be rate limited."""
    return epoch(NOW) + seconds


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def write_file(path, data, mode):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(str(path), mode)


CHECKOUT_FILES = (
    "fleet-ops-control.py",
    "muster_approval.py",
    "fleet_resource_control.py",
    "fleet_seat_inventory.py",
    "reduced_status.py",
)
# Folders of the copy and the files in each (None: every .py file).
CHECKOUT_FOLDERS = {
    "muster_agent": ("muster_trust_files.py",),
    "fleet_secrets": None,
    "hetzner": ("fleet_coding_memory.py", "coding_resources.py"),
}


def make_checkout(root, replacements=()):
    """A private copy of the files the `--from-muster` entry loads, in a folder the loader accepts.

    A source checkout on a shared server can be group-writable, and the loader refuses a folder or a
    file that group or others can write, so the entry is run from a copy with modes 0755 and 0644.
    `replacements` are (file, old text, new text) changes of the copy, each of which must apply once."""
    folder = Path(root) / "scripts"
    folder.mkdir()
    for name in CHECKOUT_FILES:
        shutil.copyfile(HERE / name, folder / name)
    for sub, names in CHECKOUT_FOLDERS.items():
        (folder / sub).mkdir()
        for name in names or sorted(p.name for p in (HERE / sub).glob("*.py")):
            shutil.copyfile(HERE / sub / name, folder / sub / name)
    for name, old, new in replacements:
        path = folder / name
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, (name, old)
        path.write_text(text.replace(old, new), encoding="utf-8")
    for path in [folder, *folder.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return folder


_SHARED_CHECKOUT = []


def checkout():
    """One private copy for the whole run (made on first use, removed at exit)."""
    if not _SHARED_CHECKOUT:
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="muster-checkout-")))
        atexit.register(shutil.rmtree, str(root), True)
        _SHARED_CHECKOUT.append(make_checkout(root))
    return _SHARED_CHECKOUT[0]


ALLOWLIST = {
    "schemaVersion": 1,
    "mac": {},
    "server": {
        "units": [
            {
                "unit": "fleet-server-status.timer",
                "scope": "user",
                "actions": ["start", "stop", "restart"],
            },
            {"unit": "botfleet-server.service", "scope": "system", "actions": ["restart"]},
        ],
        "resourcePolicy": True,
    },
}


def synced_clock():
    """What `adjtimex` says on a synchronised host: TIME_OK, STA_UNSYNC clear, 175 ms of error."""
    return 0, 0, 175_000


class Env:
    """A private world for one request: /etc/muster, the state directory, and a home."""

    def __init__(self, test, stored="v1", ledger=b"", key=True):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.etc = self.root / "etc-muster"
        self.etc.mkdir()
        os.chmod(self.etc, 0o755)
        write_file(self.etc / "owner.pub", (FIXTURE["pin"] + "\n").encode(), 0o644)
        write_file(self.etc / "owner-history.jsonl", (FIXTURE["history"] + "\n").encode(), 0o644)
        self.home = self.root / "home"
        self.state = self.home / ".local" / "state" / "fleet-tools"
        self.state.mkdir(parents=True, mode=0o700)
        for parent in (self.home, self.home / ".local", self.home / ".local" / "state", self.state):
            os.chmod(parent, 0o700)
        if key:
            pem = HELPER.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            write_file(self.state / "muster-helper.key", pem, 0o600)
        if stored:
            self.store(FIXTURE["lists"][stored] if isinstance(stored, str) else stored)
        if ledger:
            write_file(self.state / "muster-nonces.jsonl", ledger, 0o600)
        self.allowlist = self.root / "fleet-ops-control.json"
        write_file(self.allowlist, json.dumps(ALLOWLIST).encode(), 0o600)

    def store(self, doc, mode=0o600):
        write_file(self.state / "muster-trust.json", json.dumps(doc).encode(), mode)

    @property
    def ledger(self):
        path = self.state / "muster-nonces.jsonl"
        return path.read_bytes() if path.exists() else None

    def config(self, now, **changes):
        settings = {
            "etc_dir": str(self.etc),
            "state_dir": str(self.state),
            "root_uid": UID,
            "uid": UID,
            "now": epoch(now) if isinstance(now, str) else now,
            "clock_state": synced_clock,
        }
        settings.update(changes)
        return settings

    def module_config(self, now, **changes):
        settings = self.config(now, **changes)
        fixed = settings["now"]
        return ma.MusterConfig(
            settings["etc_dir"],
            settings["state_dir"],
            settings["root_uid"],
            settings["uid"],
            settings.get("clock") or (lambda: fixed),
            settings["clock_state"],
        )

    def audit_rows(self):
        path = self.state / "ops-audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


class FakeBackend:
    """What the helper would offer, with nothing behind it, and a record of what was asked."""

    def __init__(self, plan=ILLUSTRATIVE_PLAN, busy=False, on_lock=None, outcome="done"):
        self.plan = plan
        self.busy = busy
        self.on_lock = on_lock
        self.outcome = outcome
        self.events = []
        self.rows = []
        self.runs = []
        self.refuse_plan = None
        self.refuse_code = None
        self.refuse_read = None
        self.run_error = None
        self.run_detail = "restarted"
        self.target_check = None
        self.audit_id = "a1b2c3d4e5f6"

    def target_ok(self, verb, target):
        """The helper's grammar for a target, as far as these tests need one."""
        if self.target_check is not None:
            return self.target_check(verb, target)
        return True

    def lock(self):
        backend = self

        class Held:
            def __enter__(self):
                if backend.busy:
                    raise ma.LockBusy
                backend.events.append("lock")
                if backend.on_lock:
                    backend.on_lock()
                return self

            def __exit__(self, *exc):
                backend.events.append("unlock")

        return Held()

    def plan_bytes(self, verb, target):
        self.events.append("plan")
        if self.refuse_plan:
            raise ma.BackendRefused(self.refuse_plan, self.refuse_code)
        return self.plan

    def read(self, verb, target):
        self.events.append("read")
        if self.refuse_read:
            raise ma.BackendRefused(self.refuse_read, self.refuse_code)
        return ma.ReadResult("read ok", b'{"x":1}')

    def run(self, verb, target, approval_fields):
        self.events.append("run")
        self.runs.append((verb, target, approval_fields))
        if self.run_error is not None:
            raise self.run_error
        return ma.RunResult(self.outcome, self.run_detail, "line one\n", self.audit_id)

    def audit(self, scope, verb, target, result, fields):
        self.rows.append(
            {"scope": scope, "verb": verb, "target": target, "result": result, **fields}
        )
        return True


def module_request(env, command, stdin, now, backend=None, argv_ok=True, **config_changes):
    backend = backend or FakeBackend()
    answer, code = ma.handle_request(
        env.module_config(now, **config_changes),
        backend,
        command,
        argv_ok,
        lambda limit: stdin[: limit + 1],
        mt,
    )
    return answer, code, backend


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=None, env=None, want_stdout=False, max_bytes=None, **kw):
        self.calls.append(list(argv))
        if "journalctl" in argv[0]:
            return 0, b"line one\nline two\n"
        return 0, b""


SYSTEMCTL = "/usr/bin/systemctl"


@contextlib.contextmanager
def entry_patches():
    """What an in-process run of the helper's entry needs: the private checkout as the helper's own
    folder, and the process environment put back afterwards (the entry empties it, as in production)."""
    with mock.patch.dict(os.environ), mock.patch.object(foc, "HELPER_DIR", checkout()):
        yield


def helper_request(env, command, stdin, now, systemctl=SYSTEMCTL, ctx=None, **config_changes):
    """The helper's `--from-muster` entry, in process.  Returns (answer, code, runner, rows)."""
    config = env.config(now, **config_changes)
    if ctx is None:
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            config_path=env.allowlist,
            now=lambda: config["now"],
            environ={"HOME": str(env.home)},
        )
    runner = ctx.runner
    out = io.StringIO()
    # The entry empties the process environment, as it does in production, so it is restored here.
    with (
        mock.patch.dict(os.environ),
        mock.patch.object(foc, "_systemctl", lambda: systemctl),
        mock.patch.object(foc, "HELPER_DIR", checkout()),
    ):
        code = foc.main(
            ["--from-muster"],
            ctx=ctx,
            out=out,
            muster={"config": config, "ssh_command": command, "stdin": stdin},
        )
    return json.loads(out.getvalue()), code, runner, env.audit_rows()


DRIVER = r"""
import importlib.util, json, sys
from pathlib import Path
spec = json.load(open(sys.argv[1]))
loaded = importlib.util.spec_from_file_location("foc", spec["helper"])
foc = importlib.util.module_from_spec(loaded)
loaded.loader.exec_module(foc)
foc._systemctl = lambda: spec["systemctl"]
def runner(argv, timeout=None, env=None, want_stdout=False, max_bytes=None, **kw):
    with open(spec["calls"], "a") as log:
        log.write(json.dumps(argv) + "\n")
    return 0, b""
ctx = foc.Context(platform="linux", home=Path(spec["home"]), runner=runner,
                  config_path=Path(spec["allowlist"]), now=lambda: spec["config"]["now"],
                  environ={"HOME": spec["home"]})
config = dict(spec["config"], clock_state=lambda: (0, 0, 175000))
sys.exit(foc.main(["--from-muster"], ctx=ctx, muster={"config": config}))
"""


ENV_DRIVER = r"""
import importlib.util, json, os, sys
from pathlib import Path
spec = json.load(open(sys.argv[1]))
loaded = importlib.util.spec_from_file_location("foc", spec["helper"])
foc = importlib.util.module_from_spec(loaded)
loaded.loader.exec_module(foc)
foc._systemctl = lambda: "/usr/bin/systemctl"
real_load = foc._load_muster_modules
report = {}
def spy(ctx):
    report["environment"] = dict(os.environ)
    report["cryptography_loaded"] = "cryptography" in sys.modules
    report["urllib_request_loaded"] = "urllib.request" in sys.modules
    report["ssl_loaded"] = "ssl" in sys.modules
    return real_load(ctx)
foc._load_muster_modules = spy
ctx = foc.Context(platform="linux", home=Path(spec["home"]), runner=lambda argv, **kw: (0, b""),
                  config_path=Path(spec["allowlist"]), now=lambda: spec["config"]["now"],
                  environ={"HOME": spec["home"]})
config = dict(spec["config"], clock_state=lambda: (0, 0, 175000))
code = foc.main(["--from-muster"], ctx=ctx, muster={"config": config})
with open(spec["report"], "w") as handle:
    handle.write(json.dumps(report))
sys.exit(code)
"""


def subprocess_request(test, env, command, stdin, now, systemctl=SYSTEMCTL):
    """The helper's `--from-muster` entry as a real `python3 -I` process fed over standard input."""
    driver = env.root / "driver.py"
    driver.write_text(DRIVER)
    calls = env.root / "calls.jsonl"
    spec = env.root / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "helper": str(checkout() / "fleet-ops-control.py"),
                "systemctl": systemctl,
                "calls": str(calls),
                "home": str(env.home),
                "allowlist": str(env.allowlist),
                "config": {k: v for k, v in env.config(now).items() if k != "clock_state"},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, "-I", str(driver), str(spec)],
        input=stdin,
        capture_output=True,
        env={"SSH_ORIGINAL_COMMAND": command},
        timeout=60,
        check=False,
    )
    test.assertEqual(result.stderr, b"", result.stderr)
    answer = json.loads(result.stdout)
    logged = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return answer, result.returncode, logged, env.audit_rows()


def helper_public():
    return b64u(spki(HELPER))


class Checks(unittest.TestCase):
    """Shared assertions on signed answers."""

    def assertSigned(self, answer):
        self.assertTrue(ma.verify_signed_answer(answer, helper_public()), answer)

    def assertRefused(self, answer, code, expected, *, verified=None, reason=None):
        self.assertEqual(code, 2, answer)
        self.assertEqual(answer["kind"], "refusal", answer)
        self.assertEqual(answer["body"]["errorCode"], expected, answer)
        self.assertSigned(answer)
        if verified is not None:
            self.assertEqual(answer["body"]["verified"], verified, answer)
        self.assertEqual(answer["body"]["reason"], reason)

    def assertDone(self, answer, code):
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(answer["body"]["outcome"], "done", answer)
        self.assertSigned(answer)

    def run_within(self, seconds, call, unblock=None):
        """`call()` on a thread with a deadline.  A regression that waits without a limit (a lock, a
        FIFO, a pipe) fails here, after `seconds`, instead of hanging the suite and leaving its
        temporary folders behind.  `unblock()`, when given, lets a stuck call go before the failure."""
        import threading

        outcome = []

        def work():
            try:
                outcome.append(("returned", call()))
            except BaseException as exc:  # noqa: BLE001 - handed back to the test's own thread
                outcome.append(("raised", exc))

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            if unblock is not None:
                unblock()
                thread.join(5)
            self.fail(
                f"the call had not returned after {seconds} seconds: it waits without a limit"
            )
        kind, value = outcome[0]
        if kind == "raised":
            raise value
        return value

    def release_fifos(self, directory):
        """`release_fifo` for every FIFO directly inside `directory`."""
        for path in Path(directory).iterdir():
            if stat.S_ISFIFO(os.lstat(path).st_mode):
                self.release_fifo(path)

    @staticmethod
    def foreign_owner(path, directory=False):
        """`os.fstat` as it is, except that the one file (or directory) at `path` belongs to another
        account.  A test cannot be root or chown, so the owner is faked per descriptor."""
        target = os.stat(path)
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            same = (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino)
            if same and stat.S_ISDIR(info.st_mode) == directory:
                fields = list(tuple(info))
                fields[4] = UID + 1000
                return os.stat_result(fields)
            return info

        return mock.patch.object(os, "fstat", fstat)

    @staticmethod
    def release_fifo(path):
        """Open the write end of a FIFO that a reader is stuck on, so that the reader goes on."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.close(fd)


# MARK: - Canonical bytes, keys and the signed answers (section 2, 13 and the vectors)


class CanonicalTests(Checks):
    def test_the_canonical_form_vector(self):
        vector = FIXTURE["canonical"]
        produced = ma.canonical_bytes(json.loads(vector["source"]))
        self.assertEqual(produced.decode("ascii"), vector["expected"])
        self.assertEqual(hashlib.sha256(produced).hexdigest(), vector["sha256"])
        self.assertIn(b"/usr/bin/systemctl", produced)
        self.assertNotIn(b"\\/", produced)
        self.assertTrue(produced.startswith(b'{"B":null,"a":"x","a10":'))
        self.assertIn(b'"a9":{"b":-3', produced)

    def test_the_encoder_agrees_with_a_json_library_on_a_battery(self):
        battery = [
            None,
            True,
            False,
            0,
            -7,
            2**53 - 1,
            -(2**53 - 1),
            "",
            "a b",
            'q"uote\\back/slash~',
            [],
            {},
            [1, [2, [3, {"k": None}]]],
            {"b": 1, "B": 2, "a10": 3, "a9": 4, "": 5, "~": 6, " ": 7},
            {"nested": {"z": [True, False, None], "a": {"y": "x"}}},
        ]
        for value in battery:
            with self.subTest(value=value):
                expected = json.dumps(
                    value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
                self.assertEqual(ma.canonical_bytes(value), expected)

    def test_values_the_form_cannot_hold_are_refused(self):
        deep = []
        for _ in range(ma.CANON_MAX_DEPTH + 2):
            deep = [deep]
        for value in (
            1.5,
            float("nan"),
            2**53,
            -(2**53),
            "café",
            "tab\there",
            "nul\x00",
            "del\x7f",
            "line\nfeed",
            {1: "k"},
            {"é": 1},
            (1, 2),
            b"bytes",
            {"a": object()},
            deep,
        ):
            with self.subTest(value=repr(value)[:40]), self.assertRaises(ma.NotCanonical):
                ma.canonical_bytes(value)

    def test_a_bool_is_not_an_integer_and_the_keys_sort_as_bytes(self):
        self.assertEqual(ma.canonical_bytes([True, 1]), b"[true,1]")
        self.assertEqual(ma.canonical_bytes({"b": 1, "B": 2}), b'{"B":2,"b":1}')

    def test_history_lines_are_canonical_for_both_modules(self):
        # The shared /etc/muster module builds the canonical line by hand; it must agree.
        key = FIXTURE["keys"]["owner"]["public"]
        for start, end, digest in ((1, None, None), (1, 4, "ab" * 32), (5, None, None)):
            line = ma.canonical_bytes(
                {"fromVersion": start, "ownerKey": key, "toSHA256": digest, "toVersion": end}
            )
            self.assertEqual(mt._history_line(line)["fromVersion"], start)


class KeyAndAnswerTests(Checks):
    def test_every_test_key_derives_to_its_published_public_key(self):
        for label, published in FIXTURE["keys"].items():
            with self.subTest(label=label):
                self.assertEqual(b64u(spki(KEYS[label])), published["public"])
                digest = hashlib.sha256(spki(KEYS[label])).hexdigest()
                grouped = " ".join(digest[i : i + 4] for i in range(0, 64, 4))
                self.assertEqual(grouped, published["fingerprint"])

    def test_the_printed_answers_verify_and_only_under_their_own_prefix(self):
        self.assertEqual(
            set(FIXTURE["answers"]),
            {"plan", "result", "refusalV3", "refusalV6", "chainRefusalT7", "ackT2", "ackT3"},
        )
        for name, item in FIXTURE["answers"].items():
            answer = item["answer"]
            with self.subTest(answer=name):
                self.assertTrue(ma.verify_signed_answer(answer, helper_public()))
                self.assertEqual(
                    hashlib.sha256(ma.canonical_bytes(answer["body"])).hexdigest(),
                    item["bodySHA256"],
                )
                self.assertFalse(
                    ma.verify_signed_answer(answer, b64u(spki(KEYS["rogue helper (not enrolled)"])))
                )
                for other in ("plan", "result", "refusal", "ack"):
                    if other != answer["kind"]:
                        swapped = dict(answer, kind=other)
                        self.assertFalse(ma.verify_signed_answer(swapped, helper_public()))
                self.assertFalse(
                    ma.verify_signature(
                        helper_public(), answer["signature"], ma.canonical_bytes(answer["body"])
                    ),
                    "the bare body, without its prefix, must not verify",
                )

    def test_the_module_reproduces_every_printed_answer_byte_for_byte(self):
        key = ma.HelperKey(HELPER)
        self.assertEqual(key.key_id, "38fbd10025f412d0")
        self.assertEqual(key.sign(b"x"), key.sign(b"x"), "signing is deterministic")
        for name, item in FIXTURE["answers"].items():
            answer = item["answer"]
            with self.subTest(answer=name):
                built = ma.signed_answer(key, answer["kind"], answer["body"])
                self.assertEqual(built, answer)

    def test_a_tampered_answer_does_not_verify(self):
        answer = json.loads(json.dumps(FIXTURE["answers"]["result"]["answer"]))
        for path, value in (("outcome", "failed"), ("target", "botfleet-server.service")):
            tampered = json.loads(json.dumps(answer))
            tampered["body"][path] = value
            self.assertFalse(ma.verify_signed_answer(tampered, helper_public()))
        tampered = json.loads(json.dumps(answer))
        tampered["keyId"] = "0" * 16
        self.assertFalse(ma.verify_signed_answer(tampered, helper_public()))

    def test_strict_der_accepts_canonical_and_refuses_each_malformed_shape(self):
        good = sign(DEVICE_A, b"message")
        self.assertIsNotNone(ma.parse_der_signature(good))
        r_and_s = ma.parse_der_signature(good)

        def der(r, s, long_form=False, pad_r=False, trailing=b""):
            def integer(value, pad=False):
                body = value.to_bytes((value.bit_length() + 8) // 8, "big")
                return (
                    b"\x02"
                    + bytes([len(body) + 1 if pad else len(body)])
                    + (b"\x00" if pad else b"")
                    + body
                )

            content = integer(r, pad_r) + integer(s) + trailing
            if long_form:
                return b"\x30\x81" + bytes([len(content)]) + content
            return b"\x30" + bytes([len(content)]) + content

        self.assertEqual(ma.parse_der_signature(der(*r_and_s)), r_and_s)
        for name, blob in (
            ("long-form length", der(*r_and_s, long_form=True)),
            ("padded integer", der(*r_and_s, pad_r=True)),
            ("trailing byte", der(*r_and_s, trailing=b"\x00")),
            ("zero r", der(0, 5)),
            ("zero s", der(5, 0)),
            ("r equal to n", der(ma.P256_ORDER, 5)),
            ("s above n", der(5, ma.P256_ORDER + 1)),
            ("empty", b""),
            ("one byte", b"\x30"),
            ("wrong tag", b"\x31" + good[1:]),
            ("truncated", good[:-1]),
            ("extended", good + b"\x00"),
            (
                "too long",
                b"\x30"
                + bytes([70])
                + b"\x02"
                + bytes([33])
                + b"\x00"
                + b"\xff" * 32
                + b"\x02\x21"
                + b"\x00"
                + b"\xff" * 32
                + b"\x00" * 4,
            ),
        ):
            with self.subTest(shape=name):
                self.assertIsNone(ma.parse_der_signature(blob))
                self.assertFalse(
                    ma.verify_signature(
                        FIXTURE["keys"]["device A approval"]["public"], b64u(blob), b"message"
                    )
                )

    def test_strict_der_is_checked_before_the_library_is_asked(self):
        # Defence in depth: a library that accepted anything must still not be asked about a
        # signature that is not canonical DER.
        asked = []

        class Lenient:
            def verify(self, signature, message, algorithm):
                asked.append(signature)

        good = sign(DEVICE_A, b"message")
        padded = b"\x30\x81" + bytes([len(good) - 2]) + good[2:]
        with mock.patch.object(ma, "load_p256_public", lambda text: Lenient()):
            self.assertFalse(ma.verify_signature("k", b64u(padded), b"message"))
            self.assertEqual(asked, [])
            self.assertTrue(ma.verify_signature("k", b64u(good), b"message"))
            self.assertEqual(asked, [good])

    def test_base64url_is_strict(self):
        for bad in ("abc=", "ab cd", "a", "abcde", "+/", "ab\n", "AB+/", "é"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                ma.b64u_decode(bad)
        self.assertEqual(ma.b64u_decode(ma.b64u_encode(b"\x00\xff\x10")), b"\x00\xff\x10")
        # Stray trailing bits: "QQ" decodes to b"A", "QR" decodes to the same byte but is not canonical.
        self.assertEqual(ma.b64u_decode("QQ"), b"A")
        with self.assertRaises(ValueError):
            ma.b64u_decode("QR")

    def test_public_keys_must_be_exactly_one_uncompressed_p256_key(self):
        good = FIXTURE["keys"]["owner"]["public"]
        self.assertIsNotNone(ma.load_p256_public(good))
        other = ec.generate_private_key(ec.SECP384R1())
        der384 = other.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        compressed = (
            derive("compressed")
            .public_key()
            .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        )
        for bad in (
            b64u(der384),
            b64u(b"\x30" * 91),
            b64u(compressed),
            good[:-4],
            good + "AAAA",
            "",
        ):
            with self.subTest(bad=bad[:20]), self.assertRaises(ValueError):
                ma.load_p256_public(bad)


# MARK: - The vectors, through the module and through the helper


class VectorTests(Checks):
    """Section 16: every approval vector must give exactly the spec's result and error code."""

    # Which vectors have passed Y5, so that their refusals name verified fields.
    VERIFIED = frozenset({"V4", "V5", "V6", "V9", "V10"})
    # Which of them leave an audit row: only a refusal after Y8 has spent the nonce (spec section 14).
    AUDITED = frozenset({"V6"})
    # The `helperTrustListVersion` of each refusal (section 13): the stored list's version when the
    # request had read it, else null.  V13 is refused at P3, V16 holds nothing, V15 holds version 3.
    REFUSAL_VERSION = {"V13": None, "V15": 3, "V16": None}

    def plan_for(self, vector):
        digest = vector["helperPlanSHA256"]
        for plan in (ILLUSTRATIVE_PLAN, OTHER_PLAN):
            if hashlib.sha256(plan).hexdigest() == digest:
                return plan
        raise AssertionError("no plan hashes to " + digest)

    def env_for(self, vector):
        ledger = b"".join(ledger_line(nonce) for nonce in vector["ledger"])
        stored = None if vector["stored"] == "none" else vector["stored"]
        return Env(self, stored=stored, ledger=ledger)

    def test_the_fixture_matches_the_spec_file_in_the_repository(self):
        text = SPEC_PATH.read_text(encoding="utf-8")
        # Version 5, or 5.1 once that spec lands: the vectors are the same.
        self.assertRegex(text, r"\*\*Spec version 5(\.\d+)?\.\*\*")
        for vector in FIXTURE["approvals"]:
            self.assertIn(vector["stdin"], text)
            self.assertIn(vector["signature"], text)
            self.assertEqual(len(vector["stdin"].encode()), vector["stdinLength"])
        self.assertIn(FIXTURE["canonical"]["expected"], text)

    def test_every_vector_builds_from_its_parts_to_the_printed_bytes(self):
        for vector in FIXTURE["approvals"]:
            with self.subTest(vector=vector["id"]):
                document = json.loads(vector["stdin"])
                self.assertEqual(compact(document), vector["stdin"].encode())
                text = text_of(document["approval"])
                self.assertEqual(text.hex(), vector["textHex"])
                self.assertEqual(hashlib.sha256(text).hexdigest(), vector["textSHA256"])
        self.assertEqual(stdin_of(approval()).decode(), FIXTURE["approvals"][0]["stdin"])

    def check_vector(self, vector, answer, code, took_the_plan_path):
        expected = vector["expected"]
        if expected == "accepted":
            self.assertDone(answer, code)
            body = answer["body"]
            self.assertEqual(body["approvalNonce"], BASE_APPROVAL["approvalNonce"])
            self.assertEqual(body["planSHA256"], BASE_APPROVAL["planSHA256"])
            self.assertEqual(body["helperTrustListVersion"], 1)
        else:
            self.assertRefused(answer, code, expected, verified=vector["id"] in self.VERIFIED)
            self.assertEqual(
                answer["body"]["helperTrustListVersion"],
                self.REFUSAL_VERSION.get(vector["id"], 1),
                vector["id"],
            )

    def test_all_sixteen_vectors_through_the_module(self):
        for vector in FIXTURE["approvals"]:
            with self.subTest(vector=vector["id"]):
                env = self.env_for(vector)
                backend = FakeBackend(plan=self.plan_for(vector))
                answer, code, backend = module_request(
                    env, COMMAND, vector["stdin"].encode(), vector["now"], backend
                )
                self.check_vector(vector, answer, code, True)
                ran = backend.events.count("run")
                self.assertEqual(ran, 1 if vector["expected"] == "accepted" else 0)
                spent = BASE_APPROVAL["approvalNonce"].encode() in (env.ledger or b"")
                reached_y8 = vector["expected"] in ("accepted", "plan_mismatch", "replayed_nonce")
                self.assertEqual(spent, reached_y8, vector["id"])
                audited = vector["id"] in self.AUDITED
                self.assertEqual(len(backend.rows), 1 if audited else 0, backend.rows)

    def test_all_sixteen_vectors_through_the_helper_in_process(self):
        for vector in FIXTURE["approvals"]:
            with self.subTest(vector=vector["id"]):
                env = self.env_for(vector)
                # The helper builds the real plan: with the host resolving /usr/bin/systemctl it is the
                # vectors' plan byte for byte.  V6 says the helper's own plan hashes differently, which
                # here is a host whose first systemctl is the other candidate, /bin/systemctl.
                systemctl = "/bin/systemctl" if vector["id"] == "V6" else SYSTEMCTL
                answer, code, runner, rows = helper_request(
                    env, COMMAND, vector["stdin"].encode(), vector["now"], systemctl=systemctl
                )
                self.check_vector(vector, answer, code, False)
                expected_calls = (
                    [[SYSTEMCTL, "--user", "restart", "fleet-server-status.timer"]]
                    if vector["expected"] == "accepted"
                    else []
                )
                self.assertEqual(runner.calls, expected_calls)
                wanted = (
                    1 if vector["expected"] == "accepted" or vector["id"] in self.AUDITED else 0
                )
                self.assertEqual(len(rows), wanted, rows)

    def test_all_sixteen_vectors_through_the_helper_as_a_subprocess_over_stdin(self):
        for vector in FIXTURE["approvals"]:
            with self.subTest(vector=vector["id"]):
                env = self.env_for(vector)
                systemctl = "/bin/systemctl" if vector["id"] == "V6" else SYSTEMCTL
                answer, code, calls, rows = subprocess_request(
                    self, env, COMMAND, vector["stdin"].encode(), vector["now"], systemctl
                )
                self.check_vector(vector, answer, code, False)
                if vector["expected"] == "accepted":
                    self.assertEqual(
                        calls, [[SYSTEMCTL, "--user", "restart", "fleet-server-status.timer"]]
                    )
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["result"], "done")
                else:
                    self.assertEqual(calls, [])

    def test_the_illustrative_plan_is_what_the_helper_builds(self):
        env = Env(self)
        with mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL):
            ctx = foc.Context(
                platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
            )
            config = foc.load_config(env.allowlist)
            commands, _, audit_target = foc.plan_request(
                ctx, config, "server", "restart", "fleet-server-status.timer"
            )
        built = ma.canonical_bytes(
            foc.plan_object(
                "server", "restart", "fleet-server-status.timer", commands, audit_target
            )
        )
        self.assertEqual(built, ILLUSTRATIVE_PLAN)
        self.assertEqual(hashlib.sha256(built).hexdigest(), BASE_APPROVAL["planSHA256"])

    def test_the_printed_refusals_and_result_are_what_the_flow_signs(self):
        # Same bodies as the vectors print, modulo the clock readings and the audit id.
        env = Env(self)
        answer, _, _ = module_request(env, COMMAND, FIXTURE["approvals"][2]["stdin"].encode(), NOW)
        printed = FIXTURE["answers"]["refusalV3"]["answer"]["body"]
        self.assertEqual(
            {k: v for k, v in answer["body"].items() if k != "refusedAt"},
            {k: v for k, v in printed.items() if k != "refusedAt"},
        )
        env = Env(self)
        answer, _, _ = module_request(
            env,
            COMMAND,
            FIXTURE["approvals"][5]["stdin"].encode(),
            NOW,
            FakeBackend(plan=OTHER_PLAN),
        )
        printed = FIXTURE["answers"]["refusalV6"]["answer"]["body"]
        self.assertEqual(
            {k: v for k, v in answer["body"].items() if k != "refusedAt"},
            {k: v for k, v in printed.items() if k != "refusedAt"},
        )
        env = Env(self)
        answer, _, _ = module_request(env, COMMAND, FIXTURE["approvals"][0]["stdin"].encode(), NOW)
        printed = FIXTURE["answers"]["result"]["answer"]["body"]
        skip = {"finishedAt", "auditId", "detail", "outputB64"}
        self.assertEqual(
            {k: v for k, v in answer["body"].items() if k not in skip},
            {k: v for k, v in printed.items() if k not in skip},
        )

    def test_the_printed_signed_plan_is_what_the_flow_signs_byte_for_byte(self):
        # The plan request of V1's job, at the printed clock reading, gives the printed answer whole,
        # signature included (signing is deterministic).
        env = Env(self)
        printed = FIXTURE["answers"]["plan"]["answer"]
        answer, code, backend = module_request(
            env, PLAN_COMMAND, job_stdin(), printed["body"]["issuedAt"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(answer, printed)
        self.assertEqual(
            hashlib.sha256(printed["body"]["plan"].encode()).hexdigest(),
            printed["body"]["planSHA256"],
        )


# MARK: - P0 to P3: key, pin, command line and standard input


class KeyTests(Checks):
    """Any fault in the helper key file is the unsigned `helper_key_missing`, never `state_unsafe`."""

    def assertUnsigned(self, answer, code):
        self.assertEqual(code, 2)
        self.assertEqual(answer["kind"], "refusal")
        self.assertEqual(answer["body"]["errorCode"], "helper_key_missing")
        self.assertIsNone(answer["keyId"])
        self.assertIsNone(answer["signature"])
        self.assertEqual(set(answer["body"]), {"contract", "errorCode"})

    def ask(self, env):
        return module_request(env, COMMAND, stdin_of(approval()), NOW)

    def test_a_missing_key(self):
        env = Env(self, key=False)
        answer, code, backend = self.ask(env)
        self.assertUnsigned(answer, code)
        self.assertEqual(backend.events, [])
        self.assertEqual(env.ledger, None)

    def test_a_symbolic_link_group_bits_a_directory_and_junk(self):
        cases = {}

        def link(env):
            real = env.state / "real.key"
            (env.state / "muster-helper.key").rename(real)
            os.symlink(real, env.state / "muster-helper.key")

        def mode(bits):
            return lambda env: os.chmod(env.state / "muster-helper.key", bits)

        def junk(env):
            write_file(env.state / "muster-helper.key", b"not a pem", 0o600)

        def empty(env):
            write_file(env.state / "muster-helper.key", b"", 0o600)

        def wrong_curve(env):
            pem = ec.generate_private_key(ec.SECP384R1()).private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            write_file(env.state / "muster-helper.key", pem, 0o600)

        def directory(env):
            (env.state / "muster-helper.key").unlink()
            (env.state / "muster-helper.key").mkdir()

        def encrypted(env):
            pem = HELPER.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(b"password"),
            )
            write_file(env.state / "muster-helper.key", pem, 0o600)

        cases = {
            "symbolic link": link,
            "mode 0644": mode(0o644),
            "mode 0640": mode(0o640),
            "mode 0604": mode(0o604),
            "unparseable": junk,
            "empty": empty,
            "wrong curve": wrong_curve,
            "a directory": directory,
            "encrypted": encrypted,
        }
        for name, damage in cases.items():
            with self.subTest(fault=name):
                env = Env(self)
                damage(env)
                answer, code, backend = self.ask(env)
                self.assertUnsigned(answer, code)
                self.assertEqual(backend.events, [])

    def test_a_state_directory_that_is_a_symbolic_link_or_open_to_others(self):
        env = Env(self)
        real = env.root / "real-state"
        env.state.rename(real)
        os.symlink(real, env.state)
        answer, code, _ = self.ask(env)
        self.assertUnsigned(answer, code)
        env = Env(self)
        os.chmod(env.state, 0o770)
        answer, code, _ = self.ask(env)
        self.assertUnsigned(answer, code)

    def test_a_key_owned_by_someone_else_is_unusable(self):
        env = Env(self)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW, uid=UID + 1)
        self.assertUnsigned(answer, code)

    def test_the_unsafe_key_code_is_one_named_constant(self):
        self.assertEqual(ma.UNSAFE_HELPER_KEY_CODE, "helper_key_missing")
        self.assertEqual(ma.ERR_HELPER_KEY_MISSING, ma.UNSAFE_HELPER_KEY_CODE)

    def test_the_key_is_checked_before_anything_else(self):
        env = Env(self, key=False)
        os.chmod(env.etc, 0o777)  # pin_unsafe, too: P0 still goes first
        answer, code, _ = module_request(env, "mac restart x --yes", b"junk", NOW)
        self.assertUnsigned(answer, code)


class PinTests(Checks):
    """P1: `/etc/muster` is read once, through the shared module, and only here is `pin_unsafe`."""

    def ask(self, env, command=COMMAND, stdin=None):
        return module_request(
            env, command, stdin if stdin is not None else stdin_of(approval()), NOW
        )

    def test_the_vector_pin_is_accepted_and_returns_what_it_read(self):
        env = Env(self)
        files = mt.read_trust_files(str(env.etc), UID)
        self.assertEqual(files.owner_key, FIXTURE["pin"])
        self.assertEqual(files.history[0]["toVersion"], None)
        self.assertIsNone(files.reopen_bytes)
        write_file(
            env.etc / "reopen-codes.jsonl", ("\n".join(FIXTURE["reopen"]) + "\n").encode(), 0o644
        )
        files = mt.read_trust_files(str(env.etc), UID)
        self.assertEqual(files.reopen_bytes.decode().splitlines(), FIXTURE["reopen"])
        answer, code, _ = self.ask(env, stdin=FIXTURE["approvals"][0]["stdin"].encode())
        self.assertDone(answer, code)

    def test_each_fault_is_pin_unsafe(self):
        owner = FIXTURE["pin"]
        history_line = FIXTURE["history"]

        def damage_file(name, data=None, mode=None):
            def apply(env):
                path = env.etc / name
                if data is not None:
                    write_file(path, data, 0o644)
                if mode is not None:
                    os.chmod(path, mode)

            return apply

        def link(name):
            def apply(env):
                real = env.root / ("real-" + name)
                (env.etc / name).rename(real)
                os.symlink(real, env.etc / name)

            return apply

        other = b64u(spki(KEYS["rogue owner (not pinned)"]))
        history_two = history_line.replace(
            '"toSHA256":null,"toVersion":null', '"toSHA256":"%s","toVersion":2' % ("a" * 64)
        )
        second = '{"fromVersion":3,"ownerKey":"%s","toSHA256":null,"toVersion":null}' % owner
        first_open_not_last = history_line + "\n" + second
        two_open = history_line + "\n" + second.replace('"fromVersion":3', '"fromVersion":2')
        gap = history_two + "\n" + second.replace('"fromVersion":3', '"fromVersion":4')
        reopen = ("\n".join(FIXTURE["reopen"]) + "\n").encode()

        def reopen_group_writable(env):
            write_file(env.etc / "reopen-codes.jsonl", reopen, 0o664)

        def reopen_link(env):
            write_file(env.root / "reopen-real", reopen, 0o644)
            os.symlink(env.root / "reopen-real", env.etc / "reopen-codes.jsonl")

        def reopen_directory(env):
            (env.etc / "reopen-codes.jsonl").mkdir()

        good_line = FIXTURE["reopen"][1]
        old_format = (
            '{"code":"%s","createdAt":"2026-11-07T20:45:00Z","version":4}'
            % "4f2a9c0e7b1d6e3a8c5f0b2d9e7a1c4b"
        )

        def reopen_with(data):
            return lambda env: write_file(env.etc / "reopen-codes.jsonl", data.encode(), 0o644)

        reopen_format_faults = {
            "a line of the old format (no deviceId)": old_format + "\n",
            "a line that is not canonical (spaces)": good_line.replace(",", ", ") + "\n",
            "a line without its line feed": good_line,
            "a blank line": good_line + "\n\n",
            "junk": "this is not even json\n",
            "a code of uppercase hex": good_line.replace("4f2a9c0e", "4F2A9C0E") + "\n",
            "a short code": good_line.replace("4f2a9c0e7b1d6e3a8c5f0b2d9e7a1c4b", "4f2a") + "\n",
            "a time that is no time": good_line.replace(
                "2026-11-07T20:45:00Z", "2026-13-07T20:45:00Z"
            )
            + "\n",
            "a bad device id": good_line.replace("d-f4e396a67ceefe6a551abbc0", "d-f4e3") + "\n",
            "version 0": good_line.replace('"version":4', '"version":0') + "\n",
            "version as a string": good_line.replace('"version":4', '"version":"4"') + "\n",
            "an extra key": good_line[:-1] + ',"x":1}\n',
            "a duplicate key": good_line.replace("{", '{"code":"x",', 1) + "\n",
            "a good line then a bad one": good_line + "\n" + old_format + "\n",
        }

        def missing_directory(env):
            for name in ("owner.pub", "owner-history.jsonl"):
                (env.etc / name).unlink()
            env.etc.rmdir()

        cases = {
            "owner.pub group-writable": damage_file("owner.pub", mode=0o664),
            "owner.pub world-writable": damage_file("owner.pub", mode=0o646),
            "owner.pub a symbolic link": link("owner.pub"),
            "owner.pub missing": lambda env: (env.etc / "owner.pub").unlink(),
            "owner.pub is not a key": damage_file("owner.pub", b"nonsense\n"),
            "owner.pub is another curve": damage_file(
                "owner.pub",
                (
                    b64u(
                        ec.generate_private_key(ec.SECP384R1())
                        .public_key()
                        .public_bytes(
                            serialization.Encoding.DER,
                            serialization.PublicFormat.SubjectPublicKeyInfo,
                        )
                    )
                    + "\n"
                ).encode(),
            ),
            "owner.pub has no line feed": damage_file("owner.pub", owner.encode()),
            "owner.pub has two lines": damage_file(
                "owner.pub", (owner + "\n" + owner + "\n").encode()
            ),
            "owner.pub is empty": damage_file("owner.pub", b""),
            "owner.pub is too large": damage_file("owner.pub", b"A" * 2000 + b"\n"),
            "history group-writable": damage_file("owner-history.jsonl", mode=0o664),
            "history a symbolic link": link("owner-history.jsonl"),
            "history missing": lambda env: (env.etc / "owner-history.jsonl").unlink(),
            "history empty": damage_file("owner-history.jsonl", b""),
            "history without line feed": damage_file("owner-history.jsonl", history_line.encode()),
            "history not canonical (spaces)": damage_file(
                "owner-history.jsonl", (history_line.replace(",", ", ") + "\n").encode()
            ),
            "history open entry is another key": damage_file(
                "owner-history.jsonl",
                (history_line.replace(owner, other) + "\n").encode(),
            ),
            "history does not start at version 1": damage_file(
                "owner-history.jsonl",
                (history_line.replace('"fromVersion":1', '"fromVersion":2') + "\n").encode(),
            ),
            "history has an open entry that is not last": damage_file(
                "owner-history.jsonl", (first_open_not_last + "\n").encode()
            ),
            "history has two open entries": damage_file(
                "owner-history.jsonl", (two_open + "\n").encode()
            ),
            "history is not contiguous": damage_file("owner-history.jsonl", (gap + "\n").encode()),
            "history closed entry has no hash": damage_file(
                "owner-history.jsonl",
                (history_line.replace('"toVersion":null', '"toVersion":2') + "\n").encode(),
            ),
            "history open entry has a hash": damage_file(
                "owner-history.jsonl",
                (
                    history_line.replace('"toSHA256":null', '"toSHA256":"%s"' % ("b" * 64)) + "\n"
                ).encode(),
            ),
            "history has a duplicate key": damage_file(
                "owner-history.jsonl",
                (history_line.replace("{", '{"fromVersion":1,', 1) + "\n").encode(),
            ),
            "history has an unknown key": damage_file(
                "owner-history.jsonl", (history_line[:-1] + ',"x":1}\n').encode()
            ),
            "reopen record group-writable": reopen_group_writable,
            "reopen record a symbolic link": reopen_link,
            "reopen record a directory": reopen_directory,
            **{
                "reopen record: " + name: reopen_with(data)
                for name, data in reopen_format_faults.items()
            },
            "/etc/muster group-writable": lambda env: os.chmod(env.etc, 0o775),
            "/etc/muster missing": missing_directory,
            "/etc/muster a symbolic link": lambda env: (
                env.etc.rename(env.root / "real-etc"),
                os.symlink(env.root / "real-etc", env.etc),
            ),
        }
        for name, damage in cases.items():
            with self.subTest(fault=name):
                env = Env(self)
                damage(env)
                answer, code, backend = self.ask(env)
                self.assertRefused(answer, code, "pin_unsafe", verified=False)
                self.assertEqual(backend.events, [])
                self.assertEqual(backend.rows, [])
                self.assertIsNone(env.ledger)

    def test_files_owned_by_another_uid_are_unsafe(self):
        env = Env(self)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW, root_uid=UID + 1)
        self.assertRefused(answer, code, "pin_unsafe", verified=False)

    def test_pin_unsafe_comes_before_the_command_line_and_standard_input(self):
        env = Env(self)
        os.chmod(env.etc / "owner.pub", 0o666)
        answer, code, _ = module_request(env, "mac restart x --yes", b"junk", NOW)
        self.assertRefused(answer, code, "pin_unsafe", verified=False)

    def test_an_empty_reopen_record_and_the_vector_record_are_accepted(self):
        # The record's absence is no error, and an empty one holds no line that could fail.
        for data in (b"", ("\n".join(FIXTURE["reopen"]) + "\n").encode()):
            with self.subTest(size=len(data)):
                env = Env(self)
                write_file(env.etc / "reopen-codes.jsonl", data, 0o644)
                answer, code, _ = self.ask(env, stdin=FIXTURE["approvals"][0]["stdin"].encode())
                self.assertDone(answer, code)

    def test_a_torn_final_line_of_the_reopen_record_is_pin_unsafe_and_never_repaired(self):
        # The ledger's torn-line rule (truncate and go on) is the ledger's alone.  A reopen record is
        # written by one root command that finishes its lines, so a final line without its line feed
        # is damage: `pin_unsafe` at P1, nothing repaired, nothing run.
        good = ("\n".join(FIXTURE["reopen"]) + "\n").encode()
        for name, data in (
            ("a torn line after good lines", good + b'{"code":"ab'),
            ("a whole line without its line feed", good[:-1]),
            ("only a torn line", b'{"code":"ab'),
        ):
            with self.subTest(case=name):
                env = Env(self)
                write_file(env.etc / "reopen-codes.jsonl", data, 0o644)
                answer, code, backend = self.ask(env)
                self.assertRefused(answer, code, "pin_unsafe", verified=False)
                self.assertEqual((env.etc / "reopen-codes.jsonl").read_bytes(), data)
                self.assertEqual(backend.events, [])
                self.assertEqual(backend.rows, [], "no ledger-repair row for the record")
                self.assertIsNone(env.ledger)

    def test_the_pin_files_are_read_once_per_request(self):
        env = Env(self)
        write_file(
            env.etc / "reopen-codes.jsonl", ("\n".join(FIXTURE["reopen"]) + "\n").encode(), 0o644
        )
        opened = []
        real = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append(str(path))
            return real(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", spy):
            answer, code, _ = self.ask(env, stdin=FIXTURE["approvals"][0]["stdin"].encode())
        self.assertDone(answer, code)
        for name in ("owner.pub", "owner-history.jsonl", "reopen-codes.jsonl"):
            self.assertEqual(opened.count(name), 1, (name, opened))

    def test_the_module_check_returns_the_record_it_read(self):
        env = Env(self)
        data = ("\n".join(FIXTURE["reopen"]) + "\n").encode()
        write_file(env.etc / "reopen-codes.jsonl", data, 0o644)
        pin = ma.check_pin(env.module_config(NOW), mt)
        self.assertEqual(pin.reopen_bytes, data)
        self.assertEqual([line["version"] for line in pin.reopen_lines], [4, 4])
        self.assertEqual(pin.reopen_lines[1]["deviceId"], "d-f4e396a67ceefe6a551abbc0")
        self.assertEqual(pin.owner_key, FIXTURE["pin"])
        self.assertEqual(pin.key_for_version(7), FIXTURE["pin"])


class CommandTests(Checks):
    def ask(self, command, argv_ok=True, backend=None):
        env = Env(self)
        stdin = stdin_of(approval())
        return module_request(env, command, stdin, NOW, backend, argv_ok)

    def test_the_verb_table(self):
        plan = job_stdin()
        env = Env(self)
        # The plan mode reaches the backend for every verb (rate limits aside: one call each).
        for index, (verb, target) in enumerate(
            (
                ("start", "fleet-server-status.timer"),
                ("stop", "fleet-server-status.timer"),
                ("restart", "fleet-server-status.timer"),
                ("logs", "fleet-server-status.timer"),
                ("update", "fleet-cli"),
                ("redeploy", "abc123def456"),
                ("resource-policy", "cpu-weight.100." + "a" * 64),
                ("resource-status", "coding"),
                ("seat-inventory", "claude"),
                ("update-status", "fleet-cli." + "a" * 32 + ".0"),
            )
        ):
            with self.subTest(verb=verb, mode="plan"):
                backend = FakeBackend()
                answer, code, backend = module_request(
                    env, f"server {verb} {target} --plan", plan, 1_800_000_000 + index * 10, backend
                )
                self.assertEqual(code, 0, answer)
                reads = verb in ("resource-status", "seat-inventory", "update-status")
                self.assertEqual(answer["kind"], "result" if reads else "plan")
                self.assertSigned(answer)
        for verb in ("resource-status", "seat-inventory", "update-status"):
            with self.subTest(verb=verb, mode="yes"):
                target = {"resource-status": "coding", "seat-inventory": "claude"}.get(verb, "abc")
                answer, code, backend = module_request(
                    env, f"server {verb} {target} --yes", stdin_of(approval()), NOW
                )
                self.assertRefused(answer, code, "mode_not_allowed", verified=False)
                self.assertEqual(backend.events, [])

    def test_everything_else_is_bad_command(self):
        for command in (
            "mac restart com.jay.x --yes",
            "pm2 restart shellular --yes",
            "muster trust-list other --plan",
            "muster trust-list current other --plan",
            "muster trust-list current --json",
            "muster other current --plan",
            "Muster trust-list current --plan",
            "muster trust-list current-list --plan",
            "server eavesdropper-status server --plan",
            "server eavesdropper-status server --yes",
            "server frobnicate x --yes",
            "server restart x",
            "server restart x --json",
            "server restart x --plan --json",
            "server restart x --YES",
            "server restart --yes",
            "server restart a b --yes",
            "server  restart x --yes",
            " server restart x --yes",
            "server restart x --yes ",
            "server restart x/y --yes",
            "server restart x;y --yes",
            "server restart x\ny --yes",
            "server restart café --yes",
            "server restart " + "a" * 129 + " --yes",
            "server " + "restart " * 40 + "x --yes",
            "server resource-status other --plan",
            "",
            "-",
        ):
            with self.subTest(command=command[:40]):
                answer, code, backend = self.ask(command)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(backend.events, [])

    def test_the_chain_verb_has_one_mode_and_its_plan_mode_is_not_allowed(self):
        # `--plan` is `mode_not_allowed` (section 4: chain accepts only `--yes`): not counted, nothing
        # claimed, no backend.  The `--yes` mode is tested from `ChainVectorTests` on.
        env = Env(self)
        answer, code, backend = module_request(
            env, "muster trust-list chain --plan", b'{"protocol":"muster-helper/1","chain":[]}', NOW
        )
        self.assertRefused(answer, code, "mode_not_allowed", verified=False)
        self.assertEqual(answer["body"]["claimed"], {})
        self.assertEqual(backend.events, [])
        self.assertFalse((env.state / "muster-rate.json").exists())
        self.assertIsNone(env.ledger)

    def test_with_nothing_stored_every_approval_is_no_trust_list(self):
        # Until a chain has been accepted nothing has written the stored list, so on a server where none
        # was put there every approval is `no_trust_list`.
        env = Env(self, stored=None)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        self.assertFalse((env.state / "muster-trust.json").exists())

    def test_a_command_over_256_characters_is_bad_command(self):
        answer, code, _ = self.ask("server restart " + "a" * 128 + " --yes " + "b" * 130)
        self.assertRefused(answer, code, "bad_command")

    def test_a_missing_command_or_extra_arguments(self):
        for command, argv_ok in ((None, True), (COMMAND, False)):
            with self.subTest(command=command, argv_ok=argv_ok):
                answer, code, backend = self.ask(command, argv_ok)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(backend.events, [])

    def test_the_helper_refuses_extra_arguments(self):
        env = Env(self)
        for argv in (["--from-muster", "--plan"], ["--from-ssh", "--from-muster"]):
            with self.subTest(argv=argv), entry_patches():
                out = io.StringIO()
                code = foc.main(
                    argv,
                    out=out,
                    ctx=foc.Context(platform="linux", home=env.home, runner=FakeRunner()),
                    muster={
                        "config": env.config(NOW),
                        "ssh_command": COMMAND,
                        "stdin": stdin_of(approval()),
                    },
                )
                self.assertRefused(json.loads(out.getvalue()), code, "bad_command")

    def test_the_command_class_matches_the_helpers_ssh_rules(self):
        self.assertEqual(ma.SSH_COMMAND_MAX, foc.SSH_COMMAND_MAX)
        self.assertEqual(ma.SSH_SAFE_RE.pattern, foc.SSH_SAFE_RE.pattern)

    def test_the_change_verbs_are_the_helpers_own(self):
        helper = {"start", "stop", "restart", "logs", "update", "redeploy", "resource-policy"}
        self.assertEqual(set(ma.CHANGE_VERBS), helper)
        self.assertEqual(set(ma.READ_VERBS), {"resource-status", "seat-inventory", "update-status"})
        self.assertNotIn("eavesdropper-status", ma.CHANGE_VERBS + ma.READ_VERBS)
        # The helper's `server unit-status` is for the ssh route only until the spec offers it here.
        self.assertNotIn("unit-status", ma.CHANGE_VERBS + ma.READ_VERBS)


class InputTests(Checks):
    """P3: every malformed input is `bad_input`, before anything is looked up."""

    def ask(self, raw, command=COMMAND):
        env = Env(self)
        answer, code, backend = module_request(env, command, raw, NOW)
        self.assertEqual(backend.events, [])
        self.assertEqual(backend.rows, [])
        self.assertIsNone(env.ledger)
        return answer, code

    def bad(self, raw, command=COMMAND):
        answer, code = self.ask(raw, command)
        self.assertRefused(answer, code, "bad_input", verified=False)
        return answer

    def test_oversize_input(self):
        answer, code = self.ask(b" " * 4097)
        self.assertRefused(answer, code, "input_too_large", verified=False)
        padded = stdin_of(approval()) + b" " * (4097 - len(stdin_of(approval())))
        answer, code = self.ask(padded)
        self.assertRefused(answer, code, "input_too_large")
        # Exactly the limit is not too large (here: not an approval, so bad_input).
        self.bad(b" " * 4096)
        # The helper reads one byte past the limit and no more.
        asked = []
        env = Env(self)
        ma.handle_request(
            env.module_config(NOW),
            FakeBackend(),
            COMMAND,
            True,
            lambda limit: asked.append(limit) or b"x" * (limit + 1),
            mt,
        )
        self.assertEqual(asked, [4096])

    def test_truncated_and_empty_input(self):
        good = stdin_of(approval())
        for cut in (0, 1, 10, len(good) // 2, len(good) - 1):
            with self.subTest(cut=cut):
                self.bad(good[:cut])

    def test_not_one_json_object(self):
        good = stdin_of(approval())
        for name, raw in (
            ("array", b"[" + good + b"]"),
            ("string", b'"x"'),
            ("number", b"7"),
            ("null", b"null"),
            ("two objects", good + good),
            ("trailing text", good + b" x"),
            ("leading text", b"x" + good),
            ("single quotes", good.replace(b'"', b"'")),
            ("comments", b"/*x*/" + good),
            ("trailing comma", good[:-1] + b",}"),
        ):
            with self.subTest(shape=name):
                self.bad(raw)

    def test_whitespace_and_key_order_do_not_matter(self):
        document = json.loads(stdin_of(approval()))
        env = Env(self)
        shuffled = json.dumps(document, indent=3, sort_keys=False).encode()
        answer, code, _ = module_request(env, COMMAND, b"\n\t " + shuffled + b" \r\n", NOW)
        self.assertDone(answer, code)

    def test_duplicate_keys(self):
        document = stdin_of(approval()).decode()
        self.bad(
            document.replace('{"approval"', '{"protocol":"muster-helper/1","approval"', 1).encode()
        )
        self.bad(
            document.replace(
                '"jobId":"job7k2m9q4x1"', '"jobId":"job7k2m9q4x1","jobId":"other"'
            ).encode()
        )
        self.bad(document[:-1].encode() + b',"approval":{}}')

    def test_unknown_and_missing_keys(self):
        good = approval()
        for name, document in (
            ("top-level extra", {"protocol": "muster-helper/1", "approval": good, "x": 1}),
            ("approval extra", {"protocol": "muster-helper/1", "approval": dict(good, extra="x")}),
            (
                "approval missing signature",
                {
                    "protocol": "muster-helper/1",
                    "approval": {k: v for k, v in good.items() if k != "signature"},
                },
            ),
            (
                "approval missing target",
                {
                    "protocol": "muster-helper/1",
                    "approval": {k: v for k, v in good.items() if k != "target"},
                },
            ),
            ("no approval", {"protocol": "muster-helper/1"}),
            ("a job instead", {"protocol": "muster-helper/1", "job": JOB}),
            ("wrong protocol", {"protocol": "muster-helper/2", "approval": good}),
            ("no protocol", {"approval": good}),
            ("approval is a list", {"protocol": "muster-helper/1", "approval": [good]}),
            ("approval is null", {"protocol": "muster-helper/1", "approval": None}),
        ):
            with self.subTest(case=name):
                self.bad(compact(document))

    def test_the_wrong_document_for_the_mode(self):
        self.bad(stdin_of(approval()), PLAN_COMMAND)
        self.bad(job_stdin(), COMMAND)

    def test_not_utf8(self):
        good = stdin_of(approval())
        for name, raw in (
            ("invalid byte", good.replace(b"job7k2m9q4x1", b"job\xff7k2m9q4x1")),
            ("lone continuation", b'{"a":"\x80"}'),
            ("overlong slash", b'{"a":"\xc0\xaf"}'),
            ("surrogate", b'{"a":"\xed\xa0\x80"}'),
            ("utf-16", good.decode().encode("utf-16")),
            ("byte order mark", b"\xef\xbb\xbf" + good),
        ):
            with self.subTest(case=name):
                self.bad(raw)

    def test_control_characters_nul_and_non_ascii(self):
        good = stdin_of(approval()).decode()
        for name, raw in (
            ("raw tab in a string", good.replace("job7k2m9q4x1", "job\t1")),
            ("raw newline in a string", good.replace("job7k2m9q4x1", "job\n1")),
            ("raw NUL", good.replace("job7k2m9q4x1", "job\x001")),
            ("escaped NUL", good.replace("job7k2m9q4x1", "job\\u00001")),
            ("escaped bell", good.replace("job7k2m9q4x1", "job\\u00071")),
            ("escaped DEL", good.replace("job7k2m9q4x1", "job\\u007f1")),
            ("escaped e-acute", good.replace("job7k2m9q4x1", "job\\u00e91")),
            ("raw e-acute", good.replace("job7k2m9q4x1", "jobé1")),
            ("non-ASCII key", good.replace('"jobId"', '"jobIé"')),
            (
                "escaped slash is fine for JSON but not for the pattern",
                good.replace("job7k2m9q4x1", "job\\/1"),
            ),
            ("escaped quote", good.replace("job7k2m9q4x1", 'job\\"1')),
            ("emoji", good.replace("job7k2m9q4x1", "job\U0001f600")),
        ):
            with self.subTest(case=name):
                self.bad(raw.encode("utf-8"))

    def test_huge_numbers_and_depth(self):
        good = stdin_of(approval()).decode()
        for name, raw in (
            ("float", good[:-1] + ',"n":1.5}'),
            ("exponent", good[:-1] + ',"n":1e999}'),
            ("huge exponent in a field", good.replace('"jobId":"job7k2m9q4x1"', '"jobId":1e999')),
            ("huge integer", good[:-1] + ',"n":' + "9" * 3000 + "}"),
            ("integer beyond 2^53", good[:-1] + ',"n":9007199254740993}'),
            ("negative huge", good[:-1] + ',"n":-' + "9" * 40 + "}"),
            ("NaN", good[:-1] + ',"n":NaN}'),
            ("Infinity", good[:-1] + ',"n":Infinity}'),
            ("minus Infinity", good[:-1] + ',"n":-Infinity}'),
            (
                "a number where a string is wanted",
                good.replace('"jobId":"job7k2m9q4x1"', '"jobId":7'),
            ),
            (
                "a boolean where a string is wanted",
                good.replace('"jobId":"job7k2m9q4x1"', '"jobId":true'),
            ),
            ("depth", "[" * 2000 + "]" * 2000),
            ("depth in objects", '{"a":' * 700 + "1" + "}" * 700),
            ("depth that was cut", "[" * 3000 + "]" * 3000),
        ):
            with self.subTest(case=name):
                self.bad(raw.encode()[:4096])

    def test_each_field_pattern_is_enforced_alone(self):
        bad_values = {
            "jobId": ["", "JOB", "job_1", "a" * 27, "job 1", "job-1"],
            "jobNonce": ["", "0" * 31, "0" * 33, "F" * 32, "g" * 32, "0" * 31 + " "],
            "approvalNonce": ["", "a" * 31, "a" * 33, "A" * 32, "z" * 32],
            "host": ["", "a b", "a/b", "a" * 129, "café", "a;b", "a\nb"],
            "action": ["", "a b", "a/b", "a" * 129],
            "target": ["", "a b", "a/b", "a" * 129, "../x"],
            "planSHA256": ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
            "deviceId": [
                "",
                "d-" + "a" * 23,
                "d-" + "a" * 25,
                "d-" + "A" * 24,
                "x-" + "a" * 24,
                "D-" + "a" * 24,
                "a" * 26,
            ],
            "issuedAt": [
                "",
                "2026-11-07 21:00:00Z",
                "2026-11-07T21:00:00",
                "2026-11-07T21:00:00+00:00",
                "2026-13-07T21:00:00Z",
                "2026-11-31T21:00:00Z",
                "2026-11-07T25:00:00Z",
                "2026-11-07T21:00:60Z",
                "2026-11-07t21:00:00Z",
                "2026-11-07T21:00:00.5Z",
                "26-11-07T21:00:00Z",
                "2026-11-07T21:00:00Zz",
                "٢026-11-07T21:00:00Z",
            ],
            "expiresAt": ["", "2026-11-07T21:02:00", "tomorrow", "2026-02-30T00:00:00Z"],
            "signature": ["", "A" * 101, "AAAA=", "AA AA", "AA/AA", "AA+AA", "é"],
        }
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=value[:30]):
                    document = {
                        "protocol": "muster-helper/1",
                        "approval": dict(approval(), **{field: value}),
                    }
                    self.bad(json.dumps(document).encode())

    def test_equal_nonces_are_bad_input(self):
        nonce = BASE_APPROVAL["jobNonce"]
        self.bad(stdin_of(approval(approvalNonce=nonce)))

    def test_a_plan_document_is_strict_too(self):
        env = Env(self)
        for name, document in (
            ("extra", {"protocol": "muster-helper/1", "job": dict(JOB, x="y")}),
            ("missing", {"protocol": "muster-helper/1", "job": {"jobId": JOB["jobId"]}}),
            ("bad deviceId", {"protocol": "muster-helper/1", "job": dict(JOB, deviceId="d-1")}),
            ("bad jobNonce", {"protocol": "muster-helper/1", "job": dict(JOB, jobNonce="x")}),
            ("approval", {"protocol": "muster-helper/1", "approval": approval()}),
        ):
            with self.subTest(case=name):
                answer, code, backend = module_request(env, PLAN_COMMAND, compact(document), NOW)
                self.assertRefused(answer, code, "bad_input", verified=False)
                self.assertEqual(backend.events, [])

    def test_a_stdin_that_never_ends_is_bad_input(self):
        env = Env(self)

        def never(limit):
            raise TimeoutError("standard input did not end in time")

        answer, code = ma.handle_request(
            env.module_config(NOW), FakeBackend(), COMMAND, True, never, mt
        )
        self.assertRefused(answer, code, "bad_input")

    def test_read_stdin_limited_reads_to_the_end_within_its_limit(self):
        read_end, write_end = os.pipe()
        os.write(write_end, b"x" * 100)
        os.close(write_end)
        self.assertEqual(ma.read_stdin_limited(read_end, 50), b"x" * 51)
        os.close(read_end)
        read_end, write_end = os.pipe()
        try:
            with self.assertRaises(TimeoutError):
                self.run_within(
                    10,
                    lambda: ma.read_stdin_limited(read_end, 50, timeout=0.2),
                    unblock=lambda: os.write(write_end, b"x" * 51),
                )
        finally:
            os.close(read_end)
            os.close(write_end)

    def test_a_refusal_at_p1_to_p3_claims_nothing(self):
        # Spec section 13: `claimed` is empty for a refusal at P1 to P3, whatever the document held.
        document = {
            "protocol": "muster-helper/1",
            "approval": dict(approval(), jobId="BAD ID", approvalNonce="x"),
        }
        for raw in (
            json.dumps(document).encode(),
            b"\xff",
            stdin_of(approval(approvalNonce=JOB["jobNonce"])),
        ):
            with self.subTest(raw=raw[:20]):
                answer = self.bad(raw)
                self.assertEqual(answer["body"]["claimed"], {})
        answer, code = self.ask(b" " * 5000)
        self.assertEqual(answer["body"]["claimed"], {})
        answer, code = self.ask(stdin_of(approval()), "server mac restart x --yes")
        self.assertEqual(answer["body"]["claimed"], {})

    def test_a_refusal_after_p3_claims_the_fields_that_parsed(self):
        # An approval refusal after P3 names all eight fields under `claimed`; a plan refusal names the
        # three job fields and nothing else.
        env = Env(self)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval(host="mac")), NOW)
        self.assertRefused(answer, code, "field_mismatch", verified=False)
        self.assertEqual(
            answer["body"]["claimed"],
            {
                "jobId": BASE_APPROVAL["jobId"],
                "jobNonce": BASE_APPROVAL["jobNonce"],
                "approvalNonce": BASE_APPROVAL["approvalNonce"],
                "deviceId": BASE_APPROVAL["deviceId"],
                "host": "mac",
                "action": "restart",
                "target": "fleet-server-status.timer",
                "planSHA256": BASE_APPROVAL["planSHA256"],
            },
        )
        backend = FakeBackend()
        backend.refuse_plan = "no"
        backend.refuse_code = "allowlist_miss"
        answer, code, _ = module_request(Env(self), PLAN_COMMAND, job_stdin(), NOW, backend)
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="allowlist_miss")
        self.assertEqual(answer["body"]["claimed"], JOB)
        # A plan request that fails at P3 claims nothing.
        answer, code, _ = module_request(Env(self), PLAN_COMMAND, b"junk", NOW)
        self.assertEqual(answer["body"]["claimed"], {})


# MARK: - Y2 to Y6: the stored list, the binding and the window


class TrustListTests(Checks):
    def ask(self, env, approval_dict=None, now=NOW, backend=None):
        return module_request(env, COMMAND, stdin_of(approval_dict or approval()), now, backend)

    def test_nothing_stored_a_closed_list_and_an_unlisted_device(self):
        answer, code, _ = self.ask(Env(self, stored=None))
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        self.assertIsNone(answer["body"]["helperTrustListVersion"])
        answer, code, _ = self.ask(Env(self, stored="v3"))
        self.assertRefused(answer, code, "unknown_device", verified=False)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 3)
        answer, code, _ = self.ask(
            Env(self), approval(signer=KEYS["device B approval"], deviceId=DEVICE_B)
        )
        self.assertRefused(answer, code, "unknown_device", verified=False)

    def test_a_list_that_is_not_signed_by_the_current_owner_key_is_no_list(self):
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        rogue = KEYS["rogue owner (not pinned)"]
        cases = {
            "signed by another key, naming the pinned key": resign(body, rogue),
            "naming and signed by another key": resign(
                dict(body, ownerKey=b64u(spki(rogue))), rogue
            ),
            "signed under the countersignature prefix": resign(
                body, OWNER, b"muster-trust-countersign/1\n"
            ),
            "signed over the bare body": resign(body, OWNER, b""),
            "signed under the approval head": resign(body, OWNER, b"muster-approve/1\n"),
            "body changed after signing": dict(
                FIXTURE["lists"]["v1"], body=dict(body, issuedAt="2026-11-07T20:00:01Z")
            ),
            "no signature that fits": dict(FIXTURE["lists"]["v1"], ownerSignature="AAAA"),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                env = Env(self, stored=document)
                answer, code, backend = self.ask(env)
                self.assertRefused(answer, code, "no_trust_list", verified=False)
                self.assertEqual(backend.events, [])
                # Section 13: the version of the stored list the request read, whoever signed it.
                # The refusal is what says that it is not the current owner's.
                self.assertEqual(answer["body"]["helperTrustListVersion"], 1)

    def test_a_list_signed_by_the_owner_over_the_right_prefix_is_accepted(self):
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        env = Env(self, stored=resign(body, OWNER))
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)

    def test_a_retired_owner_key_authorises_nothing(self):
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        history = {
            "fromVersion": 1,
            "ownerKey": b64u(spki(KEYS["rogue owner (not pinned)"])),
            "toSHA256": "c" * 64,
            "toVersion": 3,
        }
        new = {"fromVersion": 4, "ownerKey": FIXTURE["pin"], "toSHA256": None, "toVersion": None}
        env = Env(self)
        write_file(
            env.etc / "owner-history.jsonl",
            (ma.canonical_bytes(history) + b"\n" + ma.canonical_bytes(new) + b"\n"),
            0o644,
        )
        # v1 names and is signed by the pinned key, but the history says another key covered version 1.
        answer, code, _ = self.ask(env)
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        self.assertEqual(body["version"], 1)

    def test_an_unsafe_or_damaged_stored_file_is_state_unsafe_never_nothing(self):
        good = json.dumps(FIXTURE["lists"]["v1"]).encode()

        def mode(bits):
            return lambda env: os.chmod(env.state / "muster-trust.json", bits)

        def replace(data):
            return lambda env: write_file(env.state / "muster-trust.json", data, 0o600)

        def link(env):
            real = env.root / "real-trust"
            (env.state / "muster-trust.json").rename(real)
            os.symlink(real, env.state / "muster-trust.json")

        def fifo(env):
            (env.state / "muster-trust.json").unlink()
            os.mkfifo(env.state / "muster-trust.json", 0o600)

        def directory(env):
            (env.state / "muster-trust.json").unlink()
            (env.state / "muster-trust.json").mkdir()

        broken = json.loads(good)
        del broken["body"]["devices"][0]["name"]
        cases = {
            "world-writable": mode(0o666),
            "group-readable": mode(0o640),
            "world-readable": mode(0o604),
            "empty": replace(b""),
            "unparseable": replace(b"{"),
            "not an object": replace(b"[]"),
            "a symbolic link": link,
            "a FIFO": fifo,
            "a directory": directory,
            "too large": replace(b" " * (ma.TRUST_FILE_MAX_BYTES + 1) + good),
            "a device without a name": replace(json.dumps(broken).encode()),
            "duplicate key": replace(good.replace(b"{", b'{"body":1,', 1)),
            "non-ASCII": replace(good.replace(b"Test Phone A", "Test Phoné".encode())),
        }
        for name, damage in cases.items():
            with self.subTest(fault=name):
                env = Env(self)
                damage(env)
                answer, code, backend = self.run_within(
                    10, lambda: self.ask(env), unblock=lambda: self.release_fifos(env.state)
                )
                self.assertRefused(answer, code, "state_unsafe", verified=False)
                self.assertEqual(backend.events, [])
                self.assertIsNone(env.ledger)

    def test_the_trust_file_is_never_created_by_a_request(self):
        env = Env(self, stored=None)
        self.ask(env)
        self.assertFalse((env.state / "muster-trust.json").exists())
        self.assertEqual([p.name for p in env.state.iterdir() if p.name.startswith(".")], [])

    def test_the_list_that_moved_between_y2_and_y7_is_trust_list_changed(self):
        def swap_to_closed(env):
            return lambda: env.store(FIXTURE["lists"]["v3"])

        def remove(env):
            return lambda: (env.state / "muster-trust.json").unlink()

        def corrupt(env):
            return lambda: write_file(env.state / "muster-trust.json", b"junk", 0o600)

        def widen(env):
            body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
            body["issuedAt"] = "2026-11-07T20:00:01Z"
            return lambda: env.store(resign(body))

        for name, make, expected in (
            ("replaced by the closed list", swap_to_closed, "trust_list_changed"),
            ("removed", remove, "trust_list_changed"),
            ("replaced by a newer v1 body", widen, "trust_list_changed"),
            ("corrupted", corrupt, "state_unsafe"),
        ):
            with self.subTest(change=name):
                env = Env(self)
                backend = FakeBackend(on_lock=make(env))
                answer, code, backend = self.ask(env, backend=backend)
                self.assertRefused(answer, code, expected, verified=True)
                self.assertNotIn("run", backend.events)
                self.assertNotIn("plan", backend.events)
                self.assertIsNone(env.ledger, "the nonce must not be spent")
                self.assertEqual(backend.rows, [], "a refusal before Y8 writes no row")

    def test_the_same_body_with_another_signature_is_not_a_change(self):
        # The hash is of the canonical body: re-signing the same body leaves it equal.
        def resign_same(env):
            body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
            return lambda: env.store(
                dict(resign(body), ownerSignature=FIXTURE["lists"]["v1"]["ownerSignature"])
            )

        env = Env(self)
        answer, code, _ = self.ask(env, backend=FakeBackend(on_lock=resign_same(env)))
        self.assertDone(answer, code)

    def test_signature_and_window_are_checked_before_the_lock_is_taken(self):
        # OPEN-4: a request that cannot be authentic never holds ops-control.lock.
        for name, kwargs, now in (
            ("bad signature", {"signer": KEYS["device A request"]}, NOW),
            ("not listed", {"deviceId": DEVICE_B}, NOW),
            ("wrong host", {"host": "mac"}, NOW),
            ("expired", {}, "2026-11-07T21:02:01Z"),
            ("window too long", {"expiresAt": "2026-11-07T21:05:00Z"}, NOW),
            (
                "not yet valid",
                {"issuedAt": "2026-11-07T21:10:00Z", "expiresAt": "2026-11-07T21:12:00Z"},
                NOW,
            ),
        ):
            with self.subTest(case=name):
                env = Env(self)
                backend = FakeBackend()
                kwargs = dict(kwargs)
                signer = kwargs.pop("signer", DEVICE_A)
                answer, code, backend = self.ask(env, approval(signer, **kwargs), now, backend)
                self.assertEqual(code, 2)
                self.assertNotIn("lock", backend.events, answer)
        # A busy lock only matters to a request that is already authentic.
        env = Env(self)
        answer, code, backend = self.ask(
            env, approval(signer=KEYS["device A request"]), backend=FakeBackend(busy=True)
        )
        self.assertRefused(answer, code, "bad_signature")
        env = Env(self)
        answer, code, backend = self.ask(env, backend=FakeBackend(busy=True))
        self.assertRefused(answer, code, "action_busy", verified=True)
        self.assertIsNone(env.ledger)
        self.assertEqual(backend.rows, [], "a refusal before Y8 writes no row")


class BindingTests(Checks):
    """A signature by the right key is worth nothing for another action, host, target, plan or device."""

    def ask(self, env, approval_dict, now=NOW, backend=None, command=COMMAND):
        return module_request(env, command, stdin_of(approval_dict), now, backend)

    def test_another_action_host_or_target(self):
        for name, over, expected in (
            ("action", {"action": "stop"}, "field_mismatch"),
            ("action to a read verb", {"action": "resource-status"}, "field_mismatch"),
            ("host", {"host": "mac"}, "field_mismatch"),
            ("host with another case", {"host": "Server"}, "field_mismatch"),
            ("target", {"target": "botfleet-server.service"}, "field_mismatch"),
            ("target with a suffix", {"target": "fleet-server-status.timer2"}, "field_mismatch"),
        ):
            with self.subTest(case=name):
                env = Env(self)
                answer, code, backend = self.ask(env, approval(**over))
                self.assertRefused(answer, code, expected, verified=False)
                self.assertEqual(backend.events, [])
                self.assertIsNone(env.ledger)

    def test_the_command_not_the_approval_decides_what_is_checked(self):
        env = Env(self)
        answer, code, _ = self.ask(
            env, approval(), command="server stop fleet-server-status.timer --yes"
        )
        self.assertRefused(answer, code, "field_mismatch")
        answer, code, _ = self.ask(
            env, approval(), command="server restart botfleet-server.service --yes"
        )
        self.assertRefused(answer, code, "field_mismatch")

    def test_another_plan_hash_is_refused_after_the_nonce_is_spent(self):
        other = hashlib.sha256(b"another plan").hexdigest()
        env = Env(self)
        answer, code, backend = self.ask(env, approval(planSHA256=other))
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertEqual(answer["body"]["planSHA256"], other)
        self.assertNotIn("run", backend.events)
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        self.assertEqual(backend.rows[0]["errorCode"], "plan_mismatch")

    def test_the_plan_hash_is_compared_in_full(self):
        real = BASE_APPROVAL["planSHA256"]
        for name, other in (
            ("last character", real[:-1] + ("0" if real[-1] != "0" else "1")),
            ("first character", ("0" if real[0] != "0" else "1") + real[1:]),
        ):
            with self.subTest(case=name):
                env = Env(self)
                answer, code, backend = self.ask(env, approval(planSHA256=other))
                self.assertRefused(answer, code, "plan_mismatch", verified=True)
                self.assertNotIn("run", backend.events)

    def test_another_device_another_key(self):
        env = Env(self)
        b = KEYS["device B approval"]
        answer, code, _ = self.ask(env, approval(signer=b))
        self.assertRefused(answer, code, "bad_signature", verified=False)
        answer, code, _ = self.ask(Env(self), approval(signer=b, deviceId=DEVICE_B))
        self.assertRefused(answer, code, "unknown_device", verified=False)
        for label in (
            "device A request",
            "device X approval (not on any list)",
            "rogue helper (not enrolled)",
            "owner",
        ):
            with self.subTest(signer=label):
                answer, code, _ = self.ask(Env(self), approval(signer=KEYS[label]))
                self.assertRefused(answer, code, "bad_signature", verified=False)

    def test_the_reading_and_notification_keys_never_approve(self):
        body = FIXTURE["lists"]["v1"]["body"]["devices"][0]
        self.assertNotEqual(body["readingKey"], body["approvalKey"])
        for label in ("reading", "notification"):
            with self.subTest(role=label):
                answer, code, _ = self.ask(Env(self), approval(signer=derive("device A " + label)))
                self.assertRefused(answer, code, "bad_signature")

    def test_a_signature_over_another_prefix_or_text_does_not_verify(self):
        text = text_of(dict(BASE_APPROVAL))
        for name, message in (
            ("trust-list prefix", b"muster-trust-list/1\n" + text),
            ("countersign prefix", b"muster-trust-countersign/1\n" + text),
            ("trailing line feed", text + b"\n"),
            ("a different field", text.replace(b"restart", b"stop")),
            ("empty", b""),
        ):
            with self.subTest(case=name):
                forged = dict(BASE_APPROVAL, signature=b64u(sign(DEVICE_A, message)))
                answer, code, _ = self.ask(Env(self), forged)
                self.assertRefused(answer, code, "bad_signature", verified=False)

    def test_odd_signatures_are_bad_signature(self):
        good = sign(DEVICE_A, text_of(dict(BASE_APPROVAL)))
        for name, signature in (
            ("truncated", good[:-2]),
            ("extended", good + b"\x00"),
            ("flipped last byte", good[:-1] + bytes([good[-1] ^ 1])),
            ("flipped first data byte", good[:4] + bytes([good[4] ^ 1]) + good[5:]),
            ("zeros", b"\x00" * 64),
            ("raw r||s", good[-64:]),
            ("a single byte", b"\x30"),
        ):
            with self.subTest(case=name):
                forged = dict(BASE_APPROVAL, signature=b64u(signature))
                answer, code, _ = self.ask(Env(self), forged)
                self.assertRefused(answer, code, "bad_signature", verified=False)

    def test_a_signature_that_parses_but_is_not_base64url_canonical(self):
        good = approval()["signature"]
        # Change the last character to another that decodes to the same bytes: stray trailing bits.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        last = alphabet.index(good[-1])
        if len(good) % 4 in (2, 3):
            variant = good[:-1] + alphabet[last ^ 1]
            answer, code, _ = self.ask(Env(self), dict(BASE_APPROVAL, signature=variant))
            self.assertRefused(answer, code, "bad_signature", verified=False)

    def test_check_order_binding_before_signature_before_window_before_ledger_before_plan(self):
        # Two faults at once: the earlier step's code wins.
        late = "2026-11-07T21:03:00Z"
        other_plan = hashlib.sha256(b"x").hexdigest()
        cases = (
            (
                "mismatch and bad signature",
                dict(host="mac"),
                KEYS["device A request"],
                NOW,
                "field_mismatch",
            ),
            (
                "unknown device and bad signature",
                dict(deviceId=DEVICE_B),
                DEVICE_A,
                NOW,
                "unknown_device",
            ),
            ("bad signature and expired", {}, KEYS["device A request"], late, "bad_signature"),
            (
                "window too long and not yet valid",
                dict(issuedAt="2026-11-07T21:10:00Z", expiresAt="2026-11-07T21:30:00Z"),
                DEVICE_A,
                NOW,
                "window_too_long",
            ),
            (
                "window too long and expired",
                dict(expiresAt="2026-11-07T21:05:00Z"),
                DEVICE_A,
                "2026-11-07T21:10:00Z",
                "window_too_long",
            ),
            ("expired and replayed", {}, DEVICE_A, late, "expired"),
            (
                "replayed and plan mismatch",
                dict(planSHA256=other_plan),
                DEVICE_A,
                NOW,
                "replayed_nonce",
            ),
        )
        for name, over, signer, now, expected in cases:
            with self.subTest(case=name):
                env = Env(self, ledger=ledger_line(BASE_APPROVAL["approvalNonce"]))
                answer, code, _ = self.ask(env, approval(signer, **over), now)
                self.assertRefused(answer, code, expected)

    def test_stored_list_faults_come_before_the_binding(self):
        answer, code, _ = self.ask(Env(self, stored=None), approval(host="mac"))
        self.assertRefused(answer, code, "no_trust_list")


class WindowTests(Checks):
    def ask(self, now, **over):
        env = Env(self)
        return module_request(env, COMMAND, stdin_of(approval(**over)), now)

    def test_the_edges_of_the_window(self):
        issued = epoch("2026-11-07T21:00:00Z")
        for name, now, over, expected in (
            ("now equals expiresAt", issued + 120, {}, "accepted"),
            ("one second past expiresAt", issued + 121, {}, "expired"),
            ("half a second past expiresAt (no grace)", issued + 120.5, {}, "expired"),
            ("a millisecond past expiresAt", issued + 120.001, {}, "expired"),
            ("just before expiresAt", issued + 119.999, {}, "accepted"),
            ("issuedAt equals now", issued, {}, "accepted"),
            ("issued exactly 60 s ahead", issued - 60, {}, "accepted"),
            ("issued 61 s ahead", issued - 61, {}, "not_yet_valid"),
            ("issued 60.001 s ahead", issued - 60.001, {}, "not_yet_valid"),
            ("a span of exactly 120 s", issued + 30, {}, "accepted"),
            (
                "a span of 121 s",
                issued + 30,
                {"expiresAt": "2026-11-07T21:02:01Z"},
                "window_too_long",
            ),
            ("a span of 1 s", issued, {"expiresAt": "2026-11-07T21:00:01Z"}, "accepted"),
            ("a span of 0 s", issued, {"expiresAt": "2026-11-07T21:00:00Z"}, "window_too_long"),
            ("a negative span", issued, {"expiresAt": "2026-11-07T20:59:59Z"}, "window_too_long"),
            (
                "a span of 300 s",
                issued + 30,
                {"expiresAt": "2026-11-07T21:05:00Z"},
                "window_too_long",
            ),
        ):
            with self.subTest(case=name):
                answer, code, _ = self.ask(now, **over)
                if expected == "accepted":
                    self.assertDone(answer, code)
                else:
                    self.assertRefused(answer, code, expected, verified=True)

    def test_a_clock_61_seconds_ahead_of_the_approval(self):
        answer, code, _ = self.ask("2026-11-07T20:58:59Z")
        self.assertRefused(answer, code, "not_yet_valid", verified=True)
        answer, code, _ = self.ask("2026-11-07T20:59:00Z")
        self.assertDone(answer, code)

    def test_the_clock_is_read_by_the_helper_not_the_request(self):
        # The approval cannot say what time it is: the injected clock alone decides.
        answer, code, _ = self.ask("2030-01-01T00:00:00Z")
        self.assertRefused(answer, code, "expired", verified=True)


# MARK: - Y8: the nonce ledger


class LedgerTests(Checks):
    def ask(self, env, now=NOW, backend=None, **over):
        return module_request(env, COMMAND, stdin_of(approval(**over)), now, backend)

    def test_the_first_use_is_recorded_and_the_second_is_a_replay(self):
        env = Env(self)
        answer, code, backend = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(env.ledger, ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW))
        self.assertEqual(stat.S_IMODE((env.state / "muster-nonces.jsonl").stat().st_mode), 0o600)
        answer, code, backend = self.ask(env, now=later(3), backend=FakeBackend())
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertEqual(backend.rows, [], "a replay adds no row: one row per nonce, ever")

    def test_another_nonce_is_not_a_replay(self):
        env = Env(self, ledger=ledger_line("1" * 32))
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(
            env.ledger,
            ledger_line("1" * 32) + ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW),
        )

    def test_the_line_is_durable_before_anything_runs(self):
        env = Env(self)
        backend = FakeBackend()
        real = os.fsync

        def spy(fd):
            backend.events.append("fsync")
            return real(fd)

        real_rate = ma.check_rate

        def rate(*args, **kwargs):
            backend.events.append("rate_start")
            try:
                return real_rate(*args, **kwargs)
            finally:
                backend.events.append("rate_done")

        with mock.patch.object(ma.os, "fsync", spy), mock.patch.object(ma, "check_rate", rate):
            answer, code, backend = self.ask(env, backend=backend)
        self.assertDone(answer, code)
        events = backend.events
        self.assertLess(events.index("lock"), events.index("rate_start"))
        # The rate event is recorded durably (its file and its directory) before the nonce is
        # appended, and the nonce's own fsync comes before anything is planned or run.
        between = events[events.index("rate_start") : events.index("rate_done")]
        self.assertEqual(between.count("fsync"), 2)
        after = events[events.index("rate_done") :]
        self.assertLess(after.index("fsync"), after.index("plan"))
        self.assertLess(max(i for i, e in enumerate(after) if e == "fsync"), after.index("plan"))
        self.assertLess(after.index("fsync"), after.index("run"))
        # A new ledger file is fsynced and so is its directory: two calls.
        self.assertEqual(after.count("fsync"), 2)
        events.clear()
        with mock.patch.object(ma.os, "fsync", spy):
            self.ask(env, now=later(3), backend=backend, approvalNonce="2" * 32)
        self.assertEqual(
            events.count("fsync"), 3, "the rate event's two and an existing ledger's one"
        )

    def test_the_nonce_is_spent_even_when_the_run_fails_or_the_plan_differs(self):
        for name, backend, expected_kind in (
            ("plan mismatch", FakeBackend(plan=OTHER_PLAN), "refusal"),
            ("failed run", FakeBackend(outcome="failed"), "result"),
        ):
            with self.subTest(case=name):
                env = Env(self)
                answer, code, backend = self.ask(env, backend=backend)
                self.assertEqual(answer["kind"], expected_kind)
                self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
                answer, code, _ = self.ask(env, now=later(3), backend=FakeBackend())
                self.assertRefused(answer, code, "replayed_nonce")

    def test_a_run_that_raises_is_answered_as_unknown_and_stays_spent(self):
        # The run was called, so the action may have begun: its end cannot be told, and a phone that
        # retried on `failed` could run it twice.
        class Broken(FakeBackend):
            def run(self, verb, target, approval_fields):
                raise RuntimeError("boom")

        env = Env(self)
        answer, code, backend = self.ask(env, backend=Broken())
        self.assertEqual(code, 1)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertSigned(answer)
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        self.assertEqual(backend.rows[0]["result"], "unknown")

    def test_a_torn_final_fragment_is_truncated_and_the_request_proceeds(self):
        whole = ledger_line("1" * 32)
        torn = ledger_line("3" * 32)[:-9]
        for name, fragment in (
            ("a prefix of a line", torn),
            ("a lone brace", b"{"),
            ("junk", b"\x00\x01zz"),
        ):
            with self.subTest(case=name):
                env = Env(self, ledger=whole + fragment)
                answer, code, backend = self.ask(env)
                self.assertDone(answer, code)
                self.assertEqual(
                    env.ledger, whole + ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW)
                )
                repair = [row for row in backend.rows if row["verb"] == "ledger-repair"]
                self.assertEqual(len(repair), 1)
                self.assertEqual(repair[0]["result"], "repaired")
                self.assertEqual(
                    (repair[0]["scope"], repair[0]["target"]), ("muster", "muster-nonces.jsonl")
                )
                self.assertEqual(
                    {k for k in repair[0] if k not in ("scope", "verb", "target", "result")},
                    {"route", "via"},
                    "a repair row has the route fields and no approval's",
                )

    def test_the_repair_rows_through_the_helper_are_named_muster_ledger_repair(self):
        for name, ledger, result in (
            ("torn", ledger_line("1" * 32)[:20], "repaired"),
            ("complete without a line feed", ledger_line("1" * 32)[:-1], "completed"),
        ):
            with self.subTest(case=name):
                env = Env(self, ledger=ledger)
                answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
                self.assertDone(answer, code)
                self.assertEqual(
                    [(row["verb"], row["result"]) for row in rows],
                    [("muster ledger-repair", result), ("server restart", "done")],
                )
                repair = rows[0]
                self.assertEqual(repair["target"], "muster-nonces.jsonl")
                self.assertEqual((repair["route"], repair["via"]), ("from-muster", "muster-ios"))
                for absent in ("jobId", "deviceId", "approvalNonce", "planSHA256", "errorCode"):
                    self.assertNotIn(absent, repair)

    def test_a_torn_fragment_that_is_the_whole_file(self):
        env = Env(self, ledger=ledger_line("1" * 32)[:20])
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(env.ledger, ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW))

    def test_a_complete_final_line_without_a_line_feed_stays_spent(self):
        # OPEN-3: the line feed is appended, the nonce stays spent, and an audit row says so.
        nonce = "4" * 32
        env = Env(self, ledger=ledger_line("1" * 32) + ledger_line(nonce)[:-1])
        answer, code, backend = self.ask(env, approvalNonce=nonce)
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(env.ledger, ledger_line("1" * 32) + ledger_line(nonce))
        repair = [row for row in backend.rows if row["verb"] == "ledger-repair"]
        self.assertEqual([row["result"] for row in repair], ["completed"])
        # And another nonce proceeds, with the old line kept.
        env = Env(self, ledger=ledger_line(nonce)[:-1])
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(
            env.ledger,
            ledger_line(nonce) + ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW),
        )

    def test_a_final_fragment_that_parses_but_is_not_a_ledger_line_is_corrupt(self):
        spaced = ledger_line("5" * 32)[:-1].replace(b",", b", ")
        for fragment in (b"{}", b"[]", b'{"approvalNonce":"x"}', b"7", spaced):
            with self.subTest(fragment=fragment[:30]):
                env = Env(self, ledger=ledger_line("1" * 32) + fragment)
                answer, code, backend = self.ask(env)
                self.assertRefused(answer, code, "ledger_corrupt", verified=True)
                self.assertEqual(env.ledger, ledger_line("1" * 32) + fragment, "never guess")

    def test_a_corrupt_middle_line_refuses_every_approval(self):
        good = ledger_line("1" * 32)
        for name, bad in (
            ("junk", b"not json\n"),
            ("blank", b"\n"),
            ("extra field", compact({**json.loads(good), "x": 1}) + b"\n"),
            (
                "missing field",
                compact({k: v for k, v in json.loads(good).items() if k != "jobId"}) + b"\n",
            ),
            ("spaces (not canonical)", good.replace(b",", b", ")),
            ("bad nonce", good.replace(b"1" * 32, b"x" * 32)),
            ("bad time", good.replace(b"2026-11-07T21:02:00Z", b"tomorrow")),
            ("duplicate key", good.replace(b"{", b'{"jobId":"x",', 1)),
            ("number for a string", good.replace(b'"jobId":"job7k2m9q4x1"', b'"jobId":7')),
        ):
            with self.subTest(case=name):
                content = good + bad + ledger_line("2" * 32)
                env = Env(self, ledger=content)
                answer, code, backend = self.ask(env)
                self.assertRefused(answer, code, "ledger_corrupt", verified=True)
                self.assertEqual(env.ledger, content, "the file is left exactly as it was")
                self.assertEqual(backend.rows, [], "ledger_corrupt is refused without a row")
                self.assertNotIn("plan", backend.events)
                # Every later approval is refused, even one with a fresh nonce.
                answer, code, _ = self.ask(
                    env, now=later(3), approvalNonce="9" * 32, backend=FakeBackend()
                )
                self.assertRefused(answer, code, "ledger_corrupt")

    def test_a_ledger_over_its_size_limit_is_corrupt(self):
        env = Env(self)
        write_file(env.state / "muster-nonces.jsonl", b"x" * (ma.LEDGER_MAX_BYTES + 1), 0o600)
        answer, code, _ = self.ask(env)
        self.assertRefused(answer, code, "ledger_corrupt", verified=True)

    def test_an_unsafe_ledger_is_state_unsafe(self):
        def with_mode(bits):
            return lambda env: write_file(
                env.state / "muster-nonces.jsonl", ledger_line("1" * 32), bits
            )

        def link(env):
            real = env.root / "real-ledger"
            write_file(real, b"", 0o600)
            os.symlink(real, env.state / "muster-nonces.jsonl")

        def dangling(env):
            os.symlink(env.root / "nowhere", env.state / "muster-nonces.jsonl")

        def directory(env):
            (env.state / "muster-nonces.jsonl").mkdir()

        def fifo(env):
            os.mkfifo(env.state / "muster-nonces.jsonl", 0o600)

        for name, damage in (
            ("mode 0644", with_mode(0o644)),
            ("mode 0660", with_mode(0o660)),
            ("mode 0602", with_mode(0o602)),
            ("a symbolic link", link),
            ("a dangling symbolic link", dangling),
            ("a directory", directory),
            ("a FIFO", fifo),
        ):
            with self.subTest(fault=name):
                env = Env(self)
                damage(env)
                answer, code, backend = self.run_within(
                    10, lambda: self.ask(env), unblock=lambda: self.release_fifos(env.state)
                )
                self.assertRefused(answer, code, "state_unsafe", verified=True)
                self.assertNotIn("plan", backend.events)
                self.assertNotIn("run", backend.events)

    def test_a_symbolic_link_ledger_is_never_written_through(self):
        env = Env(self)
        target = env.root / "elsewhere"
        write_file(target, b"keep\n", 0o600)
        os.symlink(target, env.state / "muster-nonces.jsonl")
        answer, code, _ = self.ask(env)
        self.assertRefused(answer, code, "state_unsafe")
        self.assertEqual(target.read_bytes(), b"keep\n")

    def lines_with(self, count, expires, start=0):
        """`count` canonical ledger lines with distinct nonces, all expiring at `expires`."""
        return b"".join(
            ledger_line(
                f"{start + index + 1:032x}", expires=expires, recorded="2026-10-01T00:00:00Z"
            )
            for index in range(count)
        )

    def count_lines(self, env):
        return (env.ledger or b"").count(b"\n")

    def test_the_clock_alone_never_forgets_a_nonce(self):
        # Ten lines whose approvals expired a year ago stay: pruning needs the count as well.
        old = self.lines_with(10, "2025-11-07T21:00:00Z")
        env = Env(self, ledger=old)
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(
            env.ledger, old + ledger_line(BASE_APPROVAL["approvalNonce"], recorded=NOW)
        )

    def test_the_last_4096_lines_stay_and_older_ones_go_only_when_seven_days_old(self):
        self.assertEqual((ma.LEDGER_KEEP_LINES, ma.LEDGER_KEEP_SECONDS), (4096, 7 * 86400))
        eight_days = "2026-10-30T21:00:00Z"  # more than seven days before NOW (21:00:30 on the 7th)
        six_days = "2026-11-01T21:00:00Z"
        # 4,096 lines, all old: with the new one that is one more than the limit, so one goes.
        env = Env(self, ledger=self.lines_with(4096, eight_days))
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(self.count_lines(env), 4096)
        self.assertNotIn(b"%032x" % 1, env.ledger.split(b"\n")[0])
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        # 4,096 lines that expired six days ago: seven days are kept whatever the count.
        env = Env(self, ledger=self.lines_with(4096, six_days))
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(self.count_lines(env), 4097)
        # A mix: 4,100 lines before the request, five over the limit with the new one.  The first four
        # are old and go; the fifth candidate is only six days old and stays.
        env = Env(
            self,
            ledger=self.lines_with(4, eight_days) + self.lines_with(4096, six_days, start=100),
        )
        answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        self.assertEqual(self.count_lines(env), 4100 + 1 - 4)

    def test_the_seven_day_boundary_is_to_the_second(self):
        # NOW is 2026-11-07T21:00:30Z; seven days before it is 2026-10-31T21:00:30Z.
        for expires, dropped in (("2026-10-31T21:00:30Z", False), ("2026-10-31T21:00:29Z", True)):
            with self.subTest(expires=expires):
                env = Env(self, ledger=self.lines_with(4096, expires))
                answer, code, _ = self.ask(env)
                self.assertDone(answer, code)
                self.assertEqual(self.count_lines(env), 4096 if dropped else 4097)

    def test_a_clock_that_steps_forward_a_day_and_back_cannot_replay_an_approval(self):
        # The reviewer's attack: an approval runs; the clock steps forward a day and two approvals
        # run there; the clock steps back inside the first approval's window and it is sent again.
        env = Env(self)
        first = approval()
        answer, code, _ = module_request(env, COMMAND, stdin_of(first), NOW)
        self.assertDone(answer, code)
        base = epoch(NOW) + 86400
        for index, nonce in enumerate(("b" * 32, "c" * 32)):
            moment = base + index * 10
            document = approval(
                approvalNonce=nonce,
                issuedAt=ma.format_time(moment - 30),
                expiresAt=ma.format_time(moment + 90),
            )
            answer, code, _ = module_request(env, COMMAND, stdin_of(document), moment)
            self.assertDone(answer, code)
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        for seconds in (13, 43, 60):
            answer, code, backend = module_request(
                env, COMMAND, stdin_of(first), epoch(NOW) + seconds, FakeBackend()
            )
            self.assertRefused(answer, code, "replayed_nonce", verified=True)
            self.assertEqual(backend.events.count("run"), 0)

    def test_a_clock_that_steps_forward_a_year_and_back_cannot_either(self):
        env = Env(self)
        first = approval()
        self.assertDone(*module_request(env, COMMAND, stdin_of(first), NOW)[:2])
        for step, nonce in enumerate(("d" * 32, "e" * 32, "f" * 32)):
            moment = epoch(NOW) + 400 * 86400 + step * 10
            document = approval(
                approvalNonce=nonce,
                issuedAt=ma.format_time(moment - 30),
                expiresAt=ma.format_time(moment + 90),
            )
            self.assertDone(*module_request(env, COMMAND, stdin_of(document), moment)[:2])
        answer, code, _ = module_request(
            env, COMMAND, stdin_of(first), epoch(NOW) + 20, FakeBackend()
        )
        self.assertRefused(answer, code, "replayed_nonce", verified=True)

    def test_pruning_goes_through_a_temporary_file_and_a_rename(self):
        env = Env(self, ledger=self.lines_with(4096, "2026-10-30T21:00:00Z"))
        renames = []
        real = os.rename

        def spy(src, dst, **kwargs):
            renames.append((src, dst))
            return real(src, dst, **kwargs)

        with mock.patch.object(ma.os, "rename", spy):
            self.ask(env)
        ledger = [pair for pair in renames if pair[1] == "muster-nonces.jsonl"]
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger[0][0].endswith(".tmp"))

    def test_the_prune_rewrite_is_durable_file_then_rename_then_directory(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("the descriptors are named through /proc")
        env = Env(self, ledger=self.lines_with(4096, "2026-10-30T21:00:00Z"))
        events = []
        real_fsync, real_rename = os.fsync, os.rename

        def name_of(fd):
            try:
                return os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                return "?"

        def fsync(fd):
            events.append(("fsync", name_of(fd)))
            return real_fsync(fd)

        def rename(src, dst, **kwargs):
            events.append(("rename", dst))
            return real_rename(src, dst, **kwargs)

        with mock.patch.object(ma.os, "fsync", fsync), mock.patch.object(ma.os, "rename", rename):
            answer, code, _ = self.ask(env)
        self.assertDone(answer, code)
        position = events.index(("rename", "muster-nonces.jsonl"))
        before, after = events[:position], events[position + 1 :]
        self.assertTrue(
            any(kind == "fsync" and name.endswith(".tmp") for kind, name in before),
            "the rewritten file is fsynced before it replaces the ledger",
        )
        self.assertIn(("fsync", env.state.name), after, "and the directory afterwards")

    def test_an_expired_line_does_not_make_the_nonce_reusable_inside_its_window(self):
        # The window check refuses an approval after expiry, so a pruned nonce cannot be replayed.
        env = Env(self)
        self.ask(env)
        answer, code, _ = self.ask(env, now="2026-11-07T21:06:00Z", backend=FakeBackend())
        self.assertRefused(answer, code, "expired")

    def test_the_ledger_is_opened_without_following_links_and_not_inherited(self):
        env = Env(self)
        flags = {}
        real = os.open

        def spy(path, flag, *args, **kwargs):
            flags.setdefault(str(path), []).append(flag)
            return real(path, flag, *args, **kwargs)

        with mock.patch.object(ma.os, "open", spy):
            self.ask(env)
        for name in (
            "muster-nonces.jsonl",
            "muster-trust.json",
            "muster-helper.key",
            "muster-rate.json",
            "muster-rate.lock",
        ):
            self.assertIn(name, flags)
            for flag in flags[name]:
                self.assertTrue(flag & os.O_NOFOLLOW, name)
                self.assertTrue(flag & os.O_CLOEXEC, name)
        creation = [f for f in flags["muster-nonces.jsonl"] if f & os.O_CREAT]
        self.assertTrue(all(f & os.O_EXCL for f in creation))
        for flag in flags.get("muster-trust.json", []):
            self.assertFalse(flag & os.O_CREAT, "the trust file is never created by a request")


# MARK: - The rate limits (R1, Y8b, K1)


class RateTests(Checks):
    def plan(self, env, now, target="fleet-server-status.timer", verb="restart"):
        return module_request(env, f"server {verb} {target} --plan", job_stdin(), now)

    def yes(self, env, now, nonce="a" * 32, backend=None):
        return module_request(env, COMMAND, stdin_of(approval(approvalNonce=nonce)), now, backend)

    def test_one_plan_per_verb_and_target_in_five_seconds(self):
        env = Env(self)
        base = 1_800_000_000.0
        answer, code, _ = self.plan(env, base)
        self.assertEqual(code, 0)
        for offset in (0, 1, 4.9, 4.999):
            answer, code, _ = self.plan(env, base + offset)
            self.assertRefused(answer, code, "rate_limited", verified=False)
        answer, code, _ = self.plan(env, base + 5.0)
        self.assertEqual(code, 0, answer)
        # Another target, another verb: not limited by the first.
        answer, code, _ = self.plan(env, base + 5.0, target="botfleet-server.service")
        self.assertEqual(code, 0)
        answer, code, _ = self.plan(env, base + 5.0, verb="stop")
        self.assertEqual(code, 0)

    def test_times_are_integer_milliseconds_rounded_down(self):
        # Section 12: the file holds integer milliseconds, the float clock rounded down.  A request
        # at 0.9 s is counted at 900 ms, so the spacing of 5,000 ms is over at 5.9 s and not before.
        env = Env(self)
        base = 1_800_000_000.0
        self.assertEqual(self.plan(env, base + 0.9)[1], 0)
        answer, code, _ = self.plan(env, base + 5.5)
        self.assertRefused(answer, code, "rate_limited")
        self.assertEqual(self.plan(env, base + 5.9)[1], 0)
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual(
            [event[2] for event in data["events"]], [1_800_000_000_900, 1_800_000_005_900]
        )
        self.assertTrue(all(type(event[2]) is int for event in data["events"]))

    def rate_file(self, env, events):
        document = {"contract": "muster-rate/1", "events": events}
        write_file(env.state / "muster-rate.json", ma.canonical_bytes(document), 0o600)

    def test_the_spacing_boundary_is_inclusive_to_the_millisecond(self):
        # `now - last >= spacing` passes: exactly 5,000 ms later is allowed, 4,999 ms later is not.
        key = "restart fleet-server-status.timer"
        for offset, allowed in ((4999, False), (5000, True), (5001, True)):
            with self.subTest(offset=offset):
                env = Env(self)
                self.rate_file(env, [["plan", key, 1_800_000_000_000]])
                answer, code, _ = self.plan(env, (1_800_000_000_000 + offset) / 1000)
                if allowed:
                    self.assertEqual(code, 0, answer)
                else:
                    self.assertRefused(answer, code, "rate_limited")

    def test_the_hour_boundary_is_inclusive_to_the_millisecond(self):
        # The cap counts events with `now - at <= 3,600,000`: an event exactly an hour old still
        # counts, and one millisecond older has left the hour.
        now_ms = 1_800_010_000_000
        for age, limited in ((3_600_000, True), (3_600_001, False)):
            with self.subTest(age=age):
                env = Env(self)
                full = [["plan", f"restart u{i}.service", now_ms - age + i] for i in range(120)]
                self.rate_file(env, full)
                answer, code, _ = self.plan(env, now_ms / 1000, target="fresh.service")
                if limited:
                    self.assertRefused(answer, code, "rate_limited")
                else:
                    self.assertEqual(code, 0, answer)

    def test_an_event_dated_in_the_future_never_locks_a_key_out(self):
        # A clock stepped back an hour must not hold the key until the clock catches up: the future
        # event is counted as made now (it ages from there), so only the spacing applies.
        env = Env(self)
        key = "restart fleet-server-status.timer"
        later_ms = epoch(NOW) * 1000 + 3_600_000
        self.rate_file(env, [["plan", key, later_ms]])
        answer, code, _ = self.plan(env, epoch(NOW))
        self.assertRefused(answer, code, "rate_limited")
        answer, code, _ = self.plan(env, epoch(NOW) + 5)
        self.assertEqual(code, 0, answer)
        data = json.loads((env.state / "muster-rate.json").read_text())
        times = [event[2] for event in data["events"]]
        self.assertEqual(times, sorted(times), "the file stays oldest first")
        self.assertLessEqual(max(times), int((epoch(NOW) + 5) * 1000))
        # Another key was never in the way, and a far-future event is no different.
        env = Env(self)
        self.rate_file(env, [["plan", key, 4_000_000_000_000]])
        self.assertEqual(self.plan(env, epoch(NOW), target="other.service")[1], 0)
        self.assertEqual(self.plan(env, epoch(NOW) + 5)[1], 0)

    def test_future_events_still_count_toward_the_hourly_cap(self):
        # Clamping keeps them counted: dropping them would let a stepped clock reset the cap.
        env = Env(self)
        base = epoch(NOW)
        future = [
            ["plan", f"restart u{i}.service", int(base * 1000) + 10_000 + i] for i in range(120)
        ]
        self.rate_file(env, future)
        answer, code, _ = self.plan(env, base, target="fresh.service")
        self.assertRefused(answer, code, "rate_limited")
        # The refusal kept the clamp on disk: the events now carry the time of the request, so an
        # hour and a second after it they have aged out like any other event.
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual({event[2] for event in data["events"]}, {int(base * 1000)})
        answer, code, _ = self.plan(env, base + 3601, target="fresh.service")
        self.assertEqual(code, 0, answer)

    def test_a_rate_file_with_events_out_of_order_is_state_unsafe(self):
        env = Env(self)
        self.rate_file(env, [["plan", "a", 2000], ["plan", "b", 1000]])
        answer, code, backend = self.plan(env, 1_800_000_000.0)
        self.assertRefused(answer, code, "state_unsafe")
        self.assertEqual(backend.events, [])

    def test_a_refused_plan_is_not_counted(self):
        env = Env(self)
        base = 1_800_000_000.0
        self.plan(env, base)
        for _ in range(5):
            answer, code, _ = self.plan(env, base + 1)
            self.assertRefused(answer, code, "rate_limited")
        answer, code, _ = self.plan(env, base + 5)
        self.assertEqual(code, 0, "the refused calls did not extend the window")
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual(len(data["events"]), 2)

    def test_120_plans_a_sliding_hour(self):
        env = Env(self)
        base = 1_800_000_000.0
        for index in range(120):
            answer, code, _ = self.plan(env, base + index * 5, target=f"unit{index}.service")
            self.assertEqual(code, 0, (index, answer))
        answer, code, _ = self.plan(env, base + 120 * 5, target="onemore.service")
        self.assertRefused(answer, code, "rate_limited")
        # The hour is sliding and the cap is per class, not per verb and target.  An event exactly
        # 3,600 seconds old still counts; one second later it has left the hour.
        answer, code, _ = self.plan(env, base + 3600, target="afterwards.service")
        self.assertRefused(answer, code, "rate_limited")
        answer, code, _ = self.plan(env, base + 3601, target="afterwards.service")
        self.assertEqual(code, 0, answer)

    # -- The `yes` class: counted at Y8, inside the action lock, after the ledger has passed.

    def valid(self, nonce, now, **overrides):
        """An approval that is valid at `now`: issued then, good for 100 seconds."""
        return stdin_of(
            approval(
                approvalNonce=nonce,
                issuedAt=ma.format_time(now),
                expiresAt=ma.format_time(now + 100),
                **overrides,
            )
        )

    def yes_events(self, env):
        path = env.state / "muster-rate.json"
        if not path.exists():
            return []
        return [e for e in json.loads(path.read_text())["events"] if e[0] == "yes"]

    BASE = 1_794_085_230.0  # NOW

    def test_nothing_unauthenticated_ever_records_a_yes_event(self):
        base = self.BASE
        for name, now, clock_state, overrides in (
            ("a bad signature", base, None, {"signer": KEYS["device A request"]}),
            ("an unknown device", base, None, {"deviceId": "d-" + "1" * 24}),
            ("a field mismatch", base, None, {"host": "mac"}),
            ("an unsynchronised clock", base, lambda: (5, 0, 0), {}),
            ("an expired approval", base + 500, None, {}),
            ("an approval from the future", base - 500, None, {}),
            ("a window that is too long", base, None, {"expiresAt": "2026-11-07T21:09:00Z"}),
        ):
            with self.subTest(case=name):
                env = Env(self)
                changes = {} if clock_state is None else {"clock_state": clock_state}
                answer, code, backend = module_request(
                    env, COMMAND, stdin_of(approval(**overrides)), now, **changes
                )
                self.assertEqual(answer["kind"], "refusal", answer)
                self.assertNotIn(answer["body"]["errorCode"], ("rate_limited", "replayed_nonce"))
                self.assertEqual(self.yes_events(env), [])
                self.assertFalse((env.state / "muster-rate.json").exists())

    def test_a_replay_a_changed_list_and_a_busy_lock_record_nothing_either(self):
        env = Env(self)
        base = self.BASE
        self.assertDone(*module_request(env, COMMAND, self.valid("a" * 32, base), base)[:2])
        self.assertEqual(len(self.yes_events(env)), 1)
        for step in range(100):
            answer, code, backend = module_request(
                env, COMMAND, self.valid("a" * 32, base), base + 3 + step * 0.5
            )
            self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(len(self.yes_events(env)), 1, "100 replays used no budget")
        # A list that moved while the lock was being taken, and a lock that is busy.
        env = Env(self)
        v2 = FIXTURE["lists"]["v2"]
        answer, code, _ = module_request(
            env,
            COMMAND,
            self.valid("b" * 32, base),
            base,
            FakeBackend(on_lock=lambda: env.store(v2)),
        )
        self.assertRefused(answer, code, "trust_list_changed", verified=True)
        env = Env(self)
        answer, code, _ = module_request(
            env, COMMAND, self.valid("c" * 32, base), base, FakeBackend(busy=True)
        )
        self.assertRefused(answer, code, "action_busy", verified=True)
        self.assertEqual(self.yes_events(env), [])

    def test_junk_cannot_lock_the_owner_out(self):
        base = self.BASE
        junk = stdin_of(approval(signer=KEYS["device A request"]))
        for count in (1, 60, 600):
            with self.subTest(junk=count):
                env = Env(self)
                for step in range(count):
                    answer, code, _ = module_request(env, COMMAND, junk, base + step * 0.001)
                    self.assertRefused(answer, code, "bad_signature")
                answer, code, _ = module_request(env, COMMAND, self.valid("1" * 32, base), base)
                self.assertDone(answer, code)
                self.assertEqual(len(self.yes_events(env)), 1)

    def test_junk_signed_by_a_stranger_and_for_every_verb_cannot_either(self):
        env = Env(self)
        base = self.BASE
        for step in range(70):
            document = approval(signer=KEYS["rogue owner (not pinned)"], target=f"u{step}.service")
            answer, code, _ = module_request(
                env, f"server restart u{step}.service --yes", stdin_of(document), base
            )
            self.assertRefused(answer, code, "bad_signature")
        answer, code, _ = module_request(env, COMMAND, self.valid("1" * 32, base), base)
        self.assertDone(answer, code)

    def test_one_yes_per_verb_and_target_in_two_seconds_and_nothing_is_burned_by_a_refusal(self):
        base = self.BASE
        env = Env(self)
        first = self.valid("1" * 32, base)
        self.assertDone(*module_request(env, COMMAND, first, base)[:2])
        second = self.valid("2" * 32, base)
        for offset in (0, 1, 1.999):
            answer, code, backend = module_request(env, COMMAND, second, base + offset)
            self.assertRefused(answer, code, "rate_limited", verified=True)
            self.assertNotIn("plan", backend.events)
            self.assertNotIn("run", backend.events)
            self.assertNotIn(b"2" * 32, env.ledger, "a refused request does not spend its nonce")
        # Exactly two seconds on it passes, with the same approval, still inside its window.
        answer, code, backend = module_request(env, COMMAND, second, base + 2)
        self.assertDone(answer, code)
        self.assertIn(b"2" * 32, env.ledger)
        # Another verb or target is not held by that spacing.
        document = approval(
            approvalNonce="3" * 32,
            action="stop",
            issuedAt=ma.format_time(base),
            expiresAt=ma.format_time(base + 100),
        )
        answer, code, _ = module_request(
            env, "server stop fleet-server-status.timer --yes", stdin_of(document), base + 2
        )
        self.assertDone(answer, code)

    def test_sixty_valid_approvals_then_the_sixty_first_is_rate_limited_and_not_burned(self):
        base = self.BASE
        env = Env(self)
        for index in range(60):
            now = base + index * 2
            answer, code, _ = module_request(
                env, COMMAND, self.valid(f"{index + 1:032x}", now), now
            )
            self.assertDone(answer, code)
        now = base + 120
        document = self.valid("f" * 32, now)
        answer, code, backend = module_request(env, COMMAND, document, now)
        self.assertRefused(answer, code, "rate_limited", verified=True)
        self.assertNotIn(b"f" * 32, env.ledger, "its nonce is not burned")
        self.assertEqual(backend.rows, [], "a refusal before the nonce is spent writes no row")
        # The hour is inclusive: the oldest event counts until it is a millisecond past an hour.
        answer, code, _ = module_request(
            env, COMMAND, self.valid("e" * 32, base + 3600), base + 3600
        )
        self.assertRefused(answer, code, "rate_limited", verified=True)
        answer, code, _ = module_request(
            env, COMMAND, self.valid("d" * 32, base + 3601), base + 3601
        )
        self.assertDone(answer, code)

    def test_a_plan_mismatch_and_a_failed_run_still_count_because_they_spent_a_nonce(self):
        base = self.BASE
        for name, backend in (
            ("a plan mismatch", FakeBackend(plan=OTHER_PLAN)),
            ("a failed run", FakeBackend(outcome="failed")),
            ("an unknown run", FakeBackend(outcome="unknown")),
        ):
            with self.subTest(case=name):
                env = Env(self)
                module_request(env, COMMAND, self.valid("1" * 32, base), base, backend)
                self.assertIn(b"1" * 32, env.ledger)
                self.assertEqual(len(self.yes_events(env)), 1)
                answer, code, _ = module_request(env, COMMAND, self.valid("2" * 32, base), base + 1)
                self.assertRefused(answer, code, "rate_limited", verified=True)

    def test_the_rate_step_is_inside_the_lock_and_after_the_ledger(self):
        base = self.BASE
        real_check, real_lock = ma.check_rate, ma._lock_rate

        def trace(backend):
            def rate(*args, **kwargs):
                backend.events.append("rate_start")
                return real_check(*args, **kwargs)

            def lock(fd):
                backend.events.append("rate_lock")
                return real_lock(fd)

            return mock.patch.multiple(ma, check_rate=rate, _lock_rate=lock)

        # An approval: the action lock first, the rate lock second, both released in that order.
        env, backend = Env(self), FakeBackend()
        with trace(backend):
            module_request(env, COMMAND, self.valid("1" * 32, base), base, backend)
        events = backend.events
        self.assertLess(events.index("lock"), events.index("rate_start"))
        self.assertLess(events.index("rate_start"), events.index("rate_lock"))
        self.assertLess(events.index("rate_lock"), events.index("unlock"))
        self.assertLess(events.index("rate_lock"), events.index("plan"))
        # A replay is refused before the rate step: the lock is taken, the rate lock never is.
        env = Env(self)
        module_request(env, COMMAND, self.valid("2" * 32, base), base)
        backend = FakeBackend()
        with trace(backend):
            answer, code, backend = module_request(
                env, COMMAND, self.valid("2" * 32, base), base + 10, backend
            )
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertIn("lock", backend.events)
        self.assertNotIn("rate_start", backend.events)
        self.assertNotIn("rate_lock", backend.events)
        # So is a ledger that cannot be read.
        env, backend = Env(self, ledger=b"not a line\n"), FakeBackend()
        with trace(backend):
            answer, code, backend = module_request(
                env, COMMAND, self.valid("3" * 32, base), base, backend
            )
        self.assertRefused(answer, code, "ledger_corrupt", verified=True)
        self.assertIn("lock", backend.events)
        self.assertNotIn("rate_start", backend.events)
        self.assertNotIn("rate_lock", backend.events)
        # A plan and an acknowledgement only ever take the rate lock, never the action lock.
        for command, stdin in ((PLAN_COMMAND, job_stdin()), (CURRENT_COMMAND, CURRENT_STDIN)):
            env, backend = Env(self), FakeBackend()
            with trace(backend):
                module_request(env, command, stdin, base, backend)
            self.assertIn("rate_lock", backend.events)
            self.assertNotIn("lock", backend.events)
            self.assertNotIn("unlock", backend.events)

    def test_a_damaged_rate_file_or_a_held_rate_lock_refuses_the_approval_without_spending_it(self):
        import fcntl

        base = self.BASE
        env = Env(self)
        write_file(env.state / "muster-rate.json", b"{", 0o600)
        answer, code, _ = module_request(env, COMMAND, self.valid("1" * 32, base), base)
        self.assertRefused(answer, code, "state_unsafe", verified=True)
        self.assertNotIn(b"1" * 32, env.ledger or b"")
        env = Env(self)
        module_request(env, PLAN_COMMAND, job_stdin(), base)  # creates the lock file
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(ma, "RATE_LOCK_WAIT", 0.2):
                answer, code, _ = self.run_within(
                    10,
                    lambda: module_request(env, COMMAND, self.valid("2" * 32, base), base + 10),
                    unblock=lambda: fcntl.flock(fd, fcntl.LOCK_UN),
                )
            self.assertRefused(answer, code, "rate_limited", verified=True)
            self.assertNotIn(b"2" * 32, env.ledger or b"")
        finally:
            os.close(fd)

    def test_the_classes_are_counted_apart(self):
        env = Env(self)
        base = epoch("2026-11-07T21:00:00Z")
        for index in range(ma.RATE_LIMITS["plan"][1]):
            answer, code, _ = self.plan(env, base + index * 6, target=f"u{index}.service")
            self.assertEqual(code, 0)
        answer, code, _ = self.plan(env, base + 721, target="more.service")
        self.assertRefused(answer, code, "rate_limited")
        now = base + 722
        document = approval(issuedAt=ma.format_time(now), expiresAt=ma.format_time(now + 60))
        answer, code, _ = module_request(env, COMMAND, stdin_of(document), now)
        self.assertDone(answer, code)

    def test_the_limits_are_the_specs(self):
        self.assertEqual(
            ma.RATE_LIMITS,
            {
                "plan": (5000, 120),
                "yes": (2000, 60),
                "chain": (10000, 30),
                "current": (2000, 600),
            },
        )
        self.assertEqual(ma.RATE_HOUR, 3_600_000)
        self.assertEqual(ma.RATE_MAX_EVENTS, 810)
        self.assertEqual(ma.RATE_CONTRACT, "muster-rate/1")
        self.assertEqual(ma.RATE_LOCK_WAIT, 2.0)
        self.assertEqual(ma.STATE_RATE_NAME, "muster-rate.json")
        self.assertEqual(ma.STATE_RATE_LOCK_NAME, "muster-rate.lock")

    def test_the_rate_file_has_the_specs_format_and_is_pruned_to_the_hour(self):
        env = Env(self)
        self.plan(env, 1_800_000_000.0)
        for name in ("muster-rate.json", "muster-rate.lock"):
            self.assertEqual(stat.S_IMODE((env.state / name).stat().st_mode), 0o600)
        raw = (env.state / "muster-rate.json").read_bytes()
        self.assertEqual(
            raw,
            b'{"contract":"muster-rate/1","events":[["plan","restart fleet-server-status.timer",1800000000000]]}',
        )
        self.plan(env, 1_800_000_010.0, target="other.service")
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual(
            [event[2] for event in data["events"]], [1_800_000_000_000, 1_800_000_010_000]
        )
        self.plan(env, 1_800_004_000.0, target="late.service")
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual(data["events"], [["plan", "restart late.service", 1_800_004_000_000]])
        # Written by a temporary file and a rename, never in place.
        self.assertEqual([p.name for p in env.state.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_damaged_or_unsafe_rate_file_is_state_unsafe(self):
        def replace(data, mode=0o600):
            return lambda env: write_file(env.state / "muster-rate.json", data, mode)

        def link(env):
            write_file(env.root / "real-rate", b'{"contract":"muster-rate/1","events":[]}', 0o600)
            os.symlink(env.root / "real-rate", env.state / "muster-rate.json")

        def lock_link(env):
            write_file(env.root / "real-lock", b"", 0o600)
            os.symlink(env.root / "real-lock", env.state / "muster-rate.lock")

        def hard_link(env):
            write_file(
                env.state / "muster-rate.json", b'{"contract":"muster-rate/1","events":[]}', 0o600
            )
            os.link(env.state / "muster-rate.json", env.root / "second-name")

        head = b'{"contract":"muster-rate/1","events":'
        for name, damage in (
            ("unparseable", replace(b"{")),
            ("an old format", replace(b'{"calls":[]}')),
            ("wrong contract", replace(b'{"contract":"muster-rate/2","events":[]}')),
            ("events not a list", replace(head + b"7}")),
            ("an event too short", replace(head + b'[["plan","x"]]}')),
            ("an unknown class", replace(head + b'[["zzz","x",1]]}')),
            ("a time that is not an integer", replace(head + b'[["plan","x","1"]]}')),
            ("a negative time", replace(head + b'[["plan","x",-1]]}')),
            ("a time that is a float", replace(head + b'[["plan","x",1.5]]}')),
            ("a key that is not text", replace(head + b'[["plan",7,1]]}')),
            ("a key that is too long", replace(head + b'[["plan","' + b"k" * 301 + b'",1]]}')),
            ("events out of order", replace(head + b'[["plan","x",2],["plan","y",1]]}')),
            ("an extra key", replace(b'{"contract":"muster-rate/1","events":[],"x":1}')),
            ("a second hard link", hard_link),
            ("mode 0644", replace(head + b"[]}", 0o644)),
            ("a symbolic link", link),
            ("the lock a symbolic link", lock_link),
            (
                "more events than the caps allow",
                replace(head + b",".join([b'["plan","x",1]'] * 811).join([b"[", b"]"]) + b"}"),
            ),
        ):
            with self.subTest(fault=name):
                env = Env(self)
                damage(env)
                answer, code, backend = self.plan(env, 1_800_000_000.0)
                self.assertRefused(answer, code, "state_unsafe")
                self.assertEqual(backend.events, [])

    def test_the_default_wait_for_the_rate_lock_is_long_enough_to_outlast_a_brief_holder(self):
        # With the real constant (2 seconds): a lock that is let go after half a second does not
        # limit the request.
        import fcntl
        import threading

        env = Env(self)
        self.plan(env, 1_800_000_000.0)
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        timer = threading.Timer(0.5, lambda: fcntl.flock(fd, fcntl.LOCK_UN))
        timer.start()
        try:
            answer, code, _ = self.plan(env, 1_800_000_100.0, target="other.service")
            self.assertEqual(code, 0, answer)
        finally:
            timer.join()
            os.close(fd)

    def test_a_held_rate_lock_is_waited_for_then_rate_limited_and_not_counted(self):
        import fcntl
        import threading
        import time

        env = Env(self)
        self.plan(env, 1_800_000_000.0)
        before = (env.state / "muster-rate.json").read_bytes()
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(ma, "RATE_LOCK_WAIT", 0.3):
                started = time.monotonic()
                answer, code, _ = self.run_within(
                    10,
                    lambda: self.plan(env, 1_800_000_100.0, target="other.service"),
                    unblock=lambda: fcntl.flock(fd, fcntl.LOCK_UN),
                )
                waited = time.monotonic() - started
            self.assertRefused(answer, code, "rate_limited")
            self.assertGreaterEqual(waited, 0.25, "the helper waits for the lock before giving up")
            self.assertEqual((env.state / "muster-rate.json").read_bytes(), before, "not counted")
            # A lock that is let go inside the wait does not limit the call.
            timer = threading.Timer(0.1, lambda: fcntl.flock(fd, fcntl.LOCK_UN))
            timer.start()
            with mock.patch.object(ma, "RATE_LOCK_WAIT", 2.0):
                answer, code, _ = self.plan(env, 1_800_000_200.0, target="third.service")
            timer.join()
            self.assertEqual(code, 0, answer)
        finally:
            os.close(fd)


# MARK: - Plans and reads (R1 to R3)


class PlanTests(Checks):
    def test_a_signed_plan(self):
        env = Env(self)
        answer, code, backend = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0)
        self.assertSigned(answer)
        body = answer["body"]
        self.assertEqual(answer["kind"], "plan")
        self.assertEqual(body["contract"], "muster-signed-plan/1")
        self.assertEqual(body["claimed"], JOB)
        self.assertEqual(
            (body["host"], body["action"], body["target"]),
            ("server", "restart", "fleet-server-status.timer"),
        )
        self.assertEqual(body["plan"].encode(), ILLUSTRATIVE_PLAN)
        self.assertEqual(body["planSHA256"], BASE_APPROVAL["planSHA256"])
        self.assertEqual(body["helperTrustListVersion"], 1)
        self.assertEqual(body["issuedAt"], NOW)
        self.assertEqual(
            set(body),
            {
                "contract",
                "claimed",
                "host",
                "action",
                "target",
                "plan",
                "planSHA256",
                "helperTrustListVersion",
                "issuedAt",
            },
        )
        self.assertEqual(backend.events, ["plan"])
        self.assertEqual(backend.rows, [], "--plan writes no row")
        self.assertIsNone(env.ledger, "--plan spends no nonce")

    def test_a_plan_needs_neither_a_list_nor_a_lock(self):
        env = Env(self, stored=None)
        answer, code, backend = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0)
        self.assertIsNone(answer["body"]["helperTrustListVersion"])
        self.assertNotIn("lock", backend.events)
        env = Env(self, stored="v3")
        answer, code, _ = module_request(
            env, PLAN_COMMAND, job_stdin(), NOW, FakeBackend(busy=True)
        )
        self.assertEqual(answer["body"]["helperTrustListVersion"], 3)

    def test_a_plan_names_the_version_of_the_list_it_read_whoever_signed_it(self):
        # Section 13: the version of the stored list the request read.  Whether that list is signed
        # by the current owner key is what an acknowledgement's `signedByCurrentKey` says.
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        env = Env(self, stored=resign(body, KEYS["rogue owner (not pinned)"]))
        answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)

    def test_a_plan_whose_list_cannot_be_read_still_answers_with_a_null_version(self):
        env = Env(self)
        write_file(env.state / "muster-trust.json", b"{", 0o600)
        answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0)
        self.assertSigned(answer)
        self.assertIsNone(answer["body"]["helperTrustListVersion"])

    def test_a_signed_read(self):
        env = Env(self)
        answer, code, backend = module_request(
            env, "server seat-inventory claude --plan", job_stdin(), NOW
        )
        self.assertEqual(code, 0)
        self.assertSigned(answer)
        body = answer["body"]
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(body["claimed"], JOB)
        for name in ("jobId", "jobNonce", "deviceId", "approvalNonce"):
            self.assertNotIn(name, body)
        self.assertEqual(
            (body["outcome"], body["planSHA256"], body["auditId"]), ("planned", None, None)
        )
        self.assertEqual(unb64u(body["outputB64"]), b'{"x":1}')
        self.assertEqual(
            set(body),
            {
                "contract",
                "claimed",
                "host",
                "action",
                "target",
                "planSHA256",
                "outcome",
                "detail",
                "outputB64",
                "auditId",
                "helperTrustListVersion",
                "finishedAt",
            },
        )

    def test_a_plan_the_helper_cannot_build_is_a_refusal_with_the_planners_reason(self):
        # Section 4: the reason is the planner's own error_code when it gives one, else
        # allowlist_miss, report_stale or revision_stale.  Each word the engine allows round-trips.
        for reason in ma.PLAN_REFUSED_REASONS:
            with self.subTest(reason=reason):
                env = Env(self)
                backend = FakeBackend()
                backend.refuse_plan = "the unit is not in the allowlist"
                backend.refuse_code = reason
                answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW, backend)
                self.assertRefused(answer, code, "plan_refused", verified=False, reason=reason)
                self.assertEqual(answer["body"]["claimed"]["jobId"], JOB["jobId"])
                self.assertEqual(backend.rows, [])
                self.assertIsNone(env.ledger)

    def test_a_refusal_without_a_known_reason_is_internal_error(self):
        # Section 12: R2 answers `plan_refused` with its reason, or `internal_error`.  A word that
        # is not one of the allowed reasons, or none, never reaches the phone as a reason.
        for code_word in (None, "", "sudo_missing", "x" * 40, "plan_mismatch", "ALLOWLIST_MISS"):
            with self.subTest(code=code_word):
                env = Env(self)
                backend = FakeBackend()
                backend.refuse_plan = "some fault"
                backend.refuse_code = code_word
                answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW, backend)
                self.assertRefused(answer, code, "internal_error", verified=False, reason=None)

    def test_a_read_the_helper_cannot_make_is_refused_the_same_way(self):
        env = Env(self)
        backend = FakeBackend()
        backend.refuse_read = "coding resource controls are not in the allowlist"
        backend.refuse_code = "allowlist_miss"
        answer, code, _ = module_request(
            env, "server resource-status coding --plan", job_stdin(), NOW, backend
        )
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="allowlist_miss")
        backend.refuse_code = None
        answer, code, _ = module_request(
            Env(self), "server resource-status coding --plan", job_stdin(), later(10), backend
        )
        self.assertRefused(answer, code, "internal_error", verified=False)

    def test_an_unexpected_fault_in_a_plan_is_internal_error_and_names_what_it_claimed(self):
        env = Env(self)
        backend = FakeBackend()

        def boom(verb, target):
            raise KeyError("boom")

        backend.plan_bytes = boom
        answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW, backend)
        self.assertRefused(answer, code, "internal_error", verified=False)
        self.assertEqual(answer["body"]["claimed"], JOB)
        self.assertNotIn("boom", json.dumps(answer))

    def test_detail_is_printable_ascii(self):
        env = Env(self)
        backend = FakeBackend()
        backend.run_detail = "café \x07 line\nfeed"
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW, backend)
        self.assertDone(answer, code)
        self.assertEqual(answer["body"]["detail"], "caf? ? line?feed")
        self.assertEqual(ma.printable("café\n\x00x"), "caf???x")

    def test_the_run_result_names_the_approval_and_carries_log_text_as_base64url(self):
        env = Env(self)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        body = answer["body"]
        self.assertEqual(body["approvalNonce"], BASE_APPROVAL["approvalNonce"])
        self.assertEqual(unb64u(body["outputB64"]), b"line one\n")
        self.assertEqual(body["auditId"], "a1b2c3d4e5f6")
        self.assertEqual(body["jobId"], JOB["jobId"])

    def test_a_refusal_that_is_verified_names_the_approval_and_plan(self):
        env = Env(self)
        answer, code, _ = module_request(
            env, COMMAND, stdin_of(approval()), NOW, FakeBackend(plan=OTHER_PLAN)
        )
        self.assertEqual(
            answer["body"], FIXTURE["answers"]["refusalV6"]["answer"]["body"] | {"refusedAt": NOW}
        )
        self.assertSigned(answer)


# MARK: - The helper: audit rows, the lock, resource policy, the loader, the production entry


class HelperTests(Checks):
    def test_an_approved_run_leaves_one_row_with_the_routes_fields(self):
        env = Env(self)
        answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["verb"], "server restart")
        self.assertEqual(row["target"], "fleet-server-status.timer")
        self.assertEqual(row["result"], "done")
        self.assertEqual(row["route"], "from-muster")
        self.assertEqual(row["via"], "muster-ios")
        for name in ("jobId", "deviceId", "approvalNonce", "planSHA256"):
            self.assertEqual(row[name], BASE_APPROVAL[name])
        self.assertNotIn("errorCode", row)
        self.assertEqual(answer["body"]["auditId"], row["id"])
        self.assertEqual(stat.S_IMODE((env.state / "ops-audit.jsonl").stat().st_mode), 0o600)

    def test_only_a_refusal_after_the_nonce_is_spent_writes_a_row(self):
        for name, stdin, now, code_expected, rows_expected in (
            ("bad input", b"junk", NOW, "bad_input", 0),
            ("no list", stdin_of(approval()), None, "no_trust_list", 0),
            (
                "bad signature",
                stdin_of(approval(signer=KEYS["device A request"])),
                NOW,
                "bad_signature",
                0,
            ),
            ("field mismatch", stdin_of(approval(host="mac")), NOW, "field_mismatch", 0),
            ("expired", stdin_of(approval()), "2026-11-07T21:09:00Z", "expired", 0),
            (
                "window",
                stdin_of(approval(expiresAt="2026-11-07T21:09:00Z")),
                NOW,
                "window_too_long",
                0,
            ),
        ):
            with self.subTest(case=name):
                env = Env(self, stored=None if now is None else "v1")
                answer, code, runner, rows = helper_request(env, COMMAND, stdin, now or NOW)
                self.assertRefused(answer, code, code_expected)
                self.assertEqual(len(rows), rows_expected, rows)
                self.assertEqual(runner.calls, [])
                if rows_expected:
                    self.assertEqual(rows[0]["errorCode"], code_expected)
                    self.assertEqual(rows[0]["result"], "refused")
                    self.assertEqual(rows[0]["approvalNonce"], BASE_APPROVAL["approvalNonce"])

    def test_a_plan_mismatch_inside_the_shared_function_writes_its_row_once(self):
        env = Env(self)
        # A host whose first systemctl is /bin/systemctl plans another argv than the approved one.
        answer, code, runner, rows = helper_request(
            env, COMMAND, stdin_of(approval()), NOW, systemctl="/bin/systemctl"
        )
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["errorCode"], "plan_mismatch")
        self.assertEqual(rows[0]["result"], "refused")

    def test_a_replay_through_the_helper_adds_no_row(self):
        # One row per approval nonce, ever: the run's, and nothing for the replays after it.
        env = Env(self)
        document = stdin_of(approval())
        answer, code, runner, rows = helper_request(env, COMMAND, document, NOW)
        self.assertDone(answer, code)
        self.assertEqual(len(rows), 1)
        for step in (3, 10, 200):
            answer, code, runner, rows = helper_request(env, COMMAND, document, later(step))
            self.assertRefused(answer, code, "replayed_nonce" if step < 100 else "expired")
            self.assertEqual(len(rows), 1, rows)
        self.assertEqual(len(runner.calls), 0)

    def test_the_audit_row_of_a_refusal_after_y8_is_written_inside_the_lock(self):
        env = Env(self)
        backend = FakeBackend(plan=OTHER_PLAN)
        original = backend.audit

        def audit(*args, **kwargs):
            backend.events.append("audit")
            return original(*args, **kwargs)

        backend.audit = audit
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW, backend)
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        events = backend.events
        self.assertLess(events.index("lock"), events.index("audit"))
        self.assertLess(events.index("audit"), events.index("unlock"))

    def test_the_shared_function_refuses_without_an_approval_and_on_a_wrong_hash(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        runner = ctx.runner
        for approval_arg, expected in (
            (None, "approval_required"),
            ({}, "approval_required"),
            (dict(BASE_APPROVAL, planSHA256="short"), "approval_required"),
            (dict(BASE_APPROVAL, planSHA256="0" * 64), "plan_mismatch"),
        ):
            with self.subTest(approval=expected):
                with mock.patch.object(foc, "_systemctl", lambda: "systemctl"):
                    answer, code = foc.handle(
                        ctx,
                        "server",
                        "restart",
                        "fleet-server-status.timer",
                        "yes",
                        require_approval=True,
                        approval=approval_arg,
                        lock_held=True,
                        plan_bytes=ma.canonical_bytes,
                    )
                self.assertEqual(
                    (code, answer["result"], answer["errorCode"]), (2, "refused", expected)
                )
                self.assertEqual(runner.calls, [])
        self.assertFalse(runner.calls)

    def test_the_shared_function_without_the_switch_behaves_as_it_always_has(self):
        env = Env(self)
        runner = FakeRunner()
        ctx = foc.Context(platform="linux", home=env.home, runner=runner, config_path=env.allowlist)
        with mock.patch.object(foc, "_systemctl", lambda: "systemctl"):
            answer, code = foc.handle(ctx, "server", "restart", "fleet-server-status.timer", "yes")
        self.assertEqual((code, answer["result"]), (0, "done"))
        rows = env.audit_rows()
        self.assertEqual(len(rows), 1)
        for name in (
            "route",
            "via",
            "jobId",
            "deviceId",
            "approvalNonce",
            "planSHA256",
            "errorCode",
        ):
            self.assertNotIn(name, rows[0])

    def test_a_logs_run_takes_the_lock_and_returns_the_text(self):
        env = Env(self)
        journalctl = "/usr/bin/journalctl"
        # A timer's logs plan reads the journal of the service it starts (its `logsUnit`); the plan
        # still names the timer as its target and audit target.
        plan = foc.plan_object(
            "server",
            "logs",
            "fleet-server-status.timer",
            [
                [
                    journalctl,
                    "--user",
                    "-u",
                    "fleet-server-status.service",
                    "-n",
                    "200",
                    "--no-pager",
                ]
            ],
            "fleet-server-status.timer",
        )
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = approval(action="logs", planSHA256=digest)
        locks = []
        real = foc.action_lock

        @contextlib.contextmanager
        def spy(ctx):
            locks.append("taken")
            with real(ctx):
                yield

        with (
            mock.patch.object(foc, "action_lock", spy),
            mock.patch.object(foc, "_journalctl", lambda: journalctl),
        ):
            answer, code, runner, rows = helper_request(
                env, "server logs fleet-server-status.timer --yes", stdin_of(document), NOW
            )
        self.assertDone(answer, code)
        self.assertEqual(locks, ["taken"], "taken once, by the route, never twice")
        self.assertEqual(unb64u(answer["body"]["outputB64"]), b"line one\nline two\n")
        self.assertEqual(rows[0]["result"], "done")
        self.assertEqual(rows[0]["lines"], 2)

    def test_the_shared_function_takes_the_lock_for_a_logs_run_only_under_the_route(self):
        env = Env(self)
        plan = foc.plan_object(
            "server",
            "logs",
            "fleet-server-status.timer",
            [
                [
                    "/usr/bin/journalctl",
                    "--user",
                    "-u",
                    "fleet-server-status.service",
                    "-n",
                    "200",
                    "--no-pager",
                ]
            ],
            "fleet-server-status.timer",
        )
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = dict(BASE_APPROVAL, action="logs", planSHA256=digest)
        for routed, lock_held, taken in (
            (True, False, 1),  # the route requires the lock for logs too
            (True, True, 0),  # unless its caller already holds it
            (False, False, 0),  # a plain `--yes` logs run takes none, as it always has
        ):
            with self.subTest(routed=routed, lock_held=lock_held):
                locks = []
                real = foc.action_lock

                @contextlib.contextmanager
                def spy(ctx, real=real, locks=locks):
                    locks.append("taken")
                    with real(ctx):
                        yield

                ctx = foc.Context(
                    platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
                )
                options = (
                    {
                        "require_approval": True,
                        "approval": document,
                        "plan_bytes": ma.canonical_bytes,
                    }
                    if routed
                    else {}
                )
                with (
                    mock.patch.object(foc, "action_lock", spy),
                    mock.patch.object(foc, "_journalctl", lambda: "/usr/bin/journalctl"),
                ):
                    answer, code = foc.handle(
                        ctx,
                        "server",
                        "logs",
                        "fleet-server-status.timer",
                        "yes",
                        lock_held=lock_held,
                        **options,
                    )
                self.assertEqual((code, answer["result"]), (0, "done"), answer)
                self.assertEqual(len(locks), taken)

    def test_a_held_action_lock_is_action_busy_for_an_authentic_request_only(self):
        import fcntl

        env = Env(self)
        fd = os.open(env.state / "ops-control.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
            self.assertRefused(answer, code, "action_busy", verified=True)
            self.assertEqual(runner.calls, [])
            self.assertEqual(rows, [], "a refusal before Y8 writes no row")
            self.assertIsNone(env.ledger)
            env2 = Env(self)
            fd2 = os.open(env2.state / "ops-control.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd2, fcntl.LOCK_EX)
            try:
                answer, code, runner, rows = helper_request(
                    env2, COMMAND, stdin_of(approval(signer=KEYS["device A request"])), NOW
                )
                self.assertRefused(answer, code, "bad_signature")
                self.assertEqual(rows, [])
            finally:
                os.close(fd2)
        finally:
            os.close(fd)

    def test_reads_through_the_helper(self):
        env = Env(self)
        answer, code, runner, rows = helper_request(
            env, "server seat-inventory claude --plan", job_stdin(), NOW
        )
        self.assertEqual(code, 0, answer)
        self.assertSigned(answer)
        self.assertEqual(answer["body"]["outcome"], "planned")
        document = json.loads(unb64u(answer["body"]["outputB64"]))
        self.assertEqual(document["result"], "planned")
        self.assertIn("inventory", document)
        self.assertEqual(rows, [])
        # A platform the module does not know is not a target at all: `bad_command` at P2, and it is
        # not counted against the rate limit.
        env = Env(self)
        answer, code, _, _ = helper_request(
            env, "server seat-inventory nonesuch --plan", job_stdin(), NOW
        )
        self.assertRefused(answer, code, "bad_command", verified=False)
        self.assertFalse((env.state / "muster-rate.json").exists())

    def test_a_plan_through_the_helper_matches_what_a_run_would_hash(self):
        env = Env(self)
        answer, code, runner, rows = helper_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["body"]["planSHA256"], BASE_APPROVAL["planSHA256"])
        self.assertEqual(answer["body"]["plan"].encode(), ILLUSTRATIVE_PLAN)
        self.assertEqual((runner.calls, rows), ([], []))
        # A unit that is not in the allowlist cannot be planned.
        answer, code, _, _ = helper_request(
            Env(self), "server restart not-listed.service --plan", job_stdin(), NOW
        )
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="allowlist_miss")
        # Nor can a redeploy, when the allowlist does not offer redeploys at all.
        answer, code, _, _ = helper_request(
            Env(self), "server redeploy abc123def456 --plan", job_stdin(), NOW
        )
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="allowlist_miss")

    def test_the_mac_scopes_and_the_eavesdropper_read_are_not_offered(self):
        env = Env(self)
        for command in (
            "mac restart com.jay.fleet-tools-doctor --plan",
            "pm2 restart shellular --yes",
            "server eavesdropper-status server --plan",
            "server unit-status fleet-server-status.timer --plan",
            "server unit-status docker.service --plan",
            "server unit-status fleet-server-status.timer --yes",
        ):
            with self.subTest(command=command):
                answer, code, runner, _ = helper_request(env, command, job_stdin(), NOW)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(runner.calls, [])

    def test_the_route_field_keeps_the_update_route_in_updateroute(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        written = foc.audit(
            ctx,
            "server",
            "update",
            "fleet-cli",
            "submitted",
            route="sudo-system-update",
            invocation_id="a" * 32,
            baseline=5,
            row_id="0123456789ab",
            extra={"route": "from-muster", "via": "muster-ios", "jobId": "j1", "deviceId": None},
        )
        self.assertTrue(written)
        row = env.audit_rows()[-1]
        self.assertEqual(
            (row["route"], row["updateRoute"], row["via"]),
            ("from-muster", "sudo-system-update", "muster-ios"),
        )
        self.assertIsNone(row["deviceId"])
        # `update-status` still finds the baseline of a run that came through Muster.
        ctx.audit_path = env.state / "ops-audit.jsonl"
        self.assertEqual(foc.captured_update_baseline(ctx, "a" * 32), 5)

    def test_audit_extra_takes_only_known_names_and_plain_values(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        foc.audit(
            ctx,
            "server",
            "restart",
            "x.timer",
            "done",
            extra={
                "jobId": "job1",
                "secret": "x",
                "planSHA256": "has space",
                "errorCode": "bad_signature",
            },
        )
        row = env.audit_rows()[-1]
        self.assertEqual(row["jobId"], "job1")
        self.assertEqual(row["errorCode"], "bad_signature")
        self.assertNotIn("secret", row)
        self.assertNotIn("planSHA256", row)


class LoaderTests(Checks):
    """The sibling modules are loaded as the reducer is: read through descriptors, judged by fstat,
    compiled in memory, never imported, and only as the installed layout requires.

    A folder owned by TRUSTED_MODULE_UID (root, in production) is the installed layout; these tests
    stand in for it by naming this account the trusted one (`installed`).  A folder owned by the
    calling account while root is the trusted one is a checkout (`checkout`)."""

    def build(self, dir_mode=0o755, file_mode=0o644, flat=False, agent=True, approval_text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(os.path.realpath(tmp.name)) / "helper"
        folder.mkdir()
        source = (HERE / "muster_approval.py").read_bytes()
        (folder / "muster_approval.py").write_bytes(
            source if approval_text is None else approval_text
        )
        trust = (HERE / "muster_agent" / "muster_trust_files.py").read_bytes()
        if flat:
            (folder / "muster_trust_files.py").write_bytes(trust + b'\nPLACE = "flat"\n')
        if agent:
            (folder / "muster_agent").mkdir()
            (folder / "muster_agent" / "muster_trust_files.py").write_bytes(
                trust + b'\nPLACE = "muster_agent"\n'
            )
        for path in folder.rglob("*"):
            os.chmod(path, dir_mode if path.is_dir() else file_mode)
        os.chmod(folder, dir_mode)
        return folder

    def context(self, uid=None):
        context = foc.Context(platform="linux", home=Path(tempfile.gettempdir()), environ={})
        if uid is not None:
            context.uid = uid
        return context

    def load(self, folder, uid=None, trusted=0):
        with (
            mock.patch.object(foc, "HELPER_DIR", folder),
            mock.patch.object(foc, "TRUSTED_MODULE_UID", trusted),
        ):
            return foc._load_muster_modules(self.context(uid))

    def installed(self, folder, **kwargs):
        return self.load(folder, trusted=os.getuid(), **kwargs)

    def checkout(self, folder, **kwargs):
        return self.load(folder, trusted=0, **kwargs)

    def not_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root, so every file here is the trusted owner's")

    def test_a_checkout_loads_both_modules_in_memory_and_the_reader_from_muster_agent(self):
        self.not_root()
        folder = self.build()
        modules = self.checkout(folder)
        self.assertIsNotNone(modules)
        approval_module, trust_module = modules
        self.assertEqual(approval_module.__name__, "muster_approval")
        self.assertEqual(trust_module.PLACE, "muster_agent")
        for name in ("muster_approval", "muster_trust_files"):
            self.assertNotIn(name, sys.modules)
        self.assertEqual(
            sorted(p.name for p in folder.rglob("__pycache__")), [], "nothing was compiled to disk"
        )

    def test_the_installed_layout_reads_the_reader_flat_beside_the_helper(self):
        folder = self.build(flat=True, agent=False)
        modules = self.installed(folder)
        self.assertIsNotNone(modules)
        self.assertEqual(modules[1].PLACE, "flat")

    def test_beside_the_helper_comes_before_muster_agent(self):
        folder = self.build(flat=True, agent=True)
        self.assertEqual(self.installed(folder)[1].PLACE, "flat")

    def test_a_missing_module_gives_the_unsigned_answer(self):
        folder = self.build()
        (folder / "muster_approval.py").unlink()
        self.assertIsNone(self.installed(folder))
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(foc, "HELPER_DIR", folder),
            mock.patch.object(foc, "TRUSTED_MODULE_UID", os.getuid()),
        ):
            code = foc.main(["--from-muster"], out=out)
        answer = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(answer, foc.MUSTER_UNSIGNED_ANSWER)
        self.assertEqual(answer["body"]["errorCode"], "helper_key_missing")

    def test_both_modules_are_needed(self):
        self.assertIsNone(self.installed(self.build(agent=False)))
        folder = self.build()
        (folder / "muster_approval.py").unlink()
        self.assertIsNone(self.installed(folder))
        self.assertIsNotNone(self.installed(self.build()))

    def test_a_symbolic_link_or_a_broken_module_is_not_loaded(self):
        folder = self.build()
        real = folder.parent / "real.py"
        real.write_bytes((HERE / "muster_approval.py").read_bytes())
        (folder / "muster_approval.py").unlink()
        (folder / "muster_approval.py").symlink_to(real)
        self.assertIsNone(self.installed(folder))
        (folder / "muster_approval.py").unlink()
        for name, text in (
            ("a syntax error", "def handle_request(:\n"),
            ("a module that raises", "raise RuntimeError('no')\n"),
            ("a module that exits", "raise SystemExit(0)\n"),
            ("no entry point", "x = 1\n"),
            ("an entry point that is not callable", "handle_request = 1\n"),
        ):
            with self.subTest(case=name):
                (folder / "muster_approval.py").write_text(text)
                os.chmod(folder / "muster_approval.py", 0o644)
                self.assertIsNone(self.installed(folder))
        (folder / "muster_approval.py").unlink()
        (folder / "muster_approval.py").mkdir()
        self.assertIsNone(self.installed(folder))
        (folder / "muster_approval.py").rmdir()
        os.mkfifo(folder / "muster_approval.py", 0o644)
        self.assertIsNone(
            self.run_within(
                10,
                lambda: self.installed(folder),
                unblock=lambda: self.release_fifo(folder / "muster_approval.py"),
            )
        )

    def test_a_linked_or_loose_muster_agent_folder_is_not_trusted(self):
        self.not_root()
        folder = self.build()
        self.assertIsNotNone(self.checkout(folder))
        for mode in (0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                os.chmod(folder / "muster_agent", mode)
                self.assertIsNone(self.checkout(folder))
        os.chmod(folder / "muster_agent", 0o755)
        real = folder.parent / "elsewhere"
        (folder / "muster_agent").rename(real)
        (folder / "muster_agent").symlink_to(real, target_is_directory=True)
        self.assertIsNone(self.checkout(folder))

    def test_the_installed_layout_demands_the_trusted_owner_and_no_group_or_other_write(self):
        for mode in (0o664, 0o646, 0o622, 0o666):
            with self.subTest(file=oct(mode)):
                self.assertIsNone(self.installed(self.build(file_mode=mode)))
        for mode in (0o775, 0o757, 0o722, 0o777):
            with self.subTest(folder=oct(mode)):
                self.assertIsNone(self.installed(self.build(dir_mode=mode)))
        # Owned by the account while root is trusted: a checkout; owned by the account while the
        # account is the trusted one: the installed layout, which then wants the trusted owner's file.
        folder = self.build()
        self.assertIsNotNone(self.installed(folder))
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            if not stat.S_ISREG(info.st_mode):
                return info
            return os.stat_result(tuple(info)[:4] + (os.getuid() + 1000,) + tuple(info)[5:])

        with mock.patch.object(foc.os, "fstat", fstat):
            self.assertIsNone(self.installed(folder))

    def test_the_installed_layout_does_not_trust_a_file_of_the_calling_account(self):
        # Root owns the folder and the calling account owns the files: the installed layout wants
        # the trusted owner's file.  The harness cannot tell the trusted uid from the calling one
        # (they are the same account), so this names a distinct trusted uid and fakes each
        # descriptor's owner: the folders are the trusted uid's, the file is whoever is asked.
        trusted = os.getuid() + 7
        folder = self.build()
        real = os.fstat

        def fstat_with(file_owner):
            def fstat(fd):
                info = real(fd)
                fields = list(tuple(info))
                fields[4] = trusted if stat.S_ISDIR(info.st_mode) else file_owner
                return os.stat_result(fields)

            return fstat

        with mock.patch.object(foc.os, "fstat", fstat_with(trusted)):
            self.assertIsNotNone(self.load(folder, trusted=trusted))
        with mock.patch.object(foc.os, "fstat", fstat_with(os.getuid())):
            self.assertIsNone(self.load(folder, trusted=trusted))
        # In a checkout (the folder is the account's, root is the trusted uid) the account's file is
        # trusted, as the existing tests show.
        self.not_root()
        self.assertIsNotNone(self.checkout(self.build()))

    def test_in_a_checkout_the_account_may_own_the_files_and_still_no_group_or_other_write(self):
        self.not_root()
        self.assertIsNotNone(self.checkout(self.build()))
        for file_mode, dir_mode in ((0o664, 0o755), (0o646, 0o755), (0o644, 0o775), (0o644, 0o757)):
            with self.subTest(file=oct(file_mode), folder=oct(dir_mode)):
                self.assertIsNone(self.checkout(self.build(file_mode=file_mode, dir_mode=dir_mode)))
        self.assertIsNone(self.checkout(self.build(), uid=os.getuid() + 1000))

    def test_a_file_swapped_in_after_the_open_is_never_read(self):
        folder = self.build()
        marker = folder.parent / "evil-ran"
        evil = folder.parent / "evil.py"
        evil.write_text("open(%r, 'w').write('ran')\nraise SystemExit(0)\n" % str(marker))
        os.chmod(evil, 0o644)
        real_open = os.open

        def hook(name, flags, *args, **kwargs):
            descriptor = real_open(name, flags, *args, **kwargs)
            if name == "muster_approval.py" and kwargs.get("dir_fd") is not None:
                os.replace(evil, folder / "muster_approval.py")
            return descriptor

        with mock.patch.object(foc.os, "open", hook):
            modules = self.installed(folder)
        self.assertIsNotNone(modules)
        self.assertFalse(marker.exists(), "the swapped-in file was run")
        self.assertIn("raise SystemExit", (folder / "muster_approval.py").read_text())

    def test_nothing_is_loaded_through_sys_path_or_the_environment(self):
        folder = self.build()
        trap = folder.parent / "trap"
        trap.mkdir()
        for name in ("muster_approval.py", "muster_trust_files.py"):
            (trap / name).write_text("raise SystemExit('loaded through the path')\n")
        with mock.patch.object(sys, "path", [str(trap)] + sys.path):
            self.assertIsNotNone(self.installed(folder))
        tree = ast.parse((HERE / "fleet-ops-control.py").read_text())
        wanted = ("_compile_module", "_load_muster_module", "_load_muster_modules")
        found = {node.name for node in tree.body if getattr(node, "name", None) in wanted}
        self.assertEqual(found, set(wanted))
        for node in tree.body:
            if getattr(node, "name", None) in wanted:
                for inner in ast.walk(node):
                    self.assertFalse(
                        isinstance(inner, ast.Attribute)
                        and inner.attr in ("path", "environ", "import_module")
                        and isinstance(inner.value, ast.Name)
                        and inner.value.id in ("sys", "os", "importlib"),
                        node.name,
                    )
                    self.assertFalse(isinstance(inner, ast.Name) and inner.id == "__import__")

    def test_the_old_loader_and_its_second_layout_are_gone(self):
        self.assertFalse(hasattr(foc, "_load_trusted_sibling"))

    def test_the_trust_files_reader_has_one_source_file(self):
        self.assertTrue((HERE / "muster_agent" / "muster_trust_files.py").is_file())
        self.assertFalse((HERE / "muster_trust_files.py").exists())


class ProductionEntryTests(Checks):
    """The forced command reads `SSH_ORIGINAL_COMMAND` first and then empties its environment."""

    def environment_accesses(self, tree):
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb"):
                found.append(node)
        return found

    def test_the_modules_never_touch_the_environment(self):
        for name in ("muster_approval.py", "muster_agent/muster_trust_files.py"):
            with self.subTest(module=name):
                tree = ast.parse((HERE / name).read_text())
                self.assertEqual(self.environment_accesses(tree), [])
                imports = {
                    alias.name.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                } | {
                    node.module.split(".")[0]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module
                }
                for forbidden in (
                    "subprocess",
                    "socket",
                    "urllib",
                    "http",
                    "requests",
                    "ssl",
                    "sys",
                    "importlib",
                    "tempfile",
                    "shutil",
                ):
                    self.assertNotIn(forbidden, imports)

    def test_the_helpers_muster_branch_reads_one_variable_and_then_empties_the_environment(self):
        text = (HERE / "fleet-ops-control.py").read_text()
        tree = ast.parse(text)
        counts = {}
        for node in tree.body:
            name = getattr(node, "name", None)
            if name in (
                "run_from_muster",
                "_empty_environment",
                "_muster_config",
                "_compile_module",
                "_load_muster_module",
                "_load_muster_modules",
                "_MusterBackend",
                "_MusterLock",
                "_sibling",
            ):
                counts[name] = [
                    ast.get_source_segment(text, access)
                    for access in self.environment_accesses(node)
                ]
        self.assertEqual(counts["run_from_muster"], ["os.environ"])
        self.assertEqual(counts["_empty_environment"], ["os.environ"] * 3)
        for name, accesses in counts.items():
            if name not in ("run_from_muster", "_empty_environment"):
                self.assertEqual(accesses, [], name)
        source = inspect.getsource(foc.run_from_muster)
        self.assertIn('os.environ.get("SSH_ORIGINAL_COMMAND")', source)
        # The order is the point: capture the command, empty the environment, and only then load the
        # modules (which import `cryptography`).
        order = [
            source.index('os.environ.get("SSH_ORIGINAL_COMMAND")'),
            source.index("_empty_environment("),
            source.index("_load_muster_modules("),
        ]
        self.assertEqual(order, sorted(order))
        clear = inspect.getsource(foc._empty_environment)
        self.assertIn("os.environ.clear()", clear)
        self.assertEqual(
            len([line for line in clear.splitlines() if "os.environ[" in line]),
            2,
            "only HOME and PATH are set after the clear",
        )

    def test_the_entry_captures_the_command_empties_the_environment_and_nothing_else_is_read(self):
        env = Env(self)
        planted = {
            "SSH_ORIGINAL_COMMAND": PLAN_COMMAND,
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "OPENSSL_CONF": "/nonexistent/openssl.cnf",
            "SSL_CERT_FILE": "/nonexistent/cert.pem",
            "LC_ALL": "xx_XX.UTF-8",
            "LC_CTYPE": "xx_XX.UTF-8",
            "LANG": "xx_XX.UTF-8",
            "HOME": "/nonexistent/home",
            "PYTHONPATH": "/nonexistent",
            "TZ": "Pacific/Kiritimati",
        }
        seen = {}
        real_load = foc._load_muster_modules

        def spy(ctx):
            seen["environment"] = dict(os.environ)
            return real_load(ctx)

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, planted, clear=True),
            mock.patch.object(foc, "HELPER_DIR", checkout()),
            mock.patch.object(foc, "_load_muster_modules", spy),
            mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL),
        ):
            code = foc.main(
                ["--from-muster"],
                out=out,
                ctx=foc.Context(
                    platform="linux",
                    home=env.home,
                    runner=FakeRunner(),
                    config_path=env.allowlist,
                    environ={"HOME": str(env.home)},
                ),
                muster={"config": env.config(NOW), "stdin": job_stdin()},
            )
            after = dict(os.environ)
        # The command came from the variable, read before the environment was emptied.
        answer = json.loads(out.getvalue())
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["kind"], "plan")
        self.assertEqual(answer["body"]["action"], "restart")
        # Emptied before the modules were loaded: only a fixed HOME and PATH remain.
        self.assertEqual(set(seen["environment"]), {"HOME", "PATH"})
        self.assertEqual(after, seen["environment"])
        self.assertEqual(seen["environment"]["PATH"], foc.MUSTER_CHILD_ENV["PATH"])
        self.assertNotIn("nonexistent", json.dumps(seen["environment"]))

    def test_a_fresh_process_has_loaded_nothing_but_the_standard_library_when_the_environment_is_empty(
        self,
    ):
        # The real order, in a real `python3 -I` process: at the moment the loader is called the
        # environment holds only HOME and PATH and `cryptography` has not been imported.
        env = Env(self)
        driver = env.root / "env_driver.py"
        driver.write_text(ENV_DRIVER)
        spec = env.root / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "helper": str(checkout() / "fleet-ops-control.py"),
                    "home": str(env.home),
                    "allowlist": str(env.allowlist),
                    "config": {k: v for k, v in env.config(NOW).items() if k != "clock_state"},
                    "report": str(env.root / "report.json"),
                }
            )
        )
        hostile = {
            "SSH_ORIGINAL_COMMAND": PLAN_COMMAND,
            "OPENSSL_CONF": "/nonexistent/openssl.cnf",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "LC_ALL": "xx_XX.UTF-8",
            "HOME": "/nonexistent/home",
        }
        result = subprocess.run(
            [sys.executable, "-I", str(driver), str(spec)],
            input=job_stdin(),
            capture_output=True,
            env=hostile,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.stderr, b"", result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads((env.root / "report.json").read_text())
        self.assertEqual(sorted(report["environment"]), ["HOME", "PATH"])
        self.assertFalse(report["cryptography_loaded"])
        # Nor `urllib.request` or `ssl`, which read the OpenSSL configuration and the proxy variables
        # from the environment: the helper imports them inside the function that uses them.
        self.assertFalse(report["urllib_request_loaded"])
        self.assertFalse(report["ssl_loaded"])
        self.assertEqual(json.loads(result.stdout)["kind"], "plan")

    def test_the_production_configuration_ignores_the_environment(self):
        reference = ma.production_config()
        hostile = {
            "HOME": "/tmp/elsewhere",
            "XDG_STATE_HOME": "/tmp/elsewhere",
            "MUSTER_STATE_DIR": "/tmp/elsewhere",
            "MUSTER_ETC_DIR": "/tmp/elsewhere",
            "FLEET_OPS_CONTROL_HOME": "/tmp/elsewhere",
            "TZ": "Pacific/Kiritimati",
            "PYTHONPATH": "/tmp/elsewhere",
        }
        with mock.patch.dict(os.environ, hostile):
            changed = ma.production_config()
        self.assertEqual(reference, changed)
        self.assertEqual(changed.etc_dir, "/etc/muster")
        self.assertEqual(changed.root_uid, 0)
        self.assertEqual(changed.uid, os.geteuid())
        import pwd

        self.assertEqual(
            changed.state_dir,
            os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, ".local", "state", "fleet-tools"),
        )
        self.assertTrue(changed.state_dir.endswith("/.local/state/fleet-tools"))
        self.assertIs(changed.clock, __import__("time").time)
        self.assertIs(changed.clock_state, ma.kernel_clock_state)

    # -- The production entry, run for real in a world of its own.
    #
    # The two tests below run the helper as the forced command does, as a `python3 -I` process with
    # no hook.  The helper's own constants name the real /etc/muster and the account's real state
    # directory, which a test must never read or write, so they are run from a private copy whose
    # two path constants are replaced (the copy of the file is the only thing changed).

    def production_world(self):
        """(env, copy folder, the copy's /etc/muster) with a valid pin, list and key, by constants."""
        env = Env(self)
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="muster-production-")))
        self.addCleanup(shutil.rmtree, str(root), True)
        folder = make_checkout(
            root,
            (
                ("muster_approval.py", 'PIN_DIR = "/etc/muster"', f"PIN_DIR = {str(env.etc)!r}"),
                (
                    "muster_approval.py",
                    "home = pwd.getpwuid(uid).pw_dir",
                    f"home = {str(env.home)!r}",
                ),
                # The copy's /etc/muster is this account's, as the real one is root's.
                ("muster_approval.py", "root_uid=0,", f"root_uid={UID},"),
            ),
        )
        return env, folder

    def run_production(self, folder, environment, stdin):
        result = subprocess.run(
            [sys.executable, "-I", str(folder / "fleet-ops-control.py"), "--from-muster"],
            input=stdin,
            capture_output=True,
            env=environment,
            cwd="/",
            timeout=60,
            check=False,
        )
        self.assertEqual(result.stderr, b"", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_the_production_entry_ignores_every_environment_variable(self):
        # A planted world reachable only through the environment, with a different valid key.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        planted = Path(os.path.realpath(tmp.name))
        state = planted / ".local" / "state" / "fleet-tools"
        state.mkdir(parents=True, mode=0o700)
        for parent in (planted, planted / ".local", planted / ".local" / "state", state):
            os.chmod(parent, 0o700)
        rogue = KEYS["rogue helper (not enrolled)"]
        pem = rogue.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        write_file(state / "muster-helper.key", pem, 0o600)
        before = sorted((p.name, p.stat().st_mtime_ns) for p in state.iterdir())
        hostile = {
            "HOME": str(planted),
            "XDG_STATE_HOME": str(planted),
            "XDG_RUNTIME_DIR": str(planted),
            "MUSTER_STATE_DIR": str(state),
            "MUSTER_ETC_DIR": str(planted),
            "MUSTER_HELPER_KEY": str(state / "muster-helper.key"),
            "FLEET_STATE_DIR": str(state),
            "PYTHONPATH": str(planted),
            "PYTHONHOME": str(planted),
            "PYTHONSTARTUP": str(planted / "x.py"),
            "SSH_ORIGINAL_COMMAND": COMMAND,
            "SSH_CONNECTION": "1.2.3.4 5 6 7",
            "LD_PRELOAD": "",
            "TZ": "UTC",
        }
        # Wrong key on purpose: the request is refused at Y5 and nothing could ever run.
        stdin = stdin_of(approval(signer=KEYS["device B approval"]))
        answers = []
        for environment in (hostile, {"SSH_ORIGINAL_COMMAND": COMMAND}):
            env, folder = self.production_world()
            code, answer = self.run_production(folder, environment, stdin)
            answers.append((code, answer, env))
        for code, answer, env in answers:
            self.assertEqual(code, 2)
            self.assertEqual(answer["kind"], "refusal")
            self.assertEqual(answer["body"]["errorCode"], "bad_signature")
            # Signed by the key the constants name, never the planted one.
            self.assertEqual(answer["keyId"], ma.HelperKey(HELPER).key_id)
            self.assertNotEqual(answer["keyId"], ma.HelperKey(rogue).key_id)
            self.assertTrue(ma.verify_signed_answer(answer, helper_public()))
        bodies = [{k: v for k, v in a["body"].items() if k != "refusedAt"} for _, a, _ in answers]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(sorted((p.name, p.stat().st_mtime_ns) for p in state.iterdir()), before)

    def test_the_production_entry_wants_the_four_words_from_ssh_original_command_only(self):
        # No SSH_ORIGINAL_COMMAND: nothing answers a request that was never made.
        env, folder = self.production_world()
        code, answer = self.run_production(folder, {}, b"")
        self.assertEqual(code, 2)
        self.assertEqual(answer["kind"], "refusal")
        self.assertEqual(answer["body"]["errorCode"], "bad_command")
        self.assertTrue(ma.verify_signed_answer(answer, helper_public()))

    def test_the_production_world_is_the_copys_constants_and_not_the_real_machine(self):
        env, folder = self.production_world()
        text = (folder / "muster_approval.py").read_text()
        self.assertNotIn('PIN_DIR = "/etc/muster"', text)
        self.assertIn(str(env.etc), text)
        self.assertIn(str(env.home), text)
        # The unmodified module is what ships: its constants are the real ones.
        self.assertEqual(ma.PIN_DIR, "/etc/muster")

    def test_the_hook_exists_only_as_a_parameter_the_forced_command_does_not_pass(self):
        # main() is called with no arguments by `__main__`: the hook is None, so production's own
        # configuration is used.
        source = (HERE / "fleet-ops-control.py").read_text()
        self.assertTrue(
            source.rstrip().endswith('if __name__ == "__main__":\n    sys.exit(main())')
        )
        parameters = inspect.signature(foc.main).parameters
        self.assertIsNone(parameters["muster"].default)
        self.assertEqual(
            foc.MUSTER_CHILD_ENV,
            {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )


class ResourcePolicyTests(Checks):
    """`resource-policy` compares the plan hash inside fleet_resource_control, under its own lock."""

    def setUp(self):
        import fleet_resource_control as resource
        import test_fleet_resource_control as fixtures

        self.resource = resource
        self.fixtures = fixtures
        self.store = fixtures.Store()
        self.events = []
        self.reloads = []

    def context(self, env):
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            config_path=env.allowlist,
            now=lambda: self.fixtures.NOW,
        )
        r = self.resource

        def runner(argv, **kwargs):
            self.reloads.append(argv)
            return 0, b""

        ctx.runner = runner
        ctx.resource_store = self.store
        ctx.resource_host_bounds = lambda: (16, 32 * r.GIB)
        ctx.resource_snapshot = lambda: self.fixtures.snapshot(self.store.p)
        ctx.resource_audit = self.events.append
        ctx.resource_monotonic = lambda: 0
        ctx.resource_sleep = lambda seconds: None
        return ctx

    def target(self):
        return f"cpu-weight.100.{self.resource.revision(self.store.p)}"

    def plan_hash(self):
        plan = self.resource.resource_plan(
            "cpu-weight", 50, 100, self.resource.revision(self.store.p)
        )
        return plan, hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()

    def command(self, mode):
        return f"server resource-policy {self.target()} --{mode}"

    def test_the_plan_is_the_resource_plan_object(self):
        env = Env(self)
        ctx = self.context(env)
        answer, code, _, rows = helper_request(env, self.command("plan"), job_stdin(), NOW, ctx=ctx)
        self.assertEqual(code, 0, answer)
        plan, digest = self.plan_hash()
        self.assertEqual(answer["body"]["plan"].encode(), ma.canonical_bytes(plan))
        self.assertEqual(answer["body"]["planSHA256"], digest)
        self.assertEqual(plan["old"], 50)
        self.assertEqual(plan["new"], 100)
        self.assertEqual(plan["auditTarget"], "cpu-weight")
        self.assertFalse(self.store.saves)
        self.assertEqual(rows, [])

    def test_an_approved_change_is_saved_audited_and_answered(self):
        env = Env(self)
        ctx = self.context(env)
        _, digest = self.plan_hash()
        document = approval(action="resource-policy", target=self.target(), planSHA256=digest)
        answer, code, runner, rows = helper_request(
            env, self.command("yes"), stdin_of(document), NOW, ctx=ctx
        )
        self.assertDone(answer, code)
        self.assertEqual(len(self.store.saves), 1)
        self.assertEqual(self.store.saves[0]["cpu_weight"], 100)
        self.assertEqual([e["result"] for e in self.events], ["intent", "done"])
        for event in self.events:
            self.assertEqual(event["source"], "muster-ios")
            self.assertEqual(event["route"], "from-muster")
            self.assertEqual(event["via"], "muster-ios")
            for name in ("jobId", "deviceId", "approvalNonce", "planSHA256"):
                self.assertEqual(
                    event[name], BASE_APPROVAL[name] if name != "planSHA256" else digest
                )
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        self.assertEqual(len(rows), 1, "the helper's own audit also holds one row")
        self.assertEqual(rows[0]["verb"], "server resource-policy")
        self.assertEqual(rows[0]["target"], "cpu-weight")
        self.assertEqual(rows[0]["route"], "from-muster")

    def test_a_plan_that_differs_under_the_lock_is_refused_and_nothing_is_written(self):
        env = Env(self)
        ctx = self.context(env)
        document = approval(
            action="resource-policy",
            target=self.target(),
            planSHA256=hashlib.sha256(b"x").hexdigest(),
        )
        answer, code, _, rows = helper_request(
            env, self.command("yes"), stdin_of(document), NOW, ctx=ctx
        )
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertFalse(self.store.saves)
        self.assertEqual(self.events, [], "no intent row, no write")
        self.assertEqual(self.reloads, [])
        self.assertIn(
            BASE_APPROVAL["approvalNonce"].encode(), env.ledger, "the nonce is spent first"
        )
        self.assertEqual(rows[0]["errorCode"], "plan_mismatch")

    def test_the_resource_function_refuses_a_write_without_a_verified_approval(self):
        env = Env(self)
        ctx = self.context(env)
        for kwargs in (
            {},
            {"approval": None},
            {"approval": {"jobId": "x"}},
            {"approval": dict(BASE_APPROVAL, planSHA256="bad")},
            {
                "approval": dict(BASE_APPROVAL, planSHA256=self.plan_hash()[1]),
                "source": "fleet-ops-ctl-owner-key",
            },
            {"approval": dict(BASE_APPROVAL, planSHA256=self.plan_hash()[1]), "plan_bytes": None},
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                call = {
                    "require_approval": True,
                    "source": "muster-ios",
                    "plan_bytes": ma.canonical_bytes,
                }
                call.update(kwargs)
                answer, code = self.resource.handle(
                    ctx, "resource-policy", self.target(), "yes", **call
                )
                self.assertEqual((code, answer["result"]), (2, "refused"))
                self.assertFalse(self.store.saves)
                self.assertEqual(self.events, [])

    def test_the_default_call_is_unchanged(self):
        env = Env(self)
        ctx = self.context(env)
        answer, code = self.resource.handle(ctx, "resource-policy", self.target(), "yes")
        self.assertEqual((code, answer["result"]), (0, "done"))
        self.assertTrue(all(e["source"] == "fleet-ops-ctl-owner-key" for e in self.events))
        self.assertNotIn("plan", answer)
        self.assertNotIn("route", self.events[0])

    def stale_target(self):
        return "cpu-weight.100." + "a" * 64

    def test_a_plan_for_a_stale_revision_is_refused_with_the_reason_revision_stale(self):
        env = Env(self)
        ctx = self.context(env)
        command = f"server resource-policy {self.stale_target()} --plan"
        answer, code, _, rows = helper_request(env, command, job_stdin(), NOW, ctx=ctx)
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="revision_stale")
        self.assertEqual(rows, [])
        self.assertFalse(self.store.saves)

    def test_an_approval_that_meets_a_changed_policy_is_failed_with_the_same_reason(self):
        env = Env(self)
        ctx = self.context(env)
        _, digest = self.plan_hash()
        target = self.target()
        document = approval(action="resource-policy", target=target, planSHA256=digest)
        self.store.p["cpu_weight"] = 60  # the saved policy changed after the plan was made
        answer, code, _, rows = helper_request(
            env, f"server resource-policy {target} --yes", stdin_of(document), NOW, ctx=ctx
        )
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "failed")
        self.assertEqual(answer["body"]["detail"], "plan_refused: revision_stale")
        self.assertFalse(self.store.saves)
        self.assertEqual(self.reloads, [])
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger, "the nonce is spent")
        self.assertEqual([(r["target"], r["result"]) for r in rows], [("cpu-weight", "failed")])

    def test_an_exception_out_of_a_policy_write_is_unknown(self):
        # Once the write is under way, an exception out of it may have followed a PATCH that landed.
        env = Env(self)
        ctx = self.context(env)
        _, digest = self.plan_hash()
        document = approval(action="resource-policy", target=self.target(), planSHA256=digest)

        def explode(*args, **kwargs):
            raise RuntimeError("the connection was lost")

        with mock.patch.object(self.resource, "handle", explode):
            answer, code, _, rows = helper_request(
                env, self.command("yes"), stdin_of(document), NOW, ctx=ctx
            )
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)

    def test_only_the_route_sees_the_error_code_of_a_stale_revision(self):
        env = Env(self)
        ctx = self.context(env)
        for mode in ("plan", "yes"):
            with self.subTest(mode=mode):
                answer, code = self.resource.handle(
                    ctx, "resource-policy", self.stale_target(), mode
                )
                self.assertEqual((code, answer["result"]), (2, "refused"))
                self.assertNotIn("errorCode", answer)
        self.assertFalse(self.store.saves)


# MARK: - Version 5: the clock, faults, acknowledgements, targets and the state files


def rows_of(backend):
    return backend.rows


class ClockTests(Checks):
    """Section 7: an approval is refused `clock_unsynced` unless the kernel's clock is synchronised."""

    def approve(self, clock_state, now=NOW, backend=None, **overrides):
        env = Env(self)
        answer, code, backend = module_request(
            env, COMMAND, stdin_of(approval(**overrides)), now, backend, clock_state=clock_state
        )
        return answer, code, backend, env

    def test_a_synchronised_clock_lets_the_approval_through(self):
        for name, reading in (
            ("TIME_OK", (0, 0, 175_000)),
            ("a leap second pending", (1, 0, 175_000)),
            ("leap second deleted", (2, 0, 175_000)),
            ("leap second in progress", (3, 0, 175_000)),
            ("waiting after a leap second", (4, 0, 175_000)),
            ("other status bits", (0, 0x2001, 1000)),
            ("maxerror of exactly one second", (0, 0, 1_000_000)),
        ):
            with self.subTest(clock=name):
                answer, code, _, _ = self.approve(lambda reading=reading: reading)
                self.assertDone(answer, code)

    def test_each_way_the_clock_is_not_synchronised_is_clock_unsynced(self):
        def fails():
            raise OSError(1, "adjtimex failed")

        for name, reader in (
            ("the call fails", fails),
            ("the call raises anything", lambda: 1 / 0),
            ("TIME_ERROR", lambda: (5, 0, 1000)),
            ("STA_UNSYNC alone", lambda: (0, 0x40, 1000)),
            ("STA_UNSYNC among other bits", lambda: (0, 0x2041, 1000)),
            ("maxerror above one second", lambda: (0, 0, 1_000_001)),
            ("a negative return value", lambda: (-1, 0, 0)),
            ("not three numbers", lambda: (0, 0)),
            ("nothing", lambda: None),
            ("a float", lambda: (0, 0, 1.5)),
            ("a boolean", lambda: (0, 0, True)),
            ("text", lambda: ("0", 0, 0)),
        ):
            with self.subTest(clock=name):
                answer, code, backend, env = self.approve(reader)
                self.assertRefused(answer, code, "clock_unsynced", verified=True)
                self.assertEqual(backend.events, [], "no lock, no plan, no run")
                self.assertIsNone(env.ledger, "the nonce is not spent")
                self.assertEqual(backend.rows, [], "a refusal before Y8 writes no row")

    def test_the_clock_is_checked_after_the_signature_and_before_the_window(self):
        def unsynced():
            return (5, 0, 0)

        # Y5 comes first: a bad signature is `bad_signature` whatever the clock says.
        answer, code, _, _ = self.approve(unsynced, signer=KEYS["device A request"])
        self.assertRefused(answer, code, "bad_signature", verified=False)
        # Y6 asks the clock before it looks at the window: an expired approval and one whose window
        # is too long are both `clock_unsynced` while the clock cannot be trusted.
        answer, code, _, _ = self.approve(unsynced, now="2026-11-07T21:09:00Z")
        self.assertRefused(answer, code, "clock_unsynced", verified=True)
        answer, code, _, _ = self.approve(unsynced, expiresAt="2026-11-07T21:09:00Z")
        self.assertRefused(answer, code, "clock_unsynced", verified=True)
        # With a good clock the same approvals are refused for their own reasons.
        answer, code, _, _ = self.approve(synced_clock, now="2026-11-07T21:09:00Z")
        self.assertRefused(answer, code, "expired", verified=True)

    def test_only_an_approval_asks_the_clock_and_it_asks_once(self):
        asked = []

        def reader():
            asked.append(1)
            return (5, 0, 0)  # unsynchronised: a plan, a read or an acknowledgement ignores it

        env = Env(self)
        for index, (command, stdin) in enumerate(
            (
                (PLAN_COMMAND, job_stdin()),
                ("server seat-inventory claude --plan", job_stdin()),
                (CURRENT_COMMAND, CURRENT_STDIN),
            )
        ):
            with self.subTest(command=command):
                answer, code, _ = module_request(
                    env, command, stdin, later(10 * index), clock_state=reader
                )
                self.assertEqual(code, 0, answer)
        self.assertEqual(asked, [])
        module_request(env, COMMAND, stdin_of(approval()), later(100), clock_state=reader)
        self.assertEqual(asked, [1], "one call per approval")

    def test_the_window_uses_the_reading_taken_after_the_clock_check(self):
        events = []
        readings = iter([NOW_EPOCH + step for step in range(100)])

        def clock():
            value = next(readings)
            events.append(("clock", value))
            return value

        def reader():
            events.append(("clock_state",))
            return synced_clock()

        seen = []
        real = ma.check_window

        def window(approval_fields, now):
            seen.append(now)
            events.append(("window", now))
            return real(approval_fields, now)

        env = Env(self)
        with mock.patch.object(ma, "check_window", window):
            answer, code, _ = module_request(
                env, COMMAND, stdin_of(approval()), NOW, clock=clock, clock_state=reader
            )
        self.assertDone(answer, code)
        self.assertEqual(len(seen), 1)
        position = events.index(("clock_state",))
        self.assertEqual(events[position + 1][0], "clock")
        self.assertEqual(events[position + 2], ("window", events[position + 1][1]))

    def test_the_default_configuration_asks_the_kernel_and_a_test_may_replace_it(self):
        config = ma.MusterConfig("/etc/x", "/state", 0, 1000, __import__("time").time)
        self.assertIs(config.clock_state, ma.kernel_clock_state)
        self.assertIs(ma.production_config().clock_state, ma.kernel_clock_state)
        self.assertIs(ma.MusterConfig("a", "b", 0, 1, None, synced_clock).clock_state, synced_clock)
        self.assertNotEqual(
            config, ma.MusterConfig("/etc/x", "/state", 0, 1000, config.clock, synced_clock)
        )

    def test_the_probe_is_adjtimex_with_modes_zero_and_nothing_else(self):
        recorded = []

        class Function:
            result = 0

            def __call__(self, reference):
                timex = reference._obj
                recorded.append(timex.modes)
                timex.status = 0x2000
                timex.maxerror = 1234
                return self.result

        class FakeLibc:
            adjtimex = Function()

        with mock.patch.object(ma.ctypes, "CDLL", lambda name, use_errno=False: FakeLibc()):
            self.assertEqual(ma.kernel_clock_state(), (0, 0x2000, 1234))
            FakeLibc.adjtimex.result = -1
            with self.assertRaises(OSError):
                ma.kernel_clock_state()
        self.assertEqual(recorded, [0, 0], "modes is 0: the call can only read")

    def test_the_probe_structure_is_linux_struct_timex(self):
        if not sys.platform.startswith("linux") or ma.ctypes.sizeof(ma.ctypes.c_long) != 8:
            self.skipTest("the layout is checked for 64-bit Linux")
        self.assertEqual(ma.ctypes.sizeof(ma._Timex), 208)
        self.assertEqual(ma._Timex.maxerror.offset, 24)
        self.assertEqual(ma._Timex.status.offset, 40)

    def test_the_real_probe_answers_and_works_under_isolated_mode(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("adjtimex is a Linux call")
        state, status, maxerror = ma.kernel_clock_state()
        self.assertTrue(all(type(value) is int for value in (state, status, maxerror)))
        self.assertTrue(0 <= state <= 5 and maxerror >= 0)
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.kernel_clock_state())\n"
            "print(module.clock_is_synchronised(module.kernel_clock_state))\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(HERE / "muster_approval.py")],
            capture_output=True,
            env={},
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.decode().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(lines[1], ("True", "False"))

    def test_clock_is_synchronised_is_the_one_rule(self):
        self.assertTrue(ma.clock_is_synchronised(lambda: (0, 0, 0)))
        self.assertFalse(ma.clock_is_synchronised(lambda: (ma.TIME_ERROR, 0, 0)))
        self.assertFalse(ma.clock_is_synchronised(lambda: (0, ma.STA_UNSYNC, 0)))
        self.assertFalse(ma.clock_is_synchronised(lambda: (0, 0, ma.CLOCK_MAX_ERROR_US + 1)))
        self.assertTrue(ma.clock_is_synchronised(lambda: (0, 0, ma.CLOCK_MAX_ERROR_US)))


class FaultTests(Checks):
    """Section 12: before Y8 a fault is `internal_error`; after it, a signed `failed` result."""

    def approve(self, backend, now=NOW, **overrides):
        env = Env(self)
        answer, code, backend = module_request(
            env, COMMAND, stdin_of(approval(**overrides)), now, backend
        )
        return answer, code, backend, env

    def assertFailedResult(self, answer, code, detail, outcome="failed"):
        self.assertEqual(code, 1, answer)
        self.assertEqual(answer["kind"], "result", answer)
        self.assertSigned(answer)
        body = answer["body"]
        self.assertEqual(body["outcome"], outcome)
        self.assertEqual(body["detail"], detail)
        self.assertEqual(body["approvalNonce"], BASE_APPROVAL["approvalNonce"])
        self.assertEqual(body["planSHA256"], BASE_APPROVAL["planSHA256"])

    def assertOneFailedRow(self, backend, result="failed"):
        self.assertEqual(len(backend.rows), 1, backend.rows)
        row = backend.rows[0]
        self.assertEqual(
            (row["scope"], row["verb"], row["target"], row["result"]),
            ("server", "restart", "fleet-server-status.timer", result),
        )
        self.assertEqual(row["route"], "from-muster")
        self.assertEqual(row["via"], "muster-ios")
        for name in ("jobId", "deviceId", "approvalNonce", "planSHA256"):
            self.assertEqual(row[name], BASE_APPROVAL[name])
        self.assertNotIn("errorCode", row, "an error code belongs to a refusal")

    def test_a_fault_before_the_nonce_is_spent_is_internal_error_and_nothing_ran(self):
        backend = FakeBackend()

        def boom():
            raise RuntimeError("the lock exploded")

        backend.lock = boom
        answer, code, backend, env = self.approve(backend)
        self.assertRefused(answer, code, "internal_error", verified=True)
        self.assertIsNone(env.ledger)
        self.assertEqual((backend.runs, backend.rows), ([], []))
        self.assertNotIn("exploded", json.dumps(answer))
        # The command line's own check is a step too (P2): a fault there answers the same.
        backend = FakeBackend()

        def worse(verb, target):
            raise RuntimeError("no grammar")

        backend.target_ok = worse
        answer, code, backend, env = self.approve(backend)
        self.assertRefused(answer, code, "internal_error", verified=False)
        self.assertEqual(answer["body"]["claimed"], {})

    def test_an_unexpected_fault_after_the_nonce_is_a_failed_result_with_its_one_row(self):
        # Y9 builds the plan before anything is run, so a fault there provably started nothing.
        for name, plant in (
            ("a KeyError in the plan", KeyError("boom")),
            ("a RuntimeError in the plan", RuntimeError("boom")),
            ("a TypeError in the plan", TypeError("boom")),
        ):
            with self.subTest(fault=name):
                backend = FakeBackend()

                def boom(verb, target, plant=plant):
                    raise plant

                backend.plan_bytes = boom
                answer, code, backend, env = self.approve(backend)
                self.assertFailedResult(answer, code, "internal_error")
                self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
                self.assertEqual(backend.runs, [], "nothing ran")
                self.assertOneFailedRow(backend)
                self.assertNotIn("boom", json.dumps(answer))

    def test_a_plan_refusal_after_the_nonce_names_the_planners_reason_in_the_result(self):
        self.assertEqual(len(ma.PLAN_REFUSED_REASONS), 14)
        for reason in ma.PLAN_REFUSED_REASONS:
            with self.subTest(reason=reason):
                backend = FakeBackend()
                backend.refuse_plan = "the planner said no"
                backend.refuse_code = reason
                answer, code, backend, env = self.approve(backend)
                self.assertFailedResult(answer, code, "plan_refused: " + reason)
                self.assertOneFailedRow(backend)
                self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        for other in (None, "", "sudo_missing", "x" * 40, "plan_mismatch", "Update_Busy"):
            with self.subTest(other=other):
                backend = FakeBackend()
                backend.refuse_plan = "something else"
                backend.refuse_code = other
                answer, code, backend, _ = self.approve(backend)
                self.assertFailedResult(answer, code, "internal_error")
                self.assertOneFailedRow(backend)

    def test_a_run_that_raises_after_it_was_called_is_unknown_with_one_row(self):
        # The action may have begun and its end was never seen: `unknown`, never `failed`, so that a
        # phone that retries on `failed` cannot run it twice.
        for name, error in (
            ("a plain exception", RuntimeError("secret")),
            ("a fault that says it started", ma.RunFault(started=True)),
        ):
            with self.subTest(case=name):
                backend = FakeBackend()
                backend.run_error = error
                answer, code, backend, env = self.approve(backend)
                self.assertFailedResult(
                    answer,
                    code,
                    "the helper stopped unexpectedly while running",
                    outcome="unknown",
                )
                self.assertOneFailedRow(backend, "unknown")
                self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
                self.assertNotIn("secret", json.dumps(answer))

    def test_a_run_that_says_it_never_started_is_failed(self):
        backend = FakeBackend()
        backend.run_error = ma.RunFault(started=False)
        answer, code, backend, _ = self.approve(backend)
        self.assertFailedResult(answer, code, "the helper stopped unexpectedly while running")
        self.assertOneFailedRow(backend)

    def test_a_refusal_inside_the_run_that_is_not_a_plan_mismatch_is_a_failed_result(self):
        # The shared function refused after the nonce was spent, before it ran anything (an update
        # that turned busy, say): for the route that is a fault of the run, never a refusal, and the
        # action provably did not begin, so it is `failed`.
        for audit_id, rows in ((None, 1), ("0123456789ab", 0)):
            with self.subTest(audit_id=audit_id):
                backend = FakeBackend()
                backend.run = lambda verb, target, fields, audit_id=audit_id: ma.RunResult(
                    "refused", "plan_refused: update_busy", None, audit_id
                )
                answer, code, backend, _ = self.approve(backend)
                self.assertFailedResult(answer, code, "plan_refused: update_busy")
                self.assertEqual(len(backend.rows), rows, "the shared function's own row counts")
                if rows:
                    self.assertOneFailedRow(backend)

    def test_a_plan_mismatch_inside_the_run_is_the_refusal_and_has_one_row(self):
        for audit_id, rows in ((None, 1), ("0123456789ab", 0)):
            with self.subTest(audit_id=audit_id):
                backend = FakeBackend()
                backend.run = lambda verb, target, fields, audit_id=audit_id: ma.RunResult(
                    "refused",
                    "the approved plan is not the plan this request would run",
                    None,
                    audit_id,
                    "plan_mismatch",
                )
                answer, code, backend, _ = self.approve(backend)
                self.assertRefused(answer, code, "plan_mismatch", verified=True)
                self.assertEqual(len(backend.rows), rows)
                if rows:
                    self.assertEqual(backend.rows[0]["errorCode"], "plan_mismatch")
                    self.assertEqual(backend.rows[0]["result"], "refused")

    def test_the_outcomes_and_their_exit_statuses(self):
        for outcome, expected_code in (
            ("done", 0),
            ("submitted", 0),
            ("failed", 1),
            ("unknown", 1),
        ):
            with self.subTest(outcome=outcome):
                backend = FakeBackend(outcome=outcome)
                answer, code, backend, _ = self.approve(backend)
                self.assertEqual(code, expected_code, answer)
                self.assertEqual(answer["kind"], "result")
                self.assertEqual(answer["body"]["outcome"], outcome)
                self.assertSigned(answer)
        # An outcome the module does not know came back from a run that was called: unknown.
        for outcome in ("planned", "bogus", "", None):
            with self.subTest(outcome=outcome):
                backend = FakeBackend(outcome=outcome)
                answer, code, backend, _ = self.approve(backend)
                self.assertFailedResult(answer, code, "internal_error", outcome="unknown")

    def test_the_last_resort_still_answers_and_writes_the_row(self):
        # Even when answering the fault itself faults, an authentic spent approval gets its result
        # and its row, outside the lock, rather than nothing.  The run was called, so: unknown.
        backend = FakeBackend()
        backend.run_error = RuntimeError("x")
        with mock.patch.object(ma._Flow, "fault", side_effect=RuntimeError("signing failed")):
            answer, code, backend, env = self.approve(backend)
        self.assertFailedResult(answer, code, "internal_error", outcome="unknown")
        self.assertOneFailedRow(backend, "unknown")
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        # Before the run was called it is `failed`.
        backend = FakeBackend()

        def boom(verb, target):
            raise KeyError("x")

        backend.plan_bytes = boom
        with mock.patch.object(ma._Flow, "fault", side_effect=RuntimeError("signing failed")):
            answer, code, backend, env = self.approve(backend)
        self.assertFailedResult(answer, code, "internal_error")
        self.assertOneFailedRow(backend)


class CurrentTests(Checks):
    """`muster trust-list current --plan`: what the helper holds, signed (section 12, K1 to K3)."""

    def ask(self, env, now=NOW, stdin=None, command=CURRENT_COMMAND, backend=None, **config):
        return module_request(
            env, command, CURRENT_STDIN if stdin is None else stdin, now, backend, **config
        )

    def test_the_acknowledgement_of_the_held_list(self):
        env = Env(self)
        answer, code, backend = self.ask(env)
        self.assertEqual(code, 0)
        self.assertSigned(answer)
        self.assertEqual(answer["kind"], "ack")
        body = answer["body"]
        self.assertEqual(
            set(body),
            {
                "contract",
                "helperTrustListVersion",
                "closed",
                "signedByCurrentKey",
                "trustListSHA256",
                "finalRefusals",
                "at",
            },
        )
        self.assertEqual(body["contract"], "muster-signed-ack/1")
        self.assertEqual(body["helperTrustListVersion"], 1)
        self.assertEqual(body["closed"], False)
        self.assertEqual(body["signedByCurrentKey"], True)
        self.assertEqual(body["trustListSHA256"], FIXTURE["listBodyHashes"]["v1"])
        self.assertEqual(body["finalRefusals"], [])
        self.assertEqual(body["at"], NOW)
        self.assertEqual(backend.events, [], "no plan, no lock, no read, no run")
        self.assertEqual(backend.rows, [])
        self.assertIsNone(env.ledger)

    def test_a_closed_list_and_a_list_that_is_not_signed_by_the_current_key(self):
        answer, _, _ = self.ask(Env(self, stored="v3"))
        self.assertEqual(answer["body"]["closed"], True)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 3)
        self.assertEqual(answer["body"]["trustListSHA256"], FIXTURE["listBodyHashes"]["v3"])
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        env = Env(self, stored=resign(body, KEYS["rogue owner (not pinned)"]))
        answer, code, _ = self.ask(env)
        self.assertEqual(code, 0)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
        self.assertEqual(answer["body"]["signedByCurrentKey"], False)
        # A list from before a rotation is the same case: its key is not the current one.
        history = {
            "fromVersion": 1,
            "ownerKey": b64u(spki(KEYS["rogue owner (not pinned)"])),
            "toSHA256": "c" * 64,
            "toVersion": 1,
        }
        new = {"fromVersion": 2, "ownerKey": FIXTURE["pin"], "toSHA256": None, "toVersion": None}
        env = Env(self)
        write_file(
            env.etc / "owner-history.jsonl",
            ma.canonical_bytes(history) + b"\n" + ma.canonical_bytes(new) + b"\n",
            0o644,
        )
        answer, _, _ = self.ask(env)
        self.assertEqual(answer["body"]["signedByCurrentKey"], False)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)

    def test_when_nothing_is_held(self):
        answer, code, _ = self.ask(Env(self, stored=None))
        self.assertEqual(code, 0)
        self.assertSigned(answer)
        body = answer["body"]
        self.assertEqual(
            (
                body["helperTrustListVersion"],
                body["closed"],
                body["signedByCurrentKey"],
                body["trustListSHA256"],
                body["finalRefusals"],
            ),
            (None, False, False, None, []),
        )

    def test_it_needs_neither_the_clock_nor_a_lock_and_writes_nothing(self):
        env = Env(self)
        asked = []
        answer, code, backend = self.ask(
            env, clock_state=lambda: asked.append(1) or (5, 0, 0), backend=FakeBackend(busy=True)
        )
        self.assertEqual((code, asked, backend.events), (0, [], []))
        self.assertEqual(env.audit_rows(), [])
        self.assertFalse((env.state / "muster-nonces.jsonl").exists())

    def test_it_is_rate_limited_as_its_own_class(self):
        env = Env(self)
        base = epoch(NOW)
        self.assertEqual(self.ask(env, base)[1], 0)
        for offset in (0, 1, 1.999):
            answer, code, backend = self.ask(env, base + offset)
            self.assertRefused(answer, code, "rate_limited", verified=False)
            self.assertEqual(answer["body"]["claimed"], {})
            self.assertEqual(backend.events, [])
        self.assertEqual(self.ask(env, base + 2)[1], 0)
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual([event[:2] for event in data["events"]], [["current", "current"]] * 2)
        # 600 an hour, counted apart from the plan and approval classes.
        env = Env(self)
        for index in range(600):
            self.assertEqual(self.ask(env, base + index * 2)[1], 0, index)
        answer, code, _ = self.ask(env, base + 1200)
        self.assertRefused(answer, code, "rate_limited", verified=False)
        self.assertEqual(module_request(env, PLAN_COMMAND, job_stdin(), base + 1200)[1], 0)

    def test_a_list_that_cannot_be_read_is_state_unsafe(self):
        env = Env(self)
        write_file(env.state / "muster-trust.json", b"{", 0o600)
        answer, code, _ = self.ask(env)
        self.assertRefused(answer, code, "state_unsafe", verified=False)
        self.assertEqual(answer["body"]["claimed"], {})
        self.assertIsNone(answer["body"]["helperTrustListVersion"])

    def test_the_refusal_names_nothing_it_did_not_verify(self):
        env = Env(self)
        for stdin, name in (
            (b"{}", "no protocol"),
            (compact({"protocol": "muster-helper/2"}), "another protocol"),
            (compact({"protocol": "muster-helper/1", "job": dict(JOB)}), "a job"),
            (compact({"protocol": "muster-helper/1", "x": 1}), "an extra key"),
            (b"[]", "not an object"),
            (b"", "empty"),
            (b'{"protocol":"muster-helper/1","protocol":"muster-helper/1"}', "a duplicate key"),
        ):
            with self.subTest(case=name):
                answer, code, backend = self.ask(env, stdin=stdin)
                self.assertRefused(answer, code, "bad_input", verified=False)
                self.assertEqual(answer["body"]["claimed"], {})
        self.assertFalse((env.state / "muster-rate.json").exists(), "refused at P3, never counted")

    def test_whitespace_and_key_order_do_not_matter(self):
        env = Env(self)
        answer, code, _ = self.ask(env, stdin=b'  {\n "protocol" : "muster-helper/1"\n}\n ')
        self.assertEqual(code, 0, answer)

    def test_the_mode_must_be_plan_and_the_pin_is_checked_first(self):
        env = Env(self)
        answer, code, _ = self.ask(env, command="muster trust-list current --yes")
        self.assertRefused(answer, code, "mode_not_allowed", verified=False)
        self.assertFalse((env.state / "muster-rate.json").exists())
        os.chmod(env.etc, 0o777)
        answer, code, _ = self.ask(env)
        self.assertRefused(answer, code, "pin_unsafe", verified=False)

    def test_the_key_comes_first_as_for_every_request(self):
        env = Env(self, key=False)
        answer, code, _ = self.ask(env)
        self.assertEqual(
            (code, answer["body"]["errorCode"], answer["signature"]),
            (2, "helper_key_missing", None),
        )

    def test_through_the_helper_in_process_and_as_a_subprocess(self):
        env = Env(self)
        answer, code, runner, rows = helper_request(env, CURRENT_COMMAND, CURRENT_STDIN, NOW)
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["kind"], "ack")
        self.assertSigned(answer)
        self.assertEqual((runner.calls, rows), ([], []))
        env = Env(self)
        answer, code, calls, rows = subprocess_request(
            self, env, CURRENT_COMMAND, CURRENT_STDIN, NOW
        )
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
        self.assertEqual((calls, rows), ([], []))


def write_allowlist(env, **server):
    """Replace the helper's allowlist with the tests' own, plus the given `server` keys."""
    document = json.loads(json.dumps(ALLOWLIST))
    document["server"].update(server)
    write_file(env.allowlist, json.dumps(document).encode(), 0o600)


REDEPLOY_UUID = "abc123def456"
REDEPLOY_ROW = {
    "uuid": REDEPLOY_UUID,
    "name": "My App",
    "type": "application",
    "status": "running",
    "uuidSource": "label",
}


def write_status_report(env, rows, age=30):
    report = {
        "tool": "fleet-server-status",
        "generatedAt": ma.format_time(NOW_EPOCH - age),
        "coolify": rows,
    }
    write_file(env.state / "server-status.json", json.dumps(report).encode(), 0o600)


class TargetGrammarTests(Checks):
    """P2: a word that cannot be the target of its verb is `bad_command`, before the request is
    counted.  A word that can be, but is not on the allowlist, is a plan refusal, after it."""

    def backend(self, env):
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        return foc._MusterBackend(ctx, ma)

    def test_the_grammar_of_each_verbs_target(self):
        env = Env(self)
        backend = self.backend(env)
        digest = "a" * 64
        for verb, target, expected in (
            ("start", "fleet-server-status.timer", True),
            ("restart", "botfleet-server.service", True),
            ("stop", "Unit_1.service", True),
            ("logs", "fleet-server-status.timer", True),
            ("restart", "x", False),
            ("restart", "a.b", False),
            ("logs", "x.target", False),
            ("restart", "-bad.service", False),
            ("restart", "a@b.service", False),
            ("restart", "a" * 122 + ".service", False),
            ("update", "fleet-cli", True),
            ("update", "Fleet", False),
            ("update", "a_b", False),
            ("update", "-x", False),
            ("update", "a" * 65, False),
            ("redeploy", REDEPLOY_UUID, True),
            ("redeploy", "a" * 64, True),
            ("redeploy", "abc", False),
            ("redeploy", "a" * 65, False),
            ("redeploy", "a.b.c.d.e.f", False),
            ("update-status", "fleet-cli." + "a" * 32 + ".0", True),
            ("update-status", "fleet-cli." + "a" * 32 + ".18446744073709551615", True),
            ("update-status", "fleet-cli." + "a" * 32 + ".18446744073709551616", False),
            ("update-status", "fleet-cli." + "A" * 32 + ".0", False),
            ("update-status", "fleet-cli." + "a" * 32 + ".01", False),
            ("update-status", "abc", False),
            ("resource-status", "coding", True),
            ("resource-status", "other", False),
            ("resource-policy", "cpu-weight.100." + digest, True),
            ("resource-policy", "borrow-idle.1." + digest, True),
            ("resource-policy", "cpu-weight.0." + digest, False),
            ("resource-policy", "borrow-idle.2." + digest, False),
            ("resource-policy", "cpu-weight.0100." + digest, False),
            ("resource-policy", "nonesuch.1." + digest, False),
            ("resource-policy", "cpu-weight.100." + "A" * 64, False),
            ("resource-policy", "cpu-weight.100.abc", False),
            ("seat-inventory", "claude", True),
            ("seat-inventory", "nonesuch", False),
            ("seat-inventory", "a.b", False),
            ("frobnicate", "x", False),
        ):
            with self.subTest(verb=verb, target=target[:30]):
                self.assertIs(backend.target_ok(verb, target), expected)

    def test_a_target_that_cannot_be_one_is_bad_command_and_is_not_counted(self):
        for command in (
            "server restart a.b --plan",
            "server restart a.b --yes",
            "server logs x --plan",
            "server update Fleet --plan",
            "server redeploy abc --plan",
            "server update-status abc --plan",
            "server resource-policy cpu-weight.0." + "a" * 64 + " --plan",
            "server seat-inventory nonesuch --plan",
        ):
            with self.subTest(command=command[:50]):
                env = Env(self)
                stdin = stdin_of(approval()) if command.endswith("--yes") else job_stdin()
                answer, code, runner, rows = helper_request(env, command, stdin, NOW)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(answer["body"]["claimed"], {})
                self.assertFalse((env.state / "muster-rate.json").exists(), "never counted")
                self.assertIsNone(env.ledger)
                self.assertEqual((runner.calls, rows), ([], []))

    def test_the_module_asks_the_backend_before_it_counts_anything(self):
        for command, stdin in (
            (PLAN_COMMAND, job_stdin()),
            (COMMAND, stdin_of(approval())),
            ("server resource-status coding --plan", job_stdin()),
        ):
            with self.subTest(command=command):
                env = Env(self)
                backend = FakeBackend()
                backend.target_check = lambda verb, target: False
                answer, code, backend = module_request(env, command, stdin, NOW, backend)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(backend.events, [])
                self.assertFalse((env.state / "muster-rate.json").exists())
        seen = []
        backend = FakeBackend()
        backend.target_check = lambda verb, target: seen.append((verb, target)) or True
        module_request(Env(self), PLAN_COMMAND, job_stdin(), NOW, backend)
        self.assertEqual(seen, [("restart", "fleet-server-status.timer")])

    def test_a_listed_looking_word_that_is_not_on_the_allowlist_is_a_counted_plan_refusal(self):
        env = Env(self)
        answer, code, runner, rows = helper_request(
            env, "server restart no-such-unit.timer --plan", job_stdin(), NOW
        )
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="allowlist_miss")
        data = json.loads((env.state / "muster-rate.json").read_text())
        self.assertEqual(len(data["events"]), 1, "it passed P2, so it is counted")
        # The same word on a `--yes` is refused after the nonce is spent, as a failed result that
        # names the reason, with its one row.
        env = Env(self)
        document = approval(target="no-such-unit.timer")
        answer, code, runner, rows = helper_request(
            env, "server restart no-such-unit.timer --yes", stdin_of(document), NOW
        )
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "failed")
        self.assertEqual(answer["body"]["detail"], "plan_refused: allowlist_miss")
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        self.assertEqual(runner.calls, [])
        self.assertEqual(
            [(r["verb"], r["target"], r["result"]) for r in rows],
            [("server restart", "no-such-unit.timer", "failed")],
        )


class LedgerClockStepTests(Checks):
    """A clock that steps cannot make the ledger forget a nonce (the retention itself is tested with the
    ledger, in LedgerTests)."""

    def approve(self, env, document, now, backend=None):
        return module_request(env, COMMAND, stdin_of(document), now, backend)

    def test_a_replay_is_a_replay_whatever_the_rate_file_remembers_of_the_stepped_clock(self):
        # The approval ran at NOW.  The clock stepped an hour forward for a second one, and back: the
        # rate file holds an event from the future, and the replay is refused as a replay, before the
        # rate step is even reached (the ledger is read first), so it uses no budget either.
        env = Env(self)
        first = approval()
        self.assertDone(*self.approve(env, first, NOW)[:2])
        second = approval(
            approvalNonce="b" * 32,
            issuedAt="2026-11-07T22:00:00Z",
            expiresAt="2026-11-07T22:02:00Z",
        )
        self.assertDone(*self.approve(env, second, "2026-11-07T22:00:30Z")[:2])
        events_before = json.loads((env.state / "muster-rate.json").read_text())["events"]
        answer, code, backend = self.approve(env, first, "2026-11-07T21:01:30Z")
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        events_after = json.loads((env.state / "muster-rate.json").read_text())["events"]
        self.assertEqual(events_after, events_before, "a replay records no event")


class WindowExactnessTests(Checks):
    """Section 7: `now` is compared as it is, a float, with no grace.  (A reviewer asked for whole
    seconds; section 7 says `expiresAt` + 0.001 is expired and `now` = `expiresAt` is accepted.)"""

    def ask(self, now, **overrides):
        env = Env(self)
        return module_request(env, COMMAND, stdin_of(approval(**overrides)), now)

    def test_the_literal_epoch_of_now_is_right(self):
        self.assertEqual(NOW_EPOCH, epoch(NOW))
        self.assertEqual(NOW_EPOCH, calendar_epoch(2026, 11, 7, 21, 0, 30))

    def test_now_equal_to_expires_at_is_accepted_and_a_millisecond_later_is_expired(self):
        expires = epoch("2026-11-07T21:02:00Z")
        for now, expected in (
            (expires - 0.0001, None),
            (expires, None),
            (expires + 0.001, "expired"),
            (expires + 0.5, "expired"),
            (expires + 1, "expired"),
        ):
            with self.subTest(now=now - expires):
                answer, code, _ = self.ask(now)
                if expected is None:
                    self.assertDone(answer, code)
                else:
                    self.assertRefused(answer, code, expected, verified=True)

    def test_issued_at_sixty_seconds_ahead_is_accepted_and_a_millisecond_more_is_not_yet_valid(
        self,
    ):
        issued = epoch("2026-11-07T21:00:00Z")
        for ahead, expected in (
            (59.999, None),
            (60.0, None),
            (60.001, "not_yet_valid"),
            (60.9, "not_yet_valid"),
        ):
            with self.subTest(ahead=ahead):
                answer, code, _ = self.ask(issued - ahead)
                if expected is None:
                    self.assertDone(answer, code)
                else:
                    self.assertRefused(answer, code, expected, verified=True)

    def test_the_window_is_at_most_120_seconds(self):
        for seconds, expected in (
            (120, None),
            (121, "window_too_long"),
            (60, None),
            (0, "window_too_long"),
            (-1, "window_too_long"),
        ):
            with self.subTest(seconds=seconds):
                issued = epoch("2026-11-07T21:00:00Z")
                answer, code, _ = self.ask(
                    issued + 10,
                    expiresAt=ma.format_time(issued + seconds),
                )
                if expected is None:
                    self.assertDone(answer, code)
                else:
                    self.assertRefused(answer, code, expected, verified=True)

    def test_the_comparison_is_not_rounded_to_seconds(self):
        # `now` = 21:02:00.9 is more than `expiresAt`: it is not truncated to 21:02:00 first.
        answer, code, _ = self.ask(epoch("2026-11-07T21:02:00Z") + 0.9)
        self.assertRefused(answer, code, "expired", verified=True)
        answer, code, _ = self.ask(epoch("2026-11-07T21:00:00Z") - 60 + 0.9)
        self.assertDone(answer, code)


class StoredListLimitsTests(Checks):
    """Section 8: at most 64 devices and 64 countersignatures, and every signature field a signature."""

    def document(self, devices=1, countersignatures=0, **changes):
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        first = body["devices"][0]
        extra = []
        for index in range(devices - 1):
            copy = dict(first)
            copy["deviceId"] = "d-%024x" % (index + 1)
            extra.append(copy)
        body["devices"] = [first] + extra
        document = resign(body)
        document["countersignatures"] = [
            {"deviceId": "d-%024x" % (index + 1), "signature": "A" * 86}
            for index in range(countersignatures)
        ]
        document.update(changes)
        return document

    def ask(self, document):
        env = Env(self, stored=document)
        return module_request(env, COMMAND, stdin_of(approval()), NOW)

    def test_sixty_four_devices_and_sixty_four_countersignatures_are_the_most(self):
        answer, code, _ = self.ask(self.document(devices=64, countersignatures=64))
        self.assertDone(answer, code)
        answer, code, _ = self.ask(self.document(devices=65))
        self.assertRefused(answer, code, "state_unsafe", verified=False)
        answer, code, _ = self.ask(self.document(countersignatures=65))
        self.assertRefused(answer, code, "state_unsafe", verified=False)

    def test_every_signature_field_must_look_like_one(self):
        for name, document in (
            ("owner signature too long", self.document(ownerSignature="A" * 101)),
            ("owner signature empty", self.document(ownerSignature="")),
            ("owner signature with padding", self.document(ownerSignature="AAAA=")),
            (
                "countersignature device id",
                self.document(countersignatures=1)
                | {"countersignatures": [{"deviceId": "d-1", "signature": "A" * 86}]},
            ),
            (
                "countersignature device id in capitals",
                self.document()
                | {"countersignatures": [{"deviceId": "D-" + "A" * 24, "signature": "A" * 86}]},
            ),
            (
                "countersignature device id not text",
                self.document() | {"countersignatures": [{"deviceId": 5, "signature": "A" * 86}]},
            ),
            (
                "countersignature too long",
                self.document()
                | {"countersignatures": [{"deviceId": "d-" + "a" * 24, "signature": "A" * 101}]},
            ),
            (
                "countersignature empty",
                self.document()
                | {"countersignatures": [{"deviceId": "d-" + "a" * 24, "signature": ""}]},
            ),
            (
                "countersignature with a space",
                self.document()
                | {"countersignatures": [{"deviceId": "d-" + "a" * 24, "signature": "AA AA"}]},
            ),
            (
                "countersignature not text",
                self.document()
                | {"countersignatures": [{"deviceId": "d-" + "a" * 24, "signature": 7}]},
            ),
        ):
            with self.subTest(case=name):
                answer, code, _ = self.ask(document)
                self.assertRefused(answer, code, "state_unsafe", verified=False)

    def body_document(self, **changes):
        body = json.loads(json.dumps(FIXTURE["lists"]["v1"]["body"]))
        body.update(changes)
        return resign(body)

    def test_a_reopen_tag_and_a_previous_hash_are_a_hash_or_null(self):
        for field in ("reopenTag", "previousSHA256"):
            for bad in ("xyz", "A" * 64, "a" * 63, "a" * 65, 5, "", ["a" * 64], True):
                with self.subTest(field=field, value=bad):
                    answer, code, _ = self.ask(self.body_document(**{field: bad}))
                    self.assertRefused(answer, code, "state_unsafe", verified=False)
                    with self.assertRaises(ValueError):
                        ma.parse_stored_list(
                            json.dumps(self.body_document(**{field: bad})).encode()
                        )
            # A hash is read (what it means is for the chain slice), and so is null.
            answer, code, _ = self.ask(self.body_document(**{field: "a" * 64}))
            self.assertDone(answer, code)
            answer, code, _ = self.ask(self.body_document(**{field: None}))
            self.assertDone(answer, code)

    def test_the_parser_applies_the_same_rules(self):
        good = json.dumps(self.document(devices=64, countersignatures=64)).encode()
        self.assertEqual(ma.parse_stored_list(good).version, 1)
        for document in (self.document(devices=65), self.document(countersignatures=65)):
            with self.assertRaises(ValueError):
                ma.parse_stored_list(json.dumps(document).encode())


class StateFileLinkTests(Checks):
    """A state file with a second hard link is `state_unsafe`, so that another name cannot be used to
    write what this one reads, and a file is never written through a link to something else."""

    def victim(self, env, content=b"precious"):
        path = env.root / "victim"
        write_file(path, content, 0o600)
        return path

    def test_a_hard_linked_ledger_rate_file_lock_or_trust_file_is_state_unsafe(self):
        for name in (
            "muster-nonces.jsonl",
            "muster-rate.json",
            "muster-rate.lock",
            "muster-trust.json",
        ):
            with self.subTest(file=name):
                env = Env(self)
                path = env.state / name
                if path.exists():
                    content = path.read_bytes()
                    path.unlink()
                else:
                    content = b""
                victim = self.victim(env, content or b"precious")
                os.link(victim, path)
                before = victim.read_bytes()
                started = __import__("time").monotonic()
                answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
                self.assertLess(__import__("time").monotonic() - started, 5)
                self.assertRefused(answer, code, "state_unsafe")
                self.assertEqual(backend.runs, [])
                self.assertEqual(
                    victim.read_bytes(), before, "nothing was written through the link"
                )

    def test_a_hard_linked_helper_key_is_the_unsigned_answer(self):
        env = Env(self)
        path = env.state / "muster-helper.key"
        os.link(path, env.root / "second-name-for-the-key")
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertEqual(
            (code, answer["body"]["errorCode"], answer["signature"]),
            (2, "helper_key_missing", None),
        )
        self.assertEqual(backend.events, [])

    def test_the_other_kinds_of_file_in_the_ledgers_place(self):
        # The reviewer's list: a symbolic link (live or dangling), a FIFO, a directory, a loose mode.
        victim_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(victim_dir), True)
        victim = victim_dir / "victim"
        write_file(victim, b"precious", 0o600)
        for kind in ("symlink", "dangling", "fifo", "dir", "mode644"):
            with self.subTest(kind=kind):
                env = Env(self)
                path = env.state / "muster-nonces.jsonl"
                if kind == "symlink":
                    os.symlink(victim, path)
                elif kind == "dangling":
                    os.symlink(victim_dir / "nope", path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                elif kind == "dir":
                    os.mkdir(path, 0o700)
                else:
                    write_file(path, b"", 0o644)
                started = __import__("time").monotonic()
                answer, code, backend = self.run_within(
                    10,
                    lambda: module_request(env, COMMAND, stdin_of(approval()), NOW),
                    unblock=lambda: self.release_fifos(env.state),
                )
                self.assertLess(
                    __import__("time").monotonic() - started, 5, "never waits on a FIFO"
                )
                self.assertRefused(answer, code, "state_unsafe")
                self.assertEqual(backend.runs, [])
                self.assertEqual(victim.read_bytes(), b"precious")

    def test_a_rate_file_that_is_damaged_stays_state_unsafe_until_repaired(self):
        env = Env(self)
        write_file(
            env.state / "muster-rate.json",
            b'{"contract":"muster-rate/1","events":[["yes","k",1]',
            0o600,
        )
        for step in range(2):
            answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), later(10 * step))
            self.assertRefused(answer, code, "state_unsafe")


class RedeployAuditTests(Checks):
    """A redeploy is named in the audit by its application's name or `coolify resource`, never by
    its uuid, whichever of the two writers wrote the row."""

    def world(self):
        env = Env(self)
        write_allowlist(env, coolify={"redeploy": True})
        write_status_report(env, [REDEPLOY_ROW])
        return env

    COMMAND = f"server redeploy {REDEPLOY_UUID} --yes"

    def test_a_plan_mismatch_row_written_by_the_module_does_not_name_the_uuid(self):
        env = self.world()
        document = approval(action="redeploy", target=REDEPLOY_UUID, planSHA256="0" * 64)
        answer, code, runner, rows = helper_request(env, self.COMMAND, stdin_of(document), NOW)
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "coolify resource")
        self.assertEqual(rows[0]["errorCode"], "plan_mismatch")
        self.assertNotIn(REDEPLOY_UUID, json.dumps(rows))
        self.assertEqual(runner.calls, [])

    def test_a_plan_refusal_after_the_nonce_row_does_not_name_the_uuid(self):
        env = self.world()
        (env.state / "server-status.json").unlink()
        document = approval(action="redeploy", target=REDEPLOY_UUID, planSHA256="0" * 64)
        answer, code, runner, rows = helper_request(env, self.COMMAND, stdin_of(document), NOW)
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["detail"], "plan_refused: report_stale")
        self.assertEqual(
            [(r["target"], r["result"]) for r in rows], [("coolify resource", "failed")]
        )
        self.assertNotIn(REDEPLOY_UUID, json.dumps(rows))

    def test_a_redeploy_that_runs_is_named_by_its_application(self):
        env = self.world()
        config = foc.load_config(env.allowlist)
        ctx = foc.Context(
            platform="linux", home=env.home, config_path=env.allowlist, now=lambda: NOW_EPOCH
        )
        commands, _, audit_target = foc.plan_request(
            ctx, config, "server", "redeploy", REDEPLOY_UUID
        )
        plan = foc.plan_object("server", "redeploy", REDEPLOY_UUID, commands, audit_target)
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = approval(action="redeploy", target=REDEPLOY_UUID, planSHA256=digest)
        with mock.patch.object(foc, "execute", lambda *args: "Coolify accepted the redeploy"):
            answer, code, runner, rows = helper_request(env, self.COMMAND, stdin_of(document), NOW)
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["body"]["outcome"], "submitted")
        self.assertEqual([(r["target"], r["result"]) for r in rows], [("My App", "submitted")])
        self.assertNotIn(REDEPLOY_UUID, json.dumps(rows))

    def test_the_one_mapping(self):
        mapping = foc.redeploy_audit_target
        self.assertEqual(mapping("server", "redeploy", REDEPLOY_UUID, None), "coolify resource")
        self.assertEqual(
            mapping("server", "redeploy", REDEPLOY_UUID, REDEPLOY_UUID), "coolify resource"
        )
        self.assertEqual(mapping("server", "redeploy", REDEPLOY_UUID, "My App"), "My App")
        self.assertEqual(mapping("server", "restart", "x.timer", None), "x.timer")
        self.assertEqual(mapping("server", "restart", "x.timer", "x.timer"), "x.timer")
        self.assertEqual(
            mapping("muster", "ledger-repair", "muster-nonces.jsonl", None), "muster-nonces.jsonl"
        )

    def test_the_backends_audit_goes_through_it(self):
        env = Env(self)
        ctx = foc.Context(platform="linux", home=env.home, config_path=env.allowlist)
        backend = foc._MusterBackend(ctx, ma)
        backend.audit(
            "server",
            "redeploy",
            REDEPLOY_UUID,
            "failed",
            {"route": "from-muster", "via": "muster-ios"},
        )
        backend.audit(
            "muster",
            "ledger-repair",
            "muster-nonces.jsonl",
            "repaired",
            {"route": "from-muster", "via": "muster-ios"},
        )
        rows = env.audit_rows()
        self.assertEqual(
            [(r["verb"], r["target"], r["result"]) for r in rows],
            [
                ("server redeploy", "coolify resource", "failed"),
                ("muster ledger-repair", "muster-nonces.jsonl", "repaired"),
            ],
        )
        self.assertNotIn("errorCode", rows[1])


class BackendMappingTests(Checks):
    """What `_MusterBackend` makes of the helper's shared functions' answers."""

    def backend(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        return foc._MusterBackend(ctx, ma), env

    def run_backend(self, answer, reason=None):
        backend, _ = self.backend()

        def fake(ctx, scope, action, target, mode, **kwargs):
            if reason is not None:
                kwargs["reason_out"]["reason"] = reason
            return dict(answer), 0

        with mock.patch.object(foc, "handle", fake):
            return backend.run("restart", "fleet-server-status.timer", dict(BASE_APPROVAL))

    def test_each_result_word_is_a_route_outcome(self):
        for result, outcome in (
            ("done", "done"),
            ("submitted", "submitted"),
            ("unknown", "unknown"),
            ("failed", "failed"),
            ("weird", "failed"),
            ("", "failed"),
        ):
            with self.subTest(result=result):
                run = self.run_backend(
                    {"result": result, "detail": "d", "auditID": "0123456789ab", "output": "o"}
                )
                self.assertEqual(
                    (run.outcome, run.detail, run.output, run.audit_id),
                    (outcome, "d", "o", "0123456789ab"),
                )

    def test_a_plan_mismatch_stays_a_refusal(self):
        run = self.run_backend(
            {"result": "refused", "detail": "no", "errorCode": "plan_mismatch", "auditID": "1" * 12}
        )
        self.assertEqual(
            (run.outcome, run.error_code, run.audit_id), ("refused", "plan_mismatch", "1" * 12)
        )

    def test_any_other_refusal_names_a_reason_or_internal_error_and_never_a_code(self):
        for reason, detail in (
            ("update_busy", "plan_refused: update_busy"),
            ("authorization_unavailable", "plan_refused: authorization_unavailable"),
            ("allowlist_miss", "plan_refused: allowlist_miss"),
            ("report_stale", "plan_refused: report_stale"),
            ("revision_stale", "plan_refused: revision_stale"),
            (None, "internal_error"),
            ("something else", "internal_error"),
            ("sudo_missing", "internal_error"),
        ):
            with self.subTest(reason=reason):
                run = self.run_backend(
                    {"result": "refused", "detail": "words that are not sent"}, reason
                )
                self.assertEqual(
                    (run.outcome, run.detail, run.error_code), ("refused", detail, None)
                )

    def test_the_planner_reasons(self):
        env = Env(self)
        write_allowlist(env, coolify={"redeploy": True}, resourcePolicy=False)
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        backend = foc._MusterBackend(ctx, ma)
        for verb, target, code in (
            ("restart", "not-listed.service", "allowlist_miss"),
            ("stop", "botfleet-server.service", "allowlist_miss"),
            ("logs", "not-listed.service", "allowlist_miss"),
            ("update", "nonesuch", "allowlist_miss"),
            ("resource-policy", "cpu-weight.100." + "a" * 64, "allowlist_miss"),
            ("redeploy", REDEPLOY_UUID, "report_stale"),
        ):
            with self.subTest(verb=verb, target=target):
                with self.assertRaises(ma.BackendRefused) as caught:
                    backend.plan_bytes(verb, target)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ma.BackendRefused) as caught:
            backend.read("resource-status", "coding")
        self.assertEqual(caught.exception.code, "allowlist_miss")

    def test_the_reason_never_changes_what_the_other_callers_of_handle_see(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        answer, code = foc.handle(ctx, "server", "restart", "not-listed.service", "plan")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertNotIn("reason", answer)
        self.assertEqual(set(answer), {"ok", "verb", "target", "commands", "result", "detail"})


class PlannerBytesTests(Checks):
    """The helper's planner is the one planner (`plan_request`), and the route signs what it builds.

    Section 4 of the spec is written from it, so these tests pin the planner's own bytes: if the
    planner changes, they fail, and so must the spec."""

    JOURNALCTL = "/usr/bin/journalctl"
    PROPERTIES = (
        "LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=\nType=oneshot\n"
        "InvocationID=\nResult=success\nExecMainStatus=0\nExecMainCode=0\n"
        "ExecMainStartTimestampMonotonic=0\nExecMainExitTimestampMonotonic=0\n"
    )
    SUDO_UPDATE = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "--no-block"]

    def cases(self):
        """(name, verb, target, uid, the planner's commands, its auditTarget)."""
        unit = "fleet-server-status.timer"
        system = "botfleet-server.service"
        update_unit = "fleet-cli-git-update.service"
        return (
            (
                "restart, user scope",
                "restart",
                unit,
                1000,
                [[SYSTEMCTL, "--user", "restart", unit]],
                unit,
            ),
            (
                "restart, system scope",
                "restart",
                system,
                1000,
                [[SYSTEMCTL, "restart", system]],
                system,
            ),
            (
                # A timer reads the journal of the service it starts (its `logsUnit`), and the
                # plan's target and audit target stay the timer.
                "logs, user scope, a timer",
                "logs",
                unit,
                1000,
                [
                    [
                        self.JOURNALCTL,
                        "--user",
                        "-u",
                        "fleet-server-status.service",
                        "-n",
                        "200",
                        "--no-pager",
                    ]
                ],
                unit,
            ),
            (
                "logs, system scope",
                "logs",
                system,
                1000,
                [[self.JOURNALCTL, "-u", system, "-n", "200", "--no-pager"]],
                system,
            ),
            (
                "update, system scope, the account is not root",
                "update",
                "fleet-cli",
                1000,
                [self.SUDO_UPDATE + [update_unit]],
                "fleet-cli",
            ),
            (
                "update, user scope",
                "update",
                "user-update",
                1000,
                [[SYSTEMCTL, "--user", "start", "--no-block", "user-update.service"]],
                "user-update",
            ),
            (
                "redeploy",
                "redeploy",
                REDEPLOY_UUID,
                UID,
                [["coolify", "GET", f"/api/v1/deploy?uuid={REDEPLOY_UUID}"]],
                "My App",
            ),
        )

    def plan(self, verb, target, uid):
        env = Env(self)
        write_allowlist(
            env,
            units=[
                {"unit": "fleet-server-status.timer", "scope": "user", "actions": ["restart"]},
                {"unit": "botfleet-server.service", "scope": "system", "actions": ["restart"]},
            ],
            updates=[
                {"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"},
                {"name": "user-update", "unit": "user-update.service", "scope": "user"},
            ],
            coolify={"redeploy": True},
        )
        write_status_report(env, [REDEPLOY_ROW])
        properties = self.PROPERTIES.encode()

        def runner(argv, timeout=None, env=None, want_stdout=False, max_bytes=None, **kw):
            return (0, properties) if "show" in argv else (0, b"")

        ctx = foc.Context(
            platform="linux",
            uid=uid,
            home=env.home,
            runner=runner,
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        with (
            mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL),
            mock.patch.object(foc, "_journalctl", lambda: self.JOURNALCTL),
            mock.patch.object(foc.os.path, "isfile", lambda path: True),
            mock.patch.object(foc.os, "access", lambda path, mode: True),
        ):
            config = foc.load_config(env.allowlist)
            return foc.plan_request(ctx, config, "server", verb, target)

    def test_the_planner_builds_exactly_these_commands(self):
        for name, verb, target, uid, commands, audit_target in self.cases():
            with self.subTest(case=name):
                built, _note, audited = self.plan(verb, target, uid)
                self.assertEqual(built, commands)
                self.assertEqual(audited, audit_target)

    def test_the_plan_bytes_and_hashes_of_each_case_are_fixed(self):
        # The canonical bytes the route signs, written out, with their SHA-256: a change of either
        # the planner or the canonical form shows here first.
        expected = {
            "restart, user scope": (
                b'{"action":"restart","auditTarget":"fleet-server-status.timer","commands":'
                b'[["/usr/bin/systemctl","--user","restart","fleet-server-status.timer"]],'
                b'"contract":"fleet-ops-plan/1","scope":"server","target":"fleet-server-status.timer"}',
                "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
            ),
            "restart, system scope": (
                b'{"action":"restart","auditTarget":"botfleet-server.service","commands":'
                b'[["/usr/bin/systemctl","restart","botfleet-server.service"]],'
                b'"contract":"fleet-ops-plan/1","scope":"server","target":"botfleet-server.service"}',
                "227b77614ece73e51feb0aaee86d66e2c0ea5d2238646295efb09ea43cfc65bd",
            ),
            "logs, system scope": (
                b'{"action":"logs","auditTarget":"botfleet-server.service","commands":'
                b'[["/usr/bin/journalctl","-u","botfleet-server.service","-n","200","--no-pager"]],'
                b'"contract":"fleet-ops-plan/1","scope":"server","target":"botfleet-server.service"}',
                "68bd5dc7ee8ae9722e33fc518f327d7772ad1c90351f8306e4d4987bbf38a4a5",
            ),
            "redeploy": (
                b'{"action":"redeploy","auditTarget":"My App","commands":'
                b'[["coolify","GET","/api/v1/deploy?uuid=abc123def456"]],'
                b'"contract":"fleet-ops-plan/1","scope":"server","target":"abc123def456"}',
                "ce5e692a2960a5a0842d1f90795abcca59de05c54e4982fe1a35ab8b92cb1260",
            ),
        }
        for name, verb, target, uid, commands, audit_target in self.cases():
            if name not in expected:
                continue
            with self.subTest(case=name):
                bytes_, digest = expected[name]
                built = ma.canonical_bytes(
                    foc.plan_object("server", verb, target, commands, audit_target)
                )
                self.assertEqual(built, bytes_)
                self.assertEqual(hashlib.sha256(built).hexdigest(), digest)

    def test_a_non_ascii_application_name_is_written_with_a_question_mark_per_code_point(self):
        # The cap of 120 is applied first, and then each code point outside 0x20 to 0x7E becomes
        # one `?`: nothing collapsed, nothing trimmed, never longer than 120.
        for name, wanted in (
            ("Café App", "Caf? App"),
            ("日本語", "???"),
            ("tab\there", "tab?here"),
            ("  edges  ", "  edges  "),
            ("é" * 130, "?" * 120),
            ("a" * 119 + "éé", "a" * 119 + "?"),
            ("emoji \U0001f600!", "emoji ?!"),
        ):
            with self.subTest(name=name[:12]):
                env = Env(self)
                write_allowlist(env, coolify={"redeploy": True})
                write_status_report(env, [dict(REDEPLOY_ROW, name=name)])
                ctx = foc.Context(
                    platform="linux",
                    home=env.home,
                    runner=FakeRunner(),
                    config_path=env.allowlist,
                    now=lambda: NOW_EPOCH,
                )
                config = foc.load_config(env.allowlist)
                commands, _, audited = foc.plan_request(
                    ctx, config, "server", "redeploy", REDEPLOY_UUID
                )
                self.assertEqual(audited, name[:120], "the non-muster route's value is untouched")
                plan = foc.plan_object("server", "redeploy", REDEPLOY_UUID, commands, audited)
                self.assertEqual(plan["auditTarget"], wanted)
                self.assertLessEqual(len(plan["auditTarget"]), 120)
                ma.canonical_bytes(plan)  # always writable

    def test_a_redeploy_of_a_non_ascii_name_can_be_planned_and_approved_through_the_helper(self):
        env = Env(self)
        write_allowlist(env, coolify={"redeploy": True})
        write_status_report(env, [dict(REDEPLOY_ROW, name="Café App")])
        answer, code, _, _ = helper_request(
            env, f"server redeploy {REDEPLOY_UUID} --plan", job_stdin(), NOW
        )
        self.assertEqual(code, 0, answer)
        plan = json.loads(answer["body"]["plan"])
        self.assertEqual(plan["auditTarget"], "Caf? App")
        self.assertEqual(
            answer["body"]["planSHA256"],
            hashlib.sha256(answer["body"]["plan"].encode()).hexdigest(),
        )
        # The approved run goes through, and its audit row keeps the real name.
        digest = answer["body"]["planSHA256"]
        document = approval(action="redeploy", target=REDEPLOY_UUID, planSHA256=digest)
        env = Env(self)
        write_allowlist(env, coolify={"redeploy": True})
        write_status_report(env, [dict(REDEPLOY_ROW, name="Café App")])
        with mock.patch.object(foc, "execute", lambda *args: "Coolify accepted the redeploy"):
            answer, code, _, rows = helper_request(
                env, f"server redeploy {REDEPLOY_UUID} --yes", stdin_of(document), later(60)
            )
        self.assertEqual(code, 0, answer)
        self.assertEqual([(r["target"], r["result"]) for r in rows], [("Café App", "submitted")])

    def test_systemctl_and_journalctl_get_user_only_for_a_user_scope_unit(self):
        user = self.plan("restart", "fleet-server-status.timer", 1000)[0][0]
        system = self.plan("restart", "botfleet-server.service", 1000)[0][0]
        self.assertIn("--user", user)
        self.assertNotIn("--user", system)
        user = self.plan("logs", "fleet-server-status.timer", 1000)[0][0]
        system = self.plan("logs", "botfleet-server.service", 1000)[0][0]
        self.assertIn("--user", user)
        self.assertNotIn("--user", system)

    def test_argv0_is_the_hosts_first_existing_candidate_and_the_redeploy_has_a_pseudo_argv0(self):
        self.assertEqual(foc.SYSTEMCTL_CANDIDATES, ("/usr/bin/systemctl", "/bin/systemctl"))
        self.assertEqual(foc.JOURNALCTL_CANDIDATES, ("/usr/bin/journalctl", "/bin/journalctl"))
        for find, paths in (
            (foc._systemctl, foc.SYSTEMCTL_CANDIDATES),
            (foc._journalctl, foc.JOURNALCTL_CANDIDATES),
        ):
            only_second = paths[1]
            with mock.patch.object(foc.os.path, "isfile", lambda path: path == only_second):
                self.assertEqual(find(), paths[1], "the first candidate that exists")
            with mock.patch.object(foc.os.path, "isfile", lambda path: True):
                self.assertEqual(find(), paths[0])
            with mock.patch.object(foc.os.path, "isfile", lambda path: False):
                self.assertEqual(
                    find(), paths[0], "none exists: the first, as the planner always has"
                )
        built, _, _ = self.plan("redeploy", REDEPLOY_UUID, UID)
        self.assertEqual(built[0][0], "coolify")

    def test_a_plan_by_root_has_no_sudo(self):
        built, _, audited = self.plan("update", "fleet-cli", 0)
        self.assertEqual(
            built, [[SYSTEMCTL, "start", "--no-block", "fleet-cli-git-update.service"]]
        )
        self.assertEqual(audited, "fleet-cli")

    def test_the_vectors_plan_is_the_planners_when_the_host_resolves_usr_bin_systemctl(self):
        # Every vector's plan is the restart of the user-scope timer: the planner builds it byte for
        # byte, so no vector is bent and none differs.  This is the check that needs no patching: on a
        # host with /usr/bin/systemctl the real `_systemctl()` is that path.
        if not os.path.isfile("/usr/bin/systemctl"):
            self.skipTest("this host has no /usr/bin/systemctl")
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        config = foc.load_config(env.allowlist)
        commands, _, audited = foc.plan_request(
            ctx, config, "server", "restart", "fleet-server-status.timer"
        )
        built = ma.canonical_bytes(
            foc.plan_object("server", "restart", "fleet-server-status.timer", commands, audited)
        )
        self.assertEqual(built, ILLUSTRATIVE_PLAN)
        for vector in FIXTURE["approvals"]:
            self.assertIn(
                vector["helperPlanSHA256"],
                {BASE_APPROVAL["planSHA256"], hashlib.sha256(OTHER_PLAN).hexdigest()},
            )


RACE_DRIVER = r"""
import importlib.util, json, os, sys, time
from pathlib import Path
spec = json.load(open(sys.argv[1]))
loaded = importlib.util.spec_from_file_location("foc", spec["helper"])
foc = importlib.util.module_from_spec(loaded)
loaded.loader.exec_module(foc)
foc._systemctl = lambda: "/usr/bin/systemctl"
real_load = foc._load_muster_modules
def patched(ctx):
    modules = real_load(ctx)
    if modules:
        if not spec.get("real_rate"):
            modules[0].RATE_LIMITS["yes"] = (0, 100000)
            modules[0].RATE_LIMITS["plan"] = (0, 100000)
        if spec.get("stdin_timeout"):
            modules[0].read_stdin_limited.__defaults__ = (spec["stdin_timeout"],)
    return modules
foc._load_muster_modules = patched
if spec.get("die_at_plan"):
    def dying_plan(*args, **kwargs):
        os._exit(9)
    foc.plan_request = dying_plan
def runner(argv, timeout=None, env=None, want_stdout=False, max_bytes=None, **kw):
    with open(spec["calls"], "a") as log:
        log.write(json.dumps(argv) + "\n")
    time.sleep(spec.get("sleep", 0))
    return 0, b""
config = {k: v for k, v in spec["config"].items() if k != "now"}
config["clock_state"] = lambda: (0, 0, 175000)
ctx = foc.Context(platform="linux", home=Path(spec["home"]), runner=runner,
                  config_path=Path(spec["allowlist"]), now=time.time, environ={"HOME": spec["home"]})
sys.exit(foc.main(["--from-muster"], ctx=ctx, muster={"config": config}))
"""


class RaceAndCrashTests(Checks):
    """Real processes, the real clock: one approval is never run twice, and a crash after the nonce is
    spent leaves it spent and nothing run."""

    def driver(self, env):
        """The driver file: written once for this test, in its own temporary folder, by an atomic
        rename, so that no process that is already running it ever sees it half written."""
        path = env.root / "race_driver.py"
        if not path.exists():
            partial = env.root / f"race_driver.py.{os.getpid()}.partial"
            partial.write_text(RACE_DRIVER)
            os.replace(partial, path)
        return path

    def launch(self, env, stdin, command, keep_stdin_open=False, **extra):
        spec_path = env.root / f"spec-{__import__('time').monotonic_ns()}.json"
        driver = self.driver(env)
        calls = env.root / "calls.jsonl"
        spec = {
            "helper": str(checkout() / "fleet-ops-control.py"),
            "calls": str(calls),
            "home": str(env.home),
            "allowlist": str(env.allowlist),
            "config": {k: v for k, v in env.config(NOW).items() if k != "clock_state"},
        }
        spec.update(extra)
        spec_path.write_text(json.dumps(spec))
        process = subprocess.Popen(
            [sys.executable, "-I", str(driver), str(spec_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"SSH_ORIGINAL_COMMAND": command},
        )
        process.stdin.write(stdin)
        if keep_stdin_open:
            process.stdin.flush()
        else:
            process.stdin.close()
        return process, calls

    def live(self, **overrides):
        now = int(__import__("time").time())
        return approval(
            issuedAt=ma.format_time(now - 5), expiresAt=ma.format_time(now + 100), **overrides
        )

    def test_the_same_approval_in_ten_processes_at_once_runs_once(self):
        env = Env(self)
        stdin = stdin_of(self.live())
        processes = [self.launch(env, stdin, COMMAND, sleep=0.7)[0] for _ in range(10)]
        outcomes = []
        for process in processes:
            out, err = process.communicate(timeout=120)
            self.assertEqual(err, b"")
            body = json.loads(out)["body"]
            outcomes.append(body.get("errorCode") or body["outcome"])
        calls = env.root / "calls.jsonl"
        self.assertEqual(len(calls.read_text().splitlines()), 1, outcomes)
        self.assertEqual(len(env.ledger.splitlines()), 1)
        self.assertEqual(outcomes.count("done"), 1)
        self.assertLessEqual(set(outcomes), {"done", "action_busy", "replayed_nonce"}, outcomes)
        self.assertEqual(len(env.audit_rows()), 1, "one row for one nonce, however many asked")

    def test_two_approvals_for_one_slot_at_once_run_one_and_spend_one_nonce(self):
        # The real `yes` limit (one request per `verb target` every two seconds): of two different
        # approvals for the same change, one runs, and the other is refused before it can spend its
        # nonce, whether it met the lock (`action_busy`) or the spacing (`rate_limited`).
        env = Env(self)
        stdins = (stdin_of(self.live()), stdin_of(self.live(approvalNonce="b" * 32)))
        processes = [
            self.launch(env, stdin, COMMAND, sleep=1.8, real_rate=True)[0] for stdin in stdins
        ]
        outcomes = []
        for process in processes:
            out, err = process.communicate(timeout=120)
            self.assertEqual(err, b"")
            body = json.loads(out)["body"]
            outcomes.append(body.get("errorCode") or body["outcome"])
        self.assertEqual(outcomes.count("done"), 1, outcomes)
        refused = [word for word in outcomes if word != "done"]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertIn(refused[0], {"action_busy", "rate_limited"})
        calls = env.root / "calls.jsonl"
        self.assertEqual(len(calls.read_text().splitlines()), 1, outcomes)
        self.assertEqual(len(env.ledger.splitlines()), 1, "the refused one spent nothing")
        self.assertEqual(len(env.audit_rows()), 1)

    def test_the_driver_is_written_once_and_never_rewritten(self):
        env = Env(self)
        first = self.driver(env)
        stamp = first.stat().st_ino
        first.write_text(first.read_text() + "\n# marker\n")
        self.assertEqual(self.driver(env), first)
        self.assertIn("# marker", first.read_text(), "a second call must not rewrite the file")
        self.assertEqual(first.stat().st_ino, stamp)
        self.assertEqual(
            sorted(path.name for path in env.root.glob("race_driver.py*")), ["race_driver.py"]
        )

    def test_a_crash_after_the_nonce_is_spent_leaves_it_spent_and_nothing_run(self):
        env = Env(self)
        stdin = stdin_of(self.live())
        process, calls = self.launch(env, stdin, COMMAND, die_at_plan=True)
        out, err = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 9)
        self.assertEqual(out, b"")
        self.assertFalse(calls.exists(), "something ran")
        self.assertEqual(len(env.ledger.splitlines()), 1, "the nonce is durably spent")
        self.assertEqual(env.audit_rows(), [], "a hard crash cannot write a row")
        process, calls = self.launch(env, stdin, COMMAND)
        out, err = process.communicate(timeout=60)
        self.assertEqual(json.loads(out)["body"]["errorCode"], "replayed_nonce")
        self.assertFalse(calls.exists())

    def test_a_standard_input_that_never_ends_is_given_up_on_by_the_helper(self):
        # The child's standard input stays OPEN and silent after a partial document: only the
        # deadline can end the read, so the answer must come after about that long, and never wait on.
        import time

        env = Env(self)
        process, _ = self.launch(
            env, b'{"protocol":', COMMAND, keep_stdin_open=True, stdin_timeout=1.0
        )
        for stream in (process.stdout, process.stderr):
            self.addCleanup(stream.close)
        self.addCleanup(process.wait)
        self.addCleanup(lambda: process.stdin.close() if not process.stdin.closed else None)
        started = time.monotonic()
        out = self.run_within(20, process.stdout.read, unblock=process.stdin.close)
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.9, "the helper answered before its deadline")
        self.assertLess(waited, 15)
        self.assertEqual(json.loads(out)["body"]["errorCode"], "bad_input")
        self.assertEqual(process.stderr.read(), b"")
        self.assertEqual(ma.STDIN_TIMEOUT, 10.0)


class PlanSwapTests(Checks):
    """The plan is built twice (Y9, then inside the shared function); a plan that changes between the
    two is refused, and nothing runs."""

    def swapped(self, count_from, replacement):
        original = foc.plan_request
        state = {"calls": 0}

        def planner(ctx, config, scope, action, target):
            state["calls"] += 1
            if state["calls"] >= count_from:
                return replacement(ctx, config, scope, action, target)
            return original(ctx, config, scope, action, target)

        return planner, state

    def stop_plan(self, ctx, config, scope, action, target):
        return [[SYSTEMCTL, "--user", "stop", target]], "user unit", target

    def test_a_plan_that_differs_the_second_time_is_refused_and_nothing_runs(self):
        env = Env(self)
        planner, state = self.swapped(2, self.stop_plan)
        with mock.patch.object(foc, "plan_request", planner):
            answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["errorCode"], "plan_mismatch")
        self.assertEqual(len(env.ledger.splitlines()), 1)

    def test_a_plan_that_differs_the_first_time_never_reaches_the_shared_function(self):
        env = Env(self)
        planner, state = self.swapped(1, self.stop_plan)
        with mock.patch.object(foc, "plan_request", planner):
            answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "plan_mismatch", verified=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(state["calls"], 1, "the run was never reached")
        self.assertEqual(len(rows), 1)

    def test_a_fault_in_the_plan_after_the_nonce_is_a_failed_result_with_its_one_row(self):
        def boom(ctx, config, scope, action, target):
            raise KeyError("boom")

        for count_from in (1, 2):
            with self.subTest(fails_from_call=count_from):
                env = Env(self)
                planner, state = self.swapped(count_from, boom)
                with mock.patch.object(foc, "plan_request", planner):
                    answer, code, runner, rows = helper_request(
                        env, COMMAND, stdin_of(approval()), NOW
                    )
                self.assertEqual((code, answer["kind"]), (1, "result"), answer)
                self.assertEqual(answer["body"]["outcome"], "failed")
                self.assertSigned(answer)
                self.assertEqual(runner.calls, [])
                self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual(rows[0]["result"], "failed")
                self.assertNotIn("errorCode", rows[0])
                self.assertNotIn("boom", json.dumps(answer))


class SignatureShapeTests(Checks):
    """The reviewer's signature and message variants: only the canonical DER over the exact text
    verifies, and the two forms of one signature are one nonce."""

    def parts(self):
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        return decode_dss_signature(sign(DEVICE_A, text_of(BASE_APPROVAL)))

    @staticmethod
    def der(r, s):
        def integer(value):
            raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
            if raw[0] & 0x80:
                raw = b"\x00" + raw
            return b"\x02" + bytes([len(raw)]) + raw

        body = integer(r) + integer(s)
        return b"\x30" + bytes([len(body)]) + body

    def test_the_high_s_twin_verifies_and_is_the_same_spent_nonce(self):
        r, s = self.parts()
        low, high = self.der(r, s), self.der(r, N - s)
        env = Env(self)
        document = dict(BASE_APPROVAL, signature=b64u(low))
        answer, code, _ = module_request(env, COMMAND, stdin_of_raw(document), NOW)
        self.assertDone(answer, code)
        document = dict(BASE_APPROVAL, signature=b64u(high))
        answer, code, backend = module_request(env, COMMAND, stdin_of_raw(document), later(10))
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertEqual(env.ledger.count(b"\n"), 1)

    def test_message_variants_are_bad_signature(self):
        text = text_of(BASE_APPROVAL)
        for name, message in {
            "a trailing line feed": text + b"\n",
            "CRLF line ends": text.replace(b"\n", b"\r\n"),
            "no head": text.split(b"\n", 1)[1],
            "the countersignature prefix": b"muster-trust-countersign/1\n"
            + text.split(b"\n", 1)[1],
            "the trust list prefix": b"muster-trust-list/1\n" + text.split(b"\n", 1)[1],
        }.items():
            with self.subTest(case=name):
                document = dict(BASE_APPROVAL, signature=b64u(sign(DEVICE_A, message)))
                answer, code, _ = module_request(Env(self), COMMAND, stdin_of_raw(document), NOW)
                self.assertRefused(answer, code, "bad_signature", verified=False)

    def test_no_other_key_can_sign_an_approval(self):
        for label in (
            "owner",
            "server helper",
            "device A request",
            "device B approval",
            "rogue owner (not pinned)",
            "rogue helper (not enrolled)",
        ):
            with self.subTest(key=label):
                answer, code, _ = module_request(
                    Env(self), COMMAND, stdin_of(approval(signer=KEYS[label])), NOW
                )
                self.assertRefused(answer, code, "bad_signature", verified=False)


def stdin_of_raw(document):
    """Standard input for an approval document that is already complete (signature included)."""
    return compact({"protocol": "muster-helper/1", "approval": document})


class HostileDocumentTests(Checks):
    """The reviewer's list of hostile documents and command lines: each is refused, never a crash."""

    def test_hostile_documents_are_bad_input_and_spend_nothing(self):
        valid = dict(BASE_APPROVAL, signature="A" * 10)
        good = stdin_of(approval())
        cases = {
            "a duplicate key at the top": b'{"protocol":"muster-helper/1","protocol":"muster-helper/1","approval":{}}',
            "a duplicate key inside": good.replace(
                b'"host":"server"', b'"host":"server","host":"server"'
            ),
            "an unknown key": json.dumps(
                {"protocol": "muster-helper/1", "approval": valid, "x": 1}
            ).encode(),
            "deep arrays": b"[" * 2000 + b"]" * 2000,
            "deep objects": b'{"a":' * 600 + b"1" + b"}" * 600,
            "NaN": b'{"protocol":"muster-helper/1","approval":NaN}',
            "minus Infinity": b'{"protocol":"muster-helper/1","approval":-Infinity}',
            "a float": b'{"protocol":"muster-helper/1","approval":1.0}',
            "an exponent": b'{"protocol":"muster-helper/1","approval":1e999}',
            "a huge integer": b'{"protocol":"muster-helper/1","approval":' + b"9" * 4000 + b"}",
            "a NUL": good.replace(b"server", b"serv\\u0000er", 1),
            "a non-ASCII letter": good.replace(b"server", "servér".encode(), 1),
            "a byte order mark": b"\xef\xbb\xbf" + good,
            "two documents": good + good,
            "trailing text": good + b"x",
            "nothing": b"",
            "only whitespace": b"   \n",
            "a lone surrogate": b'{"protocol":"muster-helper/1","approval":"\\ud800"}',
            "invalid UTF-8": b'\xff\xfe{"a":1}',
            "a number for a string": json.dumps(
                {"protocol": "muster-helper/1", "approval": dict(valid, jobId=5)}
            ).encode(),
            "a boolean for a string": json.dumps(
                {"protocol": "muster-helper/1", "approval": dict(valid, host=True)}
            ).encode(),
            "equal nonces": stdin_of(approval(jobNonce=BASE_APPROVAL["approvalNonce"])),
            "capital hex": stdin_of(approval(approvalNonce="A1B2C3D4E5F60718293A4B5C6D7E8F90")),
            "a signature of 101": json.dumps(
                {"protocol": "muster-helper/1", "approval": dict(valid, signature="A" * 101)}
            ).encode(),
            "second 60": stdin_of(approval(expiresAt="2026-11-07T21:02:60Z")),
            "year 0": stdin_of(approval(issuedAt="0000-01-01T00:00:00Z")),
            "a lower case z": stdin_of(approval(expiresAt="2026-11-07T21:02:00z")),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                env = Env(self)
                answer, code, backend = module_request(env, COMMAND, raw, NOW)
                self.assertRefused(answer, code, "bad_input", verified=False)
                self.assertEqual(backend.events, [])
                self.assertFalse((env.state / "muster-nonces.jsonl").exists())

    def test_the_size_edges(self):
        good = stdin_of(approval())

        def pad(size):
            return good + b" " * (size - len(good))

        answer, code, _ = module_request(Env(self), COMMAND, pad(4096), NOW)
        self.assertDone(answer, code)
        answer, code, _ = module_request(Env(self), COMMAND, pad(4097), NOW)
        self.assertRefused(answer, code, "input_too_large", verified=False)
        answer, code, _ = module_request(Env(self), COMMAND, b"x" * 10_000_000, NOW)
        self.assertRefused(answer, code, "input_too_large", verified=False)

    def test_more_command_lines_that_are_not_requests(self):
        good = stdin_of(approval())
        for command in (
            "server restart fleet-server-status.timer --yes --json",
            "Server restart fleet-server-status.timer --yes",
            "mac restart fleet-server-status.timer --yes",
            "server restart ../x --yes",
            "server restart $(id) --yes",
            "server restart x;id --yes",
            "server\trestart fleet-server-status.timer --yes",
            "server restart fleet-server-status.timer --yes\n",
            "server restart fleet-server-status.timer --yes --yes",
            "server " + "a" * 300 + " x --yes",
        ):
            with self.subTest(command=command[:40]):
                answer, code, backend = module_request(Env(self), command, good, NOW)
                self.assertRefused(answer, code, "bad_command", verified=False)
                self.assertEqual(backend.events, [])
        for command in (
            "server resource-status coding --yes",
            "server seat-inventory claude --yes",
        ):
            answer, code, _ = module_request(Env(self), command, good, NOW)
            self.assertRefused(answer, code, "mode_not_allowed", verified=False)


class TimeoutOutcomeTests(Checks):
    """Section 13: a run that was started and killed at its time limit is `unknown`, not `failed`."""

    def context(self, env, runner):
        return foc.Context(
            platform="linux",
            home=env.home,
            runner=runner,
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )

    def test_a_command_that_times_out_is_unknown_in_the_answer_and_in_its_row(self):
        env = Env(self)
        ctx = self.context(env, lambda argv, **kw: (124, b""))
        answer, code, _, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW, ctx=ctx)
        self.assertEqual(code, 1, answer)
        self.assertEqual(answer["kind"], "result")
        self.assertSigned(answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])
        self.assertEqual(answer["body"]["auditId"], rows[0]["id"])
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)

    def test_a_command_that_fails_is_failed_and_one_that_cannot_start_is_failed(self):
        for status in (1, 3, 127):
            with self.subTest(status=status):
                env = Env(self)
                ctx = self.context(env, lambda argv, status=status, **kw: (status, b""))
                answer, code, _, rows = helper_request(
                    env, COMMAND, stdin_of(approval()), NOW, ctx=ctx
                )
                self.assertEqual((code, answer["body"]["outcome"]), (1, "failed"))
                self.assertEqual([row["result"] for row in rows], ["failed"])

    def test_the_other_route_without_the_switch_still_says_failed(self):
        env = Env(self)
        ctx = self.context(env, lambda argv, **kw: (124, b""))
        with mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL):
            answer, code = foc.handle(ctx, "server", "restart", "fleet-server-status.timer", "yes")
        self.assertEqual((code, answer["result"]), (1, "failed"))
        self.assertIn("exited 124", answer["detail"])

    def update_submission_answer(self, status):
        """(answer, code, rows) of a `server update fleet-cli --yes` whose submission exits `status`."""
        env = Env(self)
        write_allowlist(
            env,
            updates=[
                {"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"}
            ],
        )
        properties = PlannerBytesTests.PROPERTIES.encode()

        def runner(argv, **kw):
            if "show" in argv:
                return 0, properties
            if "-l" in argv:
                return 0, b""
            return status, b""

        ctx = self.context(env, runner)
        with (
            mock.patch.object(foc.os.path, "isfile", lambda path: True),
            mock.patch.object(foc.os, "access", lambda path, mode: True),
        ):
            config = foc.load_config(env.allowlist)
            commands, _, audited = foc.plan_request(ctx, config, "server", "update", "fleet-cli")
            plan = foc.plan_object("server", "update", "fleet-cli", commands, audited)
            digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
            document = approval(action="update", target="fleet-cli", planSHA256=digest)
            answer, code, _, rows = helper_request(
                env, "server update fleet-cli --yes", stdin_of(document), NOW, ctx=ctx
            )
        return answer, code, rows

    def test_an_update_whose_submission_times_out_is_unknown(self):
        answer, code, rows = self.update_submission_answer(124)
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])

    def test_an_update_submission_killed_by_a_signal_is_unknown_and_a_plain_failure_is_failed(self):
        for status in (-9, -15, -11):
            with self.subTest(status=status):
                answer, code, rows = self.update_submission_answer(status)
                self.assertEqual((code, answer["body"]["outcome"]), (1, "unknown"))
                self.assertEqual([row["result"] for row in rows], ["unknown"])
        for status in (1, 3, 255):
            with self.subTest(status=status):
                answer, code, rows = self.update_submission_answer(status)
                self.assertEqual((code, answer["body"]["outcome"]), (1, "failed"))
                self.assertEqual([row["result"] for row in rows], ["failed"])

    def test_a_child_killed_by_a_signal_is_unknown_not_failed(self):
        # A negative status is a signal (-9 from the memory controller, -15, -11): the child had
        # started and its end was not seen.
        for status in (-9, -15, -11):
            with self.subTest(status=status):
                env = Env(self)
                ctx = self.context(env, lambda argv, status=status, **kw: (status, b""))
                answer, code, _, rows = helper_request(
                    env, COMMAND, stdin_of(approval()), NOW, ctx=ctx
                )
                self.assertEqual((code, answer["body"]["outcome"]), (1, "unknown"))
                self.assertEqual([row["result"] for row in rows], ["unknown"])
                self.assertEqual(answer["body"]["auditId"], rows[0]["id"])
        for status in (1, 2, 126, 255):
            with self.subTest(status=status):
                env = Env(self)
                ctx = self.context(env, lambda argv, status=status, **kw: (status, b""))
                answer, code, _, rows = helper_request(
                    env, COMMAND, stdin_of(approval()), NOW, ctx=ctx
                )
                self.assertEqual((code, answer["body"]["outcome"]), (1, "failed"))

    def test_a_logs_run_killed_at_its_limit_or_by_a_signal_is_unknown(self):
        journalctl = "/usr/bin/journalctl"
        plan = foc.plan_object(
            "server",
            "logs",
            "fleet-server-status.timer",
            [
                [
                    journalctl,
                    "--user",
                    "-u",
                    "fleet-server-status.service",
                    "-n",
                    "200",
                    "--no-pager",
                ]
            ],
            "fleet-server-status.timer",
        )
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = approval(action="logs", planSHA256=digest)
        for status, outcome in ((124, "unknown"), (-9, "unknown"), (-15, "unknown"), (2, "failed")):
            with self.subTest(status=status):
                env = Env(self)
                ctx = self.context(env, lambda argv, status=status, **kw: (status, b""))
                with mock.patch.object(foc, "_journalctl", lambda: journalctl):
                    answer, code, _, rows = helper_request(
                        env,
                        "server logs fleet-server-status.timer --yes",
                        stdin_of(document),
                        NOW,
                        ctx=ctx,
                    )
                self.assertEqual((code, answer["body"]["outcome"]), (1, outcome), answer)
                self.assertEqual([row["result"] for row in rows], [outcome])

    def test_the_other_route_still_says_failed_for_a_signal_or_a_logs_timeout(self):
        # Without the route's switch nothing changes for the other callers: `failed`, as ever.
        env = Env(self)
        for status in (-9, 124):
            ctx = self.context(env, lambda argv, status=status, **kw: (status, b""))
            with mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL):
                answer, code = foc.handle(
                    ctx, "server", "restart", "fleet-server-status.timer", "yes"
                )
            self.assertEqual((code, answer["result"]), (1, "failed"), status)


class PlanRefusalReasonTests(Checks):
    """Section 4: a plan refusal's reason is the planner's own `error_code` when it gives one, else
    `allowlist_miss` or `report_stale`; the real planner, through the helper."""

    def update_world(self, properties=None, sudo_status=0, show_status=0, **replace):
        env = Env(self)
        write_allowlist(
            env,
            updates=[
                {"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"},
                {"name": "other", "unit": "other.service", "scope": "system"},
            ],
            coolify={"redeploy": True},
        )
        text = PlannerBytesTests.PROPERTIES if properties is None else properties
        for old, new in replace.items():
            text = text.replace(old.replace("__", "="), new.replace("__", "="))
        payload = text.encode()

        def runner(argv, **kw):
            if "show" in argv:
                return show_status, payload
            if "-l" in argv:
                return sudo_status, b""
            return 0, b""

        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=runner,
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        return env, ctx

    def plan_update(self, env, ctx, command="server update fleet-cli --plan", stdin=None):
        with (
            mock.patch.object(foc.os.path, "isfile", lambda path: True),
            mock.patch.object(foc.os, "access", lambda path, mode: True),
        ):
            return helper_request(env, command, stdin or job_stdin(), NOW, ctx=ctx)

    def test_each_of_the_planners_own_error_codes_is_passed_through_unchanged(self):
        for expected, make in (
            (
                "update_busy",
                lambda: self.update_world(ActiveState__inactive="ActiveState=activating"),
            ),
            ("update_busy", lambda: self.update_world(Job__="Job=17")),
            ("authorization_unavailable", lambda: self.update_world(sudo_status=1)),
            ("unit_status_unavailable", lambda: self.update_world(show_status=1)),
            ("unit_status_invalid", lambda: self.update_world(properties="garbage\n")),
            ("unit_status_invalid", lambda: self.update_world(InvocationID__="InvocationID=zz")),
            (
                "unit_unavailable",
                lambda: self.update_world(LoadState__loaded="LoadState=not-found"),
            ),
            ("unit_type_unsupported", lambda: self.update_world(Type__oneshot="Type=simple")),
        ):
            with self.subTest(code=expected):
                env, ctx = make()
                answer, code, _, rows = self.plan_update(env, ctx)
                self.assertRefused(answer, code, "plan_refused", verified=False, reason=expected)
                self.assertEqual(rows, [])
        # A system update that is not `fleet-cli` has no approved route.
        env, ctx = self.update_world()
        answer, code, _, _ = self.plan_update(env, ctx, "server update other --plan")
        self.assertRefused(
            answer, code, "plan_refused", verified=False, reason="update_route_unavailable"
        )
        # The approved systemctl is not there, and no sudo is: two more codes of the planner's own.
        env, ctx = self.update_world()
        with (
            mock.patch.object(foc.os.path, "isfile", lambda path: path != foc.UPDATE_SYSTEMCTL),
            mock.patch.object(foc.os, "access", lambda path, mode: True),
        ):
            answer, code, _, _ = helper_request(
                env, "server update fleet-cli --plan", job_stdin(), NOW, ctx=ctx
            )
        self.assertRefused(
            answer, code, "plan_refused", verified=False, reason="update_executable_unavailable"
        )
        env, ctx = self.update_world()
        with (
            mock.patch.object(foc.os.path, "isfile", lambda path: path != foc.SUDO),
            mock.patch.object(foc.os, "access", lambda path, mode: True),
        ):
            answer, code, _, _ = helper_request(
                env, "server update fleet-cli --plan", job_stdin(), NOW, ctx=ctx
            )
        self.assertRefused(
            answer, code, "plan_refused", verified=False, reason="authorization_unavailable"
        )

    def test_a_failed_idle_state_is_not_a_refusal(self):
        env, ctx = self.update_world(ActiveState__inactive="ActiveState=failed")
        answer, code, _, _ = self.plan_update(env, ctx)
        self.assertEqual(code, 0, "a failed unit is idle, so it can be updated again")

    def test_an_update_whose_route_changes_before_it_runs_is_plan_refused_route_changed(self):
        # `update_command` is asked three times in a run: by Y9, by the shared function's own plan,
        # and by the recheck just before the submission.  The third answer differs.
        env, ctx = self.update_world()
        answer, code, _, _ = self.plan_update(env, ctx)
        self.assertEqual(code, 0, answer)
        document = approval(
            action="update", target="fleet-cli", planSHA256=answer["body"]["planSHA256"]
        )
        real = foc.update_command
        asked = []

        def changing(context, target, entry):
            asked.append(1)
            command = real(context, target, entry)
            return command + ["--changed"] if len(asked) >= 3 else command

        with mock.patch.object(foc, "update_command", changing):
            answer, code, _, rows = self.plan_update(
                env, ctx, "server update fleet-cli --yes", stdin_of(document)
            )
        self.assertEqual(len(asked), 3)
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "failed")
        self.assertEqual(answer["body"]["detail"], "plan_refused: update_route_changed")
        self.assertEqual([row["result"] for row in rows], ["failed"])

    RECEIPT = "fleet-cli." + "a" * 32 + ".500"

    def receipt_world(self, audit_rows):
        env, ctx = self.update_world()
        if audit_rows is not None:
            data = b"".join(compact(row) + b"\n" for row in audit_rows)
            write_file(env.state / "ops-audit.jsonl", data, 0o600)
        return env, ctx

    @staticmethod
    def receipt_row(baseline=500, **changes):
        row = {
            "time": "2026-11-07T20:00:00Z",
            "host": "server",
            "verb": "server update",
            "target": "fleet-cli",
            "result": "submitted",
            "route": "sudo-system-update",
            "invocationID": "a" * 32,
            "baselineStartMonotonic": baseline,
        }
        row.update(changes)
        return row

    def update_status(self, env, ctx, target=None):
        command = f"server update-status {target or self.RECEIPT} --plan"
        return helper_request(env, command, job_stdin(), NOW, ctx=ctx)

    def test_update_status_with_its_receipt_is_a_planned_read(self):
        env, ctx = self.receipt_world([self.receipt_row()])
        answer, code, _, rows = self.update_status(env, ctx)
        self.assertEqual((code, answer["kind"]), (0, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "planned")
        self.assertEqual(len(rows), 1, "a read adds no row to the audit (the receipt is the one)")
        self.assertEqual(rows[0]["invocationID"], "a" * 32)

    def test_update_status_whose_baseline_is_not_the_recorded_one_is_receipt_context_mismatch(self):
        env, ctx = self.receipt_world([self.receipt_row(baseline=499)])
        answer, code, _, _ = self.update_status(env, ctx)
        self.assertRefused(
            answer, code, "plan_refused", verified=False, reason="receipt_context_mismatch"
        )

    def test_update_status_without_a_recorded_receipt_is_receipt_context_unavailable(self):
        for name, rows in (
            ("no audit file", None),
            ("another invocation", [self.receipt_row(invocationID="b" * 32)]),
            ("not an update row", [self.receipt_row(verb="server restart")]),
            ("an unknown route", [self.receipt_row(route="somewhere")]),
        ):
            with self.subTest(case=name):
                env, ctx = self.receipt_world(rows)
                answer, code, _, _ = self.update_status(env, ctx)
                self.assertRefused(
                    answer,
                    code,
                    "plan_refused",
                    verified=False,
                    reason="receipt_context_unavailable",
                )

    def test_the_same_reasons_are_what_a_failed_run_names_after_the_nonce(self):
        env, ctx = self.update_world()
        answer, code, _, _ = self.plan_update(env, ctx)
        self.assertEqual(code, 0, answer)
        digest = answer["body"]["planSHA256"]
        document = approval(action="update", target="fleet-cli", planSHA256=digest)
        for reason, make in (
            (
                "update_busy",
                lambda: self.update_world(ActiveState__inactive="ActiveState=activating"),
            ),
            ("authorization_unavailable", lambda: self.update_world(sudo_status=1)),
            (
                "unit_unavailable",
                lambda: self.update_world(LoadState__loaded="LoadState=not-found"),
            ),
        ):
            with self.subTest(reason=reason):
                env, ctx = make()
                answer, code, runner, rows = self.plan_update(
                    env, ctx, "server update fleet-cli --yes", stdin_of(document)
                )
                self.assertEqual((code, answer["kind"]), (1, "result"), answer)
                self.assertEqual(answer["body"]["outcome"], "failed")
                self.assertEqual(answer["body"]["detail"], "plan_refused: " + reason)
                self.assertEqual([row["result"] for row in rows], ["failed"])

    def redeploy(self, env, age, rows=None, uuid=REDEPLOY_UUID):
        write_status_report(env, [REDEPLOY_ROW] if rows is None else rows, age=age)
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        return helper_request(env, f"server redeploy {uuid} --plan", job_stdin(), NOW, ctx=ctx)

    def test_a_report_that_is_old_unsafe_foreign_or_does_not_list_the_application_is_report_stale(
        self,
    ):
        def world():
            env = Env(self)
            write_allowlist(env, coolify={"redeploy": True})
            return env

        answer, code, _, _ = self.redeploy(world(), 45 * 60)
        self.assertEqual(code, 0, "a report of exactly 45 minutes is still fresh")
        for name, age, rows in (
            ("46 minutes old", 45 * 60 + 1, None),
            ("an hour old", 3600, None),
            ("not listed", 30, []),
            ("another uuid", 30, [dict(REDEPLOY_ROW, uuid="zzz999yyy888")]),
            ("not an application", 30, [dict(REDEPLOY_ROW, type="service")]),
            ("not running", 30, [dict(REDEPLOY_ROW, status="absent")]),
            ("uuid that is a name", 30, [dict(REDEPLOY_ROW, uuidSource="name")]),
        ):
            with self.subTest(report=name):
                answer, code, _, _ = self.redeploy(world(), age, rows)
                self.assertRefused(
                    answer, code, "plan_refused", verified=False, reason="report_stale"
                )
        # Missing, writable by others, not JSON and from another tool are all the same reason.
        env = world()
        answer, code, _, _ = helper_request(
            env, f"server redeploy {REDEPLOY_UUID} --plan", job_stdin(), NOW
        )
        self.assertRefused(answer, code, "plan_refused", verified=False, reason="report_stale")
        for name, data, mode in (
            ("unsafe (group and world writable)", None, 0o666),
            ("not JSON", b"not json", 0o600),
            ("not an object", b"[]", 0o600),
            ("another tool's", json.dumps({"tool": "something-else"}).encode(), 0o600),
        ):
            with self.subTest(report=name):
                env = world()
                if data is None:
                    write_status_report(env, [REDEPLOY_ROW])
                    os.chmod(env.state / "server-status.json", mode)
                else:
                    write_file(env.state / "server-status.json", data, mode)
                ctx = foc.Context(
                    platform="linux",
                    home=env.home,
                    runner=FakeRunner(),
                    config_path=env.allowlist,
                    now=lambda: NOW_EPOCH,
                )
                answer, code, _, _ = helper_request(
                    env, f"server redeploy {REDEPLOY_UUID} --plan", job_stdin(), NOW, ctx=ctx
                )
                self.assertRefused(
                    answer, code, "plan_refused", verified=False, reason="report_stale"
                )

    def test_allowlist_miss_is_for_what_the_allowlist_does_not_offer(self):
        for command in (
            "server restart not-listed.service --plan",
            "server stop botfleet-server.service --plan",
            "server logs not-listed.service --plan",
            "server update nonesuch --plan",
            f"server redeploy {REDEPLOY_UUID} --plan",
        ):
            with self.subTest(command=command):
                answer, code, _, _ = helper_request(Env(self), command, job_stdin(), NOW)
                self.assertRefused(
                    answer, code, "plan_refused", verified=False, reason="allowlist_miss"
                )

    def test_only_genuine_faults_are_internal_error(self):
        # The allowlist cannot be read.
        env = Env(self)
        write_file(env.allowlist, b"not json", 0o600)
        answer, code, _, _ = helper_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertRefused(answer, code, "internal_error", verified=False, reason=None)
        # The plan cannot be written canonically.
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        backend = foc._MusterBackend(ctx, ma)
        with (
            mock.patch.object(ma, "canonical_bytes", side_effect=ValueError("no")),
            self.assertRaises(ma.BackendRefused) as caught,
        ):
            backend.plan_bytes("restart", "fleet-server-status.timer")
        self.assertIsNone(caught.exception.code)
        # The helper is not on the server.
        env = Env(self)
        ctx = foc.Context(
            platform="darwin", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        answer, code, _, _ = helper_request(env, PLAN_COMMAND, job_stdin(), NOW, ctx=ctx)
        self.assertRefused(answer, code, "internal_error", verified=False, reason=None)


class FaultRowTests(Checks):
    def test_the_row_of_a_fault_after_the_nonce_is_written_inside_the_lock(self):
        env = Env(self)
        backend = FakeBackend()
        original = backend.audit

        def audit(*args, **kwargs):
            backend.events.append("audit")
            return original(*args, **kwargs)

        def boom(verb, target):
            raise KeyError("boom")

        backend.audit = audit
        backend.plan_bytes = boom
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW, backend)
        self.assertEqual((code, answer["body"]["outcome"]), (1, "failed"))
        events = backend.events
        self.assertLess(events.index("lock"), events.index("audit"))
        self.assertLess(events.index("audit"), events.index("unlock"))
        self.assertEqual(events.count("audit"), 1)


class UnknownOutcomeTests(Checks):
    """Section 13: `unknown` is an action that was started and whose end could not be observed;
    `failed` only when it provably did not start or exited with a definite failure."""

    def setUp(self):
        # The sockets of these tests are on the loopback: no proxy variable may send them elsewhere.
        patcher = mock.patch.dict(
            os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_redeploy(self, http):
        env = Env(self)
        write_allowlist(env, coolify={"redeploy": True})
        write_status_report(env, [REDEPLOY_ROW])
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            http=http,
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        config = foc.load_config(env.allowlist)
        commands, _, audited = foc.plan_request(ctx, config, "server", "redeploy", REDEPLOY_UUID)
        plan = foc.plan_object("server", "redeploy", REDEPLOY_UUID, commands, audited)
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = approval(action="redeploy", target=REDEPLOY_UUID, planSHA256=digest)
        with (
            mock.patch.object(foc, "coolify_base", lambda ctx: "http://127.0.0.1:8000"),
            mock.patch.object(foc, "read_coolify_token", lambda ctx: "test-token"),
        ):
            return helper_request(
                env, f"server redeploy {REDEPLOY_UUID} --yes", stdin_of(document), NOW, ctx=ctx
            )

    def test_a_get_that_may_have_been_sent_and_got_no_answer_is_unknown(self):
        answer, code, _, rows = self.run_redeploy(lambda url, headers: foc.NoAnswer(0))
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])
        self.assertEqual(answer["body"]["auditId"], rows[0]["id"])

    def test_a_get_that_never_left_or_that_was_answered_badly_is_failed(self):
        for name, status in (("never sent", 0), ("answered 500", 500), ("answered 404", 404)):
            with self.subTest(case=name):
                answer, code, _, rows = self.run_redeploy(lambda url, headers, s=status: s)
                self.assertEqual((code, answer["body"]["outcome"]), (1, "failed"))
                self.assertEqual([row["result"] for row in rows], ["failed"])

    def test_a_get_that_was_accepted_is_submitted(self):
        answer, code, _, rows = self.run_redeploy(lambda url, headers: 202)
        self.assertEqual((code, answer["body"]["outcome"]), (0, "submitted"))
        self.assertEqual([row["result"] for row in rows], ["submitted"])

    def serve(self, behaviour):
        """A one-shot server on a loopback port, run by `behaviour(connection)`; returns its port."""
        import socket
        import threading

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        port = listener.getsockname()[1]

        def run():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            try:
                behaviour(connection)
            finally:
                connection.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        self.addCleanup(thread.join, 10)
        return port

    def test_http_get_tells_a_get_that_never_left_from_one_that_may_have_been_sent(self):
        import socket
        import struct
        import time

        # Connection refused: nothing was sent, a plain 0.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        status = foc.http_get(f"http://127.0.0.1:{refused_port}/x", {}, timeout=2)
        self.assertEqual(status, 0)
        self.assertFalse(getattr(status, "sent", False))
        # A URL that cannot be a request was never sent either.
        status = foc.http_get("not a url", {}, timeout=1)
        self.assertEqual(status, 0)
        self.assertFalse(getattr(status, "sent", False))

        # Accepted and never answered: a timeout, which may have been sent.
        def silent(connection):
            connection.recv(4096)
            time.sleep(1.5)

        status = foc.http_get(f"http://127.0.0.1:{self.serve(silent)}/x", {}, timeout=0.4)
        self.assertEqual(status, 0)
        self.assertTrue(getattr(status, "sent", False))

        # Reset after the request: the same.
        def reset(connection):
            connection.recv(4096)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

        status = foc.http_get(f"http://127.0.0.1:{self.serve(reset)}/x", {}, timeout=2)
        self.assertEqual(status, 0)
        self.assertTrue(getattr(status, "sent", False))

        # Closed without an answer: the same.
        def hang_up(connection):
            connection.recv(4096)

        status = foc.http_get(f"http://127.0.0.1:{self.serve(hang_up)}/x", {}, timeout=2)
        self.assertEqual(status, 0)
        self.assertTrue(getattr(status, "sent", False))

        # Answered: its own status, whatever it is.
        for line, expected in (("404 Not Found", 404), ("202 Accepted", 202)):
            answer = f"HTTP/1.1 {line}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()

            def respond(connection, answer=answer):
                connection.recv(4096)
                connection.sendall(answer)

            status = foc.http_get(f"http://127.0.0.1:{self.serve(respond)}/x", {}, timeout=2)
            self.assertEqual(status, expected)
            self.assertFalse(getattr(status, "sent", False))

    def test_no_answer_behaves_as_zero_everywhere_else(self):
        status = foc.NoAnswer(0)
        self.assertEqual(status, 0)
        self.assertFalse(status)
        self.assertFalse(200 <= status < 300)
        self.assertIsInstance(status, int)

    def helper_run(self, command, document, **patches):
        env = Env(self)
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(foc, name, value))
            return helper_request(env, command, stdin_of(document), NOW)

    def test_an_exception_out_of_the_execute_functions_is_unknown(self):
        def explode(*args, **kwargs):
            raise OSError("the status of the work was lost")

        answer, code, runner, rows = self.helper_run(COMMAND, approval(), execute=explode)
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])
        self.assertNotIn("status of the work", json.dumps(answer))
        self.assertEqual(answer["body"]["detail"], "the helper stopped unexpectedly while running")

    def test_an_exception_before_any_execute_function_is_failed(self):
        # The shared function builds its plan again before it runs anything: a fault there provably
        # started nothing.
        original = foc.plan_request
        calls = []

        def second_one_fails(ctx, config, scope, action, target):
            calls.append(1)
            if len(calls) >= 2:
                raise KeyError("boom")
            return original(ctx, config, scope, action, target)

        answer, code, runner, rows = self.helper_run(
            COMMAND, approval(), plan_request=second_one_fails
        )
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "failed")
        self.assertEqual([row["result"] for row in rows], ["failed"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(calls), 2)

    def test_the_backend_reports_whether_the_run_began_through_its_fault(self):
        env = Env(self)
        ctx = foc.Context(
            platform="linux", home=env.home, runner=FakeRunner(), config_path=env.allowlist
        )
        backend = foc._MusterBackend(ctx, ma)
        for began, expected in ((True, True), (False, False)):
            with self.subTest(began=began):

                def handle(ctx, scope, action, target, mode, began=began, **kwargs):
                    if began:
                        kwargs["reason_out"]["started"] = True
                    raise RuntimeError("boom")

                with (
                    mock.patch.object(foc, "handle", handle),
                    self.assertRaises(ma.RunFault) as caught,
                ):
                    backend.run("restart", "fleet-server-status.timer", dict(BASE_APPROVAL))
                self.assertIs(caught.exception.started, expected)

    def test_the_engine_reads_the_fault_the_same_way(self):
        for started, outcome in ((True, "unknown"), (False, "failed")):
            with self.subTest(started=started):
                env = Env(self)
                backend = FakeBackend()
                backend.run_error = ma.RunFault(started=started)
                answer, code, backend = module_request(
                    env, COMMAND, stdin_of(approval()), NOW, backend
                )
                self.assertEqual((code, answer["body"]["outcome"]), (1, outcome))
                self.assertEqual([row["result"] for row in backend.rows], [outcome])
        # Any other exception out of the run is taken to have begun.
        env = Env(self)
        backend = FakeBackend()
        backend.run_error = OSError("lost")
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW, backend)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in backend.rows], ["unknown"])


class NonceKeyTests(Checks):
    """Section 11: the ledger's key is the `approvalNonce` alone."""

    def test_the_same_nonce_from_another_job_or_device_is_a_replay(self):
        first = approval()
        for name, document, stored in (
            ("another job", approval(jobId="job9z9z9z9"), "v1"),
            (
                "another device",
                approval(signer=KEYS["device B approval"], deviceId=DEVICE_B),
                "v2",
            ),
            (
                "another device and job",
                approval(signer=KEYS["device B approval"], deviceId=DEVICE_B, jobId="job8y8y8y8"),
                "v2",
            ),
        ):
            with self.subTest(case=name):
                env = Env(self, stored=stored)
                self.assertDone(*module_request(env, COMMAND, stdin_of(first), NOW)[:2])
                answer, code, backend = module_request(
                    env, COMMAND, stdin_of(document), later(10), FakeBackend()
                )
                self.assertRefused(answer, code, "replayed_nonce", verified=True)
                self.assertEqual(backend.events.count("run"), 0)
                self.assertEqual(env.ledger.count(b"\n"), 1)

    def test_the_ledger_compares_the_nonce_and_nothing_else(self):
        # With the same device and job but another nonce it is not a replay; with the same nonce and
        # everything else different, it is.
        env = Env(self, stored="v2")
        self.assertDone(*module_request(env, COMMAND, stdin_of(approval()), NOW)[:2])
        other_nonce = approval(approvalNonce="9" * 32)
        self.assertDone(*module_request(env, COMMAND, stdin_of(other_nonce), later(10))[:2])
        everything_else = approval(
            signer=KEYS["device B approval"],
            deviceId=DEVICE_B,
            jobId="jobzzzzzzzz",
            jobNonce="1" * 32,
            issuedAt="2026-11-07T21:00:10Z",
        )
        answer, code, _ = module_request(env, COMMAND, stdin_of(everything_else), later(20))
        self.assertRefused(answer, code, "replayed_nonce", verified=True)


class RealStdinReaderTests(Checks):
    """The module's own `read_stdin_limited`, over a real pipe, at the limit and one byte beyond it."""

    def reader(self, data):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        os.write(write_end, data)
        os.close(write_end)
        return lambda limit: ma.read_stdin_limited(read_end, limit)

    def ask(self, data, command=COMMAND):
        env = Env(self)
        return ma.handle_request(
            env.module_config(NOW), FakeBackend(), command, True, self.reader(data), mt
        )

    def padded(self, base, size):
        return base + b" " * (size - len(base))

    def test_one_byte_over_the_limit_is_input_too_large_not_bad_input(self):
        good = stdin_of(approval())
        self.assertLess(len(good), ma.STDIN_LIMIT)
        answer, code = self.ask(self.padded(good, ma.STDIN_LIMIT + 1))
        self.assertRefused(answer, code, "input_too_large", verified=False)
        # At the limit it is read whole (the trailing spaces are JSON white space) and accepted.
        answer, code = self.ask(self.padded(good, ma.STDIN_LIMIT))
        self.assertDone(answer, code)
        # The plan class has the same limit, through the same reader.
        plan_input = job_stdin()
        answer, code = self.ask(self.padded(plan_input, ma.STDIN_LIMIT + 1), PLAN_COMMAND)
        self.assertRefused(answer, code, "input_too_large", verified=False)

    def feed_in_two_parts(self, first, second):
        """A pipe whose writer sends `first`, waits, then sends `second` and closes it."""
        import threading
        import time

        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)

        def feed():
            os.write(write_end, first)
            time.sleep(0.3)
            os.write(write_end, second)
            os.close(write_end)

        thread = threading.Thread(target=feed)
        thread.start()
        self.addCleanup(thread.join)
        return lambda limit: ma.read_stdin_limited(read_end, limit)

    def test_a_byte_that_arrives_after_exactly_the_limit_is_still_seen(self):
        # The reader must go on reading once it holds `limit` bytes, because limit + 1 is the proof.
        reader = self.feed_in_two_parts(b" " * ma.STDIN_LIMIT, b" ")
        self.assertEqual(len(reader(ma.STDIN_LIMIT)), ma.STDIN_LIMIT + 1)
        good = stdin_of(approval())
        reader = self.feed_in_two_parts(self.padded(good, ma.STDIN_LIMIT), b" ")
        env = Env(self)
        answer, code = ma.handle_request(
            env.module_config(NOW), FakeBackend(), COMMAND, True, reader, mt
        )
        self.assertRefused(answer, code, "input_too_large", verified=False)

    def test_the_reader_returns_at_most_the_limit_plus_one_byte(self):
        read_end, write_end = os.pipe()
        os.write(write_end, b"x" * 5000)
        os.close(write_end)
        try:
            self.assertEqual(len(ma.read_stdin_limited(read_end, 4096)), 4097)
        finally:
            os.close(read_end)


class TrustFilesOwnershipTests(Checks):
    """`muster_trust_files` judges each file's owner as well as the directory's."""

    def owned_by(self, uid, nth):
        """`os.fstat` as it is, except that the `nth` regular file it is asked about belongs to `uid`."""
        real = os.fstat
        seen = []

        def fstat(fd):
            info = real(fd)
            if not stat.S_ISREG(info.st_mode):
                return info
            seen.append(fd)
            if len(seen) != nth:
                return info
            fields = list(tuple(info))
            fields[4] = uid
            return os.stat_result(fields)

        return mock.patch.object(mt.os, "fstat", fstat)

    def test_a_file_that_root_does_not_own_is_refused_under_a_root_owned_directory(self):
        env = Env(self)
        write_file(
            env.etc / "reopen-codes.jsonl", ("\n".join(FIXTURE["reopen"]) + "\n").encode(), 0o644
        )
        self.assertEqual(mt.read_trust_files(str(env.etc), UID).owner_key, FIXTURE["pin"])
        # The pin, the history and the reopen record are the first, second and third file read.
        for nth, name in ((1, "owner.pub"), (2, "owner-history.jsonl"), (3, "reopen-codes.jsonl")):
            with self.subTest(file=name):
                with self.owned_by(UID + 1000, nth), self.assertRaises(mt.TrustFilesUnsafe):
                    mt.read_trust_files(str(env.etc), UID)
                with self.owned_by(UID + 1000, nth), self.assertRaises(ma.PinUnsafe):
                    ma.check_pin(env.module_config(NOW), mt)
        # With the owner it expects, the same files are read.
        self.assertEqual(mt.read_trust_files(str(env.etc), UID).owner_key, FIXTURE["pin"])

    def test_the_directory_is_judged_against_the_root_uid_it_was_given_too(self):
        env = Env(self)
        with self.assertRaises(mt.TrustFilesUnsafe):
            mt.read_trust_files(str(env.etc), UID + 1)


class ClockProbeGuardTests(Checks):
    def test_a_struct_of_another_size_fails_closed(self):
        env = Env(self)
        with mock.patch.object(ma.ctypes, "sizeof", lambda kind: 4):
            with self.assertRaises(OSError):
                ma.kernel_clock_state()
            self.assertFalse(ma.clock_is_synchronised(ma.kernel_clock_state))
            answer, code, backend = module_request(
                env, COMMAND, stdin_of(approval()), NOW, clock_state=ma.kernel_clock_state
            )
        self.assertRefused(answer, code, "clock_unsynced", verified=True)
        self.assertEqual(backend.events, [])

    def test_the_struct_is_the_size_the_probe_was_written_for(self):
        self.assertEqual(ma.ctypes.sizeof(ma._Timex), ma.TIMEX_SIZE)


class EnvironmentOrderTests(Checks):
    def test_the_comment_says_what_is_true(self):
        text = inspect.getdoc(foc._empty_environment)
        self.assertIn("urllib.request", text)
        self.assertIn("hashlib", text)
        self.assertNotIn("before any module", text)
        # OpenSSL reads its configuration at `import hashlib`, before the environment is emptied, so
        # the forced command (not the lazy import) is what gives a clean start.
        self.assertIn("OPENSSL_CONF", text)
        self.assertIn("env -i", text)
        self.assertIn("SSH_ORIGINAL_COMMAND", text)
        fetch = inspect.getdoc(foc.http_get)
        self.assertIn("hashlib", fetch)
        self.assertNotIn("because it loads `ssl`, and OpenSSL reads", fetch)
        module = inspect.getdoc(ma)
        self.assertIn("env -i", module)
        self.assertIn('SSH_ORIGINAL_COMMAND="$SSH_ORIGINAL_COMMAND"', module)

    def test_openssl_reads_its_configuration_when_hashlib_is_imported(self):
        # The fact the comment rests on, measured: with a configuration that offers only the base
        # provider, `import hashlib` before the environment is emptied falls back to the builtin
        # `sha256`; with the environment emptied first it keeps OpenSSL's.  So `os.environ.clear()`
        # in the helper, which comes after, cannot be what protects it.
        import textwrap

        config = Path(tempfile.mkdtemp()) / "base-only.cnf"
        self.addCleanup(shutil.rmtree, str(config.parent), True)
        config.write_text(
            textwrap.dedent(
                """\
                openssl_conf = openssl_init
                [openssl_init]
                providers = provider_sect
                [provider_sect]
                base = base_sect
                [base_sect]
                activate = 1
                """
            )
        )
        probe = (
            "import os, sys\n"
            "if sys.argv[1] == 'clear-first':\n"
            "    os.environ.clear()\n"
            "import hashlib\n"
            "print(type(hashlib.sha256).__name__, hashlib.sha256.__name__)\n"
        )
        results = {}
        for order in ("clear-first", "import-first"):
            result = subprocess.run(
                [sys.executable, "-I", "-c", probe, order],
                capture_output=True,
                env={"OPENSSL_CONF": str(config)},
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            results[order] = result.stdout.decode().strip()
        if results["clear-first"] == results["import-first"]:
            self.skipTest("this OpenSSL does not change its hash provider with that configuration")
        self.assertIn("openssl", results["clear-first"])
        self.assertNotIn("openssl", results["import-first"])


class AppendFaultTests(Checks):
    """Y8c: a fault at the nonce append is `internal_error`, not `state_unsafe`, and the nonce may be
    on disk (a failed fsync leaves the line there, so the same approval is then `replayed_nonce`).
    A fault before the rate step stays `state_unsafe`."""

    @staticmethod
    def target_of(fd):
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return ""

    def failing_write(self, name, error):
        real = ma._write_all

        def write(fd, data):
            if self.target_of(fd).endswith(name):
                raise OSError(error, os.strerror(error))
            return real(fd, data)

        return mock.patch.object(ma, "_write_all", write)

    def failing_fsync(self, ends_with, error, nth=1):
        """Fail the `nth` fsync of a descriptor whose target ends with `ends_with`."""
        real = os.fsync
        seen = []

        def fsync(fd):
            if self.target_of(fd).endswith(ends_with):
                seen.append(fd)
                if len(seen) == nth:
                    raise OSError(error, os.strerror(error))
            return real(fd)

        return mock.patch.object(os, "fsync", fsync)

    def ask(self, env, now=NOW):
        return module_request(env, COMMAND, stdin_of(approval()), now)

    def test_a_write_that_fails_at_the_append_is_internal_error_and_the_retry_runs(self):
        import errno

        env = Env(self)
        with self.failing_write("muster-nonces.jsonl", errno.ENOSPC):
            answer, code, backend = self.ask(env)
        self.assertRefused(answer, code, "internal_error", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertEqual(backend.rows, [], "the nonce was not spent, so no row")
        self.assertNotIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger or b"")
        answer, code, backend = self.ask(env, later(10))
        self.assertDone(answer, code)

    def test_an_fsync_that_fails_leaves_the_nonce_on_disk_and_the_retry_is_a_replay(self):
        import errno

        env = Env(self)
        with self.failing_fsync("muster-nonces.jsonl", errno.EIO):
            answer, code, backend = self.ask(env)
        self.assertRefused(answer, code, "internal_error", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertEqual(backend.rows, [])
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        answer, code, backend = self.ask(env, later(10))
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(backend.events.count("run"), 0)

    def test_a_directory_fsync_that_fails_after_the_file_is_created_is_internal_error(self):
        import errno

        env = Env(self)
        self.assertIsNone(env.ledger, "the ledger is created by this request")
        # The first fsync of the directory is the rate event's (Y8b); the second is the append's.
        with self.failing_fsync(env.state.name, errno.EIO, nth=2):
            answer, code, backend = self.ask(env)
        self.assertRefused(answer, code, "internal_error", verified=True)
        self.assertEqual(backend.events.count("run"), 0)

    def test_a_fault_before_the_rate_step_is_still_state_unsafe(self):
        import errno

        # Reading the ledger fails: the ledger is a state file that cannot be used.
        env = Env(self)
        real = ma._read_all

        def read(fd, cap):
            if cap == ma.LEDGER_MAX_BYTES:
                raise OSError(errno.EIO, "read")
            return real(fd, cap)

        with mock.patch.object(ma, "_read_all", read):
            answer, code, backend = self.ask(env)
        self.assertRefused(answer, code, "state_unsafe", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        # Writing the rate file fails: the rate file is a state file that cannot be used.
        env = Env(self)
        real_replace = ma._replace_file

        def replace(dir_fd, name, data, uid, durable):
            if name == ma.STATE_RATE_NAME:
                raise OSError(errno.EIO, "replace")
            return real_replace(dir_fd, name, data, uid, durable)

        with mock.patch.object(ma, "_replace_file", replace):
            answer, code, backend = self.ask(env)
        self.assertRefused(answer, code, "state_unsafe", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertNotIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger or b"")

    def test_the_docstring_and_the_error_say_the_same(self):
        self.assertIn("internal_error", inspect.getdoc(ma.LedgerAppendFault))
        self.assertTrue(issubclass(ma.LedgerAppendFault, Exception))
        self.assertFalse(issubclass(ma.LedgerAppendFault, ma.StateUnsafe))


class PostAppendNonFaultTests(Checks):
    """After the nonce is durably appended, a failed close of the ledger and a failed prune rewrite
    are not faults: the append is durable, the prune is atomic and the next approval retries it, so
    the run proceeds as if neither had failed.  Faults before the append stay refusals, and any other
    unexpected exception after it stays a signed result with one audit row."""

    @staticmethod
    def failing_ledger_close():
        real = os.close

        def close(fd):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                target = ""
            real(fd)
            if target.endswith("muster-nonces.jsonl"):
                raise OSError(5, "close")

        return mock.patch.object(os, "close", close)

    def test_a_close_that_fails_after_the_fsynced_append_is_a_done_run_with_one_row(self):
        env = Env(self)
        with self.failing_ledger_close():
            answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(env.ledger.count(b"\n"), 1)
        self.assertIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger)
        self.assertEqual([row["result"] for row in rows], ["done"])
        self.assertEqual(len(runner.calls), 1, "it ran, once")
        # The nonce is spent: the same approval is a replay.
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), later(10))
        self.assertRefused(answer, code, "replayed_nonce", verified=True)

    def test_a_close_that_fails_before_the_append_does_not_hide_the_refusal(self):
        corrupt = Env(self, ledger=b"not a ledger line\n")
        replayed = Env(self, ledger=ledger_line(BASE_APPROVAL["approvalNonce"]))
        with self.failing_ledger_close():
            answer, code, backend = module_request(corrupt, COMMAND, stdin_of(approval()), NOW)
            self.assertRefused(answer, code, "ledger_corrupt", verified=True)
            answer, code, backend = module_request(replayed, COMMAND, stdin_of(approval()), NOW)
            self.assertRefused(answer, code, "replayed_nonce", verified=True)
            self.assertEqual(backend.events.count("run"), 0)

    def test_a_prune_that_raises_anything_is_swallowed_and_the_run_proceeds(self):
        for error in (
            OSError(5, "rewrite"),
            RuntimeError("boom"),
            TypeError("a date was not a number"),
            ValueError("bad"),
            KeyError("expiresAt"),
        ):
            with self.subTest(error=type(error).__name__):
                env = Env(self)
                with mock.patch.object(ma.Ledger, "_prune", side_effect=error):
                    answer, code, runner, rows = helper_request(
                        env, COMMAND, stdin_of(approval()), NOW
                    )
                self.assertDone(answer, code)
                self.assertEqual(
                    env.ledger.count(b"\n"), 1, "the ledger is as it was, plus the line"
                )
                self.assertEqual([row["result"] for row in rows], ["done"])
                answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), later(10))
                self.assertRefused(answer, code, "replayed_nonce", verified=True)

    def test_a_repeated_prune_failure_never_turns_an_approval_into_a_refusal(self):
        env = Env(self)
        with mock.patch.object(ma.Ledger, "_prune", side_effect=RuntimeError("boom")):
            for step in range(3):
                nonce = "%032x" % (step + 1)
                answer, code, _ = module_request(
                    env, COMMAND, stdin_of(approval(approvalNonce=nonce)), later(10 * step)
                )
                self.assertDone(answer, code)
        self.assertEqual(env.ledger.count(b"\n"), 3)

    def old_ledger(self):
        """4,100 lines whose approvals expired weeks ago: the next prune would drop the first of them."""
        return b"".join(
            ledger_line(
                "%032x" % index, expires="2026-10-01T00:00:00Z", recorded="2026-10-01T00:00:00Z"
            )
            for index in range(1, ma.LEDGER_KEEP_LINES + 5)
        )

    def strays(self, env):
        return sorted(p.name for p in env.state.iterdir() if p.name.startswith(".muster-nonces"))

    def test_a_rewrite_that_fails_leaves_the_ledger_intact_and_no_temporary_file(self):
        import errno

        old = self.old_ledger()
        real_rename = os.rename

        def rename(source, destination, *args, **kwargs):
            if str(source).startswith(".muster-nonces.jsonl."):
                raise OSError(errno.EIO, "rename")
            return real_rename(source, destination, *args, **kwargs)

        env = Env(self, ledger=old)
        with mock.patch.object(os, "rename", rename):
            answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertTrue(env.ledger.startswith(old), "the old lines are all still there")
        self.assertEqual(env.ledger.count(b"\n"), ma.LEDGER_KEEP_LINES + 5)
        self.assertEqual(self.strays(env), [], "the temporary file was cleaned up")
        # The next approval retries the rewrite, and it works.
        answer, code, _ = module_request(
            env, COMMAND, stdin_of(approval(approvalNonce="9" * 32)), later(10)
        )
        self.assertDone(answer, code)
        self.assertEqual(env.ledger.count(b"\n"), ma.LEDGER_KEEP_LINES)
        self.assertEqual(self.strays(env), [])

    def test_a_rewrite_that_raises_something_other_than_an_oserror_is_swallowed_too(self):
        env = Env(self, ledger=self.old_ledger())
        real = ma._replace_file

        def replace(dir_fd, name, data, uid, durable):
            if name == ma.STATE_LEDGER_NAME:
                raise ValueError("the rewrite broke")
            return real(dir_fd, name, data, uid, durable)

        with mock.patch.object(ma, "_replace_file", replace):
            answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(env.ledger.count(b"\n"), ma.LEDGER_KEEP_LINES + 5)

    def test_any_other_fault_after_the_append_is_still_a_signed_result_with_one_row(self):
        def explode(*args, **kwargs):
            raise OSError("the status of the work was lost")

        env = Env(self)
        with (
            mock.patch.object(ma.Ledger, "_prune", side_effect=RuntimeError("boom")),
            self.failing_ledger_close(),
            mock.patch.object(foc, "execute", explode),
        ):
            answer, code, _, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertEqual((code, answer["kind"]), (1, "result"), answer)
        self.assertEqual(answer["body"]["outcome"], "unknown")
        self.assertEqual([row["result"] for row in rows], ["unknown"])
        self.assertEqual(env.ledger.count(b"\n"), 1)


class Y8FaultTests(Checks):
    """An unexpected exception inside Y8 happens before any nonce is spent: the refusal
    `internal_error` (exit 2), no audit row, nothing on the ledger."""

    def check(self, env, backend=None):
        answer, code, backend = module_request(
            env, COMMAND, stdin_of(approval()), NOW, backend or FakeBackend()
        )
        self.assertRefused(answer, code, "internal_error", verified=True)
        self.assertEqual(backend.rows, [])
        self.assertEqual(backend.events.count("run"), 0)
        self.assertNotIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger or b"")

    def test_an_exception_out_of_the_rate_step(self):
        env = Env(self)
        with mock.patch.object(ma, "check_rate", side_effect=RuntimeError("boom")):
            self.check(env)

    def test_an_exception_out_of_the_ledger_read(self):
        env = Env(self)
        real = ma._read_all

        def read(fd, cap):
            if cap == ma.LEDGER_MAX_BYTES:
                raise RuntimeError("boom")
            return real(fd, cap)

        with mock.patch.object(ma, "_read_all", read):
            self.check(env)

    def test_an_exception_out_of_the_ledger_record_itself(self):
        env = Env(self)
        with mock.patch.object(ma.Ledger, "record", side_effect=RuntimeError("boom")):
            self.check(env)


class ExpiryEdgeTests(Checks):
    """The window's last instant: a nonce used up to `expiresAt` is still spent at `expiresAt`."""

    EXPIRES = epoch("2026-11-07T21:02:00Z")

    def test_a_replay_at_exactly_expires_at_is_a_replay_and_runs_nothing(self):
        env = Env(self)
        stdin = stdin_of(approval())
        answer, code, backend = module_request(env, COMMAND, stdin, self.EXPIRES - 2.0)
        self.assertDone(answer, code)
        answer, code, backend = module_request(env, COMMAND, stdin, self.EXPIRES)
        self.assertRefused(answer, code, "replayed_nonce", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertEqual(env.ledger.count(b"\n"), 1)

    def test_one_microsecond_after_expires_at_is_expired(self):
        for beyond in (1e-6, 1e-4, 5e-4, 1e-3):
            with self.subTest(beyond=beyond):
                env = Env(self)
                answer, code, backend = module_request(
                    env, COMMAND, stdin_of(approval()), self.EXPIRES + beyond
                )
                self.assertRefused(answer, code, "expired", verified=True)
                self.assertEqual(backend.events, [])
        answer, code, _ = module_request(Env(self), COMMAND, stdin_of(approval()), self.EXPIRES)
        self.assertDone(answer, code)


class LockOrderTests(Checks):
    """Lock order is `ops-control.lock` first and `muster-rate.lock` second, everywhere, and a plan or
    an acknowledgement takes the rate lock alone."""

    def acquisitions(self, command, stdin):
        import fcntl

        real = fcntl.flock
        taken = []

        def flock(fd, operation):
            if operation & fcntl.LOCK_EX:
                try:
                    taken.append(os.path.basename(os.readlink(f"/proc/self/fd/{fd}")))
                except OSError:
                    taken.append("?")
            return real(fd, operation)

        env = Env(self)
        with mock.patch.object(fcntl, "flock", flock):
            answer, code, _, _ = helper_request(env, command, stdin, NOW)
        ours = ("ops-control.lock", "muster-rate.lock")
        return answer, code, [name for name in taken if name in ours]

    def test_an_approval_takes_the_action_lock_and_then_the_rate_lock(self):
        answer, code, taken = self.acquisitions(COMMAND, stdin_of(approval()))
        self.assertEqual(code, 0, answer)
        self.assertEqual(taken, ["ops-control.lock", "muster-rate.lock"])

    def test_a_plan_and_an_acknowledgement_take_the_rate_lock_alone(self):
        answer, code, taken = self.acquisitions(PLAN_COMMAND, job_stdin())
        self.assertEqual(code, 0, answer)
        self.assertEqual(taken, ["muster-rate.lock"])
        answer, code, taken = self.acquisitions(CURRENT_COMMAND, CURRENT_STDIN)
        self.assertEqual(code, 0, answer)
        self.assertEqual(taken, ["muster-rate.lock"])

    def test_a_refusal_before_y7_takes_no_lock_at_all(self):
        bad = approval(signer=KEYS["rogue owner (not pinned)"])
        answer, code, taken = self.acquisitions(COMMAND, stdin_of(bad))
        self.assertEqual(answer["body"]["errorCode"], "bad_signature")
        self.assertEqual(taken, [])


class RateLockWaitTests(Checks):
    """The wait for the rate lock is two seconds and no more: an unbounded wait fails here."""

    def test_the_real_wait_is_two_seconds_and_then_rate_limited(self):
        import fcntl
        import time

        env = Env(self)
        module_request(env, PLAN_COMMAND, job_stdin(), NOW)  # creates the lock file
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        started = time.monotonic()
        answer, code, _ = self.run_within(
            15,
            lambda: module_request(env, PLAN_COMMAND, job_stdin(), later(100)),
            unblock=lambda: fcntl.flock(fd, fcntl.LOCK_UN),
        )
        waited = time.monotonic() - started
        self.assertRefused(answer, code, "rate_limited", verified=False)
        self.assertGreaterEqual(waited, 1.8, "it waited for the lock")
        self.assertLess(waited, 6, "and not for ever")

    def test_a_held_rate_lock_at_y8b_is_rate_limited_with_the_nonce_unspent(self):
        import fcntl

        env = Env(self)
        module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        with mock.patch.object(ma, "RATE_LOCK_WAIT", 0.3):
            answer, code, backend = self.run_within(
                15,
                lambda: module_request(env, COMMAND, stdin_of(approval()), NOW),
                unblock=lambda: fcntl.flock(fd, fcntl.LOCK_UN),
            )
        self.assertRefused(answer, code, "rate_limited", verified=True)
        self.assertEqual(backend.events.count("run"), 0)
        self.assertNotIn(BASE_APPROVAL["approvalNonce"].encode(), env.ledger or b"")


class StateModeAndOwnerTests(Checks):
    """The owner and mode rules of the state directory and of each state file, one at a time.

    A test cannot be root or chown, so the owner is faked per descriptor: `os.fstat` is as it is
    except for the one file or directory asked about."""

    def test_the_state_directory_owner_is_checked_on_its_own(self):
        env = Env(self)
        with self.foreign_owner(env.state, directory=True):
            answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertEqual(
            (code, answer["body"]["errorCode"], answer["signature"]),
            (2, "helper_key_missing", None),
        )
        self.assertEqual(backend.events, [])
        answer, code, _ = module_request(Env(self), COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)

    def test_the_etc_muster_directory_owner_is_checked_on_its_own(self):
        env = Env(self)
        self.assertEqual(mt.read_trust_files(str(env.etc), UID).owner_key, FIXTURE["pin"])
        with self.foreign_owner(env.etc, directory=True):
            with self.assertRaises(mt.TrustFilesUnsafe):
                mt.read_trust_files(str(env.etc), UID)
            with self.assertRaises(ma.PinUnsafe):
                ma.check_pin(env.module_config(NOW), mt)
        self.assertIsNotNone(ma.check_pin(env.module_config(NOW), mt))

    def test_each_state_file_owned_by_another_account_is_refused(self):
        ledger = ledger_line("2" * 32)
        for name, expected, verified in (
            ("muster-helper.key", "helper_key_missing", None),
            ("muster-trust.json", "state_unsafe", False),
            ("muster-nonces.jsonl", "state_unsafe", True),
            ("muster-rate.json", "state_unsafe", True),
            ("muster-rate.lock", "state_unsafe", True),
        ):
            with self.subTest(file=name):
                env = Env(self, ledger=ledger)
                write_file(
                    env.state / "muster-rate.json",
                    b'{"contract":"muster-rate/1","events":[]}',
                    0o600,
                )
                write_file(env.state / "muster-rate.lock", b"", 0o600)
                with self.foreign_owner(env.state / name):
                    answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
                self.assertEqual(code, 2, answer)
                self.assertEqual(answer["body"]["errorCode"], expected, answer)
                if verified is not None:
                    self.assertEqual(answer["body"]["verified"], verified)
                self.assertEqual(backend.events.count("run"), 0)
        # The same files, owned by us, are accepted.
        env = Env(self, ledger=ledger)
        write_file(
            env.state / "muster-rate.json", b'{"contract":"muster-rate/1","events":[]}', 0o600
        )
        write_file(env.state / "muster-rate.lock", b"", 0o600)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)

    def test_a_plan_meets_the_same_rate_file_rules(self):
        env = Env(self)
        write_file(
            env.state / "muster-rate.json", b'{"contract":"muster-rate/1","events":[]}', 0o600
        )
        with self.foreign_owner(env.state / "muster-rate.json"):
            answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertRefused(answer, code, "state_unsafe", verified=False)

    def test_the_state_directory_with_any_group_or_other_bit_is_helper_key_missing(self):
        # The spec's own harness names 0755.  Each mode below carries one or more bits of 0o077.
        for mode in (0o755, 0o750, 0o705, 0o701, 0o710, 0o740, 0o704, 0o770, 0o777, 0o707):
            with self.subTest(mode=oct(mode)):
                env = Env(self)
                os.chmod(env.state, mode)
                answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
                self.assertEqual(
                    (code, answer["body"]["errorCode"], answer["signature"]),
                    (2, "helper_key_missing", None),
                )
                self.assertEqual(backend.events, [])
        # Through the helper's own entry, the spec's 0755: the unsigned answer, and nothing ran.
        env = Env(self)
        os.chmod(env.state, 0o755)
        answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertEqual(
            (code, answer["body"]["errorCode"], answer["signature"]),
            (2, "helper_key_missing", None),
        )
        self.assertEqual((runner.calls, rows), ([], []))
        env = Env(self)
        os.chmod(env.state, 0o700)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)


class StateFileSizeTests(Checks):
    """The size limits of the rate file (128 KiB) and of the helper key (4,096 bytes)."""

    RATE_EMPTY = b'{"contract":"muster-rate/1","events":[]}'

    def padded_rate(self, size):
        return self.RATE_EMPTY + b" " * (size - len(self.RATE_EMPTY))

    def test_a_rate_file_over_128_kib_is_state_unsafe_and_one_at_the_limit_is_read(self):
        self.assertEqual(ma.RATE_MAX_BYTES, 128 * 1024)
        for size in (ma.RATE_MAX_BYTES + 1, 129 * 1024):
            with self.subTest(size=size):
                env = Env(self)
                write_file(env.state / "muster-rate.json", self.padded_rate(size), 0o600)
                answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
                self.assertRefused(answer, code, "state_unsafe", verified=False)
                answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
                self.assertRefused(answer, code, "state_unsafe", verified=True)
                self.assertEqual(backend.events.count("run"), 0)
        env = Env(self)
        write_file(env.state / "muster-rate.json", self.padded_rate(ma.RATE_MAX_BYTES), 0o600)
        answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        self.assertEqual(code, 0, answer)

    def test_a_helper_key_file_over_the_limit_is_unusable_and_one_at_the_limit_is_read(self):
        env = Env(self)
        pem = (env.state / "muster-helper.key").read_bytes()
        self.assertLess(len(pem), ma.KEY_MAX_BYTES)
        for extra, expected in ((1, "helper_key_missing"), (0, None)):
            with self.subTest(extra=extra):
                env = Env(self)
                padding = b"\n" * (ma.KEY_MAX_BYTES + extra - len(pem))
                write_file(env.state / "muster-helper.key", pem + padding, 0o600)
                answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
                if expected:
                    self.assertEqual(
                        (code, answer["body"]["errorCode"], answer["signature"]),
                        (2, expected, None),
                    )
                else:
                    self.assertDone(answer, code)


class ActionLockFaultTests(Checks):
    """An unsafe `ops-control.lock` is `state_unsafe` (never `action_busy`), through the helper's own
    entry: nothing ran, the nonce is unspent, and no ledger line or audit row was written."""

    def ask(self, env):
        answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "state_unsafe", verified=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(rows, [])
        self.assertIsNone(env.ledger, "no ledger line, and no ledger file")

    def test_a_symbolic_link_a_directory_and_another_owner(self):
        env = Env(self)
        write_file(env.root / "real-lock", b"", 0o600)
        os.symlink(env.root / "real-lock", env.state / "ops-control.lock")
        self.ask(env)
        env = Env(self)
        (env.state / "ops-control.lock").mkdir(mode=0o700)
        self.ask(env)
        env = Env(self)
        write_file(env.state / "ops-control.lock", b"", 0o600)
        with self.foreign_owner(env.state / "ops-control.lock"):
            self.ask(env)

    def test_a_lock_that_is_held_is_action_busy_and_spends_nothing(self):
        import fcntl

        env = Env(self)
        write_file(env.state / "ops-control.lock", b"", 0o600)
        fd = os.open(env.state / "ops-control.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "action_busy", verified=True)
        self.assertEqual((runner.calls, rows, env.ledger), ([], [], None))


class LedgerContentTests(Checks):
    """A ledger line whose date is the right shape and no date is `ledger_corrupt`, at read time."""

    def test_an_impossible_date_in_either_field(self):
        for field in ("expires", "recorded"):
            for bad in (
                "2026-13-45T21:00:00Z",
                "2026-02-30T21:00:00Z",
                "2026-11-07T24:00:00Z",
                "2026-11-07T21:60:00Z",
                "2026-11-07T21:00:60Z",
            ):
                with self.subTest(field=field, date=bad):
                    ledger = ledger_line("2" * 32, **{field: bad})
                    env = Env(self, ledger=ledger)
                    answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
                    self.assertRefused(answer, code, "ledger_corrupt", verified=True)
                    self.assertEqual(backend.events.count("run"), 0)
                    self.assertEqual(env.ledger, ledger, "the file is left as it was")
        env = Env(self, ledger=ledger_line("2" * 32))
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)

    def test_the_parser_refuses_the_same_lines(self):
        good = ledger_line("2" * 32).rstrip(b"\n")
        self.assertEqual(ma._parse_ledger_line(good)["approvalNonce"], "2" * 32)
        bad = ledger_line("2" * 32, expires="2026-13-45T21:00:00Z").rstrip(b"\n")
        with self.assertRaises(ma.LedgerCorrupt):
            ma._parse_ledger_line(bad)


class LedgerBoundTests(Checks):
    """The ledger's size limit is derived once and leaves twice the room that the `yes` cap and the
    retention can fill."""

    LONGEST = {
        "approvalNonce": "f" * 32,
        "deviceId": "d-" + "f" * 24,
        "expiresAt": "2026-11-07T21:02:00Z",
        "jobId": "z" * 26,
        "recordedAt": "2026-11-07T21:00:00Z",
    }

    def test_the_most_lines_times_the_longest_line_is_under_half_the_limit(self):
        longest = len(ma.canonical_bytes(self.LONGEST)) + 1
        per_hour = ma.RATE_LIMITS["yes"][1]
        retained = max(4096, per_hour * (7 * 24 + 1))
        self.assertLess(retained * longest, ma.LEDGER_MAX_BYTES / 2)
        self.assertGreaterEqual(ma.LEDGER_LINE_MAX_BYTES, longest)
        self.assertGreaterEqual(ma.LEDGER_MAX_RETAINED_LINES, retained)
        self.assertEqual(ma.LEDGER_MAX_BYTES, 4 * 1024 * 1024)

    def test_a_ledger_of_that_many_longest_lines_is_read(self):
        retained = ma.LEDGER_MAX_RETAINED_LINES
        lines = b"".join(
            ma.canonical_bytes(dict(self.LONGEST, approvalNonce="%032x" % index)) + b"\n"
            for index in range(1, retained + 1)
        )
        self.assertLess(len(lines), ma.LEDGER_MAX_BYTES)
        env = Env(self, ledger=lines)
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)

    def test_one_byte_over_the_limit_is_ledger_corrupt(self):
        env = Env(self)
        write_file(env.state / "muster-nonces.jsonl", b"x" * (ma.LEDGER_MAX_BYTES + 1), 0o600)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "ledger_corrupt", verified=True)


class PrintableBoundsTests(Checks):
    """`printable_ascii` (the plan's audit target) and `printable` (a detail) keep 0x20 to 0x7E."""

    def test_each_boundary_character(self):
        for code, kept in (
            (0x00, False),
            (0x09, False),
            (0x1F, False),
            (0x20, True),
            (0x41, True),
            (0x7D, True),
            (0x7E, True),
            (0x7F, False),
            (0x80, False),
            (0xE9, False),
            (0x4E2D, False),
            (0x1F600, False),
        ):
            with self.subTest(code=hex(code)):
                text = "a" + chr(code) + "b"
                expected = text if kept else "a?b"
                self.assertEqual(foc.printable_ascii(text), expected)
                self.assertEqual(ma.printable(text), expected)

    def test_one_question_mark_per_code_point_with_nothing_collapsed_or_trimmed(self):
        for text, expected in (
            ("éé\U0001f600", "???"),
            (" a ", " a "),
            ("\x7f\x1f ", "?? "),
            ("Café App", "Caf? App"),
        ):
            self.assertEqual(foc.printable_ascii(text), expected)
            self.assertEqual(ma.printable(text), expected)

    def test_a_plan_with_such_a_name_is_canonical(self):
        for name in ("a\x7fb", "a\x1fb", "a\u0080b", "a~b", "a b"):
            plan = foc.plan_object("server", "redeploy", "abc123def456", [["coolify"]], name)
            ma.canonical_bytes(plan)  # raises NotCanonical for any byte outside 0x20 to 0x7E
        self.assertEqual(
            foc.plan_object("server", "redeploy", "abc123def456", [], "a\x7fb")["auditTarget"],
            "a?b",
        )


class AuditTargetCapTests(Checks):
    """The audit row names at most 120 characters of a target; the grammar of a unit allows 129."""

    def test_an_audit_row_names_at_most_120_characters_of_a_long_unit(self):
        unit = "a" + "b" * 118 + ".timer"
        self.assertEqual(len(unit), 125)
        self.assertIsNotNone(foc.UNIT_RE.fullmatch(unit))
        env = Env(self)
        write_allowlist(env, units=[{"unit": unit, "scope": "user", "actions": ["restart"]}])
        ctx = foc.Context(
            platform="linux",
            home=env.home,
            runner=FakeRunner(),
            config_path=env.allowlist,
            now=lambda: NOW_EPOCH,
        )
        config = foc.load_config(env.allowlist)
        commands, _, audited = foc.plan_request(ctx, config, "server", "restart", unit)
        plan = foc.plan_object("server", "restart", unit, commands, audited)
        digest = hashlib.sha256(ma.canonical_bytes(plan)).hexdigest()
        document = approval(action="restart", target=unit, planSHA256=digest)
        answer, code, _, rows = helper_request(
            env, f"server restart {unit} --yes", stdin_of(document), NOW, ctx=ctx
        )
        self.assertDone(answer, code)
        self.assertEqual(answer["body"]["target"], unit, "the answer names the whole target")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], unit[:120])
        self.assertEqual(len(rows[0]["target"]), 120)


class EnvironmentContextTests(Checks):
    def test_the_context_the_route_builds_gives_its_children_the_fixed_environment(self):
        env = Env(self)
        seen = []
        real = foc.Context

        class Spy(real):
            def __init__(self, *args, **kwargs):
                seen.append(dict(kwargs.get("environ") or {}))
                super().__init__(*args, **kwargs)

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PATH": "/tmp/evil:/usr/bin", "HOME": "/tmp/evil"}),
            mock.patch.object(foc, "HELPER_DIR", checkout()),
            mock.patch.object(foc, "Context", Spy),
            mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL),
        ):
            foc.main(
                ["--from-muster"],
                out=out,
                muster={
                    "config": env.config(NOW),
                    "ssh_command": PLAN_COMMAND,
                    "stdin": job_stdin(),
                },
            )
        import pwd

        home = pwd.getpwuid(os.geteuid()).pw_dir
        self.assertEqual(seen[0], {"HOME": home, **foc.MUSTER_CHILD_ENV})
        self.assertEqual(seen[0]["PATH"], "/usr/bin:/bin")


class FixtureLeavesTests(Checks):
    """Every long string of the fixture is printed in section 16 of the spec, so the fixture cannot
    drift from the spec (its `note` leaves are prose of its own)."""

    def leaves(self, value, path=""):
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, dict):
            for key, inner in value.items():
                yield from self.leaves(inner, path + "/" + str(key))
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                yield from self.leaves(inner, path + "[%d]" % index)

    # What the code makes (the generator's additions) is not printed in the spec: the whole answer
    # lines and their hashes, and the signature, answer and plan entries that were made by running it.
    MADE = ("badSignatures", "generatedAnswers", "generatedPlans", "resourceFieldScales")

    def test_every_long_leaf_the_spec_prints_is_in_section_16(self):
        text = SPEC_PATH.read_text(encoding="utf-8").replace("\u00a0 ", "  ")
        section = text[text.index("## 16.") : text.index("## 17.")]
        checked = 0
        for path, leaf in self.leaves(FIXTURE):
            top = path.split("/")[1].split("[")[0]
            if top in self.MADE or path.endswith(("/line", "/lineSHA256")):
                continue
            if len(leaf) < 32 or path.endswith("/note") or path == "/about":
                continue
            checked += 1
            self.assertIn(leaf, section, path)
        self.assertGreater(checked, 150)

    def test_what_the_code_makes_is_not_taken_for_what_the_spec_prints(self):
        # The exclusion above is exactly the generator's additions.
        printed = make.from_spec(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(FIXTURE) - set(printed) - {"about"}, set(self.MADE))

    def test_the_fixture_says_where_the_chain_slice_must_read_its_lists(self):
        about = FIXTURE["about"]
        for words in ("chain", "section 16", "T-lists", "entry hashes"):
            self.assertIn(words, about)


class FixtureGeneratorTests(Checks):
    """The fixture is what `scripts/make_muster_vectors.py` makes from the spec and the code, so it can
    never drift from either."""

    def committed(self):
        return (HERE / "fixtures" / "muster-vectors-v5.json").read_text(encoding="utf-8")

    def test_the_fixture_is_what_the_generator_makes(self):
        made = make.render(make.generate(SPEC_PATH.read_text(encoding="utf-8")))
        self.assertEqual(
            made,
            self.committed(),
            "scripts/fixtures/muster-vectors-v5.json differs from what scripts/make_muster_vectors.py "
            "makes:  run `python3 scripts/make_muster_vectors.py --write` and review the change",
        )

    def test_a_fresh_process_makes_the_same_bytes(self):
        # A different process (and hash seed) checks the committed file: nothing in it depends on
        # the run, the order of a dictionary, the clock, the host or a random number.
        result = subprocess.run(
            [sys.executable, "-I", str(HERE / "make_muster_vectors.py"), "--check"],
            capture_output=True,
            env={"PYTHONHASHSEED": "random"},
            timeout=300,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_check_says_so_when_the_file_differs_and_write_rewrites_it(self):
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "muster-vectors-v5.json"
            other.write_text(self.committed())
            stub = mock.patch.object(make, "generate", lambda text: {"changed": 1})
            with stub, mock.patch.object(make, "FIXTURE_PATH", other):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(make.main(["--check"]), 1)
                    self.assertIn("--write", errors.getvalue())
                    self.assertEqual(make.main(["--write"]), 0)
                    self.assertEqual(other.read_text(), make.render({"changed": 1}))
                    self.assertEqual(make.main(["--check"]), 0)

    def test_the_about_note_says_how_to_regenerate_it(self):
        about = FIXTURE["about"]
        self.assertIn("python3 scripts/make_muster_vectors.py --write", about)
        self.assertEqual(about, make.ABOUT)

    def test_what_the_spec_prints_is_what_the_spec_prints(self):
        printed = make.from_spec(SPEC_PATH.read_text(encoding="utf-8"))
        for name, value in printed.items():
            if name == "answers":
                for answer, entry in value.items():
                    kept = {k: v for k, v in FIXTURE["answers"][answer].items() if k in entry}
                    self.assertEqual(kept, entry, answer)
            else:
                self.assertEqual(FIXTURE[name], value, name)
        added = set(FIXTURE) - set(printed)
        self.assertEqual(
            added,
            {"about", "badSignatures", "generatedAnswers", "generatedPlans", "resourceFieldScales"},
        )

    def test_the_keys_are_test_keys_derived_from_their_labels(self):
        for label, entry in FIXTURE["keys"].items():
            key = KEYS[label]
            self.assertEqual(b64u(spki(key)), entry["public"], label)
            self.assertEqual(
                make.b64u(spki(make.derive(label))), entry["public"], "the generator's own"
            )


class FixtureAnswerLineTests(Checks):
    """The whole answer line of every signed answer: the one canonical-JSON line and its line feed."""

    def every_answer(self):
        for name, entry in FIXTURE["answers"].items():
            yield "answers." + name, entry["answer"], entry
        for section in ("badSignatures", "generatedAnswers", "generatedPlans"):
            for entry in FIXTURE[section]:
                yield f"{section}.{entry['id']}", entry["answer"], entry

    def test_each_line_is_the_canonical_bytes_of_its_answer_and_a_line_feed(self):
        count = 0
        for name, answer, entry in self.every_answer():
            with self.subTest(answer=name):
                line = entry["line"]
                self.assertEqual(line, ma.encode_answer(answer))
                self.assertEqual(line, ma.canonical_bytes(answer).decode("ascii") + "\n")
                self.assertTrue(line.endswith("}\n"))
                self.assertEqual(line.count("\n"), 1, "one line")
                self.assertEqual(json.loads(line), answer)
                self.assertEqual(entry["lineSHA256"], hashlib.sha256(line.encode()).hexdigest())
                count += 1
        self.assertEqual(count, 7 + 13 + 10 + 6)

    def test_each_signed_answer_verifies_under_its_own_prefix(self):
        for name, answer, entry in self.every_answer():
            with self.subTest(answer=name):
                if answer["signature"] is None:
                    self.assertEqual(answer, ma.unsigned_helper_key_missing())
                    continue
                self.assertSigned(answer)
                digest = hashlib.sha256(ma.canonical_bytes(answer["body"])).hexdigest()
                self.assertEqual(entry["bodySHA256"], digest)

    def test_the_unsigned_answer_is_the_same_line_whoever_writes_it(self):
        entry = next(e for e in FIXTURE["generatedAnswers"] if e["id"] == "helperKeyMissing")
        self.assertEqual(entry["answer"], foc.MUSTER_UNSIGNED_ANSWER)
        self.assertEqual(
            entry["line"],
            json.dumps(foc.MUSTER_UNSIGNED_ANSWER, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self.assertEqual(
            entry["answer"],
            {
                "protocol": "muster-helper/1",
                "kind": "refusal",
                "body": {"contract": "muster-signed-refusal/1", "errorCode": "helper_key_missing"},
                "keyId": None,
                "signature": None,
            },
        )
        self.assertEqual(entry["exitStatus"], 2)


class FixtureBadSignatureTests(Checks):
    """Each wrong signature, as data: the signature, the approval stdin that carries it, and the refusal."""

    CASES = (
        "leading-zero",
        "negative-integer",
        "trailing-byte",
        "long-form-length",
        "r-zero",
        "s-equals-n",
        "truncated",
        "stray-trailing-bits",
        "impossible-length",
        "padding-present",
        "over-100-characters",
        "empty",
        "high-s-twin",
    )
    BAD_INPUT = {"padding-present", "over-100-characters", "empty"}

    def entries(self):
        return {entry["id"]: entry for entry in FIXTURE["badSignatures"]}

    def test_the_cases_are_these_and_in_this_order(self):
        self.assertEqual([entry["id"] for entry in FIXTURE["badSignatures"]], list(self.CASES))

    def test_each_case_carries_its_signature_in_v1s_approval_and_nothing_else_changes(self):
        v1 = json.loads(FIXTURE["approvals"][0]["stdin"])["approval"]
        for case_id, entry in self.entries().items():
            with self.subTest(case=case_id):
                document = json.loads(entry["stdin"])
                self.assertEqual(entry["stdin"].encode(), stdin_of(document["approval"]))
                self.assertEqual(document["approval"]["signature"], entry["signature"])
                self.assertEqual(
                    {**document["approval"], "signature": None}, {**v1, "signature": None}
                )
                self.assertEqual((entry["stored"], entry["now"]), ("v1", NOW))

    def test_each_case_is_refused_as_it_says_and_the_one_good_twin_is_accepted(self):
        for case_id, entry in self.entries().items():
            with self.subTest(case=case_id):
                answer, code, backend = module_request(
                    Env(self), COMMAND, entry["stdin"].encode(), NOW
                )
                if case_id == "high-s-twin":
                    self.assertEqual(entry["expected"], {"kind": "result", "outcome": "done"})
                    self.assertDone(answer, code)
                    continue
                expected = "bad_input" if case_id in self.BAD_INPUT else "bad_signature"
                self.assertEqual(
                    entry["expected"], {"kind": "refusal", "errorCode": expected, "verified": False}
                )
                self.assertRefused(answer, code, expected, verified=False)
                self.assertEqual(answer, entry["answer"])
                self.assertEqual(code, entry["exitStatus"])
                self.assertEqual(backend.events, [])

    def test_no_wrong_signature_verifies_and_the_twin_does(self):
        v1 = json.loads(FIXTURE["approvals"][0]["stdin"])["approval"]
        text = text_of(v1)
        device_a = FIXTURE["lists"]["v1"]["body"]["devices"][0]["approvalKey"]
        for case_id, entry in self.entries().items():
            with self.subTest(case=case_id):
                self.assertEqual(
                    ma.verify_signature(device_a, entry["signature"], text),
                    case_id == "high-s-twin",
                )

    @staticmethod
    def contents(sig):
        """(r bytes, s bytes) of a DER signature with short-form lengths, as written."""
        position, out = 2, []
        for _ in range(2):
            length = sig[position + 1]
            out.append(sig[position + 2 : position + 2 + length])
            position += 2 + length
        return out

    def test_each_case_is_the_fault_its_title_names(self):
        entries = self.entries()
        valid = FIXTURE["approvals"][0]["signature"]
        valid_der = unb64u(valid)
        r, s = (int.from_bytes(part, "big") for part in self.contents(valid_der))
        der = {
            case_id: unb64u(entries[case_id]["signature"])
            for case_id in (
                "leading-zero",
                "negative-integer",
                "trailing-byte",
                "long-form-length",
                "r-zero",
                "s-equals-n",
                "truncated",
                "high-s-twin",
            )
        }
        zero = self.contents(der["leading-zero"])[0]
        self.assertEqual(zero[0], 0)
        self.assertFalse(zero[1] & 0x80 and zero[1] != 0, "no sign bit needs that zero byte")
        self.assertGreaterEqual(self.contents(der["negative-integer"])[0][0], 0x80)
        self.assertEqual(der["trailing-byte"], valid_der + b"\x00")
        self.assertEqual(der["long-form-length"][:2], b"\x30\x81")
        self.assertEqual(self.contents(der["r-zero"])[0], b"\x00")
        self.assertEqual(int.from_bytes(self.contents(der["s-equals-n"])[1], "big"), N)
        self.assertEqual(der["truncated"], valid_der[:-1])
        stray = entries["stray-trailing-bits"]["signature"]
        self.assertEqual(unb64u(stray), valid_der)
        self.assertNotEqual(stray, valid)
        self.assertEqual(len(entries["impossible-length"]["signature"]) % 4, 1)
        self.assertTrue(entries["padding-present"]["signature"].endswith("="))
        self.assertEqual(len(entries["over-100-characters"]["signature"]), 101)
        self.assertEqual(entries["empty"]["signature"], "")
        high = [int.from_bytes(part, "big") for part in self.contents(der["high-s-twin"])]
        self.assertEqual(high, [r, N - s])
        self.assertGreater(high[1], N // 2)


class FixtureGeneratedEntryTests(Checks):
    """Each generated answer and plan is made again by the module and the helper, and says what it
    claims to say."""

    def entries(self):
        return {e["id"]: e for e in FIXTURE["generatedAnswers"] + FIXTURE["generatedPlans"]}

    def test_the_entries_are_these(self):
        self.assertEqual(
            [e["id"] for e in FIXTURE["generatedAnswers"]],
            [
                "ackNothingHeld",
                "planRefusedAllowlistMiss",
                "logsResult",
                "updateStatusRead",
                "planRefusedReceiptMismatch",
                "failedResultPlanRefused",
                "unknownResult",
                "rateLimitedRefusal",
                "planMismatchResourcePolicy",
                "helperKeyMissing",
            ],
        )
        self.assertEqual(
            [e["id"] for e in FIXTURE["generatedPlans"]],
            [
                "restartPlanUserScope",
                "restartPlanSystemScope",
                "redeployPlanNonAscii",
                "resourcePlanCpuWeight",
                "resourcePlanRamBudget",
                "resourcePlanBorrowIdle",
            ],
        )

    def test_each_entry_is_made_again_by_the_module_and_the_helper(self):
        for entry_id, entry in self.entries().items():
            with self.subTest(entry=entry_id):
                answer, line, code = make.run_request(
                    FIXTURE, entry["setup"], entry["command"], entry["stdin"], entry["now"]
                )
                self.assertEqual(answer, entry["answer"])
                self.assertEqual(line, entry["line"])
                self.assertEqual(code, entry["exitStatus"])
                self.assertEqual(hashlib.sha256(line.encode()).hexdigest(), entry["lineSHA256"])

    def test_the_approvals_name_the_plan_the_helper_makes_for_them(self):
        for entry_id in ("logsResult", "unknownResult", "rateLimitedRefusal"):
            entry = self.entries()[entry_id]
            with self.subTest(entry=entry_id):
                approval_in = json.loads(entry["stdin"])["approval"]
                verb, target = approval_in["action"], approval_in["target"]
                plan, _, code = make.run_request(
                    FIXTURE, entry["setup"], f"server {verb} {target} --plan", make.job_stdin()
                )
                self.assertEqual(code, 0)
                self.assertEqual(plan["body"]["planSHA256"], approval_in["planSHA256"])
                device_a = FIXTURE["lists"]["v1"]["body"]["devices"][0]["approvalKey"]
                self.assertTrue(
                    ma.verify_signature(device_a, approval_in["signature"], text_of(approval_in))
                )

    def test_nothing_held_is_an_acknowledgement_with_null_hash_and_version(self):
        body = self.entries()["ackNothingHeld"]["answer"]["body"]
        self.assertEqual(self.entries()["ackNothingHeld"]["answer"]["kind"], "ack")
        self.assertEqual(
            {
                k: body[k]
                for k in (
                    "helperTrustListVersion",
                    "closed",
                    "signedByCurrentKey",
                    "trustListSHA256",
                )
            },
            {
                "helperTrustListVersion": None,
                "closed": False,
                "signedByCurrentKey": False,
                "trustListSHA256": None,
            },
        )
        self.assertEqual(body["finalRefusals"], [])
        self.assertEqual(self.entries()["ackNothingHeld"]["exitStatus"], 0)

    def test_a_plan_refusal_names_its_reason_and_is_never_verified(self):
        for entry_id, reason in (
            ("planRefusedAllowlistMiss", "allowlist_miss"),
            ("planRefusedReceiptMismatch", "receipt_context_mismatch"),
        ):
            entry = self.entries()[entry_id]
            body = entry["answer"]["body"]
            with self.subTest(entry=entry_id):
                self.assertEqual(entry["answer"]["kind"], "refusal")
                self.assertEqual((body["errorCode"], body["reason"]), ("plan_refused", reason))
                self.assertFalse(body["verified"])
                self.assertEqual(body["claimed"], JOB)
                self.assertIsNone(body["helperTrustListVersion"], "the list is read at R3")
                self.assertEqual(entry["exitStatus"], 2)
                self.assertIn(reason, ma.PLAN_REFUSED_REASONS)

    def test_after_the_nonce_the_same_reason_is_a_failed_results_detail(self):
        entry = self.entries()["failedResultPlanRefused"]
        body = entry["answer"]["body"]
        self.assertEqual(entry["answer"]["kind"], "result")
        self.assertEqual((body["outcome"], body["detail"]), ("failed", "plan_refused: update_busy"))
        self.assertEqual(entry["exitStatus"], 1)
        self.assertEqual(body["helperTrustListVersion"], 1)

    def test_a_read_result_carries_output_and_a_done_logs_result_carries_the_text(self):
        logs = self.entries()["logsResult"]["answer"]["body"]
        self.assertEqual(unb64u(logs["outputB64"]).decode("utf-8"), make.JOURNAL_TEXT)
        self.assertIn("ï", make.JOURNAL_TEXT)
        self.assertEqual(
            (logs["outcome"], logs["detail"], logs["auditId"]),
            ("done", "3 journal lines", make.AUDIT_ID),
        )
        read = self.entries()["updateStatusRead"]["answer"]["body"]
        self.assertEqual(
            (read["outcome"], read["claimed"], read["planSHA256"]), ("planned", JOB, None)
        )
        self.assertNotIn("approvalNonce", read)
        observed = json.loads(unb64u(read["outputB64"]))
        self.assertEqual(observed["executionState"], "completed")
        self.assertEqual(observed["baselineStartMonotonic"], make.RECEIPT_BASELINE)

    def test_an_unknown_outcome_is_exit_one_and_a_rate_limit_is_a_verified_refusal(self):
        unknown = self.entries()["unknownResult"]
        self.assertEqual(unknown["answer"]["body"]["outcome"], "unknown")
        self.assertEqual(unknown["exitStatus"], 1)
        self.assertEqual(unknown["setup"]["runnerStatus"], 124)
        limited = self.entries()["rateLimitedRefusal"]
        body = limited["answer"]["body"]
        self.assertEqual((body["errorCode"], body["verified"]), ("rate_limited", True))
        self.assertEqual(body["approvalNonce"], BASE_APPROVAL["approvalNonce"])
        self.assertIsNone(body["reason"])
        self.assertEqual(limited["exitStatus"], 2)
        key, at = limited["setup"]["rate"][0][1], limited["setup"]["rate"][0][2]
        self.assertEqual(key, "restart fleet-server-status.timer")
        self.assertEqual(epoch(NOW) * 1000 - at, 1000, "one second inside the two-second spacing")

    def test_a_plan_mismatch_is_a_verified_refusal_of_a_resource_policy_change(self):
        entry = self.entries()["planMismatchResourcePolicy"]
        body = entry["answer"]["body"]
        self.assertEqual((body["errorCode"], body["verified"]), ("plan_mismatch", True))
        self.assertEqual(body["action"], "resource-policy")
        self.assertEqual(entry["exitStatus"], 2)
        refused = FIXTURE["answers"]["refusalV6"]["answer"]["body"]
        self.assertEqual((refused["errorCode"], refused["verified"]), ("plan_mismatch", True))

    def test_each_plan_is_canonical_and_hashes_to_its_hash(self):
        for entry in FIXTURE["generatedPlans"]:
            with self.subTest(plan=entry["id"]):
                body = entry["answer"]["body"]
                plan = body["plan"]
                self.assertEqual(entry["plan"], plan)
                self.assertEqual(entry["planSHA256"], body["planSHA256"])
                self.assertEqual(
                    hashlib.sha256(plan.encode("ascii")).hexdigest(), body["planSHA256"]
                )
                self.assertEqual(ma.canonical_bytes(json.loads(plan)), plan.encode("ascii"))
                self.assertEqual(body["claimed"].keys(), JOB.keys())
                self.assertEqual(entry["exitStatus"], 0)

    def plan_of(self, entry_id):
        return json.loads(self.entries()[entry_id]["plan"])

    def test_a_system_unit_has_no_user_flag_and_a_user_unit_has(self):
        user = self.plan_of("restartPlanUserScope")
        system = self.plan_of("restartPlanSystemScope")
        self.assertEqual(
            user["commands"],
            [["/usr/bin/systemctl", "--user", "restart", "fleet-server-status.timer"]],
        )
        self.assertEqual(
            system["commands"], [["/usr/bin/systemctl", "restart", "botfleet-server.service"]]
        )
        self.assertEqual((user["contract"], system["contract"]), ("fleet-ops-plan/1",) * 2)
        self.assertEqual(system["auditTarget"], "botfleet-server.service")

    def test_a_non_ascii_application_name_is_one_question_mark_per_code_point(self):
        plan = self.plan_of("redeployPlanNonAscii")
        self.assertEqual(plan["auditTarget"], "Caf? App")
        self.assertEqual(plan["auditTarget"], foc.printable_ascii("Café App"))
        self.assertEqual(
            plan["commands"], [["coolify", "GET", f"/api/v1/deploy?uuid={make.APPLICATION}"]]
        )
        report = self.entries()["redeployPlanNonAscii"]["setup"]["report"]
        self.assertEqual(report[0]["name"], "Café App", "the row keeps the real name")

    def test_the_generators_fake_clock_advances_with_a_sleep(self):
        # A wait that the broker makes for the controller must end at its deadline, whatever code is
        # behind it: a clock that never moves made a mutant of the broker hang the mutation check.
        import types

        context = types.SimpleNamespace()
        make._resource_context(context, make.modules()[3])
        before = context.resource_monotonic()
        context.resource_sleep(0.25)
        self.assertEqual(context.resource_monotonic(), before + 0.25)
        context.resource_sleep(15)
        self.assertGreaterEqual(context.resource_monotonic(), before + 15)

    def test_a_resource_plan_holds_the_codes_own_units(self):
        import fleet_resource_control as frc

        cpu = self.plan_of("resourcePlanCpuWeight")
        ram = self.plan_of("resourcePlanRamBudget")
        borrow = self.plan_of("resourcePlanBorrowIdle")
        for plan in (cpu, ram, borrow):
            self.assertEqual(plan["contract"], "fleet-resource-plan/1")
            self.assertEqual(plan["auditTarget"], plan["field"])
            self.assertRegex(plan["revision"], r"[0-9a-f]{64}")
        self.assertEqual((cpu["old"], cpu["new"]), (50, 100))
        self.assertEqual((ram["old"], ram["new"]), (8 * frc.GIB, 12 * frc.GIB))
        self.assertEqual((ram["old"], ram["new"]), (8589934592, 12884901888))
        self.assertEqual((borrow["old"], borrow["new"]), (True, False))
        self.assertIs(borrow["old"], True)
        self.assertIs(borrow["new"], False)
        self.assertEqual(
            FIXTURE["resourceFieldScales"],
            {field: scale for field, (key, scale) in frc.FIELDS.items()},
        )
        self.assertEqual(FIXTURE["resourceFieldScales"]["ram-budget-gib"], frc.GIB)
        self.assertEqual(cpu["revision"], ram["revision"])
        self.assertEqual(cpu["revision"], make.resource_revision())


class CiSkipTests(Checks):
    """A machine without `cryptography` skips these files instead of failing; one that has it runs
    everything.  `sys.modules['cryptography'] = None` makes the module not importable."""

    CODE = (
        "import sys, unittest\n"
        "sys.modules['cryptography'] = None\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "if sys.argv[3] == 'discover':\n"
        "    suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern=sys.argv[2])\n"
        "else:\n"
        "    suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])\n"
        "result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)\n"
        "print('RESULT', result.testsRun, len(result.skipped), len(result.failures),"
        " len(result.errors))\n"
    )

    def run_without_cryptography(self, folder, name, how="name"):
        result = subprocess.run(
            [sys.executable, "-I", "-c", self.CODE, str(folder), name, how],
            capture_output=True,
            env={},
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.decode().splitlines() if line.startswith("RESULT")]
        return [int(word) for word in lines[-1].split()[1:]]

    def test_this_file_is_skipped_when_a_runner_discovers_it(self):
        # Discovery turns a SkipTest raised at import into one skipped test.  The copy sits in a
        # folder of its own, so that discovery meets nothing else (the guard is its first lines).
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(folder), True)
        shutil.copy(HERE / "test_muster_approval.py", folder)
        ran, skipped, failures, errors = self.run_without_cryptography(
            folder, "test_muster_approval.py", how="discover"
        )
        self.assertEqual((ran, skipped, failures, errors), (1, 1, 0, 0))

    def test_this_file_run_directly_says_so_and_exits_zero(self):
        code = (
            "import runpy, sys\n"
            "sys.modules['cryptography'] = None\n"
            "runpy.run_path(sys.argv[1], run_name='__main__')\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(HERE / "test_muster_approval.py")],
            capture_output=True,
            env={},
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"skipped", result.stdout)

    def test_the_installers_import_check_skips_and_still_runs_everything_else(self):
        name = (
            "test_install_resource_control.InstallerTests."
            "test_fixed_payload_modes_isolated_imports_and_preserved_state"
        )
        ran, skipped, failures, errors = self.run_without_cryptography(HERE / "hetzner", name)
        self.assertEqual((ran, skipped, failures, errors), (1, 1, 0, 0))


class DocumentedBoundariesTests(Checks):
    def test_the_choices_of_spec_version_5_are_named_in_one_place(self):
        self.assertEqual(ma.WINDOW_MAX, 120)
        self.assertEqual(ma.SKEW_MAX, 60)
        self.assertEqual((ma.LEDGER_KEEP_LINES, ma.LEDGER_KEEP_SECONDS), (4096, 7 * 86400))
        self.assertFalse(hasattr(ma, "LEDGER_PRUNE_AFTER"))
        self.assertEqual(ma.TIMEX_SIZE, 208)
        self.assertEqual(
            set(ma.PLAN_REFUSED_REASONS) - set(foc.MUSTER_PLANNER_CODES),
            {"allowlist_miss", "report_stale", "revision_stale"},
        )
        self.assertEqual(ma.AUDIT_FROM_STEP, "Y8")
        self.assertEqual(ma.LIST_MAX_DEVICES, 64)
        self.assertEqual(ma.LIST_MAX_COUNTERSIGNATURES, 64)
        self.assertEqual(ma.CLOCK_MAX_ERROR_US, 1_000_000)
        self.assertEqual((ma.TIME_ERROR, ma.STA_UNSYNC), (5, 0x40))
        self.assertEqual(ma.PLAN_REFUSED_REASONS, foc.MUSTER_PLAN_REASONS)
        self.assertEqual(ma.OUTCOMES, ("done", "submitted", "failed", "unknown", "planned"))

    def test_every_error_code_the_flow_answers_is_one_of_sections_15(self):
        section_15 = {
            "helper_key_missing",
            "pin_unsafe",
            "bad_command",
            "mode_not_allowed",
            "input_too_large",
            "bad_input",
            "rate_limited",
            "state_unsafe",
            "no_trust_list",
            "field_mismatch",
            "unknown_device",
            "bad_signature",
            "clock_unsynced",
            "window_too_long",
            "not_yet_valid",
            "expired",
            "action_busy",
            "trust_list_changed",
            "ledger_corrupt",
            "replayed_nonce",
            "plan_mismatch",
            "plan_refused",
            "internal_error",
            "chain_refused",
        }
        self.assertEqual(ma.ANSWERED_CODES, section_15)
        # The module defines no error constant outside that set, and none of the old ad-hoc ones.
        defined = {v for k, v in vars(ma).items() if k.startswith("ERR_")}
        self.assertEqual(defined, section_15)
        self.assertFalse(hasattr(ma, "ERR_HELPER_ERROR"))
        # Section 15's own table, in the repository's spec: the flow answers all of it.
        text = SPEC_PATH.read_text(encoding="utf-8")
        start = text.index("## 15.")
        table = text[start : text.index("Exit status:", start)]
        codes = set()
        for row in table.splitlines():
            if row.startswith("| `"):
                codes |= set(
                    part for part in row.split("|")[1].replace("`", " ").replace(",", " ").split()
                )
        self.assertEqual(codes, section_15)

    def test_no_code_is_answered_that_the_flow_does_not_name(self):
        # Every `Refusal(...)` and `refusal(...)` in the engine names a constant of that set.
        tree = ast.parse((HERE / "muster_approval.py").read_text())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("Refusal",):
                if node.args and isinstance(node.args[0], ast.Name):
                    used.add(node.args[0].id)
                else:
                    self.fail("a Refusal with a code that is not a named constant")
        self.assertTrue(used)
        for name in used:
            self.assertIn(getattr(ma, name), ma.ANSWERED_CODES, name)

    def test_the_trust_files_module_is_standalone(self):
        tree = ast.parse((HERE / "muster_agent" / "muster_trust_files.py").read_text())
        names = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("muster_approval", names)
        self.assertLessEqual(
            names, {"__future__", "base64", "datetime", "json", "os", "re", "stat", "cryptography"}
        )


# MARK: - The trust-list chain verb (sections 5, 8 and 12: P3 and C1 to C5)

CHAIN_COMMAND = "muster trust-list chain --yes"
CHAIN_PLAN_COMMAND = "muster trust-list chain --plan"
# The reading of the clock in the spec's T7 refusal: the vectors run at it unless a test says otherwise.
CHAIN_NOW = FIXTURE["answers"]["chainRefusalT7"]["answer"]["body"]["refusedAt"]
TRUST_NAME = "muster-trust.json"
ROTATED_OWNER = "owner after rotation (T24 only)"
# Every chain vector the spec prints, T9 having been retired (a number is never reused).
CHAIN_VECTOR_NUMBERS = [*range(1, 9), *range(10, 28)]
# The vectors that reach rule C: a Milestone 1 to 4 helper refuses each `reopen_unavailable`.
RULE_C_VECTORS = {"T4", "T5", "T8", "T15", "T18", "T20", "T21", "T22", "T23"}


def chain_stdin(*documents):
    return compact({"protocol": "muster-helper/1", "chain": list(documents)})


def printed(*names):
    """The lists section 16 prints, by the name its chain table uses."""
    return [FIXTURE["lists"][name] for name in names]


def entry_sha256(document):
    """The entry hash, made without the code under test: SHA-256 of the whole signed object."""
    return hashlib.sha256(compact(document)).hexdigest()


def body_sha256(document):
    return hashlib.sha256(compact(document["body"])).hexdigest()


def rotate_history(env):
    """Pin the rotated history (T24 only): version 1 closed with its body hash, then the new owner key."""
    lines = FIXTURE["historyRotated"].split("\n")
    write_file(env.etc / "owner-history.jsonl", (FIXTURE["historyRotated"] + "\n").encode(), 0o644)
    write_file(env.etc / "owner.pub", (json.loads(lines[-1])["ownerKey"] + "\n").encode(), 0o644)


def reversed_keys(value):
    """`value` with every object's keys in reverse order (a different formatting of the same list)."""
    if isinstance(value, dict):
        return {name: reversed_keys(value[name]) for name in sorted(value, reverse=True)}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def stored_bytes(env):
    path = env.state / TRUST_NAME
    return path.read_bytes() if path.exists() else None


def leftovers(env):
    """Temporary files of a write of the trust list that are still in the state directory."""
    return sorted(p.name for p in env.state.iterdir() if p.name.startswith(".muster-trust.json."))


def countersignature(signer, device_id, body):
    return {
        "deviceId": device_id,
        "signature": b64u(sign(signer, b"muster-trust-countersign/1\n" + compact(body))),
    }


def next_rule_a(previous, owner=OWNER, countersigner=DEVICE_A, **changes):
    """The list after `previous` under rule A, owner-signed and countersigned by device A (on every
    list of the vectors).  `changes` are body fields set before anything is signed."""
    body = json.loads(json.dumps(previous["body"]))
    body["version"] += 1
    body["previousSHA256"] = body_sha256(previous)
    body["issuedAt"] = "2026-11-07T21:%02d:00Z" % body["version"]
    body.update(changes)
    document = resign(body, owner)
    document["countersignatures"] = [
        countersignature(countersigner, BASE_APPROVAL["deviceId"], body)
    ]
    return document


def long_chain(count):
    """`count` lists from version 1, each a valid rule A step on the one before."""
    documents = printed("v1")
    while len(documents) < count:
        documents.append(next_rule_a(documents[-1]))
    return documents


def ack_body(newest, at=CHAIN_NOW, current=True):
    return {
        "at": at,
        "closed": newest["body"]["closed"],
        "contract": "muster-signed-ack/1",
        "finalRefusals": [],
        "helperTrustListVersion": newest["body"]["version"],
        "signedByCurrentKey": current,
        "trustListSHA256": body_sha256(newest),
    }


class ChainChecks(Checks):
    """What a chain answer must look like, shared by the classes below."""

    def chain_world(self, held="v1", rotated=False, **kwargs):
        env = Env(self, stored=held, **kwargs)
        if rotated:
            rotate_history(env)
        return env

    def ask(self, env, *documents, command=CHAIN_COMMAND, now=CHAIN_NOW, raw=None, backend=None):
        stdin = chain_stdin(*documents) if raw is None else raw
        return module_request(env, command, stdin, now, backend)

    def assertChainRefused(self, answer, code, reason, refused=None, version=None):
        """`chain_refused` for `reason`, naming `refused` (a list), with `version` as the held version."""
        self.assertRefused(answer, code, "chain_refused", verified=False, reason=reason)
        body = answer["body"]
        self.assertNotIn("claimed", body)
        self.assertEqual(body["helperTrustListVersion"], version)
        if refused is not None:
            self.assertEqual(
                body["chain"],
                {
                    "version": refused["body"]["version"],
                    "entrySHA256": entry_sha256(refused),
                    "final": False,
                },
            )

    def assertOtherRefusal(self, answer, code, expected, version=None):
        """Any refusal that is not C3's: its `chain` field is null and it carries no `claimed`."""
        self.assertRefused(answer, code, expected, verified=False)
        body = answer["body"]
        self.assertIn("chain", body)
        self.assertIsNone(body["chain"])
        self.assertNotIn("claimed", body)
        self.assertEqual(body["helperTrustListVersion"], version)

    def assertAccepted(self, env, answer, code, newest, current=True, at=CHAIN_NOW):
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["kind"], "ack")
        self.assertSigned(answer)
        self.assertEqual(answer["body"], ack_body(newest, at, current))
        self.assertEqual(stored_bytes(env), compact(newest))
        self.assertEqual(leftovers(env), [])


# MARK: - The vectors


class ChainVectorTests(ChainChecks):
    """Section 16's chain vectors, driven from the fixture's `frozenExpected` (never `proposalExpected`,
    which is the OPEN proposal's answer for a rule C row) through the module, the helper in process and
    the helper as a real process fed over standard input."""

    def row_world(self, row):
        return self.chain_world(row["held"], rotated=row["history"] == "historyRotated")

    def row_stdin(self, row):
        return chain_stdin(*printed(*row["chain"]))

    def check_row(self, row, env, answer, code, before, audit_rows):
        expected = row["frozenExpected"]
        names = row["chain"]
        if expected == "ack":
            newest = FIXTURE["lists"][names[-1]]
            self.assertAccepted(env, answer, code, newest)
            held_version = (
                None if row["held"] is None else FIXTURE["lists"][row["held"]]["body"]["version"]
            )
            self.assertEqual(len(audit_rows), 1, audit_rows)
            fields = dict(audit_rows[0])
            fields.pop("time", None)
            fields.pop("host", None)
            verb = fields.pop("verb")
            if "scope" in fields:  # the module's fake backend records the scope apart
                self.assertEqual((fields.pop("scope"), verb), ("muster", "trust-list"))
            else:  # the helper's own row writes both words in its `verb`
                self.assertEqual(verb, "muster trust-list")
            self.assertEqual(
                fields,
                {
                    "target": "chain",
                    "result": "accepted",
                    "route": "from-muster",
                    "via": "muster-ios",
                    "trustListVersion": newest["body"]["version"],
                    "trustListSHA256": body_sha256(newest),
                    "fromVersion": held_version,
                },
            )
        else:
            refused = FIXTURE["lists"][names[expected["atEntry"] - 1]]
            self.assertChainRefused(
                answer, code, expected["reason"], refused, expected["helperTrustListVersion"]
            )
            self.assertEqual(answer["body"]["errorCode"], expected["errorCode"])
            self.assertEqual(stored_bytes(env), before, "a refused chain stores nothing")
            self.assertEqual(leftovers(env), [])
            self.assertEqual(audit_rows, [], "a refused chain writes no audit row")

    def test_the_fixture_carries_every_vector_and_marks_the_rule_c_rows(self):
        rows = FIXTURE["chainVectors"]
        self.assertEqual(
            sorted(int(row["id"].split(" ")[0][1:]) for row in rows), CHAIN_VECTOR_NUMBERS
        )
        self.assertNotIn(9, CHAIN_VECTOR_NUMBERS)
        self.assertEqual({r["id"].split(" ")[0] for r in rows if r["ruleC"]}, RULE_C_VECTORS)
        for row in rows:
            with self.subTest(vector=row["id"]):
                self.assertEqual(
                    set(row),
                    {
                        "id",
                        "held",
                        "chain",
                        "history",
                        "ruleC",
                        "frozenExpected",
                        "proposalExpected",
                    },
                )
                self.assertIn(row["history"], ("history", "historyRotated"))
                # Only T24 runs under the rotated history.
                self.assertEqual(row["history"] == "historyRotated", row["id"].startswith("T24 "))
                expected = row["frozenExpected"]
                if row["ruleC"]:
                    self.assertEqual(expected["reason"], "reopen_unavailable")
                else:
                    self.assertNotEqual(
                        expected == "ack" or expected["reason"], "reopen_unavailable"
                    )
                if expected != "ack":
                    self.assertEqual(expected["errorCode"], "chain_refused")
                    self.assertIn(expected["reason"], ma.CHAIN_REFUSED_REASONS)

    def test_a_rule_c_row_is_refused_at_its_first_entry_except_t5_at_its_fourth(self):
        for row in FIXTURE["chainVectors"]:
            if row["ruleC"]:
                at = 4 if row["id"].startswith("T5 ") else 1
                self.assertEqual(row["frozenExpected"]["atEntry"], at, row["id"])
        # T5 holds nothing, so it is refused with no version to report.
        t5 = next(row for row in FIXTURE["chainVectors"] if row["id"].startswith("T5 "))
        self.assertIsNone(t5["frozenExpected"]["helperTrustListVersion"])

    def test_every_vector_through_the_module(self):
        for row in FIXTURE["chainVectors"]:
            with self.subTest(vector=row["id"]):
                env = self.row_world(row)
                before = stored_bytes(env)
                answer, code, backend = module_request(
                    env, CHAIN_COMMAND, self.row_stdin(row), CHAIN_NOW
                )
                self.check_row(row, env, answer, code, before, backend.rows)
                self.assertEqual(backend.events.count("run"), 0)
                self.assertEqual(
                    backend.events, ["lock", "unlock"] if row["frozenExpected"] == "ack" else []
                )

    def test_every_vector_through_the_helper_in_process(self):
        for row in FIXTURE["chainVectors"]:
            with self.subTest(vector=row["id"]):
                env = self.row_world(row)
                before = stored_bytes(env)
                answer, code, runner, rows = helper_request(
                    env, CHAIN_COMMAND, self.row_stdin(row), CHAIN_NOW
                )
                self.check_row(row, env, answer, code, before, rows)
                self.assertEqual(runner.calls, [], "no command is run for a chain")

    def test_every_vector_through_the_helper_as_a_subprocess_over_stdin(self):
        for row in FIXTURE["chainVectors"]:
            with self.subTest(vector=row["id"]):
                env = self.row_world(row)
                before = stored_bytes(env)
                answer, code, calls, rows = subprocess_request(
                    self, env, CHAIN_COMMAND, self.row_stdin(row), CHAIN_NOW
                )
                self.check_row(row, env, answer, code, before, rows)
                self.assertEqual(calls, [])

    def test_the_printed_answers_are_what_the_flow_signs_byte_for_byte(self):
        # T2 and T3's acknowledgements and T7's refusal, at the clock readings they print: the whole
        # answer, signature included (signing is deterministic), and so its line and body hash.
        for name, vector, field in (
            ("ackT2", "T2 ", "at"),
            ("ackT3", "T3 ", "at"),
            ("chainRefusalT7", "T7 ", "refusedAt"),
        ):
            with self.subTest(answer=name):
                row = next(r for r in FIXTURE["chainVectors"] if r["id"].startswith(vector))
                reading = FIXTURE["answers"][name]["answer"]["body"][field]
                for variant in ("module", "helper", "subprocess"):
                    env = self.row_world(row)
                    stdin = self.row_stdin(row)
                    if variant == "module":
                        answer, _, _ = module_request(env, CHAIN_COMMAND, stdin, reading)
                    elif variant == "helper":
                        answer, _, _, _ = helper_request(env, CHAIN_COMMAND, stdin, reading)
                    else:
                        answer, _, _, _ = subprocess_request(
                            self, env, CHAIN_COMMAND, stdin, reading
                        )
                    printed_answer = FIXTURE["answers"][name]
                    self.assertEqual(answer, printed_answer["answer"], variant)
                    self.assertEqual(ma.encode_answer(answer), printed_answer["line"], variant)
                    self.assertEqual(
                        hashlib.sha256(ma.canonical_bytes(answer["body"])).hexdigest(),
                        printed_answer["bodySHA256"],
                    )

    def test_the_spec_prints_the_entry_hashes_the_helper_computes(self):
        # v4 and its tampered copy T15 share a body hash and differ in entry hash (section 16).
        text = SPEC_PATH.read_text(encoding="utf-8")
        v4, t15 = FIXTURE["lists"]["v4"], FIXTURE["lists"]["T15 list"]
        self.assertEqual(body_sha256(v4), body_sha256(t15))
        self.assertNotEqual(entry_sha256(v4), entry_sha256(t15))
        for digest in (entry_sha256(v4), entry_sha256(t15), body_sha256(v4)):
            self.assertIn(digest, text)
        answer, _, _ = module_request(
            self.chain_world("v3"), CHAIN_COMMAND, chain_stdin(v4), CHAIN_NOW
        )
        self.assertEqual(answer["body"]["chain"]["entrySHA256"], entry_sha256(v4))

    def test_the_acknowledgement_names_the_body_hash_and_not_the_entry_hash(self):
        v2 = FIXTURE["lists"]["v2"]
        answer, code, _ = module_request(
            self.chain_world("v1"), CHAIN_COMMAND, chain_stdin(v2), CHAIN_NOW
        )
        self.assertEqual(code, 0)
        self.assertEqual(answer["body"]["trustListSHA256"], body_sha256(v2))
        self.assertNotEqual(body_sha256(v2), entry_sha256(v2))

    def test_t5_stores_nothing_not_even_the_entries_before_the_one_it_refuses(self):
        row = next(r for r in FIXTURE["chainVectors"] if r["id"].startswith("T5 "))
        env = self.row_world(row)
        answer, code, backend = module_request(env, CHAIN_COMMAND, self.row_stdin(row), CHAIN_NOW)
        self.assertChainRefused(
            answer, code, "reopen_unavailable", FIXTURE["lists"]["v4"], version=None
        )
        self.assertFalse((env.state / TRUST_NAME).exists())
        self.assertEqual(backend.events, [], "the lock is not taken for a refused chain")
        self.assertEqual(backend.rows, [])


class ChainRulesTests(ChainChecks):
    """Section 8's checks one at a time, beyond the printed vectors."""

    def reason_of(self, held, *documents, rotated=False):
        """(the reason, the version of the refused entry) of a chain that must be `chain_refused`."""
        env = self.chain_world(held, rotated=rotated)
        answer, code, backend = self.ask(env, *documents)
        self.assertEqual(code, 2, answer)
        self.assertEqual(answer["body"]["errorCode"], "chain_refused", answer)
        self.assertEqual(backend.rows, [])
        if held is None:
            self.assertFalse((env.state / TRUST_NAME).exists())
        return answer["body"]["reason"], answer["body"]["chain"]["version"]

    def test_a_thirty_two_entry_chain_from_nothing_is_accepted_and_the_newest_is_stored(self):
        documents = long_chain(32)
        env = self.chain_world(None)
        answer, code, backend = self.ask(env, *documents)
        self.assertAccepted(env, answer, code, documents[-1])
        self.assertEqual(answer["body"]["helperTrustListVersion"], 32)
        self.assertEqual(len(backend.rows), 1)
        self.assertEqual(backend.rows[0]["fromVersion"], None)
        self.assertEqual(backend.rows[0]["trustListVersion"], 32)
        # Only the newest entry is stored, and the walk reached all 32 (the last has version 32).
        self.assertEqual(json.loads(stored_bytes(env))["body"]["version"], 32)

    def test_a_chain_continues_from_what_is_held(self):
        documents = long_chain(10)
        env = self.chain_world(None)
        env.store(documents[3])
        answer, code, backend = self.ask(env, *documents[4:])
        self.assertAccepted(env, answer, code, documents[-1])
        self.assertEqual(backend.rows[0]["fromVersion"], 4)
        self.assertEqual(backend.rows[0]["trustListVersion"], 10)

    def test_one_bad_entry_anywhere_refuses_the_whole_chain_and_stores_nothing(self):
        documents = long_chain(32)
        for index in (0, 1, 15, 30, 31):
            with self.subTest(index=index):
                bad = json.loads(json.dumps(documents))
                # A countersignature that does not verify breaks rule A (check 8); the first list may
                # carry none at all.
                if index == 0:
                    bad[0]["countersignatures"] = [
                        {"deviceId": BASE_APPROVAL["deviceId"], "signature": "A" * 86}
                    ]
                else:
                    bad[index]["countersignatures"][0]["signature"] = "A" * 86
                env = self.chain_world(None)
                answer, code, backend = self.ask(env, *bad)
                self.assertEqual(answer["body"]["errorCode"], "chain_refused", answer)
                self.assertEqual(answer["body"]["chain"]["version"], index + 1)
                self.assertFalse((env.state / TRUST_NAME).exists(), "none of the chain is kept")
                self.assertEqual(backend.rows, [])
                self.assertEqual(backend.events, [])

    def test_entries_that_do_not_ascend_are_version_gap_and_not_bad_input(self):
        v1, v2, v3 = printed("v1", "v2", "v3")
        for held, documents, version in (
            (None, [v2], 2),
            (None, [v1, v1], 1),
            (None, [v1, v3], 3),
            (None, [v2, v1], 2),
            ("v1", [v1], 1),
            ("v1", [v3], 3),
            ("v2", [v1, v2], 1),
        ):
            with self.subTest(held=held, versions=[d["body"]["version"] for d in documents]):
                env = self.chain_world(held)
                answer, code, _ = self.ask(env, *documents)
                self.assertChainRefused(
                    answer,
                    code,
                    "version_gap",
                    None,
                    None if held is None else FIXTURE["lists"][held]["body"]["version"],
                )
                self.assertEqual(answer["body"]["chain"]["version"], version)

    def test_replaying_an_accepted_chain_is_version_gap(self):
        env = self.chain_world(None)
        documents = printed("v1", "v2")
        answer, code, _ = self.ask(env, *documents)
        self.assertEqual(code, 0)
        answer, code, _ = self.ask(env, *documents, now=later(60))
        self.assertChainRefused(answer, code, "version_gap", documents[0], 2)
        self.assertEqual(stored_bytes(env), compact(documents[-1]))

    def test_a_chain_whose_owner_key_is_not_the_pinned_one_is_refused(self):
        rogue = KEYS["rogue owner (not pinned)"]
        v1 = printed("v1")[0]
        rogue_pub = b64u(spki(rogue))
        # The list names the rogue key and is signed by it: check 3.
        body = json.loads(json.dumps(v1["body"]))
        body["ownerKey"] = rogue_pub
        document = resign(body, rogue)
        env = self.chain_world(None)
        answer, code, _ = self.ask(env, document)
        self.assertChainRefused(answer, code, "owner_key", document, None)
        # It names the pinned key but the rogue key signed it: check 4.
        body = json.loads(json.dumps(v1["body"]))
        document = resign(body, rogue)
        answer, code, _ = self.ask(env, document, now=later(60))
        self.assertChainRefused(answer, code, "owner_signature", document, None)
        # It is the pinned key's own signature on a body that names another key: check 3 first.
        body = json.loads(json.dumps(v1["body"]))
        body["ownerKey"] = rogue_pub
        document = resign(body, OWNER)
        answer, code, _ = self.ask(env, document, now=later(120))
        self.assertChainRefused(answer, code, "owner_key", document, None)
        self.assertFalse((env.state / TRUST_NAME).exists())

    def test_the_owner_signature_covers_the_list_prefix_and_the_body(self):
        # The pinned key's signature over the body WITHOUT the `muster-trust-list/1` line does not
        # verify: the prefix is part of what is signed (section 2).
        v1 = json.loads(json.dumps(printed("v1")[0]))
        v1["ownerSignature"] = b64u(sign(OWNER, compact(v1["body"])))
        answer, code, _ = self.ask(self.chain_world(None), v1)
        self.assertChainRefused(answer, code, "owner_signature", v1, None)

    def test_a_countersignature_uses_its_own_prefix_and_the_previous_lists_device(self):
        v1, v2 = printed("v1", "v2")
        # Signed over the body under the trust list prefix instead of the countersign prefix.
        wrong = json.loads(json.dumps(v2))
        wrong["countersignatures"] = [
            {
                "deviceId": BASE_APPROVAL["deviceId"],
                "signature": b64u(
                    sign(DEVICE_A, b"muster-trust-list/1\n" + compact(wrong["body"]))
                ),
            }
        ]
        answer, code, _ = self.ask(self.chain_world("v1"), wrong)
        self.assertChainRefused(answer, code, "countersignature", wrong, 1)
        # Signed with device A's REQUEST key (a key of the device that is not its approval key).
        request_key = KEYS["device A request"]
        wrong = json.loads(json.dumps(v2))
        wrong["countersignatures"] = [
            countersignature(request_key, BASE_APPROVAL["deviceId"], wrong["body"])
        ]
        answer, code, _ = self.ask(self.chain_world("v1"), wrong)
        self.assertChainRefused(answer, code, "countersignature", wrong, 1)
        # Two countersignatures, one good and one from a device that is not on the previous list.
        wrong = json.loads(json.dumps(v2))
        wrong["countersignatures"].append(
            countersignature(KEYS["device B approval"], DEVICE_B, wrong["body"])
        )
        answer, code, _ = self.ask(self.chain_world("v1"), wrong)
        self.assertChainRefused(answer, code, "countersignature", wrong, 1)
        # No countersignature at all.
        wrong = json.loads(json.dumps(v2))
        wrong["countersignatures"] = []
        answer, code, _ = self.ask(self.chain_world("v1"), wrong)
        self.assertChainRefused(answer, code, "countersignature", wrong, 1)
        # Two countersignatures naming one device: check 6, before the rule.
        twice = json.loads(json.dumps(v2))
        twice["countersignatures"] = twice["countersignatures"] * 2
        answer, code, _ = self.ask(self.chain_world("v1"), twice)
        self.assertChainRefused(answer, code, "countersignature", twice, 1)
        # And the good one is accepted.
        answer, code, _ = self.ask(self.chain_world("v1"), v2)
        self.assertEqual(code, 0)

    def test_the_countersigner_must_be_on_the_previous_list_not_this_one(self):
        # Device B is on the NEW list (v2) and not on the previous (v1): B's countersignature over v2
        # is valid and is still refused.
        v2 = json.loads(json.dumps(printed("v2")[0]))
        v2["countersignatures"] = [
            countersignature(KEYS["device B approval"], DEVICE_B, v2["body"])
        ]
        answer, code, _ = self.ask(self.chain_world("v1"), v2)
        self.assertChainRefused(answer, code, "countersignature", v2, 1)

    def test_a_closed_list_may_neither_carry_a_countersignature_nor_a_reopen_tag(self):
        v3 = json.loads(json.dumps(printed("v3")[0]))
        v3["countersignatures"] = printed("v2")[0]["countersignatures"]
        answer, code, _ = self.ask(self.chain_world("v2"), v3)
        self.assertChainRefused(answer, code, "closed_rule", v3, 2)
        body = json.loads(json.dumps(printed("v3")[0]["body"]))
        body["reopenTag"] = "a" * 64
        tagged = resign(body)
        answer, code, _ = self.ask(self.chain_world("v2"), tagged)
        self.assertChainRefused(answer, code, "closed_rule", tagged, 2)

    def test_the_first_list_may_not_be_closed_countersigned_or_tagged(self):
        v1 = printed("v1")[0]
        closed = json.loads(json.dumps(v1["body"]))
        closed["devices"] = []
        closed["closed"] = True
        tagged = json.loads(json.dumps(v1["body"]))
        tagged["reopenTag"] = "b" * 64
        for name, document in (
            ("closed", resign(closed)),
            ("countersigned", dict(v1, countersignatures=printed("v2")[0]["countersignatures"])),
            ("tagged", resign(tagged)),
        ):
            with self.subTest(case=name):
                answer, code, _ = self.ask(self.chain_world(None), document)
                self.assertChainRefused(answer, code, "first_rule", document, None)

    def test_a_normal_change_may_not_carry_a_reopen_tag(self):
        body = json.loads(json.dumps(printed("v2")[0]["body"]))
        body["reopenTag"] = "c" * 64
        document = resign(body)
        document["countersignatures"] = [
            countersignature(DEVICE_A, BASE_APPROVAL["deviceId"], body)
        ]
        answer, code, _ = self.ask(self.chain_world("v1"), document)
        self.assertChainRefused(answer, code, "first_rule", document, 1)

    def test_a_close_may_keep_or_remove_service_keys_and_never_swap_or_add_one(self):
        v2 = printed("v2")[0]
        base = json.loads(json.dumps(printed("v3")[0]["body"]))
        helper_pub = v2["body"]["services"]["serverHelper"]
        other_pub = b64u(spki(KEYS["rogue helper (not enrolled)"]))
        cases = (
            ("kept", {"serverHelper": helper_pub}, None),
            ("removed other", {"macPublisher": None}, None),
            ("swapped", {"serverHelper": other_pub}, "services_changed"),
            ("added", {"macPublisher": other_pub}, "services_changed"),
            ("both server keys removed", {"serverHelper": None}, "server_keys_removed"),
        )
        for name, services, reason in cases:
            with self.subTest(case=name):
                body = json.loads(json.dumps(base))
                body["services"].update(services)
                document = resign(body)
                env = self.chain_world("v2")
                answer, code, _ = self.ask(env, document)
                if reason is None:
                    self.assertEqual(code, 0, answer)
                else:
                    self.assertChainRefused(answer, code, reason, document, 2)

    def test_rule_a_may_not_leave_the_list_without_a_server_key_when_it_had_one(self):
        # Vector T25 is the printed case; these are its boundaries.
        helper_pub = printed("v2")[0]["body"]["services"]["serverHelper"]
        publisher_pub = b64u(spki(KEYS["rogue helper (not enrolled)"]))
        other_pub = b64u(spki(KEYS["server helper"]))

        def services(**named):
            result = dict.fromkeys(
                (
                    "macPublisher",
                    "macExecutor",
                    "macAgreement",
                    "serverPublisher",
                    "serverExecutor",
                    "serverExecutorAgreement",
                    "serverHelper",
                )
            )
            result.update(named)
            return result

        both = services(serverHelper=helper_pub, serverPublisher=publisher_pub)
        for name, held_services, new_services, refused in (
            (
                "helper swapped for a publisher",
                services(serverHelper=helper_pub),
                services(serverPublisher=publisher_pub),
                False,
            ),
            (
                "helper removed, publisher kept",
                both,
                services(serverPublisher=publisher_pub),
                False,
            ),
            ("publisher removed, helper kept", both, services(serverHelper=helper_pub), False),
            ("both removed", both, services(), True),
            ("helper removed, none else", services(serverHelper=helper_pub), services(), True),
            (
                "publisher removed, none else",
                services(serverPublisher=publisher_pub),
                services(),
                True,
            ),
            ("none before, none after", services(), services(), False),
            ("none before, one added", services(), services(serverHelper=helper_pub), False),
            (
                "other keys change freely",
                services(serverHelper=helper_pub),
                services(serverHelper=helper_pub, macPublisher=other_pub),
                False,
            ),
        ):
            with self.subTest(case=name):
                held = json.loads(json.dumps(printed("v1")[0]["body"]))
                held["services"] = held_services
                held_doc = resign(held)
                step = json.loads(json.dumps(held))
                step["version"] = 2
                step["previousSHA256"] = body_sha256(held_doc)
                step["services"] = new_services
                document = resign(step)
                document["countersignatures"] = [
                    countersignature(DEVICE_A, BASE_APPROVAL["deviceId"], step)
                ]
                env = self.chain_world(held_doc)
                answer, code, _ = self.ask(env, document)
                if refused:
                    self.assertChainRefused(answer, code, "server_keys_removed", document, 1)
                else:
                    self.assertEqual(code, 0, answer)

    def test_a_list_may_hold_sixty_four_devices(self):
        body = json.loads(json.dumps(printed("v1")[0]["body"]))
        first = body["devices"][0]
        body["devices"] = [first] + [
            dict(first, deviceId="d-%024x" % (n + 1), name="Phone %d" % n) for n in range(63)
        ]
        document = resign(body)
        env = self.chain_world(None)
        answer, code, _ = self.ask(env, document)
        self.assertAccepted(env, answer, code, document)

    def test_the_check_order_is_the_specs_when_several_checks_fail_at_once(self):
        # Each pair breaks two checks at once; the earlier one in section 8's order is the answer.
        v1, v2 = printed("v1", "v2")

        def tamper(document, **body_changes):
            body = json.loads(json.dumps(document["body"]))
            body.update(body_changes)
            return resign(body), body

        # version_gap (1) before previous_hash (2)
        document, _ = tamper(v2, version=5, previousSHA256="0" * 64)
        self.assertEqual(self.reason_of("v1", document)[0], "version_gap")
        # previous_hash (2) before owner_key (3)
        document, _ = tamper(
            v2, previousSHA256="0" * 64, ownerKey=b64u(spki(KEYS["rogue owner (not pinned)"]))
        )
        self.assertEqual(self.reason_of("v1", document)[0], "previous_hash")
        # owner_key (3) before owner_signature (4)
        document, _ = tamper(v2, ownerKey=b64u(spki(KEYS["rogue owner (not pinned)"])))
        document["ownerSignature"] = b64u(sign(KEYS["rogue owner (not pinned)"], b"x"))
        self.assertEqual(self.reason_of("v1", document)[0], "owner_key")
        # owner_signature (4) before duplicate_device (5)
        document, body = tamper(v2, devices=v2["body"]["devices"] + v2["body"]["devices"][:1])
        document["ownerSignature"] = b64u(sign(OWNER, b"x"))
        self.assertEqual(self.reason_of("v1", document)[0], "owner_signature")
        # duplicate_device (5) before countersignature (6)
        document, body = tamper(v2, devices=v2["body"]["devices"] + v2["body"]["devices"][:1])
        document["countersignatures"] = v2["countersignatures"] * 2
        self.assertEqual(self.reason_of("v1", document)[0], "duplicate_device")
        # countersignature (6) before closed_rule (7)
        document, body = tamper(v2, closed=True)
        document["countersignatures"] = v2["countersignatures"] * 2
        self.assertEqual(self.reason_of("v1", document)[0], "countersignature")
        # closed_rule (7) before the rule (8)
        document, body = tamper(v2, closed=True)
        self.assertEqual(self.reason_of("v1", document)[0], "closed_rule")

    def test_a_chain_of_entries_walks_each_against_the_one_before_it(self):
        # v3 closes the list v2 and must be judged against v2's services and server keys, not the held v1.
        env = self.chain_world("v1")
        answer, code, _ = self.ask(env, *printed("v2", "v3"))
        self.assertAccepted(env, answer, code, printed("v3")[0])
        self.assertEqual(answer["body"]["closed"], True)

    def test_a_close_after_a_close_is_rule_b_and_not_rule_c(self):
        # A second close on a closed list is rule B (this list is closed), not rule C.
        v3 = printed("v3")[0]
        body = json.loads(json.dumps(v3["body"]))
        body["version"] = 4
        body["previousSHA256"] = body_sha256(v3)
        body["issuedAt"] = "2026-11-07T21:04:00Z"
        document = resign(body)
        env = self.chain_world("v3")
        answer, code, _ = self.ask(env, document)
        self.assertAccepted(env, answer, code, document)

    def test_rotation_the_old_key_signs_up_to_its_line_and_the_new_key_after_it(self):
        v1 = printed("v1")[0]
        new_owner = KEYS[ROTATED_OWNER]
        new_pub = b64u(spki(new_owner))
        old_pub = b64u(spki(OWNER))
        # Under the rotated history version 1 is the old key's and must hash to its line's toSHA256:
        # the real v1 is accepted, and its acknowledgement says it is not signed by the current key.
        env = self.chain_world(None, rotated=True)
        answer, code, _ = self.ask(env, v1)
        self.assertAccepted(env, answer, code, v1, current=False)
        self.assertFalse(answer["body"]["signedByCurrentKey"])
        # An approval is refused by the pre-rotation list (section 10): `no_trust_list`.
        approval_answer, approval_code, _ = module_request(
            env, COMMAND, stdin_of(approval()), "2026-11-07T21:00:30Z"
        )
        self.assertRefused(approval_answer, approval_code, "no_trust_list", verified=False)
        self.assertEqual(approval_answer["body"]["helperTrustListVersion"], 1)
        # Version 2 signed by the NEW key and naming it: accepted, and signed by the current key.
        v2 = next_rule_a(v1, owner=new_owner, ownerKey=new_pub)
        answer, code, _ = self.ask(env, v2, now=later(60))
        self.assertAccepted(env, answer, code, v2, current=True, at=ma.format_time(later(60)))
        approval_answer, approval_code, _ = module_request(
            env, COMMAND, stdin_of(approval()), "2026-11-07T21:00:30Z"
        )
        self.assertDone(approval_answer, approval_code)
        # Version 3 signed by the RETIRED key is refused, and so is one that names it.
        for step, (signer, named) in enumerate(
            ((OWNER, old_pub), (OWNER, new_pub), (new_owner, old_pub))
        ):
            with self.subTest(
                signed_by="old" if signer is OWNER else "new", names=named == old_pub
            ):
                v3 = next_rule_a(v2, owner=signer, ownerKey=named)
                answer, code, _ = self.ask(env, v3, now=later(120 + 20 * step))
                expected = "owner_key" if named == old_pub else "owner_signature"
                self.assertChainRefused(answer, code, expected, v3, 2)

    def test_a_chain_across_the_rotation_walks_both_keys(self):
        v1 = printed("v1")[0]
        new_owner = KEYS[ROTATED_OWNER]
        v2 = next_rule_a(v1, owner=new_owner, ownerKey=b64u(spki(new_owner)))
        env = self.chain_world(None, rotated=True)
        answer, code, _ = self.ask(env, v1, v2)
        self.assertAccepted(env, answer, code, v2, current=True)
        # The retired key cannot take version 2.
        bad = next_rule_a(v1)
        env = self.chain_world(None, rotated=True)
        answer, code, _ = self.ask(env, v1, bad)
        self.assertChainRefused(answer, code, "owner_key", bad, None)
        self.assertFalse((env.state / TRUST_NAME).exists())

    def test_the_held_list_at_a_rotation_line_must_hash_to_it_or_the_helper_is_state_unsafe(self):
        # T24's list held under the rotated history: version 1 with a body hash its line did not name.
        env = self.chain_world("T24 list", rotated=True)
        answer, code, backend = self.ask(env, *printed("v1"))
        self.assertOtherRefusal(answer, code, "state_unsafe", None)
        self.assertEqual(backend.events, [], "refused at C2, before the lock")
        # The same list held under the ordinary history is just a held version 1.
        env = self.chain_world("T24 list")
        answer, code, _ = self.ask(env, printed("v2")[0])
        self.assertRefused(answer, code, "chain_refused", verified=False, reason="previous_hash")
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
        # The real v1 held under the rotated history is fine (its hash is the line's).
        env = self.chain_world("v1", rotated=True)
        new_owner = KEYS[ROTATED_OWNER]
        v2 = next_rule_a(printed("v1")[0], owner=new_owner, ownerKey=b64u(spki(new_owner)))
        answer, code, _ = self.ask(env, v2)
        self.assertAccepted(env, answer, code, v2)

    def test_the_hash_of_the_body_and_not_of_the_signatures_is_what_the_history_names(self):
        # The history line names the body hash.  A copy of version 1 with different countersignatures
        # or another (valid) owner signature has the same body hash and is accepted at version 1.
        v1 = json.loads(json.dumps(printed("v1")[0]))
        v1["ownerSignature"] = b64u(
            OWNER.sign(b"muster-trust-list/1\n" + compact(v1["body"]), ec.ECDSA(hashes.SHA256()))
        )
        env = self.chain_world(None, rotated=True)
        answer, code, _ = self.ask(env, v1)
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["body"]["trustListSHA256"], body_sha256(printed("v1")[0]))
        self.assertEqual(stored_bytes(env), compact(v1))


# MARK: - P3 for a chain: standard input


def with_change(document, path, value):
    """A deep copy of `document` with the value at `path` (a list of keys and indexes) replaced."""
    copy = json.loads(json.dumps(document))
    target = copy
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return copy


def without(document, path):
    copy = json.loads(json.dumps(document))
    target = copy
    for step in path[:-1]:
        target = target[step]
    del target[path[-1]]
    return copy


class ChainInputTests(ChainChecks):
    """A chain document that is malformed in any way is `bad_input` at P3: before the rate step, the
    list, the lock or anything that could be written.  Formatting does not matter, but structure does."""

    def refuse(self, raw, expected="bad_input", held="v1"):
        env = self.chain_world(held)
        before = stored_bytes(env)
        answer, code, backend = self.ask(env, raw=raw)
        self.assertOtherRefusal(answer, code, expected)
        self.assertEqual(backend.events, [])
        self.assertEqual(backend.rows, [])
        self.assertFalse(
            (env.state / "muster-rate.json").exists(), "refused while parsing is not counted"
        )
        self.assertEqual(stored_bytes(env), before)
        return answer

    def refuse_entry(self, entry, label, held="v1"):
        with self.subTest(case=label):
            self.refuse(chain_stdin(entry), held=held)

    V2 = FIXTURE["lists"]["v2"]

    def test_every_body_field_is_required(self):
        for name in self.V2["body"]:
            self.refuse_entry(without(self.V2, ["body", name]), "missing " + name)

    def test_every_entry_key_is_required_and_no_other_is_allowed(self):
        for name in ("body", "ownerSignature", "countersignatures"):
            self.refuse_entry(without(self.V2, [name]), "missing " + name)
        self.refuse_entry(dict(self.V2, extra=1), "an extra entry key")
        self.refuse_entry(with_change(self.V2, ["body", "extra"], 1), "an extra body key")
        self.refuse_entry(
            with_change(self.V2, ["body", "devices", 0, "extra"], 1), "an extra device key"
        )
        self.refuse_entry(
            with_change(self.V2, ["body", "services", "extra"], None), "an extra service"
        )
        self.refuse_entry(
            with_change(self.V2, ["countersignatures", 0, "extra"], 1), "an extra cs key"
        )

    def test_every_nested_key_is_required(self):
        for name in self.V2["body"]["devices"][0]:
            self.refuse_entry(without(self.V2, ["body", "devices", 0, name]), "device " + name)
        for name in self.V2["body"]["services"]:
            self.refuse_entry(without(self.V2, ["body", "services", name]), "service " + name)
        for name in self.V2["countersignatures"][0]:
            self.refuse_entry(without(self.V2, ["countersignatures", 0, name]), "cs " + name)

    BAD_VALUES = (
        (["body", "contract"], ["muster-trust-list/2", "", 5, None, ["muster-trust-list/1"]]),
        (["body", "version"], [0, -1, True, False, "2", None, [2], {"v": 2}, 2**53]),
        (
            ["body", "previousSHA256"],
            ["xyz", "A" * 64, "a" * 63, "a" * 65, 5, "", True, ["a" * 64]],
        ),
        (
            ["body", "issuedAt"],
            [
                "2026-13-01T00:00:00Z",
                "2026-02-30T00:00:00Z",
                "2026-11-07 20:00:00",
                "2026-11-07T20:00:00+00:00",
                "2026-11-07T20:00:00z",
                "",
                5,
                None,
            ],
        ),
        (["body", "ownerKey"], ["AAAA", "", 5, None, "A" * 201, "!!!!", [], ["x"]]),
        (["body", "closed"], [0, 1, "false", "true", None, [], {}]),
        (["body", "devices"], [None, {}, "x", 5, True]),
        (["body", "services"], [None, [], "x", 5]),
        (["body", "reopenTag"], ["xyz", "A" * 64, "a" * 63, 5, "", True]),
        (["ownerSignature"], ["", "A" * 101, "AA=", 5, None, "AA AA", ["A"], "Aé"]),
        (["countersignatures"], [None, {}, "x", 5]),
        (["body", "devices", 0], [None, 5, "x", [], []]),
        (
            ["body", "devices", 0, "deviceId"],
            ["d-1", "D-" + "A" * 24, "d-" + "a" * 23, "d-" + "a" * 25, 5, None, ""],
        ),
        (["body", "devices", 0, "name"], ["", "x" * 41, 5, None, "café", "tab\there"]),
        (["body", "devices", 0, "approvalKey"], ["AAAA", "", 5, None, "x" * 201]),
        (["body", "devices", 0, "requestKey"], ["AAAA", "", 5, None]),
        (["body", "devices", 0, "readingKey"], ["AAAA", "", 5, None]),
        (["body", "devices", 0, "notificationKey"], ["AAAA", "", 5, None]),
        (["body", "services", "serverHelper"], ["AAAA", "", 5, True, []]),
        (["body", "services", "macPublisher"], ["AAAA", "", 5, True, []]),
        (["countersignatures", 0], [None, 5, "x", []]),
        (["countersignatures", 0, "deviceId"], ["d-1", "D-" + "A" * 24, 5, None, ""]),
        (["countersignatures", 0, "signature"], ["", "A" * 101, "AA=", 5, None, "AA AA"]),
    )

    def test_every_field_has_its_type_and_pattern(self):
        for path, values in self.BAD_VALUES:
            for value in values:
                self.refuse_entry(
                    with_change(self.V2, path, value), "%s = %r" % ("/".join(map(str, path)), value)
                )

    def test_a_key_must_be_a_p256_key_so_that_an_accepted_list_can_always_be_read_back(self):
        # A string that looks like base64url is not enough: the helper stores the list, and its own
        # reader holds every key of it to be a P-256 key (section 2).  A chain it could not read back
        # would wedge the route.  The Ed25519 key below is well formed, a valid key, and not P-256.
        not_p256 = "MCowBQYDK2VwAyEA" + "A" * 43
        for path in (
            ["body", "ownerKey"],
            ["body", "devices", 0, "approvalKey"],
            ["body", "services", "serverHelper"],
        ):
            self.refuse_entry(with_change(self.V2, path, not_p256), "/".join(map(str, path)))

    def test_sixty_five_devices_and_sixty_five_countersignatures_are_too_many(self):
        first = self.V2["body"]["devices"][0]
        many = [dict(first, deviceId="d-%024x" % n) for n in range(65)]
        self.refuse_entry(with_change(self.V2, ["body", "devices"], many), "65 devices")
        many_cs = [{"deviceId": "d-%024x" % n, "signature": "A" * 86} for n in range(65)]
        self.refuse_entry(
            with_change(self.V2, ["countersignatures"], many_cs), "65 countersignatures"
        )
        # Sixty-four are the most: these reach the walk and are refused there for their own reason.
        env = self.chain_world("v1")
        answer, code, _ = self.ask(
            env, with_change(self.V2, ["countersignatures"], many_cs[:64]), now=later(60)
        )
        self.assertEqual(answer["body"]["errorCode"], "chain_refused", answer)

    def test_a_chain_holds_one_to_thirty_two_entries(self):
        self.refuse(chain_stdin(), "bad_input")
        documents = long_chain(33)
        self.assertEqual(len(documents), 33)
        self.refuse(chain_stdin(*documents), "bad_input", held=None)
        # Thirty-three entries are bad_input whatever they are: even thirty-three copies of one.
        self.refuse(chain_stdin(*([FIXTURE["lists"]["v1"]] * 33)), "bad_input")

    def test_the_document_is_exactly_protocol_and_chain(self):
        good = json.loads(chain_stdin(self.V2))
        for name, document in (
            ("no protocol", {"chain": good["chain"]}),
            ("no chain", {"protocol": "muster-helper/1"}),
            ("an extra key", dict(good, extra=1)),
            ("another protocol", dict(good, protocol="muster-helper/2")),
            ("a protocol that is not text", dict(good, protocol=1)),
            ("a chain that is null", dict(good, chain=None)),
            ("a chain that is an object", dict(good, chain=good["chain"][0])),
            ("a chain that is a string", dict(good, chain="x")),
            ("an approval instead", {"protocol": "muster-helper/1", "approval": {}}),
            ("a job instead", {"protocol": "muster-helper/1", "job": {}}),
            ("an array", [good]),
            ("a number", 5),
            ("null", None),
        ):
            with self.subTest(case=name):
                self.refuse(compact(document))
        self.refuse(compact({"protocol": "muster-helper/1", "chain": [None]}))
        self.refuse(compact({"protocol": "muster-helper/1", "chain": [[]]}))
        self.refuse(compact({"protocol": "muster-helper/1", "chain": [5, "x"]}))

    def test_hostile_json_is_bad_input(self):
        base = chain_stdin(self.V2)
        text = base.decode()
        for name, raw in (
            ("empty", b""),
            ("whitespace only", b" \n\t "),
            ("not JSON", b"muster"),
            ("truncated", base[: len(base) // 2]),
            ("trailing garbage", base + b"x"),
            ("two documents", base + base),
            ("a byte order mark", b"\xef\xbb\xbf" + base),
            ("not UTF-8", base.replace(b'"chain"', b'"ch\xffin"')),
            ("overlong UTF-8", b'{"protocol":"muster-helper/1","chain":[],"\xc0\xaf":1}'),
            ("a lone surrogate", text.replace('"chain"', '"ch\\ud800in"').encode()),
            ("a non-ASCII string", text.replace("Test Phone A", "Tést Phone A").encode("utf-8")),
            ("an escaped control character", text.replace("Test Phone A", "T\\u0007st").encode()),
            ("a NUL", base.replace(b"Test Phone A", b"Test\x00Phone")),
            ("NaN", base.replace(b'"version":2', b'"version":NaN')),
            ("Infinity", base.replace(b'"version":2', b'"version":Infinity')),
            ("-Infinity", base.replace(b'"version":2', b'"version":-Infinity')),
            ("a float", base.replace(b'"version":2', b'"version":2.0')),
            ("an exponent", base.replace(b'"version":2', b'"version":2e0')),
            ("a huge integer", base.replace(b'"version":2', b'"version":' + b"9" * 400)),
            ("an integer past 2^53", base.replace(b'"version":2', b'"version":9007199254740993')),
            ("a duplicate top-level key", base[:-1] + b',"protocol":"muster-helper/1"}'),
            ("a duplicate body key", base.replace(b'"version":2', b'"version":2,"version":2')),
            (
                "a duplicate device key",
                base.replace(b'"name":"Test Phone A"', b'"name":"Test Phone A","name":"x"'),
            ),
            (
                "a duplicate escaped key",
                base.replace(b'"version":2', b'"version":2,"v\\u0065rsion":2'),
            ),
            (
                "a depth bomb",
                b'{"protocol":"muster-helper/1","chain":' + b"[" * 20000 + b"]" * 20000 + b"}",
            ),
            (
                "an object depth bomb",
                b'{"protocol":"muster-helper/1","chain":['
                + b'{"a":' * 9000
                + b"1"
                + b"}" * 9000
                + b"]}",
            ),
            ("single quotes", base.replace(b'"', b"'")),
            ("comments", b"/* x */" + base),
            ("a trailing comma", base[:-2] + b",]}"),
        ):
            with self.subTest(case=name):
                self.refuse(raw)

    def test_formatting_and_key_order_do_not_matter(self):
        # Section 5: the helper works from the parsed values, and computes canonical bytes itself.
        v2 = self.V2
        reversed_keys = json.dumps(
            {"protocol": "muster-helper/1", "chain": [v2]}, indent=4, sort_keys=True
        )
        for name, raw in (
            ("indented and sorted", reversed_keys.encode()),
            ("with CRLF", reversed_keys.replace("\n", "\r\n").encode()),
            ("with tabs", reversed_keys.replace("    ", "\t").encode()),
            ("an escaped key", chain_stdin(v2).replace(b'"version"', b'"v\\u0065rsion"', 1)),
            ("an escaped value", chain_stdin(v2).replace(b"Test Phone A", b"Test \\u0050hone A")),
            ("padded", b"\n  " + chain_stdin(v2) + b"  \n"),
        ):
            with self.subTest(case=name):
                env = self.chain_world("v1")
                answer, code, _ = self.ask(env, raw=raw)
                self.assertEqual(code, 0, (name, answer))
                self.assertEqual(answer["kind"], "ack")
                # What is stored is the canonical bytes of what was parsed, whatever was sent.
                parsed = json.loads(raw)["chain"][0]
                self.assertEqual(stored_bytes(env), ma.canonical_bytes(parsed))

    def test_the_input_limit_is_65536_bytes_for_a_chain_and_4096_for_everything_else(self):
        raw = chain_stdin(self.V2)
        for size, expected in ((65536, None), (65537, "input_too_large")):
            with self.subTest(size=size):
                env = self.chain_world("v1")
                padded = raw + b" " * (size - len(raw))
                self.assertEqual(len(padded), size)
                answer, code, backend = self.ask(env, raw=padded)
                if expected is None:
                    self.assertEqual(code, 0, answer)
                else:
                    self.assertOtherRefusal(answer, code, expected)
                    self.assertEqual(backend.events, [])
                    self.assertFalse((env.state / "muster-rate.json").exists())
        # The other commands keep their 4,096: a plan, an acknowledgement and an approval.
        env = self.chain_world("v1")
        for command, document in (
            (PLAN_COMMAND, job_stdin()),
            (CURRENT_COMMAND, CURRENT_STDIN),
            (COMMAND, stdin_of(approval())),
        ):
            with self.subTest(command=command):
                for size, expected in ((4096, None), (4097, "input_too_large")):
                    padded = document + b" " * (size - len(document))
                    answer, code, _ = module_request(env, command, padded, later(100 * size))
                    if expected:
                        self.assertRefused(answer, code, expected, verified=False)
                    else:
                        self.assertNotEqual(answer["body"].get("errorCode"), "input_too_large")

    def test_the_helper_reads_at_most_the_limit_plus_one_byte(self):
        env = self.chain_world("v1")
        seen = []

        def read(limit):
            seen.append(limit)
            return (b" " * (limit + 5))[: limit + 1]

        answer, code = ma.handle_request(
            env.module_config(CHAIN_NOW), FakeBackend(), CHAIN_COMMAND, True, read, mt
        )
        self.assertEqual(seen, [65536])
        self.assertOtherRefusal(answer, code, "input_too_large")
        seen.clear()
        ma.handle_request(env.module_config(CHAIN_NOW), FakeBackend(), COMMAND, True, read, mt)
        ma.handle_request(env.module_config(CHAIN_NOW), FakeBackend(), PLAN_COMMAND, True, read, mt)
        self.assertEqual(seen, [4096, 4096])

    def test_a_stdin_that_cannot_be_read_is_bad_input(self):
        env = self.chain_world("v1")

        def broken(limit):
            raise OSError("closed")

        answer, code = ma.handle_request(
            env.module_config(CHAIN_NOW), FakeBackend(), CHAIN_COMMAND, True, broken, mt
        )
        self.assertOtherRefusal(answer, code, "bad_input")

    def test_the_helper_entry_reads_a_chain_of_up_to_65536_bytes_from_a_pipe(self):
        # The real reader (`read_stdin_limited`) in a real process: a padded chain of exactly 65,536
        # bytes is accepted and one byte more is not.
        raw = chain_stdin(self.V2)
        for size, expected in ((65536, "ack"), (65537, "input_too_large")):
            with self.subTest(size=size):
                env = self.chain_world("v1")
                answer, code, _, _ = subprocess_request(
                    self, env, CHAIN_COMMAND, raw + b" " * (size - len(raw)), CHAIN_NOW
                )
                self.assertEqual(answer["kind"], "ack" if expected == "ack" else "refusal")
                if expected != "ack":
                    self.assertEqual(answer["body"]["errorCode"], expected)

    def test_a_list_too_large_for_the_stored_file_is_bad_input(self):
        # The stored file may be 65,536 bytes at most (section 11); a list whose canonical bytes
        # exceed that could be accepted and then never read.  It is refused instead.
        v1 = FIXTURE["lists"]["v1"]
        with mock.patch.object(ma, "TRUST_FILE_MAX_BYTES", len(compact(v1)) - 1):
            self.refuse(chain_stdin(v1), held=None)
        with mock.patch.object(ma, "TRUST_FILE_MAX_BYTES", len(compact(v1))):
            env = self.chain_world(None)
            answer, code, _ = self.ask(env, v1)
            self.assertEqual(code, 0, answer)
            self.assertEqual(len(stored_bytes(env)), ma.TRUST_FILE_MAX_BYTES)
            # And what was stored is read back: the route answers about it.
            answer, code, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, later(100))
            self.assertEqual(code, 0, answer)


class ChainCommandTests(ChainChecks):
    V2 = FIXTURE["lists"]["v2"]

    def test_only_the_yes_mode_of_chain_exists(self):
        env = self.chain_world("v1")
        before = stored_bytes(env)
        for command, expected in (
            (CHAIN_PLAN_COMMAND, "mode_not_allowed"),
            ("muster trust-list chain", "bad_command"),
            ("muster trust-list chain --json", "bad_command"),
            ("muster trust-list chain --YES", "bad_command"),
            ("muster trust-list chain now --yes", "bad_command"),
            ("muster trust-list chain-list --yes", "bad_command"),
            ("muster trust-list Chain --yes", "bad_command"),
            ("muster trust-list chain  --yes", "bad_command"),
            (" muster trust-list chain --yes", "bad_command"),
            ("muster trust-list chain --yes ", "bad_command"),
            ("server trust-list chain --yes", "bad_command"),
            ("muster trust-list current --yes", "mode_not_allowed"),
            ("mac trust-list chain --yes", "bad_command"),
        ):
            with self.subTest(command=command):
                answer, code, backend = module_request(
                    env, command, chain_stdin(self.V2), CHAIN_NOW
                )
                self.assertRefused(answer, code, expected, verified=False)
                self.assertEqual(answer["body"]["claimed"], {})
                self.assertNotIn("chain", answer["body"])
                self.assertEqual(backend.events, [])
                self.assertFalse((env.state / "muster-rate.json").exists())
        self.assertEqual(stored_bytes(env), before)

    def test_chain_is_refused_with_the_extra_argument_of_the_forced_command(self):
        env = self.chain_world("v1")
        answer, code, _ = module_request(
            env, CHAIN_COMMAND, chain_stdin(self.V2), CHAIN_NOW, argv_ok=False
        )
        self.assertRefused(answer, code, "bad_command", verified=False)

    def test_the_pin_and_the_key_are_checked_before_the_chain_is_read(self):
        # P0 and P1 come before anything is read from standard input.
        def must_not_read(limit):
            raise AssertionError("standard input was read")

        env = self.chain_world("v1", key=False)
        answer, code = ma.handle_request(
            env.module_config(CHAIN_NOW), FakeBackend(), CHAIN_COMMAND, True, must_not_read, mt
        )
        self.assertEqual(code, 2)
        self.assertEqual(answer["body"]["errorCode"], "helper_key_missing")
        self.assertIsNone(answer["signature"])
        env = self.chain_world("v1")
        os.chmod(env.etc / "owner.pub", 0o666)
        answer, code = ma.handle_request(
            env.module_config(CHAIN_NOW), FakeBackend(), CHAIN_COMMAND, True, must_not_read, mt
        )
        self.assertRefused(answer, code, "pin_unsafe", verified=False)
        self.assertNotIn("chain", answer["body"], "the command was not parsed yet")
        # P2 comes before P3 too: a bad command never reads its input.
        env = self.chain_world("v1")
        answer, code = ma.handle_request(
            env.module_config(CHAIN_NOW), FakeBackend(), CHAIN_PLAN_COMMAND, True, must_not_read, mt
        )
        self.assertRefused(answer, code, "mode_not_allowed", verified=False)

    def test_a_pin_that_cannot_be_trusted_refuses_the_chain_and_stores_nothing(self):
        for name, damage in (
            ("owner.pub group-writable", lambda env: os.chmod(env.etc / "owner.pub", 0o664)),
            ("history missing", lambda env: os.unlink(env.etc / "owner-history.jsonl")),
            ("history empty", lambda env: write_file(env.etc / "owner-history.jsonl", b"", 0o644)),
        ):
            with self.subTest(case=name):
                env = self.chain_world(None)
                damage(env)
                answer, code, backend = self.ask(env, FIXTURE["lists"]["v1"])
                self.assertRefused(answer, code, "pin_unsafe", verified=False)
                self.assertFalse((env.state / TRUST_NAME).exists())
                self.assertFalse((env.state / "muster-rate.json").exists(), "not counted")
                self.assertEqual(backend.events, [])


# MARK: - C1 to C5: the rate step, the stored list, the lock and the write


class ChainStateTests(ChainChecks):
    """C2: the stored list is read without a lock, and anything wrong with it is `state_unsafe`
    (reported with no version, since nothing could be read), counted by the rate step that came first."""

    V1 = FIXTURE["lists"]["v1"]

    def damage(self, env, name):
        path = env.state / TRUST_NAME
        v1 = self.V1
        if name == "symbolic link":
            victim = env.root / "victim"
            write_file(victim, b"precious", 0o600)
            os.symlink(victim, path)
        elif name == "empty":
            write_file(path, b"", 0o600)
        elif name == "hard link":
            env.store(v1)
            os.link(path, env.root / "twin")
        elif name == "mode 0644":
            env.store(v1, mode=0o644)
        elif name == "mode 0640":
            env.store(v1, mode=0o640)
        elif name == "oversize":
            write_file(path, json.dumps(v1).encode() + b" " * 70000, 0o600)
        elif name == "one byte over the limit":
            raw = json.dumps(v1).encode()
            write_file(path, raw + b" " * (65537 - len(raw)), 0o600)
        elif name == "garbage":
            write_file(path, b"not json", 0o600)
        elif name == "a list in an array":
            write_file(path, json.dumps([v1]).encode(), 0o600)
        elif name == "directory":
            os.mkdir(path, 0o700)
        elif name == "fifo":
            os.mkfifo(path, 0o600)
        elif name == "two devices with one id":
            body = json.loads(json.dumps(v1["body"]))
            body["devices"] = body["devices"] * 2
            env.store(resign(body))
        elif name == "closed with a device":
            body = json.loads(json.dumps(v1["body"]))
            body["closed"] = True
            env.store(resign(body))
        elif name == "two countersignatures for one device":
            env.store(
                dict(
                    FIXTURE["lists"]["v2"],
                    countersignatures=FIXTURE["lists"]["v2"]["countersignatures"] * 2,
                )
            )
        elif name == "version zero":
            body = json.loads(json.dumps(v1["body"]))
            body["version"] = 0
            env.store(resign(body))
        elif name == "a key that is not P-256":
            body = json.loads(json.dumps(v1["body"]))
            body["services"]["serverHelper"] = "MCowBQYDK2VwAyEA" + "A" * 43
            env.store(resign(body))
        else:
            raise AssertionError(name)

    NAMES = (
        "symbolic link",
        "empty",
        "hard link",
        "mode 0644",
        "mode 0640",
        "oversize",
        "one byte over the limit",
        "garbage",
        "a list in an array",
        "directory",
        "fifo",
        "two devices with one id",
        "closed with a device",
        "two countersignatures for one device",
        "version zero",
        "a key that is not P-256",
    )

    def test_a_stored_list_that_cannot_be_trusted_is_state_unsafe_and_nothing_is_written(self):
        for name in self.NAMES:
            with self.subTest(case=name):
                env = self.chain_world(None)
                self.damage(env, name)
                names_before = sorted(p.name for p in env.state.iterdir())
                # A regression that waits on the FIFO fails here after 15 seconds instead of hanging.
                answer, code, backend = self.run_within(
                    15,
                    lambda: self.ask(env, FIXTURE["lists"]["v2"]),
                    unblock=lambda: self.release_fifo(env.state / TRUST_NAME),
                )
                self.assertOtherRefusal(answer, code, "state_unsafe", None)
                self.assertEqual(backend.events, [], "refused at C2, before the lock")
                self.assertEqual(backend.rows, [])
                self.assertEqual(leftovers(env), [])
                after = sorted(p.name for p in env.state.iterdir())
                # Only the rate step's files appeared: it came first and counted the request.
                self.assertEqual(
                    sorted(set(after) - set(names_before)), ["muster-rate.json", "muster-rate.lock"]
                )
                events = json.loads((env.state / "muster-rate.json").read_text())["events"]
                self.assertEqual([event[:2] for event in events], [["chain", "chain"]])
                if name == "symbolic link":
                    self.assertEqual((env.root / "victim").read_bytes(), b"precious")

    def test_a_stored_list_owned_by_another_account_is_state_unsafe(self):
        env = self.chain_world("v1")
        with self.foreign_owner(env.state / TRUST_NAME):
            answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"])
        self.assertOtherRefusal(answer, code, "state_unsafe", None)

    def test_a_stored_list_of_exactly_65536_bytes_is_read(self):
        env = self.chain_world(None)
        raw = json.dumps(self.V1).encode()
        write_file(env.state / TRUST_NAME, raw + b" " * (65536 - len(raw)), 0o600)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"])
        self.assertEqual(code, 0, answer)

    def test_a_stored_list_in_any_formatting_is_read_and_replaced_by_canonical_bytes(self):
        v1, v2 = self.V1, FIXTURE["lists"]["v2"]
        for name, raw in (
            ("indented with a line feed", (json.dumps(v1, indent=2) + "\n").encode()),
            ("keys in reverse order", json.dumps(reversed_keys(v1), indent=1).encode()),
            ("spaced", json.dumps(v1).encode()),
            ("crlf", json.dumps(v1, indent=2).replace("\n", "\r\n").encode() + b"\r\n"),
        ):
            with self.subTest(case=name):
                env = self.chain_world(None)
                write_file(env.state / TRUST_NAME, raw, 0o600)
                answer, code, _ = self.ask(env, v2)
                self.assertAccepted(env, answer, code, v2)

    def test_a_rate_limit_comes_before_the_stored_list_is_read(self):
        env = self.chain_world(None)
        self.damage(env, "garbage")
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"])
        self.assertOtherRefusal(answer, code, "state_unsafe", None)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"], now=epoch(CHAIN_NOW) + 5)
        self.assertOtherRefusal(answer, code, "rate_limited", None)

    def test_a_chain_that_is_refused_for_its_content_still_counts(self):
        env = self.chain_world("v1")
        answer, code, _ = self.ask(env, FIXTURE["lists"]["T7 list"])
        self.assertChainRefused(answer, code, "previous_hash", FIXTURE["lists"]["T7 list"], 1)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"], now=epoch(CHAIN_NOW) + 5)
        self.assertOtherRefusal(answer, code, "rate_limited", None)


class OrderBackend(FakeBackend):
    """A backend that notes what the stored list held when the audit row was asked for."""

    def __init__(self, env, **kwargs):
        super().__init__(**kwargs)
        self.env = env
        self.held_at_audit = "never"

    def audit(self, scope, verb, target, result, fields):
        self.events.append("audit")
        self.held_at_audit = stored_bytes(self.env)
        return super().audit(scope, verb, target, result, fields)


class ChainLockWindowTests(ChainChecks):
    """C4: the action lock, then the stored list again, and `trust_list_changed` unless it is the list
    C2 read (its body hash, and a list held exactly when C2 found one)."""

    V1 = FIXTURE["lists"]["v1"]
    V2 = FIXTURE["lists"]["v2"]

    def changed(self, held, document, between, version, *, accepted=False):
        env = self.chain_world(held)
        before = stored_bytes(env)
        backend = FakeBackend(on_lock=lambda: between(env))
        answer, code, backend = self.ask(env, document, backend=backend)
        if accepted:
            return env, answer, code, backend
        self.assertOtherRefusal(answer, code, "trust_list_changed", version)
        self.assertEqual(backend.rows, [], "nothing was written, so there is no row")
        self.assertEqual(backend.events, ["lock", "unlock"])
        self.assertEqual(leftovers(env), [])
        return env, answer, code, before

    def test_the_stored_list_replaced_between_c2_and_c4(self):
        env, _, _, _ = self.changed("v1", self.V2, lambda env: env.store(FIXTURE["lists"]["v3"]), 1)
        # The replacement is what is on disk afterwards: the chain did not overwrite it.
        self.assertEqual(json.loads(stored_bytes(env))["body"]["version"], 3)

    def test_a_list_stored_after_c2_where_none_was_held(self):
        env, _, _, _ = self.changed(None, self.V1, lambda env: env.store(self.V1), None)
        self.assertEqual(json.loads(stored_bytes(env)), self.V1)
        self.assertNotEqual(stored_bytes(env), compact(self.V1), "the other writer's bytes remain")

    def test_a_list_removed_after_c2(self):
        env, _, _, _ = self.changed("v1", self.V2, lambda env: os.unlink(env.state / TRUST_NAME), 1)
        self.assertFalse((env.state / TRUST_NAME).exists(), "and the chain did not store itself")

    def test_the_same_body_with_other_signatures_is_the_same_list(self):
        # C4 compares the body hash (section 12, v5.3): an owner signature made again over the same
        # body is not a change, and the chain goes on.
        def resigned(env):
            doc = json.loads(json.dumps(self.V1))
            doc["ownerSignature"] = b64u(
                OWNER.sign(
                    b"muster-trust-list/1\n" + compact(doc["body"]), ec.ECDSA(hashes.SHA256())
                )
            )
            self.assertNotEqual(doc["ownerSignature"], self.V1["ownerSignature"])
            env.store(doc)

        env, answer, code, _ = self.changed("v1", self.V2, resigned, 1, accepted=True)
        self.assertAccepted(env, answer, code, self.V2)

    def test_the_same_file_written_again_is_not_a_change(self):
        env, answer, code, _ = self.changed(
            "v1", self.V2, lambda env: env.store(self.V1), 1, accepted=True
        )
        self.assertAccepted(env, answer, code, self.V2)

    def test_a_stored_list_that_cannot_be_read_again_is_state_unsafe(self):
        env, _, _, _ = self.changed_unsafe(
            lambda env: write_file(env.state / TRUST_NAME, b"{", 0o600)
        )
        self.assertEqual((env.state / TRUST_NAME).read_bytes(), b"{")

    def changed_unsafe(self, between):
        env = self.chain_world("v1")
        backend = FakeBackend(on_lock=lambda: between(env))
        answer, code, backend = self.ask(env, self.V2, backend=backend)
        self.assertOtherRefusal(answer, code, "state_unsafe", 1)
        self.assertEqual(backend.rows, [])
        self.assertEqual(leftovers(env), [])
        return env, answer, code, backend

    def test_a_link_put_in_place_of_the_stored_list_is_never_followed_or_written_through(self):
        victim_holder = []

        def link(env):
            victim = env.root / "victim"
            write_file(victim, b"precious", 0o600)
            os.unlink(env.state / TRUST_NAME)
            os.symlink(victim, env.state / TRUST_NAME)
            victim_holder.append(victim)

        env, _, _, _ = self.changed_unsafe(link)
        self.assertEqual(victim_holder[0].read_bytes(), b"precious")
        self.assertTrue((env.state / TRUST_NAME).is_symlink(), "the link itself is untouched")

    def test_a_busy_action_lock_is_action_busy_and_nothing_is_written(self):
        env = self.chain_world("v1")
        before = stored_bytes(env)
        answer, code, backend = self.ask(env, self.V2, backend=FakeBackend(busy=True))
        self.assertOtherRefusal(answer, code, "action_busy", 1)
        self.assertEqual(stored_bytes(env), before)
        self.assertEqual(backend.rows, [])
        self.assertEqual(backend.events, [])

    def test_an_unsafe_action_lock_is_state_unsafe(self):
        class Unsafe(FakeBackend):
            def lock(self):
                class Held:
                    def __enter__(self):
                        raise ma.LockUnsafe

                    def __exit__(self, *exc):
                        return False

                return Held()

        env = self.chain_world("v1")
        before = stored_bytes(env)
        answer, code, backend = self.ask(env, self.V2, backend=Unsafe())
        self.assertOtherRefusal(answer, code, "state_unsafe", 1)
        self.assertEqual(stored_bytes(env), before)

    def test_the_write_comes_before_the_audit_row_and_both_are_under_the_lock(self):
        env = self.chain_world("v1")
        backend = OrderBackend(env)
        answer, code, backend = self.ask(env, self.V2, backend=backend)
        self.assertAccepted(env, answer, code, self.V2)
        self.assertEqual(backend.events, ["lock", "audit", "unlock"])
        self.assertEqual(backend.held_at_audit, compact(self.V2), "durable before the row")

    def test_the_locks_a_chain_takes_are_the_rate_lock_alone_and_then_the_action_lock(self):
        import fcntl

        real = fcntl.flock

        def acquisitions(document, held="v1"):
            taken = []

            def flock(fd, operation):
                if operation & fcntl.LOCK_EX:
                    try:
                        taken.append(os.path.basename(os.readlink(f"/proc/self/fd/{fd}")))
                    except OSError:
                        taken.append("?")
                return real(fd, operation)

            env = self.chain_world(held)
            with mock.patch.object(fcntl, "flock", flock):
                answer, code, _, _ = helper_request(
                    env, CHAIN_COMMAND, chain_stdin(document), CHAIN_NOW
                )
            return answer, code, [n for n in taken if n in ("ops-control.lock", "muster-rate.lock")]

        answer, code, taken = acquisitions(self.V2)
        self.assertEqual(code, 0, answer)
        self.assertEqual(taken, ["muster-rate.lock", "ops-control.lock"])
        answer, code, taken = acquisitions(FIXTURE["lists"]["T7 list"])
        self.assertEqual(answer["body"]["reason"], "previous_hash")
        self.assertEqual(
            taken, ["muster-rate.lock"], "a chain refused at C3 never takes the action lock"
        )
        answer, code, taken = acquisitions(dict(self.V2, extra=1))
        self.assertEqual(answer["body"]["errorCode"], "bad_input")
        self.assertEqual(taken, [], "a chain refused at P3 takes no lock")

    def test_a_chain_does_not_touch_the_ledger(self):
        env = self.chain_world(None)
        answer, code, _ = self.ask(env, self.V1)
        self.assertEqual(code, 0)
        self.assertFalse((env.state / "muster-nonces.jsonl").exists())


class ChainFaultTests(ChainChecks):
    """C5's faults, in process: before the rename nothing is stored and no file is left behind; after
    it the list is held, and the same chain sent again is `version_gap`."""

    V1 = FIXTURE["lists"]["v1"]
    V2 = FIXTURE["lists"]["v2"]

    @contextlib.contextmanager
    def failing(self, name, newest):
        """Make step `name` of the write of `newest` fail with an I/O error, and nothing else."""
        import errno

        error = OSError(errno.EIO, "injected")
        real_open, real_fsync, real_rename = os.open, os.fsync, os.rename
        real_write = ma._write_all
        calls = {"fsync": 0}
        target = compact(newest)

        def opener(path, *args, **kwargs):
            if str(path).startswith(".muster-trust.json."):
                raise error
            return real_open(path, *args, **kwargs)

        def writer(fd, data):
            if bytes(data) == target:
                raise error
            return real_write(fd, data)

        def fsync(fd):
            calls["fsync"] += 1
            if calls["fsync"] == {"file fsync": 1, "directory fsync": 2}[name]:
                raise error
            return real_fsync(fd)

        def rename(src, dst, *args, **kwargs):
            if str(src).startswith(".muster-trust.json."):
                raise error
            return real_rename(src, dst, *args, **kwargs)

        patches = {
            "temp file": mock.patch.object(os, "open", opener),
            "write": mock.patch.object(ma, "_write_all", writer),
            "file fsync": mock.patch.object(os, "fsync", fsync),
            "rename": mock.patch.object(os, "rename", rename),
            "directory fsync": mock.patch.object(os, "fsync", fsync),
        }
        with patches[name]:
            yield

    BEFORE_THE_RENAME = ("temp file", "write", "file fsync", "rename")

    def test_a_fault_before_the_rename_is_internal_error_and_nothing_is_stored(self):
        for name in self.BEFORE_THE_RENAME:
            with self.subTest(fault=name):
                env = self.chain_world("v1")
                before = stored_bytes(env)
                with self.failing(name, self.V2):
                    answer, code, backend = self.ask(env, self.V2)
                self.assertOtherRefusal(answer, code, "internal_error", 1)
                self.assertEqual(stored_bytes(env), before, "the held list is untouched")
                self.assertEqual(leftovers(env), [], "the temporary file is removed")
                self.assertEqual(backend.rows, [], "no row for a chain that was not stored")
                # Nothing lingers: the same chain sent again is accepted.
                answer, code, _ = self.ask(env, self.V2, now=later(60))
                self.assertAccepted(env, answer, code, self.V2, at=ma.format_time(later(60)))

    def test_a_fault_before_the_rename_from_nothing_leaves_nothing(self):
        for name in self.BEFORE_THE_RENAME:
            with self.subTest(fault=name):
                env = self.chain_world(None)
                with self.failing(name, self.V1):
                    answer, code, _ = self.ask(env, self.V1)
                self.assertOtherRefusal(answer, code, "internal_error", None)
                self.assertFalse((env.state / TRUST_NAME).exists())
                self.assertEqual(leftovers(env), [])

    def test_a_fault_after_the_rename_is_internal_error_and_the_list_is_held(self):
        env = self.chain_world("v1")
        with self.failing("directory fsync", self.V2):
            answer, code, backend = self.ask(env, self.V2)
        self.assertOtherRefusal(answer, code, "internal_error", 1)
        self.assertEqual(stored_bytes(env), compact(self.V2), "the rename had happened")
        self.assertEqual(leftovers(env), [])
        # The sender learns it from `current --plan`, and a resend is `version_gap`.
        answer, code, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, later(60))
        self.assertEqual(answer["body"]["helperTrustListVersion"], 2)
        self.assertEqual(answer["body"]["trustListSHA256"], body_sha256(self.V2))
        answer, code, _ = self.ask(env, self.V2, now=later(120))
        self.assertChainRefused(answer, code, "version_gap", self.V2, 2)

    def test_a_failed_audit_append_after_the_rename_is_not_a_fault(self):
        for how in ("raises", "returns false"):
            with self.subTest(audit=how):
                env = self.chain_world("v1")
                backend = FakeBackend()
                if how == "raises":
                    backend.audit = mock.Mock(side_effect=RuntimeError("the log is gone"))
                else:
                    backend.audit = mock.Mock(return_value=False)
                answer, code, _ = self.ask(env, self.V2, backend=backend)
                self.assertAccepted(env, answer, code, self.V2)
                self.assertEqual(backend.audit.call_count, 1)

    def test_a_failed_audit_append_in_the_helper_is_not_a_fault_either(self):
        env = self.chain_world("v1")
        with mock.patch.object(foc, "audit", return_value=False):
            answer, code, _, rows = helper_request(
                env, CHAIN_COMMAND, chain_stdin(self.V2), CHAIN_NOW
            )
        self.assertAccepted(env, answer, code, self.V2)
        self.assertEqual(rows, [])
        with mock.patch.object(foc, "audit", side_effect=OSError("disk full")):
            answer, code, _, _ = helper_request(
                self.chain_world("v1"), CHAIN_COMMAND, chain_stdin(self.V2), CHAIN_NOW
            )
        self.assertEqual(code, 0, answer)

    def test_a_fault_while_signing_the_acknowledgement_is_internal_error_with_the_list_held(self):
        env = self.chain_world("v1")
        real_sign = ma.HelperKey.sign

        def sign_unless_ack(self, message):
            if message.startswith(b"muster-signed-ack/1\n"):
                raise RuntimeError("injected")
            return real_sign(self, message)

        backend = FakeBackend()
        with mock.patch.object(ma.HelperKey, "sign", sign_unless_ack):
            answer, code, backend = self.ask(env, self.V2, backend=backend)
        self.assertOtherRefusal(answer, code, "internal_error", 1)
        self.assertEqual(stored_bytes(env), compact(self.V2))
        self.assertEqual(
            len(backend.rows), 1, "the list was durable and the row was appended first"
        )
        answer, code, _ = self.ask(env, self.V2, now=later(60))
        self.assertChainRefused(answer, code, "version_gap", self.V2, 2)

    def test_an_unexpected_exception_at_any_step_is_internal_error(self):
        for name, patch in (
            (
                "reading the stored list",
                mock.patch.object(ma, "load_stored_list", side_effect=RuntimeError("x")),
            ),
            (
                "walking the chain",
                mock.patch.object(ma, "walk_chain", side_effect=RuntimeError("x")),
            ),
            (
                "the history comparison",
                mock.patch.object(ma, "history_hash_matches", side_effect=RuntimeError("x")),
            ),
            ("the rate step", mock.patch.object(ma, "check_rate", side_effect=RuntimeError("x"))),
        ):
            with self.subTest(step=name):
                env = self.chain_world("v1")
                before = stored_bytes(env)
                with patch:
                    answer, code, backend = self.ask(env, self.V2)
                self.assertRefused(answer, code, "internal_error", verified=False)
                self.assertEqual(stored_bytes(env), before)
                self.assertEqual(backend.rows, [])
                self.assertEqual(leftovers(env), [])
                self.assertIsNone(answer["body"]["chain"])

    def test_the_temporary_file_is_opened_the_way_the_spec_says(self):
        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            if str(path).startswith(".muster-trust.json."):
                opened.append((str(path), flags, args[0] if args else kwargs.get("mode")))
            return real_open(path, flags, *args, **kwargs)

        env = self.chain_world(None)
        with mock.patch.object(os, "open", spy):
            answer, code, _ = self.ask(env, self.V1)
        self.assertEqual(code, 0, answer)
        self.assertEqual(len(opened), 1)
        path, flags, mode = opened[0]
        self.assertRegex(path, r"^\.muster-trust\.json\.[0-9a-f]{16}\.tmp$")
        wanted = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        self.assertEqual(flags & wanted, wanted)
        self.assertEqual(mode, 0o600)
        # And the file it left is private, a regular file with one link, owned by the account.
        info = os.stat(env.state / TRUST_NAME)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual((info.st_nlink, info.st_uid), (1, UID))

    def test_the_file_is_synced_and_the_directory_too_in_that_order(self):
        events = []
        real_fsync, real_rename = os.fsync, os.rename

        def fsync(fd):
            mode = os.fstat(fd).st_mode
            events.append("fsync " + ("directory" if stat.S_ISDIR(mode) else "file"))
            return real_fsync(fd)

        def rename(src, dst, *args, **kwargs):
            if str(src).startswith(".muster-trust.json."):
                events.append("rename")
            return real_rename(src, dst, *args, **kwargs)

        env = self.chain_world(None)
        with mock.patch.object(os, "fsync", fsync), mock.patch.object(os, "rename", rename):
            answer, code, _ = self.ask(env, self.V1)
        self.assertEqual(code, 0, answer)
        self.assertEqual(events, ["fsync file", "rename", "fsync directory"])

    def test_the_rate_step_does_not_sync(self):
        # Section 12: a lost rate update only forgets recent requests, so C1 makes no fsync (a refused
        # chain therefore makes none at all).
        calls = []
        real_fsync = os.fsync

        def fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        env = self.chain_world("v1")
        with mock.patch.object(os, "fsync", fsync):
            answer, code, _ = self.ask(env, FIXTURE["lists"]["T7 list"])
        self.assertEqual(answer["body"]["reason"], "previous_hash")
        self.assertEqual(calls, [])


class ChainThenApprovalTests(ChainChecks):
    """The route comes alive with the first chain: approvals stop answering `no_trust_list`."""

    def test_an_approval_before_and_after_the_first_chain_through_the_module(self):
        env = Env(self, stored=None)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        self.assertIsNone(answer["body"]["helperTrustListVersion"])
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v1"])
        self.assertEqual(code, 0, answer)
        answer, code, backend = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
        self.assertEqual(backend.events.count("run"), 1)

    def test_an_approval_before_and_after_the_first_chain_through_the_helper(self):
        env = Env(self, stored=None)
        answer, code, runner, _ = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        self.assertEqual(runner.calls, [])
        answer, code, _, rows = helper_request(
            env, CHAIN_COMMAND, chain_stdin(FIXTURE["lists"]["v1"]), CHAIN_NOW
        )
        self.assertEqual(code, 0, answer)
        answer, code, runner, rows = helper_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(
            runner.calls, [[SYSTEMCTL, "--user", "restart", "fleet-server-status.timer"]]
        )
        self.assertEqual([row["result"] for row in rows], ["accepted", "done"])

    def test_an_approval_before_and_after_the_first_chain_as_two_subprocesses(self):
        env = Env(self, stored=None)
        answer, code, _, _ = subprocess_request(self, env, COMMAND, stdin_of(approval()), NOW)
        self.assertRefused(answer, code, "no_trust_list", verified=False)
        answer, code, _, _ = subprocess_request(
            self, env, CHAIN_COMMAND, chain_stdin(FIXTURE["lists"]["v1"]), CHAIN_NOW
        )
        self.assertEqual(code, 0, answer)
        answer, code, calls, _ = subprocess_request(self, env, COMMAND, stdin_of(approval()), NOW)
        self.assertDone(answer, code)
        self.assertEqual(calls, [[SYSTEMCTL, "--user", "restart", "fleet-server-status.timer"]])

    def test_a_device_added_by_a_chain_can_approve_and_one_removed_by_a_close_cannot(self):
        device_b = approval(signer=KEYS["device B approval"], deviceId=DEVICE_B)
        env = Env(self, stored="v1")
        answer, code, _ = module_request(env, COMMAND, stdin_of(device_b), NOW)
        self.assertRefused(answer, code, "unknown_device", verified=False)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"])
        self.assertEqual(code, 0, answer)
        answer, code, _ = module_request(env, COMMAND, stdin_of(device_b), NOW)
        self.assertDone(answer, code)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 2)
        # The close (rule B) leaves no device: every approval is `unknown_device` again.
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v3"], now=later(60))
        self.assertEqual(code, 0, answer)
        again = approval(
            signer=KEYS["device B approval"], deviceId=DEVICE_B, approvalNonce="b" * 32
        )
        answer, code, _ = module_request(env, COMMAND, stdin_of(again), later(70))
        self.assertRefused(answer, code, "unknown_device", verified=False)
        self.assertEqual(answer["body"]["helperTrustListVersion"], 3)

    def test_the_acknowledgement_after_a_chain_is_the_acknowledgement_of_what_was_stored(self):
        env = Env(self, stored=None)
        answer, code, _ = self.ask(env, *printed("v1", "v2"))
        written = answer
        answer, code, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, CHAIN_NOW)
        self.assertEqual(code, 0)
        self.assertEqual(answer["body"], written["body"])

    def test_the_budgets_of_the_classes_are_separate(self):
        # A chain is not a plan, an acknowledgement or an approval: none uses another's budget.
        env = Env(self, stored="v1")
        self.assertEqual(self.ask(env, FIXTURE["lists"]["T7 list"])[1], 2)
        self.assertEqual(module_request(env, CURRENT_COMMAND, CURRENT_STDIN, CHAIN_NOW)[1], 0)
        self.assertEqual(module_request(env, PLAN_COMMAND, job_stdin(), CHAIN_NOW)[1], 0)
        answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
        self.assertNotEqual(answer["body"].get("errorCode"), "rate_limited")
        events = json.loads((env.state / "muster-rate.json").read_text())["events"]
        self.assertEqual(
            sorted({event[0] for event in events}), ["chain", "current", "plan", "yes"]
        )


class ChainAuditTests(ChainChecks):
    def test_only_an_accepted_chain_writes_a_row(self):
        env = Env(self, stored="v1")
        v2 = FIXTURE["lists"]["v2"]
        # Accepted: one row.
        answer, code, _, rows = helper_request(env, CHAIN_COMMAND, chain_stdin(v2), CHAIN_NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["verb"], "muster trust-list")
        self.assertEqual(row["target"], "chain")
        self.assertEqual(row["result"], "accepted")
        self.assertEqual(row["host"], "server")
        self.assertEqual(
            {k: v for k, v in row.items() if k not in ("time", "host", "verb", "target", "result")},
            {
                "route": "from-muster",
                "via": "muster-ios",
                "trustListVersion": 2,
                "trustListSHA256": body_sha256(v2),
                "fromVersion": 1,
            },
        )
        for name in ("jobId", "deviceId", "approvalNonce", "planSHA256", "errorCode"):
            self.assertNotIn(name, row)
        # A replay (version_gap), a refusal at C3, a malformed input, a request over the rate limit:
        # each is refused, and none adds a row.
        base = epoch(CHAIN_NOW)
        for raw, when, expected in (
            (chain_stdin(v2), base + 20, "chain_refused"),
            (chain_stdin(FIXTURE["lists"]["T7 list"]), base + 40, "chain_refused"),
            (b"{}", base + 60, "bad_input"),
            (chain_stdin(FIXTURE["lists"]["v3"]), base + 62, None),  # accepted: the second row
            (chain_stdin(FIXTURE["lists"]["v4"]), base + 65, "rate_limited"),
        ):
            answer, code, _, rows = helper_request(env, CHAIN_COMMAND, raw, when)
            if expected is None:
                self.assertEqual(code, 0, answer)
            else:
                self.assertEqual(answer["body"]["errorCode"], expected, answer)
        self.assertEqual(
            [r["trustListVersion"] for r in rows], [2, 3], "one row per accepted chain"
        )

    def test_the_row_of_a_first_chain_has_a_null_from_version(self):
        env = Env(self, stored=None)
        _, code, _, rows = helper_request(
            env, CHAIN_COMMAND, chain_stdin(FIXTURE["lists"]["v1"]), CHAIN_NOW
        )
        self.assertEqual(code, 0)
        self.assertIn("fromVersion", rows[0])
        self.assertIsNone(rows[0]["fromVersion"])
        self.assertEqual(rows[0]["trustListVersion"], 1)

    def test_a_chain_refused_for_any_reason_writes_no_row_through_the_helper(self):
        env = Env(self, stored="v1")
        for index, document in enumerate(
            (FIXTURE["lists"]["T7 list"], FIXTURE["lists"]["T11 list"], FIXTURE["lists"]["v4"])
        ):
            answer, code, _, rows = helper_request(
                env, CHAIN_COMMAND, chain_stdin(document), later(100 + 20 * index)
            )
            self.assertEqual(code, 2, answer)
            self.assertEqual(rows, [])


# MARK: - C1: the rate limits of the `chain` class


class ChainRateTests(ChainChecks):
    """One chain per 10 seconds and 30 an hour, counted before anything but the pin and the input,
    counting every request that passed the rate step (whatever came after it)."""

    T7 = FIXTURE["lists"]["T7 list"]
    BASE_MS = epoch(CHAIN_NOW) * 1000

    def at(self, offset_ms):
        return (self.BASE_MS + offset_ms) / 1000

    def refused(self, env, offset_ms):
        return self.ask(env, self.T7, now=self.at(offset_ms))

    def test_two_chains_9999_milliseconds_apart_are_rate_limited_and_10000_are_not(self):
        env = self.chain_world("v1")
        answer, code, _ = self.refused(env, 0)
        self.assertChainRefused(answer, code, "previous_hash", self.T7, 1)
        answer, code, backend = self.refused(env, 9999)
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        self.assertEqual((backend.events, backend.rows), ([], []))
        answer, code, _ = self.refused(env, 10000)
        self.assertChainRefused(answer, code, "previous_hash", self.T7, 1)
        # The request that was refused rate_limited was not counted: the spacing runs from 10,000.
        answer, code, _ = self.refused(env, 19999)
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        answer, code, _ = self.refused(env, 20000)
        self.assertChainRefused(answer, code, "previous_hash", self.T7, 1)

    def test_a_rate_limited_chain_stores_nothing_and_leaves_the_held_list(self):
        env = self.chain_world("v1")
        self.refused(env, 0)
        before = stored_bytes(env)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"], now=self.at(5000))
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        self.assertEqual(stored_bytes(env), before)
        self.assertEqual(leftovers(env), [])
        self.assertEqual(env.audit_rows(), [])

    def test_thirty_chains_in_an_hour_are_the_most_and_the_hour_is_inclusive(self):
        env = self.chain_world("v1")
        for index in range(30):
            answer, code, _ = self.refused(env, index * 10_000)
            self.assertEqual(answer["body"]["reason"], "previous_hash", index)
        # The 31st, though the spacing is long over, is over the cap.
        answer, code, _ = self.refused(env, 300_000)
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        # An event exactly 3,600,000 ms old still counts, and one millisecond older has left the hour.
        answer, code, _ = self.refused(env, 3_600_000)
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        answer, code, _ = self.refused(env, 3_600_001)
        self.assertEqual(answer["body"]["reason"], "previous_hash", answer)

    def test_a_chain_that_is_accepted_counts_like_one_that_is_refused(self):
        env = self.chain_world(None)
        documents = long_chain(32)
        answer, code, _ = self.ask(env, *documents, now=self.at(0))
        self.assertEqual(code, 0, answer)
        # The same chain again is `version_gap` (it is held now) and counts all the same.
        for index in range(1, 30):
            answer, code, _ = self.ask(env, *documents, now=self.at(index * 10_000))
            self.assertEqual(answer["body"]["reason"], "version_gap", index)
        answer, code, _ = self.ask(env, *documents, now=self.at(400_000))
        self.assertOtherRefusal(answer, code, "rate_limited", None)

    def test_the_hourly_cap_and_spacing_are_the_specs(self):
        self.assertEqual(ma.RATE_LIMITS["chain"], (10_000, 30))
        self.assertEqual(ma.CHAIN_TARGET, "chain")

    def test_requests_refused_while_parsing_are_not_counted(self):
        env = self.chain_world("v1")
        for index in range(100):
            for raw in (b"", b"{}", b"x" * 70000, chain_stdin()):
                answer, code, _ = self.ask(env, raw=raw, now=self.at(0))
        self.assertFalse((env.state / "muster-rate.json").exists())
        answer, code, _ = self.refused(env, 0)
        self.assertEqual(answer["body"]["reason"], "previous_hash")
        events = json.loads((env.state / "muster-rate.json").read_text())["events"]
        self.assertEqual(len(events), 1)

    def test_a_chain_refused_for_the_pin_or_the_command_is_not_counted_either(self):
        env = self.chain_world("v1")
        os.chmod(env.etc / "owner.pub", 0o666)
        self.assertRefused(self.ask(env, self.T7)[0], 2, "pin_unsafe", verified=False)
        os.chmod(env.etc / "owner.pub", 0o644)
        self.assertRefused(
            self.ask(env, self.T7, command=CHAIN_PLAN_COMMAND)[0],
            2,
            "mode_not_allowed",
            verified=False,
        )
        self.assertFalse((env.state / "muster-rate.json").exists())

    def test_a_clock_stepped_back_never_holds_the_chain_key(self):
        env = self.chain_world("v1")
        later_ms = self.BASE_MS + 3_600_000
        document = {"contract": "muster-rate/1", "events": [["chain", "chain", later_ms]]}
        write_file(env.state / "muster-rate.json", ma.canonical_bytes(document), 0o600)
        answer, code, _ = self.refused(env, 0)
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        answer, code, _ = self.refused(env, 10_000)
        self.assertEqual(answer["body"]["reason"], "previous_hash")

    def test_a_rate_lock_held_elsewhere_for_more_than_the_wait_is_rate_limited(self):
        import fcntl

        env = self.chain_world("v1")
        self.refused(env, 0)  # creates the lock file
        fd = os.open(env.state / "muster-rate.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        before = stored_bytes(env)
        with mock.patch.object(ma, "RATE_LOCK_WAIT", 0.3):
            answer, code, _ = self.run_within(
                15,
                lambda: self.ask(env, FIXTURE["lists"]["v2"], now=self.at(100_000)),
                unblock=lambda: fcntl.flock(fd, fcntl.LOCK_UN),
            )
        self.assertOtherRefusal(answer, code, "rate_limited", None)
        self.assertEqual(stored_bytes(env), before)

    def test_an_unreadable_rate_file_is_state_unsafe_for_a_chain_too(self):
        env = self.chain_world("v1")
        write_file(env.state / "muster-rate.json", b"{", 0o600)
        before = stored_bytes(env)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["v2"])
        self.assertOtherRefusal(answer, code, "state_unsafe", None)
        self.assertEqual(stored_bytes(env), before)


# MARK: - Real processes: a crash at every step of C5, and chains at once

CHAIN_DRIVER = r"""
import importlib.util, json, os, signal, sys, time
from pathlib import Path
spec = json.load(open(sys.argv[1]))
loaded = importlib.util.spec_from_file_location("foc", spec["helper"])
foc = importlib.util.module_from_spec(loaded)
loaded.loader.exec_module(foc)
point = spec.get("kill_at")
real_load = foc._load_muster_modules

def die():
    os.kill(os.getpid(), signal.SIGKILL)

def mark(word):
    if spec.get("marker"):
        Path(spec["marker"]).write_text(word)

def wait_for_release(path):
    # The test releases this process by creating the file; a lost test cannot leave it for ever.
    if not path:
        return
    deadline = time.monotonic() + 60
    while not Path(path).exists():
        if time.monotonic() > deadline:
            os._exit(3)
        time.sleep(0.01)

def patched(ctx):
    modules = real_load(ctx)
    if not modules:
        return modules
    module = modules[0]
    if not spec.get("real_rate"):
        module.RATE_LIMITS["chain"] = (0, 100000)
    real_replace = module._replace_file

    def replace(dir_fd, name, data, uid, durable):
        if name != "muster-trust.json":
            return real_replace(dir_fd, name, data, uid, durable)
        mark("in-lock")
        time.sleep(spec.get("hold", 0))
        wait_for_release(spec.get("release_in_lock"))
        if point == "before_temp":
            die()
        real_fsync, real_rename = os.fsync, os.rename
        count = [0]

        def fsync(fd):
            count[0] += 1
            if count[0] == 1 and point == "before_fsync":
                die()
            real_fsync(fd)
            if count[0] == 1 and point == "before_rename":
                die()
            if count[0] == 2 and point == "after_dir_fsync":
                die()

        def rename(src, dst, *args, **kwargs):
            real_rename(src, dst, *args, **kwargs)
            if point == "after_rename":
                die()

        os.fsync, os.rename = fsync, rename
        try:
            return real_replace(dir_fd, name, data, uid, durable)
        finally:
            os.fsync, os.rename = real_fsync, real_rename

    module._replace_file = replace
    return modules

foc._load_muster_modules = patched
real_lock = foc._MusterBackend.lock

def lock(self):
    if spec.get("release_before_lock"):
        mark("before-lock")
        wait_for_release(spec["release_before_lock"])
    return real_lock(self)

foc._MusterBackend.lock = lock
if point == "after_audit":
    real_audit = foc.audit

    def audit(*args, **kwargs):
        real_audit(*args, **kwargs)
        die()

    foc.audit = audit
config = {k: v for k, v in spec["config"].items() if k != "now"}
config["clock_state"] = lambda: (0, 0, 175000)
ctx = foc.Context(platform="linux", home=Path(spec["home"]), runner=lambda argv, **kw: (0, b""),
                  config_path=Path(spec["allowlist"]), now=time.time, environ={"HOME": spec["home"]})
sys.exit(foc.main(["--from-muster"], ctx=ctx, muster={"config": config}))
"""


class ChainProcessTests(ChainChecks):
    """Real processes with the real clock, a signal that cannot be caught, and the real locks."""

    V1 = FIXTURE["lists"]["v1"]
    V2 = FIXTURE["lists"]["v2"]

    def driver(self, env):
        path = env.root / "chain_driver.py"
        if not path.exists():
            partial = env.root / f"chain_driver.py.{os.getpid()}.partial"
            partial.write_text(CHAIN_DRIVER)
            os.replace(partial, path)
        return path

    def launch(self, env, *documents, command=CHAIN_COMMAND, **extra):
        import time

        spec_path = env.root / f"chain-spec-{time.monotonic_ns()}.json"
        spec = {
            "helper": str(checkout() / "fleet-ops-control.py"),
            "home": str(env.home),
            "allowlist": str(env.allowlist),
            "config": {k: v for k, v in env.config(NOW).items() if k != "clock_state"},
        }
        spec.update(extra)
        spec_path.write_text(json.dumps(spec))
        process = subprocess.Popen(
            [sys.executable, "-I", str(self.driver(env)), str(spec_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"SSH_ORIGINAL_COMMAND": command},
        )
        process.stdin.write(chain_stdin(*documents))
        process.stdin.close()
        return process

    def finish(self, process):
        out, err = process.communicate(timeout=120)
        self.assertEqual(err, b"")
        return json.loads(out) if out else None

    def wait_for(self, path, process):
        import time

        deadline = time.monotonic() + 30
        while not path.exists():
            if process.poll() is not None:
                self.fail("the process ended before it reached the marker")
            if time.monotonic() > deadline:
                process.kill()
                self.fail("the process never reached the marker")
            time.sleep(0.01)

    def current(self, env):
        """What `trust-list current` says now, asked at the real time (the processes ran at it)."""
        import time

        answer, _, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, time.time())
        return answer["body"]

    # -- a crash at every step of C5

    POINTS_BEFORE_THE_RENAME = ("before_temp", "before_fsync", "before_rename")
    POINTS_AFTER_THE_RENAME = ("after_rename", "after_dir_fsync", "after_audit")

    def test_kill_nine_before_the_rename_leaves_what_was_held_and_never_a_partial_list(self):
        for held, newest in ((None, self.V1), ("v1", self.V2)):
            for point in self.POINTS_BEFORE_THE_RENAME:
                with self.subTest(held=held, point=point):
                    env = self.chain_world(held)
                    before = stored_bytes(env)
                    process = self.launch(env, newest, kill_at=point)
                    self.assertIsNone(self.finish(process), "no answer")
                    self.assertEqual(process.returncode, -9)
                    self.assertEqual(stored_bytes(env), before, "nothing stored")
                    self.assertEqual(env.audit_rows(), [])
                    # The process could not remove its temporary file, once it had made one; it is
                    # harmless to the next request, which is accepted as if nothing had happened and
                    # sweeps the leftover.
                    self.assertEqual(len(leftovers(env)), 0 if point == "before_temp" else 1, point)
                    process = self.launch(env, newest)
                    answer = self.finish(process)
                    self.assertEqual(answer["kind"], "ack", answer)
                    self.assertEqual(stored_bytes(env), compact(newest))
                    self.assertEqual(len(env.audit_rows()), 1)
                    self.assertEqual(leftovers(env), [], "swept by the next chain")

    def test_kill_nine_after_the_rename_leaves_the_new_list_and_a_resend_is_version_gap(self):
        for held, newest in ((None, self.V1), ("v1", self.V2)):
            for point in self.POINTS_AFTER_THE_RENAME:
                with self.subTest(held=held, point=point):
                    env = self.chain_world(held)
                    process = self.launch(env, newest, kill_at=point)
                    self.assertIsNone(self.finish(process), "no answer")
                    self.assertEqual(process.returncode, -9)
                    self.assertEqual(stored_bytes(env), compact(newest), "the list is held")
                    self.assertEqual(
                        len(env.audit_rows()), 1 if point == "after_audit" else 0, point
                    )
                    self.assertEqual(
                        self.current(env)["helperTrustListVersion"], newest["body"]["version"]
                    )
                    process = self.launch(env, newest)
                    answer = self.finish(process)
                    self.assertEqual(answer["body"]["errorCode"], "chain_refused", answer)
                    self.assertEqual(answer["body"]["reason"], "version_gap")
                    self.assertEqual(stored_bytes(env), compact(newest))

    def test_a_reader_sees_the_held_list_until_the_rename_and_the_new_one_after_never_a_mixture(
        self,
    ):
        env = self.chain_world("v1")
        old, new = stored_bytes_of(self, "v1"), compact(self.V2)
        marker, release = env.root / "marker", env.root / "release"
        process = self.launch(env, self.V2, release_in_lock=str(release), marker=str(marker))
        self.wait_for(marker, process)
        # The writer is inside the lock and has written nothing yet: the held list is what is read.
        seen = {stored_bytes(env) for _ in range(200)}
        self.assertEqual(seen, {old})
        release.write_text("go")
        while process.poll() is None:  # and while it writes, never a mixture
            seen.add(stored_bytes(env))
        self.assertEqual(self.finish(process)["kind"], "ack")
        seen.add(stored_bytes(env))
        self.assertLessEqual(seen, {old, new})
        self.assertIn(new, seen)

    # -- chains at once

    def outcomes(self, processes):
        words = []
        for process in processes:
            answer = self.finish(process)
            words.append(answer["kind"] if answer["kind"] == "ack" else answer["body"]["errorCode"])
            if answer["kind"] != "ack" and answer["body"]["errorCode"] == "chain_refused":
                words[-1] = "chain_refused:" + answer["body"]["reason"]
        return words

    def test_trust_list_changed_when_another_chain_lands_between_c2_and_c4(self):
        env = self.chain_world(None)
        marker, release = env.root / "slow", env.root / "release"
        slow = self.launch(env, self.V1, release_before_lock=str(release), marker=str(marker))
        self.wait_for(marker, slow)  # the slow one has read nothing held and walked the chain
        fast = self.launch(env, self.V1)
        self.assertEqual(self.finish(fast)["kind"], "ack")
        release.write_text("go")
        answer = self.finish(slow)
        self.assertEqual(answer["body"]["errorCode"], "trust_list_changed", answer)
        self.assertIsNone(answer["body"]["helperTrustListVersion"])
        self.assertEqual(stored_bytes(env), compact(self.V1))
        self.assertEqual(len(env.audit_rows()), 1, "only the chain that was stored left a row")

    def test_action_busy_while_another_chain_holds_the_lock(self):
        env = self.chain_world(None)
        marker, release = env.root / "holder", env.root / "release"
        holder = self.launch(env, self.V1, release_in_lock=str(release), marker=str(marker))
        self.wait_for(marker, holder)  # it is inside the lock
        other = self.launch(env, self.V1)
        answer = self.finish(other)
        self.assertEqual(answer["body"]["errorCode"], "action_busy", answer)
        release.write_text("go")
        self.assertEqual(self.finish(holder)["kind"], "ack")
        self.assertEqual(stored_bytes(env), compact(self.V1))
        self.assertEqual(len(env.audit_rows()), 1)

    def test_version_gap_when_the_other_chain_landed_before_this_one_read(self):
        env = self.chain_world(None)
        self.assertEqual(self.finish(self.launch(env, self.V1))["kind"], "ack")
        answer = self.finish(self.launch(env, self.V1))
        self.assertEqual(answer["body"]["errorCode"], "chain_refused")
        self.assertEqual(answer["body"]["reason"], "version_gap")
        self.assertEqual(answer["body"]["helperTrustListVersion"], 1)

    def test_eight_processes_presenting_the_same_chain_at_once_store_it_once(self):
        env = self.chain_world(None)
        processes = [self.launch(env, self.V1, hold=0.3) for _ in range(8)]
        words = self.outcomes(processes)
        self.assertEqual(words.count("ack"), 1, words)
        self.assertLessEqual(
            set(words) - {"ack"},
            {"action_busy", "trust_list_changed", "chain_refused:version_gap"},
            words,
        )
        self.assertEqual(stored_bytes(env), compact(self.V1))
        self.assertEqual(len(env.audit_rows()), 1)
        self.assertEqual(leftovers(env), [])

    def test_two_different_chains_at_once_store_exactly_one_of_them(self):
        for _ in range(3):
            env = self.chain_world(None)
            first = self.launch(env, self.V1, self.V2, hold=0.3)
            second = self.launch(env, self.V1, hold=0.3)
            words = self.outcomes([first, second])
            self.assertEqual(words.count("ack"), 1, words)
            stored = json.loads(stored_bytes(env))
            self.assertIn(stored["body"]["version"], (1, 2))
            self.assertEqual(len(env.audit_rows()), 1)
            self.assertEqual(
                stored_bytes(env), compact(self.V2 if stored["body"]["version"] == 2 else self.V1)
            )


def stored_bytes_of(test, name):
    """The bytes `Env` wrote for the stored list `name` (json.dumps, spaced), for comparison."""
    return json.dumps(FIXTURE["lists"][name]).encode()


# MARK: - Review of the chain verb: boundaries, an unlocked reader, a trickle, the sweep


class ChainBoundaryTests(ChainChecks):
    """Cases the two independent reviews found missing: each kills a lenient edit of the walk or of the
    parse that the first suite let survive."""

    def test_the_owner_signature_over_the_countersign_prefix_is_refused(self):
        # Domain separation: the owner key's signature over `muster-trust-countersign/1` + body (what a
        # device that shares the owner's key would have made) is not an owner signature.
        v1 = json.loads(json.dumps(printed("v1")[0]))
        v1["ownerSignature"] = b64u(
            sign(OWNER, b"muster-trust-countersign/1\n" + compact(v1["body"]))
        )
        answer, code, _ = self.ask(self.chain_world(None), v1)
        self.assertChainRefused(answer, code, "owner_signature", v1, None)

    def test_a_countersignature_over_the_bare_body_is_refused(self):
        v2 = json.loads(json.dumps(printed("v2")[0]))
        v2["countersignatures"] = [
            {
                "deviceId": BASE_APPROVAL["deviceId"],
                "signature": b64u(sign(DEVICE_A, compact(v2["body"]))),
            }
        ]
        answer, code, _ = self.ask(self.chain_world("v1"), v2)
        self.assertChainRefused(answer, code, "countersignature", v2, 1)

    def test_an_open_list_with_no_device_is_closed_rule(self):
        # Check 7 is "exactly when": closed with a device is T16, and open with none is the other way.
        body = json.loads(json.dumps(printed("v1")[0]["body"]))
        body["devices"] = []
        body["closed"] = False
        document = resign(body)
        answer, code, _ = self.ask(self.chain_world(None), document)
        self.assertChainRefused(answer, code, "closed_rule", document, None)

    def test_a_list_for_an_old_version_may_not_name_the_current_pinned_key(self):
        # The history assigns version 1 to the old key, and the pin is the new key.  A version 1 whose
        # ownerKey names the new key, signed by the old key, is check 3 (owner_key), not accepted.
        env = self.chain_world(None)
        new_pub = b64u(spki(KEYS[ROTATED_OWNER]))
        body = json.loads(json.dumps(printed("v1")[0]["body"]))
        body["ownerKey"] = new_pub
        document = resign(body, OWNER)
        lines = [
            {
                "fromVersion": 1,
                "ownerKey": b64u(spki(OWNER)),
                "toSHA256": hashlib.sha256(compact(body)).hexdigest(),
                "toVersion": 1,
            },
            {"fromVersion": 2, "ownerKey": new_pub, "toSHA256": None, "toVersion": None},
        ]
        history = "".join(compact(line).decode() + "\n" for line in lines)
        write_file(env.etc / "owner-history.jsonl", history.encode(), 0o644)
        write_file(env.etc / "owner.pub", (new_pub + "\n").encode(), 0o644)
        answer, code, _ = self.ask(env, document)
        self.assertChainRefused(answer, code, "owner_key", document, None)

    def test_the_largest_legal_version_is_accepted(self):
        # Section 2: integers up to 2^53 - 1 (sixteen digits).  A held list at 2^53 - 2 and a successor.
        body = json.loads(json.dumps(printed("v2")[0]["body"]))
        body["version"] = 2**53 - 2
        held = resign(body)
        env = self.chain_world(None)
        env.store(held)
        document = next_rule_a(held, issuedAt="2026-11-07T21:00:00Z")
        self.assertEqual(document["body"]["version"], 2**53 - 1)
        answer, code, _ = self.ask(env, document)
        self.assertAccepted(env, answer, code, document)

    def test_c4_compares_the_body_and_not_the_version(self):
        # The held list is replaced between C2 and C4 by a different owner-signed list of the SAME
        # version.  The version is unchanged; the body is not, so it is `trust_list_changed`.
        v1 = printed("v1")[0]
        body = json.loads(json.dumps(v1["body"]))
        body["issuedAt"] = "2026-11-07T19:00:00Z"
        other = resign(body)
        self.assertEqual(other["body"]["version"], v1["body"]["version"])
        self.assertNotEqual(body_sha256(other), body_sha256(v1))
        env = self.chain_world("v1")
        backend = FakeBackend(on_lock=lambda: env.store(other))
        answer, code, backend = self.ask(env, printed("v2")[0], backend=backend)
        self.assertOtherRefusal(answer, code, "trust_list_changed", 1)
        self.assertEqual(json.loads(stored_bytes(env)), other, "the replacement is untouched")
        self.assertEqual(backend.rows, [])


class UnlockedReaderTests(Checks):
    """The stored list is read without a lock because it changes only by rename.  A reader that opened
    the old file just before a rename sees it with no link left (`st_nlink` 0), and that is no fault."""

    @staticmethod
    def no_link(path):
        """`os.fstat` as it is, except that the file at `path` is reported with no link left."""
        target = os.stat(path)
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                fields = list(tuple(info))
                fields[3] = 0
                return os.stat_result(fields)
            return info

        return mock.patch.object(os, "fstat", fstat)

    def test_a_read_only_open_of_the_stored_list_with_no_link_left_is_accepted(self):
        env = Env(self, stored="v1")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        with self.no_link(env.state / TRUST_NAME):
            self.assertEqual(ma.load_stored_list(dir_fd, UID).version, 1)
            opened = ma._open_state_file(dir_fd, TRUST_NAME, UID, os.O_RDONLY, unlinked_ok=True)
            os.close(opened[0])

    def test_nothing_but_the_stored_list_accepts_a_file_with_no_link_left(self):
        # Section 11 asks for exactly one link; only the stored list, which a chain replaces by rename,
        # may be seen with none (the finding on PR 1543).  The helper key is never replaced that way.
        env = Env(self, stored="v1")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        with self.no_link(env.state / "muster-helper.key"):
            with self.assertRaises(ma.KeyUnusable):
                ma.load_helper_key(dir_fd, UID)
            with self.assertRaises(ma.StateUnsafe):
                ma._open_state_file(dir_fd, "muster-helper.key", UID, os.O_RDONLY)
        # The default of the opener is the strict rule, for the stored list's name as for any other.
        with self.no_link(env.state / TRUST_NAME), self.assertRaises(ma.StateUnsafe):
            ma._open_state_file(dir_fd, TRUST_NAME, UID, os.O_RDONLY)
        # Through the flow: the helper key with no link left is the unusable key, so the route is unsigned.
        with self.no_link(env.state / "muster-helper.key"):
            answer, code, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, NOW)
        self.assertEqual((answer, code), (ma.unsigned_helper_key_missing(), 2))

    def test_every_unlocked_read_of_the_stored_list_survives_it(self):
        # Y2 (an approval), K2 (`current`) and C2 (a chain) all read the list without a lock.
        env = Env(self, stored="v1")
        with self.no_link(env.state / TRUST_NAME):
            answer, code, _ = module_request(env, CURRENT_COMMAND, CURRENT_STDIN, NOW)
            self.assertEqual(code, 0, answer)
            self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
            answer, code, _ = module_request(env, COMMAND, stdin_of(approval()), NOW)
            self.assertDone(answer, code)
            answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), later(100))
            self.assertEqual(answer["body"]["helperTrustListVersion"], 1)
        env = Env(self, stored="v1")
        with self.no_link(env.state / TRUST_NAME):
            answer, code, _ = module_request(
                env, CHAIN_COMMAND, chain_stdin(FIXTURE["lists"]["v2"]), CHAIN_NOW
            )
        self.assertEqual(code, 0, answer)

    def test_a_handle_that_can_write_to_a_file_with_no_link_left_is_refused(self):
        env = Env(self, stored="v1")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        for name, flags in (
            ("muster-nonces.jsonl", os.O_RDWR | os.O_APPEND),
            ("muster-rate.json", os.O_RDWR),
            ("muster-rate.lock", os.O_RDWR),
            ("muster-rate.json", os.O_WRONLY),
        ):
            with self.subTest(name=name, flags=flags):
                write_file(env.state / name, b"", 0o600)
                with self.no_link(env.state / name), self.assertRaises(ma.StateUnsafe):
                    ma._open_state_file(dir_fd, name, UID, flags)
                # Asking for the stored list's allowance does not help a handle that can write.
                with self.no_link(env.state / name), self.assertRaises(ma.StateUnsafe):
                    ma._open_state_file(dir_fd, name, UID, flags, unlinked_ok=True)
        # Through the flow: a plan counts in the rate file, which is opened to be written.
        env = Env(self, stored="v1")
        module_request(env, PLAN_COMMAND, job_stdin(), NOW)
        with self.no_link(env.state / "muster-rate.json"):
            answer, code, _ = module_request(env, PLAN_COMMAND, job_stdin(), later(100))
        self.assertRefused(answer, code, "state_unsafe", verified=False)

    def test_a_file_with_two_links_is_still_refused_read_only_too(self):
        env = Env(self, stored="v1")
        os.link(env.state / TRUST_NAME, env.root / "twin")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        with self.assertRaises(ma.StateUnsafe):
            ma.load_stored_list(dir_fd, UID)

    def test_readers_racing_the_chains_own_writer_never_see_a_fault(self):
        # The real writer (`_replace_file`, as C5 calls it) against three readers.  Before the fix about
        # one read in twenty failed; a few hundred renames, bounded in time, show it at once.
        import threading
        import time

        env = Env(self, stored="v1")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        documents = [compact(FIXTURE["lists"]["v1"]), compact(FIXTURE["lists"]["v2"])]
        done = threading.Event()
        problems = []
        reads = [0]

        def writer():
            deadline = time.monotonic() + 20
            try:
                for index in range(600):
                    if time.monotonic() > deadline:
                        break
                    ma._replace_file(dir_fd, TRUST_NAME, documents[index % 2], UID, False)
            finally:
                done.set()

        def reader():
            while not done.is_set():
                try:
                    stored = ma.load_stored_list(dir_fd, UID)
                    reads[0] += 1
                    if stored is None or stored.version not in (1, 2):
                        problems.append("read " + repr(stored))
                except Exception as exc:  # noqa: BLE001 - every kind of failure is a problem here
                    problems.append(type(exc).__name__ + ": " + str(exc))

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse([t for t in threads if t.is_alive()], "a thread did not finish")
        self.assertGreater(reads[0], 50)
        self.assertEqual(problems[:3], [], "%d of %d reads failed" % (len(problems), reads[0]))


class ChainStdinDeadlineTests(ChainChecks):
    """The deadline for standard input is a total, not a wait per read: a sender that trickles one byte
    at a time must not hold the helper (and the chain's lock-free steps) for ever."""

    def trickle(self, interval):
        import threading
        import time

        read_end, write_end = os.pipe()
        stop = threading.Event()

        def drip():
            try:
                for _ in range(60):
                    if stop.is_set():
                        break
                    os.write(write_end, b" ")
                    time.sleep(interval)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    os.close(write_end)

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(10)
            with contextlib.suppress(OSError):
                os.close(read_end)

        self.addCleanup(cleanup)
        return read_end

    def test_a_trickle_ends_at_the_total_deadline_and_not_later(self):
        import time

        read_end = self.trickle(0.1)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.run_within(15, lambda: ma.read_stdin_limited(read_end, 65536, 0.5))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertLess(elapsed, 3.0, "the deadline was reset by each byte")

    def test_a_chain_that_trickles_in_is_bad_input_at_the_deadline(self):
        import time

        read_end = self.trickle(0.1)
        env = self.chain_world("v1")
        started = time.monotonic()
        answer, code = self.run_within(
            15,
            lambda: ma.handle_request(
                env.module_config(CHAIN_NOW),
                FakeBackend(),
                CHAIN_COMMAND,
                True,
                lambda limit: ma.read_stdin_limited(read_end, limit, 0.5),
                mt,
            ),
        )
        self.assertOtherRefusal(answer, code, "bad_input")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse((env.state / "muster-rate.json").exists())


class ChainTemporaryFileTests(ChainChecks):
    """The temporary file of C5: removed after any fault, and what a crash leaves is swept by the next
    chain and nothing else is touched."""

    V1 = FIXTURE["lists"]["v1"]
    V2 = FIXTURE["lists"]["v2"]
    LEFTOVER = ".muster-trust.json.0123456789abcdef.tmp"

    def test_the_temporary_file_is_removed_after_any_fault_and_not_only_an_oserror(self):
        for exc in (MemoryError("injected"), RuntimeError("injected"), OSError("injected")):
            with self.subTest(fault=type(exc).__name__):
                env = self.chain_world("v1")
                before = stored_bytes(env)
                with mock.patch.object(os, "fsync", side_effect=exc):
                    answer, code, backend = self.ask(env, self.V2)
                self.assertOtherRefusal(answer, code, "internal_error", 1)
                self.assertEqual(stored_bytes(env), before)
                self.assertEqual(leftovers(env), [], "the temporary file is gone")
                self.assertEqual(backend.rows, [])
        # The same through the writer itself.
        env = self.chain_world("v1")
        dir_fd = ma.open_state_dir(env.module_config(NOW))
        self.addCleanup(os.close, dir_fd)
        with (
            mock.patch.object(os, "fsync", side_effect=MemoryError("injected")),
            self.assertRaises(MemoryError),
        ):
            ma._replace_file(dir_fd, TRUST_NAME, b"x", UID, True)
        self.assertEqual(leftovers(env), [])

    def test_a_leftover_of_a_crashed_write_is_swept_by_the_next_chain(self):
        env = self.chain_world("v1")
        names = [
            ".muster-trust.json.0123456789abcdef.tmp",
            ".muster-trust.json.fedcba9876543210.tmp",
            ".muster-trust.json.00000000deadbeef.tmp",
        ]
        write_file(env.state / names[0], b"", 0o600)
        write_file(env.state / names[1], compact(self.V2)[:100], 0o600)
        write_file(env.state / names[2], compact(self.V2), 0o600)
        self.assertEqual(leftovers(env), sorted(names))
        answer, code, _ = self.ask(env, self.V2)
        self.assertAccepted(env, answer, code, self.V2)
        self.assertEqual(leftovers(env), [])

    def test_the_sweep_runs_under_the_action_lock_and_before_the_write(self):
        env = self.chain_world("v1")
        write_file(env.state / self.LEFTOVER, b"", 0o600)
        seen = []
        backend = FakeBackend(on_lock=lambda: seen.append(leftovers(env)))
        answer, code, _ = self.ask(env, self.V2, backend=backend)
        self.assertAccepted(env, answer, code, self.V2)
        self.assertEqual(seen, [[self.LEFTOVER]], "still there when the lock was taken")

    def test_nothing_but_a_regular_file_of_that_exact_name_is_touched(self):
        env = self.chain_world("v1")
        state = env.state
        victim = env.root / "victim"
        write_file(victim, b"precious", 0o600)
        keep = {}  # name -> how to see it is unchanged
        os.symlink(victim, state / ".muster-trust.json.1111111111111111.tmp")
        os.mkdir(state / ".muster-trust.json.2222222222222222.tmp", 0o700)
        write_file(state / ".muster-trust.json.3333333333333333.tmp", b"two links", 0o600)
        os.link(state / ".muster-trust.json.3333333333333333.tmp", env.root / "second-name-of-it")
        for name in (
            ".muster-trust.json.abc.tmp",
            ".muster-trust.json.0123456789abcdef.tmp2",
            ".muster-trust.json.0123456789abcdef.tmp.bak",
            ".muster-trust.json.0123456789ABCDEF.tmp",
            ".muster-trust.json.0123456789abcde.tmp",
            ".muster-trust.json.0123456789abcdef0.tmp",
            "muster-trust.json.0123456789abcdef.tmp",
            "x.muster-trust.json.0123456789abcdef.tmp",
            ".muster-rate.json.0123456789abcdef.tmp",
            ".muster-nonces.jsonl.0123456789abcdef.tmp",
            ".muster-trust.json",
            ".muster-trust.json.0123456789abcdef",
        ):
            write_file(state / name, b"keep " + name.encode(), 0o600)
            keep[name] = b"keep " + name.encode()
        before = sorted(p.name for p in state.iterdir())
        answer, code, _ = self.ask(env, self.V2)
        self.assertEqual(code, 0, answer)
        self.assertEqual(stored_bytes(env), compact(self.V2))
        after = sorted(p.name for p in state.iterdir())
        # The write added muster-rate.json and muster-rate.lock (the rate step) and nothing else.
        self.assertEqual(sorted(set(after) - set(before)), ["muster-rate.json", "muster-rate.lock"])
        self.assertEqual(sorted(set(before) - set(after)), [])
        self.assertTrue((state / ".muster-trust.json.1111111111111111.tmp").is_symlink())
        self.assertEqual(victim.read_bytes(), b"precious")
        self.assertTrue((state / ".muster-trust.json.2222222222222222.tmp").is_dir())
        self.assertEqual(
            (state / ".muster-trust.json.3333333333333333.tmp").read_bytes(), b"two links"
        )
        for name, content in keep.items():
            self.assertEqual((state / name).read_bytes(), content, name)

    def test_a_file_of_another_account_is_not_touched(self):
        env = self.chain_world("v1")
        write_file(env.state / self.LEFTOVER, b"theirs", 0o600)
        real = os.stat

        def stat_as_another_account(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if str(path) == self.LEFTOVER:
                fields = list(tuple(info))
                fields[4] = UID + 1000
                return os.stat_result(fields)
            return info

        with mock.patch.object(os, "stat", stat_as_another_account):
            answer, code, _ = self.ask(env, self.V2)
        self.assertEqual(code, 0, answer)
        self.assertEqual((env.state / self.LEFTOVER).read_bytes(), b"theirs")

    def test_a_chain_that_is_refused_or_busy_sweeps_nothing(self):
        env = self.chain_world("v1")
        write_file(env.state / self.LEFTOVER, b"", 0o600)
        answer, code, _ = self.ask(env, FIXTURE["lists"]["T7 list"])
        self.assertEqual(answer["body"]["reason"], "previous_hash")
        answer, code, _ = self.ask(env, self.V2, now=later(60), backend=FakeBackend(busy=True))
        self.assertOtherRefusal(answer, code, "action_busy", 1)
        self.assertEqual(leftovers(env), [self.LEFTOVER])

    def test_a_failing_sweep_never_stops_the_write(self):
        for how in ("listing", "unlink", "stat"):
            with self.subTest(fails=how):
                env = self.chain_world("v1")
                write_file(env.state / self.LEFTOVER, b"", 0o600)
                injected = OSError("injected")
                if how == "listing":
                    patch = mock.patch.object(os, "listdir", side_effect=injected)
                elif how == "unlink":
                    real = os.unlink

                    def refuse_unlink(path, *args, **kwargs):
                        if str(path) == self.LEFTOVER:
                            raise injected
                        return real(path, *args, **kwargs)

                    patch = mock.patch.object(os, "unlink", refuse_unlink)
                else:
                    real_stat = os.stat

                    def refuse_stat(path, *args, **kwargs):
                        if str(path) == self.LEFTOVER:
                            raise injected
                        return real_stat(path, *args, **kwargs)

                    patch = mock.patch.object(os, "stat", refuse_stat)
                with patch:
                    answer, code, _ = self.ask(env, self.V2)
                self.assertEqual(code, 0, answer)
                self.assertEqual(stored_bytes(env), compact(self.V2))

    def test_a_signing_fault_that_repeats_is_the_unsigned_helper_key_missing(self):
        # The refusal that would say `internal_error` is signed with the same key: when signing fails
        # every time the only answer left is the unsigned one.  The list is held, with its row.
        env = self.chain_world("v1")
        backend = FakeBackend()
        with mock.patch.object(ma.HelperKey, "sign", side_effect=RuntimeError("injected")):
            answer, code, backend = self.ask(env, self.V2, backend=backend)
        self.assertEqual(code, 2)
        self.assertEqual(answer["body"]["errorCode"], "helper_key_missing")
        self.assertIsNone(answer["signature"])
        self.assertEqual(stored_bytes(env), compact(self.V2))
        self.assertEqual(len(backend.rows), 1)

    def test_no_audit_row_when_only_the_directory_sync_fails(self):
        # A row means the list was durable (section 14); the list is held, `current --plan` is authoritative,
        # and the audit tail is best effort.
        env = self.chain_world("v1")
        calls = {"n": 0}
        real = os.fsync

        def fsync(fd):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected")
            return real(fd)

        with mock.patch.object(os, "fsync", fsync):
            answer, code, backend = self.ask(env, self.V2)
        self.assertOtherRefusal(answer, code, "internal_error", 1)
        self.assertEqual(stored_bytes(env), compact(self.V2))
        self.assertEqual(backend.rows, [])


# MARK: - What is written down


class DocumentedChainTests(ChainChecks):
    def test_the_chain_constants_are_the_specs(self):
        self.assertEqual(
            (ma.CHAIN_STDIN_LIMIT, ma.CHAIN_MIN_ENTRIES, ma.CHAIN_MAX_ENTRIES), (65536, 1, 32)
        )
        self.assertEqual(ma.STDIN_LIMIT, 4096)
        self.assertEqual(ma.COUNTERSIGN_PREFIX, b"muster-trust-countersign/1\n")
        self.assertEqual(ma.TRUST_LIST_PREFIX, b"muster-trust-list/1\n")
        self.assertEqual(ma.CHAIN_AUDIT_RESULT, "accepted")
        self.assertEqual(ma.STATE_TRUST_NAME, "muster-trust.json")

    def test_the_reasons_are_section_15s_closed_list_less_the_open_proposals(self):
        text = SPEC_PATH.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| `chain_refused` |"))
        words = set(re.findall(r"`([a-z_]+)`", row)) - {"chain_refused", "reason"}
        proposal = {"reopen_unrecorded", "reopen_tag", "reopen_device"}
        self.assertLessEqual(proposal, words)
        self.assertEqual(set(ma.CHAIN_REFUSED_REASONS), words - proposal)
        self.assertEqual(len(set(ma.CHAIN_REFUSED_REASONS)), len(ma.CHAIN_REFUSED_REASONS))
        self.assertIn("history_hash", ma.CHAIN_REFUSED_REASONS)

    def test_the_vectors_reach_every_reason_the_helper_can_answer(self):
        reasons = {
            row["frozenExpected"]["reason"]
            for row in FIXTURE["chainVectors"]
            if row["frozenExpected"] != "ack"
        }
        self.assertEqual(reasons, set(ma.CHAIN_REFUSED_REASONS))

    def test_the_checks_of_section_8_are_in_the_order_the_code_applies_them(self):
        text = SPEC_PATH.read_text(encoding="utf-8")
        start = text.index("**The checks, in order.**")
        table = text[start : text.index("Exactly one rule applies", start)]
        rows = [line for line in table.splitlines() if re.match(r"\| [1-8] \|", line)]
        self.assertEqual(len(rows), 8)
        spec_order = []
        for row in rows:
            spec_order += re.findall(r"`([a-z_]+)`", row.split("|")[3])
        # The spec's reasons in the spec's order are a subsequence of the code's list: the code adds
        # `first_rule` (check 2, and the rules) and `services_changed`/`server_keys_removed`/
        # `reopen_unavailable` (check 8, "as the rule says") that the table names by reference.
        wanted = [name for name in spec_order if name in ma.CHAIN_REFUSED_REASONS]
        positions = [ma.CHAIN_REFUSED_REASONS.index(name) for name in wanted]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            wanted,
            [
                "version_gap",
                "first_rule",
                "previous_hash",
                "owner_key",
                "history_hash",
                "owner_signature",
                "duplicate_device",
                "countersignature",
                "closed_rule",
            ],
        )

    def test_the_module_says_the_chain_verb_is_built_and_what_is_not(self):
        self.assertIn("THE CHAIN VERB", ma.__doc__)
        self.assertIn("rule C", ma.__doc__)
        self.assertIn("reopen_unavailable", ma.__doc__)
        self.assertNotIn("is not built (the spec's section 17A is open)", ma.__doc__)
        self.assertIn("trust-list chain --yes", foc.__doc__)
        self.assertIn("trust-list chain --yes", inspect.getsource(foc.run_from_muster))
        self.assertNotIn("NOT BUILT", inspect.getsource(foc.run_from_muster))

    def test_the_audit_names_the_helper_accepts_include_the_chain_rows_fields(self):
        for name in ("route", "via", "trustListVersion", "trustListSHA256", "fromVersion"):
            self.assertIn(name, foc.MUSTER_AUDIT_NAMES)
        # A row with a null from-version and a hash is written, and nothing else is let through.
        env = Env(self)
        ctx = foc.Context(platform="linux", home=env.home, runner=FakeRunner())
        foc.audit(
            ctx,
            "muster",
            "trust-list",
            "chain",
            "accepted",
            extra={
                "route": "from-muster",
                "via": "muster-ios",
                "trustListVersion": 3,
                "trustListSHA256": "a" * 64,
                "fromVersion": None,
                "somethingElse": "x",
            },
        )
        row = env.audit_rows()[-1]
        self.assertEqual(row["fromVersion"], None)
        self.assertEqual(row["trustListVersion"], 3)
        self.assertNotIn("somethingElse", row)

    def test_the_rollout_note_exists_and_leaves_a_no_break_space_after_every_sentence(self):
        note = (HERE.parent / "docs" / "rollouts" / "2026-11-08-trust-list-chain.md").read_text(
            encoding="utf-8"
        )
        self.assertGreater(len(note), 2000)
        inside = False
        for number, line in enumerate(note.splitlines(), 1):
            if line.startswith("```"):
                inside = not inside
            if inside or line.startswith(("|", "#")):
                continue
            self.assertIsNone(re.search(r"[.?!]  \S", line), (number, line))
            self.assertIsNone(re.search(r"[.?!] [A-Z]", line), (number, line))
        self.assertIn(".  ", note)


if __name__ == "__main__":
    unittest.main()
