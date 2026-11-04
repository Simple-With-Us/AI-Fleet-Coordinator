import XCTest

/// The Apps & Work failure path (AFC-616):  the words a failed refresh shows, the header's
/// times, and the real collector run through the app's own fetch and decoder.
final class FleetViewCollectorTests: XCTestCase {
    private typealias ServiceError = FleetViewService.ServiceError

    private static let allErrors: [ServiceError] = [
        .scriptsNotFound,
        .cliFailed("Traceback (most recent call last):\nImportError: boom\n"),
        .unparseable,
        .unknownSchema(9),
        .timedOut,
        .outputTooLarge,
        .launchFailed("The file does not exist."),
    ]

    private var scriptsDirectory: URL {
        // .../muster/MusterTests/FleetViewCollectorTests.swift -> repo root is three levels up
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("scripts", isDirectory: true)
    }

    // MARK: The reason

    func test_every_failure_has_a_reason_with_a_next_step_in_the_owners_format() {
        for error in Self.allErrors {
            let reason = FleetViewFailure(error: error).reason
            XCTAssertFalse(reason.isEmpty, "\(error)")
            XCTAssertFalse(reason.contains("&nbsp;"), reason)
            XCTAssertFalse(reason.contains(".  "), "two plain spaces in:  \(reason)")
            XCTAssertTrue(reason.contains(".\u{00A0} "), "no sentence gap, so no next step:  \(reason)")
            XCTAssertEqual(error.errorDescription, reason)
        }
    }

    func test_each_failure_says_something_different() {
        let reasons = Self.allErrors.map { FleetViewFailure(error: $0).reason }
        XCTAssertEqual(Set(reasons).count, reasons.count)
    }

    func test_the_timeout_reason_names_the_real_deadline() {
        XCTAssertTrue(FleetViewFailure(error: ServiceError.timedOut).reason.contains("within 60 seconds"))
    }

    func test_the_unknown_format_is_named() {
        XCTAssertTrue(FleetViewFailure(error: ServiceError.unknownSchema(9)).reason.contains("format 9"))
    }

    func test_only_a_command_that_spoke_has_a_detail() {
        XCTAssertNil(FleetViewFailure(error: ServiceError.timedOut).detail)
        XCTAssertNil(FleetViewFailure(error: ServiceError.scriptsNotFound).fullDetail)
        XCTAssertEqual(
            FleetViewFailure(error: ServiceError.launchFailed("The file does not exist.")).detail,
            "The file does not exist."
        )
    }

    func test_an_unexpected_error_still_gives_a_reason_and_its_own_words() {
        struct Odd: LocalizedError {
            var errorDescription: String? { "something odd" }
        }
        let failure = FleetViewFailure(error: Odd())
        XCTAssertTrue(failure.reason.contains("does not recognize"))
        XCTAssertEqual(failure.detail, "something odd")
    }

    func test_the_detail_is_the_last_line_and_copy_carries_the_tail() {
        let stderr = """
        Traceback (most recent call last):
          File "/x/fleet_view/__main__.py", line 25, in <module>

        ImportError: cannot import name 'UTC' from 'datetime' (/usr/lib/python3.9/datetime.py)

        """
        let failure = FleetViewFailure(error: ServiceError.cliFailed(stderr))
        XCTAssertEqual(failure.detail, "ImportError: cannot import name 'UTC' from 'datetime' (/usr/lib/python3.9/datetime.py)")
        XCTAssertTrue(failure.copyText.hasPrefix(failure.reason))
        XCTAssertTrue(failure.copyText.contains("Traceback (most recent call last):"))
        XCTAssertTrue(failure.copyText.hasSuffix("/usr/lib/python3.9/datetime.py)"))
    }

    func test_a_flood_is_bounded() {
        let noise = String(repeating: "x y ", count: 5000)
        let lots = (0..<500).map { "line \($0)" }.joined(separator: "\n") + "\n" + noise
        let failure = FleetViewFailure(error: ServiceError.cliFailed(lots))
        XCTAssertEqual(failure.detail?.count, FleetViewFailure.maximumLineCharacters + 1)
        XCTAssertEqual(failure.fullDetail?.split(separator: "\n").count, FleetViewFailure.copiedLineCount)
    }

    func test_a_real_failure_reaches_the_banner_through_fetch() throws {
        let directory = try makeTemporaryDirectory()
        let script = directory.appendingPathComponent("python3")
        try """
        #!/bin/sh
        echo "Traceback (most recent call last):" >&2
        echo "ImportError: cannot import name 'UTC' from 'datetime'" >&2
        exit 1
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)

        XCTAssertThrowsError(try FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil, environment: [:], timeout: 10
        )) { error in
            let failure = FleetViewFailure(error: error)
            XCTAssertEqual(failure.detail, "ImportError: cannot import name 'UTC' from 'datetime'")
            XCTAssertTrue(failure.reason.contains("stopped with an error"))
        }
    }

    // MARK: Scrubbing

    func test_credentials_never_reach_the_banner() {
        // Fake values, assembled here so the source carries nothing credential shaped.
        let fakeGitHubToken = "ghp_" + String(repeating: "a1", count: 18)
        let fakeGitHubPat = "github_pat_" + String(repeating: "b2", count: 11)
        let cases: [(input: String, leaked: String)] = [
            ("fatal: could not read from https://x-access-token:abc123secret@github.com/a/b.git", "abc123secret"),
            ("fatal: bad url https://abc123secretvalue@github.com/a/b", "abc123secretvalue"),
            ("GET https://api.example.com/v1?access_token=abc123secret&page=1 failed", "abc123secret"),
            ("Authorization: Bearer abc123secret", "abc123secret"),
            ("authorization: abc123secret", "abc123secret"),
            ("sent 'x-api-key: abc123secret' and failed", "abc123secret"),
            ("password: hunter2hunter2", "hunter2hunter2"),
            ("run --token abc123secret now", "abc123secret"),
            ("run --token=abc123secret now", "abc123secret"),
            ("TOKEN=abc123secret python3", "abc123secret"),
            ("OPENAI_API_KEY=abc123secret python3", "abc123secret"),
            (#"{"token":"abc123secret"}"#, "abc123secret"),
            ("gh said \(fakeGitHubToken)", fakeGitHubToken),
            ("value='\(fakeGitHubPat)'", fakeGitHubPat),
            ("service st.abc123.def456.ghi789 refused", "st.abc123.def456.ghi789"),
            ("opaque a1b2c3d4e5f6a7b8c9d0e1f2a3b4 here", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4"),
        ]
        for (input, leaked) in cases {
            let scrubbed = FleetViewFailure.scrub(input)
            XCTAssertFalse(scrubbed.contains(leaked), "leaked from \(input):  \(scrubbed)")
            XCTAssertTrue(scrubbed.contains(ConfigFileReader.redactionMark), input)
        }
    }

    func test_the_detail_is_scrubbed_on_screen_and_on_the_clipboard() {
        let failure = FleetViewFailure(error: ServiceError.cliFailed("""
        TOKEN=abc123secret ran
        fatal: https://x:abc123secret@github.com/a/b
        """))
        XCTAssertFalse(failure.detail?.contains("abc123secret") ?? true)
        XCTAssertFalse(failure.copyText.contains("abc123secret"))
    }

    func test_ordinary_failure_lines_survive_scrubbing() {
        for line in [
            "ImportError: cannot import name 'UTC' from 'datetime' (/usr/lib/python3.9/datetime.py)",
            "subprocess.CalledProcessError: Command returned non-zero exit status 1.",
            "fleet-view: registry unreadable: [Errno 2] No such file or directory: '/Users/jay/Code/fleet-apps.json'",
            "fatal: unable to access 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git/'",
            "exit 1",
            "a token was rejected",
        ] {
            XCTAssertEqual(FleetViewFailure.scrub(line), line)
        }
    }

    // MARK: The header's times

    func test_a_success_shows_the_collectors_stamp_and_not_the_clock() throws {
        let generated = "2026-10-25T16:00:00Z"
        let stamp = try XCTUnwrap(FleetTime.parse(generated))
        var clock = FleetViewRefreshClock()
        XCTAssertNil(clock.headerText)
        clock.recordSuccess(generatedAt: generated, at: stamp.addingTimeInterval(3600))
        XCTAssertEqual(clock.headerText, "Collected \(FleetTime.clock(stamp))")
    }

    func test_a_failure_never_says_refreshed_or_collected_at_the_failure_time() throws {
        let failedAt = try XCTUnwrap(FleetTime.parse("2026-10-25T18:15:00Z"))
        var clock = FleetViewRefreshClock()
        clock.recordFailure(at: failedAt)
        XCTAssertEqual(clock.headerText, "Last attempt \(FleetTime.clock(failedAt)) failed")
    }

    func test_a_failure_after_a_success_keeps_both_times_apart() throws {
        let generated = "2026-10-25T16:00:00Z"
        let stamp = try XCTUnwrap(FleetTime.parse(generated))
        let failedAt = stamp.addingTimeInterval(7200)
        var clock = FleetViewRefreshClock()
        clock.recordSuccess(generatedAt: generated, at: stamp)
        clock.recordFailure(at: failedAt)
        XCTAssertEqual(
            clock.headerText,
            "Last attempt \(FleetTime.clock(failedAt)) failed · Collected \(FleetTime.clock(stamp))"
        )
        XCTAssertEqual(clock.collectedAt, stamp)
        XCTAssertEqual(clock.lastAttemptAt, failedAt)

        clock.recordSuccess(generatedAt: generated, at: failedAt.addingTimeInterval(60))
        XCTAssertFalse(clock.lastAttemptFailed)
        XCTAssertEqual(clock.headerText, "Collected \(FleetTime.clock(stamp))")
    }

    func test_an_unreadable_stamp_reads_unknown_and_not_now() {
        var clock = FleetViewRefreshClock()
        clock.recordSuccess(generatedAt: "not a time")
        XCTAssertEqual(clock.headerText, "Collection time unknown")
    }

    /// The view is not in the test bundle, so its use of these types is pinned by its source.
    func test_the_view_keeps_the_reason_and_the_times_apart() throws {
        let view = scriptsDirectory.deletingLastPathComponent()
            .appendingPathComponent("muster/Muster/FleetView.swift")
        let source = try String(contentsOf: view, encoding: .utf8)
        XCTAssertTrue(source.contains("failure = FleetViewFailure(error: error)"))
        XCTAssertTrue(source.contains("refreshClock.recordFailure()"))
        XCTAssertTrue(source.contains("refreshClock.recordSuccess(generatedAt: loaded.generatedAt)"))
        XCTAssertFalse(source.contains("refreshedAt"))
        XCTAssertFalse(source.contains("\"Refreshed "))
    }

    // MARK: The real collector, under the interpreter the app runs

    private func makeTemporaryDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-collector-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return directory
    }

    private func writeStub(_ name: String, in directory: URL, printing json: String) throws {
        let stub = directory.appendingPathComponent(name)
        try "#!/bin/sh\ncat <<'JSON'\n\(json)\nJSON\n".write(to: stub, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: stub.path)
    }

    /// Runs the same command as `FleetViewService.load()`, with a temp lanes root and the
    /// given stub folder first on `PATH`, and decodes through `fetch`, as the app does.
    private func runRealCollector(home: URL, bin: URL) throws -> FleetViewEnvelope {
        try XCTSkipUnless(
            FileManager.default.isExecutableFile(atPath: FleetViewService.interpreterPath),
            "\(FleetViewService.interpreterPath) is not on this machine"
        )
        let lanes = home.appendingPathComponent("lanes/AI-Fleet-Coordinator/claude-smoke", isDirectory: true)
        try FileManager.default.createDirectory(at: lanes, withIntermediateDirectories: true)
        return try FleetViewService.fetch(
            executable: URL(fileURLWithPath: FleetViewService.interpreterPath),
            arguments: [
                "-m", "fleet_view",
                "--lanes-root", home.appendingPathComponent("lanes").path,
                "status", "--json",
            ],
            currentDirectory: scriptsDirectory,
            environment: ["HOME": home.path, "PATH": "\(bin.path):/usr/bin:/bin"],
            timeout: 120
        )
    }

    func test_the_real_collector_decodes_under_the_apps_python() throws {
        let home = try makeTemporaryDirectory()
        let bin = home.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        try writeStub("gh", in: bin, printing: "[]")
        try writeStub("simple-board", in: bin, printing: #"""
        [{"identifier": "AFC-616", "title": "Muster", "labels": ["claimed:CLAUDE"], "updated_at": "2026-10-20T12:00:00.123Z"}]
        """#)

        let envelope = try runRealCollector(home: home, bin: bin)

        XCTAssertEqual(envelope.schemaVersion, 1)
        XCTAssertNotNil(FleetTime.parse(envelope.generatedAt), envelope.generatedAt)
        XCTAssertEqual(envelope.summary.overallVerdict, "PASS")
        XCTAssertTrue(envelope.sections.lanes.isKnown)
        XCTAssertTrue(envelope.sections.pullRequests.isKnown)
        XCTAssertTrue(envelope.sections.claims.isKnown)
        XCTAssertEqual(envelope.sections.lanes.items.first?.seat, "CLAUDE")
        XCTAssertEqual(envelope.sections.claims.items.first?.identifier, "AFC-616")
        XCTAssertGreaterThan(envelope.sections.claims.items.first?.ageSeconds ?? -1, 0)
    }

    func test_the_real_collector_reads_unknown_when_its_tools_are_missing() throws {
        let home = try makeTemporaryDirectory()
        let bin = home.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)

        let envelope = try runRealCollector(home: home, bin: bin)

        XCTAssertEqual(envelope.summary.overallVerdict, "UNKNOWN")
        XCTAssertFalse(envelope.sections.pullRequests.isKnown)
        XCTAssertFalse(envelope.sections.claims.isKnown)
    }
}
