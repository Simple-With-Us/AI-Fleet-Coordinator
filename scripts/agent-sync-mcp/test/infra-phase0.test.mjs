import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { z } from "zod";
import { ROOT } from "./helpers.mjs";

// probe.stdout crosses a process boundary:  it is another program's JSON, not
// this test's own data.  Decoding it with JSON.parse alone means a mistyped or
// truncated probe is discovered inside an assertion that reads it as something
// else, so the shape is checked once, here, where the failure names itself.
const SecretRunSchema = z.object({
  raised: z.string().nullish(),
  error: z.string().nullish(),
  exit_code: z.union([z.string(), z.number()]).nullish(),
  signals: z.array(z.tuple([z.number(), z.number()])),
  start_new_session: z.boolean().nullish(),
  child_cwd: z.string(),
  child_env_has_key: z.boolean(),
  sigterm_handler_installed: z.boolean(),
});

const ProbeSchema = z.object({
  hosts_after_tunnel: z.array(z.string().nullable()),
  deleted_record_ids: z.array(z.string()),
  queried: z.array(z.string()),
  output: z.string(),
  malformed_errors: z.record(z.string(), z.string()),
  tunnel_config_reads: z.number(),
  absent_zone: z.object({
    deleted: z.array(z.string()),
    raised: z.string().nullable(),
    output: z.array(z.string()),
  }),
  secrets: z.object({
    diagnostic: SecretRunSchema,
    quiet_failure: SecretRunSchema,
    no_wrangler_on_path: SecretRunSchema,
    popen_missing: SecretRunSchema,
    interrupted: SecretRunSchema,
    api_key_labelled: SecretRunSchema,
    short_id: SecretRunSchema,
    sigterm_handler_restored: z.boolean(),
  }),
});

function readProbe() {
  const probe = spawnSync("python3", [path.join(ROOT, "test", "infra_phase0_probe.py")], {
    encoding: "utf8",
  });
  assert.equal(probe.status, 0, probe.stderr);
  const parsed = ProbeSchema.safeParse(JSON.parse(probe.stdout));
  if (!parsed.success) {
    assert.fail(
      `probe output must match the schema: ${JSON.stringify(parsed.error.issues.slice(0, 3))}`,
    );
  }
  return parsed.data;
}

// The hard cutover stopped SERVING agent-sync.jays.services, but a public
// hostname still aimed at the Mac's local dev port with no Access in front of
// it is a standing risk.  These cover the cleanup and the post-deploy checks
// that have to keep seeing the retired host after HOST was repointed.
test("tunnel --apply drops the retired host's ingress rule as well as the served one", () => {
  const result = readProbe();
  // Before the fix, HOST alone was removed and the retired hostname survived.
  assert.deepEqual(result.hosts_after_tunnel, [null], "only the catch-all rule may remain");
  assert.match(result.output, /remove: agent-sync\.jays\.services -> http:\/\/localhost:8787/);
  assert.match(result.output, /remove: agents\.simplewithus\.com ->/);
});

test("dns --apply finds the tunnel CNAME in both the new and the retired zone", () => {
  const { queried, deleted_record_ids: deleted } = readProbe();
  // The retired host's CNAME lives in jays.services;  scanning only
  // simplewithus.com reported "nothing to delete" while it still resolved.
  assert.deepEqual(queried, [
    "jays.services:agent-sync.jays.services",
    "simplewithus.com:agents.simplewithus.com",
  ]);
  assert.deepEqual(deleted, ["rec-new", "rec-old"]);
});

test("a retired zone that is not on the account does not abort dns --apply", () => {
  const { absent_zone: absent } = readProbe();
  // The served zone is walked first, so a fatal retired-zone lookup aborts
  // after that deletion and leaves the stale CNAME behind with a bare error.
  assert.equal(absent.raised, null, `dns --apply aborted: ${absent.raised}`);
  assert.deepEqual(absent.deleted, ["rec-new"], "the served zone's record is still deleted");
  assert.ok(
    absent.output.some((line) => /jays\.services is not on account/.test(line)),
    `the absent zone is reported, not swallowed: ${JSON.stringify(absent.output)}`,
  );
});

test("a malformed tunnel answer is refused by name, not crashed on", () => {
  const { malformed_errors: malformedErrors } = readProbe();
  // Every caller reads rule["hostname"] / rule["service"];  an answer that is
  // not the documented shape has to stop with a precise error, because in
  // `check` a crash reads as "could not verify" rather than "not understood".
  for (const [label, message] of Object.entries(malformedErrors)) {
    assert.notEqual(message, "ACCEPTED", `${label}: a malformed ingress was accepted`);
    assert.match(message, /^error: tunnel tunnel-1 /, `${label}: ${message}`);
  }
});

test("a failing secret put reports only allow-listed fields of wrangler's output", () => {
  const { diagnostic, quiet_failure: quiet } = readProbe().secrets;
  // The exit code plus the two scalar fields wrangler documents are what an
  // operator can act on:  a wrong account or a missing Worker both land here.
  assert.equal(diagnostic.raised, "SystemExit");
  assert.match(diagnostic.error, /wrangler secret put ACCESS_AUD failed \(exit 1\)/);
  assert.match(diagnostic.error, /cf code 10000/);
  assert.match(diagnostic.error, /request id 8f14e45fceea167a5a36dedd4bea2543/);
  // The probe's failing answer carries the secret it was fed, the API key that
  // authenticated, and a response body with an Authorization header.  Replacing
  // known secrets is a denylist;  none of it may reach the error at all.
  assert.doesNotMatch(diagnostic.error, /b{64}/, "the secret value leaked");
  assert.doesNotMatch(diagnostic.error, /cf-api-key-value/, "the Cloudflare API key leaked");
  assert.doesNotMatch(
    diagnostic.error,
    /authorization|bearer|authentication error|"errors"/i,
    "raw wrangler output leaked",
  );
  // With nothing allow-listed in the output, the exit code is the whole report.
  assert.match(quiet.error, /^error: wrangler secret put ACCESS_AUD failed \(exit 1\)$/);
});

test("tunnel --apply reads the configuration once, not twice", () => {
  const result = readProbe();
  // The PUT writes {**config, "ingress": keep}:  a second fetch between the
  // two reads would let a concurrent change be silently reverted and missed
  // by the backup.
  assert.equal(result.tunnel_config_reads, 1, "one GET configurations per tunnel --apply");
});

test("a request id shaped like the API key is withheld, and a short id is ignored", () => {
  const { api_key_labelled: labelled, short_id: short } = readProbe().secrets;
  // Wrangler labelled the API key itself as the request id:  the whole detail
  // is withheld rather than relayed into the error and CI logs.
  assert.equal(labelled.raised, "SystemExit");
  assert.match(
    labelled.error,
    /^error: wrangler secret put ACCESS_AUD failed \(exit 1\)$/,
    "no allow-listed detail may survive when it contains the key",
  );
  assert.doesNotMatch(labelled.error, /c{32}/, "the Cloudflare API key leaked");
  // A request id that is not Cloudflare's fixed 32-hex shape is not relayed.
  assert.match(short.error, /cf code 10001/);
  assert.doesNotMatch(short.error, /request id/, "a short lookalike is not a request id");
});

test("the probe restores the caller's SIGTERM handler", () => {
  const { sigterm_handler_restored: restored } = readProbe().secrets;
  assert.equal(restored, true, "cleanup must not reset a custom handler to SIG_DFL");
});

test("a missing wrangler executable exits 127, not 1", () => {
  const { no_wrangler_on_path: absent, popen_missing: vanished } = readProbe().secrets;
  // 127 is the shell's "command not found";  a string SystemExit would report a
  // plain failure that automation cannot tell apart from a rejected secret.
  assert.equal(absent.exit_code, 127, "nothing on PATH at all");
  assert.equal(vanished.exit_code, 127, "the executable disappeared between the check and the run");
});

test("an interrupted provisioning run does not leave wrangler running", () => {
  const { interrupted } = readProbe().secrets;
  // The child runs in its own session, so a signal aimed at this process never
  // reaches it and only a process-group signal ends the real work:  wrangler is
  // an npx shim that spawns node underneath.
  assert.equal(interrupted.start_new_session, true);
  assert.equal(interrupted.child_cwd, path.resolve(ROOT), "wrangler runs in the Worker directory");
  assert.equal(interrupted.child_env_has_key, true, "wrangler gets the Cloudflare credentials");
  // A SIGTERM handler is installed for the run, so a SIGTERM to this process
  // takes the child with it instead of orphaning it mid-secret.
  assert.equal(interrupted.sigterm_handler_installed, true);
  assert.deepEqual(
    interrupted.signals,
    [[424242, 15]],
    "the child's process group is signalled with SIGTERM on the way out",
  );
  assert.equal(interrupted.raised, "KeyboardInterrupt", "the interrupt is not swallowed");
});