# Exact same-repository issue dependencies

Two default-off own-App issues:write commands add/remove a blocked-by relation.
Caller supplies source and blocker issue numbers in one authorized repository.
Both prereads bind repository, positive database IDs and issue numbers, refuse PRs
and self-dependency. No raw global ID, foreign repo, parent replacement or transfer.
Fixed POST issue_id and DELETE derived ID endpoints only. Receipts must identify
one of those two bound participants; malformed/mismatched response is502UNKNOWN.
No title or body returned by these writes. Existing response secret scan applies.
Identity prereads are non-atomic, NOT a state/version fence. Server enforces cycles,
permissions and concurrent relation semantics; no graph-completeness claim.
Known-success desired-state operations reapply for A-B-A; pending/UNKNOWN held
exactly as before, never replayed or cleared. No configured permissions, App grants,
connections, actual relation mutations or deployment. Subissues/transfer still absent.

Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
