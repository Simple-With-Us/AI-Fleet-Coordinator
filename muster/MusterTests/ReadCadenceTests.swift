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
        cache.record(
            loaded(), seat: "CLAUDE", host: .mac, platform: "claude", runStamp: runA,
            startedAt: start.addingTimeInterval(-2), at: start
        )
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.readAt, start)
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.runStamp, runA)
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .mac)?.reading, loaded())
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))
        XCTAssertNil(cache.entry(seat: "CODEX", host: .mac))
    }

    func test_a_failed_read_is_not_kept_and_drops_the_reading_before_it() {
        var cache = SeatInventoryCache()
        cache.record(
            .unavailable("timed out"), seat: "CLAUDE", host: .server, platform: "claude", runStamp: runA,
            startedAt: start.addingTimeInterval(-20), at: start
        )
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))

        cache.record(
            loaded(), seat: "CLAUDE", host: .server, platform: "claude", runStamp: runA,
            startedAt: start.addingTimeInterval(-2), at: start
        )
        cache.record(
            .unavailable("The server did not answer in time."), seat: "CLAUDE", host: .server,
            platform: "claude", runStamp: runA, startedAt: start.addingTimeInterval(10), at: start.addingTimeInterval(30)
        )
        XCTAssertNil(cache.entry(seat: "CLAUDE", host: .server))
        XCTAssertEqual(decide(cache.entry(seat: "CLAUDE", host: .server), host: .server, after: 31), .read)
    }

    /// Two reads of one seat and host overlap:  a slow failure that started before a success
    /// landed leaves the success in place.
    func test_a_slow_failure_never_drops_a_success_that_landed_while_it_ran() {
        var cache = SeatInventoryCache()
        // The failing read starts first, at `start`.
        cache.record(
            loaded(), seat: "CLAUDE", host: .server, platform: "claude", runStamp: runA,
            startedAt: start.addingTimeInterval(5), at: start.addingTimeInterval(8)
        )
        cache.record(
            .unavailable("The server did not answer in time."), seat: "CLAUDE", host: .server,
            platform: "claude", runStamp: runA, startedAt: start, at: start.addingTimeInterval(20)
        )
        XCTAssertEqual(cache.entry(seat: "CLAUDE", host: .server)?.readAt, start.addingTimeInterval(8))
    }

    /// A read that comes back after the owner switched seat or host is not drawn over the new
    /// one (it is still cached by `record`).
    func test_a_read_goes_on_screen_only_for_the_seat_and_host_shown() {
        let read = SeatInventoryCache.Key(seat: "CLAUDE", host: .server)
        XCTAssertTrue(SeatInventoryCache.belongsOnScreen(readFor: read, shown: read))
        XCTAssertFalse(SeatInventoryCache.belongsOnScreen(
            readFor: read, shown: SeatInventoryCache.Key(seat: "CLAUDE", host: .mac)))
        XCTAssertFalse(SeatInventoryCache.belongsOnScreen(
            readFor: read, shown: SeatInventoryCache.Key(seat: "CODEX", host: .server)))
        XCTAssertFalse(SeatInventoryCache.belongsOnScreen(readFor: read, shown: nil))
    }

    func test_the_read_line_gives_the_time_and_the_window() {
        let line = SeatInventoryCache.readLine(readAt: start)
        XCTAssertTrue(line.hasPrefix("Read at \(FleetTime.clock(start)).\u{00A0} "))
        XCTAssertTrue(line.contains("5 minutes"))
        XCTAssertFalse(line.contains("UTC"))
    }

    // MARK: - polling only while the app is in front

    private func polling(active: Bool = true, readAgo seconds: TimeInterval? = nil) -> ForegroundPolling {
        var polling = ForegroundPolling(interval: 30, active: active)
        if let seconds { polling.didRead(at: start.addingTimeInterval(-seconds)) }
        return polling
    }

    func test_a_page_that_has_not_read_reads_at_once() {
        XCTAssertEqual(polling().nextStep(now: start, busy: false), .read)
    }

    func test_in_front_it_waits_out_the_rest_of_the_interval_then_reads() {
        XCTAssertEqual(polling(readAgo: 10).nextStep(now: start, busy: false), .wait(20))
        XCTAssertEqual(polling(readAgo: 30).nextStep(now: start, busy: false), .read)
        XCTAssertEqual(polling(readAgo: 95).nextStep(now: start, busy: false), .read)
    }

    func test_a_wait_is_never_shorter_than_a_second() {
        XCTAssertEqual(polling(readAgo: 29.9).nextStep(now: start, busy: false), .wait(1))
    }

    func test_a_busy_page_waits_a_whole_interval_instead_of_reading() {
        XCTAssertEqual(polling().nextStep(now: start, busy: true), .wait(30))
        XCTAssertEqual(polling(readAgo: 40).nextStep(now: start, busy: true), .wait(30))
    }

    func test_a_clock_that_went_back_reads() {
        XCTAssertEqual(polling(readAgo: -60).nextStep(now: start, busy: false), .read)
    }

    /// In the background the timer stops outright:  no wake every 30 seconds.
    func test_in_the_background_the_timer_stops() {
        XCTAssertEqual(polling(active: false).nextStep(now: start, busy: false), .stop)
        XCTAssertEqual(polling(active: false, readAgo: 600).nextStep(now: start, busy: false), .stop)
        var moved = polling(readAgo: 5)
        moved.setActive(false)
        XCTAssertEqual(moved.nextStep(now: start, busy: false), .stop)
    }

    /// Coming back reads once when a read came due while away, and otherwise waits.
    func test_coming_back_reads_once_when_a_read_is_due() {
        var away = polling(active: false, readAgo: 120)
        away.setActive(true)
        XCTAssertEqual(away.nextStep(now: start, busy: false), .read)
        away.didRead(at: start)
        XCTAssertEqual(away.nextStep(now: start.addingTimeInterval(1), busy: false), .wait(29))

        var brief = polling(active: false, readAgo: 5)
        brief.setActive(true)
        XCTAssertEqual(brief.nextStep(now: start, busy: false), .wait(25))
    }

    /// An action finished in front reads now;  one finished while away is read on coming back.
    func test_an_action_finishing_away_is_read_on_coming_back() {
        var front = polling(readAgo: 5)
        XCTAssertTrue(front.actionFinished())

        var away = polling(active: false, readAgo: 5)
        XCTAssertFalse(away.actionFinished())
        XCTAssertEqual(away.nextStep(now: start, busy: false), .stop)
        away.setActive(true)
        XCTAssertEqual(away.nextStep(now: start, busy: false), .read)
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
        // A read that came back for another seat or host is dropped before it is drawn.
        XCTAssertTrue(view.contains("guard SeatInventoryCache.belongsOnScreen("))
    }

    /// Resource Limits' timer goes through ForegroundPolling, stops in the background, and an
    /// action finishing goes through the same gate.
    func test_resource_limits_polls_only_in_front() throws {
        let view = try source("Muster/Views/ResourceLimitsView.swift")
        XCTAssertTrue(view.contains("@Environment(\\.scenePhase)"))
        XCTAssertTrue(view.contains(".task(id: polling.active) { await keepReading() }"))
        XCTAssertTrue(view.contains("polling.nextStep(now: Date(), busy: controller.isBusy || loading)"))
        XCTAssertTrue(view.contains("polling.setActive(phase == .active)"))
        XCTAssertTrue(view.contains("guard polling.actionFinished() else { return }"))
        XCTAssertFalse(view.contains("if controller.isBusy { continue }"))
    }
}
