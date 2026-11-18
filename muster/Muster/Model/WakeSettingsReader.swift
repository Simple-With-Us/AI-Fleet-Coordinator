import Foundation

// Reading the wake settings:  the configuration file,  the eavesdropper's own status,  the kill
// switch,  and the ledger that says how much of the budget is used.
//
// Nothing here decides anything.  It reads,  and hands what it read to the builder.

// MARK: - Where the state lives

/// Where the eavesdropper keeps its state on this Mac.
struct ListenerStateLocation: Equatable {
    /// The eavesdropper's state directory:  `~/.simple-tools`.
    var root: URL

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.root = home.appendingPathComponent(".simple-tools", isDirectory: true)
    }

    /// The configuration file.
    var configURL: URL {
        root.appendingPathComponent("listener.toml")
    }

    /// The eavesdropper's own published state.
    var statusURL: URL {
        root.appendingPathComponent("listener/status.json")
    }

    /// The kill switch.
    var pauseURL: URL {
        root.appendingPathComponent("listener/pause.json")
    }

    /// One seat's directory.
    func seatDirectory(_ tag: String) -> URL {
        root.appendingPathComponent(tag, isDirectory: true)
    }

    /// The seat's wake ledger.
    func ledgerURL(seat: String) -> URL {
        seatDirectory(seat).appendingPathComponent("wakes.jsonl")
    }
}

// MARK: - The ledger cache

/// Wake ledgers already parsed, by path, so an unchanged ledger is not decoded on every read.
///
/// It is shared by the reads one store starts, which run off the main thread, so it holds a
/// lock.  An entry is used only while the ledger's size and modification time are the ones it
/// was parsed at, and for no longer than `maximumAge`.
final class LedgerParseCache: @unchecked Sendable {
    /// How long a parse is reused at most.  Torn rows are judged against the last day at parse
    /// time, so they are judged again at least this often.
    static let maximumAge: TimeInterval = 600

    /// The largest configuration, pause or ledger file the Seats panel reads whole.
    static let maximumFileBytes = 32 * 1024 * 1024

    /// What identifies one version of a ledger.
    struct Stamp: Equatable {
        let size: UInt64
        let modified: Date
    }

    private struct Entry {
        let stamp: Stamp
        let parsedAt: Date
        let parsed: WakeLedger.Parsed
        let truncated: Bool
    }

    private let lock = NSLock()
    private var entries: [String: Entry] = [:]
    private var hitCount = 0

    init() {}

    /// How many reads reused a parse.
    var hits: Int {
        lock.lock()
        defer { lock.unlock() }
        return hitCount
    }

    /// The ledger's size and modification time, or nil when they cannot be read.
    static func stamp(of url: URL) -> Stamp? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return Stamp(size: size, modified: modified)
    }

    /// The parse kept for this version of the ledger, if it is young enough.
    func parsed(for path: String, stamp: Stamp, at now: Date) -> (parsed: WakeLedger.Parsed, truncated: Bool)? {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = entries[path], entry.stamp == stamp else { return nil }
        let age = now.timeIntervalSince(entry.parsedAt)
        guard age >= 0, age < Self.maximumAge else { return nil }
        hitCount += 1
        return (entry.parsed, entry.truncated)
    }

    /// Keeps a parse of this version of the ledger.
    func keep(_ parsed: WakeLedger.Parsed, truncated: Bool, for path: String, stamp: Stamp, at now: Date) {
        lock.lock()
        defer { lock.unlock() }
        entries[path] = Entry(stamp: stamp, parsedAt: now, parsed: parsed, truncated: truncated)
    }

    /// Drops what is kept for a ledger that is gone or cannot be read.
    func forget(_ path: String) {
        lock.lock()
        defer { lock.unlock() }
        entries[path] = nil
    }
}

// MARK: - Reading
