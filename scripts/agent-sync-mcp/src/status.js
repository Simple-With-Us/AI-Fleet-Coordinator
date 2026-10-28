// Static admin diagnostics.  This module contains no client script and only
// emits escaped values through the renderers supplied by pages.js.

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
  callback_timeout: ["The event callback did not respond before the delivery deadline.", "Restore the callback service and allow the listener's bounded retries to run."],
  event_verify_failed: ["The event callback host could not be verified.", "Check DNS and the public callback host, then subscribe again after verification succeeds."],
  mcp_disabled: ["The seat's MCP endpoint is disabled.", "Enable the hosted MCP endpoint for this seat before reconnecting the client."],
  not_armed: ["No matching seat was armed for this connection attempt.", "Arm the intended seat in Admin, then restart the connection within the arming window."],
  redirect_not_allowlisted: ["The redirect URL is not registered for this client.", "Sync or correct the client's redirect URL in Admin, then restart sign-in."],
  cimd_client_not_allowlisted: ["This published client metadata URL is not on the allowlist.", "Add the exact client metadata URL to the approved configuration, then restart sign-in."],
  resource_mismatch: ["The request targeted a resource that does not match this server.", "Use the configured Agent-Sync resource URL and reconnect the client."],
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
  event_wake_released: ["A wake delivery attempt failed and the wake was released for retry.", "Check the listener's callback and the Zulip channel delivery, then let the scheduled retry run."],
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

function errorDetails(record) {
  const code = String(record.error_code ?? record.reason ?? record.outcome ?? record.verdict ?? "unknown");
  const [cause, remedy] = ERROR_GUIDANCE[code] ?? ["The request was refused or failed.", "Review the event details and configuration, then retry only after correcting the cause."];
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

function seatSummary(seat, calls, diagnostics, generatedAt, diagnosticsUnavailable, e) {
  const ordered = [...calls].filter((c) => (c.authenticated_seat ?? c.seat) === seat.seat).sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0));
  const last = ordered[0];
  const success = ordered.find((c) => !failure(c));
  const seatDiagnostics = diagnostics.filter((c) => (c.authenticated_seat ?? c.seat) === seat.seat && Number.isFinite(c.ts));
  const error = [...ordered.filter(failure), ...seatDiagnostics].sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0))[0];
  const unavailableFields = new Set(seat.unavailableFields ?? []);
  const fieldUnavailable = (name) => unavailableFields.has(name);
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
  const diagnosticAfterSuccess = seatDiagnostics.some((row) => !success || row.ts >= success.ts);
  const currentError = hasRecent && (failure(last) || diagnosticAfterSuccess);
  const recentSuccess = configured && hasRecent && !currentError && !diagnosticsUnavailable;
  const readiness = readsUnknown ? "Diagnostics unavailable" : seat.paused ? "Paused" : !configured ? `Needs setup: ${missing.join(", ")}` : currentError ? "Recent failure" : recentSuccess ? "Recent success" : roleUnchecked ? "Configured; role not checked yet" : "Configured; no recent traffic";
  const tone = recentSuccess ? "state-ready" : "state-attention";
  return `<article class="status-card"><h3>${e.escape(e.seatLabel ? e.seatLabel(seat.seat) : seat.seat)}</h3><p class="status-state ${tone}">${e.escape(readiness)}</p>
<dl><dt>Seat</dt><dd>${fieldUnavailable("state") ? "Unknown" : seat.paused ? "Paused" : "Unpaused"}</dd>
<dt>Zulip Key</dt><dd>${seat.keyInstalled ? "Installed" : "Missing"}</dd>
<dt>Member Role</dt><dd>${fieldUnavailable("role") ? "Unknown" : roleUnchecked ? "Not checked yet (checked on the seat's next call)" : seat.role.ok ? `Verified (${e.escape(seat.role.role ?? "member")})` : `Refused${seat.role.reason ? `: ${e.escape(seat.role.reason)}` : ""}`}</dd>
<dt>Grant</dt><dd>${fieldUnavailable("grants") ? "Unknown" : (seat.grants ?? []).length ? `${(seat.grants ?? []).length} present` : "Missing"}</dd>
<dt>Last Call</dt><dd>${last ? `${e.escape(e.time(last.ts))} · ${e.escape(last.tool ?? "unknown tool")}` : "No recent traffic"}</dd>
<dt>Last Success</dt><dd>${success ? e.escape(e.time(success.ts)) : "None in retained sample"}</dd>
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
  const diagnosticsUnavailable = Boolean(globalDiagnosticsUnavailable || seats.some((s) => s.diagnosticsUnavailable || (s.unavailableFields ?? []).length));
  const collectedErrors = dedupeErrors(collectErrorRows({ statusCalls, statusErrors, refusals, tokenRefusals, audits, runtimeErrors }));
  const noTraffic = statusCalls.length === 0;
  const recentFailures = collectedErrors.length;
  const heading = diagnosticsUnavailable ? "Diagnostics Unavailable" : recentFailures ? "Recorded Failures" : noTraffic ? "No Recent Traffic" : "Recent Calls; No Recorded Failures";
  const status = diagnosticsUnavailable
    ? `The diagnostic sample could not be loaded.${e.gap} Configuration fields do not verify service health.`
    : noTraffic
      ? `No calls appear in the retained sample.${e.gap} A quiet seat is not verified healthy.`
      : `${statusCalls.length} retained tool calls; ${recentFailures ? "failure or refusal records are present" : "no failure records are present"}.${e.gap} A successful past call does not guarantee current readiness.`;
  const seatCards = seats.map((s) => seatSummary(s, statusCalls, collectedErrors, generatedAt, globalDiagnosticsUnavailable || Boolean(s.diagnosticsUnavailable), e)).join("") || "<p>No seats are configured.</p>";
  const errors = recentErrors(collectedErrors, e);
  // Folded by default (AFC-347):  the summary line carries the verdict, so the owner sees it without scrolling.
  const tone = recentFailures || noTraffic || diagnosticsUnavailable ? "state-attention" : "state-neutral";
  return `<details id="status-overview" class="status-overview fold"><summary><h2>Status Overview</h2> <span class="status-state ${tone}">${e.escape(heading)}</span></summary><header class="overview-heading"><div><p class="status-state ${tone}">${e.escape(heading)}</p></div><form method="get" action="/admin"><button type="submit">Refresh Status</button></form></header>
<p>${e.escape(status)}${generatedAt ? `${e.gap}Generated ${e.escape(e.time(generatedAt))}.` : ""}</p>
<p class="coverage">History Coverage: latest per-seat calls (up to 100), separately retained errors, refusal counters, and retained audit events.${e.gap} Cloudflare Access denials and client-local errors that never reach the Worker are not included.${e.gap} Detailed error messages are available only for calls recorded after diagnostic logging was added.${e.gap} Older events may not appear.</p>
<div class="status-grid">${seatCards}</div><h3>Recent Errors And Refusals</h3><div class="error-list">${errors}</div></details>`;
}
