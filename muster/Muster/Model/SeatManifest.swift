import Foundation

/// The desired-state manifest, `fleet-seats.json`.
///
/// The envelope carries no roster:  a seat with no Mac surface produces no results at
/// all, so the grid reads the manifest for the roster and for the not-applicable covers
/// (the design document, decision D5).  The manifest says what should exist;  the
/// envelope says what does.
struct SeatManifest: Decodable {
    /// The manifest version this app understands.
    ///
    /// The manifest is versioned separately from the status envelope,  and they are
    /// not the same number:  the manifest is at 2 and the envelope is at 1.  A
    /// manifest the app cannot read is refused,  the same way an envelope it cannot
    /// read is refused,  because a partly-read manifest drops fields and a missing
    /// field looks like an absent rule.
    static let supportedSchemaVersion = 2

    let schemaVersion: Int
    let seats: [String: SeatManifestEntry]

    /// The fleet-wide compliance rules,  when the manifest declares them.
    ///
    /// The key is additive and optional (AFC-400):  a manifest from before the
    /// compliance family has none,  and this app reads that manifest unchanged.  The
    /// rules are decoded here so the model knows the family exists,  and the grid
    /// column for them lands in a later phase.
    let complianceRules: [ComplianceRule]?

    /// The manifest,  if it is a version this app reads.
    static func read(at url: URL) throws -> SeatManifest {
        let data = try Data(contentsOf: url)
        let manifest = try JSONDecoder().decode(SeatManifest.self, from: data)
        guard manifest.schemaVersion == supportedSchemaVersion else {
            throw ManifestVersionError.unsupported(manifest.schemaVersion)
        }
        return manifest
    }

    /// A manifest version this app does not read.
    ///
    /// The error names the version it found and the version it reads,  because a
    /// refusal nobody can act on is a refusal nobody acts on.
    enum ManifestVersionError: LocalizedError {
        case unsupported(Int)

        var errorDescription: String? {
            switch self {
            case .unsupported(let found):
                return "the manifest is version \(found);  this app reads version \(SeatManifest.supportedSchemaVersion)"
            }
        }
    }
}

/// One seat's desired state.
struct SeatManifestEntry: Decodable {
    let platforms: [String]
    let rules: [ManifestRule]
    let notApplicable: [ManifestCover]
}

/// One rule of a seat, as the manifest declares it.
struct ManifestRule: Decodable {
    let id: String
    let kind: String
    let platforms: [String]
    /// What the rule checks, and where.
    let check: ManifestCheck?
    /// Where the rule came from:  the directive,  the date or the message that
    /// asked for it.
    ///
    /// This is the citation the fleet asked for,  and it is empty on every rule
    /// today,  so the panel says that it is empty rather than showing nothing.
    let source: String?

    /// The check a rule declares.
    struct ManifestCheck: Decodable {
        /// The kind of check, such as `marked_block_hash`.
        let type: String?
        /// Which block or file within the target the check looks at.
        let detail: String?
        /// The path the check looks at, which may name a file or a directory.
        let target: String?
    }
}

/// One not-applicable cover, which says a kind does not apply to a seat and why.
struct ManifestCover: Decodable {
    let kind: String
    let reason: String
    let platforms: [String]
}

/// One fleet-wide rule, as the manifest declares it.
///
/// A compliance rule is about the fleet rather than about one seat's config surface, so
/// it names the owner seat that answers for it, the hosts it is about, and whether the
/// engine can run it yet.
struct ComplianceRule: Decodable {
    let id: String
    /// The seat that answers for this rule's drift.
    let ownerSeat: String?
    /// The hosts the rule is about.
    let scope: [String]?
    /// Whether the engine has a check for this rule's check type.
    let implemented: Bool?
    /// Why the rule cannot run yet, when it cannot.
    let implementationReason: String?
    /// What the rule checks, and where.
    let check: ManifestRule.ManifestCheck?
}

extension SeatManifest {
    /// The reason this seat and kind are covered by an explicit not-applicable entry.
    ///
    /// A cover with no platforms covers every platform the seat lists;  a cover with
    /// platforms covers only those.  Silence is never a cover:  a seat and kind with
    /// neither a rule nor a cover gets no reason at all.
    func coverReason(seat: String, kind: ConfigKind) -> String? {
        guard let entry = seats[seat] else { return nil }
        let seatPlatforms = Set(entry.platforms)
        for cover in entry.notApplicable where cover.kind == kind.rawValue {
            if cover.platforms.isEmpty { return cover.reason }
            if !cover.platforms.filter({ seatPlatforms.contains($0) }).isEmpty { return cover.reason }
        }
        return nil
    }

    /// The rules the manifest declares for one seat and kind.
    func rules(seat: String, kind: ConfigKind) -> [ManifestRule] {
        guard let entry = seats[seat] else { return [] }
        return entry.rules.filter { $0.kind == kind.rawValue }
    }

    /// The fleet-wide rules the manifest declares,  in manifest order.
    var allComplianceRules: [ComplianceRule] {
        complianceRules ?? []
    }

    /// The fleet-wide rules the engine cannot run yet.
    ///
    /// These are the gaps the registry is honest about, and the panel will show them
    /// rather than leave the fleet's own missing checks invisible.
    var complianceRulesNotImplemented: [ComplianceRule] {
        allComplianceRules.filter { $0.implemented == false }
    }

    /// The artifact the manifest says a seat and kind live in.
    ///
    /// The envelope carries no path,  so the manifest is the only place that knows
    /// which file a cell is about.  A pair with several rules has several artifacts,
    /// and the panel shows them all rather than picking one for the operator.
    func targets(seat: String, kind: ConfigKind) -> [(rule: ManifestRule, target: String, checkType: String?)] {
        rules(seat: seat, kind: kind).compactMap { rule in
            guard let target = rule.check?.target, !target.isEmpty else { return nil }
            return (rule, target, rule.check?.type)
        }
    }
}

/// The fleet registry, `fleet-apps.json`, which holds the roster in registry order.
struct FleetRegistry: Decodable {
    struct Entry: Decodable {
        let tag: String
        let retired: Bool?
        let aliasOf: String?
    }

    let seats: [Entry]

    /// The live roster, in registry order.
    ///
    /// A row counts when it is neither retired nor an alias of another seat, which is
    /// the same call the engine's own loader makes.
    var liveTags: [String] {
        seats
            .filter { ($0.retired ?? false) == false && $0.aliasOf == nil }
            .map { $0.tag }
    }
}

/// Reads the two JSON files the grid needs beside the envelope.
enum FleetDataReader {
    static func loadManifest(at url: URL) throws -> SeatManifest {
        try SeatManifest.read(at: url)
    }

    static func loadRegistry(at url: URL) throws -> FleetRegistry {
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(FleetRegistry.self, from: data)
    }
}
