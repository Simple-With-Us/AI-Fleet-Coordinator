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
        assert all(0x20 <= ord(c) <= 0x7E for c in v)
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ",".join(enc(x) for x in v) + "]"
    keys = sorted(v, key=lambda k: k.encode("ascii"))
    return "{" + ",".join(enc(k) + ":" + enc(v[k]) for k in keys) + "}"


def canon(v):
    return enc(v).encode("ascii")


def H(b):
    return hashlib.sha256(b).hexdigest()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ok(pub, sig, data):
    try:
        serialization.load_der_public_key(unb64u(pub)).verify(
            unb64u(sig), data, ec.ECDSA(hashes.SHA256())
        )
        return True
    except (InvalidSignature, ValueError):
        return False


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
check(len(lists) == 20, "list count %d" % len(lists))
alias = {
    "v1": "Version 1",
    "v2": "Version 2 (rule A)",
    "v3": "Version 3 (rule B, closed)",
    "v4": "Version 4 (rule C, reopened)",
    "v4 at 20:55": "Version 4, signed again at 20:55 (T22, R5)",
}


def L(n):
    return lists[alias.get(n, n)]


# -------- chain checker:  every check, not only the first
def hist_key(v):
    for h in history:
        if h["fromVersion"] <= v and (h["toVersion"] is None or v <= h["toVersion"]):
            return h["ownerKey"]


def failures(prev, e, frozen=False):
    """Every failing check.  frozen=True is version 5 as frozen:  rule C is not available."""
    b, f = e["body"], []
    bb = canon(b)
    if b["version"] != (1 if prev is None else prev["body"]["version"] + 1):
        f.append("version_gap")
    if prev is None and b["previousSHA256"] is not None:
        f.append("first_rule")
    if prev is not None and b["previousSHA256"] != H(canon(prev["body"])):
        f.append("previous_hash")
    k = hist_key(b["version"])
    if b["ownerKey"] != k:
        f.append("owner_key")
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

    def svc():
        p = prev["body"]["services"]
        if not all(v is None or v == p[k2] for k2, v in b["services"].items()):
            f.append("services_changed")
        if (p["serverHelper"] or p["serverPublisher"]) and not (
            b["services"]["serverHelper"] or b["services"]["serverPublisher"]
        ):
            f.append("server_keys_removed")

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
    return f


ORDER = [
    "version_gap",
    "first_rule",
    "previous_hash",
    "owner_key",
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
check(len(rows) == 22, "chain rows %d" % len(rows))
check(any(r[0].startswith("T23 ") for r in rows), "the frozen vector T23 is printed")
for name, held, chain, expect, final in rows:
    frozen = name.startswith("T23 ")
    prev = None if held.strip() == "nothing" else L(held.strip())
    allf = []
    for c in [x.strip() for x in chain.split(",")]:
        allf = failures(prev, L(c), frozen=frozen)
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
