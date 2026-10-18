# Fleet App Store Rules, Registry, And Icon Audit

Owner request, 2026-10-02: *"so many apps have outdated icons or no icon at all.  Please
find a way to fix the icon thing since it drives me crazy and never seems to get better.
Let's add to the Google Sheet file about App Store information and TestFlight bundles and
stuff, and make sure that top or end of spreadsheet has a rules/standards/policies sort of
section."*

Everything here is generated.  Nothing in the sheet is hand-maintained, because a
hand-maintained sheet is exactly why it never converged.

## Files

| File | What it does |
|---|---|
| `sheet-rules.json` | The fleet's App Store / TestFlight rules as data.  **This is the file to edit** when a rule changes. |
| `../fleet-apps-sheet-sync.mjs` | Regenerates the registry tabs:  `RULES & STANDARDS`, `App Store Registry`, `TestFlight Builds`, `Icon Audit`, `Fleet Apps` and `Seats and Bots`.  Clears each tab before writing it.  `--dry-run` writes nothing; `--no-asc` skips the two App Store Connect tabs. |
| `../fleet-apps-sheet-facts.json` | Curated facts no registry file carries (successor ASC records, seat platforms, hosted seats).  Edit it, then re-run the sync. |
| `../sheet-read.mjs` | Read-only dump of the sheet, for verifying a sync. |
| `fleet-icon-audit.mjs` | The standing icon check.  Exits non-zero on errors. |
| `fleet_icon_fix_alpha.py` | Stages an opaque version of any iOS 1024 icon that is genuinely transparent. |
| `icon-lock.json` | Approved icon hash per app.  Change an icon on purpose, then re-run `--approve`. |
| `ios_testflight_freshness.mjs` | The TestFlight freshness deploy check:  is each app's newest processed build behind its native tree on main?  `--ship-behind` dispatches the app's own ship workflow under the fleet guards;  `--alert` posts to Zulip.  Runs every two hours as the LaunchAgent `com.jay.ios-testflight-freshness`.  See `docs/ios-testflight-freshness.md`. |
| `testflight-apps.json` | The freshness check's registry (appleId, repo, native paths, ship workflow, compile check).  Edit it when an app or its pipeline changes. |
| `install-testflight-freshness.sh` | Installs the check and registry into `~/apps/ios-fleet` and loads the LaunchAgent. |

## The Google Sheet

`https://docs.google.com/spreadsheets/d/1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY/edit`

Strictly private.  Service account `fleet-sheets-sync@jay-wedgeworth.iam.gserviceaccount.com`,
key at `~/.secrets/google-sheets-sa.json`.  Never make it public and never query it anonymously.

Runs from an AI-Fleet-Coordinator lane at origin/main (never from a stale copy; an old copy
rolled the sheet back before):

```bash
~/apps/lane new AFC <slug>        # then, inside the lane:
node scripts/fleet-apps-sheet-sync.mjs --dry-run   # live vs new row counts, writes nothing
node scripts/fleet-apps-sheet-sync.mjs             # backs up the workbook, then rewrites the tabs
```

Then run the ops half from a Fleet-OPS lane:  `node scripts/fleet-sheet/fleet-ops-sheet-sync.mjs`.

`ios-fleet/publish-ios-versions.sh` calls `~/apps/fleet-apps-sheet-sync.mjs` after a
successful manifest push.  That file is a guarded shim:  it runs
`~/Code/AI-Fleet-Coordinator/scripts/fleet-apps-sheet-sync.mjs` only when that tree is clean
and at origin/main, and otherwise exits non-zero with the lane command.

## Why the rules block is generated

The old sync wrote a fixed `A1:N` range, so *any* hand-typed row was destroyed on the next
run.  That is precisely why the sheet could never hold a stable standards section.  The
rules block is now rendered from `sheet-rules.json`, so it is impossible to clobber.

The old tab also had four hand-added rows (rows 18-21) written against a completely
different column layout.  They were kept as `Legacy pre-2026-10-02` for a while; that tab
no longer exists.

## Why the repo, and not App Store Connect, is the icon source of truth

Verified against the ASC API on 2026-10-02: `appInfos` exposes only `ageRatingDeclaration`,
`appInfoLocalizations`, and category relationships.  There is **no app-icon resource** — you
cannot even read the icon back.  And for a TestFlight build, the icon the tester sees is
baked into the uploaded **binary**, not read from App Store Connect.  So the repo's asset
catalog is the only place the icon can be checked, and it is the only place a fix lands.

## Severity is measured, not guessed

An early version of the audit read the PNG colour-type byte and reported "alpha channel" as
an App Store rejection.  That produced a **false positive**: Autorotate's iOS 1024 is
colour-type 6 (RGBA) but every pixel is opaque.  A guard that fires on a healthy file is
worse than no guard, because people learn to ignore it.

The audit now measures actual transparency with Pillow:

- `ICON_TRANSPARENT` (error) — a real share of pixels is see-through.  Apple rejects this as
  ITMS-90717 and the app ships with no valid icon.  **CodeCaps and Hog Hunter, 4.8%.**
- `ICON_ALPHA_CHANNEL` (warn) — alpha channel present but 100% opaque.  Harmless today;
  strip it so no validator flags it.  **Autorotate.**
- macOS icon sets are never flagged.  macOS icons are *required* to carry alpha (rounded
  rect, vibrancy); flattening them would break the Mac app.

## Release gate

`publish-ios-versions.sh` runs the audit before publishing and blocks on an error:

```
FLEET_ICON_AUDIT=block   # default: refuse to publish an app with a transparent icon
FLEET_ICON_AUDIT=warn    # report and ship anyway
FLEET_ICON_AUDIT=off     # skip the check
```

## Drift detection

`icon-lock.json` records the approved hash per app.  Any later change shows as `DRIFT` in
the Icon Audit tab until somebody deliberately re-approves.  That is what stops the slow
rot: a rebrand that changes the icon can no longer land silently.

The audit also compares lane worktrees under `~/apps` (`~/apps/lanes/<Repo>/<seat>-<slug>`, and the older
locations until the migration moves them) against the canonical tree, which is how the unlanded Hog Hunter
icon branches were found.  It asks `git worktree list` for them, so it does not depend on folder names.

## Scan hygiene

Vendor checkouts live inside repo working trees — for example
`ContactLogo/build/derivedData/SourcePackages/checkouts/sentry-cocoa/Samples/`.  A naive
recursive glob surfaces a dozen phantom `AppIcon` sets from Sentry's sample apps, and agent
worktrees under `.claude/worktrees/` duplicate the canonical tree.  Both are pruned.

## Re-approving after an intentional icon change

```bash
python3 fleet_icon_fix_alpha.py --app <key>          # stage the fix, inspect it
# copy the staged file over the source in your worktree, commit
node fleet-icon-audit.mjs --approve                  # accept the new icon; land icon-lock.json by PR
node ../fleet-apps-sheet-sync.mjs                    # refresh the sheet, from an AFC lane at origin/main
```
