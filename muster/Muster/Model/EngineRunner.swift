import Foundation

/// Where the engine, its manifest and its registry live on this Mac.
struct EngineLocation {
    let root: URL
    let interpreter: URL

    var packageDirectory: URL { root.appendingPathComponent("scripts", isDirectory: true) }
    var manifestURL: URL { root.appendingPathComponent("fleet-seats.json") }
    var registryURL: URL { root.appendingPathComponent("fleet-apps.json") }

    /// The arguments the app runs, after the interpreter.
    ///
    /// The manifest and the registry are named by absolute path, so the engine reads
    /// the same two files the grid reads and can never fall back to a different copy.
    ///
    /// `-s` and not `-I`:  isolated mode implies `-E`, which ignores `PYTHONPATH`, and
    /// the engine has to be importable from an absolute path that is not the working
    /// directory.  `-s` keeps the user site directory out of the run, which is the part
    /// of isolation that matters here, and `PYTHONPATH` below names the package parent
    /// exactly.
    ///
    /// `--no-doctor` because the MAC level doctors add no rows to the envelope and
    /// cost about a minute:  the envelope has no field for their notes, so the grid
    /// cannot show them either way.
    var arguments: [String] {
        [
            "-s",
            "-m", "fleet_config",
            "--manifest", manifestURL.path,
            "--registry", registryURL.path,
            "status",
            "--json",
            "--no-doctor",
        ]
    }

    /// The command as a person would type it.
    ///
    /// The footer shows this, so a reviewer can run exactly what the app ran and compare
    /// the cells one for one.
    var commandLine: String {
        ([interpreter.path] + arguments).joined(separator: " ")
    }
}

/// Finds the engine checkout on this Mac.
enum EngineLocator {
    /// The pinned interpreter:  the system Python, which is the version the engine is
    /// written for and the one the rest of the fleet's Mac jobs use.
    static let defaultInterpreter = "/usr/bin/python3"

    /// The checkout the app runs the engine from.
    ///
    /// The order is deliberate:  an explicit override wins, then a checkout beside the
    /// app (which is what a development build has), then the live integration tree.
    static func locate(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        bundle: URL = Bundle.main.bundleURL
    ) -> EngineLocation? {
        let interpreterPath = environment["FLEET_ENGINE_PYTHON"] ?? defaultInterpreter
        let interpreter = URL(fileURLWithPath: interpreterPath)
        var candidates: [URL] = []
        if let override = environment["FLEET_ENGINE_ROOT"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: (override as NSString).expandingTildeInPath))
        }
        candidates.append(contentsOf: ancestors(of: bundle))
        candidates.append(home.appendingPathComponent("Code/AI-Fleet-Coordinator"))
        for candidate in candidates {
            if isEngineRoot(candidate) {
                return EngineLocation(root: candidate.standardizedFileURL, interpreter: interpreter)
            }
        }
        return nil
    }

    /// True when a directory carries the engine and the two files beside it.
    static func isEngineRoot(_ root: URL) -> Bool {
        let fileManager = FileManager.default
        let required = [
            root.appendingPathComponent("scripts/fleet_config/__main__.py"),
            root.appendingPathComponent("fleet-seats.json"),
            root.appendingPathComponent("fleet-apps.json"),
        ]
        return required.allSatisfy { fileManager.fileExists(atPath: $0.path) }
    }

    /// The directories above a starting point, nearest first.
    static func ancestors(of start: URL) -> [URL] {
        var found: [URL] = []
        var current = start.standardizedFileURL
        for _ in 0..<8 {
            let parent = current.deletingLastPathComponent()
            guard parent.path != current.path, parent.path != "/" else { break }
            found.append(parent)
            current = parent
        }
        return found
    }
}

/// What one engine run produced.
struct EngineRun {
    /// The envelope, when the run produced one this app can read.
    let envelope: StatusEnvelope?
    let exitCode: Int32
    let standardError: String
    /// Why the run cannot be trusted, when it cannot.
    let failure: String?
    let timedOut: Bool
}

/// Runs the engine as a subprocess with a pinned interpreter and an explicit environment.
struct EngineRunner {
    let location: EngineLocation
    let home: URL
    let timeout: TimeInterval

    init(
        location: EngineLocation,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        timeout: TimeInterval = 240
    ) {
        self.location = location
        self.home = home
        self.timeout = timeout
    }

    /// The environment the engine sees.
    ///
    /// It is explicit because a GUI app inherits no useful shell environment:  an app
    /// launched from Finder gets a minimal `PATH`, so `fleet-secrets` (which lives in
    /// `~/.local/bin`) would not be found and every credential check would fail.  The
    /// `PATH` below names that directory, and the secret store is the only outside
    /// thing the checks reach for.
    var environment: [String: String] {
        EngineRunner.childEnvironment(location: location, home: home)
    }

    /// The environment every run of the engine sees.
    ///
    /// It is the same for the full run and for the smaller questions the app asks the
    /// engine,  so both reach the engine's own modules the same way.
    static func childEnvironment(location: EngineLocation, home: URL) -> [String: String] {
        var environment: [String: String] = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:\(home.appendingPathComponent(".local/bin").path)",
            "HOME": home.path,
            "PYTHONPATH": location.packageDirectory.path,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        ]
        environment["TMPDIR"] = NSTemporaryDirectory()
        return environment
    }

    /// Run the engine and hand back what it produced.
    ///
    /// The completion runs on a background queue;  a caller that touches the interface
    /// hops to the main actor itself.
    func run(completion: @escaping (EngineRun) -> Void) {
        let process = Process()
        let outputPipe = Pipe()
        let errorPipe = Pipe()

        process.executableURL = location.interpreter
        process.arguments = location.arguments
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        process.environment = environment

        let outputQueue = DispatchQueue(label: "muster.engine.stdout")
        let errorQueue = DispatchQueue(label: "muster.engine.stderr")
        let outputBox = DataBox()
        let errorBox = DataBox()
        let readers = DispatchGroup()
        readers.enter()
        outputQueue.async {
            outputBox.set(outputPipe.fileHandleForReading.readDataToEndOfFile() ?? Data())
            readers.leave()
        }
        readers.enter()
        errorQueue.async {
            errorBox.set(errorPipe.fileHandleForReading.readDataToEndOfFile() ?? Data())
            readers.leave()
        }

        let state = RunState()
        let timeoutItem = DispatchWorkItem {
            guard process.isRunning else { return }
            state.markTimedOut()
            process.terminate()
        }
        DispatchQueue.global(qos: .utility).asyncAfter(
            deadline: .now() + timeout,
            execute: timeoutItem
        )

        process.terminationHandler = { finished in
            timeoutItem.cancel()
            readers.wait()
            let output = outputBox.get()
            let errors = errorBox.get()
            let errorText = String(data: errors, encoding: .utf8) ?? ""
            if state.timedOut {
                completion(
                    EngineRun(
                        envelope: nil,
                        exitCode: finished.terminationStatus,
                        standardError: errorText,
                        failure: "the engine did not finish within \(Int(timeout)) seconds",
                        timedOut: true
                    )
                )
                return
            }
            do {
                let envelope = try EnvelopeDecoder.decode(output)
                completion(
                    EngineRun(
                        envelope: envelope,
                        exitCode: finished.terminationStatus,
                        standardError: errorText,
                        failure: nil,
                        timedOut: false
                    )
                )
            } catch {
                var reason = (error as? LocalizedError)?.errorDescription
                    ?? error.localizedDescription
                let trimmed = errorText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty {
                    reason += "\n" + trimmed
                }
                completion(
                    EngineRun(
                        envelope: nil,
                        exitCode: finished.terminationStatus,
                        standardError: errorText,
                        failure: reason,
                        timedOut: false
                    )
                )
            }
        }

        do {
            try process.run()
        } catch {
            timeoutItem.cancel()
            // The process never started,  so no child holds the write ends of the pipes
            // and the reader threads would wait for an EOF that never comes.  Closing
            // the write ends here gives them the EOF and lets them finish.
            outputPipe.fileHandleForWriting.closeFile()
            errorPipe.fileHandleForWriting.closeFile()
            readers.wait()
            completion(
                EngineRun(
                    envelope: nil,
                    exitCode: -1,
                    standardError: "",
                    failure: "the engine could not be started:  \(error.localizedDescription)",
                    timedOut: false
                )
            )
        }
    }
}

/// A box that carries bytes from the thread that reads a pipe to the thread that waits
/// for the run to end.
private final class DataBox {
    private let lock = NSLock()
    private var data = Data()

    func set(_ newData: Data) {
        lock.lock()
        data = newData
        lock.unlock()
    }

    func get() -> Data {
        lock.lock()
        defer { lock.unlock() }
        return data
    }
}

/// The one flag the run and its timeout share.
private final class RunState {    private let lock = NSLock()
    private var timedOutFlag = false

    var timedOut: Bool {
        lock.lock()
        defer { lock.unlock() }
        return timedOutFlag
    }

    func markTimedOut() {
        lock.lock()
        timedOutFlag = true
        lock.unlock()
    }
}
