import { z } from "zod";
const PrSchema = z.object({
  state: z.enum(["open", "closed"]),
  mergeable_state: z.string().nullable().optional(),
  number: z.number().int().positive().safe(),
  title: z.string(), html_url: z.string().url(),
  base: z.object({ ref: z.string() }),
  head: z.object({ ref: z.string() }),
  user: z.object({ login: z.string() }),
});
const WebhookSchema = z.object({
  repository: z.object({ full_name: z.string().regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/) }),
  pull_request: z.object({ number: z.number().int().positive().safe() }).optional(),
  check_suite: z.object({pull_requests: z.array(z.object({number: z.number().int().positive().safe()}))}).optional(),
  check_run: z.object({pull_requests: z.array(z.object({number: z.number().int().positive().safe()}))}).optional(),
});
import { readBoundedBody, BodyLimitError } from "./body.js";
// pr-conflict-watch: GitHub webhook receiver that alerts the fleet's Zulip
// #Alerts stream the moment a PR's mergeable_state turns dirty (merge
// conflict), and again when that conflict clears.  The alert mentions the seat
// that owns the PR branch, so the seat hears about its own conflict.
//
// Compatible release: preserve existing GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN
// and PR_STATE bindings. Bind only the verified existing Alerts bot's
// ZULIP_SITE, ZULIP_BOT_EMAIL and ZULIP_BOT_API_KEY for the delivery cutover.

import { decideStateTransition } from "./state.js";
import { seatMentionFor } from "./seats.js";


// The stream every conflict alert goes to.  Alerts are never posted to a
// private DM and never mention @all or @everyone.
const ZULIP_STREAM = "Alerts";

// The fleet's sentence gap: a no-break space followed by a normal space, so
// the gap survives Markdown rendering.
const SENTENCE_GAP = "\u00A0 ";

async function verifySignature(request, secret, body) {
  const sigHeader = request.headers.get("x-hub-signature-256") || "";
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const mac = await crypto.subtle.sign("HMAC", key, body);
  const digest =
    "sha256=" +
    [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (digest.length !== sigHeader.length) return false;
  let diff = 0;
  for (let i = 0; i < digest.length; i++) diff |= digest.charCodeAt(i) ^ sigHeader.charCodeAt(i);
  return diff === 0;
}

async function ghFetch(url, token) {
  return fetch(url, {
    redirect: "manual",
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "pr-conflict-watch",
      Accept: "application/vnd.github+json",
    },
  });
}

async function getMergeState(owner, repo, number, token) {
  // mergeable_state is computed async by GitHub; retry a couple times.
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await ghFetch(`https://api.github.com/repos/${owner}/${repo}/pulls/${number}`, token);
    if (!res.ok) return null;
    let parsed;
    try { parsed = PrSchema.safeParse(await res.json()); } catch { return null; }
    if (!parsed.success) return null;
    const pr = parsed.data;
    if (pr.state !== "open") return { state: "closed", pr };
    if (pr.mergeable_state && pr.mergeable_state !== "unknown") {
      return { state: pr.mergeable_state, pr };
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  return null;
}

/**
 * The names of the required secrets this Worker was not given.
 *
 * The only secret binding left is the Infisical service token: every other
 * secret comes from Infisical itself, so a missing one is a missing secret
 * in the folder, not a missing binding.
 */
export function missingSecretNames(env) {
  return ["GITHUB_WEBHOOK_SECRET", "GITHUB_TOKEN", "ZULIP_SITE", "ZULIP_BOT_EMAIL", "ZULIP_BOT_API_KEY"]
    .filter(name => typeof env?.[name] !== "string" || !env[name].trim());
}

/** The Zulip messages endpoint for this deployment. */
export function zulipMessagesUrl(config) {
  const site = String(config?.ZULIP_SITE ?? "").trim().replace(/\/+$/, "");
  return `${site}/api/v1/messages`;
}

/** The message used when the response body is not a Zulip result we can read. */
const UNPARSEABLE_RESPONSE = "unparseable Zulip response";

/** True for a plain object, false for arrays, null and everything else. */
function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** The outcome labels a Zulip response can have. */
const ZULIP_OUTCOME_SUCCESS = "success";
const ZULIP_OUTCOME_ERROR = "error";
const ZULIP_OUTCOME_UNPARSEABLE = "unparseable";

/**
 * Validate a Zulip response body at the boundary.
 *
 * The external response goes through zod before any result fields are used.  Only a plain object with a string `result` and a string
 * `msg` (when present) is trusted.  Anything else is a failure with a fixed
 * message, so an unexpected body is never mistaken for a success.
 *
 * @param {unknown} json the parsed response body.
 * @returns {{ok: boolean, msg?: string, outcome: string}} ok is true only for
 *   result "success".  The outcome is the allow-listed label a failure log
 *   carries; the message is returned to the caller but never logged.
 */
function parseZulipBody(json) {
  const validated = z.object({ result: z.string(), msg: z.string().optional(), id: z.number().int().positive().optional() }).safeParse(json);
  if (!validated.success) {
    return { ok: false, msg: UNPARSEABLE_RESPONSE, outcome: ZULIP_OUTCOME_UNPARSEABLE };
  }
  json = validated.data;
  const result = json.result;
  if (typeof result !== "string") {
    return { ok: false, msg: UNPARSEABLE_RESPONSE, outcome: ZULIP_OUTCOME_UNPARSEABLE };
  }

  const msg = json.msg;
  if (msg !== undefined && typeof msg !== "string") {
    return { ok: false, msg: UNPARSEABLE_RESPONSE, outcome: ZULIP_OUTCOME_UNPARSEABLE };
  }

  const ok = result === "success";
  const parsed = { ok, outcome: ok ? ZULIP_OUTCOME_SUCCESS : ZULIP_OUTCOME_ERROR };
  if (typeof msg === "string") parsed.msg = msg;
  return parsed;
}

/**
 * Validate a Zulip response body at the boundary.
 *
 * The exported contract is the shape a caller can rely on: ok is true only
 * for result "success", and msg carries Zulip's own message when the body had
 * one.  The message is never logged: the failure log carries the HTTP status
 * and an outcome label only.
 *
 * @param {unknown} json the parsed response body.
 * @returns {{ok: boolean, msg?: string}} ok is true only for result "success".
 */
export function parseZulipResult(json) {
  const { ok, msg } = parseZulipBody(json);
  const parsed = { ok };
  if (msg !== undefined) parsed.msg = msg;
  return parsed;
}

/**
 * Post one message to Zulip #Alerts.
 *
 * The API key never leaves this function: the request carries it in a Basic
 * auth header.  A failure logs allow-listed fields only -- the HTTP status
 * and a fixed outcome label -- and never Zulip's own message, so nothing the
 * server said reaches the logs.
 *
 * @param {object} config the existing deployment configuration.
 * @returns {Promise<boolean>} true when Zulip accepted the message.
 */
export async function postZulip(config, topic, content) {
  const body = new URLSearchParams({
    type: "stream",
    to: ZULIP_STREAM,
    topic,
    content,
  }).toString();

  const auth = btoa(`${config.ZULIP_BOT_EMAIL}:${config.ZULIP_BOT_API_KEY}`);
  const res = await fetch(zulipMessagesUrl(config), {
    method: "POST",
    redirect: "manual",
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body,
  });

  let json = null;
  try {
    json = await res.json();
  } catch {
    json = null;
  }
  const parsed = parseZulipBody(json);

  // The failure log carries allow-listed fields only: the HTTP status and a
  // fixed outcome label.  Zulip's own message is never logged.
  const logFailure = () =>
    console.error(
      JSON.stringify({
        event: "zulip_post_failed",
        status: res.status,
        result: parsed.outcome,
      })
    );

  if (!res.ok) {
    logFailure();
    return false;
  }
  if (!parsed.ok) {
    logFailure();
    return false;
  }
  return true;
}

/** Escape the Markdown characters that would break the link text of a title. */
function escapeTitle(title) {
  return String(title ?? "").replace(/([`[\]])/g, "\\$1");
}

// A zero-width space (U+200B) after the "@" breaks Zulip's mention syntax
// while leaving the text a human reads exactly as it was.
const ZERO_WIDTH_SPACE = "\u200B";

// The mention starters Zulip renders: "@**name**", "@_name_" and "@*name*".
const MENTION_STARTER = /@(?=[*_])/g;

// The bare group mentions Zulip renders.
const GROUP_MENTION = /@(all|everyone|channel|stream|topic)\b/gi;

/**
 * Neutralize Zulip mention syntax in a string.
 *
 * A PR title, head ref, base ref or author login is chosen by whoever opened
 * the PR, so any mention it carries is a ping the author asked for.  The "@"
 * gets a zero-width space after it, which Zulip does not render as a mention
 * and a human does not see.
 *
 * @param {unknown} text
 * @returns {string}
 */
export function neutralizeMentions(text) {
  return String(text ?? "")
    .replace(MENTION_STARTER, `@${ZERO_WIDTH_SPACE}`)
    .replace(GROUP_MENTION, `@${ZERO_WIDTH_SPACE}$1`);
}

/**
 * Build the alert text for one PR.
 *
 * A new conflict names the seat that owns the branch, so the seat can rebase.
 * A recovery names nobody: the conflict is already gone.
 *
 * @param {"conflict"|"recovered"} kind
 * @param {string} repo repository name, e.g. "BotFleet".
 * @param {object} pr GitHub pull request payload.
 * @param {string} [state] mergeable_state, for a recovery.
 * @returns {string}
 */
export function buildMessage({ kind, repo, pr, state }) {
  const number = pr?.number;
  const url = pr?.html_url ?? "";
  const repoText = `\`${repo}\``;

  if (kind === "recovered") {
    return `PR [#${number}](${url}) on ${repoText} no longer conflicts (now \`${state}\`).`;
  }

  // Every field the PR author controls is neutralized before it reaches the
  // message, so a PR title of "@**all**" cannot ping the stream.  The seat
  // mention is ours and stays intact.
  const title = escapeTitle(neutralizeMentions(pr?.title));
  const base = neutralizeMentions(pr?.base?.ref ?? "main");
  const mention =
    seatMentionFor(pr) ??
    `the author (\`${neutralizeMentions(pr?.user?.login ?? "unknown")}\`, branch \`${neutralizeMentions(
      pr?.head?.ref ?? "unknown"
    )}\`)`;

  return `PR [#${number} ${title}](${url}) on ${repoText} has a merge conflict with \`${base}\`.${SENTENCE_GAP}${mention} please rebase it.`;
}

/** The Zulip topic for one repository's conflicts. */
export function topicForRepo(repo) {
  return `pr-conflicts ${repo}`;
}

/**
 * Watch one PR and post its alert.
 *
 * The KV key is the dedupe record, so it is only written once Zulip has
 * confirmed the message: a key written before a failed post would suppress
 * that alert forever, and a key deleted before a failed post would lose the
 * recovery.  Both orders below keep a failed post retryable on the next
 * delivery.
 *
 * @param {string} owner repository owner.
 * @param {string} repo repository name.
 * @param {number} number pull request number.
 * @param {object} config the existing deployment configuration.
 * @param {object} env Worker bindings (PR_STATE).
 */
export async function handlePr(owner, repo, number, config, env) {
  const kvKey = `state:${owner}/${repo}#${number}`;
  const result = await getMergeState(owner, repo, number, config.GITHUB_TOKEN);
  if (!result) return;
  const prev = await env.PR_STATE.get(kvKey);

  const action = decideStateTransition(prev, result.state);
  if (action === "noop") return;

  if (action === "clear") {
    await env.PR_STATE.delete(kvKey);
    return;
  }

  if (action === "alert") {
    const posted = await postZulip(
      config,
      topicForRepo(repo),
      buildMessage({ kind: "conflict", repo, pr: result.pr })
    );
    // Only a delivered alert may claim the dedupe key.
    if (posted) {
      await env.PR_STATE.put(kvKey, result.state, { expirationTtl: 60 * 60 * 24 * 14 });
    }
    return;
  }

  // recover: left dirty (only state we store)
  const posted = await postZulip(
    config,
    topicForRepo(repo),
    buildMessage({ kind: "recovered", repo, pr: result.pr, state: result.state })
  );
  // Only a delivered recovery may clear the dedupe key.
  if (posted) {
    await env.PR_STATE.delete(kvKey);
  }
}

export default {
  async fetch(request, env, ctx) {
    // A health probe answers before anything else, so a probe never reports
    // the Worker as broken just because it has no Infisical token.
    if (request.method !== "POST") return new Response("ok", { status: 200 });

    // A misconfigured Worker says so by name, before it touches anything.
    const missing = missingSecretNames(env);
    if (missing.length > 0) {
      return new Response(`pr-conflict-watch misconfigured: missing ${missing.join(", ")}`, {
        status: 500,
      });
    }

    if (!/^sha256=[a-f0-9]{64}$/.test(request.headers.get("x-hub-signature-256") || ""))
      return new Response("bad signature", { status: 401 });
    let bytes;
    try { bytes = await readBoundedBody(request, 25 * 1024 * 1024); }
    catch (error) {
      return new Response(error instanceof BodyLimitError ? "payload too large" : "invalid body",
        { status: error instanceof BodyLimitError ? 413 : 400 });
    }

    // Preserve the existing GitHub bindings; only delivery credentials change.
    const config = env;
    const valid = await verifySignature(request, config.GITHUB_WEBHOOK_SECRET, bytes);
    if (!valid) return new Response("bad signature", { status: 401 });

    const event = request.headers.get("x-github-event") || "";
    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(bytes)); }
    catch { return new Response("invalid JSON", { status: 400 }); }
    if (!payload || typeof payload !== "object" || Array.isArray(payload))
      return new Response("invalid payload", { status: 400 });
    const candidates = []; // [owner, repo, number]

    const relevant = event === "pull_request" || event === "check_suite" || event === "check_run";
    if (!relevant) return new Response("ignored", { status: 200 });
    const parsed = WebhookSchema.safeParse(payload);
    if (!parsed.success) return new Response("invalid payload", { status: 400 });
    payload = parsed.data;
    const name = payload.repository?.full_name;
    if (typeof name !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(name))
      return new Response("invalid repository", { status: 400 });
    const [owner, repo] = name.split("/");
    const prs = event === "pull_request" ? [payload.pull_request] : payload[event]?.pull_requests;
    if (!Array.isArray(prs) || prs.some(pr => !Number.isSafeInteger(pr?.number) || pr.number < 1))
      return new Response("invalid pull requests", { status: 400 });
    for (const number of new Set(prs.map(pr => pr.number))) candidates.push([owner, repo, number]);
    // Drain the whole delivery with four concurrent lookups, never one per PR.
    ctx.waitUntil((async () => {
      let next = 0;
      await Promise.all(Array.from({ length: Math.min(4, candidates.length) }, async () => {
        while (next < candidates.length) {
          const [owner, repo, number] = candidates[next++];
          try { await handlePr(owner, repo, number, config, env); }
          catch { console.error(JSON.stringify({ event: "pr_dispatch_failed", number })); }
        }
      }));
    })());
    return new Response("ok", { status: 200 });
  },
};
