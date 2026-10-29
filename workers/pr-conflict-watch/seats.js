// Seat mentions for pr-conflict-watch.
//
// A PR's head branch carries the seat that owns the work: the prefix before
// the first "/" is the seat slug ("gb-deployer/afc-123-fix-thing").  The
// conflict alert mentions that seat so the owner of the branch hears about
// the conflict directly, instead of everyone watching #Alerts for it.
//
// The mapping is a closed list on purpose.  An unknown prefix produces no
// mention at all rather than a guess: a wrong mention pages the wrong seat,
// and a missing mention still gets the alert to the channel.

// Seats with a fixed mention.  A null value is a deliberate "no mention":
// that seat does not want paged for merge conflicts.
const FIXED_SEAT_MENTIONS = new Map([
  ["mm", "@**MiniMax**"],
  ["minimax", "@**MiniMax**"],
  ["cursor", "@**Cursor**"],
  ["codex", "@**Codex**"],
  ["ag", "@**Antigravity**"],
  ["claude", "@**Claude**"],
  ["grokbot", null],
]);

// The fleet's generic branch prefixes: "gb-<role>" and "bf-<role>", where the
// role becomes the seat's display name ("bf-builder" -> "BF-Builder").
const GENERIC_SEAT_PREFIX = /^(gb|bf)-([a-z]+)$/;

const capitalize = (word) => word.charAt(0).toUpperCase() + word.slice(1);

/**
 * The branch prefix of a PR: the text before the first "/".
 * A branch with no "/" carries no seat, so it has no prefix.
 *
 * @param {string} ref
 * @returns {string|null}
 */
export function seatPrefixFor(ref) {
  if (typeof ref !== "string") return null;
  const slash = ref.indexOf("/");
  if (slash <= 0) return null;
  return ref.slice(0, slash).toLowerCase();
}

/**
 * The Zulip mention for the seat that owns this PR, or null when the seat is
 * unknown or does not want to be mentioned.
 *
 * @param {{head?: {ref?: string}}} pr GitHub pull request payload.
 * @returns {string|null} e.g. "@**GB-Deployer**", or null.
 */
export function seatMentionFor(pr) {
  const prefix = seatPrefixFor(pr?.head?.ref);
  if (!prefix) return null;

  if (FIXED_SEAT_MENTIONS.has(prefix)) return FIXED_SEAT_MENTIONS.get(prefix);

  const match = GENERIC_SEAT_PREFIX.exec(prefix);
  if (!match) return null;

  const fleet = match[1].toUpperCase(); // GB or BF
  const role = capitalize(match[2]);
  return `@**${fleet}-${role}**`;
}
