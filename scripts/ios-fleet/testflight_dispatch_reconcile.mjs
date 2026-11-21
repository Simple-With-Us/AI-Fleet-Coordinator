// Administrative reconciliation only. No POST, retry, cancel, permission or key change.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { makeDeps } from './ios_testflight_freshness.mjs';
export async function inspectIntent(intent, gh) {
  if (!intent?.intentId || !intent.repo || !intent.workflow || !intent.ref) throw Error('incomplete-dispatch-intent');
  // Only an ID actually returned by dispatch can support automatic exact attribution.
  if (!intent.runId) return {state:'reconciliation-required', intent, reason:'No dispatch-specific run ID received. Lists, timing and matching SHA cannot prove attribution. Owner disposition required; no retry.'};
  const run = await gh(`repos/${intent.repo}/actions/runs/${intent.runId}`);
  if (run.id !== intent.runId || run.repository?.full_name !== intent.repo || run.event !== 'workflow_dispatch' || run.head_branch !== intent.ref || run.head_sha !== intent.headSha || run.path?.split('@')[0] !== `.github/workflows/${intent.workflow}`) throw Error('dispatch-run-evidence-mismatch');
  return {state:run.status==='completed'?'completed':'active',intent,run:{id:run.id,html_url:run.html_url,status:run.status,conclusion:run.conclusion,head_sha:run.head_sha,event:run.event,path:run.path}};
}
export function recordDisposition(state, {app, intentId, disposition, evidenceRef, ownerApprovalRef}, now = new Date()) {
  const intent = state.apps?.[app]?.remedy;
  if (typeof intentId !== 'string' || !intentId.trim() || typeof intent?.intentId !== 'string' || !intent.intentId.trim() || intent.intentId !== intentId || intent.dispatchOutcome !== 'unknown') throw Error('stale-or-non-unknown-intent');
  if (!['confirmed-rejected','confirmed-completed'].includes(disposition) || typeof evidenceRef !== 'string' || !evidenceRef.trim() || typeof ownerApprovalRef !== 'string' || !ownerApprovalRef.trim()) throw Error('explicit-evidence-and-owner-disposition-required');
  // References do not authenticate themselves. The operator must recover and verify
  // actual owner authority before this administrative mutation. No standing grant.
  state.dispatchResolutions ||= [];
  state.dispatchResolutions.push({app,intent:{...intent},disposition,evidenceRef,ownerApprovalRef,at:now.toISOString()});
  if (disposition === 'confirmed-rejected') delete state.apps[app].remedy;
  else { intent.dispatchOutcome='accepted'; intent.reconciledCompletedAt=now.toISOString(); }
  return state;
}
export async function reconcileMain(args, deps = makeDeps()) {
  if (args.length !== 2 || !['inspect','record'].includes(args[0])) throw Error('usage: testflight_dispatch_reconcile.mjs inspect APP | record APPROVED_DECISION_FILE');
  const unlock=deps.lock(); if(!unlock)throw Error('freshness-lock-held');
  try {
    const state=deps.readState();
    if(args[0]==='inspect') return await inspectIntent(state.apps?.[args[1]]?.remedy,deps.gh);
    const decision=JSON.parse(fs.readFileSync(args[1],'utf8'));
    recordDisposition(state,decision);deps.writeState(state);
    return {recorded:true,app:decision.app,intentId:decision.intentId,disposition:decision.disposition};
  } finally {unlock();}
}
if(process.argv[1] && import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href) reconcileMain(process.argv.slice(2)).then(r=>console.log(JSON.stringify(r,null,2))).catch(e=>{console.error(e.message);process.exitCode=2;});
