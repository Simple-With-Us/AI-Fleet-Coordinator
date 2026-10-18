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
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: form ? new URLSearchParams(form).toString() : undefined,
  });
  if (!res.ok) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

/**
 * Post a single alert line.  Topic = service, per the fleet alerts format.
 * @returns {Promise<number|null>} the new message id
 */
export async function postEvent({ email, apiKey, channel, topic, severity, service, what, link }) {
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
 * Rewrite the standing status message for a topic.
 *
 * Creates it the first time, then PATCHes it forever after.  `statusId` is the
 * message id cached by the caller; passing a stale or wrong one would edit the
 * wrong message, so a failed PATCH falls back to posting a fresh one and
 * returning the new id.
 *
 * @returns {Promise<number|null>} the message id that now holds the status
 */
export async function writeStatus({ email, apiKey, channel, topic, content, statusId }) {
  if (statusId) {
    const patched = await zulip(`/messages/${statusId}`, {
      email,
      apiKey,
      method: "PATCH",
      form: { content, propagate_mode: "change_one" },
    });
    if (patched && patched.id) return statusId;
  }
  const created = await zulip("/messages", {
    email,
    apiKey,
    method: "POST",
    form: { type: "stream", to: channel, topic, content },
  });
  return created && created.id ? created.id : null;
}

/** Render the standing status block for a topic's current states. */
export function renderStatus({ topic, states, updatedAt }) {
  const rows = states
    .map((s) => {
      const mark = s.phase === "alerting" ? "\u25CF" : s.phase === "pending" ? "\u25CB" : "\u25CB";
      const sev = s.phase === "alerting" ? s.severity : "INFO";
      return `${mark} \`${sev}\`  \u2022  ${s.service}  \u2022  ${s.text}${s.link ? `  \u2022  ${s.link}` : ""}`;
    })
    .sort((a, b) => a.localeCompare(b));

  const firing = states.filter((s) => s.phase === "alerting").length;
  const header = firing
    ? `**${firing} firing** of ${states.length}`
    : `**All clear** (${states.length} monitored)`;

  return [
    `**Status: ${topic}**`,
    "",
    header,
    "",
    ...(rows.length ? rows : ["_no signals yet_"]),
    "",
    `_Updated ${updatedAt}.  This message is edited in place; new events post below it._`,
  ].join("\n");
}

export const __testing = { zulip, API };