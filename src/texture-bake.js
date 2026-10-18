import * as T from 'three';
import {meshesOf,resetBones} from './rig-core.js';
import {sceneState,makeCanvas} from './media-core.js';
import {referenceCamera} from './reference-core.js';
import {t} from './i18n/index.js';
// Prepared concepts use a white encoder background. Extend the nearest object
// colour past that silhouette so small reconstruction errors don't paint a
// white outline onto the model. Transparent references keep their alpha.
function extendWhiteBackground(source){
 const w=source.width,h=source.height,canvas=makeCanvas(w);canvas.height=h;const ctx=canvas.getContext('2d');ctx.drawImage(source,0,0,w,h);const pixels=ctx.getImageData(0,0,w,h),data=pixels.data,n=w*h;
 const white=i=>{const r=data[i*4],g=data[i*4+1],b=data[i*4+2];return data[i*4+3]>250&&Math.min(r,g,b)>160&&Math.max(r,g,b)-Math.min(r,g,b)<18;};
 if(![0,w-1,(h-1)*w,n-1].every(white))return source;
 const mask=new Uint8Array(n),queue=new Int32Array(n);let begin=0,end=0;
 const enqueue=i=>{if(!mask[i]&&white(i)){mask[i]=1;queue[end++]=i;}};
 for(let x=0;x<w;x++){enqueue(x);enqueue((h-1)*w+x);}for(let y=0;y<h;y++){enqueue(y*w);enqueue(y*w+w-1);}
 while(begin<end){const i=queue[begin++],x=i%w;if(x)enqueue(i-1);if(x<w-1)enqueue(i+1);if(i>=w)enqueue(i-w);if(i<n-w)enqueue(i+w);}
 if(end===n)return source;
 // Rembg's soft matte contains a pale fringe just inside the white boundary.
 // Exclude that fringe from the nearest-colour seeds as well.
 for(let step=0;step<4;step++){const old=mask.slice();for(let y=1;y<h-1;y++)for(let x=1;x<w-1;x++){const i=y*w+x;if(old[i]||old[i-1]||old[i+1]||old[i-w]||old[i+w])mask[i]=1;}}
 const nearest=new Int32Array(n),distance=new Float32Array(n);
 for(let i=0;i<n;i++){nearest[i]=mask[i]?-1:i;distance[i]=mask[i]?1e9:0;}
 const relax=(i,j,weight)=>{const d=distance[j]+weight;if(d<distance[i]){distance[i]=d;nearest[i]=nearest[j];}};
 for(let y=0;y<h;y++)for(let x=0;x<w;x++){const i=y*w+x;if(x)relax(i,i-1,1);if(y){relax(i,i-w,1);if(x)relax(i,i-w-1,1.4142);if(x<w-1)relax(i,i-w+1,1.4142);}}
 for(let y=h-1;y>=0;y--)for(let x=w-1;x>=0;x--){const i=y*w+x;if(x<w-1)relax(i,i+1,1);if(y<h-1){relax(i,i+w,1);if(x)relax(i,i+w-1,1.4142);if(x<w-1)relax(i,i+w+1,1.4142);}}
 for(let i=0;i<n;i++)if(mask[i]&&nearest[i]>=0){const j=nearest[i]*4;data[i*4]=data[j];data[i*4+1]=data[j+1];data[i*4+2]=data[j+2];}
 ctx.putImageData(pixels,0,0);return canvas;
}
export async function projectReference(studio,root,source,size=2048,view='front',coverage=.83,calibration={}){
 source=extendWhiteBackground(source);
 const state=sceneState(studio.content),hidden=[],background=studio.scene.background,fog=studio.scene.fog,override=studio.scene.overrideMaterial,renderer=new T.WebGLRenderer({alpha:true,antialias:false});renderer.setPixelRatio(1);renderer.setSize(size,size);renderer.setClearColor(0,0);const resources=[];
 try{resetBones(root);for(const m of meshesOf(root))if(m.isSkinnedMesh){m.boundingBox=null;m.boundingSphere=null;}for(const o of studio.content.children)if(o!==root){hidden.push([o,o.visible]);o.visible=false;}for(const o of studio.scene.children)if(o!==studio.content&&(o.isMesh||o.isLine||o.isGroup||o.isPoints)){hidden.push([o,o.visible]);o.visible=false;}studio.scene.background=null;studio.scene.fog=null;
  const {camera,direction:dir}=referenceCamera(root,{view,coverage,...calibration},calibration.frame||null);
  hidden.push([studio.gizmo.getHelper(),studio.gizmo.getHelper().visible]);studio.gizmo.getHelper().visible=false;const depth=new T.WebGLRenderTarget(size,size);depth.depthTexture=new T.DepthTexture(size,size,T.UnsignedIntType);resources.push(depth);renderer.setRenderTarget(depth);renderer.render(studio.scene,camera);const matrix=camera.projectionMatrix.clone().multiply(camera.matrixWorldInverse);
  const ref=new T.Texture(source);ref.colorSpace=T.NoColorSpace;ref.needsUpdate=true;resources.push(ref);const target=new T.WebGLRenderTarget(size,size);resources.push(target);const uvScene=new T.Scene(),current=meshesOf(root).find(m=>m.material?.map)?.material.map;const previous=current?.clone()||new T.Texture(makeCanvas(1));if(!current){previous.image.getContext('2d').fillStyle='#b8c1bb';previous.image.getContext('2d').fillRect(0,0,1,1);}previous.colorSpace=T.NoColorSpace;previous.needsUpdate=true;resources.push(previous);
  const vertexShader='uniform mat3 worldNormals; varying vec3 wPos; varying vec3 wNormal; varying vec2 texUV; void main(){wPos=(modelMatrix*vec4(position,1.)).xyz; wNormal=normalize(worldNormals*normal); texUV=uv; gl_Position=vec4(uv*2.-1.,0.,1.);}';
  const fragmentShader='uniform sampler2D refImage; uniform sampler2D depthImage; uniform sampler2D previousMap; uniform mat4 referenceMatrix; uniform vec3 facing; varying vec3 wPos; varying vec3 wNormal; varying vec2 texUV; void main(){vec4 p=referenceMatrix*vec4(wPos,1.); vec3 q=p.xyz/p.w*.5+.5; vec3 c=texture2D(previousMap,texUV).rgb; float d=texture2D(depthImage,q.xy).r; if(q.x>=0.&&q.x<=1.&&q.y>=0.&&q.y<=1.&&dot(normalize(wNormal),facing)>.15&&q.z<=d+.002){vec4 ref=texture2D(refImage,q.xy);if(ref.a>.05)c=mix(c,ref.rgb,ref.a);} gl_FragColor=vec4(c,1.);}';
  for(const mesh of meshesOf(root)){if(!mesh.geometry.attributes.uv)throw new Error(t('texture.error.uvFirst'));const material=new T.ShaderMaterial({vertexShader,fragmentShader,uniforms:{worldNormals:{value:new T.Matrix3().getNormalMatrix(mesh.matrixWorld)},refImage:{value:ref},depthImage:{value:depth.depthTexture},previousMap:{value:previous},referenceMatrix:{value:matrix},facing:{value:dir}},side:T.DoubleSide,depthTest:false,depthWrite:false,toneMapped:false});resources.push(material);const surface=new T.Mesh(mesh.geometry,material);surface.matrixAutoUpdate=false;surface.matrix.copy(mesh.matrixWorld);uvScene.add(surface);}
  renderer.setRenderTarget(target);renderer.clear();renderer.render(uvScene,camera);const pixels=new Uint8Array(size*size*4);renderer.readRenderTargetPixels(target,0,0,size,size,pixels);const canvas=makeCanvas(size),ctx=canvas.getContext('2d'),out=ctx.createImageData(size,size);for(let y=0;y<size;y++)out.data.set(pixels.subarray(y*size*4,(y+1)*size*4),(size-1-y)*size*4);
  // Expand chart edges before mipmapping. Remaining padding keeps the previous
  // material instead of a pale neutral colour that would bleed into dark robes.
  const fallback=makeCanvas(size),fallbackCtx=fallback.getContext('2d');fallbackCtx.drawImage(previous.image,0,0,size,size);const fallbackPixels=fallbackCtx.getImageData(0,0,size,size).data;
  for(let step=0;step<4;step++){if(studio.mediaAbort)throw new Error(t('texture.error.bakeCancelled'));const old=new Uint8ClampedArray(out.data);for(let y=1;y<size-1;y++)for(let x=1;x<size-1;x++){const i=(y*size+x)*4;if(old[i+3])continue;for(const off of [-size*4,size*4,-4,4])if(old[i+off+3]){out.data.set(old.subarray(i+off,i+off+4),i);break;}}await new Promise(r=>setTimeout(r,0));}for(let i=0;i<out.data.length;i+=4)if(!out.data[i+3]){out.data[i]=fallbackPixels[i];out.data[i+1]=fallbackPixels[i+1];out.data[i+2]=fallbackPixels[i+2];out.data[i+3]=255;}ctx.putImageData(out,0,0);return canvas;
 }finally{for(const [o,visible] of hidden)o.visible=visible;studio.scene.background=background;studio.scene.fog=fog;studio.scene.overrideMaterial=override;state.restore();resources.forEach(r=>r.dispose());renderer.dispose();renderer.forceContextLoss();}
}
