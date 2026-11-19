# AFC-798 RAG Bootstrap Source

- Canonical direct-reader dependencies must be on main and the final source must pass CI and review before installation.
- Verify this recall consumer's existing authorized bootstrap binding, registry metadata, exact configured-project prod `/` read-only grant and finite expiry before activation.
- Verify the installed primary reader, setup doctor, nightly and collection-health paths using that own binding; this source unit supplies no store or grant and performs no installation.

Refs [AFC-798](https://linear.app/simple-with-us/issue/AFC-798), under [AFC-785](https://linear.app/simple-with-us/issue/AFC-785).

## Source Contract

The primary recall reader accepts only its explicit `FLEET_RECALL_INFISICAL_TOKEN` process binding or `FLEET_RECALL_INFISICAL_TOKEN_FILE` selector for an already authorized protected host-bootstrap store.  An explicitly empty process binding refuses without file fallback.  No current non-retired live recall store was qualified in this source task.

Core, setup doctor and nightly share the selector and custody check.  No default file is discovered.  Explicit retired `.secrets` selectors, symlink aliases and hard-linked files refuse before credential content reads.  A selected file must be an owned regular 0400 or 0600 file, with one link, in a protected directory; metadata checks use descriptors and nonblocking opens.  Every opened directory, including the root anchor, must belong to root or the caller and reject group/other write access before the next child is opened.  Only root-owned sticky `/tmp` and `/private/tmp` temporary anchors may be shared; arbitrary shared or nonsticky ancestors refuse.  The recall-only decoder accepts a single raw service token or the existing recall assignment with optional `export` and matching quotes.  It does not evaluate a shell or load unrelated environment assignments.  Doctor's presence row never reads the file contents and does not claim a verified grant.

`core.infisical_fetch` requires the own recall binding before invoking the canonical bounded named reader.  It retains the configured recall project, prod and root folder, a 30-second operation bound and the approved folder-record contract.  Ambient generic, AFC, write and knobs tokens cannot replace an absent recall binding.  The independent optional Fleet-OPS reader and the nightly's separate Linear and Zulip routes retain their existing contracts.

The two job examples leave the recall file selector commented and the jobs disabled.  No retired file is opened, copied, renamed or migrated by this change.  No new credential purpose, token, default path or grant is introduced.

## Verification

The focused tests exercise protected temporary stores and a real canonical worker against loopback-only metadata and named-record responses.  Cases cover explicit process/file precedence, explicit-empty refusal, retired defaults and aliases before content reads, private-file custody, FIFO and size refusal, supplied environment maps, exact project/prod/root/names, read-only finite-expiry grants, malformed or unresolved values, and absent primary binding despite other-purpose tokens.  The existing installer fixture copies the actual package and canonical reader into a temporary home, then proves process and file reads and missing-binding refusal through an isolated Python child without a secrets app.

After the iterator and ancestor-custody review fixes, from `scripts`, `python3 -m unittest fleet_rag.tests.test_core_config fleet_rag.tests.test_doctor fleet_rag.tests.test_nightly fleet_rag.tests.test_collection_health` passes 139 tests.  The initial 207-test combined run also exercised `fleet_rag.tests.test_secret_guard`.  The unchanged shared reader's `python3 -m unittest fleet_secrets.tests.test_bounded_reads` run passes 20 tests.  Compilation and diff checks pass; the unchanged source scanner accepts all 13 changed files.  Local Ruff and Black are unavailable, so hosted formatting remains a CI gate.

These are synthetic source and copied-payload checks; they are not live binding, provider, job or installed acceptance receipts.  Remaining child/runtime work is not completed here, and this unit does not establish global app retirement.
