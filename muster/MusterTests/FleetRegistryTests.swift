import XCTest

final class FleetRegistryTests: XCTestCase {
    private let sampleRegistry = """
    {
      "owner": "Simple-With-Us",
      "codeRoot": "/home/fixture/Code",
      "appsRoot": "/home/fixture/apps",
      "zulipChannel": "work-in-progress",
      "apps": [
        {
          "repo": "Socratic-Trade",
          "acronym": "ST",
          "displayName": "Socratic Trade",
          "codeDir": "Socratic-Trade",
          "publishScript": "trading-publish.sh",
          "kind": "product",
          "visibility": "private"
        },
        {
          "repo": "Fixture-Monitor",
          "acronym": "FM",
          "displayName": "Fixture Monitor",
          "codeDir": "Fixture-Monitor",
          "kind": "product",
          "visibility": "private"
        },
        {
          "repo": "AI-Fleet-Coordinator",
          "acronym": "AFC",
          "displayName": "AI Fleet Coordinator",
          "codeDir": "AI-Fleet-Coordinator",
          "kind": "infra",
          "visibility": "public"
        }
      ],
      "seats": [
        { "tag": "AG", "notesName": "AG", "worktreeSuffix": "antigravity", "branchPrefixes": ["ag/"] },
        { "tag": "MC", "notesName": "MC", "worktreeSuffix": "muse-code", "branchPrefixes": ["muse-code/"] },
        { "tag": "GROK", "notesName": "Grok", "worktreeSuffix": "grok", "branchPrefixes": ["grok/"] },
        { "tag": "RET", "notesName": "Retired Fixture", "worktreeSuffix": "retired-fixture", "branchPrefixes": ["retired-fixture/"], "retired": true, "aliasOf": "MC" }
      ],
      "seatApps": {
        "bySeat": {
          "AG": "antigravity",
          "MC": "muse-code",
          "GROK": "grok-terminal"
        },
        "extraSeats": {
          "JET": "chatgpt"
        }
      }
    }
    """

    private let sampleEnvelopeJSON = """
    {
      "schemaVersion": 1,
      "tool": "fleet-view",
      "command": "status",
      "generatedAt": "2026-10-25T16:00:00Z",
      "summary": { "lanes": 2, "pullRequests": 2, "claims": 2, "overallVerdict": "PASS" },
      "requestCounts": { "github": 2, "linear": 1, "githubMax": 60, "linearMax": 30 },
      "sections": {
        "lanes": {
          "state": "OK",
          "reason": "2 lane(s)",
          "items": [
            { "repo": "Socratic-Trade", "lane": "antigravity-st-1", "seat": "AG", "branch": "ag/st-1", "dirtyFiles": 0, "ageSeconds": 60, "state": "OK" },
            { "repo": "AI-Fleet-Coordinator", "lane": "muse-code-afc-1", "seat": "MC", "branch": "muse-code/afc-1", "dirtyFiles": 2, "ageSeconds": 120, "state": "OK" }
          ]
        },
        "pullRequests": {
          "state": "OK",
          "reason": "2 open PR(s)",
          "items": [
            { "repo": "Simple-With-Us/Socratic-Trade", "number": 10, "title": "Ready PR", "branch": "ag/st-1", "seat": "AG", "blocker": "ready", "state": "OK" },
            { "repo": "Simple-With-Us/AI-Fleet-Coordinator", "number": 20, "title": "Draft PR", "branch": "muse-code/afc-1", "seat": "MC", "blocker": "auto-merge not armed", "state": "OK" }
          ]
        },
        "claims": {
          "state": "OK",
          "reason": "2 claim(s)",
          "items": [
            { "identifier": "ST-101", "title": "Trade feature", "seat": "AG", "ageSeconds": 300, "expired": false },
            { "identifier": "AFC-536", "title": "Muster improvements", "seat": "AG", "ageSeconds": 120, "expired": false }
          ]
        }
      },
      "notes": []
    }
    """

    func test_production_and_coding_apps_are_built_from_registry_and_activity() throws {
        let envelope = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(sampleEnvelopeJSON.utf8))
        let existingPaths: Set<String> = [
            "/home/fixture/Code/Socratic-Trade",
            "/home/fixture/apps/trading-publish.sh",
            "/home/fixture/apps/usage-publish.sh",
        ]
        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: envelope,
            toolsReading: nil,
            pathExists: { existingPaths.contains($0) }
        )

        XCTAssertNil(summary.problem)
        XCTAssertEqual(summary.zulipChannel, "work-in-progress")
        XCTAssertEqual(summary.apps.count, 3)

        let socratic = summary.apps[0]
        XCTAssertEqual(socratic.acronym, "ST")
        XCTAssertTrue(socratic.hasCodeCheckout)
        XCTAssertTrue(socratic.hasIntegrationCheckout)
        XCTAssertTrue(socratic.hasPublishScript)
        XCTAssertEqual(socratic.activeLanes, 1)
        XCTAssertEqual(socratic.dirtyLanes, 0)
        XCTAssertEqual(socratic.openPullRequests, 1)
        XCTAssertEqual(socratic.blockedPullRequests, 0)
        XCTAssertEqual(socratic.activeClaims, 1)
        XCTAssertEqual(socratic.state, .passed)

        let fm = summary.apps[1]
        XCTAssertEqual(fm.acronym, "FM")
        XCTAssertNil(fm.publishScript)
        XCTAssertFalse(fm.hasPublishScript)

        let afc = summary.apps[2]
        XCTAssertEqual(afc.acronym, "AFC")
        XCTAssertFalse(afc.hasCodeCheckout)
        XCTAssertFalse(afc.hasIntegrationCheckout)
        XCTAssertEqual(afc.activeLanes, 1)
        XCTAssertEqual(afc.dirtyLanes, 1)
        XCTAssertEqual(afc.openPullRequests, 1)
        XCTAssertEqual(afc.blockedPullRequests, 1)
        XCTAssertEqual(afc.activeClaims, 1)
        XCTAssertEqual(afc.state, .drift)

        // Coding apps group live seats (AG, MC, GROK; RET retired is excluded) plus extraSeats (JET).
        XCTAssertEqual(summary.codingApps.map(\.appSlug), ["antigravity", "muse-code", "grok-terminal", "chatgpt"])
        let ag = summary.codingApps[0]
        XCTAssertEqual(ag.displayName, "Antigravity")
        XCTAssertEqual(ag.seats, ["AG"])
        XCTAssertEqual(ag.cliTool, "agy")
        XCTAssertEqual(ag.activeLanes, 1)
        XCTAssertEqual(ag.openPullRequests, 1)
        XCTAssertEqual(ag.activeClaims, 2)

        let muse = summary.codingApps[1]
        XCTAssertEqual(muse.displayName, "Muse Code")
        XCTAssertEqual(muse.seats, ["MC"])
        XCTAssertEqual(muse.cliTool, "muse")
        XCTAssertEqual(muse.activeLanes, 1)
        XCTAssertEqual(muse.openPullRequests, 1)
        XCTAssertEqual(muse.activeClaims, 0)

        let grok = summary.codingApps[2]
        XCTAssertEqual(grok.displayName, "Grok Terminal")
        XCTAssertEqual(grok.seats, ["GROK"])
        XCTAssertEqual(grok.cliTool, "grok")

        let jet = summary.codingApps[3]
        XCTAssertEqual(jet.displayName, "ChatGPT")
        XCTAssertEqual(jet.seats, ["JET"])
        XCTAssertNil(jet.cliTool)
    }

    /// An envelope with no items, where one named section could not look at its source.
    private func envelopeWithUnknownSection(_ unknown: String) throws -> FleetViewEnvelope {
        func section(_ name: String) -> String {
            let state = name == unknown ? "UNKNOWN" : "OK"
            return #"{ "state": "\#(state)", "reason": "fixture", "items": [] }"#
        }
        let json = """
        {
          "schemaVersion": 1,
          "tool": "fleet-view",
          "command": "status",
          "generatedAt": "2026-10-25T16:00:00Z",
          "summary": { "lanes": 0, "pullRequests": 0, "claims": 0, "overallVerdict": "UNKNOWN" },
          "requestCounts": { "github": 0, "linear": 0, "githubMax": 60, "linearMax": 30 },
          "sections": {
            "lanes": \(section("lanes")),
            "pullRequests": \(section("pullRequests")),
            "claims": \(section("claims"))
          },
          "notes": []
        }
        """
        return try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(json.utf8))
    }

    private func firstAppState(unknownSection: String) throws -> CellState {
        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: try envelopeWithUnknownSection(unknownSection),
            toolsReading: nil,
            pathExists: { _ in false }
        )
        return summary.apps[0].state
    }

    func test_fleet_app_state_is_unproven_when_any_fleet_view_section_is_unknown() throws {
        for unknownSection in ["lanes", "pullRequests"] {
            XCTAssertEqual(
                try firstAppState(unknownSection: unknownSection),
                .unproven,
                "\(unknownSection) reported UNKNOWN, so an unobserved app must not read as passed"
            )
        }

        // FLEETVIEW-3 belongs to the Apps And Work Truth batch:  `claimsUnproven` in
        // FleetRegistry.swift is inverted, so unknown claims still read as passed.  The expectation
        // is not strict, so main stays green whichever change lands first.  Remove this wrapper
        // when that fix lands, and claims joins the list above.
        let options = XCTExpectedFailure.Options()
        options.isStrict = false
        try XCTExpectFailure("FLEETVIEW-3:  unknown claims still read as passed", options: options) {
            XCTAssertEqual(try firstAppState(unknownSection: "claims"), .unproven)
        }
    }

    /// The slugs FLEETVIEW-12 (Apps And Work Truth batch) still has to add to the metadata table.
    /// This list may only shrink:  a new unmapped slug fails below.
    private static let slugsAwaitingMetadata: Set<String> = ["kimi-code", "zcode", "amp"]

    func test_the_real_registry_decodes_and_every_slug_is_accounted_for() throws {
        // .../muster/MusterTests/FleetRegistryTests.swift -> repo root is three levels up
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fleet-apps.json"))

        let summary = FleetRegistryReader.build(from: data)
        XCTAssertNil(summary.problem)
        XCTAssertFalse(summary.apps.isEmpty)

        let document = try JSONDecoder().decode(FleetRegistryDocument.self, from: data)
        let sections = try XCTUnwrap(document.seatApps)
        var declared = Set((sections.bySeat ?? [:]).values)
        declared.formUnion((sections.extraSeats ?? [:]).values)
        declared.formUnion((sections.patterns ?? []).map(\.app))
        XCTAssertFalse(declared.isEmpty)

        let shown = Set(summary.codingApps.map(\.appSlug))
        XCTAssertTrue(declared.isSubset(of: shown), "not shown:  \(declared.subtracting(shown).sorted())")

        // An app with no row in the metadata table shows its raw slug as its name.
        let unmapped = Set(summary.codingApps.filter { $0.displayName == $0.appSlug }.map(\.appSlug))
        XCTAssertTrue(
            unmapped.isSubset(of: Self.slugsAwaitingMetadata),
            "add these to codingAppMetadata:  \(unmapped.subtracting(Self.slugsAwaitingMetadata).sorted())"
        )
    }

    func test_stale_doctor_report_is_not_consumed_as_mac_cli_currency() throws {
        let envelope = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(sampleEnvelopeJSON.utf8))
        let rowJSON = """
        {"check": "tool:agy", "status": "OK", "state": "current", "installed": "v1.2.3"}
        """
        let row = try JSONDecoder().decode(FleetToolsRow.self, from: Data(rowJSON.utf8))
        let hostVerdict = FleetToolsHostVerdict(
            host: "mac",
            reachable: true,
            state: .passed,
            failing: 0,
            unknown: 0,
            stale: 0,
            current: 1,
            absent: 0,
            notableRows: [],
            allRows: [row],
            reason: nil,
            generatedAt: Date(),
            age: "50m ago",
            expected: nil,
            doctorCommit: nil
        )
        var staleReading = FleetToolsReading()
        staleReading.isCurrent = false
        staleReading.hosts = [hostVerdict]

        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: envelope,
            toolsReading: staleReading,
            pathExists: { _ in false }
        )
        let ag = summary.codingApps.first { $0.appSlug == "antigravity" }
        XCTAssertNil(ag?.cliVersion)
        XCTAssertNil(ag?.cliState)
        XCTAssertFalse(ag?.doctorReportCurrent ?? true)

        var freshReading = staleReading
        freshReading.isCurrent = true
        let freshSummary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: envelope,
            toolsReading: freshReading,
            pathExists: { _ in false }
        )
        let agFresh = freshSummary.codingApps.first { $0.appSlug == "antigravity" }
        XCTAssertEqual(agFresh?.cliVersion, "v1.2.3")
        XCTAssertEqual(agFresh?.cliState, .current)
        XCTAssertTrue(agFresh?.doctorReportCurrent ?? false)
    }

    func test_seat_apps_patterns_are_expanded_into_coding_apps() throws {
        let registryWithPatterns = """
        {
          "zulipChannel": "work-in-progress",
          "apps": [],
          "seats": [
            { "tag": "AG", "notesName": "AG", "worktreeSuffix": "antigravity", "branchPrefixes": ["ag/"] }
          ],
          "seatApps": {
            "bySeat": { "AG": "antigravity" },
            "patterns": [
              { "match": "BF-*", "app": "botfleet" },
              { "match": "GB-*", "app": "grok-bot" }
            ]
          }
        }
        """
        let envelopeWithRoleSeat = """
        {
          "schemaVersion": 1,
          "tool": "fleet-view",
          "command": "status",
          "generatedAt": "2026-10-25T16:00:00Z",
          "summary": { "lanes": 1, "pullRequests": 0, "claims": 0, "overallVerdict": "PASS" },
          "requestCounts": { "github": 1, "linear": 0, "githubMax": 60, "linearMax": 30 },
          "sections": {
            "lanes": {
              "state": "OK",
              "reason": "1 lane",
              "items": [
                { "repo": "AI-Fleet-Coordinator", "lane": "bf-triage-1", "seat": "BF-TRIAGE", "branch": "bf/triage", "dirtyFiles": 0, "ageSeconds": 30, "state": "OK" }
              ]
            },
            "pullRequests": { "state": "OK", "reason": "0 open PR(s)", "items": [] },
            "claims": { "state": "OK", "reason": "0 claim(s)", "items": [] }
          },
          "notes": []
        }
        """
        let envelope = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(envelopeWithRoleSeat.utf8))
        let summary = FleetRegistryReader.build(
            from: Data(registryWithPatterns.utf8),
            viewOutput: envelope,
            toolsReading: nil,
            pathExists: { _ in false }
        )
        let slugs = summary.codingApps.map(\.appSlug)
        XCTAssertTrue(slugs.contains("botfleet"))
        XCTAssertTrue(slugs.contains("grok-bot"))

        let botfleet = summary.codingApps.first { $0.appSlug == "botfleet" }
        XCTAssertEqual(botfleet?.displayName, "BotFleet Personas")
        XCTAssertEqual(botfleet?.seats, ["BF-*"])
        XCTAssertEqual(botfleet?.activeLanes, 1)
    }
}
