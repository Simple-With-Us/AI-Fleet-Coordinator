import Foundation

/// The drift history,  kept on disk so it survives a restart.
///
/// The file is the app's own:  `~/Library/Application Support/Muster/drift-history.json`.
/// It is written when a cell turns red and when the history is cleared,  and it is
/// read once when the app starts.  Nothing else touches it,  and the app reads no
/// credential to write it.
struct DriftHistoryStore {
    /// The version of the file this app writes.
    ///
    /// The version is exact,  the way every other file the app reads is:  a history
    /// it cannot read is an empty history,  never a wrong one.
    static let schemaVersion = 1

    /// The most transitions the file keeps.
    ///
    /// A long-lived app would otherwise grow the file without end,  and the panel
    /// shows the most recent rows anyway.
    static let limit = 500

    /// Where the history lives.
    let url: URL

    init(url: URL = DriftHistoryStore.defaultURL()) {
        self.url = url
    }

    /// The history file for this user.
    static func defaultURL(
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL {
        home
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("Muster", isDirectory: true)
            .appendingPathComponent("drift-history.json", isDirectory: false)
    }

    /// The transitions on disk,  newest first.
    ///
    /// A file that is not there,  a file that cannot be read and a file this app
    /// does not read all give the same answer:  no history.  The history is a record
    /// of what happened,  and a record that cannot be trusted is not a record.
    func load() -> [DriftTransition] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        guard let data = try? Data(contentsOf: url) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let document = try? decoder.decode(DriftHistoryDocument.self, from: data) else {
            return []
        }
        guard document.schemaVersion == DriftHistoryStore.schemaVersion else { return [] }
        return document.transitions
    }

    /// Write the transitions,  newest first.
    ///
    /// A history that cannot be written is not kept in memory either:  the panel
    /// would show a record that a restart would lose,  and a record that a restart
    /// loses is not a record.
    @discardableResult
    func save(_ transitions: [DriftTransition]) -> Bool {
        let document = DriftHistoryDocument(
            schemaVersion: DriftHistoryStore.schemaVersion,
            transitions: Array(transitions.prefix(DriftHistoryStore.limit))
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(document) else { return false }
        let directory = url.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            try data.write(to: url, options: .atomic)
            return true
        } catch {
            return false
        }
    }

    /// Empty the history,  on disk and in the file.
    @discardableResult
    func clear() -> Bool {
        save([])
    }
}

/// The file on disk.
struct DriftHistoryDocument: Codable, Equatable {
    /// The version of the file.
    let schemaVersion: Int
    /// The transitions,  newest first.
    let transitions: [DriftTransition]
}
