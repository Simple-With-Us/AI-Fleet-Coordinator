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

/** The Zulip error message of a failed response, or a safe placeholder. */
function zulipErrorMessage(payload) {
  const msg = payload?.msg;
  if (typeof msg !== "string" || msg.trim() === "") return "no message";
  return msg;
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

  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }

  if (!res.ok) {
    console.error(`zulip post failed: http ${res.status}: ${zulipErrorMessage(payload)}`);
    return false;
  }
  if (!payload || payload.result !== "success") {
    console.error(`zulip post failed: http ${res.status}: ${zulipErrorMessage(payload)}`);
    return false;
  }
  return true;
}

/** Escape the Markdown characters that would break the link text of a title. */
function escapeTitle(title) {
  return String(title ?? "").replace(/([`[\]])/g, "\\$1");
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

  const title = escapeTitle(pr?.title);
  const base = pr?.base?.ref ?? "main";
  const mention =
    seatMentionFor(pr) ??
    `the author (\`${pr?.user?.login ?? "unknown"}\`, branch \`${pr?.head?.ref ?? "unknown"}\`)`;

  return `PR [#${number} ${title}](${url}) on ${repoText} has a merge conflict with \`${base}\`.${SENTENCE_GAP}${mention} please rebase it.`;
}

/** The Zulip topic for one repository's conflicts. */
export function topicForRepo(repo) {
  return `pr-conflicts ${repo}`;
}

async function handlePr(owner, repo, number, env) {
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
    await env.PR_STATE.put(kvKey, result.state, { expirationTtl: 60 * 60 * 24 * 14 });
    await postZulip(
      env,
      topicForRepo(repo),
      buildMessage({ kind: "conflict", repo, pr: result.pr })
    );
    return;
  }

  // recover: left dirty (only state we store)
  await env.PR_STATE.delete(kvKey);
  await postZulip(
    env,
    topicForRepo(repo),
    buildMessage({ kind: "recovered", repo, pr: result.pr, state: result.state })
  );
}

export default {
  async fetch(request, env, ctx) {
    // A misconfigured Worker says so by name, before it touches anything.
    const missing = missingSecretNames(env);
    if (missing.length > 0) {
      return new Response(`pr-conflict-watch misconfigured: missing ${missing.join(", ")}`, {
        status: 500,
      });
    }

    if (request.method !== "POST") return new Response("ok", { status: 200 });

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
