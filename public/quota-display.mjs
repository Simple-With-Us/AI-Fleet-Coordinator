/**
 * Agent quota panel helpers for home.jays.services.
 * Consumed by public/index.html and tests/quota-display.test.mjs.
 */

const PRIMARY_WINDOW_KEYS = ["5h", "7d"];

/**
 * Countdown to a future instant (no prefix).  Past resets return empty — use
 * formatResetCopy() for user-facing reset labels.
 */
export function relTime(iso, nowMs = Date.now()) {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - nowMs;
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  const m = Math.round(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m";
  const d = Math.floor(h / 24);
  return d + "d " + (h % 24) + "h";
}

/** Title Case reset line for one quota window. */
export function formatResetCopy(resetAt, nowMs = Date.now()) {
  if (!resetAt) return "—";
  const ms = new Date(resetAt).getTime() - nowMs;
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "Refreshing Now";
  const tail = relTime(resetAt, nowMs);
  return tail === "—" ? "—" : "Resets In " + tail;
}

export function windowBarShortLabel(windowKey) {
  if (windowKey === "5h") return "5h";
  if (windowKey === "7d") return "Weekly";
  return null;
}

export function remainingPercentLabel(primary) {
  if (!primary) return "Unknown";
  if (primary.remainingUnknown || primary.remainingPercent == null) return "Unknown";
  if (primary.isExhausted || primary.remainingPercent === 0) return "0% Exhausted";
  return Math.round(primary.remainingPercent) + "% Remaining";
}

/**
 * Bar colour state for CodeCaps-style quota bars.
 * stale freshness -> grey; exhausted -> red; <=20% -> amber; else teal.
 */
export function barColorState(primary) {
  if (!primary) return "unknown";
  if (primary.freshness === "stale" || primary.freshness === "old") return "stale";
  if (primary.isExhausted || (primary.remainingPercent === 0 && !primary.remainingUnknown)) {
    return "exhausted";
  }
  if (primary.remainingUnknown || primary.remainingPercent == null) return "unknown";
  if (primary.remainingPercent <= 20) return "amber";
  return "teal";
}

export function shouldShowExtraWindow(win) {
  if (!win || !win.primary) return false;
  const key = win.windowKey;
  if (key === "5h" || key === "7d") return false;
  const p = win.primary;
  if (p.isExhausted || (p.remainingPercent === 0 && !p.remainingUnknown)) return true;
  if (typeof p.remainingPercent === "number" && p.remainingPercent <= 20) return true;
  return false;
}

export function findWindow(group, windowKey) {
  if (!group || !Array.isArray(group.windows)) return null;
  return group.windows.find((w) => w.windowKey === windowKey) || null;
}

function minKnownRemaining(group, keys) {
  let min = null;
  for (const key of keys) {
    const w = findWindow(group, key);
    const p = w && w.primary;
    if (!p || p.remainingUnknown || typeof p.remainingPercent !== "number") continue;
    if (min == null || p.remainingPercent < min) min = p.remainingPercent;
  }
  return min;
}

/** Sort provider groups: lowest remaining across 5h/7d first; unknown last. */
export function sortQuotaGroups(quotaGroups) {
  return [...(quotaGroups || [])].sort((a, b) => {
    const ar = minKnownRemaining(a, PRIMARY_WINDOW_KEYS);
    const br = minKnownRemaining(b, PRIMARY_WINDOW_KEYS);
    const aSort = ar == null ? 101 : ar;
    const bSort = br == null ? 101 : br;
    return aSort - bSort;
  });
}

export function staleReportedLabel(primary, relTimeAgoFn) {
  if (!primary || primary.freshness !== "stale" || !primary.reportedAt) return null;
  const ago = relTimeAgoFn(primary.reportedAt);
  if (!ago || ago === "—") return null;
  if (ago === "just now") return "Last Reported Just Now";
  return "Last Reported " + ago + " Ago";
}

/** Legacy aggregation when quotaGroups is absent (windows[] only). */
export function aggregateQuotas(windows) {
  const byProvider = new Map();
  for (const w of windows || []) {
    const p = w.provider || w.sourceApp || "unknown";
    if (!byProvider.has(p)) byProvider.set(p, { provider: p, models: [], exhausted: 0 });
    const g = byProvider.get(p);
    g.models.push(w);
    if (w.isExhausted || (w.remainingPercent === 0 && !w.remainingUnknown)) g.exhausted += 1;
  }
  for (const g of byProvider.values()) {
    const knowns = g.models.filter((m) => !m.remainingUnknown && typeof m.remainingPercent === "number");
    g.remainingPercent = knowns.length ? Math.min(...knowns.map((m) => m.remainingPercent)) : null;
    const resets = g.models.map((m) => m.resetAt).filter(Boolean).map((s) => new Date(s).getTime());
    g.resetAt = resets.length ? new Date(Math.min(...resets)).toISOString() : null;
  }
  return [...byProvider.values()].sort((a, b) => {
    const ar = a.remainingPercent == null ? 101 : a.remainingPercent;
    const br = b.remainingPercent == null ? 101 : b.remainingPercent;
    return ar - br;
  });
}

export function usedPercentFromPrimary(primary) {
  if (!primary || primary.remainingUnknown || primary.remainingPercent == null) return null;
  const rem = Math.max(0, Math.min(100, primary.remainingPercent));
  return 100 - rem;
}

export function primaryWindowKeys() {
  return PRIMARY_WINDOW_KEYS;
}

export function olderSourcesForGroup(group) {
  if (!group || !Array.isArray(group.windows)) return [];
  const out = [];
  for (const win of group.windows) {
    if (!win.sources || !win.sources.length) continue;
    for (const src of win.sources) {
      if (src.freshness === "old") {
        out.push({ window: win, source: src });
      }
    }
  }
  return out;
}
