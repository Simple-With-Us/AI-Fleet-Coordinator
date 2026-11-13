// node --test scripts/ios-fleet/build_number_audit.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import {
  EXIT_FAILED,
  EXIT_OK,
  EXIT_STRICT,
  FLEET_BUILD_TIMESTAMP_RE,
  auditAppBuilds,
  auditBuildFormat,
  auditIosMacPair,
  auditMonotonic,
  auditMarketing,
  buildNumberValue,
  compareSemver,
  effectiveExpectedMarketing,
  exitCodeForReport,
  expectedMarketingBase,
  fetchApprovedVersion,
  invalidBuildDetail,
  isFleetBuildNumber,
  maxApprovedMarketingVersion,
  normalizeAscBuilds,
  parseAuditArgs,
  productGroupKey,
  renderAuditTable,
  runAudit,
  scopeBuildsForAudit,
} from "./build_number_audit.mjs";

import { loadRegistry } from "./ios_testflight_freshness.mjs";

const VALID = "202611091400";
const VALID_OLD = "202611080600";
const COUNTER = "2320";
const SHORT = "235";

function buildRow(build, marketing, uploaded, platform = "IOS") {
  return {
    build,
    marketingVersion: marketing,
    platform,
    uploadedAt: Date.parse(uploaded),
    processingState: "VALID",
    expired: false,
  };
}

test("parseAuditArgs", () => {
  const o = parseAuditArgs(["--json", "--strict", "--all-history", "--app", "a,b", "--limit", "5"]);
  assert.equal(o.json, true);
  assert.equal(o.strict, true);
  assert.equal(o.allHistory, true);
  assert.deepEqual(o.apps, ["a", "b"]);
  assert.equal(o.limit, 5);
});

test("parseAuditArgs rejects empty --app", () => {
  assert.throws(() => parseAuditArgs(["--app", ""]), /--app needs at least one app key/);
  assert.throws(() => parseAuditArgs(["--app", "  ,  "]), /--app needs at least one app key/);
});

test("productGroupKey pairs Mac suffix", () => {
  assert.equal(productGroupKey("contactlogo-mac"), "contactlogo");
  assert.equal(productGroupKey("congress"), "congress");
});

test("expected marketing baseline", () => {
  assert.equal(expectedMarketingBase("botfleet"), "0.1.0");
  assert.equal(expectedMarketingBase("congress"), "1.1.2");
  assert.equal(effectiveExpectedMarketing("botfleet", "0.2.0"), "0.2.0");
  assert.equal(effectiveExpectedMarketing("congress", "1.0.0"), "1.1.2");
});

test("compareSemver", () => {
  assert.equal(compareSemver("0.1.0", "0.1.0"), 0);
  assert.ok(compareSemver("1.1.2", "1.1.1") > 0);
  assert.ok(compareSemver("0.0.9", "0.1.0") < 0);
});

test("fleet build number format", () => {
  assert.ok(isFleetBuildNumber(VALID));
  assert.ok(isFleetBuildNumber(VALID_OLD));
  assert.ok(!isFleetBuildNumber(COUNTER));
  assert.ok(!isFleetBuildNumber(SHORT));
  assert.ok(!isFleetBuildNumber("1.0.2358"));
  assert.ok(!isFleetBuildNumber(""));
  assert.ok(!isFleetBuildNumber(null));
  assert.ok(FLEET_BUILD_TIMESTAMP_RE.test(VALID));
  assert.ok(!FLEET_BUILD_TIMESTAMP_RE.test(COUNTER));
});

test("finding: invalid build format", () => {
  assert.equal(auditBuildFormat([buildRow(SHORT, "0.1.0", "2026-11-09T20:00:00Z")], "demo", "IOS").length, 1);
  assert.equal(
    auditBuildFormat([buildRow(COUNTER, "0.1.0", "2026-11-09T20:00:00Z")], "demo", "IOS").length,
    1,
  );
  assert.equal(
    auditBuildFormat([buildRow(VALID, "0.1.0", "2026-11-09T20:00:00Z")], "demo", "IOS").length,
    0,
  );
  assert.match(invalidBuildDetail(COUNTER), /main-merge timestamp/);
});

test("finding: missing build number", () => {
  const f = auditBuildFormat([buildRow("", "0.1.0", "2026-11-09T20:00:00Z")], "demo", "IOS");
  assert.equal(f.length, 1);
  assert.match(f[0].detail, /missing/);
});

test("finding: non-monotonic uploads (adjacent only)", () => {
  const builds = [
    buildRow(VALID, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow(VALID_OLD, "0.1.0", "2026-11-08T12:00:00Z"),
  ];
  const swapped = [
    buildRow(VALID_OLD, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow(VALID, "0.1.0", "2026-11-08T12:00:00Z"),
  ];
  assert.equal(auditMonotonic(builds, "demo", "IOS").length, 0);
  assert.equal(auditMonotonic(swapped, "demo", "IOS").length, 1);
  assert.equal(buildNumberValue(VALID) > buildNumberValue(VALID_OLD), true);
});

test("finding: ios/mac mismatch", () => {
  const platformBuilds = {
    IOS: [buildRow("202611091500", "0.1.0", "2026-11-09T21:00:00Z", "IOS")],
    MAC_OS: [buildRow("202611091400", "0.1.0", "2026-11-09T20:00:00Z", "MAC_OS")],
  };
  const f = auditIosMacPair("contactlogo", platformBuilds);
  assert.equal(f.length, 1);
  assert.equal(f[0].type, "ios_mac_build_mismatch");
});

test("finding: missing train counterpart on current-only pair", () => {
  const platformBuilds = {
    IOS: [buildRow(VALID, "0.1.0", "2026-11-09T21:00:00Z", "IOS")],
    MAC_OS: [buildRow(VALID_OLD, "0.0.9", "2026-10-01T12:00:00Z", "MAC_OS")],
  };
  const f = auditIosMacPair("contactlogo", platformBuilds);
  assert.equal(f.length, 1);
  assert.match(f[0].detail, /missing/);
});

test("finding: marketing drift", () => {
  const builds = [buildRow(VALID, "0.2.0", "2026-11-09T20:00:00Z")];
  const f = auditMarketing(builds, "botfleet", "IOS", null);
  assert.equal(f.length, 1);
  assert.equal(f[0].type, "marketing_version_drift");
  const ok = auditMarketing([buildRow(VALID, "0.1.0", "2026-11-09T20:00:00Z")], "botfleet", "IOS", null);
  assert.equal(ok.length, 0);
});

test("marketing drift skipped when approved store fetch failed", () => {
  const builds = [buildRow(VALID, "0.2.0", "2026-11-09T20:00:00Z")];
  const findings = auditAppBuilds({
    appKey: "botfleet",
    platform: "IOS",
    builds,
    approvedStoreVersion: null,
    approvedStoreError: "App Store Connect timeout",
    allHistory: false,
  });
  assert.ok(!findings.some((f) => f.type === "marketing_version_drift"));
});

test("clean app: no findings", () => {
  const builds = [
    buildRow(VALID, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow(VALID_OLD, "0.1.0", "2026-11-08T12:00:00Z"),
  ];
  const findings = auditAppBuilds({ appKey: "botfleet", platform: "IOS", builds, approvedStoreVersion: null });
  assert.deepEqual(findings, []);
});

test("current-only scope ignores old-scheme history", () => {
  const builds = [
    buildRow(VALID, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow(SHORT, "1.0.50", "2026-10-28T18:00:00Z"),
    buildRow(COUNTER, "0.0.9", "2026-09-01T12:00:00Z"),
  ];
  const scoped = scopeBuildsForAudit(builds, { allHistory: false });
  assert.equal(scoped.scope, "current");
  assert.equal(scoped.formatBuilds[0].build, VALID);

  const current = auditAppBuilds({
    appKey: "codecaps-mac",
    platform: "MAC_OS",
    builds,
    approvedStoreVersion: null,
    allHistory: false,
  });
  assert.deepEqual(current, []);

  const historical = auditAppBuilds({
    appKey: "codecaps-mac",
    platform: "MAC_OS",
    builds,
    approvedStoreVersion: null,
    allHistory: true,
  });
  assert.ok(historical.length > 0);
  assert.ok(historical.some((f) => f.type === "invalid_build_number"));
});

test("exitCodeForReport strict flag", () => {
  const report = { findings: [{ type: "x" }], errors: [] };
  assert.equal(exitCodeForReport(report), EXIT_OK);
  assert.equal(exitCodeForReport(report, { strict: true }), EXIT_STRICT);
  assert.equal(exitCodeForReport({ findings: [], errors: ["boom"] }), EXIT_FAILED);
});

test("normalizeAscBuilds and maxApprovedMarketingVersion", () => {
  const json = {
    data: [
      {
        attributes: { version: VALID, uploadedDate: "2026-11-09T20:00:00Z", processingState: "VALID" },
        relationships: { preReleaseVersion: { data: { id: "p1" } } },
      },
    ],
    included: [{ type: "preReleaseVersions", id: "p1", attributes: { version: "0.1.0", platform: "IOS" } }],
  };
  const builds = normalizeAscBuilds(json, "IOS");
  assert.equal(builds[0].build, VALID);
  const store = {
    data: [
      { attributes: { versionString: "0.2.0", appStoreState: "READY_FOR_SALE" } },
      { attributes: { versionString: "0.1.0", appStoreState: "PREPARE_FOR_SUBMISSION" } },
    ],
  };
  assert.equal(maxApprovedMarketingVersion(store), "0.2.0");
});

test("fetchApprovedVersion surfaces errors", async () => {
  const bad = await fetchApprovedVersion({
    asc: async () => {
      throw new Error("HTTP 503");
    },
  }, "1");
  assert.equal(bad.version, null);
  assert.match(bad.error, /503/);
});

test("runAudit integration with stubbed ASC", async () => {
  const registry = loadRegistry(new URL("./testflight-apps.json", import.meta.url).pathname);
  const only = {
    owner: registry.owner,
    apps: {
      clean: {
        displayName: "Clean",
        appleId: "9001",
        bundleId: "clean.ios",
        platform: "IOS",
        repo: "Clean",
        paths: ["ios"],
        ship: { workflow: "ios-ship.yml", inputs: {} },
        compileCheck: "test",
        autoRemedy: false,
      },
    },
  };

  const buildsJson = {
    data: [
      {
        attributes: {
          version: VALID,
          uploadedDate: "2026-11-09T20:00:00Z",
          processingState: "VALID",
          expired: false,
        },
        relationships: { preReleaseVersion: { data: { id: "p1" } } },
      },
    ],
    included: [{ type: "preReleaseVersions", id: "p1", attributes: { version: "0.1.0", platform: "IOS" } }],
  };

  const deps = {
    now: () => new Date("2026-11-09T22:00:00Z"),
    asc: async (p) => {
      if (p.includes("/appStoreVersions")) return { data: [] };
      if (p.includes("filter[app]=9001")) return buildsJson;
      throw new Error(`unexpected asc ${p}`);
    },
  };

  const report = await runAudit({ apps: ["clean"], limit: 5 }, deps, only);
  assert.equal(report.findings.length, 0);
  assert.match(renderAuditTable(report), /No findings/);
});

test("main rejects empty --app via parseAuditArgs path", async () => {
  const { main } = await import("./build_number_audit.mjs");
  const code = await main(["--app", ""], { now: () => new Date() });
  assert.equal(code, EXIT_FAILED);
});
