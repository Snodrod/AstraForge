import {test} from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import zlib from 'node:zlib';import {fileURLToPath} from 'node:url';
import * as fx from '../src/fx-core.js';import {FX_PRESETS_DATA,AF_FX_MOUNT_GD} from '../src/fx-data.js';import {FX_PRESETS as FORGE_FX} from '../src/forge/forge-core.js';
const root=new URL('../',import.meta.url),read=p=>fs.readFileSync(new URL(p,root),'utf8');
const IDS=FX_PRESETS_DATA&&Object.keys(FX_PRESETS_DATA.presets);
// Alpha-channel sums of the 64x64 textures from the Python reference (_export_lib.make_fx_texture, numpy 2.4): the JS port produces bit-identical images.
const PY_ALPHA_SUM={soft:132360,spark:64004,star:99652,smoke:153580,ring:298828};

// The data copy in src/fx-data.js is needed because the Vite server does not serve scripts/ to the browser. FX_SYNC=1 overwrites the copy.
test('fx-data: the copy matches fx_presets.json and af_fx_mount.gd (FX_SYNC=1 updates it)',()=>{
 const json=JSON.parse(read('scripts/blender/data/fx_presets.json')),gd=read('scripts/blender/godot/af_fx_mount.gd').replace(/\r\n/g,'\n');
 if(process.env.FX_SYNC){const head=read('src/fx-data.js').split('\n').slice(0,2).join('\n');fs.writeFileSync(fileURLToPath(new URL('src/fx-data.js',root)),head+'\nexport const FX_PRESETS_DATA='+JSON.stringify(json)+';\nexport const AF_FX_MOUNT_GD='+JSON.stringify(gd)+';\n');return;}
 assert.deepEqual(FX_PRESETS_DATA,json,'src/fx-data.js is out of date: FX_SYNC=1 node --test tests/fx-core.test.mjs');
 assert.equal(AF_FX_MOUNT_GD.replace(/\r\n/g,'\n'),gd,'af_fx_mount.gd changed: FX_SYNC=1 node --test tests/fx-core.test.mjs');
 assert.equal(FX_PRESETS_DATA.format,fx.FX_FORMAT);});

test('all 12 Forge presets are present in the data and normalize without corrections',()=>{
 assert.equal(IDS.length,12);assert.deepEqual(FORGE_FX.map(p=>p.id).sort(),[...IDS].sort());
 assert.equal(fx.FX_PRESET_LIST.length,13);assert.equal(fx.FX_PRESET_LIST.at(-1).id,'empty');
 for(const id of IDS){const raw=FX_PRESETS_DATA.presets[id];assert.deepEqual(fx.validateParams(raw),[],id);const p=fx.normalizeParams(raw);
  assert.ok(p.amount>=1&&p.amount<=fx.FX_LIMITS.amount,id);assert.ok(p.lifetime>0,id);assert.ok(p.color_ramp.length>=2&&p.scale_curve.length>=2,id);assert.ok(fx.FX_TEXTURES.includes(p.texture)&&fx.FX_BLENDS.includes(p.blend),id);
  assert.equal(p.amount,raw.amount);assert.equal(p.lifetime,raw.lifetime);assert.equal(p.quad_size,raw.quad_size);
  const r=fx.resolveFx(fx.makeFx(id,5));assert.equal(r.preset,id);assert.equal(r.texture,raw.texture);assert.equal(!!r.light,!!raw.light);assert.deepEqual(fx.patchFx(fx.makeFx(id,5),{}).params,{});}
 assert.equal(fx.presetTitle('fire'),'Fire');assert.equal(fx.presetId('no-such-id'),'empty');});

test('merging edits: only the difference from the preset is stored, the source object is not modified',()=>{
 const a=fx.makeFx('fire',1),before=JSON.stringify(a);const b=fx.patchFx(a,{params:{amount:100,blend:'mix',lifetime:0.8}});
 assert.equal(JSON.stringify(a),before);assert.deepEqual(b.params,{amount:100,blend:'mix'});assert.notEqual(b,a);assert.notEqual(b.params,a.params);
 const r=fx.resolveFx(b);assert.equal(r.amount,100);assert.equal(r.blend,'mix');assert.equal(r.lifetime,0.8);assert.equal(r.spread,12);
 const c=fx.patchFx(b,{params:{amount:48}});assert.deepEqual(c.params,{blend:'mix'});
 const d=fx.patchFx(b,{unset:['blend']});assert.deepEqual(d.params,{amount:100});
 const e=fx.patchFx(b,{params:{emission:{shape:'ring',radius:.4,inner_radius:.1,height:.02}},texture:'ring',flipbook:{h:4,v:4,fps:24}});assert.equal(fx.resolveFx(e).emission.shape,'ring');assert.equal(fx.resolveFx(e).texture,'ring');assert.deepEqual(fx.resolveFx(e).flipbook,{h:4,v:4,fps:24});
 const f=fx.patchFx(e,{params:{light:null}});assert.equal(fx.resolveFx(f).light,null);assert.equal(fx.resolveFx(fx.patchFx(f,{params:{light:{color:[1,.6,.25],energy:1.5,range:3}}})).light.energy,1.5);assert.ok(!('light' in fx.patchFx(f,{params:{light:{color:[1,.6,.25],energy:1.5,range:3}}}).params));
 const g=fx.patchFx(e,{preset:'ice'});assert.equal(g.preset,'ice');assert.deepEqual(g.params,{});assert.equal(g.texture,'star');assert.equal(g.flipbook,null);
 const h=fx.patchFx(e,{reset:true});assert.deepEqual(h.params,{});assert.equal(h.texture,'soft');assert.equal(h.flipbook,null);
 assert.equal(fx.patchFx(a,{textureAsset:{id:'1',url:'/api/x.png',name:'x.png'}}).textureAsset.url,'/api/x.png');assert.equal(fx.patchFx(a,{textureAsset:{url:''}}).textureAsset,null);});

test('invalid values are clamped, extras are dropped',()=>{
 const p=fx.normalizeParams({amount:99999,lifetime:-3,spread:999,velocity:[5,1],direction:[0,0,0],blend:'xor',texture:'nope',explosiveness:7,randomness:'abc',quad_size:NaN,scale:[Infinity,2],gravity:['1',null,'x'],color_ramp:[[3,[9,-1,.5,2]]],scale_curve:[],emission:{shape:'cone',radius:-5,inner_radius:99,axis:[0,0,0]},light:{energy:1e9,range:0},unknown_field:1,turbulence:-1,fixed_fps:0.2,speed_scale:0});
 assert.equal(p.amount,4000);assert.equal(p.lifetime,.02);assert.equal(p.spread,180);assert.deepEqual(p.velocity,[1,5]);assert.deepEqual(p.direction,[0,1,0]);assert.equal(p.blend,'add');assert.equal(p.texture,'soft');assert.equal(p.explosiveness,1);assert.equal(p.randomness,0);assert.equal(p.quad_size,.2);
 assert.ok(p.scale.every(Number.isFinite)&&p.scale[0]<=p.scale[1]);assert.deepEqual(p.gravity,[1,0,0]);assert.equal(p.color_ramp.length,2);assert.deepEqual(p.scale_curve,[[0,1],[1,1]]);
 assert.equal(p.emission.shape,'point');assert.equal(p.emission.radius,0);assert.equal(p.emission.inner_radius,0);assert.deepEqual(p.emission.axis,[0,1,0]);assert.equal(p.light.energy,16);assert.equal(p.light.range,.1);assert.ok(!('unknown_field' in p));assert.equal(p.turbulence,0);assert.equal(p.fixed_fps,1);assert.equal(p.speed_scale,.05);
 const bad=fx.validateParams({amount:99999,mystery:1,blend:'xor',lifetime:1});assert.deepEqual(bad.map(x=>x.key).sort(),['amount','blend','mystery']);
 const r=fx.resolveFx({preset:'fire',params:{amount:'7',lifetime:'x'},texture:'bad',flipbook:{h:1,v:1},textureAsset:{url:5},seed:-3});assert.equal(r.amount,7);assert.equal(r.lifetime,.8);assert.equal(r.texture,'soft');assert.equal(r.flipbook,null);assert.equal(r.textureAsset,null);assert.equal(r.seed,0);
 assert.equal(fx.resolveFx(null).preset,'empty');assert.equal(fx.resolveFx({preset:'unknown'}).preset,'empty');assert.equal(fx.normalizeParams(null).amount,16);
 const ramp=fx.normalizeParams({color_ramp:[[1,[0,0,0,0]],[0,[1,1,1]],[.5,[.5,.5,.5,.5]]]}).color_ramp;assert.deepEqual(ramp.map(x=>x[0]),[0,.5,1]);assert.deepEqual(ramp[0][1],[1,1,1,1]);
 assert.equal(fx.normalizeParams({color_ramp:Array.from({length:20},(_,i)=>[i/20,[1,1,1,1]])}).color_ramp.length,8);});

test('a particle is deterministic in time and seed',()=>{
 const c=fx.compileFx(fx.resolveFx(fx.makeFx('fire',9)));const a={},b={};
 for(let i=0;i<c.amount;i++)for(const t of [0,.37,1.5,12.9,-3.2]){fx.particleAt(c,9,i,t,a);fx.particleAt(c,9,i,t,b);assert.deepEqual({...a,_col:0},{...b,_col:0});}
 fx.particleAt(c,9,5,1.234,a);fx.particleAt(c,10,5,1.234,b);assert.notDeepEqual([a.x,a.y,a.z],[b.x,b.y,b.z]);
 const s1=fx.makeSpawn(c,9,4,2),s2=fx.makeSpawn(c,9,4,2),s3=fx.makeSpawn(c,9,4,3);assert.deepEqual(s1,s2);assert.notDeepEqual(s1,s3);
 assert.equal(fx.hash3(1,2,3),fx.hash3(1,2,3));assert.notEqual(fx.hash3(1,2,3),fx.hash3(1,3,2));assert.equal(fx.seedFromString('abc'),fx.seedFromString('abc'));
 const rng=fx.mulberry32(42),v=Array.from({length:5},rng),rng2=fx.mulberry32(42);assert.deepEqual(v,Array.from({length:5},rng2));assert.ok(v.every(x=>x>=0&&x<1));});

test('particle age is always in [0, lifetime); a loop keeps all amount particles; one_shot plays once',()=>{
 for(const id of IDS){const r=fx.resolveFx(fx.makeFx(id,3)),c=fx.compileFx(r),tm={cycle:0,age:0};let alive=0,total=0;
  for(const t of [0,.013,.2,.9,3.3,17.77])for(let i=0;i<c.amount;i++){total++;if(fx.particleTiming(c,3,i,t,tm)){alive++;assert.ok(tm.age>=0&&tm.age<c.lifetime,id+' age '+tm.age);}}
  assert.ok(alive>0,id+': no live particles');
  if(!c.one_shot&&c.explosiveness===0&&c.randomness===0)assert.equal(alive,total,id+': a loop without explosiveness is always alive');
  if(c.one_shot){let before=0,after=0;for(let i=0;i<c.amount;i++){if(fx.particleTiming(c,3,i,-.01,tm))before++;if(fx.particleTiming(c,3,i,c.lifetime*2+5,tm))after++;}assert.equal(before,0);assert.equal(after,0,id+': one_shot lives once');}}
 const burst=fx.compileFx(fx.resolveFx(fx.makeFx('muzzle_flash',1))),tm={};let n0=0;for(let i=0;i<burst.amount;i++)if(fx.particleTiming(burst,1,i,0.001,tm))n0++;assert.equal(n0,burst.amount,'explosiveness=1: all particles are born at once');
 const half=fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx('sparks',1),{params:{explosiveness:0,amount:100}})));let live=0;for(let i=0;i<100;i++)if(fx.particleTiming(half,1,i,.3,tm))live++;assert.equal(live,100);
 const speed=fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx('fire',1),{params:{speed_scale:2}})));const a={},b={},base=fx.compileFx(fx.resolveFx(fx.makeFx('fire',1)));fx.particleAt(speed,1,3,.4,a);fx.particleAt(base,1,3,.8,b);assert.ok(Math.abs(a.u-b.u)<1e-9,'speed_scale=2 doubles the time speed');});

test('motion: direction, gravity, damping, radial acceleration, turbulence',()=>{
 const mk=(params,preset='empty')=>fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx(preset,1),{params})));const o={};
 const up=mk({amount:1,lifetime:2,velocity:[2,2],spread:0,direction:[0,1,0],emission:{shape:'point'},damping:[0,0],gravity:[0,0,0]});fx.particleAt(up,1,0,1,o);const age=o.age;assert.ok(Math.abs(o.y-2*age)<1e-9&&o.x===0&&o.z===0);
 const east=mk({amount:1,lifetime:2,velocity:[1,1],spread:0,direction:[1,0,0],emission:{shape:'point'}});fx.particleAt(east,1,0,1,o);assert.ok(o.x>0&&Math.abs(o.y)<1e-9);
 const grav=mk({amount:1,lifetime:2,velocity:[0,0],spread:0,emission:{shape:'point'},gravity:[0,-10,0]});fx.particleAt(grav,1,0,1,o);assert.ok(Math.abs(o.gy+5*o.age*o.age)<1e-9);
 assert.equal(fx.dampedDistance(4,2,1),3);assert.equal(fx.dampedDistance(4,2,5),4);assert.equal(fx.dampedDistance(-4,2,5),-4);assert.equal(fx.dampedDistance(3,0,2),6);
 const far=mk({amount:1,lifetime:3,velocity:[3,3],spread:0,damping:[0,0]}),near=mk({amount:1,lifetime:3,velocity:[3,3],spread:0,damping:[6,6]});const a={},b={};fx.particleAt(far,1,0,2,a);fx.particleAt(near,1,0,2,b);assert.ok(b.y<a.y);
 const base={amount:1,lifetime:2,velocity:[1,1],spread:180,emission:{shape:'sphere',radius:.5}},out=mk({...base,radial_accel:[4,4]}),inn=mk({...base,radial_accel:[-4,-4]}),rest=mk(base);let r0=0,r1=0,r2=0;
 for(let i=0;i<30;i++){const c1={},c2={},c3={};fx.particleAt(out,i,0,1.5,c1);fx.particleAt(inn,i,0,1.5,c2);fx.particleAt(rest,i,0,1.5,c3);r0+=Math.hypot(c1.x,c1.y,c1.z);r1+=Math.hypot(c2.x,c2.y,c2.z);r2+=Math.hypot(c3.x,c3.y,c3.z);}assert.ok(r0>r2&&r2>r1,'radial acceleration pushes apart/pulls together');
 assert.ok(mk({tangential_accel:[2,2],radial_accel:[0,0],amount:1}).stepped);assert.ok(!rest.stepped);
 const tur=mk({amount:1,velocity:[0,0],emission:{shape:'point'},turbulence:1}),calm=mk({amount:1,velocity:[0,0],emission:{shape:'point'}});let moved=0;for(let i=0;i<10;i++){const q={},w={};fx.particleAt(tur,i,0,.9,q);fx.particleAt(calm,i,0,.9,w);moved+=Math.hypot(q.x-w.x,q.y-w.y,q.z-w.z);}assert.ok(moved>.1);
 const big=fx.compileFx(fx.resolveFx(fx.makeFx('fire',1)),3),small=fx.compileFx(fx.resolveFx(fx.makeFx('fire',1)),1);assert.ok(Math.abs(big.quad_size-small.quad_size*3)<1e-9);assert.ok(Math.abs(big.gravity[1]-small.gravity[1]*3)<1e-9);});

test('emission shapes produce points within the given bounds',()=>{
 const mk=em=>fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx('empty',1),{params:{emission:em,velocity:[0,0],amount:200}})));
 const maxR=(c,f)=>{let m=0,mn=1e9;for(let i=0;i<200;i++){const s=fx.makeSpawn(c,1,i,0),v=f(s);m=Math.max(m,v);mn=Math.min(mn,v);}return [mn,m];};
 const sphere=mk({shape:'sphere',radius:.5});assert.ok(maxR(sphere,s=>Math.hypot(s.px,s.py,s.pz))[1]<=.5+1e-9);
 const surf=mk({shape:'sphere_surface',radius:.5}),[a,b]=maxR(surf,s=>Math.hypot(s.px,s.py,s.pz));assert.ok(a>.5-1e-9&&b<.5+1e-9);
 const box=mk({shape:'box',extents:[.1,.2,.3]});for(let i=0;i<200;i++){const s=fx.makeSpawn(box,1,i,0);assert.ok(Math.abs(s.px)<=.1&&Math.abs(s.py)<=.2&&Math.abs(s.pz)<=.3);}
 const ring=mk({shape:'ring',axis:[0,1,0],radius:.45,inner_radius:.35,height:.05}),[r0,r1]=maxR(ring,s=>Math.hypot(s.px,s.pz));assert.ok(r0>=.35-1e-9&&r1<=.45+1e-9);for(let i=0;i<50;i++)assert.ok(Math.abs(fx.makeSpawn(ring,1,i,0).py)<=.025+1e-9);
 const tilted=mk({shape:'ring',axis:[1,0,0],radius:.4,inner_radius:.4,height:0});for(let i=0;i<50;i++)assert.ok(Math.abs(fx.makeSpawn(tilted,1,i,0).px)<1e-9);
 const cone=fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx('empty',1),{params:{spread:20,direction:[0,0,-1],velocity:[1,1]}})));for(let i=0;i<100;i++){const s=fx.makeSpawn(cone,1,i,0);assert.ok(Math.abs(Math.hypot(s.dx,s.dy,s.dz)-1)<1e-9);assert.ok(-s.dz>=Math.cos(20*Math.PI/180)-1e-9);}});

test('color_ramp and scale_curve are interpolated',()=>{
 const ramp=[[0,[1,0,0,1]],[.5,[0,1,0,.5]],[1,[0,0,1,0]]];assert.deepEqual(fx.sampleRamp(ramp,-1),[1,0,0,1]);assert.deepEqual(fx.sampleRamp(ramp,0),[1,0,0,1]);assert.deepEqual(fx.sampleRamp(ramp,.25),[.5,.5,0,.75]);assert.deepEqual(fx.sampleRamp(ramp,.5),[0,1,0,.5]);assert.deepEqual(fx.sampleRamp(ramp,2),[0,0,1,0]);
 const out=[9,9,9,9,9];fx.sampleRamp(ramp,.75,out,1);assert.deepEqual(out,[9,0,.5,.5,.25]);
 const curve=[[0,0],[.2,1],[1,.4]];assert.equal(fx.sampleCurve(curve,-1),0);assert.equal(fx.sampleCurve(curve,.2),1);assert.equal(fx.sampleCurve(curve,5),.4);assert.ok(Math.abs(fx.sampleCurve(curve,.1)-.5)<1e-9,'smoothstep: the midpoint of the segment = the average');assert.ok(fx.sampleCurve(curve,.05)<.25&&fx.sampleCurve(curve,.15)>.75,'ease in/out like Godot (zero tangents)');
 assert.equal(fx.sampleCurve([[0,.7],[1,.7]],.4),.7);
 const c=fx.compileFx(fx.resolveFx(fx.makeFx('aura',1))),o={};fx.particleAt(c,1,0,.05,o);const u0=o.u;fx.particleAt(c,1,0,.05+c.lifetime*.5,o);assert.ok(o.alive&&o.a>0&&o.size>0&&u0<1);
 const nav=fx.compileFx(fx.resolveFx(fx.makeFx('nav_light',1)));let sizes=[];for(let k=0;k<20;k++){fx.particleAt(nav,1,0,k/20,o);sizes.push(o.size);}assert.ok(Math.max(...sizes)>.4&&Math.min(...sizes)<1e-6,'nav_light blinks: the size reaches zero');});

test('flipbook: the frame is computed from age and fps',()=>{
 const c=fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx('empty',1),{params:{lifetime:2,amount:1},flipbook:{h:4,v:2,fps:8}}))),o={};assert.equal(c.frames,8);
 for(const [t,f] of [[0.01,0],[.2,1],[.55,4],[1.0,0],[1.5,4]]){const p=fx.particleAt(c,1,0,t,o);if(o.alive)assert.equal(o.frame,Math.floor(o.age*8)%8,'t='+t);}
 fx.particleAt(fx.compileFx(fx.resolveFx(fx.makeFx('fire',1))),1,0,.3,o);assert.equal(o.frame,0);});

test('built-in textures: size, shape, match with the numpy reference',()=>{
 for(const kind of fx.FX_TEXTURES)for(const size of [32,64]){const t=fx.fxTexture(kind,size);assert.equal(t.width,size);assert.equal(t.height,size);assert.equal(t.data.length,size*size*4);for(let i=0;i<size*size;i++)assert.ok(t.data[i*4]===255&&t.data[i*4+1]===255&&t.data[i*4+2]===255);}
 const soft=fx.fxTexture('soft',64),px=(t,x,y)=>t.data[(y*t.width+x)*4+3];assert.ok(px(soft,31,31)>=235&&px(soft,32,32)>=235&&px(soft,0,0)===0&&px(soft,63,0)===0&&px(soft,31,5)<px(soft,31,20));
 const ring=fx.fxTexture('ring',64);assert.ok(px(ring,32,32)<10&&px(ring,32,8)>200||px(ring,32,56)>200);
 assert.throws(()=>fx.fxTexture('nope'));
 const rng=fx.numpyRandom(7);assert.deepEqual([rng(),rng(),rng(),rng()],[0.625095466604667,0.8972138009695755,0.7756856902451935,0.22520718999059186]);
 const smoke=fx.fxTexture('smoke',64);assert.equal(px(smoke,0,0),0);assert.ok(px(smoke,32,32)>20);assert.deepEqual(fx.fxTexture('smoke',64).data,smoke.data);
 // reference values from the Python reference (_export_lib.make_fx_texture): the alpha sum over the image
 const sum=t=>t.data.reduce((n,v,i)=>n+(i%4===3?v:0),0);for(const [kind,expected] of Object.entries(PY_ALPHA_SUM))assert.equal(sum(fx.fxTexture(kind,64)),expected,kind+': alpha sum = Python reference');});

test('an uncompressed PNG decodes back to the same pixels',()=>{
 const t=fx.fxTexture('star',64),png=fx.encodePng(t.width,t.height,t.data);assert.deepEqual([...png.subarray(0,8)],[137,80,78,71,13,10,26,10]);
 const dv=new DataView(png.buffer,png.byteOffset,png.byteLength);assert.equal(dv.getUint32(16),64);assert.equal(dv.getUint32(20),64);assert.equal(png[24],8);assert.equal(png[25],6);
 const chunks={};let p=8;while(p<png.length){const n=dv.getUint32(p),type=String.fromCharCode(...png.subarray(p+4,p+8));chunks[type]=png.subarray(p+8,p+8+n);const crc=zlib.crc32?zlib.crc32(png.subarray(p+4,p+8+n)):null;if(crc!==null)assert.equal(dv.getUint32(p+8+n),crc,type+' crc');p+=12+n;}
 const raw=zlib.inflateSync(Buffer.from(chunks.IDAT));assert.equal(raw.length,(64*4+1)*64);for(let y=0;y<64;y++){assert.equal(raw[y*257],0);assert.deepEqual([...raw.subarray(y*257+1,y*257+257)],[...t.data.subarray(y*256,y*256+256)]);}
 const big=fx.encodePng(300,300,new Uint8Array(300*300*4).fill(7)),bdv=new DataView(big.buffer,big.byteOffset,big.byteLength);let q=8,idat;while(q<big.length){const n=bdv.getUint32(q);if(String.fromCharCode(...big.subarray(q+4,q+8))==='IDAT')idat=big.subarray(q+8,q+8+n);q+=12+n;}const inflated=zlib.inflateSync(Buffer.from(idat));assert.equal(inflated.length,(300*4+1)*300);assert.ok(inflated.subarray(1,1201).every(v=>v===7));
 assert.deepEqual([...fx.fxTexturePng('ring')].slice(0,4),[137,80,78,71]);});

const sceneOf=(preset,params={},extra={})=>{const f=fx.patchFx(fx.makeFx(preset,1),{params,...extra}),r=fx.resolveFx(f);return {r,text:fx.fxSceneText('fx_'+preset,{...r,_preset:preset},'textures/'+r.texture+'.png',r.flipbook)};};
test('.tscn: GPUParticles3D, ParticleProcessMaterial, QuadMesh and the effect parameters',()=>{
 for(const id of IDS){const {r,text}=sceneOf(id);assert.match(text,/^\[gd_scene load_steps=\d+ format=3\]/);for(const w of ['GPUParticles3D','ParticleProcessMaterial','QuadMesh','StandardMaterial3D','GradientTexture1D','CurveTexture'])assert.ok(text.includes(w),id+' '+w);
  assert.ok(text.includes('amount = '+r.amount)&&text.includes('lifetime = '+fx.gdF(r.lifetime)),id);assert.ok(text.includes('[ext_resource type="Texture2D" path="textures/'+r.texture+'.png" id="1_tex"]'),id);assert.ok(text.includes('albedo_texture = ExtResource("1_tex")'));
  assert.equal(text.includes('OmniLight3D'),!!r.light,id+': light');assert.equal(/blend_mode = (\d)/.exec(text)[1],r.blend==='add'?'1':'0');assert.ok(text.includes('one_shot = '+(r.one_shot?'true':'false')));assert.ok(text.includes('emitting = '+(r.autostart?'true':'false')));
  const subs=(text.match(/\[sub_resource /g)||[]).length;assert.equal(+/load_steps=(\d+)/.exec(text)[1],subs+2,id+': load_steps');assert.equal((text.match(/\[node /g)||[]).length,r.light?2:1);assert.ok(text.endsWith('\n'));}
 const {text:custom}=sceneOf('fire',{amount:123,lifetime:2.5,quad_size:.77,local_coords:true,one_shot:true,autostart:false,spread:33,blend:'mix'});for(const s of ['amount = 123','lifetime = 2.5','Vector2(0.77, 0.77)','local_coords = true','one_shot = true','emitting = false','spread = 33','blend_mode = 0'])assert.ok(custom.includes(s),s);
 assert.ok(sceneOf('fire',{light:null}).text.indexOf('OmniLight3D')<0);assert.ok(sceneOf('empty',{light:{color:[.1,.2,.3],energy:2,range:5}}).text.includes('light_color = Color(0.1, 0.2, 0.3, 1)'));
 assert.ok(sceneOf('empty',{emission:{shape:'ring',radius:.4,inner_radius:.1,height:.02,axis:[0,0,1]}}).text.includes('emission_ring_axis = Vector3(0, 0, 1)'));assert.ok(sceneOf('empty',{emission:{shape:'box',extents:[1,2,3]}}).text.includes('emission_box_extents = Vector3(1, 2, 3)'));
 assert.ok(sceneOf('poison').text.includes('turbulence_enabled = true')&&!sceneOf('fire').text.includes('turbulence'));assert.ok(sceneOf('magic_cast').text.includes('tangential_accel_max = 3'));
 const flip=sceneOf('empty',{lifetime:2},{flipbook:{h:4,v:2,fps:8}}).text;for(const s of ['particles_anim_h_frames = 4','particles_anim_v_frames = 2','particles_anim_loop = true','anim_speed_min = 2'])assert.ok(flip.includes(s),s);assert.ok(!sceneOf('empty').text.includes('particles_anim'));
 assert.equal(fx.gdF(1),'1');assert.equal(fx.gdF(0.35),'0.35');assert.equal(fx.gdF(-0),'0');assert.equal(fx.gdF(1.0000004),'1');assert.equal(fx.gdF(-2.5),'-2.5');assert.equal(fx.gdF(100.0000004),'100');assert.equal(fx.gdF(0.1+0.2),'0.3');});

test('the emitter scale is baked into the parameters as merge_fx(scale=k)',()=>{
 const r=fx.resolveFx(fx.makeFx('fire',1)),s=fx.scaleFx(r,2.5);assert.equal(s.quad_size,r.quad_size*2.5);assert.deepEqual(s.velocity,r.velocity.map(v=>v*2.5));assert.deepEqual(s.gravity,r.gravity.map(v=>v*2.5));assert.equal(s.emission.radius,r.emission.radius*2.5);assert.equal(s.light.range,r.light.range*2.5);assert.deepEqual(s.visibility_aabb,[-10,-10,-10,20,20,20]);
 assert.equal(fx.scaleFx(r,1),r);assert.equal(fx.scaleFx(r,0),r);assert.equal(r.quad_size,.35,'the source parameters are untouched');assert.equal(s.amount,r.amount);assert.equal(s.lifetime,r.lifetime);});

test('Godot package: scenes, textures, mounts, README and af_fx_mount.gd',()=>{
 const tf=(pos=[0,1,0],scale=[1,1,1])=>({position:pos,rotation:[0,0,0],quaternion:[0,0,0,1],scale});
 const items=[{name:'FX · Fire',fx:fx.makeFx('fire',1),node:'FX · Fire',nodePath:'AstraForge/Sword/FX · Fire',parent:'Sword',transform:tf([0,1.2,0],[2,2,2])},{name:'FX · Fire',fx:fx.makeFx('fire',2),node:'FX · Fire',nodePath:'AstraForge/FX · Fire',parent:null,transform:tf()},
  {name:'Sparks: test',fx:fx.patchFx(fx.makeFx('sparks',3),{textureAsset:{id:'a1',url:'/api/assets/a1.webp',name:'flash.webp'},flipbook:{h:2,v:2,fps:12}}),node:'sparks.1',parent:'hand.R',transform:tf()}];
 const pkg=fx.buildFxPackage(items),paths=pkg.files.map(f=>f.path);assert.deepEqual(paths.filter(p=>p.endsWith('.tscn')),['fx/fire.tscn','fx/fire_2.tscn','fx/sparks_test.tscn']);
 for(const p of ['fx/textures/soft.png','fx/textures/sparks_test_tex.webp','fx_bindings.json','af_fx_mount.gd','README.md'])assert.ok(paths.includes(p),p);assert.equal(paths.filter(p=>p==='fx/textures/soft.png').length,1,'built-in textures are not duplicated');
 const file=p=>pkg.files.find(f=>f.path===p);assert.ok(file('fx/textures/soft.png').bytes instanceof Uint8Array);assert.equal(file('fx/textures/sparks_test_tex.webp').url,'/api/assets/a1.webp');
 const scaled=file('fx/fire.tscn').text;assert.ok(scaled.includes('Vector2(0.7, 0.7)'),'emitter scale 2 x quad_size 0.35');
 assert.equal(pkg.bindings.length,3);const b=pkg.bindings[0];assert.equal(b.scene,'fx/fire.tscn');assert.equal(b.parent,'Sword');assert.equal(b.node,'FX · Fire');assert.equal(b.node_godot,'FX · Fire');assert.deepEqual(b.transform.position,[0,1.2,0]);assert.deepEqual(b.transform.scale,[2,2,2]);assert.equal(pkg.bindings[1].parent,'AstraForge');assert.equal(pkg.bindings[2].parent_godot,'hand_R');assert.equal(pkg.bindings[2].node_godot,'sparks_1');
 assert.deepEqual(JSON.parse(file('fx_bindings.json').text),pkg.bindings);assert.equal(file('af_fx_mount.gd').text,AF_FX_MOUNT_GD);assert.ok(file('fx/sparks_test.tscn').text.includes('textures/sparks_test_tex.webp')&&file('fx/sparks_test.tscn').text.includes('particles_anim_h_frames = 2'));
 const readme=file('README.md').text;for(const s of ['af_fx_mount.gd','fx_bindings.json','fx/fire.tscn','GPUParticles3D'])assert.ok(readme.includes(s),s);
 assert.equal(fx.fxSlug('FX · Fire'),'fire');assert.equal(fx.fxSlug('Engine trail'),'engine_trail');assert.equal(fx.fxSlug('###'),'fx');const used=new Set();assert.deepEqual(['a','a','a'].map(n=>fx.fxSlug(n,used)),['a','a_2','a_3']);assert.equal(fx.godotNodeName('a.b:c@d/e"f%g'),'a_b_c_d_e_f_g');
 assert.equal(fx.buildFxPackage([]).bindings.length,0);});

test('the seed of a new emitter is a 31-bit integer, different on repeated calls',()=>{const s=Array.from({length:50},()=>fx.newSeed());assert.ok(s.every(v=>Number.isInteger(v)&&v>=0&&v<2**31));assert.ok(new Set(s).size>45);assert.equal(fx.makeFx('fire').format,'astraforge-fx/1');assert.equal(JSON.stringify(JSON.parse(JSON.stringify(fx.makeFx('fire',5)))),JSON.stringify(fx.makeFx('fire',5)));});

test('performance: 5 effects of 300 particles each fit within the frame budget',()=>{
 const cs=['fire','smoke','magic_cast','poison','aura'].map((id,i)=>[fx.compileFx(fx.resolveFx(fx.patchFx(fx.makeFx(id,i+1),{params:{amount:300}}))),i+1]),o={};const t0=performance.now();let n=0;
 for(let f=0;f<60;f++)for(const [c,seed] of cs)for(let i=0;i<c.amount;i++){fx.particleAt(c,seed,i,f/60,o);n++;}const per=(performance.now()-t0)/60;assert.ok(per<40,'frame '+per.toFixed(2)+' ms at '+n/60+' particles');});

// ---------------------------------------------------------------- runtime (fx-runtime.js) without WebGL: real Object3D and buffers, a studio stub
import * as T from 'three';import {installFxTools} from '../src/fx-runtime.js';
function makeStudio(){
 class Stub{constructor(){this.scene=new T.Scene();this.content=new T.Group();this.content.name='AstraForge';this.scene.add(this.content);this.editor={time:0,duration:8,bases:{},tracks:{},preset:'none'};this.camera=new T.PerspectiveCamera(40,1,.01,200);this.camera.position.set(5,3,5);this.camera.updateMatrixWorld(true);this.playing=false;this.recording=false;this.selected=null;this.history=[];this.editRevision=0;this.box={visible:false};this.ray=new T.Raycaster();this.orbit={target:new T.Vector3(),update(){}};this.emits=0;this.initFxTools();}
  pause(){this.playing=false;} checkpoint(){this.history.push(this.content.toJSON());this.editRevision++;} emit(){this.emits++;} rigPick(){return null;} frame(){}
  select(id){this.selected=id?this.content.getObjectByProperty('uuid',id):null;this.emit();}
  snapshot(){const objects=[];this.content.traverse(o=>{if(o!==this.content)objects.push({id:o.uuid,name:o.name});});return {objects};}
  duplicate(){const o=this.selected.clone();o.name+=' · copy';this.selected.parent.add(o);this.select(o.uuid);} async restore(){} remove(){this.selected.removeFromParent();this.select(null);} dispose(){}
  toggleVisibility(id){const o=this.content.getObjectByProperty('uuid',id);o.visible=!o.visible;}}
 installFxTools(Stub);return new Stub();}
const frames=(s,n=1)=>{for(let i=0;i<n;i++)s.fxTick();};
const itemOf=(s,id)=>s.fx.items.get(id);
const resolved=(s,id)=>fx.resolveFx(s.content.getObjectByProperty('uuid',id).userData.fx);

test('runtime: an effect is added to the selected object and ends up in the snapshot, the overlay is outside content',()=>{
 const s=makeStudio(),mesh=new T.Group();mesh.name='Sword';s.content.add(mesh);s.select(mesh.uuid);
 const id=s.addFx('fire'),o=s.content.getObjectByProperty('uuid',id);assert.equal(o.parent,mesh);assert.equal(o.name,'FX · Fire');assert.equal(o.userData.fx.preset,'fire');assert.equal(o.userData.fx.format,'astraforge-fx/1');assert.equal(s.selected,o);assert.equal(s.history.length,1);
 const snap=s.snapshot();assert.equal(snap.fx.list.length,1);assert.equal(snap.fx.list[0].parent,'Sword');assert.equal(snap.fx.target,'Sword','an emitter is selected -> the target is its parent');assert.equal(snap.objects.find(x=>x.id===id).fx,true);assert.equal(snap.objects.find(x=>x.id===mesh.uuid).fx,undefined);assert.equal(snap.fx.selected.id,id);assert.equal(snap.fx.selected.res.amount,48);
 frames(s,2);assert.equal(s.fx.items.size,1);assert.ok(s.fx.overlay.parent===s.scene&&s.content.getObjectByProperty('uuid',s.fx.overlay.uuid)===undefined,'overlay outside content');
 const json=JSON.stringify(s.content.toJSON());assert.ok(json.includes('astraforge-fx/1')&&!json.includes('aPos')&&!json.includes('PointLight'),'only the emitter data goes into the project');
 const root=makeStudio();root.addFx('empty');root.select(null);assert.equal(root.snapshot().fx.target,null,'no selection - the scene center');assert.equal(root.fxParent(),root.content);});

test('runtime: particles live around the emitter, computed from the live clock and from the timeline time',()=>{
 const s=makeStudio(),id=s.addFx('fire'),o=s.content.getObjectByProperty('uuid',id);o.position.set(1,2,3);frames(s);
 const it=itemOf(s,id);assert.ok(it.mesh.visible&&it.count===48,'the loop is full from the first frame: '+it.count);
 const pa=it.geo.attributes.aPos.array;for(let i=0;i<it.count;i++){assert.ok(Number.isFinite(pa[i*3])&&Math.abs(pa[i*3]-1)<2&&Math.abs(pa[i*3+2]-3)<2&&pa[i*3+1]>1.5,'particle '+i);}
 assert.equal(it.geo.instanceCount,48);const col=it.geo.attributes.aCol.array;assert.ok(col[3]>=0&&col[3]<=1);
 s.fx.live=5;it.liveEpoch=0;frames(s);const a=[...pa.slice(0,12)];s.fx.live=5-s.fx.live+5;frames(s);
 s.playing=true;s.editor.time=2;frames(s);const t2=[...pa.slice(0,12)];s.editor.time=2;frames(s);assert.deepEqual([...pa.slice(0,12)],t2,'timeline: the same time gives the same positions');s.editor.time=2.3;frames(s);assert.notDeepEqual([...pa.slice(0,12)],t2);
 s.playing=false;o.visible=false;frames(s);assert.equal(it.mesh.visible,false,'a hidden emitter is not drawn');o.visible=true;s.content.visible=false;frames(s);assert.equal(it.mesh.visible,false,'a hidden ancestor too');s.content.visible=true;frames(s);assert.ok(it.mesh.visible);assert.ok(a.every(Number.isFinite));});

test('runtime: local_coords=false leaves the trail in the world, true moves it with the emitter',()=>{
 for(const local of [false,true]){const s=makeStudio(),id=s.addFx('empty');s.updateFx(id,{params:{local_coords:local,amount:200,lifetime:1,velocity:[0,0],spread:0,emission:{shape:'point'}}});const o=s.content.getObjectByProperty('uuid',id);s.playing=true;
  for(let k=0;k<=10;k++){s.editor.time=k*.05;o.position.set(0,0,0);frames(s);}o.position.set(10,0,0);s.editor.time=.55;frames(s);const it=itemOf(s,id),pa=it.geo.attributes.aPos.array;let min=1e9,max=-1e9;for(let i=0;i<it.count;i++){min=Math.min(min,pa[i*3]);max=Math.max(max,pa[i*3]);}
  if(local){assert.ok(min>9.99&&max<10.01,'local: all at the emitter '+[min,max]);}else{assert.ok(min<.01,'world: old particles stayed where they were born '+min);assert.ok(max>9.99,'new ones are at the emitter '+max);}}});

test('runtime: the emitter scale changes the effect size, the parent scale does not',()=>{
 const s=makeStudio(),parent=new T.Group();s.content.add(parent);s.select(parent.uuid);const id=s.addFx('fire'),o=s.content.getObjectByProperty('uuid',id);s.playing=true;s.editor.time=1;frames(s);const it=itemOf(s,id),size=()=>Math.max(...it.geo.attributes.aSR.array.filter((_,i)=>i%4===0).slice(0,it.count));const base=size();
 parent.scale.setScalar(.2);frames(s);assert.ok(Math.abs(size()-base)<1e-6,'the parent scale is ignored (effect meters)');o.scale.setScalar(2);frames(s);assert.ok(Math.abs(size()/base-2)<1e-6,'the emitter own scale multiplies the size');assert.equal(Math.round(it.cfg.k*100),200);});

test('runtime: edits are merged into one undo step, a one-shot emitter is started by a button',()=>{
 const s=makeStudio(),id=s.addFx('sparks'),before=s.history.length;for(const v of [60,70,80,90])assert.ok(s.updateFx(id,{params:{amount:v}}));assert.equal(s.history.length,before+1,'a slider = one undo step');assert.equal(resolved(s,id).amount,90);
 s.updateFx(id,{params:{lifetime:2}});assert.equal(s.history.length,before+2,'another field - a new step');assert.ok(!s.updateFx('no-such-id',{params:{amount:1}}));
 const rev=s.editRevision;s.updateFx(id,{params:{lifetime:3}});assert.equal(s.editRevision,rev+1,'a merged edit still marks the project as modified');
 s.updateFx(id,{params:{one_shot:true,explosiveness:1,amount:20,lifetime:.5}});frames(s);const it=itemOf(s,id);s.playing=true;s.editor.time=3;s.triggerFx(id);assert.equal(resolved(s,id).startTime,3);
 s.editor.time=2.5;frames(s);assert.equal(it.count,0,'nothing before the start');s.editor.time=3.05;frames(s);assert.equal(it.count,20,'a flash after the start');s.editor.time=3.6;frames(s);assert.equal(it.count,0,'the flash is over');
 const o=s.content.getObjectByProperty('uuid',id),ud=o.userData;s.updateFx(id,{params:{amount:21}});assert.notEqual(o.userData,ud,'userData is replaced as a whole: undo snapshots do not change');assert.equal(ud.fx.params.amount,20);
 s.updateFx(id,{name:'Flash'});assert.equal(o.name,'Flash');});

test('runtime: light, sorting, deletion, duplicates, particle budget',()=>{
 const s=makeStudio(),id=s.addFx('fire');frames(s);const it=itemOf(s,id);assert.ok(it.light&&it.light.intensity>0&&it.light.distance>0,'fire emits light');s.updateFx(id,{params:{light:null}});frames(s);assert.equal(it.light.intensity,0);
 const sm=s.addFx('smoke');frames(s,2);const mi=itemOf(s,sm),pa=mi.geo.attributes.aPos.array,cam=s.camera.position;let prev=Infinity;for(let i=0;i<mi.count;i++){const d=Math.hypot(pa[i*3]-cam.x,pa[i*3+1]-cam.y,pa[i*3+2]-cam.z);assert.ok(d<=prev+1e-5,'far ones first');prev=d;}
 s.select(sm);s.duplicate();const copy=s.selected;assert.notEqual(copy.userData.fx.seed,s.content.getObjectByProperty('uuid',sm).userData.fx.seed,'a copy has its own seed');assert.equal(copy.userData.fx.preset,'smoke');
 const o=s.content.getObjectByProperty('uuid',id);o.removeFromParent();frames(s,7);assert.equal(s.fx.items.has(id),false);assert.equal(s.fx.overlay.children.includes(it.mesh),false,'after deletion the mesh and the light are removed from the overlay');
 const big=makeStudio();for(let i=0;i<9;i++){const e=big.addFx('empty');big.updateFx(e,{params:{amount:4000}});}frames(big,2);assert.equal(big.snapshot().fx.budget.over,true);const total=[...big.fx.items.values()].reduce((n,x)=>n+x.count,0);assert.ok(total<=fx.FX_LIMITS.total&&total>=fx.FX_LIMITS.total-4000,'drawn '+total);
 big.dispose();assert.equal(big.scene.children.length,1);});

test('runtime: the Godot package from the studio collects effect mounts with paths and transforms',async()=>{
 const s=makeStudio(),arm=new T.Group();arm.name='Hand';s.content.add(arm);s.select(arm.uuid);const id=s.addFx('magic_cast'),o=s.content.getObjectByProperty('uuid',id);o.position.set(0,.5,0);o.scale.setScalar(1.5);
 const origFetch=globalThis.fetch,hadDoc='document' in globalThis;let sent;globalThis.document={createElement:()=>({click(){}, remove(){}}),body:{appendChild(){}}};
 globalThis.fetch=async(url,opt)=>{sent=opt?.body;return {ok:true,json:async()=>({url:'/x',name:'AstraForge-FX.zip'})};};
 try{const pkg=await s.exportFxPackage();assert.equal(pkg.bindings.length,1);const b=pkg.bindings[0];assert.equal(b.parent,'Hand');assert.equal(b.node_path,'AstraForge/Hand/FX · Magic cast');assert.deepEqual(b.transform.position,[0,.5,0]);assert.deepEqual(b.transform.scale,[1.5,1.5,1.5]);assert.ok(sent instanceof FormData,'the ZIP went through download()');
  const file=sent.get('file');assert.ok(file.size>1000);const JSZip=(await import('jszip')).default,z=await JSZip.loadAsync(await file.arrayBuffer());assert.ok(z.file('fx_bindings.json')&&z.file('af_fx_mount.gd')&&z.file('README.md')&&z.file('fx/magic_cast.tscn')&&z.file('fx/textures/star.png'));
  assert.ok((await z.file('fx/magic_cast.tscn').async('string')).includes('Vector2(0.18, 0.18)'),'scale 1.5 x quad 0.12 is baked into the scene');}
 finally{globalThis.fetch=origFetch;if(!hadDoc)delete globalThis.document;}
 await assert.rejects(makeStudio().exportFxPackage(),/at least one effect/);});

// ---------------------------------------------------------------- auto-size and direction of a new effect
const BX=(min,max)=>({min,max});
test('auto-size: k = the long side of the model / reference_size_m, a nozzle part by reference_socket_m, clamped to [0.05, 50]',()=>{
 const ship=BX([-.5,-.5,-2],[.5,.5,2]);
 assert.equal(fx.fxAutoPlacement('smoke',BX([0,0,0],[4,1,1]),null,{size:2,socket:0}).k,2,'model 4 m, reference_size_m=2 -> 2');
 assert.equal(fx.fxAutoPlacement('smoke',ship,ship,{size:2,socket:0}).k,2,'the parent = the model itself');
 assert.equal(fx.fxAutoPlacement('aura',ship,ship).k,Math.round(4/1.8*1000)/1000,'the real aura preset: reference_size_m=1.8');
 const nozzle=BX([-.25,-.25,-2],[.25,.25,-1.5]);
 const part=fx.fxAutoPlacement('engine_trail',ship,nozzle);assert.equal(part.source,'socket');assert.equal(part.k,Math.round(.5/.45*1000)/1000,'nozzle diameter 0.5 m / reference_socket_m 0.45');
 assert.equal(fx.fxAutoPlacement('engine_trail',ship,ship).source,'model');assert.equal(fx.fxAutoPlacement('engine_trail',ship,ship).k,Math.round(4/6*1000)/1000);
 assert.equal(fx.fxAutoPlacement('engine_trail',ship,BX([-.5,-.5,-2],[.5,.5,1])).source,'model','a large part (75% of the model) - same as the model');
 assert.equal(fx.fxAutoPlacement('aura',ship,nozzle).source,'model','aura has no reference_socket_m - the size comes from the model even for a part');
 const flat=fx.fxAutoPlacement('engine_trail',ship,BX([0,0,-2],[0,0,-2]));assert.equal(flat.source,'model','a bone (a point) gives no diameter - the size comes from the model');
 assert.equal(fx.fxAutoPlacement('smoke',BX([0,0,0],[400,1,1]),null,{size:2,socket:0}).k,50);assert.equal(fx.fxAutoPlacement('smoke',BX([0,0,0],[.01,.01,.01]),null,{size:2,socket:0}).k,.05);
 assert.equal(fx.fxAutoPlacement('smoke',BX([0,0,0],[0,0,0]),null).k,1,'an empty bounding box - no scale');assert.equal(fx.fxAutoPlacement('smoke',null,null).k,1);assert.equal(fx.fxAutoPlacement('empty',ship,ship).k,1,'the empty preset has no reference_size_m');
 assert.deepEqual([...fx.ENGINE_PRESET_IDS].sort(),['engine_trail','thruster_glow']);assert.ok(fx.isEnginePreset('engine_trail')&&!fx.isEnginePreset('smoke')&&!fx.isEnginePreset('fire'));assert.deepEqual(fx.presetRefs('engine_trail'),{size:6,socket:.45});assert.deepEqual(fx.presetRefs('fire'),{size:1.8,socket:0});});

test('auto-size: an engine preset points outward along the long axis from the part face, the others point up from the center',()=>{
 const ship=BX([-.5,-.5,-2],[.5,.5,2]);
 const aft=fx.fxAutoPlacement('engine_trail',ship,BX([-.25,-.25,-2],[.25,.25,-1.5]));assert.deepEqual(aft.axis,[0,0,-1],'nozzle at -Z -> exhaust at -Z');assert.deepEqual(aft.point,[0,0,-2],'center of the outer nozzle face');
 const front=fx.fxAutoPlacement('engine_trail',ship,BX([.5,-.25,1.2],[1,.25,2]));assert.deepEqual(front.axis,[0,0,1]);assert.deepEqual(front.point,[.75,0,2]);
 const whole=fx.fxAutoPlacement('thruster_glow',ship,ship);assert.deepEqual(whole.axis,[0,0,1],'whole model: a tie -> +Z (the stern of models with the nose at -Z)');assert.deepEqual(whole.point,[0,0,2]);
 const along=fx.fxAutoPlacement('engine_trail',BX([-2,-.5,-.5],[2,.5,.5]),BX([-2,-.25,-.25],[-1.5,.25,.25]));assert.deepEqual(along.axis,[-1,0,0]);assert.equal(along.longAxis,0);
 const up=fx.fxAutoPlacement('engine_trail',BX([-.5,-2,-.5],[.5,2,.5]),BX([-.25,1.5,-.25],[.25,2,.25]));assert.deepEqual(up.axis,[0,1,0]);assert.equal(up.longAxis,1);
 const starter=fx.fxAutoPlacement('engine_trail',BX([-2.25,1.1,-2.1],[2.25,1.7,2.1]),null);assert.equal(starter.longAxis,2,'a wingspan of 4.5 does not override a length of 4.2 along Z');assert.deepEqual(starter.axis,[0,0,1]);
 assert.equal(fx.fxAutoPlacement('engine_trail',BX([-3,-.5,-1],[3,.5,1]),null).longAxis,0,'a model clearly elongated along X - the X axis');
 const bone=fx.fxAutoPlacement('engine_trail',ship,BX([0,0,-1.8],[0,0,-1.8]));assert.deepEqual(bone.axis,[0,0,-1],'a bone at the stern: the exhaust points toward it');assert.deepEqual(bone.point,[0,0,-1.8]);
 for(const id of ['aura','fire','heal','magic_cast','empty','smoke']){const r=fx.fxAutoPlacement(id,ship,BX([.2,0,-.5],[.6,.4,.5]));assert.deepEqual(r.axis,[0,1,0],id);assert.ok(Math.abs(r.point[0]-.4)<1e-9&&Math.abs(r.point[1]-.2)<1e-9&&Math.abs(r.point[2])<1e-9,id+': center of the parent bounding box');}
 assert.deepEqual(fx.fxAutoPlacement('fire',null,null).point,null);});

const worldDir=(o,v=[0,1,0])=>{o.updateWorldMatrix(true,false);const q=new T.Quaternion();o.matrixWorld.decompose(new T.Vector3(),q,new T.Vector3());return new T.Vector3(...v).applyQuaternion(q);};
const worldPos=o=>{o.updateWorldMatrix(true,false);return o.getWorldPosition(new T.Vector3());};
function buildShip(){const body=new T.Mesh(new T.BoxGeometry(1,1,4),new T.MeshBasicMaterial());body.name='Hull';const nozzle=new T.Mesh(new T.BoxGeometry(.5,.5,.5),new T.MeshBasicMaterial());nozzle.name='Nozzle';nozzle.position.set(0,0,-1.75);const model=new T.Group();model.name='Ship';model.add(body,nozzle);return {model,body,nozzle};}
test('runtime: addFx picks the scale, the point and the direction from the model itself (taking the parent rotation and scale into account)',()=>{
 const near=(a,b,eps=1e-3)=>assert.ok(Math.abs(a-b)<eps,a+' ≈ '+b);
 // nozzle at the stern -Z: scale by the nozzle diameter, exhaust to -Z from the center of the outer face
 {const s=makeStudio(),{model,nozzle}=buildShip();s.content.add(model);s.select(nozzle.uuid);const id=s.addFx('engine_trail'),o=s.content.getObjectByProperty('uuid',id);
  near(o.scale.x,.5/.45);near(o.scale.y,o.scale.x);const d=worldDir(o);near(d.x,0);near(d.y,0);near(d.z,-1);const p=worldPos(o);near(p.x,0);near(p.y,0);near(p.z,-2);assert.equal(s.editor.bases[id].scale[0],o.scale.x,'the scale is stored in the base for the timeline');
  s.select(null);const meshes=s.addFx('aura'),a=s.content.getObjectByProperty('uuid',meshes);near(a.scale.x,4/1.8);near(worldDir(a).y,1);near(worldPos(a).z,0,1e-2);}
 // the whole model is selected: the engine preset goes to the +Z stern, k by the model length
 {const s=makeStudio(),{model}=buildShip();s.content.add(model);s.select(model.uuid);const o=s.content.getObjectByProperty('uuid',s.addFx('engine_trail'));near(o.scale.x,4/6);const d=worldDir(o);near(d.z,1);near(worldPos(o).z,2);}
 // the model is rotated 90 degrees around Y and doubled: the long axis is world X, the nozzle is at -X, local coordinates are recomputed
 {const s=makeStudio(),{model,nozzle}=buildShip();model.rotation.y=Math.PI/2;model.scale.setScalar(2);model.position.set(5,1,0);s.content.add(model);s.select(nozzle.uuid);
  const id=s.addFx('engine_trail'),o=s.content.getObjectByProperty('uuid',id);near(o.scale.x,(.5*2)/.45,2e-3);const d=worldDir(o);near(d.x,-1);near(d.y,0,2e-3);near(d.z,0,2e-3);const p=worldPos(o);near(p.x,5-4);near(p.y,1);near(p.z,0);
  assert.ok(Math.abs(o.position.z+.25)<1e-3&&Math.abs(o.position.x)<1e-3&&Math.abs(o.position.y)<1e-3,'local position: the outer nozzle face in nozzle axes, '+o.position.toArray());
  s.select(null);const up=s.addFx('fire'),f=s.content.getObjectByProperty('uuid',up);s.select(model.uuid);const g=s.addFx('fire'),fo=s.content.getObjectByProperty('uuid',g);near(worldDir(fo).y,1);near(worldDir(fo).x,0,2e-3);near(worldPos(fo).x,5);assert.equal(f.parent,s.content);}
 // bone: the position is the bone itself, the size comes from the model, the exhaust points from the center toward the bone
 {const s=makeStudio(),{model}=buildShip(),bone=new T.Bone();bone.name='Tail';bone.position.set(0,0,-1.9);model.add(bone);s.content.add(model);s.select(bone.uuid);const o=s.content.getObjectByProperty('uuid',s.addFx('engine_trail'));
  near(o.scale.x,4/6);assert.ok(o.position.length()<1e-6,'the emitter at the bone origin');near(worldDir(o).z,-1);}
 // an empty scene and a model without geometry: scale 1, the origin, up
 {const s=makeStudio(),o=s.content.getObjectByProperty('uuid',s.addFx('aura'));assert.equal(o.scale.x,1);assert.equal(o.position.length(),0);near(worldDir(o).y,1);}
 {const s=makeStudio(),g=new T.Group();s.content.add(g);s.select(g.uuid);const o=s.content.getObjectByProperty('uuid',s.addFx('fire'));assert.equal(o.scale.x,1);assert.equal(o.position.length(),0);}
 // effects on the model do not affect the model bounding box: the second effect gets the same k
 {const s=makeStudio(),{model}=buildShip();s.content.add(model);s.select(model.uuid);const a=s.content.getObjectByProperty('uuid',s.addFx('aura'));s.select(model.uuid);const b=s.content.getObjectByProperty('uuid',s.addFx('aura'));near(a.scale.x,b.scale.x,1e-9);}});
