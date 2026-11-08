import XCTest

/// The version columns a registry row shows in Tools And Worker Health (AFC-461).
///
/// The rule under test:  every value reads as words,  a missing one reads "Not known"
/// and never blank or zero,  and a value too long for its column is never cut short.
final class ToolVersionColumnsTests: XCTestCase {
    /// Decode one row the way the report reader does.
    private func row(_ json: String) throws -> FleetToolsRow {
        try JSONDecoder().decode(FleetToolsRow.self, from: Data(json.utf8))
    }

    private func columns(_ json: String) throws -> ToolVersionColumns {
        let decoded = try row(json)
        return try XCTUnwrap(ToolVersionColumns.make(for: decoded))
    }

    // MARK: - All present

    /// A row with every field shows all three values,  the source as a tooltip,  and why
    /// it is behind.
    func test_a_row_with_every_field_shows_three_values() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "WARN", "state": "stale", "category": "dev-tool", "tool": "gh", "version": "2.60.0", "latest": "2.62.0", "latestSource": "brew", "basis": "latest", "required": true, "path": "/opt/homebrew/bin/gh", "skew": {"host": "server", "version": "2.61.0"}}
        """)
        XCTAssertEqual(result.cells.map(\.label), ["Installed", "Latest", "Other Host"])
        XCTAssertEqual(result.cells.map(\.text), ["2.60.0", "2.62.0", "Server 2.61.0"])
        XCTAssertTrue(result.cells.allSatisfy(\.isKnown))
        XCTAssertEqual(result.installed.help, "Found at /opt/homebrew/bin/gh")
        XCTAssertEqual(result.latest.help, "Latest read from brew.")
        XCTAssertEqual(result.otherHost.help, "The Server's copy is newer than this one.")
        XCTAssertEqual(result.basis, "Behind the latest release")
        XCTAssertEqual(result.basisHelp, "compared with the latest release")
        XCTAssertTrue(result.explainsStale)
        XCTAssertFalse(result.needsOwnLines)
    }

    /// A current row says what it was compared with,  and does not claim to explain a
    /// problem it does not have.
    func test_a_current_row_says_what_it_was_compared_with() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "OK", "state": "current", "category": "dev-tool", "version": "2.62.0", "latest": "2.62.0", "latestSource": "brew", "basis": "latest"}
        """)
        XCTAssertEqual(result.basis, "Compared with the latest release")
        XCTAssertFalse(result.explainsStale)
    }

    // MARK: - Latest missing

    /// A row whose latest the doctor did not read shows "Not known",  never blank or zero,
    /// and the tooltip names the source it tried.
    func test_a_missing_latest_reads_not_known() throws {
        let result = try columns("""
        {"check": "tool:codex", "status": "WARN", "state": "unknown", "category": "platform-cli", "version": "0.50.0", "latestSource": "npm", "latest": null}
        """)
        XCTAssertEqual(result.latest.text, "Not known")
        XCTAssertFalse(result.latest.isKnown)
        XCTAssertNil(result.latest.value)
        XCTAssertEqual(result.latest.help, "The doctor did not read the latest from npm.")
        XCTAssertEqual(result.installed.text, "0.50.0")
        XCTAssertTrue(result.installed.isKnown)
        // No basis was written,  so the row says nothing about one.
        XCTAssertNil(result.basis)
        XCTAssertFalse(result.explainsStale)
    }

    /// A blank latest is not a version either.
    func test_a_blank_latest_reads_not_known() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "OK", "state": "current", "version": "2.62.0", "latest": "  "}
        """)
        XCTAssertEqual(result.latest.text, "Not known")
        XCTAssertEqual(result.latest.help, "The doctor read no latest version for this tool.")
    }

    // MARK: - Skew present and absent

    /// A row older than the other host names that host and its version,  and says so.
    func test_a_skewed_row_names_the_other_host() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "WARN", "state": "stale", "category": "dev-tool", "version": "2.60.0", "basis": "skew", "skew": {"host": "mac", "version": "2.62.0"}}
        """)
        XCTAssertEqual(result.otherHost.text, "Mac 2.62.0")
        XCTAssertTrue(result.otherHost.isKnown)
        XCTAssertEqual(result.otherHost.help, "The Mac's copy is newer than this one.")
        XCTAssertEqual(result.basis, "Older than the Mac")
        XCTAssertEqual(result.basisHelp, "compared with the other machine")
        XCTAssertTrue(result.explainsStale)
        // The latest was not read on this row.
        XCTAssertEqual(result.latest.text, "Not known")
    }

    /// A skew without a host name still shows the version,  and says "the other machine".
    func test_a_skew_without_a_host_shows_the_version_alone() throws {
        let result = try columns("""
        {"check": "tool:node", "status": "WARN", "state": "stale", "version": "22.10.0", "basis": "skew", "skew": {"host": 1, "version": "22.11.0"}}
        """)
        XCTAssertEqual(result.otherHost.text, "22.11.0")
        XCTAssertEqual(result.otherHost.help, "The other machine's copy is newer than this one.")
        XCTAssertEqual(result.basis, "Older than the other machine")
    }

    /// No skew reads "Not known",  and the tooltip says when the doctor writes one.
    func test_no_skew_reads_not_known() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "OK", "state": "current", "version": "2.62.0", "latest": "2.62.0", "latestSource": "brew", "basis": "latest", "skew": null}
        """)
        XCTAssertEqual(result.otherHost.text, "Not known")
        XCTAssertFalse(result.otherHost.isKnown)
        XCTAssertEqual(
            result.otherHost.help,
            "The doctor names the other machine's version only when it is newer than this one."
        )
    }

    // MARK: - Basis presence only

    /// A tool with no latest source is judged on presence,  and says so in words.
    func test_a_presence_only_row_says_so() throws {
        let result = try columns("""
        {"check": "tool:jq", "status": "OK", "state": "current", "category": "dev-tool", "version": "1.7.1", "basis": "presence", "path": "/usr/bin/jq"}
        """)
        XCTAssertEqual(result.cells.map(\.text), ["1.7.1", "Not known", "Not known"])
        XCTAssertEqual(result.basis, "Installed, no latest to compare")
        XCTAssertEqual(result.basisHelp, "installed, no latest to compare")
        XCTAssertFalse(result.explainsStale)
    }

    /// A basis the doctor's helper does not know reads as nothing,  not as a guess.
    func test_an_unknown_basis_reads_as_nothing() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "WARN", "state": "stale", "version": "2.60.0", "basis": "wobble"}
        """)
        XCTAssertNil(result.basis)
        XCTAssertNil(result.basisHelp)
        XCTAssertFalse(result.explainsStale)
    }

    // MARK: - Very long version strings

    /// A value longer than a column is kept whole,  and the row lists each value on its
    /// own line instead of truncating it.
    func test_a_very_long_version_is_never_cut_short() throws {
        let long = "2.62.0-beta.10+build.20261107.0123456789abcdef0123456789abcdef"
        let result = try columns("""
        {"check": "tool:gh", "status": "WARN", "state": "stale", "version": "\(long)", "latest": "2.62.0", "basis": "latest"}
        """)
        XCTAssertEqual(result.installed.text, long)
        XCTAssertTrue(result.needsOwnLines)
    }

    /// A long other-host value counts its host name too.
    func test_a_long_other_host_value_counts_the_host_name() throws {
        // "Server 1.2.3-rc.10000" is 21 characters:  one past the limit.
        let result = try columns("""
        {"check": "tool:x", "status": "WARN", "state": "stale", "version": "1.2.2", "basis": "skew", "skew": {"host": "server", "version": "1.2.3-rc.10000"}}
        """)
        XCTAssertEqual(result.otherHost.text, "Server 1.2.3-rc.10000")
        XCTAssertTrue(result.needsOwnLines)
    }

    /// Values at the limit stay in their columns.
    func test_values_at_the_limit_stay_in_columns() throws {
        let twenty = String(repeating: "9", count: ToolVersionColumns.columnCharacterLimit)
        let result = try columns("""
        {"check": "tool:x", "status": "OK", "state": "current", "version": "\(twenty)", "latest": "\(twenty)", "basis": "latest"}
        """)
        XCTAssertFalse(result.needsOwnLines)
    }

    // MARK: - Rows without a version reading

    /// A row with no version fields (a fleet tool,  a checkout,  a service) gets no
    /// columns,  so it keeps the look it had.
    func test_a_row_without_version_fields_gets_no_columns() throws {
        let decoded = try row("""
        {"check": "tool:recall", "status": "OK", "state": "current", "category": "fleet-tool", "installed": "1a2f97d", "latest": null, "skew": null}
        """)
        XCTAssertNil(ToolVersionColumns.make(for: decoded))
    }

    /// A skew that names a host but carries no version is not a version reading,  so the
    /// row gets no columns and keeps its old look.
    func test_a_skew_with_a_host_but_no_version_gets_no_columns() throws {
        let decoded = try row("""
        {"check": "tool:gh", "status": "WARN", "state": "unknown", "category": "dev-tool", "skew": {"host": "server"}}
        """)
        XCTAssertEqual(decoded.otherHostName, "server")
        XCTAssertNil(decoded.otherHostVersion)
        XCTAssertNil(ToolVersionColumns.make(for: decoded))
    }

    /// The same skew on a row that does carry a version reads "Not known" for the other
    /// host,  because the host name alone is not its version.
    func test_a_skew_with_a_host_but_no_version_reads_not_known_beside_a_reading() throws {
        let result = try columns("""
        {"check": "tool:gh", "status": "OK", "state": "current", "category": "dev-tool", "version": "2.62.0", "skew": {"host": "server"}}
        """)
        XCTAssertEqual(result.installed.text, "2.62.0")
        XCTAssertEqual(result.otherHost.text, "Not known")
        XCTAssertFalse(result.otherHost.isKnown)
    }

    // MARK: - The narrowest pane

    /// Three columns and their gaps fit the narrowest row,  with room left for the tool
    /// name,  and a value at the character limit fits its column.
    func test_the_columns_fit_the_narrowest_pane() {
        XCTAssertEqual(ToolVersionColumns.narrowestRowWidth, 752)
        let columns = 3 * ToolVersionColumns.columnWidth + 2 * ToolVersionColumns.columnSpacing
        XCTAssertLessThanOrEqual(columns, ToolVersionColumns.narrowestRowWidth)
        // 10-pt monospaced glyphs are 6 pt wide at most.
        XCTAssertLessThanOrEqual(Double(ToolVersionColumns.columnCharacterLimit) * 6, ToolVersionColumns.columnWidth)
    }

    /// The host names read as words.
    func test_host_names_read_as_words() {
        XCTAssertEqual(ToolVersionColumns.hostTitle("mac"), "Mac")
        XCTAssertEqual(ToolVersionColumns.hostTitle("server"), "Server")
        XCTAssertEqual(ToolVersionColumns.hostTitle("vm"), "Vm")
    }
}
