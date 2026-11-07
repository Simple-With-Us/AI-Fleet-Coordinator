# Muster Auto-Update (Sparkle 2)

Owner ruling, Tue, Sep 29, 2026:  every fleet Mac app updates itself through Sparkle 2 from releases CI signs, notarizes and staples on each merge to main.  A hand build is never the delivery route.  CodeCaps' `docs/AUTO-UPDATE.md` is the canonical pattern and Hog Hunter's copy is the one Muster follows.  This page says what Muster adds.

## What Happens

1. A PR that changes `muster/` merges to main.
2. `.github/workflows/muster-release.yml` runs on a GitHub-hosted Mac.  It builds a universal Release bundle, stamps it, signs it with the Developer ID certificate (hardened runtime, secure timestamp, Sparkle's helpers signed inside-out), notarizes and staples the app and the DMG, and writes an appcast whose one item is the new ZIP, signed with Muster's own EdDSA key.
3. It publishes a release tagged `muster-v<version>-build.<N>` with the ZIP, the DMG and their `.sha256` files, never marked latest.  Then it replaces the one asset of the fixed `muster-feed` release, `appcast.xml`, and confirms the live feed names build N, waiting up to five minutes for GitHub's download cache.
4. Every installed copy reads `https://github.com/Simple-With-Us/AI-Fleet-Coordinator/releases/download/muster-feed/appcast.xml` once an hour, downloads the new ZIP in the background, checks its EdDSA signature before unpacking it, and checks that the new app is signed by the same Developer ID team.
5. The install waits in `UpdateInstallGate` until nothing Muster started is still running and Muster is not the app in front.  Then Sparkle installs it and relaunches Muster.

A pull request that touches `muster/` runs a separate job with a read-only token, builds and verifies the bundle the same way with an ad-hoc signature, and publishes nothing;  the job that can write releases never runs for a pull request.

## The Feed, And Why It Is Not `releases/latest`

CodeCaps and Hog Hunter each own their repository, so `releases/latest/download/appcast.xml` is theirs.  This repository holds many things, and a feed that follows "latest" would follow any release anyone made here later.  On Thu, Nov 5, 2026 the repository had no releases and no tags (`gr_wf_release_list` and `gr_wf_repo_tags` both empty), and nothing in `.github/workflows` or `scripts/` publishes a GitHub release:  `scripts/fleet-cli-release.py` installs releases on a host's own disk and never touches GitHub.  That could change, so Muster's feed is a fixed tag that only `muster-release.yml` writes, `muster-feed`, and each build's own release is created with `--latest=false`, so Muster never becomes the repository's latest release either.  The repository's GitHub Pages site was not used, because `fleet-activity-site.yml` owns what that site serves.

Even a feed someone replaced could not install anything:  Sparkle refuses an archive whose EdDSA signature does not match the public key built into the app.

## Holding The Install While Muster Is Busy

`Muster/Model/AutoUpdatePolicy.swift` has the gate, and `Muster/AutoUpdate.swift` connects it to Sparkle.

| Sparkle hands over | Waits for |
|---|---|
| The automatic install (`willInstallUpdateOnQuit`) | No work in flight, and Muster not in front |
| A relaunch, including Install And Relaunch the owner chose (`shouldPostponeRelaunchForUpdate`) | No work in flight |

Work in flight is an apply on the app-lifetime `ApplyCoordinator` (planning, applying or re-proving), an `OpsController` run (planning, running or re-checking a receipt), and a `ServiceActionRouter` action on the direct route (from the click to its result, its alert included) or a service FleetAdmin is acting on.  An open plan sheet with nothing running does not hold it.  The gate looks again whenever any of those change and when Muster stops being in front.  Settings says what an update is waiting for, and the menu says only Update Waiting.  `MusterTests/UpdateInstallGateTests.swift` proves the gate, including with a real `ApplyCoordinator` whose plan is still running.

## Build Identity

The build stamps its Info.plist through `muster/Support/Info.plist`, merged into the generated one:

| Key | Value |
|---|---|
| `CFBundleVersion` | The commit's time in UTC, `YYYYMMDDHHMM` |
| `MusterCommit` | The full commit |
| `MusterCommitTime` | The commit's time, ISO 8601 in UTC |
| `MusterBuildChannel` | `release` (CI from main), `main` (a hand build of a clean main commit), `lane` (anything else by hand), `development` (Debug) |

Settings ▸ This Build and the foot of the menu bar menu show the version, build and short commit, with Development Build, Not From Main or Commit Not Recorded when that applies.  Times are shown on the owner's clock (America/Chicago, 12-hour).

The build number is UTC and not the Mac's zone because the clock falls back an hour every November, and a later merge must never get a smaller number:  Sparkle offers only a larger one.  The copy installed before this change carries an America/Chicago number, which is always smaller than the UTC number for the same moment, so the change never strands it.  `script/build_and_run.sh` refuses to install a build numbered below the installed one unless `--allow-older` is given.

### The Checkout Beside The App

Muster runs its Python from a checkout on the Mac (`fleetConfigRepoRoot`, then `MUSTER_REPO_ROOT`, then `~/Code/AI-Fleet-Coordinator`, and the engine checkout `EngineLocator` finds when that is a different one).  That checkout and the app update on different tracks:  the checkout follows main within minutes, and the app follows the release feed within the hour.  Settings and the menu show the checkout's commit, read from its git files without running git, beside the app's, and say plainly when they differ.  The rule for changing a report's format across the two tracks is in `docs/protocols/fleet-tool-parity.md`, Changing A Report's Format.

## Secrets

Names only;  no value is ever printed.

| Name | Where | Read with |
|---|---|---|
| `MAC_CERT_P12_BASE64`, `MAC_CERT_PASSWORD` | Fleet-OPS prod `/mac-certs` | `INFISICAL_MAC_CERTS_READ_TOKEN` and `INFISICAL_FLEET_OPS_PROJECT_ID` |
| `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8` | Fleet-OPS prod `/asc` | `INFISICAL_ASC_READ_TOKEN` and `INFISICAL_FLEET_OPS_PROJECT_ID` |
| `MUSTER_SPARKLE_ED_PRIVATE_KEY` | AI Fleet Coordinator prod `/muster-sparkle` | `INFISICAL_MUSTER_SPARKLE_READ_TOKEN` |
| `MUSTER_SPARKLE_PUBLIC_KEY` | `muster/project.yml`, Release config (public) | |

`muster/script/load_release_secrets.mjs` reads each folder through its own token with `scripts/infisical-fetch.mjs`, which checks that the token is read-only and scoped to exactly that project, environment and folder.  The certificate, the App Store Connect key and the Sparkle key reach the later steps only as 0600 files under the runner's temporary folder;  the password and the two key ids are masked before they reach the job's environment.  A token that is absent is named and the job builds without publishing.  A token that is present and fails its scope check fails the job, so a broken credential never looks like a missing one.

Each read token is its own, per the service-token rule:  one consumer, one project, one environment, one folder, read-only.  The tokens Hog Hunter and CodeCaps use are theirs and are not copied here.  The workflow's first step prints which of these names this repository's Actions hold, as set or missing, never a value.

## Keys

Muster has its own EdDSA key pair, so no other app's key can sign a Muster update.  It was made on Fri, Nov 6, 2026 on the server by the CLAUDE seat, in memory, with no copy on disk:

- The private half is `MUSTER_SPARKLE_ED_PRIVATE_KEY` in AI Fleet Coordinator prod `/muster-sparkle`, in Sparkle's export format (base64 of the 32-byte Ed25519 seed), which `generate_appcast --ed-key-file` reads.  It is in no keychain and no repository secret.
- The public half is `MUSTER_SPARKLE_PUBLIC_KEY` in the Release config of `muster/project.yml`.  `make_appcast.sh` derives the public key from the signing key (`script/sparkle_public_key.swift`) and refuses an archive whose `SUPublicEDKey` differs, so a mix-up fails in CI and nothing is published.

The three read tokens are Muster's own, each read-only and scoped to one folder, and each expires on Wed, May 5, 2027:

| Repository secret | Infisical token | Reads | Expires (UTC, from Infisical's metadata) |
|---|---|---|---|
| `INFISICAL_MAC_CERTS_READ_TOKEN` | `muster-ci-mac-certs-read` | Fleet-OPS prod `/mac-certs` | 2027-05-05T07:54:20Z |
| `INFISICAL_ASC_READ_TOKEN` | `muster-ci-asc-read` | Fleet-OPS prod `/asc` | 2027-05-05T07:54:23Z |
| `INFISICAL_MUSTER_SPARKLE_READ_TOKEN` | `muster-ci-sparkle-read` | AI Fleet Coordinator prod `/muster-sparkle` | 2027-05-05T07:54:25Z |

Each token's value is kept in AI Fleet Coordinator prod `/github-actions` under the repository secret's name, and the Infisical secret sync `ai-fleet-coordinator-gh-actions` (non-destructive, automatic) copies that folder into this repository's Actions secrets.  So a token is rotated by minting the replacement, writing it to `/github-actions` under the same name, letting the sync run, and then revoking the old token.  Before the tokens expire, rotate them, or releases stop with a scope-check failure.  Every token is a row in the Infisical Service Tokens registry.

Verification record (Fri, Nov 6, 2026):

- Key:  the private key was read back from Infisical byte-for-byte equal to the generated one, the public key above was derived from that stored copy, and a sign and verify self-test passed.  Every release repeats the derivation in `make_appcast.sh`.
- Tokens:  each token's project, single prod folder, `read` permission and expiry were read from Infisical's own metadata (`/api/v2/service-token`), each token listed its folder's expected names, and a read aimed at another folder returned nothing from it.  Each has a row in the Infisical Service Tokens registry (rows 82 to 84 on that day), which holds its id.
- Sync:  `ai-fleet-coordinator-gh-actions` reported `succeeded` right after the three writes.  A `workflow_dispatch` run on main started after the sync finished with its credential step green;  its log could not be read from the server, so which names it reported as set is not confirmed.
- Not yet proven here:  a published release.  The first main run that carries this key is that proof;  until it publishes and the feed names its build, treat the route as unverified end to end.

If the private key is lost, installed copies can no longer update and need one install by hand of a build with a new public key.  If it leaks, rotate:  ship one release signed with the old key that carries the new public key, then switch the secret.

## The First Install

The Muster on the owner's Mac was built by hand and has no Sparkle, so it cannot update itself.  One Sparkle-enabled build has to reach `~/Applications/Muster.app` once.  That is a Mac seat's step, not the owner's:  a Mac seat installs the ZIP from the first published `muster-v…` release (or runs `script/build_and_run.sh --relaunch` from main), then confirms that Settings shows Automatic Updates: On and the release's commit.  Install it to `~/Applications/Muster.app`, where the earlier copy is, so the Mac keeps one copy.

## The Developer Route

`script/build_and_run.sh` stays the way to try an unmerged lane on a Mac.  It stamps the build as `lane` (Not From Main) or `development`, and its number is the lane commit's time.  A Release lane build carries the feed, so the next release from main with a larger number replaces it on its own;  a Development build carries no feed and never updates.

## Rollback

Sparkle never goes backward, so a rollback is a fix forward:  revert the bad PR on main, and the release workflow publishes the old code under a larger build number, which every copy takes within the hour.  To pause releases, disable the workflow.  A build that cannot update itself (one that crashes before the updater starts) needs one install by hand of the fixed release's ZIP.
