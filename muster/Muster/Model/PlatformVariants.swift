import Foundation

/// The read-only highlights the instruction previews carry,  for onboarding a new
/// platform (AFC-444).
///
/// An instruction file is mostly shared:  the same lane map,  the same protocol words,
/// on every platform.  What usually changes when the file is adapted for a platform is
/// small and named:  placeholders like `<SEAT>`,  the seat's tag,  a platform's name,  and
/// the config folder the platform reads.  This type finds those spans so the preview can
/// mark them,  and says in words that the marks are reminders and not limits:  a new
/// platform may need edits anywhere,  and the whole file still needs review.
///
/// The analysis is a pure function of the text and the viewed platform:  it reads no
/// file,  writes nothing,  and changes nothing,  exactly like the panel that shows it.
enum PlatformVariants {
    /// One highlighted span inside one line.
    struct Span: Equatable {
        /// What kind of customization point the span is.
        enum Kind: Equatable {
            /// A placeholder the file itself marks as variable,  like `<SEAT>`.
            case placeholder
            /// A seat tag,  like `ZC` or `CLAUDE`.
            case seatTag
            /// A config folder a platform reads,  like `~/.claude/`.
            case configFolder
            /// A platform's name in prose,  like `Claude` or `Z Code`.
            case platformName
        }

        /// The span,  in UTF-16 offsets inside its own line,  so the same numbers an
        /// `NSString` sees are the numbers the preview paints.
        let range: NSRange
        let kind: Kind
        /// The platform a `platformName` or `configFolder` span belongs to,  or nil.
        let platform: String?
    }

    /// One line that carries at least one span.
    struct MarkedLine: Equatable {
        /// The line's index,  zero-based,  counting every line of the file.
        let number: Int
        let spans: [Span]
        /// True when the line usually changes with the platform:  it holds a placeholder,
        /// a seat tag or a config folder.  A line whose only spans are platform names is
        /// shared prose that mentions platforms,  and is not a customization point.
        let usuallyChanges: Bool
    }

    /// One markdown section whose body holds customization points.
    struct MarkedSection: Equatable {
        /// The heading line's index,  zero-based.
        let headingNumber: Int
        /// The heading's text with its `#` marks removed.
        let title: String
        /// The lines the section covers,  as line indexes,  endpoints exclusive.
        let bodyRange: Range<Int>
        /// How many lines in the section usually change.
        let changingLines: Int
    }

    /// What analyzing one file found.
    struct Analysis: Equatable {
        /// The platform whose file the preview shows.
        let platform: String
        /// Every line that carries a span,  in file order.
        let lines: [MarkedLine]
        /// Every section that holds at least one changing line.
        let sections: [MarkedSection]

        /// The line indexes that usually change,  for a checklist read.
        var changingLineNumbers: Set<Int> {
            Set(lines.filter(\.usuallyChanges).map(\.number))
        }
    }

    /// The guidance the preview carries beside the highlights.  It says what the marks
    /// are for and,  in words,  that they do not limit where a new platform may edit.
    static let guidance = "Highlights mark places that usually change for a platform:  "
        + "placeholders,  seat tags,  platform names and config folders.\u{00A0} They are "
        + "reminders,  not limits:  a new platform may need edits anywhere in the file,  and "
        + "the whole file still needs review.\u{00A0} The preview is read-only and changes "
        + "nothing."

    /// The platforms whose names and folders the analysis knows,  with the words each
    /// one is called by.  The slugs are the seat-app slugs `FleetRegistryReader` carries,
    /// and the folders are the instruction roots the manifest's targets use today.
    private static let platformWords: [(platform: String, words: [String], folders: [String])] = [
        (platform: "claude-code", words: ["Claude Code", "Claude"], folders: ["~/.claude/"]),
        (platform: "codex", words: ["Codex"], folders: ["~/.codex/"]),
        (platform: "cursor", words: ["Cursor"], folders: ["~/.cursor/"]),
        (platform: "antigravity", words: ["Antigravity"], folders: ["~/.antigravity/"]),
        (platform: "grok", words: ["Grok"], folders: ["~/.grok/"]),
        (platform: "muse", words: ["Muse"], folders: ["~/.muse/"]),
        (platform: "minimax", words: ["MiniMax"], folders: ["~/.minimax/"]),
        (platform: "fx", words: ["FX"], folders: ["~/.fx/"]),
        (platform: "opencode", words: ["OpenCode"], folders: ["~/.config/opencode/"]),
        (platform: "clutch", words: ["Clutch"], folders: ["~/.clutch/"]),
        (platform: "kimi", words: ["Kimi"], folders: ["~/.kimi-code/"]),
        (platform: "zcode", words: ["Z Code", "ZCode"], folders: ["~/.zcode/"]),
        (platform: "gemini", words: ["Gemini"], folders: ["~/.gemini/"]),
    ]

    /// A placeholder the file itself marks as variable:  `<SEAT>`,  `<APP-SLUG>`,  and the
    /// same shape in any mix of letters,  digits and dashes.  Scanned by hand,  so no
    /// pattern has to compile at run time.
    /// The ASCII code of a one-character literal,  as the unichar comparisons read it.
    private static func ascii(_ literal: Character) -> unichar {
        guard let value = literal.asciiValue else { return 0 }
        return unichar(value)
    }

    private static func placeholders(in line: String) -> [NSRange] {
        // A unichar is a plain UInt16,  so every comparison names its ASCII constant
        // instead of leaning on a String literal the type cannot take.
        let lt = ascii("<"), gt = ascii(">"), dash = ascii("-")
        let hay = line as NSString
        var found: [NSRange] = []
        var index = 0
        while index < hay.length {
            guard hay.character(at: index) == lt else {
                index += 1
                continue
            }
            var end = index + 1
            while end < hay.length {
                let c = hay.character(at: end)
                if c == gt { break }
                if !isLetter(c) && !isDigit(c) && c != dash { break }
                end += 1
            }
            let inner = end - index - 1
            if end < hay.length, hay.character(at: end) == gt, inner >= 2,
               isLetter(hay.character(at: index + 1)) {
                found.append(NSRange(location: index, length: end - index + 1))
                index = end + 1
            } else {
                index += 1
            }
        }
        return found
    }

    private static func isLetter(_ c: unichar) -> Bool {
        (c >= ascii("a") && c <= ascii("z"))
            || (c >= ascii("A") && c <= ascii("Z"))
    }

    private static func isDigit(_ c: unichar) -> Bool {
        c >= ascii("0") && c <= ascii("9")
    }

    /// Find the customization points in `text` as the preview of `platform`'s file shows
    /// them.  `seatTags` are the roster's seat tags,  so a tag is marked wherever it
    /// stands as a word,  including the file's own.
    static func analyze(_ text: String, platform: String, seatTags: [String] = []) -> Analysis {
        let allLines = text.components(separatedBy: "\n")
        var marked: [MarkedLine] = []
        for (index, line) in allLines.enumerated() {
            let spans = spans(in: line, seatTags: seatTags)
            guard !spans.isEmpty else { continue }
            let changes = spans.contains { span in
                span.kind == .placeholder || span.kind == .seatTag || span.kind == .configFolder
            }
            marked.append(MarkedLine(number: index, spans: spans, usuallyChanges: changes))
        }
        return Analysis(platform: platform, lines: marked, sections: sections(of: allLines, marked: marked))
    }

    /// The spans of one line,  leftmost first,  never overlapping:  a placeholder keeps
    /// its whole angle brackets even when a word inside it names a platform.
    private static func spans(in line: String, seatTags: [String]) -> [Span] {
        var found: [Span] = []
        for range in placeholders(in: line) {
            found.append(Span(range: range, kind: .placeholder, platform: nil))
        }
        for entry in platformWords {
            for word in entry.words {
                search(line, for: word) { range in
                    found.append(Span(range: range, kind: .platformName, platform: entry.platform))
                }
            }
            for folder in entry.folders {
                search(line, for: folder) { range in
                    found.append(Span(range: range, kind: .configFolder, platform: entry.platform))
                }
            }
        }
        for tag in seatTags where !tag.isEmpty {
            search(line, for: tag, wordBoundary: true, underscoreBreaks: true) { range in
                found.append(Span(range: range, kind: .seatTag, platform: nil))
            }
        }
        return resolveOverlaps(found)
    }

    /// Case-sensitive whole-token search:  a platform word is marked only where it
    /// stands as its own word,  so shared prose that names platforms reads as prose and
    /// a path's inner words are not double-marked.
    private static func search(
        _ line: String,
        for needle: String,
        wordBoundary: Bool = true,
        underscoreBreaks: Bool = false,
        _ record: (NSRange) -> Void
    ) {
        let hay = line as NSString
        let needleLength = (needle as NSString).length
        guard needleLength > 0 else { return }
        var searchAt = 0
        while searchAt <= hay.length - needleLength {
            let hit = hay.range(of: needle, range: NSRange(location: searchAt, length: hay.length - searchAt))
            guard hit.location != NSNotFound else { return }
            let beforeOK: Bool
            let afterOK: Bool
            if wordBoundary {
                beforeOK = hit.location == 0
                    || isTokenBreak(hay.character(at: hit.location - 1), underscoreBreaks: underscoreBreaks)
                let after = hit.location + hit.length
                afterOK = after >= hay.length
                    || isTokenBreak(hay.character(at: after), underscoreBreaks: underscoreBreaks)
            } else {
                // A folder needle ends in "/", so it cannot sit inside a longer folder
                // name, and what follows it is the path it contains.
                beforeOK = true
                afterOK = true
            }
            if beforeOK && afterOK {
                record(hit)
            }
            searchAt = hit.location + needleLength
        }
    }

    /// Anything a word may not be glued to and still be its own word.
    private static func isTokenBreak(_ character: unichar, underscoreBreaks: Bool = false) -> Bool {
        if isLetter(character) || isDigit(character) { return false }
        if character == ascii("-") || character == ascii("/") { return false }
        if character == ascii("_") {
            // A seat tag is marked inside a longer identifier the tag begins,  like
            // `ZC_ZULIP_*`,  because the identifier is the tag's own variable.
            return underscoreBreaks
        }
        return true
    }

    /// Keep the leftmost span when spans overlap,  so a placeholder or a folder keeps its
    /// whole shape,  and return them in line order.
    private static func resolveOverlaps(_ spans: [Span]) -> [Span] {
        var kept: [Span] = []
        for span in spans.sorted(by: { $0.range.location < $1.range.location }) {
            if let last = kept.last, span.range.location < last.range.location + last.range.length { continue }
            kept.append(span)
        }
        return kept
    }

    /// The markdown sections that hold at least one changing line,  so an onboarding
    /// read can jump by section.  A heading is a line of `#` marks;  a section runs to
    /// the next heading of the same or higher level.
    private static func sections(of lines: [String], marked: [MarkedLine]) -> [MarkedSection] {
        let changing = Set(marked.filter(\.usuallyChanges).map(\.number))
        guard !changing.isEmpty else { return [] }
        var headings: [(number: Int, level: Int, title: String)] = []
        for (index, raw) in lines.enumerated() {
            let trimmed = raw.drop(while: { $0 == " " })
            guard trimmed.hasPrefix("#") else { continue }
            let marks = trimmed.prefix(while: { $0 == "#" }).count
            guard marks <= 6, trimmed.count > marks, trimmed.dropFirst(marks).first == " " else { continue }
            let title = trimmed.dropFirst(marks).drop(while: { $0 == " " })
            headings.append((index, marks, String(title)))
        }
        var result: [MarkedSection] = []
        for (position, heading) in headings.enumerated() {
            let end: Int
            if position + 1 < headings.count {
                end = headings[position + 1].number
            } else {
                end = lines.count
            }
            let bodyRange = (heading.number + 1)..<end
            let count = changing.intersection(Set(bodyRange)).count
            guard count > 0 else { continue }
            result.append(MarkedSection(
                headingNumber: heading.number,
                title: heading.title,
                bodyRange: bodyRange,
                changingLines: count
            ))
        }
        return result
    }
}
