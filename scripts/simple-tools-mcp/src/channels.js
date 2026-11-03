// The channels a hosted seat may use (spec 3.11, "Channels";  owner ruling Sat,
// Oct 10, 2026:  "I want bots to wake to any channel or DM").
//
// The allowed set is every PUBLIC stream the seat's own bot is subscribed to,
// read from Zulip (GET users/me/subscriptions), minus the optional
// CHANNELS_DENY stream ids.  A private stream is never allowed, even when the
// bot is subscribed to it.  Subscriptions, not GET /streams, because they are
// the streams the bot can actually see and the ones the fleet listener wakes
// on:  a mention in a public stream the bot is not in never reaches it anyway.
//
// Everything is enforced by stream id.  A name is only a way in:  a tool's
// `channel` argument resolves (exact, case-insensitive) against the CURRENT
// names of the fetched set, and every narrow and post after that uses the id.
// A renamed channel keeps its id, so it stays allowed under its new name and
// its old name stops resolving once the cache refreshes.
//
// Channel names are Zulip content:  untrusted data wherever they are shown.
//
// No `cloudflare:` imports, so plain `node --test` loads it.

import { clean } from "./contract.js";

export const CHANNELS_TTL_MS = 60 * 1000;
// A name or id that misses re-reads the set once, when it is at least this old,
// so a channel the bot joined (or renamed) a moment ago works without waiting
// out the TTL, and a stream of misses cannot turn into a stream of requests.
export const CHANNELS_MISS_REFRESH_MS = 10 * 1000;
// How many allowed names a channel_not_allowed error lists.
export const CHANNEL_REFUSAL_NAMES = 10;
export const CHANNEL_REFUSAL_NAME_LIMIT = 60;

// Allowed sets per seat, shared by the tools and the wake path of one isolate.
export const CHANNEL_CACHE = new Map();

/** The allowed channels of one seat at one moment:  ids enforced, names resolved. */
export class ChannelSet {
  /**
   * @param {object[]} subscriptions  rows of users/me/subscriptions
   * @param {number[]} deny           CHANNELS_DENY stream ids
   */
  constructor(subscriptions, deny = []) {
    const denied = new Set(deny);
    this.byId = new Map();
    const byName = new Map();
    const clashes = new Set();
    for (const s of Array.isArray(subscriptions) ? subscriptions : []) {
      if (!s || typeof s !== "object") continue;
      // Strictly false:  a row without the field counts as private.
      if (s.invite_only !== false) continue;
      if (s.is_archived === true) continue;
      if (!Number.isSafeInteger(s.stream_id) || s.stream_id <= 0) continue;
      if (typeof s.name !== "string" || s.name === "") continue;
      if (denied.has(s.stream_id)) continue;
      this.byId.set(s.stream_id, s.name);
      const key = s.name.toLowerCase();
      if (byName.has(key)) clashes.add(key);
      byName.set(key, s.stream_id);
    }
    // Zulip names are unique case-insensitively;  should two ever clash, the
    // name resolves to neither (the ids stay allowed).
    for (const key of clashes) byName.delete(key);
    this.byName = byName;
  }

  get size() {
    return this.byId.size;
  }

  /** Is this stream id allowed? */
  has(streamId) {
    return Number.isSafeInteger(streamId) && this.byId.has(streamId);
  }

  /** The stream id for a channel name (no leading #), or null. */
  idOf(name) {
    if (typeof name !== "string") return null;
    return this.byName.get(name.toLowerCase()) ?? null;
  }

  /** At most `limit` allowed names, sorted, each cleaned, kept to one line and clipped (untrusted). */
  names(limit = CHANNEL_REFUSAL_NAMES) {
    return [...this.byId.values()]
      .sort((a, b) => a.localeCompare(b))
      .slice(0, limit)
      .map((n) => clean(n, CHANNEL_REFUSAL_NAME_LIMIT)[0].replace(/[\n\r\t\u2028\u2029\x85]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`));
  }
}

/**
 * The seat's allowed set, from the cache when it is younger than `maxAgeMs`,
 * else from Zulip.  A failed fetch throws and is never cached.
 */
export async function loadChannelSet({ seat, zulip, config, cache = CHANNEL_CACHE, now = Date.now, maxAgeMs = CHANNELS_TTL_MS }) {
  const cached = cache.get(seat);
  if (cached && now() - cached.at < maxAgeMs) return cached.set;
  const result = await zulip.get("users/me/subscriptions");
  const set = new ChannelSet(result.subscriptions ?? [], config.channelsDeny ?? []);
  cache.set(seat, { at: now(), set });
  return set;
}

/**
 * Look something up in the seat's set, re-reading it once on a miss when the
 * cached copy is at least CHANNELS_MISS_REFRESH_MS old.  `pick(set)` returns
 * the hit or null.  Returns {set, hit}.
 */
export async function lookupChannel(options, pick) {
  let set = await loadChannelSet(options);
  let hit = pick(set);
  if (hit === null || hit === false) {
    set = await loadChannelSet({ ...options, maxAgeMs: CHANNELS_MISS_REFRESH_MS });
    hit = pick(set);
  }
  return { set, hit };
}
