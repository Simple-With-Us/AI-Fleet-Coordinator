// The declared Fleet-OPS secret set for the scoped-service-token runtime
// (Jay's approval pending as of 2026-10-23):  the 72 copies the population
// run creates, mirrored here so required mode FAILS VISIBLY when a declared
// secret is missing instead of silently disabling a configured capability.
// Absence from this list marks a capability intentionally absent (the SPIKE
// wake key is ephemeral; GB-ENGINEER's FleetLink token was never provisioned
// - see FLEETLINK_SEATS_DISABLED).  Copies are point-in-time:  they do NOT
// track source rotation;  re-running the copy script is a separate manual
// operation that overwrites destination values in place.

// Leaf module:  imports ONLY the one persona list (never delegated-identities.js,
// which itself imports infisical.js - that would be a cycle).  The name
// derivation below must stay identical to delegatedNames() there;  a test
// guards the drift.
import GB_TABLE from "../../agent_sync/mcp/gb-identities.json" with { type: "json" };

const GB_IDENTITIES = Object.freeze([...GB_TABLE.enabled]);
const gbFieldNames = (identity) => {
  const role = identity.slice(3).replaceAll("-", "_");
  return { zulipKey: `ZULIP_GB_${role}_API_KEY`, zulipEmail: `ZULIP_GB_${role}_EMAIL`,
    linearClientId: `LINEAR_GB_${role}_CLIENT_ID`, linearClientSecret: `LINEAR_GB_${role}_CLIENT_SECRET` };
};

const WAKE = Object.freeze(["/wake:WAKE_HMAC_KEY_JET", "/wake:WA_BRIDGE_HMAC_KEY_JET", "/wake:WA_OPERATOR_HMAC_KEY_JET"]);

const FLEETLINK_SEATS = Object.freeze(["JET", "ECHO", "WISE", "CURSOR", "CLAUDE", "GROK_WEB",
  "GB_COMPILER", "GB_DEPLOYER", "GB_DESIGNER", "GB_DIRECTOR", "GB_FIXER", "GB_HOUSEKEEPER",
  "GB_MONITOR", "GB_ORACLE", "GB_PLUMBER", "GB_PUBLISHER", "GB_TRADER"]);
const FLEETLINK = Object.freeze(["/fleetlink:FLEETLINK_AGENT_SECRET",
  ...FLEETLINK_SEATS.map((s) => `/fleetlink:FLEETLINK_TOKEN_${s}`)]);

const GB = Object.freeze(GB_IDENTITIES.flatMap((identity) => {
  const n = gbFieldNames(identity);
  return [`/zulip:${n.zulipEmail}`, `/zulip:${n.zulipKey}`, `/linear:${n.linearClientId}`, `/linear:${n.linearClientSecret}`];
}));

const RECALL = Object.freeze(["/tools:RECALL_API_TOKEN", "/tools:RECALL_ACCESS_CLIENT_ID", "/tools:RECALL_ACCESS_CLIENT_SECRET"]);

/** The declared set, "path:name" entries, 3 + 18 + 48 + 3 = 72. */
export const INFISICAL_EXPECT_DEFAULT = Object.freeze([...WAKE, ...FLEETLINK, ...GB, ...RECALL]);
