# Own installation metadata reader

`wf_installation_metadata` / `gr_wf_installation_metadata` accepts only a canonical, policy-allowlisted `{repo: "owner/name"}`. Existing authenticated caller identity selects its own App through live policy. There is no seat, App slug, installation ID, credential, URL or arbitrary API selector.

The existing credential custodian signs an internal App JWT and sends bounded GET `/app`, then GET `/repos/{owner}/{name}/installation`. App ID/slug, installation app ID/slug, target owner account, safe integer IDs, permission names/levels and suspension shape are validated. Only this safe projection returns:

```
{installation: {app_slug, installation_id, account: {id, login},
 accepted_permissions: {permission_name: "read" | "write"}, suspended: boolean}}
```

No token POST, key/JWT/token return, raw headers/body/errors, token-cache change, alternate identity fallback, account/policy update or new credential is included. An installation that is suspended is reported as suspended; this is not deployment-ready. Registration permissions differ from accepted installation permissions, and even accepted metadata does not prove every later operation will succeed.

The read remains behind existing workflow enablement, authenticated per-seat action/repository allowlists and audit. Activation needs only this read action added for the approved caller to current policy, not workflow-write grant or whole checked-in policy replacement. The separate workflow-write change can therefore be qualified without circular token-mint proof.

Source additions:
- `git-relay/git_relay/app_tokens.py`: read-only metadata method using existing custodian transport.
- `git-relay/git_relay/workflow_verbs.py`: closed shape/read classification.
- `git-relay/git_relay/policy.py`: recognized action, no live policy changes.
- `git-relay/git_relay/server.py`: authenticated dispatcher special case, no `token_for`.
- `scripts/agent-sync-mcp/src/gr-workflow-v1.js`: closed input, safe strict output, read-tool classification and generated hosted catalog.

Runtime writer must adapt the reviewed delta to the current held live baseline, preserving unrelated actions, ledger/grants, credentials, audit and unknown-outcome handling. Do not replace entire runtime files or seat policy. No host or Worker deployment is performed by this source PR. Qualification receipts distinguish merged source, relay activation, MCP catalog activation, actual own-call metadata and subsequent workflow-write execution.
