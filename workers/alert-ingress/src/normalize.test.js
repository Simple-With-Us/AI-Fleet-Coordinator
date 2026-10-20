// Normalizer tests.  Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { normalize, fromDatadog, fromBetterStack, fromGeneric } from "./normalize.js";
import { normalizeSeverity, decide, initialState } from "./state.js";

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
test("datadog error is an active alert, not a recovery", () => {
  assert.equal(fromDatadog({ alert_type: "Error", monitor_id: 1 }).firing, true);
});

test("the datadog transition wins over alert_type, and reopened is firing", () => {
  // A monitor that recovered and fires again arrives as "Reopened" -- firing,
  // not recovered, even though alert_type still reads "triggered".
  const reopened = fromDatadog({
    alert_type: "Triggered",
    alert_transition_type: "Reopened",
    monitor_id: 5,
  });
  assert.equal(reopened.firing, true);

  const recovered = fromDatadog({
    alert_type: "Warning",
    alert_transition_type: "Recovered",
    monitor_id: 5,
  });
  assert.equal(recovered.firing, false, "a recovered transition must not read as firing on alert_type");
});

test("generic never reads a non-recovery word as recovered", () => {
  for (const status of ["broken", "disrupted", "ookup", "clearance"]) {
    assert.equal(fromGeneric({ alert: { status } }).firing, true, status);
  }
});

test("a firing Datadog alert keeps its severity", () => {
  // Dropping severity here pinned every alert to INFO, so an erroring monitor
  // could never escalate and was mislabelled in the Zulip announcement.
  assert.equal(fromDatadog({ alert_type: "Error", monitor_id: 1 }).severity, "error");
  assert.equal(fromDatadog({ alert_type: "Warning", monitor_id: 1 }).severity, "warning");
  assert.equal(
    normalizeSeverity(fromDatadog({ alert_type: "Error", monitor_id: 1 }).severity),
    "SEVERE",
    "a Datadog error is SEVERE, not the INFO fallback",
  );
  assert.equal(
    normalizeSeverity(fromDatadog({ alert_type: "Warning", monitor_id: 1 }).severity),
    "MODERATE",
  );
  // A recovered event stays quiet.
  assert.equal(fromDatadog({ alert_type: "Recovered", monitor_id: 1 }).severity, "INFO");
});

test("a status-less generic payload does not resolve an open incident", () => {
  // No recovery signal is unknown, not a recovery: this must not let decide()
  // discard or resolve a pending or alerting incident.
  const bare = fromGeneric({ alert: { id: "a1", summary: "no status field here" } });
  assert.equal(bare.firing, true);
  // An explicit false is still honoured.
  assert.equal(fromGeneric({ alert: { id: "a1", firing: false } }).firing, false);
});

test("anchoring the recovery words kept the real inflections", () => {
  // The anchored form must not cost the inflections the unanchored one
  // accepted -- an unrecognized recovery leaves the incident firing forever.
  for (const status of ["okay", "recovers", "recovery", "clears", "resolves", "resolving", "clearing"]) {
    assert.equal(fromGeneric({ alert: { status } }).firing, false, status);
  }
});

test("a generic route name binds the state-key namespace, not the payload source", () => {
  const e = normalize("uptime-robot", { source: "datadog", alert: { id: "real-id", status: "firing" } });
  assert.equal(e.source, "uptime-robot");
  assert.equal(e.key, "uptime-robot:real-id");
});

test("Better Stack's documented JSON:API incident body is accepted and read", () => {
  // The default uptime incident body nests the fields in data.attributes and
  // the monitor identity in data.relationships.monitor.data.  Rejecting it as
  // an oversized text field 400'd every correctly authenticated delivery.
  const incident = {
    data: {
      type: "incident",
      id: "4242",
      attributes: {
        id: 4242,
        status: "triggered",
        title: "congress.trade is DOWN",
        url: "https://uptime.betterstack.com/team/1/monitors/99887766",
        started_at: "2026-10-20T20:00:00Z",
      },
      relationships: { monitor: { data: { type: "monitor", id: "99887766" } } },
    },
  };
  const e = normalize("betterstack", incident);
  assert.equal(e.firing, true, "a triggered incident is firing");
  assert.equal(e.key, "betterstack:99887766", "the monitor id is the stable key");
  assert.equal(e.link, "https://uptime.betterstack.com/team/1/monitors/99887766");
  const resolved = {
    ...incident,
    data: { ...incident.data, attributes: { ...incident.data.attributes, status: "resolved" } },
  };
  assert.equal(normalize("betterstack", resolved).firing, false, "a resolved incident is not firing");
});

test("Better Stack keeps reading the flat custom-template body", () => {
  for (const status of ["down", "failing", "failed", "triggered"])
    assert.equal(fromBetterStack({ data: { check_name: "x", status } }).firing, true, status);
  for (const status of ["up", "passing", "ok", "resolved"])
    assert.equal(fromBetterStack({ data: { check_name: "x", status } }).firing, false, status);
});

test("a default Better Stack incident keys on the monitor, not the incident", () => {
  // The documented default template carries no relationships.monitor, so the
  // per-incident data.id must not become the key -- every outage would mint a
  // fresh alert that never resolves the last one.
  const monitorId = "99887766554433221100aabb";
  const url = `https://uptime.betterstack.com/team/1/monitors/${monitorId}`;
  const outage = normalize("betterstack", {
    data: { type: "incident", id: "77001", attributes: { status: "triggered", url } },
  });
  assert.equal(outage.key, `betterstack:${monitorId}`);
  const recovery = normalize("betterstack", {
    data: { type: "incident", id: "77002", attributes: { status: "resolved", url } },
  });
  assert.equal(recovery.key, outage.key, "the recovery must address the outage's key");

  // The state machine then closes that incident rather than opening a new one.
  const open = decide(initialState(), outage, 0);
  assert.equal(open.state.phase, "pending");
  assert.equal(decide(open.state, recovery, 1000).action, "discard");
  assert.equal(decide({ ...open.state, phase: "alerting" }, recovery, 2000).action, "resolve");
});

test("a Better Stack payload with no monitor identity uses its stable name", () => {
  const e = normalize("betterstack", {
    data: { type: "incident", id: "77001", attributes: { status: "triggered" } },
  });
  assert.equal(e.key, "betterstack:betterstack check");
});

test("default Better Stack monitored URLs stay stable across incident IDs", () => {
  for (const url of ["https://example.com/health", "https://uptime.betterstack.com/team/1/monitors/99887766"]) {
    const event = (id) => normalize("betterstack", {data: {id, attributes: {url, resolved_at: null}}});
    assert.equal(event("one").key, event("two").key);
    assert.notEqual(event("one").key, "betterstack:one");
  }
});

test("Datadog triggered notifications preserve explicit monitor priority", () => {
  const event = fromDatadog({monitor_id: 1, alert_type: "Triggered", priority: "P1"});
  assert.equal(event.firing, true);
  assert.equal(normalizeSeverity(event.severity), "SEVERE");
});
