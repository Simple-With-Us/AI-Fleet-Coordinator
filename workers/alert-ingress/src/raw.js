// The raw copy of a vendor alert, for the quiet #Alerts-Raw channel.
//
// #Alerts holds the distilled standing blocks and #Alerts-Log the one-line
// updates;  #Alerts-Raw keeps every delivery as the vendor sent it, the way the
// old per-vendor Zulip integrations showed it:  the vendor's own title and full
// text first, then the complete original payload in a collapsed spoiler, so
// nothing the vendor said is lost.

import { redactCredentials } from "./normalize.js";

// Zulip refuses a message longer than 10,000 characters.
const MAX_CONTENT = 9500,
  MAX_TOP = 6000;

// Vendor text is not ours to let ping people:  `@**all**` or `@**Jay**` inside
// an alert body would notify, so the mention syntax is broken with a zero-width
// space.  Silent mentions (`@_**`) and group mentions (`@*group*`, `@_*group*`)
// are broken too, for one simple rule:  no `@` may touch a `*`.
function quiet(text) {
  return String(text ?? "").replace(/@(_?)\*/g, "@​$1*");
}

const str = (value) => (typeof value === "string" ? value.trim() : "");

// Every string value is redacted before encoding:  JSON writes a newline as
// `\n`, and a token right after it has no word boundary for the shapes to
// anchor on, so redacting only the encoded text would miss it.
function redactValues(value, depth = 0) {
  if (typeof value === "string") return redactCredentials(value);
  if (depth > 12 || value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((v) => redactValues(v, depth + 1));
  return Object.fromEntries(
    Object.entries(value).map(([k, v]) => [redactCredentials(k), redactValues(v, depth + 1)]),
  );
}

// A fence longer than any backtick run in the body, so a payload cannot close
// it.  Runs are capped first, so a payload of backticks cannot make the fences
// themselves outgrow a Zulip message.
const MAX_RUN = 20;
const capRuns = (text) => text.replace(/`{20,}/g, "`".repeat(MAX_RUN - 1) + "…");
function fence(text) {
  const longest = Math.max(2, ...(String(text).match(/`+/g) || []).map((run) => run.length));
  return "`".repeat(longest + 1);
}

function datadog(payload) {
  const title = str(payload.alert_title) || str(payload.event_title) || str(payload.title);
  // Datadog wraps its Markdown body in %%% markers;  Zulip renders the inside.
  const body = (str(payload.event_msg) || str(payload.body)).replace(/^%%%\s*|\s*%%%$/g, "");
  return { title, body, link: str(payload.event_url) || str(payload.link) };
}

function betterstack(payload) {
  const a = payload?.data?.attributes || payload || {};
  const lines = [
    ["Cause", a.cause],
    ["URL", a.url],
    ["Started", a.started_at],
    ["Acknowledged", a.acknowledged_at],
    ["Resolved", a.resolved_at],
    ["Response", a.response_url],
    ["Screenshot", a.screenshot_url],
  ]
    .filter(([, value]) => str(value))
    .map(([label, value]) => `${label}:  ${str(value)}`);
  return { title: str(a.name) || "Better Stack incident", body: lines.join("\n"), link: "" };
}

function pagerduty(payload) {
  const event = payload?.event || {};
  const d = event.data || {};
  const lines = [
    ["Event", event.event_type],
    ["Status", d.status],
    ["Urgency", d.urgency],
    ["Priority", d.priority?.summary],
    ["Service", d.service?.summary],
    ["Assigned", (d.assignees || []).map((x) => x?.summary).filter(Boolean).join(", ")],
    ["By", event.agent?.summary],
  ]
    .filter(([, value]) => str(value))
    .map(([label, value]) => `${label}:  ${str(value)}`);
  const title = [d.number ? `#${d.number}` : "", str(d.title)].filter(Boolean).join(" ");
  return { title: title || "PagerDuty incident", body: lines.join("\n"), link: str(d.html_url) };
}

const RENDERERS = { datadog, betterstack, pagerduty };

/** Render one delivery for #Alerts-Raw:  vendor title and text, then the full payload. */
export function renderRaw(source, payload) {
  const name = String(source || "generic").toLowerCase();
  let head = { title: "", body: "", link: "" };
  try {
    head = (RENDERERS[name] || (() => head))(payload) || head;
  } catch {
    // A shape the renderer did not expect still gets its payload posted below.
  }
  // A credential a vendor echoed back (a token in a URL, a key in a label) is
  // redacted before anything is posted;  only the shape is replaced.
  const clean = (text) => quiet(redactCredentials(text));
  const json = capRuns(redactCredentials(JSON.stringify(redactValues(payload), null, 2)));
  const parts = [`**${clean(head.title) || `${name} alert`}**`];
  if (head.body) parts.push(clean(head.body));
  if (head.link) parts.push(clean(head.link));
  let top = parts.join("\n\n");
  if (top.length > MAX_TOP) top = `${top.slice(0, MAX_TOP)}\n... (truncated;  the payload below has it all)`;
  // Room for the spoiler header and four fences of at most MAX_RUN + 1 each.
  const room = MAX_CONTENT - top.length - 200;
  const shown = json.length > room ? `${json.slice(0, Math.max(0, room))}\n... (truncated)` : json;
  // The spoiler fence must be longer than the code fence it holds.
  const inner = fence(shown),
    outer = `${inner}\``;
  return `${top}\n\n${outer}spoiler Original ${name} payload\n${inner}json\n${shown}\n${inner}\n${outer}`;
}
