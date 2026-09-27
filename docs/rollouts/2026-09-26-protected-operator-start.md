# Protected operator start alias

The hosted start page previously repeated operator destinations in a public HTML file.  Page aliases now return a temporary redirect to the existing authenticated home.  Unknown paths return 404 and only the icon and robots files remain directly served.  The asset router runs after the Worker, including for `/index.html`.

Older installed local start pages retain their search suggestion endpoint and CORS behavior.  Their local files and Safari preferences have not been changed.  `install.sh` is still an explicit on-demand action; running it now copies the small redirect page.

Validation: `node --test scripts/safari-start/worker.test.mjs` passes three tests covering aliases, asset boundaries, empty suggestions and preflight.  `wrangler deploy --dry-run` with Wrangler 4.125.0 packaged the Worker and three static assets, with no deploy.  CI also runs the Node check.  Production verification is pending merge and deployment; a protected-home redirect alone does not prove an authenticated operator workflow.

Rollback: revert the Worker and static-page commit, then redeploy using the documented Worker workflow.  No Access policy or DNS record is modified by this unit.  Coordinate with the Personal-Site `/start/` redirect if rolling back the shared entry path.
