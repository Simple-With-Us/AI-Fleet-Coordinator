import Foundation

/// A script in the owner's configured checkout, checked before the app runs it.
///
/// The app runs the owner's own checkout, like its other engines;  this is not a sandbox for
/// arbitrary Python supplied by a caller.  The check protects the script, its folder and the
/// checkout's folders from replacement by another user:  each must be owned by this user or by
/// root and must not be writable by anyone else (a sticky folder, like `/tmp`, is allowed as a
/// parent).
enum TrustedCheckout {
    enum Lookup: Equatable {
        /// The checkout's real path, and the script inside it.
        case found(root: URL, script: URL)
        /// The checkout is not there.  The path is where the script would have been.
        case missing(path: String)
        /// Something on the way to the script is not safe to run.
        case unsafe(path: String)
    }

    /// Finds `script` (a path relative to the checkout, such as `scripts/tool.py`) and checks it.
    static func lookup(repoRoot: URL, script relative: String, fileManager: FileManager = .default) -> Lookup {
        guard let resolvedPath = realpath(repoRoot.path, nil) else {
            return .missing(path: repoRoot.appendingPathComponent(relative).path)
        }
        let root = URL(fileURLWithPath: String(cString: resolvedPath), isDirectory: true)
        free(resolvedPath)
        let script = root.appendingPathComponent(relative)
        let folder = script.deletingLastPathComponent()

        func trusted(_ url: URL, type: FileAttributeType, stickyParent: Bool = false) -> Bool {
            guard let attributes = try? fileManager.attributesOfItem(atPath: url.path),
                  attributes[.type] as? FileAttributeType == type,
                  let owner = attributes[.ownerAccountID] as? NSNumber,
                  owner.uint32Value == getuid() || owner.uint32Value == 0,
                  let permissions = attributes[.posixPermissions] as? NSNumber else { return false }
            return permissions.intValue & 0o022 == 0
                || (stickyParent && permissions.intValue & 0o1000 != 0)
        }

        var parent = root.deletingLastPathComponent()
        var trustedParents = true
        while parent.path != "/" {
            if !trusted(parent, type: .typeDirectory, stickyParent: true) { trustedParents = false; break }
            parent.deleteLastPathComponent()
        }
        guard trusted(root, type: .typeDirectory), trusted(folder, type: .typeDirectory),
              trusted(script, type: .typeRegular), trustedParents else {
            return .unsafe(path: script.path)
        }
        return .found(root: root, script: script)
    }
}
