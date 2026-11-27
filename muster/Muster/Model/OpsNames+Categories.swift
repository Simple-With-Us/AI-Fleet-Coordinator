import Foundation

/// How the menu bar groups Mac services into non-privileged category submenus.
public enum FleetServiceCategory: Equatable, Sendable {
    case core
    case platformBridge
}

extension FleetServiceDescriptor {
    /// Category used by the menu bar so platform bridges share one dedicated submenu.
    public var menuCategory: FleetServiceCategory {
        switch id {
        case "agy-acp", "grok-leader":
            return .platformBridge
        default:
            return .core
        }
    }
}

extension FleetServiceEntry {
    /// Category used by the menu bar.
    public var menuCategory: FleetServiceCategory { descriptor.menuCategory }
}

extension OpsNames {
    /// Server systemd units that act as platform bridges / remote-agent adapters.
    static let serverPlatformBridgeUnits: Set<String> = [
        "afc-wa-bridge.service",
        "clutch-chat-bridge.service",
        "claude-remote-control.service",
        "opencode-server.service",
    ]

    /// True when the server systemd unit belongs in the Platform Bridges submenu.
    static func isServerPlatformBridge(_ unit: String) -> Bool {
        serverPlatformBridgeUnits.contains(unit)
    }

    /// Mac LaunchAgent labels that act as platform bridges / wake-delivery adapters.
    static let macPlatformBridgeAgents: Set<String> = [
        "com.jay.claude-remote-control",
        "com.jay.lark-mm-bridge",
        "com.jay.operator-wake-alerter",
    ]

    /// True when the Mac LaunchAgent belongs in the Platform Bridges submenu.
    static func isMacPlatformBridgeAgent(_ label: String) -> Bool {
        macPlatformBridgeAgents.contains(label)
    }

    /// Human-readable menu title for a server systemd unit.
    static func menuUnitTitle(_ unit: String) -> String {
        switch unit {
        case "afc-wake-router.service": return "Wake Router"
        case "afc-wake-tls.service": return "Wake TLS Proxy"
        case "fleet-vm-status.timer": return "VM Status Report"
        case "operator-sheets-drain.timer": return "Operator Sheets Drain"
        case "fleet-coding-memory.service": return "Coding Memory"
        case "shellular.service": return "Shellular"
        case "congress-health-recover.service": return "Congress.Trade Recovery"
        case "ct-deploy-overlap.service": return "Congress.Trade Deploy Overlap"
        case "afc-wa-bridge.service": return "WhatsApp Bridge"
        case "clutch-chat-bridge.service": return "Clutch Chat Bridge"
        case "claude-remote-control.service": return "Claude Remote Control"
        case "opencode-server.service": return "OpenCode Server"
        case "codex-operator-coordination-check.timer": return "Codex Coordination Check"
        case "lane-placement-watch.timer": return "Lane Placement Watch"
        case "lane-auto-retire.timer": return "Lane Auto-Retire"
        case "fleet-rag-nightly.timer": return "Fleet RAG Nightly"
        case "usage-monitor-replica-status.timer": return "Usage Monitor Replica Status"
        case "ct-deploy-guard.timer": return "Congress.Trade Deploy Guard"
        default: return unitTitle(unit)
        }
    }

    /// Human-readable menu title for a Mac LaunchAgent label.
    static func menuAgentTitle(_ label: String) -> String {
        switch label {
        case "com.jay.claude-remote-control": return "Claude Remote Control"
        case "com.jay.lark-mm-bridge": return "Lark Bridge"
        case "com.jay.merge-shepherd": return "Merge Shepherd"
        case "com.jay.provider-knob-sync": return "Provider Knob Sync"
        case "com.jays.antigravity-usage-collector": return "Antigravity Usage Collector"
        case "com.jays.mac-server-watchdog": return "Mac Server Watchdog"
        case "com.jays.subscription-quota-collector": return "Subscription Quota Collector"
        default: return agentTitle(label)
        }
    }
}
