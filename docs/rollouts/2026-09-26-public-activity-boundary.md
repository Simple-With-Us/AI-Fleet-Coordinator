# Public activity publication boundary

The public digest previously read internal effort-board rows and used a repository list that included private fleet inventory.  That put internal metadata and links into the checked-in Pages HTML, Markdown, and calendar feeds.

The digest and per-commit calendar now share an explicit public repository allowlist.  At build time, each requested repository must also be verified as public with the GitHub API; an unlisted, private, or unverified repository is skipped.  If none can be verified, the build fails without replacing published files.  Public PR and issue links are constructed for the selected repositories.  Internal effort boards remain in their existing private workflow and are no longer fetched by the public generator.

The checked-in `site/index.html`, `site/digest.md`, both root `calendar/*.ics` feeds, and their `site/calendar/` copies were regenerated.  An offline scan found zero links to the excluded repositories and zero rendered effort-board sections in those six files.  The HTML and Markdown each retained 1,067 public PR links.  `site/ios-versions.json` was untouched.  Regression checks cover private, unlisted, and unavailable visibility responses plus preserved public PR rendering and the committed artifact link boundary.

This update replaces current published artifacts when the Pages workflow runs.  It does not rewrite Git history or change private board source files.
