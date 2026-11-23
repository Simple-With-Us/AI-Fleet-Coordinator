#!/usr/bin/env node
/* One-shot stock headless exec with supervised pre-admission catalog observation.
 * No daemon, custom tool call, session rewrite, or selector change.
 * Usage: node THIS exact-package-root options-json-file < private-prompt
 * Supervisor owns pipe FD3, waits for this process's publication readiness,
 * rechecks existing target session, then writes exactly GO\n and closes pipe.
 */
import {createHash} from 'node:crypto';
import {readFile, realpath, lstat} from 'node:fs/promises';
import {Socket} from 'node:net';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export async function waitForAdmission({fd=3, timeoutMs=60000, signal}={}) {
  if (signal?.aborted) throw new Error('Catalog admission cancelled');
  return new Promise((resolve,reject)=>{
    let stream, timer, settled=false, data='';
    const finish=(error)=>{
      if(settled)return;
      settled=true; clearTimeout(timer);
      signal?.removeEventListener('abort',cancel);
      stream?.destroy();
      error?reject(error):resolve();
    };
    const cancel=()=>finish(new Error('Catalog admission cancelled'));
    signal?.addEventListener('abort',cancel,{once:true});
    timer=setTimeout(()=>finish(new Error('Catalog admission timed out')),timeoutMs);
    try {
      stream=new Socket({fd,readable:true,writable:false});
      stream.setEncoding('utf8');
      stream.on('error',()=>finish(new Error('Catalog admission pipe failed')));
      stream.on('data',chunk=>{
        data+=chunk;
        if(data.length>3 || !'GO\n'.startsWith(data)) finish(new Error('Invalid catalog admission token'));
      });
      stream.on('end',()=>finish(data==='GO\n'?undefined:new Error('Catalog admission was not granted')));
    } catch { finish(new Error('Catalog admission pipe unavailable')); }
  });
}

export async function runSupervised({runExec, createRuntime, shutdownRuntime, prompt, options, wait=waitForAdmission, processRef=process}) {
  const controller=new AbortController();
  const cancel=()=>controller.abort();
  let runtime, closed=false;
  const shutdown=async value=>{
    if(closed)return;
    closed=true;
    return shutdownRuntime(value);
  };
  processRef.once('SIGINT',cancel); processRef.once('SIGTERM',cancel); processRef.once('SIGHUP',cancel);
  try {
    await runExec(prompt,options,'0.7.4',{
      processRef,
      createRuntime:async (...args)=>{
        if(controller.signal.aborted)throw new Error('Catalog admission cancelled');
        runtime=await createRuntime(...args);
        try {
          await wait({signal:controller.signal});
          if(controller.signal.aborted)throw new Error('Catalog admission cancelled');
          return runtime;
        } catch(error) {
          await shutdown(runtime);
          throw error;
        }
      },
      shutdownRuntime:shutdown,
    });
  } finally {
    processRef.off('SIGINT',cancel); processRef.off('SIGTERM',cancel); processRef.off('SIGHUP',cancel);
    // Stock runExec owns normal shutdown. Cleanup covers a dependency failure
    // after creation but before the stock run receives the runtime.
    if(runtime&&!closed) await shutdown(runtime);
  }
}

async function main() {
  const [root,optionsPath,...extra]=process.argv.slice(2);
  if(!root||!optionsPath||extra.length)throw new Error('Expected exact package root and options JSON file');
  if(await realpath(root)!==root)throw new Error('Package root must be real and absolute');
  const expected={
  "chunks/run-exec-command-UQB5WLPC.js": "0ab882c8f7d189963c0796aa7f1b90e86c82c6bf006e73615b16da8e5e897972",
  "chunks/lifecycle-GP7HDNAT.js": "226ac62bd86571381f3f6909b9cbb3269325a45ce79ff1f08d1889b642860936",
  "chunks/chunk-YYSTRBDA.js": "701d9c0af2c0da359db856a8f0b6a6bdd0c18265a780d9ef8399b5bd97f212c4",
  "chunks/chunk-66K6XTTT.js": "35bcf165f29f3d1f3b05504432eec083c4d045a312939bba9a70bf168c387fbe",
  "chunks/chunk-334CV6RF.js": "258bf9a241feb460547cec3a1e9d29b8070938ee663d95ca100d3b4743544e11",
  "chunks/chunk-CMWH3NZU.js": "30f1b333276276c686eb905e5d0bc6e010da25b72c66d4c8e9d633f5e4dfe699",
  "chunks/chunk-DCFP5ZHZ.js": "552aea3758cd8f6cc48c9c3b71d83fe0b7ca8e02d2b55386d12248fc1087df32",
  "chunks/data-dir-VLNTBIAQ.js": "621b250b414b1764e5dd6a7e045ae7cfde2650fe060ade7cc0f649d3bde83271",
  "chunks/main-G357CS7U.js": "72c7f96333f83c9193172cde94bf34d9c4b1bd639aef66c555000413c9ca49b6"
};
  for(const [relative,hash] of Object.entries(expected)) {
    const path=join(root,relative), info=await lstat(path);
    if(!info.isFile()||info.uid!==process.geteuid()||(info.mode&0o022))throw new Error('Unsafe pinned runtime file');
    const bytes=await readFile(path);
    if(createHash('sha256').update(bytes).digest('hex')!==hash)throw new Error('Pinned runtime file hash mismatch');
  }
  const pkg=JSON.parse(await readFile(join(root,'package.json'),'utf8'));
  if(pkg.name!=='@minimax-ai/code'||pkg.version!=='0.7.4')throw new Error('Unsupported package version');
  if(process.env.AFC_MM_CATALOG_SHAPE!=='1')throw new Error('Catalog observation must be explicitly enabled');
  const optionsInfo=await lstat(optionsPath);
  if(!optionsInfo.isFile()||optionsInfo.uid!==process.geteuid()||(optionsInfo.mode&0o077))throw new Error('Options must be private owner file');
  const options=JSON.parse(await readFile(optionsPath,'utf8'));
  const permitted=new Set(['session','cwd','model','permission','maxSteps','timeout','outputFormat','input']);
  if(!options||typeof options!=='object'||Array.isArray(options)||Object.keys(options).some(key=>!permitted.has(key)))throw new Error('Unsupported exec options');
  if(typeof options.session!=='string'||!/^mvs_[0-9a-f]{32}$/.test(options.session)||typeof options.cwd!=='string'||options.permission!=='off'||options.input!=='-')throw new Error('Expected bounded existing-session exec options');
  if(typeof options.maxSteps!=='string'||!Number.isInteger(Number(options.maxSteps))||Number(options.maxSteps)<1||Number(options.maxSteps)>16)throw new Error('Invalid step bound');
  const entry=join(root,'cli.js');
  const load=relative=>import(pathToFileURL(join(root,relative)).href);
  await runPinnedBoot({entry,options,load});
}

// Mirror cli.js initialization before main/runExec imports. Main remains owner
// of Node/proxy/CLI validation. Suppress only its final process.exit so guard
// removal and caller cleanup run normally; do not suppress its exit code.
export async function runPinnedBoot({entry,options,load,processRef=process,supervise=runSupervised}) {
  const identity=await load('chunks/chunk-334CV6RF.js');
  const allow=identity.B(identity.A(entry));
  const environment=await load('chunks/chunk-CMWH3NZU.js');
  const argv=[processRef.execPath,entry,'exec'];
  const flags={session:'--session',cwd:'--cwd',model:'--model',permission:'--permission',maxSteps:'--max-steps',timeout:'--timeout',outputFormat:'--output-format',input:'--input'};
  for(const [key,value] of Object.entries(options))argv.push(flags[key],String(value));
  const startup=environment.t(argv.slice(2),allow);
  const data=await load('chunks/data-dir-VLNTBIAQ.js');
  environment.r({dataDir:data.getTuiDataDirPath(),...(startup?{startupBuildEnvironment:startup}:{})});
  const guardModule=await load('chunks/chunk-DCFP5ZHZ.js');
  const guard=await guardModule.h(entry);
  try {
    const cli=await load('chunks/main-G357CS7U.js');
    const facade={env:processRef.env,versions:processRef.versions,argv,stdout:processRef.stdout,stderr:processRef.stderr,
      get title(){return processRef.title},set title(value){processRef.title=value},
      get exitCode(){return processRef.exitCode},set exitCode(value){processRef.exitCode=value},
      exit(code){processRef.exitCode=code}};
    await cli.runTuiCli({allowStartupEnvironmentSelection:allow,processRef:facade,
      runExec:async(prompt,parsed,version)=>{
        if(version!=='0.7.4')throw new Error('Pinned CLI version mismatch');
        const exec=await load('chunks/run-exec-command-UQB5WLPC.js');
        const life=await load('chunks/lifecycle-GP7HDNAT.js');
        await supervise({runExec:exec.runTuiExecCommand,createRuntime:life.createTuiRuntime,shutdownRuntime:life.shutdownTuiRuntime,prompt,options:parsed,processRef});
      }});
  } finally { guard.remove(); }
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href) {
  main().catch(()=>{process.stderr.write('Supervised MiniMax exec failed before verified completion\n');process.exitCode=1;});
}
