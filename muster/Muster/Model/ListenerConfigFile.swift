import Foundation

// The listener's own configuration file:  `~/.agent-sync/listener.toml`.
//
// This is not a general TOML implementation.  It reads and writes the shape the listener
// actually uses:  scalars,  string arrays,  and inline tables such as
//
//     [seat.MM]
//     wake = "muse"
//     budget = { wakes_per_hour = 5, wakes_per_day = 80 }
//
// Two properties matter more than completeness:
//
//   * a value this app cannot read is reported as a problem,  never guessed at
//   * a write changes one value and leaves every other byte of the file alone,  so a
//     hand-written comment,  a key this app does not know about,  or a key this app is not
//     allowed to touch all survive untouched
//
// A path into the file is a section,  a key,  and an optional path inside the key's value:
//  `("seat.MM", "budget", ["wakes_per_hour"])`.  The section is a `[header]`,  the key is the
// name inside it,  and the sub-path walks into an inline table.
//
// MARK: - Values

/// A value in the listener's configuration file.
enum ListenerConfigValue: Equatable {
    case text(String)
    case bool(Bool)
    case number(Double)
    case list([ListenerConfigValue])
    case table([String: ListenerConfigValue])

    /// The text the panel shows.
    var displayText: String {
        switch self {
        case .text(let value):
            return value
        case .bool(let value):
            return value ? "true" : "false"
        case .number(let value):
            return ListenerConfigValue.format(number: value)
        case .list(let items):
            return "[" + items.map(\.displayText).joined(separator: ", ") + "]"
        case .table(let entries):
            let body = entries.keys.sorted()
                .map { "\($0) = \(entries[$0]?.displayText ?? "")" }
                .joined(separator: ", ")
            return "{ " + body + " }"
        }
    }

    /// The text a value is written back as.
    var tomlText: String {
        switch self {
        case .text(let value):
            return ListenerConfigValue.quote(value)
        case .bool(let value):
            return value ? "true" : "false"
        case .number(let value):
            return ListenerConfigValue.format(number: value)
        case .list(let items):
            return "[" + items.map(\.tomlText).joined(separator: ", ") + "]"
        case .table(let entries):
            let body = entries.keys.sorted()
                .map { "\($0) = \(entries[$0]?.tomlText ?? "")" }
                .joined(separator: ", ")
            return "{ " + body + " }"
        }
    }

    /// The text of a value that is not a group.
    var scalarText: String? {
        switch self {
        case .text, .bool, .number: return displayText
        case .list, .table: return nil
        }
    }

    /// A number the way a person would write it:  whole numbers have no fraction.
    static func format(number: Double) -> String {
        if number == number.rounded(), abs(number) < 1e15 {
            return String(Int(number))
        }
        return String(format: "%g", number)
    }

    static func quote(_ value: String) -> String {
        var result = "\""
        for character in value {
            switch character {
            case "\"": result += "\\\""
            case "\\": result += "\\\\"
            case "\n": result += "\\n"
            case "\t": result += "\\t"
            default: result.append(character)
            }
        }
        result += "\""
        return result
    }

    /// A table holding one value at a path,  for a key the file does not have yet.
    static func nested(path: [String], value: ListenerConfigValue) -> ListenerConfigValue {
        guard let step = path.first else { return value }
        return .table([step: nested(path: Array(path.dropFirst()), value: value)])
    }

    /// Whether two values mean the same thing.
    ///
    /// Numbers compare by value,  so `5` in the file and `5.0` from the engine are the same
    /// number and not a drift.  Everything else compares as text.
    func matches(_ other: ListenerConfigValue) -> Bool {
        switch (self, other) {
        case (.number(let left), .number(let right)):
            return abs(left - right) < 1e-9
        case (.bool(let left), .bool(let right)):
            return left == right
        case (.text(let left), .text(let right)):
            return left == right
        case (.list(let left), .list(let right)):
            return left.count == right.count && zip(left, right).allSatisfy { $0.matches($1) }
        case (.table(let left), .table(let right)):
            return left.keys.sorted() == right.keys.sorted()
                && left.keys.allSatisfy { key in
                    guard let mine = left[key], let theirs = right[key] else { return false }
                    return mine.matches(theirs)
                }
        default:
            return false
        }
    }
}

// MARK: - Parsed nodes

/// One key and its value inside an inline table.
struct ListenerConfigEntry: Equatable {
    var key: String
    var node: ListenerConfigNode
    /// The span of the key's text,  so a writer can remove the key and its value together.
    var keyRange: Range<String.Index>
}

/// A parsed value,  with the span of the text it came from.
///
/// The span is what makes a surgical write possible:  the writer replaces the text of one
/// value and leaves the rest of the line,  including its comment,  exactly as it was.
struct ListenerConfigNode: Equatable {
    indirect enum Body: Equatable {
        case scalar(ListenerConfigValue)
        case list([ListenerConfigNode])
        case table([ListenerConfigEntry])
    }

    var body: Body
    /// The span of this value's text,  relative to the line it was parsed from.
    var range: Range<String.Index>

    /// The value,  without the spans.
    var value: ListenerConfigValue {
        switch body {
        case .scalar(let value):
            return value
        case .list(let items):
            return .list(items.map(\.value))
        case .table(let entries):
            var result: [String: ListenerConfigValue] = [:]
            for entry in entries {
                result[entry.key] = entry.node.value
            }
            return .table(result)
        }
    }

    /// The table's entries,  when this node is a table.
    var tableEntries: [ListenerConfigEntry]? {
        if case .table(let entries) = body { return entries }
        return nil
    }
}

// MARK: - The scanner

/// A scanner over one line of the configuration file.
private struct ListenerConfigScanner {
    let text: String
    var index: String.Index

    init(_ text: String, from start: String.Index? = nil) {
        self.text = text
        self.index = start ?? text.startIndex
    }

    var isAtEnd: Bool { index >= text.endIndex }

    func peek() -> Character? {
        isAtEnd ? nil : text[index]
    }

    mutating func skipBlanks() {
        while let character = peek(), character == " " || character == "\t" {
            index = text.index(after: index)
        }
    }

    /// Whether only blanks and a comment are left on the line.
    func onlyCommentRemains() -> Bool {
        var probe = index
        while probe < text.endIndex, text[probe] == " " || text[probe] == "\t" {
            probe = text.index(after: probe)
        }
        return probe >= text.endIndex || text[probe] == "#"
    }

    mutating func parseKey() -> String? {
        skipBlanks()
        guard let character = peek() else { return nil }
        if character == "\"" || character == "'" {
            guard let node = parseString(), case .scalar(.text(let value)) = node.body else { return nil }
            return value
        }
        var key = ""
        while let character = peek(),
              character.isLetter || character.isNumber || character == "_" || character == "-" {
            key.append(character)
            index = text.index(after: index)
        }
        return key.isEmpty ? nil : key
    }

    mutating func parseString() -> ListenerConfigNode? {
        skipBlanks()
        guard let quote = peek(), quote == "\"" || quote == "'" else { return nil }
        let start = index
        index = text.index(after: index)
        var result = ""
        while let character = peek() {
            if character == quote {
                index = text.index(after: index)
                return ListenerConfigNode(body: .scalar(.text(result)), range: start..<index)
            }
            if character == "\\", quote == "\"" {
                index = text.index(after: index)
                guard let escaped = peek() else { return nil }
                index = text.index(after: index)
                switch escaped {
                case "n": result.append("\n")
                case "t": result.append("\t")
                case "r": result.append("\r")
                case "\"": result.append("\"")
                case "\\": result.append("\\")
                default: result.append(escaped)
                }
                continue
            }
            result.append(character)
            index = text.index(after: index)
        }
        return nil
    }

    mutating func parseBareValue() -> ListenerConfigNode? {
        skipBlanks()
        let start = index
        var raw = ""
        while let character = peek() {
            if character == "," || character == "}" || character == "]" || character == "#" {
                break
            }
            raw.append(character)
            index = text.index(after: index)
        }
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        let value: ListenerConfigValue
        switch trimmed {
        case "true": value = .bool(true)
        case "false": value = .bool(false)
        default:
            if trimmed.rangeOfCharacter(from: .letters) == nil, let number = Double(trimmed) {
                value = .number(number)
            } else {
                value = .text(trimmed)
            }
        }
        // The value's span stops at the value,  not at the comment:  a write leaves the
        // spacing and the comment in front of it exactly as they were.
        var end = index
        while end > start {
            let previous = text.index(before: end)
            if text[previous] == " " || text[previous] == "\t" {
                end = previous
                continue
            }
            break
        }
        return ListenerConfigNode(body: .scalar(value), range: start..<end)
    }

    mutating func parseList() -> ListenerConfigNode? {
        skipBlanks()
        guard peek() == "[" else { return nil }
        let start = index
        index = text.index(after: index)
        var items: [ListenerConfigNode] = []
        while true {
            skipBlanks()
            guard let character = peek() else { return nil }
            if character == "]" {
                index = text.index(after: index)
                break
            }
            if character == "," {
                index = text.index(after: index)
                continue
            }
            guard let item = parseValue() else { return nil }
            items.append(item)
        }
        return ListenerConfigNode(body: .list(items), range: start..<index)
    }

    mutating func parseTable() -> ListenerConfigNode? {
        skipBlanks()
        guard peek() == "{" else { return nil }
        let start = index
        index = text.index(after: index)
        var entries: [ListenerConfigEntry] = []
        while true {
            skipBlanks()
            guard let character = peek() else { return nil }
            if character == "}" {
                index = text.index(after: index)
                break
            }
            if character == "," {
                index = text.index(after: index)
                continue
            }
            let keyStart = index
            guard let key = parseKey() else { return nil }
            let keyEnd = index
            skipBlanks()
            guard peek() == "=" else { return nil }
            index = text.index(after: index)
            guard let node = parseValue() else { return nil }
            entries.append(ListenerConfigEntry(key: key, node: node, keyRange: keyStart..<keyEnd))
        }
        return ListenerConfigNode(body: .table(entries), range: start..<index)
    }

    mutating func parseValue() -> ListenerConfigNode? {
        skipBlanks()
        switch peek() {
        case "\"", "'": return parseString()
        case "[": return parseList()
        case "{": return parseTable()
        default: return parseBareValue()
        }
    }
}

// MARK: - The document

/// The listener's configuration file.
///
/// Reading gives the values the file holds.  Writing changes one value and nothing else.
struct ListenerConfigDocument: Equatable {
    /// One `key = value` line inside a section.
    struct Assignment: Equatable {
        var key: String
        var node: ListenerConfigNode
        /// The line the assignment is on,  so a writer can find it again.
        var line: Int
    }

    /// One `[section]` of the file.
    struct Section: Equatable {
        var name: String
        var assignments: [Assignment]
        /// The line the section header is on,  or nil for the region before the first header.
        var headerLine: Int?
    }

    let text: String
    let problems: [String]
    let sections: [Section]

    init(text: String) {
        self.text = text

        var problems: [String] = []
        var sections: [Section] = []
        var current = Section(name: "", assignments: [], headerLine: nil)
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)

        for (number, line) in lines.enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            // A section header may carry a comment:  the listener's own file has one.
            if trimmed.hasPrefix("["), !trimmed.hasPrefix("[["),
               let close = trimmed.firstIndex(of: "]") {
                let name = String(trimmed[trimmed.index(after: trimmed.startIndex)..<close])
                    .trimmingCharacters(in: .whitespaces)
                if current.headerLine != nil || !current.assignments.isEmpty {
                    sections.append(current)
                }
                current = Section(name: name, assignments: [], headerLine: number)
                continue
            }
            guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { continue }
            var scanner = ListenerConfigScanner(line)
            guard let key = scanner.parseKey() else {
                problems.append("Line \(number + 1) is not a key this app can read.")
                continue
            }
            scanner.skipBlanks()
            guard scanner.peek() == "=" else {
                problems.append("Line \(number + 1) is not a key this app can read.")
                continue
            }
            scanner.index = line.index(after: scanner.index)
            guard let node = scanner.parseValue(), scanner.onlyCommentRemains() else {
                problems.append("Line \(number + 1) holds a value this app cannot read.")
                continue
            }
            current.assignments.append(Assignment(key: key, node: node, line: number))
        }
        if current.headerLine != nil || !current.assignments.isEmpty {
            sections.append(current)
        }
        self.problems = problems
        self.sections = sections
    }

    // MARK: Reading

    /// The value a section's key holds,  following the sub-path into an inline table.
    func value(section: String, key: String, subPath: [String] = []) -> ListenerConfigValue? {
        guard let assignment = assignment(section: section, key: key) else { return nil }
        return descend(assignment.node, path: subPath)
    }

    /// The assignment a section's key holds.
    func assignment(section: String, key: String) -> Assignment? {
        sections.first(where: { $0.name == section })?.assignments.first(where: { $0.key == key })
    }

    /// The value of a node at a sub-path inside it.
    private func descend(_ node: ListenerConfigNode, path: [String]) -> ListenerConfigValue? {
        guard let step = path.first else { return node.value }
        guard let entries = node.tableEntries,
              let entry = entries.first(where: { $0.key == step }) else { return nil }
        return descend(entry.node, path: Array(path.dropFirst()))
    }

    /// Whether the file holds a value at a path.
    func contains(section: String, key: String, subPath: [String] = []) -> Bool {
        value(section: section, key: key, subPath: subPath) != nil
    }

    // MARK: Writing

    enum WriteError: Error, Equatable, LocalizedError {
        case pathIsEmpty
        case notATable(String)

        var errorDescription: String? {
            switch self {
            case .pathIsEmpty:
                return "The setting has no name, so nothing was written."
            case .notATable(let name):
                return "The \(name) value is not a group of settings, so nothing was written."
            }
        }
    }

    /// The file's text with one value changed.
    ///
    /// The value is written in place when the file already holds it,  and added to its
    /// section when it does not.  Everything else in the file,  including comments and keys
    /// this app does not know about,  is left exactly as it was.
    func setting(
        section: String,
        key: String,
        subPath: [String] = [],
        to newValue: ListenerConfigValue
    ) throws -> String {
        var lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)

        guard let found = sections.first(where: { $0.name == section }) else {
            // A section the file does not have yet:  add it at the end.
            if lines.last?.isEmpty == true { lines.removeLast() }
            lines.append("[\(section)]")
            lines.append("\(key) = \(ListenerConfigValue.nested(path: subPath, value: newValue).tomlText)")
            return lines.joined(separator: "\n")
        }

        guard let assignment = found.assignments.first(where: { $0.key == key }) else {
            // The section exists but the key does not:  add the key to it.
            let value = ListenerConfigValue.nested(path: subPath, value: newValue)
            if let header = found.headerLine {
                lines.insert("\(key) = \(value.tomlText)", at: header + 1)
            } else if let last = found.assignments.last {
                lines.insert("\(key) = \(value.tomlText)", at: last.line + 1)
            } else {
                lines.append("\(key) = \(value.tomlText)")
            }
            return lines.joined(separator: "\n")
        }

        guard !subPath.isEmpty else {
            lines[assignment.line] = replacing(assignment.node.range, in: lines[assignment.line], with: newValue.tomlText)
            return lines.joined(separator: "\n")
        }

        guard let entry = locate(assignment.node, path: subPath) else {
            throw WriteError.notATable("\(section).\(key)")
        }
        lines[assignment.line] = replacing(entry.range, in: lines[assignment.line], with: newValue.tomlText)
        return lines.joined(separator: "\n")
    }

    /// The node at a sub-path inside a table node.
    private func locate(_ node: ListenerConfigNode, path: [String]) -> ListenerConfigNode? {
        guard let step = path.first else { return node }
        guard let entries = node.tableEntries,
              let entry = entries.first(where: { $0.key == step }) else { return nil }
        return locate(entry.node, path: Array(path.dropFirst()))
    }
    /// The file's text with one value removed.
    ///
    /// Removing a key inside an inline table removes that key and its value,  and leaves
    /// the rest of the table alone.  Removing the last key of a table removes the table.
    func removing(section: String, key: String, subPath: [String] = []) throws -> String {
        var lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard let found = sections.first(where: { $0.name == section }) else {
            throw WriteError.pathIsEmpty
        }
        guard let assignment = found.assignments.first(where: { $0.key == key }) else {
            throw WriteError.pathIsEmpty
        }
        guard !subPath.isEmpty else {
            lines.remove(at: assignment.line)
            return lines.joined(separator: "\n")
        }
        guard let entry = locate(assignment.node, path: subPath),
              let entries = assignment.node.tableEntries,
              let target = entries.first(where: { $0.node.range == entry.range }) else {
            throw WriteError.notATable("\(section).\(key)")
        }
        // A table with one key left has no reason to stay.
        if entries.count == 1 {
            lines.remove(at: assignment.line)
            return lines.joined(separator: "\n")
        }
        let line = lines[assignment.line]
        var start = target.keyRange.lowerBound
        var end = entry.range.upperBound
        var probe = end
        while probe < line.endIndex, line[probe] == " " || line[probe] == "\t" {
            probe = line.index(after: probe)
        }
        if probe < line.endIndex, line[probe] == "," {
            probe = line.index(after: probe)
            while probe < line.endIndex, line[probe] == " " || line[probe] == "\t" {
                probe = line.index(after: probe)
            }
            end = probe
        } else {
            // The last entry of the table:  take the comma in front of it too.
            var back = start
            while back > line.startIndex {
                let previous = line.index(before: back)
                if line[previous] == " " || line[previous] == "\t" {
                    back = previous
                    continue
                }
                if line[previous] == "," {
                    start = previous
                    var after = line.index(after: start)
                    while after < target.keyRange.lowerBound,
                          line[after] == " " || line[after] == "\t" {
                        after = line.index(after: after)
                    }
                    start = after
                }
                break
            }
        }
        var result = line
        result.replaceSubrange(start..<end, with: "")
        lines[assignment.line] = result
        return lines.joined(separator: "\n")
    }

    /// The text of a line with a span replaced.
    private func replacing(
        _ range: Range<String.Index>,
        in line: String,
        with replacement: String
    ) -> String {
        var result = line
        result.replaceSubrange(range, with: replacement)
        return result
    }
}
