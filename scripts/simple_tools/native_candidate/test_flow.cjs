'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm'),crypto=require('crypto');
const path=require('path'),ROOT=__dirname,source=JSON.parse(fs.readFileSync(path.join(ROOT,'offline_source.json'),'utf8'));
const MODULES={flow:fs.readFileSync(path.join(ROOT,'native_flow.cjs'),'utf8'),rpc:fs.readFileSync(path.join(ROOT,'native_rpc.cjs'),'utf8')};
const z=require('./offline_node_modules/zod');
const context={z113:z,z121:z,V4_WIRE_PROTOCOL_VERSION:3,timestampSchema2:z.number(),v4ColdResumeWorkspaceSchema:z.unknown().optional(),localTtftContextSchema:z.unknown(),commandTypeSchema:z.enum(['sendText'])};vm.createContext(context);
vm.runInContext(source.hello+';globalThis.hello=clientHelloSchema;',context);
vm.runInContext(source.envelope+';globalThis.envelope=commandEnvelopeSchema;',context);
// Envelope dependencies are explicit, restricted fixtures. Real hello strictness is intact.
function load(code){const c={require,Buffer,console,setTimeout,clearTimeout,module:{exports:{}}};vm.createContext(c);vm.runInContext(code,c);return c.module.exports}
const flow=require('./native_flow.cjs'),rpcModule=load(MODULES.rpc);
const {WebSocket,WebSocketServer}=require('./offline_node_modules/ws');
class B{constructor(b){this.buffer=b;this.byteLength=b.length}slice(a,b){return new B(this.buffer.subarray(a,b))}toString(){return this.buffer.toString('utf8')}readUInt8(n){return this.buffer.readUInt8(n)}writeUInt8(v,n){this.buffer.writeUInt8(v,n)}}
const VSBuffer=Object.assign(B,{alloc:n=>new B(Buffer.alloc(n)),wrap:b=>new B(Buffer.from(b)),fromString:s=>new B(Buffer.from(s)),concat:bs=>new B(Buffer.concat(bs.map(b=>b.buffer)))});
const c={Buffer,VSBuffer};vm.createContext(c);vm.runInContext(source.serialization+';globalThis.encode=(h,b)=>{const w=new BufferWriter();serialize(w,h);serialize(w,b);return w.buffer.buffer};globalThis.decode=b=>{const r=new BufferReader(VSBuffer.wrap(b));return [deserialize(r),deserialize(r)]};',c);
const frame=(codec,h,b)=>{const body=codec.encode(h,b),out=Buffer.alloc(13+body.length);out[0]=1;out.writeUInt32BE(body.length,9);body.copy(out,13);return out};
const parser=require('./native_codec.cjs').parser;
const snapshotFixture=require('./snapshot_fixture.json');
const binding={host:'server',local:true,session_id:'sess-fixture',workspace:'/fixture',generation:1};
let count=0;async function one(mode){
 const calls=[];let boundClient,subscribed=false,sendCount=0,eventId,ordinal=0;

 function emitEvent(ws,wire){
  if(ws.readyState!==WebSocket.OPEN)return;
  if(mode==='fragmented'){
   const bytes=Buffer.from(JSON.stringify(wire.frame)),mid=Math.floor(bytes.length/2),crc=require('./native_assembler.cjs').crc32WireBytes(bytes);
   const {frame:unused,...rest}=wire;
   for(const i of [1,0]){const b=i?bytes.subarray(mid):bytes.subarray(0,mid);ws.send(frame(c,[204,eventId],{...rest,kind:'fragment',fragmentIndex:i,fragmentCount:2,logicalBytes:bytes.length,checksum:{algorithm:'crc32',value:crc},dataBase64:b.toString('base64')}))}
  }else ws.send(frame(c,[204,eventId],wire));
 }
 const wss=new WebSocketServer({host:'127.0.0.1',port:0});await new Promise(r=>wss.once('listening',r));
 wss.on('connection',(ws,req)=>{assert.equal(req.headers.authorization,'Bearer x');ws.send(frame(c,[200]));ws.on('message',parser(c,(h,args)=>{
  const name=h[3],p=args?.[0];if(h[0]===102){eventId=h[1];return}if(h[0]===103)return;calls.push(name);let body;
  if(name==='helloConversationV4')body={kind:'hello',protocolVersion:3,connectionId:'fixture-connection',serverTime:1,capabilities:{nativeDialogs:false,localTerminal:false,binaryFrames:true,compression:'none'},auth:{},deliveryProfile:'replayable',clientMode:mode==='badhello'?'bad':'web-remote-replayable'};
  if(name==='initializeConversationV4'){context.hello.parse(p);boundClient=p.clientId;body=mode==='badinit'?{ok:true}:undefined}
  if(name==='subscribeConversationV4'&&mode==='lostsubscribe')return ws.terminate();
  if(name==='subscribeConversationV4'){assert.equal(p.workspacePath,binding.workspace);assert.equal(p.sessionId,binding.session_id);subscribed=true;body={ack:{subscriptionId:'own-fixture',mode:'snapshot',logEpoch:'epoch'}};setTimeout(()=>emitEvent(ws,{wireVersion:3,kind:'complete',deliveryKind:'online',logicalFrameId:'f'+(++ordinal),logicalFrameOrdinal:ordinal,topic:'conversation/'+binding.session_id,subscriptionId:'own-fixture',frame:{topic:'conversation/'+binding.session_id,subscriptionId:'own-fixture',sentAt:Date.now(),fromSeq:0,toSeq:0,payload:{kind:'snapshot',snapshot:{...snapshotFixture,protocolVersion:1,sessionId:mode==='wrongsnapshot'?'wrong':binding.session_id,seq:0,logEpoch:'epoch',runtimeInitialization:{status:mode==='coldfailed'?'failed':mode==='coldready'?'initializing':'ready',attemptId:'attempt-fixture'}}}}}),5)}
  if(name==='subscribeConversationV4'&&mode==='malformedsubscribe')body={ack:{subscriptionId:'not-owned',mode:'snapshot',logEpoch:'epoch',untrusted:true}};
  if(name==='sendConversationCommandV4'){
   sendCount++;assert(subscribed);context.envelope.parse(p.envelope);assert.equal(p.envelope.clientId,boundClient);assert.equal(p.envelope.sessionId,binding.session_id);assert.equal(JSON.stringify(p.envelope.payload),JSON.stringify({text:'fixture',requestedDelivery:'queue'}));
   if(mode==='disconnect')return ws.terminate();if(mode==='timeout')return;
   body={commandId:mode==='wrongack'?'wrong':p.envelope.commandId,status:'accepted',revisionAtDecision:1,result:{type:'inputAccepted',inputId:p.envelope.commandId,delivery:'queue'}};
  }
  if(name==='unsubscribeConversationV4'&&mode==='unsubscribeclosed')return ws.terminate();
  if(name==='unsubscribeConversationV4'){assert.equal(p.subscriptionId,'own-fixture');body={ok:true}}
  if(name==='queryConversationCommandsV4'){assert.equal(p.workspacePath,binding.workspace);body={results:[{key:{...p.commands[0],sessionId:mode==='wrongquery'?'wrong':binding.session_id},result:'unknown'}]}}
  if(name==='subscribeConversationV4'&&mode==='coldready')setTimeout(()=>emitEvent(ws,{wireVersion:3,kind:'complete',deliveryKind:'online',logicalFrameId:'f'+(++ordinal),logicalFrameOrdinal:ordinal,topic:'conversation/'+binding.session_id,subscriptionId:'own-fixture',frame:{topic:'conversation/'+binding.session_id,subscriptionId:'own-fixture',sentAt:Date.now(),fromSeq:0,toSeq:1,payload:{kind:'deltas',deltas:[{op:'state.updated',patch:{runtimeInitialization:{status:'ready',attemptId:'attempt-fixture'}}}]}}}),15);
  const bytes=frame(c,[201,h[1]],body);
  if(mode==='partial'&&name==='sendConversationCommandV4'){ws.send(bytes.subarray(0,8));return ws.terminate()}
  if(mode==='split'){ws.send(bytes.subarray(0,8));ws.send(bytes.subarray(8))}else ws.send(bytes);
 }))});
 const rpc=rpcModule.connect({WebSocket,url:`ws://127.0.0.1:${wss.address().port}/ws`,token:'x',codec:c,frame,parser,timeoutMs:200});
 const action=['query','wrongquery'].includes(mode)?'query':'dispatch';
 const request={action,binding,command_id:'app-control-echo-review-'+'a'.repeat(64),client_id:mode==='badclient'?'wrong':'echo-review-alerts',text:'fixture'};
 let error,checks=0;
 try{await flow.run(request,rpc,async()=>{checks++;
   if(['readyfailed','readyinitializing','readyinvalid','readyepoch','readyabsent','readynull','readymalformed','readyduplicate','readyregress'].includes(mode)&&checks===3){
    const socket=[...wss.clients][0];
    const bad=mode==='readyinvalid'?'wrong':'conversation/'+binding.session_id;
    if(['readyepoch','readyabsent','readynull','readymalformed','readyduplicate','readyregress'].includes(mode)){socket.send(frame(c,[204,eventId],{wireVersion:3,kind:'complete',deliveryKind:'online',logicalFrameId:'f'+(++ordinal),logicalFrameOrdinal:ordinal,topic:bad,subscriptionId:'own-fixture',frame:{topic:bad,subscriptionId:'own-fixture',sentAt:Date.now(),fromSeq:0,toSeq:1,payload:{kind:'snapshot',snapshot:{...snapshotFixture,runtimeInitialization:undefined,protocolVersion:1,sessionId:binding.session_id,seq:mode==='readyduplicate'?0:mode==='readyregress'?-1:1,logEpoch:mode==='readyepoch'?'changed':'epoch',...(mode==='readyepoch'?{runtimeInitialization:{status:'ready',attemptId:'attempt-fixture'}}:mode==='readynull'?{runtimeInitialization:null}:mode==='readymalformed'?{runtimeInitialization:{status:'ready'}}:{})}}}}));await new Promise(r=>setTimeout(r,10));return true}
    socket.send(frame(c,[204,eventId],{wireVersion:3,kind:'complete',deliveryKind:'online',logicalFrameId:'f'+(++ordinal),logicalFrameOrdinal:ordinal,topic:bad,subscriptionId:'own-fixture',frame:{topic:bad,subscriptionId:'own-fixture',sentAt:Date.now(),fromSeq:0,toSeq:1,payload:{kind:'deltas',deltas:[{op:'state.updated',patch:{runtimeInitialization:{status:mode==='readyfailed'?'failed':'initializing',attemptId:'new-attempt'}}}]}}}));
    await new Promise(r=>setTimeout(r,10));
   }
   return mode!=='stale'&&!(mode==='race'&&checks===3)})}catch(e){error=e}
 finally{for(const client of wss.clients)client.terminate();await new Promise(r=>wss.close(r))}
 assert.equal(Boolean(error),!['ok','split','query','coldready','fragmented','unsubscribeclosed'].includes(mode));assert(sendCount<=1);if(['readyfailed','readyinitializing','readyinvalid','readyepoch','readyabsent','readynull','readymalformed','readyduplicate','readyregress'].includes(mode))assert.equal(sendCount,0);
 if(['ok','split','wrongack','race','coldready'].includes(mode))assert(calls.includes('unsubscribeConversationV4'));
 if(['lostsubscribe','malformedsubscribe'].includes(mode)){assert.equal(sendCount,0);assert(!calls.includes('unsubscribeConversationV4'))}
 if(['badclient','stale'].includes(mode))assert.equal(calls.length,0);
 if(action==='query')assert(!calls.includes('sendConversationCommandV4'));
 console.log('PASS native authenticated loopback',mode,calls.join(','));count++;
}
(async()=>{assert.throws(()=>context.hello.parse({kind:'clientHello',protocolVersion:3,clientId:'x',appVersion:'x',trustedRelay:true}));for(const mode of ['ok','split','fragmented','unsubscribeclosed','lostsubscribe','malformedsubscribe','query','wrongquery','badhello','badclient','stale','wrongack','disconnect','timeout','partial','race','wrongsnapshot','coldfailed','badinit','coldready','readyfailed','readyinitializing','readyinvalid','readyepoch','readyabsent','readynull','readymalformed','readyduplicate','readyregress'])await one(mode);console.log('TOTAL',count,'transport-flow tests, installed framing/hello and full installed snapshot/delta/wire schemas and restricted effect schema. Fixture endpoint only.')})().catch(e=>{console.error(e);process.exitCode=1});
