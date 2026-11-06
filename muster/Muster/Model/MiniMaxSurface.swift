import Foundation

/// What the MiniMax install on the Mac looks like right now.
///
/// This is display data, never a verdict.  The states in the grid come from the engine
/// and from nowhere else;  these panels say what is installed, so a person can judge a
/// drift against something concrete instead of against a summary.
///
/// It covers the Mac only.  The server has a MiniMax install of its own, and nothing
/// here reads it.
struct MiniMaxSurface {
    /// What became of one file or folder the reader looked for.
    ///
    /// A folder that is not there and a folder that could not be read are different
    /// facts, and neither of them is an empty folder:  a panel that called all three
    /// "nothing installed" would be saying something it never checked.
    enum FileState: Equatable {
        /// Nothing is there.
        case absent
        /// Something is there, and it could not be read.
        case unreadable
        /// It was read.
        case read
    }

    /// What the reader found at the script a hook command runs.
    enum ScriptState: Equatable {
        /// The command names no file path:  it runs a program by name from the PATH, or
        /// hands the interpreter code inline, so there is no file to look at.
        case noPath
        /// The file is there, and it can run.
        case present
        /// The file is there and is run directly, but it has no execute permission.
        case notExecutable
        /// Nothing is at that path.
        case missing
    }

    /// One hook registration, from a hook manifest.
    struct Hook: Identifiable {
        let id: String
        /// The plugin whose manifest registers this hook, or nil for the top-level
        /// `~/.minimax/hooks.json`.
        let plugin: String?
        /// Where the registration was read, in words:  a plugin, or the top-level file.
        let origin: String
        let event: String
        let matcher: String
        /// The id the engine gives this hook, derived the way the engine derives it:
        /// the entry's own `name`, else the name of the script the command runs.
        let hookId: String
        /// The command as the panel shows it.
        ///
        /// The manifest's command is free text, so it is not shown as written:  the home
        /// folder is shortened to `~`, and any argument that is named like a secret or
        /// shaped like one is replaced with a mark before it is kept.
        let shownCommand: String
        /// The absolute path of the script the command runs, when it names one.
        let scriptPath: String?
        /// What is at that path.
        let script: ScriptState
        let timeout: Int?
        /// False when the manifest turns its hooks off with `enabled: false`.
        let enabled: Bool
        /// The proof the engine reached for this hook, when it has a rule for it.
        ///
        /// This is the column the fleet has nowhere else:  a hook that is registered
        /// and a hook that has been seen to deny are different states, and only the
        /// engine's own proof says which one this is.
        let proof: HookProof?

        /// The same hook, carrying the engine's proof.
        func withProof(_ proof: HookProof) -> Hook {
            Hook(
                id: id,
                plugin: plugin,
                origin: origin,
                event: event,
                matcher: matcher,
                hookId: hookId,
                shownCommand: shownCommand,
                scriptPath: scriptPath,
                script: script,
                timeout: timeout,
                enabled: enabled,
                proof: proof
            )
        }
    }

    /// The engine's proof for one hook.
    struct HookProof: Equatable {
        /// The id of the hook the engine proved, as the rule names it.
        let hookName: String
        /// The proof level the rule requires.
        let required: String
        /// The proof level the rule reached.
        let achieved: String?
        /// When the proof ran.
        let checkedAt: Date?
        /// The state the rule reached.
        let state: String
    }

    /// One installed plugin.
    struct Plugin: Identifiable {
        let id: String
        let name: String
        let hasHookManifest: Bool
        let hookManifestValid: Bool
        let events: [String]
        let commandCount: Int
    }

    /// One channel file.
    struct Channel: Identifiable {
        let id: String
        let file: String
        let state: FileState
        let schemaVersion: String?
        let detail: String
        /// The routes this file points at, with what each one names.
        let routes: [Route]

        var present: Bool { state != .absent }
    }

    /// One route target.
    struct Route: Identifiable {
        /// What an agent id turned out to name.
        enum Target: Equatable {
            /// A fleet seat on the roster this app read.
            ///
            /// The roster says the seat is not retired.  It does not say anything is
            /// running, so the panel never calls this route live.
            case fleetSeat(String)
            /// An agent of the MiniMax runtime on the Mac that is not a fleet seat.
            case runtimeAgent(String)
            /// Neither:  no seat on the roster and no agent on the Mac has that id.
            case unknown
        }

        let id: String
        /// The channel the route is for, or `rule N` for the Nth routing rule.
        let channel: String
        /// The top-level block the route was read from:  `defaultRoute`, `rules`, or
        /// another key.
        let section: String
        let agentId: String
        /// What the agent id turned out to be.
        let target: Target
    }

    /// One installed sub-agent.
    struct Agent: Identifiable {
        let id: String
        let name: String
        /// The files that make up the definition, at the top of the directory.
        ///
        /// The list is the definition's own files and not every file under it:  the
        /// seat's agent directory holds a whole memory tree, and counting that as
        /// "the definition" would report a hundred files for a prompt that is one.
        let files: [String]
        /// Whether the definition carries a prompt file.
        let hasPrompt: Bool
        /// Whether this directory is the seat's own agent.
        ///
        /// The seat's agent is the one whose directory carries `memory/` and no
        /// `.agent-instance-id`;  an agent with an instance id is a spawned instance,
        /// even when it keeps a memory folder of its own.  This is the same test
        /// `MiniMaxReader.seatRuntimeAgentName` uses, so the row and the route agree.
        let ownsSeatMemory: Bool
        /// When the newest file of the definition was written.
        let newestChange: Date?
    }

    /// One allowed setting from `config.yaml`.
    struct Setting: Identifiable {
        let id: String
        let key: String
        let value: String
    }

    /// When the reader looked at the disk.
    let readAt: Date
    /// Whether `~/.minimax` is there at all.
    let installFound: Bool
    let settingsFile: FileState
    let settings: [Setting]
    /// The top-level `~/.minimax/hooks.json`.
    let userHooksFile: FileState
    let hooks: [Hook]
    let pluginsFolder: FileState
    let plugins: [Plugin]
    let channels: [Channel]
    let agentsFolder: FileState
    let agents: [Agent]
    /// Anything that could not be read, as sentences, so a failed read is never a
    /// silent gap.  The panel shows every one of them.
    let problems: [String]
}

/// Reads the MiniMax install from the Mac's disk.
///
/// Most values come from an allowlist of keys or from a structural count:  the reader
/// never prints a whole file, because `config.yaml` carries an `apiKey` and a printed
/// file would print it.  The one free-text value is a hook's command line, and it is
/// redacted before it is kept (see `shownCommand`).
enum MiniMaxReader {
    /// The `config.yaml` keys the panel shows.
    ///
    /// The list is an allowlist on purpose:  `config.yaml` holds an `apiKey`, and a
    /// reader that showed every key would show that one too.
    static let allowedSettings = [
        "defaultModel",
        "defaultModelVariant",
        "defaultModelThinking",
        "permissionMode",
        "model_order",
    ]

    /// The channel files the panel shows, in the order it shows them.
    ///
    /// Lark is the only IM platform this seat is linked to (owner, Oct 24, 2026), so the
    /// panel names the three fleet channel files and no platform file for a platform
    /// nothing answers on.  A retired platform is not shown as "not on the Mac" either:
    /// it is not part of this seat's surface at all, and a panel that lists it invites
    /// the reader to go looking for a connection that was never there.
    static let channelFiles = [
        "channel-bindings.yaml",
        "channel-routes.yaml",
        "channel-owner.yaml",
    ]

    /// How the top-level hook file is named on the panel.
    static let userHooksOrigin = "~/.minimax/hooks.json"

    /// The script suffixes the engine reads a hook id from (`checks_hooks._SCRIPT_SUFFIXES`).
    static let scriptSuffixes: Set<String> = ["py", "js", "mjs", "cjs", "sh", "bash", "zsh"]

    /// Programs that run a script named after them, so the script is the next path.
    static let interpreters: Set<String> = ["env", "bash", "sh", "zsh", "node", "ruby", "perl", "deno", "bun"]

    /// Read the whole surface.
    ///
    /// `rosterSeats` is the fleet's roster as this app read it, and `seatAliases` maps
    /// a runtime agent id to the seat tag it serves, because a MiniMax route names the
    /// runtime agent (`mavis`) and never the fleet seat tag (`MM`).
    static func read(
        home: URL,
        rosterSeats: Set<String>,
        seatAliases: [String: String] = [:],
        proofs: [MiniMaxSurface.HookProof] = [],
        now: Date = Date()
    ) -> MiniMaxSurface {
        let root = home.appendingPathComponent(".minimax", isDirectory: true)
        var problems: [String] = []
        var isDirectory: ObjCBool = false
        let installFound = FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory)
            && isDirectory.boolValue
        let settings = readSettings(in: root, problems: &problems)
        let plugins = readPlugins(in: root, problems: &problems)
        let hooks = readHooks(
            in: root,
            home: home,
            plugins: plugins.plugins,
            proofs: proofs,
            problems: &problems
        )
        let agents = readAgents(in: root, problems: &problems)
        let channels = readChannels(
            in: root,
            rosterSeats: rosterSeats,
            seatAliases: seatAliases,
            runtimeAgents: Set(agents.agents.map { $0.name }),
            problems: &problems
        )
        return MiniMaxSurface(
            readAt: now,
            installFound: installFound,
            settingsFile: settings.state,
            settings: settings.settings,
            userHooksFile: hooks.userFile,
            hooks: hooks.hooks,
            pluginsFolder: plugins.state,
            plugins: plugins.plugins,
            channels: channels,
            agentsFolder: agents.state,
            agents: agents.agents,
            problems: problems
        )
    }

    /// The runtime agent id that serves the seat on the Mac.
    ///
    /// The MiniMax runtime names its own seat agent `mavis`, while the fleet calls that
    /// seat `MM`.  The seat's agent is the one whose directory carries the durable
    /// `memory/` tree;  every other agent there is a spawned instance with an instance
    /// id beside it.
    static func seatRuntimeAgentName(home: URL) -> String? {
        let agentsRoot = home.appendingPathComponent(".minimax/agents", isDirectory: true)
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: agentsRoot,
            includingPropertiesForKeys: nil
        ) else { return nil }
        let seatAgents = entries.filter { entry in
            !entry.lastPathComponent.hasPrefix(".") && isSeatAgentDirectory(entry)
        }
        return seatAgents.map { $0.lastPathComponent }.sorted().first
    }

    /// Whether an agent directory is the seat's own:  it keeps `memory/` and carries no
    /// instance id.
    static func isSeatAgentDirectory(_ entry: URL) -> Bool {
        let memory = entry.appendingPathComponent("memory", isDirectory: true)
        let instance = entry.appendingPathComponent(".agent-instance-id")
        return FileManager.default.fileExists(atPath: memory.path)
            && !FileManager.default.fileExists(atPath: instance.path)
    }

    // MARK: - reading one file or folder

    /// The text of a file, or why there is none.
    private static func readText(_ url: URL) -> (state: MiniMaxSurface.FileState, text: String?) {
        guard FileManager.default.fileExists(atPath: url.path) else { return (.absent, nil) }
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return (.unreadable, nil) }
        return (.read, text)
    }

    /// The entries of a folder, or why there are none.
    private static func listFolder(
        _ url: URL,
        keys: [URLResourceKey]? = nil
    ) -> (state: MiniMaxSurface.FileState, entries: [URL]) {
        guard FileManager.default.fileExists(atPath: url.path) else { return (.absent, []) }
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: url,
            includingPropertiesForKeys: keys
        ) else { return (.unreadable, []) }
        return (.read, entries)
    }

    // MARK: - config.yaml

    private static func readSettings(
        in root: URL,
        problems: inout [String]
    ) -> (state: MiniMaxSurface.FileState, settings: [MiniMaxSurface.Setting]) {
        let loaded = readText(root.appendingPathComponent("config.yaml"))
        guard let text = loaded.text else {
            if loaded.state == .unreadable {
                problems.append("~/.minimax/config.yaml is there but could not be read.")
            }
            return (loaded.state, [])
        }
        let settings = allowedSettings.compactMap { key -> MiniMaxSurface.Setting? in
            guard let value = scalarValue(in: text, key: key) else { return nil }
            return MiniMaxSurface.Setting(id: key, key: key, value: value)
        }
        return (.read, settings)
    }

    /// The value of a top-level key, with a list or a nested block gathered.
    private static func scalarValue(in text: String, key: String) -> String? {
        let lines = text.components(separatedBy: "\n")
        var index = 0
        while index < lines.count {
            let line = lines[index]
            if line.hasPrefix(key + ":") {
                let inline = stripComment(String(line.dropFirst(key.count + 1))).trimmingCharacters(in: .whitespaces)
                if !inline.isEmpty {
                    return unquote(inline)
                }
                var gathered: [String] = []
                var cursor = index + 1
                while cursor < lines.count {
                    let next = lines[cursor]
                    if next.trimmingCharacters(in: .whitespaces).isEmpty {
                        cursor += 1
                        continue
                    }
                    if !next.hasPrefix(" ") && !next.hasPrefix("\t") { break }
                    let trimmed = next.trimmingCharacters(in: .whitespaces)
                    if trimmed.hasPrefix("- ") {
                        let item = String(trimmed.dropFirst(2))
                        gathered.append(unquote(stripComment(item)).trimmingCharacters(in: .whitespaces))
                    } else if let colon = trimmed.firstIndex(of: ":") {
                        let name = String(trimmed[trimmed.startIndex..<colon])
                        let value = stripComment(String(trimmed[trimmed.index(after: colon)...])).trimmingCharacters(in: .whitespaces)
                        gathered.append("\(name): \(unquote(value))")
                    }
                    cursor += 1
                }
                return gathered.isEmpty ? nil : gathered.joined(separator: ", ")
            }
            index += 1
        }
        return nil
    }

    // MARK: - plugins and hooks

    private static func readPlugins(
        in root: URL,
        problems: inout [String]
    ) -> (state: MiniMaxSurface.FileState, plugins: [MiniMaxSurface.Plugin]) {
        let listing = listFolder(root.appendingPathComponent("plugins", isDirectory: true))
        if listing.state == .unreadable {
            problems.append("~/.minimax/plugins is there but could not be listed, so no plugin or plugin hook is shown.")
        }
        var plugins: [MiniMaxSurface.Plugin] = []
        for entry in listing.entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let name = entry.lastPathComponent
            guard name.hasPrefix(".") == false else { continue }
            let manifestURL = entry.appendingPathComponent("hooks/hooks.json")
            guard FileManager.default.fileExists(atPath: manifestURL.path) else {
                plugins.append(
                    MiniMaxSurface.Plugin(
                        id: name,
                        name: name,
                        hasHookManifest: false,
                        hookManifestValid: false,
                        events: [],
                        commandCount: 0
                    )
                )
                continue
            }
            let parsed = parseHookManifest(at: manifestURL)
            let events = parsed?.entries.map { $0.event }.distinctValues().sorted()
            plugins.append(
                MiniMaxSurface.Plugin(
                    id: name,
                    name: name,
                    hasHookManifest: true,
                    hookManifestValid: parsed != nil,
                    events: events ?? [],
                    commandCount: parsed?.entries.count ?? 0
                )
            )
            if parsed == nil {
                problems.append("The hook manifest of plugin \(name) is not readable JSON, so its hooks are not shown.")
            }
        }
        return (listing.state, plugins)
    }

    /// One registration as the manifest writes it, before the panel shapes it.
    private struct RawHook {
        let event: String
        let matcher: String
        let name: String?
        let command: String
        let timeout: Int?
    }

    private static func readHooks(
        in root: URL,
        home: URL,
        plugins: [MiniMaxSurface.Plugin],
        proofs: [MiniMaxSurface.HookProof],
        problems: inout [String]
    ) -> (userFile: MiniMaxSurface.FileState, hooks: [MiniMaxSurface.Hook]) {
        var hooks: [MiniMaxSurface.Hook] = []
        // The top-level hook file first:  it is not a plugin, and an install that
        // registers its guard there would otherwise never reach the panel.
        let userURL = root.appendingPathComponent("hooks.json")
        var userFile: MiniMaxSurface.FileState = .absent
        if FileManager.default.fileExists(atPath: userURL.path) {
            if let parsed = parseHookManifest(at: userURL) {
                userFile = .read
                hooks += shape(parsed, plugin: nil, origin: userHooksOrigin, home: home)
            } else {
                userFile = .unreadable
                problems.append("\(userHooksOrigin) is there but is not readable JSON, so its hooks are not shown.")
            }
        }
        for plugin in plugins where plugin.hasHookManifest && plugin.hookManifestValid {
            let manifestURL = root
                .appendingPathComponent("plugins", isDirectory: true)
                .appendingPathComponent(plugin.id, isDirectory: true)
                .appendingPathComponent("hooks/hooks.json")
            guard let parsed = parseHookManifest(at: manifestURL) else { continue }
            hooks += shape(parsed, plugin: plugin.name, origin: "plugin \(plugin.name)", home: home)
        }
        let matched = attach(proofs, to: hooks)
        // The manifest is a dictionary keyed by event, so the read order is not stable.
        // The rows are sorted, so a reload never shuffles them.
        return (userFile, matched.sorted { lhs, rhs in
            (lhs.origin, lhs.event, lhs.matcher, lhs.id) < (rhs.origin, rhs.event, rhs.matcher, rhs.id)
        })
    }

    /// The registrations of one manifest, shaped for the panel.
    private static func shape(
        _ parsed: (enabled: Bool, entries: [RawHook]),
        plugin: String?,
        origin: String,
        home: URL
    ) -> [MiniMaxSurface.Hook] {
        parsed.entries.enumerated().map { index, raw in
            let location = scriptLocation(in: raw.command, home: home)
            return MiniMaxSurface.Hook(
                // The position in the manifest keeps two identical registrations apart.
                id: "\(origin)/\(raw.event)/\(raw.matcher)/\(index)",
                plugin: plugin,
                origin: origin,
                event: raw.event,
                matcher: raw.matcher,
                hookId: hookId(name: raw.name, command: raw.command),
                shownCommand: shownCommand(raw.command, home: home),
                scriptPath: location.path,
                script: scriptState(location),
                timeout: raw.timeout,
                enabled: parsed.enabled,
                proof: nil
            )
        }
    }

    /// Give each hook the engine's proof for it, by the hook's id.
    ///
    /// The engine compares ids with a trailing `-hook` dropped, so a rule naming
    /// `fleet-guard-hook` proves a hook whose id is `fleet-guard`, and the panel
    /// compares the same way.  When two rules prove the same id, the one that did not
    /// pass is kept, so a failing proof is never hidden behind a passing one.
    static func attach(
        _ proofs: [MiniMaxSurface.HookProof],
        to hooks: [MiniMaxSurface.Hook]
    ) -> [MiniMaxSurface.Hook] {
        var byKey: [String: MiniMaxSurface.HookProof] = [:]
        for proof in proofs {
            let key = hookKey(proof.hookName)
            if let existing = byKey[key], existing.state != "PASS" { continue }
            byKey[key] = proof
        }
        return hooks.map { hook in
            guard let proof = byKey[hookKey(hook.hookId)] else { return hook }
            return hook.withProof(proof)
        }
    }

    /// The hook manifest as a flat list of registrations, or nil when it is not JSON or
    /// its root is not an object.
    ///
    /// The shape is `hooks` keyed by event, each holding matcher groups, each holding
    /// the commands themselves, so the panel can say what runs, for what, and through
    /// which matcher.
    private static func parseHookManifest(at url: URL) -> (enabled: Bool, entries: [RawHook])? {
        guard
            let data = try? Data(contentsOf: url),
            let object = try? JSONSerialization.jsonObject(with: data),
            let root = object as? [String: Any]
        else { return nil }
        // JSON that is an object with no `hooks` key registers no hooks:  that is empty,
        // not unreadable.
        let events = root["hooks"] as? [String: Any] ?? [:]
        let enabled = (root["enabled"] as? Bool) ?? true
        var hooks: [RawHook] = []
        for event in events.keys.sorted() {
            guard let groups = events[event] as? [[String: Any]] else { continue }
            for group in groups {
                let matcher = group["matcher"] as? String ?? "any"
                guard let entries = group["hooks"] as? [[String: Any]] else { continue }
                for entry in entries {
                    hooks.append(
                        RawHook(
                            event: event,
                            matcher: matcher,
                            name: entry["name"] as? String,
                            command: entry["command"] as? String ?? "",
                            timeout: entry["timeout"] as? Int
                        )
                    )
                }
            }
        }
        return (enabled, hooks)
    }

    // MARK: - reading a hook command

    /// The id the engine gives a hook (`checks_hooks._hook_id`):  the entry's own name,
    /// else the file name of the script its command runs.
    static func hookId(name: String?, command: String) -> String {
        if let name, !name.trimmingCharacters(in: .whitespaces).isEmpty {
            return name.trimmingCharacters(in: .whitespaces)
        }
        return commandId(command)
    }

    /// The id a command names (`checks_hooks.command_id`):  the first token with a
    /// script suffix, without the suffix, else the file name of the first token.
    static func commandId(_ command: String) -> String {
        let tokens = command.split(whereSeparator: { $0 == " " || $0 == "\t" }).map {
            String($0).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        }
        // Leading `NAME=value` assignments are never an id:  the value may be a secret.
        let words = tokens.drop(while: { isAssignment($0) })
        guard let first = words.first else { return "" }
        for token in words {
            let base = (token as NSString).lastPathComponent
            let suffix = (base as NSString).pathExtension
            if scriptSuffixes.contains(suffix) {
                return (base as NSString).deletingPathExtension
            }
        }
        return (first as NSString).lastPathComponent
    }

    /// A hook id as the engine compares it:  a trailing `-hook` does not matter.
    static func hookKey(_ name: String) -> String {
        name.hasSuffix("-hook") ? String(name.dropLast("-hook".count)) : name
    }

    /// Where the script a command runs lives, and whether an interpreter runs it.
    struct ScriptLocation: Equatable {
        let path: String?
        let viaInterpreter: Bool
    }

    /// The script a hook command runs.
    ///
    /// Leading `NAME=value` assignments are skipped, and so is an interpreter in front
    /// of the script (`python3 ~/x.py`, `env bash $HOME/x.sh`).  `~`, `$HOME` and
    /// `${HOME}` are expanded against the home folder.  A command that runs a program
    /// by name, or hands the interpreter inline code with `-c`, names no path.
    static func scriptLocation(in command: String, home: URL) -> ScriptLocation {
        let tokens = command.split(whereSeparator: { $0 == " " || $0 == "\t" }).map {
            String($0).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        }
        var index = 0
        while index < tokens.count, isAssignment(tokens[index]) { index += 1 }
        guard index < tokens.count else { return ScriptLocation(path: nil, viaInterpreter: false) }
        var viaInterpreter = false
        if isInterpreter(tokens[index]) {
            viaInterpreter = true
            index += 1
            while index < tokens.count {
                let token = tokens[index]
                if token == "-c" || token == "-m" || token == "-e" {
                    return ScriptLocation(path: nil, viaInterpreter: true)
                }
                if token.hasPrefix("-") || isAssignment(token) || isInterpreter(token) {
                    index += 1
                    continue
                }
                break
            }
            guard index < tokens.count else { return ScriptLocation(path: nil, viaInterpreter: true) }
        }
        let expanded = expandHome(tokens[index], home: home)
        return ScriptLocation(path: expanded.hasPrefix("/") ? expanded : nil, viaInterpreter: viaInterpreter)
    }

    /// What is at the script a command runs.
    static func scriptState(_ location: ScriptLocation) -> MiniMaxSurface.ScriptState {
        guard let path = location.path else { return .noPath }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              !isDirectory.boolValue else { return .missing }
        // An interpreter reads the script, so the script itself need not be executable.
        if location.viaInterpreter { return .present }
        return FileManager.default.isExecutableFile(atPath: path) ? .present : .notExecutable
    }

    /// A hook command as the panel may show it.
    ///
    /// Any `NAME=value` whose name is a secret name, or whose value is shaped like a
    /// token, keeps its name and loses its value.  The argument after a flag named like
    /// a secret (`--token x`) or after `Bearer` is replaced too, and so is any bare
    /// argument shaped like a token.  The rules are `ConfigFileReader`'s, so the two
    /// readers hide the same things.  The home folder is shortened to `~`.
    static func shownCommand(_ command: String, home: URL) -> String {
        let mark = ConfigFileReader.redactionMark
        let tokens = command.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
        var shown: [String] = []
        var hideNext = false
        for raw in tokens {
            if hideNext {
                shown.append(mark)
                hideNext = false
                continue
            }
            let bare = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if let equals = bare.firstIndex(of: "="), equals != bare.startIndex {
                let name = String(bare[..<equals])
                let key = name.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
                let value = String(bare[bare.index(after: equals)...])
                if ConfigFileReader.isSecretKey(key) || ConfigFileReader.looksLikeSecretValue(value) {
                    shown.append("\(name)=\(mark)")
                    continue
                }
            }
            if bare.hasPrefix("-") {
                let flag = bare.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
                if !flag.isEmpty, ConfigFileReader.isSecretKey(flag) { hideNext = true }
                shown.append(bare)
                continue
            }
            if bare.lowercased() == "bearer" {
                hideNext = true
                shown.append(bare)
                continue
            }
            if ConfigFileReader.looksLikeSecretValue(bare) {
                shown.append(mark)
                continue
            }
            shown.append(abbreviateHome(bare, home: home))
        }
        return shown.joined(separator: " ")
    }

    private static func isAssignment(_ token: String) -> Bool {
        guard let equals = token.firstIndex(of: "="), equals != token.startIndex else { return false }
        let name = token[..<equals]
        guard let first = name.first, first.isLetter || first == "_" else { return false }
        return name.allSatisfy { $0.isLetter || $0.isNumber || $0 == "_" }
    }

    private static func isInterpreter(_ token: String) -> Bool {
        let base = (token as NSString).lastPathComponent
        return interpreters.contains(base) || base.hasPrefix("python")
    }

    private static func expandHome(_ token: String, home: URL) -> String {
        let base = home.path.hasSuffix("/") ? String(home.path.dropLast()) : home.path
        for prefix in ["~/", "$HOME/", "${HOME}/"] where token.hasPrefix(prefix) {
            return base + "/" + String(token.dropFirst(prefix.count))
        }
        if token == "~" || token == "$HOME" || token == "${HOME}" { return base }
        return token
    }

    private static func abbreviateHome(_ token: String, home: URL) -> String {
        let base = home.path.hasSuffix("/") ? String(home.path.dropLast()) : home.path
        guard !base.isEmpty, base != "/" else { return token }
        if token == base { return "~" }
        if token.hasPrefix(base + "/") { return "~/" + String(token.dropFirst(base.count + 1)) }
        return token
    }

    // MARK: - channels

    private static func readChannels(
        in root: URL,
        rosterSeats: Set<String>,
        seatAliases: [String: String],
        runtimeAgents: Set<String>,
        problems: inout [String]
    ) -> [MiniMaxSurface.Channel] {
        var channels: [MiniMaxSurface.Channel] = []
        for file in channelFiles {
            let loaded = readText(root.appendingPathComponent(file))
            guard let text = loaded.text else {
                if loaded.state == .unreadable {
                    problems.append("~/.minimax/\(file) is there but could not be read.")
                }
                channels.append(
                    MiniMaxSurface.Channel(
                        id: file,
                        file: file,
                        state: loaded.state,
                        schemaVersion: nil,
                        detail: loaded.state == .unreadable ? "there, but could not be read" : "not on the Mac",
                        routes: []
                    )
                )
                continue
            }
            let schema = scalarValue(in: text, key: "schemaVersion")
            let routes = file == "channel-routes.yaml"
                ? readRoutes(
                    in: text,
                    rosterSeats: rosterSeats,
                    seatAliases: seatAliases,
                    runtimeAgents: runtimeAgents
                )
                : []
            channels.append(
                MiniMaxSurface.Channel(
                    id: file,
                    file: file,
                    state: .read,
                    schemaVersion: schema,
                    detail: describeChannel(file: file, text: text, routes: routes),
                    routes: routes
                )
            )
        }
        return channels
    }

    /// The route targets `channel-routes.yaml` points at.
    ///
    /// A route names the runtime agent, not the fleet seat:  the MiniMax runtime calls
    /// its own seat `mavis`, while the fleet calls that seat `MM`.  Each `agentId` is
    /// kept with the top-level block it sits in, so a default route and a routing rule
    /// are never counted as one another, and a `- agentId:` list item counts the same
    /// as a plain `agentId:` line.
    static func readRoutes(
        in text: String,
        rosterSeats: Set<String>,
        seatAliases: [String: String],
        runtimeAgents: Set<String>
    ) -> [MiniMaxSurface.Route] {
        var routes: [MiniMaxSurface.Route] = []
        var section = ""
        var childIndent: Int?
        var currentChannel = "default"
        var ruleNumber = 0
        for (lineNumber, line) in text.components(separatedBy: "\n").enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
            let indent = line.prefix(while: { $0 == " " }).count
            if indent == 0 && !trimmed.hasPrefix("- ") {
                section = trimmed.firstIndex(of: ":").map { String(trimmed[..<$0]) } ?? trimmed
                childIndent = nil
                currentChannel = section == "defaultRoute" ? "default" : section
                continue
            }
            if childIndent == nil { childIndent = indent }
            var body = trimmed
            let startsItem = body.hasPrefix("- ")
            if startsItem {
                body = String(body.dropFirst(2)).trimmingCharacters(in: .whitespaces)
            }
            if section == "rules", startsItem, indent == childIndent {
                ruleNumber += 1
                currentChannel = "rule \(ruleNumber)"
            }
            if section == "defaultRoute", !startsItem, indent == childIndent, body.hasSuffix(":") {
                let name = String(body.dropLast())
                if !name.isEmpty, !name.contains(":") { currentChannel = name }
                continue
            }
            guard body.hasPrefix("agentId:") else { continue }
            let agentId = unquote(
                stripComment(String(body.dropFirst("agentId:".count))).trimmingCharacters(in: .whitespaces)
            )
            guard !agentId.isEmpty else { continue }
            routes.append(
                MiniMaxSurface.Route(
                    id: "\(lineNumber)/\(currentChannel)/\(agentId)",
                    channel: currentChannel,
                    section: section,
                    agentId: agentId,
                    target: resolveTarget(
                        agentId,
                        rosterSeats: rosterSeats,
                        seatAliases: seatAliases,
                        runtimeAgents: runtimeAgents
                    )
                )
            )
        }
        return routes
    }

    /// What an agent id names.
    static func resolveTarget(
        _ agentId: String,
        rosterSeats: Set<String>,
        seatAliases: [String: String],
        runtimeAgents: Set<String>
    ) -> MiniMaxSurface.Route.Target {
        if rosterSeats.contains(agentId) { return .fleetSeat(agentId) }
        if let seat = seatAliases[agentId], rosterSeats.contains(seat) { return .fleetSeat(seat) }
        if runtimeAgents.contains(agentId) { return .runtimeAgent(agentId) }
        return .unknown
    }

    /// A one-line summary of what a channel file holds.
    private static func describeChannel(
        file: String,
        text: String,
        routes: [MiniMaxSurface.Route]
    ) -> String {
        switch file {
        case "channel-bindings.yaml":
            let connections = childCount(in: text, under: "connections")
            let filters = childCount(in: text, under: "messageFilters")
            return "\(connections) connections, \(filters) message filters"
        case "channel-routes.yaml":
            let rules = childCount(in: text, under: "rules")
            let defaults = routes.filter { $0.section == "defaultRoute" }.count
            return "\(rules) rules, \(defaults) default routes"
        case "channel-owner.yaml":
            let owners = childCount(in: text, under: "owners")
            return owners == 0 ? "no owner declared" : "\(owners) owners declared"
        default:
            let bindings = childCount(in: text, under: "bindings")
            return "\(bindings) bindings"
        }
    }

    /// How many direct children a top-level key holds:  list items, or map children.
    ///
    /// Only the direct children count, and the children of a child are not counted
    /// again.  A connection entry carries a handful of fields of its own, so counting
    /// every nested key would report eight connections where there is one.
    private static func childCount(in text: String, under key: String) -> Int {
        let lines = text.components(separatedBy: "\n")
        var index = 0
        while index < lines.count {
            let line = lines[index]
            if line.hasPrefix(key + ":") {
                let inline = String(line.dropFirst(key.count + 1)).trimmingCharacters(in: .whitespaces)
                if inline == "[]" || inline == "{}" { return 0 }
                let keyIndent = line.prefix(while: { $0 == " " }).count
                var items = 0
                var children = 0
                var cursor = index + 1
                while cursor < lines.count {
                    let next = lines[cursor]
                    if next.trimmingCharacters(in: .whitespaces).isEmpty {
                        cursor += 1
                        continue
                    }
                    if !next.hasPrefix(" ") && !next.hasPrefix("\t") && !next.hasPrefix("- ") { break }
                    let indent = next.prefix(while: { $0 == " " }).count
                    // A list may sit at the key's own indent (`rules:` then `- a`).
                    let isItem = next.trimmingCharacters(in: .whitespaces).hasPrefix("- ")
                    // A line indented no further than the key itself ends the block.
                    if indent <= keyIndent && !(isItem && indent == keyIndent) { break }
                    // Only the first level below the key is a direct child.
                    let childLevel = isItem && indent == keyIndent ? keyIndent : keyIndent + 2
                    guard indent == childLevel else {
                        cursor += 1
                        continue
                    }
                    let trimmed = next.trimmingCharacters(in: .whitespaces)
                    if trimmed.hasPrefix("- ") {
                        items += 1
                    } else if trimmed.contains(":") {
                        children += 1
                    }
                    cursor += 1
                }
                return items > 0 ? items : children
            }
            index += 1
        }
        return 0
    }

    // MARK: - agents

    private static func readAgents(
        in root: URL,
        problems: inout [String]
    ) -> (state: MiniMaxSurface.FileState, agents: [MiniMaxSurface.Agent]) {
        let listing = listFolder(
            root.appendingPathComponent("agents", isDirectory: true),
            keys: [.isDirectoryKey]
        )
        if listing.state == .unreadable {
            problems.append("~/.minimax/agents is there but could not be listed, so no agent is shown.")
        }
        var agents: [MiniMaxSurface.Agent] = []
        for entry in listing.entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let name = entry.lastPathComponent
            guard name.hasPrefix(".") == false else { continue }
            // A stray file (a README, say) is not an agent folder and is not a read problem.
            guard (try? entry.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true else { continue }
            var files: [String] = []
            var newest: Date?
            // The definition's own files are the ones at the top of the directory.  The
            // seat's agent directory also holds its whole memory tree, and counting that
            // as the definition would report a hundred files for a one-file prompt.
            if let top = try? FileManager.default.contentsOfDirectory(
                at: entry,
                includingPropertiesForKeys: [.contentModificationDateKey],
                options: [.skipsHiddenFiles]
            ) {
                for fileURL in top.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                    var isDirectory: ObjCBool = false
                    guard FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isDirectory),
                          isDirectory.boolValue == false else { continue }
                    let values = try? fileURL.resourceValues(forKeys: [.contentModificationDateKey])
                    if let date = values?.contentModificationDate {
                        if newest == nil || date > newest! { newest = date }
                    }
                    files.append(fileURL.lastPathComponent)
                }
            } else {
                problems.append("The folder of agent \(name) could not be listed.")
            }
            if files.isEmpty {
                problems.append("The definition of agent \(name) holds no files.")
            }
            agents.append(
                MiniMaxSurface.Agent(
                    id: name,
                    name: name,
                    files: files.sorted(),
                    hasPrompt: files.contains("agent.md"),
                    ownsSeatMemory: isSeatAgentDirectory(entry),
                    newestChange: newest
                )
            )
        }
        return (listing.state, agents)
    }

    // MARK: - small helpers

    /// Drop a trailing comment, leaving a `#` inside a quoted value alone.
    private static func stripComment(_ text: String) -> String {
        var inSingle = false
        var inDouble = false
        var previous: Character?
        for (offset, character) in text.enumerated() {
            if character == "\"" , !inSingle { inDouble.toggle() }
            if character == "'", !inDouble { inSingle.toggle() }
            if character == "#", !inSingle, !inDouble {
                if previous == nil || previous == " " || previous == "\t" {
                    return String(text[text.startIndex..<text.index(text.startIndex, offsetBy: offset)])
                }
            }
            previous = character
        }
        return text
    }

    /// Take the quotes off a value.
    private static func unquote(_ text: String) -> String {
        if text.count >= 2 {
            let first = text.first
            let last = text.last
            if (first == "\"" && last == "\"") || (first == "'" && last == "'") {
                return String(text.dropFirst().dropLast())
            }
        }
        return text
    }
}

/// The panel's words, kept out of the view so the tests can read them.
enum MiniMaxCopy {
    /// What a proof level means, as a noun phrase.
    ///
    /// The levels are the engine's (`fleet_config/types.py`, `ProofLevel`):  L0 is the
    /// config matching, L1 is the installed hook denying a synthetic write, and L2 is a
    /// headless MiniMax run that shows the platform loads the hook.
    static func levelWords(_ level: String?) -> String {
        guard let level, !level.isEmpty else { return "nothing" }
        switch level {
        case "L0": return "a matching config (L0)"
        case "L1": return "a blocked test write (L1)"
        case "L2": return "a real MiniMax run loading it (L2)"
        default: return level
        }
    }

    /// A check state as a short sentence start.
    static func stateWords(_ state: String) -> String {
        switch CheckState(envelopeValue: state) {
        case .passed: return "Proved"
        case .drift: return "Drifted"
        case .missing: return "Missing"
        case .unproven: return "Not proved"
        case .unsupported: return "Not checkable here"
        case .unknown: return "Unknown"
        }
    }

    /// What the engine proved about one hook, in plain words.
    ///
    /// A hook with no proof reads as unproved rather than as fine:  "installed" and
    /// "protecting" are different states.
    static func proofText(
        _ proof: MiniMaxSurface.HookProof?,
        engineAnswered: Bool = true,
        now: Date = Date()
    ) -> String {
        guard let proof else {
            // The store holds no proofs until the engine has answered, so "no rule" is
            // only true after it has.
            return engineAnswered
                ? "Not proved: the engine has no rule for this hook."
                : "Not proved: the engine has not answered yet."
        }
        let checked: String
        if let checkedAt = proof.checkedAt {
            checked = "Checked \(FleetTime.ageText(since: checkedAt, now: now)) ago."
        } else {
            checked = "The check time is unknown."
        }
        if CheckState(envelopeValue: proof.state) == .passed {
            return ApplyCopy.join("Proved by \(levelWords(proof.achieved)).", checked)
        }
        return ApplyCopy.join(
            "\(stateWords(proof.state)).",
            "Needs \(levelWords(proof.required)), reached \(levelWords(proof.achieved)).",
            checked
        )
    }

    /// Whether a hook's row should stand out:  it has no passing proof.
    static func needsLook(_ proof: MiniMaxSurface.HookProof?) -> Bool {
        guard let proof else { return true }
        return CheckState(envelopeValue: proof.state) != .passed
    }

    /// What is at a hook's script, in plain words.
    static func scriptText(_ hook: MiniMaxSurface.Hook) -> String {
        switch hook.script {
        case .noPath:
            return "no file path in the command, so nothing was checked"
        case .present:
            return "\(hook.scriptPath ?? ""), present"
        case .notExecutable:
            return "\(hook.scriptPath ?? ""), present but not executable"
        case .missing:
            return "\(hook.scriptPath ?? ""), missing"
        }
    }

    /// What a route's agent id turned out to name.
    ///
    /// The roster says a seat is not retired, and nothing more:  it does not say the
    /// seat is running, so a route is never called live.
    static func routeTargetText(_ target: MiniMaxSurface.Route.Target) -> String {
        switch target {
        case .fleetSeat(let seat):
            return "seat \(seat), on the roster"
        case .runtimeAgent(let name):
            return "\(name), an agent on the Mac that is not a fleet seat"
        case .unknown:
            return "no seat on the roster and no agent on the Mac has this id"
        }
    }

    /// Where the engine's answers came from, and how old they are.
    static func engineLine(phase: FleetStore.Phase, generatedAt: Date?) -> String {
        switch phase {
        case .idle:
            return "The engine has not run yet, so nothing here is proved."
        case .running:
            return ApplyCopy.join("The engine is checking now.", "Its answers appear when it finishes.")
        case .failed(let why):
            return "The engine run failed: \(ApplyCopy.normalize(why))"
        case .loaded:
            guard let generatedAt else { return "The engine answered, and its answer carries no time." }
            return "The engine's answers are from its run at \(FleetTime.clock(generatedAt))."
        }
    }

    /// What the engine's-answers section says when it has no cell for the seat.
    ///
    /// The store clears its cells while the engine is idle, running or failed, so "no
    /// row for the seat" is only true once the engine has answered.
    static func verdictsEmptyText(phase: FleetStore.Phase, seat: String) -> String {
        if phase == .loaded {
            return "The engine has no row for seat \(seat), so there is no answer to show."
        }
        return "The engine has not answered yet, so there is no answer to show."
    }

    /// Where the panel's own reading came from, and how old it is.
    static func readLine(_ surface: MiniMaxSurface) -> String {
        ApplyCopy.join(
            "Read from ~/.minimax on the Mac at \(FleetTime.clock(surface.readAt)).",
            "The server's MiniMax install is not shown here."
        )
    }

    /// The line above the list of things that need a look.
    static func headline(count: Int) -> String {
        count == 1 ? "1 thing needs a look." : "\(count) things need a look."
    }

    /// What the hooks section says when it has no row.
    ///
    /// A hook file that could not be read is not "no hook":  the panel says it could
    /// not look, and the problem itself is at the top of the panel.
    static func hooksEmptyText(_ surface: MiniMaxSurface) -> String {
        let unreadable = surface.pluginsFolder == .unreadable
            || surface.userHooksFile == .unreadable
            || surface.plugins.contains { $0.hasHookManifest && !$0.hookManifestValid }
        if unreadable {
            return ApplyCopy.join(
                "No hook is shown because a hook file or folder could not be read.",
                "The problem is listed at the top of this panel."
            )
        }
        return "No plugin manifest and no ~/.minimax/hooks.json on the Mac registers a hook."
    }

    /// What the plugins section says when it has no row.
    static func pluginsEmptyText(_ state: MiniMaxSurface.FileState) -> String {
        switch state {
        case .absent: return "There is no ~/.minimax/plugins folder on the Mac."
        case .unreadable: return "~/.minimax/plugins is there but could not be listed."
        case .read: return "No plugin is installed under ~/.minimax/plugins."
        }
    }

    /// What the config.yaml section says when it has no row.
    static func settingsEmptyText(_ state: MiniMaxSurface.FileState) -> String {
        switch state {
        case .absent: return "There is no config.yaml in ~/.minimax on the Mac."
        case .unreadable: return "config.yaml is there but could not be read."
        case .read: return "config.yaml holds none of the keys this panel shows."
        }
    }

    /// What the agents section says when it has no row.
    static func agentsEmptyText(_ state: MiniMaxSurface.FileState) -> String {
        switch state {
        case .absent: return "There is no ~/.minimax/agents folder on the Mac."
        case .unreadable: return "~/.minimax/agents is there but could not be listed."
        case .read: return "No sub-agent is installed under ~/.minimax/agents."
        }
    }

    /// A list of names in prose:  `A`, `A and B`, `A, B and C`.
    static func list(_ names: [String]) -> String {
        guard names.count > 1 else { return names.first ?? "" }
        return names.dropLast().joined(separator: ", ") + " and " + (names.last ?? "")
    }
}

/// What on the panel needs the owner's look, worst first, as sentences.
///
/// This is the answer to "is MiniMax all right, and what should I do?".  An empty
/// list is earned only by the engine passing every kind for the seat and every file
/// being read:  an engine that has not answered is never read as fine.
enum MiniMaxAttention {
    static func items(
        surface: MiniMaxSurface,
        seat: String,
        engine: [(kind: ConfigKind, state: CellState)],
        engineAnswered: Bool
    ) -> [String] {
        var items: [String] = []
        if !surface.installFound {
            items.append("MiniMax is not installed on the Mac: there is no ~/.minimax folder.")
        }
        // The store clears its cells while the engine is idle, running or failed, so an
        // empty list only means "no row for the seat" once the engine has answered.
        if !engineAnswered {
            items.append("The engine has not answered, so nothing on this panel is proved.")
        } else if engine.isEmpty {
            items.append("The engine has no row for seat \(seat), so nothing on this panel is proved.")
        } else {
            let failingStates: [CellState] = [.missing, .drift, .unproven]
            let failing = engine.filter { failingStates.contains($0.state) }
            if !failing.isEmpty {
                items.append(
                    "The engine does not pass \(MiniMaxCopy.list(failing.map { $0.kind.title })) for seat \(seat)."
                )
            }
            let unsupported = engine.filter { $0.state == .unsupported }
            if !unsupported.isEmpty {
                items.append(
                    "The engine cannot check \(MiniMaxCopy.list(unsupported.map { $0.kind.title })) for seat \(seat)."
                )
            }
            let unknown = engine.filter { $0.state == .unknown }
            if !unknown.isEmpty {
                items.append(
                    "The engine has no answer for \(MiniMaxCopy.list(unknown.map { $0.kind.title })) for seat \(seat)."
                )
            }
        }
        for hook in surface.hooks {
            if let proof = hook.proof {
                if MiniMaxCopy.needsLook(proof) {
                    items.append(
                        "The \(hook.event) hook \(hook.hookId) is \(MiniMaxCopy.stateWords(proof.state).lowercased())."
                    )
                }
            } else {
                // No proof is "installed" without "protecting", never fine.
                let why = engineAnswered ? "the engine has no rule for it" : "the engine has not answered"
                items.append("The \(hook.event) hook \(hook.hookId) is not proved: \(why).")
            }
            if !hook.enabled {
                items.append("The \(hook.event) hook \(hook.hookId) is switched off in its manifest.")
            }
            switch hook.script {
            case .missing:
                items.append("The \(hook.event) hook \(hook.hookId) runs a script that is missing.")
            case .notExecutable:
                items.append("The \(hook.event) hook \(hook.hookId) runs a script that is not executable.")
            case .present, .noPath:
                break
            }
        }
        for route in surface.channels.flatMap({ $0.routes }) where route.target == .unknown {
            items.append(
                "The \(route.channel) route names \(route.agentId), and no seat on the roster or agent on the Mac has that id."
            )
        }
        items += surface.problems
        return items
    }
}

extension Array where Element: Hashable {
    /// The values with duplicates removed, keeping the first occurrence.
    ///
    /// The name is not `uniqued`, because the standard library now carries one and two
    /// of them would shadow each other.
    func distinctValues() -> [Element] {
        var seen: Set<Element> = []
        return filter { seen.insert($0).inserted }
    }
}
