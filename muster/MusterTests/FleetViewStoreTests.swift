import XCTest

/// The Apps & Work store:  one reading for the whole app,  one read of a checkout in flight,
/// and the page's own fail-closed rules.
@MainActor
final class FleetViewStoreTests: XCTestCase {
    private static let envelopeJSON = """
    {
      "schemaVersion": 1, "tool": "fleet-view", "command": "status",
      "host": "mac", "claimExpiryHours": 72, "durationSeconds": 6.6,
      "generatedAt": "2026-11-05T22:00:00Z",
      "summary": { "lanes": 0, "pullRequests": 0, "claims": 0, "overallVerdict": "PASS" },
      "requestCounts": { "github": 17, "linear": 1, "githubMax": 60, "linearMax": 30 },
      "sections": {
        "lanes": { "state": "OK", "reason": "0 lanes", "items": [], "unread": [] },
        "pullRequests": { "state": "OK", "reason": "0 open PRs", "items": [], "unread": [] },
        "claims": { "state": "OK", "reason": "0 claims", "items": [], "unread": [] }
      },
      "notes": []
    }
    """

    private func envelope() throws -> FleetViewEnvelope {
        try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(Self.envelopeJSON.utf8))
    }

    /// A store whose loader the test drives.
    private func makeStore(
        script: FleetViewLoadScript,
        root: URL? = URL(fileURLWithPath: "/tmp/afc"),
        clock: FleetViewStoreClock = FleetViewStoreClock()
    ) -> FleetViewStore {
        let loader = FleetViewStore.Loader(
            loadView: { try await script.load() },
            readTools: { FleetToolsReading() },
            readRegistry: { checkout, _, _ in
                script.recordRegistry(root: checkout)
                return FleetRegistrySummary(zulipChannel: "from-the-registry")
            },
            locateRoot: { root }
        )
        return FleetViewStore(loader: loader, now: { clock.now })
    }

    /// A good read fills the page,  and the registry rows are built from the checkout.
    func test_a_good_read_fills_the_page() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        let store = makeStore(script: script)

        await store.refresh()

        XCTAssertNotNil(store.envelope)
        XCTAssertNil(store.failure)
        XCTAssertFalse(store.loading)
        XCTAssertEqual(store.registrySummary.zulipChannel, "from-the-registry")
        XCTAssertNotNil(store.lastSuccessAt)
        XCTAssertNotNil(store.refreshClock.headerText)
        XCTAssertEqual(script.registryRoots, [URL(fileURLWithPath: "/tmp/afc")])
    }

    /// Two refreshes asked for at once are one read.
    func test_a_second_refresh_waits_for_the_read_in_flight() async throws {
        let script = FleetViewLoadScript(envelope: try envelope(), delay: 0.1)
        let store = makeStore(script: script)

        let first = Task { await store.refresh() }
        let second = Task { await store.refresh() }
        await first.value
        await second.value

        XCTAssertEqual(script.loads, 1, "a refresh during a read started another read")
        XCTAssertEqual(store.readsStarted, 1)
        XCTAssertNotNil(store.envelope)
        XCTAssertFalse(store.loading)
    }

    /// A refresh of another checkout does not join the read in flight,  whose registry rows
    /// are the first checkout's:  it starts its own read,  and the older read,  landing last,
    /// cannot write over the newer checkout's rows.
    func test_a_refresh_of_another_checkout_starts_its_own_read_and_wins() async throws {
        let script = FleetViewLoadScript(envelope: try envelope(), delay: 0.1)
        let older = URL(fileURLWithPath: "/tmp/older-checkout")
        let newer = URL(fileURLWithPath: "/tmp/newer-checkout")
        let loader = FleetViewStore.Loader(
            loadView: { try await script.load() },
            readTools: { FleetToolsReading() },
            readRegistry: { checkout, _, _ in
                script.recordRegistry(root: checkout)
                // The older checkout's rows take longer,  so its read lands after the newer one.
                if checkout == older {
                    Thread.sleep(forTimeInterval: 0.3)
                }
                return FleetRegistrySummary(zulipChannel: checkout.lastPathComponent)
            },
            locateRoot: { nil }
        )
        let store = FleetViewStore(loader: loader)

        let first = Task { await store.refresh(engineRoot: older) }
        while store.readsStarted < 1 {
            await Task.yield()
        }
        let second = Task { await store.refresh(engineRoot: newer) }
        await second.value
        XCTAssertEqual(store.registrySummary.zulipChannel, "newer-checkout")
        await first.value

        XCTAssertEqual(script.loads, 2, "a refresh of another checkout joined the read in flight")
        XCTAssertEqual(store.readsStarted, 2)
        XCTAssertEqual(Set(script.registryRoots), [older, newer])
        XCTAssertEqual(
            store.registrySummary.zulipChannel,
            "newer-checkout",
            "the older checkout's read wrote over the newer one's rows"
        )
        XCTAssertNotNil(store.envelope)
        XCTAssertFalse(store.loading)
    }

    /// A refresh that names the same checkout as the read in flight still joins it.
    func test_a_refresh_of_the_same_checkout_joins_the_read_in_flight() async throws {
        let script = FleetViewLoadScript(envelope: try envelope(), delay: 0.1)
        let store = makeStore(script: script)
        let named = URL(fileURLWithPath: "/tmp/named")

        let first = Task { await store.refresh(engineRoot: named) }
        let second = Task { await store.refresh(engineRoot: named) }
        await first.value
        await second.value

        XCTAssertEqual(script.loads, 1)
        XCTAssertEqual(store.readsStarted, 1)
        XCTAssertEqual(script.registryRoots, [named])
    }

    /// A failed read fails closed:  the lists clear and the failure is set,  so the last green
    /// report never stays on screen as if it were fresh.
    func test_a_failed_read_clears_the_page() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        let store = makeStore(script: script)
        await store.refresh()
        XCTAssertNotNil(store.envelope)

        script.fails = true
        await store.refresh()

        XCTAssertNil(store.envelope)
        XCTAssertNotNil(store.failure)
        XCTAssertTrue(store.failed)
        XCTAssertEqual(store.registrySummary, FleetRegistrySummary())
        XCTAssertNil(store.lastSuccessAt)
        XCTAssertTrue(store.refreshClock.headerText?.hasPrefix("Last attempt") ?? false)
    }

    /// Without a checkout the registry says so,  and lists no apps from an older run.
    func test_a_read_without_a_checkout_says_so() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        let store = makeStore(script: script, root: nil)

        await store.refresh()

        XCTAssertNotNil(store.envelope)
        XCTAssertEqual(store.registrySummary.problem, FleetViewStore.missingCheckoutProblem)
        XCTAssertTrue(store.registrySummary.apps.isEmpty)
        XCTAssertTrue(script.registryRoots.isEmpty)
    }

    /// The checkout the caller names wins over the one the loader would look for.
    func test_the_named_checkout_is_used() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        let store = makeStore(script: script)
        let named = URL(fileURLWithPath: "/tmp/named")

        await store.refresh(engineRoot: named)

        XCTAssertEqual(script.registryRoots, [named])
    }

    /// A return to the page inside the window shows the reading on hand,  and reads again once
    /// the window has passed.
    func test_a_return_inside_the_window_does_not_read_again() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        let clock = FleetViewStoreClock()
        let store = makeStore(script: script, clock: clock)
        await store.refresh()
        XCTAssertEqual(script.loads, 1)

        clock.advance(by: 10)
        await store.refreshIfOlder(than: 60)
        XCTAssertEqual(script.loads, 1, "a return inside the window read again")

        clock.advance(by: 51)
        await store.refreshIfOlder(than: 60)
        XCTAssertEqual(script.loads, 2, "a return after the window did not read")
    }

    /// A return to a page that failed reads again:  a failure is nothing worth keeping.  But
    /// not at once:  while fleet-view keeps failing,  returns inside `failureRetryAfter` do not
    /// rerun its reads,  and the Refresh button (a plain refresh) is never held back.
    func test_a_return_after_a_failure_reads_again() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        script.fails = true
        let clock = FleetViewStoreClock()
        let store = makeStore(script: script, clock: clock)
        await store.refresh()
        XCTAssertTrue(store.failed)
        XCTAssertNotNil(store.lastFailureAt)

        clock.advance(by: 1)
        await store.refreshIfOlder(than: 600)
        XCTAssertEqual(script.loads, 1, "a return right after a failure read again")

        await store.refresh()
        XCTAssertEqual(script.loads, 2, "the Refresh button was held back")

        clock.advance(by: FleetViewStore.failureRetryAfter + 1)
        script.fails = false
        await store.refreshIfOlder(than: 600)
        XCTAssertEqual(script.loads, 3)
        XCTAssertFalse(store.failed)
        XCTAssertNil(store.lastFailureAt)
        XCTAssertNotNil(store.envelope)
    }

    /// A window shorter than `failureRetryAfter` is the shorter gap after a failure too.
    func test_a_short_window_retries_a_failure_after_that_window() async throws {
        let script = FleetViewLoadScript(envelope: try envelope())
        script.fails = true
        let clock = FleetViewStoreClock()
        let store = makeStore(script: script, clock: clock)
        await store.refresh()
        clock.advance(by: 11)
        await store.refreshIfOlder(than: 10)
        XCTAssertEqual(script.loads, 2)
    }
}

/// What the store's loader does,  set by the test.
private final class FleetViewLoadScript: @unchecked Sendable {
    private let lock = NSLock()
    private let envelope: FleetViewEnvelope
    private let delay: TimeInterval
    private var loadCount = 0
    private var shouldFail = false
    private var roots: [URL] = []

    init(envelope: FleetViewEnvelope, delay: TimeInterval = 0) {
        self.envelope = envelope
        self.delay = delay
    }

    var fails: Bool {
        get { lock.lock(); defer { lock.unlock() }; return shouldFail }
        set { lock.lock(); shouldFail = newValue; lock.unlock() }
    }

    var loads: Int {
        lock.lock()
        defer { lock.unlock() }
        return loadCount
    }

    var registryRoots: [URL] {
        lock.lock()
        defer { lock.unlock() }
        return roots
    }

    func load() async throws -> FleetViewEnvelope {
        lock.lock()
        loadCount += 1
        let failing = shouldFail
        lock.unlock()
        if delay > 0 {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
        }
        if failing { throw FleetViewService.ServiceError.timedOut }
        return envelope
    }

    func recordRegistry(root: URL) {
        lock.lock()
        roots.append(root)
        lock.unlock()
    }
}

/// A clock the test moves by hand.
private final class FleetViewStoreClock {
    private(set) var now = Date(timeIntervalSince1970: 1_800_000_000)

    func advance(by seconds: TimeInterval) {
        now = now.addingTimeInterval(seconds)
    }
}
