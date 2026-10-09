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
