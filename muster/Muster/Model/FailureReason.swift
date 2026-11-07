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
    /// The most characters it shows, for a reason that is one very long line.
    static let shownCharacters = 600
    /// The banner's cap on wrapped lines, so even wrapped text keeps the banner short.
    static let wrappedLineLimit = 9

    /// The part of `reason` the banner shows:  its last lines, after a line that says how many
    /// earlier lines were left out, and an ellipsis where a line was cut.
    static func shown(_ reason: String,
                      lines maxLines: Int = shownLines,
                      characters maxCharacters: Int = shownCharacters) -> String {
        let trimmed = reason
            .replacingOccurrences(of: "\r\n", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var kept = trimmed.components(separatedBy: "\n")
        let dropped = max(0, kept.count - maxLines)
        if dropped > 0 {
            kept = Array(kept.suffix(maxLines))
        }
        var body = kept.joined(separator: "\n")
        var cut = false
        if body.count > maxCharacters {
            body = String(body.suffix(maxCharacters))
            cut = true
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

    /// True when the banner leaves part of `reason` out, so it offers the full text to copy.
    static func isShortened(_ reason: String) -> Bool {
        let trimmed = reason
            .replacingOccurrences(of: "\r\n", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return shown(reason) != trimmed
    }
}
