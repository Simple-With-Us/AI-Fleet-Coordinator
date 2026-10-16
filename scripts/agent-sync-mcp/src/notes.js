// Apple Notes for hosted seats (AFC-331, docs/protocols/per-call-identity.md section 5).
//
// The Worker cannot run apple-notes-coding.sh, so notes_create, notes_update, notes_pin and notes_unpin
// always relay:  they post a JSON envelope to #notes-relay as the identity the call acts as (topic = the note
// title cut to 60 characters, the full title inside), and the Mac listener (scripts/agent_sync/notes_relay.py)
// validates it, runs the script and reacts with a check mark or a cross mark.  notes_status reads that
// reaction.  The envelope format, the title rule and the flag set are the Python side's
// (scripts/agent_sync/notes.py);  test/notes.test.mjs round-trips envelopes between the two.
//
// The tool specs are scripts/agent_sync/mcp/notes-tools.json, shared with the stdio server.  Pure module apart
// from the HostedTools methods at the bottom:  no `cloudflare:` imports.

import NOTES_JSON from "../../agent_sync/mcp/notes-tools.json" with { type: "json" };
import { z } from "zod";
import { ToolError, okResult, clean, seatTagFor } from "./contract.js";
import { parseArgs } from "./args.js";

export const RELAY_CHANNEL = "notes-relay";
export const ENVELOPE_VERSION = 1;
export const ACTIONS = Object.freeze(["create", "update", "pin", "unpin"]);
export const TOPIC_MAX = 60;
export const TITLE_MAX = 200;
export const SUMMARY_MAX = 280;
export const MARKDOWN_MAX = 500_000;
export const UPLOAD_MAX = 1024 * 1024;
export const INLINE_JSON_MAX = 8500;
export const ENVELOPE_PARSE_MAX = 20_000;
export const FLAG_NAMES = Object.freeze(["pin", "notify", "needs_owner", "summary", "pr", "html"]);
export const REFUSAL_PREFIX = "notes-relay refused ";
// Reactions the Mac listener sets (Zulip unicode codes and names):  a check mark and a cross mark.
const CHECK_NAMES = Object.freeze({
  done: ["check", "white_check_mark", "check_mark_button"],
  doneCodes: ["2705"],
  refused: ["cross_mark", "x", "negative_squared_cross_mark"],
  refusedCodes: ["274c"],
});
const REQUEST_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/;
const UPLOAD_PATH_RE = /^\/user_uploads\/[A-Za-z0-9_./-]{1,200}$/;
const TITLE_BAD_RE = /["\\\u0000-\u001f\u007f\u0085\u2028\u2029]/;
const PR_RE = /^[0-9#, ]{1,40}$/;
const FENCE_OPEN = "```json\n";
const FENCE_CLOSE = "\n```";

export const NOTES_TOOLS = Object.freeze(NOTES_JSON.tools.map((tool) => Object.freeze(tool)));
export const NOTES_WRITE_TOOLS = Object.freeze(new Set(["notes_create", "notes_update", "notes_pin", "notes_unpin"]));
export const NOTES_READ_TOOLS = Object.freeze(new Set(["notes_status"]));

// ---------------------------------------------------------------- zod schemas at the boundary
// Tool arguments (also checked against notes-tools.json by HostedTools.call), the envelope inside a relay
// message, and the Zulip message and reaction objects notes_status reads.  Strict where the shape is ours.

const optionalFlag = z.boolean().optional();
const WRITE_SHAPE = {
  title: z.string(),
  request_id: z.string().optional(),
  transport: z.enum(["auto", "relay"]).optional(),
};
export const NotesWriteArgs = z.strictObject({
  ...WRITE_SHAPE,
  markdown: z.string().max(MARKDOWN_MAX).optional(),
  html: optionalFlag,
  pin: optionalFlag,
  notify: optionalFlag,
  needs_owner: optionalFlag,
  summary: z.string().max(SUMMARY_MAX).optional(),
  pr: z.string().max(40).optional(),
});
export const NotesPinArgs = z.strictObject(WRITE_SHAPE);
export const NotesStatusArgs = z.strictObject({ request_id: z.string(), message_id: z.number().int().positive().optional() });
export const RelayEnvelope = z.strictObject({
  v: z.literal(ENVELOPE_VERSION),
  action: z.enum(ACTIONS),
  title: z.string(),
  markdown: z.string().max(MARKDOWN_MAX).optional(),
  flags: z.record(z.string(), z.unknown()).optional(),
  request_id: z.string(),
  identity: z.string().min(1),
  markdown_url: z.string().optional(),
  markdown_bytes: z.number().int().optional(),
  markdown_sha256: z.string().optional(),
});
export const ReactionObject = z.looseObject({
  user_id: z.number().int().optional(),
  user: z.looseObject({ id: z.number().int().optional() }).optional(),
  emoji_code: z.string().optional(),
  emoji_name: z.string().optional(),
});
export const RelayMessage = z.looseObject({
  id: z.number().int(),
  sender_id: z.number().int().optional(),
  sender_email: z.string().optional(),
  content: z.string().optional(),
  stream_id: z.number().int().optional(),
  subject: z.string().optional(),
  topic: z.string().optional(),
  reactions: z.array(z.unknown()).optional(),
});

const invalid = (message) => new ToolError("invalid_argument", message);
const points = (text) => [...text];

// ---------------------------------------------------------------- titles, flags, ids

export function checkTitle(raw) {
  if (typeof raw !== "string") throw invalid("argument 'title' must be a string");
  const title = raw.trim();
  if (!title) throw invalid("argument 'title' must not be empty");
  if (points(title).length > TITLE_MAX) throw invalid(`argument 'title' is ${points(title).length} characters; at most ${TITLE_MAX}`);
  if (!title.startsWith("[")) throw invalid('argument \'title\' must start with [APP, Agent], such as "[AFC, Claude] topic"');
  if (TITLE_BAD_RE.test(title)) throw invalid("argument 'title' must not hold a double quote, a backslash or a control character");
  return title;
}

export function checkFlags(raw) {
  const flags = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  for (const name of Object.keys(flags)) {
    if (!FLAG_NAMES.includes(name)) throw invalid(`unknown flag ${/^[A-Za-z0-9_]{1,40}$/.test(name) ? `'${name}'` : "(name not shown)"}; flags are ${FLAG_NAMES.join(", ")}`);
  }
  const out = {};
  for (const name of ["pin", "notify", "needs_owner", "html"]) {
    if (Object.hasOwn(flags, name)) {
      if (typeof flags[name] !== "boolean") throw invalid(`flag '${name}' must be true or false`);
      out[name] = flags[name];
    }
  }
  if (flags.summary !== undefined && flags.summary !== null) {
    const summary = flags.summary;
    if (typeof summary !== "string" || points(summary).length > SUMMARY_MAX || TITLE_BAD_RE.test(summary)) {
      throw invalid(`flag 'summary' must be text of at most ${SUMMARY_MAX} characters with no double quote, backslash or control character`);
    }
    if (summary) out.summary = summary;
  }
  if (flags.pr !== undefined && flags.pr !== null) {
    if (typeof flags.pr !== "string" || !PR_RE.test(flags.pr)) throw invalid('flag \'pr\' must be pull request numbers such as "46" or "46, 47"');
    if (flags.pr.trim()) out.pr = flags.pr.trim();
  }
  return out;
}

export function newRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(10));
  return `nr${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

export function checkRequestId(raw) {
  if (typeof raw !== "string" || !REQUEST_ID_RE.test(raw)) throw invalid("argument 'request_id' must be 8 to 64 letters, digits, - or _");
  return raw;
}

export function topicFor(title) {
  const oneLine = title.split(/\s+/).filter(Boolean).join(" ");
  const chars = points(oneLine);
  return chars.length <= TOPIC_MAX ? oneLine : `${chars.slice(0, TOPIC_MAX - 1).join("").trimEnd()}…`;
}

// ---------------------------------------------------------------- envelopes

export function makeEnvelope(action, title, markdown, flags, requestId, identity) {
  if (!ACTIONS.includes(action)) throw invalid(`action must be one of ${ACTIONS.join(", ")}`);
  return { v: ENVELOPE_VERSION, action, title: checkTitle(title), markdown, flags: checkFlags(flags), request_id: checkRequestId(requestId), identity };
}

/** One line of ASCII JSON (every non-ASCII character as \\uXXXX), as the Python side writes it. */
export function envelopeJson(envelope) {
  return JSON.stringify(envelope).replace(/[\u007f-￿]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

export const renderMessage = (envelope) => FENCE_OPEN + envelopeJson(envelope) + FENCE_CLOSE;
export const needsUpload = (envelope) => envelopeJson(envelope).length > INLINE_JSON_MAX;

export async function withUpload(envelope, { url, bytes }) {
  if (!UPLOAD_PATH_RE.test(url)) throw new ToolError("internal", "Zulip returned an upload path this relay does not accept");
  const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
  return { ...envelope, markdown: "", markdown_url: url, markdown_bytes: bytes.length, markdown_sha256: digest };
}

/** The envelope inside a relay message body, validated like notes.parse_message;  null when it is not one. */
export function parseMessage(content) {
  if (typeof content !== "string" || !content || content.length > ENVELOPE_PARSE_MAX) return null;
  const start = content.indexOf(FENCE_OPEN);
  const end = content.lastIndexOf(FENCE_CLOSE);
  if (start < 0 || end <= start) return null;
  try {
    const parsed = RelayEnvelope.safeParse(JSON.parse(content.slice(start + FENCE_OPEN.length, end)));
    if (!parsed.success) return null;
    const raw = parsed.data;
    const envelope = makeEnvelope(raw.action, raw.title, raw.markdown ?? "", raw.flags ?? {}, raw.request_id, raw.identity);
    if (raw.markdown_url === undefined && (raw.markdown_bytes !== undefined || raw.markdown_sha256 !== undefined)) return null;
    if (raw.markdown_url !== undefined) {
      const { markdown_url: url, markdown_bytes: size, markdown_sha256: digest } = raw;
      if (!UPLOAD_PATH_RE.test(url) || url.includes("..")) return null;
      if (size === undefined || size <= 0 || size > UPLOAD_MAX) return null;
      if (digest === undefined || !/^[0-9a-f]{64}$/.test(digest) || envelope.markdown) return null;
      return { ...envelope, markdown_url: url, markdown_bytes: size, markdown_sha256: digest };
    }
    return envelope;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- the HostedTools methods

export const NOTES_METHODS = {
  /** Hosted notes always relay (transport auto and relay mean the same here). */
  async notesWrite(action, rawArgs, audit) {
    const isBody = action === "create" || action === "update";
    const args = parseArgs(isBody ? NotesWriteArgs : NotesPinArgs, rawArgs);
    const requestId = args.request_id ? checkRequestId(args.request_id) : newRequestId();
    const flags = {};
    if (isBody) {
      for (const name of ["pin", "notify", "needs_owner", "summary", "pr", "html"]) {
        if (args[name] !== undefined && args[name] !== false && args[name] !== "") flags[name] = args[name];
        else if (name === "pin" && args.pin === false) flags.pin = false;
      }
    }
    let envelope = makeEnvelope(action, args.title, isBody ? (args.markdown ?? "") : "", flags, requestId, this.seat);
    audit.request_id = requestId;
    // Every caller-written text, flags included:  the summary reaches the owner's notification and the script's command line.
    this.scan({ title: envelope.title, markdown: envelope.markdown, ...(envelope.flags.summary ? { summary: envelope.flags.summary } : {}), ...(envelope.flags.pr ? { pr: envelope.flags.pr } : {}) });
    if (needsUpload(envelope)) {
      const bytes = new TextEncoder().encode(envelope.markdown);
      if (!bytes.length || bytes.length > UPLOAD_MAX) throw invalid(`the note body is ${bytes.length} bytes;  a relayed body is at most ${UPLOAD_MAX}`);
      const uploaded = await this.tool_upload({ filename: `note-${requestId}.md`, text: envelope.markdown, idempotency_key: `notes-${requestId}` }, audit);
      envelope = await withUpload(envelope, { url: String(uploaded.structuredContent?.url ?? ""), bytes });
      if (needsUpload(envelope)) throw invalid("the note's title and flags are too long for a relay message");
    }
    const posted = await this.tool_post({ channel: RELAY_CHANNEL, topic: topicFor(envelope.title), text: renderMessage(envelope), idempotency_key: `notes-${requestId}` }, audit);
    const data = posted.structuredContent ?? {};
    return this.writeResult({
      request_id: requestId,
      action,
      status: "queued",
      transport: "relay",
      message_id: Number(data.id) || 0,
      channel_id: Number(data.channel_id) || 0,
      duplicate: data.duplicate === true,
    });
  },

  tool_notes_create(args, audit) { return this.notesWrite("create", args, audit); },
  tool_notes_update(args, audit) { return this.notesWrite("update", args, audit); },
  tool_notes_pin(args, audit) { return this.notesWrite("pin", args, audit); },
  tool_notes_unpin(args, audit) { return this.notesWrite("unpin", args, audit); },

  /**
   * Is this Zulip user the relay consumer's bot?  #notes-relay is public and every fleet bot can react or reply in
   * it, so a check mark, a cross mark or a refusal reply counts only from the bot of the configured consumer seat
   * (NOTES_RELAY_CONSUMER, default CLAUDE), resolved to its user id from the realm's users.
   */
  async notesFleetBot(userId, members) {
    const user = members.find((m) => m.user_id === userId);
    if (!user || user.is_bot !== true) return false;
    return seatTagFor(user) === (this.config.notesRelayConsumer ?? "CLAUDE");
  },

  async tool_notes_status(rawArgs, audit) {
    const args = parseArgs(NotesStatusArgs, rawArgs);
    const requestId = checkRequestId(args.request_id);
    audit.request_id = requestId;
    await this.ensureSeat();
    await this.reserve("read");
    const channelId = await this.channelId(RELAY_CHANNEL);
    // Zulip's answers are boundary data too:  a message that does not parse is skipped, never read field by field.
    const asMessage = (raw) => {
      const parsed = RelayMessage.safeParse(raw);
      return parsed.success ? parsed.data : null;
    };
    const mine = (m) => m !== null && String(m.sender_email ?? "").toLowerCase() === this.email && parseMessage(m.content)?.request_id === requestId;
    let message = null;
    if (args.message_id !== undefined) {
      const one = asMessage((await this.zulip.get(`messages/${args.message_id}`, { apply_markdown: false })).message);
      if (one?.stream_id === channelId && mine(one)) message = one;
    } else {
      for (const extra of [[{ operator: "search", operand: requestId }], []]) {
        const found = (await this.page([{ operator: "channel", operand: channelId }, ...extra], null, 100)).map(asMessage);
        message = [...found].reverse().find(mine) ?? null;
        if (message) break;
      }
    }
    if (!message) throw new ToolError("not_found", `no relay request with that request_id from this identity in #${RELAY_CHANNEL}`);
    const members = await this.users();
    let done = false;
    let refused = false;
    for (const raw of message.reactions ?? []) {
      const parsed = ReactionObject.safeParse(raw);
      if (!parsed.success) continue;
      const reaction = parsed.data;
      const userId = reaction.user_id ?? reaction.user?.id;
      if (!(await this.notesFleetBot(userId, members))) continue;
      const code = reaction.emoji_code ?? "";
      const name = reaction.emoji_name ?? "";
      if (CHECK_NAMES.refusedCodes.includes(code) || CHECK_NAMES.refused.includes(name)) refused = true;
      else if (CHECK_NAMES.doneCodes.includes(code) || CHECK_NAMES.done.includes(name)) done = true;
    }
    const info = { request_id: requestId, message_id: message.id, status: refused ? "refused" : done ? "done" : "queued" };
    if (refused) {
      const prefix = REFUSAL_PREFIX + requestId;
      const replies = (await this.page([{ operator: "channel", operand: channelId }, { operator: "topic", operand: message.subject ?? message.topic ?? "" }], message.id, 30)).map(asMessage);
      for (const reply of replies) {
        if (reply === null) continue;
        const text = (reply.content ?? "").trimStart();
        if (reply.id > message.id && text.startsWith(prefix) && (await this.notesFleetBot(reply.sender_id, members))) {
          info.reason = clean(text.slice(prefix.length).replace(/^[:\s]+/, "").trim(), 300)[0];
          break;
        }
      }
    }
    return okResult(JSON.stringify(info), info);
  },
};
