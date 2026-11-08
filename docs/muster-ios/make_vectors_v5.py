"""Generate the test vectors for server-route-spec.md, version 5.

Every key here is a TEST key derived from a fixed label.  No real key is read, used or printed.

Run from the folder holding server-route-spec.md:  /usr/bin/python3 -I make_vectors_v5.py
It checks every vector against the reference checker below, then writes the vectors into
section 16 of server-route-spec.md (replacing "<!-- VECTORS -->", or the section's old body).
"""

import base64
import datetime
import hashlib
import hmac
import json
import re
import sys

import cryptography
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

SPEC = "server-route-spec.md"
MARK = "<!-- VECTORS -->"
CURVE = ec.SECP256R1()
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
FINAL = {
    "owner_signature",
    "duplicate_device",
    "countersignature",
    "closed_rule",
    "first_rule",
    "services_changed",
    "server_keys_removed",
    "reopen_tag",
    "reopen_device",
}


def ecdsa():
    try:
        return ec.ECDSA(hashes.SHA256(), deterministic_signing=True)
    except TypeError:
        return ec.ECDSA(hashes.SHA256())


def test_key(label):
    scalar = (
        int.from_bytes(hashlib.sha256(("muster test key " + label).encode()).digest(), "big")
        % (N - 1)
        + 1
    )
    return ec.derive_private_key(scalar, CURVE)


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


B64U_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def unb64u(text):
    """Strict base64url (v5.4):  the alphabet only, no padding, a possible length, no stray trailing bits."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u(data) != text:
        raise ValueError("base64url with stray trailing bits")
    return data


# DER SubjectPublicKeyInfo of an uncompressed P-256 point (v5.4):  this prefix, then 04 || X || Y, 91 bytes.
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def load_key(text):
    """The P-256 key of a base64url SubjectPublicKeyInfo that is exactly the 91-byte uncompressed form."""
    der = unb64u(text)
    if len(der) != 91 or not der.startswith(P256_SPKI_PREFIX) or der[26] != 0x04:
        raise ValueError("not the 91-byte SubjectPublicKeyInfo of an uncompressed P-256 point")
    key = serialization.load_der_public_key(der)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 key")
    return key


def spki(key):
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def fingerprint(der):
    h = hashlib.sha256(der).hexdigest()
    return " ".join(h[i : i + 4] for i in range(0, 64, 4))


def check_printable(value):
    if isinstance(value, str):
        if not all(0x20 <= ord(c) <= 0x7E for c in value):
            raise ValueError("not printable ASCII")
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("integer out of range")
    elif isinstance(value, list):
        for v in value:
            check_printable(v)
    elif isinstance(value, dict):
        for k, v in value.items():
            check_printable(k)
            check_printable(v)
    else:
        raise ValueError("type not allowed")


def canonical(obj):
    check_printable(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sign(key, data):
    return key.sign(data, ecdsa())


def verify(pub_b64, sig_b64, data):
    """v5.4:  the key and the signature are decoded strictly, so stray trailing bits fail the check."""
    try:
        load_key(pub_b64).verify(unb64u(sig_b64), data, ecdsa())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def t(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)


def real_time(text):
    """v5.4:  §2's form and a real calendar time (no 23:59:60, no February 30, no year 0000)."""
    if not isinstance(text, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", text
    ):
        return False
    try:
        datetime.datetime(
            int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]), int(text[17:19])
        )
    except ValueError:
        return False
    return True


def reopen_check(version, device_id, code):
    return sha(f"muster-reopen-check/1\n{version}\n{device_id}\n{code}".encode("ascii"))[:4]


def reopen_display(version, device_id, code):
    return (
        "-".join(code[i : i + 4] for i in range(0, 32, 4))
        + " "
        + reopen_check(version, device_id, code)
    )


def entry_hash(entry):
    return sha(canonical(entry))


# ---------------------------------------------------------------- keys
owner = test_key("owner")
new_owner = test_key("owner after rotation (T24 only)")
rogue_owner = test_key("rogue owner (not pinned)")
helper = test_key("server helper")
devA = {r: test_key("device A " + r) for r in ("approval", "request", "reading", "notification")}
devB = {r: test_key("device B " + r) for r in ("approval", "request", "reading", "notification")}
devC = {r: test_key("device C " + r) for r in ("approval", "request", "reading", "notification")}
stranger = test_key("device X approval (not on any list)")
rogue_helper = test_key("rogue helper (not enrolled)")


def device_entry(keys, name):
    return {
        "deviceId": "d-" + sha(spki(keys["approval"]))[:24],
        "name": name,
        "approvalKey": b64u(spki(keys["approval"])),
        "requestKey": b64u(spki(keys["request"])),
        "readingKey": b64u(spki(keys["reading"])),
        "notificationKey": b64u(spki(keys["notification"])),
    }


DA, DB, DC = (
    device_entry(devA, "Test Phone A"),
    device_entry(devB, "Test iPad B"),
    device_entry(devC, "Test Phone C"),
)
SERVICES = {
    "macPublisher": None,
    "macExecutor": None,
    "macAgreement": None,
    "serverPublisher": None,
    "serverExecutor": None,
    "serverExecutorAgreement": None,
    "serverHelper": b64u(spki(helper)),
}
OWNER = b64u(spki(owner))
PIN_LINE = OWNER + "\n"
HISTORY_LINE = (
    canonical({"fromVersion": 1, "ownerKey": OWNER, "toSHA256": None, "toVersion": None}).decode()
    + "\n"
)


def make_list(
    version,
    prev,
    issued,
    devices,
    closed=False,
    services=None,
    countersigners=(),
    reopen_code=None,
    owner_key=None,
    signer=None,
    previous=None,
    contract="muster-trust-list/1",
):
    body = {
        "contract": contract,
        "version": version,
        "previousSHA256": previous
        if previous is not None
        else (sha(canonical(prev["body"])) if prev else None),
        "issuedAt": issued,
        "ownerKey": owner_key or OWNER,
        "closed": closed,
        "devices": devices,
        "services": services if services is not None else dict(SERVICES),
        "reopenTag": None,
    }
    if reopen_code is not None:
        body["reopenTag"] = hmac.new(
            reopen_code.encode("ascii"), canonical(body), hashlib.sha256
        ).hexdigest()
    bb = canonical(body)
    signed = {
        "body": body,
        "ownerSignature": b64u(sign(signer or owner, b"muster-trust-list/1\n" + bb)),
        "countersignatures": [],
    }
    for dev_id, key in countersigners:
        signed["countersignatures"].append(
            {"deviceId": dev_id, "signature": b64u(sign(key, b"muster-trust-countersign/1\n" + bb))}
        )
    return signed


CODE_UNUSED = "9d0b3e51a7c2f4086e1d5b9a3c7f2e04"
CODE_USED = "4f2a9c0e7b1d6e3a8c5f0b2d9e7a1c4b"
FP_C = DC["deviceId"]
FP_B = DB["deviceId"]
RECORDS = [
    {"code": CODE_UNUSED, "createdAt": "2026-11-07T20:30:00Z", "deviceId": FP_C, "version": 4},
    {"code": CODE_USED, "createdAt": "2026-11-07T20:45:00Z", "deviceId": FP_C, "version": 4},
]
RECORD_LINES = "".join(canonical(r).decode() + "\n" for r in RECORDS)

A_ = (DA["deviceId"], devA["approval"])
B_ = (DB["deviceId"], devB["approval"])
C_ = (DC["deviceId"], devC["approval"])
L1 = make_list(1, None, "2026-11-07T20:00:00Z", [DA])
L2 = make_list(2, L1, "2026-11-07T20:10:00Z", [DA, DB], countersigners=[A_])
L3 = make_list(3, L2, "2026-11-07T20:20:00Z", [], closed=True)
L4 = make_list(4, L3, "2026-11-07T20:50:00Z", [DC], countersigners=[C_], reopen_code=CODE_USED)
L4b = make_list(4, L3, "2026-11-07T20:55:00Z", [DC], countersigners=[C_], reopen_code=CODE_USED)
bad = {
    "T6": make_list(
        2,
        L1,
        "2026-11-07T20:10:00Z",
        [],
        closed=True,
        services=dict(SERVICES, serverHelper=b64u(spki(rogue_helper))),
    ),
    "T7": make_list(
        2, L1, "2026-11-07T20:10:00Z", [DA, DB], countersigners=[A_], previous="0" * 64
    ),
    "T8": make_list(
        4,
        L3,
        "2026-11-07T20:50:00Z",
        [DC],
        countersigners=[C_],
        reopen_code="00000000000000000000000000000000",
    ),
    "T10": make_list(3, L1, "2026-11-07T20:20:00Z", [DA], countersigners=[A_]),
    "T11": make_list(
        2,
        L1,
        "2026-11-07T20:10:00Z",
        [DA, DB],
        countersigners=[A_],
        owner_key=b64u(spki(rogue_owner)),
    ),
    "T12": make_list(
        2, L1, "2026-11-07T20:10:00Z", [DA, DB], countersigners=[A_], signer=rogue_owner
    ),
    "T13": make_list(2, L1, "2026-11-07T20:10:00Z", [DA, DA], countersigners=[A_]),
    "T14": make_list(2, L1, "2026-11-07T20:10:00Z", [DA, DB], countersigners=[B_]),
    "T15": None,
    "T16": make_list(2, L1, "2026-11-07T20:10:00Z", [DA], closed=True),
    "T17": make_list(1, None, "2026-11-07T20:00:00Z", [DA], previous="0" * 64),
    "T18": make_list(4, L3, "2026-11-07T20:50:00Z", [DC], countersigners=[C_]),
    "T19": make_list(
        2, L1, "2026-11-07T20:10:00Z", [], closed=True, services=dict(SERVICES, serverHelper=None)
    ),
    "T20": make_list(
        4, L3, "2026-11-07T20:50:00Z", [DB], countersigners=[B_], reopen_code=CODE_USED
    ),
    "T21": make_list(
        4, L3, "2026-11-07T20:50:00Z", [DC], countersigners=[C_], reopen_code=CODE_UNUSED
    ),
    # v5.3:  a version 1 other than the one the rotated history closed (check 3, history_hash).
    "T24": make_list(1, None, "2026-11-07T20:05:00Z", [DA]),
    # v5.3 (§17A A8):  rule A that removes both server keys.
    "T25": make_list(
        2,
        L1,
        "2026-11-07T20:10:00Z",
        [DA, DB],
        countersigners=[A_],
        services=dict(SERVICES, serverHelper=None),
    ),
}
# T15:  the same body as v4, countersigned by A instead of C (a tampered copy a hostile hub could make)
bad["T15"] = {
    "body": L4["body"],
    "ownerSignature": L4["ownerSignature"],
    "countersignatures": [
        {
            "deviceId": DA["deviceId"],
            "signature": b64u(
                sign(devA["approval"], b"muster-trust-countersign/1\n" + canonical(L4["body"]))
            ),
        }
    ],
}


def stray_bit(text):
    """The same base64url with one stray trailing bit set in its last character (v5.4).  The bytes a lenient
    decoder reads are unchanged;  only canonical base64url refuses it.  Possible only when the byte length is
    not a multiple of 3."""
    data = unb64u(text)
    assert len(data) % 3, "no stray bits at this length"
    last = B64U_ALPHABET.index(text[-1])
    assert last & 1 == 0
    changed = text[:-1] + B64U_ALPHABET[last | 1]
    assert base64.urlsafe_b64decode(changed + "=" * (-len(changed) % 4)) == data
    return changed


def first_fit(make, field):
    """The first list `make(i)` builds whose signature in `field` has a byte length that can carry stray bits.
    Signing is deterministic (RFC 6979), so the search always ends at the same list."""
    for i in range(50):
        lst = make(i)
        if len(unb64u(field(lst))) % 3:
            return lst
    sys.exit("no list fits")


# T26 (v5.4):  a first list whose owner signature carries a stray trailing bit (check 4, owner_signature).
_t26 = first_fit(
    lambda i: make_list(1, None, f"2026-11-07T20:{i:02d}:30Z", [DA]), lambda lst: lst["ownerSignature"]
)
bad["T26"] = dict(_t26, ownerSignature=stray_bit(_t26["ownerSignature"]))
# T27 (v5.4):  rule A whose one countersignature carries a stray trailing bit (rule A, countersignature).
_t27 = first_fit(
    lambda i: make_list(2, L1, f"2026-11-07T20:{10 + i:02d}:30Z", [DA, DB], countersigners=[A_]),
    lambda lst: lst["countersignatures"][0]["signature"],
)
bad["T27"] = dict(
    _t27,
    countersignatures=[
        dict(_t27["countersignatures"][0], signature=stray_bit(_t27["countersignatures"][0]["signature"]))
    ],
)


# ---------------------------------------------------------------- P3:  section 5's and 8's structure (v5.4 strictness)
DEVICE_ID = re.compile(r"d-[0-9a-f]{24}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SIG_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}")
SERVICE_NAMES = {
    "macPublisher",
    "macExecutor",
    "macAgreement",
    "serverPublisher",
    "serverExecutor",
    "serverExecutorAgreement",
    "serverHelper",
}


def p3_failure(entry):
    """What P3 refuses in one signed list (`bad_input`), or None.  Keys are decoded and loaded here;  a
    signature only has its pattern checked here, and its decoding is judged when it is verified."""

    def key_ok(value):
        try:
            load_key(value)
            return True
        except (ValueError, TypeError):
            return False

    if not isinstance(entry, dict) or set(entry) != {"body", "ownerSignature", "countersignatures"}:
        return "entry keys"
    b = entry["body"]
    if not isinstance(b, dict) or set(b) != {
        "contract",
        "version",
        "previousSHA256",
        "issuedAt",
        "ownerKey",
        "closed",
        "devices",
        "services",
        "reopenTag",
    }:
        return "body keys"
    if b["contract"] != "muster-trust-list/1":
        return "contract"
    if type(b["version"]) is not int or not 1 <= b["version"] <= 2**53 - 1:
        return "version"
    for name in ("previousSHA256", "reopenTag"):
        if b[name] is not None and not (isinstance(b[name], str) and HEX64.fullmatch(b[name])):
            return name
    if not real_time(b["issuedAt"]):
        return "issuedAt"
    if not key_ok(b["ownerKey"]):
        return "ownerKey"
    if type(b["closed"]) is not bool:
        return "closed"
    if not isinstance(b["devices"], list) or len(b["devices"]) > 64:
        return "devices"
    for d in b["devices"]:
        if not isinstance(d, dict) or set(d) != {
            "deviceId",
            "name",
            "approvalKey",
            "requestKey",
            "readingKey",
            "notificationKey",
        }:
            return "device keys"
        if not (isinstance(d["deviceId"], str) and DEVICE_ID.fullmatch(d["deviceId"])):
            return "deviceId"
        if not (isinstance(d["name"], str) and 1 <= len(d["name"]) <= 40):
            return "name"
        if not all(key_ok(d[k]) for k in ("approvalKey", "requestKey", "readingKey", "notificationKey")):
            return "device key"
    s = b["services"]
    if not isinstance(s, dict) or set(s) != SERVICE_NAMES:
        return "services"
    if not all(v is None or key_ok(v) for v in s.values()):
        return "service key"
    if not (isinstance(entry["ownerSignature"], str) and SIG_PATTERN.fullmatch(entry["ownerSignature"])):
        return "ownerSignature"
    cs = entry["countersignatures"]
    if not isinstance(cs, list) or len(cs) > 64:
        return "countersignatures"
    for c in cs:
        if (
            not isinstance(c, dict)
            or set(c) != {"deviceId", "signature"}
            or not (isinstance(c["deviceId"], str) and DEVICE_ID.fullmatch(c["deviceId"]))
            or not (isinstance(c["signature"], str) and SIG_PATTERN.fullmatch(c["signature"]))
        ):
            return "countersignature"
    try:
        canonical(entry)
    except ValueError:
        return "not canonical"
    return None


# ---------------------------------------------------------------- reference checker:  section 8's numbered order
def history_key(history, version):
    for h in history:
        if h["fromVersion"] <= version and (h["toVersion"] is None or version <= h["toVersion"]):
            return h["ownerKey"]
    return None


def check_chain(held, chain, history, records, frozen=False):
    """frozen=True is version 5 as frozen:  every rule C entry is refused reopen_unavailable after checks 1 to 7.
    v5.4:  P3 reads the whole document before any check, so one malformed entry is bad_input for the request."""
    for entry in chain:
        if p3_failure(entry) is not None:
            return "bad_input", entry
    prev = held
    for entry in chain:
        b = entry["body"]
        bb = canonical(b)
        want = 1 if prev is None else prev["body"]["version"] + 1
        if b["version"] != want:  # 1
            return "version_gap", entry
        if prev is None:  # 2
            if b["previousSHA256"] is not None:
                return "first_rule", entry
        elif b["previousSHA256"] != sha(canonical(prev["body"])):
            return "previous_hash", entry
        key = history_key(history, b["version"])
        if key is None or b["ownerKey"] != key:  # 3
            return "owner_key", entry
        closing = [h for h in history if h["toVersion"] == b["version"]]
        if any(h["toSHA256"] != sha(bb) for h in closing):  # 3, v5.3:  every line for this version
            return "history_hash", entry
        if not verify(key, entry["ownerSignature"], b"muster-trust-list/1\n" + bb):  # 4
            return "owner_signature", entry
        ids = [d["deviceId"] for d in b["devices"]]
        if len(ids) != len(set(ids)):  # 5
            return "duplicate_device", entry
        cs = entry["countersignatures"]
        cs_ids = [c["deviceId"] for c in cs]
        if len(cs_ids) != len(set(cs_ids)):  # 6
            return "countersignature", entry
        if b["closed"] != (len(b["devices"]) == 0):  # 7
            return "closed_rule", entry

        def cs_ok(devices):
            by_id = {d["deviceId"]: d for d in devices}
            for c in cs:
                d = by_id.get(c["deviceId"])
                if d is None or not verify(
                    d["approvalKey"], c["signature"], b"muster-trust-countersign/1\n" + bb
                ):
                    return False
            return True

        def services_only_removed():
            return all(
                b["services"][k] in (prev["body"]["services"][k], None) for k in b["services"]
            )

        def server_keys_removed():
            had = (
                prev["body"]["services"]["serverHelper"]
                or prev["body"]["services"]["serverPublisher"]
            )
            return bool(had) and not (
                b["services"]["serverHelper"] or b["services"]["serverPublisher"]
            )

        if prev is None:  # 8 First
            if b["closed"] or cs or b["reopenTag"] is not None:
                return "first_rule", entry
        elif b["closed"]:  # 8 B
            if cs or b["reopenTag"] is not None:
                return "closed_rule", entry
            if not services_only_removed():
                return "services_changed", entry
            if server_keys_removed():
                return "server_keys_removed", entry
        elif prev["body"]["closed"]:  # 8 C
            if frozen:
                return "reopen_unavailable", entry
            if not services_only_removed():
                return "services_changed", entry
            if server_keys_removed():
                return "server_keys_removed", entry
            if sorted(cs_ids) != sorted(ids) or not cs_ok(b["devices"]):
                return "countersignature", entry
            lines = [r for r in records if r["version"] == b["version"]]
            if b["reopenTag"] is None or not lines:
                return "reopen_unrecorded", entry
            line = lines[-1]  # only the newest line for a version counts;  older ones are retired
            untagged = canonical(dict(b, reopenTag=None))
            if not hmac.compare_digest(
                hmac.new(line["code"].encode("ascii"), untagged, hashlib.sha256).hexdigest(),
                b["reopenTag"],
            ):
                return "reopen_tag", entry
            if ids != [line["deviceId"]]:
                return "reopen_device", entry
        else:  # 8 A
            if b["reopenTag"] is not None:
                return "first_rule", entry
            if not cs or not cs_ok(prev["body"]["devices"]):
                return "countersignature", entry
            if server_keys_removed():  # v5.3, §17A A8
                return "server_keys_removed", entry
        prev = entry
    return "accepted", None


HISTORY = [{"fromVersion": 1, "ownerKey": OWNER, "toSHA256": None, "toVersion": None}]
# v5.3, T24 only:  the owner key rotated after version 1, so the closed line names v1's body hash.
NEW_OWNER = b64u(spki(new_owner))
HISTORY_ROTATED = [
    {"fromVersion": 1, "ownerKey": OWNER, "toSHA256": sha(canonical(L1["body"])), "toVersion": 1},
    {"fromVersion": 2, "ownerKey": NEW_OWNER, "toSHA256": None, "toVersion": None},
]
HISTORY_ROTATED_LINES = "".join(canonical(h).decode() + "\n" for h in HISTORY_ROTATED)
CASE_HISTORY = {"T24": HISTORY_ROTATED}  # every other chain vector runs under HISTORY
chain_cases = [
    ("T1 first list", None, [L1], "accepted", "Holding nothing, version 1 under rule First."),
    ("T2 rule A", L1, [L2], "accepted", "Adds device B, countersigned by device A from version 1."),
    (
        "T3 rule B",
        L2,
        [L3],
        "accepted",
        "Closes the list:  no devices, services unchanged, owner signature only.",
    ),
    (
        "T4 rule C after an abandoned first code",
        L3,
        [L4],
        "accepted",
        "Reopens with device C.  Two record lines name version 4;  the list is tagged with the newer one, the only line that counts, and names exactly its device.",
    ),
    (
        "T5 whole history",
        None,
        [L1, L2, L3, L4],
        "accepted",
        "A late verifier walks First, A, B and C from nothing.",
    ),
    (
        "T6 close that swaps a service key",
        L1,
        [bad["T6"]],
        "services_changed",
        "A close may only keep or remove service keys.",
    ),
    (
        "T7 wrong previous hash",
        L1,
        [bad["T7"]],
        "previous_hash",
        "previousSHA256 is all zeros;  nothing else is wrong.",
    ),
    (
        "T8 reopen with an unrecorded code",
        L3,
        [bad["T8"]],
        "reopen_tag",
        "The newest line for version 4 does not make this tag (nor does any other line).",
    ),
    (
        "T10 gap",
        L1,
        [bad["T10"]],
        "version_gap",
        "Version 3 held over version 1, with previousSHA256 naming version 1, so only the version check fails.",
    ),
    (
        "T11 wrong owner key",
        L1,
        [bad["T11"]],
        "owner_key",
        "Names a key the history does not assign;  signed by the pinned key, so only check 3 fails.",
    ),
    (
        "T12 bad owner signature",
        L1,
        [bad["T12"]],
        "owner_signature",
        "Names the pinned key but is signed by another.",
    ),
    ("T13 duplicate device", L1, [bad["T13"]], "duplicate_device", "Device A listed twice."),
    (
        "T14 rule A countersigned by a newcomer",
        L1,
        [bad["T14"]],
        "countersignature",
        "Only device B countersigns, and B is not on version 1.",
    ),
    (
        "T15 tampered copy of version 4",
        L3,
        [bad["T15"]],
        "countersignature",
        "v4's own body and owner signature, countersigned by A instead of C.  Same body hash as v4, different entry hash.",
    ),
    (
        "T16 closed flag with a device",
        L1,
        [bad["T16"]],
        "closed_rule",
        "closed is true while devices is not empty.",
    ),
    (
        "T17 first list naming a predecessor",
        None,
        [bad["T17"]],
        "first_rule",
        "Version 1 with a non-null previousSHA256.",
    ),
    (
        "T18 reopen without a tag",
        L3,
        [bad["T18"]],
        "reopen_unrecorded",
        "A rule C entry whose reopenTag is null.",
    ),
    (
        "T19 close that removes both server keys",
        L1,
        [bad["T19"]],
        "server_keys_removed",
        "serverHelper set to null, leaving no server key to acknowledge a later reopen.",
    ),
    (
        "T20 reopen with the wrong device",
        L3,
        [bad["T20"]],
        "reopen_device",
        "Tagged with the newest code for version 4, but that line names device C and the list names device B.",
    ),
    (
        "T21 reopen with a retired code",
        L3,
        [bad["T21"]],
        "reopen_tag",
        "Identical to v4 except that it is tagged with the first code, which the newer line for version 4 retired.",
    ),
    (
        "T22 a second valid version 4",
        L3,
        [L4b],
        "accepted",
        "Signed at 20:55 with the same code and device:  a server could hold this instead of v4 (R5).",
    ),
    (
        "T24 a version 1 the rotated history did not close",
        None,
        [bad["T24"]],
        "history_hash",
        "Under the rotated history, whose closed line names v1's body hash:  another version 1, signed by the key that history assigns to version 1, so only check 3's history hash fails (v5.3).",
    ),
    (
        "T25 rule A that removes both server keys",
        L1,
        [bad["T25"]],
        "server_keys_removed",
        "Adds device B, countersigned by device A, and sets serverHelper to null, leaving no server key (§17A A8, settled in v5.3).",
    ),
    (
        "T26 owner signature with a stray trailing bit",
        None,
        [bad["T26"]],
        "owner_signature",
        "A first list whose ownerSignature has a stray trailing bit set in its last base64url character:  a lenient decoder reads the same valid DER, but the base64url is not canonical, so check 4 fails (v5.4).",
    ),
    (
        "T27 countersignature with a stray trailing bit",
        L1,
        [bad["T27"]],
        "countersignature",
        "Adds device B, countersigned by device A, but the countersignature has a stray trailing bit set in its last base64url character, so rule A's countersignature condition fails (v5.4).",
    ),
]
for name, held, chain, expect, _ in chain_cases:
    history = CASE_HISTORY.get(name.split(" ")[0], HISTORY)
    got, _ = check_chain(held, chain, history, RECORDS)
    if got != expect:
        sys.exit(f"{name}: expected {expect}, reference says {got}")
# T24's list is refused only by the history it runs under, and the genuine v1 passes under it.
assert check_chain(None, [bad["T24"]], HISTORY, RECORDS)[0] == "accepted"
assert check_chain(None, [L1], HISTORY_ROTATED, RECORDS)[0] == "accepted"
# v5.4:  every list this script prints passes P3, so the helper never refuses one of them as bad_input.
for _entry in [L1, L2, L3, L4, L4b] + list(bad.values()):
    assert p3_failure(_entry) is None, p3_failure(_entry)


# v5.4:  the strictness §16's harness cases build, each re-signed so that only its one fault remains.
def _compressed(key):
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    return b64u(bytes.fromhex("3039301306072a8648ce3d020106082a8648ce3d030107032200") + point)


def _hybrid(key):
    der = spki(key)
    return b64u(der[:26] + bytes([0x06 | (der[-1] & 1)]) + der[27:])


for _label, _lst in [
    ("compressed owner key", make_list(1, None, "2026-11-07T20:00:00Z", [DA], owner_key=_compressed(owner))),
    ("hybrid owner key", make_list(1, None, "2026-11-07T20:00:00Z", [DA], owner_key=_hybrid(owner))),
    (
        "compressed service key",
        make_list(1, None, "2026-11-07T20:00:00Z", [DA], services=dict(SERVICES, serverExecutor=_compressed(helper))),
    ),
    ("owner key with a stray bit", make_list(1, None, "2026-11-07T20:00:00Z", [DA], owner_key=stray_bit(OWNER))),
    ("owner key with padding", make_list(1, None, "2026-11-07T20:00:00Z", [DA], owner_key=OWNER + "==")),
    ("leap second", make_list(1, None, "2026-11-07T23:59:60Z", [DA])),
    ("February 30", make_list(1, None, "2026-02-30T20:00:00Z", [DA])),
    ("year 0000", make_list(1, None, "0000-11-07T20:00:00Z", [DA])),
    ("another contract", make_list(1, None, "2026-11-07T20:00:00Z", [DA], contract="muster-trust-list/2")),
]:
    assert check_chain(None, [_lst], HISTORY, RECORDS)[0] == "bad_input", _label
    # The same fault in the second entry of a chain still refuses the whole request at P3.
    assert check_chain(None, [L1, dict(_lst)], HISTORY, RECORDS)[0] == "bad_input", _label
# v5.4, check 7 says "exactly when":  an open first list with no device is closed_rule.
assert check_chain(None, [make_list(1, None, "2026-11-07T20:00:00Z", [])], HISTORY, RECORDS)[0] == "closed_rule"
# Version 5 as frozen (Milestones 1 to 4):  a rule C entry is refused reopen_unavailable.
frozen_cases = [
    (
        "T23 v4 under version 5 as frozen",
        L3,
        [L4],
        "reopen_unavailable",
        "The same chain as T4, judged by a Milestone 1 to 4 verifier:  checks 1 to 7 pass, and rule C is not available.",
    ),
]
for name, held, chain, expect, _ in frozen_cases:
    got, _ = check_chain(held, chain, HISTORY, RECORDS, frozen=True)
    if got != expect:
        sys.exit(f"{name}: expected {expect}, reference says {got}")
assert sha(canonical(bad["T15"]["body"])) == sha(canonical(L4["body"]))
assert entry_hash(bad["T15"]) != entry_hash(L4)


# ---------------------------------------------------------------- recovery rule (Muster's side, design and §9)
def muster_decision(pending, ack):
    """§9 Recovery:  what Muster does with a pending entry, given one verified acknowledgement from the required key."""
    v, h = pending["body"]["version"], sha(canonical(pending["body"]))
    held = ack["helperTrustListVersion"]
    if held is not None and held == v:  # coverage is checked first and always wins
        return "apply" if ack["trustListSHA256"] == h else "adopt the server's chain"
    if held is not None and held > v:
        return "fetch the server's chain and walk it"
    if any(r["entrySHA256"] == entry_hash(pending) for r in ack["finalRefusals"]):
        return "discard"
    return "keep waiting"


def fr(entry, reason):
    return {
        "at": "2026-11-07T20:51:00Z",
        "entrySHA256": entry_hash(entry),
        "reason": reason,
        "version": entry["body"]["version"],
    }


recovery_cases = [
    (
        "R1 a final refusal of a tampered copy",
        L4,
        {
            "helperTrustListVersion": 3,
            "trustListSHA256": sha(canonical(L3["body"])),
            "finalRefusals": [fr(bad["T15"], "countersignature")],
        },
        "keep waiting",
        "The refused entry is T15:  the same body hash as v4, a different entry hash.",
    ),
    (
        "R2 after a non-final refusal",
        L4,
        {
            "helperTrustListVersion": 3,
            "trustListSHA256": sha(canonical(L3["body"])),
            "finalRefusals": [],
        },
        "keep waiting",
        "The server refused v4 with version_gap (sent out of order);  non-final reasons are never recorded.",
    ),
    (
        "R3 coverage",
        L4,
        {
            "helperTrustListVersion": 4,
            "trustListSHA256": sha(canonical(L4["body"])),
            "finalRefusals": [],
        },
        "apply",
        "The acknowledgement names version 4 with v4's body hash.",
    ),
    (
        "R4 a final refusal of the pending entry itself",
        bad["T8"],
        {
            "helperTrustListVersion": 3,
            "trustListSHA256": sha(canonical(L3["body"])),
            "finalRefusals": [fr(bad["T8"], "reopen_tag")],
        },
        "discard",
        "Pending entry T8 (as if the check group had been bypassed) was refused for a final reason;  it can never be accepted.",
    ),
    (
        "R5 the server holds a different version 4",
        L4,
        {
            "helperTrustListVersion": 4,
            "trustListSHA256": sha(canonical(L4b["body"])),
            "finalRefusals": [],
        },
        "adopt the server's chain",
        "Muster fetches the chain the acknowledgement covers and walks it under §8;  it never signs a redo.",
    ),
]
for name, pending, ack0, expect, _ in recovery_cases:
    got = muster_decision(pending, ack0)
    if got != expect:
        sys.exit(f"{name}: expected {expect}, got {got}")

# ---------------------------------------------------------------- approvals (unchanged from v3)
plan_obj = {
    "contract": "fleet-ops-plan/1",
    "scope": "server",
    "action": "restart",
    "target": "fleet-server-status.timer",
    "commands": [["/usr/bin/systemctl", "--user", "restart", "fleet-server-status.timer"]],
    "auditTarget": "fleet-server-status.timer",
}
plan_bytes = canonical(plan_obj)
PLAN_SHA = sha(plan_bytes)
COMMAND = "server restart fleet-server-status.timer --yes"
SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@-")


def approval_text(a):
    return "\n".join(
        [
            "muster-approve/1",
            a["jobId"],
            a["jobNonce"],
            a["approvalNonce"],
            a["host"],
            a["action"],
            a["target"],
            a["planSHA256"],
            a["deviceId"],
            a["issuedAt"],
            a["expiresAt"],
        ]
    ).encode("ascii")


def approval(signer, **over):
    a = {
        "jobId": "job7k2m9q4x1",
        "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "host": "server",
        "action": "restart",
        "target": "fleet-server-status.timer",
        "planSHA256": PLAN_SHA,
        "deviceId": DA["deviceId"],
        "issuedAt": "2026-11-07T21:00:00Z",
        "expiresAt": "2026-11-07T21:02:00Z",
    }
    a.update(over)
    text = approval_text(a)
    a["signature"] = b64u(sign(signer, text))
    return a, text


def well_formed(a):
    pats = {
        "jobId": r"[a-z0-9]{1,26}",
        "jobNonce": r"[0-9a-f]{32}",
        "approvalNonce": r"[0-9a-f]{32}",
        "planSHA256": r"[0-9a-f]{64}",
        "deviceId": r"d-[0-9a-f]{24}",
        "issuedAt": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
        "expiresAt": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
        "signature": r"[A-Za-z0-9_-]{1,100}",
    }
    for k, p in pats.items():
        if not isinstance(a.get(k), str) or not re.fullmatch(p, a[k]):
            return False
    for k in ("host", "action", "target"):
        v = a.get(k)
        if not isinstance(v, str) or not (1 <= len(v) <= 128) or not set(v) <= SAFE:
            return False
    return a["jobNonce"] != a["approvalNonce"] and set(a) == set(pats) | {
        "host",
        "action",
        "target",
    }


def check_approval(stdin, now, ledger, helper_sha, stored, history):
    scope, verb, target, mode = COMMAND.split(" ")
    if len(stdin) > 4096:
        return "input_too_large"
    doc = json.loads(stdin.decode("utf-8"))
    if (
        set(doc) != {"protocol", "approval"}
        or doc["protocol"] != "muster-helper/1"
        or not well_formed(doc["approval"])
    ):
        return "bad_input"
    a = doc["approval"]
    current = [h for h in history if h["toVersion"] is None][0]["ownerKey"]
    if stored is None or stored["body"]["ownerKey"] != current:
        return "no_trust_list"
    if (a["host"], a["action"], a["target"]) != ("server", verb, target):
        return "field_mismatch"
    dev = [d for d in stored["body"]["devices"] if d["deviceId"] == a["deviceId"]]
    if not dev or stored["body"]["closed"]:
        return "unknown_device"
    if not verify(dev[0]["approvalKey"], a["signature"], approval_text(a)):
        return "bad_signature"
    issued, expires, n = t(a["issuedAt"]), t(a["expiresAt"]), t(now)
    span = (expires - issued).total_seconds()
    if span <= 0 or span > 120:
        return "window_too_long"
    if (issued - n).total_seconds() > 60:
        return "not_yet_valid"
    if n > expires:
        return "expired"
    if a["approvalNonce"] in ledger:
        return "replayed_nonce"
    if a["planSHA256"] != helper_sha:
        return "plan_mismatch"
    return "accepted"


approval_cases = []


def acase(name, a, text, now, ledger, helper_sha, expect, note, stored=L1, stored_name="version 1"):
    stdin = canonical({"protocol": "muster-helper/1", "approval": a})
    got = check_approval(stdin, now, ledger, helper_sha, stored, HISTORY)
    if got != expect:
        sys.exit(f"{name}: expected {expect}, reference says {got}")
    approval_cases.append(
        (name, text, a["signature"], stdin, now, ledger, helper_sha, expect, note, stored_name)
    )


a, x = approval(devA["approval"])
acase(
    "V1 valid",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "accepted",
    "Listed approval key, inside the window, fresh nonce, matching plan.  Audit row:  yes.",
)
a, x = approval(devA["request"])
acase(
    "V2 request key used as approval key",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "bad_signature",
    "Signed by the device's request key.  Audit row:  no.",
)
a, x = approval(stranger)
acase(
    "V3 key not on the list",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "bad_signature",
    "Signed by a key that is not the listed approval key.  Audit row:  no.",
)
a, x = approval(devA["approval"])
acase(
    "V4 expired",
    a,
    x,
    "2026-11-07T21:02:01Z",
    [],
    PLAN_SHA,
    "expired",
    'One second after expiresAt.  The phone reads this as "Not known whether it ran".  Audit row:  no (refused before Y8).',
)
a, x = approval(devA["approval"])
acase(
    "V5 replayed nonce",
    a,
    x,
    "2026-11-07T21:00:45Z",
    ["a1b2c3d4e5f60718293a4b5c6d7e8f90"],
    PLAN_SHA,
    "replayed_nonce",
    "The approvalNonce is already in the ledger.  Audit row:  no.",
)
a, x = approval(devA["approval"])
acase(
    "V6 plan hash mismatch",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    sha(b"a different plan"),
    "plan_mismatch",
    'The helper\'s own plan hashes differently.  The only refusal the phone reads as "Refused, nothing ran".  Audit row:  yes (after Y8).',
)
a, x = approval(devA["approval"], host="mac")
acase(
    "V7 wrong host",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "field_mismatch",
    "Well-formed and correctly signed, but host is mac.  It passes P3 and fails at Y3.  Audit row:  no.",
)
a, x = approval(devA["approval"], target="botfleet-server.service")
acase(
    "V8 wrong target",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "field_mismatch",
    "Correctly signed for another unit than the command names.  Audit row:  no.",
)
a, x = approval(devA["approval"], expiresAt="2026-11-07T21:05:00Z")
acase(
    "V9 window too long",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "window_too_long",
    "expiresAt is 300 seconds after issuedAt.  Audit row:  no.",
)
a, x = approval(devA["approval"], issuedAt="2026-11-07T21:10:00Z", expiresAt="2026-11-07T21:12:00Z")
acase(
    "V10 issued in the future",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "not_yet_valid",
    "issuedAt is 570 seconds after the helper's clock.  Audit row:  no.",
)
a, x = approval(devA["approval"])
acase(
    "V11 checked exactly at expiresAt",
    a,
    x,
    "2026-11-07T21:02:00Z",
    [],
    PLAN_SHA,
    "accepted",
    "now equals expiresAt.  Accepted, so it passes Y8 and runs.  Audit row:  yes.",
)
a, x = approval(devA["approval"], issuedAt="2026-11-07T21:01:30Z", expiresAt="2026-11-07T21:03:30Z")
acase(
    "V12 issued exactly 60 seconds ahead",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "accepted",
    "issuedAt equals now plus 60 seconds.  Accepted, so it passes Y8 and runs.  Audit row:  yes.",
)
a, x = approval(devA["approval"], approvalNonce="0f1e2d3c4b5a69788796a5b4c3d2e1f0")
acase(
    "V13 approval nonce equals job nonce",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "bad_input",
    "The two nonces must differ.  Audit row:  no.",
)
a, x = approval(devB["approval"], deviceId=DB["deviceId"])
acase(
    "V14 device not on the stored list",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "unknown_device",
    "Device B, correctly signed by its own approval key, while the helper holds version 1, which lists only A.  Audit row:  no.",
)
a, x = approval(devA["approval"])
acase(
    "V15 stored list closed",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "unknown_device",
    "V1's approval while the helper holds version 3, which is closed.  Audit row:  no.",
    stored=L3,
    stored_name="version 3 (closed)",
)
a, x = approval(devA["approval"])
acase(
    "V16 nothing stored",
    a,
    x,
    "2026-11-07T21:00:30Z",
    [],
    PLAN_SHA,
    "no_trust_list",
    "V1's approval while the helper holds no list.  Audit row:  no.",
    stored=None,
    stored_name="nothing",
)

# ---------------------------------------------------------------- canonical form
canon_obj = {
    "z": "fleet-ops-plan/1",
    "a10": ["/usr/bin/systemctl", 7, True, None],
    "B": None,
    "a9": {"y": 'a"b\\c', "b": -3},
    "a": "x",
}
canon_bytes = canonical(canon_obj)

# ---------------------------------------------------------------- signed answers
KEYID = sha(spki(helper))[:16]


def signed_answer(kind, prefix, body):
    sig = sign(helper, prefix + canonical(body))
    assert verify(b64u(spki(helper)), b64u(sig), prefix + canonical(body))
    return {
        "protocol": "muster-helper/1",
        "kind": kind,
        "body": body,
        "keyId": KEYID,
        "signature": b64u(sig),
    }


plan_answer = signed_answer(
    "plan",
    b"muster-signed-plan/1\n",
    {
        "contract": "muster-signed-plan/1",
        "claimed": {
            "jobId": "job7k2m9q4x1",
            "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
            "deviceId": DA["deviceId"],
        },
        "host": "server",
        "action": "restart",
        "target": "fleet-server-status.timer",
        "plan": plan_bytes.decode("ascii"),
        "planSHA256": PLAN_SHA,
        "helperTrustListVersion": 1,
        "issuedAt": "2026-11-07T20:59:50Z",
    },
)
assert sha(plan_answer["body"]["plan"].encode("ascii")) == plan_answer["body"]["planSHA256"]
result = signed_answer(
    "result",
    b"muster-signed-result/1\n",
    {
        "contract": "muster-signed-result/1",
        "jobId": "job7k2m9q4x1",
        "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "deviceId": DA["deviceId"],
        "host": "server",
        "action": "restart",
        "target": "fleet-server-status.timer",
        "planSHA256": PLAN_SHA,
        "outcome": "done",
        "detail": "restarted",
        "outputB64": None,
        "auditId": "a1b2c3d4",
        "helperTrustListVersion": 1,
        "finishedAt": "2026-11-07T21:00:33Z",
    },
)
refusal_unverified = signed_answer(
    "refusal",
    b"muster-signed-refusal/1\n",
    {
        "contract": "muster-signed-refusal/1",
        "verified": False,
        "claimed": {
            "jobId": "job7k2m9q4x1",
            "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
            "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "deviceId": DA["deviceId"],
            "host": "server",
            "action": "restart",
            "target": "fleet-server-status.timer",
            "planSHA256": PLAN_SHA,
        },
        "errorCode": "bad_signature",
        "reason": None,
        "helperTrustListVersion": 1,
        "refusedAt": "2026-11-07T21:00:31Z",
    },
)
refusal_verified = signed_answer(
    "refusal",
    b"muster-signed-refusal/1\n",
    {
        "contract": "muster-signed-refusal/1",
        "verified": True,
        "jobId": "job7k2m9q4x1",
        "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "deviceId": DA["deviceId"],
        "host": "server",
        "action": "restart",
        "target": "fleet-server-status.timer",
        "planSHA256": PLAN_SHA,
        "errorCode": "plan_mismatch",
        "reason": None,
        "helperTrustListVersion": 1,
        "refusedAt": "2026-11-07T21:00:31Z",
    },
)
refusal_chain = signed_answer(
    "refusal",
    b"muster-signed-refusal/1\n",
    {
        "contract": "muster-signed-refusal/1",
        "verified": False,
        "chain": {"version": 2, "entrySHA256": entry_hash(bad["T7"]), "final": False},
        "errorCode": "chain_refused",
        "reason": "previous_hash",
        "helperTrustListVersion": 1,
        "refusedAt": "2026-11-07T20:11:00Z",
    },
)
ack_t8 = signed_answer(
    "ack",
    b"muster-signed-ack/1\n",
    {
        "contract": "muster-signed-ack/1",
        "helperTrustListVersion": 2,
        "closed": False,
        "signedByCurrentKey": True,
        "trustListSHA256": sha(canonical(L2["body"])),
        "finalRefusals": [],
        "at": "2026-11-07T20:12:02Z",
    },
)
ack = signed_answer(
    "ack",
    b"muster-signed-ack/1\n",
    {
        "contract": "muster-signed-ack/1",
        "helperTrustListVersion": 3,
        "closed": True,
        "signedByCurrentKey": True,
        "trustListSHA256": sha(canonical(L3["body"])),
        "finalRefusals": [],
        "at": "2026-11-07T20:22:02Z",
    },
)

# ---------------------------------------------------------------- emit
o = []
w = o.append
w(
    f"Generated by `make_vectors_v5.py` with `cryptography` {cryptography.__version__} under `/usr/bin/python3 -I` "
    f"(Python {sys.version.split()[0]}).  Every vector was checked by the script's reference checker before it was written, "
    "and `check_vectors_v5.py` re-checks the printed vectors from this file alone.  "
    "**The rule C, reopen-record and recovery vectors (T4, T5's last step, T8, T15, T18, T20, T21, T22, R1 to R5) illustrate the OPEN proposal of §9 and will change when §17A is settled;  under version 5 as frozen, every rule C entry is refused `reopen_unavailable`.**  "
    "**Every key below is a test key derived from a fixed label (the whole bold heading, parenthesis included) and is used nowhere else.**  "
    "A harness resets the ledger and the rate file between approval vectors, because several reuse one approvalNonce, and one `verb target` at one clock."
)
w("")
w("### Test Keys")
w("")
w(
    'Each private key is the P-256 scalar `int(SHA-256("muster test key " + label)) mod (n - 1) + 1`, where the label is the bold heading below.  '
    "Only the public halves are printed:  a test harness derives each private key from its label, and private key blocks are kept out of the repository so its secret scanning stays meaningful."
)
w("")
for label, key in [
    ("owner", owner),
    ("owner after rotation (T24 only)", new_owner),
    ("rogue owner (not pinned)", rogue_owner),
    ("server helper", helper),
    ("device A approval", devA["approval"]),
    ("device A request", devA["request"]),
    ("device B approval", devB["approval"]),
    ("device C approval", devC["approval"]),
    ("device X approval (not on any list)", stranger),
    ("rogue helper (not enrolled)", rogue_helper),
]:
    w(f"**{label}**  (fingerprint `{fingerprint(spki(key))}`)")
    w("")
    w("```")
    w("public, base64url SPKI DER:  " + b64u(spki(key)))
    w("```")
    w("")
w(
    "The reading and notification keys of devices A, B and C use the labels `device A reading`, `device A notification` and so on;  only their public halves appear, inside the lists."
)
w("")
w("### Canonical Form")
w("")
w(
    "This object, given here out of order and with spaces, has a null object member and keys that sort differently by bytes than by case or by number:"
)
w("")
w("```")
w(
    '{"z": "fleet-ops-plan/1", "a10": ["/usr/bin/systemctl", 7, true, null], "B": null, "a9": {"y": "a\\"b\\\\c", "b": -3}, "a": "x"}'
)
w("```")
w("")
w(
    "Its canonical bytes, exactly:  `B` before `a`, `a10` before `a9`, the null member present, `/` not escaped:"
)
w("")
w("```")
w(canon_bytes.decode("ascii"))
w("```")
w("")
w(f"SHA-256:  `{sha(canon_bytes)}`")
w("")
w("### Pin, Owner History And Reopen Record")
w("")
w("`/etc/muster/owner.pub`:")
w("")
w("```")
w(PIN_LINE.rstrip("\n"))
w("```")
w("")
w("`/etc/muster/owner-history.jsonl` (one line):")
w("")
w("```")
w(HISTORY_LINE.rstrip("\n"))
w("```")
w("")
w(
    f"`/etc/muster/reopen-codes.jsonl` (two lines for version 4 and device C, `{FP_C}`;  the first code was issued and then retired when the owner ran `reopen` again for version 4, and the second was used):"
)
w("")
w("```")
w(RECORD_LINES.rstrip("\n"))
w("```")
w("")
w(
    f"How `muster-agent reopen --version 4 --device {FP_C}` prints the second code with its check group:  `{reopen_display(4, FP_C, CODE_USED)}`.  "
    f"The same code checked against version 5 gives `{reopen_check(5, FP_C, CODE_USED)}`, and against device B (`{FP_B}`) gives `{reopen_check(4, FP_B, CODE_USED)}`.  "
    f"The retired first code's check group for version 4 and device C was `{reopen_check(4, FP_C, CODE_UNUSED)}`."
)
w("")
w(
    "`/etc/muster/owner-history.jsonl` for T24 only (two lines, v5.3:  the owner key rotated after version 1, so the closed line names version 1's body hash, and `owner.pub` holds the `owner after rotation (T24 only)` key):"
)
w("")
w("```")
w(HISTORY_ROTATED_LINES.rstrip("\n"))
w("```")
w("")
w("### Trust Lists")
w("")
for label, lst in [
    ("Version 1", L1),
    ("Version 2 (rule A)", L2),
    ("Version 3 (rule B, closed)", L3),
    ("Version 4 (rule C, reopened)", L4),
    ("Version 4, signed again at 20:55 (T22, R5)", L4b),
]:
    w(
        f"**{label}**, canonical body SHA-256 `{sha(canonical(lst['body']))}`, entry SHA-256 `{entry_hash(lst)}`:"
    )
    w("")
    w("```json")
    w(json.dumps(lst, indent=2, sort_keys=True))
    w("```")
    w("")
w("### Bad Lists")
w("")
for key in [
    "T6",
    "T7",
    "T8",
    "T10",
    "T11",
    "T12",
    "T13",
    "T14",
    "T15",
    "T16",
    "T17",
    "T18",
    "T19",
    "T20",
    "T21",
    "T24",
    "T25",
    "T26",
    "T27",
]:
    w(f"**{key} list**, entry SHA-256 `{entry_hash(bad[key])}`:")
    w("")
    w("```json")
    w(json.dumps(bad[key], indent=2, sort_keys=True))
    w("```")
    w("")
w("### Chain Vectors")
w("")
w(
    "Each is `muster trust-list chain --yes` with the lists named, under the pin, history and record above.  The reason is the first failing check in §8's numbered order;  each bad list fails exactly one check.  "
    "**The rule C rows (T4, T5, T8, T15, T18, T20, T21 and T22) give the OPEN proposal's outcomes.  A Milestone 1 to 4 builder skips them, or expects `chain_refused` with reason `reopen_unavailable` for each rule C entry (T5 is refused at its fourth entry), as T23 below shows.**  "
    "`Final` gives the version 4 proposal's classification, which is OPEN (§17A, A2);  version 5 records and reports nothing as final.  Until the chain slice lands, the helper answers each of these requests `bad_command` (§4), so a helper harness runs these against the chain slice;  the other verifiers check them now.  "
    "T24 runs under the rotated history printed above (T24 only);  every other row runs under the one-line history."
)
w("")
w("| Vector | Held Before | Chain | Expected | Final | Why |")
w("| --- | --- | --- | --- | --- | --- |")
names = {id(L1): "v1", id(L2): "v2", id(L3): "v3", id(L4): "v4", id(L4b): "v4 at 20:55"}
for k, v in bad.items():
    names[id(v)] = k + " list"
for name, held, chain, expect, why in chain_cases:
    hv = "nothing" if held is None else names[id(held)]
    cv = ", ".join(names[id(c)] for c in chain)
    out = "ack" if expect == "accepted" else f"`chain_refused`, reason `{expect}`"
    fin = "" if expect == "accepted" else ("yes" if expect in FINAL else "no")
    w(f"| {name} | {hv} | {cv} | {out} | {fin} | {why} |")
w("")
w(
    "T9 was retired, and a vector's number is never reused, so the table has no T9.  T24 and T25 were added in version 5.3, and T26 and T27 in version 5.4;  none of them is a rule C row.  "
    "T26 and T27 carry the same DER signature bytes a lenient base64url decoder would read, so a verifier that does not hold base64url to its canonical form (§5, v5.4) accepts them, and the helper refuses them."
)
w("")
w("### Frozen Version 5 Vector")
w("")
w("What every verifier in Milestones 1 to 4 must answer for a rule C entry.")
w("")
w("| Vector | Held Before | Chain | Expected | Final | Why |")
w("| --- | --- | --- | --- | --- | --- |")
for name, held, chain, expect, why in frozen_cases:
    hv = names[id(held)]
    cv = ", ".join(names[id(c)] for c in chain)
    w(f"| {name} | {hv} | {cv} | `chain_refused`, reason `{expect}` | no | {why} |")
w("")
w("### Recovery Vectors")
w("")
w(
    f"OPEN proposal (§9, §17A).  Muster holds an entry pending and sees one verified acknowledgement from the key §9 requires.  v4's entry SHA-256 is `{entry_hash(L4)}` and its body SHA-256 `{sha(canonical(L4['body']))}`;  T15 has the same body hash but entry SHA-256 `{entry_hash(bad['T15'])}`.  Hashes below are shortened to 16 characters;  the vectors above give them in full."
)
w("")
w("| Vector | Pending | Acknowledgement Says | Muster Must | Why |")
w("| --- | --- | --- | --- | --- |")
for name, pending, a0, expect, why in recovery_cases:
    held = f"holds version {a0['helperTrustListVersion']}, body `{a0['trustListSHA256'][:16]}…`"
    if a0["finalRefusals"]:
        r0 = a0["finalRefusals"][0]
        held += f";  final refusal of version {r0['version']}, entry `{r0['entrySHA256'][:16]}…`, `{r0['reason']}`"
    pn = "v4" if pending is L4 else names[id(pending)]
    w(f"| {name} | {pn} | {held} | {expect} | {why} |")
w("")
w("### Approval Vectors")
w("")
w(
    "Each runs `server restart fleet-server-status.timer --yes` under the pin and history above, holding the list each names."
)
w("")
for name, text, sig, stdin, now, ledger, helper_sha, expect, note, stored_name in approval_cases:
    w(f"#### {name}")
    w("")
    w(note)
    w("")
    w("```")
    w("stored list:           " + stored_name)
    w("helper clock (now):    " + now)
    w("ledger nonces:         " + (", ".join(ledger) if ledger else "(none)"))
    w("helper plan SHA-256:   " + helper_sha)
    w("expected outcome:      " + expect)
    w("approval text bytes (hex):")
    w(text.hex())
    w("approval text SHA-256: " + sha(text))
    w("signature (base64url DER): " + sig)
    w("stdin bytes (" + str(len(stdin)) + " bytes):")
    w(stdin.decode("ascii"))
    w("```")
    w("")
w("### Illustrative Plan")
w("")
w("```")
w(plan_bytes.decode("ascii"))
w("```")
w("")
w(
    f"`planSHA256` = `{PLAN_SHA}`.  The helper builds the real plan;  tests inject the helper's plan hash as each vector says."
)
w("")
w("### Signed Answers")
w("")
for label, ans in [
    (
        "A signed plan for V1's job (the phone checks SHA-256 of plan equals planSHA256)",
        plan_answer,
    ),
    ("A done result for V1", result),
    ("The refusal for V3 (unverified:  the fields are only claimed)", refusal_unverified),
    ("The refusal for V6 (verified)", refusal_verified),
    ("A chain refusal for T7 (not final)", refusal_chain),
    ("The acknowledgement after T2, holding version 2", ack_t8),
    ("The acknowledgement after T3, holding the closed version 3", ack),
]:
    w(f"**{label}**, canonical body SHA-256 `{sha(canonical(ans['body']))}`:")
    w("")
    w("```json")
    w(json.dumps(ans, indent=2, sort_keys=True))
    w("```")
    w("")
w("### Cases Without Fixed Bytes")
w("")
w(
    "A harness builds these:  `pin_unsafe` (owner.pub group-writable, owned by jay, or a symbolic link;  the same for the reopen record when it exists, or a record line that does not match its pattern);  `helper_key_missing` (the key file absent, a symbolic link, mode 0644, or unparseable);  `input_too_large` (4,097 bytes);  `bad_input` (a duplicate key, `NaN`, a non-ASCII string, or a chain entry missing a body field);  a valid approval whose stdin keys are in another order with extra spaces (accepted, since formatting does not matter);  `state_unsafe` (`muster-nonces.jsonl` mode 0644, `muster-trust.json` a symbolic link or empty, or an unparseable rate file);  a torn final ledger fragment (truncated, then the approval proceeds);  a complete final ledger line without a line feed (a line feed is appended, and that nonce stays spent);  a corrupt middle line (`ledger_corrupt`);  `rate_limited` (two `plan` requests for one `verb target` within the spacing, two different approvals for one `verb target` within 2 seconds, the class's hourly cap reached, or `muster-rate.lock` held elsewhere for more than 2 seconds);  a `yes` approval refused `rate_limited` at Y8b (its nonce not in the ledger afterwards, and no audit row), then retried inside its window and accepted;  60 approvals with bad signatures, then V1 (accepted:  none of the 60 used the `yes` budget);  V1 replayed 60 times after it ran (`replayed_nonce` each time, and a fresh approval for the same `verb target` 2 seconds after V1 is accepted);  `bad_command` for `muster trust-list chain --yes` until the chain slice lands (before its input is read, and no audit row);  the acknowledgement with nothing held (`closed` false, `signedByCurrentKey` false, `helperTrustListVersion` and `trustListSHA256` null);  `plan_refused` with `revision_stale` (a `resource-policy` target naming a revision older than the saved policy's);  a redeploy row named `Café App` (the muster plan's `auditTarget` is `Caf? App`, and the `--from-ssh` route's `audit_target` is unchanged);  `trust_list_changed` (the stored list replaced between Y2 and Y7);  a replay of V1 long after its window (`expired` at Y6, before the ledger is read, and no audit row);  V1 replayed inside its window after the clock was stepped forward more than seven days and back (`replayed_nonce`:  the ledger kept its nonce, which is within the last 4,096 lines);  a ledger of more than 4,096 lines rewritten under the lock (only lines beyond the last 4,096 whose `expiresAt` is more than seven days before `now` are dropped, a line whose `expiresAt` is after `now` stays, and the rewrite is `fsync`ed, renamed and its directory `fsync`ed);  a ledger over 4 MiB (`ledger_corrupt`);  V1's `approvalNonce` in another approval from another device or another job (`replayed_nonce`);  V1's high-S twin sent after V1 ran (`replayed_nonce`);  `clock_unsynced` (V1 with the clock reported unsynchronised, and no audit row);  V1 at `now` = 21:02:00.001 (`expired`);  rate boundaries (a second `plan` request exactly 5 seconds after the first passes, at 4.999 seconds is `rate_limited`;  an event exactly 3,600 seconds old still counts;  a rate file with 811 events is `state_unsafe`);  `plan_refused` (`update_busy` with the update's unit activating;  `report_stale` with a 46-minute-old report;  each with `helperTrustListVersion` `null`, since R2 comes before the list is read);  an unsafe state directory (mode 0755:  `helper_key_missing`);  a fault after Y8c before the action began (a signed result with outcome `failed`, `detail` `plan_refused: <reason>` or `internal_error`, and one audit row whose `result` is `failed`);  a fault once the run began (outcome `unknown`, `detail` `the helper stopped unexpectedly while running`, and one audit row whose `result` is `unknown`);  a command killed at its limit, and a Coolify GET that timed out, was reset or got a cut answer (`unknown` each);  a refused connection to Coolify (`failed`);  an empty reopen record (accepted as no lines);  a record whose last line has no line feed (`pin_unsafe`);  "
    "once the chain slice lands (v5.3):  after T1, `muster-trust.json` holds v1's canonical bytes with no line feed, and its SHA-256 is v1's entry SHA-256;  "
    "a stored list written by someone else with spaces and a line feed (accepted, since readers take any formatting);  "
    "the T24 list held under the rotated history, whose closed line names another body hash for version 1 (`state_unsafe` at C2);  "
    "a list stored after C2 where none was held, and a list removed after C2 (`trust_list_changed` at C4 each);  "
    "a fault at C5 before the rename (`internal_error`, nothing stored, and the temporary file gone);  "
    "a fault at C5 after the rename (`internal_error`, the list held, and the same chain sent again is `version_gap`);  "
    "a failed audit append at C5 after the rename (the signed `ack` is answered and the list is held);  "
    "from version 5.4, each at P3 and `bad_input`, never a §8 reason, and not counted, with the list re-signed so that only its one fault remains (`make_vectors_v5.py` asserts each):  "
    "an `ownerKey`, a device key or a `services` key that is a compressed point (a 33-byte point in a 59-byte SubjectPublicKeyInfo) or a hybrid point (the 91-byte form with `06` or `07` where `04` belongs);  "
    "a key whose last base64url character has a stray trailing bit set, or that ends in `=` padding;  "
    "an `issuedAt` of `2026-11-07T23:59:60Z`, `2026-02-30T20:00:00Z` or `0000-11-07T20:00:00Z`;  "
    "a body whose `contract` is `muster-trust-list/2`;  "
    "and any of these in the second entry of a chain whose first entry is valid (the whole request is `bad_input`);  "
    "also from version 5.4:  an approval whose `signature` has a stray trailing bit set (`bad_signature` at Y5);  "
    "a first list with `closed` false and `devices` empty (`chain_refused`, reason `closed_rule`:  check 7 holds `closed` true exactly when there is no device);  "
    "a reader that opens `muster-trust.json` just before a chain's rename and `fstat`s it just after (link count 0:  accepted, and it reads the old list);  "
    "a second hard link to `muster-trust.json` (`state_unsafe`);  "
    "a FIFO in place of `muster-trust.json`, the ledger or the rate file (the open does not hang, and the answer is `state_unsafe`);  "
    "`pin_unsafe` for a chain, `bad_command` for `muster trust-list chain --yes` with a fifth word, and `mode_not_allowed` for `muster trust-list chain --plan` (each with `claimed` `{}` and no `chain`), against `bad_input` and `rate_limited` for a chain (`chain` `null` and no `claimed`);  "
    "the T24 list held under the rotated history (`state_unsafe` at C2 with `helperTrustListVersion` `null`);  "
    "a signing fault at C5 after the rename that repeats when the refusal is signed (the unsigned `helper_key_missing`, and the list held) and one that does not (the signed `internal_error`, and the list held);  "
    "and a failed directory `fsync` at C5 (`internal_error`, the list held, and no audit row)."
)

with open(SPEC, encoding="utf-8") as f:
    spec = f.read()
if MARK not in spec:
    start = spec.index("## 16.  Test Vectors\n\n") + len("## 16.  Test Vectors\n\n")
    end = spec.index("\n\n## 17.  Open")
    spec = spec[:start] + MARK + spec[end:]
with open(SPEC, "w", encoding="utf-8") as f:
    # The file's prose puts a U+00A0 before the space after a sentence (the fleet's sentence gap);
    # the literals above keep two plain spaces, and the gap is made here, as gap.py makes it.
    # A sentence that ends in bold or code markup takes the gap after the markup.
    f.write(spec.replace(MARK, re.sub(r"([.?!](?:\*\*|`)?)  (?=\S)", "\\1\u00a0 ", "\n".join(o))))
print(
    "approval vectors",
    len(approval_cases),
    "chain vectors",
    len(chain_cases),
    "recovery vectors",
    len(recovery_cases),
    "cryptography",
    cryptography.__version__,
    "python",
    sys.version.split()[0],
)
