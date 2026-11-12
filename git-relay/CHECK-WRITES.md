# Check-run write GitHub commands

wf_check_create/update are default-off own-App checks:write commands. Existing
seat ceiling/repo/action allowlists apply; no configured permission grant changes.
Create binds an exact commit SHA; update pre-reads check ID and expected SHA, which
is not an atomic fence. GitHub enforces the check-owning App on updates. No caller
App selector, annotations, actions, URLs or arbitrary output object.

Status/conclusion combinations are validated before quota/upstream work. Name100,
title255, summary32768 UTF-8 bytes, nonblank and secret-scanned. Projected receipts
verify ID, head SHA, name, status and conclusion; uncertain writes stay UNKNOWN.
Capability is never approval to fabricate CI results or bypass a gate. No live check
run was created or changed to test this source.

Check names are fixed to the authenticated App namespace `<seat>/report`, not arbitrary
CI contexts or another App. This namespace must not be configured as independent CI
evidence. The command cannot prove caller report truth; execution still requires owner
authority and actual result evidence.
