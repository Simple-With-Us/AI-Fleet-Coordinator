import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, rmSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCRIPT = join(ROOT, "scripts/ios-fleet/publish-ios-versions.sh");

const FLEET = {
  schemaVersion: 1,
  updatedAt: "2026-08-24T07:44:52Z",
  apps: {
    "com.socratictrade.ios": {
      displayName: "Socratic-Trade",
      marketingVersion: "1.0.68",
      appleId: 1111111111,
    },
    "trade.congress.ios": {
      displayName: "Congress.Trade",
      marketingVersion: "1.0.80",
      appleId: 1111111112,
    },
    "net.dealdex": {
      marketingVersion: "1.0.2",
      build: "202608230250",
      appleId: 1111111113,
      displayName: "DealDex",
    },
  },
};

function run(args, env = {}) {
  return spawnSync("bash", [SCRIPT, ...args], { encoding: "utf8", env: { ...process.env, FLEET_ICON_AUDIT: "off", ...env } });
}

function fixture(name, doc) {
  const dir = mkdtempSync(join(tmpdir(), name));
  const base = join(dir, "remote.json");
  const out = join(dir, "out.json");
  writeFileSync(base, JSON.stringify(doc, null, 2) + "\n");
  return { dir, base, out };
}

test("merge onto a multi-app remote keeps every sibling", () => {
  const { dir, base, out } = fixture("afc-ios-versions-", FLEET);
  const res = run([
    "--bundle-id",
    "com.socratictrade.ios",
    "--version",
    "1.0.69",
    "--build",
    "202608250045",
    "--apple-id",
    "1111111111",
    "--display-name",
    "Socratic-Trade",
    "--base-json",
    base,
    "--out-json",
    out,
    "--skip-push",
    "--skip-local-write",
  ]);
  assert.equal(res.status, 0, res.stderr || res.stdout);
  const merged = JSON.parse(readFileSync(out, "utf8"));
  assert.deepEqual(Object.keys(merged.apps).sort(), Object.keys(FLEET.apps).sort());
  assert.equal(merged.apps["com.socratictrade.ios"].marketingVersion, "1.0.69");
  assert.equal(merged.apps["com.socratictrade.ios"].build, "202608250045");
  assert.equal(merged.apps["net.dealdex"].marketingVersion, "1.0.2");
  assert.equal(merged.apps["trade.congress.ios"].marketingVersion, "1.0.80");
  rmSync(dir, { recursive: true, force: true });
});

test("empty base is refused so a missing local file cannot wipe the fleet", () => {
  const { dir, base, out } = fixture("afc-ios-versions-empty-", { schemaVersion: 1, apps: {} });
  const res = run([
    "--bundle-id",
    "com.socratictrade.ios",
    "--version",
    "1.0.69",
    "--base-json",
    base,
    "--out-json",
    out,
    "--skip-push",
    "--skip-local-write",
  ]);
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /no apps|refusing/i);
  rmSync(dir, { recursive: true, force: true });
});

test("a single-app base stays a single-app document", () => {
  const { dir, base, out } = fixture("afc-ios-versions-one-app-", {
    schemaVersion: 1,
    apps: { "com.socratictrade.ios": { marketingVersion: "1.0.68" } },
  });
  const res = run([
    "--bundle-id",
    "com.socratictrade.ios",
    "--version",
    "1.0.69",
    "--base-json",
    base,
    "--out-json",
    out,
    "--skip-push",
    "--skip-local-write",
  ]);
  // A single-app base is a one-app remote, so one app stays one app:  the
  // refusal is about the merged document shrinking a multi-app remote.
  assert.equal(res.status, 0, res.stderr || res.stdout);
  const merged = JSON.parse(readFileSync(out, "utf8"));
  assert.deepEqual(Object.keys(merged.apps), ["com.socratictrade.ios"]);
  rmSync(dir, { recursive: true, force: true });
});

test("a version and build the remote already records is a no-op", () => {
  const doc = JSON.parse(JSON.stringify(FLEET));
  doc.apps["com.socratictrade.ios"].build = "202608250045";
  const { dir, base } = fixture("afc-ios-versions-noop-", doc);
  const res = run(
    [
      "--bundle-id",
      "com.socratictrade.ios",
      "--version",
      "1.0.68",
      "--build",
      "202608250045",
      "--base-json",
      base,
      "--skip-local-write",
    ],
    { GH_TOKEN: "", GITHUB_TOKEN: "" },
  );
  // The check runs before the credential is resolved, so a ship with nothing
  // to publish needs no token, reaches no network, and leaves no pull request.
  assert.equal(res.status, 0, res.stderr || res.stdout);
  assert.match(res.stdout, /already recorded/);
  assert.doesNotMatch(res.stdout, /pull/);
  rmSync(dir, { recursive: true, force: true });
});

test("the same version with a new build is NOT a no-op", () => {
  const doc = JSON.parse(JSON.stringify(FLEET));
  doc.apps["com.socratictrade.ios"].build = "202608250045";
  const { dir, base } = fixture("afc-ios-versions-newbuild-", doc);
  const res = run(
    [
      "--bundle-id",
      "com.socratictrade.ios",
      "--version",
      "1.0.68",
      "--build",
      "202608259999",
      "--base-json",
      base,
      "--skip-local-write",
    ],
    { GH_TOKEN: "", GITHUB_TOKEN: "ghs_notarealtoken" },
  );
  // It got past the no-op check and reached the credential, which is refused.
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /GITHUB_TOKEN cannot write to/);
  rmSync(dir, { recursive: true, force: true });
});

test("the publish never writes to the default branch", () => {
  const src = readFileSync(SCRIPT, "utf8");
  // Every contents write names the publish branch, so no write can land on
  // the default branch:  a ruleset with required status checks refuses that.
  const putCount = (src.match(/--method PUT/g) || []).length;
  const branchCount = (src.match(/-f branch="\$\{PR_BRANCH\}"/g) || []).length;
  assert.ok(putCount >= 1, "the script writes the document with a contents PUT");
  assert.equal(putCount, branchCount, "every contents write names the publish branch");
  assert.match(src, /git\/refs\/heads\//);
  assert.match(src, /--method POST\s+"repos\/\$\{REPO\}\/pulls"/);
  assert.match(src, /repos\/\$\{REPO\}\/pulls\?head=/);
});

test("the publish refuses the workflow's own read-only GITHUB_TOKEN", () => {
  const src = readFileSync(SCRIPT, "utf8");
  assert.match(src, /GITHUB_TOKEN cannot write to/);
  const { dir, base } = fixture("afc-ios-versions-token-", FLEET);
  const res = run(
    ["--bundle-id", "com.socratictrade.ios", "--version", "1.0.69", "--base-json", base, "--skip-local-write"],
    { GITHUB_TOKEN: "ghs_notarealtoken", GH_TOKEN: "" },
  );
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /GITHUB_TOKEN cannot write to/);
  rmSync(dir, { recursive: true, force: true });
});

test("the publish reads the live remote and refuses an empty apps map", () => {
  const src = readFileSync(SCRIPT, "utf8");
  assert.match(src, /fetch_remote_json/);
  assert.match(src, /refusing to seed from the local snapshot/);
  assert.match(src, /--skip-push/);
  assert.doesNotMatch(src, /data = \{"schemaVersion": 1, "apps": \{\}\}/);
});

test("the published document keeps exactly one trailing newline", () => {
  const { dir, base, out } = fixture("afc-ios-versions-newline-", FLEET);
  const res = run([
    "--bundle-id",
    "com.socratictrade.ios",
    "--version",
    "1.0.69",
    "--build",
    "202608250045",
    "--base-json",
    base,
    "--out-json",
    out,
    "--skip-push",
    "--skip-local-write",
  ]);
  assert.equal(res.status, 0, res.stderr || res.stdout);
  // The merge runs inside a command substitution, and a command substitution
  // strips trailing newlines.  The write has to put one back, or the file
  // lands in the repository without its final newline and every later commit
  // shows a spurious "no newline at end of file" line.
  const raw = readFileSync(out, "utf8");
  assert.ok(raw.endsWith("\n"), "the manifest must end with a newline");
  assert.ok(!raw.endsWith("\n\n"), "the manifest must end with exactly one newline");
  rmSync(dir, { recursive: true, force: true });
});

test("the manifest target is the org repository the apps read", () => {
  const src = readFileSync(SCRIPT, "utf8");
  assert.match(src, /Simple-With-Us\/AI-Fleet-Coordinator/);
  assert.match(src, /site\/ios-versions\.json/);
});

// ---- stubbed gh: the publish path without a network ------------------------

function stubDir(opts = {}) {
  const dir = mkdtempSync(join(tmpdir(), "afc-ios-stub-"));
  const live = join(dir, "live.json");
  writeFileSync(live, JSON.stringify(opts.live || FLEET, null, 2) + "\n");
  const gh = `#!/usr/bin/env bash
echo "gh $*" >> "${dir}/calls.log"
args="$*"
if [[ "$1" == "pr" ]]; then
  [[ "${opts.mergeFail ? 1 : 0}" == "1" ]] && exit 1
  exit 0
fi
if [[ "$args" == *"@tsv"* ]]; then
  n=$(cat "${dir}/reads" 2>/dev/null || echo 0); echo $((n+1)) > "${dir}/reads"
  sha=sha-one; [[ "${opts.moves ? 1 : 0}" == "1" && $n -ge 1 ]] && sha=sha-two
  printf '%s\\t%s\\n' "$sha" "$(base64 < "${dir}/live.json" | tr -d '\\n')"
  exit 0
fi
case "$args" in
  *"git/ref/heads/main"*) echo basesha ;;
  *"git/ref/heads/ios-versions-publish"*) exit 1 ;;
  *"git/refs"*) ;;
  *"contents/site/ios-versions.json?ref=ios-versions-publish"*) exit 1 ;;
  *"--method PUT"*) ;;
  *"pulls?head="*) ;;
  *"--method POST"*"/pulls"*) echo https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/1 ;;
esac
exit 0
`;
  writeFileSync(join(dir, "gh"), gh);
  chmodSync(join(dir, "gh"), 0o755);
  return dir;
}

function publish(stub, extra = [], env = {}) {
  return run(
    ["--bundle-id", "com.socratictrade.ios", "--version", "1.0.69", "--skip-local-write", ...extra],
    { PATH: `${stub}:${process.env.PATH}`, GH_TOKEN: "ghp_notarealtoken", GITHUB_TOKEN: "", ...env },
  );
}

test("a failed auto-merge arm exits nonzero after the pull request exists", () => {
  const stub = stubDir({ mergeFail: true });
  const res = publish(stub);
  assert.notEqual(res.status, 0);
  assert.match(res.stdout, /version-manifest-pr https:\/\/github.com/);
  assert.match(res.stderr, /auto-merge could not be armed/);
  rmSync(stub, { recursive: true, force: true });
});

test("a successful publish arms auto-merge and exits zero", () => {
  const stub = stubDir();
  const res = publish(stub);
  assert.equal(res.status, 0, res.stderr || res.stdout);
  assert.match(res.stdout, /auto-merge armed/);
  rmSync(stub, { recursive: true, force: true });
});

test("a concurrent publish (the remote blob moved) is refused before any write", () => {
  const stub = stubDir({ moves: true });
  const res = publish(stub);
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /changed on main since it was read/);
  const calls = readFileSync(join(stub, "calls.log"), "utf8");
  assert.doesNotMatch(calls, /--method (PUT|POST|PATCH)/);
  rmSync(stub, { recursive: true, force: true });
});

test("a document that drops an app the live remote has is refused", () => {
  const stub = stubDir();
  const { dir, base } = fixture("afc-ios-versions-shrink-", {
    schemaVersion: 1,
    apps: { "com.socratictrade.ios": { marketingVersion: "1.0.68" }, "other.app": { marketingVersion: "1.0.0" } },
  });
  const res = publish(stub, ["--base-json", base]);
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /drop apps the live remote has/);
  assert.doesNotMatch(readFileSync(join(stub, "calls.log"), "utf8"), /--method (PUT|POST|PATCH)/);
  rmSync(stub, { recursive: true, force: true });
  rmSync(dir, { recursive: true, force: true });
});

test("there is no local-snapshot fallback when the live manifest cannot be read", () => {
  const stub = mkdtempSync(join(tmpdir(), "afc-ios-stub-"));
  writeFileSync(join(stub, "gh"), "#!/usr/bin/env bash\nexit 1\n");
  chmodSync(join(stub, "gh"), 0o755);
  const res = publish(stub);
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /could not read the live manifest/);
  rmSync(stub, { recursive: true, force: true });
});

test("a change to the Apple ID or display name is not a no-op", () => {
  const { dir, base } = fixture("afc-ios-versions-meta-", FLEET);
  const res = run(
    ["--bundle-id", "com.socratictrade.ios", "--version", "1.0.68", "--apple-id", "1111111199", "--base-json", base, "--skip-local-write"],
    { GH_TOKEN: "", GITHUB_TOKEN: "ghs_notarealtoken" },
  );
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /GITHUB_TOKEN cannot write to/);
  rmSync(dir, { recursive: true, force: true });
});

test("the no-op check compares the base as read, even when --out-json is the same file", () => {
  const { dir, base } = fixture("afc-ios-versions-same-", FLEET);
  const res = run(
    ["--bundle-id", "com.socratictrade.ios", "--version", "1.0.69", "--base-json", base, "--out-json", base, "--skip-local-write"],
    { GH_TOKEN: "", GITHUB_TOKEN: "ghs_notarealtoken" },
  );
  assert.notEqual(res.status, 0);
  assert.doesNotMatch(res.stdout, /already recorded/);
  rmSync(dir, { recursive: true, force: true });
});

test("a bare gh login is not a credential; fleet-secrets GH_PAT is, or the run fails", () => {
  const { dir, base } = fixture("afc-ios-versions-pat-", FLEET);
  const stub = mkdtempSync(join(tmpdir(), "afc-ios-stub-"));
  writeFileSync(join(stub, "fleet-secrets"), "#!/usr/bin/env bash\nexit 1\n");
  chmodSync(join(stub, "fleet-secrets"), 0o755);
  writeFileSync(join(stub, "gh"), "#!/usr/bin/env bash\nexit 0\n");
  chmodSync(join(stub, "gh"), 0o755);
  const res = run(
    ["--bundle-id", "com.socratictrade.ios", "--version", "1.0.69", "--base-json", base, "--skip-local-write"],
    { PATH: `${stub}:${process.env.PATH}`, GH_TOKEN: "", GITHUB_TOKEN: "" },
  );
  assert.notEqual(res.status, 0);
  assert.match(res.stderr, /fleet-secrets could not return GH_PAT/);
  rmSync(dir, { recursive: true, force: true });
  rmSync(stub, { recursive: true, force: true });
});

test("an option with no value is a usage error, not an unbound variable", () => {
  const res = run(["--bundle-id", "com.socratictrade.ios", "--version"]);
  assert.equal(res.status, 2);
  assert.match(res.stderr, /--version needs a value/);
});

// ---- release gates: icon audit and sheet sync -----------------------------

function auditStub(code) {
  const dir = mkdtempSync(join(tmpdir(), "afc-ios-audit-"));
  const file = join(dir, "audit.mjs");
  writeFileSync(file, `process.exit(${code});\n`);
  return { dir, file };
}

test("the icon audit blocks the publish by default and on an audit error", () => {
  const stub = stubDir();
  const audit = auditStub(1);
  for (const env of [{ FLEET_ICON_AUDIT: "" }, { FLEET_ICON_AUDIT: "block" }]) {
    const res = publish(stub, [], { ...env, FLEET_ICON_AUDIT_SCRIPT: audit.file });
    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /icon audit failed or could not run/);
  }
  rmSync(stub, { recursive: true, force: true });
  rmSync(audit.dir, { recursive: true, force: true });
});

test("a missing audit script blocks too, warn continues, off skips", () => {
  const stub = stubDir();
  const missing = join(tmpdir(), "afc-no-such-audit.mjs");
  const blocked = publish(stub, [], { FLEET_ICON_AUDIT: "block", FLEET_ICON_AUDIT_SCRIPT: missing });
  assert.notEqual(blocked.status, 0);
  const audit = auditStub(1);
  const warned = publish(stub, [], { FLEET_ICON_AUDIT: "warn", FLEET_ICON_AUDIT_SCRIPT: audit.file });
  assert.equal(warned.status, 0, warned.stderr);
  assert.match(warned.stderr, /publishing anyway/);
  const off = publish(stub, [], { FLEET_ICON_AUDIT: "off", FLEET_ICON_AUDIT_SCRIPT: missing });
  assert.equal(off.status, 0, off.stderr);
  const bad = publish(stub, [], { FLEET_ICON_AUDIT: "maybe" });
  assert.equal(bad.status, 2);
  rmSync(stub, { recursive: true, force: true });
  rmSync(audit.dir, { recursive: true, force: true });
});

test("the sheet sync runs after the publish, a failure fails the run, a missing shim is skipped", () => {
  const stub = stubDir();
  const ok = auditStub(0);
  const bad = auditStub(1);
  const done = publish(stub, [], { FLEET_SHEET_SYNC: ok.file });
  assert.equal(done.status, 0, done.stderr);
  assert.match(done.stdout, /sheet-sync: done/);
  const failed = publish(stub, [], { FLEET_SHEET_SYNC: bad.file });
  assert.notEqual(failed.status, 0);
  assert.match(failed.stderr, /sheet sync failed/);
  const skipped = publish(stub, [], { FLEET_SHEET_SYNC: join(tmpdir(), "afc-no-shim.mjs") });
  assert.equal(skipped.status, 0, skipped.stderr);
  assert.match(skipped.stdout, /not present, skipped/);
  rmSync(stub, { recursive: true, force: true });
  rmSync(ok.dir, { recursive: true, force: true });
  rmSync(bad.dir, { recursive: true, force: true });
});
