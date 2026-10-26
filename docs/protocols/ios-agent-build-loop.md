# iOS agent build loop (owner ruling 2026-08-13 — ALL seats, ALL apps)

> Moved from `FLEET-PROTOCOL.md` on 2026-09-01 (Plan B slice 2, doc diet).  Still binding for every agent on every platform.  Canonical pointer stays in FLEET-PROTOCOL.md; this file is the full text and is ingested into the fleet-agents corpus (`recall`).

Owner: do **not** stand up, debug, or "fix" Xcode MCP (`XcodeBuildMCP`, `xcrun mcpbridge`, `build_sim`).  We are not using that path.

**`xcodebuild` and `xcrun simctl` via the shell are the default, pre-approved tools.**  Run them.  Do not ask permission.  Do not pause to explain that MCP is missing.  Do not write a paragraph about why you cannot use `build_sim`.  Never block bash `xcodebuild` — that is the only iOS compile loop this fleet uses.

**Verify:** a simulator screenshot (`xcrun simctl io booted screenshot …`) is required before claiming a user-visible iOS client change is done.  `BUILD SUCCEEDED` is not visual QA.  Discover simulators with `xcrun simctl list devices available` — do not hardcode a device name.

**Do not hand-edit** `.pbxproj`, anything inside `.xcodeproj/` or `.xcworkspace/`, `.xib`, `.storyboard`, or `.entitlements`.  Create the `.swift` file and report that it needs target membership.  Where the app uses XcodeGen (ST `ios/project.yml`, UM `ios/UsageMonitor/project.yml`), edit `project.yml` and run `xcodegen generate` — do not patch the generated `project.pbxproj` by hand.  Claude seats: a PreToolUse hook (`.claude/hooks/block-xcode-project-writes.py`, template in AI-Fleet-Coordinator + `/Users/jay/apps/ios-fleet/`) enforces the write block.

Architecture unless the file you are editing already differs:

- `@Observable` + `@MainActor` on stores (never `ObservableObject`)
- `NavigationStack` + value-based `NavigationLink` (never `NavigationView` / destination-closure links)
- Light is the product default theme
- Two spaces between sentences in every human-readable string, including App Store listing and review notes (see § Two spaces between sentences)

Signing / TestFlight last-mile stays `scripts/ios-ship-testflight.sh` + `/Users/jay/apps/ios-fleet/README.md`.  Do not debug code-signing by guessing.

**After a merge that touches an app's native iOS tree (owner 2026-10-17 — ALL seats).**  The deploy is not verified until the TestFlight freshness check (`node ~/apps/ios-fleet/ios_testflight_freshness.mjs --app <key>`;  guide in `docs/ios-testflight-freshness.md`) reports that app current.  A merged PR or a green compile check does not count.  Where to look when it reports behind (ship workflow run, runner, rate gate, compile check) and the rules that matter (What to Test with no seat or agent names, patch version `1.0.N` on every upload, TestFlight uploads authorized but App Store submission not) are in the `deploy-verify` skill, section 5.  The `ios-ship` skill (a claude.ai account skill, not part of the fleet pack) names this document as its canon, so this paragraph is where that skill's cross-reference lands.

Per-app iOS onboarding (annotated file tree + scheme): `ios/CLAUDE.md`, `clients/ios/CLAUDE.md`, or `native/ios/CLAUDE.md`.

**iOS Debug vs TestFlight (owner 2026-08-21 — ALL seats).**  Do the Xcode-console kind of debug **as autonomously as possible**.  Ask the owner only when the phone, signing, or a gesture is actually blocking.  Helper (on-demand, not a daemon): `bash /Users/jay/apps/ios-fleet/ios-debug.sh <app>`.  Tracked copy: `AI-Fleet-Coordinator/scripts/ios-debug.sh`.

Do **not** open the Xcode GUI and do **not** make the owner press Run as the default.  The helper is the console: simulator `simctl launch --console` (print/NSLog) plus `log stream` / device `log collect`.  `xcrun devicectl` can also screenshot and launch on a paired phone.

When to use which path:

1. **Simulator Debug (default).**  UI, layout, most logic, `print` / `os_log`.  `ios-debug.sh` with default `--target simulator`.  Screenshot stays required for user-visible changes.
2. **Device logs, keep TestFlight.**  Phone-only bug (push, IAP, background, Keychain, camera, Health, entitlements) where TestFlight/Release is the truth.  `--target device --logs-only`.  Does **not** replace the TestFlight install.  Unified logs only — `print()` from a Release/TestFlight build often never appears.
3. **Device Debug install.**  Only when we need `#if DEBUG`, an unreleased binary, or a debugger-attached equivalent on the phone.  `--target device --install-debug`.  This **replaces** TestFlight for that bundle until the owner reinstalls from TestFlight.  Say that in the turn you do it.
4. **Owner presses Run in Xcode.**  Last resort: LLDB (`po`, breakpoints, pause-on-exception) or the IDE console still has the smoking gun after (1)–(3).  Ask for a paste of that pane rather than narrating MCP.

Ask the owner (short `NEED OWNER:` line) only for: plug/unlock/trust the phone, enable Developer Mode, Allow/Touch ID on a signing dialog, or “reproduce this tap now, I have N seconds of logs.”  Do not wait for them to start a Debug session if the helper can run.

Do **not** treat an Xcode Run as a substitute for a TestFlight repro.  Debug + development signing is a different binary.  Known trap: ST push sandbox vs `#if DEBUG` (TestFlight is not DEBUG).

---

