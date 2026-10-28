# Commit status GitHub commands

wf_commit_statuses reads bounded status rows; wf_commit_status_create creates a status
on an exact commit SHA with state/context/description. No caller URL input. These are
not check-run commands. They require explicit statuses:read or statuses:write in the
seat ceiling. The minter recognizes the statuses permission name, but no configured
seat or App is given it by this source change; contents permission does not imply
statuses permission.

Default off, per-seat repo/action allowlists, own App and secret scanning remain.
Adding code grants no authority to manufacture CI results or bypass review/merge gates.
No live status was created. Description 140 UTF-8 bytes, context 255 UTF-8 bytes,
nonblank. Status writes use the existing durable UNKNOWN/no-replay ledger. Target URLs,
combined-status API and status history pagination beyond the bounded first page are
not implemented here.

Status-create context is fixed to the authenticated App namespace `<seat>/report`.
It cannot write `test`, `gitleaks`, another App namespace or arbitrary gate contexts.
This report namespace must not be configured as independent CI evidence. No source
capability can prove the truth of a caller-supplied report; owner authorization and
actual result evidence still apply to every execution.
