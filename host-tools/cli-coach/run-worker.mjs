import path from 'node:path';
import {HostWorker,privateJson} from './worker.mjs';
const args=process.argv.slice(2);if(args.length<1||args.length>2||!path.isAbsolute(args[0])||(args.length===2&&!['--once','--status'].includes(args[1])))throw new Error('usage: node run-worker.mjs /absolute/private/worker.json [--once|--status]');
const ctrl=new AbortController();const stop=()=>ctrl.abort();process.once('SIGINT',stop);process.once('SIGTERM',stop);
try{
 const config=await privateJson(args[0]);const allowed=new Set(['baseUrl','tokenFile','stateDir','pins','killWrapper','authHome','cliLimits','httpTimeoutMs','maxHttpAttempts','pollMs']);if(Object.keys(config).some(k=>!allowed.has(k)))throw new Error('unknown_worker_configuration');
 const worker=new HostWorker(config);const result=args[1]==='--status'?await worker.status({signal:ctrl.signal}):args[1]==='--once'?await worker.tick({signal:ctrl.signal}):await worker.run({signal:ctrl.signal,onStatus:value=>process.stdout.write(JSON.stringify(value)+'\n')});
 if(args[1]||result.status==='cancelled')process.stdout.write(JSON.stringify(result)+'\n');if(['blocked','http_auth','http_rejected'].includes(result.status))process.exitCode=2;
}catch(error){process.stderr.write(JSON.stringify({status:'worker_failed',reason:/^[a-z_]{1,80}$/.test(error.message)?error.message:'local_worker_error'})+'\n');process.exitCode=2;}
