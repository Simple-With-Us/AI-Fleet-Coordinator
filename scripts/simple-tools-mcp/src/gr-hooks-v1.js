import {z} from 'zod';
// Contract for the relay hook, deploy key and environment verbs (git-relay/git_relay/hook_verbs.py).
// A webhook secret is accepted on write and never returned.  Answers are small shape-checked receipts.
const REPO = { type: 'string', pattern: '^(?!.*\\.\\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$', description: 'owner/name; org-level verbs use its owner as the organization' };
const obj = (required, properties) => ({ type: 'object', additionalProperties: false, required, properties });
// No new regex patterns: simple-git compiles only its reviewed pattern set.  The relay enforces exact shapes.
const ENV = { type: 'string', minLength: 1, maxLength: 100, pattern: '^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,99}$' };
const VNAME = { type: 'string', pattern: '^(?!GITHUB_)[A-Z_][A-Z0-9_]{0,99}$' };
const PER_PAGE = { type: 'integer', minimum: 1, maximum: 50 };
const PAGE = { type: 'integer', minimum: 1, maximum: 1000 };
const ID = { type: 'integer', minimum: 1, maximum: 9007199254740991 };
const HOOK_CONFIG = {
  url: { type: 'string', minLength: 8, maxLength: 2000, description: 'http(s) URL without credentials; put any shared secret in the secret field' },
  content_type: { type: 'string', enum: ['json', 'form'] },
  insecure_ssl: { type: 'string', enum: ['0', '1'] },
  secret: { type: 'string', minLength: 1, maxLength: 256, description: 'webhook signing secret; sent to GitHub, never returned' },
  events: { type: 'array', minItems: 1, maxItems: 50, items: { type: 'string', minLength: 1, maxLength: 60 } },
  active: { type: 'boolean' },
};
const HOOK_ID = { repo: REPO, id: ID };

export const HOOK_SPECS = {
  wf_hook_get: ['Read one repository webhook (receipt only; secret never returned). Repository Webhooks read.', obj(['repo', 'id'], HOOK_ID)],
  wf_hook_create: ['Create a repository webhook. The url must not carry credentials. Repository Webhooks write.', obj(['repo', 'url'], { repo: REPO, ...HOOK_CONFIG })],
  wf_hook_update: ['Update a repository webhook. Repository Webhooks write.', obj(['repo', 'id'], { ...HOOK_ID, ...HOOK_CONFIG })],
  wf_hook_delete: ['Delete a repository webhook. Missing is reported as already_absent.', obj(['repo', 'id'], HOOK_ID)],
  wf_hook_ping: ['Send a ping delivery to a repository webhook.', obj(['repo', 'id'], HOOK_ID)],
  wf_hook_test: ['Re-send the latest push event to a repository webhook.', obj(['repo', 'id'], HOOK_ID)],
  wf_hook_deliveries: ['List recent delivery metadata for a repository webhook (no payloads).', obj(['repo', 'id'], { ...HOOK_ID, per_page: PER_PAGE })],
  wf_hook_redeliver: ['Redeliver one webhook delivery.', obj(['repo', 'id', 'delivery_id'], { ...HOOK_ID, delivery_id: ID })],
  wf_org_hooks: ['List organization webhooks (receipts; secret never returned). Organization Webhooks read.', obj(['repo'], { repo: REPO, per_page: PER_PAGE, page: PAGE })],
  wf_org_hook_get: ['Read one organization webhook. Organization Webhooks read.', obj(['repo', 'id'], HOOK_ID)],
  wf_org_hook_create: ['Create an organization webhook. The url must not carry credentials. Organization Webhooks write.', obj(['repo', 'url'], { repo: REPO, ...HOOK_CONFIG })],
  wf_org_hook_update: ['Update an organization webhook.', obj(['repo', 'id'], { ...HOOK_ID, ...HOOK_CONFIG })],
  wf_org_hook_delete: ['Delete an organization webhook. Missing is reported as already_absent.', obj(['repo', 'id'], HOOK_ID)],
  wf_org_hook_ping: ['Send a ping delivery to an organization webhook.', obj(['repo', 'id'], HOOK_ID)],
  wf_deploy_key_get: ['Read one deploy key (public key only). Administration read.', obj(['repo', 'key_id'], { repo: REPO, key_id: ID })],
  wf_deploy_key_add: ['Add a deploy key from a PUBLIC ssh key; read_only defaults to true. Administration write.', obj(['repo', 'title', 'key'], { repo: REPO, title: { type: 'string', minLength: 1, maxLength: 200 }, key: { type: 'string', minLength: 20, maxLength: 9000 }, read_only: { type: 'boolean' } })],
  wf_deploy_key_delete: ['Delete a deploy key. Missing is reported as already_absent.', obj(['repo', 'key_id'], { repo: REPO, key_id: ID })],
  wf_environment_put: ['Create or update a repository environment (wait timer, self-review, reviewers, branch policy). Administration write.', obj(['repo', 'name'], {
    repo: REPO, name: ENV, wait_timer: { type: 'integer', minimum: 0, maximum: 43200 }, prevent_self_review: { type: 'boolean' },
    reviewers: { type: 'array', minItems: 0, maxItems: 6, items: obj(['type', 'id'], { type: { type: 'string', enum: ['User', 'Team'] }, id: ID }) },
    deployment_branch_policy: obj(['protected_branches', 'custom_branch_policies'], { protected_branches: { type: 'boolean' }, custom_branch_policies: { type: 'boolean' } }),
  })],
  wf_environment_delete: ['Delete a repository environment. Missing is reported as already_absent.', obj(['repo', 'name'], { repo: REPO, name: ENV })],
  wf_env_branch_policies: ['List an environment deployment branch policies. Administration read.', obj(['repo', 'name'], { repo: REPO, name: ENV, per_page: PER_PAGE, page: PAGE })],
  wf_env_branch_policy_create: ['Add an environment deployment branch or tag policy.', obj(['repo', 'name', 'policy_name'], { repo: REPO, name: ENV, policy_name: { type: 'string', minLength: 1, maxLength: 255 }, type: { type: 'string', enum: ['branch', 'tag'] } })],
  wf_env_branch_policy_delete: ['Delete an environment deployment branch policy. Missing is reported as already_absent.', obj(['repo', 'name', 'policy_id'], { repo: REPO, name: ENV, policy_id: ID })],
  wf_env_variables: ['List environment Actions variables (non-secret values). Variables read.', obj(['repo', 'name'], { repo: REPO, name: ENV, per_page: PER_PAGE, page: PAGE })],
  wf_env_variable_set: ['Create or update an environment Actions variable (non-secret value). The variable name is the variable argument.', obj(['repo', 'name', 'variable', 'value'], { repo: REPO, name: ENV, variable: VNAME, value: { type: 'string', maxLength: 8192 } })],
  wf_env_variable_delete: ['Delete an environment Actions variable. Missing is reported as already_absent.', obj(['repo', 'name', 'variable'], { repo: REPO, name: ENV, variable: VNAME })],
};
export const HOOK_READ_ACTIONS = ['wf_hook_get', 'wf_hook_deliveries', 'wf_org_hooks', 'wf_org_hook_get', 'wf_deploy_key_get', 'wf_env_branch_policies', 'wf_env_variables'];
export const isHookAction = (action) => Object.hasOwn(HOOK_SPECS, action);

const posInt = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const str = (n) => z.string().max(n);
const nullStr = (n) => z.string().max(n).nullable();
const hook = z.object({ id: posInt, name: str(100), active: z.boolean(), events: z.array(str(100)).max(50), url: str(2000), url_withheld: z.boolean(), content_type: z.enum(['json', 'form']).nullable(), has_secret: z.boolean() }).strict();
const delivery = z.object({ id: posInt, guid: nullStr(100), delivered_at: nullStr(100), event: nullStr(100), action: nullStr(100), status: nullStr(100), status_code: z.number().int().nullable(), redelivery: z.boolean().nullable() }).strict();
const deployKey = z.object({ id: posInt, title: str(200), read_only: z.boolean(), verified: z.boolean().nullable(), key: str(9000) }).strict();
const policy = z.object({ id: posInt, name: str(255), type: z.enum(['branch', 'tag']).nullable() }).strict();
const varName = z.string().regex(/^[A-Z_][A-Z0-9_]{0,99}$/);
const done = (extra) => z.object({ completed: z.literal(true), ...extra, already_absent: z.literal(true).optional() }).strict();
const pageNo = z.number().int().min(1).max(1000);
const SCHEMAS = {
  wf_hook_get: z.object({ hook }).strict(),
  wf_hook_create: z.object({ hook }).strict(),
  wf_hook_update: z.object({ hook }).strict(),
  wf_org_hook_get: z.object({ hook }).strict(),
  wf_org_hook_create: z.object({ hook }).strict(),
  wf_org_hook_update: z.object({ hook }).strict(),
  wf_hook_delete: done({ id: posInt }),
  wf_org_hook_delete: done({ id: posInt }),
  wf_hook_ping: done({ id: posInt }),
  wf_hook_test: done({ id: posInt }),
  wf_org_hook_ping: done({ id: posInt }),
  wf_hook_redeliver: done({ id: posInt, delivery_id: posInt }),
  wf_hook_deliveries: z.object({ deliveries: z.array(delivery).max(50) }).strict(),
  wf_org_hooks: z.object({ hooks: z.array(hook).max(50), page: pageNo, has_more: z.boolean() }).strict(),
  wf_deploy_key_get: z.object({ deploy_key: deployKey }).strict(),
  wf_deploy_key_add: z.object({ deploy_key: deployKey }).strict(),
  wf_deploy_key_delete: done({ key_id: posInt }),
  wf_environment_delete: done({ name: str(255) }),
  wf_env_branch_policies: z.object({ policies: z.array(policy).max(50), total_count: z.number().int().nonnegative(), page: pageNo }).strict(),
  wf_env_branch_policy_create: z.object({ policy }).strict(),
  wf_env_branch_policy_delete: done({ policy_id: posInt }),
  wf_env_variables: z.object({ variables: z.array(z.object({ name: varName, value: str(8192), created_at: nullStr(100), updated_at: nullStr(100) }).strict()).max(50), total_count: z.number().int().nonnegative(), page: pageNo }).strict(),
  wf_env_variable_set: z.object({ completed: z.literal(true), name: str(255), variable: varName }).strict(),
  wf_env_variable_delete: done({ name: str(255), variable: varName }),
};
// wf_environment_put is validated by the caller against the shared environment summary schema.
export function validHookResponse(action, body, args) {
  const schema = SCHEMAS[action];
  if (!schema || !schema.safeParse(body).success) return false;
  if (!args) return true;
  if ('id' in body && 'id' in args && body.id !== args.id) return false;
  if (body.hook && 'id' in args && body.hook.id !== args.id) return false;
  if ('variable' in body && body.variable !== args.variable) return false;
  if ('key_id' in body && body.key_id !== args.key_id) return false;
  if ('policy_id' in body && body.policy_id !== args.policy_id) return false;
  if ('delivery_id' in body && body.delivery_id !== args.delivery_id) return false;
  if ('name' in body && typeof args.name === 'string' && body.name !== args.name) return false;
  if ('page' in body && body.page !== (args.page ?? 1)) return false;
  return true;
}
