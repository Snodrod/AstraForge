// Step 4 "Motion": Mixamo clip library + retarget for humans, procedural clips for creatures.
import React, { useEffect, useMemo, useState } from 'react';
import { Search, Play, Loader2, Footprints, Check, RefreshCw } from 'lucide-react';
import {
  clipList, clipCategories, filterClips, defaultClipOptions, clipsForPassport, retargetClips, categoryLabel, paramDefs,
  resolveParamName, buildParams, creatureClipOptions, creatureClipAvailable, creatureClipsForPassport,
  creatureNamesFromPassport, latestPipelineEntry, planSteps, replayNote,
} from './forge-core.js';
import { getClips, uploadClips } from './forge-api.js';
import { Section, Notice, Chips, Dropzone, StepResult, useForge } from './ForgeUi.jsx';
import StickFigure from './StickFigure.jsx';
import { useT } from '../i18n/index.js';

export default function StepMotion() {
  const { kind } = useForge();
  return kind === 'creature' ? <CreatureClips /> : <ClipLibrary />;
}

function MixamoHelp() {
  const t = useT();
  return (
    <details className="forge-more forge-help">
      <summary>{t('wizard.motion.helpSummary')}</summary>
      <ol className="forge-steps-list">
        <li>{t('wizard.motion.help1')}</li>
        <li>{t('wizard.motion.help2')}</li>
        <li>{t('wizard.motion.help3')}</li>
        <li>{t('wizard.motion.help4')}</li>
      </ol>
      <p className="field-note">{t('wizard.motion.helpNote')}</p>
    </details>
  );
}

function ClipLibrary() {
  const t = useT();
  const { offline, registry, asset, notify, track, patchAsset, runPipeline, activeJobFor } = useForge();
  const [index, setIndex] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [category, setCategory] = useState('all');
  const [query, setQuery] = useState('');
  const [selection, setSelection] = useState({});
  const [preview, setPreview] = useState(null);
  const [uploading, setUploading] = useState(false);
  const running = activeJobFor(['retarget']);
  const defs = paramDefs(registry, 'retarget');
  const clipsName = resolveParamName(defs, 'clips');

  const load = () => {
    if (offline) return;
    setLoadError('');
    getClips()
      .then(setIndex)
      .catch((e) => {
        setIndex({ clips: [] });
        setLoadError(e.message);
      });
  };
  useEffect(load, [offline]); // eslint-disable-line react-hooks/exhaustive-deps

  // Selection follows the passport clips (library:<id>).
  useEffect(() => {
    const fromPassport = {};
    for (const c of asset?.clips || []) {
      const src = String(c.source || '');
      if (src.startsWith('library:')) fromPassport[src.slice(8)] = { loop: !!c.loop, in_place: c.in_place !== false };
    }
    setSelection(fromPassport);
  }, [asset?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const clips = clipList(index);
  const chips = useMemo(() => clipCategories(clips), [clips]);
  const shown = useMemo(() => filterClips(clips, { category, query }), [clips, category, query]);
  const selectedCount = Object.keys(selection).length;
  const previewClip = clips.find((c) => c.id === preview);

  const toggle = (clip) =>
    setSelection((s) => {
      const next = { ...s };
      if (next[clip.id]) delete next[clip.id];
      else next[clip.id] = defaultClipOptions(clip);
      return next;
    });
  const setOpt = (id, key, value) => setSelection((s) => ({ ...s, [id]: { ...s[id], [key]: value } }));

  async function onUpload(files) {
    const fbx = files.filter((f) => /\.fbx$/i.test(f.name));
    if (!fbx.length) {
      notify(t('wizard.motion.needFbx'), true);
      return;
    }
    setUploading(true);
    try {
      const r = await uploadClips(fbx);
      const jobIds = [r?.jobId, ...(Array.isArray(r?.jobs) ? r.jobs.map((j) => j.jobId || j.id) : [])].filter(Boolean);
      if (jobIds.length) {
        for (const id of jobIds) track(id, { steps: [{ step: 'clip_library' }] }, () => load());
        notify(t('wizard.motion.importStarted', { n: fbx.length }));
      } else {
        load();
        notify(t('wizard.motion.clipsAdded'));
      }
    } catch (e) {
      notify(e.message, true);
    } finally {
      setUploading(false);
    }
  }

  async function apply() {
    try {
      const list = clipsForPassport(selection);
      await patchAsset({ clips: [...(asset?.clips || []).filter((c) => !String(c.source || '').startsWith('library:')), ...list] });
      const params = buildParams(defs, {}, { [clipsName]: retargetClips(selection, clips) });
      const plan = planSteps(asset, 'motion', [{ step: 'retarget', params }]);
      await runPipeline(plan.steps, { from: plan.from });
      notify(t('wizard.motion.retargetStarted', { n: list.length }) + replayNote(plan, registry));
    } catch (e) {
      notify(e.message, true);
    }
  }

  return (
    <>
      <Section title={t('wizard.motion.libraryTitle')} aside={<button type="button" className="icon-btn" title={t('wizard.motion.refresh')} aria-label={t('wizard.motion.refresh')} disabled={offline} onClick={load}><RefreshCw size={14} /></button>}>
        {offline && <Notice>{t('wizard.motion.libraryOffline')}</Notice>}
        {loadError && <Notice tone="warn">{loadError}</Notice>}
        <Chips label={t('wizard.motion.categories')} options={chips} value={category} onChange={setCategory} />
        <label className="forge-search">
          <Search size={13} />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('wizard.motion.searchPlaceholder')} aria-label={t('wizard.motion.searchAria')} />
        </label>
        <div className="forge-clips">
          {index === null && !offline && <p className="field-note"><Loader2 size={12} className="spin" /> {t('wizard.motion.loading')}</p>}
          {index && !clips.length && <p className="field-note">{t('wizard.motion.empty')}</p>}
          {clips.length > 0 && !shown.length && <p className="field-note">{t('wizard.motion.notFound')}</p>}
          {shown.map((c) => {
            const sel = selection[c.id];
            const seconds = c.frames && c.fps ? t('wizard.motion.seconds', { n: (c.frames / c.fps).toFixed(1) }) : '';
            return (
              <div key={c.id} className={'forge-clip ' + (sel ? 'selected' : '')}>
                <label>
                  <input type="checkbox" checked={!!sel} onChange={() => toggle(c)} />
                  <span>
                    {c.name || c.id}
                    <small>
                      {categoryLabel(c.category || 'other')}
                      {seconds ? ' · ' + seconds : ''}
                      {c.root_motion ? ' · ' + t('wizard.motion.rootMotion') : ''}
                    </small>
                  </span>
                </label>
                <button type="button" className={'icon-btn ' + (preview === c.id ? 'active' : '')} title={t('wizard.motion.previewTitle')} aria-label={t('wizard.motion.previewAria', { name: c.name || c.id })} onClick={() => setPreview(preview === c.id ? null : c.id)}>
                  <Play size={13} />
                </button>
                {sel && (
                  <div className="forge-clip-opts">
                    <label>
                      <input type="checkbox" checked={!!sel.loop} onChange={(e) => setOpt(c.id, 'loop', e.target.checked)} />
                      {t('wizard.motion.loop')}
                    </label>
                    <label>
                      <input type="checkbox" checked={sel.in_place !== false} onChange={(e) => setOpt(c.id, 'in_place', e.target.checked)} />
                      {t('wizard.motion.inPlace')}
                    </label>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {previewClip && <StickFigure clipId={previewClip.id} title={previewClip.name || previewClip.id} />}
      </Section>

      <Section title={t('wizard.motion.addTitle')}>
        <Dropzone accept=".fbx" multiple disabled={offline || uploading} title={uploading ? t('wizard.motion.uploading') : t('wizard.motion.dropTitle')} hint={t('wizard.motion.dropHint')} onFiles={onUpload} />
        <MixamoHelp />
      </Section>

      <div className="generate-area">
        <button type="button" className="primary generate" disabled={offline || !selectedCount || !!running} onClick={apply}>
          {running ? <Loader2 size={16} className="spin" /> : <Check size={16} />}
          {running ? t('wizard.motion.retargeting') : t('wizard.motion.apply') + (selectedCount ? ' · ' + selectedCount : '')}
        </button>
        <p>{t('wizard.motion.loopNote')}</p>
      </div>

      <Section title={t('wizard.motion.result')}>
        <StepResult steps={['retarget']} />
      </Section>
    </>
  );
}

function CreatureClips() {
  const t = useT();
  const { offline, registry, asset, notify, patchAsset, runPipeline, activeJobFor } = useForge();
  const defs = paramDefs(registry, 'anim_creature');
  const clipsName = resolveParamName(defs, 'clips');
  const options = creatureClipOptions(defs);
  const template = latestPipelineEntry(asset, ['rig_creature'])?.params?.template || asset?.profile?.template || 'auto';
  const [chosen, setChosen] = useState(() => {
    const fromPassport = creatureNamesFromPassport(asset?.clips, options);
    if (fromPassport.length) return fromPassport;
    const def = defs[clipsName]?.default;
    return Array.isArray(def) && def.length ? def : options.filter((o) => !['fly', 'glide'].includes(o.base)).map((o) => o.id);
  });
  const running = activeJobFor(['anim_creature']);
  const names = chosen.filter((id) => options.some((o) => o.id === id) && creatureClipAvailable(id, template));

  async function apply() {
    try {
      await patchAsset({ clips: [...(asset?.clips || []).filter((c) => !String(c.source || '').startsWith('procedural:')), ...creatureClipsForPassport(names)] });
      const plan = planSteps(asset, 'motion', [{ step: 'anim_creature', params: buildParams(defs, {}, { [clipsName]: names }) }]);
      await runPipeline(plan.steps, { from: plan.from });
      notify(t('wizard.motion.proceduralStarted') + replayNote(plan, registry));
    } catch (e) {
      notify(e.message, true);
    }
  }

  return (
    <>
      <Section title={t('wizard.motion.proceduralTitle')}>
        <div className="forge-checkgrid">
          {options.map((o) => {
            const ok = creatureClipAvailable(o.id, template);
            return (
              <label key={o.id} className={'forge-check ' + (ok ? '' : 'off')} title={ok ? (o.loop ? t('wizard.motion.loopClip') : '') : t('wizard.motion.needWings')}>
                <input
                  type="checkbox"
                  disabled={!ok}
                  checked={ok && chosen.includes(o.id)}
                  onChange={(e) => setChosen((list) => (e.target.checked ? [...new Set([...list, o.id])] : list.filter((x) => x !== o.id)))}
                />
                <span>{o.title}{o.loop ? ' ↻' : ''}</span>
              </label>
            );
          })}
        </div>
        <p className="field-note">{t('wizard.motion.proceduralNote')}</p>
      </Section>
      <div className="generate-area">
        <button type="button" className="primary generate" disabled={offline || !names.length || !!running} onClick={apply}>
          {running ? <Loader2 size={16} className="spin" /> : <Footprints size={16} />}
          {running ? t('wizard.motion.animating') : t('wizard.motion.createMotions', { n: names.length })}
        </button>
      </div>
      <Section title={t('wizard.motion.result')}>
        <StepResult steps={['anim_creature']} />
      </Section>
    </>
  );
}
