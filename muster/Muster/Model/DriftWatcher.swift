import CoreServices
import Foundation

/// Watches the paths the manifest names,  and says when one of them changes.
///
/// The watcher is the reason the app is quiet:  it waits for the file system to
/// speak,  and it does not look at anything on its own.  There is no poll of a
/// file's contents and no fast timer behind it,  so an app that is waiting for a
/// change costs nothing.
protocol DriftWatching: AnyObject {
    /// Watch the list,  and call back with the paths that changed.
    ///
    /// The callback runs on the watcher's own queue,  not on the main one,  and it
    /// must not block:  the file system daemon is delivering events here.
    func start(_ list: DriftWatchList, onChange: @escaping ([URL]) -> Void)
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

    private var streams: [FSEventStreamRef] = []
    private var context: WatchContext?
    private let queue: DispatchQueue

    init(queue: DispatchQueue = DispatchQueue(label: "muster.drift.fsevents", qos: .utility)) {
        self.queue = queue
    }

    deinit {
        stop()
    }

    func start(_ list: DriftWatchList, onChange: @escaping ([URL]) -> Void) {
        stop()
        guard !list.watches.isEmpty else { return }
        let context = WatchContext()
        context.deliver = onChange
        self.context = context
        for watch in list.watches {
            guard let stream = makeStream(for: watch, context: context) else { continue }
            context.set(watch, for: stream)
            streams.append(stream)
        }
    }

    func stop() {
        for stream in streams {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
        }
        streams.removeAll()
        context?.clear()
        context = nil
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
    var deliver: ([URL]) -> Void = { _ in }
    /// The watches,  by the stream that is speaking.
    private let lock = NSLock()
    private var watches: [UInt: DriftWatchList.Watch] = [:]
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
