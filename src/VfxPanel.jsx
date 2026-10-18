import React,{useState,useEffect,useRef,useCallback} from 'react';import {Play,Download,FolderOpen,ExternalLink,RefreshCw,Upload,X,Globe} from 'lucide-react';import {api,upload} from './api.js';import {useT,tOr} from './i18n/index.js';import './vfx.css';
// VFX tab: catalogue of texture generators for effects and the folder of ready-made textures.
const FOLDED=['store','paid','guide'];
const SLOTS=[['map','vfx.slot.map'],['emissiveMap','vfx.slot.emissiveMap'],['normalMap','vfx.slot.normalMap'],['roughnessMap','vfx.slot.roughnessMap'],['metalnessMap','vfx.slot.metalnessMap']];
// Catalogue texts come from the server (Russian today); a client dictionary entry wins when there is one.
const kindTitle=(kind,title)=>tOr('vfx.kind.'+kind,title);
const toolDesc=tool=>tOr('vfx.toolDesc.'+tool.id,tool.en||tool.ru);
function Section({title,children,aside}){return <section className="section"><div className="section-heading"><h3>{title}</h3>{aside}</div>{children}</section>;}
const Missing=()=>{const t=useT();return <span className="optional">{t('vfx.tool.missing')}</span>;};
function ToolCard({t:tool,busy,onLaunch,onInstall,onWeb,onFolder}){const t=useT();
 const program=tool.kind==='desktop'||tool.kind==='installer';
 const open=(label,icon,fn,disabled,title)=><button className="secondary small" disabled={busy||disabled} title={title} onClick={fn}>{icon}{label}</button>;
 let action=null;
 if(tool.kind==='desktop')action=tool.available?open(t('vfx.tool.launch'),<Play size={12}/>,onLaunch):<Missing/>;
 else if(tool.kind==='installer')action=tool.installed?open(t('vfx.tool.launch'),<Play size={12}/>,onLaunch):tool.installerAvailable?open(t('vfx.tool.install'),<Download size={12}/>,onInstall):<Missing/>;
 else if(tool.kind==='web-local')action=tool.available?(tool.webUrl?open(t('vfx.tool.open'),<Globe size={12}/>,onWeb):<span className="optional vfx-warn" title={tool.webError}>{t('vfx.tool.webDown')}</span>):<Missing/>;
 else if(tool.kind==='file')action=tool.available?open(t('vfx.tool.openInMm'),<Play size={12}/>,onLaunch,!tool.launchable,tool.launchable?undefined:t('vfx.tool.needMm')):<Missing/>;
 return <div className={'vfx-card'+(tool.unavailable?' off':'')}>
  <div className="vfx-card-head"><strong>{tool.name}</strong>{tool.unavailable&&<span className="badge" title={t('vfx.tool.unavailableHint')}>{t('vfx.tool.unavailable')}</span>}</div>
  <div className="vfx-meta">{tool.author} · {tool.license}</div>
  <p className="vfx-desc">{toolDesc(tool)}</p>
  {!!tool.tags?.length&&<div className="vfx-tags">{tool.tags.map(x=><span key={x}>{x}</span>)}</div>}
  <div className="vfx-actions">{action}{program&&tool.available&&<button className="icon-btn" title={t('vfx.tool.folder')} aria-label={t('vfx.tool.folderAria',{name:tool.name})} disabled={busy} onClick={onFolder}><FolderOpen size={14}/></button>}<a className="vfx-link" href={tool.url} target="_blank" rel="noreferrer"><ExternalLink size={12}/>{t('vfx.tool.site')}</a></div>
 </div>;
}
export default function VfxPanel({studio,snapshot,notify}){
 const t=useT();
 const [data,setData]=useState(null),[loadError,setLoadError]=useState(''),[busy,setBusy]=useState(''),[web,setWeb]=useState(null),[textures,setTextures]=useState([]),[uploaded,setUploaded]=useState([]),[pickKey,setPickKey]=useState(''),[slot,setSlot]=useState('map'),[applying,setApplying]=useState(false);
 const imported=useRef(new Map()),fileInput=useRef(null),lastLoad=useRef(Date.now());
 const run=async fn=>{try{await fn();}catch(e){notify(e.message,true);}};
 const loadTools=useCallback(async fresh=>{try{setData(await api('/vfx-tools'+(fresh?'?fresh=1':'')));setLoadError('');}catch(e){setLoadError(e.message);}},[]);
 const loadTextures=useCallback(async()=>{try{setTextures(await api('/vfx-tools/textures'));}catch{}},[]);
 // Back in the studio from a generator or installer: refresh the program list and the ready-made textures
 useEffect(()=>{loadTools();loadTextures();const onFocus=()=>{if(Date.now()-lastLoad.current<3000)return;lastLoad.current=Date.now();loadTools(true);loadTextures();};window.addEventListener('focus',onFocus);return()=>window.removeEventListener('focus',onFocus);},[loadTools,loadTextures]);
 useEffect(()=>{if(!web)return;const onKey=e=>{if(e.key==='Escape')setWeb(null);};window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);},[web]);
 const act=(tool,what)=>run(async()=>{
  if(what==='install'&&!window.confirm(t('vfx.confirm.install',{name:tool.name})))return;
  setBusy(tool.id);try{await api('/vfx-tools/'+tool.id+'/'+what,{method:'POST'});}finally{setBusy('');}
  if(what!=='folder')notify(what==='install'?t('vfx.notify.installer',{name:tool.name}):t('vfx.notify.launching',{name:tool.name}));
 });
 const openFolder=where=>run(async()=>{await api('/vfx-tools/folder',{method:'POST',body:JSON.stringify(where?{where}:{})});await loadTools();});
 const refreshTextures=()=>run(async()=>{setTextures(await api('/vfx-tools/textures'));await loadTools(true);});
 const pickFile=e=>{const f=e.target.files[0];e.target.value='';if(!f)return;run(async()=>{const asset=await upload(f);setUploaded(u=>[asset,...u].slice(0,20));setPickKey('u:'+asset.id);});};
 const items=[...uploaded.map(a=>({key:'u:'+a.id,name:a.name,url:a.url,asset:a})),...textures.map(x=>({key:'f:'+x.name,name:x.name,url:x.url,stamp:x.mtime+x.size}))];
 const picked=items.find(x=>x.key===pickKey);
 const apply=()=>run(async()=>{setApplying(true);try{let asset=picked.asset||imported.current.get(picked.key+picked.stamp);if(!asset){asset=await api('/vfx-tools/textures/'+encodeURIComponent(picked.name)+'/import',{method:'POST'});imported.current.set(picked.key+picked.stamp,asset);}await studio.current.applyTextureFile(asset,slot);notify(t('vfx.notify.applied',{name:picked.name,slot:t(SLOTS.find(s=>s[0]===slot)[1])}));}finally{setApplying(false);}});
 const groups=data?Object.entries(data.kinds).map(([kind,title])=>[kind,kindTitle(kind,title),data.tools.filter(x=>x.kind===kind)]).filter(g=>g[2].length):[];
 const card=tool=><ToolCard key={tool.id} t={tool} busy={busy===tool.id} onLaunch={()=>act(tool,'launch')} onInstall={()=>act(tool,'install')} onFolder={()=>act(tool,'folder')} onWeb={()=>setWeb(tool)}/>;
 const texturesPath=data?data.root.replace(/[\\/]+$/,'')+'\\textures':'';
 return <div className="panel-scroll">
  <div className="section intro"><span className="step-label">{t('vfx.intro.step')}</span><h1>{t('vfx.intro.title')}</h1><p>{t('vfx.intro.line1')}<br/>{t('vfx.intro.line2')}</p></div>
  <div className="section vfx-status">
   <div className="vfx-root"><small>{t('vfx.root.label')}</small><code title={data?.root}>{data?.root||'…'}</code></div>
   <button className="secondary small" onClick={()=>openFolder()}><FolderOpen size={12}/>{t('vfx.root.open')}</button>
   {data&&!data.rootExists&&<p className="field-note vfx-warn">{t('vfx.root.missingBefore')} <code>scripts\vfx\Install-VfxTools.ps1</code>.</p>}
   {loadError&&<p className="field-note vfx-warn">{t('vfx.root.loadError',{error:loadError})}</p>}
   {data?.webError&&<p className="field-note vfx-warn">{data.webError}</p>}
  </div>
  <Section title={t('vfx.tex.title')} aside={<span className="optional">{items.length||t('vfx.tex.empty')}</span>}>
   <p className="field-note vfx-first">{t('vfx.tex.saveHere')}<br/><code>{texturesPath||'textures'}</code></p>
   <div className="vfx-row"><button className="secondary small" onClick={refreshTextures}><RefreshCw size={12}/>{t('vfx.tex.refresh')}</button><button className="secondary small" onClick={()=>openFolder('textures')}><FolderOpen size={12}/>{t('vfx.root.open')}</button><button className="secondary small" onClick={()=>fileInput.current.click()}><Upload size={12}/>{t('vfx.tex.upload')}</button></div>
   {!!items.length&&<div className="vfx-grid">{items.map(x=><button key={x.key} className={'vfx-thumb'+(x.key===pickKey?' active':'')} title={x.name} aria-pressed={x.key===pickKey} onClick={()=>setPickKey(x.key)}><img src={x.url} alt={x.name} loading="lazy"/><span>{x.name}</span></button>)}</div>}
   {picked&&<div className="vfx-apply">
    <div className="vfx-picked" title={picked.name}>{picked.name}</div>
    <label className="rig-label">{t('vfx.apply.slot')}<select aria-label={t('vfx.apply.slotAria')} value={slot} onChange={e=>setSlot(e.target.value)}>{SLOTS.map(([id,key])=><option key={id} value={id}>{t(key)}</option>)}</select></label>
    <button className="primary full" disabled={!snapshot?.selected||applying||!!snapshot?.surface?.task} onClick={apply}>{t('vfx.apply.button')}</button>
    {!snapshot?.selected&&<p className="field-note">{t('vfx.apply.selectObject')}</p>}
    {!!snapshot?.selected&&!snapshot?.surface?.uv&&<p className="field-note">{t('vfx.apply.noUv')}</p>}
   </div>}
   <input hidden type="file" ref={fileInput} accept=".png,.jpg,.jpeg,.webp" onChange={pickFile}/>
  </Section>
  {groups.map(([kind,title,list])=>FOLDED.includes(kind)
   ?<details key={kind} className="section vfx-fold"><summary><span>{title}</span><span className="optional">{list.length}</span></summary><div className="vfx-list">{list.map(card)}</div></details>
   :<Section key={kind} title={title} aside={<span className="optional">{list.length}</span>}><div className="vfx-list">{list.map(card)}</div></Section>)}
  {web&&<div className="vfx-web-back"><div className="vfx-web" role="dialog" aria-modal="true" aria-label={web.name}>
   <div className="vfx-web-head"><strong>{web.name}</strong><span>{web.author} · {web.license}</span><a className="vfx-link" href={web.webUrl} target="_blank" rel="noreferrer"><ExternalLink size={12}/>{t('vfx.web.newTab')}</a><button className="icon-btn" title={t('vfx.web.closeHint')} aria-label={t('vfx.web.close')} autoFocus onClick={()=>setWeb(null)}><X size={17}/></button></div>
   <iframe title={web.name} src={web.webUrl} allow="fullscreen; clipboard-write"/>
  </div></div>}
 </div>;
}
