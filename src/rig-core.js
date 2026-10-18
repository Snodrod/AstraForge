import * as T from 'three';
import {transformOf,evaluateTransform} from './animation.js';
import {t as tr} from './i18n/index.js';

export function bonesOf(root){const out=[];root?.traverse(o=>{if(o.isBone)out.push(o);});return out;}
export function meshesOf(root){const out=[];root?.traverse(o=>{if(o.isMesh)out.push(o);});return out;}
export function localBounds(root){root.updateWorldMatrix(true,true);const inv=root.matrixWorld.clone().invert(),box=new T.Box3(),v=new T.Vector3();for(const mesh of meshesOf(root)){const matrix=inv.clone().multiply(mesh.matrixWorld),p=mesh.geometry.attributes.position;for(let i=0;i<p.count;i++)box.expandByPoint(v.fromBufferAttribute(p,i).applyMatrix4(matrix));}if(box.isEmpty())throw new Error(tr('rig.error.noGeometry'));return box;}
export function createSkeleton(root,type='humanoid'){
 if(bonesOf(root).length)throw new Error(tr('rig.error.hasSkeleton'));
 const box=localBounds(root),size=box.getSize(new T.Vector3()),center=box.getCenter(new T.Vector3()),h=Math.max(size.y,.1),w=Math.max(size.x,h*.35),d=Math.max(size.z,h*.25),x=center.x,z=center.z,y=box.min.y;
 const records=new Map(),bones=[];
 const add=(name,parent,absolute,deform=true)=>{const b=new T.Bone();b.name=name;const p=new T.Vector3(...absolute);b.position.copy(p);if(parent){b.position.sub(records.get(parent).position);records.get(parent).bone.add(b);}else root.add(b);b.userData.deform=deform;b.userData.rigRest=transformOf(b);records.set(name,{bone:b,position:p});bones.push(b);return b;};
 add('Root',null,[x,y,z],false);
 if(type==='humanoid'){
  add('Hips','Root',[x,y+h*.5,z]);add('Spine','Hips',[x,y+h*.63,z]);add('Chest','Spine',[x,y+h*.73,z]);add('Neck','Chest',[x,y+h*.84,z]);add('Head','Neck',[x,y+h*.92,z]);add('HeadEnd','Head',[x,y+h,z],false);
  for(const [side,s] of [['Left',1],['Right',-1]]){
   add(side+'Shoulder','Chest',[x+s*w*.16,y+h*.76,z]);add(side+'UpperArm',side+'Shoulder',[x+s*w*.27,y+h*.76,z]);add(side+'LowerArm',side+'UpperArm',[x+s*w*.38,y+h*.76,z]);add(side+'Hand',side+'LowerArm',[x+s*w*.47,y+h*.76,z]);add(side+'HandEnd',side+'Hand',[x+s*w*.5,y+h*.76,z],false);
   add(side+'UpperLeg','Hips',[x+s*w*.09,y+h*.47,z]);add(side+'LowerLeg',side+'UpperLeg',[x+s*w*.09,y+h*.26,z]);add(side+'Foot',side+'LowerLeg',[x+s*w*.09,y+h*.055,z]);add(side+'Toes',side+'Foot',[x+s*w*.09,y+h*.035,z+d*.4]);add(side+'ToesEnd',side+'Toes',[x+s*w*.09,y+h*.035,z+d*.5],false);
  }
 }else if(type==='quadruped'){
  add('Hips','Root',[x,y+h*.65,z-d*.25]);add('Spine','Hips',[x,y+h*.7,z]);add('Chest','Spine',[x,y+h*.7,z+d*.25]);add('Neck','Chest',[x,y+h*.85,z+d*.38]);add('Head','Neck',[x,y+h*.9,z+d*.47]);
  for(const [end,zz,parent] of [['Front',z+d*.26,'Chest'],['Back',z-d*.28,'Hips']])for(const [side,s] of [['Left',1],['Right',-1]]){const name=end+side;add(name+'UpperLeg',parent,[x+s*w*.28,y+h*.62,zz]);add(name+'LowerLeg',name+'UpperLeg',[x+s*w*.28,y+h*.31,zz]);add(name+'Paw',name+'LowerLeg',[x+s*w*.28,y+h*.04,zz+d*.07]);}
  add('Tail','Hips',[x,y+h*.62,z-d*.42]);add('TailEnd','Tail',[x,y+h*.72,z-d*.65]);
 }else if(type==='mechanical'){
  add('Body','Root',[x,y+h*.5,z]);add('LeftWing','Body',[x+w*.3,y+h*.5,z]);add('RightWing','Body',[x-w*.3,y+h*.5,z]);add('Front','Body',[x,y+h*.5,z+d*.3]);add('Rear','Body',[x,y+h*.5,z-d*.3]);
 }else{add('Body','Root',[x,y+h*.5,z]);add('Tip','Body',[x,y+h*.9,z]);}
 root.userData.rig={version:1,type,bound:false};root.updateMatrixWorld(true);return bones;
}
export function resetBones(root){for(const b of bonesOf(root)){const r=b.userData.rigRest;if(r){b.position.fromArray(r.position);b.rotation.fromArray([...r.rotation,'XYZ']);b.scale.fromArray(r.scale);}}root.updateWorldMatrix(true,true);}
export function saveRest(root){for(const b of bonesOf(root))b.userData.rigRest=transformOf(b);root.updateWorldMatrix(true,true);const skels=new Set(meshesOf(root).filter(m=>m.isSkinnedMesh).map(m=>m.skeleton));for(const s of skels)s.calculateInverses();}
export function segmentDistance2(p,a,b){let len=0,dot=0;for(let i=0;i<3;i++){const axis=b[i]-a[i];len+=axis*axis;dot+=(p[i]-a[i])*axis;}const t=len?Math.min(1,Math.max(0,dot/len)):0;let d=0;for(let i=0;i<3;i++)d+=(p[i]-(a[i]+(b[i]-a[i])*t))**2;return d;}
function hierarchyDistance(a,b){const ancestors=new Map();for(let o=a,n=0;o?.isBone;o=o.parent,n++)ancestors.set(o,n);for(let o=b,n=0;o?.isBone;o=o.parent,n++)if(ancestors.has(o))return n+ancestors.get(o);return Infinity;}
function influenceSegments(root,bones){const inv=root.matrixWorld.clone().invert();return bones.map((bone,index)=>{const a=bone.getWorldPosition(new T.Vector3()).applyMatrix4(inv).toArray();const child=bone.children.find(o=>o.isBone);const b=child?child.getWorldPosition(new T.Vector3()).applyMatrix4(inv).toArray():a;return {index,bone,a,b,enabled:bone.userData.deform!==false};}).filter(x=>x.enabled);}
export function closestInfluences(point,segments,epsilon=.02,rigid=false){
 const ranked=segments.map(s=>({s,d:segmentDistance2(point,s.a,s.b)})).sort((a,b)=>a.d-b.d);const anchor=ranked[0];if(!anchor)throw new Error(tr('rig.error.noDeformBones'));
 const nearby=ranked.filter(x=>hierarchyDistance(anchor.s.bone,x.s.bone)<=4).slice(0,rigid?1:4);const values=nearby.map(x=>1/(x.d+epsilon**2)**2),sum=values.reduce((a,b)=>a+b,0);return nearby.map((x,i)=>({index:x.s.index,weight:values[i]/sum}));
}
export async function bindSkeleton(root,{rigid=false,progress=()=>{},yieldWork=()=>new Promise(r=>setTimeout(r,0))}={}){
 const bones=bonesOf(root);if(bones.length<2)throw new Error(tr('rig.error.twoBones'));if(bones.length>256)throw new Error(tr('rig.error.maxBones'));
 resetBones(root);const inv=root.matrixWorld.clone().invert(),segments=influenceSegments(root,bones),extent=localBounds(root).getSize(new T.Vector3()).length(),epsilon=Math.max(extent*.015,.00001),targets=meshesOf(root).filter(m=>!m.isSkinnedMesh),total=targets.reduce((n,m)=>n+m.geometry.attributes.position.count,0);let completed=0;
 if(!targets.length)throw new Error(tr('rig.error.alreadyBound'));
 const skeleton=new T.Skeleton(bones);for(const mesh of targets){
  const geometry=new T.BufferGeometry().copy(mesh.geometry),toRoot=inv.clone().multiply(mesh.matrixWorld);if(!geometry.attributes.normal)geometry.computeVertexNormals();const p=geometry.attributes.position,indices=new Uint16Array(p.count*4),weights=new Float32Array(p.count*4),point=new T.Vector3();let fixed;
  if(rigid){geometry.computeBoundingBox();fixed=closestInfluences(geometry.boundingBox.getCenter(new T.Vector3()).applyMatrix4(toRoot).toArray(),segments,epsilon,true);}
  for(let i=0;i<p.count;i++){point.fromBufferAttribute(p,i).applyMatrix4(toRoot);const influences=fixed||closestInfluences(point.toArray(),segments,epsilon);for(let j=0;j<influences.length;j++){indices[i*4+j]=influences[j].index;weights[i*4+j]=influences[j].weight;}if(i%4096===0){progress((completed+i)/total);await yieldWork();}}
  geometry.setAttribute('skinIndex',new T.Uint16BufferAttribute(indices,4));geometry.setAttribute('skinWeight',new T.Float32BufferAttribute(weights,4));const skinned=new T.SkinnedMesh(geometry,mesh.material);skinned.uuid=mesh.uuid;skinned.name=mesh.name;skinned.userData={...mesh.userData};skinned.position.copy(mesh.position);skinned.quaternion.copy(mesh.quaternion);skinned.scale.copy(mesh.scale);skinned.visible=mesh.visible;skinned.castShadow=mesh.castShadow;skinned.receiveShadow=mesh.receiveShadow;skinned.frustumCulled=false;const parent=mesh.parent;mesh.removeFromParent();parent.add(skinned);root.updateWorldMatrix(true,true);skinned.bind(skeleton);completed+=p.count;
 }
 root.userData.rig={...root.userData.rig,bound:true,binding:rigid?'rigid':'smooth'};progress(1);return skeleton;
}
export function unbindSkeleton(root){resetBones(root);const skeletons=new Set();for(const mesh of meshesOf(root).filter(m=>m.isSkinnedMesh)){skeletons.add(mesh.skeleton);const geometry=mesh.geometry.clone();geometry.deleteAttribute('skinIndex');geometry.deleteAttribute('skinWeight');const replacement=new T.Mesh(geometry,mesh.material);replacement.uuid=mesh.uuid;replacement.name=mesh.name;replacement.userData={...mesh.userData};replacement.position.copy(mesh.position);replacement.quaternion.copy(mesh.quaternion);replacement.scale.copy(mesh.scale);replacement.castShadow=mesh.castShadow;replacement.receiveShadow=mesh.receiveShadow;mesh.parent.add(replacement);mesh.removeFromParent();}for(const s of skeletons)s.dispose();if(root.userData.rig)root.userData.rig.bound=false;}
export function validateRig(root){
 const bones=bonesOf(root),meshes=meshesOf(root).filter(m=>m.isSkinnedMesh);let vertices=0,invalid=0,maxInfluences=0;const names=new Set(),duplicates=[];
 for(const b of bones){if(names.has(b.name))duplicates.push(b.name);names.add(b.name);}
 for(const m of meshes){const w=m.geometry.attributes.skinWeight,i=m.geometry.attributes.skinIndex;if(!w||!i){invalid++;continue;}for(let v=0;v<w.count;v++){let sum=0,n=0;for(let k=0;k<4;k++){const weight=w.getComponent(v,k),index=i.getComponent(v,k);if(weight>0)n++;if(!Number.isFinite(weight)||weight<0||index<0||index>=m.skeleton.bones.length)invalid++;sum+=weight;}if(Math.abs(sum-1)>.0001)invalid++;maxInfluences=Math.max(maxInfluences,n);vertices++;}}
 return {bones:bones.length,meshes:meshes.length,vertices,invalid,maxInfluences,duplicates,valid:meshes.length>0&&invalid===0&&duplicates.length===0};
}
export function paintVertexWeight(indices,weights,vertex,boneIndex,target){
 const map=new Map();for(let k=0;k<4;k++){const index=indices.getComponent(vertex,k),weight=weights.getComponent(vertex,k);if(index!==boneIndex&&weight>0)map.set(index,(map.get(index)||0)+weight);}
 const others=[...map.entries()].sort((a,b)=>b[1]-a[1]).slice(0,target>0?3:4);let sum=others.reduce((n,x)=>n+x[1],0);if(!sum&&target<1){others.push([boneIndex===0?1:0,1]);sum=1;}
 const list=target>0?[[boneIndex,target]]:[];for(const [id,w] of others)list.push([id,w/sum*(1-target)]);for(let k=0;k<4;k++){indices.setComponent(vertex,k,list[k]?.[0]||0);weights.setComponent(vertex,k,list[k]?.[1]||0);}
}
export function transformClip(content,settings,name='Action'){
 const tracks=[],duration=settings.duration,fps=settings.fps||30;
 content.traverse(o=>{const base=settings.bases[o.uuid];if(!base)return;const keys=settings.tracks[o.uuid]||[],preset=o.parent===content?settings.preset:'none';if(!keys.length&&preset==='none')return;const times=[],positions=[],quaternions=[],scales=[];const count=Math.ceil(duration*fps);for(let f=0;f<=count;f++){const time=Math.min(duration,f/fps),v=evaluateTransform(base,keys,time,preset);times.push(time);positions.push(...v.position);scales.push(...v.scale);quaternions.push(...new T.Quaternion().setFromEuler(new T.Euler(...v.rotation)).toArray());}tracks.push(new T.VectorKeyframeTrack(o.uuid+'.position',times,positions),new T.QuaternionKeyframeTrack(o.uuid+'.quaternion',times,quaternions),new T.VectorKeyframeTrack(o.uuid+'.scale',times,scales));});
 return tracks.length?new T.AnimationClip(name,duration,tracks):null;
}
