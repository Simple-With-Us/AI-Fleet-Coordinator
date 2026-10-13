import test from "node:test";
import assert from "node:assert/strict";
import {
  relTime,
  formatResetCopy,
  remainingPercentShort,
  windowDurationMs,
  elapsedWindowFraction,
  percentPillTone,
  formatResetTimeLeft,
  barColorState,
  shouldShowExtraWindow,
  sortQuotaGroups,
  aggregateQuotas,
  usedPercentFromPrimary,
  staleReportedLabel,
  findWindow,
  windowBarLabel,
  platformDisplayName,
  extraWindowRowLabel,
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

test("remainingPercentShort is a plain percent without Remaining", () => {
  assert.equal(remainingPercentShort({ remainingUnknown: true }), "—");
  assert.equal(remainingPercentShort({ remainingPercent: null, remainingUnknown: false }), "—");
  assert.equal(remainingPercentShort({ remainingPercent: 42 }), "42%");
  assert.equal(remainingPercentShort({ remainingPercent: 0, isExhausted: true }), "0%");
});

test("percentPillTone maps quota state to pill tint", () => {
  assert.equal(percentPillTone({ remainingPercent: 80, freshness: "fresh" }), "ok");
  assert.equal(percentPillTone({ remainingPercent: 15, freshness: "fresh" }), "low");
  assert.equal(percentPillTone({ remainingPercent: 0, isExhausted: true }), "exh");
});

test("formatResetTimeLeft omits Resets In prefix", () => {
  const future = "2026-10-14T01:30:00.000Z";
  const copy = formatResetTimeLeft(future, T0);
  assert.ok(!copy.startsWith("Resets In"));
  assert.match(copy, /1d 13h/);
});

test("elapsedWindowFraction derives elapsed position from resetAt and window length", () => {
  const resetAt = "2026-10-12T17:00:00.000Z";
  const now = Date.parse("2026-10-12T14:30:00.000Z");
  const frac = elapsedWindowFraction(resetAt, "5h", "five_hour", now);
  assert.equal(frac, 0.5);
  assert.equal(elapsedWindowFraction(resetAt, "5h", "five_hour", Date.parse(resetAt)), null);
  assert.equal(elapsedWindowFraction(null, "5h", null, now), null);
  assert.equal(windowDurationMs("7d"), 7 * 86400_000);
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

test("windowBarLabel uses hyphenated period names", () => {
  assert.equal(windowBarLabel("5h"), "5-hour");
  assert.equal(windowBarLabel("7d"), "7-day");
  assert.equal(windowBarLabel("1m"), "1-month");
  assert.equal(windowBarLabel("30d"), "30-day");
});

test("platformDisplayName shows platform only and Antigravity variants", () => {
  assert.equal(platformDisplayName("claude", "Claude Opus 4.5"), "Claude");
  assert.equal(platformDisplayName("google-antigravity", "Antigravity · Gemini"), "Antigravity · Gemini");
  assert.equal(platformDisplayName("google-antigravity-third-party", null), "Antigravity · Third-Party");
  assert.equal(platformDisplayName("openai", "gpt-5-codex"), "Codex");
});

test("extraWindowRowLabel prefers period keys over display names", () => {
  assert.equal(extraWindowRowLabel({ windowKey: "5h", displayName: "5 Hour" }), "5-hour");
});
