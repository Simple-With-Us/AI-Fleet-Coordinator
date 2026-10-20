// Vendor payload -> one common event shape.
//
// Each vendor speaks its own dialect; every downstream consumer (state machine,
// standing status message, topic routing) should speak only this one.
//
// Normalized event:
//   {
//     source:    "datadog" | "betterstack" | "generic",
//     key:       stable identity of the alertable thing (dedupe key),
//     service:   what to name it in the topic / status line,
//     firing:    true | false,
//     severity:  SEVERE | MODERATE | INFO,
//     text:      one short line,
//     link:      canonical URL,
//     prodDown:  true when this counts as production down,
//   }

const strip = (s) => String(s || "").trim();

/** First non-empty of the candidates, as a string. */
function first(...vals) {
  for (const v of vals) {
    const s = strip(typeof v === "object" && v ? JSON.stringify(v) : v);
    if (s && s !== "undefined" && s !== "null") return s;
  }
  return "";
}

/** Collapse whitespace so a template's newlines never reach the channel. */
function oneLine(s) {
  return strip(s).replace(/\s+/g, " ");
}

/**
 * Datadog monitor notification.
 * The template is verbose and variable across monitors, so nothing depends on
 * its shape except the monitor id, the alert type, and the event URL.
 */
export function fromDatadog(payload) {
  const monitorId = first(payload.monitor_id, payload.alert_id);
  const alertType = strip(payload.alert_type).toLowerCase(); // "triggered" | "recovered" | "warning"
  const tag = first(
    payload.host,
    payload.monitor_tags && Array.isArray(payload.monitor_tags)
      ? payload.monitor_tags[0]
      : "",
    payload.scope,
  );
  const title = oneLine(
    first(payload.alert_title, payload.event_title, payload.title),
  );
  // "error" is an active alert state too; without it an erroring monitor reads as recovered.
  const transition = strip(payload.alert_transition_type).toLowerCase();
  const firing = transition
    ? !/^(recovered|recover|resolved|clear|ok)$/.test(transition)
    : ["triggered", "warning", "error"].includes(alertType);

  // A multi-alert monitor fires once per group; ALERT_SCOPE identifies the
  // triggering group, so it belongs in the state key -- otherwise one group's
  // recovery resolves every other group still down on the same monitor.
  const scope = oneLine(first(payload.scope)).toLowerCase().slice(0, 128);
  const key = monitorId
    ? `datadog:${monitorId}${scope ? `:${scope}` : ""}`
    : `datadog:${title.toLowerCase()}${scope ? `:${scope}` : ""}`;
  const service = tag || title || `datadog-${monitorId || "monitor"}`;

  return {
    source: "datadog",
    key,
    service,
    firing,
    // A recovered event carries no severity; INFO keeps the close quiet.
    // A firing one must keep its own: dropping it here pinned every Datadog
    // alert to INFO, so an erroring monitor could never escalate.
    severity: firing ? first(payload.priority, alertType) : "INFO",
    text: oneLine(first(payload.alert_transition, title, alertType)) || service,
    link: first(payload.event_url, payload.url),
    prodDown: /production|prod down|site down/i.test(title),
  };
}

/**
 * BetterStack outgoing webhook.
 *
 * The documented uptime incident body is JSON:API shaped -- the fields live in
 * `data.attributes` and the monitor identity in
 * `data.relationships.monitor.data`.  A flat `data` body is also accepted so a
 * custom payload template keeps working.
 */
export function fromBetterStack(payload) {
  const envelope =
    payload && typeof payload.data === "object" && payload.data
      ? payload.data
      : payload || {};
  const data =
    envelope && typeof envelope.attributes === "object" && envelope.attributes
      ? { ...envelope, ...envelope.attributes }
      : envelope;
  const monitor =
    (envelope &&
      envelope.relationships &&
      envelope.relationships.monitor &&
      envelope.relationships.monitor.data) ||
    {};
  const url = first(data.url, data.check_url, payload.url);
  const status = strip(first(data.status, payload.status, Object.hasOwn(data, "resolved_at") ? (data.resolved_at ? "up" : "down") : "")).toLowerCase();
  const name = oneLine(
    first(data.check_name, data.name, payload.name, "betterstack check"),
  );

  // BetterStack reports either the check state (up/down) or, on the default
  // incident webhook, the incident state (triggered/resolved).  Both are
  // active-alert or recovery, never neither.
  const down = /down|failed|failing|triggered|alerting/.test(status);
  const recovered = /^(up|ok|passing|resolved|recovered|maintenance|paused)$/.test(
    status,
  );
  // An unrecognized non-empty status stays firing: the safe direction.
  // No status signal at all is unknown, not a recovery -- a status-less
  // delivery must not close a live incident (same rule as the generic path).
  const firing = status
    ? down || !recovered
    : payload.alert_type
      ? payload.alert_type === "ALERT"
      : true;

  // The key must name the monitor, not the incident: `data.id` is per-outage,
  // so a default delivery without `relationships.monitor` would mint a new key
  // every time and never resolve the previous one.  Prefer the monitor identity
  // the URL carries, then use the monitored URL or check name.
  const id = first(
    monitor.id,
    data.check_id,
    data.monitor_id,
    (url.match(/\/monitors\/([^/?#]+)/i) || [])[1],
  );
  // Default incident URLs identify the monitored resource even without an ID.
  // Never use the per-outage incident ID when a stable URL is available.
  const key = id
    ? `betterstack:${id}`
    : url
      ? `betterstack:url:${url}`
      : `betterstack:${name.toLowerCase()}`;

  return {
    source: "betterstack",
    key,
    service: oneLine(first(data.check_name, data.name)) || "betterstack",
    firing,
    severity: firing ? undefined : "INFO",
    text: `${name} is ${status || (firing ? "down" : "up")}`,
    link: url,
    prodDown: false,
  };
}

/**
 * Generic fallback for a webhook we have no bespoke parser for.
 * Reads the conventional keys rather than guessing at vendor internals.
 */
export function fromGeneric(payload) {
  const alert = payload && payload.alert ? payload.alert : payload || {};
  const service = oneLine(
    first(alert.service, alert.monitor, alert.name, alert.source, "alert"),
  );
  const status = strip(
    first(alert.status, alert.state, alert.alert_type),
  ).toLowerCase();
  // Anchor the recovery words: an unanchored /ok|up|.../ reads "broken" or
  // "disrupted" as recovered and closes a live incident.  Unknown statuses stay
  // firing, which is the safe direction.
  const firing = status
    ? !/^(ok(?:ay)?|up|recover(?:ed|ing|ies|y|s)?|resolv(?:ed|es|ing)|clear(?:ed|ing|s)?)$/.test(
        status,
      )
    : // No status signal at all is unknown, not a recovery: a payload carrying
      // only a summary must not close a live incident.  An explicit false is
      // still honoured as a recovery.
      alert.firing !== false;
  return {
    source: strip(payload.source) || "generic",
    key: `${strip(payload.source) || "generic"}:${oneLine(first(alert.id, alert.fingerprint, service)).toLowerCase()}`,
    service,
    firing,
    severity: first(alert.severity, alert.priority),
    text:
      oneLine(
        first(alert.summary, alert.message, alert.description, alert.title),
      ) || service,
    link: first(alert.link, alert.url, alert.dashboard_url),
    prodDown: alert.prod_down === true,
  };
}

const PARSERS = { datadog: fromDatadog, betterstack: fromBetterStack };

/**
 * Dispatch on the source named by the ingress route (`/webhook/<source>`).
 * The route is authoritative: it is what the vendor was configured to call, so
 * a payload that lies about its own `source` cannot cross-contaminate the
 * state of another integration.
 */
export function normalize(source, payload) {
  const hint = strip(source).toLowerCase();
  const parser = PARSERS[hint] || fromGeneric;
  const event = parser(payload || {});
  // Generic routes: the route name is the only authority for the state-key
  // namespace.  A payload claiming source:"datadog" on /webhook/<other> must
  // not overwrite a real datadog incident's state.
  if (!PARSERS[hint] && hint) {
    const tail = event.key.includes(":")
      ? event.key.split(":").slice(1).join(":")
      : event.key;
    return { ...event, source: hint, key: `${hint}:${tail}` };
  }
  return event;
}

export const __testing = { oneLine, first };
