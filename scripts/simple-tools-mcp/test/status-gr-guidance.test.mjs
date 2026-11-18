// AFC-844:  /admin's Recent Errors And Refusals reads the diagnostics log, so the relay
// check rows (gr_bearer, gr_assert) appear there.  Their codes overlap the wake route's
// (bad_signature, stale), and the page must explain them as relay checks, not wakes.

import test from "node:test";
import assert from "node:assert/strict";
import { renderStatusOverview } from "../src/status.js";

const NOW = Date.parse("2026-11-18T20:00:00Z");
const e = { escape: (s) => String(s), time: (ts) => new Date(ts).toISOString(), gap: "  " };

function render(runtimeErrors) {
  const seats = [{ seat: "CODEX", keyInstalled: true, role: { ok: true }, grants: [{ id: "g" }], paused: false, unavailableFields: [] }];
  const statusCalls = [{ seat: "CODEX", ts: NOW - 60_000, tool: "zulip_post", outcome: "ok" }];
  return renderStatusOverview({ seats, statusCalls, runtimeErrors, generatedAt: NOW }, e);
}

test("relay check rows get relay guidance, not the wake route's", () => {
  const html = render([
    { kind: "gr_bearer", reason: "bad_signature", seat: null, count: 40, ts: NOW - 1000, status: 401 },
    { kind: "gr_assert", reason: "stale", seat: null, count: 2, ts: NOW - 2000, status: 401 },
    { kind: "gr_assert", reason: "grant_invalid:stale_epoch", seat: "CODEX", count: 1, ts: NOW - 500, status: 200 },
    { kind: "gr_assert", reason: "assertion:bad_signature", seat: null, count: 1, ts: NOW - 300, status: 200 },
  ]);
  assert.ok(!html.includes("The wake signature is invalid."));
  assert.ok(!html.includes("The wake arrived outside its accepted time window."));
  assert.ok(html.includes("The relay's check call carried a wrong channel signature."));
  assert.ok(html.includes("The relay's check call was older than the accepted window."));
  assert.ok(html.includes("The assertion's grant is no longer live"));
  assert.ok(html.includes("The assertion was not signed with the Worker's current signing key."));
  assert.ok(html.includes("grant_invalid:stale_epoch"), "the detail stays visible as the code");
});

test("a wake row with the same code keeps the wake guidance", () => {
  const html = render([{ kind: "wake", reason: "bad_signature", seat: "JET", count: 1, ts: NOW - 1000, status: 401 }]);
  assert.ok(html.includes("The wake signature is invalid."));
});
