import CoreServices
import Foundation

/// Watches the paths the manifest names,  and says when one of them changes.
///
/// The watcher is the reason the app is quiet:  it waits for the file system to
/// speak,  and it does not look at anything on its own.  There is no poll of a
/// file's contents and no fast timer behind it,  so an app that is waiting for a
/// change costs nothing.
protocol DriftWatching: AnyObject {
    /// Add a stream for each of these watches,  and call back with the paths that changed.
    ///
    /// The streams the watcher already holds are left running:  a watch it holds already
    /// counts as armed and is not started again,  so no event on it is dropped.  The
    /// callback replaces the one an earlier call gave,  for every stream.
    ///
    /// The callback runs on the watcher's own queue,  not on the main one,  and it
    /// must not block:  the file system daemon is delivering events here.
    ///
    /// - Returns:  The watches that hold a stream now,  in the order given.  A watch whose
    ///   stream could not be made is left out,  and the caller asks for it again later.
    @discardableResult
    func arm(_ watches: [DriftWatchList.Watch], onChange: @escaping ([URL]) -> Void) -> [DriftWatchList.Watch]
    /// Stop and release the streams of these watches,  and leave every other stream running.
    func disarm(_ watches: [DriftWatchList.Watch])
    /// Stop watching and release every stream.
    func stop()
}

/// The watcher built on FSEvents.
///
/// One stream per watched path,  all on one queue,  so the events arrive in the
/// order the file system saw them.
final class FSEventsDriftWatcher: DriftWatching {
    /// How long the daemon may gather events before it delivers them.
    ///
    /// Half a second keeps the notice short without asking the daemon for every
    /// single write:  a file that is written three times in a row is one change.
    static let latency: CFTimeInterval = 0.5

    /// A stream the watcher holds,  with the watch it stands for.
    private struct ArmedStream {
        let watch: DriftWatchList.Watch
        let stream: FSEventStreamRef
    }

    private var streams: [ArmedStream] = []
    private var context: WatchContext?
    private let queue: DispatchQueue

    init(queue: DispatchQueue = DispatchQueue(label: "muster.drift.fsevents", qos: .utility)) {
        self.queue = queue
    }

    deinit {
        stop()
    }

    @discardableResult
    func arm(
        _ watches: [DriftWatchList.Watch],
        onChange: @escaping ([URL]) -> Void
    ) -> [DriftWatchList.Watch] {
        let context = self.context ?? WatchContext()
        self.context = context
        context.setHandler(onChange)
        var armed: [DriftWatchList.Watch] = []
        for watch in watches {
            if streams.contains(where: { $0.watch == watch }) {
                armed.append(watch)
                continue
            }
            guard let stream = makeStream(for: watch, context: context) else { continue }
            context.set(watch, for: stream)
            streams.append(ArmedStream(watch: watch, stream: stream))
            armed.append(watch)
        }
        return armed
    }

    func disarm(_ watches: [DriftWatchList.Watch]) {
        guard !watches.isEmpty else { return }
        var kept: [ArmedStream] = []
        for entry in streams {
            if watches.contains(entry.watch) {
                context?.remove(entry.stream)
                release(entry.stream)
            } else {
                kept.append(entry)
            }
        }
        streams = kept
    }

    func stop() {
        for entry in streams {
            release(entry.stream)
        }
        streams.removeAll()
        context?.clear()
        context = nil
    }

    /// Stop one stream and let it go.
    private func release(_ stream: FSEventStreamRef) {
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
    }

    /// One stream,  watching one path.
    private func makeStream(
        for watch: DriftWatchList.Watch,
        context: WatchContext
    ) -> FSEventStreamRef? {
        var info = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(context).toOpaque(),
            retain: nil,
            release: nil,
            copyDescription: nil
        )
        let flags = FSEventStreamCreateFlags(
            kFSEventStreamCreateFlagFileEvents
                | kFSEventStreamCreateFlagUseCFTypes
                | kFSEventStreamCreateFlagNoDefer
        )
        let stream = FSEventStreamCreate(
            kCFAllocatorDefault,
            FSEventsDriftWatcher.callback,
            &info,
            [watch.root.path] as CFArray,
            FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            FSEventsDriftWatcher.latency,
            flags
        )
        guard let stream else { return nil }
        FSEventStreamSetDispatchQueue(stream, queue)
        FSEventStreamStart(stream)
        return stream
    }

    /// The daemon's callback,  which arrives on the watcher's queue.
    ///
    /// The context is held for the length of the call,  so a watcher that is let go
    /// while this runs cannot pull the ground out from under it.
    private static let callback: FSEventStreamCallback = {
        stream, info, count, eventPaths, _, _ in
        guard let info else { return }
        let unmanaged = Unmanaged<WatchContext>.fromOpaque(info)
        unmanaged.retain()
        defer { unmanaged.release() }
        let context = unmanaged.takeUnretainedValue()
        guard let watch = context.watch(for: stream) else { return }

        let array = unsafeBitCast(eventPaths, to: NSArray.self)
        var relevant: [URL] = []
        for index in 0..<count {
            guard index < array.count, let text = array[index] as? String else { continue }
            let url = URL(fileURLWithPath: text)
            guard DriftEventFilter.isRelevant(event: url, watch: watch) else { continue }
            relevant.append(url)
        }
        guard !relevant.isEmpty else { return }
        context.deliver(relevant)
    }
}

/// What the daemon's callback needs:  the watch behind a stream,  and the way to
/// hand a change on.
final class WatchContext {
    /// The handler the monitor gave the watcher.
    ///
    /// It is set on the main thread and read on the watcher's queue,  so it is held
    /// under the lock.
    private var handler: ([URL]) -> Void = { _ in }
    /// The watches,  by the stream that is speaking.
    private let lock = NSLock()
    private var watches: [UInt: DriftWatchList.Watch] = [:]

    /// Set the handler a change is handed to.
    func setHandler(_ handler: @escaping ([URL]) -> Void) {
        lock.lock()
        self.handler = handler
        lock.unlock()
    }

    /// Hand a change on.  The handler is called outside the lock.
    func deliver(_ paths: [URL]) {
        lock.lock()
        let handler = self.handler
        lock.unlock()
        handler(paths)
    }

    /// Forget the watch behind a stream that is let go.
    func remove(_ stream: FSEventStreamRef) {
        lock.lock()
        watches[UInt(bitPattern: stream)] = nil
        lock.unlock()
    }
    /// The watch behind a stream.
    func watch(for stream: FSEventStreamRef) -> DriftWatchList.Watch? {
        lock.lock()
        defer { lock.unlock() }
        return watches[UInt(bitPattern: stream)]
    }

    /// The watch behind the stream that is speaking.
    func set(_ watch: DriftWatchList.Watch, for stream: FSEventStreamRef) {
        lock.lock()
        watches[UInt(bitPattern: stream)] = watch
        lock.unlock()
    }

    /// Forget every watch.
    func clear() {
        lock.lock()
        watches.removeAll()
        lock.unlock()
    }
}
