import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const script = path.join(path.dirname(new URL(import.meta.url).pathname), '..', 'report-autofix-failure.sh');

test('surfaces provider result text without secrets', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'autofix-report-'));
  const json = path.join(temp, 'claude-execution-output.json');
  fs.writeFileSync(
    json,
    JSON.stringify([
      { type: 'result', subtype: 'success', is_error: true, duration_ms: 510, result: 'Invalid model: muse-spark-1.3-contributor' },
    ]),
  );
  const out = execFileSync('bash', [script], {
    env: {
      AUTOFIX_EXECUTION_JSON: json,
      CLAUDE_STEP_OUTCOME: 'failure',
      AUTOFIX_ROUTE: 'meta',
      AUTOFIX_EFFECTIVE_MODEL: 'muse-spark-1.3',
      AUTOFIX_BASE_URL: 'https://api.meta.ai',
    },
    encoding: 'utf8',
  });
  assert.match(out, /Invalid model/);
  assert.match(out, /::error::/);
  assert.doesNotMatch(out, /Bearer sk-/);
});
