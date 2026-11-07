import XCTest

/// The Compliance Grid's words:  its scope, its one spelling of each state, where a seat and
/// a rule are read, the time limits, the watch line and the panel's selection.
final class GridPresentationTests: XCTestCase {
    private let gap = "\u{00A0} "

    private func manifest() throws -> SeatManifest {
        try JSONDecoder().decode(SeatManifest.self, from: Data(Fixtures.manifest.utf8))
    }

    /// One result, decoded the way the envelope's are, so a field the engine adds later
    /// does not break the test.
    private func result(
        ruleId: String = "claude-hooks-lane-guard",
        kind: String = "hooks",
        state: String = "PASS",
        reasonCode: String = "ok",
        checkType: String = "hook_registered",
        checkedAt: String = "2026-10-19T14:30:00Z",
        expiresAt: String? = "2026-10-19T14:45:00Z"
    ) throws -> CheckResult {
        let expires = expiresAt.map { "\"\($0)\"" } ?? "null"
        let json = """
        {
         "ruleId": "\(ruleId)", "seat": "CLAUDE", "platform": "claude", "kind": "\(kind)",
         "state": "\(state)", "reason": "a reason", "reasonCode": "\(reasonCode)",
         "expected": {}, "observed": {},
         "proofLevelRequired": "L0", "proofLevelAchieved": "L0",
         "collector": "mac", "checkType": "\(checkType)",
         "checkedAt": "\(checkedAt)", "expiresAt": \(expires),
         "ttlSeconds": 900, "stale": false, "failing": false
        }
        """
        return try JSONDecoder().decode(CheckResult.self, from: Data(json.utf8))
    }

    private func cell(_ rules: [CheckResult], kind: ConfigKind = .hooks, state: CellState? = nil) -> GridCell {
        GridCell(
            seat: "CLAUDE",
            kind: kind,
            state: state ?? GridBuilder.worstState(of: rules),
            rules: rules,
            coverReason: nil
        )
    }

    private func date(_ text: String) -> Date {
        FleetTime.parse(text)!
    }

    // MARK: Scope

    /// The grid and the Overview both say the server's seat config is not checked here.
    func test_the_scope_says_the_server_is_not_checked_here() {
        XCTAssertTrue(GridPresentation.scopeExplanation.contains("The server's seat config is not checked here."))
        XCTAssertTrue(GridPresentation.overviewScope.contains("The server's seat config is not checked here."))
        XCTAssertTrue(GridPresentation.scopeExplanation.contains("secret store (Infisical)"))
        XCTAssertTrue(GridPresentation.scopeExplanation.contains(gap))
    }

    /// A seat with rules is read on the Mac;  a seat with none says it is not on the Mac,
    /// in its cover's own sentence;  a seat the manifest does not name is not known.
    func test_a_seats_location_comes_from_its_rules_and_covers() throws {
        let manifest = try manifest()
        XCTAssertEqual(GridPresentation.SeatLocation.of(seat: "CLAUDE", manifest: manifest), .onTheMac)
        XCTAssertEqual(
            GridPresentation.SeatLocation.of(seat: "ECHO", manifest: manifest),
            .notOnTheMac(reason: "Echo runs elsewhere.")
        )
        XCTAssertEqual(GridPresentation.SeatLocation.of(seat: "NOBODY", manifest: manifest), .notKnown)
        XCTAssertEqual(GridPresentation.SeatLocation.of(seat: "CLAUDE", manifest: nil), .notKnown)
    }

    /// The chip and the row's note say it in words, and only a seat that needs a note gets one.
    func test_the_location_chip_and_note() {
        XCTAssertEqual(GridPresentation.SeatLocation.onTheMac.chip, "On The Mac")
        XCTAssertNil(GridPresentation.SeatLocation.onTheMac.note)
        let away = GridPresentation.SeatLocation.notOnTheMac(reason: "Echo runs elsewhere.")
        XCTAssertEqual(away.chip, "Not On The Mac")
        XCTAssertEqual(away.note, "Not on the Mac, so nothing here is checked for it.\(gap)Echo runs elsewhere.")
        XCTAssertNotNil(GridPresentation.SeatLocation.notKnown.note)
    }

    /// The Permissions key rule reads the secret store, and every other check reads the Mac.
    func test_a_rules_location_comes_from_its_check_type() {
        XCTAssertEqual(GridPresentation.RuleLocation.of(checkType: "credential_key_present"), .secretStore)
        XCTAssertEqual(GridPresentation.RuleLocation.of(checkType: "credential_stat"), .macFile)
        XCTAssertEqual(GridPresentation.RuleLocation.of(checkType: "marked_block_hash"), .macFile)
        XCTAssertEqual(GridPresentation.RuleLocation.secretStore.pathLabel, "Store folder")
        XCTAssertTrue(GridPresentation.RuleLocation.secretStore.label.contains("Secret Store (Infisical)"))
        XCTAssertEqual(GridPresentation.RuleLocation.macFile.label, "Read On The Mac")
    }

    /// A cell's tooltip leads with where it is read, and a Permissions cell names both places.
    func test_the_tooltip_names_where_the_cell_is_read() throws {
        let key = try result(ruleId: "claude-permissions-zulip-key", kind: "permissions",
                             checkType: "credential_key_present")
        let file = try result(ruleId: "claude-permissions-zulip-retired-file", kind: "permissions",
                              checkType: "credential_stat")
        let both = cell([key, file], kind: .permissions)
        XCTAssertEqual(GridPresentation.locationLine(for: both), "Read on the Mac and from the secret store (Infisical).")
        XCTAssertEqual(
            GridPresentation.locationLine(for: cell([key], kind: .permissions)),
            "Read from the secret store (Infisical), asked from the Mac."
        )
        XCTAssertEqual(GridPresentation.locationLine(for: cell([try result()])), "Read on the Mac.")
        let tooltip = GridPresentation.tooltip(for: both)
        XCTAssertTrue(tooltip.hasPrefix("Read on the Mac and from the secret store (Infisical).\n"))
        XCTAssertTrue(tooltip.hasSuffix(both.tooltip))
    }

    /// A cell with no rules adds no location line, so its tooltip is the grid's own.
    func test_a_cell_with_no_rules_adds_no_location_line() {
        let empty = GridCell(seat: "ECHO", kind: .hooks, state: .covered, rules: [], coverReason: "Echo runs elsewhere.")
        XCTAssertNil(GridPresentation.locationLine(for: empty))
        XCTAssertEqual(GridPresentation.tooltip(for: empty), empty.tooltip)
    }

    // MARK: State words

    /// One spelling for each state:  the engine's own words, and the cover in plain words.
    func test_each_state_has_one_spelling() {
        XCTAssertEqual(CellState.passed.gridWord, "PASS")
        XCTAssertEqual(CellState.missing.gridWord, "MISSING")
        XCTAssertEqual(CellState.unproven.gridWord, "UNPROVEN")
        XCTAssertEqual(CellState.unsupported.gridWord, "UNSUPPORTED")
        XCTAssertEqual(CellState.unknown.gridWord, "UNKNOWN")
        XCTAssertEqual(CellState.covered.gridWord, "DOES NOT APPLY")
        // No abbreviation survives on the grid's own surfaces.
        let words = GridPresentation.stateLegend.map(\.word)
        for code in ["MISS", "UNPRV", "UNSUP", "UNK", "N/A", "COVERED"] {
            XCTAssertFalse(words.contains(code), code)
        }
    }

    /// The legend covers every word a badge can show, once each, with a meaning, an action
    /// and a symbol.
    func test_the_legend_covers_every_state_once() {
        let legend = GridPresentation.stateLegend
        let allStates: [CellState] = [.passed, .drift, .missing, .unproven, .unsupported, .unknown, .covered]
        for state in allStates {
            XCTAssertEqual(legend.filter { $0.state == state }.count, 1, state.rawValue)
            XCTAssertEqual(legend.first { $0.state == state }?.word, state.gridWord)
        }
        XCTAssertEqual(legend.filter { $0.word == GridCell.noGuardWord }.count, 1)
        XCTAssertEqual(Set(legend.map(\.word)).count, legend.count)
        XCTAssertTrue(legend.allSatisfy { !$0.meaning.isEmpty && !$0.action.isEmpty && !$0.symbol.isEmpty })
    }

    /// A rule that does not apply reads as the cover does, and any other rule in its own state.
    func test_a_rules_word_uses_the_same_spelling() throws {
        XCTAssertEqual(GridPresentation.ruleWord(try result(state: "MISSING")), "MISSING")
        XCTAssertEqual(GridPresentation.ruleWord(try result(state: "PASS", reasonCode: "not-applicable")), "DOES NOT APPLY")
    }

    // MARK: The check time and its limits

    /// The check time is measured against the clock it is given, so it ages as time passes.
    func test_the_checked_line_ages_with_the_clock() {
        let at = date("2026-10-19T14:30:00Z")
        let early = GridPresentation.checkedLine(commonCheckedAt: at, now: at.addingTimeInterval(120))
        let late = GridPresentation.checkedLine(commonCheckedAt: at, now: at.addingTimeInterval(3 * 3600))
        XCTAssertEqual(early, "Checked on the Mac at \(FleetTime.clock(at)),  2m ago")
        XCTAssertEqual(late, "Checked on the Mac at \(FleetTime.clock(at)),  3h ago")
        XCTAssertNil(GridPresentation.checkedLine(commonCheckedAt: nil, now: at))
    }

    /// A result past its limit is noticed without a new run:  the same cell crosses the
    /// line as the clock moves.
    func test_a_cell_crosses_its_limit_without_a_rerun() throws {
        let cell = cell([try result()])
        let expiry = date("2026-10-19T14:45:00Z")
        XCTAssertFalse(GridPresentation.isPastLimit(cell, now: expiry.addingTimeInterval(-1)))
        XCTAssertTrue(GridPresentation.isPastLimit(cell, now: expiry.addingTimeInterval(1)))
        XCTAssertNil(GridPresentation.pastLimitBanner(cells: [cell], now: expiry.addingTimeInterval(-1)))
        let banner = GridPresentation.pastLimitBanner(cells: [cell], now: expiry.addingTimeInterval(8 * 3600))
        XCTAssertEqual(
            banner,
            "1 cell is past the time limit the engine set for the results, the first since \(FleetTime.clock(expiry))."
                + "\(gap)Read them as not current until the seat setup is checked again."
        )
    }

    /// A rule with no limit never reads as past it.
    func test_a_rule_with_no_limit_is_never_past_it() throws {
        let cell = cell([try result(expiresAt: nil)])
        XCTAssertFalse(GridPresentation.isPastLimit(cell, now: .distantFuture))
    }

    // MARK: The detail panel

    /// The panel's read is keyed on the cell and the run, so a new run reads again.
    func test_the_panels_read_key_changes_with_the_run() throws {
        let selection = GridSelection(cell([try result()]))
        XCTAssertEqual(selection.cellID, "CLAUDE/hooks")
        let first = selection.readKey(generatedAt: date("2026-10-19T14:30:00Z"))
        let second = selection.readKey(generatedAt: date("2026-10-19T14:40:00Z"))
        let running = selection.readKey(generatedAt: nil)
        XCTAssertNotEqual(first, second)
        XCTAssertNotEqual(first, running)
        XCTAssertTrue(first.hasPrefix("CLAUDE/hooks@"))
    }

    /// The selection is an identity, so the panel looks up the current cell and not a copy.
    func test_the_selection_resolves_to_the_current_cell() throws {
        let manifest = try manifest()
        let before = GridBuilder.build(roster: ["CLAUDE"], manifest: manifest, results: [try result(state: "PASS")])
        let after = GridBuilder.build(roster: ["CLAUDE"], manifest: manifest, results: [try result(state: "MISSING")])
        let selection = GridSelection(before.first { $0.kind == .hooks }!)
        let resolved = after.first { $0.seat == selection.seat && $0.kind == selection.kind }
        XCTAssertEqual(resolved?.state, .missing)
        XCTAssertEqual(resolved?.id, selection.cellID)
    }

    /// A filter that hides the selected seat closes the panel.
    func test_a_hidden_row_closes_the_panel() {
        let selection = GridSelection(seat: "CLAUDE", kind: .hooks)
        XCTAssertTrue(selection.isShown(in: ["CLAUDE", "ECHO"]))
        XCTAssertFalse(selection.isShown(in: ["ECHO"]))
    }

    /// The panel says which run it shows.
    func test_the_panel_says_which_run_it_shows() {
        let at = date("2026-10-19T14:30:00Z")
        XCTAssertEqual(GridPresentation.asOfLine(generatedAt: at), "As of \(FleetTime.clock(at))")
        XCTAssertEqual(GridPresentation.asOfLine(generatedAt: nil), "Not checked yet")
    }

    /// A source line shows only when the rule cites one.
    func test_an_empty_source_shows_no_line() {
        XCTAssertNil(GridPresentation.shownSource(nil))
        XCTAssertNil(GridPresentation.shownSource("  "))
        XCTAssertEqual(GridPresentation.shownSource("Jay, October 9"), "Jay, October 9")
    }

    // MARK: The filter

    /// An empty filter says nothing matches, and names the filter and the search.
    func test_an_empty_filter_says_nothing_matches() {
        let at = date("2026-10-19T14:30:00Z")
        XCTAssertEqual(
            GridPresentation.emptyFilterText(filter: .red, search: "", rosterCount: 3, checkedAt: at),
            "No seat matches \u{201C}Red\u{201D}.\(gap)Nothing to show here in the check at \(FleetTime.clock(at))."
        )
        XCTAssertEqual(
            GridPresentation.emptyFilterText(filter: .all, search: "zz", rosterCount: 3, checkedAt: at),
            "No seat matches a tag with \u{201C}zz\u{201D}."
        )
        XCTAssertEqual(
            GridPresentation.emptyFilterText(filter: .drift, search: "zz", rosterCount: 3, checkedAt: nil),
            "No seat matches \u{201C}Drifted\u{201D} and a tag with \u{201C}zz\u{201D}."
        )
        XCTAssertTrue(GridPresentation.emptyFilterText(filter: .all, search: "", rosterCount: 0, checkedAt: nil)
            .hasPrefix("The grid has no seats"))
    }

    // MARK: Watching

    /// Watching nothing, or not watching, is a warning and never the watching tone.
    func test_watching_nothing_is_a_warning() {
        let nothing = GridPresentation.WatchLine.make(isWatching: true, pathCount: 0, seatCount: 0, recheckSeconds: 600)
        XCTAssertEqual(nothing.tone, .warning)
        XCTAssertTrue(nothing.text.hasPrefix("Watching nothing"))
        XCTAssertTrue(nothing.text.contains("every 10 minutes"))
        let off = GridPresentation.WatchLine.make(isWatching: false, pathCount: 12, seatCount: 4, recheckSeconds: 600)
        XCTAssertEqual(off.tone, .warning)
        XCTAssertTrue(off.text.hasPrefix("Not watching"))
    }

    /// Watching paths says how many, for how many seats, and that the secret store checks
    /// are not watched.
    func test_watching_says_what_is_watched() {
        let line = GridPresentation.WatchLine.make(isWatching: true, pathCount: 12, seatCount: 1, recheckSeconds: 600)
        XCTAssertEqual(line.tone, .watching)
        XCTAssertEqual(
            line.text,
            "Set to watch 12 paths on the Mac for 1 seat.\(gap)Secret store checks cannot be watched, so they are checked again every 10 minutes."
        )
    }

    // MARK: Compliance rules

    private func complianceManifest() throws -> SeatManifest {
        let json = """
        {
         "schemaVersion": 2,
         "seats": {},
         "complianceRules": [
          {"id": "c-fleet", "scope": ["fleet"], "implemented": false, "implementationReason": "no check yet"},
          {"id": "c-mac-a", "scope": ["mac"], "implemented": true},
          {"id": "c-server", "scope": ["hetzner"], "implemented": false},
          {"id": "c-mac-b", "scope": ["mac"]},
          {"id": "c-none"}
         ]
        }
        """
        return try JSONDecoder().decode(SeatManifest.self, from: Data(json.utf8))
    }

    /// No compliance rule reads as a result:  a built check says it exists and shows no
    /// result, and the rest say they are not built.
    func test_a_compliance_rule_never_claims_a_result() throws {
        let rules = try complianceManifest().allComplianceRules
        let built = rules.first { $0.id == "c-mac-a" }!
        let unbuilt = rules.first { $0.id == "c-fleet" }!
        let unsaid = rules.first { $0.id == "c-mac-b" }!
        XCTAssertEqual(GridPresentation.complianceStatus(built).text, "CHECK EXISTS, RESULT NOT SHOWN")
        XCTAssertTrue(GridPresentation.complianceStatus(built).isBuilt)
        XCTAssertEqual(GridPresentation.complianceStatus(unbuilt).text, "NOT BUILT YET")
        XCTAssertFalse(GridPresentation.complianceStatus(unbuilt).isBuilt)
        XCTAssertTrue(GridPresentation.complianceStatus(unsaid).isBuilt)
        for rule in rules {
            XCTAssertFalse(GridPresentation.complianceStatus(rule).text.contains("ACTIVE"))
        }
    }

    /// The rules group by place, the Mac first, and each rule is listed once.
    func test_compliance_rules_group_by_place_each_once() throws {
        let groups = GridPresentation.complianceGroups(try complianceManifest().allComplianceRules)
        XCTAssertEqual(groups.map(\.heading), ["On The Mac", "On The Server", "Across The Fleet", "No Place Named"])
        XCTAssertEqual(groups.first?.ruleIDs, ["c-mac-a", "c-mac-b"])
        let all = groups.flatMap(\.ruleIDs)
        XCTAssertEqual(all.count, 5)
        XCTAssertEqual(Set(all).count, 5)
    }

    /// The manifest's host words become the app's place words.
    func test_scope_words_use_the_apps_places() {
        XCTAssertEqual(GridPresentation.scopeWords("hetzner"), "On The Server")
        XCTAssertEqual(GridPresentation.scopeWords("mac"), "On The Mac")
        XCTAssertEqual(GridPresentation.scopeWords("fleet"), "Across The Fleet")
        XCTAssertEqual(GridPresentation.scopeWords("vm"), "vm")
    }
}
