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
| `../scripts/fleet-apps-sheet-sync.mjs` | Regenerates the sheet: `RULES & STANDARDS`, `App Store Registry`, `TestFlight Builds`, `Icon Audit`, plus the preserved `Legacy pre-2026-10-02` tab. |
| `../scripts/sheet-read.mjs` | Read-only dump of the sheet, for verifying a sync. |
| `fleet-icon-audit.mjs` | The standing icon check.  Exits non-zero on errors. |
| `fleet_icon_fix_alpha.py` | Stages an opaque version of any iOS 1024 icon that is genuinely transparent. |
| `icon-lock.json` | Approved icon hash per app.  Change an icon on purpose, then re-run `--approve`. |

## The Google Sheet

`https://docs.google.com/spreadsheets/d/1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY/edit`

Strictly private.  Service account `fleet-sheets-sync@jay-wedgeworth.iam.gserviceaccount.com`,
key at `~/.secrets/google-sheets-sa.json`.  Never make it public and never query it anonymously.

Runs automatically from `ios-fleet/publish-ios-versions.sh` after a successful manifest
push.  Run it by hand any time:

```bash
node /Users/jay/apps/fleet-apps-sheet-sync.mjs
```

## Why the rules block is generated

The old sync wrote a fixed `A1:N` range, so *any* hand-typed row was destroyed on the next
run.  That is precisely why the sheet could never hold a stable standards section.  The
rules block is now rendered from `sheet-rules.json`, so it is impossible to clobber.

The old tab also had four hand-added rows (rows 18-21) written against a completely
different column layout.  They are preserved untouched as `Legacy pre-2026-10-02`.

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
node fleet-icon-audit.mjs --approve                  # accept the new icon
node /Users/jay/apps/fleet-apps-sheet-sync.mjs       # refresh the sheet
```
