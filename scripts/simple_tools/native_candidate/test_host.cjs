'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path'),crypto=require('crypto');
const code=fs.readFileSync(path.join(__dirname,'native_driver.cjs'),'utf8');
const ROOT='/home/jay/.zcode/server/releases/3.19.0-linux-x64-60f71efd6a01/runtime';
let count=0;
for(const mode of ['ok','wrongargv','wronguid','wrongport','statusrace','symlink','tokenhardlink','tokenmode','releaseancestor','stateancestor','custodyrefused','restart']){
 const status={state:'ready',protocolVersion:1,version:'3.19.0',host:'127.0.0.1',pid:123,port:42799,generation:3,startedAt:1};
 const hex=status.port.toString(16).toUpperCase().padStart(4,'0');
 const args=[ROOT+'/node',ROOT+'/server-core.js','3'];if(mode==='wrongargv')args.push('--extra');
 let reads=0;
 const fake={constants:fs.constants,openSync:p=>p,closeSync(){},fstatSync:p=>({isFile:()=>true,uid:1001,mode:mode==='tokenmode'&&String(p).includes('ingress')?0o644:0o600,nlink:mode==='tokenhardlink'?2:1,size:100}),
  lstatSync:p=>({isSymbolicLink:()=>mode==='symlink'||mode==='releaseancestor'&&p===path.dirname(ROOT)||mode==='stateancestor'&&p==='/home/jay/.zcode/server',uid:1001,mode:0o700}),statSync:p=>({uid:mode==='wronguid'?1002:1001}),realpathSync:p=>ROOT+'/node',
  readdirSync:p=>['5'],readlinkSync:p=>p.endsWith('/exe')?ROOT+'/node':'socket:[999]',
  readFileSync(p,encoding){p=String(p);if(p.endsWith('status.json')){reads++;return JSON.stringify({...status,pid:mode==='restart'&&reads>1?124:123,port:mode==='statusrace'&&reads>1?999:status.port})}
   if(p.endsWith('/cmdline'))return args.join('\0')+'\0';if(p.endsWith('server-core.js'))return Buffer.from('fixture');
   if(p==='/proc/net/tcp')return 'header\n0: 0100007F:'+hex+' 00000000:0000 0A 0 0 0 1001 0 '+(mode==='wrongport'?'888':'999')+'\n';
   if(p.endsWith('/stat'))return '123 (node) '+Array.from({length:22},(_,i)=>i===19?'456':'0').join(' ');
   if(p.includes('ingress-token'))return 'fixture-not-a-secret';throw Error('unexpected-read:'+p)
  }};
 const fakeCrypto={createHash:()=>({update(){return this},digest(){return 'ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0'}})};
 const module={exports:{}};const req=n=>n==='child_process'?{execFileSync:()=>JSON.stringify({ok:mode!=='custodyrefused'})}:n==='fs'?fake:n==='crypto'?fakeCrypto:n==='./native_rpc.cjs'?{}:n==='./native_flow.cjs'?{}:n==='./native_codec.cjs'?{ROOT}:require(n);
 vm.runInNewContext(code,{require:req,module,process:{platform:'linux',getuid:()=>1001},Buffer,console,setTimeout,clearTimeout,__dirname});
 let error=false;try{const a=module.exports.hostSnapshot('/home/jay');if(['restart','statusrace'].includes(mode)){const b=module.exports.hostSnapshot('/home/jay');assert.notEqual(JSON.stringify(a),JSON.stringify(b))}if(mode.startsWith('token'))module.exports.privateRead('/home/jay/ingress-token',4096,true)}catch{error=true}
 assert.equal(error,['wrongargv','wronguid','wrongport','symlink','tokenhardlink','tokenmode','statusrace','releaseancestor','stateancestor','custodyrefused'].includes(mode));console.log('PASS host custody fixture',mode);count++;
}
console.log('TOTAL',count,'host custody fixtures. Real process/socket and token not opened.');
