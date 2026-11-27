# Worker deployment service credentials

No seat bearer is stored in deployment CI.

| Consumer | Actions secret | Source locator | Scope | Expiry UTC |
| --- | --- | --- | --- | --- |
| AFC publisher | AFC_WORKER_DEPLOY_INFISICAL_TOKEN | Fleet-OPS service token afc-worker-deploy-ci-read-20261127, id9d8c5c41-06de-4314-9d75-1367834929a3 | prod:/cloudflare read only | 2027-05-26T14:27:36Z |
| UM publisher | INFISICAL_UM_WORKER_DEPLOY_READ_TOKEN | Fleet-OPS service token um-worker-deploy-ci-read-20261127, id68b72f6c-8bb0-4379-9088-92245f4e2131 | prod:/um-worker-deploy read only | 2027-05-26T14:37:52Z |

Renew before expiry. The credential maintenance follow-up is due May12. Create a replacement scoped read-only finite service token with the same project and exact folder, verify metadata and intended record access, seal it to the repository Actions public key and update only the named secret through Echo's relay identity. Run deployment qualification and verify the workflow can read it before retiring the old reader. Never replace it with an agent token or broaden its scope. Keep the new expiry and maintenance follow-up current.

The canary bearer belongs only to the service. Its digest is in the Worker config; CI holds the bearer in MCP_CANARY_BEARER. It permits only a fixed read-only MCP tool and grants no seat, upstream, storage or delegation capability. Keep this distinct from production seat credential-resolution proof. Access health credentials use the existing AGENTS_JAYS_SERVICES Access service token, verified against the exact health URL before staging.

## Cutover evidence still required

Credential staging is not activation or deployment. Current-head checks and review, exact uploaded version promotion, live service canary and functional Worker receipts are required. Worker version previews remain disabled. External deploy writers are not excluded atomically by Cloudflare; the publisher detects interleaving through deployment history and does not roll back blindly.
