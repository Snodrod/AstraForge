import { spawn } from 'node:child_process';
import { existsSync,promises as fs } from 'node:fs';
import path from 'node:path';
import {root,data,assetInfo,saveAsset,comfyDir} from './storage.mjs';
export async function prepareConcept(imageId,jobId,signal,mode='auto'){
 const comfy=comfyDir();const python=path.join(comfy,'.venv','Scripts','python.exe');
 if(!existsSync(python))throw new Error('Background removal requires the local Python from ComfyUI.');
 const asset=await assetInfo(imageId);const source=path.join(data,'assets',asset.id+asset.extension),output=path.join(data,'runtime','temp',jobId+'-concept.png'),reference=path.join(data,'runtime','temp',jobId+'-reference.png'),metadata=path.join(data,'runtime','temp',jobId+'-reference.json');
 const cache=path.join(data,'models','background');await fs.mkdir(cache,{recursive:true});
 await new Promise((resolve,reject)=>{
  const process=spawn(python,[path.join(root,'scripts','prepare_concept.py'),source,output,mode,reference,metadata],{windowsHide:true,env:{...globalThis.process.env,U2NET_HOME:cache,NUMBA_CACHE_DIR:path.join(data,'runtime','temp','numba'),PYTHONDONTWRITEBYTECODE:'1'}});
  let error='';process.stderr.on('data',b=>{error=(error+b.toString()).slice(-1200);});
  const abort=()=>process.kill();signal.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(()=>process.kill(),180000);
  process.on('error',e=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(e);});
  process.on('exit',code=>{clearTimeout(timer);signal.removeEventListener('abort',abort);if(signal.aborted)reject(new DOMException('Cancelled','AbortError'));else if(code!==0)reject(new Error('Failed to prepare the concept background. '+error));else resolve();});
 });
 try{const prepared=await saveAsset(await fs.readFile(output),'.png','Shape generator input');const texture=await saveAsset(await fs.readFile(reference),'.png','Source color · alpha');prepared.reference={...texture,originalId:imageId,framing:JSON.parse(await fs.readFile(metadata,'utf8'))};return prepared;}finally{for(const file of [output,reference,metadata])await fs.unlink(file).catch(()=>{});}
}
