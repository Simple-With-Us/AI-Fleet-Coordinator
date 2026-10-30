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
  if (seat === "CURSOR") return "Cursor & Grok Bots";
  // The two Jet logins (owner decision, Oct 23 2026):  the original
  // connection is wake-only;  the new one carries Jet's normal access.
  if (seat === "JET") return "Jet-Wake";
  if (seat === "JET-CHAT") return "Jet";
  return String(seat ?? "");
}

// One fixed stylesheet, allowed by hash so the CSP can keep `default-src 'none'`.
// test/pages.test.mjs recomputes the hash, so editing the CSS without updating
// STYLE_HASH fails the build instead of shipping an unstyled page.
export const STYLE = "body{font:16px/1.5 system-ui,sans-serif;max-width:70rem;margin:2rem auto;padding:0 1rem;background:#fff;color:#1a1a1a}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}h3{font-size:1rem;margin:1.2rem 0 .4rem}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}code{background:#f3f3f3;padding:0 .2rem}.warn{background:#fff4e5;border:1px solid #f0c36d;padding:.6rem .8rem}.ok{color:#196c2e}.service-missing{color:#b00020}button{font:inherit;padding:.4rem .9rem;margin:.2rem .3rem .2rem 0}form.inline{display:inline}.bearer-warn{display:block;margin:.5rem 0;max-width:34rem}.mismatch-note{display:block;width:fit-content;max-width:100%;margin:.2rem 0 0;padding:.15rem .4rem}.bearer-form{display:block}.bearer-form .bearer-accept{display:block;margin:.2rem 0 .5rem}.status-overview{border:1px solid #bbb;border-radius:.6rem;padding:1rem;margin:1rem 0 2rem}.overview-heading{display:flex;align-items:center;justify-content:space-between;gap:1rem}.overview-heading h2{margin:.25rem 0}.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,18rem),1fr));gap:.8rem}.status-card,.error-card{border:1px solid #ddd;border-radius:.45rem;padding:.7rem .85rem;min-width:0}.status-card h3,.error-card p{margin:.2rem 0 .5rem}.status-state{font-weight:700}.state-ready{color:#196c2e}.state-attention{color:#8a4b00}.state-neutral{color:#333}.status-card dl{display:grid;grid-template-columns:minmax(6rem,auto) 1fr;gap:.2rem .7rem;margin:.4rem 0}.status-card dt{font-weight:600}.status-card dd{margin:0;overflow-wrap:anywhere}.coverage,.error-meta{color:#555;font-size:.9rem}.error-list{display:grid;gap:.55rem}.error-card header{display:flex;justify-content:space-between;gap:1rem}.error-card time{white-space:nowrap}.status-overview{overflow-wrap:anywhere}details.fold{border:1px solid #ddd;border-radius:.45rem;margin:1rem 0;padding:0 1rem}details.fold[open]{padding-bottom:1rem}details.fold>summary{cursor:pointer;padding:.6rem 0;list-style-position:outside}details.fold>summary h2{display:inline;font-size:1.05rem;margin:0}details.fold>summary .count,details.fold>summary .status-state{margin-left:.6rem;font-size:.95rem}details.status-overview{padding:0 1rem}@media(max-width:38rem){.overview-heading{align-items:flex-start;flex-direction:column}.error-card header{flex-direction:column;gap:0}.status-card dl{grid-template-columns:1fr}.status-card dd{margin-bottom:.35rem}table{display:block;overflow-x:auto;white-space:nowrap;background:linear-gradient(to right,#fff 30%,rgba(255,255,255,0)) 0 0,linear-gradient(to left,#fff 30%,rgba(255,255,255,0)) 100% 0,radial-gradient(farthest-side at 0 50%,rgba(0,0,0,.18),rgba(0,0,0,0)) 0 0,radial-gradient(farthest-side at 100% 50%,rgba(0,0,0,.18),rgba(0,0,0,0)) 100% 0;background-color:#fff;background-repeat:no-repeat;background-size:2.5rem 100%,2.5rem 100%,.9rem 100%,.9rem 100%;background-attachment:local,local,scroll,scroll}.bearer-warn,.bearer-form .bearer-accept{white-space:normal;max-width:22rem}.mismatch-note{white-space:normal;max-width:min(22rem,calc(100vw - 10rem))}}#gb-bearer-token{overflow-wrap:anywhere}";
export const STYLE_HASH = "sha256-TjS3lP/R2Wij0EyPO3ufMfyltgxi5X5DeLFkSiQhHIE=";

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
      "If you meant to connect, open Fleet MCP Admin, arm the seat, and start again within 10 minutes.",
    )}</p>`,
    { status: 403 },
  );
}

/**
 * One or more ASCII spaces after a sentence become SENTENCE_GAP, which survives HTML.
 * One-or-more on purpose: a double space can arrive collapsed to a single space by the
 * time it reaches us, and that collapse is intermittent, so a two-space-only pattern
 * silently matched nothing and rendered with no gap at all (verified 2026-10-18).
 * The lookahead and the (?<![0-9]) lookbehind are the same as every other sentence-gap
 * surface (perl, SKILL.md): only a gap that opens a sentence counts, so "e.g. tools" and
 * the "1. Item" list marker are left alone.
 *
 * Gap first, escape second, and the order matters: `escapeHtml` rewrites `"` and the
 * backtick to `&#34;`/`&#96;`, so escaping first left the lookahead unable to see either
 * admitted opener and `Done. "Next"` rendered with an ordinary space (Codex P2, fixed
 * 2026-10-20).  Gapping on the raw text is what the peer surfaces do anyway.  SENTENCE_GAP
 * is a literal U+00A0 rather than an entity, so it passes through escapeHtml unchanged.
 */
export function gapped(text) {
  return escapeHtml(String(text ?? "").replace(/(?<![0-9])([.!?])[ \t]+(?=[A-Z0-9"*`#@\[])/g, `$1${SENTENCE_GAP}`));
}

export function errorPage(title, message, status = 400) {
  return htmlResponse(title, `<h1>${escapeHtml(title)}</h1><p>${gapped(message)}</p>`, { status });
}

/**
 * The consent page (spec 3.4 step 5).  `facts` come from the library's parsed
 * request and client lookup;  every string is escaped here.
 */
export function consentPage({ clientName, clientDomain, clientId, redirectHost, loopback = false, scopes, seat, handle, replacing, whatsapp = false, eventsOnly }, headers) {
  // eventsOnly is required and must come from the request's own loaded config:  a
  // default read of the static staging set would show full-tool consent copy to a
  // wake-only seat and misstate what the owner is approving.
  if (typeof eventsOnly !== "boolean") {
    throw new Error("consentPage: eventsOnly must be passed explicitly from the loaded config");
  }
  const verified = clientDomain
    ? `Published by <strong>${escapeHtml(clientDomain)}</strong> (a client metadata document on that domain).`
    : `Registered by hand in Fleet MCP Admin.${SENTENCE_GAP}Its name is not verified.`;
  const replaceLine = replacing.length
    ? `<p class="warn">${replacing
        .map((g) => `This replaces the grant from ${escapeHtml(g.client)}, created ${escapeHtml(ownerTime(g.createdAt))}.`)
        .join("<br>")}</p>`
    : "<p>This seat has no other grant.</p>";
  const scopeBoxes = scopes
    .map((s) => `<label><input type="checkbox" name="scope" value="${escapeHtml(s)}" checked> <code>${escapeHtml(s)}</code></label>`)
    .join("<br>");
  const body = `<h1>Connect ${escapeHtml(clientName)} as ${escapeHtml(adminSeatLabel(seat))}?</h1>
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
<tr><th>Seat</th><td><label><input type="radio" name="seat" value="${escapeHtml(seat)}" form="consent" checked required> <strong>${escapeHtml(adminSeatLabel(seat))}</strong></label> (the armed seat for this app)</td></tr>
</table>
${replaceLine}
<p>${sentences(
    ...(eventsOnly
      ? ["This seat only delivers wake notifications to the app;  it cannot read or post.", "Private channels are never open to it."]
      : ["This seat reads and posts in any public channel its Zulip bot is subscribed to, as that bot, within its budgets.", "Private Zulip channels are never open to it.", "Every chat on that account will act as this seat.", ...(whatsapp ? ["It can also read and acknowledge messages in the owner's private WhatsApp inbox."] : [])]),
  )}</p>
<form id="consent" method="post" action="/authorize">
<input type="hidden" name="handle" value="${escapeHtml(handle)}">
<p>${scopeBoxes}</p>
<p><button type="submit" name="decision" value="approve">Approve</button><button type="submit" name="decision" value="deny">Deny</button></p>
</form>`;
  return htmlResponse(`Connect ${seat}`, body, { headers });
}

function gbBearerSection(rows, csrf, mintId = "") {
  if (!rows.length) return "";
  const mintIdField = mintId ? `<input type="hidden" name="mint_id" value="${escapeHtml(mintId)}">` : "";
  const body = rows
    .map((r) => {
      const st = r.status ?? {};
      const state = r.status === null ? "Unknown (diagnostic read failed)" : st.active ? `<span class="ok">Active</span>` : st.expired ? `<span class="warn">Expired</span>` : "None";
      const mcpState = st.active || st.expired ? (st.mcp_accept_recorded === false ? "No recorded /mcp acceptance (legacy); /mcp refused" : st.mcp_accept ? "also accepted on /mcp" : "Git Relay only") : "";
      const detail = st.created_at ? `minted ${escapeHtml(ownerTime(st.created_at))}, expires ${escapeHtml(ownerTime(st.expires_at))}${st.last_used_at ? `, last used ${escapeHtml(ownerTime(st.last_used_at))}` : ", never used"}` : "";
      // Wrong-secret presentations against this identity:  counts and a time,
      // never a hash (the narrowing review verdict:  make a guessing storm visible here).
      // A failed read is NOT a zero:  say so instead of hiding it (Tailnet
      // review of the metric, 2026-10-25).  Counts are approximate per
      // identity (write-throttled:  buffered increments can be lost to a DO
      // eviction, so a stored total is a lower bound) and kept only while
      // the identity stays configured;  they do not reset.
      const mm = r.mismatchesUnavailable
        ? `<br><small>wrong-secret counter read unavailable</small>`
        : r.mismatches && Number(r.mismatches.count) > 0
          ? `<small class="warn mismatch-note">${Number(r.mismatches.count)} wrong-secret attempt${Number(r.mismatches.count) === 1 ? "" : "s"}${r.mismatches.last_at ? `, last ${escapeHtml(ownerTime(r.mismatches.last_at))}` : ""} (approximate; updates at most once a minute)</small>`
          : "";
      const serviceReady = r.credentialsUnavailable
        ? `<span class="warn">Service readiness unknown (diagnostic read failed)</span>`
        : !r.credentials
          ? `<strong class="service-missing">${r.kind === "seat" ? "Missing Zulip credentials (token or bot email)" : "Missing service credentials (Zulip or Linear bundle)"}</strong><br><small>Not ready for service calls; an active bearer is not service readiness.</small>`
          : `<span>Service credentials loaded; connectivity not verified</span>`;
      const mintLabel = st.active || st.expired ? "Rotate (Mint New)" : "Mint";
      // A rotation without the box ticked drops a seat's existing /mcp access:
      // a block-level warning ABOVE the actions, before the owner commits.
      const rotateDrops = r.kind === "seat" && (st.active || st.expired) && st.mcp_accept === true
        ? `<div class="warn bearer-warn">This bearer is currently accepted on /mcp.  Rotating without the box ticked removes that:  the new bearer will be Git Relay only.</div>`
        : "";
      const mint = r.credentialsUnavailable
        ? `<span class="warn">Credential readiness unknown (diagnostic read failed)</span>`
        : !r.credentials
        ? `<span class="warn">No credentials loaded</span>`
        : r.kind === "seat"
          ? `${rotateDrops}<form class="inline bearer-form" method="post" action="/admin/action">${csrfField(csrf)}<input type="hidden" name="action" value="mint_gb_bearer"><input type="hidden" name="identity" value="${escapeHtml(r.identity)}">${mintIdField}<label class="bearer-accept"><input type="checkbox" name="mcp_accept" value="1"> Also accept this bearer on /mcp itself, in place of the consent flow</label><button type="submit">${escapeHtml(mintLabel)}</button></form>`
          : actionButton(csrf, "mint_gb_bearer", mintLabel, { identity: r.identity, ...(mintId ? { mint_id: mintId } : {}) });
      const buttons = [
        mint,
        st.active || st.expired ? actionButton(csrf, "revoke_gb_bearer", "Revoke", { identity: r.identity }) : "",
      ].join("");
      return `<tr><td><strong>${escapeHtml(r.identity)}</strong><br><small>${r.kind === "seat" ? "seat" : "Grok Bot"}</small></td><td>${state}${mcpState ? `<br><small>${mcpState}</small>` : ""}<br><small>${detail}</small>${mm}</td><td>${serviceReady}</td><td>${buttons}</td></tr>`;
    })
    .join("");
  return `<h2>Per-Identity Bearers</h2>
<p>${sentences("A bearer is one credential per identity, and every bearer is honored by Git Relay (git-relay).", "The same bearer is the identity's credential for fleet tools that verify with this Worker;  support lands per tool, starting with simple-board.", "Minting shows it once, and the server keeps only its hash.", "Rotating replaces the old one at once.", "Each lasts 90 days.")}</p>
<p>${sentences("A Grok Bot's bearer is its only credential, so it is also accepted on Fleet MCP's /mcp.", "A seat's bearer is accepted on Fleet MCP's /mcp only when minted with the box ticked;  minted unticked, Fleet MCP refuses it and Git Relay still honors it.", "Ticking the box lets that seat act without the consent screen:  it is your per-seat choice, recorded with the mint.")}</p>
<table><tr><th>Identity</th><th>Bearer</th><th>Service Readiness</th><th>Actions</th></tr>${body}</table>`;
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

/**
 * The one place a per-identity bearer is ever shown (src/gb-bearer.js).  The
 * Worker stores only the hash, so this is the single showing:  there is no
 * server-side copy to re-render.  The mint form that produced it was consumed
 * atomically with the mint (src/seat-state.js gbBearerMintChecked), so an
 * accidental refresh or resubmit cannot touch the bearer - it is acked from
 * the stored outcome instead (gbBearerMintCompletedPage).  The
 * send line states what THIS minted bearer can actually do:  a persona bearer
 * and a seat bearer minted with the acceptance reach Fleet MCP's /mcp and Git Relay;
 * a seat bearer minted without it is Git Relay only.  Both forms also note that a
 * fleet tool verifying with this Worker (simple-board is the first) uses the
 * same bearer - the /mcp acceptance does not gate that, and per-tool support
 * is stated as landing, never as already shipped.
 */
export function gbBearerRevealPage({ identity, token, expiresAt, kind = "persona", mcpAccept = true }) {
  const onMcp = kind !== "seat" || mcpAccept === true;
  const send = onMcp
    ? "Send it as an Authorization: Bearer header to Fleet MCP's /mcp URL and to Git Relay;  it acts only as this identity.  A fleet tool that verifies with this Worker (simple-board is the first) uses this same bearer as the identity's credential."
    : "Send it as an Authorization: Bearer header to Git Relay (git-relay) only;  it acts only as this identity.  Fleet MCP's /mcp refuses this bearer because it was minted without /mcp acceptance.  A fleet tool that verifies with this Worker (simple-board is the first) uses this same bearer as the identity's credential.  If this seat should act on /mcp too, mint a new one with the box ticked.";
  const body = `<h1>Bearer For ${escapeHtml(identity)}</h1>
<p class="warn">${sentences("This is the only time it is shown.", "Copy it now.", "The server keeps only its hash, so it cannot show it again.", "Refreshing or resubmitting cannot mint a replacement by accident:  this form was single-use, so a resubmit only shows this bearer's details again (never the token) and the bearer stays exactly as it is.", "To replace it on purpose, mint a new one from the admin page.")}</p>
<p><code id="gb-bearer-token">${escapeHtml(token)}</code></p>
<p>${sentences(`Expires ${ownerTime(expiresAt)}.`)}${SENTENCE_GAP}${gapped(send)}</p>
<p><a href="/admin">Back to Fleet MCP Admin</a></p>`;
  return htmlResponse(`Bearer For ${identity}`, body, { headers: { "Cache-Control": "no-store, private", Pragma: "no-cache" } });
}

/** A form whose mint already completed:  the stored outcome, never the token. */
export function gbBearerMintCompletedPage({ identity, result = null }) {
  const detail = result
    ? [`That form already minted this identity's bearer ${ownerTime(result.created_at)}.`, `It expires ${ownerTime(result.expires_at)}.`, `This is mint #${Number(result.mint_count) || 1} for the identity.`, result.mcp_accept ? "It was minted with /mcp acceptance." : "It was minted for Git Relay only (no /mcp acceptance)."]
    : ["That form already minted this identity's bearer."];
  const body = `<h1>Already Minted</h1>
<p>${sentences(...detail, "The token itself is never shown again - the server keeps only its hash.", "If you lost it, mint a new one from the admin page (that replaces it).")}</p>
<p><a href="/admin">Back to Fleet MCP Admin</a></p>`;
  return htmlResponse(`Already Minted - ${identity}`, body, { headers: { "Cache-Control": "no-store, private", Pragma: "no-cache" } });
}

/** A mint form that arrived stale, malformed, or into a full window:  refused, nothing changed. */
export function gbBearerMintRefusedPage({ identity, reason = "stale" }) {
  const why = reason === "busy"
    ? "Too many bearers were minted in a short window, so this mint was refused to protect the current one."
    : "That mint form had expired, so it was refused.";
  const next = reason === "busy"
    ? "Wait for the earlier forms to expire (one hour), then reload the admin page."
    : "Reload the admin page and mint again from the fresh form.";
  const body = `<h1>Nothing Was Minted</h1>
<p>${sentences(why, "No bearer was minted and the current one is unchanged.", next)}</p>
<p><a href="/admin">Back to Fleet MCP Admin</a></p>`;
  return htmlResponse(`Nothing Was Minted - ${identity}`, body, { headers: { "Cache-Control": "no-store, private", Pragma: "no-cache" } });
}

/** /admin (spec 3.9):  seat state, keys, grants, clients, refusals, tool calls, audit. */
export function adminPage({ gbBearers = [], mintId = "", email, seats, clients, clientsDeferred = false, clientsUnavailable = false, clientsCheckedAt = null, refusals, tokenRefusals = [], audits, calls = [], statusCalls = calls, statusErrors = [], statusAudits = audits, runtimeErrors = [], generatedAt, diagnosticsUnavailable = false, csrf, notice, manualSeats = [], infisical = null }, headers) {
  const seatRows = seats
    .map((s) => {
      const unavailable = new Set(s.unavailableFields ?? []);
      const stateUnknown = unavailable.has("state");
      const grants = s.grantsDeferred ? "Not checked (load on request)" : unavailable.has("grants") ? "Unknown (diagnostic read failed)" : s.grants.length
        ? s.grants.map((g) => `${escapeHtml(g.client)} <small>(${escapeHtml(g.scope.join(" "))}, ${escapeHtml(ownerTime(g.createdAt))})</small>`).join("<br>")
        : "none";
      const armed = stateUnknown ? "Unknown" : s.armed ? `<span class="ok">Armed until ${escapeHtml(ownerTime(s.armedUntil))}</span>` : "Not armed";
      const role = unavailable.has("role") ? "Unknown (diagnostic read failed)" : s.role ? (s.role.ok ? `role ${escapeHtml(s.role.role)}, checked ${escapeHtml(ownerTime(s.role.checked_at))}` : `<span class="warn">refused:  ${escapeHtml(s.role.reason)}</span>`) : "not checked yet";
      const key = s.keyInstalled ? `Installed<br><small>${role}</small>` : `<span class="warn">Missing</span>`;
      return `<tr><td><strong>${escapeHtml(adminSeatLabel(s.seat))}</strong></td><td>${armed}</td><td>${stateUnknown ? "Unknown" : s.paused ? "Paused" : "Live"}</td><td>${stateUnknown ? "Unknown" : escapeHtml(s.epoch)}</td><td>${key}</td><td>${grants}${s.grantsCheckedAt ? `<br><small>Checked ${escapeHtml(ownerTime(s.grantsCheckedAt))}; snapshot only</small>` : ""}<br><a href="/admin?inspect_grants=${encodeURIComponent(s.seat)}">Check This Seat's Grants</a></td><td>${[
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
    : `<tr><td colspan="5">${clientsDeferred ? "Not checked (load on request)." : clientsUnavailable ? "Unknown (diagnostic read failed)." : "No hand-registered clients."}</td></tr>`;
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
  // The runtime-secret mode and this isolate's counters (src/infisical.js, DEPLOY.md), so "required mode, zero
  // fallbacks" is checkable here.  Rendered only when the Worker passes them.
  const infisicalLine = infisical
    ? `<p${Number(infisical.fallbacks) > 0 ? ' class="warn"' : ""}>${sentences(
        `Secrets mode:  ${infisical.mode}.`,
        `Since this isolate started:  ${Number(infisical.fetched) || 0} Infisical reads, ${Number(infisical.cacheHits) || 0} cache hits and ${Number(infisical.fallbacks) || 0} fallbacks to Worker bindings.`,
        `${Number(infisical.absent) || 0} reads found an optional secret set nowhere, which every mode answers the same way, and are not fallbacks.`,
      )}</p>`
    : "";
  // Owner controls first, then everything long folded behind a summary that shows its count (AFC-347).
  const fold = (title, count, inner) =>
    `<details class="fold"><summary><h2>${escapeHtml(title)}</h2> <span class="count">${escapeHtml(count)}</span></summary>${inner}</details>`;
  const body = `<h1>Fleet MCP Admin</h1>
<p>Signed in as ${escapeHtml(email)}.${SENTENCE_GAP}A seat serves tools only with its key installed and a live member role.</p>
${notice ? `<p class="warn">${gapped(notice)}</p>` : ""}
<h2>Seats</h2>
<p>Grant and manual-client inventories load only when requested. Checks are snapshots, not live connection guarantees. <a href="/admin">Return To Default View</a> to reload without listing inventories.</p>
<table><tr><th>Seat</th><th>Arming</th><th>State</th><th>Epoch</th><th>Zulip Key</th><th>Grants</th><th>Actions</th></tr>${seatRows}</table>${infisicalLine}
${gbBearerSection(gbBearers, csrf, mintId)}
<h2>Hand-Registered Clients (Manual Forms)</h2>
<p>${sentences(
    "ChatGPT and Grok normally connect with a published client metadata document and need no manual client.",
    "Create one only for an app that asks for a client ID, or that cannot register itself.",
    "Each manual client is bound to one seat and gets that seat's redirect URIs.",
    "On a callback two seats share, the client's seat must also be the armed seat.",
  )}</p>
${manualRows ? `<h3>Manual seats</h3>\n<table><tr><th>Seat</th><th>Redirect URIs</th><th></th></tr>${manualRows}</table>` : ""}
<h3>Hand-registered clients</h3>
<p><a href="/admin?inspect_clients=1">Check Manual Clients</a>${clientsCheckedAt ? ` <small>Checked ${escapeHtml(ownerTime(clientsCheckedAt))}; snapshot only</small>` : ""}</p>
<table><tr><th>Client ID</th><th>Name</th><th>Seat</th><th>Redirect URIs</th><th></th></tr>${clientRows}</table>
${statusOverview}
${fold("Refused Authorize Requests", `${refusals.length} logged`, `<p>${sentences("Each row is a request that passed Cloudflare Access, so the signed-in email is shown.", "Only copy a redirect URI from here if the time matches your own attempt.")}</p>
<table><tr><th>When</th><th>Signed In As</th><th>Reason</th><th>Client ID</th><th>Redirect URI</th><th>Resource</th></tr>${refusalRows}</table>`)}
${fold("Refused Token Requests (Unauthenticated, Counted)", `${tokenRefusals.length} reasons`, `<p>${sentences("Anyone on the internet can send these, so they are counted per reason and client ID and kept apart from the authorize log.")}</p>
<table><tr><th>Last Seen</th><th>Count</th><th>Reason</th><th>Client ID</th></tr>${tokenRefusalRows}</table>`)}
${fold("Tool Calls", `${calls.length} calls`, `<p>${sentences("One row per call, newest first.", "Bodies, keys and tokens are never stored.", "A topic that looked like a secret shows as its hash.")}</p>
<table><tr><th>When</th><th>Seat</th><th>Tool</th><th>Outcome</th><th>Stream</th><th>Topic</th><th>Message</th><th>Ms</th><th>Grant</th><th>Network</th></tr>${callRows}</table>`)}
${fold("Audit", `${audits.length} events`, `<table><tr><th>When</th><th>Seat</th><th>Event</th><th>Detail</th></tr>${auditRows}</table>`)}`;
  return htmlResponse("Fleet MCP Admin", body, { headers });
}
