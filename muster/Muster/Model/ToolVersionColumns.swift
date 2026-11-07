import Foundation

// The version columns a registry row shows in Tools And Worker Health (AFC-461).
//
// Every value comes from `FleetToolsRow`'s own helpers (`installedVersion`,
// `latestVersion`,  `otherHostVersion`,  `otherHostName` and `basisText`,  from PR 1467).
// This file only says how each one reads on screen,  so the view stays a layout and the
// words are tested.  It carries no SwiftUI,  so the test bundle compiles it directly.

/// One of the three values:  its label,  its words,  and the tooltip behind them.
struct ToolVersionCell: Equatable {
    /// The column's Title Case label.
    let label: String
    /// The value,  or nil when the doctor did not read one.
    let value: String?
    /// The tooltip,  when there is something to say beyond the value.
    let help: String?

    /// The words on screen:  the value,  or "Not known",  never blank and never zero.
    var text: String { value ?? ToolVersionColumns.notKnown }

    /// Whether the doctor read a value.  The view repeats this in colour only.
    var isKnown: Bool { value != nil }
}

/// The three version values of one registry row,  and what the row was judged against.
struct ToolVersionColumns: Equatable {
    /// What a missing value reads as.
    static let notKnown = "Not known"

    /// The column labels,  in Title Case.
    static let installedLabel = "Installed"
    static let latestLabel = "Latest"
    static let otherHostLabel = "Other Host"

    /// One fixed-width column,  in points.  Three of them and their gaps fit the narrowest
    /// row (`narrowestRowWidth`),  so the columns never need truncating.
    static let columnWidth: Double = 132
    /// The gap between two columns,  in points.
    static let columnSpacing: Double = 12
    /// The narrowest width a tool row gets:  the window's 1160-pt minimum less the
    /// sidebar's 300-pt maximum and 48 pt of split view and inset is an 812-pt detail
    /// pane,  and the panel's 22-pt side padding and the rows' 16-pt indent leave 752.
    static let narrowestRowWidth: Double = 1160 - 300 - 48 - 22 * 2 - 16
    /// The longest value a column holds on one line,  in characters:  10-pt monospaced
    /// digits are about 6 pt wide,  so 20 of them fill 120 of a column's 132 pt.  A row
    /// with a longer value puts each value on its own line instead of truncating it.
    static let columnCharacterLimit = 20

    let installed: ToolVersionCell
    let latest: ToolVersionCell
    let otherHost: ToolVersionCell
    /// What the row was judged against,  in words,  or nil when the doctor did not say.
    let basis: String?
    /// The doctor's basis in `basisText`'s own words,  for the basis line's tooltip.
    let basisHelp: String?
    /// Whether the basis line says why the row is behind,  which the view repeats in the
    /// row's own colour.
    let explainsStale: Bool

    /// The three values,  in the order the row shows them.
    var cells: [ToolVersionCell] { [installed, latest, otherHost] }

    /// True when a value is too long for a fixed-width column,  so the row lists each value
    /// on its own line under the tool name rather than truncate it.
    var needsOwnLines: Bool {
        cells.contains { $0.text.count > ToolVersionColumns.columnCharacterLimit }
    }

    /// The columns for one row,  or nil when the row carries no version reading at all
    /// (a fleet tool,  a checkout,  a service or a harness row),  which keeps its old look.
    ///
    /// A host name with no version is not a reading:  it names the other machine but says
    /// nothing about any version,  so on its own it builds no columns.
    static func make(for row: FleetToolsRow) -> ToolVersionColumns? {
        let installedValue = row.installedVersion
        let latestValue = row.latestVersion
        let otherVersion = row.otherHostVersion
        let otherName = row.otherHostName
        let basisText = row.basisText
        guard installedValue != nil || latestValue != nil || otherVersion != nil
            || basisText != nil
        else { return nil }

        let installed = ToolVersionCell(
            label: installedLabel,
            value: installedValue,
            help: trimmed(row.path).map { "Found at \($0)" }
        )

        let source = trimmed(row.latestSource)
        let latestHelp: String?
        if let source {
            latestHelp = latestValue == nil
                ? "The doctor did not read the latest from \(source)."
                : "Latest read from \(source)."
        } else {
            latestHelp = latestValue == nil ? "The doctor read no latest version for this tool." : nil
        }
        let latest = ToolVersionCell(label: latestLabel, value: latestValue, help: latestHelp)

        let otherHost: ToolVersionCell
        if let otherVersion {
            let value = otherName.map { "\(hostTitle($0)) \(otherVersion)" } ?? otherVersion
            let who = otherName.map { "The \(hostTitle($0))'s" } ?? "The other machine's"
            otherHost = ToolVersionCell(
                label: otherHostLabel,
                value: value,
                help: "\(who) copy is newer than this one."
            )
        } else {
            otherHost = ToolVersionCell(
                label: otherHostLabel,
                value: nil,
                help: "The doctor names the other machine's version only when it is newer than this one."
            )
        }

        let words = basisWords(
            basis: row.basis,
            basisText: basisText,
            outcome: row.outcome,
            otherHostName: otherName
        )
        return ToolVersionColumns(
            installed: installed,
            latest: latest,
            otherHost: otherHost,
            basis: words?.text,
            basisHelp: basisText,
            explainsStale: words?.explainsStale ?? false
        )
    }

    /// The basis in words.
    ///
    /// A row that is behind says why,  in the doctor's own terms:  behind the latest
    /// release,  or older than the other machine (the doctor writes a skew only on the
    /// older host's row).  Any other row says what it was compared with,  from
    /// `basisText`.  A basis the helper does not know reads as nothing.
    static func basisWords(
        basis: String?,
        basisText: String?,
        outcome: FleetToolsRowOutcome,
        otherHostName: String?
    ) -> (text: String, explainsStale: Bool)? {
        guard let basisText else { return nil }
        let behind = outcome == .stale || outcome == .failing
        if behind {
            switch trimmed(basis) ?? "" {
            case "latest":
                return ("Behind the latest release", true)
            case "skew":
                let other = otherHostName.map { "the \(hostTitle($0))" } ?? "the other machine"
                return ("Older than \(other)", true)
            default:
                break
            }
        }
        return (sentenceCase(basisText), false)
    }

    /// A host's name as a word:  "mac" is the Mac and "server" is the Server.
    static func hostTitle(_ name: String) -> String {
        switch name.lowercased() {
        case "mac": return "Mac"
        case "server": return "Server"
        default: return sentenceCase(name)
        }
    }

    private static func sentenceCase(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.uppercased() + text.dropFirst()
    }

    private static func trimmed(_ value: String?) -> String? {
        guard let value else { return nil }
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}
