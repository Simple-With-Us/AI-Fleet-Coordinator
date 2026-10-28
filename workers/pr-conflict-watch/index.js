// pr-conflict-watch: GitHub webhook receiver that alerts the fleet's Zulip
// #Alerts stream the moment a PR's mergeable_state turns dirty (merge
// conflict), and again when that conflict clears.  The alert mentions the seat
// that owns the PR branch, so the seat hears about its own conflict.
// Env bindings (Worker secrets): GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN,
// ZULIP_SITE, ZULIP_BOT_EMAIL, ZULIP_BOT_API_KEY
// KV binding: PR_STATE

import { decideStateTransition } from "./state.js";
import { seatMentionFor } from "./seats.js";

// The stream every conflict alert goes to.  Alerts are never posted to a
// private DM and never mention @all or @everyone.
const ZULIP_STREAM = "Alerts";

// Every secret the Worker needs.  A missing one is a deployment mistake, and
// the Worker names it instead of failing later with a confusing 401 from
// GitHub or Zulip.  The values live in Infisical and are pushed into the
// Worker as secrets at deploy time; they are never in this repo.
const REQUIRED_SECRETS = [
  "GITHUB_WEBHOOK_SECRET",
  "GITHUB_TOKEN",
  "ZULIP_SITE",
  "ZULIP_BOT_EMAIL",
  "ZULIP_BOT_API_KEY",
];

// The fleet's sentence gap: a no-break space followed by a normal space, so
// the gap survives Markdown rendering.
const SENTENCE_GAP = "\u00A0 ";

async function verifySignature(request, secret) {
  const sigHeader = request.headers.get("x-hub-signature-256") || "";
  const body = await request.clone().arrayBuffer();
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
    const pr = await res.json();
    if (pr.state !== "open") return { state: "closed", pr };
    if (pr.mergeable_state && pr.mergeable_state !== "unknown") {
      return { state: pr.mergeable_state, pr };
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  return null;
}

/** The names of the required secrets this Worker was not given. */
export function missingSecretNames(env) {
  return REQUIRED_SECRETS.filter((name) => {
    const value = env?.[name];
    return typeof value !== "string" || value.trim() === "";
  });
}

/** The Zulip messages endpoint for this deployment. */
export function zulipMessagesUrl(env) {
  const site = String(env?.ZULIP_SITE ?? "").trim().replace(/\/+$/, "");
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

/**
 * Validate a Zulip response body at the boundary.
 *
 * This Worker has no npm dependencies, so there is no zod here: the shape
 * check is by hand.  Only a plain object with a string `result` and a string
 * `msg` (when present) is trusted.  Anything else is a failure with a fixed
 * message, so an unexpected body is never echoed into the logs and never
 * mistaken for a success.
 *
 * @param {unknown} json the parsed response body.
 * @returns {{ok: boolean, msg?: string}} ok is true only for result "success".
 */
export function parseZulipResult(json) {
  if (!isPlainObject(json)) return { ok: false, msg: UNPARSEABLE_RESPONSE };

  const result = json.result;
  if (typeof result !== "string") return { ok: false, msg: UNPARSEABLE_RESPONSE };

  const msg = json.msg;
  if (msg !== undefined && typeof msg !== "string") {
    return { ok: false, msg: UNPARSEABLE_RESPONSE };
  }

  const parsed = { ok: result === "success" };
  if (typeof msg === "string") parsed.msg = msg;
  return parsed;
}

/** The message to log for a failed post: Zulip's own, or a safe placeholder. */
function failureMessage(parsed) {
  if (typeof parsed.msg !== "string" || parsed.msg.trim() === "") return "no message";
  return parsed.msg;
}

/**
 * Post one message to Zulip #Alerts.
 *
 * The API key never leaves this function: the request carries it in a Basic
 * auth header, and a failure logs the HTTP status and Zulip's own error
 * message, which never contain the credential.
 *
 * @returns {Promise<boolean>} true when Zulip accepted the message.
 */
export async function postZulip(env, topic, content) {
  const body = new URLSearchParams({
    type: "stream",
    to: ZULIP_STREAM,
    topic,
    content,
  }).toString();

  const auth = btoa(`${env.ZULIP_BOT_EMAIL}:${env.ZULIP_BOT_API_KEY}`);
  const res = await fetch(zulipMessagesUrl(env), {
    method: "POST",
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
  const parsed = parseZulipResult(json);

  if (!res.ok) {
    console.error(`zulip post failed: http ${res.status}: ${failureMessage(parsed)}`);
    return false;
  }
  if (!parsed.ok) {
    console.error(`zulip post failed: http ${res.status}: ${failureMessage(parsed)}`);
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
 * @param {object} env Worker bindings.
 */
export async function handlePr(owner, repo, number, env) {
  const kvKey = `state:${owner}/${repo}#${number}`;
  const result = await getMergeState(owner, repo, number, env.GITHUB_TOKEN);
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
      env,
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
    env,
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
    // the Worker as broken just because it has no webhook secret.
    if (request.method !== "POST") return new Response("ok", { status: 200 });

    // A misconfigured Worker says so by name, before it touches anything.
    const missing = missingSecretNames(env);
    if (missing.length > 0) {
      return new Response(`pr-conflict-watch misconfigured: missing ${missing.join(", ")}`, {
        status: 500,
      });
    }

    const valid = await verifySignature(request, env.GITHUB_WEBHOOK_SECRET);
    if (!valid) return new Response("bad signature", { status: 401 });

    const event = request.headers.get("x-github-event") || "";
    const payload = await request.json();
    const candidates = []; // [owner, repo, number]

    if (event === "pull_request" && payload.pull_request) {
      const [owner, repo] = payload.repository.full_name.split("/");
      candidates.push([owner, repo, payload.pull_request.number]);
    } else if ((event === "check_suite" || event === "check_run") && payload.repository) {
      const [owner, repo] = payload.repository.full_name.split("/");
      const prs = (event === "check_suite" ? payload.check_suite.pull_requests : payload.check_run.pull_requests) || [];
      for (const pr of prs) candidates.push([owner, repo, pr.number]);
    } else {
      return new Response("ignored", { status: 200 });
    }

    ctx.waitUntil(
      Promise.all(candidates.map(([owner, repo, number]) => handlePr(owner, repo, number, env)))
    );
    return new Response("ok", { status: 200 });
  },
};
