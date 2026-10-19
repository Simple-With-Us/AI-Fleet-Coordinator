// HTML for the consent page, the arming notice and /admin.
import { callSeatLabel, renderStatusOverview } from "./status.js";
//
// Pure module.  Every page is static HTML with no script.  Every string that
// could come from a client (a CIMD document, a registration, a query string)
// goes through `escapeHtml`.  Copy follows FLEET-UI-COPY.md:  light theme,
// Title Case headings and buttons, U+00A0 plus a space between sentences
// (HTML collapses two ASCII spaces), 12-hour times with am or pm on Jay's
// clock and no zone abbreviation.

export const SENTENCE_GAP = "\u00a0 ";

/** Admin display label only; stored seat identifiers and action values remain canonical. */
export function adminSeatLabel(seat) {
  return seat === "CURSOR" ? "Cursor & Grok Bots" : String(seat ?? "");
}

// One fixed stylesheet, allowed by hash so the CSP can keep `default-src 'none'`.
// test/pages.test.mjs recomputes the hash, so editing the CSS without updating
// STYLE_HASH fails the build instead of shipping an unstyled page.
export const STYLE = "body{font:16px/1.5 system-ui,sans-serif;max-width:70rem;margin:2rem auto;padding:0 1rem;background:#fff;color:#1a1a1a}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}code{background:#f3f3f3;padding:0 .2rem}.warn{background:#fff4e5;border:1px solid #f0c36d;padding:.6rem .8rem}.ok{color:#196c2e}button{font:inherit;padding:.4rem .9rem;margin:.2rem .3rem .2rem 0}form.inline{display:inline}.status-overview{border:1px solid #bbb;border-radius:.6rem;padding:1rem;margin:1rem 0 2rem}.overview-heading{display:flex;align-items:center;justify-content:space-between;gap:1rem}.overview-heading h2{margin:.25rem 0}.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,18rem),1fr));gap:.8rem}.status-card,.error-card{border:1px solid #ddd;border-radius:.45rem;padding:.7rem .85rem;min-width:0}.status-card h3,.error-card p{margin:.2rem 0 .5rem}.status-state{font-weight:700}.state-ready{color:#196c2e}.state-attention{color:#8a4b00}.state-neutral{color:#333}.status-card dl{display:grid;grid-template-columns:minmax(6rem,auto) 1fr;gap:.2rem .7rem;margin:.4rem 0}.status-card dt{font-weight:600}.status-card dd{margin:0;overflow-wrap:anywhere}.coverage,.error-meta{color:#555;font-size:.9rem}.error-list{display:grid;gap:.55rem}.error-card header{display:flex;justify-content:space-between;gap:1rem}.error-card time{white-space:nowrap}.status-overview{overflow-wrap:anywhere}details.fold{border:1px solid #ddd;border-radius:.45rem;margin:1rem 0;padding:0 1rem}details.fold[open]{padding-bottom:1rem}details.fold>summary{cursor:pointer;padding:.6rem 0;list-style-position:outside}details.fold>summary h2{display:inline;font-size:1.05rem;margin:0}details.fold>summary .count,details.fold>summary .status-state{margin-left:.6rem;font-size:.95rem}details.status-overview{padding:0 1rem}@media(max-width:38rem){.overview-heading{align-items:flex-start;flex-direction:column}.error-card header{flex-direction:column;gap:0}.status-card dl{grid-template-columns:1fr}.status-card dd{margin-bottom:.35rem}table{display:block;overflow-x:auto;white-space:nowrap}}";
export const STYLE_HASH = "sha256-VZRPtCAzyltYKL+L8GIle7XBD0E8CLUC2qn7VccECeM=";

export const PAGE_CSP = `default-src 'none'; style-src '${STYLE_HASH}'; frame-ancestors 'none'; base-uri 'none'`;

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"'`]/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** "Fri, Oct 9, 3:15am" on Jay's clock (Central), no zone abbreviation. */
export function ownerTime(ms) {
  if (!Number.isFinite(ms)) return "";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).formatToParts(new Date(ms));
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("weekday")}, ${get("month")} ${get("day")}, ${get("hour")}:${get("minute")}${get("dayPeriod").toLowerCase()}`;
}

// `same-origin`, never `no-referrer`:  a browser sends `Origin: null` on a form
// POST from a page with `no-referrer` (Fetch Standard, "append a request Origin
// header"), and the Worker refuses anything but `Origin: <issuer>`, so Jay could
// neither arm a seat nor approve a consent.  `same-origin` keeps the Origin on
// our own forms and sends no Referer (so no /authorize URL) to anyone else,
// including the redirect back to the client.  test/pages.test.mjs pins this.
export const REFERRER_POLICY = "same-origin";

/** Add the page Referrer-Policy to the headers of a redirect (it applies to the next hop). */
export function withReferrerPolicy(headers) {
  const next = new Headers(headers);
  next.set("Referrer-Policy", REFERRER_POLICY);
  return next;
}

export function pageHeaders(extra) {
  const headers = new Headers(extra);
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set("Content-Security-Policy", PAGE_CSP);
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", REFERRER_POLICY);
  headers.set("Cache-Control", "no-store");
  headers.set("X-Content-Type-Options", "nosniff");
  return headers;
}

export function page(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${escapeHtml(title)}</title><style>${STYLE}</style></head><body>${body}</body></html>`;
}

export function htmlResponse(title, body, { status = 200, headers } = {}) {
  return new Response(page(title, body), { status, headers: pageHeaders(headers) });
}

export function sentences(...parts) {
  return parts.map(escapeHtml).join(SENTENCE_GAP);
}

export function noConnectionPage() {
  return htmlResponse(
    "No Connection Expected",
    `<h1>No Connection Expected</h1><p>${sentences(
      "No seat is armed for this app right now, so nothing was granted.",
      "If you meant to connect, open Agent-Sync Admin, arm the seat, and start again within 10 minutes.",
    )}</p>`,
    { status: 403 },
  );
}

/**
 * One or more ASCII spaces after a sentence become SENTENCE_GAP, which survives HTML.
 * One-or-more on purpose: a double space can arrive collapsed to a single space by the
 * time it reaches us, and that collapse is intermittent, so a two-space-only pattern
 * silently matched nothing and rendered with no gap at all (verified 2026-10-18).
 */
export function gapped(text) {
  return escapeHtml(text).replace(/(?<![0-9])([.!?])[ \t]+(?=\S)/g, `$1${SENTENCE_GAP}`);
}

export function errorPage(title, message, status = 400) {
  return htmlResponse(title, `<h1>${escapeHtml(title)}</h1><p>${gapped(message)}</p>`, { status });
}

/**
 * The consent page (spec 3.4 step 5).  `facts` come from the library's parsed
 * request and client lookup;  every string is escaped here.
 */
export function consentPage({ clientName, clientDomain, clientId, redirectHost, loopback = false, scopes, seat, handle, replacing }, headers) {
  const verified = clientDomain
    ? `Published by <strong>${escapeHtml(clientDomain)}</strong> (a client metadata document on that domain).`
    : `Registered by hand in Agent-Sync Admin.${SENTENCE_GAP}Its name is not verified.`;
  const replaceLine = replacing.length
    ? `<p class="warn">${replacing
        .map((g) => `This replaces the grant from ${escapeHtml(g.client)}, created ${escapeHtml(ownerTime(g.createdAt))}.`)
        .join("<br>")}</p>`
    : "<p>This seat has no other grant.</p>";
  const scopeBoxes = scopes
    .map((s) => `<label><input type="checkbox" name="scope" value="${escapeHtml(s)}" checked> <code>${escapeHtml(s)}</code></label>`)
    .join("<br>");
  const body = `<h1>Connect ${escapeHtml(clientName)} as ${escapeHtml(seat)}?</h1>
<table>
<tr><th>App</th><td>${escapeHtml(clientName)}<br><small>${verified}</small></td></tr>
<tr><th>Client ID</th><td><code>${escapeHtml(clientId)}</code></td></tr>
<tr><th>Tokens Go To</th><td>${
    loopback
      ? `<strong>A program on the computer running this browser (${escapeHtml(redirectHost)})</strong><br><small class="warn">${sentences(
          redirectHost === "localhost:8787"
            ? "localhost must resolve to the computer running this browser; approval does not verify a listening callback receiver."
            : "127.0.0.1 always means this same computer.",
          "Approve only if you opened this page on the app's own computer, right after starting the connection there.",
        )}</small>`
      : `<strong>${escapeHtml(redirectHost)}</strong>`
  }</td></tr>
<tr><th>Seat</th><td><label><input type="radio" name="seat" value="${escapeHtml(seat)}" form="consent" checked required> <strong>${escapeHtml(seat)}</strong></label> (the armed seat for this app)</td></tr>
</table>
${replaceLine}
<p>${sentences(
    "This seat reads and posts in any public channel its Zulip bot is subscribed to, as that bot, within its budgets.",
    "Private channels are never open to it.",
    "Every chat on that account will act as this seat.",
  )}</p>
<form id="consent" method="post" action="/authorize">
<input type="hidden" name="handle" value="${escapeHtml(handle)}">
<p>${scopeBoxes}</p>
<p><button type="submit" name="decision" value="approve">Approve</button><button type="submit" name="decision" value="deny">Deny</button></p>
</form>`;
  return htmlResponse(`Connect ${seat}`, body, { headers });
}

function csrfField(csrf) {
  return `<input type="hidden" name="csrf" value="${escapeHtml(csrf)}">`;
}

function actionButton(csrf, action, label, fields = {}) {
  const hidden = Object.entries(fields)
    .map(([k, v]) => `<input type="hidden" name="${escapeHtml(k)}" value="${escapeHtml(v)}">`)
    .join("");
  return `<form class="inline" method="post" action="/admin/action">${csrfField(csrf)}<input type="hidden" name="action" value="${escapeHtml(action)}">${hidden}<button type="submit">${escapeHtml(label)}</button></form>`;
}

/** /admin (spec 3.9):  seat state, keys, grants, clients, refusals, tool calls, audit. */
export function adminPage({ email, seats, clients, refusals, tokenRefusals = [], audits, calls = [], statusCalls = calls, statusErrors = [], statusAudits = audits, runtimeErrors = [], generatedAt, diagnosticsUnavailable = false, csrf, notice, manualSeats = [] }, headers) {
  const seatRows = seats
    .map((s) => {
      const unavailable = new Set(s.unavailableFields ?? []);
      const stateUnknown = unavailable.has("state");
      const grants = unavailable.has("grants") ? "Unknown (diagnostic read failed)" : s.grants.length
        ? s.grants.map((g) => `${escapeHtml(g.client)} <small>(${escapeHtml(g.scope.join(" "))}, ${escapeHtml(ownerTime(g.createdAt))})</small>`).join("<br>")
        : "none";
      const armed = stateUnknown ? "Unknown" : s.armed ? `<span class="ok">Armed until ${escapeHtml(ownerTime(s.armedUntil))}</span>` : "Not armed";
      const role = unavailable.has("role") ? "Unknown (diagnostic read failed)" : s.role ? (s.role.ok ? `role ${escapeHtml(s.role.role)}, checked ${escapeHtml(ownerTime(s.role.checked_at))}` : `<span class="warn">refused:  ${escapeHtml(s.role.reason)}</span>`) : "not checked yet";
      const key = s.keyInstalled ? `Installed<br><small>${role}</small>` : `<span class="warn">Missing</span>`;
      return `<tr><td><strong>${escapeHtml(adminSeatLabel(s.seat))}</strong></td><td>${armed}</td><td>${stateUnknown ? "Unknown" : s.paused ? "Paused" : "Live"}</td><td>${stateUnknown ? "Unknown" : escapeHtml(s.epoch)}</td><td>${key}</td><td>${grants}</td><td>${[
        actionButton(csrf, "arm", "Arm", { seat: s.seat }),
        s.armed ? actionButton(csrf, "disarm", "Disarm", { seat: s.seat }) : "",
        stateUnknown ? "" : s.paused ? actionButton(csrf, "unpause", "Unpause", { seat: s.seat }) : actionButton(csrf, "pause", "Pause", { seat: s.seat }),
        actionButton(csrf, "revoke", "Revoke All And Bump Epoch", { seat: s.seat }),
      ].join("")}</td></tr>`;
    })
    .join("");
  const clientRows = clients.length
    ? clients
        .map(
          (c) =>
            `<tr><td><code>${escapeHtml(c.clientId)}</code></td><td>${escapeHtml(c.clientName ?? "")}</td><td>${c.seat ? escapeHtml(adminSeatLabel(c.seat)) : `<span class="warn">none</span>`}</td><td>${escapeHtml((c.redirectUris ?? []).join(" "))}</td><td>${c.seat ? actionButton(csrf, "sync_manual_client", "Sync Redirects", { client_id: c.clientId }) : ""}</td></tr>`,
        )
        .join("")
    : `<tr><td colspan="5">No hand-registered clients.</td></tr>`;
  const manualRows = manualSeats
    .map(
      (m) =>
        `<tr><td><strong>${escapeHtml(adminSeatLabel(m.seat))}</strong></td><td>${escapeHtml(
          m.redirectsConfigured
            ? m.shared
              ? "Configured, shared with another seat of the same app"
              : "Configured"
            : "None yet (a new client gets a placeholder, so the first attempt is refused and logged below)",
        )}</td><td>${actionButton(csrf, "create_manual_client", `Create Manual Client For ${adminSeatLabel(m.seat)}`, { seat: m.seat })}</td></tr>`,
    )
    .join("");
  const refusalRows = refusals.length
    ? refusals
        .map(
          (r) =>
            `<tr><td>${escapeHtml(ownerTime(r.ts))}</td><td>${escapeHtml(r.by ?? "")}</td><td>${escapeHtml(r.reason)}</td><td><code>${escapeHtml(r.client_id)}</code></td><td><code>${escapeHtml(r.redirect_uri)}</code></td><td><code>${escapeHtml(r.resource ?? "")}</code></td></tr>`,
        )
        .join("")
    : `<tr><td colspan="6">No refusals logged.</td></tr>`;
  const tokenRefusalRows = tokenRefusals.length
    ? tokenRefusals
        .map(
          (r) =>
            `<tr><td>${escapeHtml(ownerTime(r.ts))}</td><td>${escapeHtml(r.count ?? 1)}</td><td>${escapeHtml(r.reason)}</td><td><code>${escapeHtml(r.client_id)}</code></td></tr>`,
        )
        .join("")
    : `<tr><td colspan="4">None.</td></tr>`;
  const callRows = calls.length
    ? calls
        .map(
          (c) =>
            `<tr><td>${escapeHtml(ownerTime(c.ts))}</td><td>${escapeHtml(callSeatLabel(c, ""))}</td><td>${escapeHtml(c.tool)}</td><td>${escapeHtml(c.error_code ?? "ok")}</td><td>${escapeHtml(c.channel_id ?? "")}</td><td>${escapeHtml(c.topic ?? "")}</td><td>${escapeHtml(c.message_id ?? "")}</td><td>${escapeHtml(c.latency_ms ?? "")}</td><td><code>${escapeHtml(c.grant_ref ?? "")}</code></td><td>${escapeHtml([c.asn ?? "", c.country ?? ""].join(" ").trim())}</td></tr>`,
        )
        .join("")
    : `<tr><td colspan="10">No tool calls yet.</td></tr>`;
  const auditRows = audits.length
    ? audits
        .map((a) => `<tr><td>${escapeHtml(ownerTime(a.ts))}</td><td>${escapeHtml(a.seat)}</td><td>${escapeHtml(a.event)}</td><td><code>${escapeHtml(JSON.stringify(a.detail ?? {}))}</code></td></tr>`)
        .join("")
    : `<tr><td colspan="4">No events yet.</td></tr>`;
  const statusOverview = renderStatusOverview({ seats, statusCalls, statusErrors, refusals, tokenRefusals, audits: statusAudits, runtimeErrors, generatedAt, diagnosticsUnavailable }, {
    escape: escapeHtml,
    time: ownerTime,
    gap: SENTENCE_GAP,
    seatLabel: adminSeatLabel,
  });
  // Owner controls first, then everything long folded behind a summary that shows its count (AFC-347).
  const fold = (title, count, inner) =>
    `<details class="fold"><summary><h2>${escapeHtml(title)}</h2> <span class="count">${escapeHtml(count)}</span></summary>${inner}</details>`;
  const body = `<h1>Agent-Sync Admin</h1>
<p>Signed in as ${escapeHtml(email)}.${SENTENCE_GAP}A seat serves tools only with its key installed and a live member role.</p>
${notice ? `<p class="warn">${gapped(notice)}</p>` : ""}
<h2>Seats</h2>
<table><tr><th>Seat</th><th>Arming</th><th>State</th><th>Epoch</th><th>Zulip Key</th><th>Grants</th><th>Actions</th></tr>${seatRows}</table>
<h2>Hand-Registered Clients (Manual Forms)</h2>
<p>${sentences(
    "ChatGPT and Grok normally connect with a published client metadata document and need no manual client.",
    "Create one only for an app that asks for a client ID, or that cannot register itself.",
    "Each manual client is bound to one seat and gets that seat's redirect URIs.",
    "On a callback two seats share, the client's seat must also be the armed seat.",
  )}</p>
<table><tr><th>Seat</th><th>Redirect URIs</th><th></th></tr>${manualRows}</table>
<table><tr><th>Client ID</th><th>Name</th><th>Seat</th><th>Redirect URIs</th><th></th></tr>${clientRows}</table>
${statusOverview}
${fold("Refused Authorize Requests", `${refusals.length} logged`, `<p>${sentences("Each row is a request that passed Cloudflare Access, so the signed-in email is shown.", "Only copy a redirect URI from here if the time matches your own attempt.")}</p>
<table><tr><th>When</th><th>Signed In As</th><th>Reason</th><th>Client ID</th><th>Redirect URI</th><th>Resource</th></tr>${refusalRows}</table>`)}
${fold("Refused Token Requests (Unauthenticated, Counted)", `${tokenRefusals.length} reasons`, `<p>${sentences("Anyone on the internet can send these, so they are counted per reason and client ID and kept apart from the authorize log.")}</p>
<table><tr><th>Last Seen</th><th>Count</th><th>Reason</th><th>Client ID</th></tr>${tokenRefusalRows}</table>`)}
${fold("Tool Calls", `${calls.length} calls`, `<p>${sentences("One row per call, newest first.", "Bodies, keys and tokens are never stored.", "A topic that looked like a secret shows as its hash.")}</p>
<table><tr><th>When</th><th>Seat</th><th>Tool</th><th>Outcome</th><th>Stream</th><th>Topic</th><th>Message</th><th>Ms</th><th>Grant</th><th>Network</th></tr>${callRows}</table>`)}
${fold("Audit", `${audits.length} events`, `<table><tr><th>When</th><th>Seat</th><th>Event</th><th>Detail</th></tr>${auditRows}</table>`)}`;
  return htmlResponse("Agent-Sync Admin", body, { headers });
}
