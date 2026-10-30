// A fake Zulip REST API for the hosted tools, as a `fetch` function.  No
// dependencies, so it runs under CI's plain `node --test` and as the workerd
// flow's outbound service.  It implements only what the seven tools call:
// users/me, users, users/me/subscriptions, users/me/<stream>/topics, messages
// (GET with narrow and anchor, POST), messages/<id> and
// messages/<id>/reactions.  Streams:  agent-sync, sandbox and design are
// public;  other (999001) is private, and every bot is subscribed to all four,
// so "other" stands for a private stream a bot is in.  Direct messages
// are real-Zulip shaped:  `display_recipient` lists every person in the
// conversation, the sender included;  `is:dm` and `dm:[ids]` narrows work;  a
// caller sees only the DMs it is in;  a POST of type direct takes a JSON array
// of user ids;  a message carries the "read" flag once read (a bot's own sends
// are born read).

export const REALM = "https://simplewithus.zulipchat.com";
// The realm renamed the fleet's coordination channel;  the worker resolves its default channel
// from the shared tool contract, so the fake carries the live name (and keeps the old one as the
// same stream so fixtures written against it still address the same place).
// The stream ids are synthetic on purpose:  a fake must not carry a production resource id, and
// nothing here depends on the number matching the realm.
export const DEFAULT_CHANNEL = "work-in-progress";
export const STREAMS = { [DEFAULT_CHANNEL]: 900001, "agent-sync": 900001, sandbox: 900002, other: 999001, design: 700001 };
export const PRIVATE_STREAMS = Object.freeze([STREAMS.other]);
export const OWNER_ID = 1211974;
export const BOT_EMAIL = "grok-web-bot@simplewithus.zulipchat.com";

/** A fresh 32-character mixed-case key that is nobody's. */
export function zulipShapedKey(seed = 1) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "Aa1";
  let x = seed * 7919;
  while (out.length < 32) {
    x = (x * 1103515245 + 12345) % 2147483648;
    out += chars[x % chars.length];
  }
  return out;
}

export class FakeZulip {
  /**
   * `me` overrides the GROK-WEB bot's fields;  `bots` adds more bots with their
   * own keys ({email, full_name, key, role}), so one fake can serve two seats.
   */
  constructor({ key = zulipShapedKey(3), me = {}, bots = [] } = {}) {
    this.key = key;
    this.nextId = 5000;
    this.time = Date.parse("2026-10-09T10:00:00Z") / 1000; // seconds, the tests' clock
    this.users = [
      { user_id: 1212596, email: BOT_EMAIL, full_name: "Grok Web", is_bot: true, is_active: true, role: 400, is_admin: false, is_owner: false, ...me },
      { user_id: OWNER_ID, email: "jay@zulip.test", full_name: "Jay Wedgeworth", is_bot: false, is_active: true, role: 100 },
      { user_id: 1212001, email: "codex-bot@simplewithus.zulipchat.com", full_name: "Codex", is_bot: true, is_active: true, role: 400 },
      { user_id: 1212002, email: "cursor-bot@simplewithus.zulipchat.com", full_name: "Cursor", is_bot: true, is_active: true, role: 400 },
      { user_id: 1212003, email: "mm-bot@simplewithus.zulipchat.com", full_name: "MiniMax", is_bot: true, is_active: true, role: 400 },
    ];
    this.keys = new Map([[this.users[0].email, key]]);
    for (const [i, bot] of bots.entries()) {
      this.users.push({ user_id: 1212700 + i, is_bot: true, is_active: true, role: 400, is_admin: false, is_owner: false, ...bot, key: undefined });
      this.keys.set(bot.email, bot.key);
    }
    // Stream table:  current name, id, privacy.  `unsubscribed` holds ids no bot is in.
    this.streams = Object.entries(STREAMS).map(([name, stream_id]) => ({ stream_id, name, invite_only: PRIVATE_STREAMS.includes(stream_id) }));
    this.unsubscribed = new Set();
    this.messages = [];
    this.reactions = new Set();
    // AFC-330:  the rest of the API the full-coverage tools use.
    this.histories = new Map();
    this.canDeleteAny = new Set(); // user ids in the realm's can_delete_any_message_group
    this.presence = new Map();
    this.statuses = [];
    this.userGroups = [];
    this.emoji = { 1: { id: "1", name: "fleet", deactivated: false, author_id: OWNER_ID }, 2: { id: "2", name: "old", deactivated: true, author_id: OWNER_ID } };
    this.uploads = [];
    this.scheduled = new Map();
    this.drafts = new Map();
    this.typing = [];
    this.userTopics = [];
    this.nextAux = 1;
    this.requests = [];
    this.queue = []; // [{match: (method, path) => bool, respond: () => Response | throws}]
    this.fetch = this.fetch.bind(this);
  }

  get me() {
    return this.users[0];
  }

  /** A stream id from its current name (or an old STREAMS key). */
  streamId(name) {
    return this.streams.find((s) => s.name === name)?.stream_id ?? STREAMS[name];
  }

  /** Add a stream ({name, stream_id, invite_only}). */
  addStream(name, streamId, { inviteOnly = false } = {}) {
    this.streams.push({ stream_id: streamId, name, invite_only: inviteOnly });
    return streamId;
  }

  /** Rename a stream in place:  the id stays, its messages show the new name. */
  renameStream(streamId, name) {
    const stream = this.streams.find((s) => s.stream_id === streamId);
    stream.name = name;
    for (const msg of this.messages) if (msg.type === "stream" && msg.stream_id === streamId) msg.display_recipient = name;
  }

  addUser(email, fullName, { isBot = false } = {}) {
    const user = { user_id: 1213000 + this.users.length, email, full_name: fullName, is_bot: isBot, is_active: true, role: 400 };
    this.users.push(user);
    return user;
  }

  /** A user object from a full name (created when unknown) or a user object. */
  person(who) {
    return typeof who === "string" ? this.users.find((u) => u.full_name === who) ?? this.addUser(`${who.toLowerCase().replace(/\W+/g, "")}@zulip.test`, who) : who;
  }

  /**
   * A direct message from `sender` to `others` (names or user objects;  the bot when
   * none), group or 1:1.  Unread unless `read`, and born read when the bot sent it.
   */
  addDm(sender, others, content, { client = "website", read = false } = {}) {
    const user = this.person(sender);
    const people = [user, ...(others && others.length ? others : [this.me]).map((o) => this.person(o))];
    const unique = [...new Map(people.map((p) => [p.user_id, p])).values()];
    const mentioned = content.includes(`@**${this.me.full_name}**`);
    const message = {
      id: this.nextId++,
      sender_id: user.user_id,
      sender_email: user.email,
      sender_full_name: user.full_name,
      client,
      timestamp: this.time++,
      content,
      type: "private",
      display_recipient: unique.map((p) => ({ id: p.user_id, email: p.email, full_name: p.full_name })),
      subject: "",
      flags: [...(mentioned ? ["mentioned"] : []), ...(read || user === this.me ? ["read"] : [])],
    };
    this.messages.push(message);
    return message;
  }

  /** A message from `sender` (a full name or a user object);  `channel` "DM" for a direct message to the bot. */
  addMessage(sender, channel, topic, content, { client = "website", dm = false } = {}) {
    if (dm) return this.addDm(sender, [], content, { client });
    const user = this.person(sender);
    const mentioned = content.includes(`@**${this.me.full_name}**`);
    const message = {
      id: this.nextId++,
      sender_id: user.user_id,
      sender_email: user.email,
      sender_full_name: user.full_name,
      client,
      timestamp: this.time++,
      content,
      type: "stream",
      stream_id: this.streamId(channel),
      display_recipient: channel,
      subject: topic,
      flags: mentioned ? ["mentioned"] : [],
    };
    this.messages.push(message);
    return message;
  }

  /** Answer the next request matching `method` and `path` with `respond()` instead of the normal handler. */
  failNext(method, path, respond) {
    this.queue.push({ method, path, respond });
  }

  requestsTo(method, path) {
    return this.requests.filter((r) => r.method === method && r.path === path);
  }

  json(status, body, headers = {}) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
  }

  ok(body = {}) {
    return this.json(200, { result: "success", msg: "", ...body });
  }

  error(status, code, extra = {}) {
    return this.json(status, { result: "error", msg: "fake error naming #secret-channel > secret topic", code, ...extra });
  }

  async fetch(input, init = {}) {
    // Miniflare hands the outbound service its own Request class, so test the shape, not the class.
    const request = typeof input === "string" || input instanceof URL ? new Request(input, init) : input;
    const url = new URL(request.url);
    if (url.origin !== REALM || !url.pathname.startsWith("/api/v1/")) return new Response("blocked in test", { status: 599 });
    const path = url.pathname.slice("/api/v1/".length);
    const method = request.method;
    let params;
    if (method === "GET") params = Object.fromEntries(url.searchParams);
    else if (String(request.headers.get("content-type") ?? "").startsWith("multipart/form-data")) {
      const form = await request.formData();
      const file = form.get("filename");
      params = { _file: file ? { name: file.name, type: file.type, bytes: new Uint8Array(await file.arrayBuffer()) } : null };
    } else params = Object.fromEntries(new URLSearchParams(await request.text()));
    const entry = { method, path, params, redirect: request.redirect, authorization: request.headers.get("authorization") };
    this.requests.push(entry);
    const queued = this.queue.findIndex((q) => q.method === method && (q.path === path || (q.path instanceof RegExp && q.path.test(path))));
    if (queued >= 0) {
      const [q] = this.queue.splice(queued, 1);
      return q.respond(entry);
    }
    const caller = this.users.find((u) => this.keys.has(u.email) && entry.authorization === `Basic ${btoa(`${u.email}:${this.keys.get(u.email)}`)}`);
    if (!caller) return this.error(401, "UNAUTHORIZED");
    entry.caller = caller.email;
    return this.route(method, path, params, caller);
  }

  route(method, path, params, caller = this.me) {
    if (method === "GET" && path === "users/me") return this.ok({ ...caller, key: undefined });
    if (method === "GET" && path === "users") return this.ok({ members: this.users.map((u) => ({ ...u, key: undefined })) });
    if (method === "GET" && path === "users/me/subscriptions") {
      return this.ok({ subscriptions: this.streams.filter((s) => !this.unsubscribed.has(s.stream_id)).map((s) => ({ ...s, color: "#c2c2c2", is_muted: false })) });
    }
    let m = /^users\/me\/(\d+)\/topics$/.exec(path);
    if (method === "GET" && m) {
      const sid = Number(m[1]);
      const byName = new Map();
      for (const msg of this.messages) {
        if (msg.type === "stream" && msg.stream_id === sid) byName.set(msg.subject, Math.max(byName.get(msg.subject) ?? 0, msg.id));
      }
      return this.ok({ topics: [...byName].map(([name, max_id]) => ({ name, max_id })) });
    }
    if (method === "GET" && path === "messages") {
      const rows = this.select(params, caller);
      return this.ok({ messages: rows.map((x) => ({ ...x })), found_newest: this.foundNewest(params, rows, caller) });
    }
    m = /^messages\/(\d+)$/.exec(path);
    if (method === "GET" && m) {
      const msg = this.messages.find((x) => x.id === Number(m[1]));
      return msg ? this.ok({ message: { ...msg } }) : this.error(400, "BAD_REQUEST");
    }
    if (method === "POST" && path === "messages" && (params.type === "direct" || params.type === "private")) {
      let ids;
      try {
        ids = JSON.parse(params.to);
      } catch {
        return this.error(400, "BAD_REQUEST");
      }
      const to = Array.isArray(ids) ? ids.map((id) => this.users.find((u) => u.user_id === id && u.is_active)) : [];
      if (!to.length || to.some((u) => !u)) return this.error(400, "BAD_REQUEST");
      const msg = this.addDm(caller, to, params.content, { client: "agent-sync-mcp" });
      return this.ok({ id: msg.id });
    }
    if (method === "POST" && path === "messages") {
      const sid = Number(params.to);
      const name = this.streams.find((s) => s.stream_id === sid)?.name;
      if (params.type !== "stream" || !name) return this.error(400, "STREAM_DOES_NOT_EXIST");
      const msg = this.addMessage(caller, name, params.topic, params.content, { client: "agent-sync-mcp" });
      return this.ok({ id: msg.id });
    }
    m = /^messages\/(\d+)\/reactions$/.exec(path);
    if (method === "POST" && m) {
      const k = `${m[1]}:${params.emoji_name}`;
      if (this.reactions.has(k)) return this.error(400, "REACTION_ALREADY_EXISTS");
      this.reactions.add(k);
      return this.ok();
    }
    if (method === "DELETE" && m) {
      const k = `${m[1]}:${params.emoji_name}`;
      if (!this.reactions.has(k)) return this.error(400, "REACTION_DOES_NOT_EXIST");
      this.reactions.delete(k);
      return this.ok();
    }
    return this.routeMore(method, path, params, caller);
  }

  /** The endpoints only the AFC-330 tools call. */
  routeMore(method, path, params, caller) {
    let m = /^messages\/(\d+)$/.exec(path);
    const find = (id) => this.messages.find((x) => x.id === Number(id));
    if (m && method === "PATCH") {
      const msg = find(m[1]);
      if (!msg) return this.error(400, "BAD_REQUEST");
      if (params.content !== undefined) {
        if (msg.sender_id !== caller.user_id) return this.error(400, "BAD_REQUEST");
        const history = this.histories.get(msg.id) ?? [{ topic: msg.subject, content: msg.content, timestamp: msg.timestamp, user_id: msg.sender_id }];
        history.push({ topic: msg.subject, content: params.content, prev_content: msg.content, timestamp: msg.timestamp + history.length, user_id: caller.user_id });
        this.histories.set(msg.id, history);
        msg.content = params.content;
      }
      if (params.topic !== undefined || params.stream_id !== undefined) {
        const mode = params.propagate_mode ?? "change_one";
        const same = this.messages.filter((x) => x.type === "stream" && x.stream_id === msg.stream_id && String(x.subject).toLowerCase() === String(msg.subject).toLowerCase());
        const moved = mode === "change_all" ? same : mode === "change_later" ? same.filter((x) => x.id >= msg.id) : [msg];
        const target = params.stream_id !== undefined ? this.streams.find((s) => s.stream_id === Number(params.stream_id)) : null;
        for (const x of moved) {
          if (params.topic !== undefined) x.subject = params.topic;
          if (target) {
            x.stream_id = target.stream_id;
            x.display_recipient = target.name;
          }
        }
      }
      return this.ok();
    }
    if (m && method === "DELETE") {
      const msg = find(m[1]);
      if (!msg || (msg.sender_id !== caller.user_id && !this.canDeleteAny.has(caller.user_id))) return this.error(400, "BAD_REQUEST");
      this.messages = this.messages.filter((x) => x !== msg);
      return this.ok();
    }
    m = /^messages\/(\d+)\/history$/.exec(path);
    if (m && method === "GET") {
      const msg = find(m[1]);
      if (!msg) return this.error(400, "BAD_REQUEST");
      return this.ok({ message_history: this.histories.get(msg.id) ?? [{ topic: msg.subject, content: msg.content, timestamp: msg.timestamp, user_id: msg.sender_id }] });
    }
    const setFlag = (msg, flag, add) => {
      const flags = new Set(msg.flags ?? []);
      if (add) flags.add(flag);
      else flags.delete(flag);
      msg.flags = [...flags];
    };
    if (method === "POST" && path === "messages/flags") {
      const ids = JSON.parse(params.messages);
      for (const id of ids) {
        const msg = find(id);
        if (msg) setFlag(msg, params.flag, params.op === "add");
      }
      return this.ok({ messages: ids });
    }
    if (method === "POST" && path === "messages/flags/narrow") {
      const narrow = JSON.parse(params.narrow);
      const hits = this.messages.filter((msg) => narrow.every(({ operator, operand }) => this.matches(msg, operator, operand, caller)));
      for (const msg of hits) setFlag(msg, params.flag, params.op === "add");
      return this.ok({ processed_count: hits.length, updated_count: hits.length, found_newest: true });
    }
    if (method === "GET" && path === "streams") {
      return this.ok({ streams: this.streams.map((s) => ({ ...s, description: `the ${s.name} channel`, is_archived: false })) });
    }
    m = /^streams\/(\d+)$/.exec(path);
    if (m && method === "GET") {
      const stream = this.streams.find((s) => s.stream_id === Number(m[1]));
      return stream ? this.ok({ stream: { ...stream, description: `the ${stream.name} channel`, history_public_to_subscribers: true, date_created: 1 } }) : this.error(400, "BAD_REQUEST");
    }
    m = /^streams\/(\d+)\/members$/.exec(path);
    if (m && method === "GET") return this.ok({ subscribers: this.users.filter((u) => u.is_active).map((u) => u.user_id) });
    if (method === "DELETE" && path === "users/me/subscriptions") {
      const names = JSON.parse(params.subscriptions);
      const removed = [];
      for (const name of names) {
        const stream = this.streams.find((s) => s.name === name);
        if (stream && !this.unsubscribed.has(stream.stream_id)) {
          this.unsubscribed.add(stream.stream_id);
          removed.push(name);
        }
      }
      return this.ok({ removed, not_removed: names.filter((n) => !removed.includes(n)) });
    }
    if (method === "POST" && path === "user_topics") {
      this.userTopics.push({ ...params });
      return this.ok();
    }
    m = /^users\/(\d+)\/presence$/.exec(path);
    if (m && method === "GET") return this.ok({ presence: this.presence.get(Number(m[1])) ?? {} });
    if (method === "POST" && path === "users/me/status") {
      this.statuses.push({ ...params });
      return this.ok();
    }
    if (method === "GET" && path === "user_groups") return this.ok({ user_groups: this.userGroups });
    if (method === "GET" && path === "realm/emoji") return this.ok({ emoji: this.emoji });
    if (method === "POST" && path === "user_uploads") {
      const file = params._file;
      if (!file) return this.error(400, "BAD_REQUEST");
      this.uploads.push(file);
      const url = `/user_uploads/2/ab/${this.uploads.length}/${file.name}`;
      return this.ok({ uri: url, url, filename: file.name });
    }
    if (method === "GET" && path === "scheduled_messages") {
      return this.ok({ scheduled_messages: [...this.scheduled].map(([id, row]) => ({ ...row, scheduled_message_id: id })) });
    }
    if (method === "POST" && path === "scheduled_messages") {
      const id = this.nextAux++;
      const direct = params.type === "direct";
      this.scheduled.set(id, { type: direct ? "private" : "stream", to: direct ? JSON.parse(params.to) : Number(params.to), topic: params.topic ?? "", content: params.content, scheduled_delivery_timestamp: Number(params.scheduled_delivery_timestamp), failed: false });
      return this.ok({ scheduled_message_id: id });
    }
    m = /^scheduled_messages\/(\d+)$/.exec(path);
    if (m && (method === "PATCH" || method === "DELETE")) {
      const id = Number(m[1]);
      const row = this.scheduled.get(id);
      if (!row) return this.error(400, "BAD_REQUEST");
      if (method === "DELETE") this.scheduled.delete(id);
      else {
        if (params.content !== undefined) row.content = params.content;
        if (params.topic !== undefined) row.topic = params.topic;
        if (params.scheduled_delivery_timestamp !== undefined) row.scheduled_delivery_timestamp = Number(params.scheduled_delivery_timestamp);
      }
      return this.ok();
    }
    if (method === "GET" && path === "drafts") return this.ok({ drafts: [...this.drafts].map(([id, d]) => ({ ...d, id })) });
    if (method === "POST" && path === "drafts") {
      const ids = JSON.parse(params.drafts).map((d) => {
        const id = this.nextAux++;
        this.drafts.set(id, d);
        return id;
      });
      return this.ok({ ids });
    }
    m = /^drafts\/(\d+)$/.exec(path);
    if (m && (method === "PATCH" || method === "DELETE")) {
      const id = Number(m[1]);
      if (!this.drafts.has(id)) return this.error(404, "BAD_REQUEST");
      if (method === "DELETE") this.drafts.delete(id);
      else this.drafts.set(id, JSON.parse(params.draft));
      return this.ok();
    }
    if (method === "POST" && path === "typing") {
      this.typing.push({ ...params });
      return this.ok();
    }
    return this.error(404, "NOT_FOUND");
  }

  select(params, caller = this.me) {
    const narrow = JSON.parse(params.narrow ?? "[]");
    const inConversation = (msg) => msg.type !== "private" || msg.display_recipient.some((p) => p.id === caller.user_id);
    let rows = this.messages.filter(
      (msg) =>
        inConversation(msg) &&
        narrow.every(({ operator, operand, negated }) => {
          const hit = this.matches(msg, operator, operand, caller);
          return negated ? !hit : hit;
        }),
    );
    rows.sort((a, b) => a.id - b.id);
    const before = Number(params.num_before ?? 0);
    const after = Number(params.num_after ?? 0);
    if (params.anchor === "newest") rows = rows.slice(-Math.max(before, 1));
    else if (params.anchor === "oldest") rows = rows.slice(0, Math.max(after, 1));
    else {
      const anchor = Number(params.anchor);
      const include = params.include_anchor === "true";
      rows = rows.filter((x) => x.id > anchor || (include && x.id === anchor)).slice(0, after);
    }
    return rows;
  }

  /** Does the page end at the newest message of the narrow?  (As Zulip reports it:  no later message matches.) */
  foundNewest(params, rows, caller) {
    if (params.anchor === "newest" || rows.length === 0) return true;
    const last = rows[rows.length - 1].id;
    const everything = this.select({ ...params, anchor: String(last), num_before: 0, num_after: 1, include_anchor: "false" }, caller);
    return everything.length === 0;
  }

  /** One narrow term against one message. */
  matches(msg, operator, operand, caller) {
    if (operator === "sender") return String(msg.sender_email).toLowerCase() === String(operand).toLowerCase();
    if (operator === "is" && operand === "dm") return msg.type === "private";
    if (operator === "dm") {
      const want = new Set([caller.user_id, ...operand]);
      const have = new Set(msg.type === "private" ? msg.display_recipient.map((p) => p.id) : []);
      return msg.type === "private" && want.size === have.size && [...want].every((id) => have.has(id));
    }
    if (operator === "channel") return msg.type === "stream" && (msg.stream_id === operand || msg.display_recipient === operand);
    if (operator === "topic") return msg.type === "stream" && String(msg.subject).toLowerCase() === String(operand).toLowerCase();
    if (operator === "is" && operand === "mentioned") return msg.flags.includes("mentioned");
    if (operator === "is" && operand === "starred") return msg.flags.includes("starred");
    if (operator === "is" && operand === "unread") return !msg.flags.includes("read");
    if (operator === "is" && operand === "resolved") return msg.type === "stream" && String(msg.subject).startsWith("✔");
    if (operator === "search") return String(operand).split(/\s+/).every((w) => String(msg.content).toLowerCase().includes(w.toLowerCase()));
    if (operator === "has" && operand === "link") return /https?:\/\//.test(msg.content);
    if (operator === "has") return false;
    throw new Error(`fake zulip:  unsupported narrow ${operator}`);
  }
}
