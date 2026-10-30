/**
 * Owner rule (Oct 29 2026):  resource alerts tag the Housekeepers and Jay, except CPU.
 * OPS-1008's later Oct 29 request supersedes that exclusion for sustained severe saturation.
 * The confirmed CPU monitor is included below.
 *
 * The Datadog host monitors below report the server running short of something.
 * A new alert or escalation tags both Housekeepers and Jay in its #Alerts-Log line.
 * A recovery tags nobody.
 * CPU monitor 22024794 tags only when the state machine classifies it SEVERE;  warnings and recoveries stay quiet.
 * The ids match Fleet-OPS scripts/datadog_free_tier_provision.py.
 */
const RESOURCE_MONITORS = new Set([
  "22024800", // memory
  "22024799", // network
  "22024795", // disk latency
  "22024797", // load
  "22024794", // CPU (sustained severe saturation only)
  "22024796", // forecast disk (muted while on the free tier)
]);

export const RESOURCE_MENTIONS =
  "@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**";

/** True only for the severe CPU monitor; its Datadog threshold already requires sustained saturation. */
export function isSevereCpu(key, severity) {
  return severity === "SEVERE" && /^datadog:22024794(?::|$)/.test(typeof key === "string" ? key : "");
}

/** The mentions for an incident key, or "" when the incident is not a resource alert. */
export function resourceMentions(key, severity) {
  const match = /^datadog:(\d+)(?::|$)/.exec(typeof key === "string" ? key : "");
  if (!match || !RESOURCE_MONITORS.has(match[1])) return "";
  if (match[1] === "22024794" && !isSevereCpu(key, severity)) return "";
  return RESOURCE_MENTIONS;
}
