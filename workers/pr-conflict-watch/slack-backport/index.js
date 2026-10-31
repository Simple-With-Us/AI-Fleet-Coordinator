import { z } from "zod";
const PrSchema = z.object({
  state: z.enum(["open", "closed"]),
  mergeable_state: z.string().nullable().optional(),
  title: z.string(), html_url: z.string().url(),
});
const WebhookSchema = z.object({
  repository: z.object({ full_name: z.string().regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/) }),
  pull_request: z.object({ number: z.number().int().positive().safe() }).optional(),
  check_suite: z.object({pull_requests: z.array(z.object({number: z.number().int().positive().safe()}))}).optional(),
  check_run: z.object({pull_requests: z.array(z.object({number: z.number().int().positive().safe()}))}).optional(),
});
import { readBoundedBody, BodyLimitError } from "./body.js";
var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// state.js
var ALERT_STATES = /* @__PURE__ */ new Set(["dirty"]);
function decideStateTransition(prev, mergeableState) {
  if (mergeableState === "closed") {
    return prev ? "clear" : "noop";
  }
  if (ALERT_STATES.has(mergeableState)) {
    return prev !== mergeableState ? "alert" : "noop";
  }
  if (prev && ALERT_STATES.has(prev)) {
    return "recover";
  }
  return "noop";
}
__name(decideStateTransition, "decideStateTransition");

// index.js
var SLACK_CHANNEL = "C0BEZDJDNKV";
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
  const digest = "sha256=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (digest.length !== sigHeader.length) return false;
  let diff = 0;
  for (let i = 0; i < digest.length; i++) diff |= digest.charCodeAt(i) ^ sigHeader.charCodeAt(i);
  return diff === 0;
}
__name(verifySignature, "verifySignature");
async function ghFetch(url, token) {
  return fetch(url, {
    redirect: "manual",
    headers: {
      Authorization: `Bearer ${token}`,
      "User-Agent": "pr-conflict-watch",
      Accept: "application/vnd.github+json"
    }
  });
}
__name(ghFetch, "ghFetch");
async function getMergeState(owner, repo, number, token) {
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
__name(getMergeState, "getMergeState");
async function postSlack(token, text) {
  await fetch("https://slack.com/api/chat.postMessage", {
    method: "POST",
    redirect: "manual",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json; charset=utf-8"
    },
    body: JSON.stringify({ channel: SLACK_CHANNEL, text })
  });
}
__name(postSlack, "postSlack");
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
    const pr = result.pr;
    await postSlack(
      env.SLACK_BOT_TOKEN,
      `[PR-WATCH] repo: ${repo} -- PR #${number} "${pr.title}" -- ${result.state} (${pr.html_url})`
    );
    return;
  }
  await env.PR_STATE.delete(kvKey);
  await postSlack(env.SLACK_BOT_TOKEN, `[PR-WATCH] repo: ${repo} -- PR #${number} recovered -> ${result.state}`);
}
__name(handlePr, "handlePr");
var index_default = {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok", { status: 200 });
    if (!/^sha256=[a-f0-9]{64}$/.test(request.headers.get("x-hub-signature-256") || ""))
      return new Response("bad signature", { status: 401 });
    let bytes;
    try { bytes = await readBoundedBody(request, 25 * 1024 * 1024); }
    catch (error) {
      return new Response(error instanceof BodyLimitError ? "payload too large" : "invalid body",
        { status: error instanceof BodyLimitError ? 413 : 400 });
    }
    const valid = await verifySignature(request, env.GITHUB_WEBHOOK_SECRET, bytes);
    if (!valid) return new Response("bad signature", { status: 401 });
    const event = request.headers.get("x-github-event") || "";
    let payload;
    try { payload = JSON.parse(new TextDecoder().decode(bytes)); }
    catch { return new Response("invalid JSON", { status: 400 }); }
    if (!payload || typeof payload !== "object" || Array.isArray(payload))
      return new Response("invalid payload", { status: 400 });
    const candidates = [];
    const parsed = WebhookSchema.safeParse(payload);
    if (!parsed.success) return new Response("invalid payload", { status: 400 });
    payload = parsed.data;
    const name = payload.repository?.full_name;
    const relevant = event === "pull_request" || event === "check_suite" || event === "check_run";
    if (!relevant) return new Response("ignored", { status: 200 });
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
          await handlePr(owner, repo, number, env);
        }
      }));
    })());
    return new Response("ok", { status: 200 });
  }
};
export {
  index_default as default
};

