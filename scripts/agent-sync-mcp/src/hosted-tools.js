// The seven agent-sync tools for one hosted seat (spec sections 1, 3.6 to 3.9).
// A port of scripts/agent_sync/mcp/tools.py over the Zulip REST API, with the
// hosted rules on top:  the seat only from the grant, the D4 channel
// allowlist by stream id on every tool, the member-only role check, spacing,
// budgets and idempotency in the seat's SeatGate, and one audit row per call.
//
// No `cloudflare:` imports:  the Worker hands in a ZulipClient and a SeatGate
// stub, node tests hand in a fake Zulip and a SeatGate over a Map.

import {
  TOOLS_BY_NAME,
  WRITE_TOOLS,
  READ_TOOLS,
  BODY_LIMIT,
  NAME_LIMIT,
  RESOLVED_PREFIX,
  ToolError,
  ApiError,
  UsageError,
  mapException,
  errorResult,
  okResult,
  schemaProblem,
  withDefaults,
  clean,
  envelope,
  scanSecret,
  basicForms,
  normalizeChannel,
  normalizeTopic,
  normalizeText,
  sessionTag,
  tagPrefix,
  composeBody,
  seatTagFor,
  formatTime,
  sha256Hex,
  stableJson,
} from "./contract.js";
import { sentenceGap } from "./textfmt.js";
import { REALM } from "./zulip.js";

export const TRANSPORT = "hosted";
export const MEMBER_ROLE = 400; // spec 3.6:  hosted seats accept member only
export const RECONCILE_DELAY_MS = 15_000;
export const RECONCILE_WINDOW_S = 300;
export const IDEM_EXPLICIT_TTL_MS = 24 * 3600 * 1000;
export const IDEM_IMPLICIT_TTL_MS = 10 * 60 * 1000;
export const USERS_TTL_MS = 10 * 60 * 1000;
export const RESOURCE_SCOPES = Object.freeze({ read: "zulip:read", write: "zulip:write" });

/** tools/call named a tool this server does not have:  a protocol error, not a tool error. */
export class UnknownTool extends Error {
  constructor(name) {
    super("unknown tool");
    this.toolName = name;
  }
}

const isInt = (v) => typeof v === "number" && Number.isSafeInteger(v);

// Member lists per seat, shared by the calls of one isolate.
const USERS_CACHE = new Map();

export class HostedTools {
  /**
   * @param {object} o
   * @param {string} o.seat                the seat, from the grant's props only
   * @param {string[]} o.scopes            the grant's scopes
   * @param {string} o.clientId            the OAuth client id (audit only)
   * @param {string} o.grantRef            12 hex of the grant id's sha256 (audit only)
   * @param {boolean} o.paused             the seat's pause flag at request time
   * @param {object} o.config              loadConfig(env)
   * @param {object|null} o.zulip          a ZulipClient for this seat, or null when no key is installed
   * @param {string} o.key                 the seat's key (for the secret scan and scrubbing only)
   * @param {object} o.gate                the seat's SeatGate (RPC stub or test adapter)
   * @param {object} [o.cf]                request.cf (asn, country)
   */
  constructor({ seat, scopes, clientId, grantRef, paused, config, zulip, key, gate, cf = {}, now = Date.now, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), log = (row) => console.log(JSON.stringify(row)), usersCache = USERS_CACHE }) {
    this.seat = seat;
    this.scopes = new Set(scopes ?? []);
    this.clientId = clientId ?? "";
    this.grantRef = grantRef ?? "";
    this.paused = paused === true;
    this.config = config;
    this.zulip = zulip;
    this.email = config.botEmails[seat] ?? "";
    this.known = key ? basicForms(this.email, key) : [];
    this.gate = gate;
    this.cf = cf ?? {};
    this.now = now;
    this.sleep = sleep;
    this.log = log;
    this.usersCache = usersCache;
    this.me = null;
  }

  // ---------------------------------------------------------------- entry

  /** Run one tool.  Returns a CallToolResult.  Throws UnknownTool. */
  async call(name, rawArgs) {
    const spec = Object.hasOwn(TOOLS_BY_NAME, name) ? TOOLS_BY_NAME[name] : null;
    if (!spec) throw new UnknownTool(name);
    const started = this.now();
    // Per call, so two calls in one request never share audit fields.
    const audit = { tool: name };
    let result;
    try {
      const args = rawArgs === undefined || rawArgs === null ? {} : rawArgs;
      if (typeof args !== "object" || Array.isArray(args)) throw new ToolError("invalid_argument", "arguments must be an object");
      const problem = schemaProblem(spec.inputSchema, args);
      if (problem) throw new ToolError("invalid_argument", problem);
      this.requireScope(name);
      if (this.paused) throw new ToolError("paused", "This seat is paused.  Jay can unpause it in Agent-Sync Admin.");
      result = await this[`tool_${name}`](withDefaults(spec.inputSchema, args), audit);
      audit.outcome = "ok";
    } catch (error) {
      const mapped = mapException(error, { write: WRITE_TOOLS.has(name) });
      if (mapped.code === "rate_limited" && error instanceof ApiError) {
        await this.safely(() => this.gate.noteRateLimited({ retryAfterS: mapped.retryAfterS }));
      }
      if (!(error instanceof ToolError) && mapped.code === "internal") {
        this.log({ event: "tool_internal_error", seat: this.seat, tool: name, name: String(error?.name ?? "Error").slice(0, 40) });
      }
      result = errorResult(mapped, (text) => this.scrub(text));
      audit.outcome = "error";
      audit.error_code = mapped.code;
    }
    const latency = this.now() - started;
    const row = {
      seat: this.seat,
      grant_ref: this.grantRef,
      client_id: this.clientId,
      latency_ms: latency,
      asn: Number.isFinite(this.cf.asn) ? this.cf.asn : null,
      country: typeof this.cf.country === "string" ? this.cf.country.slice(0, 2) : null,
      ...audit,
    };
    await this.safely(() => this.gate.auditCall(row));
    this.log({ event: "tool", seat: this.seat, tool: name, outcome: row.error_code ?? "ok", latency_ms: latency, grant_ref: this.grantRef });
    return result;
  }

  async safely(fn) {
    try {
      return await fn();
    } catch {
      this.log({ event: "gate_call_failed", seat: this.seat });
      return null;
    }
  }

  /** Remove the loaded key and its Basic form from a message. */
  scrub(text) {
    let out = String(text);
    for (const value of this.known) if (value) out = out.split(value).join("[redacted]");
    return out;
  }

  requireScope(name) {
    const scope = READ_TOOLS.has(name) ? RESOURCE_SCOPES.read : RESOURCE_SCOPES.write;
    if (this.scopes.has(scope)) return;
    const challenge = `Bearer error="insufficient_scope", scope="${scope}", resource_metadata="https://agent-sync.jays.services/.well-known/oauth-protected-resource/mcp", error_description="This tool needs ${scope}"`;
    throw new ToolError("not_authorized", `This tool needs the ${scope} scope.  Reconnect the app and approve ${scope}.`, {
      meta: { "mcp/www_authenticate": [challenge] },
    });
  }

  // ---------------------------------------------------------------- seat binding

  /** Spec 3.6:  users/me on first use and every 10 minutes;  member bots of this seat only. */
  async ensureSeat() {
    if (!this.zulip) {
      throw new ToolError("not_authorized", `No Zulip key is installed for ${this.seat}.  The owner installs it (scripts/agent-sync-mcp/install_seat_key.py).`);
    }
    const cached = await this.gate.roleGet();
    if (cached) {
      if (!cached.ok) throw new ToolError("not_authorized", `The ${this.seat} bot is refused:  ${cached.reason}`);
      this.me = { user_id: cached.user_id, role: cached.role };
      return;
    }
    const me = await this.zulip.get("users/me");
    const problem = seatProblem(me, { seat: this.seat, email: this.email });
    await this.gate.roleSet({ ok: !problem, user_id: isInt(me.user_id) ? me.user_id : null, role: isInt(me.role) ? me.role : null, reason: problem ?? "" });
    if (problem) throw new ToolError("not_authorized", `The ${this.seat} bot is refused:  ${problem}`);
    this.me = { user_id: me.user_id, role: me.role };
  }

  async reserve(kind) {
    const slot = await this.gate.reserve({ kind });
    if (!slot?.ok) {
      if (slot?.code === "budget_exhausted") {
        throw new ToolError("budget_exhausted", `This seat's ${kind} budget for the ${slot.window ?? "hour"} is spent.  Try again after reset_at.`, { retryable: true, resetAt: slot.resetAt });
      }
      if (slot?.code === "paused") throw new ToolError("paused", "This seat is paused.  Jay can unpause it in Agent-Sync Admin.");
      throw new ToolError("rate_limited", "Other calls from this seat are queued", { retryable: true, retryAfterS: slot?.retryAfterS ?? 3 });
    }
    if (slot.waitMs > 0) await this.sleep(slot.waitMs);
  }

  /** The D4 allowlist:  a channel name to its pinned stream id, before any request. */
  channelId(channel) {
    const id = this.config.channels.get(channel.toLowerCase());
    if (!id) {
      throw new ToolError("channel_not_allowed", `This seat may use only these channels:  ${[...this.config.channels.keys()].join(", ")}`);
    }
    return id;
  }

  allowedStream(message) {
    return message?.type === "stream" && this.config.channelIds.includes(message.stream_id);
  }

  async users() {
    const cached = this.usersCache.get(this.seat);
    if (cached && this.now() - cached.at < USERS_TTL_MS) return cached.members;
    const members = (await this.zulip.get("users")).members ?? [];
    this.usersCache.set(this.seat, { at: this.now(), members });
    return members;
  }

  // ---------------------------------------------------------------- reads

  async messageItem(message, members) {
    const senderId = isInt(message.sender_id) ? message.sender_id : null;
    const own = String(message.sender_email ?? "").toLowerCase() === this.email;
    const user = members.find((u) => senderId !== null && u.user_id === senderId);
    const isBot = own ? true : user ? Boolean(user.is_bot) : null;
    const owner = senderId === this.config.ownerUserId && this.config.ownerClients.includes(String(message.client ?? ""));
    const [body, truncated] = clean(message.content ?? "", BODY_LIMIT);
    return {
      id: isInt(message.id) ? message.id : null,
      sender_id: senderId,
      sender: clean(message.sender_full_name ?? "", NAME_LIMIT)[0],
      sender_email: clean(message.sender_email ?? "", NAME_LIMIT)[0],
      is_bot: isBot,
      owner,
      time: formatTime(Number(message.timestamp) || 0),
      channel: clean(typeof message.display_recipient === "string" ? message.display_recipient : "DM", NAME_LIMIT)[0],
      topic: clean(message.subject ?? message.topic ?? "", NAME_LIMIT)[0],
      body,
      truncated,
    };
  }

  async messagesResult(tool, args, messages, nextSinceId) {
    const members = messages.length ? await this.users() : [];
    const items = [];
    for (const m of messages) items.push(await this.messageItem(m, members));
    const ids = items.map((i) => i.id).filter((id) => id !== null);
    const summary = `${items.length} message${items.length === 1 ? "" : "s"}, next_since_id ${nextSinceId ?? "none"}`;
    return okResult(envelope(tool, args, summary, items), { count: items.length, ids, next_since_id: nextSinceId ?? null });
  }

  async page(narrow, sinceId, limit) {
    const params =
      sinceId !== undefined && sinceId !== null
        ? { narrow, anchor: sinceId, num_before: 0, num_after: limit, include_anchor: false, apply_markdown: false }
        : { narrow, anchor: "newest", num_before: limit, num_after: 0, include_anchor: true, apply_markdown: false };
    const result = await this.zulip.get("messages", params);
    return [...(result.messages ?? [])].filter((m) => m && isInt(m.id)).sort((a, b) => a.id - b.id);
  }

  static nextSince(messages, sinceId) {
    return messages.length ? Math.max(...messages.map((m) => m.id)) : sinceId ?? null;
  }

  async tool_whoami(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    const info = {
      seat: this.seat,
      email: this.email,
      user_id: this.me?.user_id ?? 0,
      role: this.me?.role ?? 0,
      realm: REALM,
      transport: TRANSPORT,
      tag_prefix: `${tagPrefix(this.seat, null)}]`,
      session_source: "none",
    };
    return okResult(JSON.stringify(info), info);
  }

  async tool_topics(args, audit) {
    const channel = normalizeChannel(args.channel);
    const streamId = this.channelId(channel);
    audit.channel_id = streamId;
    await this.ensureSeat();
    await this.reserve("read");
    const topics = ((await this.zulip.get(`users/me/${streamId}/topics`)).topics ?? [])
      .filter((t) => t && isInt(t.max_id))
      .sort((a, b) => b.max_id - a.max_id)
      .slice(0, args.limit);
    const items = topics.map((t) => ({ name: clean(t.name ?? "", NAME_LIMIT)[0], max_id: t.max_id, resolved: String(t.name ?? "").startsWith(RESOLVED_PREFIX) }));
    const summary = `${items.length} topic${items.length === 1 ? "" : "s"}`;
    return okResult(envelope("topics", [["channel", channel]], summary, items), { count: items.length, max_ids: items.map((i) => i.max_id) });
  }

  async tool_read_topic(args, audit) {
    const channel = normalizeChannel(args.channel);
    const topic = normalizeTopic(args.topic);
    const streamId = this.channelId(channel);
    audit.channel_id = streamId;
    audit.topic = await this.auditTopic(topic);
    await this.ensureSeat();
    await this.reserve("read");
    const messages = await this.page([{ operator: "channel", operand: streamId }, { operator: "topic", operand: topic }], args.since_id, args.limit);
    const next = HostedTools.nextSince(messages, args.since_id);
    // Hosted hides this bot's own posts unless include_self (spec 1).
    const shown = messages.filter((m) => args.include_self || String(m.sender_email ?? "").toLowerCase() !== this.email);
    return this.messagesResult("read_topic", [["channel", channel], ["topic", topic]], shown, next);
  }

  async tool_inbox(args, audit) {
    await this.ensureSeat();
    await this.reserve("read");
    // One stream-scoped query per allowlisted channel:  `is:mentioned` alone
    // would return DMs and private-channel mentions (spec 1, D4).
    let merged = [];
    for (const streamId of this.config.channelIds) {
      merged.push(...(await this.page([{ operator: "channel", operand: streamId }, { operator: "is", operand: "mentioned" }], args.since_id, args.limit)));
    }
    merged = [...new Map(merged.map((m) => [m.id, m])).values()].filter((m) => this.allowedStream(m)).sort((a, b) => a.id - b.id);
    const hasSince = args.since_id !== undefined && args.since_id !== null;
    const shown = hasSince ? merged.slice(0, args.limit) : merged.slice(-args.limit);
    return this.messagesResult("inbox", [], shown, HostedTools.nextSince(shown, args.since_id));
  }

  // ---------------------------------------------------------------- writes

  /** The topic for the audit row:  as written, unless it looks like a secret, then its hash. */
  async auditTopic(topic) {
    if (scanSecret(topic, this.known)) return `sha256:${(await sha256Hex(topic)).slice(0, 12)}`;
    return topic.slice(0, 60);
  }

  scan(fields) {
    for (const [field, value] of Object.entries(fields)) {
      const kind = scanSecret(value, this.known);
      if (kind) throw new ToolError("refused_secret", `refusing to post:  the ${field} looks like it contains ${kind}`);
    }
  }

  /** cli.py _resolve_peers, one name at a time, so a refusal names the position, never a member. */
  async resolveTo(names) {
    const mentions = [];
    const labels = [];
    if (!names.length) return { mentions, labels };
    const active = (await this.users()).filter((u) => u.is_active !== false && u.full_name);
    names.forEach((raw, index) => {
      const needle = raw.trim().toLowerCase();
      let found = active.filter(
        (u) => String(u.full_name).toLowerCase() === needle || [String(u.email ?? "").toLowerCase(), String(u.delivery_email ?? "").toLowerCase()].includes(needle),
      );
      if (!found.length) found = active.filter((u) => u.is_bot && seatTagFor(u).toLowerCase() === needle);
      if (!found.length) throw new ToolError("invalid_argument", `to[${index}] matches no user or bot; use an exact full name, an email or a seat tag such as CODEX`);
      if (found.length > 1) throw new ToolError("invalid_argument", `to[${index}] matches more than one user; use the email or the seat tag`);
      const user = found[0];
      const sameName = active.filter((u) => String(u.full_name).toLowerCase() === String(user.full_name).toLowerCase()).length;
      mentions.push(sameName === 1 ? `@**${user.full_name}**` : `@**${user.full_name}|${user.user_id}**`);
      labels.push(seatTagFor(user));
    });
    return { mentions, labels };
  }

  async idemKey(explicit, parts) {
    if (explicit) return { key: `k:${explicit}`, ttlMs: IDEM_EXPLICIT_TTL_MS };
    return { key: `i:${await sha256Hex(stableJson(parts))}`, ttlMs: IDEM_IMPLICIT_TTL_MS };
  }

  /** Look for this bot's earlier attempt in its topic, no sooner than 15 seconds after it. */
  async reconcile(row) {
    const attempt = Number(row.ts) || 0;
    const wait = attempt + RECONCILE_DELAY_MS - this.now();
    if (wait > 0) await this.sleep(Math.min(wait, RECONCILE_DELAY_MS));
    const messages = await this.page([{ operator: "channel", operand: row.channel_id }, { operator: "topic", operand: String(row.topic ?? "") }], null, 100);
    for (const m of [...messages].reverse()) {
      if (
        String(m.sender_email ?? "").toLowerCase() === this.email &&
        (await sha256Hex(String(m.content ?? "").trim())) === row.body_sha &&
        Number(m.timestamp ?? 0) >= attempt / 1000 - RECONCILE_WINDOW_S
      ) {
        return m.id;
      }
    }
    return null;
  }

  async deliver({ key, ttlMs }, { content, channelId, topic, check }) {
    const fields = { body_sha: await sha256Hex(content.trim()), channel_id: channelId, topic };
    const begun = await this.gate.idemBegin({ key, ttlMs, fields });
    if (begun.verdict === "busy") {
      throw new ToolError("rate_limited", "the same write is already in flight in another chat of this seat", { retryable: true, retryAfterS: 30 });
    }
    if (begun.verdict === "duplicate") return { id: begun.row.id, channel_id: begun.row.channel_id ?? channelId, duplicate: true };
    if (begun.verdict === "reconcile") {
      let found;
      try {
        found = await this.reconcile(begun.row);
      } catch (error) {
        await this.gate.idemSet({ key, changes: { state: "unknown" } });
        throw error;
      }
      if (found !== null) {
        await this.gate.idemSet({ key, changes: { state: "sent", id: found } });
        return { id: found, channel_id: begun.row.channel_id ?? channelId, duplicate: true };
      }
      await this.gate.idemSet({ key, changes: fields });
    }
    try {
      await this.reserve("write");
    } catch (error) {
      await this.gate.idemDrop({ key });
      throw error;
    }
    const attempt = this.now();
    let messageId;
    try {
      const result = await this.zulip.post("messages", { type: "stream", to: channelId, topic, content });
      messageId = result.id;
      if (!isInt(messageId)) throw new ToolError("internal", "Zulip returned no message id");
    } catch (error) {
      const mapped = mapException(error, { write: true, sent: true });
      if (mapped.code === "outcome_unknown") {
        await this.gate.idemSet({ key, changes: { state: "unknown", ts: attempt } });
        mapped.check = { ...check };
        mapped.message += "  Check with read_topic (include_self true) before posting again, or retry with the same idempotency_key:  the server looks for the first attempt before it resends.";
      } else {
        await this.gate.idemDrop({ key });
        if (mapped.code === "rate_limited") await this.safely(() => this.gate.noteRateLimited({ retryAfterS: mapped.retryAfterS }));
      }
      throw mapped;
    }
    await this.gate.idemSet({ key, changes: { state: "sent", id: messageId, channel_id: channelId } });
    return { id: messageId, channel_id: channelId, duplicate: false };
  }

  writeResult(result) {
    return okResult(JSON.stringify(result), result);
  }

  async compose(text, to, session) {
    const { mentions, labels } = await this.resolveTo(to);
    try {
      return sentenceGap(composeBody(tagPrefix(this.seat, sessionTag(session)), text, { labels, mentions }));
    } catch (error) {
      if (error instanceof UsageError) throw new ToolError("invalid_argument", error.message);
      throw error;
    }
  }

  async tool_post(args, audit) {
    const channel = normalizeChannel(args.channel);
    const topic = normalizeTopic(args.topic);
    // The audit keeps the topic only when it passes the scan, else its hash (spec 3.9).
    audit.topic = await this.auditTopic(topic);
    // The scan runs on everything the caller wrote, before any request (spec 1).
    this.scan({ text: args.text, topic, channel });
    const text = normalizeText(args.text);
    const channelId = this.channelId(channel);
    audit.channel_id = channelId;
    const to = [...(args.to ?? [])];
    const tag = sessionTag(args.session);
    const idem = await this.idemKey(args.idempotency_key, ["post", tag, channelId, topic.toLowerCase(), text, to]);
    audit.idem_ref = (await sha256Hex(idem.key)).slice(0, 12);
    await this.ensureSeat();
    const prior = await this.gate.idemPeek({ key: idem.key });
    if (prior) return this.writeResult({ id: prior.id, channel_id: prior.channel_id ?? channelId, duplicate: true });
    const content = await this.compose(text, to, args.session);
    audit.body_len = [...content].length;
    const result = await this.deliver(idem, { content, channelId, topic, check: { channel, topic, include_self: true } });
    audit.message_id = result.id;
    return this.writeResult(result);
  }

  async tool_reply(args, audit) {
    const messageId = args.message_id;
    this.scan({ text: args.text });
    const text = normalizeText(args.text);
    const to = [...(args.to ?? [])];
    const tag = sessionTag(args.session);
    const idem = await this.idemKey(args.idempotency_key, ["reply", tag, messageId, text, to]);
    audit.idem_ref = (await sha256Hex(idem.key)).slice(0, 12);
    await this.ensureSeat();
    const prior = await this.gate.idemPeek({ key: idem.key });
    if (prior) return this.writeResult({ id: prior.id, channel_id: prior.channel_id ?? 0, duplicate: true });
    const original = (await this.zulip.get(`messages/${messageId}`, { apply_markdown: false })).message ?? {};
    if (original.type !== "stream") throw new ToolError("invalid_argument", `message ${messageId} is a direct message; reply is channel-only`);
    if (!this.allowedStream(original)) throw new ToolError("channel_not_allowed", "That message is in a channel this seat may not use.");
    const channelId = original.stream_id;
    const topic = String(original.subject ?? original.topic ?? "");
    audit.channel_id = channelId;
    audit.topic = await this.auditTopic(topic);
    const content = await this.compose(text, to, args.session);
    audit.body_len = [...content].length;
    // The topic comes from Zulip, so the check names the message, never the topic.
    const result = await this.deliver(idem, { content, channelId, topic, check: { reply_to: messageId, include_self: true } });
    audit.message_id = result.id;
    return this.writeResult(result);
  }

  async tool_react(args, audit) {
    const messageId = args.message_id;
    const emoji = args.emoji;
    await this.ensureSeat();
    const original = (await this.zulip.get(`messages/${messageId}`, { apply_markdown: false })).message ?? {};
    if (!this.allowedStream(original)) throw new ToolError("channel_not_allowed", "That message is not in a channel this seat may use.");
    audit.channel_id = original.stream_id;
    audit.message_id = messageId;
    await this.reserve("react");
    try {
      await this.zulip.post(`messages/${messageId}/reactions`, { emoji_name: emoji });
    } catch (error) {
      // An existing reaction is a success, so a retry is safe.
      if (!(error instanceof ApiError && error.code === "REACTION_ALREADY_EXISTS")) throw mapException(error, { write: true, sent: true });
    }
    return this.writeResult({ id: messageId, emoji });
  }
}

/** Why `me` cannot serve `seat`, or null (spec 3.6). */
export function seatProblem(me, { seat, email }) {
  if (!me || typeof me !== "object") return "users/me answered nothing";
  if (me.is_bot !== true) return "the key is not a bot's";
  if (String(me.email ?? "").toLowerCase() !== email) return "the key belongs to another bot";
  if (seatTagFor(me) !== seat) return "the key signs as another seat";
  if (me.is_admin === true || me.is_owner === true || me.role !== MEMBER_ROLE) {
    return `its live Zulip role is ${isInt(me.role) ? me.role : "unknown"}; hosted seats accept member (400) only`;
  }
  return null;
}

