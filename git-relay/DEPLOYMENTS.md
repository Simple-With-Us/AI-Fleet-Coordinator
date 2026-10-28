# Deployment GitHub commands

Six default-off commands list/get/create/delete deployments and list/create statuses.
Explicit deployments:read/write is recognized by the minter; no configured seat/App
permissions change. Existing own App/repo/action allowlists and ceilings remain.
Deployment create may trigger automation and requires separate owner execution approval.
No deployments were created, deleted or marked successful to test this source.

Create takes an exact commit SHA, never auto-merges a branch, and never overrides
required status contexts. Status creation sets auto_inactive false so it cannot change
other deployments. No caller target URLs or arbitrary payload. Text is secret-scanned,
nonblank and bounded (environment100/description140 UTF-8 bytes). Malformed receipts
hold UNKNOWN; no replay. First-page lists only, at most50 entries. No environment
protection approvals, custom tasks/payload, or arbitrary deployment URL support here.
