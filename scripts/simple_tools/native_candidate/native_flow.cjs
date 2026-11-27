'use strict';
// Candidate native route. No endpoint discovery, token read, or activation.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const provenance=JSON.parse(fs.readFileSync(path.join(__dirname,'schema-provenance.json'),'utf8'));
if(provenance.source_sha256!=='ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0'||crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'native_source_schemas.cjs'))).digest('hex')!==provenance.generated_sha256)throw Error('candidate-schema-provenance');
const z=require('./offline_node_modules/zod');
const restricted=require('./native_schemas.cjs').schemas(z);
const {conversationTopicFrameSchema,createTopicWireFrameSchema,helloMessageSchema2,clientHelloSchema,v4ConversationSubscribeResultSchema,commandAckSchema,commandsQueryResultSchema}=require('./native_source_schemas.cjs');
const {TopicWireFrameAssembler,PROTOCOL_V4_LIMITS}=require('./native_assembler.cjs');
const wireSchema=createTopicWireFrameSchema(conversationTopicFrameSchema);
function bindingParams(b,routing){restricted.binding.parse(b);const r=restricted.routing.parse(routing||{workspace_identity:null});return {workspacePath:b.workspace,...r.workspace_identity?{workspaceIdentity:r.workspace_identity}:{},sessionId:b.session_id}}
async function flow(request,rpc,verify){
 if(Object.keys(request).some(k=>!['action','binding','command_id','client_id','text','routing'].includes(k)))throw Error('request-fields-refused');
 const b=request.binding,p=bindingParams(b,request.routing),workspace={workspacePath:p.workspacePath,...p.workspaceIdentity?{workspaceIdentity:p.workspaceIdentity}:{}},clientId='review-alerts-router';
 if(request.client_id!==clientId||!/^app-control-review-alerts-[a-f0-9]{64}$/.test(request.command_id))throw Error('command-key-invalid');
 if(!await verify(b))throw Error('binding-changed');
 const hello=helloMessageSchema2.parse(await rpc.call('helloConversationV4',[]));
 if(hello?.kind!=='hello'||hello.protocolVersion!==3||hello.clientMode!=='web-remote-replayable')throw Error('hello-incompatible');
 const initialized=await rpc.call('initializeConversationV4',[clientHelloSchema.parse({kind:'clientHello',protocolVersion:3,clientId,clientKind:'web',appVersion:'3.19.0'})]);
 if(initialized!==undefined)throw Error('initialize-result-invalid');
 if(!await verify(b))throw Error('binding-changed');
 if(request.action==='query'){
  const q=restricted.query.parse(commandsQueryResultSchema.parse(await rpc.call('queryConversationCommandsV4',[{...workspace,commands:[{sessionId:b.session_id,commandId:request.command_id}]}])));
  const row=q.results[0];
  if(row.key.sessionId!==b.session_id||row.key.commandId!==request.command_id||row.result!=='unknown'&&row.result.commandId!==request.command_id)throw Error('query-key-mismatch');
  return {ack:row.result==='unknown'?null:row.result};
 }
 if(request.action!=='dispatch')throw Error('dispatch-invalid');
 // Construct and validate the only effect this route permits before subscribing.
 const envelope=restricted.envelope.parse({commandId:request.command_id,clientId,sessionId:b.session_id,type:'sendText',payload:{text:request.text,requestedDelivery:'queue'},issuedAt:Date.now()});
 let subscription,disposeEvents,readyResolve,readyReject,readyTimer,expiryTimer,epoch,seq,currentInitialization=null,frameError=null,pending=[],pendingBytes=0;
 const assembler=new TopicWireFrameAssembler(conversationTopicFrameSchema);
 const readiness=new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject});readiness.catch(()=>{});
 function fail(e){frameError=e;currentInitialization=null;readyReject(e)}
 function armExpiry(){clearTimeout(expiryTimer);if(assembler.nextExpiryAt!==null)expiryTimer=setTimeout(()=>{const events=assembler.expire();if(events.some(e=>e.kind==='fault'))fail(Error('frame-assembly-expired'));armExpiry()},Math.max(1,assembler.nextExpiryAt-Date.now()))}
 function acceptFrame(f){
  if(f.topic!=='conversation/'+b.session_id||f.subscriptionId!==subscription||!Number.isSafeInteger(f.fromSeq)||!Number.isSafeInteger(f.toSeq)||f.fromSeq<0||f.toSeq<0)throw Error('frame-binding');
  let initialization;
  if(f.payload.kind==='snapshot'){
   const s=f.payload.snapshot;
   if(s.sessionId!==b.session_id||s.logEpoch!==epoch||s.seq!==f.toSeq||f.fromSeq!==0||seq!==undefined&&s.seq<=seq)throw Error('snapshot-binding-or-regression');
   // Replacement means replacement, absence cannot inherit a prior ready latch.
   seq=s.seq;currentInitialization=null;initialization=s.runtimeInitialization;
  }else{
   if(seq===undefined||f.fromSeq!==seq||f.toSeq<=seq||f.toSeq-f.fromSeq!==f.payload.deltas.length)throw Error('delta-sequence');
   seq=f.toSeq;
   for(const d of f.payload.deltas)if(d.op==='state.updated'&&Object.hasOwn(d.patch,'runtimeInitialization'))initialization=d.patch.runtimeInitialization;
  }
  if(initialization!==undefined)currentInitialization=restricted.initialization.parse(initialization);
  if(currentInitialization?.status==='failed')throw Error('session-not-ready');
  if(currentInitialization?.status==='ready')readyResolve();
 }
 function accept(wire){
  if(frameError)throw frameError;
  if(!subscription){const n=Buffer.byteLength(JSON.stringify(wire));pendingBytes+=n;if(pending.length>=PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxFragments||pendingBytes>PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxStagedBytes)throw Error('early-frame-budget');pending.push(wire);return}
  // Validate wire before the assembler can allocate or retain any route state.
  wireSchema.parse(wire);
  if(wire.topic!=='conversation/'+b.session_id||wire.subscriptionId!==subscription)throw Error('snapshot-wire-binding');
  const events=assembler.accept(wire);armExpiry();
  for(const event of events){if(event.kind==='fault')throw Error('frame-assembly-refused');acceptFrame(event.frame)}
 }
 try{
  disposeEvents=await rpc.listen('onDynamicConversationFrame',workspace,wire=>{try{accept(wire)}catch(e){fail(e);throw e}});
  readyTimer=setTimeout(()=>fail(Error('readiness-timeout')),10000);
  const opened=v4ConversationSubscribeResultSchema.parse(await rpc.call('subscribeConversationV4',[p]));
  const candidate=opened?.ack?.subscriptionId;
  if(typeof candidate!=='string'||!candidate||candidate.length>1024||opened.ack.mode!=='snapshot'||typeof opened.ack.logEpoch!=='string'||!opened.ack.logEpoch||Object.keys(opened).some(k=>k!=='ack')||Object.keys(opened.ack).some(k=>!['subscriptionId','mode','logEpoch','openTiming'].includes(k)))throw Error('subscription-invalid');
  subscription=candidate;epoch=opened.ack.logEpoch;for(const wire of pending)accept(wire);pending=[];pendingBytes=0;
  try{await readiness}catch(error){error.nativeStage='readiness';throw error}clearTimeout(readyTimer);
  const readinessAttempt=currentInitialization?.attemptId;
  if(!await verify(b))throw Error('binding-changed');
  if(frameError||currentInitialization?.status!=='ready'||currentInitialization.attemptId!==readinessAttempt||assembler.getStats().assemblies!==0)throw Error('readiness-changed');
  const ack=restricted.ack.parse(commandAckSchema.parse(await rpc.call('sendConversationCommandV4',[{...workspace,envelope}])));
  if(ack.commandId!==request.command_id)throw Error('ack-key-mismatch');
  return {ack};
 }catch(error){rpc.capture?.(error);throw error}
 finally{
  clearTimeout(readyTimer);clearTimeout(expiryTimer);assembler.clear();
  if(subscription){try{await rpc.call('unsubscribeConversationV4',[{...p,subscriptionId:subscription}])}catch{}}
  if(disposeEvents)disposeEvents();
 }
}
async function run(request,rpc,verify){
 let stage='validate';
 const tracked={...rpc,capture:error=>{if(!error.nativeStage)error.nativeStage=stage},call:async(name,args)=>{stage=name;try{return await rpc.call(name,args)}catch(error){error.nativeStage=name;throw error}},listen:async(...args)=>{stage='listen';return rpc.listen(...args)}};
 try{return await flow(request,tracked,async b=>{stage='verify';return verify(b)})}
 catch(error){if(!error.nativeStage)error.nativeStage=stage;throw error}
 finally{await rpc.close()}
}
module.exports={run,bindingParams};
