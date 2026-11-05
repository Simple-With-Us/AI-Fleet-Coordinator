// The hosted read-only git route:  `simple-git fetch` and `clone` from any host reach the relay here.
//
//   GET  /git/<owner>/<repo>[.git]/info/refs?service=git-upload-pack
//   POST /git/<owner>/<repo>[.git]/git-upload-pack
//
// The Worker adds no policy of its own.  It passes the caller's bearer to the relay unchanged, adds the Access pair that
// only it holds, and streams the answer back, so the relay's allowlist, quota and audit decide every request, and the
// GitHub token stays in the relay.  Only these two git paths exist:  pushing has no route here or in the relay.

import { grUpstreamUrl, resolveGrSecrets } from "./gr-tools.js";

const PATH = /^\/git\/[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,104}\/(info\/refs|git-upload-pack)$/;
const REQUEST_HEADERS = ["Authorization", "Content-Type", "Content-Encoding", "Content-Length", "Git-Protocol", "Accept"];
const RESPONSE_HEADERS = ["Content-Type", "Content-Encoding", "Cache-Control"];
const MAX_REQUEST_BYTES = 8 * 1024 * 1024;

function refuse(status, error) {
  return new Response(JSON.stringify({ error }), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export function isGitPath(pathname) {
  return pathname === "/git" || pathname.startsWith("/git/");
}

export async function serveGit(request, env, { fetchImpl = globalThis.fetch?.bind(globalThis), secrets = resolveGrSecrets } = {}) {
  const url = new URL(request.url);
  const match = PATH.exec(url.pathname);
  if (!match) return refuse(url.pathname.includes("receive-pack") ? 403 : 404, "not found");
  const advertise = match[1] === "info/refs";
  if (request.method !== (advertise ? "GET" : "POST")) return refuse(405, "method not allowed");
  if (advertise && url.search !== "?service=git-upload-pack") {
    return refuse(url.search.includes("receive-pack") ? 403 : 400, "only git-upload-pack is served");
  }
  if (!advertise && url.search) return refuse(400, "no query is accepted");
  if (!(request.headers.get("Authorization") || "").startsWith("Bearer ")) return refuse(401, "unauthenticated");
  if (!advertise) {
    const length = Number(request.headers.get("Content-Length") || "0");
    if (!Number.isInteger(length) || length < 1 || length > MAX_REQUEST_BYTES) {
      return refuse(411, "a Content-Length within the byte bound is required");
    }
  }
  const base = grUpstreamUrl(env.GR_UPSTREAM_URL);
  const { accessId, accessSecret } = await secrets(env);
  if (!base || !accessId || !accessSecret) return refuse(503, "not_configured");
  const headers = new Headers({
    "CF-Access-Client-Id": accessId,
    "CF-Access-Client-Secret": accessSecret,
    "User-Agent": "agent-sync-mcp/git-route",
  });
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  let upstream;
  try {
    upstream = await fetchImpl(`${base}${url.pathname}${url.search}`, {
      method: request.method,
      headers,
      body: advertise ? undefined : request.body,
      redirect: "manual",
      // git's own gzip must reach the relay and the client as it is.
      encodeBody: "manual",
    });
  } catch {
    return refuse(502, "upstream_unavailable");
  }
  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out, encodeBody: "manual" });
}
