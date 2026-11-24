import {z} from 'zod';
// Contract for the relay secret verbs (git-relay/git_relay/secret_verbs.py).  Values are sealed on
// the caller machine (libsodium sealed box) and only ciphertext + key_id travel.  Answers carry
// names, visibility, timestamps and counts, never a value.
const REPO = { type: 'string', pattern: '^(?!.*\\.\\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$', description: 'owner/name; org-level verbs use its owner as the organization' };
const obj = (required, properties) => ({ type: 'object', additionalProperties: false, required, properties });
// No regex patterns here beyond REPO and the variable name: simple-git compiles only its reviewed pattern set.  The relay enforces the exact secret-name, environment, key and ciphertext shapes.
const NAME = { type: 'string', minLength: 1, maxLength: 100 };
const VNAME = { type: 'string', pattern: '^(?!GITHUB_)[A-Z_][A-Z0-9_]{0,99}$' };
const ENV = { type: 'string', minLength: 1, maxLength: 100 };
const PER_PAGE = { type: 'integer', minimum: 1, maximum: 50 };
const PAGE = { type: 'integer', minimum: 1, maximum: 1000 };
const CIPHER = { type: 'string', minLength: 60, maxLength: 70000, description: 'base64 libsodium sealed-box ciphertext' };
const KEY_ID = { type: 'string', minLength: 1, maxLength: 64 };
const VISIBILITY = { type: 'string', enum: ['all', 'private', 'selected'] };
const IDS = { type: 'array', maxItems: 1000, items: { type: 'integer', minimum: 1, maximum: 9007199254740991 } };

export const SECRET_SCOPES = {
  org_actions: { org: true, what: 'organization Actions secret' },
  org_dependabot: { org: true, what: 'organization Dependabot secret' },
  org_codespaces: { org: true, what: 'organization Codespaces secret' },
  repo_actions: { org: false, what: 'repository Actions secret' },
  repo_dependabot: { org: false, what: 'repository Dependabot secret' },
  repo_codespaces: { org: false, what: 'repository Codespaces secret' },
  env_actions: { org: false, env: true, what: 'environment Actions secret' },
};
const NOTE = ' Sealed client-side; the relay and Worker never see a plaintext value. Answers are names and counts only.';
export const SECRET_SPECS = {};
export const SECRET_READ_ACTIONS = [];
for (const [scope, { org, env, what }] of Object.entries(SECRET_SCOPES)) {
  const base = env ? { repo: REPO, environment: ENV } : { repo: REPO };
  const req = env ? ['repo', 'environment'] : ['repo'];
  SECRET_SPECS[`wf_secrets_${scope}`] = [`List ${what} names (no values).${NOTE}`, obj(req, { ...base, per_page: PER_PAGE, page: PAGE })];
  SECRET_SPECS[`wf_secret_key_${scope}`] = [`Read the public sealing key and key_id for ${what}s.${NOTE}`, obj(req, base)];
  SECRET_SPECS[`wf_secret_put_${scope}`] = [`Create or replace a ${what} from sealed ciphertext.${NOTE}`, obj([...req, 'name', 'encrypted_value', 'key_id'], { ...base, name: NAME, encrypted_value: CIPHER, key_id: KEY_ID, ...(org ? { visibility: VISIBILITY, selected_repository_ids: IDS } : {}) })];
  SECRET_SPECS[`wf_secret_delete_${scope}`] = [`Delete a ${what}. Missing is reported as already_absent.${NOTE}`, obj([...req, 'name'], { ...base, name: NAME })];
  SECRET_READ_ACTIONS.push(`wf_secrets_${scope}`, `wf_secret_key_${scope}`);
  if (org) {
    SECRET_SPECS[`wf_secret_repos_${scope}`] = [`List repositories a selected-visibility ${what} is shared with.${NOTE}`, obj(['repo', 'name'], { repo: REPO, name: NAME, per_page: PER_PAGE, page: PAGE })];
    SECRET_SPECS[`wf_secret_repos_set_${scope}`] = [`Replace the repositories a selected-visibility ${what} is shared with.${NOTE}`, obj(['repo', 'name', 'selected_repository_ids'], { repo: REPO, name: NAME, selected_repository_ids: IDS })];
    SECRET_READ_ACTIONS.push(`wf_secret_repos_${scope}`);
  }
}
SECRET_SPECS.wf_org_variables = ['List organization Actions variables (non-secret).', obj(['repo'], { repo: REPO, per_page: PER_PAGE, page: PAGE })];
SECRET_SPECS.wf_org_variable_set = ['Create or update an organization Actions variable (non-secret value).', obj(['repo', 'name', 'value'], { repo: REPO, name: VNAME, value: { type: 'string', maxLength: 8192 }, visibility: VISIBILITY, selected_repository_ids: IDS })];
SECRET_SPECS.wf_org_variable_delete = ['Delete an organization Actions variable.', obj(['repo', 'name'], { repo: REPO, name: VNAME })];
SECRET_SPECS.wf_org_variable_repos = ['List repositories an organization variable is shared with.', obj(['repo', 'name'], { repo: REPO, name: VNAME, per_page: PER_PAGE, page: PAGE })];
SECRET_SPECS.wf_org_variable_repos_set = ['Replace the repositories an organization variable is shared with.', obj(['repo', 'name', 'selected_repository_ids'], { repo: REPO, name: VNAME, selected_repository_ids: IDS })];
SECRET_READ_ACTIONS.push('wf_org_variables', 'wf_org_variable_repos');

export const SECRET_INPUT_LIMITS = Object.fromEntries(Object.keys(SECRET_SPECS).filter((a) => a.startsWith('wf_secret_put_')).map((a) => [a, 98304]));
export const isSecretAction = (action) => Object.hasOwn(SECRET_SPECS, action);

const nm = z.string().regex(/^[A-Za-z_][A-Za-z0-9_]{0,99}$/);
const ts = z.string().max(100).nullable().optional();
const posInt = z.number().int().positive();
const repoRow = z.object({ id: posInt, full_name: z.string().max(200).nullable().optional() }).strict();
export function validSecretResponse(action, body, args) {
  const scope = action.replace(/^wf_secrets?_(?:key_|put_|delete_|repos_set_|repos_)?/, '');
  let schema;
  if (action.startsWith('wf_secrets_')) {
    schema = z.object({ secrets: z.array(z.object({ name: nm, created_at: ts, updated_at: ts, visibility: z.string().max(20).optional() }).strict()).max(50), total_count: z.number().int().nonnegative(), page: z.number().int().min(1).max(1000) }).strict();
  } else if (action.startsWith('wf_secret_key_')) {
    schema = z.object({ key_id: z.string().regex(/^[A-Za-z0-9_-]{1,64}$/), key: z.string().regex(/^[A-Za-z0-9+/]+={0,2}$/).max(200) }).strict();
  } else if (action.startsWith('wf_secret_repos_set_') || action === 'wf_org_variable_repos_set') {
    schema = z.object({ completed: z.literal(true), name: nm, count: z.number().int().nonnegative() }).strict();
  } else if (action.startsWith('wf_secret_repos_') || action === 'wf_org_variable_repos') {
    schema = z.object({ repositories: z.array(repoRow).max(50), total_count: z.number().int().nonnegative(), page: z.number().int().min(1).max(1000) }).strict();
  } else if (action === 'wf_org_variables') {
    schema = z.object({ variables: z.array(z.object({ name: nm, value: z.string().max(8192), created_at: ts, updated_at: ts, visibility: z.string().max(20).nullable().optional() }).strict()).max(50), total_count: z.number().int().nonnegative(), page: z.number().int().min(1).max(1000) }).strict();
  } else {
    schema = z.object({ completed: z.literal(true), name: nm, already_absent: z.literal(true).optional() }).strict();
  }
  if (!schema.safeParse(body).success) return false;
  if (!args) return true;
  if ('name' in body && body.name !== args.name) return false;
  if (action.startsWith('wf_secrets_') || action === 'wf_org_variables') return body.page === (args.page ?? 1);
  void scope;
  return true;
}
