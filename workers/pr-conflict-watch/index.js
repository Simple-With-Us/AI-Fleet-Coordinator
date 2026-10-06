// pr-conflict-watch: GitHub webhook receiver that alerts #agent-sync Slack
// the moment a PR's mergeable_state turns dirty/blocked/unstable.
// Env bindings (Worker secrets): GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, SLACK_BOT_TOKEN
// KV binding: PR_STATE

const SLACK_CHANNEL = "C0BEZDJDNKV"; // #agent-sync
const BAD_STATES = new Set(["dirty", "blocked", "unstable"]);

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

async function postSlack(token, text) {
  const res = await fetch("https://slack.com/api/chat.postMessage", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json; charset=utf-8",
    },
    body: JSON.stringify({ channel: SLACK_CHANNEL, text }),
  });
  if (!res.ok) throw new Error(`Slack delivery failed (HTTP ${res.status})`);
  // Slack can reject a message with HTTP 200.  Invalid JSON also fails closed.
  const body = await res.json();
  if (body?.ok !== true) throw new Error("Slack delivery failed (API did not confirm success)");
}

async function handlePr(owner, repo, number, env) {
  const kvKey = `state:${owner}/${repo}#${number}`;
  const result = await getMergeState(owner, repo, number, env.GITHUB_TOKEN);
  if (!result) return;
  const prev = await env.PR_STATE.get(kvKey);

  if (result.state === "closed") {
    if (prev) await env.PR_STATE.delete(kvKey);
    return;
  }

  if (BAD_STATES.has(result.state)) {
    if (prev !== result.state) {
      const pr = result.pr;
      await postSlack(
        env.SLACK_BOT_TOKEN,
        `[PR-WATCH] repo: ${repo} -- PR #${number} "${pr.title}" -- ${result.state} (${pr.html_url})`
      );
      // Persist only confirmed delivery; a failure must leave the next event retryable.
      await env.PR_STATE.put(kvKey, result.state, { expirationTtl: 60 * 60 * 24 * 14 });
    }
  } else if (prev && BAD_STATES.has(prev)) {
    // recovered
    await postSlack(env.SLACK_BOT_TOKEN, `[PR-WATCH] repo: ${repo} -- PR #${number} recovered -> ${result.state}`);
    await env.PR_STATE.delete(kvKey);
  }
}

export default {
  async fetch(request, env, ctx) {
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
