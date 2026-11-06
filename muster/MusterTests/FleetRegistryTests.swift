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
            { "repo": "Simple-With-Us/AI-Fleet-Coordinator", "number": 20, "title": "Draft PR", "branch": "muse-code/afc-1", "seat": "MC", "blocker": "draft", "state": "OK", "severity": "waiting", "draft": true }
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
        XCTAssertNil(socratic.checkoutText)
        XCTAssertEqual(socratic.kindLabel, "Product")
        XCTAssertEqual(socratic.activeLanes, 1)
        XCTAssertEqual(socratic.dirtyLanes, 0)
        XCTAssertEqual(socratic.openPullRequests, 1)
        XCTAssertEqual(socratic.actionPullRequests, 0)
        XCTAssertEqual(socratic.activeClaims, 1)
        XCTAssertEqual(socratic.lanes.text, "1")
        XCTAssertEqual(socratic.state, .inProgress)

        // Nothing open reads as quiet, never as a pass that sounds like the app is up.
        let fm = summary.apps[1]
        XCTAssertEqual(fm.acronym, "FM")
        XCTAssertEqual(fm.state, .quiet)
        XCTAssertEqual(fm.pullRequests.text, "0")

        // A lane with uncommitted changes and a draft pull request are work in progress,
        // not problems.
        let afc = summary.apps[2]
        XCTAssertEqual(afc.acronym, "AFC")
        XCTAssertFalse(afc.hasCodeCheckout)
        XCTAssertEqual(afc.checkoutText, "No checkout in the code folder on the Mac")
        XCTAssertEqual(afc.activeLanes, 1)
        XCTAssertEqual(afc.dirtyLanes, 1)
        XCTAssertEqual(afc.openPullRequests, 1)
        XCTAssertEqual(afc.actionPullRequests, 0)
        XCTAssertEqual(afc.activeClaims, 1)
        XCTAssertEqual(afc.state, .inProgress)

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

    private func firstAppState(unknownSection: String) throws -> AppWorkState {
        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: try envelopeWithUnknownSection(unknownSection),
            toolsReading: nil,
            pathExists: { _ in false }
        )
        return summary.apps[0].state
    }

    func test_fleet_app_state_is_not_known_when_any_fleet_view_section_is_unknown() throws {
        // FLEETVIEW-3:  claims were the inverted case, and an unknown claims read left idle apps
        // reading as passed.
        for unknownSection in ["lanes", "pullRequests", "claims"] {
            XCTAssertEqual(
                try firstAppState(unknownSection: unknownSection),
                .notKnown,
                "\(unknownSection) reported UNKNOWN, so an unobserved app must not read as quiet"
            )
        }
    }

    /// The check badge from #1387 sits beside the work state and never reads PASS for an app
    /// whose sections were not all read.
    func test_the_check_badge_is_never_pass_when_a_section_could_not_look() throws {
        for unknownSection in ["lanes", "pullRequests", "claims"] {
            let summary = FleetRegistryReader.build(
                from: Data(sampleRegistry.utf8),
                viewOutput: try envelopeWithUnknownSection(unknownSection),
                toolsReading: nil,
                pathExists: { _ in false }
            )
            XCTAssertEqual(summary.apps[0].checkState, .unproven, "\(unknownSection) was not read")
        }
        let none = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8), viewOutput: nil, toolsReading: nil, pathExists: { _ in false }
        )
        XCTAssertEqual(none.apps[0].checkState, .unproven)
    }

    /// FLEETVIEW-4:  a section that could not look has no count, and rows never print a zero
    /// that reads as measured.
    func test_a_count_from_a_section_that_could_not_look_is_not_known_never_zero() throws {
        let summary = FleetRegistryReader.build(
            from: Data(sampleRegistry.utf8),
            viewOutput: try envelopeWithUnknownSection("claims"),
            toolsReading: nil,
            pathExists: { _ in false }
        )
        let socratic = summary.apps[0]
        XCTAssertEqual(socratic.claims.text, "not known")
        XCTAssertEqual(socratic.claims.phrase("claim", "claims"), "claims not known")
        XCTAssertEqual(socratic.pullRequests.text, "0")
        XCTAssertTrue(socratic.reasons.contains("Claims were not all read."), "\(socratic.reasons)")
        let ag = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "antigravity" })
        XCTAssertEqual(ag.claims.phrase("claim", "claims"), "claims not known")
        XCTAssertEqual(ag.lanes.phrase("lane", "lanes"), "0 lanes")

        XCTAssertEqual(WorkCount(observed: 2, complete: false).text, "at least 2")
        XCTAssertEqual(WorkCount(observed: 1, complete: false).phrase("PR", "PRs"), "at least 1 PR")
        XCTAssertEqual(WorkCount(observed: 1, complete: true).phrase("PR", "PRs"), "1 PR")
    }

    /// A lane git could not read, and whose seat could not be told, may belong to any coding app,
    /// so no app's lane count reads as an exact number while it exists.
    func test_an_unreadable_lane_with_no_seat_leaves_every_coding_apps_lane_count_open() throws {
        let json = """
        {
          "schemaVersion": 1, "tool": "fleet-view", "command": "status", "host": "mac",
          "generatedAt": "2026-10-25T16:00:00Z",
          "summary": { "lanes": 2, "pullRequests": 0, "claims": 0, "overallVerdict": "UNKNOWN" },
          "requestCounts": { "github": 0, "linear": 0, "githubMax": 60, "linearMax": 30 },
          "sections": {
            "lanes": { "state": "OK", "reason": "2 lanes", "unread": [], "items": [
              { "repo": "Socratic-Trade", "lane": "antigravity-st-1", "seat": "AG", "branch": "ag/st-1", "dirtyFiles": 0, "ageSeconds": 60, "state": "OK" },
              { "repo": "Socratic-Trade", "lane": "ag-broken", "seat": "UNKNOWN", "branch": "", "dirtyFiles": -1, "ageSeconds": 60, "state": "UNKNOWN" }
            ] },
            "pullRequests": { "state": "OK", "reason": "none", "items": [], "unread": [] },
            "claims": { "state": "OK", "reason": "none", "items": [], "unread": [] }
          },
          "notes": []
        }
        """
        let view = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(json.utf8))
        let summary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: view)
        let ag = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "antigravity" })
        XCTAssertEqual(ag.lanes.observed, 1)
        XCTAssertFalse(ag.lanes.complete)
        XCTAssertEqual(ag.lanes.phrase("lane", "lanes"), "at least 1 lane")
        let muse = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "muse-code" })
        XCTAssertFalse(muse.lanes.complete)
    }

    private func envelope(prs: String, prState: String = "OK", unread: String = "[]", claims: String = "[]") throws -> FleetViewEnvelope {
        let json = """
        {
          "schemaVersion": 1, "tool": "fleet-view", "command": "status", "host": "mac",
          "generatedAt": "2026-10-25T16:00:00Z",
          "summary": { "lanes": 0, "pullRequests": 0, "claims": 0, "overallVerdict": "UNKNOWN" },
          "requestCounts": { "github": 3, "linear": 1, "githubMax": 60, "linearMax": 30 },
          "sections": {
            "lanes": { "state": "OK", "reason": "0 lanes", "items": [], "unread": [] },
            "pullRequests": { "state": "\(prState)", "reason": "fixture", "items": \(prs), "unread": \(unread) },
            "claims": { "state": "OK", "reason": "fixture", "items": \(claims), "unread": [] }
          },
          "notes": []
        }
        """
        return try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(json.utf8))
    }

    private func pr(_ repo: String, _ number: Int, blocker: String, severity: String) -> String {
        #"{ "repo": "Simple-With-Us/\#(repo)", "number": \#(number), "title": "T", "branch": "ag/x", "seat": "AG", "blocker": "\#(blocker)", "state": "OK", "severity": "\#(severity)", "draft": false, "url": "https://github.com/Simple-With-Us/\#(repo)/pull/\#(number)" }"#
    }

    /// A repo the collector names as not read is not known;  every other app's count stands.
    func test_a_repo_named_as_unread_leaves_other_apps_complete() throws {
        let view = try envelope(
            prs: "[\(pr("Socratic-Trade", 1, blocker: "ready", severity: "ready"))]",
            prState: "UNKNOWN",
            unread: #"["Simple-With-Us/Fixture-Monitor"]"#
        )
        let summary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: view)
        XCTAssertTrue(summary.apps[0].pullRequests.complete)
        XCTAssertEqual(summary.apps[0].state, .inProgress)
        XCTAssertEqual(summary.apps[1].pullRequests.text, "not known")
        XCTAssertEqual(summary.apps[1].state, .notKnown)
        XCTAssertTrue(summary.apps[2].pullRequests.complete)
        // A coding app's pull requests span every repo, so one unread repo leaves them all open.
        XCTAssertFalse(summary.codingApps[0].pullRequests.complete)

        // An older collector names nothing, so nothing in an unknown section can be complete.
        let older = try envelope(prs: "[]", prState: "UNKNOWN", unread: "null")
        let olderSummary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: older)
        XCTAssertFalse(olderSummary.apps[0].pullRequests.complete)
    }

    /// FLEETVIEW-9, FLEETVIEW-10:  only a pull request someone has to act on, or an expired
    /// claim, needs attention, and it outranks a section that could not be read.
    func test_only_what_needs_someone_makes_an_app_need_attention() throws {
        let failing = try envelope(prs: "[\(pr("Socratic-Trade", 1, blocker: "failing checks", severity: "action"))]")
        let failingSummary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: failing)
        XCTAssertEqual(failingSummary.apps[0].state, .needsAttention)
        XCTAssertEqual(failingSummary.apps[0].actionPullRequests, 1)
        XCTAssertEqual(failingSummary.apps[0].reasons.first, "1 pull request needs action.")

        let draft = try envelope(prs: "[\(pr("Socratic-Trade", 1, blocker: "draft", severity: "waiting"))]")
        XCTAssertEqual(FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: draft).apps[0].state, .inProgress)

        let expiredClaim = #"[{ "identifier": "ST-9", "title": "Old", "seat": "AG", "ageSeconds": 400000, "expired": true }]"#
        let withUnknownPRs = try envelope(prs: "[]", prState: "UNKNOWN", unread: #"["Simple-With-Us/Socratic-Trade"]"#, claims: expiredClaim)
        let summary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: withUnknownPRs)
        XCTAssertEqual(summary.apps[0].state, .needsAttention)
        XCTAssertEqual(summary.apps[0].expiredClaims, 1)
        XCTAssertTrue(summary.apps[0].reasons.contains("1 claim has expired."))
        XCTAssertTrue(summary.apps[0].reasons.contains("Pull requests for Socratic-Trade were not all read."))
    }

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

        // FLEETVIEW-12:  every slug has a row that names it and says where it runs, so none
        // shows a raw slug or a guessed "hosted".
        let unmapped = declared.filter { FleetRegistryReader.codingAppMetadata[$0] == nil }
        XCTAssertTrue(unmapped.isEmpty, "add these to codingAppMetadata:  \(unmapped.sorted())")
        for (slug, meta) in FleetRegistryReader.codingAppMetadata {
            XCTAssertFalse(meta.title.isEmpty, slug)
            XCTAssertTrue(meta.cli != nil || !meta.runsOn.isEmpty, "\(slug) says neither its tool nor where it runs")
        }
        XCTAssertEqual(FleetRegistryReader.codingAppMetadata["kimi-code"]?.cli, "kimi")
        XCTAssertEqual(FleetRegistryReader.codingAppMetadata["zcode"]?.title, "Z Code")
        // FLEETVIEW-14:  the Operator is a service;  the simple-chat CLI's version says nothing about it.
        XCTAssertNil(FleetRegistryReader.codingAppMetadata["operator"]?.cli)
    }

    func test_each_machine_reads_its_own_tool_version() throws {
        let rows = [
            #"{"check": "tool:agy", "status": "OK", "state": "current", "installed": "v1.2.3"}"#,
            #"{"check": "tool:agy", "status": "WARN", "state": "stale", "installed": "v1.2.0"}"#,
            #"{"check": "tool:muse", "status": "FAIL", "state": "missing"}"#,
        ].map { try? JSONDecoder().decode(FleetToolsRow.self, from: Data($0.utf8)) }
        func host(_ name: String, reachable: Bool = true, rows: [FleetToolsRow?]) -> FleetToolsHostVerdict {
            FleetToolsHostVerdict(
                host: name, reachable: reachable, state: .passed, failing: 0, unknown: 0, stale: 0,
                current: 1, absent: 0, notableRows: [], allRows: rows.compactMap { $0 }, reason: nil,
                generatedAt: Date(), age: "1m", expected: nil, doctorCommit: nil
            )
        }
        var reading = FleetToolsReading()
        reading.isCurrent = true
        reading.generatedAt = Date()
        reading.hosts = [host("mac", rows: [rows[0], rows[2]]), host("server", rows: [rows[1]])]

        let summary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), toolsReading: reading)
        XCTAssertNil(summary.toolsNote)
        let ag = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "antigravity" })
        XCTAssertEqual(ag.toolReadings.map(\.text), ["Mac v1.2.3, up to date", "Server v1.2.0, behind"])
        XCTAssertTrue(ag.hasToolProblem)
        let muse = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "muse-code" })
        XCTAssertEqual(muse.toolReadings.map(\.outcome), [.missing, .notChecked])
        XCTAssertEqual(muse.toolReadings.first?.text, "Mac:  required, not installed")

        reading.hosts = [host("mac", rows: [rows[0]]), host("server", reachable: false, rows: [rows[1]])]
        let unreached = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), toolsReading: reading)
        let agUnreached = try XCTUnwrap(unreached.codingApps.first { $0.appSlug == "antigravity" })
        XCTAssertEqual(agUnreached.toolReadings.map(\.text), ["Mac v1.2.3, up to date", "Server:  not reached"])

        // A tool with no tracked row says where it runs instead of guessing "hosted".
        let jet = try XCTUnwrap(summary.codingApps.first { $0.appSlug == "chatgpt" })
        XCTAssertTrue(jet.toolReadings.isEmpty)
        XCTAssertEqual(jet.runsOn, "Hosted, no local tool")
    }

    /// FLEETVIEW-13:  the note says which way the tools report failed, not always "stale".
    func test_the_tools_note_names_why_versions_are_not_known() {
        XCTAssertNil(FleetRegistryReader.toolsNote({ var reading = FleetToolsReading(); reading.isCurrent = true; return reading }()))
        var missing = FleetToolsReading()
        missing.problem = "The report could not be read:  the file is not there"
        XCTAssertEqual(
            FleetRegistryReader.toolsNote(missing),
            "Tool versions are not known, because the tools report was not usable:  The report could not be read:  the file is not there"
        )
        var old = FleetToolsReading()
        old.age = "3h"
        XCTAssertEqual(
            FleetRegistryReader.toolsNote(old),
            "The tools report was written 3h ago, which is too old to trust, so tool versions are not known."
        )
        XCTAssertEqual(
            FleetRegistryReader.toolsNote(nil),
            "No tools report was read, so tool versions are not known."
        )
    }

    /// FLEETVIEW-33:  the kind filter and the search reach every section through the row's app.
    func test_the_filter_reaches_every_section_through_the_app() throws {
        let view = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(sampleEnvelopeJSON.utf8))
        let summary = FleetRegistryReader.build(from: Data(sampleRegistry.utf8), viewOutput: view)
        let stPR = view.sections.pullRequests.items[0]
        let afcPR = view.sections.pullRequests.items[1]
        let products = FleetViewFilter(kind: .product)
        XCTAssertTrue(products.includes(stPR, app: summary.app(forRepo: stPR.repo)))
        XCTAssertFalse(products.includes(afcPR, app: summary.app(forRepo: afcPR.repo)))
        XCTAssertTrue(products.isActive)

        let byTitle = FleetViewFilter(query: "socratic trade")
        let lane = view.sections.lanes.items[0]
        XCTAssertTrue(byTitle.includes(lane, app: summary.app(forRepo: lane.repo)))
        XCTAssertFalse(byTitle.includes(view.sections.lanes.items[1], app: summary.app(forRepo: "AI-Fleet-Coordinator")))

        let claim = view.sections.claims.items[1]
        XCTAssertEqual(summary.app(forClaim: claim.identifier)?.acronym, "AFC")
        XCTAssertFalse(FleetViewFilter(query: "socratic").includes(claim, app: summary.app(forClaim: claim.identifier)))

        let attention = FleetViewFilter(attentionOnly: true)
        XCTAssertFalse(attention.includes(stPR, app: nil))
        XCTAssertFalse(attention.includes(summary.apps[0]))
        XCTAssertFalse(FleetViewFilter().isActive)
    }

    /// FLEETVIEW-34:  a branch prefix two seats share counts for each of them.
    func test_a_row_naming_two_seats_counts_for_each() throws {
        let registry = """
        {
          "apps": [],
          "seats": [
            { "tag": "ECHO", "worktreeSuffix": "echo", "branchPrefixes": ["echo/", "instinct/"] },
            { "tag": "WISE", "worktreeSuffix": "wise", "branchPrefixes": ["wise/", "instinct/"] }
          ],
          "seatApps": { "bySeat": { "ECHO": "instinct", "WISE": "instinct" } }
        }
        """
        let view = try envelope(prs: #"[{ "repo": "Simple-With-Us/X", "number": 1, "title": "T", "branch": "instinct/x", "seat": "ECHO or WISE", "blocker": "draft", "state": "OK" }]"#)
        let summary = FleetRegistryReader.build(from: Data(registry.utf8), viewOutput: view)
        XCTAssertEqual(summary.codingApps.first?.displayName, "Instinct")
        XCTAssertEqual(summary.codingApps.first?.openPullRequests, 1)
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
        // FLEETVIEW-12:  the owner reads a family as its roles, not as a pattern.
        XCTAssertEqual(botfleet?.seatLabels, ["BF-<role>"])
        XCTAssertEqual(botfleet?.activeLanes, 1)
        XCTAssertEqual(botfleet?.runsOn, FleetRegistryReader.notTracked)
    }
}
