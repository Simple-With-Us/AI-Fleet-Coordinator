// Vendor payload -> one common event shape.
//
// Each vendor speaks its own dialect; every downstream consumer (state machine,
// standing status message, topic routing) should speak only this one.
//
// Normalized event:
//   {
//     source:    "datadog" | "betterstack" | "sentry" | "pagerduty" | "generic",
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
 * Sentry's own level names, mapped onto the fleet's three severities.
 *
 * An unknown level is SEVERE while firing: over-severing a new alert is
 * recoverable, and under-severing one is not.
 */
const SENTRY_SEVERITY = {
  fatal: "SEVERE",
  critical: "SEVERE",
  error: "SEVERE",
  warning: "MODERATE",
  warn: "MODERATE",
  info: "INFO",
  debug: "INFO",
};

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
 * A Sentry field as text.
 *
 * Only a string, or a number rendered as one, is text.  An object, an array
 * or a boolean never is, so a nested blob cannot be stringified into a topic
 * name, a label or a link by the parser.  The generic helper stringifies
 * objects, which is right for the vendors it was written for and wrong for a
 * vendor whose payload nests objects all the way down.
 */
const sentryText = (value) =>
  typeof value === "string"
    ? value
    : typeof value === "number" && Number.isFinite(value)
      ? String(value)
      : "";

/** The first non-empty Sentry field, as text. */
function sentryFirst(...vals) {
  for (const value of vals) {
    const text = sentryText(value).trim();
    if (text) return text;
  }
  return "";
}

/**
 * Credential shapes worth refusing in a vendor-supplied identity.
 *
 * These are the high-confidence vendor prefixes this fleet actually holds, plus
 * the two generic forms that carry no legitimate identity.  They are anchored
 * and deliberately narrow: a Sentry identity is a short id such as `PROJ-1ABC`
 * or a numeric metric-alert id, so nothing ordinary matches, and an over-broad
 * pattern would refuse real alerts.  The patterns mirror the ones in
 * `scripts/agent-sync-mcp/src/contract.js`, which scans the same fleet's
 * credentials for the same reason.
 */
const CREDENTIAL_SHAPES = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/,
  /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b/,
  /\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b/,
  /\b(?:xox[abposr]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,})\b/,
  /\bAIza[0-9A-Za-z_-]{35}\b/,
  /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/,
  /\blin_(?:api|oauth|wh)_[A-Za-z0-9]{20,}/,
  /\bsntry[su]_[A-Za-z0-9+/=_-]{20,}/,
  /\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}/,
  /\bcf(?:ut|at|k)_[A-Za-z0-9]{30,}/,
  /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/,
];

/** True when `value` carries a credential shape rather than an identity. */
function credentialShape(value) {
  const text = String(value || "");
  return CREDENTIAL_SHAPES.some((shape) => shape.test(text));
}

/**
 * Sentry webhook.
 *
 * Sentry sends two shapes, and both carry the alert under `data`: an issue
 * alert with the issue in `data.issue`, and a metric alert with the alert in
 * `data.metric_alert`.  The envelope's `action` says whether the delivery is
 * a new alert or a resolution, so the action, not the presence of a payload,
 * says whether it is firing.
 *
 * A native Sentry payload used to fall through to the generic parser, where
 * `data.issue` read as an object and failed boundary validation as an invalid
 * text field, so a Sentry route could not deliver at all.
 */
export function fromSentry(payload) {
  const data =
    payload && typeof payload.data === "object" && payload.data ? payload.data : {};
  const issue =
    data.issue && typeof data.issue === "object" && data.issue ? data.issue : null;
  const metric =
    data.metric_alert && typeof data.metric_alert === "object" && data.metric_alert
      ? data.metric_alert
      : null;
  const action = sentryFirst(payload.action).toLowerCase();
  const level = sentryFirst(
    issue?.level,
    metric?.status,
    metric?.alert_rule?.status,
    payload.level,
  ).toLowerCase();

  // A resolution says so in the action; a metric alert says it in its status.
  // An unrecognized status stays firing, which is the safe direction: a
  // status-less delivery must never close a live incident.
  // Only the actions that mean "alert is live" fire.  Housekeeping actions
  // (assigned, archived, ignored, ...) say nothing about the alert itself, so
  // they fall back to the level or status signal instead of reopening a
  // resolved incident.
  const FIRING_ACTIONS = /^(created|triggered|reopened|unresolved|escalated|critical|warning)$/;
  const CLOSING_ACTIONS = /^(resolved|closed|complete|completed|ok|archived|ignored)$/;
  const closedByLevel = /^(ok|resolved|recovered|closed)$/.test(level);
  // The issue's own status closes it, whatever the action was.  A housekeeping
  // event on an issue that is already resolved - "assigned" on a resolved issue -
  // carries the issue's level, which still reads "error", so the level alone would
  // reopen an incident that is closed.  A missing status stays firing, which is the
  // safe direction:  a status-less delivery must never close a live incident.
  // The issue's own status closes it whatever the action said, so it is read
  // before the action tests.  Sentry reports a resolved issue as resolved; a
  // delivery whose action happens to be a firing one must not reopen it and
  // raise a fresh SEVERE line for an issue the vendor has already closed.
  const issueStatus = sentryFirst(issue?.status).toLowerCase();
  const closedByStatus = /^(ok|resolved|recovered|closed|complete|completed|ignored|archived)$/.test(
    issueStatus,
  );
  const firing = closedByStatus
    ? false
    : FIRING_ACTIONS.test(action)
      ? true
      : CLOSING_ACTIONS.test(action)
        ? false
        : !closedByLevel;

  const severity =
    (Object.hasOwn(SENTRY_SEVERITY, level) && SENTRY_SEVERITY[level]) ||
    (firing ? "SEVERE" : "INFO");

  const project = issue?.project && typeof issue.project === "object" ? issue.project : {};
  const service = issue
    ? oneLine(sentryFirst(project.slug, project.name, issue.metadata?.type, "sentry"))
    : oneLine(
        sentryFirst(
          metric?.alert_rule?.name,
          metric?.metric_identifier?.name,
          metric?.title,
          "sentry",
        ),
      );

  // The issue key names the issue, so a metric alert and an issue can never
  // share one identity.  A body with no identity has no key to file the
  // incident under, so it is refused instead of falling back to a constant:
  // two different malformed bodies on one constant key would share an
  // incident, and one alert's resolution would close the other's incident and
  // standing message.
  const issueIdentity = sentryFirst(issue?.shortId, issue?.id);
  const metricIdentity = sentryFirst(metric?.id, metric?.alert_rule?.id);
  if (!issue && !metric) throw new Error("sentry payload carries no alert");
  if (issue && !issueIdentity) throw new Error("sentry issue has no identity");
  if (metric && !metricIdentity) throw new Error("sentry metric alert has no identity");
  // The identity is vendor-supplied text that becomes a durable storage key and
  // is relayed into the standing block, so it is scanned for a credential
  // before it is used as one.  A body that puts a token where an identity
  // belongs is refused rather than filed:  persisting or relaying it would copy
  // secret material out of the payload and into this worker's own storage.
  // The message names the field and never the value or a prefix of it.
  const identityField = issue ? "sentry issue identity" : "sentry metric identity";
  if (credentialShape(issue ? issueIdentity : metricIdentity)) {
    // The code lets the webhook route answer a bad request that names the
    // field, instead of the refusal escaping as a writer outage.
    const error = new Error(`${identityField} looks like a credential`);
    error.code = "invalid_identity";
    error.field = identityField;
    throw error;
  }
  const key = issue
    ? `sentry:${issueIdentity.toLowerCase()}`
    : `sentry:metric:${metricIdentity.toLowerCase()}`;

  const text = issue
    ? oneLine(sentryFirst(issue.title, issue.metadata?.value, "Sentry issue"))
    : oneLine(sentryFirst(metric?.title, metric?.alert_rule?.name, "Sentry metric alert"));

  const link = issue
    ? sentryFirst(issue.url, issue.permalink)
    : sentryFirst(metric?.url, metric?.alert_rule?.url);

  return {
    source: "sentry",
    key,
    service,
    firing,
    severity: firing ? severity : "INFO",
    text,
    link,
    prodDown: false,
  };
}

/**
 * PagerDuty v3 webhook.
 *
 * Every delivery is an envelope, `{ event: { event_type, resource_type, data } }`,
 * and for an incident event `data` is the incident itself.  The incident is
 * the alertable thing, so its id is the key:  one PagerDuty incident is one row
 * in the standing block of its PagerDuty service, from trigger to resolve, and
 * an acknowledgement or a reassignment edits that row instead of posting.
 *
 * The incident's own status decides firing, not the event type:  a
 * `incident.reassigned` on a resolved incident must not reopen it, and an
 * unknown status stays firing, which is the safe direction.
 */
export function fromPagerDuty(payload) {
  const envelope =
    payload && typeof payload.event === "object" && payload.event ? payload.event : {};
  const data =
    envelope.data && typeof envelope.data === "object" ? envelope.data : {};
  const id = sentryFirst(data.id);
  if (!id) throw new Error("pagerduty incident has no identity");
  if (credentialShape(id)) {
    const error = new Error("pagerduty incident identity looks like a credential");
    error.code = "invalid_identity";
    error.field = "pagerduty incident identity";
    throw error;
  }
  const eventType = sentryFirst(envelope.event_type).toLowerCase();
  const status = sentryFirst(data.status).toLowerCase();
  const firing = status
    ? status !== "resolved"
    : eventType !== "incident.resolved";

  const service =
    data.service && typeof data.service === "object" ? data.service : {};
  const priority =
    data.priority && typeof data.priority === "object" ? data.priority : {};
  // P1 and P2 outrank the urgency, which every incident carries.  Any other
  // priority name is the account's own vocabulary, so the urgency decides:
  // high urgency pages, so it reads SEVERE, and a missing one is SEVERE too.
  const urgency = sentryFirst(data.urgency).toLowerCase();
  const level = sentryFirst(priority.summary).toUpperCase();
  const severity =
    level === "P1"
      ? "SEVERE"
      : level === "P2"
        ? "MODERATE"
        : urgency === "low"
          ? "MODERATE"
          : "SEVERE";

  const number = sentryFirst(data.number);
  const title = oneLine(sentryFirst(data.title, "PagerDuty incident"));
  const acked = status === "acknowledged" ? " (acknowledged)" : "";

  return {
    source: "pagerduty",
    key: `pagerduty:${id.toLowerCase()}`,
    service: oneLine(sentryFirst(service.summary, service.name, "pagerduty")),
    firing,
    severity: firing ? severity : "INFO",
    text: `${number ? `#${number} ` : ""}${title}${acked}`,
    link: sentryFirst(data.html_url),
    prodDown: /production|prod down|site down/i.test(title),
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

const PARSERS = {
  datadog: fromDatadog,
  betterstack: fromBetterStack,
  sentry: fromSentry,
  pagerduty: fromPagerDuty,
};

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

export const __testing = { oneLine, first, credentialShape };
