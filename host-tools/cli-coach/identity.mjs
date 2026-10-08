import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile,realpath,stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
export const CLI_VERSION='codex-cli 0.160.1';
export async function sha256File(file){const h=createHash('sha256');for await(const chunk of createReadStream(file))h.update(chunk);return h.digest('hex');}
export async function discoverIdentity(launcher){
 const entry=await realpath(launcher);const packageRoot=path.dirname(path.dirname(entry));
 const pkg=JSON.parse(await readFile(path.join(packageRoot,'package.json'),'utf8'));
 if(pkg.name!=='@openai/codex'||pkg.version!=='0.160.1')throw new Error('unsupported_cli_package');
 const targets={'darwin-arm64':['codex-darwin-arm64','aarch64-apple-darwin'],'darwin-x64':['codex-darwin-x64','x86_64-apple-darwin'],'linux-arm64':['codex-linux-arm64','aarch64-unknown-linux-musl'],'linux-x64':['codex-linux-x64','x86_64-unknown-linux-musl']};
 const target=targets[`${process.platform}-${process.arch}`];if(!target)throw new Error('unsupported_host');
 let vendor;try{vendor=path.join(path.dirname(createRequire(entry).resolve(`@openai/${target[0]}/package.json`)),'vendor');}catch{vendor=path.join(packageRoot,'vendor');}
 const native=await realpath(path.join(vendor,target[1],'bin','codex'));
 const node=await realpath(process.execPath);
 const pins={version:CLI_VERSION};for(const [name,file] of [['launcher',entry],['native',native],['node',node]])pins[name]={path:file,sha256:await sha256File(file)};
 return pins;
}
export class IdentityGuard{
 constructor(pins){if(!pins||pins.version!==CLI_VERSION)throw new Error('unsupported_cli_version');this.pins=pins;this.identities={};}
 async verify(){
  if(await realpath(process.execPath)!==this.pins.node?.path)throw new Error('node_identity_mismatch');
  for(const name of ['launcher','native','node']){
   const pin=this.pins[name];if(!pin||!path.isAbsolute(pin.path)||!/^[a-f0-9]{64}$/.test(pin.sha256))throw new Error('invalid_executable_pin');
   if(await realpath(pin.path)!==pin.path)throw new Error('executable_identity_mismatch');
   const info=await stat(pin.path);if(!info.isFile()||(info.mode&0o022)!==0)throw new Error('unsafe_executable_permissions');
   const identity=`${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}:${info.ctimeMs}`;
   if(this.identities[name]!==identity){if(await sha256File(pin.path)!==pin.sha256)throw new Error('executable_hash_mismatch');this.identities[name]=identity;}
  }
  return this.pins.native.path;
 }
}
if(process.argv[1]&&fileURLToPath(import.meta.url)===path.resolve(process.argv[1])){
 if(process.argv.length!==3)throw new Error('usage: node identity.mjs /absolute/codex-launcher');
 process.stdout.write(JSON.stringify(await discoverIdentity(process.argv[2]),null,2)+'\n');
}
