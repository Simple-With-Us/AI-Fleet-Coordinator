#!/usr/bin/env python3
"""Generate `scripts/fixtures/muster-vectors-v5.json`, the test vectors of the `--from-muster` route as data.

  python3 scripts/make_muster_vectors.py --check    # exit 1 and say so when the file is not what this makes
  python3 scripts/make_muster_vectors.py --write    # rewrite the file

The fixture has two parts, and nothing in it is typed by hand but the `about` note:

  * What section 16 of `docs/muster-ios/server-route-spec.md` prints (the keys, the canonical form, the
    pin, both owner histories, every list, the sixteen approvals, the seven signed answers, and each
    chain vector's held list, chain, history and answers, as printed and as a frozen helper gives
    them), read out of the spec's text, so that the two cannot differ.  The spec is the source of those bytes.
  * What the code makes of them: the whole answer line of every signed answer, the signature cases
    (bad DER, bad base64url), the further signed answers (nothing held, a read result, a plan refusal, an
    unknown outcome, a rate limit, a plan mismatch, the unsigned `helper_key_missing`), and the plans
    (a restart of a system unit and of a user unit, a redeploy of an application with a non-ASCII name,
    three resource-policy plans in the code's own units).  Each is made by running `muster_approval.py` and
    `fleet-ops-control.py` themselves, in a private temporary world, with test keys.

Every key is a test key derived from its label, exactly as the spec says: the private key is the SHA-256
of "muster test key " and the label, taken modulo the curve order minus one, plus one.  Nothing here reads
a real key, `/etc/muster`, the account's state directory or the network, and the signatures are
deterministic, so the output is the same on every run and every machine.  A test regenerates the file and
fails when it differs, so the fixture cannot drift from the code or from the spec.

Needs `cryptography`, as the route does.
"""

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE.parent / "docs" / "muster-ios" / "server-route-spec.md"
FIXTURE_PATH = HERE / "fixtures" / "muster-vectors-v5.json"

NOW = "2026-11-07T21:00:30Z"
NOW_EPOCH = 1_794_085_230
CURVE_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
RUN_COMMAND = "server restart fleet-server-status.timer --yes"
CURRENT_COMMAND = "muster trust-list current --plan"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
AUDIT_ID = "a1b2c3d4e5f6"
APPLICATION = "abc123def456"
RECEIPT_INVOCATION = "a" * 32
RECEIPT_BASELINE = 500
CLOCK_STATE = (0, 0, 175_000)  # adjtimex: TIME_OK, STA_UNSYNC clear, 175 ms of error

ABOUT = (
    "Public test data only: every key is derived from a label and used nowhere else.  "
    "Section 16 of docs/muster-ios/server-route-spec.md prints the vectors of keys, canonicalForm, "
    "pin, history, reopen, lists, listBodyHashes, approvals, answers (the answer objects and "
    "their bodySHA256) and illustrativePlan, and a test checks that this file still equals the "
    "spec's text.  Everything else was made by running muster_approval.py and "
    "fleet-ops-control.py themselves: answers[].line and lineSHA256 (the whole answer line, one "
    "canonical-JSON line and a line feed), badSignatures, generatedAnswers, generatedPlans and "
    "resourceFieldScales.  To regenerate this file, run python3 scripts/make_muster_vectors.py "
    "--write; a test regenerates it and fails when it differs, so it cannot drift from the code.  "
    "The chain vectors' lists are here too: lists holds every list section 16 prints, keyed by the "
    'name its chain table uses (v1 to v4, "v4 at 20:55" and the T-lists, such as "T6 list"), and '
    "historyRotated is the two-line owner history that T24 alone runs under.  Each chainVectors row "
    "gives its id, held list (null for nothing), chain, history, ruleC, frozenExpected (what a "
    "Milestone 1 to 4 helper must answer, where every rule C entry is refused reopen_unavailable) and "
    "proposalExpected (the printed cell: for a rule C row, the OPEN proposal's answer, which the "
    "frozen helper must not be tested against).  The entry hashes are not here: the chain slice "
    "computes them from the lists, and section 16 prints them."
)

BASE_APPROVAL = {
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "host": "server",
    "action": "restart",
    "target": "fleet-server-status.timer",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
    "issuedAt": "2026-11-07T21:00:00Z",
    "expiresAt": "2026-11-07T21:02:00Z",
}
JOB = {
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
}
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
        "updates": [
            {"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"}
        ],
        "coolify": {"redeploy": True},
        "resourcePolicy": True,
    },
}
UPDATE_PROPERTIES = (
    "LoadState=loaded\nActiveState={active}\nSubState=dead\nJob=\nType=oneshot\n"
    "InvocationID={invocation}\nResult=success\nExecMainStatus=0\nExecMainCode={code}\n"
    "ExecMainStartTimestampMonotonic={started}\nExecMainExitTimestampMonotonic={exited}\n"
)
IDLE_UNIT = {"active": "inactive", "invocation": "", "code": 0, "started": 0, "exited": 0}
JOURNAL_TEXT = "line one\nline two\nnaïve three\n"


# MARK: - The modules under test, loaded from this folder by path


_MODULES = {}


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, str(HERE / relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def modules():
    """(muster_approval, muster_trust_files, fleet-ops-control, fleet_resource_control)."""
    if not _MODULES:
        _MODULES["ma"] = _load("muster_vectors_ma", "muster_approval.py")
        _MODULES["mt"] = _load("muster_vectors_mt", "muster_agent/muster_trust_files.py")
        _MODULES["foc"] = _load("muster_vectors_foc", "fleet-ops-control.py")
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import fleet_resource_control

        _MODULES["frc"] = fleet_resource_control
    return _MODULES["ma"], _MODULES["mt"], _MODULES["foc"], _MODULES["frc"]


# MARK: - What section 16 prints


def from_spec(text):
    """The part of the fixture that section 16 of the spec prints, read out of its text."""

    def block_after(label):
        return re.search(re.escape(label) + r".*?```(?:json)?\n(.*?)\n```", text, re.DOTALL).group(
            1
        )

    fixture = {"specVersion": 5 if "Spec version 5" in text[:200] else 4}
    fixture["keys"] = {}
    for m in re.finditer(
        r"\*\*([^*\n]+)\*\*  \(fingerprint `([0-9a-f ]+)`\)\n\n```\npublic, base64url SPKI DER:  (\S+)",
        text,
    ):
        fixture["keys"][m.group(1)] = {"fingerprint": m.group(2), "public": m.group(3)}
    fixture["canonical"] = {
        "source": block_after("given here out of order and with spaces"),
        "expected": block_after("Its canonical bytes, exactly"),
        "sha256": re.search(
            r"SHA-256:  `([0-9a-f]{64})`", text[text.index("### Canonical Form") :]
        ).group(1),
    }
    fixture["pin"] = block_after("`/etc/muster/owner.pub`:")
    fixture["history"] = block_after("`/etc/muster/owner-history.jsonl` (one line):")
    fixture["historyRotated"] = block_after("`/etc/muster/owner-history.jsonl` for T24 only")
    fixture["reopen"] = block_after("(two lines for version 4").splitlines()
    # Every printed list, keyed by the chain table's name for it ("v1", "v4 at 20:55", "T6 list").
    fixture["lists"] = {}
    for m in re.finditer(
        r"\*\*(Version [^*]+|T\d+ list)\*\*, (?:canonical body SHA-256 `[0-9a-f]{64}`, )?"
        r"entry SHA-256 `[0-9a-f]{64}`:\n\n```json\n(.*?)\n```",
        text,
        re.DOTALL,
    ):
        fixture["lists"][LIST_NAMES.get(m.group(1), m.group(1))] = json.loads(m.group(2))
    assert set(LIST_NAMES.values()) <= set(fixture["lists"]), sorted(fixture["lists"])
    fixture["listBodyHashes"] = {
        "v1": re.search(r"\*\*Version 1\*\*, canonical body SHA-256 `([0-9a-f]{64})`", text).group(
            1
        ),
        "v3": re.search(
            r"\*\*Version 3 \(rule B, closed\)\*\*, canonical body SHA-256 `([0-9a-f]{64})`", text
        ).group(1),
        "v4": re.search(
            r"\*\*Version 4 \(rule C, reopened\)\*\*, canonical body SHA-256 `([0-9a-f]{64})`",
            text,
        ).group(1),
    }
    fixture["approvals"] = []
    for m in re.finditer(
        r"#### (V\d+) ([^\n]*)\n\n([^\n]*)\n\n```\nstored list:\s+([^\n]*)\nhelper clock \(now\):\s+(\S+)\n"
        r"ledger nonces:\s+([^\n]*)\nhelper plan SHA-256:\s+(\S+)\nexpected outcome:\s+(\S+)\n"
        r"approval text bytes \(hex\):\n(\S+)\napproval text SHA-256: (\S+)\n"
        r"signature \(base64url DER\): (\S+)\nstdin bytes \((\d+) bytes\):\n(\{[^\n]*\})\n```",
        text,
    ):
        ledger = (
            [] if m.group(6).strip() == "(none)" else [n.strip() for n in m.group(6).split(",")]
        )
        stored = m.group(4).strip()
        fixture["approvals"].append(
            {
                "id": m.group(1),
                "title": m.group(2),
                "note": m.group(3),
                "stored": "none"
                if stored == "nothing"
                else ("v3" if stored.startswith("version 3") else "v1"),
                "now": m.group(5),
                "ledger": ledger,
                "helperPlanSHA256": m.group(7),
                "expected": m.group(8),
                "textHex": m.group(9),
                "textSHA256": m.group(10),
                "signature": m.group(11),
                "stdinLength": int(m.group(12)),
                "stdin": m.group(13),
            }
        )
    assert len(fixture["approvals"]) == 16, len(fixture["approvals"])
    fixture["answers"] = {}
    for name, label in (
        ("plan", "**A signed plan for V1's job"),
        ("result", "**A done result for V1"),
        ("refusalV3", "**The refusal for V3"),
        ("refusalV6", "**The refusal for V6"),
        ("chainRefusalT7", "**A chain refusal for T7"),
        ("ackT2", "**The acknowledgement after T2"),
        ("ackT3", "**The acknowledgement after T3"),
    ):
        start = text.index(label)
        sha = re.search(
            r"canonical body SHA-256 `([0-9a-f]{64})`", text[start : start + 400]
        ).group(1)
        fixture["answers"][name] = {"answer": json.loads(block_after(label)), "bodySHA256": sha}
    fixture["illustrativePlan"] = block_after("### Illustrative Plan")
    fixture["chainVectors"] = [
        chain_vector(fixture["lists"], row)
        for row in (
            r.split(" | ") for r in re.findall(r"^\| (T\d+ [^\n]*) \|$", text, re.MULTILINE)
        )
    ]
    rule_c = {item["id"].split(" ")[0] for item in fixture["chainVectors"] if item["ruleC"]}
    assert rule_c == RULE_C_ROWS, sorted(rule_c)
    return fixture


# The chain table's names for the lists printed under "Trust Lists";  the bad lists keep their own.
LIST_NAMES = {
    "Version 1": "v1",
    "Version 2 (rule A)": "v2",
    "Version 3 (rule B, closed)": "v3",
    "Version 4 (rule C, reopened)": "v4",
    "Version 4, signed again at 20:55 (T22, R5)": "v4 at 20:55",
}
# The rows section 16 names as rule C rows, and T23, the frozen judgement of T4.
RULE_C_ROWS = {"T4", "T5", "T8", "T15", "T18", "T20", "T21", "T22", "T23"}
# The reasons a rule C entry can fail with under the OPEN proposal, all of them in check 8.
RULE_C_REASONS = {
    "services_changed",
    "server_keys_removed",
    "countersignature",
    "reopen_unrecorded",
    "reopen_tag",
    "reopen_device",
    "reopen_unavailable",
}


def chain_vector(lists, row):
    """One chain table row as data.  `proposalExpected` is the printed cell, the OPEN proposal's
    answer for the rule C rows;  `frozenExpected` is what a Milestone 1 to 4 helper must answer
    (section 16:  every rule C entry is refused `reopen_unavailable` once checks 1 to 7 pass)."""
    vector_id, held_cell, chain_cell, expected_cell = (cell.strip() for cell in row[:4])
    held = None if held_cell == "nothing" else held_cell
    chain = [name.strip() for name in chain_cell.split(",")]
    held_version = None if held is None else lists[held]["body"]["version"]
    rule_c_at = None
    previous = None if held is None else lists[held]
    for index, name in enumerate(chain, 1):
        entry = lists[name]
        if previous is not None and previous["body"]["closed"] and not entry["body"]["closed"]:
            rule_c_at = index
            break
        previous = entry
    reason = re.search(r"reason `(\w+)`", expected_cell)
    if rule_c_at is not None:
        assert reason is None or reason.group(1) in RULE_C_REASONS, row
        frozen = {"atEntry": rule_c_at, "reason": "reopen_unavailable"}
    elif reason is None:
        assert expected_cell == "ack", row
        frozen = "ack"
    else:
        assert len(chain) == 1, row
        frozen = {"atEntry": 1, "reason": reason.group(1)}
    if frozen != "ack":
        frozen.update(errorCode="chain_refused", helperTrustListVersion=held_version)
    return {
        "id": vector_id,
        "held": held,
        "chain": chain,
        "history": "historyRotated" if vector_id.startswith("T24 ") else "history",
        "ruleC": rule_c_at is not None,
        "frozenExpected": frozen,
        # T23 sits in the frozen table, so its printed cell is the frozen answer;  under the OPEN proposal
        # it is T4's chain (held v3, chain v4), which the proposal acknowledges.
        "proposalExpected": "ack" if vector_id.startswith("T23 ") else expected_cell,
    }


# MARK: - Test keys and builders


def derive(label):
    digest = hashlib.sha256(("muster test key " + label).encode()).digest()
    value = int.from_bytes(digest, "big") % (CURVE_ORDER - 1) + 1
    return ec.derive_private_key(value, ec.SECP256R1())


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def sign(key, data):
    return key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))


def approval_text(approval):
    names = ("jobId", "jobNonce", "approvalNonce", "host", "action", "target", "planSHA256")
    names += ("deviceId", "issuedAt", "expiresAt")
    return "\n".join(["muster-approve/1"] + [approval[name] for name in names]).encode("ascii")


def signed_approval(signer, **fields):
    document = dict(BASE_APPROVAL, **fields)
    document["signature"] = b64u(sign(signer, approval_text(document)))
    return document


def approval_stdin(document):
    return compact({"protocol": "muster-helper/1", "approval": document}).decode("ascii")


def job_stdin(**changes):
    return compact({"protocol": "muster-helper/1", "job": dict(JOB, **changes)}).decode("ascii")


CURRENT_STDIN = compact({"protocol": "muster-helper/1"}).decode("ascii")


def der_integer(content):
    return b"\x02" + bytes([len(content)]) + content


def minimal_integer(value):
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return b"\x00" + raw if raw[0] & 0x80 else raw


def der_sequence(content, long_form=False):
    if long_form:
        return b"\x30\x81" + bytes([len(content)]) + content
    return b"\x30" + bytes([len(content)]) + content


def der_signature(r_content, s_content, **kwargs):
    return der_sequence(der_integer(r_content) + der_integer(s_content), **kwargs)


# MARK: - A private world for one request


class World:
    """The files one request meets, in a temporary directory: `/etc/muster`, the state directory with the
    helper key, the stored list, the ledger and the rate file, the helper's allowlist, and the reports."""

    def __init__(self, base, setup):
        ma, mt, foc, frc = modules()
        self.base, self.setup = base, setup
        self.tmp = tempfile.TemporaryDirectory(prefix="muster-vectors-")
        self.root = Path(os.path.realpath(self.tmp.name))
        self.etc = self.root / "etc-muster"
        self.etc.mkdir()
        os.chmod(self.etc, 0o755)
        self.write(self.etc / "owner.pub", (base["pin"] + "\n").encode(), 0o644)
        self.write(self.etc / "owner-history.jsonl", (base["history"] + "\n").encode(), 0o644)
        self.home = self.root / "home"
        self.state = self.home / ".local" / "state" / "fleet-tools"
        self.state.mkdir(parents=True)
        for folder in (self.home, self.home / ".local", self.home / ".local" / "state", self.state):
            os.chmod(folder, 0o700)
        if setup.get("key", True):
            pem = derive("server helper").private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self.write(self.state / "muster-helper.key", pem, 0o600)
        stored = setup.get("stored", "v1")
        if stored != "none":
            document = json.dumps(base["lists"][stored]).encode()
            self.write(self.state / "muster-trust.json", document, 0o600)
        if setup.get("ledger"):
            lines = b"".join(self.ledger_line(nonce) for nonce in setup["ledger"])
            self.write(self.state / "muster-nonces.jsonl", lines, 0o600)
        if setup.get("rate"):
            events = [list(event) for event in setup["rate"]]
            document = ma.canonical_bytes({"contract": "muster-rate/1", "events": events})
            self.write(self.state / "muster-rate.json", document, 0o600)
        if setup.get("report") is not None:
            report = {
                "tool": "fleet-server-status",
                "generatedAt": ma.format_time(NOW_EPOCH - 30),
                "coolify": setup["report"],
            }
            self.write(self.state / "server-status.json", json.dumps(report).encode(), 0o600)
        if setup.get("audit"):
            data = b"".join(compact(row) + b"\n" for row in setup["audit"])
            self.write(self.state / "ops-audit.jsonl", data, 0o600)
        self.allowlist = self.root / "fleet-ops-control.json"
        self.write(self.allowlist, json.dumps(ALLOWLIST).encode(), 0o600)

    @staticmethod
    def write(path, data, mode):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.fchmod(descriptor, mode)
            os.write(descriptor, data)
        finally:
            os.close(descriptor)

    @staticmethod
    def ledger_line(nonce):
        ma, _, _, _ = modules()
        row = {
            "approvalNonce": nonce,
            "deviceId": BASE_APPROVAL["deviceId"],
            "expiresAt": BASE_APPROVAL["expiresAt"],
            "jobId": BASE_APPROVAL["jobId"],
            "recordedAt": "2026-11-07T21:00:00Z",
        }
        return ma.canonical_bytes(row) + b"\n"

    def close(self):
        self.tmp.cleanup()


def _runner(setup):
    status = setup.get("runnerStatus", 0)
    journal = JOURNAL_TEXT.encode("utf-8")
    properties = UPDATE_PROPERTIES.format(**setup.get("updateProperties", IDLE_UNIT)).encode()

    def run(argv, timeout=None, env=None, want_stdout=False, max_bytes=None, **kwargs):
        if "show" in argv:
            return 0, properties
        if "-l" in argv:
            return 0, b""
        if argv and argv[0].endswith("journalctl"):
            return status, journal
        return status, b""

    return run


def _resource_context(ctx, frc):
    """The fake policy store and host of a resource-policy request: no Infisical, no reload."""

    class Store:
        def __init__(self):
            self.p = policy(frc)

        def get(self):
            return json.loads(json.dumps(self.p))

        def save(self, value):
            self.p = json.loads(json.dumps(value))

    store = Store()
    ctx.resource_store = store
    ctx.resource_host_bounds = lambda: (16, 32 * frc.GIB)
    ctx.resource_snapshot = dict
    ctx.resource_audit = lambda event: None
    # A fake clock that a sleep advances, so that a wait the broker makes for the controller to report
    # the policy in effect ends at its deadline instead of for ever, whatever code is behind it.
    clock = [0.0]
    ctx.resource_monotonic = lambda: clock[0]
    ctx.resource_sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    return store


def policy(frc):
    """The resource policy the vectors start from (the placeholder names are not real units)."""
    return dict(
        schema_version=1,
        budget_bytes=8 * frc.GIB,
        critical_budget_bytes=6 * frc.GIB,
        reserve_bytes=6 * frc.GIB,
        pressure_available_bytes=4 * frc.GIB,
        critical_available_bytes=2 * frc.GIB,
        recovery_available_bytes=8 * frc.GIB,
        pressure_some_avg10=2.0,
        critical_full_avg10=5.0,
        recovery_some_avg10=0.5,
        recovery_seconds=60,
        tick_seconds=5,
        refresh_seconds=300,
        discovery_seconds=30,
        coding_units=["private.service"],
        coding_containers=["private-container"],
        production_container_prefixes=["private-prefix"],
        production_name_prefixes=["private-name"],
        cpu_cores=12,
        cpu_weight=50,
        allow_borrowing=True,
    )


def resource_revision():
    _, _, _, frc = modules()
    return frc.revision(policy(frc))


def run_request(base, setup, command, stdin, now=NOW):
    """(answer object or None, the answer line, the exit status) of one `--from-muster` request, made by
    the module and the helper themselves in a private world."""
    ma, mt, foc, frc = modules()
    world = World(base, setup)
    try:
        clock = ma.parse_time(now)
        ctx = foc.Context(
            platform="linux",
            home=world.home,
            runner=_runner(setup),
            config_path=world.allowlist,
            now=lambda: clock,
            environ={"HOME": str(world.home)},
        )
        if setup.get("resource"):
            _resource_context(ctx, frc)
        config = ma.MusterConfig(
            str(world.etc),
            str(world.state),
            os.getuid(),
            os.getuid(),
            lambda: clock,
            lambda: CLOCK_STATE,
        )
        raw = stdin.encode("ascii") if isinstance(stdin, str) else stdin
        patches = [
            mock.patch.object(foc, "_systemctl", lambda: SYSTEMCTL),
            mock.patch.object(foc, "_journalctl", lambda: JOURNALCTL),
            mock.patch.object(
                foc,
                "secrets",
                types.SimpleNamespace(token_hex=lambda n=32: (AUDIT_ID * 8)[: 2 * n]),
            ),
        ]
        if setup.get("updateWorld"):
            patches += [
                mock.patch.object(foc.os.path, "isfile", lambda path: True),
                mock.patch.object(foc.os, "access", lambda path, mode: True),
            ]
        if setup.get("unreadableExecute"):
            patches.append(mock.patch.object(foc, "execute", _lost_status))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            answer, code = ma.handle_request(
                config,
                foc._MusterBackend(ctx, ma),
                command,
                True,
                lambda limit: raw[: limit + 1],
                mt,
            )
        return answer, ma.encode_answer(answer), code
    finally:
        world.close()


def _lost_status(*args, **kwargs):
    raise OSError("the status of the child was lost")


def plan_of(base, setup, command, stdin):
    """The signed plan answer's `planSHA256`, taken the way the phone takes it."""
    answer, _, code = run_request(base, setup, command, stdin)
    assert code == 0 and answer["kind"] == "plan", answer
    return answer["body"]["planSHA256"]


# MARK: - What the code makes of the vectors


def line_fields(answer_object):
    ma, _, _, _ = modules()
    line = ma.encode_answer(answer_object)
    return {"line": line, "lineSHA256": hashlib.sha256(line.encode("ascii")).hexdigest()}


def body_sha256(answer_object):
    ma, _, _, _ = modules()
    return hashlib.sha256(ma.canonical_bytes(answer_object["body"])).hexdigest()


def signed_entry(base, entry_id, title, command, stdin, setup, now=NOW, plan=False):
    """One entry with its request and its answer, both as the helper made them."""
    answer, line, code = run_request(base, setup, command, stdin, now)
    entry = {
        "id": entry_id,
        "title": title,
        "command": command,
        "stdin": stdin,
        "setup": setup,
        "now": now,
        "exitStatus": code,
        "answer": answer,
        "line": line,
        "lineSHA256": hashlib.sha256(line.encode("ascii")).hexdigest(),
    }
    if answer["signature"] is not None:
        entry["bodySHA256"] = body_sha256(answer)
    if plan:
        entry["plan"] = answer["body"]["plan"]
        entry["planSHA256"] = answer["body"]["planSHA256"]
    return entry


def approval_for(base, setup, verb, target, signer_label="device A approval", **fields):
    """The stdin of a signed approval of what the helper plans for `verb target`."""
    plan_command = f"server {verb} {target} --plan"
    digest = plan_of(base, setup, plan_command, job_stdin())
    document = signed_approval(
        derive(signer_label), action=verb, target=target, planSHA256=digest, **fields
    )
    return f"server {verb} {target} --yes", approval_stdin(document)


def bad_signature_cases(base, valid):
    """(id, title, signature string, note) for each way a signature can be wrong, from V1's own."""
    ma, _, _, _ = modules()
    der = unb64u(valid)
    r, s = ma.parse_der_signature(der)
    flipped = valid[:-1] + ALPHABET[ALPHABET.index(valid[-1]) ^ 1]
    assert len(der) % 3 != 0, "V1's signature has no spare bits in its last character"
    assert unb64u(flipped) == der and flipped != valid
    cases = [
        (
            "leading-zero",
            "An INTEGER with an unnecessary leading zero byte",
            b64u(der_signature(b"\x00" + minimal_integer(r), minimal_integer(s))),
        ),
        (
            "negative-integer",
            "An INTEGER whose high bit is set and that has no zero byte before it (a negative number)",
            b64u(der_signature((r | 1 << 255).to_bytes(32, "big"), minimal_integer(s))),
        ),
        ("trailing-byte", "A byte after the SEQUENCE", b64u(der + b"\x00")),
        (
            "long-form-length",
            "The SEQUENCE length in long form (0x81) where the short form is the only one allowed",
            b64u(
                der_signature(minimal_integer(r), minimal_integer(s), long_form=True),
            ),
        ),
        (
            "r-zero",
            "r equal to 0",
            b64u(der_signature(b"\x00", minimal_integer(s))),
        ),
        (
            "s-equals-n",
            "s equal to the curve order n",
            b64u(der_signature(minimal_integer(r), minimal_integer(CURVE_ORDER))),
        ),
        ("truncated", "The DER cut one byte short (a wrong length)", b64u(der[:-1])),
        (
            "stray-trailing-bits",
            "The same bytes, but the last base64url character carries bits the length leaves unused",
            flipped,
        ),
        (
            "impossible-length",
            "A base64url string whose length is 1 modulo 4, which no byte string encodes to",
            valid[: len(valid) // 4 * 4 + 1],
        ),
        (
            "padding-present",
            "The signature with base64 padding",
            valid + "=" * (-len(valid) % 4 or 1),
        ),
        (
            "over-100-characters",
            "More than the 100 characters a signature field may hold",
            "A" * 101,
        ),
        ("empty", "An empty signature", ""),
    ]
    high_s = b64u(der_signature(minimal_integer(r), minimal_integer(CURVE_ORDER - s)))
    return cases, high_s


def bad_signatures(base):
    valid_v1 = base["approvals"][0]
    document = json.loads(valid_v1["stdin"])["approval"]
    setup = {"stored": "v1", "ledger": []}
    cases, high_s = bad_signature_cases(base, valid_v1["signature"])
    out = []

    def entry(case_id, title, signature):
        stdin = approval_stdin(dict(document, signature=signature))
        answer, line, code = run_request(base, setup, RUN_COMMAND, stdin)
        body = answer["body"]
        result = {
            "id": case_id,
            "title": title,
            "signature": signature,
            "stdin": stdin,
            "stored": "v1",
            "now": NOW,
            "exitStatus": code,
            "answer": answer,
            "bodySHA256": body_sha256(answer),
            "line": line,
            "lineSHA256": hashlib.sha256(line.encode("ascii")).hexdigest(),
        }
        if answer["kind"] == "refusal":
            result["expected"] = {
                "kind": "refusal",
                "errorCode": body["errorCode"],
                "verified": body["verified"],
            }
        else:
            result["expected"] = {"kind": answer["kind"], "outcome": body["outcome"]}
        return result

    for case_id, title, signature in cases:
        out.append(entry(case_id, title, signature))
    out.append(
        entry(
            "high-s-twin",
            "The other form of V1's signature, s replaced by n - s:  valid, and accepted (no low-S rule)",
            high_s,
        )
    )
    return out


def generated_answers(base):
    ma, _, _, _ = modules()
    out = []
    held = {"stored": "v1"}

    # An acknowledgement with nothing held.
    out.append(
        signed_entry(
            base,
            "ackNothingHeld",
            "The acknowledgement when the helper holds no list",
            CURRENT_COMMAND,
            CURRENT_STDIN,
            {"stored": "none"},
        )
    )
    # A refusal at a plan request, with the planner's own reason.
    out.append(
        signed_entry(
            base,
            "planRefusedAllowlistMiss",
            "A plan request for a unit that is not on the allowlist: plan_refused, reason allowlist_miss",
            "server restart not-listed.service --plan",
            job_stdin(),
            held,
        )
    )
    # A read result with output.
    command, stdin = approval_for(base, held, "logs", "fleet-server-status.timer")
    out.append(
        signed_entry(
            base,
            "logsResult",
            "An approved journal read: a done result whose outputB64 is the log text (UTF-8)",
            command,
            stdin,
            held,
        )
    )
    receipt = f"fleet-cli.{RECEIPT_INVOCATION}.{RECEIPT_BASELINE}"
    audit_row = {
        "time": "2026-11-07T20:00:00Z",
        "host": "server",
        "verb": "server update",
        "target": "fleet-cli",
        "result": "submitted",
        "route": "sudo-system-update",
        "invocationID": RECEIPT_INVOCATION,
        "baselineStartMonotonic": RECEIPT_BASELINE,
    }
    update_status = {
        "stored": "v1",
        "audit": [audit_row],
        "updateProperties": {
            "active": "inactive",
            "invocation": RECEIPT_INVOCATION,
            "code": 1,
            "started": 900,
            "exited": 950,
        },
    }
    out.append(
        signed_entry(
            base,
            "updateStatusRead",
            "A read verb: the update's status, a planned result with outputB64 and the request's claimed job",
            f"server update-status {receipt} --plan",
            job_stdin(),
            update_status,
        )
    )
    # A refusal at a plan request for a receipt that does not match: the planner's own reason.
    mismatched = dict(update_status, audit=[dict(audit_row, baselineStartMonotonic=499)])
    out.append(
        signed_entry(
            base,
            "planRefusedReceiptMismatch",
            "update-status with a baseline that is not the recorded one: plan_refused, reason receipt_context_mismatch",
            f"server update-status {receipt} --plan",
            job_stdin(),
            mismatched,
        )
    )
    # An approval whose plan is refused after the nonce is spent: a failed result naming the reason.
    busy = {
        "stored": "v1",
        "updateWorld": True,
        "updateProperties": {
            "active": "activating",
            "invocation": "",
            "code": 0,
            "started": 0,
            "exited": 0,
        },
    }
    idle = dict(busy, updateProperties=dict(busy["updateProperties"], active="inactive"))
    command, stdin = approval_for(base, idle, "update", "fleet-cli")
    out.append(
        signed_entry(
            base,
            "failedResultPlanRefused",
            "An approved update whose unit is busy when it runs: a failed result, detail plan_refused: update_busy",
            command,
            stdin,
            busy,
        )
    )
    # An unknown outcome.
    killed = {"stored": "v1", "runnerStatus": 124}
    command, stdin = approval_for(base, held, "restart", "fleet-server-status.timer")
    out.append(
        signed_entry(
            base,
            "unknownResult",
            "An approved restart whose command was killed at its time limit: outcome unknown, exit status 1",
            command,
            stdin,
            killed,
        )
    )
    # A verified rate limit: a restart of the same unit was counted one second earlier.
    rate = {
        "stored": "v1",
        "rate": [["yes", "restart fleet-server-status.timer", (NOW_EPOCH - 1) * 1000]],
    }
    out.append(
        signed_entry(
            base,
            "rateLimitedRefusal",
            "A verified approval inside the two-second spacing of the last restart: rate_limited, nonce unspent",
            command,
            stdin,
            rate,
        )
    )
    # A verified plan mismatch, for a resource-policy change (the comparison is made under its own lock).
    resource = {"stored": "v1", "resource": True}
    target = f"cpu-weight.100.{resource_revision()}"
    document = signed_approval(
        derive("device A approval"),
        action="resource-policy",
        target=target,
        planSHA256=hashlib.sha256(b"not the plan").hexdigest(),
    )
    out.append(
        signed_entry(
            base,
            "planMismatchResourcePolicy",
            "A verified resource-policy approval whose plan hash is not the plan's: plan_mismatch, nonce spent",
            f"server resource-policy {target} --yes",
            approval_stdin(document),
            resource,
        )
    )
    # The one unsigned answer.
    out.append(
        signed_entry(
            base,
            "helperKeyMissing",
            "The unsigned answer when the helper has no usable key: helper_key_missing, keyId and signature null",
            RUN_COMMAND,
            approval_stdin(json.loads(base["approvals"][0]["stdin"])["approval"]),
            {"stored": "v1", "key": False},
        )
    )
    return out


def generated_plans(base):
    _, _, _, frc = modules()
    out = []
    held = {"stored": "v1"}
    out.append(
        signed_entry(
            base,
            "restartPlanUserScope",
            "restart of a user-scope unit: the argv has --user",
            "server restart fleet-server-status.timer --plan",
            job_stdin(jobId="jobplanuser01"),
            held,
            plan=True,
        )
    )
    out.append(
        signed_entry(
            base,
            "restartPlanSystemScope",
            "restart of a system-scope unit: no --user in the argv",
            "server restart botfleet-server.service --plan",
            job_stdin(jobId="jobplansystem1"),
            held,
            plan=True,
        )
    )
    report = [
        {
            "uuid": APPLICATION,
            "name": "Café App",
            "type": "application",
            "status": "running",
            "uuidSource": "label",
        }
    ]
    out.append(
        signed_entry(
            base,
            "redeployPlanNonAscii",
            "redeploy of an application named Café App: the plan's auditTarget has one ? for the é",
            f"server redeploy {APPLICATION} --plan",
            job_stdin(jobId="jobplanredeploy"),
            {"stored": "v1", "report": report},
            plan=True,
        )
    )
    revision = resource_revision()
    resource = {"stored": "v1", "resource": True}
    for entry_id, title, target in (
        (
            "resourcePlanCpuWeight",
            "cpu-weight 50 to 100: old and new are the value itself (scale 1)",
            f"cpu-weight.100.{revision}",
        ),
        (
            "resourcePlanRamBudget",
            "ram-budget-gib 8 to 12: old and new are in bytes (scale 1073741824)",
            f"ram-budget-gib.12.{revision}",
        ),
        (
            "resourcePlanBorrowIdle",
            "borrow-idle true to 0: old and new are the policy's own values (true, then a number)",
            f"borrow-idle.0.{revision}",
        ),
    ):
        out.append(
            signed_entry(
                base,
                entry_id,
                title,
                f"server resource-policy {target} --plan",
                job_stdin(jobId="jobplanresource"),
                resource,
                plan=True,
            )
        )
    return out


def resource_field_scales():
    _, _, _, frc = modules()
    return {field: scale for field, (key, scale) in sorted(frc.FIELDS.items())}


def generate(spec_text):
    """The whole fixture, as an object."""
    ma, _, _, _ = modules()
    assert ma.parse_time(NOW) == NOW_EPOCH
    fixture = from_spec(spec_text)
    for name, entry in fixture["answers"].items():
        entry.update(line_fields(entry["answer"]))
    fixture["about"] = ABOUT
    fixture["badSignatures"] = bad_signatures(fixture)
    fixture["generatedAnswers"] = generated_answers(fixture)
    fixture["generatedPlans"] = generated_plans(fixture)
    fixture["resourceFieldScales"] = resource_field_scales()
    return fixture


def render(fixture):
    return json.dumps(fixture, indent=1, sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when the committed file differs")
    mode.add_argument("--write", action="store_true", help="rewrite the committed file")
    args = parser.parse_args(argv)
    made = render(generate(SPEC_PATH.read_text(encoding="utf-8")))
    if args.write:
        FIXTURE_PATH.write_text(made, encoding="utf-8")
        print("wrote", FIXTURE_PATH)
        return 0
    if FIXTURE_PATH.read_text(encoding="utf-8") != made:
        print(
            f"{FIXTURE_PATH} is not what this generator makes; run it with --write", file=sys.stderr
        )
        return 1
    print("the fixture is what the generator makes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
