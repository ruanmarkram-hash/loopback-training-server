import ts from 'typescript';
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
const src = path.resolve('src');
const out = path.resolve('../../../artifacts/web-qa');
mkdirSync(out, { recursive: true });
const files=[];
function walk(dir) { for (const e of readdirSync(dir,{withFileTypes:true})) {const p=path.join(dir,e.name);if(e.isDirectory())walk(p);else if(p.endsWith('.tsx'))files.push(p);} }
walk(src);
const candidates=[]; const routes=[];
for(const file of files){
 const text=readFileSync(file,'utf8'); const ast=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 function visit(node){
  if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)){
   const tag=node.tagName.getText(ast);const attrs=node.attributes.properties;
   const spreads=attrs.filter(ts.isJsxSpreadAttribute).map(a=>a.expression.getText(ast));
   const props=attrs.filter(ts.isJsxAttribute).map(a=>a.name.getText(ast));
   if(['button','input','select','textarea','Link','NavLink','a','summary','Modal','ConfirmDialog'].includes(tag)||props.some(p=>/^on[A-Z]/.test(p))||spreads.some(p=>/\.bind\b/.test(p))){
    const position=ast.getLineAndCharacterOfPosition(node.getStart(ast));
    candidates.push({source:path.relative(path.resolve('..'),file),line:position.line+1,tag,properties:props,spreads,sourceText:node.getText(ast),discovery:'static_candidate',status:'not tested'});
   }
   if(tag==='Route'){const attr=attrs.find(a=>ts.isJsxAttribute(a)&&a.name.getText(ast)==='path');if(attr)routes.push({path:attr.initializer?.getText(ast),source:'frontend/src/App.tsx'});}
  }
  ts.forEachChild(node,visit);
 }
 visit(ast);
}
writeFileSync(path.join(out,process.env.LOOPBACK_QA_INVENTORY_NAME || 'source-candidates.json'),JSON.stringify({baseline:'21f1b6b3aec3f54498c4e43868744b83a6ede564', stage:process.env.LOOPBACK_QA_INVENTORY_STAGE || 'baseline',note:'Static JSX occurrence count is not the reconciled unique control/state denominator. Mapped/shared controls expand at runtime. No action passes derive from this artifact.',routes,candidates},null,2));
console.log(JSON.stringify({routes:routes.length,staticCandidates:candidates.length,files:files.length}));
