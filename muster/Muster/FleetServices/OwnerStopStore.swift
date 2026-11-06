import Foundation

// Where the owner's stops are kept between launches of Muster.
//
// A service the owner stopped on purpose stays stopped, and Muster's own restarts never undo
// it.  If the record lived in memory only, quitting Muster would forget it, and the next
// launch would start the service the owner stopped.  The file holds service ids and times
// only, and no credential.

/// What reading the record found.
enum OwnerStopLoad: Equatable {
    /// The record,  which is empty when there is no file yet.
    case stops(OwnerStops)
    /// The file is there and cannot be read.  The reason is in the owner's words.
    case unreadable(String)
}

/// Keeps the owner's stops.
protocol OwnerStopStoring {
    func load() -> OwnerStopLoad
    /// Writes the record.  Returns nil when it was written,  or why it was not.
    func save(_ stops: OwnerStops) -> String?
}

/// The record as a JSON file.
struct OwnerStopFile: OwnerStopStoring {
    static let schemaVersion = 1
    let url: URL

    /// `~/Library/Application Support/Muster/owner-stops.json`.
    static func standardURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appendingPathComponent("Library/Application Support/Muster/owner-stops.json")
    }

    init(url: URL = OwnerStopFile.standardURL()) {
        self.url = url
    }

    private struct Document: Codable {
        let schemaVersion: Int
        /// Service id to the time the owner stopped it,  as ISO 8601 text.
        let stops: [String: String]
    }

    private static let writer: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    func load() -> OwnerStopLoad {
        guard FileManager.default.fileExists(atPath: url.path) else { return .stops(OwnerStops()) }
        let unreadable = "Muster could not read its record of what you stopped (\(url.path)), so it starts nothing.\u{00A0} Fix or delete that file."
        guard let data = try? Data(contentsOf: url),
              let document = try? JSONDecoder().decode(Document.self, from: data),
              document.schemaVersion == Self.schemaVersion else { return .unreadable(unreadable) }
        var times: [String: Date] = [:]
        for (id, text) in document.stops {
            // A time that does not parse is not a reason to forget the stop.
            if let date = FleetTime.parse(text) { times[id] = date }
        }
        return .stops(OwnerStops(ids: Set(document.stops.keys), stoppedAt: times))
    }

    func save(_ stops: OwnerStops) -> String? {
        var written: [String: String] = [:]
        for id in stops.ids {
            // A stop with no known time is still a stop.
            written[id] = stops.stoppedAt[id].map { Self.writer.string(from: $0) } ?? ""
        }
        let document = Document(schemaVersion: Self.schemaVersion, stops: written)
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(document).write(to: url, options: .atomic)
            return nil
        } catch {
            return "Muster could not save your stop to \(url.path), so it will not remember it after Muster quits."
        }
    }
}
