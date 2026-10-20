# Muster

Muster is a native SwiftUI macOS 14+ scaffold.  Its initial window explicitly reports unavailable fleet status because no data source is connected.

## Scope

- `Muster/` contains only the app entry point and the fleet overview view.
- `project.yml` is the XcodeGen source of truth.  The generated Xcode project is ignored.
- `script/build_and_run.sh` owns local project generation, signing, installation, and launch.
- The repository's `.codex/environments/environment.toml` exposes the same script through the Codex **Run** action.

The app makes no network calls, invokes no CLI, and reads no credentials.  It has no sample fleet records or inferred health state.  Collector, schema, and CLI integration remain separate work.

## Prerequisites

- macOS 14 or later.
- Full Xcode, selected with `xcode-select`, including its macOS SDK and command-line tools.
- XcodeGen 2.38 or later (`brew install xcodegen`).
- Python 3 for public signing-identity selection.
- A valid **Developer ID Application** identity and its usable private key in the local keychain.  The script never exports private key material.

The script requires exactly one matching valid Developer ID Application identity.  When several exist, set `MUSTER_SIGNING_IDENTITY` to the exact certificate name or SHA-1 fingerprint to select one; it fails for a missing or ambiguous match.  Team and certificate identifiers are discovered locally, not committed.

## Build And Run

From the repository root:

```sh
./muster/script/build_and_run.sh --verify
```

The default Release configuration builds `services.jays.Muster` with Developer ID signing and hardened runtime enabled.  App Sandbox is disabled to leave room for the separately owned CLI integration; the app currently has no capabilities or custom entitlements.

The script installs the sole normal app copy at `~/Applications/Muster.app`, launches that bundle, and verifies its exact executable path with `--verify`.  Its `dist/` staging directory is removed when the script exits, including on failure.  Installation prepares and verifies a copy on the destination filesystem before replacing the installed bundle, and restores the previous bundle if replacement or verification fails.  Incremental build data stays under ignored `muster/.build/`.

If Muster is already running, the script stops before building.  Quit it manually, or explicitly authorize this invocation to terminate only the installed copy:

```sh
./muster/script/build_and_run.sh --verify --relaunch
```

The script never force-kills a process.  A second concurrent invocation fails against a local lock; after an interrupted process, inspect whether a build is still active before removing `muster/.build/run.lock`.

Optional modes are `--logs`, `--telemetry`, and `--debug`.  Log modes stream unified logs after launch; the scaffold has no custom telemetry events yet.  The build lock and staging are released before a log stream or debugger blocks.  Debug mode attaches LLDB to the installed process and is subject to macOS Developer Tools permissions and hardened-runtime debugger restrictions.

Full local builds follow the repository build coordination protocol.  The script is an on-demand developer helper, not an always-on service.

## Development Identity

```sh
./muster/script/build_and_run.sh --development --verify
```

Debug uses `services.jays.Muster.dev`, a distinct development bundle ID.  Both configurations deliberately install to the same `~/Applications/Muster.app` path so they cannot leave multiple normal app copies.  Run the default Release command after quitting a development build to replace it with the stable identity.

These bundle IDs are scaffold choices pending product identity onboarding.  They are not a finalized fleet registry, Apple portal, or distribution registration.

## Verify Locally

Project generation and shell validation do not compile the app:

```sh
bash -n muster/script/build_and_run.sh
xcodegen generate --spec muster/project.yml
```

After a successful `--verify` run, independently inspect the installed signature and bundle metadata:

```sh
codesign --verify --strict --deep ~/Applications/Muster.app
codesign -d --verbose=4 ~/Applications/Muster.app
/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' ~/Applications/Muster.app/Contents/Info.plist
test ! -d muster/dist
```

Expect a Developer ID Application authority and the `runtime` signing flag.  Inspect the window for the light appearance and unavailable fleet status.  Native Mac UI verification follows repository policy through code review and CI; process verification alone does not prove rendered appearance.

## Delivery And Onboarding Limits

Local Developer ID signing is established from the first build.  This scaffold does not implement notarization, stapling, release packaging, an updater, hosted CI signing, or a public release.  A locally verified signature is not evidence of notarization or completed product onboarding.

Muster currently lives inside an existing application repository.  The current onboarding flow does not directly fit this nested product; use the existing product registry rather than creating duplicate infrastructure.  Product identity, registration applicability, and future onboarding remain pending.  Only authorized maintainers may perform that setup.
