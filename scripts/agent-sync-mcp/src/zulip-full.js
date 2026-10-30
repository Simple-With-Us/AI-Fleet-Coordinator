// The rest of Zulip for hosted seats (AFC-330):  one message, edits of the bot's own messages,
// deletes as far as Zulip's realm permissions allow, edit history, topic moves, resolve and unresolve, reactions, read and star flags,
// search, channels, users, presence, status, user groups, custom emoji, uploads, scheduled
// messages, drafts and typing.  A port of scripts/agent_sync/zulip_ops.py and the stdio handlers
// in scripts/agent_sync/mcp/tools.py, with the hosted rules on top:
//   - the channel rule (src/channels.js) by stream id on every channel and message:  a public
//     channel the seat's bot is subscribed to, minus CHANNELS_DENY;  a direct message only when the
//     bot is in it;
//   - the secret scan over everything the caller wrote, before any request;
//   - raw @-mentions silenced and the [SEAT·session] tag on outbound message text;
//   - budgets and spacing in the seat's SeatGate:  `read` for reads, `write` for content writes,
//     `react` for light ones (reactions, flags, topic visibility, status, typing);
//   - idempotent creates (scheduled messages, drafts, uploads) and one audit row per call.
//
// The methods are installed on HostedTools.prototype (hosted-tools.js), so `this` is the seat's
// HostedTools.  No `cloudflare:` imports:  node tests run it against the fake Zulip.

import {
  BODY_LIMIT,
  NAME_LIMIT,
  MAX_CONTENT_LENGTH,
  RESOLVED_PREFIX,
  MAX_TOPIC_LENGTH,
  ToolError,
  ApiError,
  UsageError,
  mapException,
  okResult,
  clean,
  envelope,
  normalizeChannel,
  normalizeTopic,
  normalizeText,
  sessionTag,
  tagPrefix,
  composeBody,
  seatTagFor,
  formatTime,
} from "./contract.js";
import { fixOutbound } from "./textfmt.js";

export const FLAG_NARROW_MAX = 1000;
export const UPLOAD_MAX_BYTES = 1024 * 1024;
export const PRESENCE_ACTIVE_S = 140;
export const PRESENCE_IDLE_S = 15 * 60;
// Zulip's own text is left out (it can quote member-authored names);  zulip_ops.DELETE_REFUSED says the same.
export const DELETE_REFUSED = (id) =>
  `Zulip refused to delete message ${id}:  this bot has no permission to delete it, or it is past the realm's delete time limit.  The owner sets both in the realm's settings.`;

// Budget kind per tool:  anything not here is a read.
export const FULL_WRITE_KIND = Object.freeze({
  edit_message: "write", delete_message: "write", move_topic: "write", resolve_topic: "write", unresolve_topic: "write",
  unsubscribe: "write", upload: "write", scheduled_create: "write", scheduled_edit: "write", scheduled_delete: "write",
  draft_create: "write", draft_edit: "write", draft_delete: "write",
  unreact: "react", mark_read: "react", mark_unread: "react", star: "react", unstar: "react", topic_visibility: "react",
  set_status: "react", typing: "react",
});

const isInt = (v) => typeof v === "number" && Number.isSafeInteger(v);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const c = (value, limit = NAME_LIMIT) => (typeof value === "string" ? clean(value, limit)[0] : value);
const VISIBILITY = Object.freeze({ follow: 3, mute: 1, unmute: 0 });
const SEARCH_IS = Object.freeze({ unresolved: { operator: "is", operand: "resolved", negated: true } });

function userRow(user) {
  const row = {
    user_id: user.user_id ?? null,
    full_name: c(user.full_name ?? ""),
    email: c(user.email ?? ""),
    is_bot: typeof user.is_bot === "boolean" ? user.is_bot : null,
    is_active: user.is_active !== false,
    role: isInt(user.role) ? user.role : null,
  };
  if (user.is_bot && user.email) row.seat = c(seatTagFor(user));
  return row;
}

function topicCheck(value, field) {
  const topic = String(value).trim();
  if (!topic) throw new ToolError("invalid_argument", `argument '${field}':  topic must not be empty`);
  if ([...topic].length > MAX_TOPIC_LENGTH) throw new ToolError("invalid_argument", `argument '${field}':  topic is ${[...topic].length} characters; Zulip allows at most ${MAX_TOPIC_LENGTH}`);
  return topic;
}

function resolvedName(topic, resolve) {
  const mark = RESOLVED_PREFIX.trim();
  const marked = topic.startsWith(mark);
  if (resolve) {
    if (marked) throw new ToolError("invalid_argument", "that topic is already resolved");
    const next = RESOLVED_PREFIX + topic;
    if ([...next].length > MAX_TOPIC_LENGTH) throw new ToolError("invalid_argument", `the resolved topic would be ${[...next].length} characters; Zulip allows at most ${MAX_TOPIC_LENGTH}`);
    return next;
  }
  if (!marked) throw new ToolError("invalid_argument", "that topic is not resolved");
  const next = topic.slice(mark.length).trimStart();
  if (!next) throw new ToolError("invalid_argument", "that topic has no name once the check mark is removed");
  return next;
}

function decodeBase64(text) {
  try {
    const raw = atob(String(text).replace(/\s+/g, ""));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  } catch {
    throw new ToolError("invalid_argument", "content_base64 is not valid base64");
  }
}

export const ZULIP_FULL_METHODS = {
  // ---------------------------------------------------------------- shared helpers

  listResult(tool, args, summary, items, structured) {
    return okResult(envelope(tool, args, summary, items), structured);
  },

  /** A message this seat may see:  in an allowed channel, or a DM the bot is in.  Needs ensureSeat(). */
  async visibleMessage(messageId) {
    const message = (await this.zulip.get(`messages/${messageId}`, { apply_markdown: false })).message ?? {};
    if (message.type === "private") {
      const inIt = Array.isArray(message.display_recipient) && message.display_recipient.some((p) => p?.id === this.me?.user_id);
      if (inIt) return message;
    } else if (await this.allowedStream(message)) {
      return message;
    }
    throw new ToolError("channel_not_allowed", "That message is not in a channel this seat may use.");
  },

  async ownMessage(messageId) {
    const message = await this.visibleMessage(messageId);
    const own = isInt(message.sender_id) && isInt(this.me?.user_id)
      ? message.sender_id === this.me.user_id
      : String(message.sender_email ?? "").toLowerCase() === this.email;
    if (!own) throw new ToolError("invalid_argument", `message ${messageId} was not sent by this bot; only its own messages can be edited`);
    return message;
  },

  async newestInTopic(streamId, topic) {
    const newest = await this.page([{ operator: "channel", operand: streamId }, { operator: "topic", operand: topic }], null, 1);
    if (!newest.length) throw new ToolError("invalid_argument", "no messages found in that topic");
    return newest[newest.length - 1];
  },

  async tagged(text, session) {
    try {
      return fixOutbound(composeBody(tagPrefix(this.seat, sessionTag(session)), text), { limit: MAX_CONTENT_LENGTH })[0];
    } catch (error) {
      if (error instanceof UsageError) throw new ToolError("invalid_argument", error.message);
      throw error;
    }
  },

  /** The target of a scheduled message, draft or typing notice:  a channel and topic, or a DM. */
  async fullTarget(args, audit) {
    const ids = args.user_ids ?? [];
    if (ids.length) {
      if (args.topic !== undefined) throw new ToolError("invalid_argument", "a direct message has no topic:  give user_ids or topic, not both");
      const { userIds } = await this.resolveDm({ user_ids: ids }, { active: true });
      if (!userIds.length) throw new ToolError("invalid_argument", "that is only this bot:  name at least one other person");
      audit.recipient_ids = userIds;
      return { type: "direct", to: userIds };
    }
    if (args.topic === undefined) throw new ToolError("invalid_argument", "a channel message needs a topic (or give user_ids for a DM)");
    const streamId = await this.channelId(normalizeChannel(args.channel));
    audit.channel_id = streamId;
    return { type: "stream", to: streamId, topic: normalizeTopic(args.topic) };
  },

  /** Run a create once per key:  a repeat returns the first result;  an uncertain one is never resent. */
  async once(tool, explicit, parts, check, send) {
    const idem = await this.idemKey(explicit ? `${tool}:${explicit}` : undefined, [tool, ...parts]);
    const begun = await this.gate.idemBegin({ key: idem.key, ttlMs: idem.ttlMs, fields: { tool } });
    if (begun.verdict === "busy") throw new ToolError("rate_limited", "the same write is already in flight in another chat of this seat", { retryable: true, retryAfterS: 30 });
    if (begun.verdict === "duplicate") return { ...(begun.row.result ?? {}), duplicate: true };
    if (begun.verdict === "reconcile") {
      await this.gate.idemSet({ key: idem.key, changes: { state: "unknown" } });
      throw new ToolError("outcome_unknown", `an earlier ${tool} with this key may or may not have landed.  Check before trying again with a new key.`, { check });
    }
    try {
      await this.reserve("write");
    } catch (error) {
      await this.gate.idemDrop({ key: idem.key });
      throw error;
    }
    const attempt = this.now();
    let result;
    try {
      result = await send();
    } catch (error) {
      const mapped = mapException(error, { write: true, sent: true });
      if (mapped.code === "outcome_unknown") {
        await this.gate.idemSet({ key: idem.key, changes: { state: "unknown", ts: attempt } });
        mapped.check = { ...check };
      } else {
        await this.gate.idemDrop({ key: idem.key });
      }
      throw mapped;
    }
    await this.gate.idemSet({ key: idem.key, changes: { state: "sent", id: isInt(result.id) ? result.id : 0, result } });
    return { ...result, duplicate: false };
  },

  // ---------------------------------------------------------------- messages

  async tool_get_message(args, audit) {
    await this.ensureSeat();
    const message = await this.visibleMessage(args.message_id);
    audit.message_id = args.message_id;
    if (isInt(message.stream_id)) audit.channel_id = message.stream_id;
    await this.reserve("read");
    const item = await this.messageItem(message, await this.users());
    item.flags = (Array.isArray(message.flags) ? message.flags : []).filter((f) => typeof f === "string").slice(0, 20).map((f) => c(f, 40));
    item.reactions = Array.isArray(message.reactions) ? message.reactions.length : 0;
    return this.listResult("get_message", [["message_id", String(args.message_id)]], "1 message", [item], { count: 1, ids: isInt(item.id) ? [item.id] : [] });
  },

  async tool_edit_message(args, audit) {
    this.scan({ text: args.text });
    const text = normalizeText(args.text);
    await this.ensureSeat();
    const message = await this.ownMessage(args.message_id);
    audit.message_id = args.message_id;
    if (isInt(message.stream_id)) audit.channel_id = message.stream_id;
    // `to` tags peers as post does, and the listener wakes a seat an edit newly tags (owner, Oct 29
    // 2026).  The edit replaces the whole text, so the caller names again any peer it should keep.
    const [content] = await this.compose(text, [...(args.to ?? [])], args.session);
    audit.body_len = [...content].length;
    await this.reserve("write");
    await this.zulip.patch(`messages/${args.message_id}`, { content });
    return this.writeResult({ id: args.message_id, edited: true });
  },

  // Any message in a channel this seat may use:  no own-message rule and no time limit here (owner,
  // Oct 29 2026).  Zulip's realm delete permissions decide, and its refusal comes back as the error.
  async tool_delete_message(args, audit) {
    await this.ensureSeat();
    const message = await this.visibleMessage(args.message_id);
    audit.message_id = args.message_id;
    if (isInt(message.stream_id)) audit.channel_id = message.stream_id;
    await this.reserve("write");
    try {
      await this.zulip.delete(`messages/${args.message_id}`);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 400) throw error;
      // Zulip answers 400 for a refusal and for a message that is gone.  Only a message that is still
      // there was refused (permission or the time limit);  any other 400 is Zulip's own error.
      try {
        await this.zulip.get(`messages/${args.message_id}`, { apply_markdown: false });
      } catch {
        throw error;
      }
      throw new ToolError("invalid_argument", DELETE_REFUSED(args.message_id));
    }
    return this.writeResult({ id: args.message_id, deleted: true });
  },

  async tool_message_history(args, audit) {
    await this.ensureSeat();
    const message = await this.visibleMessage(args.message_id);
    audit.message_id = args.message_id;
    if (isInt(message.stream_id)) audit.channel_id = message.stream_id;
    await this.reserve("read");
    const rows = (await this.zulip.get(`messages/${args.message_id}/history`)).message_history ?? [];
    const items = rows.filter((r) => r && typeof r === "object").map((r) => ({
      user_id: isInt(r.user_id) ? r.user_id : null,
      time: formatTime(Number(r.timestamp) || 0),
      topic: c(r.topic ?? ""),
      prev_topic: typeof r.prev_topic === "string" ? c(r.prev_topic) : null,
      body: clean(r.content ?? "", BODY_LIMIT)[0],
    }));
    return this.listResult("message_history", [["message_id", String(args.message_id)]], plural(items.length, "version"), items, { count: items.length, message_id: args.message_id });
  },

  async tool_move_topic(args, audit) {
    const channel = normalizeChannel(args.channel);
    const topic = normalizeTopic(args.topic);
    const newTopic = args.new_topic !== undefined ? topicCheck(args.new_topic, "new_topic") : null;
    const newChannel = args.new_channel !== undefined ? normalizeChannel(args.new_channel) : null;
    this.scan(Object.fromEntries([["new_topic", newTopic], ["new_channel", newChannel]].filter(([, v]) => v)));
    if (newTopic === null && newChannel === null) throw new ToolError("invalid_argument", "nothing to move:  give new_topic, new_channel or both");
    audit.topic = await this.auditTopic(topic);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    const targetId = newChannel === null ? null : await this.channelId(newChannel);
    audit.channel_id = streamId;
    let anchor;
    if (args.message_id !== undefined) {
      anchor = await this.visibleMessage(args.message_id);
      if (anchor.stream_id !== streamId || String(anchor.subject ?? anchor.topic ?? "").toLowerCase() !== topic.toLowerCase()) {
        throw new ToolError("invalid_argument", `message ${args.message_id} is not in that topic`);
      }
    } else {
      anchor = await this.newestInTopic(streamId, topic);
    }
    audit.message_id = anchor.id;
    await this.reserve("write");
    await this.zulip.patch(`messages/${anchor.id}`, {
      propagate_mode: args.propagate_mode,
      send_notification_to_old_thread: args.notify_old_thread,
      send_notification_to_new_thread: args.notify_new_thread,
      ...(newTopic !== null ? { topic: newTopic } : {}),
      ...(targetId !== null ? { stream_id: targetId } : {}),
    });
    return this.writeResult({ id: anchor.id, channel_id: targetId ?? streamId });
  },

  async resolveTopic(args, audit, resolve) {
    const channel = normalizeChannel(args.channel);
    const topic = normalizeTopic(args.topic);
    // A resolve or unresolve renames every message's topic, so a name that
    // looks like it holds a secret is refused before any request.
    this.scan({ topic });
    if (resolve) resolvedName(topic, true);
    audit.topic = await this.auditTopic(topic);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    audit.channel_id = streamId;
    // Unresolving by the plain name means the check-marked topic resolve left;
    // the name as given is the fallback.
    let latest;
    if (!resolve && !topic.startsWith(RESOLVED_PREFIX.trim())) {
      try {
        latest = await this.newestInTopic(streamId, `${RESOLVED_PREFIX}${topic}`);
      } catch (error) {
        if (!(error instanceof ToolError)) throw error;
      }
    }
    latest ??= await this.newestInTopic(streamId, topic);
    const next = resolvedName(String(latest.subject ?? latest.topic ?? ""), resolve);
    audit.message_id = latest.id;
    await this.reserve("write");
    await this.zulip.patch(`messages/${latest.id}`, { topic: next, propagate_mode: "change_all" });
    return this.writeResult({ id: latest.id, channel_id: streamId });
  },

  async tool_resolve_topic(args, audit) {
    return this.resolveTopic(args, audit, true);
  },

  async tool_unresolve_topic(args, audit) {
    return this.resolveTopic(args, audit, false);
  },

  async tool_unreact(args, audit) {
    await this.ensureSeat();
    const message = await this.visibleMessage(args.message_id);
    audit.message_id = args.message_id;
    if (isInt(message.stream_id)) audit.channel_id = message.stream_id;
    await this.reserve("react");
    try {
      await this.zulip.delete(`messages/${args.message_id}/reactions`, { emoji_name: args.emoji });
    } catch (error) {
      // A reaction that is not there is a success, so a retry is safe.
      if (!(error instanceof ApiError && error.code === "REACTION_DOES_NOT_EXIST")) throw mapException(error, { write: true });
    }
    return this.writeResult({ id: args.message_id, emoji: args.emoji });
  },

  async flag(args, audit, flag, add) {
    const ids = [...new Set(args.message_ids ?? [])].sort((a, b) => a - b);
    if (ids.length && args.channel !== undefined) throw new ToolError("invalid_argument", "give message_ids or a channel, not both");
    if (args.topic !== undefined && args.channel === undefined) throw new ToolError("invalid_argument", "a topic needs its channel");
    if (!ids.length && args.channel === undefined) throw new ToolError("invalid_argument", "give message_ids, or a channel (and optionally a topic)");
    await this.ensureSeat();
    const op = add ? "add" : "remove";
    if (ids.length) {
      for (const id of ids) await this.visibleMessage(id);
      await this.reserve("react");
      const result = await this.zulip.post("messages/flags", { messages: ids, op, flag });
      return this.writeResult({ updated: (result.messages ?? []).length });
    }
    const streamId = await this.channelId(normalizeChannel(args.channel));
    audit.channel_id = streamId;
    const narrow = [{ operator: "channel", operand: streamId }];
    if (args.topic !== undefined) narrow.push({ operator: "topic", operand: normalizeTopic(args.topic) });
    await this.reserve("react");
    const result = await this.zulip.post("messages/flags/narrow", { narrow, anchor: "oldest", include_anchor: true, num_before: 0, num_after: FLAG_NARROW_MAX, op, flag });
    return this.writeResult({ updated: isInt(result.updated_count) ? result.updated_count : 0 });
  },

  async tool_mark_read(args, audit) {
    return this.flag(args, audit, "read", true);
  },
  async tool_mark_unread(args, audit) {
    return this.flag(args, audit, "read", false);
  },
  async tool_star(args, audit) {
    return this.flag(args, audit, "starred", true);
  },
  async tool_unstar(args, audit) {
    return this.flag(args, audit, "starred", false);
  },

  // ---------------------------------------------------------------- search

  async tool_search(args, audit) {
    const channel = args.channel !== undefined ? normalizeChannel(args.channel) : null;
    const topic = args.topic !== undefined ? normalizeTopic(args.topic) : null;
    const dmWith = [...new Set(args.dm_with ?? [])].sort((a, b) => a - b);
    if (channel !== null && dmWith.length) throw new ToolError("invalid_argument", "a search names a channel or a direct-message conversation, not both");
    if (topic !== null && channel === null) throw new ToolError("invalid_argument", "a topic search needs its channel");
    await this.ensureSeat();
    const narrow = [];
    if (channel !== null) {
      const streamId = await this.channelId(channel);
      audit.channel_id = streamId;
      narrow.push({ operator: "channel", operand: streamId });
    }
    if (topic !== null) narrow.push({ operator: "topic", operand: topic });
    if (dmWith.length) narrow.push({ operator: "dm", operand: dmWith });
    if (args.sender !== undefined) narrow.push({ operator: "sender", operand: args.sender });
    for (const has of args.has ?? []) narrow.push({ operator: "has", operand: has });
    if (args.is !== undefined) narrow.push(SEARCH_IS[args.is] ?? { operator: "is", operand: args.is });
    if (args.query !== undefined) narrow.push({ operator: "search", operand: args.query });
    if (!narrow.length) throw new ToolError("invalid_argument", "a search needs at least one term:  query, channel, sender, has, is or dm_with");
    await this.reserve("read");
    let messages;
    if (args.since_id === undefined && args.near !== undefined) {
      const half = Math.max(1, Math.floor(args.limit / 2));
      const result = await this.zulip.get("messages", { narrow, anchor: args.near, num_before: half, num_after: args.limit - half, include_anchor: true, apply_markdown: false });
      messages = [...(result.messages ?? [])].filter((m) => m && isInt(m.id)).sort((a, b) => a.id - b.id);
    } else {
      messages = await this.page(narrow, args.since_id, args.limit);
    }
    const next = this.constructor.nextSince(messages, args.since_id);
    const allowed = await this.channelSet();
    // The channel rule on every result;  direct messages only when the caller named the conversation.
    const shown = messages.filter((m) => (m.type === "stream" && allowed.has(m.stream_id)) || (m.type === "private" && dmWith.length));
    const shownArgs = ["query", "channel", "topic", "sender"].filter((k) => args[k] !== undefined).map((k) => [k, args[k]]);
    return this.messagesResult("search", shownArgs, shown, next);
  },

  // ---------------------------------------------------------------- channels

  async tool_channels(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const allowed = await this.channelSet();
    const deny = new Set(this.config.channelsDeny ?? []);
    let rows;
    if (args.all) {
      rows = ((await this.zulip.get("streams", { include_public: true, include_subscribed: true })).streams ?? [])
        .filter((s) => s && s.invite_only === false && s.is_archived !== true && isInt(s.stream_id) && !deny.has(s.stream_id));
    } else {
      rows = ((await this.zulip.get("users/me/subscriptions")).subscriptions ?? []).filter((s) => s && allowed.has(s.stream_id));
    }
    const items = rows
      .map((s) => ({ stream_id: s.stream_id, name: c(String(s.name ?? "")), description: c(String(s.description ?? ""), 300), public: true, subscribed: allowed.has(s.stream_id) }))
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    return this.listResult("channels", [], plural(items.length, "channel"), items, { count: items.length, ids: items.map((i) => i.stream_id) });
  },

  async tool_channel_info(args, audit) {
    const channel = normalizeChannel(args.channel);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    audit.channel_id = streamId;
    await this.reserve("read");
    const stream = (await this.zulip.get(`streams/${streamId}`)).stream ?? {};
    const members = ((await this.zulip.get(`streams/${streamId}/members`)).subscribers ?? []).filter(isInt);
    const item = {
      stream_id: streamId,
      name: c(String(stream.name ?? "")),
      description: c(String(stream.description ?? ""), 300),
      public: stream.invite_only === false,
      subscriber_count: members.length,
      history_public_to_subscribers: stream.history_public_to_subscribers === true,
      date_created: isInt(stream.date_created) ? stream.date_created : null,
    };
    return this.listResult("channel_info", [["channel", channel]], "1 channel", [item], { stream_id: streamId, subscriber_count: members.length });
  },

  async tool_channel_members(args, audit) {
    const channel = normalizeChannel(args.channel);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    audit.channel_id = streamId;
    await this.reserve("read");
    const ids = ((await this.zulip.get(`streams/${streamId}/members`)).subscribers ?? []).filter(isInt).sort((a, b) => a - b).slice(0, args.limit);
    const byId = new Map((await this.users()).map((u) => [u.user_id, u]));
    const items = ids.map((id) => userRow(byId.get(id) ?? { user_id: id }));
    return this.listResult("channel_members", [["channel", channel]], plural(items.length, "member"), items, { count: items.length, ids });
  },

  async tool_unsubscribe(args, audit) {
    const channel = normalizeChannel(args.channel);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    audit.channel_id = streamId;
    const name = (await this.channelSet()).byId.get(streamId);
    await this.reserve("write");
    const result = await this.zulip.delete("users/me/subscriptions", { subscriptions: [name] });
    // The allowed set changed:  the next call reads it again.
    this.channelsCache.delete(this.seat);
    return this.writeResult({ channel_id: streamId, removed: Array.isArray(result.removed) && result.removed.length > 0 });
  },

  async tool_topic_visibility(args, audit) {
    const channel = normalizeChannel(args.channel);
    const topic = normalizeTopic(args.topic);
    this.scan({ topic });
    audit.topic = await this.auditTopic(topic);
    await this.ensureSeat();
    const streamId = await this.channelId(channel);
    audit.channel_id = streamId;
    const policy = VISIBILITY[args.visibility];
    await this.reserve("react");
    await this.zulip.post("user_topics", { stream_id: streamId, topic, visibility_policy: policy });
    return this.writeResult({ channel_id: streamId, visibility_policy: policy });
  },

  // ---------------------------------------------------------------- users

  async tool_users(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const needle = (args.query ?? "").trim().toLowerCase();
    const email = args.email?.trim().toLowerCase();
    const rows = (await this.users()).filter((u) => {
      if (!args.include_inactive && u.is_active === false) return false;
      if (args.user_id !== undefined && u.user_id !== args.user_id) return false;
      if (email && ![String(u.email ?? "").toLowerCase(), String(u.delivery_email ?? "").toLowerCase()].includes(email)) return false;
      if (needle) {
        const hay = [String(u.full_name ?? "").toLowerCase(), String(u.email ?? "").toLowerCase()];
        if (u.is_bot && u.email) hay.push(seatTagFor(u).toLowerCase());
        if (!hay.some((h) => h.includes(needle))) return false;
      }
      return true;
    });
    rows.sort((a, b) => String(a.full_name ?? "").toLowerCase().localeCompare(String(b.full_name ?? "").toLowerCase()));
    const items = rows.slice(0, args.limit).map(userRow);
    const shown = ["query", "user_id", "email"].filter((k) => args[k] !== undefined).map((k) => [k, String(args[k])]);
    return this.listResult("users", shown, plural(items.length, "user"), items, { count: items.length, ids: items.map((i) => i.user_id) });
  },

  async tool_presence(args, audit) {
    if ((args.user_id === undefined) === (args.email === undefined)) throw new ToolError("invalid_argument", "give user_id or email (one of them)");
    await this.ensureSeat();
    let userId = args.user_id;
    if (userId === undefined) {
      const { userIds } = await this.resolveDm({ emails: [args.email] }, { active: false });
      userId = userIds[0] ?? this.me.user_id;
    }
    await this.reserve("read");
    const data = (await this.zulip.get(`users/${userId}/presence`)).presence ?? {};
    const newest = Math.max(0, ...Object.values(data).filter((v) => v && typeof v === "object").map((v) => Number(v.timestamp) || 0));
    const age = this.now() / 1000 - newest;
    const status = newest && age <= PRESENCE_ACTIVE_S ? "active" : newest && age <= PRESENCE_IDLE_S ? "idle" : "offline";
    const item = { user_id: userId, status, last_seen: newest ? formatTime(newest) : null };
    return this.listResult("presence", [["user_id", String(userId)]], "1 user", [item], { user_id: userId, status });
  },

  async tool_set_status(args, audit) {
    if (args.status_text !== undefined) this.scan({ status_text: args.status_text });
    if (args.status_text === undefined && args.emoji_name === undefined && args.away === undefined) {
      throw new ToolError("invalid_argument", "nothing to set:  give status_text, emoji_name or away");
    }
    await this.ensureSeat();
    await this.reserve("react");
    await this.zulip.post("users/me/status", {
      ...(args.status_text !== undefined ? { status_text: args.status_text } : {}),
      ...(args.emoji_name !== undefined ? { emoji_name: args.emoji_name } : {}),
      ...(args.away !== undefined ? { away: args.away } : {}),
    });
    return this.writeResult({ updated: true });
  },

  async tool_user_groups(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const groups = ((await this.zulip.get("user_groups")).user_groups ?? []).filter((g) => g && isInt(g.id));
    if (args.group_id !== undefined) {
      const group = groups.find((g) => g.id === args.group_id);
      if (!group) throw new ToolError("invalid_argument", "no user group has that group_id");
      const byId = new Map((await this.users()).map((u) => [u.user_id, u]));
      const items = (group.members ?? []).filter(isInt).slice(0, args.limit).map((id) => userRow(byId.get(id) ?? { user_id: id }));
      return this.listResult("user_groups", [["group_id", String(args.group_id)]], plural(items.length, "member"), items, { count: items.length, ids: items.map((i) => i.user_id) });
    }
    const items = groups.slice(0, args.limit).map((g) => ({
      id: g.id,
      name: c(String(g.name ?? "")),
      description: c(String(g.description ?? ""), 300),
      member_count: (g.members ?? []).filter(isInt).length,
      system: g.is_system_group === true,
    }));
    return this.listResult("user_groups", [], plural(items.length, "group"), items, { count: items.length, ids: items.map((i) => i.id) });
  },

  async tool_custom_emoji(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const items = Object.values((await this.zulip.get("realm/emoji")).emoji ?? {})
      .filter((e) => e && typeof e === "object" && (args.include_deactivated || !e.deactivated) && Number.isSafeInteger(Number(e.id)))
      .map((e) => ({ id: Number(e.id), name: c(String(e.name ?? "")), deactivated: e.deactivated === true, author_id: isInt(e.author_id) ? e.author_id : null }))
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    return this.listResult("custom_emoji", [], plural(items.length, "emoji"), items, { count: items.length, ids: items.map((i) => i.id) });
  },

  // ---------------------------------------------------------------- uploads

  async tool_upload(args, audit) {
    const hasText = args.text !== undefined;
    if (hasText === (args.content_base64 !== undefined)) throw new ToolError("invalid_argument", "give text or content_base64 (one of them)");
    const bytes = hasText ? new TextEncoder().encode(args.text) : decodeBase64(args.content_base64);
    if (!bytes.length || bytes.length > UPLOAD_MAX_BYTES) throw new ToolError("invalid_argument", `the file must be 1 byte to ${UPLOAD_MAX_BYTES} bytes`);
    let asText = "";
    try {
      asText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      asText = "";
    }
    this.scan({ filename: args.filename, ...(asText ? { file: asText } : {}) });
    await this.ensureSeat();
    audit.body_len = bytes.length;
    const kind = asText ? "text/plain" : "application/octet-stream";
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
    const result = await this.once("upload", args.idempotency_key, [args.filename, digest, bytes.length], { tool: "zulip_upload" }, async () => {
      const answer = await this.zulip.upload(args.filename, bytes, kind);
      return { url: String(answer.url ?? answer.uri ?? ""), bytes: bytes.length };
    });
    return this.writeResult({ url: result.url, bytes: result.bytes, duplicate: result.duplicate });
  },

  // ---------------------------------------------------------------- scheduled messages and drafts

  scheduleItem(row) {
    const stream = row.type === "stream";
    const to = row.to;
    return {
      id: row.id,
      dm: !stream,
      to: Array.isArray(to) ? to.filter(isInt) : isInt(to) ? to : null,
      topic: c(String(row.topic ?? "")),
      body: clean(row.content ?? "", BODY_LIMIT)[0],
      ...(row.deliver_at !== undefined ? { deliver_at: isInt(row.deliver_at) ? row.deliver_at : null, failed: row.failed === true } : {}),
    };
  },

  async tool_scheduled_list(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const allowed = await this.channelSet();
    const rows = ((await this.zulip.get("scheduled_messages")).scheduled_messages ?? [])
      .filter((r) => r && isInt(r.scheduled_message_id))
      .map((r) => ({ id: r.scheduled_message_id, type: r.type === "stream" ? "stream" : "direct", to: r.to, topic: r.topic, content: r.content, deliver_at: r.scheduled_delivery_timestamp, failed: r.failed }))
      .filter((r) => r.type !== "stream" || allowed.has(r.to));
    const items = rows.map((r) => this.scheduleItem(r));
    return this.listResult("scheduled_list", [], plural(items.length, "scheduled message"), items, { count: items.length, ids: items.map((i) => i.id) });
  },

  checkWhen(deliverAt) {
    if (deliverAt <= Math.floor(this.now() / 1000)) throw new ToolError("invalid_argument", "deliver_at must be in the future (unix seconds)");
    return deliverAt;
  },

  async tool_scheduled_create(args, audit) {
    this.scan({ text: args.text, ...(args.topic !== undefined ? { topic: args.topic } : {}), ...(args.user_ids?.length ? {} : { channel: args.channel }) });
    const text = normalizeText(args.text);
    const when = this.checkWhen(args.deliver_at);
    await this.ensureSeat();
    const target = await this.fullTarget(args, audit);
    const content = await this.tagged(text, args.session);
    audit.body_len = [...content].length;
    const result = await this.once("scheduled_create", args.idempotency_key, [sessionTag(args.session), target, text, when], { tool: "zulip_scheduled_list" }, async () => {
      const answer = await this.zulip.post("scheduled_messages", { ...target, content, scheduled_delivery_timestamp: when });
      if (!isInt(answer.scheduled_message_id)) throw new ToolError("internal", "Zulip returned no scheduled message id");
      return { id: answer.scheduled_message_id, deliver_at: when };
    });
    audit.message_id = result.id;
    return this.writeResult({ id: result.id, deliver_at: when, duplicate: result.duplicate });
  },

  async tool_scheduled_edit(args, audit) {
    if (args.text !== undefined) this.scan({ text: args.text });
    const text = args.text !== undefined ? normalizeText(args.text) : null;
    const topic = args.topic !== undefined ? normalizeTopic(args.topic) : null;
    if (topic !== null) this.scan({ topic });
    const when = args.deliver_at !== undefined ? this.checkWhen(args.deliver_at) : null;
    if (text === null && topic === null && when === null) throw new ToolError("invalid_argument", "nothing to change:  give text, topic or deliver_at");
    await this.ensureSeat();
    const row = ((await this.zulip.get("scheduled_messages")).scheduled_messages ?? []).find((r) => r?.scheduled_message_id === args.scheduled_message_id);
    if (!row) throw new ToolError("invalid_argument", "this bot has no scheduled message with that id");
    if (row.type === "stream" && !(await this.channelSet()).has(row.to)) throw new ToolError("channel_not_allowed", "That scheduled message is for a channel this seat may not use.");
    const content = text !== null ? await this.tagged(text, args.session) : null;
    await this.reserve("write");
    await this.zulip.patch(`scheduled_messages/${args.scheduled_message_id}`, {
      ...(content !== null ? { content } : {}),
      ...(topic !== null ? { topic } : {}),
      ...(when !== null ? { scheduled_delivery_timestamp: when } : {}),
    });
    return this.writeResult({ id: args.scheduled_message_id });
  },

  async tool_scheduled_delete(args, audit) {
    await this.ensureSeat();
    await this.reserve("write");
    await this.zulip.delete(`scheduled_messages/${args.scheduled_message_id}`);
    return this.writeResult({ id: args.scheduled_message_id, deleted: true });
  },

  async tool_drafts_list(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const allowed = await this.channelSet();
    const items = ((await this.zulip.get("drafts")).drafts ?? [])
      .filter((d) => d && isInt(d.id) && (d.type !== "stream" || allowed.has(Array.isArray(d.to) ? d.to[0] : d.to)))
      .map((d) => this.scheduleItem({ id: d.id, type: d.type === "stream" ? "stream" : "direct", to: d.to, topic: d.topic, content: d.content }));
    return this.listResult("drafts_list", [], plural(items.length, "draft"), items, { count: items.length, ids: items.map((i) => i.id) });
  },

  draftOf(target, text) {
    const timestamp = Math.floor(this.now() / 1000);
    return target.type === "direct"
      ? { type: "private", to: target.to, topic: "", content: text, timestamp }
      : { type: "stream", to: [target.to], topic: target.topic, content: text, timestamp };
  },

  async tool_draft_create(args, audit) {
    this.scan({ text: args.text, ...(args.topic !== undefined ? { topic: args.topic } : {}) });
    const text = normalizeText(args.text);
    await this.ensureSeat();
    const target = await this.fullTarget(args, audit);
    audit.body_len = [...text].length;
    const result = await this.once("draft_create", undefined, [target, text], { tool: "zulip_drafts_list" }, async () => {
      const ids = (await this.zulip.post("drafts", { drafts: [this.draftOf(target, text)] })).ids ?? [];
      return { id: isInt(ids[0]) ? ids[0] : 0 };
    });
    return this.writeResult({ id: result.id });
  },

  async tool_draft_edit(args, audit) {
    this.scan({ text: args.text, ...(args.topic !== undefined ? { topic: args.topic } : {}) });
    const text = normalizeText(args.text);
    await this.ensureSeat();
    const target = await this.fullTarget(args, audit);
    await this.reserve("write");
    await this.zulip.patch(`drafts/${args.draft_id}`, { draft: this.draftOf(target, text) });
    return this.writeResult({ id: args.draft_id });
  },

  async tool_draft_delete(args, audit) {
    await this.ensureSeat();
    await this.reserve("write");
    await this.zulip.delete(`drafts/${args.draft_id}`);
    return this.writeResult({ id: args.draft_id, deleted: true });
  },

  async tool_typing(args, audit) {
    await this.ensureSeat();
    const target = await this.fullTarget(args, audit);
    await this.reserve("react");
    await this.zulip.post("typing", target.type === "direct"
      ? { type: "direct", op: args.op, to: target.to }
      : { type: "stream", op: args.op, stream_id: target.to, topic: target.topic });
    return this.writeResult({ op: args.op });
  },
};

export const FULL_TOOL_NAMES = Object.freeze(Object.keys(ZULIP_FULL_METHODS).filter((k) => k.startsWith("tool_")).map((k) => k.slice(5)));
