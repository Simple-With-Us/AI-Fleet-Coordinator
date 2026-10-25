import XCTest

final class FleetRegistryTests: XCTestCase {
    private let sampleRegistry = """
    {
      "owner": "Simple-With-Us",
      "codeRoot": "/Users/jay/Code",
      "appsRoot": "/Users/jay/apps",
      "zulipChannel": "work-in-progress",
      "apps": [
        {
          "repo": "Socratic-Trade",
          "acronym": "ST",
          "displayName": "Socratic Trade",
          "integrationDir": "Socratic-Trade",
          "publishScript": "trading-publish.sh",
          "kind": "product",
          "visibility": "private"
        },
        {
          "repo": "AI-Fleet-Coordinator",
          "acronym": "AFC",
          "displayName": "AI Fleet Coordinator",
          "integrationDir": "AI-Fleet-Coordinator",
          "kind": "infra",
          "visibility": "public"
        }
      ],
      "seats": [
        { "tag": "AG", "notesName": "AG", "worktreeSuffix": "antigravity", "branchPrefixes": ["ag/"] },
        { "tag": "MC", "notesName": "MC", "worktreeSuffix": "muse-code", "branchPrefixes": ["muse-code/"] },
        { "tag": "MA", "notesName": "MA", "worktreeSuffix": "muse", "branchPrefixes": ["muse/"] },
        { "tag": "MONET", "notesName": "Monet", "worktreeSuffix": "monet", "retired": true, "aliasOf": "MC" }
      ],
      "seatApps": {
        "bySeat": {
          "AG": "antigravity",
          "MC": "muse",
          "MA": "muse"
        },
        "extraSeats": {
          "JET": "openai"
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
            "/Users/jay/apps/Socratic-Trade",
            "/Users/jay/apps/trading-publish.sh",
        ]
        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: envelope,
            toolsReading: nil,
            pathExists: { existingPaths.contains($0) }
        )

        XCTAssertNil(summary.problem)
        XCTAssertEqual(summary.zulipChannel, "work-in-progress")
        XCTAssertEqual(summary.apps.count, 2)

        let st = summary.apps[0]
        XCTAssertEqual(st.acronym, "ST")
        XCTAssertTrue(st.hasIntegrationCheckout)
        XCTAssertTrue(st.hasPublishScript)
        XCTAssertEqual(st.activeLanes, 1)
        XCTAssertEqual(st.dirtyLanes, 0)
        XCTAssertEqual(st.openPullRequests, 1)
        XCTAssertEqual(st.blockedPullRequests, 0)
        XCTAssertEqual(st.activeClaims, 1)
        XCTAssertEqual(st.state, .passed)

        let afc = summary.apps[1]
        XCTAssertEqual(afc.acronym, "AFC")
        XCTAssertFalse(afc.hasIntegrationCheckout)
        XCTAssertEqual(afc.activeLanes, 1)
        XCTAssertEqual(afc.dirtyLanes, 1)
        XCTAssertEqual(afc.openPullRequests, 1)
        XCTAssertEqual(afc.blockedPullRequests, 1)
        XCTAssertEqual(afc.activeClaims, 1)
        XCTAssertEqual(afc.state, .drift)

        // Coding apps group live seats (MC and MA under muse; MONET retired is excluded) plus extraSeats.
        XCTAssertEqual(summary.codingApps.map(\.appSlug), ["antigravity", "muse", "openai"])
        let ag = summary.codingApps[0]
        XCTAssertEqual(ag.seats, ["AG"])
        XCTAssertEqual(ag.cliTool, "agy")
        XCTAssertEqual(ag.activeLanes, 1)
        XCTAssertEqual(ag.openPullRequests, 1)
        XCTAssertEqual(ag.activeClaims, 2)

        let muse = summary.codingApps[1]
        XCTAssertEqual(muse.seats, ["MC", "MA"])
        XCTAssertEqual(muse.cliTool, "muse")
        XCTAssertEqual(muse.activeLanes, 1)
        XCTAssertEqual(muse.openPullRequests, 1)
        XCTAssertEqual(muse.activeClaims, 0)
    }
}
