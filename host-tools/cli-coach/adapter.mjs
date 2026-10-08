import { spawn } from 'node:child_process';
import { readFile, mkdir, open, unlink, rename, mkdtemp, realpath, rm, access } from 'node:fs/promises';
import { constants, writeFileSync, openSync, fsyncSync, closeSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const base = path.dirname(fileURLToPath(import.meta.url));
const disabled = ['shell_tool','unified_exec','apps','plugins','hooks','multi_agent','computer_use','browser_use','browser_use_external','browser_use_full_cdp_access','in_app_browser','in_app_chat','in_app_local_automation','code_mode','code_mode_host','image_generation','view_image','skill_search','tool_suggest','sleep_tool','goals','agent_message_board','api_key_model_discovery','daemon_auto_start','auth_elicitation','multi_agent_v2','remote_plugin','recommended_plugins','plugin_sharing','shell_snapshot','shell_snapshot_v2','skill_mcp_dependency_install','tool_call_mcp_elicitation'];
const hash = value => createHash('sha256').update(value).digest('hex');
const exact = (o, keys) => o && typeof o === 'object' && !Array.isArray(o) && Object.keys(o).length === keys.length && keys.every(k => Object.hasOwn(o,k));
const text = (s,max) => typeof s === 'string' && s.length > 0 && s.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(s);
export function validateRequest(r) {
  if (!exact(r,['version','correlationId','idempotencyKey','context']) || r.version !== 1 || typeof r.correlationId!=='string' || !/^[A-Za-z0-9_-]{1,80}$/.test(r.correlationId) || typeof r.idempotencyKey!=='string' || !/^[A-Za-z0-9_-]{1,120}$/.test(r.idempotencyKey) || !r.context || typeof r.context !== 'object' || Array.isArray(r.context)) throw new Error('invalid_request');
  const data=JSON.stringify(r); if(Buffer.byteLength(data)>32768) throw new Error('request_too_large');
  return data;
}
export function validateOutput(o) {
  if (!exact(o,['status','reason','proposedChanges']) || !['monitoring','propose','insufficient_evidence'].includes(o.status) || !text(o.reason,2000) || !Array.isArray(o.proposedChanges) || o.proposedChanges.length>10 || (o.status !== 'propose' && o.proposedChanges.length) || (o.status === 'propose' && !o.proposedChanges.length)) return false;
  return o.proposedChanges.every(c=>exact(c,['workoutId','field','value','reason']) && typeof c.workoutId==='string' && /^[A-Za-z0-9_-]{1,120}$/.test(c.workoutId) && ['pace_seconds_per_km','distance_meters','duration_seconds'].includes(c.field) && Number.isFinite(c.value) && c.value>0 && c.value<=1000000 && text(c.reason,1000));
}
export function restrictTaskResult(result,context) {
  if(context?.task!=='explanation_only'||result?.status!=='ok'||!Array.isArray(result.output?.proposedChanges)||!result.output.proposedChanges.length)return result;
  const {output,...evidence}=result;return {...evidence,status:'policy_rejected',diagnostics:[...(Array.isArray(result.diagnostics)?result.diagnostics:[]),'explanation_changes_forbidden']};
}
function classify(message) {
  if (/quota|usage limit|rate.?limit|429/i.test(message)) return 'quota';
  if (/unauthori[sz]ed|401|auth.*expir|not logged|login required|refresh.*token/i.test(message)) return 'auth_expired';
  if (/policy|refus|content.*filter/i.test(message)) return 'policy_rejected';
  return 'transient';
}

const persistOwnership=(file,metadata)=>{writeFileSync(file,JSON.stringify(metadata),{mode:0o600});const fd=openSync(file,'r');try{fsyncSync(fd);}finally{closeSync(fd);}const directory=openSync(path.dirname(file),'r');try{fsyncSync(directory);}finally{closeSync(directory);}};
const alive=pid=>{if(!Number.isSafeInteger(pid)||pid<1)return false;try {process.kill(pid,0);return true;}catch(e){return e.code!=='ESRCH';}};
export async function acquireHostLock(lockPath) {
  const metadata={pid:process.pid,childPid:null,createdAt:new Date().toISOString(),token:randomUUID()};
  const create=async()=>{const handle=await open(lockPath,'wx',0o600);await handle.writeFile(JSON.stringify(metadata));return {handle,metadata};};
  try {return await create();}catch(e){if(e.code!=='EEXIST')throw e;}
  // Recovery mutex prevents two live contenders from reclaiming the same dead owner.
  const recovery=lockPath+'.recovery';let guard;
  try {guard=await open(recovery,'wx',0o600);await guard.writeFile(JSON.stringify({pid:process.pid}));}catch(e){if(e.code==='EEXIST')return null;throw e;}
  try {
    let owner;try {owner=JSON.parse(await readFile(lockPath,'utf8'));}catch{return null;}
    if(!owner || owner.quarantined===true || (owner.spawnIntent===true&&owner.childPid===null) || !Number.isSafeInteger(owner.pid) || alive(owner.pid) || (owner.childPid!==null && (!Number.isSafeInteger(owner.childPid)||alive(owner.childPid))))return null;
    await unlink(lockPath);try {return await create();}catch(e){if(e.code==='EEXIST')return null;throw e;}
  }finally {await guard.close();await unlink(recovery);}
}

export class CliCoach {
  constructor({executable,stateDir,killWrapper,timeoutMs=90000,maxDailyCalls=20,maxDailyTokens=400000,tokenReservation=30000,maxEventBytes=262144,authHome,evidenceRecorder,clock=()=>new Date()}={}) {
    if (![executable,stateDir,killWrapper,authHome].every(x=>typeof x==='string' && path.isAbsolute(x))) throw new Error('absolute_host_paths_required');
    if (![timeoutMs,maxDailyCalls,maxDailyTokens,tokenReservation,maxEventBytes].every(Number.isSafeInteger) || timeoutMs<10 || timeoutMs>300000 || maxDailyCalls<1 || maxDailyCalls>100 || tokenReservation<1 || maxDailyTokens<tokenReservation || maxDailyTokens>10000000 || maxEventBytes<1024 || maxEventBytes>1048576) throw new Error('invalid_limits');
    Object.assign(this,{executable,stateDir,killWrapper,timeoutMs,maxDailyCalls,maxDailyTokens,tokenReservation,maxEventBytes,authHome,evidenceRecorder,clock});
  }
  async lookup(request,{signal}={}) {return this.analyze(request,{signal,cacheOnly:true});}
  async analyze(request,{signal,cacheOnly=false}={}) {
    const data=validateRequest(request), requestHash=hash(data), day=this.clock().toISOString().slice(0,10);
    if(this.blocked)return {status:'termination_failed',correlationId:request.correlationId};
    await mkdir(this.stateDir,{recursive:true,mode:0o700});
    const lockPath=path.join(this.stateDir,'worker.lock');const acquired=await acquireHostLock(lockPath);
    if(!acquired)return {status:'busy',correlationId:request.correlationId};
    const lock=acquired.handle;this.lockMetadata=acquired.metadata;this.lockPath=lockPath;
    try {
      const file=path.join(this.stateDir,'budget.json'); let state={day,calls:0,reservedTokens:0,usageTokens:0,results:{}};
      try {state=JSON.parse(await readFile(file,'utf8'));} catch(e) {if(e.code!=='ENOENT') throw new Error('budget_state_invalid');}
      if (!state || !/^\d{4}-\d{2}-\d{2}$/.test(state.day) || !['calls','reservedTokens','usageTokens'].every(k=>Number.isSafeInteger(state[k]) && state[k]>=0) || !state.results || typeof state.results!=='object' || Array.isArray(state.results) || Object.keys(state.results).length>200) throw new Error('budget_state_invalid');
      // Never discard today's reservations after a crash; a future clock prevents budget resets.
      if(state.dailyHistory!==undefined&&(!Array.isArray(state.dailyHistory)||state.dailyHistory.length>7||state.dailyHistory.some(h=>!h||!/^\d{4}-\d{2}-\d{2}$/.test(h.day)||!['calls','reservedTokens','usageTokens'].every(k=>Number.isSafeInteger(h[k])&&h[k]>=0))))throw new Error('budget_state_invalid');
      if(state.day<day) {
        const priorDay=state.day, cutoff=new Date(Date.parse(day+'T00:00:00Z')-86400000).toISOString().slice(0,10);
        // A worker lease is at most10min. Retain prior-day attempts (including uncertain reservations)
        // independently of daily counters, so a crash/midnight restart cannot spend twice.
        const results=Object.fromEntries(Object.entries(state.results).filter(([,e])=>(e.attemptDay??priorDay)>=cutoff).map(([k,e])=>[k,{...e,attemptDay:e.attemptDay??priorDay}]));
        const dailyHistory=[...(state.dailyHistory??[]),{day:priorDay,calls:state.calls,reservedTokens:state.reservedTokens,usageTokens:state.usageTokens}].slice(-7);
        state={day,calls:0,reservedTokens:0,usageTokens:0,results,dailyHistory};
      }
      const key=hash(request.idempotencyKey), existing=state.results[key];
      if(existing) return existing.requestHash===requestHash ? {...restrictTaskResult(existing.result,request.context),replayed:true} : {status:'idempotency_conflict',correlationId:request.correlationId};
      if(cacheOnly)return null;
      if(signal?.aborted) return {status:'cancelled',correlationId:request.correlationId};
      if(Object.keys(state.results).length>=200)return {status:'budget_exceeded',correlationId:request.correlationId,diagnostics:['attempt_cache_full']};
      if(state.calls>=this.maxDailyCalls || Math.max(state.reservedTokens,state.usageTokens)+this.tokenReservation>this.maxDailyTokens) return {status:'budget_exceeded',correlationId:request.correlationId};
      const persist=async()=>{const temp=file+'.tmp';const writer=await open(temp,'w',0o600);try{await writer.writeFile(JSON.stringify(state));await writer.sync();}finally{await writer.close();}await rename(temp,file);const directory=await open(this.stateDir,'r');try{await directory.sync();}finally{await directory.close();}};
      state.calls++; state.reservedTokens+=this.tokenReservation;
      state.results[key]={requestHash,attemptDay:state.day,result:{status:'interrupted',correlationId:request.correlationId}}; await persist();
      const authResult=await this.#runUnderOwnership('',signal,true);
      const result=restrictTaskResult(authResult.status==='authenticated' ? await this.#runUnderOwnership(data,signal) : authResult,request.context); result.correlationId=request.correlationId;
      if(result.usage) state.usageTokens+=result.usage.input_tokens+result.usage.output_tokens;
      state.results[key]={requestHash,attemptDay:state.day,result}; await persist(); return result;
    } finally {await lock.close();if(!this.blocked){await unlink(lockPath);this.lockMetadata=null;this.lockPath=null;}}
  }
  async run(data,signal,authOnly=false) {
    if(!authOnly)throw new Error('inference_requires_admission');
    if(this.blocked)return {status:'termination_failed'};
    await mkdir(this.stateDir,{recursive:true,mode:0o700});const lockPath=path.join(this.stateDir,'worker.lock');const owned=await acquireHostLock(lockPath);
    if(!owned)return {status:'busy'};this.lockMetadata=owned.metadata;this.lockPath=lockPath;
    try{return await this.#runUnderOwnership(data,signal,authOnly);}finally{await owned.handle.close();if(!this.blocked){await unlink(lockPath);this.lockMetadata=null;this.lockPath=null;}}
  }
  async #runUnderOwnership(data,signal,authOnly=false) {
    const executable=await realpath(this.executable);
    await access(this.killWrapper,constants.X_OK);
    const cwd=await mkdtemp(path.join(this.stateDir,'request-'));
    const args=authOnly==='version'?['--version']:authOnly?['login','status']:['exec','--ignore-user-config','--ignore-rules','--ephemeral','--skip-git-repo-check','--sandbox','read-only','--model','gpt-6.1-sol','--json','--output-schema',path.join(base,'schema.json'),'-c','web_search="disabled"','-c','project_doc_max_bytes=0','-c',`model_instructions_file=${JSON.stringify(path.join(base,'analyst.txt'))}`,'--enable','skip_host_skill_discovery',...disabled.flatMap(x=>['--disable',x]),'-'];
    // Credential lookup remains in the existing authenticated CLI home. No secrets copied or API-key route inherited.
    const env={PATH:`${path.dirname(process.execPath)}:/usr/bin:/bin:/usr/sbin:/sbin`,HOME:process.env.HOME,CODEX_HOME:this.authHome,LANG:'en_US.UTF-8'};
    try {
      if(this.lockMetadata){this.lockMetadata.childPid=null;this.lockMetadata.spawnIntent=true;this.lockMetadata.quarantined=true;try{persistOwnership(this.lockPath,this.lockMetadata);}catch{return {status:'transient',diagnostics:['ownership_intent_failed']};}}
      return await new Promise(resolve=>{
      let stdout='',stderr='',bytes=0,stopReason=null,grace,terminationWatchdog,killFailed=false,childExited=false;
      if(signal?.aborted)return resolve({status:'cancelled'});
      const child=spawn(executable,args,{cwd,env,stdio:['pipe','pipe','pipe'],shell:false});
      let ownershipFailed=false;if(child.pid && this.lockMetadata){this.lockMetadata.childPid=child.pid;this.lockMetadata.spawnIntent=false;try{persistOwnership(this.lockPath,this.lockMetadata);}catch{ownershipFailed=true;this.blocked=true;}}
      const stop=reason=>{
        if(stopReason) return; stopReason=reason;
        const terminate=signalName=>{if(childExited||child.exitCode!==null||child.signalCode!==null)return;const guard=spawn(this.killWrapper,['--signal',signalName,String(child.pid)],{stdio:'ignore',shell:false}); guard.on('error',()=>{killFailed=true;});guard.on('exit',code=>{if(code!==0 && child.exitCode===null) killFailed=true;});};
        if(child.pid) {terminate('TERM');grace=setTimeout(()=>terminate('KILL'),1000);}
        terminationWatchdog=setTimeout(()=>{this.blocked=true;if(this.lockMetadata){this.lockMetadata.quarantined=true;try{persistOwnership(this.lockPath,this.lockMetadata);}catch{}}finish({status:'termination_failed',diagnostics:[...(ownershipFailed?['ownership_record_failed']:[]),childExited?'output_pipes_not_closed':'owned_child_not_confirmed_terminated'],childPid:child.pid});},3500);
      };
      const timer=setTimeout(()=>stop('timeout'),authOnly?Math.min(this.timeoutMs,5000):this.timeoutMs);
      const abort=()=>stop('cancelled'); signal?.addEventListener('abort',abort,{once:true}); if(signal?.aborted)abort();
      const collect=(target,chunk)=>{bytes+=Buffer.byteLength(chunk);if(bytes>this.maxEventBytes){stop('output_limit');return;}if(target==='stdout')stdout+=chunk;else stderr+=chunk;};
      child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
      child.stdout.on('data',c=>collect('stdout',c));child.stderr.on('data',c=>collect('stderr',c));
      child.stdin.on('error',()=>{}); child.stdin.end(data);
      child.once('exit',()=>{childExited=true;clearTimeout(grace);});
      child.once('error',()=>finish({status:'transient',diagnostics:['cli_spawn_failed']}));
      let done=false;
      const finish=result=>{if(done)return;done=true;clearTimeout(timer);clearTimeout(grace);clearTimeout(terminationWatchdog);signal?.removeEventListener('abort',abort);resolve(result);};
      child.once('close',(code,exitSignal)=>{
        if(this.lockMetadata&&!this.blocked){this.lockMetadata.childPid=null;this.lockMetadata.spawnIntent=false;this.lockMetadata.quarantined=false;try{persistOwnership(this.lockPath,this.lockMetadata);}catch{ownershipFailed=true;this.blocked=true;}}
        if(!authOnly && this.evidenceRecorder)try {this.evidenceRecorder({stdout,stderr});}catch {return finish({status:'evidence_failed'});}
        if(ownershipFailed)return finish({status:'termination_failed',diagnostics:['ownership_record_failed','owned_child_exit_confirmed'],childPid:child.pid,exitCode:code,exitSignal,terminationGuardFailed:killFailed});
        if(authOnly==='version')return finish({status:stopReason||(code===0&&stdout.trim()==='codex-cli 0.160.1'?'version_verified':'version_mismatch'),exitCode:code,exitSignal,terminationGuardFailed:killFailed});
        if(authOnly)return finish({status:stopReason||(code===0&&/Logged in using ChatGPT/.test(stdout+stderr)?'authenticated':'auth_expired'),exitCode:code,exitSignal,terminationGuardFailed:killFailed,diagnostics:code===0?[]:['chatgpt_login_required']});
        const evidence={eventHash:hash(stdout),diagnosticHash:hash(stderr),exitCode:code,exitSignal,diagnostics:[]};
        let malformed=false;const events=[];
        for(const line of stdout.trim().split('\n').filter(Boolean)){try{const e=JSON.parse(line);if(!e||typeof e!=='object'||Array.isArray(e)||typeof e.type!=='string'||(e.item&&(typeof e.item!=='object'||Array.isArray(e.item))))malformed=true;else events.push(e);}catch{malformed=true;}}
        evidence.eventTypes=[...new Set(events.map(e=>e.type))].slice(0,16).map(t=>t.slice(0,64));
        evidence.itemTypes=[...new Set(events.filter(e=>e.item).map(e=>String(e.item.type)))].slice(0,16).map(t=>t.slice(0,64));
        const usage=events.findLast(e=>e.type==='turn.completed')?.usage;
        const validUsage=usage && ['input_tokens','output_tokens'].every(k=>Number.isSafeInteger(usage[k])&&usage[k]>=0);
        if(validUsage)evidence.usage=Object.fromEntries(Object.entries(usage).filter(([k,v])=>['input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens'].includes(k)&&Number.isSafeInteger(v)&&v>=0));
        if(stopReason)return finish({status:stopReason,...evidence,terminationGuardFailed:killFailed});
        if(malformed)return finish({status:'malformed',...evidence});
        const errors=events.filter(e=>e.type==='error'||e.type==='turn.failed'||e.item?.type==='error');
        // Disabled Code Mode emits this known startup diagnostic; record it, never silently erase it.
        const startupCategory=e=>{const message=e.item?.message; if(typeof message!=='string')return null; if(message.startsWith('Code Mode is unavailable because code-mode host is disabled.'))return 'disabled_code_mode_startup'; if(message.startsWith('Under-development features enabled: skip_host_skill_discovery.'))return 'host_skill_discovery_disabled_warning';return null;};
        const actualErrors=errors.filter(e=>!startupCategory(e));
        if(errors.length)evidence.diagnostics.push(...errors.slice(0,16).map(e=>startupCategory(e)||'provider_error'));
        if(events.some(e=>e.item && !['agent_message','reasoning','error'].includes(e.item.type)))return finish({status:'capability_violation',...evidence});
        if(code!==0||actualErrors.length)return finish({status:classify(stderr+actualErrors.map(JSON.stringify).join(' ')),...evidence});
        const message=events.findLast(e=>e.type==='item.completed'&&e.item?.type==='agent_message')?.item.text;
        let output;try {output=JSON.parse(message);}catch{return finish({status:'malformed',...evidence});}
        if(!validateOutput(output)||!validUsage)return finish({status:'malformed',...evidence});
        finish({status:'ok',output,...evidence});
      });
      if(ownershipFailed)stop('termination_failed');
    }); } finally { await rm(cwd,{recursive:true,force:true}); }
  }
}
