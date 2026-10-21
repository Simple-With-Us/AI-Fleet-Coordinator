import Foundation

/// What the MiniMax surface looks like on this Mac right now.
///
/// This is display data, never a verdict.  The states in the grid come from the engine
/// and from nowhere else;  these panels say what is installed, so a person can judge a
/// drift against something concrete instead of against a summary.
struct MiniMaxSurface {
    /// One hook registration, from a plugin's hook manifest.
    struct Hook: Identifiable {
        let id: String
        let plugin: String
        let event: String
        let matcher: String
        let command: String
        /// The absolute path the command runs, when it names one.
        let scriptPath: String?
        /// Whether that path exists on this Mac.
        let scriptExists: Bool
        let timeout: Int?
        /// The proof the engine reached for this hook, when it has one.
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
                event: event,
                matcher: matcher,
                command: command,
                scriptPath: scriptPath,
                scriptExists: scriptExists,
                timeout: timeout,
                proof: proof
            )
        }
    }

    /// The engine's proof for one hook.
    struct HookProof: Equatable {
        /// The name of the hook the engine proved, which is the script's own name.
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
        let present: Bool
        let schemaVersion: String?
        let detail: String
        /// The routes this file points at, with whether the fleet still answers them.
        let routes: [Route]
    }

    /// One route target.
    struct Route: Identifiable {
        /// What an agent id turned out to name.
        enum Target: Equatable {
            /// A fleet seat tag, which the registry holds.
            case fleetSeat(String)
            /// An agent of the MiniMax runtime on this Mac.
            case runtimeAgent(String)
            /// Neither:  nothing on this Mac answers for that id.
            case unknown
        }

        let id: String
        let channel: String
        let agentId: String
        /// What the agent id turned out to be.
        let target: Target
        /// True when something on this Mac still answers for this route.
        let answered: Bool
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
        /// Whether this directory holds the seat's own durable memory.
        ///
        /// The seat's agent is the one whose directory carries `memory/`;  every other
        /// agent there is a spawned instance with an instance id beside it.
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

    let settings: [Setting]
    let hooks: [Hook]
    let plugins: [Plugin]
    let channels: [Channel]
    let agents: [Agent]
    /// Anything that could not be read, so a missing file is never a silent gap.
    let problems: [String]
}

/// Reads the MiniMax surface from disk.
///
/// Every value here comes from an allowlist of keys or from a structural count:  the
/// reader never prints a whole file, because `config.yaml` carries an `apiKey` and a
/// printed file would print it.
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
    static let channelFiles = [
        "channel-bindings.yaml",
        "channel-routes.yaml",
        "channel-owner.yaml",
        "telegram-channel.yaml",
        "wechat-channel.yaml",
    ]

    /// Read the whole surface.
    ///
    /// `liveSeats` is the fleet's own seat tags, and `seatAliases` maps a runtime
    /// agent id to the seat tag it serves, because a MiniMax route names the runtime
    /// agent (`mavis`) and never the fleet seat tag (`MM`).
    static func read(
        home: URL,
        liveSeats: Set<String>,
        seatAliases: [String: String] = [:],
        proofs: [MiniMaxSurface.HookProof] = []
    ) -> MiniMaxSurface {
        let root = home.appendingPathComponent(".minimax", isDirectory: true)
        var problems: [String] = []
        let settings = readSettings(in: root, problems: &problems)
        let plugins = readPlugins(in: root, problems: &problems)
        let hooks = readHooks(in: root, plugins: plugins, proofs: proofs)
        let channels = readChannels(
            in: root,
            liveSeats: liveSeats,
            seatAliases: seatAliases,
            problems: &problems
        )
        let agents = readAgents(in: root, problems: &problems)
        return MiniMaxSurface(
            settings: settings,
            hooks: hooks,
            plugins: plugins,
            channels: channels,
            agents: agents,
            problems: problems
        )
    }

    /// The runtime agent id that serves the seat on this Mac.
    ///
    /// The MiniMax runtime names its own seat agent `mavis`, while the fleet calls that
    /// seat `MM`, so a route that names `mavis` names a live seat.  The seat's agent is
    /// the one whose directory carries the durable `memory/` tree;  every other agent
    /// there is a spawned instance with an instance id beside it.
    static func seatRuntimeAgentName(home: URL) -> String? {
        let agentsRoot = home.appendingPathComponent(".minimax/agents", isDirectory: true)
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: agentsRoot,
            includingPropertiesForKeys: nil
        ) else { return nil }
        let seatAgents = entries.filter { entry in
            let name = entry.lastPathComponent
            guard !name.hasPrefix(".") else { return false }
            let memory = entry.appendingPathComponent("memory", isDirectory: true)
            let instance = entry.appendingPathComponent(".agent-instance-id")
            return FileManager.default.fileExists(atPath: memory.path)
                && !FileManager.default.fileExists(atPath: instance.path)
        }
        return seatAgents.map { $0.lastPathComponent }.sorted().first
    }

    // MARK: - config.yaml

    private static func readSettings(in root: URL, problems: inout [String]) -> [MiniMaxSurface.Setting] {
        let url = root.appendingPathComponent("config.yaml")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            problems.append("config.yaml could not be read")
            return []
        }
        return allowedSettings.compactMap { key in
            guard let value = scalarValue(in: text, key: key) else { return nil }
            return MiniMaxSurface.Setting(id: key, key: key, value: value)
        }
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

    private static func readPlugins(in root: URL, problems: inout [String]) -> [MiniMaxSurface.Plugin] {
        let pluginsRoot = root.appendingPathComponent("plugins", isDirectory: true)
        let entries = (try? FileManager.default.contentsOfDirectory(
            at: pluginsRoot,
            includingPropertiesForKeys: nil
        )) ?? []
        var plugins: [MiniMaxSurface.Plugin] = []
        for entry in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
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
            let events = parsed?.map { $0.event }.distinctValues().sorted()
            let commands = parsed?.count ?? 0
            plugins.append(
                MiniMaxSurface.Plugin(
                    id: name,
                    name: name,
                    hasHookManifest: true,
                    hookManifestValid: parsed != nil,
                    events: events ?? [],
                    commandCount: commands
                )
            )
            if parsed == nil {
                problems.append("the hook manifest of plugin \(name) is not readable JSON")
            }
        }
        return plugins
    }

    private static func readHooks(
        in root: URL,
        plugins: [MiniMaxSurface.Plugin],
        proofs: [MiniMaxSurface.HookProof]
    ) -> [MiniMaxSurface.Hook] {
        var hooks: [MiniMaxSurface.Hook] = []
        for plugin in plugins where plugin.hasHookManifest {
            let manifestURL = root
                .appendingPathComponent("plugins", isDirectory: true)
                .appendingPathComponent(plugin.id, isDirectory: true)
                .appendingPathComponent("hooks/hooks.json")
            guard let parsed = parseHookManifest(at: manifestURL) else { continue }
            hooks.append(contentsOf: parsed)
        }
        // The engine proves a hook by the name it registered, which is the script's own
        // name and not the whole command line.
        var byName: [String: MiniMaxSurface.HookProof] = [:]
        for proof in proofs { byName[proof.hookName] = proof }
        return hooks.map { hook in
            guard
                let path = hook.scriptPath,
                let proof = byName[(path as NSString).lastPathComponent]
            else { return hook }
            return hook.withProof(proof)
        }
    }

    /// The hook manifest as a flat list of registrations.
    ///
    /// The shape is `hooks` keyed by event, each holding matcher groups, each holding
    /// the commands themselves, so the panel can say what runs, for what, and through
    /// which matcher.
    private static func parseHookManifest(at url: URL) -> [MiniMaxSurface.Hook]? {
        guard
            let data = try? Data(contentsOf: url),
            let object = try? JSONSerialization.jsonObject(with: data),
            let root = object as? [String: Any],
            let events = root["hooks"] as? [String: Any]
        else { return nil }
        var hooks: [MiniMaxSurface.Hook] = []
        for (event, value) in events {
            guard let groups = value as? [[String: Any]] else { continue }
            for group in groups {
                let matcher = group["matcher"] as? String ?? "any"
                guard let entries = group["hooks"] as? [[String: Any]] else { continue }
                for entry in entries {
                    let command = entry["command"] as? String ?? ""
                    let path = command.hasPrefix("/")
                        ? String(command.split(separator: " ").first ?? "")
                        : nil
                    hooks.append(
                        MiniMaxSurface.Hook(
                            id: "\(event)/\(matcher)/\(command)",
                            plugin: url.deletingLastPathComponent().deletingLastPathComponent().lastPathComponent,
                            event: event,
                            matcher: matcher,
                            command: command,
                            scriptPath: path,
                            scriptExists: path.map { FileManager.default.isExecutableFile(atPath: $0) } ?? false,
                            timeout: entry["timeout"] as? Int,
                            proof: nil
                        )
                    )
                }
            }
        }
        return hooks
    }

    // MARK: - channels

    private static func readChannels(
        in root: URL,
        liveSeats: Set<String>,
        seatAliases: [String: String],
        problems: inout [String]
    ) -> [MiniMaxSurface.Channel] {
        var channels: [MiniMaxSurface.Channel] = []
        for file in channelFiles {
            let url = root.appendingPathComponent(file)
            guard let text = try? String(contentsOf: url, encoding: .utf8) else {
                problems.append("\(file) could not be read")
                channels.append(
                    MiniMaxSurface.Channel(
                        id: file,
                        file: file,
                        present: false,
                        schemaVersion: nil,
                        detail: "not on this Mac",
                        routes: []
                    )
                )
                continue
            }
            let schema = scalarValue(in: text, key: "schemaVersion")
            let routes = readRoutes(
                in: text,
                file: file,
                liveSeats: liveSeats,
                seatAliases: seatAliases
            )
            channels.append(
                MiniMaxSurface.Channel(
                    id: file,
                    file: file,
                    present: true,
                    schemaVersion: schema,
                    detail: describeChannel(file: file, text: text),
                    routes: routes
                )
            )
        }
        return channels
    }

    /// The route targets a channel file points at.
    ///
    /// A route names the runtime agent, not the fleet seat:  the MiniMax runtime calls
    /// its own seat `mavis`, while the fleet calls that seat `MM`.  A route is answered
    /// when the id names a live fleet seat, or when it names a runtime agent that maps
    /// to one, and it is never answered by an id nothing on this Mac knows.
    private static func readRoutes(
        in text: String,
        file: String,
        liveSeats: Set<String>,
        seatAliases: [String: String]
    ) -> [MiniMaxSurface.Route] {
        guard file == "channel-routes.yaml" else { return [] }
        var routes: [MiniMaxSurface.Route] = []
        var currentChannel = "default"
        for line in text.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasSuffix(":") {
                let name = String(trimmed.dropLast())
                if !name.isEmpty, !name.contains(":"), name != "rules", name != "defaultRoute" {
                    currentChannel = name
                }
            }
            if trimmed.hasPrefix("agentId:") {
                let agentId = unquote(
                    stripComment(String(trimmed.dropFirst("agentId:".count))).trimmingCharacters(in: .whitespaces)
                )
                routes.append(
                    MiniMaxSurface.Route(
                        id: "\(currentChannel)/\(agentId)",
                        channel: currentChannel,
                        agentId: agentId,
                        target: resolveTarget(agentId, liveSeats: liveSeats, seatAliases: seatAliases),
                        answered: resolveTarget(agentId, liveSeats: liveSeats, seatAliases: seatAliases)
                            != .unknown
                    )
                )
            }
        }
        return routes
    }

    /// What an agent id names.
    private static func resolveTarget(
        _ agentId: String,
        liveSeats: Set<String>,
        seatAliases: [String: String]
    ) -> MiniMaxSurface.Route.Target {
        if liveSeats.contains(agentId) { return .fleetSeat(agentId) }
        if let seat = seatAliases[agentId] {
            return liveSeats.contains(seat) ? .fleetSeat(seat) : .unknown
        }
        return .unknown
    }

    /// A one-line summary of what a channel file holds.
    private static func describeChannel(file: String, text: String) -> String {
        switch file {
        case "channel-bindings.yaml":
            let connections = childCount(in: text, under: "connections")
            let filters = childCount(in: text, under: "messageFilters")
            return "\(connections) connections, \(filters) message filters"
        case "channel-routes.yaml":
            let rules = childCount(in: text, under: "rules")
            let routes = readRoutes(
                in: text,
                file: file,
                liveSeats: [],
                seatAliases: [:]
            ).count
            return "\(rules) rules, \(routes) default routes"
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
                    if !next.hasPrefix(" ") && !next.hasPrefix("\t") { break }
                    let indent = next.prefix(while: { $0 == " " }).count
                    // A line indented no further than the key itself ends the block.
                    if indent <= keyIndent { break }
                    // Only the first level below the key is a direct child.
                    guard indent == keyIndent + 2 else {
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

    private static func readAgents(in root: URL, problems: inout [String]) -> [MiniMaxSurface.Agent] {
        let agentsRoot = root.appendingPathComponent("agents", isDirectory: true)
        let entries = (try? FileManager.default.contentsOfDirectory(
            at: agentsRoot,
            includingPropertiesForKeys: [.contentModificationDateKey]
        )) ?? []
        var agents: [MiniMaxSurface.Agent] = []
        for entry in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            let name = entry.lastPathComponent
            guard name.hasPrefix(".") == false else { continue }
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
            }
            let hasPrompt = files.contains("agent.md")
            let ownsSeatMemory = FileManager.default.fileExists(
                atPath: entry.appendingPathComponent("memory", isDirectory: true).path
            )
            if files.isEmpty {
                problems.append("the definition of agent \(name) holds no files")
            }
            agents.append(
                MiniMaxSurface.Agent(
                    id: name,
                    name: name,
                    files: files.sorted(),
                    hasPrompt: hasPrompt,
                    ownsSeatMemory: ownsSeatMemory,
                    newestChange: newest
                )
            )
        }
        return agents
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
