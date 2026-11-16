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
import { normalizeHeader } from "./contract.js";

export const SENTENCE_GAP_ARG = "sentence_gap";

/** Only explicit PR routes supply PR context; ordinary issue routes keep their repository headers. */
export const GITHUB_PR_HEADER_FIELDS = Object.freeze({
  open_pr: ["title", "body"],
  edit_pr: ["title", "body"],
  comment_pr: ["body"],
  reply_thread: ["body"],
  wf_pr_create: ["title", "body"],
  wf_pr_edit: ["title", "body"],
  wf_review_submit: ["body"],
  wf_review_inline: ["body"],
  wf_review_update: ["body"],
  wf_review_dismiss: ["message"],
  wf_review_comment_reply: ["body"],
  wf_review_comment_update: ["body"],
});

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
  // Commit messages, check-run summaries and milestone descriptions are prose too (AFC-722 review).  Label,
  // deployment and commit-status descriptions are not listed:  they are short machine-facing metadata under a
  // 100- or 140-byte cap.  Title prose, names, branches and file content are not gapped;
  // explicit PR title headers still use the separate compact-context map above.
  wf_git_commit_create: ["message"],
  wf_file_put: ["message"],
  wf_files_put: ["message"],
  wf_file_patch: ["message"],
  wf_file_rename: ["message"],
  wf_file_delete: ["message"],
  wf_check_create: ["summary"],
  wf_check_update: ["summary"],
  wf_milestone_create: ["description"],
  wf_milestone_update: ["description"],
});

// The non-workflow relay verbs (gr_open_pr, gr_edit_pr, gr_comment_pr, gr_reply_thread) cap a body at 32 KiB.  Each
// gap the server writes adds a character and two UTF-8 bytes, so a body that fit as typed and no longer fits once
// gapped is sent as typed, and the result says so.
export const GITHUB_GAP_BYTE_CAP = 32768;
const utf8Bytes = (text) => new TextEncoder().encode(text).length;

const OPTION_SCHEMA = Object.freeze({
  type: "boolean",
  default: true,
  description:
    "The server writes each sentence gap as U+00A0 plus a space (the house rule), since a typed U+00A0 arrives as a plain space.  Fenced, indented and inline code, HTML <pre>/<code>/<kbd>/<samp> and comments are left alone;  fence pasted data (JSON, diffs, commands) or pass false to send the text byte for byte.",
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

/** Run `fn` with the call's gap choice in scope;  `notices` collects what the caller must be told. */
export function runWithGap(enabled, fn, notices = []) {
  return storage.run({ enabled: enabled !== false, notices }, fn);
}

/** Tell the caller (through the tool result) that the gap was not written. */
export function noteGapSkipped(text) {
  storage.getStore()?.notices.push(text);
}

/** The notice for a field sent as typed because the gapped text no longer fit. */
export const capNotice = (tool, field) =>
  `sentence_gap: ${tool} ${field} was sent as typed, without the U+00A0 gap, because the gapped text no longer fit the relay's size cap.`;

/** Whether the current call wants the gap written (true outside any call, as before). */
export function gapEnabled() {
  return storage.getStore()?.enabled !== false;
}

/** `text` with the house gap, unless the current call opted out. */
export function outboundGap(text) {
  return gapEnabled() ? houseGap(text) : text;
}

/**
 * Compact only explicit PR headers; the byte-for-byte opt-out keeps supplied text unchanged.
 */
export function compactGitHubArgs(action, args, { seat = null } = {}) {
  const fields = GITHUB_PR_HEADER_FIELDS[action];
  if (!fields || !args || typeof args !== "object" || !gapEnabled()) return args;
  let out = args;
  for (const field of fields) {
    if (typeof args[field] !== "string") continue;
    const description = field === "body" && ["open_pr", "edit_pr", "wf_pr_create", "wf_pr_edit"].includes(action);
    const context = field === "title" ? "github-pr-title" : description ? "github-pr-body" : "github-pr";
    const header = normalizeHeader(args[field], { seat, context });
    if (header !== args[field]) {
      if (out === args) out = { ...args };
      out[field] = header;
    }
  }
  return out;
}

/** Add prose gaps after PR context, preserving compact headers when only the added gaps exceed a cap. */
export function gapGitHubArgs(action, args, { byteCap = null, seat = null } = {}) {
  args = compactGitHubArgs(action, args, { seat });
  const fields = GITHUB_GAP_FIELDS[action];
  if (!fields || !args || typeof args !== "object" || !gapEnabled()) return args;
  let out = args;
  for (const field of fields) {
    if (typeof args[field] !== "string") continue;
    const gapped = houseGap(args[field]);
    if (byteCap !== null && gapped !== args[field] && utf8Bytes(gapped) > byteCap && utf8Bytes(args[field]) <= byteCap) {
      noteGapSkipped(capNotice(`gr_${action}`, field));
      continue;
    }
    if (gapped !== args[field]) {
      if (out === args) out = { ...args };
      out[field] = gapped;
    }
  }
  return out;
}
