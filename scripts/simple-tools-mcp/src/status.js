// Static admin diagnostics.  This module contains no client script and only
// emits escaped values through the renderers supplied by pages.js.


// Successful reads write no call row (owner ruling 2026-11-06), so a failed
// read row can never be followed by the success that cleared it.  It counts
// as the seat's current state only this long;  after that the card says the
// last recorded call was a failed read, without calling it current.  A failed
// write stays current until a later row says otherwise, as before.
export const READ_FAILURE_CURRENT_MS = 10 * 60 * 1000;

const ERROR_GUIDANCE = {
  budget_exhausted: ["The seat reached its usage budget.", "Wait until the displayed reset time before retrying."],
  rate_limited: ["A service is rate limiting requests.", "Wait for the retry interval, then retry once."],
  invalid_argument: ["The request did not match the tool's input rules.", "Correct the tool arguments and retry."],
  insufficient_scope: ["The grant does not include the required scope.", "Reconnect this client and approve the requested scope."],
  unauthorized: ["The upstream credential was rejected.", "Refresh the credential in its source and verify the seat's connection."],
  forbidden: ["The upstream account denied this operation.", "Check the account role, grant scopes, and service permissions."],
  unavailable: ["The upstream service could not be reached.", "Retry after the service recovers; avoid repeating writes with a new idempotency key."],
  upstream_error: ["The upstream service rejected the request.", "Review the safe diagnostic and correct permissions or request data before retrying."],
  outcome_unknown: ["The request may have reached the upstream service, but completion is unknown.", "Check the upstream record before retrying; reuse the same idempotency key for writes."],
  callback_not_allowlisted: ["The callback URL is not allowed for this client.", "Use a registered callback URL or sync the manual client's redirect list."],
  callback_error: ["The event callback rejected or could not accept delivery.", "Verify the callback endpoint and its allowlisted host, then let the next event retry occur."],
  callback_timeout: ["The event callback did not respond before the delivery deadline.", "Restore the callback service and allow the eavesdropper's bounded retries to run."],
  event_verify_failed: ["The event callback host could not be verified.", "Check DNS and the public callback host, then subscribe again after verification succeeds."],
  mcp_disabled: ["The seat's MCP endpoint is disabled.", "Enable the hosted MCP endpoint for this seat before reconnecting the client."],
  not_armed: ["No matching seat was armed for this connection attempt.", "Arm the intended seat in Admin, then restart the connection within the arming window."],
  redirect_not_allowlisted: ["The redirect URL is not registered for this client.", "Sync or correct the client's redirect URL in Admin, then restart sign-in."],
  cimd_client_not_allowlisted: ["This published client metadata URL is not on the allowlist.", "Add the exact client metadata URL to the approved configuration, then restart sign-in."],
  resource_mismatch: ["The request targeted a resource that does not match this server.", "Use the configured Simple-Tools resource URL and reconnect the client."],
  not_authorized: ["The request did not carry a valid authorization for this resource.", "Reconnect the client and approve the intended seat and scopes."],
  not_configured: ["A required server credential or setting is missing.", "Restore the named configuration through the approved secret or settings source."],
  writes_disabled: ["Write tools are disabled for this deployment.", "Enable writes only if intended, then verify the seat and tool scopes."],
  paused: ["The seat is paused.", "Unpause it in Admin after confirming the seat should accept requests."],
  epoch_mismatch: ["The grant belongs to an earlier seat authorization epoch.", "Reconnect the client to obtain a grant for the current seat epoch."],
  phase_mismatch: ["The request arrived in an invalid or expired protocol phase.", "Restart the client operation from its first step."],
  expired: ["The request or authorization has expired.", "Restart the connection or request to obtain fresh authorization."],
  host_not_allowed: ["The callback host is not on the allowed-host list.", "Use an approved public HTTPS host and update the subscription configuration."],
  callback_host_not_allowed: ["The callback host is not on the allowed-host list.", "Use an approved public HTTPS host and update the subscription configuration."],
  bad_signature: ["The wake signature is invalid.", "Check the sender's signing key and the configured receiver key; never paste either key into diagnostics."],
  stale: ["The wake arrived outside its accepted time window.", "Check sender clock synchronization and send a fresh wake."],
  seat_mismatch: ["The wake names a different seat than this endpoint.", "Send the wake to the endpoint configured for the intended seat."],
  callback_url_refused: ["The callback URL failed the URL safety policy.", "Use a public HTTPS hostname without credentials, a private address, or a nonstandard port."],
  event_wake_released: ["A wake delivery attempt failed and the wake was released for retry.", "Check the eavesdropper's callback and the Zulip channel delivery, then let the scheduled retry run."],
  window_expired: ["The seat's connection window expired.", "Arm the intended seat again and immediately restart connection."],
  ambiguous_armed_seats: ["More than one seat sharing this callback is armed.", "Disarm all but the intended seat, then restart connection."],
  missing_key: ["The seat has no installed Zulip key.", "Install the seat's key through the approved credential flow, then verify connectivity."],
  role_refused: ["The Zulip account does not have an eligible member role.", "Restore the bot's member role and refresh the role check."],
};

function failure(record) {
  if (record.error_code && record.error_code !== "ok") return true;
  if (record.outcome && !["delivered", "ok", "success", "accepted"].includes(record.outcome)) return true;
  if (record.verdict && !["go", "ok", "delivered", "duplicate", "paused", "mcp_disabled"].includes(record.verdict)) return true;
  return false;
}

// The git relay's check routes (AFC-844):  kinds gr_bearer (/internal/gr-bearer-verify)
// and gr_assert (/internal/gr-assert-verify).  Their codes overlap the wake route's
// (bad_signature, stale), so they get their own guidance, keyed by the code before
// any `:` detail (grant_invalid:stale_epoch reads as grant_invalid).
const GR_CHECK_KINDS = new Set(["gr_bearer", "gr_assert"]);
const GR_GUIDANCE = {
  bad_signature: ["The relay's check call carried a wrong channel signature.", "Check that git-relay's GR_BEARER_VERIFY_KEY matches the Worker's copy in Fleet-OPS /tools;  the relay reads it once at startup, so restart it after a rotation."],
  stale: ["The relay's check call was older than the accepted window.", "Check the relay host's clock."],
  future: ["The relay's check call was dated ahead of the Worker's clock.", "Check the relay host's clock."],
  not_configured: ["The Worker's relay check key is not available.", "Restore GR_BEARER_VERIFY_KEY in Fleet-OPS /tools."],
  key_unavailable: ["The Worker could not read the relay check key.", "Check the Worker's Infisical access to Fleet-OPS /tools."],
  sign_key_unavailable: ["The Worker could not read the assertion signing key.", "Check the Worker's Infisical access to the assertion signing folder."],
  none: ["The named identity has no bearer minted.", "Mint the seat's bearer in Admin, or check that the caller names the right identity."],
  mismatch: ["The presented bearer does not match the seat's bearer.", "Install the seat's current bearer on the caller;  a rotated or wrong bearer reads this way."],
  expired: ["The bearer or assertion has expired.", "Mint a new bearer, or check the relay's latency and clock for an assertion."],
  paused: ["The seat is paused.", "Unpause it in Admin after confirming the seat should accept requests."],
  events_only: ["The seat is events-only and has no Git tools.", "Lift EVENTS_ONLY_SEATS for the seat if it should use Git."],
  unavailable: ["The seat's gate could not answer the bearer check.", "Check the SeatGate Durable Object's health and quota;  the relay retries a 503."],
  grant_invalid: ["The assertion's grant is no longer live, or its seat epoch, phase or approval is stale.", "Reconnect the client to get a fresh grant;  the detail after grant_invalid: names which check refused."],
  replayed: ["The same assertion was presented twice.", "Look for a retrying caller;  each assertion is valid for one call."],
  "assertion:bad_signature": ["The assertion was not signed with the Worker's current signing key.", "Look for a signing-key rotation between mint and check, or a forged assertion."],
  "assertion:bad_token": ["The relay presented something that is not a readable assertion.", "Check the caller's credential;  only the Worker mints assertions."],
  "assertion:not_configured": ["The Worker's assertion signing key is not available.", "Restore the signing key in its Fleet-OPS folder."],
  "assertion:stale": ["The assertion was minted longer ago than its accepted window.", "Look for a slow call between mint and check, or a Worker clock fault."],
  "assertion:future": ["The assertion was minted ahead of the Worker's clock.", "Look for a clock fault between Worker isolates."],
  bad_contract: ["The relay's assertion check call named a contract version this Worker does not speak.", "Update git-relay and the Worker together;  one of them is older than the contract the other sends."],
  bad_token: ["The relay's check call carried an unreadable assertion token.", "Check the caller's credential;  only the Worker mints assertions."],
  bad_action: ["The assertion's action claim does not match the route asked.", "Check that the relay names the action the assertion was minted for;  an assertion reused for another route reads this way."],
  bad_body_sha256: ["The assertion is bound to a different request body.", "The caller changed the body after minting, or minted for another call;  mint a fresh assertion."],
  bad_sent_at: ["The relay's check call carried an unreadable sent_at.", "Check the relay host's clock and its relay version."],
  too_large: ["The relay's check call body was larger than the route accepts.", "Look for a caller posting something other than the check contract."],
  method_not_allowed: ["The relay's check route was called with a method other than POST.", "Look for a caller that is not the git relay's own check client."],
};

function errorDetails(record) {
  const code = String(record.error_code ?? record.reason ?? record.outcome ?? record.verdict ?? "unknown");
  const grBase = code.startsWith("assertion:") ? code : code.split(":")[0];
  const grGuidance = GR_CHECK_KINDS.has(record.kind) ? GR_GUIDANCE[grBase] : undefined;
  const [cause, remedy] = grGuidance ?? ERROR_GUIDANCE[code] ?? ["The request was refused or failed.", "Review the event details and configuration, then retry only after correcting the cause."];
  return { code, cause: record.error_message || cause, remedy };
}

function formatRetry(record, e) {
  const bits = [];
  if (record.retryable === true) bits.push(`Retryable${Number.isFinite(record.retry_after_s) ? ` after ${record.retry_after_s}s` : ""}`);
  else if (record.retryable === false) bits.push("Not retryable");
  if (record.reset_at) {
    const resetMs = Number.isFinite(record.reset_at) ? record.reset_at : Date.parse(record.reset_at);
    bits.push(`Reset ${e.time(resetMs) || String(record.reset_at)}`);
  }
  if (record.retry_after_s != null && record.retryable !== true) bits.push(`Retry after ${record.retry_after_s}s`);
  return bits.join(" · ") || "No retry guidance recorded";
}

/** Show delegation only when the authenticated caller differs from the actor. */
export function callSeatLabel(record, missing = "—") {
  const caller = record.authenticated_seat ?? record.seat ?? missing;
  const actor = record.acting_identity ?? record.seat ?? caller;
  return caller !== actor ? `${actor} via ${caller}` : record.seat ?? caller;
}

// The read classification arrives as data (index.js passes HOSTED_READ_TOOLS),
// so this module stays free of the tool modules and their dependencies:  the
// visual tests load it through pages.js without the Worker's packages.
const NO_READ_TOOLS = new Set();

function seatSummary(seat, calls, diagnostics, generatedAt, diagnosticsUnavailable, e, readTools = NO_READ_TOOLS) {
  const ordered = [...calls].filter((c) => (c.authenticated_seat ?? c.seat) === seat.seat).sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
  const last = ordered[0];
  const success = ordered.find((c) => !failure(c));
  const seatDiagnostics = diagnostics.filter((c) => (c.authenticated_seat ?? c.seat) === seat.seat && Number.isFinite(c.ts));
  const error = [...ordered.filter(failure), ...seatDiagnostics].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))[0];
  const unavailableFields = new Set(seat.unavailableFields ?? []);
  const fieldUnavailable = (name) => unavailableFields.has(name) || (name === "grants" && seat.grantsDeferred === true);
  // A role the Worker has never checked is not a setup gap:  it is checked on the seat's first call (and
  // every 10 minutes after), so only a check that ran and refused counts as missing (AFC-347).
  const roleUnchecked = !fieldUnavailable("role") && !seat.role;
  const dependencies = [
    ["Zulip key", seat.keyInstalled, false],
    ["role", seat.role?.ok, fieldUnavailable("role") || roleUnchecked],
    ["grant", (seat.grants ?? []).length > 0, fieldUnavailable("grants")],
  ];
  const missing = dependencies.filter(([, ok, unavailable]) => !unavailable && !ok).map(([name]) => name);
  const readsUnknown = diagnosticsUnavailable || fieldUnavailable("state") || fieldUnavailable("role") || fieldUnavailable("grants");
  const configured = !seat.paused && missing.length === 0 && !readsUnknown;
  const hasRecent = last && Number.isFinite(generatedAt) && generatedAt - last.ts <= 24 * 60 * 60 * 1000;
  // A failed READ call row older than READ_FAILURE_CURRENT_MS cannot say the
  // seat is still failing:  the successful reads after it left no row.
  // Only call rows qualify (a refusal or wake row is never a read call).
  const staleReadFailure = (row) => (row.kind === undefined || row.kind === "Tool Call") && readTools.has(row.tool) && failure(row) && Number.isFinite(generatedAt) && generatedAt - (row.ts ?? 0) > READ_FAILURE_CURRENT_MS;
  const diagnosticAfterSuccess = seatDiagnostics.some((row) => (!success || row.ts >= success.ts) && !staleReadFailure(row));
  const currentError = hasRecent && ((failure(last) && !staleReadFailure(last)) || diagnosticAfterSuccess);
  const earlierReadFailure = hasRecent && !currentError && failure(last);
  const recentSuccess = configured && hasRecent && !currentError && !earlierReadFailure && !diagnosticsUnavailable;
  const readiness = seat.grantsDeferred && !diagnosticsUnavailable && !fieldUnavailable("state") && !fieldUnavailable("role") ? (seat.paused ? "Paused; grant not checked" : !seat.keyInstalled ? "Needs setup: Zulip key; grant not checked" : seat.role && !seat.role.ok ? "Role refused; grant not checked" : currentError ? "Recent failure; grant not checked" : "Grant not checked") : readsUnknown ? "Diagnostics unavailable" : seat.paused ? "Paused" : !configured ? `Needs setup: ${missing.join(", ")}` : currentError ? "Recent failure" : earlierReadFailure ? "Last recorded call was a failed read; successful reads are not recorded" : recentSuccess ? "Recent success" : roleUnchecked ? "Configured; role not checked yet" : "Configured; no recorded writes or failures";
  const tone = recentSuccess ? "state-ready" : earlierReadFailure && configured && !diagnosticsUnavailable ? "state-neutral" : "state-attention";
  return `<article class="status-card"><h3>${e.escape(e.seatLabel ? e.seatLabel(seat.seat) : seat.seat)}</h3><p class="status-state ${tone}">${e.escape(readiness)}</p>
<dl><dt>Seat</dt><dd>${fieldUnavailable("state") ? "Unknown" : seat.paused ? "Paused" : "Unpaused"}</dd>
<dt>Zulip Key</dt><dd>${seat.keyInstalled ? "Installed" : "Missing"}</dd>
<dt>Member Role</dt><dd>${fieldUnavailable("role") ? "Unknown" : roleUnchecked ? "Not checked yet (checked on the seat's next call)" : seat.role.ok ? `Verified (${e.escape(seat.role.role ?? "member")})` : `Refused${seat.role.reason ? `: ${e.escape(seat.role.reason)}` : ""}`}</dd>
<dt>Grant</dt><dd>${seat.grantsDeferred ? "Not checked" : fieldUnavailable("grants") ? "Unknown" : (seat.grants ?? []).length ? `${(seat.grants ?? []).length} present` : "Missing"}</dd>
<dt>Last Call</dt><dd>${last ? `${e.escape(e.time(last.ts))} · ${e.escape(last.tool ?? "unknown tool")}` : "No recorded writes or failures"}</dd>
<dt>Last Success</dt><dd>${success ? e.escape(e.time(success.ts)) : "No recorded write"}</dd>
<dt>Last Error</dt><dd>${error ? `${e.escape(e.time(error.ts))} · ${e.escape(error.error_code ?? error.outcome ?? error.verdict ?? "error")}` : "None in retained sample"}</dd>
<dt>Linear</dt><dd>${seat.linearEnabled ? "Enabled" : "Disabled"}; ${seat.linearConfigured ? "credential configured" : "credential missing"}; ${seat.linearWrites ? "writes enabled" : "writes disabled"}</dd>
<dt>Fleet Recall</dt><dd>${seat.recallConfigured ? "credential configured" : "not configured"}</dd></dl></article>`;
}

function collectErrorRows({ statusCalls, statusErrors, refusals, tokenRefusals, audits, runtimeErrors }) {
  const rows = [];
  for (const c of statusErrors) rows.push({ kind: "Tool Call", ...c });
  for (const c of statusCalls) if (failure(c)) rows.push({ kind: "Tool Call", ...c });
  for (const r of refusals) rows.push({ kind: "Authorize Refusal", ts: r.ts, seat: r.seat ?? "—", client: r.client_id, tool: r.reason, error_code: r.reason, retryable: false });
  for (const r of tokenRefusals) rows.push({ kind: "Token Refusal", ts: r.ts, seat: r.seat ?? "—", client: r.client_id, tool: r.reason, error_code: r.reason, retryable: false });
  for (const a of audits) {
    const detail = a.detail ?? {};
    const isDeliveryFailure = a.event === "event_delivery" && detail.outcome !== "delivered";
    const isWakeFailure = ["event_wake_channel_refused", "event_wake_settle_stale", "event_wake_released"].includes(a.event);
    const isTokenFailure = a.event === "token_exchange" && detail.outcome !== "ok";
    const isMcpRefusal = a.event === "mcp_refused";
    if (isDeliveryFailure || isWakeFailure || isTokenFailure || isMcpRefusal) rows.push({ kind: isTokenFailure ? "Token Exchange" : isMcpRefusal ? "MCP Refusal" : "Wake Delivery", ...detail, ts: a.ts, seat: a.seat ?? detail.seat ?? "—", client: detail.host ?? detail.sub_ref ?? detail.client ?? "—", tool: a.event, error_code: detail.reason ?? detail.outcome ?? a.event });
  }
  for (const r of runtimeErrors) rows.push({ kind: r.kind ?? "Runtime Error", ts: r.ts, seat: r.seat ?? "—", client: "—", tool: r.kind ?? "runtime", error_code: r.reason ?? "unknown", count: r.count, upstream_status: r.status, check: r.check });
  return rows;
}

function dedupeErrors(rows) {
  const byKey = new Map();
  for (const r of rows) {
    const key = [r.seat, r.ts, r.tool, r.grant_ref, r.error_code ?? r.reason].join("|");
    byKey.set(key, { ...byKey.get(key), ...r });
  }
  return [...byKey.values()];
}

function recentErrors(rows, e) {
  const deduped = [...rows];
  deduped.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
  return deduped.slice(0, 30).map((r) => {
    const detail = errorDetails(r);
    const supplemental = [
      Number.isInteger(r.upstream_status) ? `Upstream HTTP ${r.upstream_status}` : "",
      r.check ? `Check ${JSON.stringify(r.check)}` : "",
    ].filter(Boolean).join(" · ");
    return `<article class="error-card"><header><strong>${e.escape(r.kind)}</strong><time>${e.escape(e.time(r.ts) || "Unknown time")}${Number.isFinite(r.count) && r.count > 1 ? ` · ${e.escape(r.count)} occurrences` : ""}</time></header>
<p><b>Cause:</b> ${e.escape(detail.cause)}</p><p><b>Remedy:</b> ${e.escape(detail.remedy)}</p>
<p class="error-meta">${e.escape(callSeatLabel(r))} · ${e.escape(r.client ?? r.client_id ?? "—")} · ${e.escape(r.tool ?? "—")} · ${e.escape(detail.code)} · ${e.escape(formatRetry(r, e))}${supplemental ? ` · ${e.escape(supplemental)}` : ""}</p></article>`;
  }).join("") || `<p>No Recent Errors In Retained Sample.</p>`;
}

export function renderStatusOverview(data, e) {
  const { seats = [], statusCalls = [], statusErrors = [], refusals = [], tokenRefusals = [], audits = [], runtimeErrors = [], generatedAt } = data;
  const globalDiagnosticsUnavailable = Boolean(data.diagnosticsUnavailable);
  const readTools = data.readTools instanceof Set ? data.readTools : NO_READ_TOOLS;
  const diagnosticsUnavailable = Boolean(globalDiagnosticsUnavailable || seats.some((s) => s.diagnosticsUnavailable || (s.unavailableFields ?? []).length));
  const collectedErrors = dedupeErrors(collectErrorRows({ statusCalls, statusErrors, refusals, tokenRefusals, audits, runtimeErrors }));
  const noTraffic = statusCalls.length === 0;
  const recentFailures = collectedErrors.length;
  const heading = diagnosticsUnavailable ? "Diagnostics Unavailable" : recentFailures ? "Recorded Failures" : noTraffic ? "No Recorded Calls" : "Recent Calls; No Recorded Failures";
  const status = diagnosticsUnavailable
    ? `The diagnostic sample could not be loaded.${e.gap} Configuration fields do not verify service health.`
    : noTraffic
      ? `No calls appear in the retained sample.${e.gap} A quiet seat is not verified healthy.`
      : `${statusCalls.length} retained tool calls; ${recentFailures ? "failure or refusal records are present" : "no failure records are present"}.${e.gap} A successful past call does not guarantee current readiness.`;
  const seatCards = seats.map((s) => seatSummary(s, statusCalls, collectedErrors, generatedAt, globalDiagnosticsUnavailable || Boolean(s.diagnosticsUnavailable), e, readTools)).join("") || "<p>No seats are configured.</p>";
  const errors = recentErrors(collectedErrors, e);
  // Folded by default (AFC-347):  the summary line carries the verdict, so the owner sees it without scrolling.
  const tone = recentFailures || noTraffic || diagnosticsUnavailable ? "state-attention" : "state-neutral";
  return `<details id="status-overview" class="status-overview fold"><summary><h2>Status Overview</h2> <span class="status-state ${tone}">${e.escape(heading)}</span></summary><header class="overview-heading"><div><p class="status-state ${tone}">${e.escape(heading)}</p></div><form method="get" action="/admin"><button type="submit">Refresh Status</button></form></header>
<p>${e.escape(status)}${generatedAt ? `${e.gap}Generated ${e.escape(e.time(generatedAt))}.` : ""}</p>
<p class="coverage">History Coverage: latest per-seat writes and failed calls (up to 100;  successful reads are not recorded, so a seat that only reads shows no recorded calls), separately retained errors, refusal counters, and retained audit events.${e.gap} Cloudflare Access denials and client-local errors that never reach the Worker are not included.${e.gap} Detailed error messages are available only for calls recorded after diagnostic logging was added.${e.gap} Older events may not appear.</p>
<div class="status-grid">${seatCards}</div><h3>Recent Errors And Refusals</h3><div class="error-list">${errors}</div></details>`;
}
