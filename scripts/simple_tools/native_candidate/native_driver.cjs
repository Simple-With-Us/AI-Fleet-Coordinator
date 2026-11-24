'use strict';
// Source-only executable driver candidate. NOT invoked against a real host in tests.
const fs=require('fs'),path=require('path'),crypto=require('crypto'),readline=require('readline');
const {connect}=require('./native_rpc.cjs'),{run}=require('./native_flow.cjs'),codecModule=require('./native_codec.cjs');
const ROOT=codecModule.ROOT,HASH='ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0';
function privateRead(file,max,secret=false){
 const fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{const st=fs.fstatSync(fd);if(!st.isFile()||st.nlink!==1||st.uid!==process.getuid()||(st.mode&(secret?0o077:0o022))||st.size>max)throw Error('private-file-invalid');return fs.readFileSync(fd,'utf8')}finally{fs.closeSync(fd)}
}
function hostSnapshot(home){
 if(process.platform!=='linux'||process.getuid()===0)throw Error('local-linux-user-required');
 const dir=path.join(home,'.zcode/server/run');
 const status=JSON.parse(privateRead(path.join(dir,'status.json'),65536));
 if(status.state!=='ready'||status.protocolVersion!==1||status.version!=='3.19.0'||status.host!=='127.0.0.1'||!Number.isSafeInteger(status.pid)||!Number.isSafeInteger(status.port)||status.port<1||status.port>65535||!Number.isSafeInteger(status.generation))throw Error('host-status-invalid');
 const proc='/proc/'+status.pid;
 if(fs.statSync(proc).uid!==process.getuid())throw Error('host-owner-mismatch');
 const args=fs.readFileSync(proc+'/cmdline','utf8').split('\0').filter(Boolean);
 if(args.length!==3||args[0]!==ROOT+'/node'||args[1]!==ROOT+'/server-core.js'||args[2]!==String(status.generation))throw Error('host-command-mismatch');
 if(fs.realpathSync(proc+'/exe')!==fs.realpathSync(ROOT+'/node'))throw Error('host-runtime-mismatch');
 const paths=new Set();
 for(const leaf of [dir,ROOT+'/node',ROOT+'/server-core.js']){let item=leaf;while(true){paths.add(item);const parent=path.dirname(item);if(parent===item)break;item=parent}}
 for(const item of paths){
  const st=fs.lstatSync(item);if(st.isSymbolicLink()||(st.uid!==process.getuid()&&st.uid!==0)||(st.mode&0o022))throw Error('host-path-custody');
 }
 const core=fs.readFileSync(ROOT+'/server-core.js');
 if(crypto.createHash('sha256').update(core).digest('hex')!==HASH)throw Error('host-source-mismatch');
 // Pinning exact source pins standalone-server absent browser executor wiring.
 const inodes=new Set();
 for(const entry of fs.readdirSync(proc+'/fd')){try{const link=fs.readlinkSync(proc+'/fd/'+entry);const m=/^socket:\[([0-9]+)\]$/.exec(link);if(m)inodes.add(m[1])}catch{}}
 const portHex=status.port.toString(16).toUpperCase().padStart(4,'0');
 const listeners=fs.readFileSync('/proc/net/tcp','utf8').trim().split('\n').slice(1).map(l=>l.trim().split(/\s+/)).filter(cols=>cols[1]==='0100007F:'+portHex&&cols[3]==='0A');
 if(listeners.length!==1||!inodes.has(listeners[0][9])||Number(listeners[0][7])!==process.getuid())throw Error('endpoint-owner-mismatch');
 const stat=fs.readFileSync(proc+'/stat','utf8');const rest=stat.slice(stat.lastIndexOf(')')+2).split(' ');
 return {pid:status.pid,generation:status.generation,startedAt:status.startedAt,port:status.port,processStartTicks:rest[19]};
}
function same(a,b){return JSON.stringify(a)===JSON.stringify(b)}
async function main(){
 const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});const iterator=lines[Symbol.asyncIterator]();
 const first=await iterator.next();if(first.done||Buffer.byteLength(first.value)>32768)throw Error('input-invalid');
 const request=JSON.parse(first.value),home=process.env.HOME;
 if(!home||!path.isAbsolute(home))throw Error('home-invalid');
 const original=hostSnapshot(home);let verificationId=0;
 async function verify(binding){
  if(!same(original,hostSnapshot(home)))return false;
  const id=++verificationId;
  process.stdout.write(JSON.stringify({verify:id,binding})+'\n');
  const answer=await iterator.next();if(answer.done||answer.value.length>1024)throw Error('verification-lost');
  const reply=JSON.parse(answer.value);
  return reply.verify===id&&reply.ok===true&&same(original,hostSnapshot(home));
 }
 if(!await verify(request.binding))throw Error('initial-binding-refused');
 // The only persistent secret read. Never returned, logged, or passed in argv.
 const tokenPath=path.join(home,'.zcode/server/run/ingress-token');
 if(fs.lstatSync(tokenPath).mode&0o077)throw Error('token-not-private');
 const token=privateRead(tokenPath,4096,true).trim();
 const {WebSocket}=require(ROOT+'/node_modules/ws');
 const codec=codecModule.codec();
 const rpc=connect({WebSocket,url:`ws://127.0.0.1:${original.port}/ws`,token,codec,frame:codecModule.frame,parser:codecModule.parser,timeoutMs:10000});
 try{const result=await run(request,rpc,verify);process.stdout.write(JSON.stringify({result})+'\n')}finally{await rpc.close();lines.close()}
}
if(require.main===module){main().catch(()=>{process.stdout.write(JSON.stringify({error:'native-driver-refused-or-uncertain'})+'\n');process.exitCode=1})}
module.exports={privateRead,hostSnapshot};
