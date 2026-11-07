import SwiftUI

/// The read-only preview of an instruction file,  with the customization points a new
/// platform usually changes marked in place (AFC-444).
///
/// The marks are reminders for onboarding,  and the caption says so in words:  they do
/// not limit where a platform may need edits,  and the whole file still needs review.
/// The preview changes nothing;  it renders the same text `ConfigFileReader` read,  with
/// `PlatformVariants` deciding what carries a mark.
struct InstructionFilePreview: View {
    /// The file's whole text,  as the reader read it.
    let text: String
    /// The platform whose file this is.
    let platform: String
    /// The seat tags the marks cover;  the file's own seat's tag is the one that appears.
    let seatTags: [String]
    /// True when the reader capped the file,  so the marks describe the shown part.
    let truncated: Bool

    private var analysis: PlatformVariants.Analysis {
        PlatformVariants.analyze(text, platform: platform, seatTags: seatTags)
    }

    var body: some View {
        let analysis = analysis
        VStack(alignment: .leading, spacing: 8) {
            Text(PlatformVariants.guidance)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            legend
            if truncated {
                Text("The file is longer than the panel reads, so this is its first part.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            summary(analysis)
            ScrollView(.horizontal, showsIndicators: true) {
                lines(analysis)
                    .padding(10)
            }
            .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
        }
    }

    /// The words for each mark,  so no mark means anything by color alone.
    private var legend: some View {
        Text("Marks:  placeholder,  seat tag,  config folder,  platform name.")
            .font(.caption2)
            .foregroundStyle(.secondary)
    }

    private func summary(_ analysis: PlatformVariants.Analysis) -> some View {
        let changing = analysis.changingLineNumbers.count
        let words: String
        if analysis.sections.isEmpty {
            words = changing == 1 ? "1 line usually changes." : "\(changing) lines usually change."
        } else {
            let sectionWord = analysis.sections.count == 1 ? "section" : "sections"
            words = "\(changing) \(changing == 1 ? "line" : "lines") usually change, in \(analysis.sections.count) marked \(sectionWord):  "
                + analysis.sections.map(\.title).joined(separator: ", ") + "."
        }
        return Text(words)
            .font(.caption2)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func lines(_ analysis: PlatformVariants.Analysis) -> some View {
        let spansByLine = Dictionary(uniqueKeysWithValues: analysis.lines.map { ($0.number, $0) })
        let all = text.components(separatedBy: "\n")
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(all.enumerated()), id: \.offset) { index, raw in
                lineText(raw, marked: spansByLine[index])
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// One line as text,  with each marked span carrying its own background.
    private func lineText(_ line: String, marked: PlatformVariants.MarkedLine?) -> Text {
        guard let marked, !marked.spans.isEmpty else {
            return Text(line.isEmpty ? " " : line)
        }
        var result = AttributedString()
        var cursor = line.startIndex
        for span in marked.spans {
            guard let range = Range(span.range, in: line) else { continue }
            if range.lowerBound > cursor {
                result += AttributedString(String(line[cursor..<range.lowerBound]))
            }
            var piece = AttributedString(String(line[range]))
            piece.backgroundColor = Self.background(for: span.kind)
            result += piece
            cursor = range.upperBound
        }
        if cursor < line.endIndex {
            result += AttributedString(String(line[cursor...]))
        }
        return Text(result)
    }

    /// The mark for each kind.  The words in the legend carry the meaning;  the color
    /// only repeats them.
    private static func background(for kind: PlatformVariants.Span.Kind) -> Color {
        switch kind {
        case .placeholder: return Color.orange.opacity(0.30)
        case .seatTag: return Color.blue.opacity(0.25)
        case .configFolder: return Color.teal.opacity(0.30)
        case .platformName: return Color.gray.opacity(0.22)
        }
    }
}
