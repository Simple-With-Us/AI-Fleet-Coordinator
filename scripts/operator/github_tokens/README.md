# Repository-scoped GitHub App token routing prototype

Status: OFFLINE PROTOTYPE ONLY. Not installed, enabled, or connected to a live shim. No live GitHub token minted during tests. No installation IDs written to Infisical.

## Verified credential schema

Infisical Fleet-OPS project, resolved at runtime from `INFISICAL_FLEET_OPS_PROJECT_ID` (never commit the project UUID), environment `prod`, folder `/github-apps`.

On October 19, 2026 at about 7:38 PM America/Chicago, a values-hidden live metadata read returned 78 secrets: 39 complete `GH_APP_SWU_<SEAT>_ID` / `GH_APP_SWU_<SEAT>_PRIVATE_KEY` pairs. Operator was absent at that check. Example slug `swu-bf-builder` maps to `GH_APP_SWU_BF_BUILDER_ID` and `GH_APP_SWU_BF_BUILDER_PRIVATE_KEY`.

## Routing contract

1. Require an explicit allowed seat/App slug, owner/repository, and requested permissions. Do not infer the seat from the repository or fall back to a shared App.
2. Supply an Infisical-only credential loader returning that App's ID and PEM. This prototype deliberately leaves loader integration to a separate reviewed change. No retired global credential file or PAT fallback.
3. Sign a short JWT using that PEM, then confirm `/app` matches the requested App ID and slug.
4. Read `/repos/{owner}/{repo}/installation` using the JWT. Verify App ID, account login, non-suspended state and installation ID. This selects personal Fleet-OPS separately from Simple-With-Us organization work without storing installation IDs in Infisical.
5. Cache key includes App ID, installation ID, case-normalized owner/repository and exact requested permissions. Cache reuse still revalidates current installation first. Token cache is private (directory 0700, files 0600), atomic, and refreshed five minutes before UTC expiry.
6. Mint with `repositories: [repo]` and explicit permissions. Reject unexpected returned repository or permission scope. Never issue a token for all repositories by default.
7. Return token only to the in-process caller. Do not print it. No CLI that emits tokens, no automatic gh login, no fallback to Jay's stored login.

## Offline checks

Run `python3 -m unittest -v` in this folder. Twelve tests pass: name mapping, invalid target/slug/empty permissions, wrong App rejection, suspended installation rejection, wrong repository-scope rejection, org/personal split, cache reuse and private file mode, permission-specific cache separation, App-specific cache separation, expired-token refresh. Mock credentials and mock tokens only.

## Before live activation

- Review and wire an Infisical-only loader using the host's sanctioned secret-reading path.
- Establish the seat-to-App allowlist. Prevent untrusted repo content or shell input from switching to another seat's App.
- Bind actual gh command targets to the explicit routing target. Commands with multiple repositories need an explicit strategy; refuse ambiguous commands rather than silently use one installation.
- Decide caller-specific permissions. Broad App grants are an upper bound, not the token default.
- Review local cache sharing and concurrent refresh policy. Current prototype allows duplicate simultaneous mints, but atomic replacement prevents mixed cache contents.
- Test actual GitHub response shape and least-permission results against a read-only operation under separately approved live testing. Validate redirects, moved repos, lost access, key revocation and rate-limit handling.
- Review safe failure behavior for transport errors and cache cleanup/revocation. Never log HTTP response bodies that may contain credentials.
- Replace no live shim until an authorized deployment step and rollback plan are agreed.

## Sources

- https://docs.github.com/en/rest/apps/apps#get-a-repository-installation-for-the-authenticated-app
- https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
- https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
