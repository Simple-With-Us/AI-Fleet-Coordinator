import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const workflow = readFileSync(new URL('./workflows/pr-autofix-reusable.yml', import.meta.url), 'utf8');
const job = workflow.slice(workflow.indexOf('jobs:'));
const steps = job.slice(job.indexOf('    steps:')).split(/\n      - /).slice(1);

const resolve = steps.find((step) => step.includes('id: pr'));
const runFix = steps.find((step) => step.includes('id: fix'));
const validate = steps.find((step) => step.includes('id: validate'));
const diagnostics = steps.find((step) => step.includes('id: diagnostics'));
const upload = steps.find((step) => step.includes('uses: actions/upload-artifact@'));
const push = steps.find((step) => step.includes('name: Commit And Push'));

test('model run fails closed and validation precedes push', () => {
  assert(resolve && runFix && validate && push);
  assert.match(resolve, /headRefOid/);
  assert.match(runFix, /set -euo pipefail/);
  assert(!runFix.includes('|| true'));
  assert.match(runFix, /--output-format stream-json/);
  assert.match(runFix, /claude-execution-output\.json/);
  assert(steps.indexOf(runFix) < steps.indexOf(validate));
  assert(steps.indexOf(validate) < steps.indexOf(push));
  assert.match(validate, /head_sha/);
  assert.match(validate, /verify\.sh/);
  assert.match(push, /START_HEAD_SHA/);
});

test('sanitized diagnostics are retained without raw transcripts', () => {
  assert(diagnostics && upload);
  assert.match(diagnostics, /if: \$\{\{ always\(\)/);
  assert.match(diagnostics, /uses: Simple-With-Us\/AI-Fleet-Coordinator\/\.github\/actions\/autofix-diagnostics@[a-f0-9]{40}/);
  assert(steps.indexOf(runFix) < steps.indexOf(diagnostics));
  assert(steps.indexOf(diagnostics) < steps.indexOf(upload));
  assert.match(upload, /steps\.diagnostics\.outputs\.summary-path != ''/);
  assert.match(upload, /retention-days: 7\s*$/);
});

test('trusted policy is loaded from the default branch, not the PR checkout', () => {
  assert.match(workflow, /pr-autofix-trusted\.yml/);
  assert.match(workflow, /contents\/\$path\?ref=\$DEFAULT_BRANCH/);
  assert(!workflow.includes('configured=$(awk \'/^max_iterations:/\' .autofix.yml'));
});

test('the caller, not the reusable job, holds the per-PR concurrency group', () => {
  // A job-level copy of the caller's group deadlocks:  GitHub cancels the job before any step
  // ("Canceling since a deadlock was detected ... between a top level workflow and
  // 'autofix / autofix'", BotFleet run 39054950194).  Serialization lives in the caller.
  assert.doesNotMatch(job, /^\s+concurrency:/m);
  const caller = readFileSync(new URL('./workflows/autofix.yml', import.meta.url), 'utf8');
  const top = caller.slice(0, caller.indexOf('jobs:'));
  assert.match(top, /^concurrency:\n  group: pr-autofix-\$\{\{ github\.repository \}\}-\$\{\{ github\.event\.issue\.number \|\| github\.event\.pull_request\.number \}\}\n  cancel-in-progress: false$/m);
});

test('push prefers the GitHub App installation token when configured', () => {
  assert.match(workflow, /create-github-app-token@/);
  assert.match(push, /PUSH_TOKEN/);
  assert.match(workflow, /vars\.SWU_AUTOFIX_GITHUB_APP_ID/);
  assert.match(workflow, /id: app_push/);
  assert.match(workflow, /mint_token=true/);
});

test('bot guard reads review.user only on pull_request_review events', () => {
  assert.match(job, /github\.event_name == 'pull_request_review'/);
  assert.match(job, /github\.event\.review\.user\.type/);
  assert.match(job, /github\.event_name != 'pull_request_review'/);
  assert.match(job, /github\.event\.comment\.user\.type/);
  assert.doesNotMatch(job, /github\.event\.comment\.user\.type \|\| github\.event\.review\.user\.type/);
});

test('step if expressions never reference secrets so startup validation passes', () => {
  for (const step of steps) {
    const ifLine = step
      .split('\n')
      .find((line) => line.trimStart().startsWith('if:'));
    if (ifLine) {
      assert(!ifLine.includes('secrets.'), `step if must not reference secrets: ${ifLine.trim()}`);
    }
  }
});

test('usage metering runs in a separate job that holds the ingest credential, never beside the model', () => {
  const extract = steps.find((step) => step.includes('name: Extract Usage Counts'));
  const retain = steps.find((step) => step.includes('name: Retain Usage Counts'));
  const report = steps.find((step) => step.includes('name: Report Usage To Usage Monitor'));
  assert(extract && retain && report);
  // The model job extracts counters only, with no secret, and hands them over as a one-day artifact.
  assert.match(extract, /select\(\.type == "result"\)/);
  assert.match(extract, /claude-stream\.ndjson/);
  assert.match(extract, /continue-on-error: true/);
  assert(!extract.includes('USAGE_INGEST_TOKEN'));
  assert.match(retain, /retention-days: 1/);
  assert.match(retain, /if-no-files-found: ignore/);
  assert(steps.indexOf(runFix) < steps.indexOf(extract));
  assert(steps.indexOf(extract) < steps.indexOf(retain));
  assert(steps.indexOf(retain) < steps.indexOf(validate));
  // The reporter lives in the `meter` job, which cannot run the model or push.
  const meter = workflow.slice(workflow.indexOf('\n  meter:'));
  assert(meter.includes(report.slice(0, 80)));
  assert.match(meter, /needs: autofix/);
  assert.match(meter, /if: \$\{\{ always\(\) && needs\.autofix\.result != 'skipped' \}\}/);
  assert.match(meter, /permissions:\n      actions: read\n/);
  assert(!meter.includes('contents: write'));
  assert(!meter.includes('MINIMAX_API_KEY'));
  assert.match(report, /continue-on-error: true/);
  assert.match(report, /uses: Simple-With-Us\/congress-trading-shared\/\.github\/actions\/ci-usage-report@[a-f0-9]{40}\n/);
  assert.match(report, /token: \$\{\{ secrets\.USAGE_INGEST_TOKEN \}\}/);
  assert.match(report, /harness: claude-code-ci/);
  assert.match(report, /input-kind: claude/);
  // The credential is optional, and the only place it is read is the meter job.
  assert.match(workflow, /USAGE_INGEST_TOKEN:\n        required: false/);
  assert.equal((workflow.match(/secrets\.USAGE_INGEST_TOKEN/g) ?? []).length, 1);
  assert.equal(workflow.indexOf('secrets.USAGE_INGEST_TOKEN') > workflow.indexOf('\n  meter:'), true);
});

test('all autofix families pin diagnostics to the published AFC migration commit', () => {
  // AFC-350: a 40-hex shape alone accepted a commit absent from this repository.
  // This immutable revision contains both action.yml and reduce.mjs; verify both
  // at the replacement revision before deliberately updating this allowlist.
  const diagnosticsRef = 'Simple-With-Us/AI-Fleet-Coordinator/.github/actions/autofix-diagnostics@070fdbfacadbd9e90cbb54098123df7d3fef737f';
  for (const family of ['pr', 'claude-code', 'codex']) {
    const source = readFileSync(new URL(`./workflows/${family}-autofix-reusable.yml`, import.meta.url), 'utf8');
    const refs = [...source.matchAll(/^\s*uses:\s*(\S*autofix-diagnostics\S*)\s*$/gm)].map((match) => match[1]);
    assert.deepEqual(refs, [diagnosticsRef], `${family} must use the verified published diagnostics action`);
  }
});

test('a PR branch .autofix.yml may only narrow the trusted allowed_bots list', () => {
  // A same-repo branch PR ships its own .autofix.yml on the checkout the gate reads, so letting
  // that file replace the list let any branch point the contents:write run at an arbitrary bot.
  // The trusted base comes from the default-branch policy (or the caller input as legacy
  // fallback), the branch file may only remove entries, and narrowing to nothing fails closed.
  assert.match(workflow, /TRUSTED_ALLOWED_BOTS/);
  assert.match(workflow, /allowed_bots=\$file_bots/);
  assert.match(workflow, /may only REMOVE bots/);
  assert.match(workflow, /narrows the trusted bot list to nothing, skipping/);
  assert(!workflow.includes('if [ -n "$file_allowed" ]; then allowed="$file_allowed"; fi'));
});

const claudeReusable = readFileSync(new URL('./workflows/claude-code-autofix-reusable.yml', import.meta.url), 'utf8');
const codexReusable = readFileSync(new URL('./workflows/codex-autofix-reusable.yml', import.meta.url), 'utf8');

test('one autofix run per bot per PR, owner and Sentry exempt (owner rule 2026-10-20)', () => {
  for (const [name, text] of [['pr-autofix', workflow], ['claude-code', claudeReusable], ['codex', codexReusable]]) {
    // The dedupe record is a marker comment on the PR;  deleting it is the documented reset.
    assert.match(text, /swu-autofix-run: bot=\$requester/, name);
    assert.match(text, /already used its one autofix run on this PR/, name);
    assert.match(text, /jaywedgeworth22/, name);
    assert.match(text, /sentry/, name);
    assert.match(text, /NOTIFY_URL/, name);
    // Every bot request, accepted or not, goes to the owner when the channel is configured;
    // the step is dormant until SWU_AUTOFIX_NOTIFY_URL exists.
    assert.match(text, /SWU_AUTOFIX_NOTIFY_URL is not set/, name);
    // Marker comments need issues: write.
    assert.match(text, /issues: write/, name);
  }
});

test('request gate precedes the paid model run in the claude-code-action reusables', () => {
  for (const [name, text] of [['claude-code', claudeReusable], ['codex', codexReusable]]) {
    const gate = text.indexOf('id: request_gate');
    const run = text.indexOf('anthropics/claude-code-action@');
    assert(gate !== -1 && run !== -1 && gate < run, name);
    assert.match(text, /steps\.request_gate\.outputs\.skip != 'true'/, name);
  }
});

test('autofix requires an explicit tag per type; untagged bot comments fire nothing (owner rules 2026-10-20 and 2026-10-21)', () => {
  // Jay:  "that is dumb needs to be tag of it", and 2026-10-21:  the tag must OPEN the
  // comment - a review that mentions the tag while discussing it is discussion, not a
  // request (Kody's verification replies started runs nobody requested).  Sentry is
  // exempt from the tag, the cap, and the notification;  the owner is exempt from the
  // cap only.  scripts/test_autofix_request_gate.py runs the marked block itself.
  for (const [name, text] of [['pr-autofix', workflow], ['claude-code', claudeReusable], ['codex', codexReusable]]) {
    assert.match(text, /REQUEST-GATE-BEGIN/, name);
    assert.match(text, /REQUEST-GATE-END/, name);
    assert.match(text, /gate_verdict=request/, name);
  }
  assert.match(workflow, /No @pr-autofix request at the start of the comment, skipping/);
  assert.match(claudeReusable, /No @claude-code-autofix request at the start of the review, skipping/);
  assert.match(codexReusable, /No @codex-autofix request at the start of the review, skipping/);
  // The tag check runs before the marker is written, so an untagged comment never
  // consumes the bot's one request on the PR.
  for (const [name, text] of [['claude-code', claudeReusable], ['codex', codexReusable]]) {
    assert(text.indexOf('REQUEST-GATE-BEGIN') < text.indexOf('swu-autofix-run: bot='), name);
  }
  assert(workflow.indexOf('REQUEST-GATE-BEGIN') < workflow.indexOf('swu-autofix-run: bot='), 'pr-autofix order');
});

test('org variable retargets model and endpoint fleet-wide, pins stay the fallback (owner switch 2026-10-21)', () => {
  for (const [name, text] of [['pr-autofix', workflow], ['claude-code', claudeReusable], ['codex', codexReusable]]) {
    assert.match(text, /vars\.SWU_AUTOFIX_MODEL/, name);
    assert.match(text, /vars\.SWU_AUTOFIX_MODEL_BASE_URL/, name);
    assert.match(text, /META_MODEL_API_TOKEN/, name);
    // Unset variables keep exactly the MiniMax configuration that ran before the switch.
    assert.match(text, /https:\/\/api\.minimax\.io\/anthropic/, name);
    assert.match(text, /MiniMax-M3\.1-Flash/, name);
    // Closed presets only:  an exact match on the Meta base URL selects host and key
    // together,  anything else keeps the MiniMax pair,  and a missing Meta secret fails
    // closed on a sentinel instead of forwarding the MiniMax credential.
    assert.match(text, /vars\.SWU_AUTOFIX_MODEL_BASE_URL == 'https:\/\/api\.meta\.ai' && 'https:\/\/api\.meta\.ai'/, name);
    assert.match(text, /\(secrets\.META_MODEL_API_TOKEN \|\| 'MISSING-META-MODEL-API-TOKEN'\)/, name);
    assert.doesNotMatch(text, /contains\(vars\.SWU_AUTOFIX_MODEL_BASE_URL/, name);
  }
  // In the trusted-policy loader the variable is applied after the default-branch file,
  // so the owner-controlled switch outranks the in-repo pin.
  const fileIdx = workflow.indexOf('if [ -n "$file_model" ]; then model="$file_model"; fi');
  const varIdx = workflow.indexOf('if [ -n "$VAR_MODEL" ]; then model="$VAR_MODEL"; fi');
  assert(fileIdx !== -1 && varIdx !== -1 && varIdx > fileIdx, 'VAR_MODEL must be applied after the trusted-file pin');
});

test('model switch is guarded:  endpoint and model move as one valid pair (Kody 2026-10-21)', () => {
  for (const [name, text] of [['pr-autofix', workflow], ['claude-code', claudeReusable], ['codex', codexReusable]]) {
    assert.match(text, /name: Validate Model Switch/, name);
    assert.match(text, /"" \| https:\/\/api\.minimax\.io\/anthropic\) route=minimax ;;/, name);
    assert.match(text, /https:\/\/api\.meta\.ai\) route=meta ;;/, name);
    assert.match(text, /\[Mm\]\[Ii\]\[Nn\]\[Ii\]\[Mm\]\[Aa\]\[Xx\]\*\) model_minimax=yes ;;/, name);
    assert.match(text, /\[ "\$route" = "meta" \] && \[ -n "\$VAR_MODEL" \] && \[ -n "\$META_TOKEN" \] && \[ "\$model_minimax" = "no" \]/, name);
    assert.match(text, /META_MODEL_API_TOKEN provisioned/, name);
    assert.match(text, /must be unset or https:\/\/api\.minimax\.io\/anthropic \(MiniMax\) or exactly https:\/\/api\.meta\.ai/, name);
    assert.match(text, /names a non-MiniMax model id while SWU_AUTOFIX_MODEL_BASE_URL selects the MiniMax host/, name);
    // Caller-controlled values must never reach the workflow command:  only set/unset
    // flags are echoed, so a hostile var cannot inject runner commands (Kody 2026-10-21).
    const guardScript = text.slice(text.indexOf('name: Validate Model Switch'), text.indexOf('name: Autofix Request Gate') === -1 ? undefined : text.length);
    assert.doesNotMatch(guardScript, /::error::[^"]*'\$VAR_(BASE|MODEL)'/, name);
    // The guard runs before the request gate / policy loader, so a bad pair fails before any run starts.
    const guardIdx = text.indexOf('name: Validate Model Switch');
    const nextIdx = Math.max(text.indexOf('name: Autofix Request Gate'), text.indexOf('name: Load Trusted Autofix Policy'));
    assert(guardIdx !== -1 && nextIdx > guardIdx, name);
  }
  // The guard honors the proceed gate so skipped closed / fork / mirror runs skip
  // instead of failing (Kody 2026-10-21).
  const prGuard = workflow.slice(workflow.indexOf('name: Validate Model Switch'));
  assert.match(prGuard.slice(0, 200), /if: steps\.pr\.outputs\.proceed == 'true'/);
  // anthropic_api_key feeds claude-code-action directly (its inline-comment classifier
  // ignores ANTHROPIC_BASE_URL),  so in the action-based reusables it must follow the
  // same closed host preset as ANTHROPIC_AUTH_TOKEN instead of always sending the
  // MiniMax key (Kody review on congress-trading-shared#394).
  for (const [name, text] of [['claude-code', claudeReusable], ['codex', codexReusable]]) {
    assert.match(
      text,
      /anthropic_api_key: \$\{\{ vars\.SWU_AUTOFIX_MODEL_BASE_URL == 'https:\/\/api\.meta\.ai' && \(secrets\.META_MODEL_API_TOKEN \|\| 'MISSING-[A-Z-]+'\) \|\| secrets\.MINIMAX_API_KEY \}\}/,
      name,
    );
  }
});
