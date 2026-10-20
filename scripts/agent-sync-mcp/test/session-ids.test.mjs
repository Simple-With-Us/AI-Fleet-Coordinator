// AFC-361:  the session id rule for the hosted Worker (src/session-ids.js), twin of scripts/agent_sync/session_ids.py.
// No dependencies:  plain `node --test`.  The registry copy is checked against fleet-apps.json here too.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import * as S from "../src/session-ids.js";
import { ROOT } from "./helpers.mjs";

const REGISTRY = JSON.parse(readFileSync(path.join(ROOT, "../../fleet-apps.json"), "utf8")).sessionIds;

test("the constants are the fleet-apps.json sessionIds block", () => {
  assert.equal(S.ID_PATTERN, REGISTRY.idPattern);
  assert.equal(S.GENERIC_ENV, REGISTRY.genericEnv);
  assert.deepEqual([...S.EXEMPT_SEATS], REGISTRY.exemptSeats);
  assert.deepEqual([...S.EXEMPT_PATTERNS], REGISTRY.exemptPatterns);
  assert.deepEqual(Object.fromEntries(Object.entries(S.ENV_BY_SEAT).map(([k, v]) => [k, [...v]])), REGISTRY.envBySeat);
  assert.deepEqual([...S.FALLBACK_ENV], REGISTRY.fallbackEnv);
});

test("exempt seats are the single-thread identities, after the canonical name rule", () => {
  for (const seat of ["GROK-BOT", "GB-FIXER", "gb-oracle", "MA", "ROB", "JET", "WISE", "INSTINCT", "ECHO"]) assert.equal(S.isExempt(seat), true, seat);
  for (const seat of ["CLAUDE", "CODEX", "GROK", "GROK-WEB", "BF-DEPLOYER", "FX", "GB", "XGB-FIXER", "", null, undefined]) assert.equal(S.isExempt(seat), false, String(seat));
});

test("an explicit value wins, and an invalid one is an error, not ignored", () => {
  const env = { AGENT_SESSION: "generic-id-0001", CLAUDE_CODE_SESSION_ID: "claude-id-0001" };
  assert.equal(S.detect("CLAUDE", "explicit-id-01", env), "explicit-id-01");
  assert.equal(S.detect("CLAUDE", "  padded-id-01  ", {}), "padded-id-01");
  for (const bad of ["abc", "-dash", "has space", "x".repeat(129), "bad/char"]) assert.throws(() => S.detect("CLAUDE", bad, env), RangeError, bad);
  assert.throws(() => S.detect("CLAUDE", 42, env), /must be text/);
});

test("AGENT_SESSION, then the seat's own variable;  a seat with an entry never falls back to other platforms' variables", () => {
  assert.equal(S.detect("CLAUDE", undefined, { AGENT_SESSION: "generic-id-0001", CLAUDE_CODE_SESSION_ID: "claude-id-0001" }), "generic-id-0001");
  assert.equal(S.detect("CLAUDE", undefined, { CLAUDE_CODE_SESSION_ID: "claude-id-0001" }), "claude-id-0001");
  assert.equal(S.detect("CODEX", undefined, { CODEX_THREAD_ID: "019a-thread-1" }), "019a-thread-1");
  assert.equal(S.detect("CURSOR", undefined, { CURSOR_AGENT_CHAT_ID: "chat-id-0002" }), "chat-id-0002");
  assert.equal(S.detect("CLAUDE", undefined, { CODEX_THREAD_ID: "019a-thread-1" }), null);
  assert.equal(S.detect("FX", undefined, { CODEX_THREAD_ID: "019a-thread-1" }), "019a-thread-1");
  assert.equal(S.detect("FX", undefined, {}), null);
  assert.equal(S.detect("FX"), null, "a Worker has no environment");
  assert.throws(() => S.detect("CLAUDE", undefined, { CLAUDE_CODE_SESSION_ID: "no" }), /CLAUDE_CODE_SESSION_ID/);
});

test("require refuses a non-exempt writer with a message that names the argument and the platform variable", () => {
  assert.throws(() => S.require("CLAUDE", undefined, {}, "tool"), (e) => e instanceof S.SessionRequired && /`session` argument/.test(e.message) && /AGENT_SESSION/.test(e.message) && /CLAUDE_CODE_SESSION_ID/.test(e.message));
  assert.throws(() => S.require("CODEX", undefined, {}, "cli"), (e) => /--session ID/.test(e.message) && /CODEX_THREAD_ID/.test(e.message));
  assert.throws(() => S.require("GROK-WEB", undefined, undefined, "hosted"), (e) => /`session` argument/.test(e.message) && !/AGENT_SESSION/.test(e.message));
  assert.throws(() => S.require("CLAUDE", undefined, {}, "nowhere"), /surface/);
});

test("require lets exempt seats through, records an id they have, and cannot be talked into exempting anyone else", () => {
  for (const seat of ["GROK-BOT", "GB-FIXER", "MA", "JET", "WISE", "ECHO"]) {
    assert.equal(S.require(seat, undefined, {}), null, seat);
    assert.equal(S.require(seat, "opt-id-0001", {}), "opt-id-0001", seat);
    assert.equal(S.require(seat, undefined, { AGENT_SESSION: "env-id-0001" }), "env-id-0001", seat);
    assert.equal(S.require(seat, undefined, { CLAUDE_CODE_SESSION_ID: "no" }), null, "a junk env value is passed over for an exempt seat");
    assert.throws(() => S.require(seat, "no", {}), RangeError, "a junk explicit value is still an error");
  }
  assert.throws(() => S.require("CLAUDE", undefined, { CLAUDE_CODE_SESSION_ID: "no" }), RangeError);
  assert.equal(S.require.length >= 1, true);
  assert.equal(S.requireSession, S.require);
});
