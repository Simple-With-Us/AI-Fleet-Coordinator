// Normalizer tests.  Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { normalize, fromDatadog, fromBetterStack, fromGeneric } from "./normalize.js";

test("datadog triggered and recovered are distinguishable", () => {
  const triggered = fromDatadog({
    alert_type: "Triggered",
    monitor_id: 22024797,
    alert_title: "System load is high for host Mac.Jays.Services:",
    host: "Mac.Jays.Services",
    event_url: "https://us5.datadoghq.com/event/event?id=1",
  });
  assert.equal(triggered.source, "datadog");
  assert.equal(triggered.firing, true);
  assert.equal(triggered.key, "datadog:22024797");
  assert.equal(triggered.service, "Mac.Jays.Services");

  const recovered = fromDatadog({
    alert_type: "Recovered",
    monitor_id: 22024797,
    alert_title: "System load is high for host Mac.Jays.Services:",
    host: "Mac.Jays.Services",
  });
  assert.equal(recovered.firing, false);
  // Same key, so the two events close and open the same incident.
  assert.equal(recovered.key, triggered.key);
});

test("datadog's verbose template collapses to one line", () => {
  const e = fromDatadog({
    alert_type: "Triggered",
    monitor_id: 7,
    alert_title: "Disk\n  usage   is high",
    host: "Mac.Jays.Services",
  });
  assert.ok(!e.text.includes("\n"), "text must be a single line");
  assert.equal(e.text, "Disk usage is high");
});

test("datadog falls back to a stable key when there is no monitor id", () => {
  const a = fromDatadog({ alert_type: "Triggered", alert_title: "CPU high" });
  const b = fromDatadog({ alert_type: "Recovered", alert_title: "CPU high" });
  assert.equal(a.key, b.key);
});

test("betterstack up and down map to firing/not firing", () => {
  const down = fromBetterStack({
    data: { check_name: "congress.trade", status: "down", url: "https://betterstack.com/uptime/checker/abc" },
  });
  assert.equal(down.firing, true);
  assert.equal(down.source, "betterstack");

  const up = fromBetterStack({
    data: { check_name: "congress.trade", status: "up", url: "https://betterstack.com/uptime/checker/abc" },
  });
  assert.equal(up.firing, false);
});

test("betterstack status words are classified, not guessed", () => {
  for (const status of ["down", "failing", "failed"]) {
    assert.equal(fromBetterStack({ data: { status, check_name: "x" } }).firing, true, status);
  }
  for (const status of ["up", "passing", "ok"]) {
    assert.equal(fromBetterStack({ data: { status, check_name: "x" } }).firing, false, status);
  }
});

test("generic reads conventional keys", () => {
  const e = fromGeneric({
    source: "cloudflare",
    alert: { service: "edge", status: "firing", severity: "critical", summary: "Origin down", link: "https://x" },
  });
  assert.equal(e.firing, true);
  assert.equal(e.service, "edge");
  assert.equal(e.key, "cloudflare:edge");
  assert.equal(e.link, "https://x");
});

test("generic treats ok/resolved as not firing", () => {
  for (const status of ["ok", "resolved", "recovered", "up"]) {
    assert.equal(fromGeneric({ alert: { status } }).firing, false, status);
  }
});

test("normalize dispatches on source and falls back safely", () => {
  assert.equal(normalize("datadog", { monitor_id: 1, alert_type: "Triggered" }).source, "datadog");
  assert.equal(normalize("BetterStack", { data: { status: "down" } }).source, "betterstack");
  // Unknown source must not throw.
  const unknown = normalize("something-new", { alert: { service: "s", status: "firing" } });
  assert.equal(unknown.source, "something-new");
  // Empty payload must not throw either.
  assert.ok(normalize("datadog", {}).key);
});

test("a malformed payload never throws", () => {
  for (const bad of [null, undefined, {}, { data: null }, { alert_type: 5 }]) {
    assert.doesNotThrow(() => normalize("datadog", bad));
    assert.doesNotThrow(() => normalize("betterstack", bad));
    assert.doesNotThrow(() => normalize("generic", bad));
  }
});