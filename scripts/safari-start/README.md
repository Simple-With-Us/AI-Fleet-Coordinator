# Operator start alias

`https://start.jays.services/` redirects temporarily to the existing protected `https://home.jays.services/` operator page.  The Worker handles page aliases before static assets, so `/index.html` cannot bypass the redirect.  Unknown paths return 404.  Only the icon and robots file remain directly served.

`GET /suggest?q=…` and its CORS behavior remain available for previously installed local start pages.  It sends the supplied search text to Google Suggest and returns a bounded suggestion list.  No operator inventory or private-repository links are returned by that endpoint.

This change does not edit local Safari preferences, quit Safari or replace an installed `~/Sites/safari-start/` copy.  The on-demand `install.sh` helper now installs the redirect page if explicitly run; it still changes Safari preferences, so run it only when that local change is intended.

Validate with `node --test scripts/safari-start/worker.test.mjs`.  Deploy from this directory using the existing `wrangler deploy` workflow after the PR is merged and checked.  Verify the page aliases redirect to home, home requires authentication, icon/robots remain reachable, `/suggest?q=` returns an empty list, and an unknown path returns 404.  Reverting this commit restores the prior hosted implementation; it does not change the home application's authentication configuration.
