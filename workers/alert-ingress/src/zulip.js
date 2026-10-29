// Zulip delivery for the shared alert ingress.
//
// Two very different jobs, and the difference is the whole point:
//
//   1. postEvent()  - a NEW one-line message, only when state genuinely changed.
//   2. writeStatus() - EDIT IN PLACE one standing message per topic, so the
//      current state of everything is readable without scrolling.
//
// Job 2 is why this Worker needs a real bot API key.  Zulip incoming-webhook
// bots can only ever create messages ("limited to only sending messages into
// Zulip"), so the edit path must go through the normal API:
//   PATCH /api/v1/messages/{message_id}
//
// Nothing secret is ever logged, and no secret is accepted from a request.

const API = "https://simplewithus.zulipchat.com/api/v1";

/** Basic-auth fetch against the Zulip API.  Returns null on any failure. */
async function zulip(path, { email, apiKey, method = "GET", form } = {}) {
  const auth = btoa(`${email}:${apiKey}`);
  const res = await fetch(`${API}${path}`, {
    method,
    signal: AbortSignal.timeout(10000),
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form ? new URLSearchParams(form).toString() : undefined,
  });
  if (!res.ok) {
    const error = new Error("Zulip rejected request");
    error.outcome =
      res.status >= 400 && res.status < 500 ? "refused" : "unknown";
    // The status code and the retry hint are safe to keep: they say what the
    // upstream answered, not what it said.  The caller needs them to decide
    // whether a refusal may be retried and how long to wait.
    error.status = res.status;
    const retryAfter = Number(res.headers?.get?.("retry-after"));
    if (Number.isInteger(retryAfter)) error.retryAfter = retryAfter;
    throw error;
  }
  try {
    const data = await res.json();
    if (
      !data ||
      typeof data !== "object" ||
      Array.isArray(data) ||
      data.result !== "success"
    )
      return null;
    if (method === "POST" && (!Number.isSafeInteger(data.id) || data.id <= 0))
      return null;
    return data;
  } catch {
    return null;
  }
}

/**
 * Post a single alert line.  Topic = service, per the fleet alerts format.
 * @returns {Promise<number|null>} the new message id
 */
export async function postEvent({
  email,
  apiKey,
  channel,
  topic,
  severity,
  service,
  what,
  link,
}) {
  const parts = [severity, service, what];
  if (link) parts.push(link);
  const data = await zulip("/messages", {
    email,
    apiKey,
    method: "POST",
    form: {
      type: "stream",
      to: channel,
      topic,
      // Two spaces between sentences in every Zulip post (owner ruling 2026-10-08).
      content: parts.filter(Boolean).join("  \u2022  "),
    },
  });
  return data && data.id ? data.id : null;
}

/**
 * Post one vendor alert as it arrived, already rendered by renderRaw().
 * @returns {Promise<number|null>} the new message id
 */
export async function postRaw({ email, apiKey, channel, topic, content }) {
  const data = await zulip("/messages", {
    email,
    apiKey,
    method: "POST",
    form: { type: "stream", to: channel, topic, content },
  });
  return data && data.id ? data.id : null;
}

/**
 * Rewrite the standing status message for a topic.
 *
 * Creates it the first time, then PATCHes it forever after.  `statusId` is the
 * message id cached by the caller; passing a stale or wrong one would edit the
 * wrong message.  Failed or ambiguous PATCHes never create another message;
 * durable caller outcomes hold the destination for explicit reconciliation.
 *
 * @returns {Promise<number|null>} the message id that now holds the status
 */
export async function writeStatus({
  email,
  apiKey,
  channel,
  topic,
  content,
  statusId,
}) {
  if (statusId) {
    const patched = await zulip(`/messages/${statusId}`, {
      email,
      apiKey,
      method: "PATCH",
      form: { content, propagate_mode: "change_one" },
    });
    // Zulip's edit endpoint answers {result:"success"} with no message id,
    // so a truthy response alone is not proof; without this check every
    // refresh fell through and posted a duplicate message.
    if (patched && patched.result === "success") return statusId;
    return null; // An ambiguous PATCH must never turn into a new POST.
  }
  const created = await zulip("/messages", {
    email,
    apiKey,
    method: "POST",
    form: { type: "stream", to: channel, topic, content },
  });
  return created && created.id ? created.id : null;
}

/**
 * The fields of a Zulip message the reconciliation contract reads.
 *
 * The worker validates the raw response with these hand-written checks instead
 * of a Zod schema because this Worker ships with no runtime dependencies.  It
 * is deliberately narrower than the response: only the fields the caller
 * consumes are required, so an unrelated addition to the Zulip API cannot
 * refuse a valid message, while a field the contract depends on can never be
 * missing or wrongly typed.
 */
const READABLE_MESSAGE_FIELDS = [
  ["id", "number"],
  ["type", "string"],
  ["subject", "string"],
  ["sender_email", "string"],
  ["content", "string"],
];

const isReadableMessage = (message) => {
  if (!message || typeof message !== "object" || Array.isArray(message)) return false;
  for (const [field, kind] of READABLE_MESSAGE_FIELDS) {
    const value = message[field];
    if (value === undefined || value === null) return false;
    if (kind === "string") {
      if (typeof value !== "string" || value === "") return false;
    } else if (!Number.isSafeInteger(value) || value <= 0) {
      return false;
    }
  }
  // The channel arrives either as the stream name or as a recipient object,
  // and either form is read, so both are accepted as long as one is present.
  const recipient = message.display_recipient;
  const hasChannelName =
    typeof recipient === "string"
      ? recipient !== ""
      : !!(
          recipient &&
          typeof recipient === "object" &&
          !Array.isArray(recipient) &&
          typeof recipient.name === "string" &&
          recipient.name !== ""
        );
  return hasChannelName;
};

/**
 * Fetch one message by id.
 *
 * A message id an operator supplies has to be proved before anything is adopted or
 * edited:  an id on its own names any message in the realm, so the caller needs to see
 * the channel, the topic and the sender to know it is the standing message it claims.
 *
 * A response that does not carry every field the contract reads is not proof of
 * anything, so it is refused here rather than handed on as a partial message:
 * a caller that read a missing field as empty would accept a message on weaker
 * evidence than it thinks it has.
 *
 * A message Zulip refuses to return (an id that does not exist, one this bot
 * cannot see) or a read that never answers is also a message that cannot be
 * read, so it comes back as null rather than as an exception.  Thrown, it
 * escaped the reconcile route and surfaced as the durable writer's generic
 * "writer unavailable", which sent the operator to the wrong subsystem.
 *
 * @returns {Promise<object|null>} the message, or null when it cannot be read
 */
export async function fetchMessage({ email, apiKey, messageId }) {
  try {
    const data = await zulip(`/messages/${messageId}`, { email, apiKey });
    return isReadableMessage(data?.msg) ? data.msg : null;
  } catch {
    return null;
  }
}

/**
 * The first line of every rendered standing block.
 *
 * `renderStatus` opens with it, so a message that starts with it was produced
 * by this worker as a standing block.  The one-line alert messages of the same
 * source are posted to the same channel and topic by the same bot and carry no
 * such header, so this is what tells a standing block apart from an alert line
 * when an operator supplies a message id to reconcile.
 */
export const STANDING_BLOCK_HEADER = "**Status: ";

/** Render the standing status block for a topic's current states. */
export function renderStatus({ topic, states, updatedAt, source }) {
  // A recovered signal is counted in the header but not drawn:  its row would
  // carry no text (a resolve clears it), and the block is read for what is
  // open, so a quiet monitor needs no line of its own.
  const rows = states
    .filter((s) => s.phase !== "ok")
    .map((s) => {
      const mark =
        s.phase === "alerting"
          ? "\u25CF"
          : s.phase === "pending"
            ? "\u25CB"
            : "\u25CB";
      const sev = s.phase === "alerting" ? s.severity : "INFO";
      return `${mark} \`${sev}\`  \u2022  ${s.service}  \u2022  ${s.text}${s.link ? `  \u2022  ${s.link}` : ""}`;
    })
    .sort((a, b) => a.localeCompare(b));

  const firing = states.filter((s) => s.phase === "alerting").length;
  const header = firing
    ? `**${firing} firing** of ${states.length}`
    : states.length
      ? `**All clear** (${states.length} monitored)`
      : "**All clear**";

  // Each source owns one standing message per topic, so the block says which
  // vendor it belongs to.
  const footer = source
    ? `_Updated ${updatedAt}.  Source: ${source}.  This message is edited in place;  each change is logged in #**Alerts-Log**, same topic._`
    : `_Updated ${updatedAt}.  This message is edited in place;  each change is logged in #**Alerts-Log**, same topic._`;

  return [
    `${STANDING_BLOCK_HEADER}${topic}**`,
    "",
    header,
    "",
    ...rows,
    "",
    footer,
  ].join("\n");
}

export const __testing = { zulip, API };
