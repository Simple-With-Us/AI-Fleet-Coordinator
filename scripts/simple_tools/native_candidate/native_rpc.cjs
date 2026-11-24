'use strict';
// Authenticated local RPC transport. Caller supplies a verified endpoint, token,
// pinned-source codec and WebSocket implementation. No discovery or secret reads.
function connect({WebSocket,url,token,codec,frame,parser,timeoutMs=10000}){
 if(!/^ws:\/\/127\.0\.0\.1:[0-9]+\/ws$/.test(url)||typeof token!=='string'||!token||/[\r\n]/.test(token))throw Error('endpoint-or-auth-invalid');
 const ws=new WebSocket(url,{headers:{Authorization:'Bearer '+token},followRedirects:false,maxPayload:2*1024*1024});
 let closed=false,ready=false,next=0,pending=null,readyResolve,readyReject;
 const events=new Map();
 const greeting=new Promise((a,b)=>{readyResolve=a;readyReject=b});
 // A consumer may construct then fail validation before awaiting greeting.
 greeting.catch(()=>{});
 let timer=setTimeout(()=>close('greeting-timeout'),timeoutMs);
 function close(reason='closed'){
  if(closed)return;closed=true;clearTimeout(timer);
  readyReject(Error(reason));if(pending){clearTimeout(pending.timer);pending.reject(Error(reason));pending=null}
  ws.terminate();
 }
 const read=parser(codec,(h,b)=>{
  if(!Array.isArray(h))return close('bad-header');
  if(!ready){if(h.length!==1||h[0]!==200)return close('bad-greeting');ready=true;clearTimeout(timer);return readyResolve()}
  if(h[0]===204||h[0]===205){const cb=events.get(h[1]);if(!cb)return close('event-mismatch');if(h[0]===205)return close('event-error');try{cb(b)}catch{return close('event-invalid')}return}
  // Unexpected events are refused, not interpreted as command receipts.
  if(!pending||h[1]!==pending.id||![201,202,203].includes(h[0]))return close('response-mismatch');
  const p=pending;pending=null;clearTimeout(p.timer);
  if(h[0]!==201)return p.reject(Error('rpc-refused'));
  p.resolve(b);
 });
 ws.on('message',data=>{try{read(Buffer.from(data))}catch{close('decode-refused')}});
 ws.on('error',()=>close('transport-error'));ws.on('close',()=>close('connection-closed'));
 return {
  async call(name,args){
   const allowed=new Set(['helloConversationV4','initializeConversationV4','subscribeConversationV4','unsubscribeConversationV4','sendConversationCommandV4','queryConversationCommandsV4']);
   if(!allowed.has(name))throw Error('rpc-method-refused');
   await greeting;if(closed||pending)throw Error('rpc-state');
   return new Promise((resolve,reject)=>{const id=next++;pending={id,resolve,reject,timer:setTimeout(()=>close('rpc-timeout'),timeoutMs)};
    try{ws.send(frame(codec,[100,id,'zcode-agent',name],args))}catch{close('send-uncertain')}
   });
  },
  async listen(name,args,callback){
   if(name!=='onDynamicConversationFrame')throw Error('event-refused');
   await greeting;if(closed)throw Error('rpc-closed');const id=next++;events.set(id,callback);
   ws.send(frame(codec,[102,id,'zcode-agent',name],args));
   return ()=>{if(!closed)ws.send(frame(codec,[103,id]));events.delete(id)};
  },
  async close(){events.clear();close()}
 };
}
module.exports={connect};
