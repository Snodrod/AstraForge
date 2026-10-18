import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { data,readJson,writeJson,assetInfo,saveAsset } from './storage.mjs';
import { hunyuanWorkflow,hunyuanMultiViewWorkflow,MV_CHECKPOINT,sdxlWorkflow } from './schema.mjs';
import {getViewsService} from './views.mjs';
import WebSocket from 'ws';
import { llmChat } from './llm.mjs';
import { prepareConcept } from './preprocess.mjs';
import {validateGeneratedMesh,maxMeshBytes} from './mesh-validation.mjs';
import {acquireEngineGpu} from './gpu-handover.mjs';
import {pixal3dConfig,pixal3dStatus} from './pixal3d.mjs';
import {generatePixal3d} from './pixal3d-run.mjs';
const jobs=new Map();const controllers=new Map();let active=null;let pumping=false;
export async function loadJobs(){for(const file of await fs.readdir(path.join(data,'jobs'))){if(!file.endsWith('.json'))continue;const j=await readJson(path.join(data,'jobs',file),null);if(!j)continue;if(['queued','running'].includes(j.status)){j.status='failed';j.message='The job was interrupted by a restart. Start the generation again.';await persist(j);}jobs.set(j.id,j);}}
const writes=new Map();
const persist=j=>{const next=(writes.get(j.id)||Promise.resolve()).catch(()=>{}).then(()=>writeJson(path.join(data,'jobs',j.id+'.json'),j));writes.set(j.id,next);return next;};
async function update(j,values){Object.assign(j,values,{updatedAt:new Date().toISOString()});await persist(j);}
export const allJobs=()=>[...jobs.values()].sort((a,b)=>b.createdAt.localeCompare(a.createdAt));
export function getJob(id){return jobs.get(id);}
export async function validateJobMesh(id){const j=jobs.get(id);if(!j?.asset||j.asset.extension!=='.glb')throw new Error('The job has no GLB to validate.');try{const info=await assetInfo(j.asset.id),file=path.join(data,'assets',info.id+info.extension);if((await fs.stat(file)).size>maxMeshBytes)throw new Error('The mesh exceeds 100 MB and was rejected.');const validation=validateGeneratedMesh(await fs.readFile(file));await update(j,{validation});return validation;}catch(e){await update(j,{status:'failed',message:e.message,validation:{accepted:false,reason:e.message}});return j.validation;}}
export async function createJob(input,config){const j={id:randomUUID(),...input,status:'queued',progress:0,message:'Waiting in the queue',createdAt:new Date().toISOString()};jobs.set(j.id,j);await persist(j);void pump(config);return j;}
export async function cancelJob(id,config){const j=jobs.get(id);if(!j)return;if(!['queued','running'].includes(j.status))return;controllers.get(id)?.abort();if(j.promptId&&j.engine!=='pixal3d'){const queue=await jsonFetch(config.comfyUrl+'/queue').catch(()=>null);if(queue?.queue_running?.some(item=>item[1]===j.promptId))await fetch(config.comfyUrl+'/interrupt',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{});else await fetch(config.comfyUrl+'/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delete:[j.promptId]})}).catch(()=>{});}await update(j,{status:'cancelled',message:'Cancelled'});}
async function pump(config){if(pumping)return;pumping=true;try{for(;;){const j=[...jobs.values()].find(x=>x.status==='queued');if(!j)break;active=j.id;const controller=new AbortController();controllers.set(j.id,controller);try{await run(j,config,controller.signal);}catch(e){if(j.status!=='cancelled')await update(j,{status:'failed',message:e.name==='AbortError'?'Generation aborted':e.message});}finally{controllers.delete(j.id);active=null;}}}finally{pumping=false;}}
async function jsonFetch(url,options={}){const res=await fetch(url,{...options,signal:options.signal||AbortSignal.timeout(10000)});let v;try{v=await res.json();}catch{throw new Error('The ComfyUI server returned an invalid response.');}if(!res.ok)throw new Error(v.error?.message||JSON.stringify(v.node_errors||v.error||v).slice(0,700));return v;}
async function waitPrompt(j,config,workflow,signal,stage){
 let nodeClass='';const clientId=randomUUID();const socket=new WebSocket(config.comfyUrl.replace('http:','ws:')+'/ws?clientId='+clientId);
 socket.on('error',()=>{});
 socket.on('message',raw=>{try{const v=JSON.parse(raw);if(j.status!=='running'||(v.data?.prompt_id&&j.promptId&&v.data.prompt_id!==j.promptId))return;if(v.type==='progress'){const ratio=v.data.value/v.data.max,meshPhase=['VAEDecodeHunyuan3D','VoxelToMesh','AstraMarchingCubes','SaveGLB'].includes(nodeClass);void update(j,{progress:meshPhase?Math.min(98,Math.round(88+ratio*10)):Math.min(87,Math.round(ratio*87)),message:(meshPhase?'Extracting the mesh':stage)+' · '+v.data.value+'/'+v.data.max}).catch(()=>{});}if(v.type==='executing'&&v.data.node){const node=workflow[v.data.node]?.class_type;nodeClass=node;const phases={VAEDecodeHunyuan3D:[88,'Extracting the volume from the latent'],VoxelToMesh:[94,'Building the polygon mesh'],AstraMarchingCubes:[94,'Extracting the surface · Marching Cubes'],SaveGLB:[97,'Saving GLB']};if(phases[node])void update(j,{progress:phases[node][0],message:phases[node][1]}).catch(()=>{});}}catch{}});
 const gpu=await acquireEngineGpu('hunyuan',{signal,failOpen:true,timeoutMs:60*60*1000,urls:{hunyuan:config.comfyUrl},onWait:()=>void update(j,{message:'Waiting for the GPU to become free (GPU lock)'}).catch(()=>{}),onStep:text=>void update(j,{message:text}).catch(()=>{})}).catch(e=>{socket.close();throw e;});
 const deadline=Date.now()+60*60*1000;
 if(gpu.handover?.freed?.length||gpu.handover?.waited)void update(j,{handover:gpu.handover}).catch(()=>{});
 try{
  const queued=await jsonFetch(config.comfyUrl+'/prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:workflow,client_id:clientId}),signal});await update(j,{promptId:queued.prompt_id});
  while(Date.now()<deadline){signal.throwIfAborted();const history=await jsonFetch(config.comfyUrl+'/history/'+queued.prompt_id,{signal});const h=history[queued.prompt_id];
   if(h){const error=h.status?.messages?.find(x=>x[0]==='execution_error');if(error)throw new Error((error[1]?.exception_message||'Engine error').slice(0,900));if(h.status?.status_str==='error')throw new Error('ComfyUI failed to run the generation. Check runtime/comfy.log.');if(h.status?.completed)return h.outputs;}
   await new Promise((resolve,reject)=>{const done=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);};const abort=()=>{done();reject(new DOMException('Cancelled','AbortError'));};const timer=setTimeout(()=>{done();resolve();},1500);signal.addEventListener('abort',abort,{once:true});});
  }throw new Error('Generation timed out (60 minutes).');
 }finally{socket.close();await gpu.release();}
}
async function downloadOutput(config,item,extension,name){const params=new URLSearchParams({filename:item.filename,subfolder:item.subfolder||'',type:item.type||'output'});const r=await fetch(config.comfyUrl+'/view?'+params,{signal:AbortSignal.timeout(60000)});if(!r.ok)throw new Error('Failed to get the generation result.');if(Number(r.headers.get('content-length'))>maxMeshBytes){await r.body.cancel();throw new Error('The result exceeds 100 MB. Check the surface settings.');}const reader=r.body.getReader(),chunks=[];let length=0;for(;;){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>maxMeshBytes){await reader.cancel();throw new Error('The result exceeds 100 MB.');}chunks.push(Buffer.from(value));}const buffer=Buffer.concat(chunks);if(extension==='.glb')validateGeneratedMesh(buffer);return saveAsset(buffer,extension,name);}
// View-set images -> ComfyUI input. The names depend on the set and its revision: a retry with a different surface level hits the engine cache.
async function uploadViews(j,config,signal){
 const set=await getViewsService().forJob(j.viewSetId,{signal}),stamp=String(set.rev||'').replace(/[^0-9]/g,'').slice(-12),uploaded={};
 for(const [slot,file] of Object.entries(set.files)){const form=new FormData();form.append('image',new Blob([await fs.readFile(file)]),'astra-vs-'+j.viewSetId.slice(0,8)+'-'+stamp+'-'+slot+'.png');form.append('overwrite','true');uploaded[slot]=(await jsonFetch(config.comfyUrl+'/upload/image',{method:'POST',body:form,signal})).name;}
 await update(j,{viewSlots:Object.keys(uploaded)});return uploaded;
}
// Text -> concept image with SDXL in the Hunyuan ComfyUI (shared by both 3D engines). Returns the asset id of the concept.
async function conceptFromPrompt(j,config,signal,nodes){
  const cp=nodes.CheckpointLoaderSimple?.input?.required?.ckpt_name?.[0]||[];if(!cp.includes(config.imageCheckpoint))throw new Error('Text input requires an installed SDXL checkpoint.');
  let englishPrompt=j.prompt;
  if(/[^\x00-\x7F]/.test(j.prompt)){
   await update(j,{message:'Translating the concept with the local LLM',progress:0});
   const response=await llmChat({...config,provider:'ollama'},{message:'Translate the concept description into English for SDXL, keeping the meaning. Return a concept action with the English prompt. Description: '+j.prompt,context:'',history:[]});
   signal.throwIfAborted();englishPrompt=response.actions.find(a=>a.type==='concept')?.prompt;
   if(!englishPrompt)throw new Error('The local LLM did not return an English concept. Click "Refine with AI" or use an English description.');
   await update(j,{englishPrompt});
  }
  await update(j,{message:'Creating the concept image · SDXL',progress:0});
  const outputs=await waitPrompt(j,config,sdxlWorkflow(englishPrompt,config.imageCheckpoint,j.seed,'astra/'+j.id),signal,'Concept image');
  const item=Object.values(outputs).flatMap(x=>x.images||[])[0];if(!item)throw new Error('SDXL did not return an image.');
  const img=await downloadOutput(config,item,'.png','Concept');await update(j,{concept:img});return img.id;
}
// Pixal3D (docs/forge/pixal3d.md): the same job record as Hunyuan, the geometry comes from the separate ComfyUI-Pixal3D on 8191.
async function runPixal3d(j,config,signal){
 await update(j,{status:'running',message:'Checking the Pixal3D engine',progress:0});
 const cfg=pixal3dConfig(),state=await pixal3dStatus({cfg});
 if(!state.installed)throw new Error(state.message);
 if(j.reextractSourceId)throw new Error('Surface re-extraction is available for Hunyuan3D only.');
 let image=j.imageId;
 if(!image){
  let nodes;try{nodes=await jsonFetch(config.comfyUrl+'/object_info',{signal});}catch{throw new Error('A text description needs the local SDXL engine (port 8190). Open the connections and click "Start 3D engine", or upload a concept image.');}
  image=await conceptFromPrompt(j,config,signal,nodes);
 }
 await update(j,{engineUrl:cfg.url,modelCheckpoint:'TencentARC/Pixal3D'});
 let prepared=null;
 if(image){await update(j,{message:'Removing the background and centering the concept · CPU',progress:0});prepared=await prepareConcept(image,j.id,signal,j.backgroundMode||'auto');await update(j,{preparedConcept:prepared});}
 let images,removeBackground=false;
 if(j.viewSetId){
  await update(j,{message:'Preparing views for Pixal3D',progress:0});
  const set=await getViewsService().forJob(j.viewSetId,{signal});
  images=set.cutFiles;if(!images?.front)throw new Error('The set has no cut-out front view. Open the "Views" block and wait for the views to be prepared.');
  await update(j,{viewSlots:Object.keys(images)});
 }else{
  if(!prepared)throw new Error('A concept image or a description is required.');
  const info=await assetInfo(prepared.reference.id);images={front:path.join(data,'assets',info.id+info.extension)};
  removeBackground=String(j.backgroundMode||'').startsWith('keep');
 }
 const result=await generatePixal3d({images,seed:j.seed,tag:j.id.slice(0,8),signal,removeBackground,cfg,
  onProgress:({percent,message})=>{if(j.status==='running')void update(j,{progress:percent,message}).catch(()=>{});},
  onPrompt:promptId=>void update(j,{promptId}).catch(()=>{}),
  onWait:message=>{if(j.status==='running')void update(j,{message}).catch(()=>{});}});
 signal.throwIfAborted();
 const asset=await saveAsset(result.buffer,'.glb',j.viewSetId?'Pixal3D · from views':'Pixal3D · '+(j.prompt||'from image').slice(0,50));
 const validation=validateGeneratedMesh(result.buffer);
 await update(j,{status:'completed',progress:100,message:'Geometry created · Pixal3D · needs review',asset,validation,pixal3d:{seconds:result.seconds,slots:result.slots,targetFaces:result.targetFaces,vramPeakMb:result.vram?.peak_used_mb??null,vramBaselineMb:result.vram?.baseline_used_mb??null,scale:result.normalization.scale,handover:result.handover}});
}
async function run(j,config,signal){
 if(j.engine==='pixal3d')return runPixal3d(j,config,signal);
 await update(j,{status:'running',message:'Checking the 3D engine',progress:0});
 let nodes;try{nodes=await jsonFetch(config.comfyUrl+'/object_info',{signal});}catch{throw new Error('The local 3D engine is not running. Open the connections and click "Start 3D engine".');}
 const names=nodes.ImageOnlyCheckpointLoader?.input?.required?.ckpt_name?.[0]||[],checkpoint=j.viewSetId?MV_CHECKPOINT:j.shapeModel==='hunyuan21'?'hunyuan_3d_v2.1.safetensors':config.shapeCheckpoint;
 if(!names.includes(checkpoint))throw new Error(j.viewSetId?'Geometry from views requires the model '+MV_CHECKPOINT+' (Hunyuan3D-2mv). It is not installed or is still downloading.':'The selected Hunyuan3D model is not installed or is still downloading. Check the connections.');
 await update(j,{modelCheckpoint:checkpoint});
 for(const node of [j.viewSetId?'Hunyuan3Dv2ConditioningMultiView':'Hunyuan3Dv2Conditioning','SaveGLB','AstraMarchingCubes'])if(!nodes[node])throw new Error('ComfyUI is missing the node '+node+'. Start the AstraForge 3D engine on port 8190.');
 let image=j.imageId;
 if(!image&&!j.reextractSourceId)image=await conceptFromPrompt(j,config,signal,nodes);
 let prepared,geometryImage;
 if(j.reextractSourceId){const source=jobs.get(j.reextractSourceId);if(!source?.preparedConcept||!source.promptId)throw new Error('There is no saved geometry source.');const history=await jsonFetch(config.comfyUrl+'/history/'+source.promptId,{signal}),workflow=history[source.promptId]?.prompt?.[2],geometrySourceId=source.geometrySourceId||source.id;geometryImage=source.geometryImage||workflow?.['2']?.inputs?.image||("astra-"+geometrySourceId+".png");if(typeof geometryImage!=='string'||geometryImage!=='astra-'+geometrySourceId+'.png')throw new Error('The source is missing from the cache. Repeat the generation from the image.');await fs.access(path.join(data,"runtime","input",geometryImage));prepared=source.preparedConcept;await update(j,{preparedConcept:prepared,message:'Re-extracting the mesh · saved input',geometryImage,geometrySourceId});}
 else{await update(j,{message:'Removing the background and centering the concept · CPU',progress:0});prepared=await prepareConcept(image,j.id,signal,j.backgroundMode||'auto');await update(j,{preparedConcept:prepared});const info=await assetInfo(prepared.id);const buffer=await fs.readFile(path.join(data,'assets',info.id+info.extension));const form=new FormData();form.append('image',new Blob([buffer]),'astra-'+j.id+info.extension);form.append('overwrite','true');const uploaded=await jsonFetch(config.comfyUrl+'/upload/image',{method:'POST',body:form,signal});geometryImage=uploaded.name;await update(j,{geometryImage});}
 let viewImages=null;
 if(j.viewSetId){await update(j,{message:'Preparing views for Hunyuan3D-2mv',progress:0});viewImages=await uploadViews(j,config,signal);}
 await update(j,{message:viewImages?'Creating geometry from views · Hunyuan3D-2mv ('+Object.keys(viewImages).length+' views)':'Creating geometry · Hunyuan3D',progress:0});
 const outputs=await waitPrompt(j,config,viewImages?hunyuanMultiViewWorkflow(viewImages,checkpoint,j.quality,j.seed,'astra/'+j.id,j.meshLevel??0):hunyuanWorkflow(geometryImage,checkpoint,j.quality,j.seed,'astra/'+j.id,j.meshLevel??0),signal,viewImages?'Geometry from views':'Geometry');signal.throwIfAborted();
 const item=Object.values(outputs).flatMap(x=>x['3d']||[])[0];if(!item)throw new Error('The engine did not return a GLB.');
 const asset=await downloadOutput(config,item,'.glb',(j.viewSetId?'Hunyuan3D-2mv · from views':'Hunyuan3D · '+j.prompt.slice(0,50)));const validation=validateGeneratedMesh(await fs.readFile(path.join(data,'assets',asset.id+asset.extension)));await update(j,{status:'completed',progress:100,message:'Geometry created · needs review',asset,validation});
}
