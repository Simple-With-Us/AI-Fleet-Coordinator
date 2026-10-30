// INACTIVE v1 candidate. Not imported by hosted-tools, index or tool registry.
// Fixed action names only. No raw token, query, URL or arbitrary path input.
export const TYPED_V1_ACTIONS = Object.freeze(['pr_state', 'pr_hold_state', 'pr_automerge_disable']);
const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const HEAD = /^[0-9a-f]{40}$/;
export function validTypedV1Args(args) {
  return !!args && typeof args === 'object' && !Array.isArray(args) &&
    Object.keys(args).sort().join(',') === 'expected_head,number,repo' &&
    typeof args.repo === 'string' && REPO.test(args.repo) &&
    Number.isInteger(args.number) && args.number > 0 && args.number < 2 ** 31 &&
    typeof args.expected_head === 'string' && HEAD.test(args.expected_head);
}
const exactKeys = (obj, keys) => obj && typeof obj === 'object' && !Array.isArray(obj) &&
  Object.keys(obj).sort().join(',') === [...keys].sort().join(',');
const text = (value, max) => typeof value === 'string' && new TextEncoder().encode(value).length <= max;
export function validTypedV1Receipt(action, args, body) {
  const keys = ['repo','number','head','pr_id','state','auto_merge_enabled','labels','label_events','complete','hold_decision','principal'];
  const disable = action === 'pr_automerge_disable';
  if (disable) keys.push('outcome','write_attempted','atomic_head_guard');
  if (!exactKeys(body,keys) || body.repo !== args.repo || body.number !== args.number || body.head !== args.expected_head ||
      typeof body.pr_id !== 'string' || !/^PR_[A-Za-z0-9_-]{1,160}$/.test(body.pr_id) ||
      !['OPEN','CLOSED','MERGED'].includes(body.state) || typeof body.auto_merge_enabled !== 'boolean' ||
      typeof body.complete !== 'boolean' || body.hold_decision !== 'not_evaluated' ||
      typeof body.principal !== 'string' || !/^swu-[a-z0-9-]+\[bot\]$/.test(body.principal) ||
      !Array.isArray(body.labels) || body.labels.length > 50 || !body.labels.every(x => text(x,100)) ||
      !Array.isArray(body.label_events) || body.label_events.length > 50) return false;
  if (!body.label_events.every(e => exactKeys(e,['id','createdAt','__typename','actor','label']) &&
      ['LabeledEvent','UnlabeledEvent'].includes(e.__typename) &&
      [e.id,e.createdAt,e.label].every(x => text(x,160)) && (e.actor === null || text(e.actor,160)))) return false;
  return !disable || (body.state === 'OPEN' && body.outcome === 'confirmed_disabled' && body.auto_merge_enabled === false &&
    typeof body.write_attempted === 'boolean' && body.atomic_head_guard === false);
}
export const TYPED_V1_TOOLS = Object.freeze(TYPED_V1_ACTIONS.map(action => ({
  name: `gr_v1_${action}`, description: 'Inactive typed v1 proposal. Own App, exact head; not an arm or hold-clear operation.',
  inputSchema: {type:'object', additionalProperties:false, required:['repo','number','expected_head'], properties:{
    repo:{type:'string',pattern:REPO.source}, number:{type:'integer',minimum:1,maximum:2147483647},
    expected_head:{type:'string',pattern:HEAD.source}}}
})));

import { ASSERTION_FAMILY_RE } from "./gr-assert.js";

export async function callTypedV1({action,args,baseUrl,bearer,minter=null,accessId,accessSecret,fetchImpl=fetch,expectedPrincipal}) {
  // Service credentials supplied only by Worker-owned resolver, never tool args.
  if (!TYPED_V1_ACTIONS.includes(action) || !validTypedV1Args(args)) return {status:400,body:{error:'invalid_typed_request'}};
  let url;
  try {url = new URL(baseUrl);} catch {return {status:503,body:{error:'not_configured'}};}
  if (url.protocol !== 'https:' || url.pathname !== '/' || url.search || url.hash || url.username || url.password ||
      (!bearer && typeof minter !== "function") || !accessId || !accessSecret || !/^swu-[a-z0-9-]+\[bot\]$/.test(expectedPrincipal)) return {status:503,body:{error:'not_configured'}};
  const body = JSON.stringify(args);
  if (!bearer) {
    const minted = await minter({action, body});
    if (typeof minted !== "string" || !minted) return {status:503,body:{error:'not_configured'}};
    bearer = minted;
  }
  const unknown = () => ({status:502,body:{error:'typed_upstream_unavailable',outcome:'unknown',retryable:false}});
  try {
    // 110s covers preflight/write/readback, each existing broker call max30s.
    const response = await fetchImpl(`${url.origin}/v1/${action}`, {method:'POST',redirect:'manual',signal:AbortSignal.timeout(110000),
      headers:{Authorization:`Bearer ${bearer}`,'CF-Access-Client-Id':accessId,'CF-Access-Client-Secret':accessSecret,'Content-Type':'application/json'},body});
    if (!response.body) return unknown();
    const reader = response.body.getReader(); const chunks=[]; let bytes=0;
    for (;;) {const {done,value}=await reader.read(); if(done) break; bytes+=value.byteLength;
      if(bytes>65536){await reader.cancel();return unknown();} chunks.push(value);}
    const raw = new Uint8Array(bytes); let offset=0; for(const chunk of chunks){raw.set(chunk,offset);offset+=chunk.byteLength;}
    const parsed = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(raw));
    if (response.status !== 200) {
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return unknown();
      if ([401,403].includes(response.status)) return {status:response.status,body:{error:'typed_request_refused',retryable:false}};
      if ([429,503].includes(response.status) && parsed.retryable === true) return {status:response.status,body:{error:'typed_preflight_unavailable',retryable:true}};
      return unknown();
    }
    if (!validTypedV1Receipt(action,args,parsed) || parsed.principal !== expectedPrincipal) return unknown();
    // Secrets must never reflect even from a compromised/misconfigured upstream.
    const encoded = JSON.stringify(parsed);
    // Request-scoped echo guard:  this call's exact credential (bearer or
    // just-minted assertion) plus the Access pair, with the assertion family
    // pattern as the fallback for any other live assertion.
    if ([bearer,accessId,accessSecret].some(secret => encoded.includes(secret)) || ASSERTION_FAMILY_RE.test(encoded)) return unknown();
    return {status:200,body:parsed};
  } catch {return unknown();}
}
