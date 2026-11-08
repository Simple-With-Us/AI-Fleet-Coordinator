"""Re-check every vector printed in server-route-spec.md (version 5) from the file's text alone.

It shares no code with make_vectors_v4.py:  its own canonical encoder, its own chain checker that runs EVERY
check (so it can confirm each bad list fails exactly one), its own approval checker, and it re-verifies every
signature, countersignature, tag, check group and hash it can see.  Test keys only.

Run from the folder holding server-route-spec.md:  /usr/bin/python3 -I check_vectors_v5.py
"""

import base64
import datetime
import hashlib
import hmac
import json
import re
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

text = open("server-route-spec.md", encoding="utf-8").read()
# Prose sentence gaps (U+00A0 plus a space) read as two spaces;  code blocks have none.
text = text.replace(chr(0xA0) + " ", "  ")
text = text[text.index("## 16.  Test Vectors") : text.index("## 17.  Open")]
FAILS = []


def check(cond, what):
    if not cond:
        FAILS.append(what)
    return cond


def enc(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        # A real error, not an assert, so it still fires under python -O.
        if not all(0x20 <= ord(c) <= 0x7E for c in v):
            raise ValueError(
                "not canonical:  a string holds a character outside printable ASCII: %r" % v[:80]
            )
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ",".join(enc(x) for x in v) + "]"
    keys = sorted(v, key=lambda k: k.encode("ascii"))
    return "{" + ",".join(enc(k) + ":" + enc(v[k]) for k in keys) + "}"


def canon(v):
    return enc(v).encode("ascii")


def H(b):
    return hashlib.sha256(b).hexdigest()


def lenient_unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def unb64u(s):
    """v5.4:  canonical base64url only.  Re-encoding the decoded bytes must give the text back, which
    refuses padding, characters outside the alphabet and stray trailing bits in one comparison."""
    if not isinstance(s, str) or len(s) % 4 == 1 or set(s) - set(B64U):
        raise ValueError("not base64url")
    raw = lenient_unb64u(s)
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != s:
        raise ValueError("not canonical base64url")
    return raw


B64U = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
SPKI_HEAD = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def p256(pub):
    """v5.4:  the key must be the 91-byte SubjectPublicKeyInfo of an uncompressed P-256 point."""
    der = unb64u(pub)
    if len(der) != 91 or der[:26] != SPKI_HEAD or der[26] != 4:
        raise ValueError("not an uncompressed P-256 SubjectPublicKeyInfo")
    key = serialization.load_der_public_key(der)
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("not P-256")
    return key


def ok(pub, sig, data):
    """True only for a valid signature.  Any malformed key, signature or encoding counts as a failure
    (reported by the caller's check), so one bad vector never stops the rest of the run.  v5.4:  the key
    and the signature are decoded strictly, so stray trailing bits make the signature fail."""
    try:
        p256(pub).verify(unb64u(sig), data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
    except Exception as error:  # noqa: BLE001 -- a malformed vector must not abort the run
        print("malformed key or signature:", type(error).__name__, str(error)[:120])
        return False


def calendar_time(s):
    """v5.4:  §2's form, and a real calendar time."""
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z", s) if isinstance(s, str) else None
    if not m:
        return False
    try:
        datetime.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return False
    return True


def p3(e):
    """v5.4:  every reason P3 refuses one signed list (`bad_input`), as a list;  empty when it parses."""
    bad = []

    def key(v, what):
        try:
            p256(v)
        except Exception:  # noqa: BLE001
            bad.append(what)

    if not isinstance(e, dict) or sorted(e) != ["body", "countersignatures", "ownerSignature"]:
        return ["entry keys"]
    b = e["body"]
    want = ["closed", "contract", "devices", "issuedAt", "ownerKey", "previousSHA256", "reopenTag",
            "services", "version"]
    if not isinstance(b, dict) or sorted(b) != want:
        return ["body keys"]
    if b["contract"] != "muster-trust-list/1":
        bad.append("contract")
    if type(b["version"]) is not int or b["version"] < 1 or b["version"] > 2**53 - 1:
        bad.append("version")
    for n in ("previousSHA256", "reopenTag"):
        if b[n] is not None and not (isinstance(b[n], str) and re.fullmatch(r"[0-9a-f]{64}", b[n])):
            bad.append(n)
    if not calendar_time(b["issuedAt"]):
        bad.append("issuedAt")
    key(b["ownerKey"], "ownerKey")
    if type(b["closed"]) is not bool:
        bad.append("closed")
    devs = b["devices"]
    if not isinstance(devs, list) or len(devs) > 64:
        return bad + ["devices"]
    for d in devs:
        if not isinstance(d, dict) or sorted(d) != ["approvalKey", "deviceId", "name", "notificationKey",
                                                    "readingKey", "requestKey"]:
            return bad + ["device"]
        if not (isinstance(d["deviceId"], str) and re.fullmatch(r"d-[0-9a-f]{24}", d["deviceId"])):
            bad.append("deviceId")
        if not (isinstance(d["name"], str) and 1 <= len(d["name"]) <= 40):
            bad.append("name")
        for k in ("approvalKey", "requestKey", "readingKey", "notificationKey"):
            key(d[k], "device " + k)
    s = b["services"]
    if not isinstance(s, dict) or len(s) != 7 or not {"macPublisher", "macExecutor", "macAgreement",
            "serverPublisher", "serverExecutor", "serverExecutorAgreement", "serverHelper"} == set(s):
        return bad + ["services"]
    for k, v in s.items():
        if v is not None:
            key(v, "service " + k)
    sig_re = r"[A-Za-z0-9_-]{1,100}"
    if not (isinstance(e["ownerSignature"], str) and re.fullmatch(sig_re, e["ownerSignature"])):
        bad.append("ownerSignature")
    cs = e["countersignatures"]
    if not isinstance(cs, list) or len(cs) > 64:
        return bad + ["countersignatures"]
    for c in cs:
        if not (isinstance(c, dict) and sorted(c) == ["deviceId", "signature"]
                and isinstance(c["deviceId"], str) and re.fullmatch(r"d-[0-9a-f]{24}", c["deviceId"])
                and isinstance(c["signature"], str) and re.fullmatch(sig_re, c["signature"])):
            bad.append("countersignature")
    try:
        canon(e)
    except (ValueError, TypeError):
        bad.append("not canonical")
    return bad


def block_after(label):
    m = re.search(re.escape(label) + r".*?```(?:json)?\n(.*?)\n```", text, re.DOTALL)
    if not m:
        sys.exit("label not found: " + label)
    return m.group(1)


# -------- keys, from labels
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def key_pub(label):
    s = (
        int.from_bytes(hashlib.sha256(("muster test key " + label).encode()).digest(), "big")
        % (N - 1)
        + 1
    )
    der = (
        ec.derive_private_key(s, ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return base64.urlsafe_b64encode(der).rstrip(b"=").decode()


for label in [
    "owner",
    "owner after rotation (T24 only)",
    "rogue owner (not pinned)",
    "server helper",
    "device A approval",
    "device A request",
    "device B approval",
    "device C approval",
    "device X approval (not on any list)",
    "rogue helper (not enrolled)",
]:
    printed = re.search(
        r"public, base64url SPKI DER:  (\S+)", block_after("**" + label + "**")
    ).group(1)
    check(printed == key_pub(label), "key " + label)
OWNER, HELPER = key_pub("owner"), key_pub("server helper")

# -------- canonical form
src = json.loads(block_after("given here out of order and with spaces"))
check(canon(src).decode() == block_after("Its canonical bytes, exactly"), "canonical bytes")

# -------- pin, history, record
check(block_after("`/etc/muster/owner.pub`:") == OWNER, "pin")
history = [json.loads(x) for x in block_after("`/etc/muster/owner-history.jsonl`").splitlines()]
check(
    history == [{"fromVersion": 1, "ownerKey": OWNER, "toSHA256": None, "toVersion": None}],
    "history",
)
rec_raw = block_after("(two lines for version 4").splitlines()
records = [json.loads(x) for x in rec_raw]
for raw, r in zip(rec_raw, records):
    check(
        canon(r).decode() == raw
        and set(r) == {"code", "createdAt", "deviceId", "version"}
        and re.fullmatch(r"[0-9a-f]{32}", r["code"])
        and re.fullmatch(r"d-[0-9a-f]{24}", r["deviceId"]),
        "record line",
    )


def cg(v, d, c):
    return H(f"muster-reopen-check/1\n{v}\n{d}\n{c}".encode())[:4]


m = re.search(
    r"--device (d-[0-9a-f]{24})` prints the second code with its check group:  `([0-9a-f-]+) ([0-9a-f]{4})`.  "
    r"The same code checked against version 5 gives `([0-9a-f]{4})`, and against device B \(`(d-[0-9a-f]{24})`\) gives `([0-9a-f]{4})`.  "
    r"The retired first code's check group for version 4 and device C was `([0-9a-f]{4})`",
    text,
)
dC, shown, g4, g5, dB, gB, g1 = m.groups()
code = records[1]["code"]
check(
    shown.replace("-", "") == code
    and g4 == cg(4, dC, code)
    and g5 == cg(5, dC, code)
    and gB == cg(4, dB, code)
    and g1 == cg(4, dC, records[0]["code"])
    and len({g4, g5, gB, g1}) == 4,
    "check groups",
)

# -------- lists
lists = {}
for m in re.finditer(
    r"\*\*(Version [^*]+|T\d+ list)\*\*, (?:canonical body SHA-256 `([0-9a-f]{64})`, )?entry SHA-256 `([0-9a-f]{64})`:\n\n```json\n(.*?)\n```",
    text,
    re.DOTALL,
):
    name, body_h, entry_h, js = m.groups()
    lst = json.loads(js)
    lists[name] = lst
    if body_h:
        check(H(canon(lst["body"])) == body_h, "body hash " + name)
    check(H(canon(lst)) == entry_h, "entry hash " + name)
    # v5.4:  every printed list parses at P3, so the helper never refuses one of them as bad_input.
    check(p3(lst) == [], "P3 refuses %s: %s" % (name, p3(lst)))
check(len(lists) == 24, "list count %d" % len(lists))
alias = {
    "v1": "Version 1",
    "v2": "Version 2 (rule A)",
    "v3": "Version 3 (rule B, closed)",
    "v4": "Version 4 (rule C, reopened)",
    "v4 at 20:55": "Version 4, signed again at 20:55 (T22, R5)",
}


def L(n):
    return lists[alias.get(n, n)]


# -------- the rotated history (v5.3, T24 only)
rot_raw = block_after("`/etc/muster/owner-history.jsonl` for T24 only").splitlines()
rotated = [json.loads(x) for x in rot_raw]
check(
    [canon(h).decode() for h in rotated] == rot_raw
    and rotated
    == [
        {
            "fromVersion": 1,
            "ownerKey": OWNER,
            "toSHA256": H(canon(L("v1")["body"])),
            "toVersion": 1,
        },
        {
            "fromVersion": 2,
            "ownerKey": key_pub("owner after rotation (T24 only)"),
            "toSHA256": None,
            "toVersion": None,
        },
    ],
    "rotated history",
)
HISTORY_OF = {"T24": rotated}  # every other row runs under the one-line history


# -------- chain checker:  every check, not only the first
def hist_key(v, hist):
    for h in hist:
        if h["fromVersion"] <= v and (h["toVersion"] is None or v <= h["toVersion"]):
            return h["ownerKey"]


def failures(prev, e, frozen=False, hist=None):
    """Every failing check.  frozen=True is version 5 as frozen:  rule C is not available."""
    hist = history if hist is None else hist
    b, f = e["body"], []
    bb = canon(b)
    if b["version"] != (1 if prev is None else prev["body"]["version"] + 1):
        f.append("version_gap")
    if prev is None and b["previousSHA256"] is not None:
        f.append("first_rule")
    if prev is not None and b["previousSHA256"] != H(canon(prev["body"])):
        f.append("previous_hash")
    k = hist_key(b["version"], hist)
    if b["ownerKey"] != k:
        f.append("owner_key")
    if any(h["toVersion"] == b["version"] and h["toSHA256"] != H(bb) for h in hist):
        f.append("history_hash")
    if not ok(k, e["ownerSignature"], b"muster-trust-list/1\n" + bb):
        f.append("owner_signature")
    ids = [d["deviceId"] for d in b["devices"]]
    if len(ids) != len(set(ids)):
        f.append("duplicate_device")
    cs = e["countersignatures"]
    csid = [c["deviceId"] for c in cs]
    if len(csid) != len(set(csid)):
        f.append("countersignature")
    if b["closed"] != (not b["devices"]):
        f.append("closed_rule")

    def cs_valid(devs):
        byid = {d["deviceId"]: d["approvalKey"] for d in devs}
        return all(
            c["deviceId"] in byid
            and ok(byid[c["deviceId"]], c["signature"], b"muster-trust-countersign/1\n" + bb)
            for c in cs
        )

    def keys_removed():
        p = prev["body"]["services"]
        if (p["serverHelper"] or p["serverPublisher"]) and not (
            b["services"]["serverHelper"] or b["services"]["serverPublisher"]
        ):
            f.append("server_keys_removed")

    def svc():
        p = prev["body"]["services"]
        if not all(v is None or v == p[k2] for k2, v in b["services"].items()):
            f.append("services_changed")
        keys_removed()

    if prev is None:
        if b["closed"] or cs or b["reopenTag"]:
            f.append("first_rule")
    elif b["closed"]:
        if cs or b["reopenTag"]:
            f.append("closed_rule")
        svc()
    elif prev["body"]["closed"] and frozen:
        f.append("reopen_unavailable")
    elif prev["body"]["closed"]:
        svc()
        if sorted(csid) != sorted(ids) or not cs_valid(b["devices"]):
            f.append("countersignature")
        lines = [r for r in records if r["version"] == b["version"]]
        if b["reopenTag"] is None or not lines:
            f.append("reopen_unrecorded")
        else:
            line = lines[-1]
            mac = hmac.new(
                line["code"].encode(), canon(dict(b, reopenTag=None)), hashlib.sha256
            ).hexdigest()
            if mac != b["reopenTag"]:
                f.append("reopen_tag")
            if ids != [line["deviceId"]]:
                f.append("reopen_device")
    else:
        if b["reopenTag"] is not None:
            f.append("first_rule")
        if not cs or not cs_valid(prev["body"]["devices"]):
            f.append("countersignature")
        keys_removed()  # v5.3, §17A A8
    return f


ORDER = [
    "version_gap",
    "first_rule",
    "previous_hash",
    "owner_key",
    "history_hash",
    "owner_signature",
    "duplicate_device",
    "countersignature",
    "closed_rule",
    "services_changed",
    "server_keys_removed",
    "reopen_unrecorded",
    "reopen_tag",
    "reopen_device",
]
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
rows = re.findall(
    r"^\| (T\d+ [^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]*) \|", text, re.MULTILINE
)
check(len(rows) == 26, "chain rows %d" % len(rows))
check(any(r[0].startswith("T23 ") for r in rows), "the frozen vector T23 is printed")
check(not any(r[0].startswith("T9 ") for r in rows), "T9 stays retired")
check("so the table has no T9" in text, "the T9 note is printed")
for name, held, chain, expect, final in rows:
    frozen = name.startswith("T23 ")
    hist = HISTORY_OF.get(name.split(" ")[0], history)
    prev = None if held.strip() == "nothing" else L(held.strip())
    allf = []
    entries = [x.strip() for x in chain.split(",")]
    # v5.4:  P3 reads the whole document first, so any malformed entry is bad_input for the request.
    if any(p3(L(c)) for c in entries):
        entries, allf = [], ["bad_input"]
    for c in entries:
        allf = failures(prev, L(c), frozen=frozen, hist=hist)
        if allf:
            break
        prev = L(c)
    exp = "ack" if expect.strip() == "ack" else re.search(r"reason `(\w+)`", expect).group(1)
    if exp == "ack":
        check(allf == [], name + " should pass, fails " + str(allf))
    else:
        check(len(allf) == 1 and allf[0] == exp, name + f": expected only {exp}, got {allf}")
        check((final.strip() == "yes") == (exp in FINAL), name + " final column")
    print(f"{name.strip():45} all failing checks: {allf or 'none'}")

check(
    failures(None, lists["T24 list"]) == [] and failures(None, L("v1"), hist=rotated) == [],
    "T24 fails only under the rotated history, and v1 passes under it",
)


# -------- v5.4 strictness, built here from v1 and checked by this file's own P3 and checks
def priv(label):
    s = int.from_bytes(hashlib.sha256(("muster test key " + label).encode()).digest(), "big") % (N - 1) + 1
    return ec.derive_private_key(s, ec.SECP256R1())


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def resigned(body):
    """v1 with this body, signed again by the pinned owner, so only the body's own fault remains."""
    sig = priv("owner").sign(b"muster-trust-list/1\n" + canon(body), ec.ECDSA(hashes.SHA256()))
    return {"body": body, "ownerSignature": b64(sig), "countersignatures": []}


v1b = L("v1")["body"]
own_der = unb64u(OWNER)
compressed = b64(
    bytes.fromhex("3039301306072a8648ce3d020106082a8648ce3d030107032200")
    + bytes([2 + (own_der[-1] & 1)])
    + own_der[27:59]
)
hybrid = b64(own_der[:26] + bytes([6 + (own_der[-1] & 1)]) + own_der[27:])
stray_key = OWNER[:-1] + B64U[B64U.index(OWNER[-1]) | 1]
check(lenient_unb64u(stray_key) == own_der, "the stray-bit key decodes leniently to the owner key")
strict_cases = {
    "compressed owner key": dict(v1b, ownerKey=compressed),
    "hybrid owner key": dict(v1b, ownerKey=hybrid),
    "compressed service key": dict(v1b, services=dict(v1b["services"], serverExecutor=compressed)),
    "owner key with a stray bit": dict(v1b, ownerKey=stray_key),
    "owner key with padding": dict(v1b, ownerKey=OWNER + "=="),
    "leap second": dict(v1b, issuedAt="2026-11-07T23:59:60Z"),
    "February 30": dict(v1b, issuedAt="2026-02-30T20:00:00Z"),
    "year 0000": dict(v1b, issuedAt="0000-11-07T20:00:00Z"),
    "another contract": dict(v1b, contract="muster-trust-list/2"),
}
for what, body in strict_cases.items():
    got = p3(resigned(body))
    check(len(got) == 1, "v5.4 %s:  P3 should refuse it for one reason, got %s" % (what, got))
check(p3(resigned(dict(v1b))) == [] and failures(None, resigned(dict(v1b))) == [], "re-signed v1 passes")
open_empty = resigned(dict(v1b, devices=[]))
check(
    p3(open_empty) == [] and failures(None, open_empty) == ["closed_rule"],
    "v5.4:  an open first list with no device fails check 7 alone",
)
# T26 and T27:  only the stray bit is wrong, so a lenient decoder would accept them.
t26, t27 = lists["T26 list"], lists["T27 list"]
lenient_sig = b64(lenient_unb64u(t26["ownerSignature"]))
check(
    lenient_sig != t26["ownerSignature"]
    and ok(OWNER, lenient_sig, b"muster-trust-list/1\n" + canon(t26["body"]))
    and failures(None, dict(t26, ownerSignature=lenient_sig)) == [],
    "T26 is a valid first list but for its stray bit",
)
cs27 = t27["countersignatures"][0]
lenient_cs = b64(lenient_unb64u(cs27["signature"]))
check(
    lenient_cs != cs27["signature"]
    and failures(L("v1"), dict(t27, countersignatures=[dict(cs27, signature=lenient_cs)])) == [],
    "T27 is a valid rule A list but for its stray bit",
)

# -------- recovery table
t15h, v4h = H(canon(lists["T15 list"])), H(canon(L("v4")))
check(
    H(canon(lists["T15 list"]["body"])) == H(canon(L("v4")["body"])) and t15h != v4h,
    "T15 shares v4's body hash only",
)
for m in re.finditer(
    r"^\| (R\d[^|]+) \| ([^|]+) \| holds version (\d+), body `([0-9a-f]{16})…`(?:;  final refusal of version (\d+), entry `([0-9a-f]{16})…`, `(\w+)`)? \| ([^|]+) \|",
    text,
    re.MULTILINE,
):
    name, pend, held, bodyp, fv, fentry, freason, expect = m.groups()
    p = L(pend.strip())
    held = int(held)
    match_held = [n for n in lists if H(canon(lists[n]["body"])).startswith(bodyp)]
    check(match_held, name + " held hash names a printed list")
    v, ph = p["body"]["version"], H(canon(p["body"]))
    if held == v:
        got = "apply" if ph.startswith(bodyp) else "adopt the server's chain"
    elif held > v:
        got = "fetch the server's chain and walk it"
    else:
        got = (
            "discard"
            if fentry and H(canon(p)).startswith(fentry) and freason in FINAL
            else "keep waiting"
        )
    check(got == expect.strip(), f"{name}: {got} vs {expect}")
    print(f"{name.strip():45} -> {got}")

# -------- approvals
plan_sha = re.search(r"`planSHA256` = `([0-9a-f]{64})`", text).group(1)
check(H(block_after("### Illustrative Plan").encode()) == plan_sha, "plan hash")
devs = {"version 1": L("v1"), "version 3 (closed)": L("v3"), "nothing": None}
for m in re.finditer(r"#### (V\d+[^\n]*)\n\n([^\n]*)\n\n```\n(.*?)\n```", text, re.DOTALL):
    name, note, blk = m.groups()
    f = dict(re.findall(r"^([a-z A-Z0-9()-]+?):\s+(.*)$", blk, re.MULTILINE))
    lines = blk.splitlines()
    hexline = lines[lines.index("approval text bytes (hex):") + 1]
    stdin = lines[-1]
    doc = json.loads(stdin)
    a = doc["approval"]
    txt = "\n".join(
        ["muster-approve/1"]
        + [
            a[k]
            for k in [
                "jobId",
                "jobNonce",
                "approvalNonce",
                "host",
                "action",
                "target",
                "planSHA256",
                "deviceId",
                "issuedAt",
                "expiresAt",
            ]
        ]
    ).encode()
    check(txt.hex() == hexline and H(txt) == f["approval text SHA-256"], name + " text")
    check(
        int(re.search(r"stdin bytes \((\d+) bytes\)", blk).group(1)) == len(stdin), name + " length"
    )
    stored = devs[f["stored list"]]
    now = datetime.datetime.strptime(f["helper clock (now)"], "%Y-%m-%dT%H:%M:%SZ")
    ledger = [] if f["ledger nonces"] == "(none)" else f["ledger nonces"].split(", ")

    def t(s):
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")

    if a["jobNonce"] == a["approvalNonce"]:
        got = "bad_input"
    elif stored is None:
        got = "no_trust_list"
    elif (a["host"], a["action"], a["target"]) != (
        "server",
        "restart",
        "fleet-server-status.timer",
    ):
        got = "field_mismatch"
    elif stored["body"]["closed"] or a["deviceId"] not in [
        d["deviceId"] for d in stored["body"]["devices"]
    ]:
        got = "unknown_device"
    elif not ok(
        [d for d in stored["body"]["devices"] if d["deviceId"] == a["deviceId"]][0]["approvalKey"],
        a["signature"],
        txt,
    ):
        got = "bad_signature"
    elif not 0 < (t(a["expiresAt"]) - t(a["issuedAt"])).total_seconds() <= 120:
        got = "window_too_long"
    elif (t(a["issuedAt"]) - now).total_seconds() > 60:
        got = "not_yet_valid"
    elif now > t(a["expiresAt"]):
        got = "expired"
    elif a["approvalNonce"] in ledger:
        got = "replayed_nonce"
    elif a["planSHA256"] != f["helper plan SHA-256"]:
        got = "plan_mismatch"
    else:
        got = "accepted"
    check(got == f["expected outcome"], f"{name}: {got} vs {f['expected outcome']}")
    # §14:  a helper row exactly when Y8 spent the nonce, which is an acceptance or plan_mismatch.
    audit = re.search(r"Audit row:  (yes|no)", note)
    check(
        audit is not None and (audit.group(1) == "yes") == (got in ("accepted", "plan_mismatch")),
        name + " audit-row statement follows the after-Y8 rule",
    )
    print(f"{name:45} {got}, audit row {audit.group(1) if audit else 'not stated'}")

# -------- signed answers
for label, prefix in [
    ("**A signed plan for V1's job", b"muster-signed-plan/1\n"),
    ("**A done result for V1", b"muster-signed-result/1\n"),
    ("**The refusal for V3", b"muster-signed-refusal/1\n"),
    ("**The refusal for V6", b"muster-signed-refusal/1\n"),
    ("**A chain refusal for T7", b"muster-signed-refusal/1\n"),
    ("**The acknowledgement after T2", b"muster-signed-ack/1\n"),
    ("**The acknowledgement after T3", b"muster-signed-ack/1\n"),
]:
    ans = json.loads(block_after(label))
    others = [
        p
        for p in [
            b"muster-signed-plan/1\n",
            b"muster-signed-result/1\n",
            b"muster-signed-refusal/1\n",
            b"muster-signed-ack/1\n",
        ]
        if p != prefix
    ]
    good = ok(HELPER, ans["signature"], prefix + canon(ans["body"])) and not any(
        ok(HELPER, ans["signature"], p + canon(ans["body"])) for p in others
    )
    check(good and ans["keyId"] == H(unb64u(HELPER))[:16], label + " signature")
    body = ans["body"]
    if "plan" in body:
        check(
            H(body["plan"].encode()) == body["planSHA256"] == plan_sha
            and canon(json.loads(body["plan"])).decode() == body["plan"],
            "plan answer",
        )
    if "chain" in body:
        check(
            body["chain"]
            == {"version": 2, "entrySHA256": H(canon(lists["T7 list"])), "final": False}
            and body["reason"] == "previous_hash"
            and failures(L("v1"), lists["T7 list"]) == ["previous_hash"],
            "chain refusal names T7",
        )
        # v5.4, §13:  a chain refusal from P3 on carries `chain` and no `claimed`.
        check("claimed" not in body, "the chain refusal carries no claimed")
    if "finalRefusals" in body:
        check(body["finalRefusals"] == [], "finalRefusals empty in v5")
        held = {2: "v2", 3: "v3"}[body["helperTrustListVersion"]]
        check(
            body["trustListSHA256"] == H(canon(L(held)["body"]))
            and body["closed"] == L(held)["body"]["closed"],
            "ack hash",
        )
    if "plan" in body or "outcome" in body:
        cmds = json.loads(block_after("### Illustrative Plan"))["commands"]
        check(all(c[0].startswith("/") for c in cmds), "argv0 absolute")
    print(f"{label[2:]:45} helper signature, prefix only:  {good}")

print("FAILURES:", FAILS if FAILS else 0)
sys.exit(1 if FAILS else 0)
