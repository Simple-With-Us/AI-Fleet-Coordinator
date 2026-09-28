#!/usr/bin/env node
// Weekly "fleet-mode compliance digest": posts a #agent-sync summary of
// frontier-vs-non-frontier Claude-family model usage and Sentry sub-agent
// tier mix across the fleet, trailing 7 days.
//
// Fleet mode (/Users/jay/apps/FLEET-MODE.md) has NO hard numeric threshold
// ("must be <30% frontier") -- it is a qualitative standard.  This digest
// SURFACES the split with brief framing; it does not compute a pass/fail
// score against a number that does not exist.
//
// Data sources:
//   1. Usage Monitor `GET /api/agent-model-mix?days=7`
//      Bearer USAGE_READ_TOKEN.  May 404 / connection-refused if the sibling
//      PR (Usage-Monitor `claude/agent-model-mix-endpoint`) is not yet
//      merged/deployed -- handled gracefully as "unavailable this week".
//   2. Sentry `agent-sessions` project, events endpoint, dataset=ourlogs,
//      query `message:*subagent*` (matches `claude_code.subagent_completed`;
//      an exact `message:subagent_completed` query returns zero results).
//      Bearer SENTRY_AUTH_TOKEN_FULLSCOPE (plain SENTRY_AUTH_TOKEN 403s on
//      this endpoint -- confirmed against this org).
//
// Usage:
//   node scripts/weekly-fleet-mode-digest.mjs [--dry-run] [--fixture path.json]
//
//   --dry-run   Prints the composed Slack message to stdout and exits 0
//               WITHOUT posting.
//   --fixture   Loads a local JSON fixture instead of hitting the network,
//               for offline development/testing of the composition logic.
//               Shape: { umMix: <agent-model-mix response or null>,
//                        sentryRows: <events response `data` array or null> }
//
// Posting (no --dry-run): shells out to the canonical Mac-local relay
// helper, per AGENT-SYNC.md -- never hand-rolls a Slack WebClient call and
// never touches SLACK_BOT_TOKEN directly:
//   AGENT_TAG=CLAUDE ~/apps/agent-sync-websocket.py --post "<message>"
//
// SECRETS: USAGE_READ_TOKEN and SENTRY_AUTH_TOKEN_FULLSCOPE are loaded
// value-blind from environment first, then ~/.secrets/global-api-keys
// (same pattern as Usage-Monitor's resolveCollectorToken).  Neither value is
// ever printed, logged, or included in the composed message.
//
// Install the LaunchAgent (every Monday 8:00 AM Central, matches system tz):
//   launchctl bootstrap gui/$(id -u) \
//     ~/Library/LaunchAgents/com.jays.weekly-fleet-mode-digest.plist
// Uninstall:
//   launchctl bootout gui/$(id -u)/com.jays.weekly-fleet-mode-digest

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const UM_BASE_URL =
  process.env.USAGE_MONITOR_BASE_URL || "https://usage.jays.services";
const UM_MIX_URL = `${UM_BASE_URL}/api/agent-model-mix?days=7`;

const SENTRY_ORG = "simple-with-us";
const SENTRY_PROJECT_ID = "4512139515592704";
const SENTRY_EVENTS_URL =
  `https://us.sentry.io/api/0/organizations/${SENTRY_ORG}/events/` +
  `?dataset=ourlogs&project=${SENTRY_PROJECT_ID}&field=model&field=count()` +
  `&query=${encodeURIComponent("message:*subagent*")}&statsPeriod=7d&sort=-count()`;

const REQUEST_TIMEOUT_MS = 20_000;

// Claude-family tier vocabulary, mirroring
// ~/.claude/hooks/subagent-economy-pretooluse.py's resolve_tier: bare tier
// name match first, then substring match, in this fixed order so a model
// string containing more than one tier word resolves deterministically.
const KNOWN_TIERS = ["haiku", "sonnet", "opus", "fable"];

function resolveTier(raw) {
  const m = (raw || "").trim().toLowerCase();
  if (!m) return null;
  if (KNOWN_TIERS.includes(m)) return m;
  for (const tier of KNOWN_TIERS) {
    if (m.includes(tier)) return tier;
  }
  return null;
}

// Frontier vs non-frontier per fleet-mode: opus is frontier-tier; haiku and
// sonnet are non-frontier (mechanical/default).  fable is Claude's historical
// frontier-equivalent tag (see AGENT-SYNC.md seat registry note) so it counts
// as frontier too.
const FRONTIER_TIERS = new Set(["opus", "fable"]);

function isClaudeFamily(row) {
  const sourceApp = (row.sourceApp || "").toLowerCase();
  const provider = (row.provider || "").toLowerCase();
  const model = (row.model || "").toLowerCase();
  return (
    sourceApp === "claude-code" ||
    provider === "anthropic" ||
    KNOWN_TIERS.some((tier) => model.includes(tier))
  );
}

function log(message) {
  console.log(`[weekly-fleet-mode-digest] ${message}`);
}

// ------------------------------------------------------------- secrets ---

/** Value-blind secret load: env var first, then ~/.secrets/global-api-keys.
 *  Never printed, logged, or returned in any user-facing string. */
function resolveSecret(envVarNames) {
  for (const name of envVarNames) {
    const val = process.env[name]?.trim();
    if (val) return val;
  }
  try {
    const secretsPath = join(homedir(), ".secrets", "global-api-keys");
    const content = readFileSync(secretsPath, "utf8");
    for (const name of envVarNames) {
      const match = new RegExp(
        `^(?:export\\s+)?${name}=["']?([^"'\\r\\n]+)["']?`,
        "m",
      ).exec(content);
      if (match && match[1]?.trim()) return match[1].trim();
    }
  } catch {
    // Missing/unreadable handoff file -- treated as "no secret available".
  }
  return null;
}

// --------------------------------------------------------------- fetch ---

async function fetchJson(url, headers) {
  let response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { accept: "application/json", ...headers },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    const cause = error && typeof error === "object" ? error.cause : null;
    const code =
      cause && typeof cause === "object"
        ? cause.code || cause.errno || cause.syscall
        : null;
    throw new Error(
      `fetch failed from ${hostOf(url)}${code ? ` (${code})` : ""}`,
    );
  }
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status} from ${hostOf(url)}`);
    error.status = response.status;
    throw error;
  }
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Non-JSON response from ${hostOf(url)}`);
  }
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

// ------------------------------------------------------- data fetchers ---

/** Returns { available: boolean, data, error? }. Never throws. */
async function fetchUsageMonitorMix() {
  const token = resolveSecret(["USAGE_READ_TOKEN"]);
  if (!token) {
    return { available: false, data: null, error: "USAGE_READ_TOKEN not found" };
  }
  try {
    const data = await fetchJson(UM_MIX_URL, { authorization: `Bearer ${token}` });
    return { available: true, data };
  } catch (error) {
    return { available: false, data: null, error: error.message };
  }
}

/** Returns { available: boolean, rows, error? }. Never throws. */
async function fetchSentrySubagentMix() {
  const token = resolveSecret(["SENTRY_AUTH_TOKEN_FULLSCOPE"]);
  if (!token) {
    return {
      available: false,
      rows: [],
      error: "SENTRY_AUTH_TOKEN_FULLSCOPE not found",
    };
  }
  try {
    const payload = await fetchJson(SENTRY_EVENTS_URL, {
      authorization: `Bearer ${token}`,
    });
    const rows = Array.isArray(payload?.data) ? payload.data : [];
    return { available: true, rows };
  } catch (error) {
    return { available: false, rows: [], error: error.message };
  }
}

// ------------------------------------------------------------ compute ---

/**
 * Computes frontier vs non-frontier token/cost share for the Claude family.
 * Groups by seat when `seatDataAvailable` is true, else falls back to a
 * single fleet-wide bucket grouped by sourceApp/model per the brief.
 */
function computeClaudeTierShare(umMix) {
  const rows = Array.isArray(umMix?.rows) ? umMix.rows : [];
  const seatDataAvailable = umMix?.seatDataAvailable === true;
  const claudeRows = rows.filter(isClaudeFamily);

  const otherPlatformTotals = new Map(); // model -> { tokens, costUsd }
  for (const row of rows) {
    if (isClaudeFamily(row)) continue;
    const model = row.model || "unknown";
    const cur = otherPlatformTotals.get(model) || { tokens: 0, costUsd: 0 };
    cur.tokens += Number(row.tokens) || 0;
    cur.costUsd += Number(row.costUsd) || 0;
    otherPlatformTotals.set(model, cur);
  }

  // groupKey: seat when available, else "fleet-wide" (one bucket, per brief:
  // "group by sourceApp/model instead of by seat in that case").
  const buckets = new Map(); // groupKey -> { frontierTokens, nonFrontierTokens, frontierCost, nonFrontierCost, unclassifiedModels: Set }
  for (const row of claudeRows) {
    const tier = resolveTier(row.model);
    const groupKey = seatDataAvailable ? row.seat || "unattributed" : "fleet-wide";
    const bucket =
      buckets.get(groupKey) ||
      {
        frontierTokens: 0,
        nonFrontierTokens: 0,
        frontierCost: 0,
        nonFrontierCost: 0,
        unclassifiedModels: new Set(),
      };
    const tokens = Number(row.tokens) || 0;
    const cost = Number(row.costUsd) || 0;
    if (tier && FRONTIER_TIERS.has(tier)) {
      bucket.frontierTokens += tokens;
      bucket.frontierCost += cost;
    } else if (tier) {
      bucket.nonFrontierTokens += tokens;
      bucket.nonFrontierCost += cost;
    } else {
      // Unknown/unclassifiable Claude-family model string -- counted as
      // non-frontier (conservative: don't inflate the frontier share on an
      // unrecognized name) but flagged so the message can note it.
      bucket.nonFrontierTokens += tokens;
      bucket.nonFrontierCost += cost;
      bucket.unclassifiedModels.add(row.model || "unknown");
    }
    buckets.set(groupKey, bucket);
  }

  return { seatDataAvailable, buckets, otherPlatformTotals, hasClaudeRows: claudeRows.length > 0 };
}

function computeSentryMix(rows) {
  let totalCount = 0;
  const tierCounts = new Map(); // tier|"other" -> count
  const modelCounts = new Map(); // raw model -> count
  for (const row of rows) {
    const count = Number(row["count()"]) || 0;
    totalCount += count;
    const model = row.model || "unknown";
    modelCounts.set(model, (modelCounts.get(model) || 0) + count);
    const tier = resolveTier(model) || "other";
    tierCounts.set(tier, (tierCounts.get(tier) || 0) + count);
  }
  return { totalCount, tierCounts, modelCounts };
}

// ------------------------------------------------------------- format ---

function pct(part, whole) {
  if (!whole) return null;
  return Math.round((part / whole) * 1000) / 10; // one decimal
}

function fmtTokens(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

/** Central Time window label, e.g. "week of Mon, Sep 22 - Sun, Sep 28 CT". */
function centralWeekLabel(now = new Date()) {
  const ctFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const end = now;
  const start = new Date(now.getTime() - 6 * 24 * 60 * 60 * 1000);
  return `week of ${ctFormatter.format(start)} - ${ctFormatter.format(end)} CT`;
}

function composeMessage({ umResult, sentryResult, now = new Date() }) {
  const windowLabel = centralWeekLabel(now);
  const lines = [];
  lines.push(`[CLAUDE]`);
  lines.push(`repo: ai-fleet-coordinator`);
  lines.push(``);
  lines.push(
    `Weekly fleet-mode compliance digest -- ${windowLabel}.  This is a surface of the ` +
      `frontier-vs-non-frontier split, not a pass/fail score (fleet-mode has no hard ` +
      `numeric threshold).`,
  );

  // --- Usage Monitor half ---
  if (!umResult.available) {
    lines.push(
      `Usage Monitor model-mix data unavailable this week (${umResult.error || "unknown error"}); ` +
        `Claude-family frontier/non-frontier share could not be computed.`,
    );
  } else {
    const { seatDataAvailable, buckets, otherPlatformTotals, hasClaudeRows } =
      computeClaudeTierShare(umResult.data);
    if (!seatDataAvailable) {
      lines.push(
        `Per-seat attribution isn't live yet, so this summary is fleet-wide only (grouped by app/model).`,
      );
    }
    if (!hasClaudeRows) {
      lines.push(`No Claude-family usage rows in the trailing 7 days.`);
    } else {
      const parts = [];
      for (const [groupKey, b] of buckets) {
        const totalTokens = b.frontierTokens + b.nonFrontierTokens;
        const frontierPct = pct(b.frontierTokens, totalTokens);
        const totalCost = b.frontierCost + b.nonFrontierCost;
        const costPct = pct(b.frontierCost, totalCost);
        const label = seatDataAvailable ? groupKey : "fleet";
        const unclassifiedNote = b.unclassifiedModels.size
          ? ` (${b.unclassifiedModels.size} unclassified model name${b.unclassifiedModels.size === 1 ? "" : "s"} counted non-frontier)`
          : "";
        parts.push(
          `${label}: ${frontierPct ?? "n/a"}% frontier tokens (${fmtTokens(b.frontierTokens)}/${fmtTokens(totalTokens)}), ${costPct ?? "n/a"}% frontier cost${unclassifiedNote}`,
        );
      }
      lines.push(`Claude-family frontier share -- ${parts.join("  |  ")}.`);
    }
    if (otherPlatformTotals.size) {
      const otherParts = [...otherPlatformTotals.entries()]
        .sort((a, b) => b[1].tokens - a[1].tokens)
        .slice(0, 5)
        .map(([model, t]) => `${model} ${fmtTokens(t.tokens)} tok/$${t.costUsd.toFixed(2)}`);
      lines.push(
        `Other platforms (tier classification applies to the Claude family only today) -- ${otherParts.join(", ")}.`,
      );
    }
  }

  // --- Sentry half ---
  if (!sentryResult.available) {
    lines.push(
      `Sub-agent data unavailable this week (${sentryResult.error || "unknown error"}; see notes).`,
    );
  } else {
    const { totalCount, tierCounts, modelCounts } = computeSentryMix(sentryResult.rows);
    if (totalCount === 0) {
      lines.push(`No sub-agent completions recorded in Sentry this week.`);
    } else {
      const tierParts = [...tierCounts.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([tier, count]) => `${tier} ${count}`);
      const topModels = [...modelCounts.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([model, count]) => `${model} (${count})`);
      lines.push(
        `Sub-agents (Sentry, trailing 7d): ${totalCount} completed -- by tier: ${tierParts.join(", ")}.  ` +
          `Top models: ${topModels.join(", ")}.`,
      );
    }
  }

  return lines.join("\n");
}

// ---------------------------------------------------------------- post ---

function postToSlack(text) {
  const result = spawnSync(
    join(homedir(), "apps", "agent-sync-websocket.py"),
    ["--post", text],
    {
      env: { ...process.env, AGENT_TAG: "CLAUDE" },
      encoding: "utf8",
      timeout: 30_000,
    },
  );
  if (result.error) {
    throw new Error(`agent-sync-websocket.py failed to launch: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(
      `agent-sync-websocket.py exited ${result.status}: ${(result.stderr || "").trim()}`,
    );
  }
  return result.stdout;
}

// ---------------------------------------------------------------- main ---

async function main() {
  const args = process.argv.slice(2);
  const dryRun = args.includes("--dry-run");
  const fixtureIdx = args.indexOf("--fixture");
  const fixturePath = fixtureIdx >= 0 ? args[fixtureIdx + 1] : null;

  let umResult;
  let sentryResult;

  if (fixturePath) {
    log(`loading fixture from ${fixturePath} (offline dev mode)`);
    const fixture = JSON.parse(readFileSync(fixturePath, "utf8"));
    umResult = fixture.umMix
      ? { available: true, data: fixture.umMix }
      : { available: false, data: null, error: "fixture: umMix omitted" };
    sentryResult = fixture.sentryRows
      ? { available: true, rows: fixture.sentryRows }
      : { available: false, rows: [], error: "fixture: sentryRows omitted" };
  } else {
    [umResult, sentryResult] = await Promise.all([
      fetchUsageMonitorMix(),
      fetchSentrySubagentMix(),
    ]);
    if (!umResult.available) log(`Usage Monitor mix unavailable: ${umResult.error}`);
    if (!sentryResult.available) log(`Sentry sub-agent data unavailable: ${sentryResult.error}`);
  }

  const message = composeMessage({ umResult, sentryResult });

  if (dryRun) {
    console.log(message);
    process.exit(0);
  }

  log("posting to #agent-sync via agent-sync-websocket.py");
  postToSlack(message);
  log("posted.");
}

main().catch((error) => {
  console.error(`[weekly-fleet-mode-digest] fatal: ${error.message}`);
  process.exit(1);
});
