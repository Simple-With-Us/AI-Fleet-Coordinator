# 2026-09-14 — Safari start page Settings and real Mac defaults

Board `970746b9`.  Branch `fx/safari-start-settings`.

Claude's start page landed in AFC #221 as a `file://` page, but Mac new tabs still opened Apple's Start Page.  `defaults write com.apple.Safari` does not stick on current Safari; the keys live in the sandboxed container plist, and Safari overwrites them if it stays running.

This lane:

- Adds a Settings gear: Light / Dark / System, hide or show any link, add a custom link.  Stored in `localStorage` on that device.
- Rewrites `install.sh` to write the container plist and quit Safari first.
- Hosts the same file at Personal-Site `site/public/start/index.html` → `https://jays.services/start/` for iPhone Add to Home Screen.
