import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// Contract for .github/workflows/autofix-ci.yml.  The autofix.ci hosted service refuses heads whose
// recent commits are bot-authored, so same-repo PRs push the Ruff fix themselves as the fleet's
// autofix App (owner:  "change it so that bot commits aren't discriminated against").

const workflow = readFileSync(new URL('./workflows/autofix-ci.yml', import.meta.url), 'utf8');
const job = workflow.slice(workflow.indexOf('jobs:'));
const steps = job.slice(job.indexOf('    steps:')).split(/\n      - /).slice(1);

const find = (needle) => steps.find((step) => step.includes(needle));
const checkout = find('uses: actions/checkout@');
const ruff = find('name: Format changed Python files with Ruff');
const creds = find('id: app_push');
const mint = find('id: push_token');
const push = find('name: Commit And Push The Ruff Fix As The Autofix App');
const missing = find('name: Note The Missing Autofix App');
const fallback = find('uses: autofix-ci/action@');
const ifOf = (step) => (step.split('\n').find((line) => line.trimStart().startsWith('if:')) ?? '').trim();

test('workflow keeps the name autofix-ci/action requires and the job token stays read-only', () => {
  assert.match(workflow, /^name: autofix\.ci$/m);
  assert.match(workflow, /^permissions:\n  contents: read\n/m);
  assert.doesNotMatch(workflow, /contents: write/);
  assert.match(job, /if: github\.head_ref != 'handbook-sync\/mirror'/);
});

test('same-repo PRs are detected by head repository, never by commit author', () => {
  assert.match(
    job,
    /SAME_REPO_PR: \$\{\{ github\.event_name == 'pull_request' && github\.event\.pull_request\.head\.repo\.full_name == github\.repository \}\}/,
  );
  // Only the loop guard may look at authorship;  no step is gated on who wrote the head.
  for (const step of steps) {
    assert.doesNotMatch(ifOf(step), /user|author|actor|bot/i, `step if must not gate on authorship: ${ifOf(step)}`);
  }
  assert.match(checkout, /github\.event\.pull_request\.head\.sha \|\| ''/);
});

test('the App token is minted the way pr-autofix-reusable does, with the same pin', () => {
  assert(creds && mint && push && missing && fallback && ruff);
  assert.equal(ifOf(creds), "if: env.SAME_REPO_PR == 'true' && vars.SWU_AUTOFIX_GITHUB_APP_ID != ''");
  assert.match(creds, /AUTOFIX_APP_PRIVATE_KEY: \$\{\{ secrets\.SWU_AUTOFIX_GITHUB_APP_PRIVATE_KEY \}\}/);
  assert.match(creds, /mint_token=true/);
  assert.equal(ifOf(mint), "if: steps.app_push.outputs.mint_token == 'true'");
  assert.match(mint, /uses: actions\/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3\.3\.0/);
  const reusable = readFileSync(new URL('./workflows/pr-autofix-reusable.yml', import.meta.url), 'utf8');
  assert(reusable.includes('actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510'), 'pin must match pr-autofix-reusable');
  assert.match(mint, /app-id: \$\{\{ vars\.SWU_AUTOFIX_GITHUB_APP_ID \}\}/);
  assert.match(mint, /private-key: \$\{\{ secrets\.SWU_AUTOFIX_GITHUB_APP_PRIVATE_KEY \}\}/);
  assert(steps.indexOf(ruff) < steps.indexOf(creds));
  assert(steps.indexOf(mint) < steps.indexOf(push));
});

test('the push uses only the App token, as the App bot identity, never github.token', () => {
  assert.equal(ifOf(push), "if: env.SAME_REPO_PR == 'true' && steps.push_token.outputs.token != ''");
  assert.match(push, /PUSH_TOKEN: \$\{\{ steps\.push_token\.outputs\.token \}\}\n/);
  assert.doesNotMatch(push, /github\.token|GITHUB_TOKEN|secrets\.GITHUB/);
  assert.doesNotMatch(workflow, /\|\| github\.token/);
  assert.match(push, /APP_SLUG: \$\{\{ steps\.push_token\.outputs\.app-slug \}\}/);
  assert.match(push, /gh api "\/users\/\$\{APP_SLUG\}%5Bbot%5D" --jq \.id/);
  assert.match(push, /app_email="\$\{app_user_id\}\+\$\{APP_SLUG\}\[bot\]@users\.noreply\.github\.com"/);
  assert.match(push, /git commit -m "style: ruff format \(autofix\)"/);
  // checkout's persisted github.token header would otherwise override the App token.
  assert.match(push, /git -c http\.https:\/\/github\.com\/\.extraheader= -c credential\.helper= /);
  // The token reaches git through a credential helper reading the env, never a URL or argv.
  assert.match(push, /credential\.helper=!f\(\) \{ echo username=x-access-token; echo "password=\$\{PUSH_TOKEN\}"; \}; f/);
  assert.match(push, /push "https:\/\/github\.com\/\$\{GH_REPO\}\.git" "HEAD:refs\/heads\/\$\{BRANCH\}"/);
  assert.doesNotMatch(push, /x-access-token:\$\{PUSH_TOKEN\}@/);
  assert.doesNotMatch(push, /--force|-f /);
});

test('the loop guard looks only at this App, and fails naming the files', () => {
  const guard = push.slice(push.indexOf('Loop guard'));
  assert.match(guard, /head_email=\$\(git log -1 --format=%ae "\$HEAD_SHA"\)/);
  assert.match(guard, /if \[ "\$head_email" = "\$app_email" \] \|\| \[ "\$head_name" = "\$app_name" \]; then/);
  assert.match(guard, /::error::[^\n]*\$\{files\}/);
  assert.match(guard, /exit 1/);
  // The guard runs after the no-change exit, so a clean App head passes, and before the commit.
  assert(push.indexOf('nothing to push') < push.indexOf('Loop guard'));
  assert(push.indexOf('Loop guard') < push.indexOf('git commit'));
  // No other bot is named anywhere in the workflow.
  assert.doesNotMatch(workflow, /swu-[a-z-]+\[bot\]|github-actions\[bot\]/);
});

test('without the App token, autofix-ci/action runs exactly as before, with a notice for same-repo PRs', () => {
  // Also runs when the App push staged nothing but workflow files, which the App may not change.
  assert.equal(ifOf(fallback), "if: steps.push_token.outputs.token == '' || steps.app_commit.outputs.fallback == 'true'");
  assert.match(fallback, /uses: autofix-ci\/action@v1\n/);
  assert.match(fallback, /fail-fast: false/);
  assert.equal(ifOf(missing), "if: env.SAME_REPO_PR == 'true' && steps.push_token.outputs.token == ''");
  assert.match(missing, /::notice::[^\n]*bot-authored heads are not covered/);
  assert(steps.indexOf(push) < steps.indexOf(fallback));
});

test('step if expressions never reference secrets so startup validation passes', () => {
  for (const step of steps) {
    assert(!ifOf(step).includes('secrets.'), `step if must not reference secrets: ${ifOf(step)}`);
  }
});

test('a failed token mint and a moved branch both fall back instead of failing the run', () => {
  const mint = steps.find((step) => step.includes('name: Mint Autofix Push Token'));
  assert.match(mint, /continue-on-error: true/);
  assert.match(push, /remote_sha=\$\(git ls-remote origin "refs\/heads\/\$\{BRANCH\}" \| cut -f1\)/);
  assert(push.indexOf('remote_sha=') < push.indexOf('git commit'));
  assert.match(push, /echo "fallback=true" >> "\$GITHUB_OUTPUT"/);
});
