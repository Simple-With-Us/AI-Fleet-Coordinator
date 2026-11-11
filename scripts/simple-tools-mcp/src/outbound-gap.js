// AFC-722:  the one place the hosted GitHub and Linear text verbs get the house sentence gap.
//
// A model cannot put U+00A0 into a tool argument:  the client receives a plain space, so a PR body, a review
// reply or a Linear comment written through these tools reached GitHub and Linear with no gap at all, while
// the same text sent from a shell file kept it.  The server therefore writes the gap itself (textfmt.js
// houseGap) on every human-readable text field these verbs persist, unless the call passes
// `sentence_gap: false` (for text that must land byte for byte, such as a quoted log).
//
// The flag rides per call in AsyncLocalStorage:  callTool strips `sentence_gap` from the arguments before any
// handler sees them, so nothing forwards it to git-relay or Linear, and the Linear tag line (linearBody) reads
// the same flag however deep the call that reaches it.  Zulip has its own outbound rule (fixOutbound) and is
// not touched here.

import { AsyncLocalStorage } from "node:async_hooks";
import { houseGap } from "./textfmt.js";

export const SENTENCE_GAP_ARG = "sentence_gap";

/** The text fields each GitHub verb persists, keyed by the relay action name. */
export const GITHUB_GAP_FIELDS = Object.freeze({
  open_pr: ["body"],
  edit_pr: ["body"],
  comment_pr: ["body"],
  reply_thread: ["body"],
  wf_pr_create: ["body"],
  wf_pr_edit: ["body"],
  wf_issue_create: ["body"],
  wf_issue_update: ["body"],
  wf_issue_comment: ["body"],
  wf_issue_comment_update: ["body"],
  wf_review_submit: ["body"],
  wf_review_inline: ["body"],
  wf_review_update: ["body"],
  wf_review_dismiss: ["message"],
  wf_review_comment_reply: ["body"],
  wf_review_comment_update: ["body"],
  wf_commit_comment_create: ["body"],
  wf_commit_comment_update: ["body"],
  wf_release_create: ["body"],
  wf_release_update: ["body"],
  wf_merge_guarded: ["commit_message"],
});

const OPTION_SCHEMA = Object.freeze({
  type: "boolean",
  default: true,
  description:
    "The server writes each sentence gap as U+00A0 plus a space (the house rule), since a typed U+00A0 arrives as a plain space.  Pass false to send the text byte for byte.",
});

/** The public tool name for a GitHub action. */
export const githubToolName = (action) => `gr_${action}`;

/** `tool` with the `sentence_gap` option added to its input schema. */
export function withGapOption(tool) {
  const schema = tool.inputSchema;
  if (!schema || typeof schema !== "object" || !schema.properties) return tool;
  return { ...tool, inputSchema: { ...schema, properties: { ...schema.properties, [SENTENCE_GAP_ARG]: OPTION_SCHEMA } } };
}

const storage = new AsyncLocalStorage();

/** Run `fn` with the call's gap choice in scope. */
export function runWithGap(enabled, fn) {
  return storage.run({ enabled: enabled !== false }, fn);
}

/** Whether the current call wants the gap written (true outside any call, as before). */
export function gapEnabled() {
  return storage.getStore()?.enabled !== false;
}

/** `text` with the house gap, unless the current call opted out. */
export function outboundGap(text) {
  return gapEnabled() ? houseGap(text) : text;
}

/** A copy of `args` with the house gap written into `action`'s text fields (unless the call opted out). */
export function gapGitHubArgs(action, args) {
  const fields = GITHUB_GAP_FIELDS[action];
  if (!fields || !args || typeof args !== "object" || !gapEnabled()) return args;
  let out = args;
  for (const field of fields) {
    if (typeof args[field] !== "string") continue;
    const gapped = houseGap(args[field]);
    if (gapped !== args[field]) {
      if (out === args) out = { ...args };
      out[field] = gapped;
    }
  }
  return out;
}
