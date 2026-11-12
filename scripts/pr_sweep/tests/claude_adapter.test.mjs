// The Claude adapter (adapters/claude/pr-sweep.workflow.js) run against stubbed Workflow hooks.
//   node --test scripts/pr_sweep/tests/claude_adapter.test.mjs
// No agent runs: `agent` answers the pr-sweep commands with canned JSON and records every worker it is asked for.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'adapters', 'claude', 'pr-sweep.workflow.js'), 'utf8')
  .replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const script = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', source)

const REPO_A = 'Simple-With-Us/DealDex'
const REPO_B = 'Simple-With-Us/BotFleet'
const PLAN = {
  chains: [
    { full: REPO_A, items: [{ n: 1, tier: 'mid' }, { n: 2, tier: 'small' }] },
    { full: REPO_B, items: [{ n: 7, tier: 'frontier' }] },
  ],
  skipped: [{ pr: REPO_B + '#9', why: 'draft' }],
}

// What `pr-sweep --compact` prints: one ASCII line of JSON, then its FNV-1a check line.
function fnv1a(text) {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0 }
  return (h >>> 0).toString(16).padStart(8, '0')
}
function compact(obj) {
  const text = JSON.stringify(obj).replace(/[\u007f-\uffff]/g, c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
  return text + '\n#fnv1a=' + fnv1a(text) + ' len=' + text.length + '\n'
}

// rounds: an array of {key: [needs_work, why]} maps, one per needs-work call, in order
function world(rounds, opts = {}) {
  const calls = { workers: [], deploys: [], needsWork: 0, audit: null, models: {} }
  const agent = async (prompt, o = {}) => {
    const props = (o.schema && o.schema.properties) || {}
    if (props.stdout) {
      const cmd = prompt.split('\n')[1]
      if (/ --compact plan/.test(cmd)) {
        const out = compact(opts.plan || PLAN)
        return { exit_code: 0, stdout: opts.tamper ? opts.tamper(out) : out }
      }
      if (/ --compact needs-work /.test(cmd)) {
        const keys = cmd.split(' needs-work ')[1].trim().split(/\s+/)
        const r = rounds[Math.min(calls.needsWork, rounds.length - 1)]
        calls.needsWork++
        const decisions = {}
        keys.forEach(k => {
          const [need, why] = r[k] || [false, 'auto-merge armed']
          decisions[k] = { needs_work: need, why, fresh: false, mode: 'merge', minutes_since_activity: 60 }
        })
        const requested = opts.requested ? opts.requested(keys) : keys
        return { exit_code: 0, stdout: compact({ generated_at: '2026-10-17T18:00:00Z', requested, decisions, missing: opts.missing || [] }) }
      }
      if (/ --compact audit /.test(cmd)) {
        calls.audit = cmd
        return { exit_code: 0, stdout: compact({ ok: true, results: [] }) }
      }
      throw new Error('unexpected command ' + cmd)
    }
    if (props.outcome) {
      const k = prompt.match(/prompt (\S+#\d+)/)[1]
      const round = Number(prompt.match(/--round (\d+)/)[1])
      calls.workers.push(k + ' r' + round)
      calls.models[k] = o.model
      return { repo: k, pr: 0, outcome: 'merged', summary: 'ok' }
    }
    if (props.deployed) {
      calls.deploys.push(prompt.match(/--deploy (\S+)/)[1])
      return { repo: 'x', mechanism: 'm', deployed: 'yes', evidence: 'e' }
    }
    return 'prune summary'
  }
  const parallel = async thunks => Promise.all(thunks.map(t => t().catch(() => null)))
  return { calls, run: a => script(a, agent, parallel, () => {}, () => {}) }
}

test('only PRs that need work get workers, repair rounds rerun only those, deploys only where touched PRs merged', async () => {
  const w = world([
    { [REPO_A + '#1']: [true, 'CI failing'], [REPO_B + '#7']: [true, '2 unresolved thread(s)'] },
    { [REPO_A + '#1']: [true, 'CI failing'], [REPO_B + '#7']: [false, 'merged'] },
    { [REPO_A + '#1']: [false, 'auto-merge armed'], [REPO_B + '#7']: [false, 'merged'] },
    { [REPO_A + '#1']: [false, 'auto-merge armed'], [REPO_B + '#7']: [false, 'merged'] },
  ])
  const res = await w.run({ cli: '/x/pr-sweep', started: '2026-10-17T17:00:00Z' })
  assert.deepEqual(w.calls.workers.sort(), [REPO_A + '#1 r1', REPO_A + '#1 r2', REPO_B + '#7 r1'].sort())
  assert.deepEqual(w.calls.deploys, [REPO_B])
  assert.equal(w.calls.models[REPO_B + '#7'], 'opus')
  assert.match(w.calls.audit, /--since 2026-10-17T17:00:00Z/)
  assert.equal(res.counts.merged, 1)
})

test('a needs-work answer for other PRs than requested stops the sweep', async () => {
  const w = world([{}], { requested: keys => keys.slice(1) })
  await assert.rejects(w.run({ cli: '/x/pr-sweep' }), /other PRs than requested/)
  assert.deepEqual(w.calls.workers, [])
})

test('a missing status stops the sweep before any worker', async () => {
  const w = world([{}], { missing: [REPO_A + '#2'] })
  await assert.rejects(w.run({ cli: '/x/pr-sweep' }), /status missing/)
  assert.deepEqual(w.calls.workers, [])
})

test('args.cli is required', async () => {
  const w = world([{}])
  await assert.rejects(w.run({}), /args.cli/)
})

test('a copy that was altered or shortened on the way back fails its check line', async () => {
  for (const tamper of [t => t.replace('"n":7', '"n":8'), t => t.split('\n')[0], t => t.replace('#fnv1a=', '#nope=')]) {
    const w = world([{}], { tamper })
    await assert.rejects(w.run({ cli: '/x/pr-sweep' }), /check line/)
    assert.deepEqual(w.calls.workers, [])
  }
})

// A copying agent tends to decode the \uXXXX escapes --compact prints (a PR title with an em dash comes back as the
// dash itself), so the adapter re-escapes the copy before it checks it against the line the CLI hashed.
const decodeEscapes = t => t.replace(/\\u([0-9a-f]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
const PLAN_NON_ASCII = {
  ...PLAN,
  skipped: [{ pr: REPO_B + '#9', why: 'draft \u2014 caf\u00e9 \u{1F680} \u007f done' }],
}

test('a copy whose unicode escapes were decoded still passes its check line', async () => {
  const seen = []
  const w = world([{}], { plan: PLAN_NON_ASCII, tamper: t => { const d = decodeEscapes(t); seen.push(d); return d } })
  const res = await w.run({ cli: '/x/pr-sweep' })
  // the stub really handed back the literal characters, not the escapes
  assert.match(seen[0], /\u2014 caf\u00e9 \u{1F680} \u007f/u)
  assert.doesNotMatch(seen[0].split('\n')[0], /\\u/)
  assert.equal(res.counts.merged, 0)
  assert.deepEqual(w.calls.workers.sort(), [])
})

test('a copy with its escapes left alone still passes, and a decoded copy that was also altered still fails', async () => {
  const keep = world([{}], { plan: PLAN_NON_ASCII })
  await keep.run({ cli: '/x/pr-sweep' })
  const bad = world([{}], { plan: PLAN_NON_ASCII, tamper: t => decodeEscapes(t).replace('"n":7', '"n":8') })
  await assert.rejects(bad.run({ cli: '/x/pr-sweep' }), /check line/)
  assert.deepEqual(bad.calls.workers, [])
})
