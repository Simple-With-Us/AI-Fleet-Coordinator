import Foundation

// Where the Mac's eavesdropper keeps its state, and how a file's version is told apart.
//
// Nothing here decides anything.  It names places and reads file sizes and times.

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

// MARK: - File versions

/// How a file's version is told apart, and the largest file the Seats panel reads whole.  The
/// parsed wake ledgers this once cached are gone with the Mac listener's settings.
enum LedgerParseCache {
    /// The largest configuration, pause or ledger file the Seats panel reads whole.
    static let maximumFileBytes = 32 * 1024 * 1024

    /// What identifies one version of a file.
    struct Stamp: Equatable {
        let size: UInt64
        let modified: Date
    }

    /// The file's size and modification time, or nil when they cannot be read.
    static func stamp(of url: URL) -> Stamp? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return Stamp(size: size, modified: modified)
    }
}
