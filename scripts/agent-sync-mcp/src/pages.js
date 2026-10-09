// HTML for the consent page, the arming notice and /admin.
//
// Pure module.  Every page is static HTML with no script.  Every string that
// could come from a client (a CIMD document, a registration, a query string)
// goes through `escapeHtml`.  Copy follows FLEET-UI-COPY.md:  light theme,
// Title Case headings and buttons, U+00A0 plus a space between sentences
// (HTML collapses two ASCII spaces), 12-hour times with am or pm on Jay's
// clock and no zone abbreviation.

export const SENTENCE_GAP = "\u00a0 ";

// One fixed stylesheet, allowed by hash so the CSP can keep `default-src 'none'`.
// test/pages.test.mjs recomputes the hash, so editing the CSS without updating
// STYLE_HASH fails the build instead of shipping an unstyled page.
export const STYLE = "body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;background:#fff;color:#1a1a1a}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}code{background:#f3f3f3;padding:0 .2rem}.warn{background:#fff4e5;border:1px solid #f0c36d;padding:.6rem .8rem}.ok{color:#196c2e}button{font:inherit;padding:.4rem .9rem;margin:.2rem .3rem .2rem 0}form.inline{display:inline}";
export const STYLE_HASH = "sha256-WLgZxQAwJjcNpjZQCMQmYZWDXVFD2o7FUF7qGOsLOyc=";

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

export function pageHeaders(extra) {
  const headers = new Headers(extra);
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set("Content-Security-Policy", PAGE_CSP);
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "no-referrer");
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

/** Two ASCII spaces after a sentence become SENTENCE_GAP, which survives HTML. */
export function gapped(text) {
  return escapeHtml(text).replace(/([.!?])  (?=\S)/g, `$1${SENTENCE_GAP}`);
}

export function errorPage(title, message, status = 400) {
  return htmlResponse(title, `<h1>${escapeHtml(title)}</h1><p>${gapped(message)}</p>`, { status });
}

/**
 * The consent page (spec 3.4 step 5).  `facts` come from the library's parsed
 * request and client lookup;  every string is escaped here.
 */
export function consentPage({ clientName, clientDomain, clientId, redirectHost, scopes, seat, handle, replacing }, headers) {
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
<tr><th>Tokens Go To</th><td><strong>${escapeHtml(redirectHost)}</strong></td></tr>
<tr><th>Seat</th><td><label><input type="radio" name="seat" value="${escapeHtml(seat)}" form="consent" checked required> <strong>${escapeHtml(seat)}</strong></label> (the armed seat for this app)</td></tr>
</table>
${replaceLine}
<p>${sentences(
    "Phase 0 stub:  the only tools are hello and hello_write, and nothing is posted to Zulip.",
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

/** /admin (spec 3.9, Phase 0 subset):  seat state, grants, clients, refusals, audit. */
export function adminPage({ email, seats, clients, refusals, audits, csrf, notice, grokRedirectsConfigured }, headers) {
  const seatRows = seats
    .map((s) => {
      const grants = s.grants.length
        ? s.grants.map((g) => `${escapeHtml(g.client)} <small>(${escapeHtml(g.scope.join(" "))}, ${escapeHtml(ownerTime(g.createdAt))})</small>`).join("<br>")
        : "none";
      const armed = s.armed ? `<span class="ok">Armed until ${escapeHtml(ownerTime(s.armedUntil))}</span>` : "Not armed";
      return `<tr><td><strong>${escapeHtml(s.seat)}</strong></td><td>${armed}</td><td>${s.paused ? "Paused" : "Live"}</td><td>${escapeHtml(s.epoch)}</td><td>${grants}</td><td>${[
        actionButton(csrf, "arm", "Arm", { seat: s.seat }),
        s.armed ? actionButton(csrf, "disarm", "Disarm", { seat: s.seat }) : "",
        s.paused ? actionButton(csrf, "unpause", "Unpause", { seat: s.seat }) : actionButton(csrf, "pause", "Pause", { seat: s.seat }),
        actionButton(csrf, "revoke", "Revoke All And Bump Epoch", { seat: s.seat }),
      ].join("")}</td></tr>`;
    })
    .join("");
  const clientRows = clients.length
    ? clients
        .map(
          (c) =>
            `<tr><td><code>${escapeHtml(c.clientId)}</code></td><td>${escapeHtml(c.clientName ?? "")}</td><td>${escapeHtml((c.redirectUris ?? []).join(" "))}</td><td>${actionButton(csrf, "update_grok_client", "Sync Grok Redirects", { client_id: c.clientId })}</td></tr>`,
        )
        .join("")
    : `<tr><td colspan="4">No hand-registered clients.</td></tr>`;
  const refusalRows = refusals.length
    ? refusals
        .map(
          (r) =>
            `<tr><td>${escapeHtml(ownerTime(r.ts))}</td><td>${escapeHtml(r.where)}</td><td>${escapeHtml(r.reason)}</td><td><code>${escapeHtml(r.client_id)}</code></td><td><code>${escapeHtml(r.redirect_uri)}</code></td></tr>`,
        )
        .join("")
    : `<tr><td colspan="5">No refusals logged.</td></tr>`;
  const auditRows = audits.length
    ? audits
        .map((a) => `<tr><td>${escapeHtml(ownerTime(a.ts))}</td><td>${escapeHtml(a.seat)}</td><td>${escapeHtml(a.event)}</td><td><code>${escapeHtml(JSON.stringify(a.detail ?? {}))}</code></td></tr>`)
        .join("")
    : `<tr><td colspan="4">No events yet.</td></tr>`;
  const body = `<h1>Agent-Sync Admin</h1>
<p>Signed in as ${escapeHtml(email)}.${SENTENCE_GAP}Phase 0 stub:  no Zulip key is loaded.</p>
${notice ? `<p class="warn">${gapped(notice)}</p>` : ""}
<h2>Seats</h2>
<table><tr><th>Seat</th><th>Arming</th><th>State</th><th>Epoch</th><th>Grants</th><th>Actions</th></tr>${seatRows}</table>
<h2>Hand-Registered Clients (Grok Manual Form)</h2>
<p>${sentences(
    grokRedirectsConfigured
      ? "GROK-WEB redirect URIs are configured."
      : "GROK-WEB has no redirect URI yet, so a new client gets a placeholder redirect and Grok's first attempt is refused and logged below.",
    "Copy Grok's redirect URI from the refusal log into SEATS in wrangler.jsonc, redeploy, then press Sync Grok Redirects.",
  )}</p>
<p>${actionButton(csrf, "create_grok_client", "Create Grok Manual Client")}</p>
<table><tr><th>Client ID</th><th>Name</th><th>Redirect URIs</th><th></th></tr>${clientRows}</table>
<h2>Refused Requests</h2>
<table><tr><th>When</th><th>Where</th><th>Reason</th><th>Client ID</th><th>Redirect URI</th></tr>${refusalRows}</table>
<h2>Audit</h2>
<table><tr><th>When</th><th>Seat</th><th>Event</th><th>Detail</th></tr>${auditRows}</table>`;
  return htmlResponse("Agent-Sync Admin", body, { headers });
}
