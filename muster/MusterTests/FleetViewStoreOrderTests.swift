import XCTest

/// The order Apps & Work relies on, now that it reads through `FleetViewStore`:  the rows are
/// rebuilt with the work read before the envelope is set (Sentry, PR 1450).  The page ends its
/// pending counts when the envelope arrives, so an envelope set before the rows would show the
/// counts that were not read, in red, for a moment.
@MainActor
final class FleetViewStoreOrderTests: XCTestCase {
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

    /// While the rows are being rebuilt with the work read, the envelope is still not on
    /// screen and the read still counts as running;  both land together when the rows are done.
    func test_the_envelope_waits_for_the_rebuilt_rows() async throws {
        let envelope = try JSONDecoder().decode(FleetViewEnvelope.self, from: Data(Self.envelopeJSON.utf8))
        let gate = RebuildGate()
        let loader = FleetViewStore.Loader(
            loadView: { envelope },
            readTools: { FleetToolsReading() },
            readRegistry: { _, _, _ in
                gate.enterAndWait()
                return FleetRegistrySummary(zulipChannel: "rebuilt")
            },
            locateRoot: { URL(fileURLWithPath: "/tmp/afc") }
        )
        let store = FleetViewStore(loader: loader)

        let read = Task { await store.refresh() }
        let deadline = Date().addingTimeInterval(5)
        while !gate.entered, Date() < deadline {
            await Task.yield()
        }
        XCTAssertTrue(gate.entered, "the store never rebuilt the rows")
        XCTAssertNil(store.envelope, "the envelope went on screen before the rows were rebuilt")
        XCTAssertTrue(store.loading)
        XCTAssertNotEqual(store.registrySummary.zulipChannel, "rebuilt")

        gate.release()
        await read.value

        XCTAssertNotNil(store.envelope)
        XCTAssertEqual(store.registrySummary.zulipChannel, "rebuilt")
        XCTAssertFalse(store.loading)
    }
}

/// Holds the rebuild of the rows until the test lets it go.
private final class RebuildGate: @unchecked Sendable {
    private let lock = NSLock()
    private var reached = false
    private let semaphore = DispatchSemaphore(value: 0)

    var entered: Bool {
        lock.lock()
        defer { lock.unlock() }
        return reached
    }

    /// Called off the main thread, from the store's detached rebuild.
    func enterAndWait() {
        lock.lock()
        reached = true
        lock.unlock()
        semaphore.wait()
    }

    func release() {
        semaphore.signal()
    }
}
