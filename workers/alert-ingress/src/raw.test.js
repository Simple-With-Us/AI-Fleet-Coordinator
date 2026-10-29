// The raw copy for #Alerts-Raw:  the vendor's own title and text, then the
// whole delivery in a spoiler, with mentions and credentials made harmless.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { renderRaw } from "./raw.js";

test("a Datadog delivery shows its title and full Markdown body, then the payload", () => {
  const out = renderRaw("datadog", {
    alert_title: "[Triggered] CPU usage is high for host Mac.Jays.Services",
    event_msg: "%%%\n## What's happening\nCPU above 90% for 10 minutes.\n%%%",
    event_url: "https://us5.datadoghq.com/monitors/22024797",
    monitor_id: "22024797",
  });
  assert.match(out, /^\*\*\[Triggered\] CPU usage is high for host Mac\.Jays\.Services\*\*\n\n## What's happening\nCPU above 90% for 10 minutes\.\n\nhttps:\/\/us5\.datadoghq\.com\/monitors\/22024797\n/);
  assert.doesNotMatch(out, /%%%\n##/);
  assert.match(out, /````spoiler Original datadog payload\n```json\n\{/);
  assert.match(out, /"monitor_id": "22024797"/);
  assert.match(out, /\n```\n````$/);
});

test("Better Stack and PagerDuty show their incident fields", () => {
  const bs = renderRaw("betterstack", { data: { id: "1", type: "incident", attributes: {
    name: "usage-monitor", cause: "Status 503", url: "https://usage.jays.services",
    started_at: "2026-10-27T00:46:08Z", resolved_at: null,
  } } });
  assert.match(bs, /^\*\*usage-monitor\*\*\n\nCause:  Status 503\nURL:  https:\/\/usage\.jays\.services\nStarted:  2026-10-27T00:46:08Z\n/);
  assert.doesNotMatch(bs, /Resolved:/);
  const pd = renderRaw("pagerduty", { event: { event_type: "incident.triggered", agent: { summary: "Owner" }, data: {
    number: 578, title: "Error spike", status: "triggered", urgency: "high",
    service: { summary: "jays.services" }, assignees: [{ summary: "Owner" }], html_url: "https://pd.example/i/1",
  } } });
  assert.match(pd, /^\*\*#578 Error spike\*\*\n\nEvent:  incident\.triggered\nStatus:  triggered\nUrgency:  high\nService:  jays\.services\nAssigned:  Owner\nBy:  Owner\n\nhttps:\/\/pd\.example\/i\/1\n/);
});

test("a mention in vendor text cannot ping anyone", () => {
  const out = renderRaw("datadog", { alert_title: "Hey @**all** and @_**Jay**", event_msg: "cc @**Jay Wedgeworth**" });
  assert.doesNotMatch(out.split("````spoiler")[0], /@\*\*|@_\*\*/);
  assert.match(out, /@​\*\*all\*\*/);
});

test("a credential shape is redacted everywhere in the raw copy", () => {
  const token = "ghp_" + "a".repeat(36);
  const out = renderRaw("datadog", { alert_title: `leak ${token}`, event_msg: token, scope: token });
  assert.doesNotMatch(out, /ghp_/);
  assert.match(out, /\[redacted\]/);
});

test("a payload cannot close the fences, and the copy fits a Zulip message", () => {
  const out = renderRaw("generic", { text: "``````` closing attempt", big: "x".repeat(20000) });
  assert.match(out, /^\*\*generic alert\*\*\n\n(`{9})spoiler Original generic payload\n(`{8})json\n/);
  assert.ok(out.length <= 10000, String(out.length));
  assert.match(out, /\.\.\. \(truncated\)/);
});

test("a token right after a newline inside a payload string is still redacted", () => {
  const token = "ghp_" + "b".repeat(36);
  const out = renderRaw("generic", { note: `first line\n${token}\n`, [`key ${token}`]: "v", list: [`\t${token}`] });
  assert.doesNotMatch(out, /ghp_/);
});

test("group mentions and a link cannot ping anyone either", () => {
  const out = renderRaw("datadog", {
    alert_title: "for @*oncall* and @_*sre*",
    event_url: "https://example.test/@**all**",
  });
  const head = out.split("`spoiler")[0];
  assert.doesNotMatch(head, /@_?\*/);
});

test("a flood of backticks cannot make the fences outgrow a Zulip message", () => {
  const out = renderRaw("generic", { text: "`".repeat(5000), more: "`".repeat(3000) });
  assert.ok(out.length <= 10000, String(out.length));
  const fences = out.match(/^`+/gm) || [];
  assert.ok(Math.max(...fences.map((f) => f.length)) <= 21);
});
