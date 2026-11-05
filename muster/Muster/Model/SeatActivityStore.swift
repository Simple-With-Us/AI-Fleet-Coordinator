import Foundation

/// The Seat Activity panel's reading,  held for the whole app.
///
/// The panel's view comes and goes as the owner moves around the sidebar,  and a view's own
/// state goes with it.  Before this store each return to the panel started a fresh read
/// beside any read still running.  Now there is one reading and at most one read in flight:
///
///   * a second request while a read runs waits for that read and starts nothing
///   * a return to the panel shows the last reading at once,  and reads again only when
///     that reading is older than the window the caller names
///   * the slow timer reads only while the app is in front (`isActive`),  so nothing is
///     read while nobody is looking
@MainActor
final class SeatActivityStore: ObservableObject {
    /// The one store the panel uses.
    static let shared = SeatActivityStore()

    /// How a read is made.  The default reads the real sources.
    typealias Read = @Sendable ([String]) async -> SeatActivityModel

    /// The last reading,  or nil before the first one.
    @Published private(set) var model: SeatActivityModel?
    /// Whether a read is running now.
    @Published private(set) var loading = false
    /// Whether the app is in front,  as the window last said.  The slow timer reads only then.
    private(set) var isActive = true
    /// The reads this store started.
    private(set) var readsStarted = 0

    private var inFlight: Task<SeatActivityModel, Never>?
    private let reader: Read
    private let now: () -> Date

    init(
        reader: @escaping Read = { roster in await SeatActivityReader().read(roster: roster) },
        now: @escaping () -> Date = { Date() }
    ) {
        self.reader = reader
        self.now = now
    }

    /// Record whether the app is in front.
    func setActive(_ active: Bool) {
        isActive = active
    }

    /// Read now,  unless a read is already running,  in which case wait for that one.
    func refresh(roster: [String]) async {
        if let inFlight {
            _ = await inFlight.value
            return
        }
        loading = true
        readsStarted += 1
        let reader = self.reader
        let task = Task.detached(priority: .utility) {
            await reader(roster)
        }
        inFlight = task
        let result = await task.value
        inFlight = nil
        loading = false
        model = result
    }

    /// Read unless the reading on hand is younger than `maximumAge`.
    func refreshIfOlder(than maximumAge: TimeInterval, roster: [String]) async {
        if let readAt = model?.readAt, now().timeIntervalSince(readAt) < maximumAge {
            return
        }
        await refresh(roster: roster)
    }

    /// One tick of the slow timer:  a read only while the app is in front.
    func timerTick(roster: [String], minimumAge: TimeInterval) async {
        guard isActive else { return }
        await refreshIfOlder(than: minimumAge, roster: roster)
    }
}
