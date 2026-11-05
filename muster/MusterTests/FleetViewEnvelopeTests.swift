import XCTest

/// The Apps & Work envelope as the app reads it (AFC-616, Apps And Work Truth):  which machine
/// the lanes are on, what a pull request's state asks of people, links, rows that will not
/// decode, and the words the page shows.
final class FleetViewEnvelopeTests: XCTestCase {
    private func decode(_ json: String) throws -> FleetViewEnvelope {
        try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(json.utf8))
    }

    /// An envelope with the given top-level extras and sections.
    private func envelope(
        extras: String = #""host": "mac", "claimExpiryHours": 72, "durationSeconds": 6.6,"#,
        verdict: String = "PASS",
        lanes: String = #"{ "state": "OK", "reason": "0 lanes", "items": [], "unread": [] }"#,
        prs: String = #"{ "state": "OK", "reason": "0 open PRs", "items": [], "unread": [] }"#,
        claims: String = #"{ "state": "OK", "reason": "0 claims", "items": [], "unread": [] }"#
    ) -> String {
        """
        {
          "schemaVersion": 1, "tool": "fleet-view", "command": "status", \(extras)
          "generatedAt": "2026-11-05T22:00:00Z",
          "summary": { "lanes": 0, "pullRequests": 0, "claims": 0, "overallVerdict": "\(verdict)" },
          "requestCounts": { "github": 17, "linear": 1, "githubMax": 60, "linearMax": 30 },
          "sections": { "lanes": \(lanes), "pullRequests": \(prs), "claims": \(claims) },
          "notes": ["claim expiry ceiling: 72 hours (CLAIM_EXPIRY_HOURS)"]
        }
        """
    }

    private let oldLane = #"{ "repo": "AI-Fleet-Coordinator", "lane": "claude-x", "seat": "CLAUDE", "branch": "claude/x", "dirtyFiles": 2, "ageSeconds": 60, "state": "OK" }"#
    private let oldPR = #"{ "repo": "Simple-With-Us/AI-Fleet-Coordinator", "number": 7, "title": "A PR", "branch": "claude/x", "seat": "CLAUDE", "blocker": "auto-merge not armed", "state": "OK" }"#

    // MARK: Older collectors

    /// An installed Muster can be newer than the checkout it runs.  The fields an older
    /// collector leaves out read as not said, never as a healthy value.
    func test_an_older_envelope_decodes_and_its_missing_fields_read_as_not_said() throws {
        let json = envelope(
            extras: "",
            lanes: #"{ "state": "OK", "reason": "1 lane(s)", "items": [\#(oldLane)] }"#,
            prs: #"{ "state": "OK", "reason": "1 open PR(s)", "items": [\#(oldPR)] }"#
        )
        let view = try decode(json)
        // Muster only ever runs the collector on the Mac, so a missing host is the Mac.
        XCTAssertEqual(view.host, .mac)
        XCTAssertNil(view.claimExpiryHours)
        XCTAssertNil(view.durationSeconds)
        XCTAssertNil(view.expiryText)
        XCTAssertNil(view.sections.pullRequests.unread)
        let lane = try XCTUnwrap(view.sections.lanes.items.first)
        XCTAssertNil(lane.host)
        XCTAssertEqual(lane.lastCommitSeconds, -1)
        XCTAssertEqual(lane.activityText, "Last commit not known")
        XCTAssertEqual(lane.changesText, "2 uncommitted changes")
        XCTAssertNil(lane.path)
        let pr = try XCTUnwrap(view.sections.pullRequests.items.first)
        // The severity is worked out from the blocker with the collector's own table.
        XCTAssertEqual(pr.severity, .waiting)
        XCTAssertFalse(pr.needsAction)
        XCTAssertNil(pr.url)
    }

    func test_the_collectors_severity_table_matches() {
        XCTAssertEqual(PullRequestSeverity(blocker: "ready"), .ready)
        for blocker in ["changes requested", "failing checks", "checks need a rerun", "conflicting with main"] {
            XCTAssertEqual(PullRequestSeverity(blocker: blocker), .action, blocker)
        }
        for blocker in ["draft", "checks pending", "behind main", "waiting for review", "blocked (see PR)", "auto-merge not armed"] {
            XCTAssertEqual(PullRequestSeverity(blocker: blocker), .waiting, blocker)
        }
        XCTAssertEqual(PullRequestSeverity(blocker: "unknown"), .unknown)
        XCTAssertEqual(PullRequestSeverity(blocker: "something new"), .unknown)
    }

    // MARK: New fields

    func test_the_machine_detached_lanes_and_last_commits_read_in_words() throws {
        let lane = #"{ "repo": "BotFleet", "lane": "review-pr-12", "seat": "REVIEW", "branch": "HEAD", "dirtyFiles": 0, "ageSeconds": 5, "state": "OK", "host": "server", "detached": true, "head": "abc1234", "lastCommitSeconds": 259200, "path": "/x/BotFleet/review-pr-12", "layout": "seat" }"#
        let view = try decode(envelope(
            extras: #""host": "server","#,
            lanes: #"{ "state": "OK", "reason": "1 lane", "items": [\#(lane)], "unread": [] }"#
        ))
        XCTAssertEqual(view.host, .server)
        XCTAssertEqual(view.lanesHeading, "Lanes On The Server")
        XCTAssertEqual(view.lanesCaption, "Read from the lanes folder on the server.\u{00A0} Lanes on the Mac are not shown here.")
        let row = try XCTUnwrap(view.sections.lanes.items.first)
        XCTAssertEqual(row.host, .server)
        XCTAssertEqual(row.branchText, "Detached at abc1234")
        XCTAssertEqual(row.activityText, "Last commit 3d ago")
        XCTAssertEqual(row.changesText, "No uncommitted changes")
        XCTAssertEqual(row.path, "/x/BotFleet/review-pr-12")

        let mac = try decode(envelope())
        XCTAssertEqual(mac.lanesHeading, "Lanes On The Mac")
        XCTAssertEqual(mac.lanesCaption, "Read from the lanes folder on the Mac.\u{00A0} Lanes on the server are not shown here.")
        XCTAssertEqual(mac.expiryText, "Expired means no update for more than 72 hours.")
    }

    /// A host value this app does not know names no machine, rather than a guessed one.
    func test_an_unknown_host_names_no_machine() throws {
        let view = try decode(envelope(extras: #""host": "laptop","#))
        XCTAssertNil(view.host)
        XCTAssertEqual(view.lanesHeading, "Lanes, Machine Not Named")
    }

    func test_only_https_links_are_kept() throws {
        func prJSON(_ url: String) -> String {
            #"{ "repo": "Simple-With-Us/R", "number": 1, "title": "T", "branch": "b", "seat": "S", "blocker": "draft", "state": "OK", "severity": "waiting", "draft": true, "url": "\#(url)" }"#
        }
        let urls = ["https://github.com/Simple-With-Us/R/pull/1", "file:///etc/hosts", "javascript:alert(1)", "http://example.com/x", ""]
        let items = urls.map(prJSON).joined(separator: ", ")
        let view = try decode(envelope(prs: #"{ "state": "OK", "reason": "5 open PRs", "items": [\#(items)], "unread": [] }"#))
        XCTAssertEqual(
            view.sections.pullRequests.items.map { $0.url?.absoluteString },
            ["https://github.com/Simple-With-Us/R/pull/1", nil, nil, nil, nil]
        )
        XCTAssertTrue(view.sections.pullRequests.items.allSatisfy(\.draft))
    }

    // MARK: Rows that will not decode (CONTRACT-SKEW-15)

    func test_one_bad_row_costs_that_row_and_not_the_page() throws {
        let badField = #"{ "repo": "AI-Fleet-Coordinator", "lane": 5, "seat": "CLAUDE", "branch": "claude/y", "dirtyFiles": 0, "ageSeconds": 1, "state": "OK" }"#
        let view = try decode(envelope(
            lanes: #"{ "state": "OK", "reason": "3 lanes", "items": [\#(oldLane), \#(badField), 7] }"#
        ))
        let lanes = view.sections.lanes
        XCTAssertEqual(lanes.items.count, 2)
        XCTAssertEqual(lanes.items[0].state, "OK")
        // A field that will not decode leaves the row on the page, reading UNKNOWN.
        XCTAssertEqual(lanes.items[1].state, "UNKNOWN")
        XCTAssertFalse(lanes.items[1].isReadable)
        // A row that is not an object is dropped, and the section stops reading as known.
        XCTAssertEqual(lanes.droppedItems, 1)
        XCTAssertFalse(lanes.isKnown)
        XCTAssertTrue(view.sections.pullRequests.isKnown)
    }

    func test_a_claim_whose_age_will_not_decode_reads_expired_not_fresh() throws {
        let claim = #"{ "identifier": "AFC-1", "title": "T", "seat": "CLAUDE", "ageSeconds": "soon", "expired": false }"#
        let view = try decode(envelope(claims: #"{ "state": "OK", "reason": "1 claim", "items": [\#(claim)] }"#))
        let row = try XCTUnwrap(view.sections.claims.items.first)
        XCTAssertTrue(row.expired)
        XCTAssertEqual(row.ageText, "Last update not known")
    }

    func test_an_answer_from_another_tool_is_refused() {
        let other = envelope().replacingOccurrences(of: #""tool": "fleet-view""#, with: #""tool": "fleet-config""#)
        XCTAssertThrowsError(try decode(other))
        let otherVerb = envelope().replacingOccurrences(of: #""command": "status""#, with: #""command": "plan""#)
        XCTAssertThrowsError(try decode(otherVerb))
    }

    // MARK: The words the page shows

    func test_empty_sections_say_which_kind_of_empty_they_are() {
        XCTAssertNil(FleetSectionEmpty.state(total: 3, shown: 1, isKnown: true, reason: "r", noneText: "none"))
        XCTAssertEqual(FleetSectionEmpty.state(total: 3, shown: 0, isKnown: true, reason: "r", noneText: "none"), .hiddenByFilter)
        XCTAssertEqual(FleetSectionEmpty.state(total: 0, shown: 0, isKnown: true, reason: "r", noneText: "No open pull requests."), .noRows("No open pull requests."))
        XCTAssertEqual(FleetSectionEmpty.state(total: 0, shown: 0, isKnown: false, reason: "gh exited 4", noneText: "none"), .notKnown("gh exited 4"))
    }

    func test_needs_attention_lists_actions_then_expired_claims_then_what_was_not_read() throws {
        let failing = #"{ "repo": "Simple-With-Us/Socratic-Trade", "number": 3, "title": "Fix it", "branch": "ag/x", "seat": "AG", "blocker": "failing checks", "state": "OK", "severity": "action", "url": "https://github.com/Simple-With-Us/Socratic-Trade/pull/3" }"#
        let draft = #"{ "repo": "Simple-With-Us/Socratic-Trade", "number": 4, "title": "Later", "branch": "ag/y", "seat": "AG", "blocker": "draft", "state": "OK", "severity": "waiting" }"#
        let expired = #"{ "identifier": "ST-9", "title": "Old claim", "seat": "AG", "ageSeconds": 432000, "expired": true }"#
        let broken = #"{ "repo": "AI-Fleet-Coordinator", "lane": "claude-z", "seat": "CLAUDE", "branch": "", "dirtyFiles": -1, "ageSeconds": 1, "state": "UNKNOWN" }"#
        let view = try decode(envelope(
            verdict: "UNKNOWN",
            lanes: #"{ "state": "OK", "reason": "1 lane; 1 could not be read", "items": [\#(broken)], "unread": [] }"#,
            prs: #"{ "state": "OK", "reason": "2 open PRs", "items": [\#(failing), \#(draft)], "unread": [] }"#,
            claims: #"{ "state": "UNKNOWN", "reason": "simple-board exited 4", "items": [\#(expired)], "unread": [] }"#
        ))
        let items = view.attentionItems
        XCTAssertEqual(items.map(\.kind), [.pullRequest, .claim, .notRead, .notRead])
        XCTAssertEqual(items[0].text, "Socratic-Trade #3:  checks failing.\u{00A0} Fix it")
        XCTAssertEqual(items[0].url?.absoluteString, "https://github.com/Simple-With-Us/Socratic-Trade/pull/3")
        XCTAssertEqual(items[1].text, "ST-9 claim expired, no update for 5d.\u{00A0} Old claim")
        XCTAssertEqual(items[2].text, "Claims were not all read:  simple-board exited 4")
        XCTAssertEqual(items[3].text, "1 lane on the Mac could not be read by git.")
        XCTAssertEqual(view.verdictText, "Some sources could not be read, so some counts below are not known.")

        let clean = try decode(envelope())
        XCTAssertTrue(clean.attentionItems.isEmpty)
        XCTAssertEqual(clean.verdictText, "Every source was read.")
        XCTAssertEqual(
            clean.sourceText,
            "Lanes from the lanes folder on the Mac.\u{00A0} Pull requests from GitHub (17 requests this run).\u{00A0} Claims from Linear (1 request this run).\u{00A0} The run took 7 seconds."
        )
    }

    func test_lists_put_what_needs_someone_first() throws {
        func prJSON(_ number: Int, _ blocker: String, _ severity: String) -> String {
            #"{ "repo": "Simple-With-Us/R", "number": \#(number), "title": "T", "branch": "b", "seat": "S", "blocker": "\#(blocker)", "state": "OK", "severity": "\#(severity)" }"#
        }
        let items = [prJSON(1, "ready", "ready"), prJSON(2, "draft", "waiting"), prJSON(3, "failing checks", "action"), prJSON(4, "unknown", "unknown")]
        let view = try decode(envelope(prs: #"{ "state": "OK", "reason": "4", "items": [\#(items.joined(separator: ", "))] }"#))
        let sorted = view.sections.pullRequests.items.sorted(by: FleetViewEnvelope.PullRequest.listOrder)
        XCTAssertEqual(sorted.map(\.number), [3, 4, 2, 1])
        XCTAssertEqual(sorted.map(\.statusText), ["Checks failing", "Merge state not known", "Draft", "Ready to merge"])

        func claimJSON(_ id: String, _ age: Int, _ expired: Bool) -> String {
            #"{ "identifier": "\#(id)", "title": "T", "seat": "S", "ageSeconds": \#(age), "expired": \#(expired) }"#
        }
        let claims = [claimJSON("A-1", 10, false), claimJSON("A-2", 500000, true), claimJSON("A-3", 9000, false)]
        let claimView = try decode(envelope(claims: #"{ "state": "OK", "reason": "3", "items": [\#(claims.joined(separator: ", "))] }"#))
        XCTAssertEqual(claimView.sections.claims.items.sorted(by: FleetViewEnvelope.Claim.listOrder).map(\.identifier), ["A-2", "A-3", "A-1"])
    }

    /// FLEETVIEW-17:  lanes group by repo, and a repo with a lane git could not read comes first.
    func test_lane_groups_put_problems_first() throws {
        func laneJSON(_ repo: String, _ name: String, dirty: Int, state: String = "OK", last: Int = 100) -> String {
            #"{ "repo": "\#(repo)", "lane": "\#(name)", "seat": "CLAUDE", "branch": "claude/\#(name)", "dirtyFiles": \#(dirty), "ageSeconds": 1, "state": "\#(state)", "lastCommitSeconds": \#(last) }"#
        }
        let rows = [
            laneJSON("Busy", "a", dirty: 0), laneJSON("Busy", "b", dirty: 0), laneJSON("Busy", "c", dirty: 0),
            laneJSON("Changed", "d", dirty: 0, last: 50), laneJSON("Changed", "e", dirty: 3, last: 900),
            laneJSON("Broken", "f", dirty: -1, state: "UNKNOWN"),
        ].joined(separator: ", ")
        let view = try decode(envelope(lanes: #"{ "state": "OK", "reason": "6 lanes", "items": [\#(rows)] }"#))
        let groups = FleetLaneGroup.groups(view.sections.lanes.items)
        XCTAssertEqual(groups.map(\.repo), ["Broken", "Changed", "Busy"])
        XCTAssertEqual(groups[1].lanes.map(\.lane), ["e", "d"])
        XCTAssertEqual(groups[0].summaryText, "1 lane · 1 could not be read")
        XCTAssertEqual(groups[1].summaryText, "2 lanes · 1 with uncommitted changes")
    }

    func test_counts_agree_with_their_noun() {
        XCTAssertEqual(FleetWords.count(1, "lane"), "1 lane")
        XCTAssertEqual(FleetWords.count(0, "lane"), "0 lanes")
        XCTAssertEqual(FleetWords.count(2, "needs", "need"), "2 need")
        XCTAssertEqual(FleetWords.machine(.mac), "Mac")
        XCTAssertEqual(FleetWords.machine(.server), "Server")
    }
}
