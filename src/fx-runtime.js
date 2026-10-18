// Rendering of effect emitters in the studio viewport. An emitter is a plain Object3D inside content with userData.fx (pure JSON, saved in the project and in the GLB as extras);
// particles, the emitter icon and the light live in a separate overlay group OUTSIDE content, so neither toJSON nor exportGLB/exportOBJ see them.
// Simulation = fx-core.js functions (analytic, deterministic by seed and time); this file only handles time, world positions and GPU buffers.
import * as T from 'three';import JSZip from 'jszip';
import {download} from './api.js';import {transformOf} from './animation.js';import {t} from './i18n/index.js';
import {FX_LIMITS,FX_EMPTY_ID,makeFx,patchFx,resolveFx,compileFx,particleTiming,makeSpawn,evalParticle,presetId,presetTitle,fxTexture,buildFxPackage,newSeed,fxAutoPlacement} from './fx-core.js';

const VERT=`attribute vec3 aPos;attribute vec4 aSR;attribute vec4 aCol;uniform vec2 uGrid;varying vec2 vUv;varying vec4 vCol;
void main(){float c=cos(aSR.y),s=sin(aSR.y);vec2 q=position.xy*aSR.x;vec2 r=vec2(c*q.x-s*q.y,s*q.x+c*q.y);vec4 mv=viewMatrix*vec4(aPos,1.0);mv.xy+=r;gl_Position=projectionMatrix*mv;
 float f=floor(aSR.z+.5);float col=mod(f,uGrid.x),row=floor(f/uGrid.x);vUv=(vec2(col,row)+vec2(position.x+.5,.5-position.y))/uGrid;vCol=aCol;}`;
// Colours are written to the frame as authored (no tone mapping or sRGB conversion): in Godot the same numbers go straight into an unshaded material.
const FRAG=`uniform sampler2D uMap;varying vec2 vUv;varying vec4 vCol;
void main(){vec4 t=texture2D(uMap,vUv);vec4 c=vec4(vCol.rgb*t.rgb,vCol.a*t.a);if(c.a<.003)discard;gl_FragColor=c;}`;
const LIGHT_K=5,MAX_LIGHTS=6,HELPER='#d5df9b',HELPER_SEL='#fff1a5';
const attached=(o,root)=>{for(let n=o;n;n=n.parent)if(n===root)return true;return false;};
const shown=(o,root)=>{for(let n=o;n;n=n.parent){if(!n.visible)return false;if(n===root)return true;}return false;};
const meanAbs=v=>(Math.abs(v.x)+Math.abs(v.y)+Math.abs(v.z))/3||1;
const TM={cycle:0,age:0},EV={},TMP_P=new T.Vector3(),TMP_Q=new T.Quaternion(),CP=new T.Vector3(),CQ=new T.Quaternion(),SC=new T.Vector3(),SP=new T.Vector3(),SQ=new T.Quaternion();
function rot(q,x,y,z,o){const tx=2*(q.y*z-q.z*y),ty=2*(q.z*x-q.x*z),tz=2*(q.x*y-q.y*x);o.x=x+q.w*tx+(q.y*tz-q.z*ty);o.y=y+q.w*ty+(q.z*tx-q.x*tz);o.z=z+q.w*tz+(q.x*ty-q.y*tx);}
const OUT=new T.Vector3();

/** Ring buffer of emitter world poses (every ~1/30 s): where a particle started with local_coords=false. Time is emitter time; a jump back or more than half a second forward resets the history. */
class PoseHistory{
 constructor(cap){this.cap=cap;this.t=new Float64Array(cap);this.p=new Float32Array(cap*3);this.q=new Float32Array(cap*4);this.n=0;this.start=0;}
 idx(i){return (this.start+i)%this.cap;}
 push(t,P,Q){if(this.n){const last=this.idx(this.n-1);if(t<this.t[last]-1e-6||t-this.t[last]>.5){this.n=0;this.start=0;}else if(t-this.t[last]<1/30)return;}
  let j;if(this.n<this.cap){j=this.idx(this.n);this.n++;}else{j=this.start;this.start=(this.start+1)%this.cap;}this.t[j]=t;this.p[j*3]=P.x;this.p[j*3+1]=P.y;this.p[j*3+2]=P.z;this.q[j*4]=Q.x;this.q[j*4+1]=Q.y;this.q[j*4+2]=Q.z;this.q[j*4+3]=Q.w;}
 /** Pose at time tb; with no record (earlier than the oldest) the current one. tNow/P/Q = the current pose as the "next" record. */
 pose(tb,tNow,P,Q,outP,outQ){if(!this.n||tb<this.t[this.start]-1e-9){outP.copy(P);outQ.copy(Q);return;}
  let lo=0,hi=this.n-1;while(lo<hi){const mid=(lo+hi+1)>>1;if(this.t[this.idx(mid)]<=tb)lo=mid;else hi=mid-1;}
  const a=this.idx(lo);SP.set(this.p[a*3],this.p[a*3+1],this.p[a*3+2]);SQ.set(this.q[a*4],this.q[a*4+1],this.q[a*4+2],this.q[a*4+3]);
  let t1,P1=P,Q1=Q;if(lo+1<this.n){const b=this.idx(lo+1);t1=this.t[b];P1=CP.set(this.p[b*3],this.p[b*3+1],this.p[b*3+2]);Q1=CQ.set(this.q[b*4],this.q[b*4+1],this.q[b*4+2],this.q[b*4+3]);}else t1=tNow;
  const d=t1-this.t[a],f=d>1e-9?Math.min(1,Math.max(0,(tb-this.t[a])/d)):1;outP.copy(SP).lerp(P1,f);outQ.copy(SQ).slerp(Q1,f);}
}
/** Particle textures: built-in ones from fx-core (DataTexture, no canvas) and assets by url. One instance per key. */
class FxTextures{
 constructor(){this.map=new Map();}
 get(kind,asset){const key=asset?'a:'+asset.url:'b:'+kind;let e=this.map.get(key);if(e)return e;let tex;
  if(!asset){const t=fxTexture(kind,64);tex=new T.DataTexture(t.data,t.width,t.height,T.RGBAFormat);tex.generateMipmaps=false;tex.minFilter=tex.magFilter=T.LinearFilter;e={tex,ready:true};}
  else{tex=new T.Texture();tex.generateMipmaps=false;tex.minFilter=tex.magFilter=T.LinearFilter;e={tex,ready:false,failed:false};const img=new Image();img.crossOrigin='anonymous';img.onload=()=>{tex.image=img;tex.needsUpdate=true;e.ready=true;};img.onerror=()=>{e.failed=true;};img.src=asset.url;}
  tex.flipY=false;tex.colorSpace=T.NoColorSpace;tex.wrapS=tex.wrapT=T.ClampToEdgeWrapping;if(!asset)tex.needsUpdate=true;this.map.set(key,e);return e;}
 dispose(){for(const e of this.map.values())e.tex.dispose();this.map.clear();}
}
export class FxRuntime{
 constructor(studio){this.s=studio;this.overlay=new T.Group();this.overlay.name='FX overlay';this.helpers=new T.Group();this.overlay.add(this.helpers);studio.scene.add(this.overlay);
  this.items=new Map();this.textures=new FxTextures();this.live=0;this.lastReal=performance.now();this.frame=0;this.dirty=true;this.tabOpen=false;this.pickables=[];this.lightsUsed=0;
  this.plane=new T.PlaneGeometry(1,1);const circle=(a,b)=>{const pts=[];for(let i=0;i<32;i++){const t0=i/32*Math.PI*2,t1=(i+1)/32*Math.PI*2;pts.push(new T.Vector3(),new T.Vector3());const set=(v,t)=>{const c=Math.cos(t)*.5,s=Math.sin(t)*.5;v.set(0,0,0);v[a]=c;v[b]=s;};set(pts[pts.length-2],t0);set(pts[pts.length-1],t1);}return pts;};
  const arrow=[new T.Vector3(0,0,0),new T.Vector3(0,1.4,0),new T.Vector3(0,1.4,0),new T.Vector3(.12,1.15,0),new T.Vector3(0,1.4,0),new T.Vector3(-.12,1.15,0),new T.Vector3(0,1.4,0),new T.Vector3(0,1.15,.12),new T.Vector3(0,1.4,0),new T.Vector3(0,1.15,-.12)];
  this.helperGeo=new T.BufferGeometry().setFromPoints([...circle('x','y'),...circle('y','z'),...circle('x','z'),...arrow]);this.pickGeo=new T.SphereGeometry(.75,8,6);this.pickMat=new T.MeshBasicMaterial({visible:false});}
 // ---- emitter registry
 scan(){const found=[];this.s.content.traverse(o=>{if(o.userData?.fx)found.push(o);});const seen=new Set();
  for(const o of found){seen.add(o.uuid);let it=this.items.get(o.uuid);if(!it){it=this.create(o);this.items.set(o.uuid,it);}it.obj=o;}
  let changed=false;for(const [id,it] of this.items)if(!seen.has(id)){this.destroy(it);this.items.delete(id);changed=true;}
  if(changed||found.length!==this.pickables.length)this.pickables=[...this.items.values()].map(it=>it.pick);this.overlay.visible=this.items.size>0;}
 create(obj){const it={id:obj.uuid,obj,fx:null,res:null,cfg:null,k:1,cap:0,mesh:null,geo:null,mat:null,tex:null,spawn:[],cycle:null,pose:null,hist:null,limit:0,light:null,helper:null,pick:null,liveEpoch:this.live,sort:null,count:0};
  it.mat=new T.ShaderMaterial({vertexShader:VERT,fragmentShader:FRAG,uniforms:{uMap:{value:null},uGrid:{value:new T.Vector2(1,1)}},transparent:true,depthWrite:false,depthTest:true,side:T.DoubleSide,toneMapped:false,fog:false});
  const h=new T.LineSegments(this.helperGeo,new T.LineBasicMaterial({color:HELPER,transparent:true,opacity:.75,depthTest:false}));h.renderOrder=102;h.frustumCulled=false;const pick=new T.Mesh(this.pickGeo,this.pickMat);pick.userData.fxObject=obj;h.add(pick);this.helpers.add(h);it.helper=h;it.pick=pick;return it;}
 destroy(it){if(it.mesh){this.overlay.remove(it.mesh);it.geo.dispose();}it.mat.dispose();if(it.light){this.overlay.remove(it.light);it.light.dispose?.();}this.helpers.remove(it.helper);it.helper.material.dispose();}
 /** Emitter parameters changed (new userData.fx object): rebuild whatever depends on them. */
 configure(it){const fx=it.obj.userData.fx,res=resolveFx(fx),first=!it.res;it.fx=fx;it.res=res;it.cfg=null;it.spawn=[];if(it.cycle)it.cycle.fill(-2147483648);
  if(res.amount!==it.cap)this.realloc(it,res.amount);
  const hcap=Math.min(2000,Math.ceil(res.lifetime/res.speed_scale*30)+12);if(!it.hist||it.hist.cap!==hcap)it.hist=new PoseHistory(hcap);else it.hist.n=0;
  const e=this.textures.get(res.texture,res.textureAsset);it.tex=e;it.mat.uniforms.uMap.value=e.tex;it.mat.uniforms.uGrid.value.set(res.flipbook?.h||1,res.flipbook?.v||1);
  it.mat.blending=res.blend==='mix'?T.NormalBlending:T.AdditiveBlending;if(!first&&res.one_shot)it.liveEpoch=this.live;}
 realloc(it,cap){if(it.mesh){this.overlay.remove(it.mesh);it.geo.dispose();}const g=new T.InstancedBufferGeometry();g.index=this.plane.index;g.setAttribute('position',this.plane.attributes.position);g.setAttribute('uv',this.plane.attributes.uv);
  for(const [name,size] of [['aPos',3],['aSR',4],['aCol',4]])g.setAttribute(name,new T.InstancedBufferAttribute(new Float32Array(cap*size),size).setUsage(T.DynamicDrawUsage));g.instanceCount=0;
  const m=new T.Mesh(g,it.mat);m.frustumCulled=false;m.renderOrder=8;m.visible=false;m.userData.fxId=it.id;this.overlay.add(m);it.mesh=m;it.geo=g;it.cap=cap;it.cycle=new Int32Array(cap).fill(-2147483648);it.pose=new Float32Array(cap*7);it.sort=null;it.spawn=[];}
 // ---- frame
 tick(){const s=this.s,real=performance.now(),dt=Math.min(.1,(real-this.lastReal)/1000);this.lastReal=real;this.live+=dt;this.frame++;
  if(this.dirty||this.frame%6===0){this.scan();this.dirty=false;}
  const timeline=s.playing||s.recording,time=s.editor.time;let used=0,lights=0;
  for(const it of this.items.values()){const o=it.obj;if(!attached(o,s.content)){it.helper.visible=false;if(it.mesh)it.mesh.visible=false;if(it.light)it.light.intensity=0;continue;}
   if(it.fx!==o.userData.fx)this.configure(it);
   o.updateWorldMatrix(true,false);o.matrixWorld.decompose(CP,CQ,SC);const P=CP.clone(),Q=CQ.clone();
   const k=meanAbs(o.scale);if(!it.cfg||Math.abs(k-it.k)>1e-6){it.k=k;it.cfg=compileFx(it.res,k);it.spawn=[];it.cycle.fill(-2147483648);}
   const res=it.res,vis=shown(o,s.content),t=timeline?(res.one_shot?time-res.startTime:time):this.live-it.liveEpoch;
   it.limit=Math.max(0,Math.min(res.amount,FX_LIMITS.total-used));used+=it.limit;
   this.simulate(it,t,P,Q,vis);
   // light
   const L=it.cfg.light;if(L&&vis&&it.count>0&&lights<MAX_LIGHTS){lights++;if(!it.light){it.light=new T.PointLight('#fff',0,1,2);this.overlay.add(it.light);}
    const fade=res.one_shot?Math.max(0,1-Math.max(0,t)/Math.max(.05,res.lifetime)):1;it.light.color.setRGB(L.color[0],L.color[1],L.color[2],T.SRGBColorSpace);it.light.intensity=L.energy*LIGHT_K*fade;it.light.distance=L.range;it.light.position.copy(P);}
   else if(it.light)it.light.intensity=0;
   // icon
   const sel=s.selected===o,showHelper=!s.recording&&shown(o,s.content)&&(this.tabOpen||sel);it.helper.visible=showHelper;
   if(showHelper){it.helper.position.copy(P);it.helper.quaternion.copy(Q);it.helper.scale.setScalar(Math.min(2,Math.max(.03,s.camera.position.distanceTo(P)*.035)));it.helper.material.color.set(sel?HELPER_SEL:HELPER);it.helper.material.opacity=sel?.95:.6;}}
  this.lightsUsed=lights;}
 simulate(it,t,P,Q,vis){const cfg=it.cfg,res=it.res,mesh=it.mesh,g=it.geo,n=it.limit,speed=cfg.speed_scale,aPos=g.attributes.aPos,aSR=g.attributes.aSR,aCol=g.attributes.aCol,local=cfg.local_coords;
  if(it.tex.failed&&res.textureAsset){it.tex=this.textures.get(res.texture,null);it.mat.uniforms.uMap.value=it.tex.tex;}
  if(!vis||!it.tex.ready||n===0){mesh.visible=false;it.count=0;return;}
  if(!local)it.hist.push(t,P,Q);
  const cam=this.s.camera.position,mix=res.blend==='mix';let c=0;const pa=aPos.array,sa=aSR.array,ca=aCol.array;
  for(let i=0;i<n;i++){if(!particleTiming(cfg,res.seed,i,t*speed,TM))continue;
   let sp=it.spawn[i];if(!sp||it.cycle[i]!==TM.cycle){sp=it.spawn[i]=makeSpawn(cfg,res.seed,i,TM.cycle);it.cycle[i]=TM.cycle;
    if(!local){it.hist.pose(t-TM.age/speed,t,P,Q,TMP_P,TMP_Q);const o7=i*7;it.pose[o7]=TMP_P.x;it.pose[o7+1]=TMP_P.y;it.pose[o7+2]=TMP_P.z;it.pose[o7+3]=TMP_Q.x;it.pose[o7+4]=TMP_Q.y;it.pose[o7+5]=TMP_Q.z;it.pose[o7+6]=TMP_Q.w;}}
   evalParticle(cfg,sp,TM.age,EV);let wx,wy,wz;
   if(local){rot(Q,EV.x+EV.gx,EV.y+EV.gy,EV.z+EV.gz,OUT);wx=P.x+OUT.x;wy=P.y+OUT.y;wz=P.z+OUT.z;}
   else{const o7=i*7;SQ.set(it.pose[o7+3],it.pose[o7+4],it.pose[o7+5],it.pose[o7+6]);rot(SQ,EV.x,EV.y,EV.z,OUT);wx=it.pose[o7]+OUT.x+EV.gx;wy=it.pose[o7+1]+OUT.y+EV.gy;wz=it.pose[o7+2]+OUT.z+EV.gz;}
   pa[c*3]=wx;pa[c*3+1]=wy;pa[c*3+2]=wz;sa[c*4]=EV.size;sa[c*4+1]=EV.angle;sa[c*4+2]=EV.frame;sa[c*4+3]=0;ca[c*4]=EV.r;ca[c*4+1]=EV.g;ca[c*4+2]=EV.b;ca[c*4+3]=EV.a;c++;}
  if(mix&&c>1&&c<=2000)this.sortBack(it,c,cam);
  for(const a of [aPos,aSR,aCol]){a.clearUpdateRanges();a.addUpdateRange(0,c*a.itemSize);a.needsUpdate=true;}
  g.instanceCount=c;it.count=c;mesh.visible=c>0;}
 /** Normal blending needs back-to-front order: rebuild the first c records of the buffers. */
 sortBack(it,c,cam){const g=it.geo,pa=g.attributes.aPos.array,sa=g.attributes.aSR.array,ca=g.attributes.aCol.array;
  if(!it.sort){it.sort={idx:new Array(it.cap),d:new Float32Array(it.cap),p:new Float32Array(it.cap*3),s:new Float32Array(it.cap*4),c:new Float32Array(it.cap*4)};}
  const S=it.sort,idx=S.idx;for(let i=0;i<c;i++){idx[i]=i;const dx=pa[i*3]-cam.x,dy=pa[i*3+1]-cam.y,dz=pa[i*3+2]-cam.z;S.d[i]=dx*dx+dy*dy+dz*dz;}
  const order=idx.slice(0,c).sort((a,b)=>S.d[b]-S.d[a]);
  for(let j=0;j<c;j++){const i=order[j];S.p[j*3]=pa[i*3];S.p[j*3+1]=pa[i*3+1];S.p[j*3+2]=pa[i*3+2];for(let m=0;m<4;m++){S.s[j*4+m]=sa[i*4+m];S.c[j*4+m]=ca[i*4+m];}}
  pa.set(S.p.subarray(0,c*3));sa.set(S.s.subarray(0,c*4));ca.set(S.c.subarray(0,c*4));}
 dispose(){for(const it of this.items.values())this.destroy(it);this.items.clear();this.s.scene.remove(this.overlay);this.textures.dispose();this.plane.dispose();this.helperGeo.dispose();this.pickGeo.dispose();this.pickMat.dispose();}
}

/** Hook into StudioScene (like rig-tools / media-tools): effect methods plus patches of snapshot, select, duplicate and remove. */
export function installFxTools(Studio){const p=Studio.prototype;
 p.initFxTools=function(){this.fx=new FxRuntime(this);this.fxEdit={key:'',at:0};};
 p.fxTick=function(){this.fx?.tick();};
 p.setFxTab=function(open){if(this.fx)this.fx.tabOpen=!!open;};
 const nodeOf=(s,id)=>{const o=s.content.getObjectByProperty('uuid',id);return o?.userData?.fx?o:null;};
 /** Where a new effect goes: the selected object or bone (for a selected emitter, its parent), else the scene root. */
 p.fxParent=function(){let o=this.selected;if(o?.userData.fx)o=o.parent;return o&&attached(o,this.content)?o:this.content;};
 p.fxList=function(){const out=[];this.content.traverse(o=>{if(o.userData.fx)out.push({id:o.uuid,name:o.name,visible:o.visible,shown:shown(o,this.content),parent:o.parent===this.content?null:o.parent?.name||null,fx:o.userData.fx});});return out;};
 p.fxBudget=function(){let requested=0;this.content.traverse(o=>{if(o.userData.fx)requested+=resolveFx(o.userData.fx).amount;});return {requested,limit:FX_LIMITS.total,over:requested>FX_LIMITS.total};};
 /** Auto size and direction of a new emitter from the model (fxAutoPlacement): k goes to the emitter scale, the point to its position, the +Y axis to its rotation (parent world rotation and scale accounted for). */
 p.fxAutoPlace=function(o,parent,id){this.content.updateMatrixWorld(true);let model=parent;if(parent!==this.content)while(model.parent&&model.parent!==this.content)model=model.parent;
  const box=ob=>{const b=new T.Box3().setFromObject(ob);return b.isEmpty()?null:{min:b.min.toArray(),max:b.max.toArray()};},mb=box(model),wp=parent.getWorldPosition(new T.Vector3()).toArray(),pb=parent===this.content?null:box(parent)||{min:wp,max:wp},r=fxAutoPlacement(id,mb,pb);if(!r.point)return r;
  o.scale.setScalar(r.k);const inv=parent.matrixWorld.clone().invert();
  const local=parent.worldToLocal(new T.Vector3(...r.point));if(Number.isFinite(local.x+local.y+local.z))o.position.copy(local);
  const dir=new T.Vector3(...r.axis).transformDirection(inv);if(Number.isFinite(dir.x+dir.y+dir.z)&&dir.lengthSq()>.5)o.quaternion.setFromUnitVectors(new T.Vector3(0,1,0),dir);return r;};
 p.addFx=function(preset=FX_EMPTY_ID){const id=presetId(preset),parent=this.fxParent();this.checkpoint();const o=new T.Object3D();o.name='FX · '+presetTitle(id);o.userData={fx:makeFx(id,newSeed())};parent.add(o);try{this.fxAutoPlace(o,parent,id);}catch(e){console.warn('Effect auto size skipped:',e);}this.editor.bases[o.uuid]=transformOf(o);this.fx.dirty=true;this.select(o.uuid);return o.uuid;};
 /** Edit an effect; patch is as for patchFx plus name. Consecutive edits of one field (slider, typing a number) merge into one undo step. */
 p.updateFx=function(id,patch){const o=nodeOf(this,id);if(!o)return false;const {name,...rest}=patch,key=id+':'+(name!==undefined?'name':'')+Object.keys(rest.params||{}).join(',')+Object.keys(rest).filter(k=>k!=='params').join(','),now=performance.now();
  if(key===this.fxEdit.key&&now-this.fxEdit.at<900){if(this.editRevision!==undefined)this.editRevision++;}else this.checkpoint();this.fxEdit={key,at:now};
  if(name!==undefined)o.name=String(name).slice(0,100);if(Object.keys(rest).length)o.userData={...o.userData,fx:patchFx(o.userData.fx,rest)};this.emit();return true;};
 /** Restart a one-shot emitter: remembers the trigger time on the timeline and restarts the live clock. */
 p.triggerFx=function(id){const o=nodeOf(this,id);if(!o)return false;this.updateFx(id,{startTime:Math.round(this.editor.time*100)/100});const it=this.fx.items.get(id);if(it)it.liveEpoch=this.fx.live;return true;};
 const snapshot=p.snapshot;
 p.snapshot=function(){const st=snapshot.call(this);if(!this.fx)return st;const list=this.fxList(),ids=new Set(list.map(e=>e.id));for(const o of st.objects)if(ids.has(o.id))o.fx=true;
  const sel=this.selected?.userData?.fx?this.selected:null,parent=this.fxParent();
  st.fx={list:list.map(e=>({id:e.id,name:e.name,visible:e.visible,shown:e.shown,parent:e.parent,preset:e.fx.preset,oneShot:!!resolveFx(e.fx).one_shot})),selected:sel?{id:sel.uuid,name:sel.name,fx:sel.userData.fx,res:resolveFx(sel.userData.fx),parent:sel.parent===this.content?null:sel.parent?.name||null}:null,
   target:parent===this.content?null:parent.name||t('fx.target.object'),budget:this.fxBudget()};return st;};
 const select=p.select;
 p.select=function(id){select.call(this,id);if(this.selected?.userData.fx)this.box.visible=false;};
 const rigPick=p.rigPick;
 p.rigPick=function(){const bone=rigPick.call(this);if(bone||!this.fx)return bone;const hit=this.ray.intersectObjects(this.fx.pickables.filter(m=>m.parent.visible),false)[0];const o=hit?.object.userData.fxObject;return o&&attached(o,this.content)?o:null;};
 const duplicate=p.duplicate;
 p.duplicate=function(){duplicate.call(this);this.selected?.traverse(o=>{if(o.userData.fx)o.userData={...o.userData,fx:{...o.userData.fx,seed:newSeed()}};});this.fx.dirty=true;this.emit();};
 const frame=p.frame;
 p.frame=function(){const o=this.selected;if(o?.userData.fx){const box=new T.Box3().setFromCenterAndSize(o.getWorldPosition(new T.Vector3()),new T.Vector3(1,1,1));const center=box.getCenter(new T.Vector3()),distance=Math.max(2.5,this.camera.position.distanceTo(this.orbit.target));this.orbit.target.copy(center);this.camera.position.copy(center).add(new T.Vector3(1,.65,1).normalize().multiplyScalar(distance));this.orbit.update();return;}frame.call(this);};
 const restore=p.restore;
 p.restore=async function(saved){await restore.call(this,saved);if(this.fx)this.fx.dirty=true;};
 const dispose=p.dispose;
 p.dispose=function(){this.fx?.dispose();dispose.call(this);};
 /** ZIP for Godot: fx/<name>.tscn per effect, fx/textures, fx_bindings.json, af_fx_mount.gd, README.md. */
 p.exportFxPackage=async function(){const items=[];this.content.updateMatrixWorld(true);
  this.content.traverse(o=>{if(!o.userData.fx)return;const path=[];for(let n=o;n;n=n.parent){path.unshift(n.name||'');if(n===this.content)break;}
   items.push({name:o.name,fx:o.userData.fx,node:o.name,nodePath:path.join('/'),parent:o.parent===this.content?null:o.parent?.name||null,transform:{position:o.position.toArray(),rotation:[o.rotation.x,o.rotation.y,o.rotation.z],quaternion:o.quaternion.toArray(),scale:o.scale.toArray()}});});
  if(!items.length)throw new Error(t('fx.export.noEffects'));
  const pkg=buildFxPackage(items,{rootName:this.content.name||'AstraForge'}),zip=new JSZip();
  for(const f of pkg.files){if(f.text!==undefined)zip.file(f.path,f.text);else if(f.bytes)zip.file(f.path,f.bytes);
   else{const r=await fetch(f.url);if(!r.ok)throw new Error(t('fx.export.textureMissing',{name:f.asset?.name||f.url}));zip.file(f.path,await r.arrayBuffer());}}
  await download(await zip.generateAsync({type:'blob',compression:'DEFLATE'}),'AstraForge-FX.zip');return pkg;};
}
