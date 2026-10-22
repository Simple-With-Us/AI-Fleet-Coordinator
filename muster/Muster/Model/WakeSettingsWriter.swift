import Foundation

// Writing the wake settings.
//
// The listener's configuration file is the one the runtime reads,  so the panel writes that
// file and then asks the listener to act on it.  Two rules hold:
//
//   * only the value that changed is rewritten,  so a comment or a key this app does not
//     know about survives
//   * the file is written the way the runtime writes it:  a temporary file at mode 0600,
//     then a rename over the original

/// Writes a seat's wake settings into the listener's configuration file.
struct WakeSettingsWriter {
    /// One change to one setting.
    struct Edit: Equatable {
        /// The seat the change belongs to.
        var seat: String
        /// The setting.
        var key: WakeSettingKey
        /// The new value,  or nil to remove the setting from the file.
        var value: ListenerConfigValue?
    }

    enum WriteError: Error, Equatable, LocalizedError {
        case unreadable(String)
        case couldNotWrite(String)

        var errorDescription: String? {
            switch self {
            case .unreadable(let path):
                return "The configuration file at \(path) could not be read,  so nothing was changed."
            case .couldNotWrite(let path):
                return "The configuration file at \(path) could not be written."
            }
        }
    }

    /// Where the file is.
    var configURL: URL

    init(configURL: URL) {
        self.configURL = configURL
    }

    /// Applies the edits to the file,  and returns the file's new text.
    ///
    /// Every edit is applied to the text the last one produced,  so two edits to the same
    /// file do not fight over it.
    @discardableResult
    func apply(_ edits: [Edit]) throws -> String {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else {
            throw WriteError.unreadable(configURL.path)
        }
        var updated = text
        for edit in edits {
            let location = edit.key.configLocation
            let document = ListenerConfigDocument(text: updated)
            if let value = edit.value {
                updated = try document.setting(
                    section: "seat.\(edit.seat)",
                    key: location.key,
                    subPath: location.subPath,
                    to: value
                )
            } else {
                updated = try document.removing(
                    section: "seat.\(edit.seat)",
                    key: location.key,
                    subPath: location.subPath
                )
            }
        }
        try write(updated)
        return updated
    }

    /// Writes the file the way the runtime writes it.
    func write(_ text: String) throws {
        let temporary = URL(fileURLWithPath: configURL.path + ".tmp")
        let fileManager = FileManager.default
        guard let data = text.data(using: .utf8) else {
            throw WriteError.couldNotWrite(configURL.path)
        }
        guard fileManager.createFile(
            atPath: temporary.path,
            contents: data,
            attributes: [.posixPermissions: 0o600]
        ) else {
            throw WriteError.couldNotWrite(configURL.path)
        }
        guard rename(temporary.path, configURL.path) == 0 else {
            try? fileManager.removeItem(at: temporary)
            throw WriteError.couldNotWrite(configURL.path)
        }
    }
}

// MARK: - Turning panel values into edits

extension WakeSettingKey {
    /// Whether the setting holds a number.
    var isNumeric: Bool {
        switch self {
        case .wakesPerHour, .wakesPerDay, .perTopicPerHour, .ownerPerDay,
             .ownerPerTopicPerHour, .usdPerDay, .livePerHour, .livePerDay,
             .livePerTopicMinutes, .liveOwnerPerDay, .liveLoopTurns:
            return true
        default:
            return false
        }
    }

    /// Whether the setting holds a yes or a no.
    var isBoolean: Bool {
        self == .fallbackUncontained
    }

    /// The value a field holds,  when the text can be read as one.
    ///
    /// Nothing is written when the text is not a value of this kind,  so a half-typed
    /// number never lands in the file.
    func value(from text: String) -> ListenerConfigValue? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        if isBoolean {
            switch trimmed.lowercased() {
            case "true", "yes": return .bool(true)
            case "false", "no": return .bool(false)
            default: return nil
            }
        }
        if isNumeric {
            guard let number = Double(trimmed) else { return nil }
            return .number(number)
        }
        return .text(trimmed)
    }
}
