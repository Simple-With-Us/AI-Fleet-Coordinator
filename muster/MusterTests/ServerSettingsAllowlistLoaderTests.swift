import XCTest

/// Where the settings allowlist copy comes from:  the app's own bundled copy first, the fleet
/// checkout's second, and a reason that names both places when neither can be read.
///
/// The owner's report was "this checkout's copy of the allowlist could not be read":  a Finder
/// launch has no repository root, the default checkout was stale and the file was not in the app,
/// and the reader said nothing of why.
final class ServerSettingsAllowlistLoaderTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("allowlist-loader-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private static let valid = """
    {"schemaVersion": 1, "applyEnabled": false,
     "seat": {"enabled": {"type": "bool"}, "wake": {"type": "enum", "values": ["inbox", "http"]}},
     "tables": {"daemon": {"max_wakes": {"type": "int", "min": 1, "max": 9}}}}
    """

    /// The same document with one extra key under `seat`, so the two copies can be told apart.
    private static let other = valid.replacingOccurrences(
        of: #""enabled": {"type": "bool"}"#,
        with: #""enabled": {"type": "bool"}, "extra_flag": {"type": "bool"}"#
    )

    private func write(_ text: String, named name: String) throws -> URL {
        let url = directory.appendingPathComponent(name)
        try Data(text.utf8).write(to: url)
        return url
    }

    private var missing: URL { directory.appendingPathComponent("nowhere.json") }

    func test_the_bundled_copy_wins_over_the_checkouts() throws {
        let bundled = try write(Self.valid, named: "bundled.json")
        let checkout = try write(Self.other, named: "checkout.json")
        let load = ServerSettingsAllowlistLoader.load(bundled: bundled, checkout: checkout)
        XCTAssertEqual(load.source, .bundled)
        XCTAssertNil(load.problem)
        XCTAssertEqual(load.allowlist?.seat["extra_flag"], nil)
        XCTAssertNotNil(load.allowlist?.seat["enabled"])
    }

    func test_the_checkout_is_used_when_the_app_holds_no_copy() throws {
        let checkout = try write(Self.other, named: "checkout.json")
        let load = ServerSettingsAllowlistLoader.load(bundled: nil, checkout: checkout)
        XCTAssertEqual(load.source, .checkout)
        XCTAssertNil(load.problem)
        XCTAssertNotNil(load.allowlist?.seat["extra_flag"])
    }

    func test_the_checkout_is_used_when_the_bundled_copy_is_broken() throws {
        let bundled = try write("{not json", named: "bundled.json")
        let checkout = try write(Self.valid, named: "checkout.json")
        let load = ServerSettingsAllowlistLoader.load(bundled: bundled, checkout: checkout)
        XCTAssertEqual(load.source, .checkout)
        XCTAssertNotNil(load.allowlist)
    }

    func test_both_missing_says_not_bundled_and_where_the_checkout_file_should_be() {
        let load = ServerSettingsAllowlistLoader.load(bundled: nil, checkout: missing)
        XCTAssertNil(load.allowlist)
        XCTAssertNil(load.source)
        let problem = load.problem ?? ""
        XCTAssertTrue(problem.contains("In the app: not bundled"), problem)
        XCTAssertTrue(problem.contains("Update Muster, or open it from the checkout."), problem)
        XCTAssertTrue(problem.contains("In the checkout: checkout file missing at \(missing.path)"), problem)
        XCTAssertTrue(problem.contains("offers nothing to change"), problem)
        XCTAssertFalse(problem.contains(".  "), "a sentence gap is a real U+00A0 and a space")
        XCTAssertTrue(problem.contains(".\u{00A0} "))
    }

    func test_both_unreadable_names_each_parse_failure() throws {
        let bundled = try write("{not json", named: "bundled.json")
        let checkout = try write(#"{"seat": {}}"#, named: "checkout.json")
        let load = ServerSettingsAllowlistLoader.load(bundled: bundled, checkout: checkout)
        XCTAssertNil(load.allowlist)
        let problem = load.problem ?? ""
        XCTAssertTrue(problem.contains("bundled file parse failed: it is not valid JSON"), problem)
        XCTAssertTrue(problem.contains("checkout file parse failed: it has no tables"), problem)
    }

    func test_a_bundled_copy_that_is_missing_on_disk_is_named() {
        let load = ServerSettingsAllowlistLoader.load(bundled: missing, checkout: nil)
        let problem = load.problem ?? ""
        XCTAssertTrue(problem.contains("bundled file missing at \(missing.path)"), problem)
        XCTAssertTrue(problem.contains("no checkout to look in"), problem)
    }

    func test_the_parse_problem_words_say_what_is_wrong() {
        XCTAssertNil(ServerSettingsAllowlistLoader.parseProblem(Data(Self.valid.utf8)))
        XCTAssertEqual(ServerSettingsAllowlistLoader.parseProblem(Data("[1]".utf8)), "it is not a JSON object")
        XCTAssertEqual(ServerSettingsAllowlistLoader.parseProblem(Data(#"{"tables": {}}"#.utf8)), "it has no seat table")
    }

    /// The store carries the reason to the panel, and clears it once a copy reads.
    @MainActor
    func test_the_store_holds_the_reason_for_the_panel() throws {
        let broken = ServerListenerSettingsStore(
            loadAllowlist: { ServerSettingsAllowlistLoader.load(bundled: nil, checkout: self.missing) },
            controller: OpsController(runner: UnusedRunner(), makeBuilder: { OpsCommandBuilder(repoRoot: self.directory) }, auditURL: nil, checksInstall: false)
        )
        XCTAssertNil(broken.allowlist)
        XCTAssertTrue(broken.allowlistProblem?.contains("In the app: not bundled") == true)
        let bundled = try write(Self.valid, named: "bundled.json")
        let fine = ServerListenerSettingsStore(
            loadAllowlist: { ServerSettingsAllowlistLoader.load(bundled: bundled, checkout: nil) },
            controller: OpsController(runner: UnusedRunner(), makeBuilder: { OpsCommandBuilder(repoRoot: self.directory) }, auditURL: nil, checksInstall: false)
        )
        XCTAssertNotNil(fine.allowlist)
        XCTAssertNil(fine.allowlistProblem)
    }

    /// The copy the app would carry is this checkout's own file, so it must read.  A change to
    /// the file that Muster cannot read fails here, before a build ships without it.
    func test_the_checkouts_real_allowlist_reads_as_the_bundled_copy_would() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let real = ServerSettingsAllowlist.url(repoRoot: root)
        let load = ServerSettingsAllowlistLoader.load(bundled: real, checkout: nil)
        XCTAssertEqual(load.source, .bundled)
        XCTAssertNotNil(load.allowlist?.seat["enabled"])
        XCTAssertNotNil(load.allowlist?.seat["wake"])
        XCTAssertEqual(ServerSettingsAllowlistLoader.resourceName + "." + ServerSettingsAllowlistLoader.resourceExtension,
                       real.lastPathComponent)
    }
}

private struct UnusedRunner: OpsRunning {
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        XCTFail("no process should run")
        return OpsProcessResult(exitCode: 1, stdout: "", stderr: "")
    }
}
