'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const {clientHelloSchema}=require('./native_source_schemas.cjs');
class Emitter{listeners=new Set();event=fn=>{this.listeners.add(fn);return {dispose:()=>this.listeners.delete(fn)}};fire(v){for(const fn of this.listeners)fn(v)}dispose(){this.listeners.clear()}}
const ctx={Emitter,TextEncoder,clientHelloSchema,clientSupportsWorkflowRunDeltas:x=>x.capabilities?.workflowRunDeltas===true,conversationTopic:s=>'conversation/'+s,createHello:()=>({kind:'hello'}),Event:{None:()=>({dispose(){}})}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/installed_connection_scope.js','utf8'),ctx);
let count=0;
async function one(mode){
 const upstream=new Emitter(),lifecycle=new Emitter(),unsubs=[],flows=[],sent=[],target={workspacePath:'/fixture',sessionId:'sess-fixture'};
 let finish,reject;
 const base={onAgentRuntimeLifecycle:lifecycle.event,onDynamicConversationFrame:()=>upstream.event,setConnectionFlowStateV4:async p=>{flows.push(p.state);if(mode==='flow-fails'&&p.state==='saturated')throw Error('fixture-flow')},subscribeConversationV4:()=>new Promise((a,b)=>{finish=a;reject=b}),unsubscribeConversationV4:async p=>{unsubs.push(p.subscriptionId);if(mode==='unsubscribe-fails')throw Error('fixture-unsubscribe')},sendConversationCommandV4:async p=>{sent.push(p);return {}}};
 const scope=ctx.makeScope(base,{connectionId:'own-fixture-connection',clientMode:'web-remote-replayable'});
 await scope.service.helloConversationV4();await scope.service.initializeConversationV4({kind:'clientHello',protocolVersion:3,clientId:'echo-review-alerts',clientKind:'web',appVersion:'3.19.0'});
 const received=[];const listener=scope.service.onDynamicConversationFrame(target)(f=>received.push(f));
 if(mode==='flow-fails')await scope.setTransportFlowState('saturated');
 const pending=scope.service.subscribeConversationV4(target);pending.catch(()=>{});
 const wire={topic:'conversation/sess-fixture',subscriptionId:'own-fixture',kind:'complete'};
 upstream.fire(wire);
 if(['dispose-before-ack','ack-never-returns'].includes(mode))await scope.dispose();
 if(mode==='runtime-restart')lifecycle.fire({state:'unavailable',workspaceKey:'/fixture',runtimeIdentity:{generation:2}});
 if(mode==='ack-never-returns'){
  assert.equal(upstream.listeners.size,0);assert.equal(unsubs.length,0);assert.equal(lifecycle.listeners.size,0);
  // No runtime-assigned subscription ID exists to cancel; unresolved base call.
  reject(Error('fixture-base-disconnected'));await assert.rejects(pending);
 }else{
  finish({ack:{subscriptionId:'own-fixture',mode:'snapshot',logEpoch:'epoch'}});
  if(['dispose-before-ack','runtime-restart','flow-fails'].includes(mode))await assert.rejects(pending);else await pending;
  if(mode==='wrong-unsubscribe'){
   await scope.service.unsubscribeConversationV4({...target,subscriptionId:'other'});assert.equal(unsubs.length,0);
  }
  if(mode==='cross-workspace'){
   await scope.service.unsubscribeConversationV4({...target,workspacePath:'/other',subscriptionId:'own-fixture'});assert.equal(unsubs.length,0);
  }
  if(mode==='explicit-unsubscribe')await scope.service.unsubscribeConversationV4({...target,subscriptionId:'own-fixture'});
  if(mode==='other-connection'){
   const sibling=ctx.makeScope(base,{connectionId:'sibling',clientMode:'web-remote-replayable'});
   await sibling.service.unsubscribeConversationV4({...target,subscriptionId:'own-fixture'});await sibling.dispose();assert.equal(unsubs.length,0);
  }
  await scope.dispose();await scope.dispose();
  if(mode!=='runtime-restart')assert.deepEqual(unsubs,['own-fixture']);
  else assert.equal(unsubs.length,0); // restart invalidated old generation, never cancels a new route
 }
 listener.dispose();assert.equal(upstream.listeners.size,0);assert.equal(lifecycle.listeners.size,0);assert.equal(sent.length,0);
 if(mode==='dispose-before-ack')assert.equal(received.length,0);
 console.log('PASS installed connection scope',mode);count++;
}
(async()=>{for(const mode of ['normal-dispose','dispose-before-ack','ack-never-returns','runtime-restart','explicit-unsubscribe','wrong-unsubscribe','cross-workspace','other-connection','unsubscribe-fails','flow-fails'])await one(mode);console.log('TOTAL',count,'exact installed scope fixtures. Transport/process teardown hook itself is not exercised.')})().catch(e=>{console.error(e);process.exitCode=1});
