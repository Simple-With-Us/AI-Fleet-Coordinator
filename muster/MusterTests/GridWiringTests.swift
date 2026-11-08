import XCTest

/// The grid's views wired to PR 1477's model:  a run in flight keeps the last answer and
/// says so (SHELL-14, GRID-8, PERF-2), the Overview and MiniMax keep their counts during a
/// run, the detail panel says why a cell is empty (GRID-10) and what the reader found
/// (GRID-31, GRID-30, COPY-63), a large file is drawn a line at a time (PERF-28), and the
/// engine's "not checked" lists are shown (GRID-19).
///
/// The words and the decisions are plain functions in `GridPresentation`, tested here.  The
/// views are SwiftUI files the logic bundle does not compile, so their wiring is pinned by
/// reading their source, as `FleetViewFilterBarTests` does.
final class GridWiringTests: XCTestCase {
    // MARK: Fixtures

    private func result(state: String = "PASS") throws -> CheckResult {
        let json = """
        {
         "ruleId": "claude-hooks-lane-guard", "seat": "CLAUDE", "platform": "claude", "kind": "hooks",
         "state": "\(state)", "reason": "a reason", "reasonCode": "ok",
         "expected": {}, "observed": {},
         "proofLevelRequired": "L0", "proofLevelAchieved": "L0",
         "collector": "mac", "checkType": "hook_registered",
         "checkedAt": "2026-10-19T14:30:00Z", "expiresAt": null,
         "ttlSeconds": 900, "stale": false, "failing": false
        }
        """
        return try JSONDecoder().decode(CheckResult.self, from: Data(json.utf8))
    }

    private func answeredCell(beingChecked: Bool) throws -> GridCell {
        let rules = [try result()]
        return GridCell(
            seat: "CLAUDE",
            kind: .hooks,
            state: GridBuilder.worstState(of: rules),
            rules: rules,
            coverReason: nil,
            isBeingChecked: beingChecked
        )
    }

    private func noAnswerYetCell() -> GridCell {
        GridCell(
            seat: "CLAUDE",
            kind: .hooks,
            state: .unknown,
            rules: [],
            coverReason: nil,
            gap: .beingChecked,
            isBeingChecked: true
        )
    }

    private func source(_ relative: String) throws -> String {
        let muster = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster")
        let url = muster.appendingPathComponent(relative)
        try XCTUnwrap(
            FileManager.default.fileExists(atPath: url.path) ? url : nil,
            "muster/Muster/\(relative) is missing"
        )
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// The text of one function in a view's source, from its declaration to the next one.
    private func body(of function: String, before next: String, in text: String) throws -> String {
        let start = try XCTUnwrap(text.range(of: function), "the view no longer has \(function)")
        let rest = text[start.upperBound...]
        let end = try XCTUnwrap(rest.range(of: next), "the view no longer has \(next) after \(function)")
        return String(rest[..<end.lowerBound])
    }

    // MARK: A run in flight (SHELL-14, GRID-8, PERF-2)

    /// A cell with no earlier answer draws as a neutral CHECKING, never as a red UNKNOWN,
    /// and its state underneath stays unknown so nothing reads as proven.
    func test_a_cell_with_no_answer_yet_draws_as_checking() {
        let cell = noAnswerYetCell()
        XCTAssertTrue(cell.isCheckingWithNoAnswer)
        XCTAssertEqual(GridPresentation.BadgeChecking.of(cell), .noAnswerYet)
        XCTAssertTrue(GridPresentation.BadgeChecking.of(cell).isNeutral)
        XCTAssertEqual(GridPresentation.badgeWord(for: cell), "CHECKING")
        XCTAssertNotEqual(GridPresentation.badgeWord(for: cell), CellState.unknown.gridWord)
        XCTAssertEqual(GridPresentation.badgeSymbol(for: cell), GridPresentation.checkingSymbol)
        XCTAssertEqual(GridPresentation.spokenWord(for: cell), "being checked, no answer yet")
        XCTAssertEqual(cell.state, .unknown)
    }

    /// A cell whose last answer is checked again keeps that answer's word and colour, dimmed.
    func test_a_cell_checked_again_keeps_its_answer_dimmed() throws {
        let cell = try answeredCell(beingChecked: true)
        let checking = GridPresentation.BadgeChecking.of(cell)
        XCTAssertEqual(checking, .lastAnswer)
        XCTAssertFalse(checking.isNeutral)
        XCTAssertEqual(GridPresentation.badgeWord(for: cell), cell.gridWord)
        XCTAssertEqual(GridPresentation.badgeSymbol(for: cell), cell.gridSymbol)
        XCTAssertEqual(GridPresentation.spokenWord(for: cell), "\(cell.gridWord), being checked again")
        XCTAssertLessThan(GridPresentation.badgeOpacity(checking: checking, pastLimit: false), 1)
    }

    /// A settled cell draws as it always did.
    func test_a_settled_cell_draws_as_its_state() throws {
        let cell = try answeredCell(beingChecked: false)
        XCTAssertEqual(GridPresentation.BadgeChecking.of(cell), .settled)
        XCTAssertEqual(GridPresentation.badgeWord(for: cell), cell.gridWord)
        XCTAssertEqual(GridPresentation.spokenWord(for: cell), cell.gridWord)
        XCTAssertEqual(GridPresentation.badgeOpacity(checking: .settled, pastLimit: false), 1)
    }

    /// Past the time limit is the dimmest, whether or not the cell is being checked again.
    func test_the_badge_opacity_takes_the_dimmer_of_the_two() {
        XCTAssertEqual(GridPresentation.badgeOpacity(checking: .settled, pastLimit: true), 0.45)
        XCTAssertEqual(GridPresentation.badgeOpacity(checking: .lastAnswer, pastLimit: false), 0.6)
        XCTAssertEqual(GridPresentation.badgeOpacity(checking: .lastAnswer, pastLimit: true), 0.45)
        XCTAssertEqual(GridPresentation.badgeOpacity(checking: .noAnswerYet, pastLimit: false), 1)
    }

    /// The counts and the MiniMax panel treat the last answer kept during a run as an answer.
    func test_the_last_answer_counts_as_an_answer() {
        XCTAssertTrue(GridPresentation.engineAnswerShown(phase: .loaded, showingLastAnswer: false))
        XCTAssertTrue(GridPresentation.engineAnswerShown(phase: .running, showingLastAnswer: true))
        XCTAssertFalse(GridPresentation.engineAnswerShown(phase: .running, showingLastAnswer: false))
        XCTAssertFalse(GridPresentation.engineAnswerShown(phase: .idle, showingLastAnswer: false))
        XCTAssertFalse(GridPresentation.engineAnswerShown(phase: .failed("the engine did not answer"), showingLastAnswer: false))
    }

    /// The grid shows the store's checking note in a neutral banner, keeps the panel open
    /// through a run, and draws each badge through the checking words.
    func test_the_grid_shows_the_run_in_neutral_words() throws {
        let view = try source("Views/FleetGridView.swift")
        XCTAssertTrue(view.contains("if store.isChecking, let note = store.checkingNote {"))
        XCTAssertTrue(view.contains("checkingBanner(note)"))
        let banner = try body(of: "private func checkingBanner(", before: "private func notImplementedSection(", in: view)
        XCTAssertFalse(banner.contains(".orange"), "the checking banner must be neutral")
        XCTAssertFalse(banner.contains(".red"), "the checking banner must be neutral")
        XCTAssertTrue(banner.contains(".foregroundStyle(.secondary)"))
        // Each badge goes through the checking words and colour.
        XCTAssertTrue(view.contains("Text(GridPresentation.badgeWord(for: cell))"))
        XCTAssertTrue(view.contains("Image(systemName: GridPresentation.badgeSymbol(for: cell))"))
        XCTAssertTrue(view.contains("GridPresentation.badgeOpacity(checking: checking, pastLimit: pastLimit)"))
        XCTAssertTrue(view.contains("checking.isNeutral ? Color.secondary : cell.state.tint"))
        XCTAssertFalse(view.contains("Text(cell.gridWord)"), "a badge must not print a red UNKNOWN for a cell being checked")
        // The panel still never closes in the middle of a run.
        XCTAssertTrue(view.contains("selection.shouldClose(visibleRoster: roster, isRunning: store.isRunning"))
        XCTAssertTrue(view.contains(".onChange(of: store.isRunning)"))
        // The one check time stays during a run.
        XCTAssertTrue(view.contains("GridPresentation.checkedLine(commonCheckedAt: store.commonCheckedAt, now: now)"))
    }

    /// The Overview and the MiniMax panel keep their answers during a run, and the MiniMax
    /// panel's proofs and engine line take the same flag as its attention list.
    func test_the_counts_keep_the_last_answer() throws {
        let overview = try source("Views/OverviewView.swift")
        XCTAssertFalse(overview.contains("trusted: store.phase == .loaded"))
        XCTAssertTrue(overview.contains(
            "trusted: GridPresentation.engineAnswerShown(phase: store.phase, showingLastAnswer: store.isShowingLastAnswer)"
        ))
        XCTAssertTrue(overview.contains("cells: store.cells.filter { !$0.isCheckingWithNoAnswer }"))

        let miniMax = try source("Views/MiniMaxDetailView.swift")
        XCTAssertFalse(miniMax.contains("engineAnswered: store.phase == .loaded"))
        XCTAssertTrue(miniMax.contains("engineAnswered: engineAnswerShown"))
        XCTAssertTrue(miniMax.contains("store.hookProofs(seat: seat, includingLastAnswer: store.isShowingLastAnswer)"))
        XCTAssertTrue(miniMax.contains("showingLastAnswer: store.isShowingLastAnswer"))
    }

    // MARK: Not checked (GRID-19)

    /// A surface the engine does not check reads as a "Not checked" line with its reason.
    func test_an_uncovered_surface_reads_as_not_checked() {
        let lines = GridPresentation.notCheckedLines([
            UncoveredSurface(seat: "CLAUDE", surface: "statusline", reason: "no config kind covers it")
        ])
        XCTAssertEqual(lines, ["Not checked:  statusline \u{2014} no config kind covers it"])
        XCTAssertEqual(GridPresentation.notCheckedLines([]), [])
    }

    /// The rules the engine cannot run are headed in Title Case with their count, and the
    /// explanation's sentences carry the owner's gap.
    func test_the_unimplemented_rules_are_headed_with_their_count() {
        XCTAssertEqual(GridPresentation.notImplementedTitle(count: 2), "Rules The Engine Cannot Run Yet (2)")
        XCTAssertTrue(GridPresentation.notImplementedExplanation.contains(ServerStatusCopy.sentenceGap))
        XCTAssertFalse(GridPresentation.notImplementedExplanation.contains(".  "))
    }

    /// The grid lists each seat's uncovered surfaces and the engine's unimplemented rules,
    /// and shows nothing when the lists are empty.
    func test_the_grid_lists_what_is_not_checked() throws {
        let view = try source("Views/FleetGridView.swift")
        XCTAssertTrue(view.contains("GridPresentation.notCheckedLines(store.uncoveredSurfaces(seat: seat))"))
        XCTAssertTrue(view.contains("if !store.notImplemented.isEmpty {"))
        XCTAssertTrue(view.contains("notImplementedSection(store.notImplemented)"))
        XCTAssertTrue(view.contains("Text(rule.ruleId)"))
        XCTAssertTrue(view.contains("rule.ownerSeat"))
        XCTAssertTrue(view.contains("Text(rule.reason)"))
    }

    // MARK: The detail panel (GRID-10, GRID-31, GRID-30, PERF-28, COPY-63)

    /// The panel and Seat Setup say why a cell is empty, and never "No rule covers" by default.
    func test_an_empty_cell_says_why() throws {
        let panel = try source("Views/ConfigFilePanel.swift")
        XCTAssertTrue(panel.contains("Text(artifacts.emptyText)"))
        XCTAssertFalse(panel.contains("coverReason ?? \"No rule covers this seat and kind.\""))
        let setup = try source("Views/SeatSetupView.swift")
        XCTAssertTrue(setup.contains("? found.emptyText"))
        XCTAssertFalse(setup.contains("coverReason ?? \"No rule covers this seat and kind.\""))
    }

    /// The panel's truncation sentence is the reader's own, and the instruction preview,
    /// which prints its own, does not get a second copy.
    func test_the_notes_above_the_body_are_the_readers() {
        let read = ConfigFileRead(
            state: .truncated,
            view: .file(path: "~/.claude/CLAUDE.md", text: "text", truncated: true, redacted: false),
            reformatted: true
        )
        XCTAssertEqual(read.notes.first, GridPresentation.truncationNote)
        let plain = GridPresentation.readNotes(read, truncated: true, previewShowsTruncation: false)
        XCTAssertEqual(plain, read.notes)
        let preview = GridPresentation.readNotes(read, truncated: true, previewShowsTruncation: true)
        XCTAssertFalse(preview.contains(GridPresentation.truncationNote))
        XCTAssertEqual(preview.count, read.notes.count - 1)
        // Without a reading, a cut-off file still says so, and a whole one says nothing.
        XCTAssertEqual(GridPresentation.readNotes(nil, truncated: true, previewShowsTruncation: false),
                       [GridPresentation.truncationNote])
        XCTAssertEqual(GridPresentation.readNotes(nil, truncated: false, previewShowsTruncation: false), [])
    }

    /// The panel uses the reader's summary, message and notes, and draws a file lazily.
    func test_the_panel_uses_the_readers_words_and_draws_lazily() throws {
        let panel = try source("Views/ConfigFilePanel.swift")
        XCTAssertTrue(panel.contains("read: artifacts.reads.indices.contains(index) ? artifacts.reads[index] : nil"))
        XCTAssertTrue(panel.contains("Text(read?.summary ?? artifact.summary)"))
        XCTAssertTrue(panel.contains("if let message = read?.message {"))
        XCTAssertTrue(panel.contains("GridPresentation.readNotes("))
        XCTAssertTrue(panel.contains("LazyTextLines(text: text)"))
        XCTAssertTrue(panel.contains("LazyVStack(alignment: .leading, spacing: 0)"))
        XCTAssertTrue(panel.contains(".textSelection(.enabled)"))
        // The one giant selectable text is gone.
        XCTAssertFalse(panel.contains("Text(text.isEmpty ? \"(the file is empty)\" : text)"))
        XCTAssertFalse(panel.contains("Text(entries.joined(separator: \"\\n\"))"))
        // Seat Setup's rule blocks get the same reading.
        let setup = try source("Views/SeatSetupView.swift")
        XCTAssertTrue(setup.contains("read: found.reads.indices.contains(index) ? found.reads[index] : nil"))
    }

    /// A file's text becomes the lines drawn, Windows line breaks included, with no empty
    /// line at the end, a space for an empty line and long lines in pieces.
    func test_a_files_text_becomes_the_lines_drawn() {
        XCTAssertEqual(GridPresentation.displayLines("a\nb\n"), ["a", "b"])
        XCTAssertEqual(GridPresentation.displayLines("a\r\nb"), ["a", "b"])
        XCTAssertEqual(GridPresentation.displayLines("a\n\nb"), ["a", " ", "b"])
        XCTAssertEqual(GridPresentation.displayLines("one line"), ["one line"])
        XCTAssertEqual(GridPresentation.displayLines(""), [" "])
        XCTAssertEqual(GridPresentation.displayLines("\n"), [" "])
    }

    /// A line past the limit, such as minified JSON, is drawn in pieces that add up to it.
    func test_a_long_line_is_drawn_in_pieces() {
        let long = String(repeating: "x", count: 2_500)
        let pieces = GridPresentation.displayLines(long, limit: 1_000)
        XCTAssertEqual(pieces.map(\.count), [1_000, 1_000, 500])
        XCTAssertEqual(pieces.joined(), long)
        XCTAssertEqual(GridPresentation.displayLines("abc", limit: 0), ["a", "b", "c"])
    }
}
