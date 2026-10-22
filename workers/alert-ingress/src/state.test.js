// State-machine tests.  Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import {
  decide,
  sweep,
  initialState,
  normalizeSeverity,
  formatEventLine,
  DEFAULT_MIN_OPEN_MS,
  PROD_DOWN_MIN_OPEN_MS,
} from "./state.js";

const T0 = 1_760_000_000_000;
const MIN = 60 * 1000;

test("first firing event is held, not announced", () => {
  const r = decide(null, { firing: true, severity: "SEVERE" }, T0);
  assert.equal(r.action, "hold");
  assert.equal(r.state.phase, "pending");
});

test("a flap that resolves before the window is discarded entirely", () => {
  const opened = decide(null, { firing: true, severity: "SEVERE" }, T0);
  const closed = decide(opened.state, { firing: false }, T0 + 2 * MIN);
  // Nobody ever saw it open, so nobody needs to see it close.
  assert.equal(closed.action, "discard");
  assert.equal(closed.state.phase, "ok");
});

test("the flapping-monitor case: ten events, one announcement", () => {
  // Exactly the Mac.Jays.Services load monitor from Oct 18.
  let state = initialState();
  const actions = [];
  let t = T0;

  // Five Triggered/Recovered pairs inside the 10 minute window.
  for (let i = 0; i < 5; i++) {
    state = decide(state, { firing: true, severity: "MODERATE", text: "load high" }, t).state;
    actions.push("triggered");
    t += 4 * MIN;
    state = decide(state, { firing: false }, t).state;
    actions.push("recovered");
    t += 4 * MIN;
  }

  // Nothing was ever announced, so nothing was ever posted.
  const announced = actions.filter((a) => a === "announced");
  assert.equal(announced.length, 0);
  assert.equal(state.phase, "ok");

  // Now it stays open past the window.
  state = decide(state, { firing: true, severity: "MODERATE", text: "load high" }, t).state;
  assert.equal(state.phase, "pending");
  const promoted = sweep(state, t + DEFAULT_MIN_OPEN_MS + 1);
  assert.equal(promoted.action, "announce");
  assert.equal(promoted.state.phase, "alerting");

  // And a long-open incident that flaps a hundred more times posts nothing more.
  state = promoted.state;
  const after = [];
  for (let i = 0; i < 100; i++) {
    t += MIN;
    after.push(decide(state, { firing: true, severity: "MODERATE", text: "load high" }, t).action);
  }
  assert.equal(after.filter((a) => a !== "refresh").length, 0);
});

test("sweep will not promote before the window", () => {
  const opened = decide(null, { firing: true, severity: "SEVERE" }, T0).state;
  assert.equal(sweep(opened, T0 + DEFAULT_MIN_OPEN_MS - 1), null);
  assert.ok(sweep(opened, T0 + DEFAULT_MIN_OPEN_MS));
});

test("production down uses the shorter wait", () => {
  const opened = decide(null, { firing: true, severity: "SEVERE", prodDown: true }, T0).state;
  assert.ok(sweep(opened, T0 + PROD_DOWN_MIN_OPEN_MS));
  assert.equal(sweep(opened, T0 + PROD_DOWN_MIN_OPEN_MS - 1), null);
});

test("escalation posts, de-escalation does not", () => {
  let state = decide(null, { firing: true, severity: "MODERATE" }, T0).state;
  state = sweep(state, T0 + DEFAULT_MIN_OPEN_MS).state;

  const worse = decide(state, { firing: true, severity: "SEVERE" }, T0 + 20 * MIN);
  assert.equal(worse.action, "update");
  assert.equal(worse.state.severity, "SEVERE");

  const better = decide(worse.state, { firing: true, severity: "MODERATE" }, T0 + 25 * MIN);
  assert.equal(better.action, "refresh");
  assert.equal(better.state.severity, "MODERATE");
});

test("a resolved announced incident posts its resolution", () => {
  let state = decide(null, { firing: true, severity: "SEVERE" }, T0).state;
  state = sweep(state, T0 + DEFAULT_MIN_OPEN_MS).state;
  const closed = decide(state, { firing: false }, T0 + 30 * MIN);
  assert.equal(closed.action, "resolve");
  assert.equal(closed.state.phase, "ok");
});

test("a second identical alert after resolution is a fresh hold", () => {
  let state = decide(null, { firing: true, severity: "SEVERE" }, T0).state;
  state = sweep(state, T0 + DEFAULT_MIN_OPEN_MS).state;
  state = decide(state, { firing: false }, T0 + 20 * MIN).state;
  const again = decide(state, { firing: true, severity: "SEVERE" }, T0 + 40 * MIN);
  assert.equal(again.action, "hold");
});

test("severity vocabularies from vendors normalize", () => {
  assert.equal(normalizeSeverity("critical"), "SEVERE");
  assert.equal(normalizeSeverity("warning"), "MODERATE");
  assert.equal(normalizeSeverity("recovered"), "INFO");
  assert.equal(normalizeSeverity(undefined, "MODERATE"), "MODERATE");
  assert.equal(normalizeSeverity("banana", "INFO"), "INFO");
});

test("the event line matches the documented alerts format", () => {
  const line = formatEventLine({
    severity: "SEVERE",
    service: "Mac.Jays.Services",
    what: "System load high",
    link: "https://example.com/m/1",
  });
  assert.equal(line, "SEVERE  \u2022  Mac.Jays.Services  \u2022  System load high  \u2022  https://example.com/m/1");
  // No link is still a valid line.
  assert.equal(
    formatEventLine({ severity: "INFO", service: "svc", what: "recovered" }),
    "INFO  \u2022  svc  \u2022  recovered",
  );
});