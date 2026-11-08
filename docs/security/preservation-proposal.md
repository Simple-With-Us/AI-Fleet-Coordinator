# AFC1258 preservation proposal

Status: inactive source proposal. Owner approved the bounded unchanged-test-fixture policy on November7,2026; actual source qualification and normal gates remain required. Both release registries are empty. No runtime or credential change is authorized by that decision.

## Two separate cases

1. Exact existing objects. An optional `source_commit` in a tree entry identifies a release-reviewed source record. The relay proves the exact same path, blob SHA, regular-file mode and size from immutable commit/tree metadata in the same repository. The workflow additionally fetches exact blob bytes, verifies cryptographic identity and declared size, runs the full scanner, and refuses loaded values and provider-guard hits. No caller bytes are accepted for this case. It cannot relocate content, synthesize a new object, alter mode, change a ref, write a workflow, accept caller approval or exceed the existing raw-reference budget. The source-record registry is empty until the specific objects are reviewed and authorized. Ordinary SHA entries still fetch and scan their content.
2. Four synthesized conflict results. Only a release-reviewed tuple of repository, path, complete source SHA256 and complete result SHA256 can enter the preservation validator. The server still fetches the pinned same-path source and reconstructs the result from clean, bounded edits. Every credential-shaped match must be on an entire, contiguous, unchanged original source line. Copied/replaced bytes, reordering, boundary assembly, changed context, new matches and digest drift fail closed. Loaded values and the existing provider guard remain absolute. The preservation registry is empty pending owner policy approval and source review. Raw uploads remain refused even for an approved preservation result.

The proposed rule is a deliberate change from the present dedicated-baseline refusal. Source review is not the owner's approval of that policy change. Installation and use remain held until both requirements are met.

The accompanying pending manifest records exact inputs/results for review. It is data, not an activation mechanism. No caller can supply or activate an approval record. A new source or output digest needs a new reviewed record.

## Evidence

- Existing dedicated-baseline and raw-upload refusals are retained by default.
- Tests cover source identity/path/mode/type/size substitution, symlinks, workflow paths, unknown fields, source record absence, ambiguous tree entries, truncated trees, immutable commit checks, pre-read failures, reference budgets, caching and fixed tree payloads.
- Preservation tests cover wrong repository/path/source/output, new/copied/replaced/reordered matches, edited containing lines, byte-boundary assembly, Unicode boundaries, exact loaded values including short or removed values, match budgets, provider guard and expected-head behavior.
- No new review is requested for AFC1382. This proposal is a different source change and needs its own first review.
