# 2026-09-03 — iOS version manifest moves to AI-Fleet-Coordinator

Owner is deleting `Simple-With-Us/ios-app-versions`.  That repo existed as a
one-file public JSON feed for the in-app update prompt (`AppUpdatePrompt.swift`)
and for `publish-ios-versions.sh` after a TestFlight ship.  It was never a
product, and it should not be a public GitHub profile row.

It was also already dead as a live tracker.  Last commit on that repo is
2026-08-25 (`trade.congress.ios` 1.0.162).  Later TestFlight ships (Congress
past 1.0.220) never landed there.  App Store prompts already use Apple's
iTunes Lookup API (`itunes.apple.com/lookup?bundleId=`).  The JSON was only
the TestFlight side-channel, and ships stopped writing it.

## Personal-Site

`jays.services` does **not** list or fetch `ios-app-versions`.  Project cards
in `site/src/lib/site.ts` are a hardcoded allowlist.  Live `jays.services`,
`personal-site-jayw.vercel.app`, and `jaywedgeworth.com` have no match.  Do
not host this JSON on Personal-Site.

## New home

Canonical public file:

`site/ios-versions.json` in `Simple-With-Us/AI-Fleet-Coordinator`

Runtime URL used by iOS / macOS clients:

https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/site/ios-versions.json

GitHub Pages will also serve it at
https://simple-with-us.github.io/AI-Fleet-Coordinator/ios-versions.json
after the next digest/Pages deploy.  Clients use the raw URL so a Contents-API
PUT is visible immediately.

`publish-ios-versions.sh` now PUTs `site/ios-versions.json` on this repo, not
the deleted one-file repo.  Local per-app `scripts/ios-fleet/ios-app-versions.json`
mirrors stay as stale fixtures / caches.  This file is only as fresh as the
last successful ship publish.  It is not an App Store Connect poll.

## Why not Personal-Site

The portfolio site is a visitor surface.  The manifest is fleet ship
infrastructure.  Coordinator already has public GitHub Pages and is already a
listed project on the site.

## Already-installed apps

Shipped TestFlight / App Store builds still fetch the old raw URL until a new
build lands.  Failures in `AppUpdatePrompt` are silent.  After this lands,
ship the iOS apps so the new URL is compiled in.  The owner can delete
`ios-app-versions` once the new file is on `main`; old builds will stop
prompting until they update.

## Verification

```bash
curl -fsS https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/site/ios-versions.json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["schemaVersion"]==1 and d["apps"]'
```
