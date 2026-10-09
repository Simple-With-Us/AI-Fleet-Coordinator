// Shared test helpers.  No dependencies:  these tests run in CI with plain
// `node --test`, before (and without) `npm ci`.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** Parse JSONC:  drop // and block comments outside strings. */
export function parseJsonc(text) {
  let out = "";
  let inString = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const next = text[i + 1];
    if (inString) {
      out += c;
      if (c === "\\") {
        out += next;
        i++;
      } else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') {
      inString = true;
      out += c;
    } else if (c === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") i++;
      out += "\n";
    } else if (c === "/" && next === "*") {
      i += 2;
      while (i < text.length && !(text[i] === "*" && text[i + 1] === "/")) i++;
      i++;
    } else out += c;
  }
  return JSON.parse(out);
}

export function wranglerConfig() {
  return parseJsonc(readFileSync(path.join(ROOT, "wrangler.jsonc"), "utf8"));
}

/** wrangler.jsonc vars, with a real-looking ACCESS_AUD so Access counts as configured. */
export function testEnv(overrides = {}) {
  const vars = { ...wranglerConfig().vars };
  vars.ACCESS_AUD = "a".repeat(64);
  return { ...vars, ...overrides };
}

export const CHATGPT_CLIENT = "https://chatgpt.com/oauth/client.json";
export const CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect";
export const RESOURCE = "https://agent-sync.jays.services/mcp";
export const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

export function authorizeParams(overrides = {}) {
  const base = {
    response_type: "code",
    client_id: CHATGPT_CLIENT,
    redirect_uri: CHATGPT_REDIRECT,
    code_challenge: CHALLENGE,
    code_challenge_method: "S256",
    resource: RESOURCE,
    scope: "zulip:read zulip:write",
    state: "s-123",
  };
  const merged = { ...base, ...overrides };
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(merged)) {
    if (v === undefined) continue;
    if (Array.isArray(v)) v.forEach((x) => params.append(k, x));
    else params.set(k, v);
  }
  return params;
}

export const ISSUER = "https://agent-sync.jays.services";

/**
 * The Origin header a browser sends on a form POST, per the Fetch Standard's
 * "append a request Origin header" (mode is not "cors", so the referrer policy
 * decides).  `policy` is the Referrer-Policy of the page that holds the form;
 * the form posts to `target` from a page at `document`.  Returns the header
 * value, or null when none is sent.  Browsers send "null" under `no-referrer`,
 * which is what broke Approve and Arm before this was pinned.
 */
export function browserPostOrigin({ policy, document = ISSUER, target = ISSUER }) {
  const docOrigin = new URL(document).origin;
  const targetUrl = new URL(target);
  // No header at all means the browser default, strict-origin-when-cross-origin.
  const effective = (policy ?? "").split(",").pop().trim().toLowerCase() || "strict-origin-when-cross-origin";
  switch (effective) {
    case "no-referrer":
      return "null";
    case "no-referrer-when-downgrade":
    case "strict-origin":
    case "strict-origin-when-cross-origin":
      return new URL(document).protocol === "https:" && targetUrl.protocol !== "https:" ? "null" : docOrigin;
    case "same-origin":
      return docOrigin === targetUrl.origin ? docOrigin : "null";
    default:
      return docOrigin;
  }
}
