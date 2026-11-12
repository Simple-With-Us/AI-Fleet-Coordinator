import Foundation

// The eavesdropper's own configuration file:  `~/.simple-tools/listener.toml`.
//
// This is not a general TOML implementation.  It reads and writes the shape the eavesdropper
// actually uses:  scalars,  string arrays,  and inline tables such as
//
//     [seat.MM]
//     routine = { url_env = "MM_ROUTINE_URL", key_env = "MM_ROUTINE_KEY" }
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

/// A value in the eavesdropper's configuration file.
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

    /// A string as TOML holds it.
    ///
    /// It walks scalars and not characters:  a carriage return and a line feed together are one
    /// `Character`,  which matched no case and reached the file raw.  TOML forbids every control
    /// character in a basic string,  and one left in would make `tomllib` refuse the whole file.
    static func quote(_ value: String) -> String {
        var result = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": result += "\\\""
            case "\\": result += "\\\\"
            case "\n": result += "\\n"
            case "\r": result += "\\r"
            case "\t": result += "\\t"
            default:
                if scalar.value < 0x20 || scalar.value == 0x7F {
                    result += String(format: "\\u%04X", scalar.value)
                } else {
                    result.unicodeScalars.append(scalar)
                }
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
                case "b": result.append("\u{08}")
                case "f": result.append("\u{0C}")
                case "u", "U":
                    guard let scalar = parseHexScalar(digits: escaped == "u" ? 4 : 8) else { return nil }
                    result.unicodeScalars.append(scalar)
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

    /// The scalar a `\u` or `\U` escape names,  or nil when the digits do not name one.
    private mutating func parseHexScalar(digits: Int) -> Unicode.Scalar? {
        var hex = ""
        for _ in 0..<digits {
            guard let digit = peek(), digit.isHexDigit else { return nil }
            hex.append(digit)
            index = text.index(after: index)
        }
        guard let code = UInt32(hex, radix: 16) else { return nil }
        return Unicode.Scalar(code)
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

/// The eavesdropper's configuration file.
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
        /// The lines in this section this app could not read.
        var problems: [String] = []
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
            // A section header may carry a comment:  the eavesdropper's own file has one.
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
                let problem = "Line \(number + 1) is not a key this app can read."
                problems.append(problem)
                current.problems.append(problem)
                continue
            }
            scanner.skipBlanks()
            guard scanner.peek() == "=" else {
                let problem = "Line \(number + 1) is not a key this app can read."
                problems.append(problem)
                current.problems.append(problem)
                continue
            }
            scanner.index = line.index(after: scanner.index)
            guard let node = scanner.parseValue(), scanner.onlyCommentRemains() else {
                let problem = "Line \(number + 1) holds a value this app cannot read."
                problems.append(problem)
                current.problems.append(problem)
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

    /// The seats the file has a section for.
    ///
    /// A dotted header such as `[seat.MM.budget]` is a table inside the MM seat,  not a seat of
    /// its own.
    var seatTags: [String] {
        var tags: [String] = []
        for section in sections where section.name.hasPrefix("seat.") {
            let tag = String(section.name.dropFirst("seat.".count).prefix { $0 != "." })
            if !tag.isEmpty, !tags.contains(tag) { tags.append(tag) }
        }
        return tags
    }

    /// The sections that belong to a seat:  its own,  and any table inside it.
    func sections(ofSeat tag: String) -> [Section] {
        sections.filter { $0.name == "seat.\(tag)" || $0.name.hasPrefix("seat.\(tag).") }
    }

    // MARK: Writing

    enum WriteError: Error, Equatable, LocalizedError {
        case pathIsEmpty
        case notATable(String)
        case heldInItsOwnSection(String)

        var errorDescription: String? {
            switch self {
            case .pathIsEmpty:
                return "The setting has no name, so nothing was written."
            case .notATable(let name):
                return "The \(name) value is not a group of settings, so nothing was written."
            case .heldInItsOwnSection(let name):
                return "The file keeps \(name) under its own [\(name)] header, so writing it in the seat's own section too would define it twice, and nothing was written."
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

        // `[seat.MM.budget]` already defines this key,  so a `budget = ...` line would define it
        // twice.  TOML refuses a duplicate,  and the eavesdropper would then read an empty config.
        guard !sections.contains(where: { $0.name == "\(section).\(key)" }) else {
            throw WriteError.heldInItsOwnSection("\(section).\(key)")
        }

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
            // The table is there and this key is not in it yet:  add the key to the table.
            guard let line = inserting(
                path: subPath,
                value: newValue,
                into: assignment.node,
                line: lines[assignment.line]
            ) else {
                throw WriteError.notATable("\(section).\(key)")
            }
            lines[assignment.line] = line
            return lines.joined(separator: "\n")
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

    /// A line with a new key added to an inline table,  or nil when the path runs into a value
    /// that is not a table.
    ///
    /// The key goes after the table's last entry,  with no trailing comma:  TOML 1.0 forbids one
    /// inside an inline table.
    private func inserting(
        path: [String],
        value: ListenerConfigValue,
        into node: ListenerConfigNode,
        line: String
    ) -> String? {
        guard let step = path.first, let entries = node.tableEntries else { return nil }
        let rest = Array(path.dropFirst())
        if let entry = entries.first(where: { $0.key == step }) {
            return inserting(path: rest, value: value, into: entry.node, line: line)
        }
        let text = "\(step) = \(ListenerConfigValue.nested(path: rest, value: value).tomlText)"
        var result = line
        if let last = entries.last {
            result.insert(contentsOf: ", \(text)", at: last.node.range.upperBound)
        } else {
            result.replaceSubrange(node.range, with: "{ \(text) }")
        }
        return result
    }

    /// The file's text with a seat's section taken out,  along with any table inside it.
    ///
    /// Comments and blank lines just above the next section stay,  because they belong to what
    /// follows.  This is the repair for a section the eavesdropper refuses:  every other line of the
    /// file is left as it was.
    func removingSeat(_ tag: String) -> String {
        var lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let headers = Set(sections.compactMap(\.headerLine))
        var ranges: [Range<Int>] = []
        for header in sections(ofSeat: tag).compactMap(\.headerLine) {
            var end = lines.count
            var index = header + 1
            while index < lines.count {
                let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
                if headers.contains(index) || trimmed.hasPrefix("[[") {
                    end = index
                    break
                }
                index += 1
            }
            while end > header + 1 {
                let trimmed = lines[end - 1].trimmingCharacters(in: .whitespaces)
                guard trimmed.isEmpty || trimmed.hasPrefix("#") else { break }
                end -= 1
            }
            ranges.append(header..<end)
        }
        // From the bottom up,  so a removal does not move the lines of the next one.
        for range in ranges.sorted(by: { $0.lowerBound > $1.lowerBound }) {
            lines.removeSubrange(range)
        }
        return lines.joined(separator: "\n")
    }

    /// The file's text with one value removed.
    ///
    /// Removing a key inside an inline table removes that key and its value,  and leaves
    /// the rest of the table alone.  Removing the last key of a table removes the table.
    ///
    /// Removing a key the file does not hold changes nothing and is not an error:  the
    /// panel seeds an unset setting's field with a dash,  so clearing one is a legitimate
    /// "remove this setting" gesture.  Throwing there would abort the whole Apply and
    /// discard every other edit in the same batch.
    func removing(section: String, key: String, subPath: [String] = []) throws -> String {
        var lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard let found = sections.first(where: { $0.name == section }) else {
            return text
        }
        guard let assignment = found.assignments.first(where: { $0.key == key }) else {
            return text
        }
        guard !subPath.isEmpty else {
            lines.remove(at: assignment.line)
            return lines.joined(separator: "\n")
        }
        guard let entry = locate(assignment.node, path: subPath),
              let entries = assignment.node.tableEntries,
              let target = entries.first(where: { $0.node.range == entry.range }) else {
            // Nothing at that sub-path to take out,  which is the same no-op as above.
            return text
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
            //
            // The comma has to stay inside the removed range.  A range that started after
            // it would leave the comma behind as a trailing comma,  and TOML 1.0 forbids one
            // inside an inline table:  `tomllib.load` would reject the whole file,  and the
            // eavesdropper would fall back to an empty config.  So the range covers
            // ", key = value",  not "key = value".
            var back = start
            while back > line.startIndex {
                let previous = line.index(before: back)
                if line[previous] == " " || line[previous] == "\t" {
                    back = previous
                    continue
                }
                if line[previous] == "," {
                    start = previous
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
