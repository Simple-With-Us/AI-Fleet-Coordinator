// The listener's signed wake (src/wake.js):  HMAC over the raw bytes, the
// freshness window, the contract shape, and the path parser.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { verifyWake, wakeSignature, wakePathSeat, readBodyBytes, WAKE_MAX_BODY } from "../src/wake.js";
import { classifyPath } from "../src/policy.js";

const KEY = "k".repeat(64);
const NOW = Date.UTC(2026, 9, 9, 15, 0, 0);

/** The listener's body (adapters.py routine_body) serialized like routine_bytes. */
export function wakeBody(overrides = {}) {
  const body = {
    contract: "agent-sync-wake/1",
    seat: "JET",
    wake_id: "w-0001",
    message_id: 4242,
    trigger_ids: [4242],
    dm: false,
    channel: "agent-sync",
    topic: "AFC Jet Zulip bridge",
    stream_id: 642232,
    dm_recipient_ids: [],
    sender_user_id: 1211974,
    sender_full_name: "Jay",
    is_bot: false,
    owner: true,
    excerpt: "<<<BEGIN nonce=abc\n@**Jet** hello\n>>>END nonce=abc",
    zulip_link: "https://simplewithus.zulipchat.com/#narrow/channel/642232-agent-sync/topic/x/near/4242",
    reply_to: { type: "stream", channel: "agent-sync", topic: "AFC Jet Zulip bridge" },
    reply_prefix: "[JET·wake] re=4242",
    sent_at: Math.floor(NOW / 1000),
    ...overrides,
  };
  const sorted = Object.fromEntries(Object.keys(body).sort().map((k) => [k, body[k]]));
  return new TextEncoder().encode(JSON.stringify(sorted));
}

/** Python's hmac.new(key.encode(), body, sha256).hexdigest(). */
const pySig = (key, bytes) => createHmac("sha256", Buffer.from(key, "utf8")).update(bytes).digest("hex");

test("the signature matches the listener's hex HMAC-SHA256 over the raw bytes", async () => {
  const bytes = wakeBody();
  assert.equal(await wakeSignature(KEY, bytes), pySig(KEY, bytes));
});

test("a correctly signed, fresh wake is accepted", async () => {
  const bytes = wakeBody();
  const out = await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW });
  assert.equal(out.ok, true);
  assert.equal(out.wake.wake_id, "w-0001");
});

test("a wrong key, a tampered body or a missing header is 401", async () => {
  const bytes = wakeBody();
  assert.deepEqual(await verifyWake({ bytes, signature: pySig("x".repeat(64), bytes), key: KEY, seat: "JET", now: NOW }), { ok: false, status: 401, reason: "bad_signature" });
  const sig = pySig(KEY, bytes);
  const tampered = wakeBody({ owner: false });
  assert.equal((await verifyWake({ bytes: tampered, signature: sig, key: KEY, seat: "JET", now: NOW })).reason, "bad_signature");
  assert.equal((await verifyWake({ bytes, signature: null, key: KEY, seat: "JET", now: NOW })).status, 401);
  assert.equal((await verifyWake({ bytes, signature: `sha256=${sig}`, key: KEY, seat: "JET", now: NOW })).status, 401);
  assert.equal((await verifyWake({ bytes, signature: sig.toUpperCase(), key: KEY, seat: "JET", now: NOW })).status, 401);
});

test("a stale or future sent_at is refused", async () => {
  const old = wakeBody({ sent_at: Math.floor(NOW / 1000) - 301 });
  assert.deepEqual(await verifyWake({ bytes: old, signature: pySig(KEY, old), key: KEY, seat: "JET", now: NOW }), { ok: false, status: 401, reason: "stale" });
  const edge = wakeBody({ sent_at: Math.floor(NOW / 1000) - 299 });
  assert.equal((await verifyWake({ bytes: edge, signature: pySig(KEY, edge), key: KEY, seat: "JET", now: NOW })).ok, true);
  const future = wakeBody({ sent_at: Math.floor(NOW / 1000) + 61 });
  assert.equal((await verifyWake({ bytes: future, signature: pySig(KEY, future), key: KEY, seat: "JET", now: NOW })).reason, "future");
});

test("the body's seat must be the path's seat, and the contract must match", async () => {
  const other = wakeBody({ seat: "GROK-WEB" });
  assert.equal((await verifyWake({ bytes: other, signature: pySig(KEY, other), key: KEY, seat: "JET", now: NOW })).reason, "seat_mismatch");
  const contract = wakeBody({ contract: "agent-sync-wake/2" });
  assert.equal((await verifyWake({ bytes: contract, signature: pySig(KEY, contract), key: KEY, seat: "JET", now: NOW })).reason, "bad_contract");
  const badId = wakeBody({ wake_id: "../../etc" });
  assert.equal((await verifyWake({ bytes: badId, signature: pySig(KEY, badId), key: KEY, seat: "JET", now: NOW })).reason, "bad_wake_id");
  const badReply = wakeBody({ reply_to: { type: "everyone" } });
  assert.equal((await verifyWake({ bytes: badReply, signature: pySig(KEY, badReply), key: KEY, seat: "JET", now: NOW })).reason, "bad_reply_to");
});

test("stream_id is a positive safe integer or null, and may be absent (an older listener)", async () => {
  const check = async (overrides) => {
    const bytes = wakeBody(overrides);
    return verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW });
  };
  for (const ok of [642232, 1, Number.MAX_SAFE_INTEGER, null, undefined]) {
    assert.equal((await check({ stream_id: ok })).ok, true, String(ok));
  }
  const dm = await check({ dm: true, channel: null, topic: null, stream_id: null, reply_to: { type: "direct", to: [1211974] } });
  assert.equal(dm.ok, true);
  for (const bad of ["642232", 642232.5, true, false, 0, -7, 2 ** 53, 1e300, [642232], { id: 642232 }]) {
    assert.deepEqual(await check({ stream_id: bad }), { ok: false, status: 400, reason: "bad_stream_id" }, JSON.stringify(bad));
  }
  // A huge integer in the raw JSON parses to an unsafe number and is refused too.
  const raw = new TextEncoder().encode(new TextDecoder().decode(wakeBody()).replace('"stream_id":642232', '"stream_id":123456789012345678901'));
  assert.equal((await verifyWake({ bytes: raw, signature: pySig(KEY, raw), key: KEY, seat: "JET", now: NOW })).reason, "bad_stream_id");
});

test("dm and reply_to must agree on whether a wake is a DM", async () => {
  const check = async (overrides) => {
    const bytes = wakeBody(overrides);
    return (await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW })).reason ?? "ok";
  };
  assert.equal(await check({ dm: true, channel: null, topic: null, stream_id: null }), "bad_reply_to", "dm with a stream reply_to");
  assert.equal(await check({ reply_to: { type: "direct", to: [1211974] } }), "bad_reply_to", "a channel wake with a direct reply_to");
});

/** One held-back item as the listener sends it. */
function heldItem(overrides = {}) {
  return {
    message_id: 4100,
    dm: false,
    channel: "agent-sync",
    topic: "budget",
    stream_id: 642232,
    sender_full_name: "Codex",
    zulip_link: "https://simplewithus.zulipchat.com/#narrow/channel/642232-agent-sync/topic/budget/near/4100",
    reason: "wakes_per_hour",
    at: Math.floor(NOW / 1000) - 600,
    ...overrides,
  };
}

const dmItem = (overrides = {}) =>
  heldItem({ message_id: 4101, dm: true, channel: null, topic: null, stream_id: null, zulip_link: "https://simplewithus.zulipchat.com/#narrow/dm/9,1211974-dm/near/4101", ...overrides });

async function heldBackVerdict(heldBack) {
  const bytes = wakeBody({ held_back: heldBack });
  const out = await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW });
  return out.ok ? "ok" : out.reason;
}

test("held_back is optional, and a well-formed one is accepted", async () => {
  for (const good of [
    undefined,
    null,
    { count: 0, items: [] },
    { count: 1, items: [heldItem()] },
    { count: 57, items: [heldItem(), dmItem()] },
    { count: 3, items: [] },
    { count: 20, items: Array.from({ length: 20 }, (_, i) => heldItem({ message_id: 5000 + i })) },
    { count: 1, items: [heldItem({ channel: "c".repeat(200), topic: "t".repeat(200), sender_full_name: "s".repeat(200), zulip_link: "", reason: "overflow", at: 0 })] },
    // Keys an item does not know are ignored (events.js never copies them), so a newer listener is not refused.
    { count: 1, items: [heldItem({ trigger_ids: [4100], owner: true })] },
  ]) {
    assert.equal(await heldBackVerdict(good), "ok", JSON.stringify(good)?.slice(0, 120));
  }
  const bytes = wakeBody({ held_back: { count: 2, items: [heldItem(), dmItem()] } });
  const out = await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW });
  assert.deepEqual(out.wake.held_back.items[1].dm, true);
});

test("a malformed held_back is 400 bad_held_back", async () => {
  const bad = {
    "not an object": [heldItem()],
    "a string": "3 held back",
    "a number": 3,
    "count missing": { items: [] },
    "count negative": { count: -1, items: [] },
    "count fractional": { count: 1.5, items: [] },
    "count a string": { count: "1", items: [] },
    "items missing": { count: 1 },
    "items an object": { count: 1, items: { 0: heldItem() } },
    "more items than the count": { count: 1, items: [heldItem(), dmItem()] },
    "21 items": { count: 21, items: Array.from({ length: 21 }, (_, i) => heldItem({ message_id: 5000 + i })) },
    "an item that is not an object": { count: 1, items: ["4100"] },
    "a null item": { count: 1, items: [null] },
    "message_id 0": { count: 1, items: [heldItem({ message_id: 0 })] },
    "message_id a string": { count: 1, items: [heldItem({ message_id: "4100" })] },
    "message_id unsafe": { count: 1, items: [heldItem({ message_id: 2 ** 53 })] },
    "dm a string": { count: 1, items: [heldItem({ dm: "false" })] },
    "channel a number": { count: 1, items: [heldItem({ channel: 642232 })] },
    "topic missing": { count: 1, items: [heldItem({ topic: undefined })] },
    "stream_id 0": { count: 1, items: [heldItem({ stream_id: 0 })] },
    "stream_id a string": { count: 1, items: [heldItem({ stream_id: "642232" })] },
    "sender missing": { count: 1, items: [heldItem({ sender_full_name: undefined })] },
    "sender null": { count: 1, items: [heldItem({ sender_full_name: null })] },
    "link missing": { count: 1, items: [heldItem({ zulip_link: undefined })] },
    "reason upper case": { count: 1, items: [heldItem({ reason: "Wakes_Per_Hour" })] },
    "reason with a space": { count: 1, items: [heldItem({ reason: "wakes per hour" })] },
    "reason empty": { count: 1, items: [heldItem({ reason: "" })] },
    "reason 65 characters": { count: 1, items: [heldItem({ reason: "a".repeat(65) })] },
    "at negative": { count: 1, items: [heldItem({ at: -1 })] },
    "at fractional": { count: 1, items: [heldItem({ at: 1.5 })] },
    "at a string": { count: 1, items: [heldItem({ at: "now" })] },
    "a DM item naming a channel": { count: 1, items: [dmItem({ channel: "secret-plans" })] },
    "a DM item naming a topic": { count: 1, items: [dmItem({ topic: "launch" })] },
    "a DM item with a stream id": { count: 1, items: [dmItem({ stream_id: 999001 })] },
  };
  for (const [name, heldBack] of Object.entries(bad)) {
    assert.equal(await heldBackVerdict(heldBack), "bad_held_back", name);
  }
  // Over-length strings:  201 code units in any string field is refused (200 passes, above).
  for (const field of ["channel", "topic", "sender_full_name", "zulip_link"]) {
    assert.equal(await heldBackVerdict({ count: 1, items: [heldItem({ [field]: "x".repeat(201) })] }), "bad_held_back", field);
  }
  // Signed by the listener's key, so it is the shape, not the signature, that is refused.
  const bytes = wakeBody({ held_back: { count: -1, items: [] } });
  assert.deepEqual(await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW }), { ok: false, status: 400, reason: "bad_held_back" });
});

test("a short or missing key means not configured (404), and an oversized body is 413", async () => {
  const bytes = wakeBody();
  assert.equal((await verifyWake({ bytes, signature: pySig("short", bytes), key: "short", seat: "JET", now: NOW })).status, 404);
  assert.equal((await verifyWake({ bytes, signature: "", key: undefined, seat: "JET", now: NOW })).status, 404);
  const big = new Uint8Array(WAKE_MAX_BODY + 1);
  assert.equal((await verifyWake({ bytes: big, signature: pySig(KEY, big), key: KEY, seat: "JET", now: NOW })).status, 413);
});

test("signed garbage is 400, not 500", async () => {
  const bytes = new TextEncoder().encode("not json");
  assert.equal((await verifyWake({ bytes, signature: pySig(KEY, bytes), key: KEY, seat: "JET", now: NOW })).reason, "bad_json");
  const arr = new TextEncoder().encode("[1]");
  assert.equal((await verifyWake({ bytes: arr, signature: pySig(KEY, arr), key: KEY, seat: "JET", now: NOW })).reason, "bad_json");
});

test("the wake path names one seat and nothing else", () => {
  assert.equal(wakePathSeat("/internal/wake/JET"), "JET");
  assert.equal(wakePathSeat("/internal/wake/GROK-WEB"), "GROK-WEB");
  assert.equal(wakePathSeat("/internal/wake/jet"), null);
  assert.equal(wakePathSeat("/internal/wake/JET/"), null);
  assert.equal(wakePathSeat("/internal/wake/"), null);
  assert.equal(classifyPath("/internal/wake/JET"), "wake");
  assert.equal(classifyPath("/internal/wake"), null);
  assert.equal(classifyPath("/internal/other"), null);
});

test("readBodyBytes keeps the exact bytes and stops past the cap", async () => {
  const raw = new Uint8Array([0xef, 0xbb, 0xbf, 0x7b, 0x7d]); // a BOM, then {}
  const got = await readBodyBytes(new Request("https://x.example/", { method: "POST", body: raw }));
  assert.deepEqual([...got], [...raw]);
  assert.equal(await readBodyBytes(new Request("https://x.example/", { method: "POST", body: new Uint8Array(10) }), 9), null);
});
