# Bounded label metadata update

Default-off `wf_label_update` uses own-App issues:write to change an exact label's
name, six-hex-digit color and description. All three desired fields are explicit.
Text is bounded to100UTF-8bytes, control-free and scanned for known credential
shapes. No archive, deletion, issue-label replacement or arbitrary fields.

An exact current name/positive label ID pre-read precedes a fixed encoded-label
PATCH. It is not an atomic conditional update: concurrent label replacement or
renaming may cause failure or a changed target between the read and write.
Callers must not mistake the ID pre-read for a provider CAS guarantee. A success
requires the same ID and exact requested metadata; only that projection returns.

Transient pre-reads are definitely unsent. Uncertain writes or malformed success
receipts stay UNKNOWN in the existing file-backed outcome store. Changing only
the lookup name after a possible rename cannot re-arm the same desired update;
restart tests prove no second dispatch. Only a verified successful desired-state update can be applied again, so A-to-B-to-A works under the existing state-setter rules. Pending and UNKNOWN writes remain held. No forced replay or new credentials.
Offline synthetic tests only. No live label was changed, and implementing this
command grants no authority to change labels or deploy it.

Official update-label schema:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
