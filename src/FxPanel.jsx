import React,{useState,useEffect,useRef} from 'react';
import {Plus,Copy,Trash2,Eye,EyeOff,Zap,Play,Download,RotateCcw,FolderOpen,Upload,X,Film,RefreshCw} from 'lucide-react';
import {api,upload} from './api.js';
import {FX_PRESET_LIST,FX_TEXTURES,FX_TEXTURE_TITLES,FX_SHAPES,FX_SHAPE_TITLES,FX_BLENDS,FX_BLEND_TITLES,FX_LIMITS,presetTitle,presetDescription,presetOf,fxTexture,sampleCurve} from './fx-core.js';
import {useT,getLang} from './i18n/index.js';
import FxTextureLab from './FxTextureLab.jsx';
import './fx.css';
// Effects tab: particle emitters on the model (fx-core.js = data and maths, fx-runtime.js = viewport rendering and studio.exportFxPackage).
const num=(v,d=2)=>{const n=Number(v);return Number.isFinite(n)?String(+n.toFixed(d)):'0';};
const clampN=(v,a,b)=>Math.min(b,Math.max(a,v));
const hex=c=>'#'+c.slice(0,3).map(v=>Math.round(clampN(v,0,1)*255).toString(16).padStart(2,'0')).join('');
const unhex=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16)/255);
const locale=()=>getLang()==='ru'?'ru-RU':'en-US';
const THUMBS={};
function thumbUrl(kind){if(THUMBS[kind])return THUMBS[kind];const t=fxTexture(kind,64),c=document.createElement('canvas');c.width=c.height=64;c.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(t.data),64,64),0,0);return THUMBS[kind]=c.toDataURL('image/png');}
function Section({title,children,aside}){return <section className="section"><div className="section-heading"><h3>{title}</h3>{aside}</div>{children}</section>;}
function Fold({title,open=false,aside,children}){return <details className="fx-fold" open={open}><summary><span>{title}</span>{aside}</summary><div className="fx-fold-body">{children}</div></details>;}
// Number field: text is typed freely, the value goes out as soon as it is valid; on blur the text is normalised.
function NumBox({value,onChange,min=-1e6,max=1e6,step=.1,digits=2,label}){const [txt,setTxt]=useState(num(value,digits)),foc=useRef(false);
 useEffect(()=>{if(!foc.current)setTxt(num(value,digits));},[value,digits]);
 const push=s=>{const v=parseFloat(String(s).replace(',','.'));if(Number.isFinite(v))onChange(clampN(v,min,max));};
 return <input className="fx-num" aria-label={label} inputMode="decimal" value={txt} onFocus={()=>{foc.current=true;}} onChange={e=>{setTxt(e.target.value);push(e.target.value);}} onBlur={()=>{foc.current=false;setTxt(num(value,digits));}} onKeyDown={e=>{if(e.key==='Enter')e.currentTarget.blur();else if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();onChange(clampN(Math.round((value+(e.key==='ArrowUp'?step:-step))*1e6)/1e6,min,max));}}}/>;}
function Row({label,mod,children,hint}){return <div className={'fx-row'+(mod?' mod':'')} title={hint}><span className="fx-lab">{label}</span><div className="fx-ctl">{children}</div></div>;}
function Num({label,value,onChange,min=0,max=1,smin=min,smax=max,step=.01,digits=2,mod,hint}){return <Row label={label} mod={mod} hint={hint}><input type="range" min={smin} max={smax} step={step} value={clampN(value,smin,smax)} aria-label={label} onChange={e=>onChange(Number(e.target.value))}/><NumBox value={value} min={min} max={max} step={step} digits={digits} onChange={onChange} label={label}/></Row>;}
function Pair({label,value,onChange,min=-1e4,max=1e4,step=.1,mod,hint}){const t=useT();return <Row label={label} mod={mod} hint={hint}><span className="fx-sub">{t('fx.pair.from')}</span><NumBox value={value[0]} min={min} max={max} step={step} onChange={v=>onChange([v,Math.max(v,value[1])])} label={t('fx.pair.fromLabel',{label})}/><span className="fx-sub">{t('fx.pair.to')}</span><NumBox value={value[1]} min={min} max={max} step={step} onChange={v=>onChange([Math.min(v,value[0]),v])} label={t('fx.pair.toLabel',{label})}/></Row>;}
function Vec({label,value,onChange,min=-1e4,max=1e4,step=.1,mod,hint}){return <Row label={label} mod={mod} hint={hint}>{['X','Y','Z'].map((a,i)=><React.Fragment key={a}><span className="fx-sub">{a}</span><NumBox value={value[i]} min={min} max={max} step={step} label={label+' '+a} onChange={v=>onChange(value.map((x,j)=>j===i?v:x))}/></React.Fragment>)}</Row>;}
function Toggle({label,value,onChange,mod,hint}){return <label className={'fx-check'+(mod?' mod':'')} title={hint}><input type="checkbox" checked={!!value} onChange={e=>onChange(e.target.checked)}/><span>{label}</span></label>;}
function Choice({label,value,options,onChange,mod}){return <Row label={label} mod={mod}><select aria-label={label} value={value} onChange={e=>onChange(e.target.value)}>{options.map(([id,name])=><option key={id} value={id}>{name}</option>)}</select></Row>;}
function RampEditor({ramp,onChange,mod}){const t=useT();
 const css='linear-gradient(90deg,'+ramp.map(([at,c])=>'rgba('+c.slice(0,3).map(v=>Math.round(v*255)).join(',')+','+c[3]+') '+Math.round(at*100)+'%').join(',')+')';
 const set=(i,fn)=>onChange(ramp.map((s,j)=>j===i?fn(s):s));
 const add=()=>{let at=0,gap=-1;for(let i=1;i<ramp.length;i++){const g=ramp[i][0]-ramp[i-1][0];if(g>gap){gap=g;at=i;}}const a=ramp[at-1],b=ramp[at];onChange([...ramp,[(a[0]+b[0])/2,a[1].map((v,k)=>(v+b[1][k])/2)]]);};
 return <div className={'fx-ramp'+(mod?' mod':'')}><div className="fx-ramp-bar"><i style={{background:css}}/></div>
  {ramp.map(([at,c],i)=><div className="fx-stop" key={i}><NumBox value={at} min={0} max={1} step={.05} label={t('fx.ramp.stopPos',{n:i+1})} onChange={v=>set(i,s=>[v,s[1]])}/><input type="color" aria-label={t('fx.ramp.stopColor',{n:i+1})} value={hex(c)} onChange={e=>set(i,s=>[s[0],[...unhex(e.target.value),s[1][3]]])}/><span className="fx-sub">α</span><NumBox value={c[3]} min={0} max={1} step={.05} label={t('fx.ramp.stopAlpha',{n:i+1})} onChange={v=>set(i,s=>[s[0],[s[1][0],s[1][1],s[1][2],v]])}/><button className="icon-btn" title={t('fx.ramp.removeStop')} aria-label={t('fx.ramp.removeStop')} disabled={ramp.length<=2} onClick={()=>onChange(ramp.filter((_,j)=>j!==i))}><X size={12}/></button></div>)}
  <button className="subtle-btn" disabled={ramp.length>=5} onClick={add}><Plus size={12}/>{t('fx.ramp.addStop')}</button></div>;}
function CurveEditor({curve,onChange,mod}){const t=useT();
 const pts=Array.from({length:41},(_,i)=>{const at=i/40;return (at*100).toFixed(1)+','+(30-clampN(sampleCurve(curve,at),0,3)/3*28-1).toFixed(1);}).join(' ');
 const set=(i,fn)=>onChange(curve.map((p,j)=>j===i?fn(p):p));
 const add=()=>{let at=1,gap=-1;for(let i=1;i<curve.length;i++){const g=curve[i][0]-curve[i-1][0];if(g>gap){gap=g;at=i;}}const a=curve[at-1],b=curve[at];onChange([...curve,[(a[0]+b[0])/2,(a[1]+b[1])/2]]);};
 return <div className={'fx-curve'+(mod?' mod':'')}><svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-label={t('fx.curve.aria')}><line x1="0" x2="100" y1={30-1/3*28-1} y2={30-1/3*28-1} className="fx-curve-one"/><polyline points={pts}/></svg>
  {curve.map(([at,v],i)=><div className="fx-stop" key={i}><NumBox value={at} min={0} max={1} step={.05} label={t('fx.curve.pointTime',{n:i+1})} onChange={x=>set(i,p=>[x,p[1]])}/><input type="range" min="0" max="3" step=".05" value={clampN(v,0,3)} aria-label={t('fx.curve.pointSize',{n:i+1})} onChange={e=>set(i,p=>[p[0],Number(e.target.value)])}/><NumBox value={v} min={0} max={10} step={.05} label={t('fx.curve.pointMul',{n:i+1})} onChange={x=>set(i,p=>[p[0],x])}/><button className="icon-btn" title={t('fx.curve.removePoint')} aria-label={t('fx.curve.removePoint')} disabled={curve.length<=2} onClick={()=>onChange(curve.filter((_,j)=>j!==i))}><X size={12}/></button></div>)}
  <button className="subtle-btn" disabled={curve.length>=6} onClick={add}><Plus size={12}/>{t('fx.curve.addPoint')}</button></div>;}
const LEGACY_DESC={sparks:'fx.legacy.desc.sparks',engine:'fx.legacy.desc.engine',energy:'fx.legacy.desc.energy'};

export default function FxPanel({studio,snapshot,notify,onEdit,legacy={}}){
 const t=useT();
 const s=()=>studio.current,fx=snapshot?.fx,sel=fx?.selected||null,res=sel?.res,changed=new Set(Object.keys(sel?.fx.params||{})),editor=snapshot?.editor||{};
 const [vfxOpen,setVfxOpen]=useState(false),[vfxItems,setVfxItems]=useState([]),[busy,setBusy]=useState(''),fileInput=useRef(null);
 useEffect(()=>{const st=studio.current;st?.setFxTab(true);return()=>st?.setFxTab(false);},[studio]);
 const edit=fn=>{try{fn();onEdit?.();}catch(e){notify(e.message,true);}};
 const act=async(name,fn)=>{setBusy(name);try{await fn();}catch(e){notify(e.message,true);}finally{setBusy('');}};
 const set=(key,value)=>edit(()=>s().updateFx(sel.id,{params:{[key]:value}}));
 const patch=p=>edit(()=>s().updateFx(sel.id,p));
 const m=k=>changed.has(k);
 /** Assign a texture asset to the effect (from the VFX folder, from disk or from the FxTextureLab generator); meta.flipbook = {h,v[,fps]} | null. */
 const useTexture=(asset,meta)=>{if(!sel)return;const fb=meta?.flipbook;patch({textureAsset:{id:asset.id,url:asset.url,name:asset.name||''},flipbook:fb&&fb.h*fb.v>1?{h:fb.h,v:fb.v,fps:fb.fps||res.flipbook?.fps||12}:null});};
 const loadVfx=async()=>{const list=await api('/vfx-tools/textures');setVfxItems(list);};
 const toggleVfx=()=>{const open=!vfxOpen;setVfxOpen(open);if(open)act('vfx',loadVfx);};
 const pickVfx=x=>act('import',async()=>{const asset=await api('/vfx-tools/textures/'+encodeURIComponent(x.name)+'/import',{method:'POST'});useTexture(asset,null);notify(t('fx.notify.vfxAssigned'));});
 const pickFile=e=>{const f=e.target.files[0];e.target.value='';if(!f)return;act('upload',async()=>{const asset=await upload(f);useTexture(asset,null);notify(t('fx.notify.uploaded'));});};
 const exportZip=()=>act('zip',async()=>{const pkg=await s().exportFxPackage();notify(t('fx.notify.exported',{n:pkg.bindings.length})+(pkg.warnings.length?' · '+pkg.warnings[0]:''));});
 const em=res?.emission;
 const toggleLight=v=>{if(!v)return set('light',null);if(presetOf(sel.fx.preset).light)patch({unset:['light']});else set('light',{color:[1,.8,.5],energy:1.5,range:3});};
 const fmt=n=>n.toLocaleString(locale());
 return <div className="panel-scroll fx-panel">
  <div className="section intro"><span className="step-label">{t('fx.intro.step')}</span><h1>{t('fx.intro.title')}</h1><p>{t('fx.intro.line1')}<br/>{t('fx.intro.line2')}</p></div>
  <Section title={t('fx.add.title')} aside={<span className="optional">{t('fx.add.inScene',{n:fx?.list.length||0})}</span>}>
   <p className="fx-where">{t('fx.add.where')} <b>{fx?.target?fx.target:t('fx.add.sceneCenter')}</b>{fx?.target?' '+t('fx.add.whereHint'):''}</p>
   <div className="fx-presets">{FX_PRESET_LIST.map(p=><button key={p.id} className={p.id==='empty'?'fx-empty':''} title={presetDescription(p.id)} onClick={()=>edit(()=>s().addFx(p.id))}>{p.id==='empty'?<Plus size={13}/>:<Zap size={13}/>}<span>{p.title}</span></button>)}</div>
   <p className="field-note">{t('fx.add.note')}</p>
   {fx?.budget.over&&<p className="fx-warn">{t('fx.add.overBudget',{requested:fmt(fx.budget.requested),total:fmt(FX_LIMITS.total),amount:fmt(FX_LIMITS.amount)})}</p>}
  </Section>
  <Section title={t('fx.list.title')}>
   {fx?.list.length?<div className="fx-list">{fx.list.map(e=><div key={e.id} className={'fx-item'+(sel?.id===e.id?' selected':'')+(e.shown?'':' off')}><button className="fx-pick" onClick={()=>s().select(e.id)}><Zap size={12}/><span>{e.name}</span><small>{e.parent||t('fx.list.scene')} · {presetTitle(e.preset)}</small></button>
    <button className="icon-btn" title={e.visible?t('fx.list.hide'):t('fx.list.show')} aria-label={e.visible?t('fx.list.hideAria'):t('fx.list.showAria')} onClick={()=>edit(()=>s().toggleVisibility(e.id))}>{e.visible?<Eye size={13}/>:<EyeOff size={13}/>}</button>
    <button className="icon-btn" title={t('fx.list.duplicate')} aria-label={t('fx.list.duplicateAria')} onClick={()=>edit(()=>{s().select(e.id);s().duplicate();})}><Copy size={13}/></button>
    <button className="icon-btn" title={t('fx.list.delete')} aria-label={t('fx.list.deleteAria')} onClick={()=>edit(()=>{s().select(e.id);s().remove();})}><Trash2 size={13}/></button></div>)}</div>
   :<p className="field-note fx-none">{t('fx.list.empty')}</p>}
  </Section>
  {sel&&res&&<>
   <Section title={t('fx.sel.title')} aside={<span className="badge">{presetTitle(sel.fx.preset)}</span>}>
    <input className="name-input" aria-label={t('fx.sel.nameAria')} value={sel.name} onChange={e=>patch({name:e.target.value})}/>
    <Row label={t('fx.sel.base')}><select aria-label={t('fx.sel.baseAria')} value={sel.fx.preset} onChange={e=>patch({preset:e.target.value})}>{FX_PRESET_LIST.map(p=><option key={p.id} value={p.id}>{p.title}</option>)}</select></Row>
    <div className="fx-buttons"><button className="secondary small" disabled={!changed.size&&!sel.fx.textureAsset&&!sel.fx.flipbook} onClick={()=>patch({reset:true})}><RotateCcw size={13}/>{t('fx.sel.reset')}</button>{changed.size>0&&<span className="optional">{t('fx.sel.changed',{n:changed.size})}</span>}</div>
    <p className="field-note">{presetDescription(sel.fx.preset)} {t('fx.sel.note')}</p>
   </Section>
   <Section title={t('fx.params.title')}>
    <Fold title={t('fx.emit.title')} open>
     <Num label={t('fx.emit.amount')} value={res.amount} min={1} max={FX_LIMITS.amount} smax={600} step={1} digits={0} mod={m('amount')} hint={t('fx.emit.amountHint',{amount:FX_LIMITS.amount,total:FX_LIMITS.total})} onChange={v=>set('amount',Math.round(v))}/>
     <Num label={t('fx.emit.lifetime')} value={res.lifetime} min={.02} max={60} smax={6} step={.05} mod={m('lifetime')} onChange={v=>set('lifetime',v)}/>
     <Num label={t('fx.emit.speedScale')} value={res.speed_scale} min={.05} max={10} smax={3} step={.05} mod={m('speed_scale')} onChange={v=>set('speed_scale',v)}/>
     <Num label={t('fx.emit.explosiveness')} value={res.explosiveness} mod={m('explosiveness')} hint={t('fx.emit.explosivenessHint')} onChange={v=>set('explosiveness',v)}/>
     <Num label={t('fx.emit.randomness')} value={res.randomness} mod={m('randomness')} onChange={v=>set('randomness',v)}/>
     <div className="fx-checks"><Toggle label={t('fx.emit.oneShot')} value={res.one_shot} mod={m('one_shot')} onChange={v=>set('one_shot',v)}/>
      <Toggle label={t('fx.emit.localCoords')} value={res.local_coords} mod={m('local_coords')} hint={t('fx.emit.localCoordsHint')} onChange={v=>set('local_coords',v)}/>
      <Toggle label={t('fx.emit.autostart')} value={res.autostart} mod={m('autostart')} onChange={v=>set('autostart',v)}/></div>
     {(res.one_shot||!res.autostart)&&<button className="secondary small fx-fire" onClick={()=>edit(()=>s().triggerFx(sel.id))}><Play size={12}/>{t('fx.emit.fire')}</button>}
     {res.one_shot&&<p className="field-note">{t('fx.emit.fireNote',{time:num(res.startTime)})}</p>}
    </Fold>
    <Fold title={t('fx.shape.title')} aside={<span className="optional">{FX_SHAPE_TITLES[em.shape]}</span>}>
     <Choice label={t('fx.shape.label')} value={em.shape} mod={m('emission')} options={FX_SHAPES.map(k=>[k,FX_SHAPE_TITLES[k]])} onChange={v=>set('emission',{...em,shape:v})}/>
     {['sphere','sphere_surface','ring'].includes(em.shape)&&<Num label={t('fx.shape.radius')} value={em.radius} min={0} max={100} smax={2} step={.01} mod={m('emission')} onChange={v=>set('emission',{...em,radius:v})}/>}
     {em.shape==='ring'&&<><Num label={t('fx.shape.innerRadius')} value={em.inner_radius} min={0} max={100} smax={2} step={.01} mod={m('emission')} onChange={v=>set('emission',{...em,inner_radius:v})}/><Num label={t('fx.shape.ringHeight')} value={em.height} min={0} max={100} smax={1} step={.01} mod={m('emission')} onChange={v=>set('emission',{...em,height:v})}/><Vec label={t('fx.shape.ringAxis')} value={em.axis} min={-1} max={1} step={.1} mod={m('emission')} onChange={v=>set('emission',{...em,axis:v})}/></>}
     {em.shape==='box'&&<Vec label={t('fx.shape.extents')} value={em.extents} min={0} max={100} step={.05} mod={m('emission')} onChange={v=>set('emission',{...em,extents:v})}/>}
     {em.shape==='point'&&<p className="field-note">{t('fx.shape.pointNote')}</p>}
    </Fold>
    <Fold title={t('fx.motion.title')}>
     <Vec label={t('fx.motion.direction')} value={res.direction} min={-1000} max={1000} step={.1} mod={m('direction')} hint={t('fx.motion.directionHint')} onChange={v=>set('direction',v)}/>
     <div className="fx-chips">{[['fx.motion.up',[0,1,0]],['fx.motion.down',[0,-1,0]],['fx.motion.forward',[0,0,-1]],['fx.motion.back',[0,0,1]]].map(([k,v])=><button key={k} className="subtle-btn" onClick={()=>set('direction',v)}>{t(k)}</button>)}</div>
     <Num label={t('fx.motion.spread')} value={res.spread} min={0} max={180} step={1} digits={0} mod={m('spread')} onChange={v=>set('spread',v)}/>
     <Pair label={t('fx.motion.velocity')} value={res.velocity} min={-200} max={200} mod={m('velocity')} onChange={v=>set('velocity',v)}/>
     <Vec label={t('fx.motion.gravity')} value={res.gravity} min={-200} max={200} mod={m('gravity')} hint={t('fx.motion.gravityHint')} onChange={v=>set('gravity',v)}/>
     <Pair label={t('fx.motion.damping')} value={res.damping} min={0} max={200} mod={m('damping')} hint={t('fx.motion.dampingHint')} onChange={v=>set('damping',v)}/>
     <Num label={t('fx.motion.turbulence')} value={res.turbulence} min={0} max={10} smax={3} step={.05} mod={m('turbulence')} onChange={v=>set('turbulence',v)}/>
     <Pair label={t('fx.motion.tangential')} value={res.tangential_accel} min={-200} max={200} mod={m('tangential_accel')} hint={t('fx.motion.tangentialHint')} onChange={v=>set('tangential_accel',v)}/>
     <Pair label={t('fx.motion.radial')} value={res.radial_accel} min={-200} max={200} mod={m('radial_accel')} hint={t('fx.motion.radialHint')} onChange={v=>set('radial_accel',v)}/>
    </Fold>
    <Fold title={t('fx.look.title')}>
     <Num label={t('fx.look.size')} value={res.quad_size} min={.001} max={50} smax={2} step={.01} mod={m('quad_size')} onChange={v=>set('quad_size',v)}/>
     <Pair label={t('fx.look.scale')} value={res.scale} min={0} max={100} step={.05} mod={m('scale')} onChange={v=>set('scale',v)}/>
     <div className="fx-sublab">{t('fx.look.sizeCurve')} {m('scale_curve')&&<i className="fx-dot"/>}</div><CurveEditor curve={res.scale_curve} mod={m('scale_curve')} onChange={v=>set('scale_curve',v)}/>
     <div className="fx-sublab">{t('fx.look.colorRamp')} {m('color_ramp')&&<i className="fx-dot"/>}</div><RampEditor ramp={res.color_ramp} mod={m('color_ramp')} onChange={v=>set('color_ramp',v)}/>
     <Choice label={t('fx.look.blend')} value={res.blend} mod={m('blend')} options={FX_BLENDS.map(k=>[k,FX_BLEND_TITLES[k]])} onChange={v=>set('blend',v)}/>
     <Pair label={t('fx.look.angle')} value={res.angle} min={-720} max={720} step={5} mod={m('angle')} onChange={v=>set('angle',v)}/>
     <Pair label={t('fx.look.angularVelocity')} value={res.angular_velocity} min={-3600} max={3600} step={5} mod={m('angular_velocity')} onChange={v=>set('angular_velocity',v)}/>
    </Fold>
    <Fold title={t('fx.tex.title')} aside={<span className="optional">{res.textureAsset?res.textureAsset.name||t('fx.tex.ownFile'):FX_TEXTURE_TITLES[res.texture]}</span>}>
     <div className="fx-tex-grid">{FX_TEXTURES.map(k=><button key={k} className={!res.textureAsset&&res.texture===k?'active':''} title={FX_TEXTURE_TITLES[k]} onClick={()=>patch({texture:k,textureAsset:null})}><img src={thumbUrl(k)} alt=""/><span>{FX_TEXTURE_TITLES[k]}</span></button>)}</div>
     {res.textureAsset&&<div className="fx-asset"><img src={res.textureAsset.url} alt=""/><span>{res.textureAsset.name||t('fx.tex.texture')}</span><button className="icon-btn" title={t('fx.tex.builtinBack')} aria-label={t('fx.tex.removeOwn')} onClick={()=>patch({textureAsset:null})}><X size={13}/></button></div>}
     <div className="fx-buttons"><button className="secondary small" disabled={busy==='vfx'} onClick={toggleVfx}><FolderOpen size={13}/>{vfxOpen?t('fx.tex.hideVfx'):t('fx.tex.fromVfx')}</button><button className="secondary small" disabled={busy==='upload'} onClick={()=>fileInput.current.click()}><Upload size={13}/>{t('fx.tex.fromDisk')}</button><input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pickFile}/></div>
     {vfxOpen&&<div className="fx-vfx">{vfxItems.length?<div className="fx-vfx-grid">{vfxItems.map(x=><button key={x.name} title={x.name} disabled={busy==='import'} onClick={()=>pickVfx(x)}><img src={x.url} alt=""/><span>{x.name}</span></button>)}</div>:<p className="field-note">{busy==='vfx'?t('fx.tex.reading'):t('fx.tex.vfxEmpty')}</p>}<button className="subtle-btn" onClick={()=>act('vfx',loadVfx)}><RefreshCw size={12}/>{t('fx.tex.refresh')}</button></div>}
     <Toggle label={t('fx.tex.flipbook')} value={!!res.flipbook} onChange={v=>patch({flipbook:v?{h:4,v:4,fps:12}:null})}/>
     {res.flipbook&&<><Row label={t('fx.tex.frames')}><span className="fx-sub">{t('fx.tex.byX')}</span><NumBox value={res.flipbook.h} min={1} max={32} step={1} digits={0} label={t('fx.tex.framesH')} onChange={v=>patch({flipbook:{...res.flipbook,h:Math.round(v)}})}/><span className="fx-sub">{t('fx.tex.byY')}</span><NumBox value={res.flipbook.v} min={1} max={32} step={1} digits={0} label={t('fx.tex.framesV')} onChange={v=>patch({flipbook:{...res.flipbook,v:Math.round(v)}})}/></Row><Num label={t('fx.tex.fps')} value={res.flipbook.fps} min={1} max={120} smax={60} step={1} digits={0} onChange={v=>patch({flipbook:{...res.flipbook,fps:Math.round(v)}})}/></>}
     <details className="fx-lab-fold"><summary>{t('fx.tex.generator')}</summary><FxTextureLab notify={notify} onUse={useTexture} compact/></details>
    </Fold>
    <Fold title={t('fx.light.title')} aside={<span className="optional">{res.light?t('fx.light.on'):t('fx.light.off')}</span>}>
     <Toggle label={t('fx.light.toggle')} value={!!res.light} mod={m('light')} onChange={toggleLight}/>
     {res.light&&<><Row label={t('fx.light.color')}><input type="color" aria-label={t('fx.light.colorAria')} value={hex(res.light.color)} onChange={e=>set('light',{...res.light,color:unhex(e.target.value)})}/></Row><Num label={t('fx.light.energy')} value={res.light.energy} min={0} max={16} smax={6} step={.1} mod={m('light')} onChange={v=>set('light',{...res.light,energy:v})}/><Num label={t('fx.light.range')} value={res.light.range} min={.1} max={100} smax={12} step={.1} mod={m('light')} onChange={v=>set('light',{...res.light,range:v})}/></>}
    </Fold>
   </Section>
  </>}
  <Section title={t('fx.export.title')}>
   <button className="primary full" disabled={!fx?.list.length||busy==='zip'} onClick={exportZip}><Download size={15}/>{busy==='zip'?t('fx.export.building'):t('fx.export.button')}</button>
   <p className="field-note">{t('fx.export.noteBefore')} <code>fx/*.tscn</code> {t('fx.export.noteScenes')} <code>fx_bindings.json</code>, <code>af_fx_mount.gd</code> {t('fx.export.noteAfter')}</p>
  </Section>
  <Section title={t('fx.video.title')}><button className="primary full" onClick={()=>edit(()=>snapshot?.recording?s().finishRecord():s().record())}><Film size={17}/>{snapshot?.recording?t('fx.video.stop'):t('fx.video.record')}</button><p className="field-note">{t('fx.video.note')}</p></Section>
  <Section title={t('fx.legacy.title')}><details className="vfx-fold fx-legacy"><summary><span>{t('fx.legacy.summary')}</span><span className="optional">{legacy[editor.effect]||t('fx.legacy.none')}</span></summary>
   <div className="preset-list">{Object.entries(legacy).map(([id,name])=><button key={id} className={editor.effect===id?'active':''} onClick={()=>edit(()=>s().setEditor({effect:id}))}><Zap size={18}/><span>{name}<small>{t(LEGACY_DESC[id]||'fx.legacy.desc.none')}</small></span></button>)}</div>
   <div className="fx-legacy-size"><span className="fx-lab">{t('fx.legacy.size')}</span><input type="range" min="0.3" max="3" step="0.1" value={editor.effectSize||1} onChange={e=>edit(()=>s().setEditor({effectSize:Number(e.target.value)}))}/></div>
   <p className="field-note">{t('fx.legacy.note')}</p></details></Section>
 </div>;
}
