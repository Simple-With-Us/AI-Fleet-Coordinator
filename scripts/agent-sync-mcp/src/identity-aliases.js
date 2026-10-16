// The seat-name aliases the stdio server resolves (scripts/agent_sync/identity.py, canonical_seat), for the Worker,
// which cannot import that module or read fleet-apps.json at run time.  Two parts, the same rule as stdio:
//   - every spelling of GROK followed by BUILD is GROK (GROK_ALIAS_PATTERN, the GROK row's aliasPattern in
//     fleet-apps.json);
//   - the published aliases in SEAT_NAME_ALIASES.
// test/identity-aliases.test.mjs fails when this file differs from identity.py or from fleet-apps.json, so a change
// there must be copied here.  No `cloudflare:` imports, so node can test it.

export const GROK_ALIAS_PATTERN = "^grok[^a-z0-9]*build[^a-z0-9]*$";
export const GROK_SEAT = "GROK";
export const SEAT_NAME_ALIASES = Object.freeze({
  INSTINCT: "WISE",
  "WISE-INSTINCT": "WISE",
  "ECHO-INSTINCT": "WISE",
  "MUSE-ASSIST": "MA",
  ROB: "MA",
  "ROB-MUSE": "MA",
});

const GROK_ALIAS = new RegExp(GROK_ALIAS_PATTERN, "i");

/** The seat a name stands for, upper-cased:  a GROK-BUILD spelling is GROK, a published alias is its seat. */
export function canonicalSeatName(raw) {
  const text = String(raw ?? "").trim();
  if (GROK_ALIAS.test(text)) return GROK_SEAT;
  const upper = text.toUpperCase();
  return SEAT_NAME_ALIASES[upper] ?? upper;
}
