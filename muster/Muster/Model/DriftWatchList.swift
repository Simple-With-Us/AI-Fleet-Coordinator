import Foundation

/// The paths the manifest names,  and the watches that cover them.
///
/// The list is derived from the manifest and never written by hand:  a seat that
/// joins the fleet is watched because the manifest names its paths,  not because
/// this file was taught about the seat.  The manifest is the only place that knows
/// where a seat's config lives,  and a watcher with its own list would be a second
/// place that could be wrong.
///
/// Two facts about FSEvents shape the watches,  and both were measured on this Mac:
///
///   * a stream given a **file** reports that file alone:  a change to a sibling in
///     the same directory is not reported
///   * a stream given a **directory** reports everything under it,  recursively
///
/// So a file is watched by its own path,  which costs one watch and no scan of the
/// directory around it,  and a directory is watched by its own path,  which is what
/// a rule like `skills_installed` means.
struct DriftWatchList {
    /// One watch:  a path handed to FSEvents,  and the target it stands for.
    struct Watch: Equatable {
        /// The path handed to FSEvents.
        let root: URL
        /// The manifest path this watch stands for.
        let target: URL
        /// Whether the target is a directory,  whose contents the watch covers too.
        let targetIsDirectory: Bool
        /// Whether the root is the target itself,  or an ancestor standing in for a
        /// path that is not there yet.
        let rootIsTheTarget: Bool
    }

    /// The watches,  one per path the manifest named.
    let watches: [Watch]
    /// The distinct paths the manifest named,  in the order they were found.
    let targetPaths: [URL]
    /// The seats whose paths are watched,  in registry order of their names.
    let seats: [String]

    /// Whether there is anything to watch.
    var isEmpty: Bool { watches.isEmpty }
}

/// Turns the manifest into the list of watches.
///
/// Nothing here is written down twice:  the paths come from the manifest's rules,
/// the seats come from the manifest's roster,  and the rules the app refuses to
/// read are the rules the app refuses to watch.
enum DriftWatchListBuilder {
    /// The watches for the manifest's paths.
    ///
    /// A rule that checks a credential is not watched:  the panel refuses to read a
    /// credential,  and a watcher that reports one would be a way around the refusal.
    /// A path outside the user's own directories is not watched either,  because a
    /// seat's config does not live there and watching it would need a privilege the
    /// app does not have.
    static func build(
        manifest: SeatManifest?,
        home: URL,
        fileManager: FileManager = .default
    ) -> DriftWatchList {
        guard let manifest else { return DriftWatchList(watches: [], targetPaths: [], seats: []) }

        var paths: [URL] = []
        var seen: Set<String> = []
        var seats: Set<String> = []
        for seat in manifest.seats.keys.sorted() {
            guard let entry = manifest.seats[seat] else { continue }
            for rule in entry.rules {
                guard let check = rule.check,
                      let target = check.target,
                      !target.isEmpty else { continue }
                guard ConfigFileReader.refusalReason(target: target, checkType: check.type) == nil else {
                    continue
                }
                let url = ConfigFileReader.resolve(target: target, home: home)
                guard isInsideHome(url, home: home) else { continue }
                seats.insert(seat)
                if seen.insert(url.path).inserted { paths.append(url) }
            }
        }

        var watches: [DriftWatchList.Watch] = []
        for path in paths {
            var isDirectory: ObjCBool = false
            let exists = fileManager.fileExists(atPath: path.path, isDirectory: &isDirectory)
            let directory = exists && isDirectory.boolValue
            // The target is watched by its own path.  FSEvents accepts a path that is
            // not there yet and reports it when it appears,  so a file that is created
            // after the app started is seen without watching anything around it.
            watches.append(
                DriftWatchList.Watch(
                    root: path,
                    target: path,
                    targetIsDirectory: directory,
                    rootIsTheTarget: true
                )
            )
            guard !exists else { continue }
            // The path is not there.  Its nearest existing ancestor is watched as
            // well,  so a directory that is created and then filled is seen even when
            // the change to the file itself is not reported.
            guard let ancestor = nearestExistingAncestor(
                of: path,
                home: home,
                fileManager: fileManager
            ) else { continue }
            watches.append(
                DriftWatchList.Watch(
                    root: ancestor,
                    target: path,
                    targetIsDirectory: false,
                    rootIsTheTarget: false
                )
            )
        }

        return DriftWatchList(
            watches: watches,
            targetPaths: paths,
            seats: seats.sorted()
        )
    }

    /// Whether a path is inside the user's own directories.
    private static func isInsideHome(_ url: URL, home: URL) -> Bool {
        let path = url.standardizedFileURL.path
        let root = home.standardizedFileURL.path
        return path == root || path.hasPrefix(root + "/")
    }

    /// The nearest directory above a path that is there.
    ///
    /// The home directory and the filesystem root are refused:  FSEvents watches a
    /// directory recursively,  and those two are the largest trees on the Mac.  The
    /// watch on the path itself already covers a file that appears,  so the loss is
    /// the safety net and not the detection.
    static func nearestExistingAncestor(
        of url: URL,
        home: URL,
        fileManager: FileManager = .default
    ) -> URL? {
        var candidate = url.deletingLastPathComponent().standardizedFileURL
        let root = home.standardizedFileURL.path
        for _ in 0..<32 {
            var isDirectory: ObjCBool = false
            if fileManager.fileExists(atPath: candidate.path, isDirectory: &isDirectory),
               isDirectory.boolValue {
                let path = candidate.path
                if path == root || path == "/" { return nil }
                return candidate
            }
            let parent = candidate.deletingLastPathComponent().standardizedFileURL
            if parent.path == candidate.path { return nil }
            candidate = parent
        }
        return nil
    }
}

/// Decides whether an event on a path matters to a watch.
///
/// The filter is what keeps the app quiet:  a watch on a directory sees everything
/// under it,  and only the paths the manifest named may start a run.
enum DriftEventFilter {
    /// Whether an event matters to a watch.
    ///
    /// Three things matter:  the target itself,  anything inside it when the target
    /// is a directory,  and a directory between the watch's root and the target,  which
    /// means the target's own directory was created or removed.
    static func isRelevant(event: URL, watch: DriftWatchList.Watch) -> Bool {
        if matches(
            eventPath: event.standardizedFileURL.path,
            targetPath: watch.target.standardizedFileURL.path,
            rootPath: watch.root.standardizedFileURL.path,
            targetIsDirectory: watch.targetIsDirectory
        ) {
            return true
        }
        // A path that reaches its target through a symbolic link is reported under
        // the link's real name:  the temporary directory is `/var` and the file
        // system says `/private/var`,  and a lane's tools live on a volume behind a
        // link.  So the comparison is made a second time with both sides resolved.
        let resolvedEvent = resolve(event)
        let resolvedTarget = resolve(watch.target)
        let resolvedRoot = resolve(watch.root)
        guard resolvedEvent.path != event.path
            || resolvedTarget.path != watch.target.path
            || resolvedRoot.path != watch.root.path else { return false }
        return matches(
            eventPath: resolvedEvent.path,
            targetPath: resolvedTarget.path,
            rootPath: resolvedRoot.path,
            targetIsDirectory: watch.targetIsDirectory
        )
    }

    /// The comparison itself,  with the paths already in hand.
    private static func matches(
        eventPath: String,
        targetPath: String,
        rootPath: String,
        targetIsDirectory: Bool
    ) -> Bool {
        if eventPath == targetPath { return true }
        if targetIsDirectory, eventPath.hasPrefix(targetPath + "/") { return true }
        // A directory between the root and the target:  the path the target needs was
        // created or removed,  and the target may have come or gone with it.
        if eventPath.hasPrefix(rootPath + "/"), targetPath.hasPrefix(eventPath + "/") {
            return true
        }
        return false
    }

    /// A path with its symbolic links resolved.
    ///
    /// The last piece is kept as it is,  because a file that was just deleted is
    /// not there to be resolved and the link above it is what matters.
    private static func resolve(_ url: URL) -> URL {
        let parent = url.deletingLastPathComponent().resolvingSymlinksInPath()
        return parent.appendingPathComponent(url.lastPathComponent)
    }
}
