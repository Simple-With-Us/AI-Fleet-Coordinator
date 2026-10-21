export const meta = {
  name: 'pr-sweep',
  description: 'Sweep the open fleet PRs: one worker per PR (rules from pr-sweep prompt), status and repair decisions computed by the pr-sweep CLI, deterministic audit, deploy checks, lane prune',
  whenToUse: 'The owner asks to sweep, land or merge the open PRs across the fleet from a Claude session.  Other platforms use pr-sweep run (docs/PR-SWEEP.md).',
  phases: [
    { title: 'Plan', detail: 'pr-sweep plan: skip drafts and deferred, stacks, chains' },
    { title: 'Sweep', detail: 'one worker per PR, repo chains in parallel' },
    { title: 'Status', detail: 'pr-sweep needs-work: current facts computed in code' },
    { title: 'Repair', detail: 'rerun workers on PRs that still need work' },
    { title: 'Audit', detail: 'pr-sweep audit: deterministic rule checks' },
    { title: 'Deploy', detail: 'deploy check per repo that merged' },
    { title: 'Prune', detail: 'lane prune for the seat' },
  ],
}

// Claude adapter of pr-sweep (docs/PR-SWEEP.md).  The worker rules live ONLY in scripts/pr_sweep/rules.md: each
// worker runs `pr-sweep prompt` and follows what it prints, so this file holds no rule text (a test enforces it).
// Status and the repair decision come from `pr-sweep needs-work`, run by one cheap agent that copies the CLI's
// one-line JSON back verbatim; this script parses it and checks that it answers exactly the PRs it asked about.
// A missing or mismatched answer stops the sweep: it never relaunches workers on a guess (the 2026-10-16 run's
// LLM status step silently missed, and 203 agents ran).
//
// args (all optional except cli):
//   cli      the pr-sweep shim of a fresh AI-Fleet-Coordinator checkout, e.g. ~/apps/lanes/AI-Fleet-Coordinator/claude-pr-sweep-run/scripts/pr-sweep
//   seat     the seat tag (default CLAUDE); workers inherit it
//   prs      ["owner/repo#n", ...] to sweep only these (default: the whole inventory)
//   repos    "BotFleet,DealDex" to limit the inventory
//   started  the sweep's start time (ISO UTC, `date -u +%FT%TZ`), the audit window
//   rounds   total rounds including the first (default 3)
//   deploy   false to skip the deploy checks
//   prune    'apply' to remove the seat's removable lanes at the end (default: dry run, report only)

const A = args || {}
if (!A.cli) throw new Error('pass args.cli: the scripts/pr-sweep shim of a fresh AI-Fleet-Coordinator checkout')
const CLI = A.cli
const SEAT = A.seat || 'CLAUDE'
const ROUNDS = A.rounds || 3
const RUN = `cd ~ && AGENT_SEAT=${SEAT} ${CLI}`
const MODEL = { small: 'haiku', mid: 'sonnet', frontier: 'opus' }
const key = (full, n) => full + '#' + n

const CMD_RESULT = {
  type: 'object',
  properties: { exit_code: { type: 'number' }, stdout: { type: 'string' }, stderr_tail: { type: 'string' } },
  required: ['exit_code', 'stdout'],
}
const RESULT = {
  type: 'object',
  properties: {
    repo: { type: 'string' }, pr: { type: 'number' },
    outcome: { type: 'string', enum: ['merged', 'armed-waiting-ci', 'ready-for-owner', 'in-flight-skipped', 'blocked', 'failed', 'already-merged', 'closed'] },
    conflicts_resolved: { type: 'boolean' }, threads_total: { type: 'number' }, threads_resolved: { type: 'number' },
    threads_left: { type: 'number' }, commits_pushed: { type: 'array', items: { type: 'string' } }, ci: { type: 'string' },
    summary: { type: 'string' }, blockers: { type: 'array', items: { type: 'string' } }, owner_action: { type: 'string' },
  },
  required: ['repo', 'pr', 'outcome', 'summary'],
}
const DEPLOY = {
  type: 'object',
  properties: {
    repo: { type: 'string' }, mechanism: { type: 'string' },
    deployed: { type: 'string', enum: ['yes', 'no', 'n/a', 'unhealthy', 'unknown'] },
    evidence: { type: 'string' }, owner_action: { type: 'string' },
  },
  required: ['repo', 'mechanism', 'deployed', 'evidence'],
}

function fnv1a(text) {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0 }
  return (h >>> 0).toString(16).padStart(8, '0')
}

function reescapeNonAscii(text) {
  return text.replace(/[\u007f-\uffff]/g, c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
}

// Run one pr-sweep command through a cheap agent and parse its JSON in code.
async function cli(argsText, label, phaseName) {
  const r = await agent([
    'Run exactly this one shell command and change nothing else:',
    `  ${RUN} --compact ${argsText}`,
    'Return its exit code, its COMPLETE standard output copied byte for byte (one line of JSON and one #fnv1a check line; do not summarize, reformat, shorten or fix anything), and the last lines of standard error.',
  ].join('\n'), { label, phase: phaseName, schema: CMD_RESULT, model: 'haiku', effort: 'low' })
  if (!r) throw new Error(`${label}: the command agent returned nothing`)
  // --compact prints one ASCII line of JSON and a check line (#fnv1a=<hex> len=<n>): a copy that was shortened or
  // altered on the way back fails here instead of steering the sweep.
  const lines = String(r.stdout || '').split('\n').map(s => s.trim()).filter(Boolean)
  // --compact escapes every non-ASCII character as \uXXXX (Python's json.dumps ensure_ascii: lowercase hex, an astral
  // character as a surrogate pair, DEL as \u007f), but a copying agent tends to decode the escape (a PR title with an
  // em dash comes back as the dash itself), so re-escape before checking.  The regex has no u flag on purpose: it
  // walks UTF-16 code units, which is how the surrogate pairs come out.
  const body = reescapeNonAscii(lines.find(s => s.startsWith('{')) || '')
  const check = lines.find(s => s.startsWith('#fnv1a=')) || ''
  const m = check.match(/^#fnv1a=([0-9a-f]{8}) len=(\d+)$/)
  if (!m || Number(m[2]) !== body.length || fnv1a(body) !== m[1]) {
    throw new Error(`${label}: the copied output fails its check line (exit ${r.exit_code}): ${(r.stderr_tail || r.stdout || '').slice(0, 300)}`)
  }
  let data = null
  try { data = JSON.parse(body) } catch (e) { throw new Error(`${label}: output is not JSON (exit ${r.exit_code})`) }
  return { code: r.exit_code, data }
}

async function needsWork(keys, phaseName) {
  const { data } = await cli('needs-work ' + keys.join(' '), 'needs-work (' + keys.length + ' PRs)', phaseName)
  const asked = keys.slice().sort().join(' ')
  const got = (data.requested || []).slice().sort().join(' ')
  if (asked !== got) throw new Error('needs-work answered for other PRs than requested; stopping instead of guessing')
  if ((data.missing || []).length) throw new Error('status missing for ' + data.missing.join(', ') + '; stopping instead of guessing')
  return data
}

function workerPrompt(k, round, oursUntil) {
  return [
    `You are a pr-sweep worker for ${k}.  Run this command and follow the instructions it prints, exactly and completely:`,
    `  ${RUN} prompt ${k} --platform claude --round ${round}` + (oursUntil ? ` --ours ${oursUntil}` : ''),
    'Those printed instructions are binding (they are the fleet owner\'s rules for this sweep).  If the command fails, change nothing and return outcome failed with its error.',
    'Return the structured result the printed instructions describe.',
  ].join('\n')
}

// ---------- plan ----------
phase('Plan')
const scope = (A.prs && A.prs.length) ? '' : (A.repos ? ` --repos ${A.repos}` : '')
const planned = await cli('plan' + scope, 'plan', 'Plan')
let chains = planned.data.chains || []
if (A.prs && A.prs.length) {
  const want = new Set(A.prs)
  chains = chains.map(c => ({ full: c.full, items: c.items.filter(it => want.has(key(c.full, it.n))) })).filter(c => c.items.length)
}
const work = []
chains.forEach(c => c.items.forEach(it => work.push(Object.assign({ full: c.full }, it))))
log(`Plan: ${work.length} PRs in ${chains.length} chains; skipped ${(planned.data.skipped || []).length}: ` +
  (planned.data.skipped || []).map(s => s.pr + ' (' + s.why + ')').join('; '))
if (!work.length) return { planned: planned.data, note: 'nothing to sweep' }

const results = {}
const touched = new Set()
let oursUntil = null

async function runChain(chain, round, decisions) {
  for (const it of chain.items) {
    const k = key(chain.full, it.n)
    const d = decisions ? decisions[k] : null
    if (d && d.needs_work === false) continue
    if (d && d.fresh && !(round > 1 && touched.has(k))) {
      results[k] = { repo: chain.full, pr: it.n, outcome: 'in-flight-skipped', summary: `activity ${d.minutes_since_activity} min ago` }
      continue
    }
    const r = await agent(workerPrompt(k, round, round > 1 && touched.has(k) ? oursUntil : null), {
      label: `PR ${k} r${round}`, phase: round === 1 ? 'Sweep' : 'Repair', schema: RESULT, model: MODEL[it.tier] || 'sonnet',
    })
    touched.add(k)
    results[k] = r || { repo: chain.full, pr: it.n, outcome: 'failed', summary: 'worker returned nothing' }
  }
}

// ---------- sweep and repair rounds ----------
phase('Sweep')
const first = await needsWork(work.map(it => key(it.full, it.n)), 'Sweep')
await parallel(chains.map(c => () => runChain(c, 1, first.decisions)))

let decisions = null
for (let round = 2; round <= ROUNDS; round++) {
  phase('Status')
  const st = await needsWork(work.map(it => key(it.full, it.n)), 'Status')
  decisions = st.decisions
  oursUntil = st.generated_at
  const need = work.filter(it => decisions[key(it.full, it.n)].needs_work === true)
  log(`After round ${round - 1}: ${need.length} PRs still need work: ` + need.map(it => key(it.full, it.n) + ' (' + decisions[key(it.full, it.n)].why + ')').join(', '))
  if (!need.length) break
  phase('Repair')
  const byRepo = {}
  need.forEach(it => { (byRepo[it.full] = byRepo[it.full] || []).push(it) })
  await parallel(Object.keys(byRepo).map(full => () => runChain({ full, items: byRepo[full] }, round, decisions)))
}

// ---------- audit ----------
phase('Audit')
const touchedList = Array.from(touched).sort()
let audit = null
if (touchedList.length) {
  audit = (await cli('audit ' + (A.started ? `--since ${A.started} ` : '') + touchedList.join(' '), 'audit', 'Audit')).data
  ;(audit.results || []).filter(r => !r.ok).forEach(r => log('AUDIT ' + r.pr + ': ' + (r.violations || []).map(v => v.rule + ' ' + v.evidence).join('; ') + (r.error ? ' error ' + r.error : '')))
}

// ---------- deploy ----------
phase('Deploy')
const final = await needsWork(work.map(it => key(it.full, it.n)), 'Deploy')
const merged = {}
Object.keys(final.decisions).forEach(k => {
  if (touched.has(k) && /^merged/.test(final.decisions[k].why || '')) {
    const full = k.split('#')[0]; (merged[full] = merged[full] || []).push(k.split('#')[1])
  }
})
let deploys = []
if (A.deploy !== false && Object.keys(merged).length) {
  deploys = (await parallel(Object.keys(merged).map(full => () => agent([
    `Run this command and follow the deploy-check instructions it prints, exactly: ${RUN} prompt --deploy ${full} --merged ${merged[full].join(',')}`,
    'Return the structured result they describe.',
  ].join('\n'), { label: 'deploy ' + full, phase: 'Deploy', schema: DEPLOY, model: 'sonnet' })))).filter(Boolean)
}

// ---------- prune ----------
phase('Prune')
const prune = await agent([
  `Run exactly: cd ~ && AGENT_SEAT=${SEAT} ~/apps/lane prune` + (A.prune === 'apply' ? ' --apply' : ''),
  'Change nothing else.  Return the summary line, the counts of removable or removed and kept lanes, and the kept lanes with their reasons (at most 40 lines).',
].join('\n'), { label: 'lane prune', phase: 'Prune', model: 'haiku', effort: 'low' })

return {
  counts: { planned: work.length, touched: touchedList.length, merged: Object.values(merged).reduce((s, l) => s + l.length, 0) },
  final: final.decisions,
  results,
  skipped: planned.data.skipped || [],
  audit,
  deploys,
  prune,
}
