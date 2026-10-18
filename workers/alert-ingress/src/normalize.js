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
    payload.monitor_tags && Array.isArray(payload.monitor_tags) ? payload.monitor_tags[0] : "",
    payload.scope,
  );
  const title = oneLine(first(payload.alert_title, payload.event_title, payload.title));
  const firing = alertType === "triggered" || alertType === "warning";

  const key = monitorId ? `datadog:${monitorId}` : `datadog:${title.toLowerCase()}`;
  const service = tag || title || `datadog-${monitorId || "monitor"}`;

  return {
    source: "datadog",
    key,
    service,
    firing,
    // A recovered event carries no severity; INFO keeps the close quiet.
    severity: firing ? undefined : "INFO",
    text: oneLine(first(payload.alert_transition, title, alertType)) || service,
    link: first(payload.event_url, payload.url),
    prodDown: /production|prod down|site down/i.test(title),
  };
}

/** BetterStack outgoing webhook (`{"text": ...}` shaped). */
export function fromBetterStack(payload) {
  const data = payload && typeof payload.data === "object" && payload.data ? payload.data : payload || {};
  const url = first(data.url, data.check_url, payload.url);
  const status = strip(first(data.status, payload.status)).toLowerCase();
  const name = oneLine(first(data.check_name, data.name, payload.name, "betterstack check"));

  // BetterStack reports up/down rather than fired/recovered.
  const down = /down|failed|failing/.test(status);
  const firing = status ? down : payload.alert_type === "ALERT";

  let m;
  if ((m = url.match(/betterstack\.com\/uptime\/(?:checker|monitor)\/([A-Za-z0-9]+)/))) {
    // keep the id stable so the same check always maps to one state key
  }
  const id = first(data.check_id, data.monitor_id, (url.match(/\/([a-f0-9]{16,})/i) || [])[1]);
  const key = id ? `betterstack:${id}` : `betterstack:${name.toLowerCase()}`;

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
  const service = oneLine(first(alert.service, alert.monitor, alert.name, alert.source, "alert"));
  const status = strip(first(alert.status, alert.state, alert.alert_type)).toLowerCase();
  const firing = status
    ? !/ok|up|recover|resolved|clear/.test(status)
    : alert.firing === true;
  return {
    source: strip(payload.source) || "generic",
    key: `${strip(payload.source) || "generic"}:${oneLine(first(alert.id, alert.fingerprint, service)).toLowerCase()}`,
    service,
    firing,
    severity: first(alert.severity, alert.priority),
    text: oneLine(first(alert.summary, alert.message, alert.description, alert.title)) || service,
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
  return event.source === "generic" && hint ? { ...event, source: hint } : event;
}

export const __testing = { oneLine, first };