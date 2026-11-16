import XCTest

/// The menu's report files are read again only when they change,  and a file over the cap is
/// refused rather than read whole every minute.
final class ReportStampCacheTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-stamp-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// Reads the file through the cache,  counting the decodes.
    private func read(_ url: URL, cache: ReportStampCache<String>, maximumBytes: Int = 1024) -> String {
        cache.read(
            url: url,
            now: Date(),
            maximumBytes: maximumBytes,
            decode: { url, _ in (try? String(contentsOf: url, encoding: .utf8)) ?? "missing" },
            tooLarge: { _, size in "too large: \(size)" },
            reuse: { kept, _ in kept }
        )
    }

    /// An unchanged file is not decoded again.
    func test_an_unchanged_file_is_decoded_once() throws {
        let url = directory.appendingPathComponent("report.json")
        try "first".write(to: url, atomically: true, encoding: .utf8)
        let cache = ReportStampCache<String>()

        XCTAssertEqual(read(url, cache: cache), "first")
        XCTAssertEqual(read(url, cache: cache), "first")
        XCTAssertEqual(cache.decodes, 1)
    }

    /// A file that changed is decoded again.
    func test_a_changed_file_is_decoded_again() throws {
        let url = directory.appendingPathComponent("report.json")
        try "first".write(to: url, atomically: true, encoding: .utf8)
        let cache = ReportStampCache<String>()
        XCTAssertEqual(read(url, cache: cache), "first")

        try "second, longer".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertEqual(read(url, cache: cache), "second, longer")
        XCTAssertEqual(cache.decodes, 2)
    }

    /// A file over the cap is refused and never decoded.
    func test_a_file_over_the_cap_is_refused() throws {
        let url = directory.appendingPathComponent("report.json")
        try String(repeating: "x", count: 2048).write(to: url, atomically: true, encoding: .utf8)
        let cache = ReportStampCache<String>()

        XCTAssertEqual(read(url, cache: cache, maximumBytes: 1024), "too large: 2048")
        XCTAssertEqual(cache.decodes, 0)
    }

    /// A missing file goes to the reader,  which says so,  and nothing is kept.
    func test_a_missing_file_is_left_to_the_reader() {
        let url = directory.appendingPathComponent("absent.json")
        let cache = ReportStampCache<String>()

        XCTAssertEqual(read(url, cache: cache), "missing")
        XCTAssertEqual(read(url, cache: cache), "missing")
        XCTAssertEqual(cache.decodes, 0)
    }

    /// A kept reading the time has changed the verdict of is read again.
    func test_a_reading_the_reuse_turns_down_is_decoded_again() throws {
        let url = directory.appendingPathComponent("report.json")
        try "first".write(to: url, atomically: true, encoding: .utf8)
        let cache = ReportStampCache<String>()
        let readOnce = {
            cache.read(url: url, now: Date(), maximumBytes: 1024,
                       decode: { _, _ in "decoded" },
                       tooLarge: { _, _ in "too large" },
                       reuse: { _, _ in nil })
        }

        _ = readOnce()
        _ = readOnce()
        XCTAssertEqual(cache.decodes, 2)
    }

    // MARK: Bringing a kept reading up to the minute

    /// A kept tools reading gets a new age,  and the same verdict.
    func test_a_kept_tools_reading_gets_a_new_age() {
        let generated = Date(timeIntervalSince1970: 1_800_000_000)
        var reading = FleetToolsReading()
        reading.generatedAt = generated
        reading.isCurrent = true
        reading.age = FleetTime.ageText(since: generated, now: generated.addingTimeInterval(60))

        let later = generated.addingTimeInterval(300)
        let reused = PlaceWatchReports.reused(tools: reading, now: later)

        XCTAssertEqual(reused?.age, FleetTime.ageText(since: generated, now: later))
        XCTAssertEqual(reused?.isCurrent, true)
    }

    /// A kept tools reading that has gone out of its window is read again,  because its
    /// verdict is the reader's to make.
    func test_a_kept_tools_reading_out_of_its_window_is_read_again() {
        let generated = Date(timeIntervalSince1970: 1_800_000_000)
        var reading = FleetToolsReading()
        reading.generatedAt = generated
        reading.isCurrent = true

        let later = generated.addingTimeInterval(TimeInterval(FleetToolsContract.maxAgeSeconds + 1))
        XCTAssertNil(PlaceWatchReports.reused(tools: reading, now: later))
    }

    /// A kept server reading gets a new age while it stays in its window.
    func test_a_kept_server_reading_gets_a_new_age() {
        let generated = Date(timeIntervalSince1970: 1_800_000_000)
        var reading = ServerStatusReading()
        reading.generatedAt = generated
        reading.isCurrent = true

        let later = generated.addingTimeInterval(600)
        XCTAssertEqual(PlaceWatchReports.reused(status: reading, now: later)?.age,
                       FleetTime.ageText(since: generated, now: later))

        let stale = generated.addingTimeInterval(TimeInterval(ServerStatusContract.maxAgeSeconds + 1))
        XCTAssertNil(PlaceWatchReports.reused(status: reading, now: stale))
    }

    /// A server file that could not be opened or decoded is never kept,  so a fix that moves
    /// no modification time still clears it within a minute.  A refusal for the version is kept.
    func test_an_unreadable_server_reading_is_not_kept() {
        var unreadable = ServerStatusReading()
        unreadable.problem = "The report could not be read"
        unreadable.refusal = .unreadable
        XCTAssertNil(PlaceWatchReports.reused(status: unreadable, now: Date()))

        var refused = ServerStatusReading()
        refused.problem = "The report is version 9"
        refused.refusal = .appOutOfDate
        XCTAssertEqual(PlaceWatchReports.reused(status: refused, now: Date())?.refusal, .appOutOfDate)
    }

    /// The same for the tools report:  a problem with no refusal is read again,  and a refusal
    /// for the version is kept.
    func test_an_unreadable_tools_reading_is_not_kept() {
        var unreadable = FleetToolsReading()
        unreadable.problem = "The report could not be read"
        XCTAssertNil(PlaceWatchReports.reused(tools: unreadable, now: Date()))

        var refused = FleetToolsReading()
        refused.problem = "The report is version 9"
        refused.refusal = .appOutOfDate
        XCTAssertEqual(PlaceWatchReports.reused(tools: refused, now: Date())?.refusal, .appOutOfDate)
    }

    /// A server file that does not decode is decoded again on the next read,  not kept.
    func test_a_server_file_that_does_not_decode_is_read_again() throws {
        let url = directory.appendingPathComponent("server-status.json")
        try "not json".write(to: url, atomically: true, encoding: .utf8)
        let cache = ReportStampCache<ServerStatusReading>()

        _ = PlaceWatchReports.readStatus(at: url, cache: cache)
        let second = PlaceWatchReports.readStatus(at: url, cache: cache)

        XCTAssertEqual(second.refusal, .unreadable)
        XCTAssertEqual(cache.decodes, 2)
    }

    /// A server report over the cap is refused with a reason,  and reads as nothing current.
    func test_a_server_report_over_the_cap_is_refused() throws {
        let url = directory.appendingPathComponent("server-status.json")
        try String(repeating: "x", count: 4096).write(to: url, atomically: true, encoding: .utf8)

        let reading = PlaceWatchReports.readStatus(
            at: url, cache: ReportStampCache<ServerStatusReading>(), maximumBytes: 1024
        )

        XCTAssertEqual(reading.refusal, .unreadable)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertTrue(reading.problem?.contains("more than") ?? false)
    }
}
