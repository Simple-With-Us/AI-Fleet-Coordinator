import Foundation

/// What this copy of Muster was built from,  read from its own Info.plist.
///
/// The build stamps three keys of its own through `Support/Info.plist`:  the commit,  the
/// commit's time,  and where the build came from.  `script/release_build.sh` (the release CI
/// runs) and `script/build_and_run.sh` (the developer route) both pass them to Xcode.  A build
/// that passed none of them,  such as the unsigned CI test build or a plain Xcode run,  reads
/// as not recorded,  never as a commit.
struct BuildIdentity: Equatable {
    /// Where a build came from.
    enum Channel: String, Equatable, CaseIterable {
        /// Signed,  notarized and published by the release workflow from main.
        case release
        /// Built on a Mac by hand from a commit that is on main,  with nothing uncommitted.
        case main
        /// Built by hand from a commit that is not on main,  or with uncommitted changes.
        case lane
        /// The Debug configuration,  with its own bundle identifier.
        case development
    }

    static let commitKey = "MusterCommit"
    static let commitTimeKey = "MusterCommitTime"
    static let channelKey = "MusterBuildChannel"

    let marketingVersion: String?
    let buildNumber: String?
    /// The full commit,  or nil when the build did not record a well-formed one.
    let commit: String?
    let commitTime: Date?
    let channel: Channel?

    init(info: [String: Any]?) {
        marketingVersion = Self.text(info?["CFBundleShortVersionString"])
        buildNumber = Self.text(info?["CFBundleVersion"])
        let rawCommit = Self.text(info?[Self.commitKey])?.lowercased()
        commit = rawCommit.flatMap { CheckoutRevision.isCommit($0) ? $0 : nil }
        commitTime = Self.text(info?[Self.commitTimeKey]).flatMap { ISO8601DateFormatter().date(from: $0) }
        channel = Self.text(info?[Self.channelKey]).flatMap(Channel.init(rawValue:))
    }

    /// The running app.
    static var current: BuildIdentity { BuildIdentity(info: Bundle.main.infoDictionary) }

    /// The first seven characters of the commit,  as git shows it.
    var shortCommit: String? { commit.map { String($0.prefix(7)) } }

    /// The words that mark a build that is not the delivered one,  or nil for a build that is.
    var marker: String? {
        guard commit != nil else { return "Commit Not Recorded" }
        switch channel {
        case .release, .main: return nil
        case .lane: return "Not From Main"
        case .development: return "Development Build"
        case nil: return "Build Source Not Recorded"
        }
    }

    private static func text(_ value: Any?) -> String? {
        guard let string = value as? String else { return nil }
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// The commit a git checkout is on,  read from its files without running git.
///
/// Muster runs its Python from a checkout on the Mac,  and that checkout moves on its own
/// track,  so its commit is shown beside the app's.  Reading the files costs a few small
/// reads and starts no process,  so the menu can do it every time it opens.
struct CheckoutRevision: Equatable {
    let commit: String
    /// The branch HEAD names,  or nil when HEAD is detached.
    let branch: String?

    var shortCommit: String { String(commit.prefix(7)) }

    /// True for a full 40-character lowercase hexadecimal commit.
    static func isCommit(_ text: String) -> Bool {
        text.count == 40 && text.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }

    /// What reading one checkout found.
    struct Reading: Equatable {
        let root: URL
        let revision: CheckoutRevision?
        /// Why the commit could not be read,  when it could not.
        let reason: String?
    }

    /// The largest file this reads.  A packed-refs file is the only one that grows.
    static let maximumFileBytes = 8 * 1024 * 1024

    static func read(root: URL, fileManager: FileManager = .default) -> Reading {
        func failed(_ reason: String) -> Reading { Reading(root: root, revision: nil, reason: reason) }
        let dotGit = root.appendingPathComponent(".git")
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: dotGit.path, isDirectory: &isDirectory) else {
            return failed("\(root.path) is not a git checkout")
        }
        let gitDirectory: URL
        if isDirectory.boolValue {
            gitDirectory = dotGit
        } else {
            // A linked worktree's .git is a file that names its own git directory.
            guard let text = readSmall(dotGit), text.hasPrefix("gitdir:") else {
                return failed("the .git file in \(root.path) names no git directory")
            }
            gitDirectory = resolve(String(text.dropFirst("gitdir:".count)), against: root)
        }
        let commonDirectory = readSmall(gitDirectory.appendingPathComponent("commondir"))
            .map { resolve($0, against: gitDirectory) } ?? gitDirectory
        guard let head = readSmall(gitDirectory.appendingPathComponent("HEAD")) else {
            return failed("the checkout's HEAD could not be read")
        }
        if head.hasPrefix("ref:") {
            let ref = head.dropFirst("ref:".count).trimmingCharacters(in: .whitespaces)
            guard ref.hasPrefix("refs/"), !ref.contains(".."), !ref.contains("\n") else {
                return failed("the checkout's HEAD names an unexpected ref")
            }
            let branch = ref.hasPrefix("refs/heads/") ? String(ref.dropFirst("refs/heads/".count)) : ref
            for directory in [gitDirectory, commonDirectory] {
                if let value = readSmall(directory.appendingPathComponent(ref))?.lowercased(), isCommit(value) {
                    return Reading(root: root, revision: CheckoutRevision(commit: value, branch: branch), reason: nil)
                }
            }
            if let packed = readSmall(commonDirectory.appendingPathComponent("packed-refs")) {
                for line in packed.split(separator: "\n") {
                    let parts = line.split(separator: " ", maxSplits: 1)
                    guard parts.count == 2, String(parts[1]) == ref else { continue }
                    let value = String(parts[0]).lowercased()
                    if isCommit(value) {
                        return Reading(root: root, revision: CheckoutRevision(commit: value, branch: branch), reason: nil)
                    }
                }
            }
            return failed("the branch \(branch) points at no commit that could be read")
        }
        let detached = head.lowercased()
        guard isCommit(detached) else { return failed("the checkout's HEAD names no commit") }
        return Reading(root: root, revision: CheckoutRevision(commit: detached, branch: nil), reason: nil)
    }

    /// The checkouts Muster runs Python from,  without repeats:  the repository root in
    /// Settings first,  then the engine checkout the grid uses when that is a different one.
    static func musterCheckouts(
        location: FleetConfigLocation = .resolve(),
        engineRoot: URL? = EngineLocator.locate()?.root
    ) -> [URL] {
        var roots = [location.repoRoot.standardizedFileURL]
        if let engineRoot {
            let standardized = engineRoot.standardizedFileURL
            if standardized.path != roots[0].path { roots.append(standardized) }
        }
        return roots
    }

    private static func resolve(_ raw: String, against base: URL) -> URL {
        let path = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if path.hasPrefix("/") { return URL(fileURLWithPath: path).standardizedFileURL }
        return base.appendingPathComponent(path).standardizedFileURL
    }

    /// The trimmed text of a small regular file,  or nil.
    private static func readSmall(_ url: URL) -> String? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              (attributes[.type] as? FileAttributeType) == .typeRegular,
              let size = (attributes[.size] as? NSNumber)?.intValue, size <= maximumFileBytes,
              let data = try? Data(contentsOf: url) else { return nil }
        let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}

/// The checkout readings the menu shows, read from disk at most once in `maximumAge`.
///
/// The menu's body can be built many times while the app publishes changes, and each build
/// would otherwise repeat the file reads on the main thread.
@MainActor
final class CheckoutReadingCache {
    static let shared = CheckoutReadingCache()
    static let maximumAge: TimeInterval = 30

    private let read: () -> [CheckoutRevision.Reading]
    private let now: () -> Date
    private var cached: (at: Date, readings: [CheckoutRevision.Reading])?

    init(
        read: @escaping () -> [CheckoutRevision.Reading] = {
            CheckoutRevision.musterCheckouts().map { CheckoutRevision.read(root: $0) }
        },
        now: @escaping () -> Date = { Date() }
    ) {
        self.read = read
        self.now = now
    }

    func readings() -> [CheckoutRevision.Reading] {
        let moment = now()
        if let cached, moment >= cached.at, moment.timeIntervalSince(cached.at) < Self.maximumAge {
            return cached.readings
        }
        let fresh = read()
        cached = (moment, fresh)
        return fresh
    }
}

/// The words Settings and the menu show about the build and the checkout.
enum BuildIdentityCopy {
    /// The gap after a sentence:  a real no-break space and then a space.
    static let gap = "\u{00A0} "

    /// `Muster 0.1.0, Build 202611051530, Commit abc1234`.
    static func versionLine(_ identity: BuildIdentity) -> String {
        let version = identity.marketingVersion ?? "Version Unknown"
        let build = identity.buildNumber.map { "Build \($0)" } ?? "Build Unknown"
        let commit = identity.shortCommit.map { "Commit \($0)" } ?? "Commit Unknown"
        return "Muster \(version), \(build), \(commit)"
    }

    /// `0.1.0, Build 202611051530`,  for the Version row in Settings.
    static func versionValue(_ identity: BuildIdentity) -> String {
        let parts = [identity.marketingVersion, identity.buildNumber.map { "Build \($0)" }].compactMap { $0 }
        return parts.isEmpty ? "Not recorded in this build" : parts.joined(separator: ", ")
    }

    /// One phrase for where the build came from.
    static func source(_ identity: BuildIdentity) -> String {
        switch identity.channel {
        case .release: return "Released by CI from main"
        case .main: return "Built by hand on a Mac from main"
        case .lane: return "Not From Main:  built from a lane or with uncommitted changes"
        case .development: return "Development Build"
        case nil: return "Not recorded in this build"
        }
    }

    /// `abc1234, committed Nov 5, 2026 at 9:30 AM`.
    static func commitLine(_ identity: BuildIdentity) -> String {
        guard let short = identity.shortCommit else { return "Not recorded in this build" }
        guard let time = identity.commitTime else { return short }
        return "\(short), committed \(day(time))"
    }

    /// `/Users/jay/Code/AI-Fleet-Coordinator at def5678 on main`.
    static func checkoutLine(_ reading: CheckoutRevision.Reading) -> String {
        guard let revision = reading.revision else {
            return "\(reading.root.path):  commit unknown,  \(reading.reason ?? "it could not be read")"
        }
        let branch = revision.branch.map { " on \($0)" } ?? ", HEAD detached"
        return "\(reading.root.path) at \(revision.shortCommit)\(branch)"
    }

    /// Says plainly whether the app and the checkout it runs Python from are at one commit.
    static func comparison(app: BuildIdentity, checkout: CheckoutRevision.Reading) -> String {
        guard let appCommit = app.commit else {
            return "This build does not record its commit,  so it cannot be compared with the checkout."
        }
        guard let revision = checkout.revision else {
            return "The checkout's commit could not be read,  so it cannot be compared with the app."
        }
        let appShort = String(appCommit.prefix(7))
        if revision.commit == appCommit {
            return "The app and the checkout it runs Python from are both at \(appShort)."
        }
        return [
            "The app was built from \(appShort),  and the checkout it runs Python from is at \(revision.shortCommit).",
            "They update separately,  so the checkout can be ahead of the app or behind it.",
            "A report the checkout writes in a newer format waits for the app to catch up.",
        ].joined(separator: gap)
    }

    /// The short form the menu shows.
    static func menuCheckoutLine(app: BuildIdentity, checkout: CheckoutRevision.Reading) -> String {
        guard let revision = checkout.revision else { return "Checkout Commit Unknown" }
        guard let appCommit = app.commit else { return "Checkout At \(revision.shortCommit), App Commit Unknown" }
        if appCommit == revision.commit {
            return "Checkout At \(revision.shortCommit), Same As The App"
        }
        return "Checkout At \(revision.shortCommit), Not The App's Commit"
    }

    /// `Nov 5, 2026 at 9:30 AM`,  on Jay's clock,  with no zone written beside it.
    static func day(_ date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Chicago") ?? .current
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let month = months[max(0, min(11, (parts.month ?? 1) - 1))]
        return "\(month) \(parts.day ?? 1), \(parts.year ?? 1970) at \(FleetTime.clock(date))"
    }
}
