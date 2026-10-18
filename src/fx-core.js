// Core of the AstraForge effects editor: pure logic, no DOM or Three (format astraforge-fx/1 = scripts/blender/data/fx_presets.json).
// Presets, normalisation and merging of edits, analytic particles (deterministic by seed and time), built-in textures, PNG, .tscn for Godot 4.5.
// make_fx_texture / fx_scene_text / merge_fx are ports of scripts/blender/steps/_export_lib.py. Rendering: fx-runtime.js, panel: FxPanel.jsx, tests: tests/fx-core.test.mjs.
import {FX_PRESETS as FORGE_FX} from './forge/forge-core.js';
import {t,tOr} from './i18n/index.js';
import {FX_PRESETS_DATA,AF_FX_MOUNT_GD} from './fx-data.js';
export {AF_FX_MOUNT_GD};

export const FX_FORMAT='astraforge-fx/1';
export const FX_LIMITS={amount:4000,total:30000};
export const FX_EMPTY_ID='empty';
export const FX_TEXTURES=['soft','spark','star','smoke','ring'];
// Title maps are getters: the label is looked up in the UI dictionary when it is read.
export const FX_TEXTURE_TITLES={get soft(){return t('fx.texture.soft');},get spark(){return t('fx.texture.spark');},get star(){return t('fx.texture.star');},get smoke(){return t('fx.texture.smoke');},get ring(){return t('fx.texture.ring');}};
export const FX_SHAPES=['point','sphere','sphere_surface','box','ring'];
export const FX_SHAPE_TITLES={get point(){return t('fx.shape.point');},get sphere(){return t('fx.shape.sphere');},get sphere_surface(){return t('fx.shape.sphereSurface');},get box(){return t('fx.shape.box');},get ring(){return t('fx.shape.ring');}};
export const FX_BLENDS=['add','mix'];
export const FX_BLEND_TITLES={get add(){return t('fx.blend.add');},get mix(){return t('fx.blend.mix');}};
const DEFAULT_AABB=[-4,-4,-4,8,8,8];
const DEG=Math.PI/180,TAU=Math.PI*2;

// ---------------------------------------------------------------- numbers and value checks
const clamp=(v,a,b)=>v<a?a:v>b?b:v;
const fin=(v,d)=>{const n=typeof v==='number'?v:typeof v==='string'&&v.trim()!==''?Number(v):NaN;return Number.isFinite(n)?n:d;};
const num=(v,d,a,b)=>clamp(fin(v,d),a,b);
const range2=(v,d,a,b)=>{const x=Array.isArray(v)?v:typeof v==='number'?[v,v]:[];const lo=num(x[0],d[0],a,b),hi=num(x[1],d[1],a,b);return lo<=hi?[lo,hi]:[hi,lo];};
const vec3=(v,d,a,b)=>[0,1,2].map(i=>num(Array.isArray(v)?v[i]:undefined,d[i],a,b));
const flag=(v,d)=>typeof v==='boolean'?v:v==null?d:!!v;
const oneOf=(v,list,d)=>list.includes(v)?v:d;
const len3=v=>Math.hypot(v[0],v[1],v[2]);
const nonzero=(v,d)=>len3(v)>1e-6?v:d;
const unit3=(v,d)=>{const l=len3(v);return l>1e-6?[v[0]/l,v[1]/l,v[2]/l]:d;};
const cross3=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const same=(a,b)=>JSON.stringify(a??null)===JSON.stringify(b??null);
const subset=(a,b)=>a&&typeof a==='object'?b&&typeof b==='object'&&Object.keys(a).every(k=>subset(a[k],b[k])):typeof a==='number'&&typeof b==='number'?Math.abs(a-b)<1e-9:a===b;
const copy=v=>v===undefined?undefined:JSON.parse(JSON.stringify(v));
function normCurve(c,d){let l=(Array.isArray(c)?c:[]).filter(Array.isArray).map(p=>[num(p[0],0,0,1),num(p[1],1,0,10)]).slice(0,8);if(l.length<2)l=copy(d);return l.sort((a,b)=>a[0]-b[0]);}
function normRamp(r,d){let l=(Array.isArray(r)?r:[]).filter(Array.isArray).map(p=>{const c=Array.isArray(p[1])?p[1]:[];return [num(p[0],0,0,1),[num(c[0],1,0,1),num(c[1],1,0,1),num(c[2],1,0,1),num(c[3],1,0,1)]];}).slice(0,8);if(l.length<2)l=copy(d);return l.sort((a,b)=>a[0]-b[0]);}
function normLight(l,d){return l===null?null:l&&typeof l==='object'?{color:vec3(l.color,d?.color||[1,1,1],0,1),energy:num(l.energy,d?.energy??1,0,16),range:num(l.range,d?.range??1,.1,100)}:copy(d);}
function normEmission(e,d){const o=e&&typeof e==='object'?e:{},shape=oneOf(o.shape,FX_SHAPES,d.shape),radius=num(o.radius,shape==='ring'?.5:.1,0,100);return {shape,radius,inner_radius:num(o.inner_radius,0,0,radius),height:num(o.height,0,0,100),axis:unit3(vec3(o.axis,[0,1,0],-1,1),[0,1,0]),extents:vec3(o.extents,[.1,.1,.1],0,100)};}
function normAabb(a,d){return Array.isArray(a)&&a.length>=6&&a.slice(0,6).every(x=>Number.isFinite(x))?a.slice(0,6):d.slice();}
const BASE_DEF={amount:16,lifetime:1,one_shot:false,autostart:true,explosiveness:0,randomness:0,local_coords:false,fixed_fps:30,speed_scale:1,direction:[0,1,0],spread:15,velocity:[0,0],gravity:[0,0,0],damping:[0,0],tangential_accel:[0,0],radial_accel:[0,0],angle:[0,0],angular_velocity:[0,0],scale:[1,1],scale_curve:[[0,1],[1,1]],color_ramp:[[0,[1,1,1,1]],[1,[1,1,1,0]]],emission:{shape:'point'},quad_size:.2,texture:'soft',blend:'add',turbulence:0,light:null,visibility_aabb:DEFAULT_AABB};
function build(r,D){return {
 amount:Math.round(num(r.amount,D.amount,1,FX_LIMITS.amount)),lifetime:num(r.lifetime,D.lifetime,.02,60),one_shot:flag(r.one_shot,D.one_shot),autostart:flag(r.autostart,D.autostart),explosiveness:num(r.explosiveness,D.explosiveness,0,1),randomness:num(r.randomness,D.randomness,0,1),local_coords:flag(r.local_coords,D.local_coords),fixed_fps:Math.round(num(r.fixed_fps,D.fixed_fps,1,240)),speed_scale:num(r.speed_scale,D.speed_scale,.05,10),
 direction:nonzero(vec3(r.direction,D.direction,-1000,1000),[0,1,0]),spread:num(r.spread,D.spread,0,180),velocity:range2(r.velocity,D.velocity,-200,200),gravity:vec3(r.gravity,D.gravity,-200,200),damping:range2(r.damping,D.damping,0,200),tangential_accel:range2(r.tangential_accel,D.tangential_accel,-200,200),radial_accel:range2(r.radial_accel,D.radial_accel,-200,200),angle:range2(r.angle,D.angle,-720,720),angular_velocity:range2(r.angular_velocity,D.angular_velocity,-3600,3600),
 scale:range2(r.scale,D.scale,0,100),scale_curve:normCurve(r.scale_curve,D.scale_curve),color_ramp:normRamp(r.color_ramp,D.color_ramp),emission:normEmission(r.emission,D.emission),quad_size:num(r.quad_size,D.quad_size,.001,50),texture:oneOf(r.texture,FX_TEXTURES,D.texture),blend:oneOf(r.blend,FX_BLENDS,D.blend),turbulence:num(r.turbulence,D.turbulence,0,10),light:normLight(r.light,D.light),visibility_aabb:normAabb(r.visibility_aabb,D.visibility_aabb)};}
/** Full emitter parameter set: missing or invalid values come from fb (the preset) or the defaults, unknown keys are dropped, numbers are clamped to their limits. */
export function normalizeParams(raw,fb){const r=raw&&typeof raw==='object'?raw:{};return build(r,fb&&typeof fb==='object'?build(fb,BASE_DEF):BASE_DEF);}
export const FX_PARAM_KEYS=Object.keys(normalizeParams({}));
const META_KEYS=['description','use','reference_size_m','reference_socket_m','_scale','_preset'];
/** Which of the given keys were corrected (not a number, out of range, unknown list value) and which keys are unknown. */
export function validateParams(raw){const issues=[];if(!raw||typeof raw!=='object')return [{key:'*',message:t('fx.validate.notObject')}];const n=normalizeParams(raw);for(const k of Object.keys(raw)){if(META_KEYS.includes(k))continue;if(!FX_PARAM_KEYS.includes(k)){issues.push({key:k,message:t('fx.validate.unknownParam')});continue;}if(raw[k]===undefined)continue;if(k==='light'&&raw[k]===null)continue;if(!subset(raw[k],n[k]))issues.push({key:k,message:t('fx.validate.corrected',{value:JSON.stringify(n[k])})});}return issues;}

// ---------------------------------------------------------------- presets
const PRESET_DATA=FX_PRESETS_DATA.presets;
export const EMPTY_PRESET={get description(){return t('fx.presetDesc.empty');},amount:24,lifetime:1,direction:[0,1,0],spread:25,velocity:[.5,1],gravity:[0,0,0],emission:{shape:'point'},scale:[.8,1.2],scale_curve:[[0,.5],[.2,1],[1,0]],color_ramp:[[0,[1,1,1,1]],[1,[1,1,1,0]]],quad_size:.2,texture:'soft',blend:'add'};
// Titles are getters: the forge-core FX_PRESETS titles follow the UI language, so they are read at call time.
export const FX_PRESET_LIST=[...FORGE_FX.filter(p=>PRESET_DATA[p.id]).map(p=>({id:p.id,get title(){return p.title;}})),{id:FX_EMPTY_ID,get title(){return t('fx.preset.empty');}}];
export const presetId=id=>id===FX_EMPTY_ID||PRESET_DATA[id]?id:FX_EMPTY_ID;
export const presetTitle=id=>FX_PRESET_LIST.find(p=>p.id===id)?.title||t('fx.preset.empty');
export const presetOf=id=>copy(id!==FX_EMPTY_ID&&PRESET_DATA[id]?PRESET_DATA[id]:EMPTY_PRESET);
/** Description shown in the UI: the dictionary entry when there is one, else the description from the preset data. */
export const presetDescription=id=>{const pid=presetId(id);return tOr('fx.presetDesc.'+pid,presetOf(pid).description||'');};

// ---------------------------------------------------------------- auto size and direction of a new effect (after godot_package.py: fx_auto_scale, nozzle exhaust)
/** Engine presets: those whose prefer in the Forge FX_PRESETS is /thruster|engine/ (engine trail, thruster glow). */
export const ENGINE_PRESET_IDS=FORGE_FX.filter(p=>p.prefer&&p.prefer.source==='thruster|engine').map(p=>p.id);
export const isEnginePreset=id=>ENGINE_PRESET_IDS.includes(id);
/** reference_size_m (long axis of the model the preset is tuned for) and reference_socket_m (nozzle/socket diameter); 0 = not set. */
export const presetRefs=id=>{const p=presetOf(id);return {size:+p.reference_size_m||0,socket:+p.reference_socket_m||0};};
export const AUTO_K_RANGE=[.05,50];
const boxSize=b=>[0,1,2].map(i=>b.max[i]-b.min[i]),boxCenter=b=>[0,1,2].map(i=>(b.max[i]+b.min[i])/2);
/** Size, position and direction of an effect placed on a part of a model. Boxes are world AABBs {min:[x,y,z], max:[x,y,z]} or null (no geometry).
 *  model = the whole model (top ancestor of the parent in the scene), part = the effect's parent (a part; the model itself works too).
 *  k: long side of the model / reference_size_m; if the preset has reference_socket_m and the parent is a clearly smaller part (its long side under 60% of the model's),
 *  then part diameter (mean of the two smaller sides) / reference_socket_m. Clamped to [0.05, 50].
 *  Engine preset: the effect axis is the model's long axis pointing out from its centre (towards the part), the point is the centre of the part face farther from the model centre.
 *  Long axis: Z is preferred (glTF models are stretched along Z) if it is at least 80% of the longest side, otherwise a wide wingspan would beat the hull length.
 *  A parent without geometry (a bone) is passed as a point {min:p,max:p}: size comes from the model, the exhaust side from the bone position.
 *  Other presets: the point is the parent box centre, the axis is world +Y. ref can be overridden (tests). */
export function fxAutoPlacement(id,model,part,ref=presetRefs(id)){
 const out={k:1,axis:[0,1,0],point:null,engine:isEnginePreset(id),source:'none',longAxis:-1};if(!model)return out;
 const ms=boxSize(model),L=Math.max(...ms);if(!(L>1e-9))return out;const la=ms[2]>=.8*L?2:ms.indexOf(L);out.longAxis=la;
 const ps=part?boxSize(part):null,small=ps&&Math.max(...ps)>1e-9&&Math.max(...ps)<.6*L;let k=1;
 if(ref.socket>0&&small){const s=[...ps].sort((a,b)=>a-b);k=(s[0]+s[1])/2/ref.socket;out.source='socket';}
 else if(ref.size>0){k=L/ref.size;out.source='model';}
 out.k=Math.round(clamp(k,AUTO_K_RANGE[0],AUTO_K_RANGE[1])*1000)/1000;
 const box=part||model,bc=boxCenter(box);
 if(!out.engine){out.point=bc;return out;}
 const mc=boxCenter(model)[la],lo=box.min[la],hi=box.max[la],dLo=Math.abs(lo-mc),dHi=Math.abs(hi-mc),face=Math.abs(dHi-dLo)>1e-6*L?(dHi>dLo?hi:lo):hi,sign=face>=mc?1:-1;
 out.point=bc.slice();out.point[la]=face;out.axis=[0,0,0];out.axis[la]=sign;return out;}

// ---------------------------------------------------------------- effect data (userData.fx) and edits on top of the preset
const normFlip=f=>{if(!f||typeof f!=='object')return null;const h=Math.round(num(f.h,1,1,32)),v=Math.round(num(f.v,1,1,32));return h*v>1?{h,v,fps:num(f.fps,12,1,120)}:null;};
const normAsset=a=>a&&typeof a==='object'&&typeof a.url==='string'&&a.url?{id:String(a.id??''),url:a.url,name:String(a.name??'')}:null;
let seedCounter=0;
/** New emitter seed (31-bit integer). Math.random is fine here: the seed is created once and saved in the project; the simulation itself never calls it. */
export const newSeed=()=>mix32((Date.now()%4294967296)+Math.floor(Math.random()*4294967296)+(++seedCounter))&0x7fffffff;
export function makeFx(preset=FX_EMPTY_ID,seed=newSeed()){const id=presetId(preset);return {format:FX_FORMAT,preset:id,params:{},texture:oneOf(presetOf(id).texture,FX_TEXTURES,'soft'),textureAsset:null,flipbook:null,startTime:0,seed};}
/** Effective parameters: preset + edits + chosen texture. Always a full, valid set. */
export function resolveFx(fx){const f=fx&&typeof fx==='object'?fx:{},id=presetId(f.preset),base=presetOf(id),merged={...base},o=f.params&&typeof f.params==='object'?f.params:{};for(const k of Object.keys(o))if(FX_PARAM_KEYS.includes(k)&&o[k]!==undefined)merged[k]=o[k];if(FX_TEXTURES.includes(f.texture))merged.texture=f.texture;
 return {...normalizeParams(merged,base),preset:id,textureAsset:normAsset(f.textureAsset),flipbook:normFlip(f.flipbook),startTime:num(f.startTime,0,-1e6,1e6),seed:Math.round(num(f.seed,1,0,2147483647))};}
/** Apply an edit and return a NEW object (the old one is untouched: undo snapshots refer to it).
 *  patch: {preset, params:{key:value}, unset:[keys], reset, texture, textureAsset, flipbook, startTime}. A value equal to the preset is not stored as an edit. */
export function patchFx(fx,patch={}){const base=fx&&typeof fx==='object'?fx:makeFx();let id=presetId(patch.preset!==undefined?patch.preset:base.preset);const out={...base,format:FX_FORMAT,preset:id,params:{...(base.params||{})}};
 if(patch.preset!==undefined&&id!==presetId(base.preset)){out.params={};out.texture=oneOf(presetOf(id).texture,FX_TEXTURES,'soft');out.textureAsset=null;out.flipbook=null;}
 if(patch.reset){out.params={};out.texture=oneOf(presetOf(id).texture,FX_TEXTURES,'soft');out.textureAsset=null;out.flipbook=null;}
 const origin=normalizeParams(presetOf(id));
 for(const k of patch.unset||[])delete out.params[k];
 for(const [k,v] of Object.entries(patch.params||{})){if(k==='texture'){if(FX_TEXTURES.includes(v))out.texture=v;continue;}if(!FX_PARAM_KEYS.includes(k)||v===undefined)continue;const clean=normalizeParams({...presetOf(id),...out.params,[k]:v})[k];if(same(clean,origin[k]))delete out.params[k];else out.params[k]=clean;}
 if(patch.texture!==undefined)out.texture=oneOf(patch.texture,FX_TEXTURES,out.texture||'soft');
 if(patch.textureAsset!==undefined)out.textureAsset=normAsset(patch.textureAsset);
 if(patch.flipbook!==undefined)out.flipbook=normFlip(patch.flipbook);
 if(patch.startTime!==undefined)out.startTime=num(patch.startTime,0,-1e6,1e6);
 if(patch.seed!==undefined)out.seed=Math.round(num(patch.seed,1,0,2147483647));
 return out;}
/** Parameter keys that differ from the preset (to highlight changes in the panel). */
export const changedKeys=fx=>Object.keys(fx?.params||{});

// ---------------------------------------------------------------- scale (merge_fx: scale)
/** All effect lengths are multiplied by k: quad_size, velocities, damping, accelerations, gravity, emission shape, light range, visibility_aabb. */
export function scaleFx(p,k=1){if(!(k>0)||k===1)return p;const q=copy(p),m=a=>a.map(v=>v*k);q.quad_size*=k;for(const key of ['velocity','damping','tangential_accel','radial_accel','gravity'])q[key]=m(q[key]);const e=q.emission;e.radius*=k;e.inner_radius*=k;e.height*=k;e.extents=m(e.extents);if(q.light)q.light.range*=k;q.visibility_aabb=m(q.visibility_aabb||DEFAULT_AABB);return q;}

// ---------------------------------------------------------------- curves
/** Colour gradient: linear interpolation between stops; the result is written to out[o..o+3]. */
export function sampleRamp(ramp,t,out=[0,0,0,0],o=0){const n=ramp.length;if(t<=ramp[0][0]||n===1){const c=ramp[0][1];out[o]=c[0];out[o+1]=c[1];out[o+2]=c[2];out[o+3]=c[3];return out;}
 for(let i=1;i<n;i++){const b=ramp[i];if(t<=b[0]||i===n-1){const a=ramp[i-1],d=b[0]-a[0],f=d>1e-9?clamp((t-a[0])/d,0,1):1;for(let j=0;j<4;j++)out[o+j]=a[1][j]+(b[1][j]-a[1][j])*f;return out;}}return out;}
/** Size curve. Godot writes points with zero tangents in FREE mode, so between points the interpolation is smoothstep, not linear. */
export function sampleCurve(curve,t){const n=curve.length;if(t<=curve[0][0])return curve[0][1];if(t>=curve[n-1][0])return curve[n-1][1];for(let i=1;i<n;i++){const b=curve[i];if(t<=b[0]){const a=curve[i-1],d=b[0]-a[0],f=d>1e-9?(t-a[0])/d:1,s=f*f*(3-2*f);return a[1]+(b[1]-a[1])*s;}}return curve[n-1][1];}

// ---------------------------------------------------------------- random numbers
function mix32(h){h^=h>>>16;h=Math.imul(h,0x85ebca6b);h^=h>>>13;h=Math.imul(h,0xc2b2ae35);h^=h>>>16;return h>>>0;}
export function hash3(a,b,c){let h=mix32((a|0)^0x9e3779b9);h=mix32((h^Math.imul(b|0,0x85ebca6b))>>>0);h=mix32((h^Math.imul(c|0,0xc2b2ae35))>>>0);return h;}
export function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
export function seedFromString(s){let h=2166136261;for(const ch of String(s)){h^=ch.codePointAt(0);h=Math.imul(h,16777619);}return h>>>0;}

/** Exact equivalent of numpy.random.default_rng(seed).random(): SeedSequence → PCG64 (XSL-RR 128/64) → 53-bit double. Needed so the smoke texture matches the Python reference. */
export function numpyRandom(seed){const mul=(a,b)=>Math.imul(a,b)>>>0;let hc=0x43b0d7e5;
 const hashmix=v=>{v=(v^hc)>>>0;hc=mul(hc,0x931e8875);v=mul(v,hc);return (v^(v>>>16))>>>0;},mix=(x,y)=>{const r=(mul(0xca01f9dd,x)-mul(0x4973f715,y))>>>0;return (r^(r>>>16))>>>0;};
 const pool=[0,0,0,0],ent=[seed>>>0];for(let i=0;i<4;i++)pool[i]=hashmix(i<ent.length?ent[i]:0);for(let a=0;a<4;a++)for(let b=0;b<4;b++)if(a!==b)pool[b]=mix(pool[b],hashmix(pool[a]));
 let hb=0x8b51f9dd;const w=[];for(let i=0;i<8;i++){let v=(pool[i%4]^hb)>>>0;hb=mul(hb,0x58f38ded);v=mul(v,hb);w.push((v^(v>>>16))>>>0);}
 const u64=i=>BigInt(w[2*i])|(BigInt(w[2*i+1])<<32n),M64=(1n<<64n)-1n,M128=(1n<<128n)-1n,MULT=(2549297995355413924n<<64n)+4865540595714422341n,inc=(((u64(2)<<64n)+u64(3))<<1n|1n)&M128;let st=0n;
 const step=()=>{st=(st*MULT+inc)&M128;};step();st=(st+(u64(0)<<64n)+u64(1))&M128;step();
 return ()=>{step();const x=((st>>64n)^(st&M64))&M64,rot=Number(st>>122n),o=((x>>BigInt(rot))|(x<<BigInt((64-rot)&63)))&M64;return Number(o>>11n)/9007199254740992;};}

// ---------------------------------------------------------------- particle simulation
function perp(n){const a=Math.abs(n[1])<.99?[0,1,0]:[1,0,0],u=unit3(cross3(a,n),[1,0,0]);return [u,cross3(n,u)];}
/** Prepare parameters for particle evaluation. r = resolveFx(...); k = emitter scale (all lengths are multiplied, see scaleFx). */
export function compileFx(r,k=1){const p=scaleFx(r,k),dirN=unit3(p.direction,[0,1,0]),[dirU,dirV]=perp(dirN),[ringU,ringV]=perp(p.emission.axis),g=p.gravity,gl=len3(g),fb=p.flipbook;
 return {...p,k,dirN,dirU,dirV,ringU,ringV,gN:gl>1e-6?[g[0]/gl,g[1]/gl,g[2]/gl]:[0,-1,0],cosSpread:Math.cos(p.spread*DEG),stepped:Math.abs(p.tangential_accel[0])+Math.abs(p.tangential_accel[1])+Math.abs(p.radial_accel[0])+Math.abs(p.radial_accel[1])>1e-9,frames:fb?fb.h*fb.v:0};}
const lerp2=(m,x)=>m[0]+(m[1]-m[0])*x;
const phaseOf=(c,seed,i,cycle)=>{let p=i/c.amount;if(c.randomness>0)p+=c.randomness*(hash3(seed^0x5bd1e995,i,cycle)/4294967296)/c.amount;return p*(1-c.explosiveness);};
/** When particle i is alive: writes {cycle, age} to out (age in simulation seconds) and returns false if the particle does not exist right now.
 *  ts = simulation time since start (speed_scale applied). Loop: particles fill a cycle of length lifetime (like Godot); for t<0 the emitter counts as "pre-warmed".
 *  one_shot: a single cycle 0, particles are born during the first lifetime*(1-explosiveness). */
export function particleTiming(c,seed,i,ts,out){const L=c.lifetime;if(c.one_shot){const age=ts-phaseOf(c,seed,i,0)*L;if(age<0||age>=L)return false;out.cycle=0;out.age=age;return true;}
 const x=ts/L;let k=Math.floor(x),u=x-k-phaseOf(c,seed,i,k);if(u<0){k--;u=x-k-phaseOf(c,seed,i,k);}if(u>=1&&u<1+1e-9)u=1-1e-12;if(u<0||u>=1)return false;out.cycle=k;out.age=u*L;return true;}
/** Random spawn values of particle (i, cycle): everything that then stays fixed for its life. The number stream is fixed: do not change the call order. */
export function makeSpawn(c,seed,i,cycle){const r=mulberry32(hash3(seed,i,cycle)),u=r,e=c.emission,a=u(),b=u(),cc=u();u();let px=0,py=0,pz=0;
 if(e.shape==='sphere'||e.shape==='sphere_surface'){const z=a*2-1,s=Math.sqrt(Math.max(0,1-z*z)),ph=b*TAU,rad=e.shape==='sphere'?e.radius*Math.cbrt(cc):e.radius;px=s*Math.cos(ph)*rad;py=s*Math.sin(ph)*rad;pz=z*rad;}
 else if(e.shape==='box'){px=(a*2-1)*e.extents[0];py=(b*2-1)*e.extents[1];pz=(cc*2-1)*e.extents[2];}
 else if(e.shape==='ring'){const ph=a*TAU,ri=e.inner_radius,ro=e.radius,rr=Math.sqrt(ri*ri+(ro*ro-ri*ri)*b),h=(cc-.5)*e.height,cu=Math.cos(ph)*rr,sv=Math.sin(ph)*rr;px=c.ringU[0]*cu+c.ringV[0]*sv+e.axis[0]*h;py=c.ringU[1]*cu+c.ringV[1]*sv+e.axis[1]*h;pz=c.ringU[2]*cu+c.ringV[2]*sv+e.axis[2]*h;}
 const q1=u(),q2=u(),cosT=c.cosSpread+(1-c.cosSpread)*q1,sinT=Math.sqrt(Math.max(0,1-cosT*cosT)),ph2=q2*TAU,cp=Math.cos(ph2)*sinT,sp=Math.sin(ph2)*sinT,n=c.dirN,du=c.dirU,dv=c.dirV;
 return {px,py,pz,dx:n[0]*cosT+du[0]*cp+dv[0]*sp,dy:n[1]*cosT+du[1]*cp+dv[1]*sp,dz:n[2]*cosT+du[2]*cp+dv[2]*sp,s0:lerp2(c.velocity,u()),damp:lerp2(c.damping,u()),tan:lerp2(c.tangential_accel,u()),rad:lerp2(c.radial_accel,u()),ang0:lerp2(c.angle,u()),angv:lerp2(c.angular_velocity,u()),scl:lerp2(c.scale,u()),tw:[u(),u(),u(),u(),u(),u()]};}
/** Distance along the direction with linear velocity damping (Godot: damping = speed loss in m/s², down to zero). */
export function dampedDistance(s0,damp,age){if(damp<=0)return s0*age;const s=Math.abs(s0),sg=s0<0?-1:1,ts=s/damp;return sg*(age<ts?s*age-.5*damp*age*age:.5*s*s/damp);}
/** Particle state at age. out: {x,y,z = offset in emitter axes (emission + velocity + radial/tangential + turbulence), gx,gy,gz = gravity offset ½gt²,
 *  size (m), angle (rad), r,g,b,a, frame, u (age/lifetime), age}. Gravity is separate: world axes with local_coords=false, emitter axes with true. */
export function evalParticle(c,sp,age,out){const u=age/c.lifetime;let x=sp.px,y=sp.py,z=sp.pz;
 if(c.stepped){const N=16,dt=age/N,gn=c.gN;let vx=sp.dx*sp.s0,vy=sp.dy*sp.s0,vz=sp.dz*sp.s0;for(let s=0;s<N;s++){const rl=Math.hypot(x,y,z);if(rl>1e-6){const rx=x/rl,ry=y/rl,rz=z/rl;vx+=sp.rad*rx*dt;vy+=sp.rad*ry*dt;vz+=sp.rad*rz*dt;
   const tx=ry*gn[2]-rz*gn[1],ty=rz*gn[0]-rx*gn[2],tz=rx*gn[1]-ry*gn[0],tl=Math.hypot(tx,ty,tz);if(tl>1e-6){const f=sp.tan*dt/tl;vx+=tx*f;vy+=ty*f;vz+=tz*f;}}
   if(sp.damp>0){const v=Math.hypot(vx,vy,vz);if(v>1e-9){const nv=Math.max(0,v-sp.damp*dt),f=nv/v;vx*=f;vy*=f;vz*=f;}}x+=vx*dt;y+=vy*dt;z+=vz*dt;}}
 else{const d=dampedDistance(sp.s0,sp.damp,age);x+=sp.dx*d;y+=sp.dy*d;z+=sp.dz*d;}
 if(c.turbulence>0){const A=c.turbulence*.18*c.k,w=sp.tw,env=Math.min(1,age*3);x+=A*env*(Math.sin((1.5+w[0]*2.5)*age+w[1]*TAU)-Math.sin(w[1]*TAU));y+=A*env*(Math.sin((1.5+w[2]*2.5)*age+w[3]*TAU)-Math.sin(w[3]*TAU));z+=A*env*(Math.sin((1.5+w[4]*2.5)*age+w[5]*TAU)-Math.sin(w[5]*TAU));}
 const h=.5*age*age,g=c.gravity;out.x=x;out.y=y;out.z=z;out.gx=g[0]*h;out.gy=g[1]*h;out.gz=g[2]*h;
 out.size=c.quad_size*sp.scl*sampleCurve(c.scale_curve,u);out.angle=(sp.ang0+sp.angv*age)*DEG;
 const col=sampleRamp(c.color_ramp,u,out._col||(out._col=[0,0,0,0]));out.r=col[0];out.g=col[1];out.b=col[2];out.a=col[3];
 out.frame=c.frames?Math.floor(age*c.flipbook.fps)%c.frames:0;out.u=u;out.age=age;return out;}
/** One whole particle at emitter time t (s since start; for one_shot since the trigger): deterministic by (c, seed, i, t). */
export function particleAt(c,seed,i,t,out={}){const tm={cycle:0,age:0};if(!particleTiming(c,seed,i,t*c.speed_scale,tm)){out.alive=false;return out;}evalParticle(c,makeSpawn(c,seed,i,tm.cycle),tm.age,out);out.alive=true;out.cycle=tm.cycle;return out;}

// ---------------------------------------------------------------- built-in textures (port of make_fx_texture)
const rintEven=x=>{const f=Math.floor(x),d=x-f;return d<.5?f:d>.5?f+1:f%2===0?f:f+1;};
/** White RGBA particle textures; the shape is in the alpha. Returns {width,height,data:Uint8Array RGBA}. smoke: noise from numpyRandom(7) = numpy.random.default_rng(7). */
export function fxTexture(kind,size=64){if(!FX_TEXTURES.includes(kind))throw new Error(t('fx.error.unknownTexture',{kind}));const data=new Uint8Array(size*size*4),c=(size-1)/2,A=new Float64Array(size*size);
 const cl=v=>v<0?0:v>1?1:v;
 if(kind==='smoke'){const rng=numpyRandom(7),noise=new Float64Array(size*size);for(const [oct,amp] of [[4,.5],[8,.3],[16,.2]]){const n1=oct+1,g=new Float64Array(n1*n1);for(let i=0;i<g.length;i++)g[i]=rng();const lin=[...Array(size)].map((_,i)=>size>1?i*oct/(size-1):0),i0=lin.map(v=>clamp(Math.floor(v),0,oct-1)),fr=lin.map((v,i)=>v-i0[i]);
   for(let y=0;y<size;y++)for(let x=0;x<size;x++){const a=i0[y],b=i0[x],fy=fr[y],fx=fr[x];noise[y*size+x]+=amp*(g[a*n1+b]*(1-fy)*(1-fx)+g[(a+1)*n1+b]*fy*(1-fx)+g[a*n1+b+1]*(1-fy)*fx+g[(a+1)*n1+b+1]*fy*fx);}}
  for(let y=0;y<size;y++)for(let x=0;x<size;x++){const dx=(x-c)/c,dy=(y-c)/c,r=Math.hypot(dx,dy);A[y*size+x]=cl(1-r)**1.5*cl(.35+noise[y*size+x]);}}
 else for(let y=0;y<size;y++)for(let x=0;x<size;x++){const dx=(x-c)/c,dy=(y-c)/c,r=Math.hypot(dx,dy);let a;
  if(kind==='soft')a=cl(1-r)**2;else if(kind==='spark')a=cl(1-r)**4+.6*Math.exp(-((r*6)**2));
  else if(kind==='star'){const cross=Math.exp(-((dx*9)**2))*cl(1-Math.abs(dy))**2+Math.exp(-((dy*9)**2))*cl(1-Math.abs(dx))**2;a=cl(cross+cl(1-r*1.6)**2);}
  else a=r<1?Math.exp(-(((r-.75)*7)**2)):0;A[y*size+x]=a;}
 for(let i=0;i<size*size;i++){data[i*4]=data[i*4+1]=data[i*4+2]=255;data[i*4+3]=rintEven(cl(A[i])*255);}
 return {width:size,height:size,data};}

// ---------------------------------------------------------------- uncompressed PNG (stored deflate blocks): used both in the browser and in tests
const CRC=(()=>{const t=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=c&1?0xedb88320^(c>>>1):c>>>1;t[n]=c>>>0;}return t;})();
const crc32=(b,s=0,e=b.length)=>{let c=0xffffffff;for(let i=s;i<e;i++)c=CRC[(c^b[i])&255]^(c>>>8);return (c^0xffffffff)>>>0;};
const adler32=b=>{let a=1,s=0;for(let i=0;i<b.length;i++){a=(a+b[i])%65521;s=(s+a)%65521;}return ((s<<16)|a)>>>0;};
/** 8-bit RGBA PNG. No compression (stored blocks): valid for any decoder, 16 KB for 64×64. */
export function encodePng(width,height,rgba){const row=width*4,raw=new Uint8Array((row+1)*height);for(let y=0;y<height;y++){raw[y*(row+1)]=0;raw.set(rgba.subarray(y*row,(y+1)*row),y*(row+1)+1);}
 const blocks=Math.max(1,Math.ceil(raw.length/65535)),z=new Uint8Array(2+raw.length+blocks*5+4);z[0]=0x78;z[1]=0x01;let o=2;for(let b=0;b<blocks;b++){const s=b*65535,n=Math.min(65535,raw.length-s);z[o++]=b===blocks-1?1:0;z[o++]=n&255;z[o++]=n>>>8;z[o++]=~n&255;z[o++]=(~n>>>8)&255;z.set(raw.subarray(s,s+n),o);o+=n;}
 const ad=adler32(raw);z[o++]=ad>>>24;z[o++]=(ad>>>16)&255;z[o++]=(ad>>>8)&255;z[o++]=ad&255;
 const parts=[],u32=v=>[v>>>24,(v>>>16)&255,(v>>>8)&255,v&255],chunk=(type,data)=>{const b=new Uint8Array(12+data.length);b.set(u32(data.length),0);for(let i=0;i<4;i++)b[4+i]=type.charCodeAt(i);b.set(data,8);b.set(u32(crc32(b,4,8+data.length)),8+data.length);parts.push(b);};
 chunk('IHDR',Uint8Array.from([...u32(width),...u32(height),8,6,0,0,0]));chunk('IDAT',z);chunk('IEND',new Uint8Array(0));
 const sig=Uint8Array.from([137,80,78,71,13,10,26,10]),total=sig.length+parts.reduce((n,p)=>n+p.length,0),out=new Uint8Array(total);out.set(sig,0);let p=sig.length;for(const part of parts){out.set(part,p);p+=part.length;}return out;}
export const fxTexturePng=(kind,size=64)=>{const t=fxTexture(kind,size);return encodePng(t.width,t.height,t.data);};

// ---------------------------------------------------------------- Godot: .tscn resources (port of fx_scene_text)
/** Number formatted as in the Python reference (_f): integers without a point, otherwise up to 6 digits without trailing zeros. */
export function gdF(v){v=Number(v);if(Math.abs(v-Math.round(v))<1e-9)return Math.abs(v)<1e15?String(Math.round(v)+0):String(v);return v.toFixed(6).replace(/0+$/,'').replace(/\.$/,'');}
const gdVec3=v=>'Vector3('+v.slice(0,3).map(gdF).join(', ')+')';
const gdColor=c=>{c=[...c];while(c.length<4)c.push(1);return 'Color('+c.slice(0,4).map(gdF).join(', ')+')';};
const EMISSION_SHAPES={point:0,sphere:1,sphere_surface:2,box:3,ring:6},BLEND_MODES={mix:0,add:1};
/** GPUParticles3D + ParticleProcessMaterial + QuadMesh/StandardMaterial3D (+ OmniLight3D). p = effective parameters (resolveFx, scaleFx if needed), p._preset = tag in metadata.
 *  The effect flies along the node's local +Y. flipbook {h,v,fps} (not in the Python reference): particles_anim_* in the material and anim_speed in ProcessMaterial. */
export function fxSceneText(nodeName,p,textureRel,flipbook=null){const subs=[],ramp=p.color_ramp&&p.color_ramp.length?p.color_ramp:[[0,[1,1,1,1]],[1,[1,1,1,0]]];
 subs.push('[sub_resource type="Gradient" id="Gradient_ramp"]\noffsets = PackedFloat32Array('+ramp.map(r=>gdF(r[0])).join(', ')+')\ncolors = PackedColorArray('+ramp.map(r=>{const c=[...r[1]];while(c.length<4)c.push(1);return c.slice(0,4).map(gdF).join(', ');}).join(', ')+')\n');
 subs.push('[sub_resource type="GradientTexture1D" id="GradientTexture1D_ramp"]\ngradient = SubResource("Gradient_ramp")\n');
 const curve=p.scale_curve&&p.scale_curve.length?p.scale_curve:[[0,1],[1,1]],cmax=Math.max(1,...curve.map(c=>Number(c[1])));
 subs.push('[sub_resource type="Curve" id="Curve_scale"]\nmax_value = '+gdF(cmax)+'\n_data = ['+curve.map(c=>'Vector2('+gdF(c[0])+', '+gdF(c[1])+'), 0.0, 0.0, 0, 0').join(', ')+']\npoint_count = '+curve.length+'\n');
 subs.push('[sub_resource type="CurveTexture" id="CurveTexture_scale"]\ncurve = SubResource("Curve_scale")\n');
 const em=p.emission||{shape:'point'},shape=EMISSION_SHAPES[em.shape||'point'],m=['[sub_resource type="ParticleProcessMaterial" id="ParticleProcessMaterial_fx"]','particle_flag_disable_z = false','emission_shape = '+shape];
 if(shape===1||shape===2)m.push('emission_sphere_radius = '+gdF(em.radius??.1));
 else if(shape===3)m.push('emission_box_extents = '+gdVec3(em.extents||[.1,.1,.1]));
 else if(shape===6){m.push('emission_ring_axis = '+gdVec3(em.axis||[0,1,0]),'emission_ring_height = '+gdF(em.height??0),'emission_ring_radius = '+gdF(em.radius??.5),'emission_ring_inner_radius = '+gdF(em.inner_radius??0));}
 const pr=(k,d)=>p[k]||d,vel=pr('velocity',[0,0]),damp=pr('damping',[0,0]),tan=pr('tangential_accel',[0,0]),rad=pr('radial_accel',[0,0]),ang=pr('angle',[0,0]),angv=pr('angular_velocity',[0,0]),sc=pr('scale',[1,1]),hue=pr('hue_variation',[0,0]);
 m.push('direction = '+gdVec3(pr('direction',[0,1,0])),'spread = '+gdF(p.spread??15),'initial_velocity_min = '+gdF(vel[0]),'initial_velocity_max = '+gdF(vel[1]),'gravity = '+gdVec3(pr('gravity',[0,0,0])),'damping_min = '+gdF(damp[0]),'damping_max = '+gdF(damp[1]),
  'tangential_accel_min = '+gdF(tan[0]),'tangential_accel_max = '+gdF(tan[1]),'radial_accel_min = '+gdF(rad[0]),'radial_accel_max = '+gdF(rad[1]),'angle_min = '+gdF(ang[0]),'angle_max = '+gdF(ang[1]),'angular_velocity_min = '+gdF(angv[0]),'angular_velocity_max = '+gdF(angv[1]),
  'scale_min = '+gdF(sc[0]),'scale_max = '+gdF(sc[1]),'scale_curve = SubResource("CurveTexture_scale")','color_ramp = SubResource("GradientTexture1D_ramp")','hue_variation_min = '+gdF(hue[0]),'hue_variation_max = '+gdF(hue[1]));
 if(flipbook){const frames=flipbook.h*flipbook.v,cycles=flipbook.fps*(p.lifetime??1)/frames;m.push('anim_speed_min = '+gdF(cycles),'anim_speed_max = '+gdF(cycles));}
 if(p.turbulence)m.push('turbulence_enabled = true','turbulence_noise_strength = '+gdF(p.turbulence));
 subs.push(m.join('\n')+'\n');
 const mat=['[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_fx"]','transparency = 1','blend_mode = '+BLEND_MODES[p.blend||'add'],'cull_mode = 2','shading_mode = 0','vertex_color_use_as_albedo = true','albedo_color = '+gdColor(p.albedo||[1,1,1,1]),'albedo_texture = ExtResource("1_tex")','billboard_mode = 3','billboard_keep_scale = true'];
 if(flipbook){const cycles=flipbook.fps*(p.lifetime??1)/(flipbook.h*flipbook.v);mat.push('particles_anim_h_frames = '+flipbook.h,'particles_anim_v_frames = '+flipbook.v,'particles_anim_loop = '+(cycles>1.0001?'true':'false'));}
 subs.push(mat.join('\n')+'\n');
 const q=Number(p.quad_size??.2);subs.push('[sub_resource type="QuadMesh" id="QuadMesh_fx"]\nmaterial = SubResource("StandardMaterial3D_fx")\nsize = Vector2('+gdF(q)+', '+gdF(q)+')\n');
 const node=['[node name="'+nodeName+'" type="GPUParticles3D"]','emitting = '+(p.autostart??true?'true':'false'),'amount = '+Math.trunc(p.amount??16),'lifetime = '+gdF(p.lifetime??1),'one_shot = '+(p.one_shot?'true':'false'),'explosiveness = '+gdF(p.explosiveness??0),'randomness = '+gdF(p.randomness??0),'speed_scale = '+gdF(p.speed_scale??1),'local_coords = '+(p.local_coords?'true':'false'),'fixed_fps = '+Math.trunc(p.fixed_fps??30),'visibility_aabb = AABB('+(p.visibility_aabb||DEFAULT_AABB).map(gdF).join(', ')+')','process_material = SubResource("ParticleProcessMaterial_fx")','draw_pass_1 = SubResource("QuadMesh_fx")','metadata/astraforge_fx = '+JSON.stringify(p._preset??nodeName)];
 let body=node.join('\n')+'\n';const light=p.light;if(light)body+='\n[node name="light" type="OmniLight3D" parent="."]\nlight_color = '+gdColor(light.color||[1,1,1])+'\nlight_energy = '+gdF(light.energy??1)+'\nomni_range = '+gdF(light.range??1)+'\nshadow_enabled = false\n';
 return '[gd_scene load_steps='+(subs.length+2)+' format=3]\n\n[ext_resource type="Texture2D" path="'+textureRel+'" id="1_tex"]\n\n'+subs.join('\n')+'\n'+body;}

// ---------------------------------------------------------------- package for Godot
// Cyrillic -> Latin for file names: keys are the lowercase letters U+0430..U+044F in alphabet order, plus U+0451 (yo).
const TRANSLIT=(()=>{const lat=['a','b','v','g','d','e','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','h','ts','ch','sh','sch','','y','','e','yu','ya'],m={[String.fromCharCode(0x451)]:'e'};lat.forEach((v,i)=>{m[String.fromCharCode(0x430+i)]=v;});return m;})();
/** File name from a title: Latin letters, digits, underscores; Cyrillic text is transliterated; a repeat gets a _2, _3… suffix. */
export function fxSlug(name,used=new Set()){let s=String(name??'').toLowerCase().replace(/^fx\s*[·.:-]\s*/,'').split('').map(ch=>TRANSLIT[ch]??ch).join('').replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'')||'fx';let out=s,n=1;while(used.has(out))out=s+'_'+(++n);used.add(out);return out;}
export const godotNodeName=n=>String(n??'').replace(/[:.@/"%]/g,'_');
const meanScale=s=>Array.isArray(s)?(Math.abs(s[0])+Math.abs(s[1])+Math.abs(s[2]))/3||1:1;
const ASSET_EXT=/\.(png|jpe?g|webp|tga|svg|exr|hdr)$/i;
/** Build the ZIP contents: items=[{name, fx, node, nodePath, parent, transform:{position,rotation,quaternion,scale}}].
 *  Returns {files:[{path, text}|{path, bytes}|{path, url}], bindings, warnings}. The emitter scale (mean of the local scale) is baked into the parameters, like merge_fx(scale=k). */
export function buildFxPackage(items,{rootName='AstraForge'}={}){const files=[],bindings=[],warnings=[],used=new Set(),textures=new Map();
 for(const it of items){const slug=fxSlug(it.name,used),r=resolveFx(it.fx),k=meanScale(it.transform?.scale),p={...scaleFx(r,k),_preset:r.preset};let texFile;
  if(r.textureAsset){const ext=(r.textureAsset.name.match(ASSET_EXT)||r.textureAsset.url.match(ASSET_EXT)||[null,'png'])[1].toLowerCase().replace('jpeg','jpg');texFile=slug+'_tex.'+ext;files.push({path:'fx/textures/'+texFile,url:r.textureAsset.url,asset:r.textureAsset});}
  else{texFile=r.texture+'.png';if(!textures.has(r.texture)){textures.set(r.texture,true);files.push({path:'fx/textures/'+texFile,bytes:fxTexturePng(r.texture)});}}
  const nodeName=godotNodeName(it.node||it.name)||slug;
  files.push({path:'fx/'+slug+'.tscn',text:fxSceneText(nodeName,p,'textures/'+texFile,r.flipbook)});
  const tr=it.transform||{},rot=tr.rotation||[0,0,0];
  if(r.amount*1>FX_LIMITS.amount)warnings.push(t('fx.warn.overLimit',{name:it.name}));
  bindings.push({name:nodeName,scene:'fx/'+slug+'.tscn',parent:it.parent||rootName,parent_godot:godotNodeName(it.parent||rootName),node:it.node||it.name,node_godot:godotNodeName(it.node||it.name),node_path:it.nodePath||'',preset:r.preset,one_shot:r.one_shot,
   transform:{position:(tr.position||[0,0,0]).map(v=>+(+v).toFixed(5)),rotation:rot.map(v=>+(+v).toFixed(5)),rotation_degrees:rot.map(v=>+(v/DEG).toFixed(3)),quaternion:(tr.quaternion||[0,0,0,1]).map(v=>+(+v).toFixed(6)),scale:(tr.scale||[1,1,1]).map(v=>+(+v).toFixed(5))}});}
 files.push({path:'fx_bindings.json',text:JSON.stringify(bindings,null,2)+'\n'});
 files.push({path:'af_fx_mount.gd',text:AF_FX_MOUNT_GD});
 files.push({path:'README.md',text:fxReadme(bindings)});
 return {files,bindings,warnings};}
/** README.md inside the ZIP: what the archive holds and how to hook the effects up in Godot (in the UI language). */
export function fxReadme(bindings){const list=bindings.map(b=>t('fx.readme.item',{scene:b.scene,node:b.node,preset:b.preset,parent:b.parent})+(b.one_shot?t('fx.readme.oneShot'):'')).join('\n')||t('fx.readme.none');
 return t('fx.readme.body',{list});}

