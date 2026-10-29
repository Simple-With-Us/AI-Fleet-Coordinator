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
// space.  Silent mentions (`@_**`) are broken too, for one simple rule.
function quiet(text) {
  return String(text ?? "").replace(/@(_?)\*\*/g, "@​$1**");
}

const str = (value) => (typeof value === "string" ? value.trim() : "");

// A fence longer than any backtick run in the body, so a payload cannot close it.
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
  const json = redactCredentials(JSON.stringify(payload, null, 2));
  const parts = [`**${clean(head.title) || `${name} alert`}**`];
  if (head.body) parts.push(clean(head.body));
  if (head.link) parts.push(redactCredentials(head.link));
  let top = parts.join("\n\n");
  if (top.length > MAX_TOP) top = `${top.slice(0, MAX_TOP)}\n... (truncated;  the payload below has it all)`;
  const room = MAX_CONTENT - top.length - 120;
  const shown = json.length > room ? `${json.slice(0, Math.max(0, room))}\n... (truncated)` : json;
  // The spoiler fence must be longer than the code fence it holds.
  const inner = fence(shown),
    outer = `${inner}\``;
  return `${top}\n\n${outer}spoiler Original ${name} payload\n${inner}json\n${shown}\n${inner}\n${outer}`;
}
