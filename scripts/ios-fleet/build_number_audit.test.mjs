// node --test scripts/ios-fleet/build_number_audit.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import {
  BUILD_NUMBER_RE,
  EXIT_OK,
  EXIT_STRICT,
  FLEET_TIMESTAMP_SCHEME_START_MS,
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
  maxApprovedMarketingVersion,
  normalizeAscBuilds,
  parseAuditArgs,
  productGroupKey,
  renderAuditTable,
  runAudit,
  scopeBuildsForAudit,
} from "./build_number_audit.mjs";

import { loadRegistry } from "./ios_testflight_freshness.mjs";

const VALID = "202611091430";
const VALID_OLD = "202611081200";

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

test("BUILD_NUMBER_RE", () => {
  assert.ok(BUILD_NUMBER_RE.test("202611091430"));
  assert.ok(!BUILD_NUMBER_RE.test("113"));
  assert.ok(!BUILD_NUMBER_RE.test("192611091430"));
});

test("finding: invalid build format", () => {
  const builds = [buildRow("113", "0.1.0", "2026-11-09T20:00:00Z")];
  const f = auditBuildFormat(builds, "demo", "IOS");
  assert.equal(f.length, 1);
  assert.equal(f[0].type, "invalid_build_number");
});

test("finding: non-monotonic uploads", () => {
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
    MAC_OS: [buildRow("202611091430", "0.1.0", "2026-11-09T20:00:00Z", "MAC_OS")],
  };
  const f = auditIosMacPair("contactlogo", platformBuilds);
  assert.equal(f.length, 1);
  assert.equal(f[0].type, "ios_mac_build_mismatch");
});

test("finding: marketing drift", () => {
  const builds = [buildRow(VALID, "0.2.0", "2026-11-09T20:00:00Z")];
  const f = auditMarketing(builds, "botfleet", "IOS", null);
  assert.equal(f.length, 1);
  assert.equal(f[0].type, "marketing_version_drift");
  const ok = auditMarketing([buildRow(VALID, "0.1.0", "2026-11-09T20:00:00Z")], "botfleet", "IOS", null);
  assert.equal(ok.length, 0);
  const congress = auditMarketing([buildRow(VALID, "1.1.2", "2026-11-09T20:00:00Z")], "congress", "IOS", null);
  assert.equal(congress.length, 0);
});

test("clean app: no findings", () => {
  const builds = [
    buildRow(VALID, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow(VALID_OLD, "0.1.0", "2026-11-08T12:00:00Z"),
  ];
  const findings = auditAppBuilds({ appKey: "botfleet", platform: "IOS", builds, approvedStoreVersion: null });
  assert.deepEqual(findings, []);
});

test("current-only scope ignores pre-scheme and stale trains", () => {
  const builds = [
    buildRow(VALID, "0.1.0", "2026-11-09T22:00:00Z"),
    buildRow("235", "1.0.50", "2026-10-28T18:00:00Z"),
    buildRow("99", "0.0.9", "2026-09-01T12:00:00Z"),
  ];
  const scoped = scopeBuildsForAudit(builds, { allHistory: false });
  assert.equal(scoped.scope, "current");
  assert.equal(scoped.formatBuilds.length, 1);
  assert.equal(scoped.formatBuilds[0].build, VALID);
  assert.equal(scoped.monotonicBuilds.length, 1);

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
  assert.ok(historical.some((f) => f.type === "marketing_version_drift"));
});

test("scheme start constant is 2026-10-29 CT midnight", () => {
  assert.equal(FLEET_TIMESTAMP_SCHEME_START_MS, Date.parse("2026-10-29T05:00:00.000Z"));
});

test("exitCodeForReport strict flag", () => {
  const report = { findings: [{ type: "x" }], errors: [] };
  assert.equal(exitCodeForReport(report), EXIT_OK);
  assert.equal(exitCodeForReport(report, { strict: true }), EXIT_STRICT);
  assert.equal(exitCodeForReport({ findings: [], errors: ["boom"] }), 2);
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
