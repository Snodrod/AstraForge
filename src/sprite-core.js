import {t} from './i18n/index.js';
export function sampleTimes(duration,fps,loop=true){if(!(duration>0)||!Number.isFinite(duration)||!Number.isInteger(fps)||fps<1||fps>60)throw new Error(t('sprite.error.frames'));const count=Math.max(1,Math.ceil(duration*fps));return Array.from({length:count+(loop?0:1)},(_,i)=>Math.min(duration,i/fps));}
export function atlasLayout(frameCount,size,columns=8,maxSide=4096){if(!Number.isInteger(frameCount)||frameCount<1||![64,128,256,512,1024].includes(size))throw new Error(t('sprite.error.size'));const cols=Math.min(columns,Math.floor(maxSide/size)),rows=Math.floor(maxSide/size),capacity=cols*rows;return {columns:cols,capacity,pages:Math.ceil(frameCount/capacity),rect:i=>({page:Math.floor(i/capacity),x:(i%cols)*size,y:Math.floor((i%capacity)/cols)*size,width:size,height:size})};}
export function spriteFramesResource(metadata,folder='AstraForgeSprites'){
 if(!/^[A-Za-z0-9_-]+$/.test(folder))throw new Error(t('sprite.error.folder'));
 const esc=v=>JSON.stringify(v);const lines=[`[gd_resource type="SpriteFrames" load_steps=${metadata.frames.length+metadata.pages.length+1} format=3]`,''];
 metadata.pages.forEach((p,i)=>lines.push(`[ext_resource type="Texture2D" path="res://${folder}/${p.file}" id="p${i}"]`));lines.push('');
 metadata.frames.forEach((f,i)=>lines.push(`[sub_resource type="AtlasTexture" id="f${i}"]`,`atlas = ExtResource("p${f.page}")`,`region = Rect2(${f.x}, ${f.y}, ${f.width}, ${f.height})`,''));
 lines.push('[resource]','animations = [');
 metadata.animations.forEach((a,index)=>{const frames=a.frames.map(i=>`{"duration": 1.0, "texture": SubResource("f${i}")}`).join(', ');lines.push(`{"frames": [${frames}], "loop": ${a.loop}, "name": &${esc(a.name)}, "speed": ${a.fps}.0}${index<metadata.animations.length-1?',':''}`);});lines.push(']');return lines.join('\n');
}
