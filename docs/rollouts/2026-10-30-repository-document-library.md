# Repository Document Library

The owner requested documents grouped by their exact repository names in a central Docs folder beside the Code mirrors, with matching report entry points in the repositories.  Existing audits, reviews, research and chronological records are currently spread across several directories.

`scripts/collect-repo-documents.py` collects tracked human documents and eligible linked assets from published Git revisions.  It records immutable source commits and content hashes, preserves existing output, rejects unsafe paths and edited generated files, and stages in-repo report-index proposals without editing source checkouts.  The shared convention is documented in `docs/protocols/report-storage.md`.

The collection covers the 16 primary GitHub repository names and excludes duplicate or scratch checkouts.  Existing historical paths remain intact.  New public-safe reports belong in `docs/reports/`; private operational reports and evidence belong in private Fleet-OPS.  The previously untracked Backblaze audit is being preserved in that private repository.

Focused fixture tests cover repository selection, published-revision consistency, link handling, output ownership, path containment, preservation of private files and idempotent collection.  Recovery tests cover interrupted multi-repository updates, edited output, a second live writer and read-only check mode.  Additional regressions cover relative output paths, Git timeouts, batched index reads and malformed preview metadata.  All 27 tests pass; exact source revisions are recorded in the PR and collection manifests.

The installed server lane guard rejected creation of `Code/Docs`.  Previews remain outside the protected integration tree until a narrow placement exception is approved.  No guard, background job, runtime, credential or backup schedule has been changed.
