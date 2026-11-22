import test from "node:test";
import assert from "node:assert/strict";
import { nativeOriginContext } from "../src/native-origin-context.js";

const child = "019a1234-abcd-7123-8123-0123456789ab";
const root = "019a1234-abcd-7123-8123-0123456789ac";

test("exact per-call thread forms retain child identity, not shared session", () => {
  for (const meta of [
    { threadId: child, sessionId: root },
    { "x-codex-turn-metadata": { thread_id: child, session_id: root } },
    { threadId: child, "x-codex-turn-metadata": { thread_id: child, session_id: root } },
  ]) {
    assert.deepEqual(nativeOriginContext(meta), { state: "exact", session: child });
    assert.ok(Object.isFrozen(nativeOriginContext(meta)));
  }
});

test("missing exact thread never falls back to root session", () => {
  for (const meta of [undefined, {}, { sessionId: root }, { "x-codex-turn-metadata": { session_id: root } }]) {
    assert.deepEqual(nativeOriginContext(meta), { state: "missing", session: null });
  }
});

test("malformed or conflicting candidates hold without retaining raw metadata", () => {
  for (const meta of [null, [], "bad", { threadId: true }, { threadId: "../wrong" },
    { threadId: child, "x-codex-turn-metadata": null },
    { threadId: child, "x-codex-turn-metadata": { thread_id: 1 } }]) {
    assert.deepEqual(nativeOriginContext(meta), { state: "invalid", session: null });
  }
  assert.deepEqual(nativeOriginContext({ threadId: root, "x-codex-turn-metadata": { thread_id: child } }),
    { state: "conflict", session: null });
});

test("each invocation creates an independent snapshot without process context", () => {
  const meta = { threadId: child };
  const first = nativeOriginContext(meta);
  meta.threadId = root;
  const second = nativeOriginContext(meta);
  assert.equal(first.session, child);
  assert.equal(second.session, root);
  assert.deepEqual(nativeOriginContext(undefined), { state: "missing", session: null });
});

test("no creation-time lookup means held, never caller claims or later generation", async () => {
  const { resolveNativeOriginClaims } = await import("../src/native-origin-context.js");
  assert.deepEqual(await resolveNativeOriginClaims("CODEX", nativeOriginContext({ threadId: child })),
    { state: "held", reason: "lookup-unavailable", claims: null });
  const claims = { origin_session_claim: child, origin_letter_claim: "AA", origin_generation_claim: 7 };
  const resolved = await resolveNativeOriginClaims("CODEX", nativeOriginContext({ threadId: child }), async (request) => {
    assert.deepEqual(request, { seat: "CODEX", session: child });
    assert.ok(Object.isFrozen(request));
    return claims;
  });
  assert.equal(resolved.state, "resolved");
  claims.origin_generation_claim = 8;
  assert.equal(resolved.claims.origin_generation_claim, 7);
  for (const bad of [{ origin_session_claim: root, origin_letter_claim: "AA", origin_generation_claim: 7 },
    { origin_session_claim: child, origin_letter_claim: "A", origin_generation_claim: 7 },
    { origin_session_claim: child, origin_letter_claim: "AA", origin_generation_claim: true }]) {
    assert.equal((await resolveNativeOriginClaims("CODEX", nativeOriginContext({ threadId: child }), async () => bad)).state, "held");
  }
  let lookedUp = false;
  assert.equal((await resolveNativeOriginClaims("CLAUDE", nativeOriginContext({ threadId: child }), async () => { lookedUp = true; })).state, "held");
  assert.equal(lookedUp, false);
});
