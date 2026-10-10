// The zod form schemas need `zod` installed.  ci.yml runs the unit suites
// without npm install, so these cases skip there and run in the
// agent-sync-mcp.yml workflow (npm ci --ignore-scripts, then npm test).
import test from "node:test";
import assert from "node:assert/strict";

let forms = null;
try {
  forms = await import("../src/forms.js");
} catch (error) {
  // Skip only when the zod package itself is missing;  any other import failure is a real bug.
  if (error?.code !== "ERR_MODULE_NOT_FOUND" || !/'zod'/.test(String(error.message))) throw error;
}
const skip = forms === null ? "zod is not installed" : false;

const HANDLE = "h".repeat(43);
const CSRF = "c".repeat(64);
const mk = (pairs) => new URLSearchParams(pairs);
const consent = (pairs) => forms.parseForm(forms.ConsentForm, mk(pairs), forms.CONSENT_ARRAY_KEYS);
const admin = (pairs) => forms.parseForm(forms.AdminForm, mk(pairs), []);

test("consent form:  the shape the consent page posts is accepted", { skip }, () => {
  const ok = consent([["handle", HANDLE], ["seat", "JET"], ["decision", "approve"], ["scope", "zulip:read"], ["scope", "zulip:write"]]);
  assert.deepEqual(ok, { ok: true, data: { handle: HANDLE, seat: "JET", decision: "approve", scope: ["zulip:read", "zulip:write"] } });
  assert.equal(consent([["handle", HANDLE], ["decision", "deny"]]).ok, true, "deny needs no seat or scope");
  assert.deepEqual(consent([["handle", HANDLE], ["seat", "GROK-WEB"], ["decision", "approve"], ["scope", "zulip:write"]]).data.scope, ["zulip:write"]);
});

test("consent form:  extra keys, repeated scalars, bad enums and bad handles are refused, not ignored", { skip }, () => {
  const base = [["handle", HANDLE], ["seat", "JET"], ["decision", "approve"], ["scope", "zulip:read"]];
  const bad = {
    "extra key": [...base, ["redirect_uri", "https://evil.example/cb"]],
    "proto key": [...base, ["__proto__", "x"]],
    "repeated handle": [...base, ["handle", HANDLE]],
    "repeated decision": [...base, ["decision", "deny"]],
    "repeated seat": [...base, ["seat", "GROK-WEB"]],
    "unknown decision": base.map(([k, v]) => (k === "decision" ? [k, "maybe"] : [k, v])),
    "unknown seat": base.map(([k, v]) => (k === "seat" ? [k, "CLAUDE"] : [k, v])),
    "unknown scope": [...base, ["scope", "zulip:admin"]],
    "too many scopes": [...base, ["scope", "zulip:write"], ["scope", "zulip:write"]],
    "short handle": base.map(([k, v]) => (k === "handle" ? [k, "short"] : [k, v])),
    "handle with markup": base.map(([k, v]) => (k === "handle" ? [k, `${HANDLE}"><b>`] : [k, v])),
    "no handle": base.filter(([k]) => k !== "handle"),
    "no decision": base.filter(([k]) => k !== "decision"),
  };
  for (const [name, pairs] of Object.entries(bad)) {
    const got = consent(pairs);
    assert.equal(got.ok, false, name);
    assert.match(got.reason, /^form_[a-z_]+$/, `${name}:  the reason is a slug, never user input`);
  }
  // A file part is not a string.
  const withFile = new FormData();
  withFile.set("handle", new Blob(["x"]), "h.txt");
  withFile.set("decision", "approve");
  assert.equal(forms.parseForm(forms.ConsentForm, withFile, forms.CONSENT_ARRAY_KEYS).ok, false);
});

test("admin form:  every action the admin page posts is accepted, anything else is refused", { skip }, () => {
  for (const action of forms.ADMIN_ACTIONS) assert.equal(admin([["csrf", CSRF], ["action", action]]).ok, true, action);
  assert.equal(admin([["csrf", CSRF], ["action", "arm"], ["seat", "JET"]]).ok, true);
  assert.equal(admin([["csrf", CSRF], ["action", "sync_manual_client"], ["client_id", "AbCdEf1234567890"]]).ok, true);
  assert.equal(admin([["csrf", CSRF], ["action", "create_manual_client"], ["seat", "ECHO"]]).ok, true);
  assert.equal(admin([["csrf", CSRF], ["action", "create_manual_client"], ["seat", "INSTINCT"]]).ok, true);
  const bad = {
    "bad csrf shape": [["csrf", "short"], ["action", "arm"]],
    "uppercase csrf": [["csrf", "C".repeat(64)], ["action", "arm"]],
    "unknown action": [["csrf", CSRF], ["action", "delete_everything"]],
    "unknown seat": [["csrf", CSRF], ["action", "arm"], ["seat", "CLAUDE"]],
    "url client id": [["csrf", CSRF], ["action", "sync_manual_client"], ["client_id", "https://evil.example/c.json"]],
    "extra key": [["csrf", CSRF], ["action", "arm"], ["seat", "JET"], ["note", "x"]],
    "repeated action": [["csrf", CSRF], ["action", "arm"], ["action", "revoke"]],
    "no csrf": [["action", "arm"]],
  };
  for (const [name, pairs] of Object.entries(bad)) assert.equal(admin(pairs).ok, false, name);
});

test("forms:  the schemas follow the config seat and scope lists", { skip }, async () => {
  const { KNOWN_SEATS, SCOPES } = await import("../src/config.js");
  for (const seat of KNOWN_SEATS) assert.equal(consent([["handle", HANDLE], ["seat", seat], ["decision", "deny"]]).ok, true, seat);
  for (const s of SCOPES) assert.equal(consent([["handle", HANDLE], ["decision", "approve"], ["scope", s]]).ok, true, s);
});
