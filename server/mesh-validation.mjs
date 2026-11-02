export const maxMeshBytes=100*1024*1024,maxMeshTriangles=2500000;
const pngSignature=[137,80,78,71,13,10,26,10];
export function validateGeneratedMesh(buffer,{requireVertexColors=false,requireUvTexture=false}={}){
 if(buffer.length>maxMeshBytes)throw new Error('The mesh exceeds 100 MB. Check the surface level; such a result will not be imported.');
 if(buffer.length<28||buffer.toString('ascii',0,4)!=='glTF'||buffer.readUInt32LE(4)!==2||buffer.readUInt32LE(8)!==buffer.length)throw new Error('Invalid GLB from the generator.');
 const size=buffer.readUInt32LE(12);if(size>buffer.length-20||buffer.toString('ascii',16,20)!=='JSON')throw new Error('Invalid geometry header.');
 const doc=JSON.parse(buffer.toString('utf8',20,20+size));let triangles=0,vertices=0,vertexColors=true,uvTexture=true;
 const binHeader=20+size,binStart=binHeader+8;
 const hasBin=binHeader+8<=buffer.length&&buffer.toString('ascii',binHeader+4,binHeader+8)==='BIN\0';
 const binLength=hasBin?buffer.readUInt32LE(binHeader):0;
 for(const mesh of doc.meshes||[])for(const primitive of mesh.primitives||[]){
  if((primitive.mode??4)!==4)throw new Error('A triangle mesh was expected.');
  const position=doc.accessors?.[primitive.attributes?.POSITION],indices=primitive.indices===undefined?position:doc.accessors?.[primitive.indices];
  if(!Number.isSafeInteger(position?.count)||position.count<3||!Number.isSafeInteger(indices?.count)||indices.count<3||indices.count%3)throw new Error('Invalid vertex/triangle count.');
  const color=doc.accessors?.[primitive.attributes?.COLOR_0];
  if(!color||color.count!==position.count||!['VEC3','VEC4'].includes(color.type)||![5121,5123,5126].includes(color.componentType))vertexColors=false;
  const uv=doc.accessors?.[primitive.attributes?.TEXCOORD_0];
  const material=doc.materials?.[primitive.material];
  const texture=doc.textures?.[material?.pbrMetallicRoughness?.baseColorTexture?.index];
  const image=doc.images?.[texture?.source],view=doc.bufferViews?.[image?.bufferView];
  const offset=view?.byteOffset??0,length=view?.byteLength;
  const validPng=hasBin&&(view?.buffer??0)===0&&Number.isSafeInteger(offset)&&offset>=0&&Number.isSafeInteger(length)&&length>=8&&offset+length<=binLength&&binStart+offset+length<=buffer.length&&pngSignature.every((v,i)=>buffer[binStart+offset+i]===v);
  if(!uv||uv.count!==position.count||uv.type!=='VEC2'||![5121,5123,5126].includes(uv.componentType)||!validPng)uvTexture=false;
  vertices+=position.count;triangles+=indices.count/3;
 }
 if(!triangles)throw new Error('The generator returned an empty mesh.');
 if(triangles>maxMeshTriangles)throw new Error('The mesh exceeds 2.5 million triangles. The surface level may be set incorrectly.');
 if(requireVertexColors&&!vertexColors)throw new Error('Invalid or missing COLOR_0 vertex colors.');
 if(requireUvTexture&&!uvTexture)throw new Error('Invalid or missing TEXCOORD_0/baseColorTexture PNG.');
 return {bytes:buffer.length,triangles,vertices,accepted:true,vertexColors,uvTexture,scope:'technical bounds; anatomy is not validated'};
}
