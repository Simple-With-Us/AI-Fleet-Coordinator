# Additive issue and PR labels

Default-off own-App issues:write `wf_labels_add` adds1-20labels to an issue or PR
with GitHub's fixed POST endpoint. Unlike replace-all labels it does not remove
another lane's labels. No label creation, suggestions, archive, deletion or raw
metadata fields. ASCII names are bounded100characters, nonblank, unique ignoring
case and scanned. Responses above100labels or malformed IDs are refused.

Receipts confirm every requested label and return only that metadata subset:
ID/name/color. Unrelated label names/descriptions do not leave the relay.
Case-insensitive matches reflect GitHub label semantics. A receipt is not a lock;
other actors can change labels immediately afterward. No overall-label-set or
concurrent-preservation proof is implied beyond using additive POST semantics.

Existing outcome keys normalize order/case, so reordered requests cannot replay
an UNKNOWN write, including after persisted restart. Only verified successful
setters can reapply, useful when a label was later removed. No forced UNKNOWN
replay or second ledger. Offline real-relay regressions cover both paths.
No label applied live, permission widened, deployment or review triggered.

Fleet anchor: scripts/hold-guard/guard_run.sh uses additive POST labels.
Official additive-vs-replacement schema verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
