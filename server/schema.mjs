import { z } from 'zod';
export const localUrl=z.string().url().refine(v=>{try {const u=new URL(v);return u.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(u.hostname)&&!u.username&&!u.password&&!u.search&&!u.hash;}catch{return false;}},'Use a local HTTP address (127.0.0.1 or localhost).');
export const configSchema=z.object({ollamaUrl:localUrl,ollamaModel:z.string().max(150),comfyUrl:localUrl,shapeCheckpoint:z.string().max(200),imageCheckpoint:z.string().max(200),provider:z.enum(['ollama','openai','anthropic']),openaiModel:z.string().max(100),anthropicModel:z.string().max(100)}).strict();
export const generationSchema=z.object({imageId:z.string().uuid().optional(),prompt:z.string().max(4000).default(''),quality:z.enum(['draft','standard','high']).default('standard'),shapeModel:z.enum(['default','hunyuan21']).default('default'),meshLevel:z.number().finite().min(-1).max(1).default(0),backgroundMode:z.enum(['auto','alpha','keep']).default('auto'),hazeFilter:z.enum(['auto','on','off']).default('auto'),seed:z.number().int().min(0).max(2147483647).default(42),
 // View set (server/views.mjs): geometry from front/left/back/right in Hunyuan3D-2mv; imageId is then the front view of the set.
 viewSetId:z.string().uuid().optional(),
 // Shape engine: 'hunyuan' (default - the previous API behaviour), 'pixal3d' (docs/forge/pixal3d.md) or 'auto' (Pixal3D if installed, otherwise Hunyuan).
 // For 'pixal3d' the quality, shapeModel and meshLevel fields are not used.
 engine:z.enum(['auto','hunyuan','pixal3d']).default('hunyuan')}).refine(x=>x.imageId||x.viewSetId||x.prompt.trim(),'Upload a concept or write a description.')
 // The exhaust haze filter is passed to scripts/prepare_concept.py inside backgroundMode: '<background>:haze=<on|off>' ('auto' is the Python default).
 .transform(x=>x.hazeFilter==='auto'?x:{...x,backgroundMode:`${x.backgroundMode}:haze=${x.hazeFilter}`});
export const chatSchema=z.object({message:z.string().min(1).max(6000),context:z.string().max(15000).default(''),history:z.array(z.object({role:z.enum(['user','assistant']),content:z.string().max(6000)})).max(12).default([])});
export const defaults={ollamaUrl:'http://127.0.0.1:11434',ollamaModel:'qwen3:8b',comfyUrl:'http://127.0.0.1:8190',shapeCheckpoint:'hunyuan3d-dit-v2.safetensors',imageCheckpoint:'sd_xl_base_1.0.safetensors',provider:'ollama',openaiModel:'',anthropicModel:''};
export function hunyuanWorkflow(image,checkpoint,quality,seed,prefix,meshLevel=0){
 const cfg={draft:{steps:15,res:128},standard:{steps:25,res:256},high:{steps:45,res:512}}[quality];
 return {
 '1':{class_type:'ImageOnlyCheckpointLoader',inputs:{ckpt_name:checkpoint}},
 '2':{class_type:'LoadImage',inputs:{image}},
 '3':{class_type:'CLIPVisionEncode',inputs:{clip_vision:['1',1],image:['2',0],crop:'none'}},
 '4':{class_type:'Hunyuan3Dv2Conditioning',inputs:{clip_vision_output:['3',0]}},
 '5':{class_type:'EmptyLatentHunyuan3Dv2',inputs:{resolution:/v2[._]1/i.test(checkpoint)?4096:3072,batch_size:1}},
 '6':{class_type:'KSampler',inputs:{model:['1',0],positive:['4',0],negative:['4',1],latent_image:['5',0],seed,steps:cfg.steps,cfg:5,sampler_name:'euler',scheduler:'simple',denoise:1}},
 '7':{class_type:'VAEDecodeHunyuan3D',inputs:{samples:['6',0],vae:['1',2],num_chunks:quality==='high'?4000:8000,octree_resolution:cfg.res}},
 // Comfy's VAE wrapper converts and clamps the signed field with (x + 1) / 2.
 '8':{class_type:'AstraMarchingCubes',inputs:{voxel:['7',0],level:(meshLevel+1)/2}},
 '9':{class_type:'SaveGLB',inputs:{mesh:['8',0],filename_prefix:prefix}}};
}
// Hunyuan3D-2mv: front / left / back / right (any subset, front is required) -> one GLB.
// Same nodes as hunyuanWorkflow; the conditioning is Hunyuan3Dv2ConditioningMultiView and every view has its own CLIP-vision encode.
// images: {front:'uploaded.png', left?, back?, right?} (names inside ComfyUI's input folder).
export const MV_CHECKPOINT='hunyuan3d-dit-v2-mv.safetensors';
export const MV_SLOTS=['front','left','back','right'];
export function hunyuanMultiViewWorkflow(images,checkpoint,quality,seed,prefix,meshLevel=0){
 if(!images?.front)throw new Error('A front view is required for geometry from views.');
 const w=hunyuanWorkflow(images.front,checkpoint,quality,seed,prefix,meshLevel);
 const ids={front:['2','3'],left:['22','23'],back:['12','13'],right:['32','33']},cond={};
 for(const slot of MV_SLOTS){
  if(!images[slot])continue;
  const [load,encode]=ids[slot];
  w[load]={class_type:'LoadImage',inputs:{image:images[slot]}};
  w[encode]={class_type:'CLIPVisionEncode',inputs:{clip_vision:['1',1],image:[load,0],crop:'none'}};
  cond[slot]=[encode,0];
 }
 w['4']={class_type:'Hunyuan3Dv2ConditioningMultiView',inputs:cond};
 return w;
}
// Pixal3D (TencentARC, MIT) shape generation in the separate ComfyUI-Pixal3D (port 8191): SHAPE ONLY.
// The texturing stage of TRELLIS.2/Pixal3D needs NVIDIA nvdiffrast (non-commercial licence), so no node of it may ever appear here;
// tests/pixal3d.test.mjs guards that. Settings are the ones chosen on the militia evaluation (docs/forge/pixal3d.md):
// heun sampler, 700k faces, fixed FOV 0.1 rad for several views, MoGe camera for one view.
// images: {front:'a.png', left?, back?, right?} = RGBA cut-outs (names inside the Pixal3D ComfyUI input folder); front is required.
// With only the front view the single-view graph is built, otherwise the multi-view one (azimuths 0/90/180/270 = front/left/back/right).
export const PIXAL3D_MODEL='TencentARC/Pixal3D';
export const PIXAL3D_AZIMUTH={front:0,left:90,back:180,right:270};
export function pixal3dWorkflow(images,{seed=1000,prefix='astra/pixal3d',targetFaces=700000,sampler='heun',fovRad=0.1,removeBackground=false}={}){
 if(!images?.front)throw new Error('Pixal3D requires a front view.');
 const slots=MV_SLOTS.filter(slot=>images[slot]),multi=slots.length>1;
 const g={};
 g['1']={class_type:'Trellis2LoadModel',inputs:{modelname:PIXAL3D_MODEL,backend:'flash_attn',device:'cuda',low_vram:true,keep_models_loaded:false,conv_backend:'flex_gemm',sparse_backend:'flash_attn',use_reconviagen:false,pixal3d_multiview:multi}};
 g['2']={class_type:'Trellis2LoadImageWithTransparency',inputs:{image:images.front}};
 g['3']={class_type:'Trellis2PreProcessImage',inputs:{image:['2',2],padding:5,remove_background:removeBackground,max_size:1024}};
 g['4']={class_type:'Trellis2ImageCondGenerator',inputs:{pipeline:['1',0],image:['3',0],max_views:1}};
 const sparse={pipeline:['4',2],image_cond:['4',0],seed,sparse_structure_steps:25,sparse_structure_guidance_strength:7.5,sparse_structure_guidance_rescale:0.7,sparse_structure_rescale_t:5,sparse_structure_sampler:sampler,sparse_structure_resolution:32,sparse_structure_guidance_interval_start:0.6,sparse_structure_guidance_interval_end:1,fill_holes:true,hole_iterations:1,verbose:false,dino_lock:0.65,dino_substeps:2,hole_fill_algorithm:'flood_fill',dino_foundation_cap:0.92,keep_only_shell:true};
 const shape={shape_steps:12,shape_guidance_strength:7.5,shape_guidance_rescale:0.5,shape_rescale_t:3,shape_sampler:sampler,shape_guidance_interval_start:0.6,shape_guidance_interval_end:1,verbose:false,dino_lock:0.65,dino_substeps:2,dino_foundation_cap:0.92};
 let camera={moge_camera_config:['4',3]};
 if(multi){
  const loads={front:'30',left:'31',back:'32',right:'33'};
  for(const slot of slots)g[loads[slot]]={class_type:'Trellis2LoadImageWithTransparency',inputs:{image:images[slot]}};
  // views are batched in a fixed order (front, left, back, right) and the azimuth list follows the same order
  let batch=[loads[slots[0]],2];
  slots.slice(1).forEach((slot,i)=>{const id=String(34+i);g[id]={class_type:'ImageBatch',inputs:{image1:[batch[0],batch[1]],image2:[loads[slot],2]}};batch=[id,0];});
  g['37']={class_type:'Trellis2FovMoGeCameraConfig',inputs:{fov:fovRad,unit:'rad',extend_pixel:0,mesh_scale:1}};
  g['38']={class_type:'Trellis2Pixal3DMultiViewConfig',inputs:{images:batch,azimuths:slots.map(s=>PIXAL3D_AZIMUTH[s]).join(','),elevations:slots.map(()=>0).join(','),fov:20,fov_unit:'deg',mesh_scale:1,distance:0,remove_background:false,invert_mask:false,framing:'auto',moge_camera_config:['37',0]}};
  camera={moge_camera_config:['38',1],pixal3d_mv_views:['38',0]};
 }
 g['5']={class_type:'Trellis2SparseGenerator',inputs:{...sparse,...camera,image:['3',0]}};
 g['6']={class_type:'Trellis2ShapeGenerator',inputs:{...shape,pipeline:['5',2],image_cond:['4',0],coords:['5',0],resolution:512,image:['3',0],...camera}};
 g['7']={class_type:'Trellis2ShapeCascadeGenerator',inputs:{...shape,pipeline:['6',2],image_cond:['4',1],shape_slat:['6',0],from_resolution:['6',1],to_resolution:1024,sparse_structure_resolution:['5',1],max_num_tokens:999999,image:['3',0],...camera}};
 g['20']={class_type:'Trellis2DecodeLatents',inputs:{pipeline:['7',2],shape_slat:['7',0],resolution:['7',1],use_tiled_decoder:true}};
 g['21']={class_type:'Trellis2FillHolesWithCuMesh',inputs:{mesh:['20',0],max_permieters:1}};
 g['22']={class_type:'Trellis2ReconstructMeshWithQuad',inputs:{mesh:['21',0],remesh_band:1,resolution:1024,remove_floaters:true,remove_inner_faces:true}};
 g['23']={class_type:'Trellis2SimplifyMesh',inputs:{mesh:['22',0],target_face_num:targetFaces,method:'Cumesh'}};
 g['24']={class_type:'Trellis2FillHolesWithCuMesh',inputs:{mesh:['23',0],max_permieters:1}}; // CuMesh (MIT), not meshlib: meshlib has a non-commercial-only license
 g['25']={class_type:'Trellis2SimplifyMesh',inputs:{mesh:['24',0],target_face_num:targetFaces,method:'Cumesh'}};
 g['26']={class_type:'Trellis2MeshWithVoxelToTrimesh',inputs:{mesh:['25',0],reorient_vertices:'None'}};
 g['27']={class_type:'Trellis2ExportMesh',inputs:{trimesh:['26',0],filename_prefix:prefix,file_format:'glb'}};
 return g;
}
export function sdxlWorkflow(prompt,checkpoint,seed,prefix){return {
 '1':{class_type:'CheckpointLoaderSimple',inputs:{ckpt_name:checkpoint}},
 '2':{class_type:'CLIPTextEncode',inputs:{clip:['1',1],text:'single product photograph, exactly one object, one view only, '+prompt+', full object visible, isolated on solid white background, centered composition, clean silhouette'}},
 '3':{class_type:'CLIPTextEncode',inputs:{clip:['1',1],text:'contact sheet, collage, grid, tiles, panels, multiple views, multi-view, different angles, variations, comparison, split screen, storyboard, text, watermark, cropped, multiple objects, blurry, scene, background, pedestal'}},
 '4':{class_type:'EmptyLatentImage',inputs:{width:768,height:768,batch_size:1}},
 '5':{class_type:'KSampler',inputs:{model:['1',0],positive:['2',0],negative:['3',0],latent_image:['4',0],seed,steps:25,cfg:6,sampler_name:'dpmpp_2m',scheduler:'karras',denoise:1}},
 '6':{class_type:'VAEDecode',inputs:{samples:['5',0],vae:['1',2]}},
 '7':{class_type:'SaveImage',inputs:{images:['6',0],filename_prefix:prefix}}};}
