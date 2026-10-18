import React, {useState, useEffect, useRef} from 'react';
import {Upload, Sparkles, RefreshCw, X, Loader2, Box, Cloud, Cpu, Gem, Zap} from 'lucide-react';
import {api} from './api.js';
import {useT} from './i18n/index.js';
import './views.css';

// "Views" block of the Create tab: front view -> missing views (ChatGPT or local MV-Adapter)
// or your own files -> "Build 3D from views" (Pixal3D or Hunyuan3D-2mv, "3D engine" switch). Details: docs/forge/views.md, docs/forge/pixal3d.md.
const SLOTS = ['front', 'left', 'back', 'right'];
const FILLABLE = ['left', 'back', 'right'];

// 3D engine switch. Pixal3D is unavailable until installed: the reason is shown under the switch.
export function EngineSelect({engine, onChange, pixal}) {
  const t = useT();
  const ok = !!pixal?.installed;
  return <div className="engine-pick">
    <span className="engine-label">{t('views.engine.label')}</span>
    <div className="segmented" role="radiogroup" aria-label={t('views.engine.label')}>
      <button role="radio" aria-checked={engine === 'pixal3d'} className={engine === 'pixal3d' ? 'active' : ''} disabled={!ok}
        title={ok ? t('views.engine.pixalTitle') : (pixal?.message || t('views.engine.pixalMissing'))} onClick={() => onChange('pixal3d')}><Gem size={12} />Pixal3D</button>
      <button role="radio" aria-checked={engine === 'hunyuan'} className={engine === 'hunyuan' ? 'active' : ''}
        title={t('views.engine.hunyuanNote')} onClick={() => onChange('hunyuan')}><Zap size={12} />Hunyuan3D</button>
    </div>
    <p className="field-note">{engine === 'pixal3d'
      ? t('views.engine.pixalNote')
      : t('views.engine.hunyuanNote')}</p>
    {pixal && !ok && <p className="field-note views-warn">{pixal.message || t('views.engine.pixalMissing')}</p>}
    {ok && engine === 'pixal3d' && !pixal.running && <p className="field-note">{t('views.engine.pixalAutostart')}</p>}
  </div>;
}

export default function ViewsPanel({image, notify, onBuild, buildBusy, engineReady, engine = 'hunyuan', onEngine, pixal}) {
  const t = useT();
  const [set, setSet] = useState(null);
  const [status, setStatus] = useState(null);
  const [provider, setProvider] = useState('codex');
  const [busy, setBusy] = useState(false);
  const fileInput = useRef(null);
  const pickSlot = useRef(null);
  const lastTask = useRef(null);
  const run = async fn => { try { return await fn(); } catch (e) { notify(e.message, true); } };

  useEffect(() => {
    api('/views/status').then(s => {
      setStatus(s);
      if (!s.codex?.available && s.mvadapter?.ready) setProvider('mvadapter');
    }).catch(() => {});
  }, []);

  // after generation the studio swaps in the cut-out reference (originalId is the source image): the view set stays the same
  const frontId = image?.originalId || image?.id;
  useEffect(() => {
    let alive = true;
    setSet(null);
    if (!frontId) return undefined;
    api('/views', {method: 'POST', body: JSON.stringify({imageId: frontId})})
      .then(s => { if (alive) setSet(s); })
      .catch(e => { if (alive) notify(t('views.error', {message: e.message}), true); });
    return () => { alive = false; };
  }, [frontId]);

  const task = set?.task;
  const running = task?.status === 'running';
  useEffect(() => {
    if (!set?.id || !running) return undefined;
    const timer = setInterval(() => api('/views/' + set.id).then(setSet).catch(() => {}), 2000);
    return () => clearInterval(timer);
  }, [set?.id, running]);
  useEffect(() => {
    const key = task ? task.startedAt + task.status : null;
    if (!task || task.status === 'running' || lastTask.current === key) return;
    if (lastTask.current !== null || task.finishedAt) {
      if (task.status === 'failed') notify(t('views.error', {message: task.message}), true);
      else if (task.status === 'completed') notify(t('views.done'));
    }
    lastTask.current = key;
  }, [task?.status, task?.startedAt]);

  const upload = async (slot, file) => {
    if (!file || !set) return;
    setBusy(true);
    await run(async () => {
      const form = new FormData();
      form.append('file', file);
      setSet(await api('/views/' + set.id + '/' + slot, {method: 'PUT', body: form}));
    });
    setBusy(false);
  };
  const remove = async slot => {
    setBusy(true);
    await run(async () => setSet(await api('/views/' + set.id + '/' + slot, {method: 'DELETE'})));
    setBusy(false);
  };
  const generate = async (slots, overwrite = false) => {
    setBusy(true);
    await run(async () => {
      lastTask.current = null;
      setSet(await api('/views/' + set.id + '/generate', {method: 'POST', body: JSON.stringify({provider, slots, overwrite})}));
    });
    setBusy(false);
  };
  const cancel = () => run(async () => setSet(await api('/views/' + set.id + '/cancel', {method: 'POST'})));

  const providerOk = provider === 'codex' ? status?.codex?.available : status?.mvadapter?.ready;
  const providerWhy = provider === 'codex' ? status?.codex?.message : status?.mvadapter?.reasons?.join('; ');
  const missing = FILLABLE.filter(s => !set?.slots?.[s]?.filled);
  const disabled = busy || running || !set;

  if (!image) {
    return <p className="field-note">{t('views.empty')}</p>;
  }
  return <div className="views-block">
    <div className="views-grid">
      {SLOTS.map(slot => {
        const label = t('views.slot.' + slot);
        const s = set?.slots?.[slot];
        return <div key={slot} className={'view-slot ' + (s?.filled ? 'filled' : '')}>
          <div className="view-frame">
            {s?.filled
              ? <img src={s.thumb} alt={t('views.slot.alt', {label})} />
              : <button className="view-empty" title={t('views.slot.upload', {label})} disabled={disabled}
                  onClick={() => { pickSlot.current = slot; fileInput.current.click(); }}
                  onDragOver={e => e.preventDefault()}
                  onDrop={e => { e.preventDefault(); upload(slot, e.dataTransfer.files[0]); }}>
                  <Upload size={16} /><span>{t('views.slot.uploadShort')}</span>
                </button>}
            {running && task.slots?.includes(slot) && !s?.filled && <div className="view-wait"><Loader2 className="spin" size={18} /></div>}
            {s?.filled && slot !== 'front' && <div className="view-tools">
              <button title={t('views.slot.replace')} disabled={disabled} onClick={() => { pickSlot.current = slot; fileInput.current.click(); }}><Upload size={12} /></button>
              <button title={t('views.slot.regenerate')} disabled={disabled || !providerOk} onClick={() => generate([slot], true)}><RefreshCw size={12} /></button>
              <button title={t('views.slot.remove')} disabled={disabled} onClick={() => remove(slot)}><X size={12} /></button>
            </div>}
          </div>
          <div className="view-caption"><strong>{label}</strong><span>{s?.filled ? (s.source === 'codex' ? 'ChatGPT' : t('views.source.' + s.source)) : '—'}</span>{s?.filled && s.seconds ? <span>{t('views.seconds', {n: Math.round(s.seconds)})}</span> : null}</div>
        </div>;
      })}
    </div>

    <div className="segmented views-provider" role="radiogroup" aria-label={t('views.provider.label')}>
      <button role="radio" aria-checked={provider === 'codex'} className={provider === 'codex' ? 'active' : ''} onClick={() => setProvider('codex')}><Cloud size={12} />ChatGPT</button>
      <button role="radio" aria-checked={provider === 'mvadapter'} className={provider === 'mvadapter' ? 'active' : ''} onClick={() => setProvider('mvadapter')}><Cpu size={12} />{t('views.provider.local')}</button>
    </div>
    <p className="field-note">{provider === 'codex'
      ? t('views.provider.codexNote')
      : t('views.provider.localNote')}</p>
    {status && !providerOk && <p className="field-note views-warn">{providerWhy || t('views.provider.unavailable')}</p>}

    {running
      ? <div className="views-progress"><Loader2 className="spin" size={14} /><span>{task.message}</span><button onClick={cancel}>{t('views.cancel')}</button></div>
      : <button className="secondary full" disabled={disabled || !providerOk || !missing.length} onClick={() => generate(missing)}>
        <Sparkles size={15} />{missing.length ? t('views.generate', {n: missing.length}) : t('views.allPresent')}
      </button>}
    {task?.status === 'failed' && !running && <p className="field-note views-warn">{task.message}</p>}

    {onEngine && <EngineSelect engine={engine} onChange={onEngine} pixal={pixal} />}
    <button className="primary full views-build" disabled={disabled || !set?.canBuild || !set?.extraViews || buildBusy || !engineReady} onClick={() => onBuild(set.id)}>
      {buildBusy ? <Loader2 className="spin" size={16} /> : <Box size={16} />}{t('views.build')}
    </button>
    <p className="field-note">{!set?.extraViews
      ? t('views.build.needView')
      : t('views.build.note', {engine: engine === 'pixal3d' ? 'Pixal3D' : 'Hunyuan3D-2mv', n: 1 + set.extraViews}) + ' '
        + (engine === 'pixal3d' ? t('views.build.pixalNote') : t('views.build.hunyuanNote'))}</p>
    <input type="file" hidden ref={fileInput} accept=".png,.jpg,.jpeg,.webp" onChange={e => { const f = e.target.files[0]; e.target.value = ''; upload(pickSlot.current, f); }} />
  </div>;
}
