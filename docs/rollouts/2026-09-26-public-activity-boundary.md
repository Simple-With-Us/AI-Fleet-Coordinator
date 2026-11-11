# Public activity publication boundary

The public digest previously read internal effort-board rows and used a repository list that included private fleet inventory.  That put internal metadata and links into the checked-in Pages HTML, Markdown, and calendar feeds.

The digest and per-commit calendar now share an explicit public repository allowlist.  At build time, each requested repository must also be verified as public with the GitHub API; an unlisted, private, or unverified repository is skipped.  If none can be verified, the build fails without replacing published files.  Public PR and issue links are constructed for the selected repositories.  Internal effort boards remain in their existing private workflow and are no longer fetched by the public generator.

The checked-in `site/index.html`, `site/digest.md`, both root `calendar/*.ics` feeds, and their `site/calendar/` copies were regenerated.  An offline scan found zero links to the excluded repositories and zero rendered effort-board sections in those six files.  The HTML and Markdown each retained 1,067 public PR links.  `site/ios-versions.json` was untouched.  Regression checks cover private, unlisted, and unavailable visibility responses plus preserved public PR rendering and the committed artifact link boundary.

PR #293 merged as `d8ecc98`; Pages workflow run `36283078232` succeeded on 2026-09-26.  Live HTML, Markdown, both ICS feeds, iOS JSON, and the logo README returned 200.  The HTML and Markdown each had 1,070 public PR links, and every GitHub repository link in the live HTML, Markdown, and per-commit ICS belonged to the allowlist.  No rendered effort-board heading or list remained.  The iOS JSON SHA-256 matched the unchanged checked-in file.  Historical `site/` paths were enumerated from Git history: no separate history or archive artifact filenames exist, and `/history/` returned 404.

The publication replaces current Pages artifacts.  It does not rewrite Git history or change private board source files.
