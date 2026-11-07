import XCTest

/// When Seat Setup reads a platform's inventory again, and when a polling page reads while the
/// app is not in front (ReadCadence.swift).
final class ReadCadenceTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_800_000_000)
    private let runA = Date(timeIntervalSince1970: 1_799_999_000)
    private let runB = Date(timeIntervalSince1970: 1_800_000_100)

    private func loaded(_ platform: String = "claude") -> SeatInventoryReading {
        .loaded(SeatInventory(
            schemaVersion: 1, tool: "fleet-seat-inventory", ok: true, host: "mac", platform: platform,
            generatedAt: "2026-11-07T15:00:00Z", sections: [], detail: ""
        ))
    }

    private func entry(
        platform: String = "claude", readAt: Date? = nil, runStamp: Date? = nil
    ) -> SeatInventoryCache.Entry {
        SeatInventoryCache.Entry(
            platform: platform, reading: loaded(platform), readAt: readAt ?? start, runStamp: runStamp ?? runA
        )
    }

    private func decide(
        _ held: SeatInventoryCache.Entry?,
        host: SeatSetupHost = .mac,
        platform: String = "claude",
        runStamp: Date? = nil,
        after seconds: TimeInterval = 60,
        force: Bool = false
    ) -> SeatInventoryCache.Decision {
        SeatInventoryCache.decide(
            held: held, host: host, platform: platform, runStamp: runStamp ?? runA,
            now: start.addingTimeInterval(seconds), force: force
        )
    }

    // MARK: - the inventory cache

    func test_the_reading_is_held_for_five_minutes() {
        XCTAssertEqual(SeatInventoryCache.freshSeconds, 300)
    }

    func test_nothing_held_reads() {
        XCTAssertEqual(decide(nil), .read)
    }

    func test_switching_back_within_the_window_shows_the_held_reading() {
        let held = entry()
        XCTAssertEqual(decide(held, after: 60), .useHeld(held))
        XCTAssertEqual(decide(held, after: 299), .useHeld(held))
        XCTAssertEqual(decide(held, host: .server, after: 299), .useHeld(held))
    }

    func test_a_reading_as_old_as_the_window_reads_again() {
        XCTAssertEqual(decide(entry(), after: 300), .read)
        XCTAssertEqual(decide(entry(), host: .server, after: 3600), .read)
    }

    func test_a_clock_that_went_back_reads_again() {
        XCTAssertEqual(decide(entry(), after: -5), .read)
    }

    func test_the_owner_asking_always_reads() {
        XCTAssertEqual(decide(entry(), after: 1, force: true), .read)
        XCTAssertEqual(decide(entry(), host: .server, after: 1, force: true), .read)
    }

    func test_another_platform_reads_again() {
        XCTAssertEqual(decide(entry(platform: "claude"), platform: "codex"), .read)
    }

    /// The fix for the double read:  one engine run is one stamp, so the same run never
    /// reads the Mac's inventory twice, and a new run on the Mac reads it once.
    func test_a_new_engine_run_reads_the_mac_once_and_the_same_run_never_twice() {
        let held = entry(runStamp: runA)
        XCTAssertEqual(decide(held, host: .mac, runStamp: runA), .useHeld(held))
        XCTAssertEqual(decide(held, host: .mac, runStamp: runB), .read)
    }

    /// The engine checks the Mac only, so its runs never make the server's reading stale.
    func test_a_new_engine_run_does_not_read_the_server_again() {
        let held = entry(runStamp: runA)
        XCTAssertEqual(decide(held, host: .server, runStamp: runB), .useHeld(held))
    }

    func test_a_reading_that_succeeded_is_kept_per_seat_and_host() {
        var cache = SeatInventoryCache()
        cache.record(loaded(), seat: "CLAUDE", host: .mac, platform: "claude", runStamp: runA, at: start)
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.readAt, start)
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.runStamp, runA)
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.reading, loaded())
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))
        XCTAssertNil(cache.entry(seat: "CODEX", host: .mac))
    }

    func test_a_failed_read_is_not_kept_and_drops_the_reading_before_it() {
        var cache = SeatInventoryCache()
        cache.record(.unavailable("timed out"), seat: "CLAUDE", host: .server, platform: "claude", runStamp: runA, at: start)
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))

        cache.record(loaded(), seat: "CLAUDE", host: .server, platform: "claude", runStamp: runA, at: start)
        cache.record(
            .unavailable("The server did not answer in time."), seat: "CLAUDE", host: .server,
            platform: "claude", runStamp: runA, at: start.addingTimeInterval(30)
        )
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))
        XCTAssertEqual(decide(cache.entry(seat: "CLAUDE", host: .server), host: .server, after: 31), .read)
    }

    func test_the_read_line_gives_the_time_and_the_window() {
        let line = SeatInventoryCache.readLine(readAt: start)
        XCTAssertTrue(line.hasPrefix("Read at \(FleetTime.clock(start)).\u{00A0} "))
        XCTAssertTrue(line.contains("5 minutes"))
        XCTAssertFalse(line.contains("UTC"))
    }

    // MARK: - polling only while the app is in front

    func test_a_tick_in_front_reads_unless_busy() {
        var polling = ForegroundPolling()
        XCTAssertTrue(polling.tick(busy: false))
        XCTAssertFalse(polling.tick(busy: true))
        XCTAssertFalse(polling.owed)
    }

    func test_a_tick_in_the_background_reads_nothing_and_is_owed() {
        var polling = ForegroundPolling()
        XCTAssertFalse(polling.phaseChanged(active: false))
        XCTAssertFalse(polling.tick(busy: false))
        XCTAssertFalse(polling.tick(busy: false))
        XCTAssertTrue(polling.owed)
    }

    func test_coming_back_after_a_skipped_tick_reads_once() {
        var polling = ForegroundPolling()
        _ = polling.phaseChanged(active: false)
        _ = polling.tick(busy: false)
        XCTAssertTrue(polling.phaseChanged(active: true))
        XCTAssertFalse(polling.owed)
        // Another change that ends in front owes nothing.
        XCTAssertFalse(polling.phaseChanged(active: true))
        _ = polling.phaseChanged(active: false)
        XCTAssertFalse(polling.phaseChanged(active: true))
    }

    func test_coming_back_with_nothing_skipped_reads_nothing() {
        var polling = ForegroundPolling()
        _ = polling.phaseChanged(active: false)
        XCTAssertFalse(polling.phaseChanged(active: true))
    }

    func test_a_read_some_other_way_clears_what_is_owed() {
        var polling = ForegroundPolling()
        _ = polling.phaseChanged(active: false)
        _ = polling.tick(busy: false)
        polling.didRead()
        XCTAssertFalse(polling.phaseChanged(active: true))
    }

    func test_a_page_opened_in_the_background_does_not_poll() {
        var polling = ForegroundPolling(active: false)
        XCTAssertFalse(polling.tick(busy: false))
        XCTAssertTrue(polling.phaseChanged(active: true))
    }

    // MARK: - the views use it

    private func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }

    /// Seat Setup's read is keyed on the engine run's stamp, not its phase, which moves twice a
    /// run, and the inventory goes through the cache.
    func test_seat_setup_keys_on_the_run_and_reads_through_the_cache() throws {
        let view = try source("Muster/Views/SeatSetupView.swift")
        XCTAssertFalse(view.contains("String(describing: store.phase)"))
        XCTAssertTrue(view.contains("store.generatedAt.map"))
        XCTAssertTrue(view.contains("SeatInventoryCache.decide("))
        XCTAssertTrue(view.contains("cacheStore.cache.record("))
    }

    /// Resource Limits' timer goes through ForegroundPolling and follows the scene phase.
    func test_resource_limits_polls_only_in_front() throws {
        let view = try source("Muster/Views/ResourceLimitsView.swift")
        XCTAssertTrue(view.contains("@Environment(\\.scenePhase)"))
        XCTAssertTrue(view.contains("guard polling.tick(busy: controller.isBusy) else { continue }"))
        XCTAssertTrue(view.contains("polling.phaseChanged(active: phase == .active)"))
        XCTAssertFalse(view.contains("if controller.isBusy { continue }"))
    }
}
