import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {runSupervised} from '../minimax_supervised_exec.mjs';

const launcher=fileURLToPath(new URL('../minimax_supervised_exec.mjs',import.meta.url));
function fixture(wait, failure=false) {
  const events=[],proc=new EventEmitter(),runtime={adapter:{}};
  const args={surface:'headless',startupExecutionPolicy:'quarantined'};
  return {events,proc,runtime,call:()=>runSupervised({
    processRef:proc,prompt:undefined,options:{session:'existing'},wait,
    createRuntime:async(a,d)=>{events.push(['create',a,d]);return runtime;},
    shutdownRuntime:async r=>{assert.equal(r,runtime);events.push('shutdown');},
    runExec:async(p,o,v,deps)=>{
      assert.equal(p,undefined);assert.equal(o.session,'existing');assert.equal(v,'0.7.4');
      const result=await deps.createRuntime(args,{unchanged:true});
      assert.equal(result,runtime);events.push('stock-admission');
      if(failure)throw Error('stock failure');
      await deps.shutdownRuntime(result);
    }
  })};
}

test('retains identical runtime until release then preserves stock admission and shutdown',async()=>{
  let release;
  const gate=new Promise(r=>release=r),f=fixture(()=>gate),work=f.call();
  await new Promise(r=>setImmediate(r));
  assert.equal(f.events.length,1);
  release();await work;
  assert.deepEqual(f.events.slice(1),['stock-admission','shutdown']);
  assert.equal(f.proc.listenerCount('SIGTERM'),0);
});
test('refused admission closes runtime once and never admits turn',async()=>{
  const f=fixture(async()=>{throw Error('deny');});
  await assert.rejects(f.call());
  assert.equal(f.events.includes('stock-admission'),false);
  assert.equal(f.events.filter(x=>x==='shutdown').length,1);
});
test('dependency failure after admission retains cleanup',async()=>{
  const f=fixture(async()=>{},true);
  await assert.rejects(f.call());
  assert.equal(f.events.filter(x=>x==='shutdown').length,1);
});
test('signal before release cannot admit turn',async()=>{
  const f=fixture(({signal})=>new Promise((r,j)=>signal.addEventListener('abort',()=>j(Error('abort')),{once:true})));
  const work=f.call();await new Promise(r=>setImmediate(r));f.proc.emit('SIGTERM');
  await assert.rejects(work);
  assert.equal(f.events.includes('stock-admission'),false);
  assert.equal(f.events.filter(x=>x==='shutdown').length,1);
});

async function pipeCase(token,{timeoutMs=1000,close=true}={}) {
  const code=`import {waitForAdmission} from ${JSON.stringify(new URL('../minimax_supervised_exec.mjs',import.meta.url).href)};try{await waitForAdmission({timeoutMs:${timeoutMs}});console.log('admit')}catch{console.log('held')}`;
  const child=spawn(process.execPath,['--input-type=module','-e',code],{stdio:['ignore','pipe','pipe','pipe']});
  let stdout='',stderr='';child.stdout.on('data',x=>stdout+=x);child.stderr.on('data',x=>stderr+=x);
  const done=new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',c=>resolve(c));});
  if(token)child.stdio[3].write(token);
  if(close)child.stdio[3].end();
  assert.equal(await done,0);assert.equal(stderr,'');return stdout.trim();
}
test('private pipe requires exact token plus EOF',async()=>{
  assert.equal(await pipeCase('GO\n'),'admit');
  for(const token of ['', 'GO', 'GO\nextra','NO\n'])assert.equal(await pipeCase(token),'held');
});
test('pipe timeout holds without token',async()=>{
  assert.equal(await pipeCase('',{timeoutMs:30,close:false}),'held');
});

test('stock boot ordering, parsed exec delegation and guard cleanup',async()=>{
  const {runPinnedBoot}=await import('../minimax_supervised_exec.mjs');
  const events=[],proc={execPath:'/node',env:{},versions:{node:'24'},stdout:{},stderr:{},exitCode:0};
  const parsed={session:'existing',maxSteps:'16'};
  const modules={
    'chunks/chunk-334CV6RF.js':{A:entry=>(events.push(['identity',entry]),'package'),B:()=>false},
    'chunks/chunk-CMWH3NZU.js':{t:argv=>(events.push(['startup',argv]),undefined),r:args=>events.push(['environment',args])},
    'chunks/data-dir-VLNTBIAQ.js':{getTuiDataDirPath:()=>'/same-data'},
    'chunks/chunk-DCFP5ZHZ.js':{h:async entry=>(events.push(['guard',entry]),{remove:()=>events.push('remove')})},
    'chunks/main-G357CS7U.js':{runTuiCli:async args=>{events.push('main');assert.equal(args.allowStartupEnvironmentSelection,false);assert.deepEqual(args.processRef.argv,['/node','/p/cli.js','exec','--session=existing','--max-steps=16']);await args.runExec(undefined,parsed,'0.7.4');args.processRef.exit(0)}},
    'chunks/run-exec-command-UQB5WLPC.js':{runTuiExecCommand:()=>{}},
    'chunks/lifecycle-GP7HDNAT.js':{createTuiRuntime:()=>{},shutdownTuiRuntime:()=>{}}
  };
  await runPinnedBoot({entry:'/p/cli.js',options:parsed,processRef:proc,load:async name=>{events.push(['load',name]);return modules[name]},supervise:async args=>{assert.equal(args.options,parsed);assert.equal(args.processRef,proc);events.push('supervise')}});
  assert.ok(events.findIndex(x=>x?.[0]==='environment')<events.indexOf('main'));
  assert.ok(events.indexOf('main')<events.indexOf('supervise'));
  assert.equal(events.at(-1),'remove');
});
test('failed main boot still removes startup guard',async()=>{
  const {runPinnedBoot}=await import('../minimax_supervised_exec.mjs');let removed=0;
  const modules={
    'chunks/chunk-334CV6RF.js':{A:()=>'',B:()=>false},
    'chunks/chunk-CMWH3NZU.js':{t:()=>undefined,r:()=>{}},
    'chunks/data-dir-VLNTBIAQ.js':{getTuiDataDirPath:()=>'/same-data'},
    'chunks/chunk-DCFP5ZHZ.js':{h:async()=>({remove:()=>removed++})},
    'chunks/main-G357CS7U.js':{runTuiCli:async()=>{throw Error('bad')}}
  };
  await assert.rejects(runPinnedBoot({entry:'/p/cli.js',options:{},load:async name=>modules[name],processRef:{execPath:'/node'}}));
  assert.equal(removed,1);
});
test('cancellation during slow creation closes returned runtime before admission',async()=>{
  let finish;const created=new Promise(r=>finish=r),events=[],proc=new EventEmitter(),runtime={};
  const work=runSupervised({processRef:proc,prompt:'',options:{},createRuntime:()=>created,shutdownRuntime:async()=>events.push('close'),wait:async({signal})=>{if(signal.aborted)throw Error('cancel')},runExec:async(p,o,v,d)=>{await d.createRuntime();events.push('admit')}});
  proc.emit('SIGTERM');finish(runtime);await assert.rejects(work);
  assert.deepEqual(events,['close']);
});


test('package boundary rejects wrong shapes and accepts real extra manifest fields',async()=>{
  const {validatePinnedPackage}=await import('../minimax_supervised_exec.mjs');
  for(const pkg of [null,[],true,1,'package',{}, {name:'@minimax-ai/code',version:0.74},{name:'other',version:'0.7.4'}])assert.throws(()=>validatePinnedPackage(pkg),/Unsupported package version/);
  const pkg={name:'@minimax-ai/code',version:'0.7.4',type:'module',dependencies:{},scripts:{postinstall:'untrusted data, not executed'}};
  assert.equal(validatePinnedPackage(pkg),pkg);
});
test('leading-hyphen and spaced values stay in a single named argv token',async()=>{
  const {runPinnedBoot}=await import('../minimax_supervised_exec.mjs');let observed;
  const modules={
    'chunks/chunk-334CV6RF.js':{A:()=>'',B:()=>false},
    'chunks/chunk-CMWH3NZU.js':{t:argv=>(observed=argv,undefined),r:()=>{}},
    'chunks/data-dir-VLNTBIAQ.js':{getTuiDataDirPath:()=>'/same-data'},
    'chunks/chunk-DCFP5ZHZ.js':{h:async()=>({remove:()=>{}})},
    'chunks/main-G357CS7U.js':{runTuiCli:async()=>{}}
  };
  await runPinnedBoot({entry:'/p/cli.js',options:{model:'--permission=full',cwd:'/path with spaces',input:'-'},load:async name=>modules[name],processRef:{execPath:'/node'}});
  assert.deepEqual(observed,['exec','--model=--permission=full','--cwd=/path with spaces','--input=-']);
});


test('cleanup rejection preserves admission failure and makes one attempt',async()=>{
  const denial=Error('admission denied'),cleanup=Error('cleanup failed');
  const proc=new EventEmitter(),diagnostics=[];proc.stderr={write:x=>diagnostics.push(x)};
  let attempts=0;
  await assert.rejects(runSupervised({processRef:proc,options:{},createRuntime:async()=>({}),
    wait:async()=>{throw denial},shutdownRuntime:async()=>{attempts++;throw cleanup},
    runExec:async(p,o,v,d)=>{await d.createRuntime()}}),error=>error===denial);
  assert.equal(attempts,1);assert.equal(diagnostics.length,1);
  assert.match(diagnostics[0],/host cleanup remains required/);
  assert.equal(proc.listenerCount('SIGTERM'),0);
});
test('normal shutdown rejection is not silently reported as success or retried',async()=>{
  const cleanup=Error('cleanup failed');let attempts=0;
  await assert.rejects(runSupervised({processRef:new EventEmitter(),options:{},wait:async()=>{},
    createRuntime:async()=>({}),shutdownRuntime:async()=>{attempts++;throw cleanup},
    runExec:async(p,o,v,d)=>{const r=await d.createRuntime();await d.shutdownRuntime(r)}}),error=>error===cleanup);
  assert.equal(attempts,1);
});
test('option boundary refuses unknown and nonstring values before boot side effects',async()=>{
  const {runPinnedBoot,validateExecOptionValues}=await import('../minimax_supervised_exec.mjs');
  for(const options of [null,[],{unknown:'x'},{toString:'x'},{model:null},{timeout:900000},{outputFormat:{}},{model:undefined}]){
    let loads=0;
    assert.throws(()=>validateExecOptionValues(options),/Unsupported exec options/);
    await assert.rejects(runPinnedBoot({options,load:async()=>{loads++}}),/Unsupported exec options/);
    assert.equal(loads,0);
  }
  const options={timeout:'900s',model:'--permission=full',cwd:'/path with spaces',input:'-'};
  assert.equal(validateExecOptionValues(options),options);
});

test('fallback cleanup failure preserves stock failure without retry',async()=>{
  const stock=Error('stock failed');let attempts=0;const proc=new EventEmitter(),diagnostics=[];
  proc.stderr={write:x=>diagnostics.push(x)};
  await assert.rejects(runSupervised({processRef:proc,options:{},wait:async()=>{},
    createRuntime:async()=>({}),shutdownRuntime:async()=>{attempts++;throw Error('cleanup')},
    runExec:async(p,o,v,d)=>{await d.createRuntime();throw stock}}),error=>error===stock);
  assert.equal(attempts,1);assert.equal(diagnostics.length,1);
});
