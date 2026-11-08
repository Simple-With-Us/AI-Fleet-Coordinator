import XCTest

/// The instruction-preview marks (AFC-444):  what the analysis finds,  what it refuses
/// to call a customization point,  and the words that keep the marks reminders and not
/// limits.
final class PlatformVariantsTests: XCTestCase {
    /// The shape of a real instruction file,  shrunk:  shared words,  a platform-bound
    /// section,  and a shared section after it.
    private let fixture = [
        "<!-- fleet-lane-map:begin v2 -->",
        "## Fleet Basics",
        "",
        "Every platform reads the same words,  and Claude files its worktrees the same way Codex does.",
        "",
        "### Seat Identity",
        "",
        "Your default seat is ZC, for Z Code desktop IDE sessions (bot zc-bot@, credential Infisical Fleet-OPS `/zulip` `ZC_ZULIP_*`).",
        "Check your seat with `simple-chat whoami --as <SEAT>` before your first fleet action.",
        "The config lives in ~/.zcode/ and its CLI reads ~/.claude/ shapes it does not use.",
        "",
        "### Shared Rules",
        "",
        "Never print a credential.",
    ].joined(separator: "\n")

    private func spans(onLine number: Int, of text: String = "", platform: String = "zcode",
                       seatTags: [String] = ["ZC"]) -> [PlatformVariants.Span] {
        let analysis = PlatformVariants.analyze(text.isEmpty ? fixture : text, platform: platform, seatTags: seatTags)
        return analysis.lines.first(where: { $0.number == number })?.spans ?? []
    }

    private func substring(_ line: String, _ span: PlatformVariants.Span) -> String {
        (line as NSString).substring(with: span.range)
    }

    private func fixtureLine(_ number: Int) -> String {
        fixture.components(separatedBy: "\n")[number]
    }

    // ---- placeholders ----

    func testPlaceholderIsMarkedWithItsWholeAngleBrackets() {
        let line = fixtureLine(8)
        let hits = spans(onLine: 8).filter { $0.kind == .placeholder }
        XCTAssertEqual(hits.count, 1)
        XCTAssertEqual(substring(line, hits[0]), "<SEAT>")
    }

    func testAMultibyteLineKeepsItsOffsets() {
        let line = "说明 ✅ before <SEAT> after"
        let hits = spans(onLine: 0, of: line).filter { $0.kind == .placeholder }
        XCTAssertEqual(hits.count, 1)
        XCTAssertEqual(substring(line, hits[0]), "<SEAT>")
    }

    func testAngleBracketsAroundSentencesAreNotPlaceholders() {
        let hits = spans(onLine: 0, of: "a < b and c > d, plus <not a placeholder!>")
        XCTAssertTrue(hits.filter { $0.kind == .placeholder }.isEmpty)
    }

    // ---- seat tags ----

    func testSeatTagIsMarkedStandaloneAndInsideAnIdentifier() {
        let line = fixtureLine(7)
        let hits = spans(onLine: 7).filter { $0.kind == .seatTag }
        XCTAssertEqual(hits.count, 2)
        XCTAssertEqual(substring(line, hits[0]), "ZC")
        XCTAssertEqual(substring(line, hits[1]), "ZC")
    }

    func testLowercaseTagIsNotTheSeatTag() {
        XCTAssertTrue(spans(onLine: 0, of: "the zc bot is not the tag", seatTags: ["ZC"]).filter { $0.kind == .seatTag }.isEmpty)
    }

    // ---- config folders ----

    func testConfigFoldersAreMarkedAndALongerFolderIsNot() {
        let line = fixtureLine(9)
        let hits = spans(onLine: 9).filter { $0.kind == .configFolder }
        XCTAssertEqual(hits.count, 2)
        XCTAssertEqual(substring(line, hits[0]), "~/.zcode/")
        XCTAssertEqual(substring(line, hits[1]), "~/.claude/")
        let decoy = "backup lives in ~/.claude-backup/ today"
        XCTAssertTrue(spans(onLine: 0, of: decoy).filter { $0.kind == .configFolder }.isEmpty)
    }

    // ---- platform names are weak marks ----

    func testAPlatformNameInSharedProseIsMarkedButTheLineStillReadsShared() {
        let line = fixtureLine(3)
        let names = spans(onLine: 3).filter { $0.kind == .platformName }
        XCTAssertEqual(substring(line, names[0]), "Claude")
        XCTAssertEqual(substring(line, names[1]), "Codex")
        let marked = PlatformVariants.analyze(fixture, platform: "zcode", seatTags: ["ZC"])
            .lines.first(where: { $0.number == 3 })
        XCTAssertEqual(marked?.usuallyChanges, false)
    }

    func testALineWithAPlaceholderOrTagOrFolderUsuallyChanges() {
        let marked = PlatformVariants.analyze(fixture, platform: "zcode", seatTags: ["ZC"])
        XCTAssertTrue(marked.lines.first(where: { $0.number == 7 })!.usuallyChanges)
        XCTAssertTrue(marked.lines.first(where: { $0.number == 8 })!.usuallyChanges)
        XCTAssertTrue(marked.lines.first(where: { $0.number == 9 })!.usuallyChanges)
    }

    // ---- sections ----

    func testSectionsWithChangingLinesAreNamedAndSharedOnesAreAbsent() {
        let sections = PlatformVariants.analyze(fixture, platform: "zcode", seatTags: ["ZC"]).sections
        // The parent "Fleet Basics" runs through its "Seat Identity" child to the next
        // same-or-higher heading, so it counts the child's three changing lines too.
        XCTAssertEqual(sections.count, 2)
        XCTAssertEqual(sections[0].title, "Fleet Basics")
        XCTAssertEqual(sections[0].changingLines, 3)
        XCTAssertEqual(sections[1].title, "Seat Identity")
        XCTAssertEqual(sections[1].changingLines, 3)
        XCTAssertFalse(sections.contains(where: { $0.title == "Shared Rules" }))
    }

    // ---- guidance and purity ----

    func testGuidanceSaysTheMarksAreRemindersNotLimits() {
        XCTAssertTrue(PlatformVariants.guidance.contains("reminders,  not limits"))
        XCTAssertTrue(PlatformVariants.guidance.contains("whole file still needs review"))
        XCTAssertTrue(PlatformVariants.guidance.contains("read-only"))
    }

    func testTheAnalysisIsPureAndStable() {
        let first = PlatformVariants.analyze(fixture, platform: "zcode", seatTags: ["ZC"])
        let second = PlatformVariants.analyze(fixture, platform: "zcode", seatTags: ["ZC"])
        XCTAssertEqual(first, second)
    }

    func testEmptyTextAnalyzesToNothing() {
        let analysis = PlatformVariants.analyze("", platform: "zcode", seatTags: ["ZC"])
        XCTAssertTrue(analysis.lines.isEmpty)
        XCTAssertTrue(analysis.sections.isEmpty)
    }

    // ---- the review findings ----

    func testAFolderFollowedByAFileNameKeepsItsMark() {
        let line = "settings live in ~/.claude/settings.json and ~/.zcode/AGENTS.md"
        let hits = spans(onLine: 0, of: line).filter { $0.kind == .configFolder }
        XCTAssertEqual(hits.count, 2)
        XCTAssertEqual(substring(line, hits[0]), "~/.claude/")
        XCTAssertEqual(substring(line, hits[1]), "~/.zcode/")
    }

    func testTheLongestSpanWinsAtTheSameStart() {
        let line = "run it through Claude Code today"
        let names = spans(onLine: 0, of: line).filter { $0.kind == .platformName }
        XCTAssertEqual(names.count, 1)
        XCTAssertEqual(substring(line, names[0]), "Claude Code")
    }

    func testASeatTagBeatsAPlatformNameAtTheSameStart() {
        let line = "the FX seat is configured"
        let analysis = PlatformVariants.analyze(line, platform: "fx", seatTags: ["FX"])
        let marked = analysis.lines.first
        XCTAssertEqual(marked?.spans.first?.kind, .seatTag)
        XCTAssertEqual(marked?.usuallyChanges, true)
    }

    func testTheVocabularyComesFromTheSeatAppRegistry() {
        let rows = PlatformVariants.platformWords
        for (slug, meta) in FleetRegistryReader.codingAppMetadata
        where rows.contains(where: { $0.platform == slug }) {
            XCTAssertTrue(
                rows.first(where: { $0.platform == slug })!.words.contains(meta.title),
                "the row for \(slug) must name the registry title"
            )
        }
        XCTAssertTrue(rows.contains(where: { $0.platform == "gemini" }), "the manifest's gemini target keeps a row")
        XCTAssertTrue(rows.contains(where: { $0.platform == "claude-code" && $0.words.contains("Claude") }))
    }
}
