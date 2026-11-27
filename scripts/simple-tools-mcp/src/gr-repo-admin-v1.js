import {z} from 'zod';
// Contract for the relay repository settings verbs (git-relay/git_relay/repo_admin_verbs.py).
// Only named fields are sent. Answers are the repository settings as GitHub reports them after the call.
const REPO = { type: 'string', pattern: '^(?!.*\\.\\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$', description: 'owner/name' };
const obj = (required, properties) => ({ type: 'object', additionalProperties: false, required, properties });
const BOOL = { type: 'boolean' };
const SEC = { type: 'string', enum: ['enabled', 'disabled'] };
export const REPO_ADMIN_SPECS = {
  wf_repo_settings: ['Read one repository settings view: description, homepage, topics, features, merge options, security_and_analysis. Administration read.', obj(['repo'], { repo: REPO })],
  wf_repo_update: ['Update named repository settings only: description, homepage (http(s) URL or empty), feature toggles, merge-method toggles, delete_branch_on_merge, allow_auto_merge, allow_update_branch, merge commit title/message formats, and security_and_analysis (secret_scanning, secret_scanning_push_protection, dependabot_security_updates). Every sent field is checked in the answer. No rename, privacy, archive or delete. Administration write.', obj(['repo'], {
    repo: REPO,
    description: { type: 'string', maxLength: 1024 },
    homepage: { type: 'string', maxLength: 1024 },
    has_issues: BOOL, has_wiki: BOOL, has_projects: BOOL, has_discussions: BOOL,
    allow_squash_merge: BOOL, allow_merge_commit: BOOL, allow_rebase_merge: BOOL, allow_auto_merge: BOOL,
    delete_branch_on_merge: BOOL, allow_update_branch: BOOL, web_commit_signoff_required: BOOL,
    squash_merge_commit_title: { type: 'string', enum: ['PR_TITLE', 'COMMIT_OR_PR_TITLE'] },
    squash_merge_commit_message: { type: 'string', enum: ['PR_BODY', 'COMMIT_MESSAGES', 'BLANK'] },
    merge_commit_title: { type: 'string', enum: ['PR_TITLE', 'MERGE_MESSAGE'] },
    merge_commit_message: { type: 'string', enum: ['PR_BODY', 'PR_TITLE', 'BLANK'] },
    security_and_analysis: obj([], { secret_scanning: SEC, secret_scanning_push_protection: SEC, dependabot_security_updates: SEC }),
  })],
  wf_repo_topics_set: ['Replace the repository topics (up to 20 unique lowercase names). Administration write.', obj(['repo', 'topics'], { repo: REPO, topics: { type: 'array', minItems: 0, maxItems: 20, items: { type: 'string', minLength: 1, maxLength: 50 } } })],
};
export const REPO_ADMIN_READ_ACTIONS = ['wf_repo_settings'];
export const isRepoAdminAction = (action) => Object.hasOwn(REPO_ADMIN_SPECS, action);

const s = (n) => z.string().max(n);
const ns = (n) => z.string().max(n).nullable();
const nb = z.boolean().nullable();
const settings = z.object({
  full_name: ns(200), description: ns(2000), homepage: ns(2000), default_branch: ns(255), visibility: ns(50),
  archived: nb, private: nb, has_issues: nb, has_wiki: nb, has_projects: nb, has_discussions: nb,
  allow_squash_merge: nb, allow_merge_commit: nb, allow_rebase_merge: nb, allow_auto_merge: nb,
  delete_branch_on_merge: nb, allow_update_branch: nb, web_commit_signoff_required: nb,
  squash_merge_commit_title: ns(50), squash_merge_commit_message: ns(50), merge_commit_title: ns(50), merge_commit_message: ns(50),
  topics: z.array(s(50)).max(20).nullable(),
  security_and_analysis: z.record(s(50), s(50)).nullable(),
}).strict();
const SCHEMAS = {
  wf_repo_settings: z.object({ settings }).strict(),
  wf_repo_update: z.object({ settings }).strict(),
  wf_repo_topics_set: z.object({ topics: z.array(s(50)).max(20) }).strict(),
};
export function validRepoAdminResponse(action, body, args) {
  const schema = SCHEMAS[action];
  if (!schema || !schema.safeParse(body).success) return false;
  if (action === 'wf_repo_topics_set' && args && Array.isArray(args.topics)) {
    return [...body.topics].sort().join(',') === [...args.topics].sort().join(',');
  }
  return true;
}
