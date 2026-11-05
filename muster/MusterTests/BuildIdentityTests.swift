import XCTest

final class BuildIdentityTests: XCTestCase {
    private let commit = "0123456789abcdef0123456789abcdef01234567"
    private let other = "fedcba9876543210fedcba9876543210fedcba98"

    private func info(channel: String? = "release", commit: String? = nil,
                      time: String? = "2026-11-05T15:30:00Z") -> [String: Any] {
        var info: [String: Any] = [
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "202611051530",
        ]
        info[BuildIdentity.commitKey] = commit ?? self.commit
        if let channel { info[BuildIdentity.channelKey] = channel }
        if let time { info[BuildIdentity.commitTimeKey] = time }
        return info
    }

    // MARK: - Reading the stamp

    func testAReleaseBuildReadsItsStampAndCarriesNoMarker() {
        let identity = BuildIdentity(info: info())
        XCTAssertEqual(identity.commit, commit)
        XCTAssertEqual(identity.shortCommit, "0123456")
        XCTAssertEqual(identity.channel, .release)
        XCTAssertNil(identity.marker)
        XCTAssertEqual(BuildIdentityCopy.versionLine(identity), "Muster 0.1.0, Build 202611051530, Commit 0123456")
        XCTAssertEqual(BuildIdentityCopy.versionValue(identity), "0.1.0, Build 202611051530")
    }

    func testTheCommitTimeIsShownOnJaysClockWithNoZone() {
        let identity = BuildIdentity(info: info())
        XCTAssertEqual(BuildIdentityCopy.commitLine(identity), "0123456, committed Nov 5, 2026 at 9:30 AM")
    }

    func testALaneAndADevelopmentBuildAreMarked() {
        XCTAssertEqual(BuildIdentity(info: info(channel: "lane")).marker, "Not From Main")
        XCTAssertEqual(BuildIdentity(info: info(channel: "development")).marker, "Development Build")
        XCTAssertNil(BuildIdentity(info: info(channel: "main")).marker)
        XCTAssertEqual(BuildIdentity(info: info(channel: nil)).marker, "Build Source Not Recorded")
    }

    /// The unsigned CI build and a plain Xcode run pass no stamp:  the build setting expands to
    /// an empty string,  which is not a commit and is never shown as one.
    func testAnUnstampedBuildReadsAsNotRecordedNeverAsACommit() {
        let identity = BuildIdentity(info: info(channel: "", commit: "", time: ""))
        XCTAssertNil(identity.commit)
        XCTAssertNil(identity.channel)
        XCTAssertEqual(identity.marker, "Commit Not Recorded")
        XCTAssertEqual(BuildIdentityCopy.versionLine(identity), "Muster 0.1.0, Build 202611051530, Commit Unknown")
        XCTAssertEqual(BuildIdentityCopy.commitLine(identity), "Not recorded in this build")
        XCTAssertEqual(BuildIdentityCopy.source(identity), "Not recorded in this build")
    }

    func testAMalformedCommitIsNotACommit() {
        XCTAssertNil(BuildIdentity(info: info(commit: "abc1234")).commit)
        XCTAssertNil(BuildIdentity(info: info(commit: "$(MUSTER_COMMIT)")).commit)
        XCTAssertNil(BuildIdentity(info: nil).commit)
        XCTAssertEqual(BuildIdentityCopy.versionValue(BuildIdentity(info: nil)), "Not recorded in this build")
    }

    // MARK: - The checkout beside the app

    private func reading(_ commit: String?, branch: String? = "main") -> CheckoutRevision.Reading {
        let root = URL(fileURLWithPath: "/tmp/muster-tests/checkout")
        guard let commit else { return CheckoutRevision.Reading(root: root, revision: nil, reason: "no .git") }
        return CheckoutRevision.Reading(root: root, revision: CheckoutRevision(commit: commit, branch: branch), reason: nil)
    }

    func testTheSameCommitSaysSo() {
        let identity = BuildIdentity(info: info())
        XCTAssertEqual(BuildIdentityCopy.comparison(app: identity, checkout: reading(commit)),
                       "The app and the checkout it runs Python from are both at 0123456.")
        XCTAssertEqual(BuildIdentityCopy.menuCheckoutLine(app: identity, checkout: reading(commit)),
                       "Checkout At 0123456, Same As The App")
    }

    func testDifferentCommitsSaySoPlainlyWithTheSentenceGap() {
        let identity = BuildIdentity(info: info())
        let text = BuildIdentityCopy.comparison(app: identity, checkout: reading(other))
        XCTAssertTrue(text.hasPrefix("The app was built from 0123456,  and the checkout it runs Python from is at fedcba9."))
        XCTAssertTrue(text.contains(".\u{00A0} They update separately"))
        XCTAssertEqual(BuildIdentityCopy.menuCheckoutLine(app: identity, checkout: reading(other)),
                       "Checkout At fedcba9, Not The App's Commit")
    }

    func testAnUnknownSideIsNeverReadAsTheSame() {
        let unstamped = BuildIdentity(info: info(commit: ""))
        XCTAssertEqual(BuildIdentityCopy.comparison(app: unstamped, checkout: reading(commit)),
                       "This build does not record its commit,  so it cannot be compared with the checkout.")
        XCTAssertEqual(BuildIdentityCopy.menuCheckoutLine(app: unstamped, checkout: reading(commit)),
                       "Checkout At 0123456, Not The App's Commit")
        let identity = BuildIdentity(info: info())
        XCTAssertEqual(BuildIdentityCopy.menuCheckoutLine(app: identity, checkout: reading(nil)), "Checkout Commit Unknown")
        XCTAssertTrue(BuildIdentityCopy.checkoutLine(reading(nil)).contains("commit unknown"))
    }

    // MARK: - Reading a checkout's files

    private func makeCheckout() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("checkout-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root.appendingPathComponent(".git/refs/heads"),
                                                withIntermediateDirectories: true)
        addTeardownBlock { _ = try? FileManager.default.removeItem(at: root) }
        return root
    }

    private func write(_ text: String, _ url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try text.write(to: url, atomically: true, encoding: .utf8)
    }

    func testABranchRefIsRead() throws {
        let root = try makeCheckout()
        try write("ref: refs/heads/main\n", root.appendingPathComponent(".git/HEAD"))
        try write("\(commit)\n", root.appendingPathComponent(".git/refs/heads/main"))
        let found = CheckoutRevision.read(root: root)
        XCTAssertEqual(found.revision, CheckoutRevision(commit: commit, branch: "main"))
        XCTAssertNil(found.reason)
    }

    func testAPackedRefIsRead() throws {
        let root = try makeCheckout()
        try write("ref: refs/heads/main\n", root.appendingPathComponent(".git/HEAD"))
        try write("# pack-refs with: peeled fully-peeled sorted\n\(other) refs/heads/other\n\(commit) refs/heads/main\n",
                  root.appendingPathComponent(".git/packed-refs"))
        XCTAssertEqual(CheckoutRevision.read(root: root).revision?.commit, commit)
    }

    func testADetachedHeadIsRead() throws {
        let root = try makeCheckout()
        try write("\(other)\n", root.appendingPathComponent(".git/HEAD"))
        XCTAssertEqual(CheckoutRevision.read(root: root).revision, CheckoutRevision(commit: other, branch: nil))
    }

    func testALinkedWorktreeIsReadThroughItsGitFile() throws {
        let root = try makeCheckout()
        let main = root.appendingPathComponent("main")
        let lane = root.appendingPathComponent("lane")
        let worktreeGit = main.appendingPathComponent(".git/worktrees/lane")
        try write("ref: refs/heads/claude/lane\n", worktreeGit.appendingPathComponent("HEAD"))
        try write("../..\n", worktreeGit.appendingPathComponent("commondir"))
        try write("\(other)\n", main.appendingPathComponent(".git/refs/heads/claude/lane"))
        try write("gitdir: \(worktreeGit.path)\n", lane.appendingPathComponent(".git"))
        XCTAssertEqual(CheckoutRevision.read(root: lane).revision,
                       CheckoutRevision(commit: other, branch: "claude/lane"))
    }

    func testAFolderThatIsNotACheckoutSaysWhy() throws {
        let root = try makeCheckout()
        try FileManager.default.removeItem(at: root.appendingPathComponent(".git"))
        let found = CheckoutRevision.read(root: root)
        XCTAssertNil(found.revision)
        XCTAssertEqual(found.reason, "\(root.path) is not a git checkout")
    }

    func testABranchWithNoCommitIsUnknownNotEmpty() throws {
        let root = try makeCheckout()
        try write("ref: refs/heads/main\n", root.appendingPathComponent(".git/HEAD"))
        let found = CheckoutRevision.read(root: root)
        XCTAssertNil(found.revision)
        XCTAssertEqual(found.reason, "the branch main points at no commit that could be read")
    }
}
