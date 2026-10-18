import React,{useState,useEffect,useRef,useMemo} from 'react';import {Dices,Download,Save,Play,Pause,Loader2,Sparkles} from 'lucide-react';import {upload,download} from './api.js';
import {FX_TEXTURE_TYPES,FX_TEXTURE_PRESETS,generateFxTexture,resolveFxParams,fxDefaultParams,fxParamVisible,fxFlipbookGrid,fxFileName,encodeFxPng} from './fx-textures.js';import './fx-lab.css';
import {t,useT} from './i18n/index.js';
// Texture generator for effects: recipes, sliders, checkerboard preview, 3×3 tiling and flipbook playback. Computation runs in a Web Worker.
const COMMON_KEEP=['size','seed','contrast','gamma','invert','colorMode','colorStops','color1','color2','color3'];
const CANVAS_PX=512,BG_KEYS=['checker','black','gray'];
const cancelled=()=>Object.assign(new Error('cancelled'),{cancelled:true});
// Worker errors: the worker runs with the default language, so a keyed error is re-translated here in the page's language
const workerError=m=>new Error(m.errorKey?t(m.errorKey,m.errorVars):m.error);
// Running a generation: one worker per preview; a new job evicts an unfinished one (the worker is killed, so a heavy frame is not finished).
// No Worker, or it failed to load: compute on the main thread after a pause so the UI gets a chance to repaint.
function makeRunner(){
 let w=null,job=null,seq=0,broken=typeof Worker==='undefined';
 const drop=()=>{if(w){w.terminate();w=null;}if(job){const j=job;job=null;j.reject(cancelled());}};
 const inline=(type,params)=>new Promise((resolve,reject)=>setTimeout(()=>{try{resolve(generateFxTexture(type,params));}catch(e){reject(e);}},0));
 return{
  run(type,params){
   if(job)drop();
   if(broken)return inline(type,params);
   if(!w){try{
    w=new Worker(new URL('./fx-textures.worker.js',import.meta.url),{type:'module'});
    w.onmessage=e=>{const m=e.data;if(!job||job.id!==m.id)return;const j=job;job=null;if(m.ok)j.resolve(m);else j.reject(workerError(m));};
    w.onerror=()=>{broken=true;const j=job;job=null;if(w){w.terminate();w=null;}if(j)inline(j.type,j.params).then(j.resolve,j.reject);};
   }catch{broken=true;return inline(type,params);}}
   return new Promise((resolve,reject)=>{const id=++seq;job={id,type,params,resolve,reject};w.postMessage({id,type,params});});
  },
  close(){drop();}
 };
}
const sheetCanvas=r=>{const c=document.createElement('canvas');c.width=r.width;c.height=r.height;c.getContext('2d').putImageData(new ImageData(r.data,r.width,r.height),0,0);return c;};
// Recipe thumbnails are computed once per session; for a flipbook we take a frame from the middle
const THUMBS=new Map();
function makeThumb(p){
 const grid=fxFlipbookGrid(p.params),tex=generateFxTexture(p.type,{...p.params,size:grid?256:128}),src=sheetCanvas(tex),out=document.createElement('canvas');out.width=out.height=96;
 let sx=0,sy=0,sw=tex.width,sh=tex.height;
 if(tex.flipbook){const k=Math.floor(tex.flipbook.h*tex.flipbook.v*0.4);sw=tex.width/tex.flipbook.h;sh=tex.height/tex.flipbook.v;sx=(k%tex.flipbook.h)*sw;sy=Math.floor(k/tex.flipbook.h)*sh;}
 out.getContext('2d').drawImage(src,sx,sy,sw,sh,0,0,96,96);return out.toDataURL('image/png');
}
const decimals=step=>step>=1?0:Math.min(3,Math.max(1,Math.ceil(-Math.log10(step)-1e-9)));
const randomSeed=()=>Math.floor(Math.random()*100000);
function Seg({label,options,value,onChange}){useT();return <div className="segmented fxl-seg" role="group" aria-label={label}>{options.map(([v,t])=><button key={String(v)} type="button" className={String(v)===String(value)?'active':''} aria-pressed={String(v)===String(value)} onClick={()=>onChange(v)}>{t}</button>)}</div>;}
function Field({q,value,onChange}){
 const t=useT();
 if(q.options){
  const short=q.options.length<=5&&q.options.every(o=>o[1].length<=(q.options.length<=3?10:5));
  return <div className="fxl-field"><span className="fxl-fl">{q.label}</span>{short?<Seg label={q.label} options={q.options} value={value} onChange={onChange}/>:<select aria-label={q.label} value={String(value)} onChange={e=>onChange(q.options.find(o=>String(o[0])===e.target.value)[0])}>{q.options.map(([v,label])=><option key={String(v)} value={String(v)}>{label}</option>)}</select>}</div>;
 }
 if(q.key==='seed')return <div className="fxl-field"><span className="fxl-fl">{q.label}</span><div className="fxl-seed"><input type="number" aria-label={q.label} min="0" step="1" value={value} onChange={e=>onChange(Math.max(0,Math.floor(Number(e.target.value)||0)))}/><button type="button" className="icon-btn" title={t('fxtex.lab.randomSeed')} aria-label={t('fxtex.lab.randomSeed')} onClick={()=>onChange(randomSeed())}><Dices size={15}/></button></div></div>;
 const d=decimals(q.step);
 return <label className="fxl-field"><span className="fxl-fl"><span>{q.label}</span><output>{Number(value).toFixed(d)}</output></span><input type="range" aria-label={q.label} min={q.min} max={q.max} step={q.step} value={value} onChange={e=>onChange(Number(e.target.value))}/></label>;
}
// Preview: views (sheet / tile / playback), background, frames. Frames tick here so the whole panel is not re-rendered.
function Stage({result,pending,meta,onStick}){
 const t=useT();
 const [view,setView]=useState('play'),[bg,setBg]=useState(0),[fps,setFps]=useState(12),[playing,setPlaying]=useState(true),[frame,setFrame]=useState(0);
 const canvas=useRef(null),root=useRef(null);
 const views=[];if(result?.flipbook)views.push(['play',t('fxtex.lab.view.play')],['sheet',t('fxtex.lab.view.sheet')]);else views.push(['sheet',t('fxtex.lab.view.texture')]);if(result?.tileable)views.push(['tile',t('fxtex.lab.view.tile')]);
 const curView=views.some(v=>v[0]===view)?view:views[0][0],frames=result?.flipbook?result.flipbook.h*result.flipbook.v:1;
 useEffect(()=>{setFrame(0);},[result?.flipbook?.h,result?.flipbook?.v]);
 useEffect(()=>{if(curView!=='play'||!playing||frames<2)return;const id=setInterval(()=>setFrame(f=>(f+1)%frames),1000/fps);return()=>clearInterval(id);},[curView,playing,fps,frames]);
 // Neighbours need the height of the pinned preview: a field scrolled into view must not end up under it (scroll-margin)
 useEffect(()=>{const el=root.current;if(!el||typeof ResizeObserver==='undefined')return;const ro=new ResizeObserver(()=>onStick(el.offsetHeight));ro.observe(el);return()=>ro.disconnect();},[onStick]);
 // Draw on a transparent canvas: the checkerboard and background are CSS (no background repaint on every frame)
 useEffect(()=>{
  const cv=canvas.current,s=result?.canvas;if(!cv||!s)return;
  const c=cv.getContext('2d'),S=CANVAS_PX;c.clearRect(0,0,S,S);c.imageSmoothingEnabled=true;c.imageSmoothingQuality='high';
  if(curView==='tile'){const p=i=>Math.round(i*S/3);for(let y=0;y<3;y++)for(let x=0;x<3;x++)c.drawImage(s,p(x),p(y),p(x+1)-p(x),p(y+1)-p(y));}   // integer tile borders: fractional ones show seams
  else if(curView==='play'&&result.flipbook){const {h,v}=result.flipbook,fw=s.width/h,fh=s.height/v,k=frame%(h*v);c.drawImage(s,(k%h)*fw,Math.floor(k/h)*fh,fw,fh,0,0,S,S);}
  else{c.drawImage(s,0,0,S,S);if(result.flipbook){const {h,v}=result.flipbook;c.strokeStyle='rgba(213,223,155,.35)';c.lineWidth=1;c.beginPath();for(let i=1;i<h;i++){c.moveTo(i*S/h,0);c.lineTo(i*S/h,S);}for(let j=1;j<v;j++){c.moveTo(0,j*S/v);c.lineTo(S,j*S/v);}c.stroke();}}
 },[result,curView,frame]);
 const playLabel=playing?t('fxtex.lab.pause'):t('fxtex.lab.play');
 return <section className="section fxl-stage-sec" ref={root}>
  <div className="section-heading"><h3>{t('fxtex.lab.preview')}</h3>{pending?<span className="fxl-busy"><Loader2 size={12}/>{t('fxtex.lab.computing')}</span>:<span className="optional">{meta}</span>}</div>
  {views.length>1&&<Seg label={t('fxtex.lab.previewView')} options={views} value={curView} onChange={setView}/>}
  <div className="fxl-stage" data-bg={bg}><canvas ref={canvas} width={CANVAS_PX} height={CANVAS_PX} aria-label={t('fxtex.lab.previewAria')} role="img"/></div>
  <div className="fxl-bar">
   {curView==='play'&&<><button type="button" className="icon-btn" title={playLabel} aria-label={playLabel} onClick={()=>setPlaying(p=>!p)}>{playing?<Pause size={14}/>:<Play size={14}/>}</button>
    <label className="fxl-fps"><span>{t('fxtex.lab.fps',{n:fps})}</span><input type="range" aria-label={t('fxtex.lab.fpsAria')} min="2" max="30" step="1" value={fps} onChange={e=>setFps(Number(e.target.value))}/></label><span className="optional">{frame+1}/{frames}</span></>}
   <button type="button" className="subtle-btn fxl-bg" title={t('fxtex.lab.bgTitle')} onClick={()=>setBg(b=>(b+1)%BG_KEYS.length)}>{t('fxtex.lab.bgLabel',{name:t('fxtex.lab.bg.'+BG_KEYS[bg])})}</button>
  </div>
 </section>;
}
export default function FxTextureLab({notify,onUse,compact}){
 const t=useT();
 const say=(text,err)=>{if(typeof notify==='function')notify(text,err);};
 const [type,setType]=useState('smoke'),[params,setParams]=useState(()=>resolveFxParams('smoke',FX_TEXTURE_PRESETS.find(p=>p.id==='smoke-puff').params)),[presetId,setPresetId]=useState('smoke-puff');
 const [result,setResult]=useState(null),[pending,setPending]=useState(true),[busy,setBusy]=useState(''),[thumbs,setThumbs]=useState(()=>Object.fromEntries(THUMBS)),[stick,setStick]=useState(0);
 const preview=useRef(null),notifyRef=useRef(say);notifyRef.current=say;
 const typeDef=useMemo(()=>FX_TEXTURE_TYPES.find(x=>x.id===type),[type]);
 const grid=fxFlipbookGrid(params);
 useEffect(()=>{preview.current=makeRunner();return()=>preview.current.close();},[]);
 // Recipe thumbnails: one per tick so the panel opens without a stall
 useEffect(()=>{let stop=false,i=0;const step=()=>{if(stop||i>=FX_TEXTURE_PRESETS.length)return;const p=FX_TEXTURE_PRESETS[i++];if(!THUMBS.has(p.id)){try{THUMBS.set(p.id,makeThumb(p));}catch{THUMBS.set(p.id,'');}setThumbs(Object.fromEntries(THUMBS));}setTimeout(step,0);};setTimeout(step,30);return()=>{stop=true;};},[]);
 // Preview: fast size (256, 512 for a flipbook); slider bounce is damped by a pause
 useEffect(()=>{
  setPending(true);
  const g=fxFlipbookGrid(params),size=Math.min(params.size,g?512:256),timer=setTimeout(()=>{
   preview.current.run(type,{...params,size}).then(r=>{setResult({width:r.width,height:r.height,flipbook:r.flipbook,tileable:r.tileable,canvas:sheetCanvas(r)});setPending(false);}).catch(e=>{if(e.cancelled)return;setPending(false);notifyRef.current(e.message||t('fxtex.lab.buildFailed'),true);});
  },90);
  return()=>clearTimeout(timer);
 },[type,params]);
 const setParam=(key,value)=>{setParams(p=>({...p,[key]:value}));setPresetId(null);};
 const pickPreset=p=>{setType(p.type);setParams(resolveFxParams(p.type,p.params));setPresetId(p.id);};
 const pickType=id=>{const next=fxDefaultParams(id);for(const k of COMMON_KEEP)next[k]=params[k];setType(id);setParams(resolveFxParams(id,next));setPresetId(null);};
 // Final texture: full size, also in the worker; lossless PNG (RGBA as is)
 const makeFile=async()=>{
  const runner=makeRunner();
  try{const r=await runner.run(type,params),png=await encodeFxPng(r),name=fxFileName(type,params);return{file:new File([png],name,{type:'image/png'}),name,flipbook:r.flipbook};}
  finally{runner.close();}
 };
 const act=(what)=>async()=>{
  if(busy)return;setBusy(what);
  try{
   const {file,name,flipbook}=await makeFile();
   if(what==='download'){await download(file,name);say(t('fxtex.lab.downloaded',{name}));}
   else{const asset=await upload(file);if(what==='use'){onUse&&await onUse(asset,{type,params:{...params},flipbook});say(t('fxtex.lab.sentToFx',{name}));}else say(t('fxtex.lab.savedToAssets',{name}));}
  }catch(e){say(e.message||t('fxtex.lab.saveFailed'),true);}
  finally{setBusy('');}
 };
 const own=typeDef.params.filter(q=>q.group!=='common'&&fxParamVisible(q,params)),common=typeDef.params.filter(q=>q.group==='common'&&fxParamVisible(q,params));
 const field=q=>q.type==='color'?null:<Field key={q.key} q={q} value={params[q.key]} onChange={v=>setParam(q.key,v)}/>;
 const colors=common.filter(q=>q.type==='color'),groups=[...new Set(FX_TEXTURE_TYPES.map(x=>x.groupId))];
 const meta=result?[`${result.width}×${result.height}`,result.flipbook?t('fxtex.lab.meta.flipbook',{h:result.flipbook.h,v:result.flipbook.v,frame:result.width/result.flipbook.h|0}):null,result.tileable?t('fxtex.lab.meta.seamless'):null,result.width!==params.size?t('fxtex.lab.meta.final',{size:params.size}):null].filter(Boolean).join(' · '):'';
 return <div className={'fxl'+(compact?' compact':'')} style={stick?{'--fxl-stick':stick+'px'}:undefined}>
  <Stage result={result} pending={pending} meta={meta} onStick={setStick}/>
  <section className="section">
   <div className="section-heading"><h3>{t('fxtex.lab.recipes')}</h3><span className="optional">{FX_TEXTURE_PRESETS.length}</span></div>
   <div className="fxl-presets">{FX_TEXTURE_PRESETS.map(p=><button key={p.id} type="button" className={'fxl-preset'+(p.id===presetId?' active':'')} aria-pressed={p.id===presetId} title={p.title} onClick={()=>pickPreset(p)}>
    <span className="fxl-thumb">{thumbs[p.id]?<img src={thumbs[p.id]} alt="" loading="lazy"/>:<Sparkles size={14}/>}</span><span className="fxl-pt">{p.title}</span></button>)}</div>
  </section>
  <section className="section">
   <div className="section-heading"><h3>{t('fxtex.lab.typeParams')}</h3></div>
   <label className="fxl-field"><span className="fxl-fl">{t('fxtex.lab.textureType')}</span><select aria-label={t('fxtex.lab.textureType')} value={type} onChange={e=>pickType(e.target.value)}>{groups.map(g=>{const list=FX_TEXTURE_TYPES.filter(x=>x.groupId===g);return <optgroup key={g} label={list[0].group}>{list.map(x=><option key={x.id} value={x.id}>{x.title}</option>)}</optgroup>;})}</select></label>
   {own.map(field)}
  </section>
  <section className="section">
   <div className="section-heading"><h3>{t('fxtex.lab.sizeNoiseColor')}</h3></div>
   {common.filter(q=>q.type!=='color').map(field)}
   {!!colors.length&&<div className="fxl-colors">{colors.map(q=><label key={q.key} className="fxl-color" title={q.label}><input type="color" aria-label={q.label} value={params[q.key]} onChange={e=>setParam(q.key,e.target.value)}/><span>{t('fxtex.lab.colorShort.'+q.key)}</span></label>)}</div>}
   {params.colorMode==='white'&&<p className="field-note">{t('fxtex.lab.whiteNote')}</p>}
  </section>
  <section className="section fxl-actions">
   {onUse&&<button type="button" className="primary full" disabled={!!busy||pending&&!result} onClick={act('use')}>{busy==='use'?<Loader2 size={14} className="fxl-spin"/>:null}{t('fxtex.lab.useInFx')}</button>}
   <div className="fxl-row"><button type="button" className="secondary small" disabled={!!busy} onClick={act('save')}>{busy==='save'?<Loader2 size={12} className="fxl-spin"/>:<Save size={12}/>}{t('fxtex.lab.saveToAssets')}</button>
    <button type="button" className="secondary small" disabled={!!busy} onClick={act('download')}>{busy==='download'?<Loader2 size={12} className="fxl-spin"/>:<Download size={12}/>}{t('fxtex.lab.downloadPng')}</button></div>
   <p className="field-note">{t('fxtex.lab.finalNote',{size:params.size,flipbook:grid?t('fxtex.lab.finalFlipbook',{h:grid.h,v:grid.v}):''})}</p>
  </section>
 </div>;
}
