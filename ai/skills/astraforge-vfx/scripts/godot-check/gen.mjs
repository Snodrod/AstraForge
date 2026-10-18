// Writes a minimal Godot project with AstraForge effects built through src/fx-core.js (the same code the studio uses for the ZIP).
import fs from 'node:fs';import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const APP=process.env.ASTRA_APP||fileURLToPath(new URL('../../../../../',import.meta.url)); // APP/ai/skills/astraforge-vfx/scripts/godot-check
const fx=await import(pathToFileURL(path.join(APP,'src','fx-core.js')).href);
const PROJ=process.argv[2]||path.join(process.env.TEMP||'.','af_fx_check');const P=p=>path.join(PROJ,p);
fs.rmSync(P(''),{recursive:true,force:true});fs.mkdirSync(P('fx/textures'),{recursive:true});fs.mkdirSync(P('_home/Roaming'),{recursive:true});fs.mkdirSync(P('_home/Local'),{recursive:true});fs.writeFileSync(P('_home/.gdignore'),'');
fs.writeFileSync(P('project.godot'),'; Engine configuration file.\nconfig_version=5\n\n[application]\n\nconfig/name="af_fx_check"\nconfig/features=PackedStringArray("4.5", "GL Compatibility")\n\n[debug]\n\nfile_logging/enable_file_logging=false\n\n[rendering]\n\nrenderer/rendering_method="gl_compatibility"\nrenderer/rendering_method.mobile="gl_compatibility"\n');
fs.copyFileSync(new URL('./check.gd',import.meta.url),P('check.gd'));
const tf=(s=[1,1,1])=>({position:[0,1,0],rotation:[0,0,0],quaternion:[0,0,0,1],scale:s});
const ids=fx.FX_PRESET_LIST.filter(p=>p.id!=='empty').map(p=>p.id);
const items=ids.map((id,i)=>({name:'FX · '+fx.presetTitle(id),fx:fx.makeFx(id,i+1),node:'fx_'+id,nodePath:'AstraForge/fx_'+id,parent:null,transform:tf()}));
// one effect with edits: ring, light, one_shot, local_coords, 4x4 flipbook on its own texture, scale 2
let edited=fx.makeFx('magic_cast',99);
edited=fx.patchFx(edited,{params:{amount:64,lifetime:1.2,one_shot:true,autostart:false,local_coords:true,explosiveness:.4,randomness:.5,emission:{shape:'ring',radius:.5,inner_radius:.2,height:.05,axis:[0,0,1]},light:{color:[.2,.9,1],energy:3,range:4},blend:'mix',turbulence:1.5,gravity:[0,-3,0],damping:[.2,.6],angle:[0,90],angular_velocity:[-45,45]},textureAsset:{id:'sheet',url:'/api/assets/sheet.png',name:'sheet.png'},flipbook:{h:4,v:4,fps:16}});
items.push({name:'FX · Edited',fx:edited,node:'fx_edited',nodePath:'AstraForge/Sword/fx_edited',parent:'Sword',transform:{position:[0,.3,0],rotation:[0,.5,0],quaternion:[0,.247404,0,.968912],scale:[2,2,2]}});
const box=fx.patchFx(fx.makeFx('empty',100),{params:{emission:{shape:'box',extents:[.3,.1,.3]},spread:180,velocity:[0,0],scale_curve:[[0,0],[.5,2.5],[1,0]],color_ramp:[[0,[1,0,0,0]],[.3,[1,1,0,1]],[.7,[0,1,1,1]],[1,[0,0,1,0]]]},texture:'smoke'});
items.push({name:'FX · Box',fx:box,node:'fx_box',nodePath:'AstraForge/fx_box',parent:null,transform:tf()});
const pkg=fx.buildFxPackage(items);
// 128x128 frame sheet (4x4 of 32 px) with the frame number in the brightness — substituted for downloading the asset
const sheet=new Uint8Array(128*128*4);for(let y=0;y<128;y++)for(let x=0;x<128;x++){const f=((y>>5)<<2)+(x>>5),o=(y*128+x)*4,r=Math.hypot((x&31)-15.5,(y&31)-15.5)/16;sheet[o]=255;sheet[o+1]=Math.round(255*f/15);sheet[o+2]=128;sheet[o+3]=Math.round(255*Math.max(0,1-r));}
let n=0;for(const f of pkg.files){if(f.text!==undefined)fs.writeFileSync(P(f.path),f.text);else if(f.bytes)fs.writeFileSync(P(f.path),f.bytes);else if(f.url){fs.writeFileSync(P(f.path),fx.encodePng(128,128,sheet));}n++;}
console.log('files written:',n,'| scenes:',pkg.files.filter(f=>f.path.endsWith('.tscn')).length,'| warnings:',JSON.stringify(pkg.warnings));
