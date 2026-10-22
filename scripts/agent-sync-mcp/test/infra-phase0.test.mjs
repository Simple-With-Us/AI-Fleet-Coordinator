import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { ROOT } from "./helpers.mjs";

// The hard cutover stopped SERVING agent-sync.jays.services, but a public
// hostname still aimed at the Mac's local dev port with no Access in front of
// it is a standing risk.  These cover the cleanup and the post-deploy checks
// that have to keep seeing the retired host after HOST was repointed.
test("tunnel --apply drops the retired host's ingress rule as well as the served one", () => {
  const probe = spawnSync("python3", [path.join(ROOT, "test", "infra_phase0_probe.py")], {
    encoding: "utf8",
  });
  assert.equal(probe.status, 0, probe.stderr);
  const result = JSON.parse(probe.stdout);
  // Before the fix, HOST alone was removed and the retired hostname survived.
  assert.deepEqual(result.hosts_after_tunnel, [null], "only the catch-all rule may remain");
  assert.match(result.output, /remove: agent-sync\.jays\.services -> http:\/\/localhost:8787/);
  assert.match(result.output, /remove: agents\.simplewithus\.com ->/);
});

test("dns --apply finds the tunnel CNAME in both the new and the retired zone", () => {
  const probe = spawnSync("python3", [path.join(ROOT, "test", "infra_phase0_probe.py")], {
    encoding: "utf8",
  });
  assert.equal(probe.status, 0, probe.stderr);
  const result = JSON.parse(probe.stdout);
  // The retired host's CNAME lives in jays.services;  scanning only
  // simplewithus.com reported "nothing to delete" while it still resolved.
  assert.deepEqual(result.queried, [
    "jays.services:agent-sync.jays.services",
    "simplewithus.com:agents.simplewithus.com",
  ]);
  assert.deepEqual(result.deleted_record_ids, ["rec-new", "rec-old"]);
});