// A fake Zulip REST API for the hosted tools, as a `fetch` function.  No
// dependencies, so it runs under CI's plain `node --test` and as the workerd
// flow's outbound service.  It implements only what the seven tools call:
// users/me, users, users/me/<stream>/topics, messages (GET with narrow and
// anchor, POST), messages/<id> and messages/<id>/reactions.

export const REALM = "https://simplewithus.zulipchat.com";
export const STREAMS = { "agent-sync": 642232, sandbox: 642167, other: 999001 };
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
    this.messages = [];
    this.reactions = new Set();
    this.requests = [];
    this.queue = []; // [{match: (method, path) => bool, respond: () => Response | throws}]
    this.fetch = this.fetch.bind(this);
  }

  get me() {
    return this.users[0];
  }

  addUser(email, fullName, { isBot = false } = {}) {
    const user = { user_id: 1213000 + this.users.length, email, full_name: fullName, is_bot: isBot, is_active: true, role: 400 };
    this.users.push(user);
    return user;
  }

  /** A message from `sender` (a full name or a user object);  `channel` "DM" for a direct message to the bot. */
  addMessage(sender, channel, topic, content, { client = "website", dm = false } = {}) {
    const user = typeof sender === "string" ? this.users.find((u) => u.full_name === sender) ?? this.addUser(`${sender.toLowerCase().replace(/\W+/g, "")}@zulip.test`, sender) : sender;
    const mentioned = content.includes(`@**${this.me.full_name}**`);
    const message = {
      id: this.nextId++,
      sender_id: user.user_id,
      sender_email: user.email,
      sender_full_name: user.full_name,
      client,
      timestamp: this.time++,
      content,
      type: dm ? "private" : "stream",
      ...(dm ? { display_recipient: [{ id: this.me.user_id, email: this.me.email }] } : { stream_id: STREAMS[channel], display_recipient: channel }),
      subject: dm ? "" : topic,
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
    const params = Object.fromEntries(method === "GET" ? url.searchParams : new URLSearchParams(await request.text()));
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
    let m = /^users\/me\/(\d+)\/topics$/.exec(path);
    if (method === "GET" && m) {
      const sid = Number(m[1]);
      const byName = new Map();
      for (const msg of this.messages) {
        if (msg.type === "stream" && msg.stream_id === sid) byName.set(msg.subject, Math.max(byName.get(msg.subject) ?? 0, msg.id));
      }
      return this.ok({ topics: [...byName].map(([name, max_id]) => ({ name, max_id })) });
    }
    if (method === "GET" && path === "messages") return this.ok({ messages: this.select(params).map((x) => ({ ...x })) });
    m = /^messages\/(\d+)$/.exec(path);
    if (method === "GET" && m) {
      const msg = this.messages.find((x) => x.id === Number(m[1]));
      return msg ? this.ok({ message: { ...msg } }) : this.error(400, "BAD_REQUEST");
    }
    if (method === "POST" && path === "messages") {
      const sid = Number(params.to);
      const name = Object.entries(STREAMS).find(([, id]) => id === sid)?.[0];
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
    return this.error(404, "NOT_FOUND");
  }

  select(params) {
    const narrow = JSON.parse(params.narrow ?? "[]");
    let rows = this.messages.filter((msg) =>
      narrow.every(({ operator, operand }) => {
        if (operator === "channel") return msg.type === "stream" && (msg.stream_id === operand || msg.display_recipient === operand);
        if (operator === "topic") return msg.type === "stream" && String(msg.subject).toLowerCase() === String(operand).toLowerCase();
        if (operator === "is" && operand === "mentioned") return msg.flags.includes("mentioned");
        throw new Error(`fake zulip:  unsupported narrow ${operator}`);
      }),
    );
    rows.sort((a, b) => a.id - b.id);
    const before = Number(params.num_before ?? 0);
    const after = Number(params.num_after ?? 0);
    if (params.anchor === "newest") rows = rows.slice(-Math.max(before, 1));
    else {
      const anchor = Number(params.anchor);
      const include = params.include_anchor === "true";
      rows = rows.filter((x) => x.id > anchor || (include && x.id === anchor)).slice(0, after);
    }
    return rows;
  }
}
