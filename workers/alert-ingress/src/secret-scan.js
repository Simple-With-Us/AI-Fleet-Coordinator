// A small credential scanner for text that is about to be persisted or
// relayed to Zulip.
//
// The fleet already has one:  `scripts/agent_sync/secretscan.py` for the
// Python side and `scripts/agent-sync-mcp/src/contract.js` for the hosted MCP
// Worker.  This is the same list of kinds, kept next to the ingress that
// needs it, because this Worker ships without a bundler and cannot import
// across the repository without dragging the MCP tool contract into the
// bundle.  `secret-scan.test.js` runs the Python suite's own samples through
// this copy and through the MCP copy, so the three cannot drift apart
// silently.
//
// It reports the kind of match only, never the matched text:  a caller that
// logs the answer must not log the secret with it.

const SECRET_PATTERNS = [
  ["private key block", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ["AWS access key", /\b(AKIA|ASIA)[0-9A-Z]{16}\b/],
  ["GitHub token", /\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b/],
  ["Anthropic or OpenAI key", /\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b/],
  ["Slack token", /\bxox[abposr]-[A-Za-z0-9-]{10,}\b/],
  ["GitLab token", /\bglpat-[A-Za-z0-9_-]{20,}\b/],
  ["Google API key", /\bAIza[0-9A-Za-z_-]{35}\b/],
  ["Stripe key", /\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b/],
  // The fleet's own services, in the same order as the Python scanner:  the
  // specific kinds before the generic assignment rows, so the answer names
  // the service.
  ["Linear API key", /\blin_(api|oauth|wh)_[A-Za-z0-9]{20,}/],
  ["Linear client secret", /\blinear[\w -]{0,24}secret\s*[=:]\s*['"]?[A-Za-z0-9]{24,}/i],
  ["Sentry token", /\bsntry[su]_[A-Za-z0-9+/=_-]{20,}/],
  ["xAI key", /\bxai-[A-Za-z0-9]{20,}/],
  ["Infisical service token", /\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}/],
  [
    "Infisical credential",
    /\binfisical[\w .-]{0,40}?(token|secret)['"]?\s*[=:]\s*['"]?[A-Za-z0-9+/=._-]{24,}/i,
  ],
  ["access token (JWT)", /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/],
  ["Cloudflare token", /\bcf(ut|at|k)_[A-Za-z0-9]{30,}/],
  ["Cloudflare token", /\bcloudflare[\w ]{0,24}(token|key)\s*[=:]\s*['"]?[A-Za-z0-9_-]{37,}/i],
  ["Coolify token", /(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])/],
  [
    "assigned credential",
    /\b[A-Z][A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_?KEY)\s*=\s*['"]?[A-Za-z0-9+/=._-]{16,}/,
  ],
  [
    "bearer token",
    /\bauthorization:\s*(basic|bearer)\s+[A-Za-z0-9+/=._-]{16,}/i,
  ],
  [
    "assigned secret",
    /\b(api[_-]?key|secret|token|password)\s*[=:]\s*['"]?[A-Za-z0-9+/=._-]{16,}/i,
  ],
];
const ZULIP_SHAPED_RE = /(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])/g;

/**
 * The kind of secret in `text`, or null.
 *
 * The same contract as the Python scanner and the MCP copy:  the kind only,
 * never the matched text, so the answer is safe to log.
 */
export function scanForCredential(text) {
  if (typeof text !== "string" || !text) return null;
  for (const [kind, pattern] of SECRET_PATTERNS) {
    if (pattern.test(text)) return kind;
  }
  for (const match of text.matchAll(ZULIP_SHAPED_RE)) {
    const token = match[0];
    if (/[A-Z]/.test(token) && /[a-z]/.test(token) && /[0-9]/.test(token)) {
      return "a Zulip-shaped API key";
    }
  }
  return null;
}

export const __testing = { SECRET_PATTERNS };
