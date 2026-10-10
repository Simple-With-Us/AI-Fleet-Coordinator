// Zod schemas for the two HTML-form trust boundaries:  the consent POST to
// /authorize and the /admin/action POST (fleet rule:  validate every untrusted
// input with a zod schema at the trust boundary).  These are the only modules
// that import `zod` besides the MCP SDK, so the plain `node --test` run in
// ci.yml (no npm install) skips test/forms.test.mjs and the workerd workflow
// runs it.  The other gates (policy.js, config.js) stay import-free on purpose
// and are validated by explicit code and unit tests.
//
// Every schema is strict:  an unknown key, a repeated scalar key, a file part
// or a value outside its enum is refused, never ignored.

import { z } from "zod";
import { KNOWN_SEATS, SCOPES } from "./config.js";

// Consent handles are 256 random bits in base64url (43 characters).
const HANDLE = /^[A-Za-z0-9_-]{16,128}$/;
// Hand-registered client ids are 16 characters of [A-Za-z0-9_-] (see policy.js).
const HAND_CLIENT_ID = /^[A-Za-z0-9_-]{8,64}$/;

const seat = z.enum([...KNOWN_SEATS]);
const scope = z.enum([...SCOPES]);

export const CONSENT_ARRAY_KEYS = Object.freeze(["scope"]);

export const ConsentForm = z.strictObject({
  handle: z.string().regex(HANDLE),
  decision: z.enum(["approve", "deny"]),
  seat: seat.optional(),
  scope: z.array(scope).max(SCOPES.length).optional(),
});

export const ADMIN_ACTIONS = Object.freeze(["arm", "disarm", "pause", "unpause", "revoke", "create_manual_client", "sync_manual_client"]);

export const AdminForm = z.strictObject({
  csrf: z.string().regex(/^[0-9a-f]{64}$/),
  action: z.enum([...ADMIN_ACTIONS]),
  seat: seat.optional(),
  client_id: z.string().regex(HAND_CLIENT_ID).optional(),
});

/**
 * FormData to a plain object for a strict schema.  A repeated key becomes an
 * array (which every scalar schema refuses);  keys named in `arrayKeys` are
 * always arrays.  The object has no prototype, so a `__proto__` field is an
 * ordinary, unknown key.
 */
export function formObject(form, arrayKeys = []) {
  const out = Object.create(null);
  for (const key of new Set(form.keys())) {
    const all = form.getAll(key);
    out[key] = arrayKeys.includes(key) ? all : all.length === 1 ? all[0] : all;
  }
  return out;
}

/** `{ ok: true, data }` or `{ ok: false, reason }` where reason is a log-safe slug, never user input. */
export function parseForm(schema, form, arrayKeys = []) {
  const parsed = schema.safeParse(formObject(form, arrayKeys));
  if (parsed.success) return { ok: true, data: parsed.data };
  const issue = parsed.error.issues[0];
  return { ok: false, reason: `form_${issue?.code ?? "invalid"}` };
}
