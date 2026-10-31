# GitHub capability project

Owner request: broad GitHub operation support, without per-tool asks. This is not limited to the current workflow publication fix.

## Grounded baseline

- Official REST specification: 1,254 operations across 46 categories at audit time.
- Relay: 167 implemented workflow handlers, plus original PR/review/dispatch verbs.
- Live MCP catalog: 173 GitHub tools. Exposure is not proof of per-seat permission or successful execution.
- Complete REST endpoint inventory is in `inventory.json`; extracted relay route templates are in `relay-endpoints.json`. Exact semantic operation mapping is pending, not inferred from name similarity.
- GraphQL is a separate audit: finite supported queries and mutations must be documented rather than claiming every possible query shape is a tool.

## Work starting now

1. Workflow publication: file create/update, atomic multi-file, delete and rename support on non-default branches. Dynamic workflows:write only for workflow paths, same own-App identity and permission ceiling. Default branch, expected head/blob, secret scans and unknown-outcome protections remain. Reviewed policy and host rollout are separate from source merge.
2. Complete endpoint-to-handler mapping: label each endpoint supported, partial, missing, permission-blocked or outside an App's account type. Include request fields, pagination, output bounds, binary/download support and safe-error diagnostics. Never mark supported from an action name alone.
3. Close repository/PR/issue/git gaps, then Actions/release/deployment/environment gaps in reviewed batches. Existing issue/PR identity distinction and bot-only changes remain.
4. Organization/team/member/hooks, Projects, packages, search, security analysis/advisories, account/App/installation and user-specific operations. Record when GitHub requires OAuth/user token instead of an App. User-account effects and destructive actions keep their own authority checks.
5. Keep parity checks against the official REST spec and catalog; regression tests prevent exposed verbs without implementation/permission contracts. Publish staged receipts with source commit, review, CI, host rollout, live own-bot qualification.

## Current limits

The App registration advertises workflows:write, but installation acceptance and actual minted token permissions have not been exercised for workflow writes. Relay code currently rejects workflow paths; changing only the App permission is not the fix. No live policy, identity or host change is implied by this source proposal. Host/service owner custody must be accepted explicitly.

No arbitrary generic HTTP proxy, credential return, borrowed identity, or unrestricted default-branch write is introduced to imitate coverage. A tool can be available while a specific requested action still requires the user's authority.

## REST category coverage denominator

| Category | API operations | Mapping status |
|---|---:|---|
| actions | 200 | endpoint audit pending |
| activity | 34 | endpoint audit pending |
| agent-automations | 10 | endpoint audit pending |
| agent-merge | 3 | endpoint audit pending |
| agent-tasks | 5 | endpoint audit pending |
| agents | 30 | endpoint audit pending |
| apps | 37 | endpoint audit pending |
| billing | 13 | endpoint audit pending |
| campaigns | 5 | endpoint audit pending |
| checks | 12 | endpoint audit pending |
| classroom | 6 | endpoint audit pending |
| code-quality | 4 | endpoint audit pending |
| code-scanning | 25 | endpoint audit pending |
| code-security | 20 | endpoint audit pending |
| codes-of-conduct | 2 | endpoint audit pending |
| codespaces | 48 | endpoint audit pending |
| copilot | 31 | endpoint audit pending |
| copilot-spaces | 28 | endpoint audit pending |
| credentials | 1 | endpoint audit pending |
| dependabot | 25 | endpoint audit pending |
| dependency-graph | 5 | endpoint audit pending |
| emojis | 1 | endpoint audit pending |
| gists | 20 | endpoint audit pending |
| git | 13 | endpoint audit pending |
| gitignore | 2 | endpoint audit pending |
| hosted-compute | 6 | endpoint audit pending |
| interactions | 16 | endpoint audit pending |
| issues | 66 | endpoint audit pending |
| licenses | 3 | endpoint audit pending |
| markdown | 2 | endpoint audit pending |
| meta | 5 | endpoint audit pending |
| migrations | 22 | endpoint audit pending |
| oidc | 8 | endpoint audit pending |
| orgs | 116 | endpoint audit pending |
| packages | 27 | endpoint audit pending |
| private-registries | 6 | endpoint audit pending |
| projects | 26 | endpoint audit pending |
| pulls | 35 | endpoint audit pending |
| rate-limit | 1 | endpoint audit pending |
| reactions | 15 | endpoint audit pending |
| repos | 203 | endpoint audit pending |
| search | 7 | endpoint audit pending |
| secret-scanning | 17 | endpoint audit pending |
| security-advisories | 14 | endpoint audit pending |
| teams | 32 | endpoint audit pending |
| users | 47 | endpoint audit pending |

Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
