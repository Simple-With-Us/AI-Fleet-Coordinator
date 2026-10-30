/**
 * Who a resource alert tags (owner, Oct 29 2026):  "any resource related thing should take the housekeepers and me
 * (other than high cpu usage which datadog says all the time)".
 *
 * The Datadog host monitors below report the server running short of something.  A new alert or an escalation on
 * one of them tags both Housekeepers and Jay in its #Alerts-Log line;  a recovery tags nobody.  The CPU monitor
 * (22024794) is left out on purpose.  The ids match Fleet-OPS scripts/datadog_free_tier_provision.py.
 */
const RESOURCE_MONITORS = new Set([
  "22024800", // memory
  "22024799", // network
  "22024795", // disk latency
  "22024797", // load
  "22024796", // forecast disk (muted while on the free tier)
]);

export const RESOURCE_MENTIONS =
  "@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**";

/** The mentions for an incident key, or "" when the incident is not a resource alert. */
export function resourceMentions(key) {
  const match = /^datadog:(\d+)(?::|$)/.exec(typeof key === "string" ? key : "");
  return match && RESOURCE_MONITORS.has(match[1]) ? RESOURCE_MENTIONS : "";
}
