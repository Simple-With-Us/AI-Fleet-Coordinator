import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const script = path.join(path.dirname(new URL(import.meta.url).pathname), '..', 'autofix-model-route.sh');

function run(env) {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'autofix-route-'));
  const envFile = path.join(temp, 'env');
  const outFile = path.join(temp, 'out');
  const payload = { ...env, GITHUB_ENV: envFile, GITHUB_OUTPUT: outFile };
  execFileSync('bash', [script], { env: payload, stdio: 'pipe' });
  const outputs = Object.fromEntries(
    fs.readFileSync(outFile, 'utf8').trim().split('\n').map(line => line.split('=')),
  );
  const written = fs.readFileSync(envFile, 'utf8');
  return { outputs, written };
}

test('meta route maps contributor id and clears anthropic api key', () => {
  const { outputs, written } = run({
    VAR_BASE: 'https://api.meta.ai',
    VAR_MODEL: 'muse-spark-1.3-contributor',
    DEFAULT_MODEL: 'MiniMax-M3.1-Flash',
  });
  assert.equal(outputs.route, 'meta');
  assert.equal(outputs.anthropic_api_key_mode, 'none');
  assert.equal(outputs.effective_model, 'muse-spark-1.3');
  assert.match(written, /^ANTHROPIC_API_KEY=$/m);
  assert.match(written, /ENABLE_TOOL_SEARCH=true/);
});

test('minimax route keeps anthropic api key mode', () => {
  const { outputs, written } = run({
    VAR_BASE: '',
    VAR_MODEL: '',
    DEFAULT_MODEL: 'MiniMax-M3.1-Flash',
  });
  assert.equal(outputs.route, 'minimax');
  assert.equal(outputs.anthropic_api_key_mode, 'minimax');
  assert.equal(outputs.effective_model, 'MiniMax-M3.1-Flash');
  assert.match(written, /api\.minimax\.io\/anthropic/);
});

test('openrouter route keeps the vendor/model slug and clears anthropic api key', () => {
  const { outputs, written } = run({
    VAR_BASE: 'https://openrouter.ai/api',
    VAR_MODEL: 'deepseek/deepseek-chat-v3.1:free',
    DEFAULT_MODEL: 'MiniMax-M3.1-Flash',
  });
  assert.equal(outputs.route, 'openrouter');
  assert.equal(outputs.anthropic_api_key_mode, 'none');
  assert.equal(outputs.effective_model, 'deepseek/deepseek-chat-v3.1:free');
  assert.match(written, /^ANTHROPIC_BASE_URL=https:\/\/openrouter\.ai\/api$/m);
  assert.match(written, /^ANTHROPIC_MODEL=deepseek\/deepseek-chat-v3\.1:free$/m);
  assert.match(written, /^ANTHROPIC_API_KEY=$/m);
});

test('openrouter route refuses a missing or malformed slug', () => {
  for (const model of ['', 'no-slash', 'a/b\nANTHROPIC_BASE_URL=https://evil.example', 'vendor/model with space', '/model']) {
    assert.throws(
      () => run({ VAR_BASE: 'https://openrouter.ai/api', VAR_MODEL: model, DEFAULT_MODEL: 'MiniMax-M3.1-Flash' }),
      /Command failed/,
      JSON.stringify(model),
    );
  }
});

test('the openrouter base must match exactly', () => {
  assert.throws(
    () => run({ VAR_BASE: 'https://openrouter.ai/api/v1', VAR_MODEL: 'a/b', DEFAULT_MODEL: 'MiniMax-M3.1-Flash' }),
    /Command failed/,
  );
});
