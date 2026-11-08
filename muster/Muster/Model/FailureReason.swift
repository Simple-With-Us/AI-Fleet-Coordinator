import Foundation

/// How much of a failure's reason the banner over a panel shows.
///
/// The banner sits in the detail column above the panel, and a reason can be a whole
/// traceback.  Shown in full it grew taller than the window and squeezed the panel under it
/// to nothing.  So the banner shows the reason's last lines, which name the error, says how
/// much it left out, and keeps the full reason one click away (Copy Full Reason).
enum FailureReason {
    /// The most lines of the reason the banner shows.
    static let shownLines = 6
    /// The most characters it shows, for a reason that is one very long line.  With the
    /// line cap this bounds the banner's height however the text wraps, so the banner needs
    /// no line limit of its own, which would hide the end of the reason this keeps.
    static let shownCharacters = 600

    /// The part of `reason` the banner shows:  its last lines, after a line that says how many
    /// earlier lines were left out, and an ellipsis where a line was cut.  The count includes
    /// the lines the character cap removed whole.
    static func shown(_ reason: String,
                      lines maxLines: Int = shownLines,
                      characters maxCharacters: Int = shownCharacters) -> String {
        let trimmed = reason
            .replacingOccurrences(of: "\r\n", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var kept = trimmed.components(separatedBy: "\n")
        var dropped = max(0, kept.count - maxLines)
        if dropped > 0 {
            kept = Array(kept.suffix(maxLines))
        }
        var body = kept.joined(separator: "\n")
        var cut = false
        if body.count > maxCharacters {
            let removed = body.prefix(body.count - maxCharacters)
            var rest = body.suffix(maxCharacters)
            // Each line break in the removed part ends a line that is gone entirely.
            dropped += removed.filter { $0 == "\n" }.count
            if rest.hasPrefix("\n") {
                // The removed part ended exactly at a line's end, so that line is gone too.
                dropped += 1
                rest = rest.dropFirst()
            } else {
                // A line is cut only when the removed part stops partway through it.
                cut = !removed.hasSuffix("\n")
            }
            body = String(rest)
        }
        var head = ""
        if dropped > 0 {
            head = "\u{2026} \(dropped) earlier \(dropped == 1 ? "line" : "lines") not shown\n"
        }
        if cut {
            head += "\u{2026}"
        }
        return head + body
    }

    /// What the banner shows, and whether that leaves part of `reason` out (so the banner
    /// offers the full text to copy), worked out once with the same limits as `shown`, so
    /// the flag always describes the text it comes with.
    static func display(_ reason: String,
                        lines maxLines: Int = shownLines,
                        characters maxCharacters: Int = shownCharacters) -> (text: String, isShortened: Bool) {
        let trimmed = reason
            .replacingOccurrences(of: "\r\n", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let text = shown(reason, lines: maxLines, characters: maxCharacters)
        return (text, text != trimmed)
    }
}
