import * as T from 'three';
import {t} from './i18n/index.js';
export const referenceDefaults={view:'front',coverage:5/6,yaw:0,pitch:0,offsetX:0,offsetY:0};
const finite=(v,fallback,min,max)=>Number.isFinite(Number(v))?Math.max(min,Math.min(max,Number(v))):fallback;
export function referenceSettings(input={}){return {view:['front','back','left','right'].includes(input.view)?input.view:'front',coverage:finite(input.coverage,5/6,.3,1),yaw:finite(input.yaw,0,-180,180),pitch:finite(input.pitch,0,-85,85),offsetX:finite(input.offsetX,0,-.25,.25),offsetY:finite(input.offsetY,0,-.25,.25)};}
export function referenceCamera(root,input={},frozen=null){
 const s=referenceSettings(input),box=new T.Box3().setFromObject(root),dimensions=box.getSize(new T.Vector3());if(box.isEmpty())throw new Error(t('ref.error.emptyModel'));
 const frame=frozen||{center:box.getCenter(new T.Vector3()).toArray(),span:Math.max(dimensions.y,['front','back'].includes(s.view)?dimensions.x:dimensions.z)/s.coverage,coverage:s.coverage,distance:Math.max(dimensions.length()*2,.1)};
 if(!Number.isFinite(frame.span)||frame.span<=0||!Number.isFinite(frame.distance)||frame.distance<=0||frame.center?.length!==3||frame.center.some(v=>!Number.isFinite(v)))throw new Error(t('ref.error.badFrame'));
 const yaw=({front:0,back:180,left:90,right:-90}[s.view]+s.yaw)*Math.PI/180,pitch=s.pitch*Math.PI/180,direction=new T.Vector3(Math.sin(yaw)*Math.cos(pitch),Math.sin(pitch),Math.cos(yaw)*Math.cos(pitch));
 const span=frame.span*(frame.coverage||s.coverage)/s.coverage,target=new T.Vector3().fromArray(frame.center),camera=new T.OrthographicCamera(-span/2,span/2,span/2,-span/2,.001,frame.distance*4);
 camera.position.copy(target).addScaledVector(direction,frame.distance);camera.lookAt(target);camera.updateMatrixWorld(true);
 const right=new T.Vector3().setFromMatrixColumn(camera.matrixWorld,0),up=new T.Vector3().setFromMatrixColumn(camera.matrixWorld,1);target.addScaledVector(right,-s.offsetX*span).addScaledVector(up,s.offsetY*span);camera.position.copy(target).addScaledVector(direction,frame.distance);camera.lookAt(target);camera.updateMatrixWorld(true);return {camera,frame,direction,settings:s};
}
export function compareReferencePixels(reference,render){
 if(reference.length!==render.length||reference.length%4)throw new Error(t('ref.error.sizeMismatch'));let union=0,intersection=0,source=0,model=0,error=0;
 for(let i=0;i<reference.length;i+=4){const a=reference[i+3]>127,b=render[i+3]>127;if(a)source++;if(b)model++;if(a||b)union++;if(a&&b){intersection++;for(let c=0;c<3;c++)error+=Math.abs(reference[i+c]-render[i+c]);}}
 if(!source||!model)throw new Error(t('ref.error.emptyMasks'));return {silhouetteIoU:intersection/union,coverage:intersection/source,precision:intersection/model,colorMAE:error/Math.max(1,intersection*3*255),referencePixels:source,modelPixels:model,intersectionPixels:intersection,scope:'one calibrated view; not a 3D quality score'};
}
export function greenEmissionPixels(pixels){const output=new Uint8ClampedArray(pixels.length);for(let i=0;i<pixels.length;i+=4){const r=pixels[i],g=pixels[i+1],b=pixels[i+2],active=g>60&&g>r*1.15+8&&g>b*1.15+8;if(active){output[i]=r;output[i+1]=g;output[i+2]=b;}output[i+3]=255;}return output;}
export function suggestContourPins(reference,render,size=512){
 if(!Number.isInteger(size)||size<32||size>1024||reference.length!==size*size*4||render.length!==reference.length)throw new Error(t('ref.error.maskSize'));let low=size,high=-1;for(let y=0;y<size;y++)for(let x=0;x<size;x++)if(reference[(y*size+x)*4+3]>127){low=Math.min(low,y);high=Math.max(high,y);}if(high<=low)throw new Error(t('ref.error.emptyReference'));
 const row=(pixels,y)=>{const runs=[];let start=-1,first=-1,last=-1;for(let x=0;x<size;x++){const active=pixels[(y*size+x)*4+3]>127;if(active){if(first<0)first=x;last=x;if(start<0)start=x;}else if(start>=0){runs.push([start,x-1]);start=-1;}}if(start>=0)runs.push([start,size-1]);runs.sort((a,b)=>(b[1]-b[0])-(a[1]-a[0]));return first<0?null:{first,last,bodyRight:runs[0][1]};};
 const pins=[],add=(from,to,y)=>{if(Math.abs(to-from)/size>.1)return;const pin={from:[(from+.5)/size,(y+.5)/size],to:[(to+.5)/size,(y+.5)/size]};if(pins.every(p=>Math.hypot(p.from[0]-pin.from[0],p.from[1]-pin.from[1])>.005))pins.push(pin);};
 for(const fraction of [.18,.26,.35,.47,.61,.76,.9,.965]){const y=Math.round(low+(high-low)*fraction),a=row(reference,y),b=row(render,y);if(!a||!b)continue;add(b.first,a.first,y);add(b.last,a.last,y);if(fraction>=.61&&fraction<=.9)add(b.bodyRight,a.bodyRight,y);}
 const candidates=pins.length;while(pins.length>=3){try{fitReferenceWarp(pins);return {pins,candidates,removed:candidates-pins.length,scope:'suggested silhouette points; not anatomical landmarks'};}catch{let worst=0;for(let i=1;i<pins.length;i++)if(Math.hypot(pins[i].from[0]-pins[i].to[0],pins[i].from[1]-pins[i].to[1])>Math.hypot(pins[worst].from[0]-pins[worst].to[0],pins[worst].from[1]-pins[worst].to[1]))worst=i;pins.splice(worst,1);}}
 throw new Error(t('ref.error.noSafePairs'));
}
const kernel=r2=>r2<1e-12?0:r2*Math.log(r2);
function solve(matrix,rhs){const n=rhs.length,a=matrix.map((row,i)=>[...row,rhs[i]]);for(let column=0;column<n;column++){let pivot=column;for(let r=column+1;r<n;r++)if(Math.abs(a[r][column])>Math.abs(a[pivot][column]))pivot=r;if(Math.abs(a[pivot][column])<1e-12)throw new Error(t('ref.error.degenerate'));[a[column],a[pivot]]=[a[pivot],a[column]];const value=a[column][column];for(let j=column;j<=n;j++)a[column][j]/=value;for(let r=0;r<n;r++)if(r!==column){const k=a[r][column];for(let j=column;j<=n;j++)a[r][j]-=k*a[column][j];}}return a.map(row=>row[n]);}
export function fitReferenceWarp(pins){
 if(!Array.isArray(pins)||pins.length<3||pins.length>24)throw new Error(t('ref.error.pinCount'));
 const input=pins.map(p=>{for(const key of ['from','to'])if(p[key]?.length!==2||p[key].some(v=>!Number.isFinite(v)||v<0||v>1))throw new Error(t('ref.error.pinsInside'));if(Math.hypot(p.to[0]-p.from[0],p.to[1]-p.from[1])>.2)throw new Error(t('ref.error.offsetTooLarge'));return {from:[...p.from],to:[...p.to]};});
 for(let i=0;i<input.length;i++)for(let j=0;j<i;j++)if(Math.hypot(input[i].from[0]-input[j].from[0],input[i].from[1]-input[j].from[1])<.005)throw new Error(t('ref.error.pinsTooClose'));
 const points=[...input,...[[0,0],[1,0],[0,1],[1,1]].filter(c=>input.every(p=>Math.hypot(p.from[0]-c[0],p.from[1]-c[1])>.02)).map(c=>({from:c,to:c}))],n=points.length;
 const matrix=Array.from({length:n+3},()=>Array(n+3).fill(0));for(let i=0;i<n;i++){for(let j=0;j<n;j++)matrix[i][j]=kernel((points[i].from[0]-points[j].from[0])**2+(points[i].from[1]-points[j].from[1])**2);matrix[i][i]+=1e-8;matrix[i][n]=matrix[n][i]=1;matrix[i][n+1]=matrix[n+1][i]=points[i].from[0];matrix[i][n+2]=matrix[n+2][i]=points[i].from[1];}
 const coefficients=[0,1].map(axis=>solve(matrix,[...points.map(p=>p.to[axis]-p.from[axis]),0,0,0]));
 const warp=(x,y)=>{const nearest=Math.min(...input.map(p=>(x-p.from[0])**2+(y-p.from[1])**2)),influence=Math.exp(-nearest/(2*.12**2)),delta=coefficients.map(c=>{let v=c[n]+c[n+1]*x+c[n+2]*y;for(let i=0;i<n;i++)v+=c[i]*kernel((x-points[i].from[0])**2+(y-points[i].from[1])**2);return v*influence;});if(Math.hypot(...delta)>.25)throw new Error(t('ref.error.unsafeWarp'));return [x+delta[0],y+delta[1]];};
 let minimumJacobian=Infinity;const eps=1e-4;for(let y=0;y<=32;y++)for(let x=0;x<=32;x++){const p=warp(x/32,y/32),px=warp(x/32+eps,y/32),py=warp(x/32,y/32+eps),det=((px[0]-p[0])*(py[1]-p[1])-(px[1]-p[1])*(py[0]-p[0]))/(eps*eps);minimumJacobian=Math.min(minimumJacobian,det);}
 if(minimumJacobian<.05)throw new Error(t('ref.error.folds'));return {warp,minimumJacobian,pins:input};
}
export function deformReferenceGeometry(meshes,camera,pins){
 const fit=fitReferenceWarp(pins),updates=[];for(const mesh of meshes){if(mesh.isSkinnedMesh||Object.keys(mesh.geometry.morphAttributes).length)throw new Error(t('ref.error.beforeRig'));const geometry=mesh.geometry.clone(),positions=geometry.attributes.position,inverse=mesh.matrixWorld.clone().invert(),point=new T.Vector3();for(let i=0;i<positions.count;i++){point.fromBufferAttribute(positions,i).applyMatrix4(mesh.matrixWorld).project(camera);const p=fit.warp((point.x+1)/2,(1-point.y)/2);point.x=p[0]*2-1;point.y=1-p[1]*2;point.unproject(camera).applyMatrix4(inverse);positions.setXYZ(i,point.x,point.y,point.z);}positions.needsUpdate=true;geometry.deleteAttribute('tangent');geometry.computeVertexNormals();geometry.computeBoundingBox();geometry.computeBoundingSphere();updates.push({mesh,geometry});}
 return {updates,minimumJacobian:fit.minimumJacobian,pins:fit.pins};
}
