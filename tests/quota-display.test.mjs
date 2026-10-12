import test from "node:test";
import assert from "node:assert/strict";
import {
  relTime,
  formatResetCopy,
  remainingPercentLabel,
  barColorState,
  shouldShowExtraWindow,
  sortQuotaGroups,
  aggregateQuotas,
  usedPercentFromPrimary,
  staleReportedLabel,
  findWindow,
} from "../public/quota-display.mjs";

const T0 = Date.parse("2026-10-12T12:00:00.000Z");

test("formatResetCopy uses Refreshing Now for past reset, never in resetting", () => {
  const past = "2026-10-12T11:00:00.000Z";
  assert.equal(formatResetCopy(past, T0), "Refreshing Now");
  assert.equal(relTime(past, T0), "");
});

test("formatResetCopy prefixes future resets with Resets In", () => {
  const future = "2026-10-14T01:30:00.000Z";
  assert.match(formatResetCopy(future, T0), /^Resets In /);
  assert.match(formatResetCopy(future, T0), /1d 13h/);
});

test("remainingPercentLabel treats unknown as Unknown, not exhausted", () => {
  assert.equal(remainingPercentLabel({ remainingUnknown: true }), "Unknown");
  assert.equal(remainingPercentLabel({ remainingPercent: null, remainingUnknown: false }), "Unknown");
  assert.equal(remainingPercentLabel({ remainingPercent: 42 }), "42% Remaining");
  assert.equal(remainingPercentLabel({ remainingPercent: 0, isExhausted: true }), "0% Exhausted");
});

test("barColorState follows stale, exhausted, amber, and teal rules", () => {
  assert.equal(barColorState({ freshness: "stale", remainingPercent: 80 }), "stale");
  assert.equal(barColorState({ remainingPercent: 0, isExhausted: true }), "exhausted");
  assert.equal(barColorState({ remainingPercent: 15 }), "amber");
  assert.equal(barColorState({ remainingPercent: 50, freshness: "fresh" }), "teal");
  assert.equal(barColorState({ remainingUnknown: true }), "unknown");
});

test("shouldShowExtraWindow only for non-primary windows that are low or exhausted", () => {
  assert.equal(shouldShowExtraWindow({ windowKey: "5h", primary: { remainingPercent: 5 } }), false);
  assert.equal(shouldShowExtraWindow({ windowKey: "other:plan", primary: { remainingPercent: 5 } }), true);
  assert.equal(shouldShowExtraWindow({ windowKey: "other:plan", primary: { remainingPercent: 80 } }), false);
  assert.equal(shouldShowExtraWindow({ windowKey: "7d:opus", primary: { isExhausted: true } }), true);
});

test("sortQuotaGroups orders by lowest 5h/7d remaining", () => {
  const groups = [
    {
      provider: "openai",
      windows: [{ windowKey: "5h", primary: { remainingPercent: 60 } }],
    },
    {
      provider: "claude",
      windows: [
        { windowKey: "5h", primary: { remainingPercent: 10 } },
        { windowKey: "7d", primary: { remainingPercent: 90 } },
      ],
    },
  ];
  const sorted = sortQuotaGroups(groups);
  assert.equal(sorted[0].provider, "claude");
  assert.equal(sorted[1].provider, "openai");
});

test("aggregateQuotas legacy path keeps per-provider min remaining", () => {
  const rows = aggregateQuotas([
    { provider: "claude", remainingPercent: 40, resetAt: "2026-10-13T00:00:00.000Z" },
    { provider: "claude", remainingPercent: 70, resetAt: "2026-10-12T18:00:00.000Z" },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].remainingPercent, 40);
  assert.equal(rows[0].resetAt, "2026-10-12T18:00:00.000Z");
});

test("usedPercentFromPrimary derives used slice from remaining", () => {
  assert.equal(usedPercentFromPrimary({ remainingPercent: 25 }), 75);
  assert.equal(usedPercentFromPrimary({ remainingUnknown: true }), null);
});

test("staleReportedLabel formats Last Reported copy", () => {
  const relAgo = () => "3h";
  const label = staleReportedLabel(
    { freshness: "stale", reportedAt: "2026-10-12T09:00:00.000Z" },
    relAgo,
  );
  assert.equal(label, "Last Reported 3h Ago");
});

test("findWindow locates window by key", () => {
  const g = { windows: [{ windowKey: "5h" }, { windowKey: "7d" }] };
  assert.equal(findWindow(g, "7d").windowKey, "7d");
  assert.equal(findWindow(g, "missing"), null);
});
