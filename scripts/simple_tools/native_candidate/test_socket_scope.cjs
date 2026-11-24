'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');const {clientHelloSchema}=require('./native_source_schemas.cjs');
class Emitter{listeners=new Set();event=fn=>{this.listeners.add(fn);return{dispose:()=>this.listeners.delete(fn)}};dispose(){this.listeners.clear()}}
async function one(mode){
 let close,finish,captured,rawDisposed=0;const unsubs=[],flow=[],upstream=new Emitter(),lifecycle=new Emitter();
 const base={onAgentRuntimeLifecycle:lifecycle.event,onDynamicConversationFrame:()=>upstream.event,setConnectionFlowStateV4:async p=>flow.push(p.state),subscribeConversationV4:()=>new Promise(a=>finish=a),unsubscribeConversationV4:async p=>unsubs.push(p.subscriptionId)};
 const agent={channelName:'zcode-agent'},share={},provider={};
 const ctx={TextEncoder,Emitter,clientHelloSchema,clientSupportsWorkflowRunDeltas:()=>false,conversationTopic:s=>'conversation/'+s,createHello:()=>({kind:'hello'}),Event:{None:()=>({dispose(){}})},wrapWebSocket:()=>({onClose:fn=>close=fn}),SocketProtocol:class{},ChannelServer:class{dispose(){rawDisposed++}},LoggingChannelServer:class{},log28:{debug(){}},randomUUID43:()=> 'fixture',IZCodeAgentService:agent,IConversationShareService:share,IProviderProvisioningTargetService:provider};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/installed_connection_scope.js','utf8')+fs.readFileSync(__dirname+'/installed_socket_scope.js','utf8'),ctx);
 const services={getOptional:key=>key===agent?base:undefined,exposeOnChannelServer(server,overrides){captured=overrides.get('zcode-agent')}};
 ctx.expose({},services,'web-remote-replayable');const target={workspacePath:'/fixture',sessionId:'s'};
 await captured.helloConversationV4();await captured.initializeConversationV4({kind:'clientHello',protocolVersion:3,clientId:'echo-review-alerts',appVersion:'3.19.0'});
 captured.onDynamicConversationFrame(target)(()=>{});const p=captured.subscribeConversationV4(target);p.catch(()=>{});
 if(mode==='close-before-subscribe-ack'){close();await new Promise(r=>setImmediate(r));finish({ack:{subscriptionId:'own',mode:'snapshot',logEpoch:'epoch'}});await assert.rejects(p)}
 else {finish({ack:{subscriptionId:'own',mode:'snapshot',logEpoch:'epoch'}});await p;close()}
 await new Promise(r=>setImmediate(r));assert.deepEqual(unsubs,['own']);assert.equal(rawDisposed,1);assert.equal(upstream.listeners.size,0);assert.equal(lifecycle.listeners.size,0);await assert.rejects(captured.helloConversationV4());console.log('PASS exact installed socket hook',mode);
}
(async()=>{await one('close-after-subscribe-ack');await one('close-before-subscribe-ack');console.log('TOTAL 2 exact installed socket-to-scope fixtures. Raw websocket/framing dependencies are fixtures, not a real native server or OS crash.')})().catch(e=>{console.error(e);process.exitCode=1});
