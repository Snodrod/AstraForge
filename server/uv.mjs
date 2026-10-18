import {spawn} from 'node:child_process';
import {promises as fs} from 'node:fs';
import {randomUUID} from 'node:crypto';
import path from 'node:path';
import {z} from 'zod';
import {root,data,comfyDir} from './storage.mjs';
const schema=z.object({meshes:z.array(z.object({id:z.string().uuid(),positions:z.array(z.number().finite()).max(9000000),indices:z.array(z.number().int().nonnegative()).max(9000000)})).min(1).max(100)});
let busy=false;
export async function unwrapUV(body,signal){
 const input=schema.parse(body);let triangles=0;
 for(const m of input.meshes){if(m.positions.length%3||m.indices.length%3||!m.positions.length||!m.indices.length)throw new Error('Invalid geometry format.');const count=m.positions.length/3;if(m.indices.some(i=>i>=count))throw new Error('Vertex index out of range.');triangles+=m.indices.length/3;}
 if(triangles>2500000)throw new Error('For UV unwrapping, reduce the model to 2.5 million triangles.');if(busy)throw new Error('Wait for the previous UV unwrap to finish.');busy=true;
 const id=randomUUID(),source=path.join(data,'runtime','temp',id+'.input.json'),output=path.join(data,'runtime','temp',id+'.output.json');
 try{
  await fs.writeFile(source,JSON.stringify(input));
  const python=path.join(comfyDir(),'.venv','Scripts','python.exe');
  await new Promise((resolve,reject)=>{const p=spawn(python,[path.join(root,'scripts','unwrap_uv.py'),source,output],{windowsHide:true,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});let error='';p.stderr.on('data',b=>error=(error+b.toString()).slice(-1500));const abort=()=>p.kill();signal?.addEventListener('abort',abort,{once:true});const timer=setTimeout(()=>p.kill(),900000);const finish=()=>{clearTimeout(timer);signal?.removeEventListener('abort',abort);};p.on('error',e=>{finish();reject(e);});p.on('exit',code=>{finish();if(code!==0)reject(new Error('UV unwrap failed: '+error));else resolve();});});
  return JSON.parse(await fs.readFile(output,'utf8'));
 }finally{busy=false;await fs.unlink(source).catch(()=>{});await fs.unlink(output).catch(()=>{});}
}
