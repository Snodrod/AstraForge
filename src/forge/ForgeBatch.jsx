// Batch: many assets from one preset (seeds 1..N, a folder of GLB, generation results).
import React, { useEffect, useRef, useState } from 'react';
import { Boxes, Loader2, FolderOpen, FilePlus2, Trash2 } from 'lucide-react';
import {
  KINDS, kindInfo, filterPresets, seedBatchItems, sourceBatchItems, defaultBatchSteps, batchCounts, batchItemsFrom,
  onlyGlb, paramDefs, buildParams, generatorStep, stepTitle, formatDate, fileBatchChunks,
} from './forge-core.js';
import { createBatch, createBatchWithFiles, getBatch, listGenerationJobs } from './forge-api.js';
import { Section, Notice, MoreParams, ProgressBar, StatusPill, NumberInput, useForge } from './ForgeUi.jsx';
import { useT } from '../i18n/index.js';

const BATCH_KEY = 'astraforge.forge.batches.v1';
const MODES = [
  ['seeds', 'forge.batch.mode.seeds'],
  ['files', 'forge.batch.mode.files'],
  ['jobs', 'forge.batch.mode.jobs'],
];
const GENERATOR_KINDS = ['ship', 'turret', 'item'];
const SEED_RANGE = { type: 'integer', min: 0, max: 2147483647, default: 1 };
const MESH_KINDS = ['humanoid', 'creature', 'item', 'ship'];

function loadSaved() {
  try {
    const v = JSON.parse(localStorage.getItem(BATCH_KEY) || '[]');
    return Array.isArray(v) ? v.slice(0, 20) : [];
  } catch {
    return [];
  }
}
function saveBatches(list) {
  try {
    localStorage.setItem(BATCH_KEY, JSON.stringify(list.slice(0, 20).map(({ id, kind, createdAt, title, items }) => ({ id, kind, createdAt, title, items: (items || []).map(({ assetId, jobId, name }) => ({ assetId, jobId, name })) }))));
  } catch {
    /* per-viewer convenience only */
  }
}

export default function ForgeBatch() {
  const t = useT();
  const { offline, registry, presets, notify } = useForge();
  const [mode, setMode] = useState('seeds');
  const [kind, setKind] = useState('ship');
  const [preset, setPreset] = useState('');
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(20);
  const [prefix, setPrefix] = useState('');
  const [base, setBase] = useState({});
  const [files, setFiles] = useState([]);
  const [genJobs, setGenJobs] = useState(null);
  const [pickedJobs, setPickedJobs] = useState([]);
  const [busy, setBusy] = useState('');
  const [batches, setBatches] = useState(loadSaved);
  const filesInput = useRef(null);
  const folderInput = useRef(null);

  const kinds = mode === 'seeds' ? GENERATOR_KINDS : MESH_KINDS;
  useEffect(() => {
    if (!kinds.includes(kind)) setKind(kinds[0]);
    setPreset('');
  }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (mode !== 'jobs' || genJobs !== null) return;
    listGenerationJobs().then(setGenJobs).catch(() => setGenJobs([]));
  }, [mode, genJobs]);

  useEffect(() => saveBatches(batches), [batches]);

  // Batch summaries every 2 s while something is still running.
  useEffect(() => {
    if (offline) return undefined;
    let stopped = false;
    const tick = async () => {
      const open = batches.filter((b) => !b.done);
      for (const b of open) {
        try {
          const summary = await getBatch(b.id);
          if (stopped) return;
          setBatches((list) => list.map((x) => {
            if (x.id !== b.id) return x;
            const items = batchItemsFrom(summary, x.items);
            const counts = batchCounts(items);
            return { ...x, items, done: counts.total > 0 && counts.queued + counts.running === 0 };
          }));
        } catch (e) {
          if (e.code === 'not_found' || e.status === 404) setBatches((list) => list.map((x) => (x.id === b.id ? { ...x, done: true, lost: true } : x)));
        }
      }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [offline, batches.map((b) => b.id + (b.done ? '1' : '0')).join(',')]); // eslint-disable-line react-hooks/exhaustive-deps

  const kindPresets = filterPresets(presets, kind);
  const genStep = generatorStep(kind);
  const genDefs = genStep ? paramDefs(registry, genStep) : {};
  const count = mode === 'seeds' ? Math.abs(Math.round(to) - Math.round(from)) + 1 : mode === 'files' ? files.length : pickedJobs.length;

  async function submit() {
    const info = kindInfo(kind);
    const namePrefix = prefix.trim() || info.defaultName;
    try {
      let items;
      if (mode === 'seeds') {
        if (count > 200) throw new Error(t('forge.batch.tooMany', { max: 200 }));
        const params = buildParams(genDefs, base);
        delete params.seed;
        items = seedBatchItems({ kind, from, to, namePrefix, params, preset: preset || undefined });
      } else if (mode === 'files') {
        if (!files.length) throw new Error(t('forge.batch.pickFiles'));
        // Files go straight into the forge (multipart POST /batch, up to 300 MB each), in parts
        // of 20 files; each part becomes its own batch card.
        const chunks = fileBatchChunks(files, { kind, preset: preset || undefined, steps: defaultBatchSteps(kind, 'upload') });
        const presetTitle = preset ? ' · ' + (kindPresets.find((p) => p.id === preset)?.title || preset) : '';
        let total = 0;
        for (let c = 0; c < chunks.length; c++) {
          const { files: part, items: partItems } = chunks[c];
          setBusy(chunks.length > 1 ? t('forge.batch.uploadingPart', { part: c + 1, total: chunks.length }) : t('forge.batch.uploading', { n: part.length }));
          const r = await createBatchWithFiles(partItems, part);
          const known = (r.items || []).map((it, i) => ({ ...it, name: partItems[i]?.name }));
          const title = t('forge.batch.cardTitle', { kind: info.title, n: partItems.length }) + (chunks.length > 1 ? ' ' + t('forge.batch.cardPart', { part: c + 1, total: chunks.length }) : '') + presetTitle;
          setBatches((list) => [{ id: r.batchId, kind, title, createdAt: new Date().toISOString(), items: known }, ...list]);
          total += partItems.length;
        }
        notify(t('forge.batch.queued', { n: total }));
        setFiles([]);
        return;
      } else {
        if (!pickedJobs.length) throw new Error(t('forge.batch.pickJobs'));
        const byId = new Map((genJobs || []).map((j) => [j.id, j]));
        const sources = pickedJobs.map((id, i) => ({ name: (byId.get(id)?.prompt || `${namePrefix} ${i + 1}`).slice(0, 60), source: { type: 'job', jobId: id } }));
        items = sourceBatchItems({ kind, sources, preset: preset || undefined, steps: defaultBatchSteps(kind, 'job'), namePrefix });
      }
      setBusy(t('forge.batch.sending'));
      const r = await createBatch(items);
      const known = (r.items || []).map((it, i) => ({ ...it, name: items[i]?.name }));
      const title = t('forge.batch.cardTitle', { kind: info.title, n: items.length }) + (preset ? ' · ' + (kindPresets.find((p) => p.id === preset)?.title || preset) : '');
      setBatches((list) => [{ id: r.batchId, kind, title, createdAt: new Date().toISOString(), items: known }, ...list]);
      notify(t('forge.batch.queued', { n: items.length }));
      setFiles([]);
      setPickedJobs([]);
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy('');
    }
  }

  return (
    <>
      <Section title={t('forge.batch.newTitle')}>
        <div className="segmented" role="radiogroup" aria-label={t('forge.batch.sourceAria')}>
          {MODES.map(([id, title]) => (
            <button key={id} type="button" role="radio" aria-checked={mode === id} className={mode === id ? 'active' : ''} onClick={() => setMode(id)}>
              {t(title)}
            </button>
          ))}
        </div>

        <label className="forge-field">
          <span>{t('forge.batch.what')}</span>
          <select value={kind} onChange={(e) => { setKind(e.target.value); setPreset(''); }}>
            {KINDS.filter((k) => kinds.includes(k.id)).map((k) => (
              <option key={k.id} value={k.id}>{k.title}</option>
            ))}
          </select>
        </label>

        <label className="forge-field">
          <span>{t('forge.batch.preset')}</span>
          <select value={preset} onChange={(e) => setPreset(e.target.value)}>
            <option value="">{mode === 'seeds' ? t('forge.batch.generatorOnly', { step: stepTitle(genStep || '', registry) }) : t('forge.batch.baseSteps', { steps: defaultBatchSteps(kind, 'asset').map((s) => stepTitle(s, registry)).join(' → ') })}</option>
            {kindPresets.map((p) => (
              <option key={p.id} value={p.id}>{p.title || p.id}</option>
            ))}
          </select>
        </label>

        {mode === 'seeds' && (
          <>
            <div className="forge-two">
              <label className="forge-field">
                <span>{t('forge.batch.seedFrom')}</span>
                <NumberInput def={SEED_RANGE} integer value={from} onChange={(v) => setFrom(Number(v) || 0)} />
              </label>
              <label className="forge-field">
                <span>{t('forge.batch.seedTo')}</span>
                <NumberInput def={SEED_RANGE} integer value={to} onChange={(v) => setTo(Number(v) || 0)} />
              </label>
            </div>
            <MoreParams step={genStep} defs={genDefs} values={base} onChange={setBase} skip={['seed']} />
          </>
        )}

        {mode === 'files' && (
          <>
            <div className="forge-row">
              <button type="button" className="secondary small" onClick={() => filesInput.current?.click()}>
                <FilePlus2 size={13} />
                {t('forge.batch.mode.files')}
              </button>
              <button type="button" className="secondary small" onClick={() => folderInput.current?.click()}>
                <FolderOpen size={13} />
                {t('forge.batch.folder')}
              </button>
            </div>
            <input ref={filesInput} type="file" hidden multiple accept=".glb" onChange={(e) => { setFiles(onlyGlb(e.target.files)); e.target.value = ''; }} />
            <input ref={folderInput} type="file" hidden webkitdirectory="" directory="" multiple onChange={(e) => { setFiles(onlyGlb(e.target.files)); e.target.value = ''; }} />
            {files.length > 0 && (
              <p className="field-note">
                {t('forge.batch.picked', { n: files.length, names: files.slice(0, 4).map((f) => f.name).join(', ') })}
                {files.length > 4 ? '…' : ''}
              </p>
            )}
            <p className="field-note">{t('forge.batch.filesNote')}</p>
          </>
        )}

        {mode === 'jobs' && (
          <div className="forge-list">
            {genJobs === null && <p className="field-note"><Loader2 size={12} className="spin" /> {t('forge.batch.jobsLoading')}</p>}
            {genJobs?.length === 0 && <p className="field-note">{t('forge.batch.jobsEmpty')}</p>}
            {genJobs?.map((j) => (
              <label key={j.id} className={'forge-list-row ' + (pickedJobs.includes(j.id) ? 'active' : '')}>
                <input type="checkbox" checked={pickedJobs.includes(j.id)} onChange={(e) => setPickedJobs((list) => (e.target.checked ? [...list, j.id] : list.filter((x) => x !== j.id)))} />
                <span>
                  {j.prompt?.slice(0, 60) || t('forge.batch.imageModel')}
                  <small>{formatDate(j.createdAt)}</small>
                </span>
              </label>
            ))}
          </div>
        )}

        <label className="forge-field">
          <span>{t('forge.batch.names')}</span>
          <input value={prefix} placeholder={kindInfo(kind)?.defaultName} onChange={(e) => setPrefix(e.target.value)} />
        </label>

        <button type="button" className="primary full rig-gap" disabled={offline || !!busy || !count} onClick={submit}>
          {busy ? <Loader2 size={15} className="spin" /> : <Boxes size={15} />}
          {busy || t('forge.batch.create', { n: count })}
        </button>
        {offline && <Notice>{t('forge.batch.offline')}</Notice>}
      </Section>

      <Section title={t('forge.batch.listTitle')} aside={<span className="counter">{batches.length}</span>}>
        {!batches.length && <p className="field-note">{t('forge.batch.listEmpty')}</p>}
        {batches.map((b) => (
          <BatchCard key={b.id} batch={b} onForget={() => setBatches((list) => list.filter((x) => x.id !== b.id))} />
        ))}
      </Section>
    </>
  );
}

function BatchCard({ batch, onForget }) {
  const t = useT();
  const { openAsset, notify } = useForge();
  const counts = batchCounts(batch.items);
  const pct = counts.total ? Math.round(((counts.done + counts.failed) / counts.total) * 100) : 0;
  return (
    <div className="forge-batch">
      <div className="forge-job-head">
        <strong>{batch.title || t('forge.batch.batch')}</strong>
        <button type="button" className="icon-btn" title={t('forge.batch.forget')} aria-label={t('forge.batch.forgetAria')} onClick={onForget}>
          <Trash2 size={13} />
        </button>
      </div>
      <small className="forge-job-meta">
        {formatDate(batch.createdAt)} · {t('forge.batch.done', { done: counts.done, total: counts.total })}
        {counts.failed ? ' · ' + t('forge.batch.errors', { n: counts.failed }) : ''}
        {batch.lost ? ' · ' + t('forge.batch.lost') : ''}
      </small>
      {!batch.done && <ProgressBar value={pct} />}
      <details className="forge-more">
        <summary>{t('forge.batch.assets', { n: counts.total })}</summary>
        <div className="forge-batch-items">
          {batch.items.map((it, i) => (
            <div key={it.assetId || it.jobId || i}>
              <span>{it.name || it.assetId?.slice(0, 8) || t('forge.batch.asset')}</span>
              <StatusPill status={it.status || 'queued'} />
              {it.assetId && (
                <button type="button" className="forge-link" onClick={() => openAsset({ id: it.assetId }).catch((e) => notify(e.message, true))}>
                  {t('forge.batch.open')}
                </button>
              )}
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

