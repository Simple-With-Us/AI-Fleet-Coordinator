import Foundation

// The engine is Python code that Muster runs, and a hooks apply installs hook code under a Touch
// ID prompt.  So the repository root that supplies that code is a trust decision, and it is
// checked before every engine run, not only before an apply.  A root is accepted only when all of
// these hold:
//   * the root, root/scripts and root/scripts/fleet_config are real directories (not symlinks)
//     owned by the current user and not group- or world-writable
//   * the root is the top of a git work tree whose origin is Simple-With-Us/AI-Fleet-Coordinator
//   * the work tree is clean (no modified, staged or untracked files)
//   * HEAD is reachable from refs/remotes/origin/main
//
// What this does not do:  it does not fetch, so refs/remotes/origin/main is whatever the checkout
// last saw, and a same-user process that can rewrite that ref or the checkout between this check
// and the engine start is not stopped.  It narrows who can supply engine code to a clean checkout
// of the fleet repository at a commit already on main.

struct FleetConfigRootStatus: Equatable, Sendable {
    /// The resolved (symlink-free) path of the validated root.
    var path: String
    /// The full HEAD commit.
    var head: String
    /// How many commits origin/main has that HEAD does not.
    var behind: Int

    var shortHead: String { String(head.prefix(9)) }
}

enum FleetConfigRootError: Error, Equatable, LocalizedError {
    case untrusted(String)

    var errorDescription: String? {
        switch self {
        case .untrusted(let reason):
            return ApplyCopy.join("The fleet-config repository is not trusted, so nothing was run.", reason)
        }
    }
}

enum FleetConfigRootValidator {
    static let git = "/usr/bin/git"
    static let expectedRepository = "simple-with-us/ai-fleet-coordinator"

    /// Full validation.  Blocking:  call from a background queue.
    static func validate(
        _ location: FleetConfigLocation,
        fileManager: FileManager = .default,
        userID: uid_t = getuid()
    ) throws -> FleetConfigRootStatus {
        let root = location.repoRoot.resolvingSymlinksInPath().standardizedFileURL
        try checkDirectories(root: root, fileManager: fileManager, userID: userID)
        let engineFile = root.appendingPathComponent("scripts/fleet_config/__main__.py")
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: engineFile.path, isDirectory: &isDirectory), !isDirectory.boolValue else {
            throw FleetConfigRootError.untrusted("The engine entry point is missing at \(engineFile.path).")
        }

        let toplevel = try gitOutput(root, ["rev-parse", "--show-toplevel"])
        guard URL(fileURLWithPath: toplevel).resolvingSymlinksInPath().path == root.path else {
            throw FleetConfigRootError.untrusted("The path is not the top of a git work tree.")
        }
        let origin = try gitOutput(root, ["config", "--get", "remote.origin.url"])
        guard isExpectedOrigin(origin) else {
            throw FleetConfigRootError.untrusted("The origin remote is not the fleet repository.")
        }
        let head = try gitOutput(root, ["rev-parse", "HEAD"])
        guard head.range(of: "^[0-9a-f]{40}$", options: .regularExpression) != nil else {
            throw FleetConfigRootError.untrusted("HEAD is not a commit.")
        }
        let dirty = try gitOutput(root, ["status", "--porcelain", "--untracked-files=all"])
        guard dirty.isEmpty else {
            throw FleetConfigRootError.untrusted("The work tree has uncommitted or untracked files.")
        }
        let ancestor = try gitRun(root, ["merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"])
        switch ancestor.exitCode {
        case 0: break
        case 1: throw FleetConfigRootError.untrusted("HEAD is not on origin/main.")
        default: throw FleetConfigRootError.untrusted("origin/main could not be read in this checkout.")
        }
        let behindText = try gitOutput(root, ["rev-list", "--count", "HEAD..refs/remotes/origin/main"])
        return FleetConfigRootStatus(path: root.path, head: head, behind: Int(behindText) ?? 0)
    }

    /// Accepts the https, scp-style and ssh forms of the fleet repository, with or without `.git`.
    /// A URL with embedded credentials, another host, another owner or another name is rejected.
    static func isExpectedOrigin(_ raw: String) -> Bool {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        while text.hasSuffix("/") { text.removeLast() }
        if text.hasSuffix(".git") { text.removeLast(4) }
        let accepted = [
            "https://github.com/" + expectedRepository,
            "git@github.com:" + expectedRepository,
            "ssh://git@github.com/" + expectedRepository,
        ]
        return accepted.contains(text)
    }

    // MARK: Directory checks

    private static func checkDirectories(root: URL, fileManager: FileManager, userID: uid_t) throws {
        let paths = [
            root,
            root.appendingPathComponent("scripts", isDirectory: true),
            root.appendingPathComponent("scripts/fleet_config", isDirectory: true),
        ]
        for url in paths {
            let attributes: [FileAttributeKey: Any]
            do {
                attributes = try fileManager.attributesOfItem(atPath: url.path)
            } catch {
                throw FleetConfigRootError.untrusted("\(url.path) could not be inspected.")
            }
            guard (attributes[.type] as? FileAttributeType) == .typeDirectory else {
                throw FleetConfigRootError.untrusted("\(url.path) is not a plain directory.")
            }
            guard let owner = attributes[.ownerAccountID] as? NSNumber, owner.uint32Value == userID else {
                throw FleetConfigRootError.untrusted("\(url.path) is not owned by the current user.")
            }
            let mode = (attributes[.posixPermissions] as? NSNumber)?.uint16Value ?? 0o777
            guard mode & 0o022 == 0 else {
                throw FleetConfigRootError.untrusted("\(url.path) is writable by its group or by others.")
            }
        }
    }

    // MARK: Git

    private static func gitRun(_ root: URL, _ arguments: [String]) throws -> FleetConfigCLIResult {
        // No optional locks and no fsmonitor hook:  reading a checkout must not run its code.
        let base = ["-C", root.path, "--no-optional-locks", "-c", "core.fsmonitor=false"]
        do {
            return try FleetConfigProcess.run(
                executable: git,
                arguments: base + arguments,
                currentDirectory: root,
                environment: FleetConfigCLI.childEnvironment(from: ProcessInfo.processInfo.environment),
                timeout: 30
            )
        } catch {
            throw FleetConfigRootError.untrusted("git could not be run: \(error.localizedDescription)")
        }
    }

    private static func gitOutput(_ root: URL, _ arguments: [String]) throws -> String {
        let result = try gitRun(root, arguments)
        guard result.exitCode == 0, !result.timedOut else {
            let detail = result.stderrTail.isEmpty ? "git exited \(result.exitCode)." : result.stderrTail
            throw FleetConfigRootError.untrusted("git \(arguments.first ?? "") failed: \(detail)")
        }
        return result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
