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
    mutating func record(
        _ reading: SeatInventoryReading,
        seat: String,
        host: SeatSetupHost,
        platform: String,
        runStamp: Date?,
        at readAt: Date
    ) {
        let key = Key(seat: seat, host: host)
        switch reading {
        case .loaded:
            entries[key] = Entry(platform: platform, reading: reading, readAt: readAt, runStamp: runStamp)
        case .unavailable:
            entries[key] = nil
        }
    }

    /// The line under a reading:  when it was read, and how long it is shown again.
    static func readLine(readAt: Date) -> String {
        "Read at \(FleetTime.clock(readAt)).\u{00A0} This reading is shown again for up to \(freshWords);  Read Again reads now."
    }
}

/// The one cache the app keeps, so a reading outlives the panel being closed and opened.
/// Only the main actor touches it.
final class SeatInventoryCacheStore {
    static let shared = SeatInventoryCacheStore()
    var cache = SeatInventoryCache()
}

/// Whether a page's timer reads, and whether the app coming back to the front reads once.
///
/// A page that polls (Resource Limits reads the server over ssh every 30 seconds) reads only
/// while the app is in front.  A tick that came while it was not is owed, and the page reads
/// once when the app comes back, so the values shown are never older than the timer allows
/// while anyone is looking.  Coming back with nothing owed reads nothing:  the reading on
/// screen is younger than the timer's interval.
struct ForegroundPolling: Equatable {
    /// Whether the app is in front (`scenePhase == .active`).
    private(set) var active = true
    /// Whether a tick was skipped while the app was not in front.
    private(set) var owed = false

    init(active: Bool = true) {
        self.active = active
    }

    /// One tick of the timer:  whether the page reads now.  A busy page skips the tick, as it
    /// always has, and a busy page in front owes nothing.
    mutating func tick(busy: Bool) -> Bool {
        guard active else {
            owed = true
            return false
        }
        return !busy
    }

    /// The app's phase changed:  whether the page reads once now.
    mutating func phaseChanged(active nowActive: Bool) -> Bool {
        let wasActive = active
        active = nowActive
        guard nowActive, !wasActive, owed else { return false }
        owed = false
        return true
    }

    /// A read happened some other way (the page's own button, an action finishing), so
    /// nothing is owed any more.
    mutating func didRead() {
        owed = false
    }
}
