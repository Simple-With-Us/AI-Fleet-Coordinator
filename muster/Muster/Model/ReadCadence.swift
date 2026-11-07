import Foundation

// When a panel reads something slow again, kept apart from the views so it can be tested
// without SwiftUI.
//
// Two panels read things that cost a process each time:  Seat Setup runs
// `scripts/fleet_seat_inventory.py` (over ssh when the server is the chosen host), and
// Resource Limits reads the server's policy over ssh on a timer.  Neither should run that
// read more often than the answer can change, nor while nobody is looking.

/// The platform inventories Seat Setup has read, one per seat and host, with when each was
/// read.
///
/// Switching back to a seat and host read in the last few minutes shows the reading already
/// held, with its time, instead of running the reader again.  On the Mac a new engine run also
/// reads again, once:  the engine runs when a watched config file changes, and those are the
/// files the inventory lists.  The engine checks the Mac only, so its runs say nothing about
/// the server and never make the server's reading stale.  A read the owner asks for always
/// runs.  A failed read is shown but never kept, so the next visit tries again.
struct SeatInventoryCache {
    /// How long a reading is shown again before switching back reads it again.
    static let freshSeconds: TimeInterval = 5 * 60

    /// The same span, in the words the panel uses.
    static let freshWords = "5 minutes"

    struct Key: Hashable {
        let seat: String
        let host: SeatSetupHost
    }

    /// One reading that succeeded.
    struct Entry: Equatable {
        /// The platform it was read for, as the engine's rules named it then.
        let platform: String
        let reading: SeatInventoryReading
        let readAt: Date
        /// The engine run it was read under:  `FleetStore.generatedAt` when the read began.
        let runStamp: Date?
    }

    enum Decision: Equatable {
        /// Run the reader.
        case read
        /// Show this reading again.
        case useHeld(Entry)
    }

    private(set) var entries: [Key: Entry] = [:]

    func entry(seat: String, host: SeatSetupHost) -> Entry? {
        entries[Key(seat: seat, host: host)]
    }

    /// Whether the panel runs the reader for this seat and host now, or shows `held` again.
    ///
    /// - Parameters:
    ///   - held: the reading already held for this seat and host, if any.
    ///   - platform: the platform the engine's rules name for the seat now.
    ///   - runStamp: the current engine run's stamp (`FleetStore.generatedAt`).
    ///   - force: the owner asked for a read.
    static func decide(
        held: Entry?,
        host: SeatSetupHost,
        platform: String,
        runStamp: Date?,
        now: Date,
        force: Bool,
        freshFor: TimeInterval = freshSeconds
    ) -> Decision {
        if force { return .read }
        guard let held else { return .read }
        if held.platform != platform { return .read }
        let age = now.timeIntervalSince(held.readAt)
        // A clock that went back cannot say how old the reading is.
        if age < 0 || age >= freshFor { return .read }
        if host == .mac, let runStamp, held.runStamp != runStamp { return .read }
        return .useHeld(held)
    }

    /// Keep a reading that succeeded.  A failed one is not kept, and it drops the reading held
    /// before it, so the failure is what the panel last saw and the next visit reads again.
    ///
    /// Two reads of one seat and host can overlap (Read Again while the panel's own read runs).
    /// A failure drops only a reading taken before it started (`startedAt`), so a slow failure
    /// never throws away a success that landed while it ran.
    mutating func record(
        _ reading: SeatInventoryReading,
        seat: String,
        host: SeatSetupHost,
        platform: String,
        runStamp: Date?,
        startedAt: Date,
        at readAt: Date
    ) {
        let key = Key(seat: seat, host: host)
        switch reading {
        case .loaded:
            entries[key] = Entry(platform: platform, reading: reading, readAt: readAt, runStamp: runStamp)
        case .unavailable:
            if let held = entries[key], held.readAt > startedAt { return }
            entries[key] = nil
        }
    }

    /// The line under a reading:  when it was read, and how long it is shown again.
    static func readLine(readAt: Date) -> String {
        "Read at \(FleetTime.clock(readAt)).\u{00A0} This reading is shown again for up to \(freshWords);  Read Again reads now."
    }

    /// Whether a read that has just come back may go on screen:  only when the panel still
    /// shows the seat and host it was read for.  A read the panel has moved on from (the owner
    /// switched seat or host while it ran) is kept in the cache by `record` and never drawn
    /// over the seat and host shown now.
    static func belongsOnScreen(readFor: Key, shown: Key?) -> Bool {
        readFor == shown
    }
}

/// The one cache the app keeps, so a reading outlives the panel being closed and opened.
/// It is the main actor's alone, so two reads that overlap never touch it at once.
@MainActor
final class SeatInventoryCacheStore {
    static let shared = SeatInventoryCacheStore()
    var cache = SeatInventoryCache()
}

/// When a page that polls reads next, so it reads only while the app is in front.
///
/// A page that polls (Resource Limits reads the server over ssh every 30 seconds) runs no
/// timer at all while the app is not in front:  `nextStep` says `.stop`, the page's loop ends,
/// and nothing wakes until the app comes back.  Coming back starts the loop again, and it
/// reads at once when the last read is at least an interval old, so it reads once to catch up
/// and otherwise waits out the rest of the interval.  Something that wants a read while the
/// app is away (an action finishing) is held until it comes back.
struct ForegroundPolling: Equatable {
    enum Step: Equatable {
        /// The app is not in front:  the timer stops until it is.
        case stop
        /// Read now.
        case read
        /// Wait this many seconds, then ask again.
        case wait(TimeInterval)
    }

    /// How often the page reads while the app is in front.
    let interval: TimeInterval
    /// Whether the app is in front (`scenePhase == .active`).
    private(set) var active: Bool
    /// When the last read finished, or nil when none has or a read is owed.
    private(set) var lastReadAt: Date?

    init(interval: TimeInterval, active: Bool = true) {
        self.interval = interval
        self.active = active
    }

    /// The app's phase changed.
    mutating func setActive(_ nowActive: Bool) {
        active = nowActive
    }

    /// What the timer does next.  A busy page (an action running, or a read already in flight)
    /// waits a whole interval rather than reading, as the timer always has.
    func nextStep(now: Date, busy: Bool) -> Step {
        guard active else { return .stop }
        if let lastReadAt {
            let age = now.timeIntervalSince(lastReadAt)
            // A clock that went back cannot say how old the reading is, so it reads.
            if age >= 0 && age < interval {
                return .wait(max(1, interval - age))
            }
        }
        return busy ? .wait(interval) : .read
    }

    /// A read finished, however it was started.
    mutating func didRead(at readAt: Date) {
        lastReadAt = readAt
    }

    /// An action finished and the page wants the server's new values:  whether it reads now.
    /// While the app is away it does not, and the read is owed for when the app comes back.
    mutating func actionFinished() -> Bool {
        if active { return true }
        lastReadAt = nil
        return false
    }
}
