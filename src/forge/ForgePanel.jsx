// Forge: step wizard over the Blender pipeline (docs/forge/API.md, docs/forge/ui.md).
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Anvil, ListChecks, Boxes, ClipboardCheck, RefreshCw, ChevronLeft, ChevronRight, Plus, Check, X, Loader2 } from 'lucide-react';
import * as forgeApi from './forge-api.js';
import {
  wizardStepsFor, wizardProgress, kindInfo, kindFromAsset, sourceFromAsset, latestPipelineEntry,
  isActiveStatus, jobError, stepTitle, jobProgress, mergeJobRecord, pollPlan,
} from './forge-core.js';
import { ForgeContext, Notice, ProgressBar } from './ForgeUi.jsx';
import StepWhat from './StepWhat.jsx';
import StepPrepare from './StepPrepare.jsx';
import StepSkeleton from './StepSkeleton.jsx';
import StepMotion from './StepMotion.jsx';
import StepItems from './StepItems.jsx';
import StepFx from './StepFx.jsx';
import StepExport from './StepExport.jsx';
import ForgeJobs from './ForgeJobs.jsx';
import ForgeBatch from './ForgeBatch.jsx';
import ForgeReview from './ForgeReview.jsx';
import { t, useT } from '../i18n/index.js';
import './forge.css';

// App.jsx renders this panel through ForgeTab.jsx (lazy chunk); the tab icon lives there.

const STORE_KEY = 'astraforge.forge.v1';
function readStore() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {};
  } catch {
    return {};
  }
}
function writeStore(value) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(value));
  } catch {
    /* per-viewer convenience only */
  }
}

// Subtitle of each wizard step (dictionary keys forge.panel.hint.<id>).
const STEP_HINTS = ['what', 'prepare', 'generator', 'skeleton', 'motion', 'items', 'fx', 'export'];
const stepHint = (id) => (STEP_HINTS.includes(id) ? t('forge.panel.hint.' + id) : '');

export default function ForgePanel({ studio, snapshot, notify, onEdit }) {
  const tr = useT();
  const stored = useMemo(readStore, []);
  const [view, setView] = useState('wizard');
  const [server, setServer] = useState({ state: 'checking', status: null, message: '' });
  const [registry, setRegistry] = useState([]);
  const [presets, setPresets] = useState([]);
  const [kind, setKind] = useState(stored.kind || 'humanoid');
  const [source, setSource] = useState(stored.source || null);
  const [assetId, setAssetId] = useState(stored.assetId || null);
  const [asset, setAsset] = useState(null);
  const [stepId, setStepId] = useState(stored.stepId || 'what');
  const [jobs, setJobs] = useState({});
  // Name typed on step 1 for generator assets (their passport is created on step 2).
  const [draftName, setDraftName] = useState('');

  // jobsRef is the source of truth for job records: every change goes through applyJobs, so
  // the poller, list refreshes and track() never overwrite each other's updates.
  const jobsRef = useRef(jobs);
  const applyJobs = useCallback((fn) => {
    const next = fn(jobsRef.current);
    if (next !== jobsRef.current) {
      jobsRef.current = next;
      setJobs(next);
    }
    return next;
  }, []);
  const finishRef = useRef(null);
  const assetIdRef = useRef(assetId);
  assetIdRef.current = assetId;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const doneCallbacks = useRef(new Map());
  const registryRef = useRef(registry);
  registryRef.current = registry;

  const offline = server.state !== 'ready';
  const scrollRef = useRef(null);

  useEffect(() => writeStore({ kind, source, assetId, stepId }), [kind, source, assetId, stepId]);

  // ------------------------------------------------------------------ server & registries
  /**
   * Merge job records from GET /jobs (or a full GET /jobs/:id). Step results and log tails
   * already known are kept (list items carry {step,status,progress} only). Jobs fetched one
   * by one by the 1-second poller are left to it; a job that finished here fires finishJob.
   */
  const mergeJobs = useCallback((list) => {
    if (!Array.isArray(list)) return;
    const finished = [];
    applyJobs((prev) => {
      const next = { ...prev };
      const polled = new Set(pollPlan(prev).each);
      for (const j of list) {
        if (!j || !j.id) continue;
        const old = prev[j.id];
        if (old && polled.has(j.id)) continue; // the poller loads its full record
        const merged = mergeJobRecord(old, j);
        next[j.id] = merged;
        if (old && isActiveStatus(old.status) && !isActiveStatus(merged.status)) finished.push(merged);
      }
      return next;
    });
    for (const job of finished) {
      finishRef.current?.(job);
      // list items have no step results / log tails: load the finished job once
      forgeApi.getJob(job.id)
        .then((full) => full && applyJobs((prev) => ({ ...prev, [job.id]: mergeJobRecord(prev[job.id], full) })))
        .catch(() => {});
    }
  }, [applyJobs]);

  const checkServer = useCallback(async () => {
    setServer((s) => ({ ...s, state: s.state === 'ready' ? 'ready' : 'checking' }));
    try {
      const status = await forgeApi.getStatus();
      setServer({ state: 'ready', status, message: '' });
      const [steps, pre, recent] = await Promise.allSettled([forgeApi.getSteps(), forgeApi.getPresets(), forgeApi.listJobs()]);
      if (steps.status === 'fulfilled') setRegistry(Array.isArray(steps.value) ? steps.value : steps.value?.steps || []);
      if (pre.status === 'fulfilled') setPresets(Array.isArray(pre.value) ? pre.value : pre.value?.presets || []);
      if (recent.status === 'fulfilled') mergeJobs(Array.isArray(recent.value) ? recent.value : recent.value?.jobs || []);
    } catch (e) {
      setServer({ state: e.notReady ? 'missing' : 'error', status: null, message: e.message });
    }
  }, [mergeJobs]);

  useEffect(() => {
    checkServer();
  }, [checkServer]);

  // Calm re-check while the server module is missing or unreachable.
  useEffect(() => {
    if (server.state === 'ready' || server.state === 'checking') return undefined;
    const timer = setInterval(checkServer, 20000);
    return () => clearInterval(timer);
  }, [server.state, checkServer]);

  // ------------------------------------------------------------------ passport
  const refreshAsset = useCallback(async (id = assetIdRef.current) => {
    if (!id) {
      setAsset(null);
      return null;
    }
    try {
      const a = await forgeApi.getAsset(id);
      if (assetIdRef.current === id) setAsset(a);
      return a;
    } catch (e) {
      if (e.code === 'not_found' || e.status === 404) {
        if (assetIdRef.current === id) {
          setAssetId(null);
          setAsset(null);
          setStepId('what');
        }
        notifyRef.current(t('forge.panel.assetNotFound'), true);
      } else if (!e.notReady) {
        notifyRef.current(e.message, true);
      }
      return null;
    }
  }, []);

  useEffect(() => {
    if (server.state === 'ready') refreshAsset(assetId);
  }, [assetId, server.state, refreshAsset]);

  const openAsset = useCallback(async (brief) => {
    const full = brief?.pipeline ? brief : await forgeApi.getAsset(brief.id);
    assetIdRef.current = full.id;
    setAssetId(full.id);
    setAsset(full);
    const k = kindFromAsset(full);
    if (k) setKind(k);
    setSource(sourceFromAsset(full));
    setStepId('prepare');
    setView('wizard');
    return full;
  }, []);

  const newAsset = useCallback(() => {
    assetIdRef.current = null;
    setAssetId(null);
    setAsset(null);
    setSource(null);
    setStepId('what');
  }, []);

  const createPassport = useCallback(async ({ name, kind: apiKind, source: src }) => {
    const a = await forgeApi.createAsset({ name, kind: apiKind, source: src });
    assetIdRef.current = a.id;
    setAssetId(a.id);
    setAsset(a);
    return a;
  }, []);

  const adoptPassport = useCallback((a) => {
    assetIdRef.current = a.id;
    setAssetId(a.id);
    setAsset(a);
  }, []);

  /** DELETE /assets/:id moves the passport to data/library/trash (the server refuses while a job runs). */
  const trashAsset = useCallback(async (id) => {
    const r = await forgeApi.deleteAsset(id);
    if (id === assetIdRef.current) {
      assetIdRef.current = null;
      setAssetId(null);
      setAsset(null);
      setStepId('what');
    }
    return r;
  }, []);

  const patchAsset = useCallback(async (patch) => {
    const id = assetIdRef.current;
    if (!id) throw new Error(t('forge.panel.noPassport'));
    const updated = await forgeApi.patchAsset(id, patch);
    if (updated && updated.id) setAsset(updated);
    else setAsset((a) => (a ? { ...a, ...patch } : a));
    return updated;
  }, []);

  // ------------------------------------------------------------------ jobs
  const track = useCallback((jobId, meta = {}, onDone) => {
    if (!jobId) return;
    if (onDone) doneCallbacks.current.set(jobId, onDone);
    applyJobs((prev) => ({
      ...prev,
      [jobId]: {
        id: jobId,
        status: 'queued',
        createdAt: new Date().toISOString(),
        steps: (meta.steps || []).map((s) => ({ step: s.step || s, status: 'queued' })),
        ...prev[jobId],
        assetId: prev[jobId]?.assetId || meta.assetId,
        _meta: { ...(prev[jobId]?._meta || {}), ...meta },
      },
    }));
  }, [applyJobs]);

  // A job can be seen finishing by the poller and by a list refresh at the same moment.
  const finishedIds = useRef(new Set());
  const finishJob = useCallback((job) => {
    if (!job?.id || finishedIds.current.has(job.id)) return;
    finishedIds.current.add(job.id);
    const cb = doneCallbacks.current.get(job.id);
    if (cb) {
      doneCallbacks.current.delete(job.id);
      try {
        cb(job);
      } catch {
        /* callback errors must not stop polling */
      }
    }
    if (job.assetId && job.assetId === assetIdRef.current) refreshAsset(job.assetId);
    const titles = (job.steps || []).map((s) => stepTitle(s.step, registryRef.current)).join(', ');
    // Batch items report in their own grid; only failures pop up.
    const quiet = !!(job.batchId || job._meta?.batchId);
    if (job.status === 'completed' && !quiet) notifyRef.current(titles ? t('forge.panel.doneWith', { steps: titles }) : t('forge.panel.done'));
    else if (job.status === 'failed') notifyRef.current(jobError(job) || t('forge.panel.stepFailed'), true);
  }, [refreshAsset]);
  finishRef.current = finishJob;

  // Progress (API.md: "poll GET /jobs/:jobId once a second"): running jobs and the ones started
  // from this view are fetched one by one every second; queued jobs merged from GET /jobs (e.g.
  // after a reload during a big batch) are refreshed with one GET /jobs every 3 seconds.
  useEffect(() => {
    let stopped = false;
    let busy = false;
    let lastList = 0;
    const pollOne = async (id) => {
      const j = jobsRef.current[id];
      if (!j) return;
      try {
        const fresh = await forgeApi.getJob(id);
        if (stopped || !fresh) return;
        const next = applyJobs((prev) => ({ ...prev, [id]: mergeJobRecord(prev[id], fresh) }));
        if (!isActiveStatus(fresh.status)) finishJob(next[id]);
      } catch (e) {
        if (stopped) return;
        if (e.code === 'not_found' || e.status === 404 || e.notReady) {
          const lost = { ...j, status: 'failed', error: e.notReady ? e.message : t('forge.panel.jobLost') };
          applyJobs((prev) => ({ ...prev, [id]: lost }));
          finishJob(lost);
        }
      }
    };
    const tick = async () => {
      if (busy) return;
      const plan = pollPlan(jobsRef.current);
      if (!plan.each.length && !plan.list) return;
      busy = true;
      try {
        await Promise.all(plan.each.map(pollOne));
        if (plan.list && Date.now() - lastList >= 3000) {
          lastList = Date.now();
          const list = await forgeApi.listJobs().catch(() => null);
          if (!stopped && list) mergeJobs(Array.isArray(list) ? list : list.jobs || []);
        }
      } finally {
        busy = false;
      }
    };
    const timer = setInterval(tick, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [finishJob, applyJobs, mergeJobs]);

  const cancel = useCallback(async (jobId) => {
    try {
      await forgeApi.cancelJob(jobId);
      notifyRef.current(t('forge.panel.cancelSent'));
    } catch (e) {
      notifyRef.current(e.message, true);
    }
  }, []);

  /** POST /assets/:id/run; steps and `from` come from planSteps() of the wizard step. */
  const runPipeline = useCallback(async (steps, { id, meta, onDone, from } = {}) => {
    const target = id || assetIdRef.current;
    if (!target) throw new Error(t('forge.panel.noPassport'));
    const r = await forgeApi.runSteps(target, steps, from);
    track(r.jobId, { assetId: target, steps, version: r.version, warnings: r.warnings || [], ...meta }, onDone);
    if (r.warnings?.length) notifyRef.current(t('forge.panel.startedWarn', { warning: r.warnings[0] }));
    return r;
  }, [track]);

  const sortedJobs = useMemo(
    () => Object.values(jobs).sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || ''))),
    [jobs],
  );

  const activeJobFor = useCallback((steps) => {
    const wanted = Array.isArray(steps) ? steps : [steps];
    return sortedJobs.find((j) => j.assetId === assetId && isActiveStatus(j.status) && (j.steps || []).some((s) => wanted.includes(s.step))) || null;
  }, [sortedJobs, assetId]);

  /**
   * Freshest result of one of the steps: passport pipeline or a tracked job of this asset.
   * While the step is being re-run, the previous result stays visible (marked stale).
   */
  const resultCache = useRef(new Map());
  const lastResult = useCallback((steps) => {
    const wanted = Array.isArray(steps) ? steps : [steps];
    const key = assetId + ':' + wanted.join(',');
    const res = freshResult(wanted);
    if (res?.result) {
      resultCache.current.set(key, res);
      return res;
    }
    const cached = resultCache.current.get(key);
    if (cached && activeJobFor(wanted)) return { ...cached, stale: true };
    return res;
  }, [asset, sortedJobs, assetId, activeJobFor]); // eslint-disable-line react-hooks/exhaustive-deps

  function freshResult(wanted) {
    const entry = latestPipelineEntry(asset, wanted);
    let fromJob = null;
    for (const j of sortedJobs) {
      if (j.assetId !== assetId) continue;
      const s = (j.steps || []).filter((x) => wanted.includes(x.step) && x.result).pop();
      if (s) {
        fromJob = { step: s.step, status: s.status, result: s.result, version: j.version, at: s.finishedAt || j.createdAt, params: s.params };
        break;
      }
    }
    if (entry?.result) {
      if (fromJob?.at && entry.at && new Date(fromJob.at) > new Date(entry.at)) return fromJob;
      return { step: entry.step, status: entry.status, result: entry.result, version: entry.version, at: entry.at, params: entry.params };
    }
    if (fromJob) return fromJob;
    return entry ? { step: entry.step, status: entry.status, result: null, version: entry.version, at: entry.at, params: entry.params } : null;
  }

  const loadGlb = useCallback(async (url, entry) => {
    try {
      if (!studio?.current) throw new Error(t('forge.panel.noViewer'));
      const label = (entry?.name || asset?.name || t('forge.panel.forge')) + (entry?.version ? ' · ' + entry.version : '');
      await studio.current.importAsset({ url, extension: '.glb', name: label });
      onEdit?.();
      notifyRef.current(t('forge.panel.addedToScene'));
    } catch (e) {
      notifyRef.current(e.message, true);
    }
  }, [studio, asset, onEdit]);

  // ------------------------------------------------------------------ wizard navigation
  const steps = wizardStepsFor(kind, source);
  const progress = wizardProgress(asset);
  const index = Math.max(0, steps.findIndex((s) => s.id === stepId));
  const current = steps[index] || steps[0];
  const canEnter = (id) => id === 'what' || !!assetId || (id === 'prepare' && source === 'params');

  useEffect(() => {
    if (!steps.some((s) => s.id === stepId)) setStepId('what');
  }, [kind, source]); // eslint-disable-line react-hooks/exhaustive-deps

  // Each wizard step opens at its top.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [stepId, view]);

  const go = (delta) => {
    const next = steps[index + delta];
    if (next && canEnter(next.id)) setStepId(next.id);
  };

  const activeCount = sortedJobs.filter((j) => isActiveStatus(j.status)).length;
  const latestActive = sortedJobs.find((j) => isActiveStatus(j.status));

  const ctx = {
    offline, server, registry, presets, kind, setKind, source, setSource, asset, assetId, notify,
    studio, snapshot, jobs, sortedJobs, track, mergeJobs, cancel, runPipeline, patchAsset, createPassport,
    adoptPassport, openAsset, newAsset, refreshAsset, lastResult, activeJobFor, loadGlb, trashAsset,
    setStepId, setView, draftName, setDraftName,
  };

  const hint = current.id === 'prepare' && source === 'params' ? stepHint('generator') : stepHint(current.id);

  return (
    <ForgeContext.Provider value={ctx}>
      <div className="forge-panel">
        <div className="forge-views segmented" role="tablist" aria-label={tr('forge.panel.viewsAria')}>
          <button role="tab" aria-selected={view === 'wizard'} className={view === 'wizard' ? 'active' : ''} onClick={() => setView('wizard')}>
            <Anvil size={13} />
            {tr('forge.panel.view.wizard')}
          </button>
          <button role="tab" aria-selected={view === 'jobs'} className={view === 'jobs' ? 'active' : ''} onClick={() => setView('jobs')}>
            <ListChecks size={13} />
            {tr('forge.panel.view.jobs')}
            {activeCount > 0 && <span className="forge-badge">{activeCount}</span>}
          </button>
          <button role="tab" aria-selected={view === 'batch'} className={view === 'batch' ? 'active' : ''} onClick={() => setView('batch')}>
            <Boxes size={13} />
            {tr('forge.panel.view.batch')}
          </button>
          <button role="tab" aria-selected={view === 'review'} className={view === 'review' ? 'active' : ''} onClick={() => setView('review')}>
            <ClipboardCheck size={13} />
            {tr('forge.panel.view.review')}
          </button>
        </div>

        {view === 'wizard' && (
          <>
            <div className="forge-railbar">
              <ol className="forge-rail" aria-label={tr('forge.panel.railAria')}>
                {steps.map((s, i) => {
                  const state = progress[s.id];
                  const cls = [s.id === current.id ? 'current' : '', state === 'done' ? 'done' : '', state === 'failed' ? 'failed' : ''].join(' ');
                  return (
                    <li key={s.id} className={cls}>
                      <button type="button" disabled={!canEnter(s.id)} onClick={() => setStepId(s.id)} title={s.title} aria-label={tr('forge.panel.stepAria', { n: i + 1, title: s.title })} aria-current={s.id === current.id ? 'step' : undefined}>
                        {state === 'done' && s.id !== current.id ? <Check size={11} /> : state === 'failed' ? <X size={11} /> : i + 1}
                      </button>
                      <span>{s.title}</span>
                    </li>
                  );
                })}
              </ol>
            </div>
            {asset && <AssetBar asset={asset} kind={kind} onNew={newAsset} onRename={(name) => patchAsset({ name }).catch((e) => notify(e.message, true))} />}
            <div className="panel-scroll forge-scroll" ref={scrollRef}>
              <ServerStrip server={server} onRetry={checkServer} />
              <div className="forge-head">
                <span className="step-label">
                  {tr('forge.panel.stepLabel', { n: index + 1, total: steps.length })}
                </span>
                <h1>{current.title}</h1>
                <p>{hint}</p>
              </div>
              {current.id === 'what' && <StepWhat onCreated={(a) => { if (a) adoptPassport(a); setStepId('prepare'); }} />}
              {current.id === 'prepare' && <StepPrepare />}
              {current.id === 'skeleton' && <StepSkeleton />}
              {current.id === 'motion' && <StepMotion />}
              {current.id === 'items' && <StepItems />}
              {current.id === 'fx' && <StepFx />}
              {current.id === 'export' && <StepExport />}
            </div>
            <div className="forge-footer">
              <button type="button" className="secondary small" disabled={index === 0} onClick={() => go(-1)}>
                <ChevronLeft size={14} />
                {tr('forge.panel.back')}
              </button>
              {latestActive ? (
                <button type="button" className="forge-dock" onClick={() => setView('jobs')} title={tr('forge.panel.openJobs')}>
                  <Loader2 size={13} className="spin" />
                  <span>{stepTitle((latestActive.steps || []).find((s) => s.status === 'running')?.step || latestActive.steps?.[0]?.step || '', registry) || tr('forge.panel.job')}</span>
                  <ProgressBar value={jobProgress(latestActive)} />
                </button>
              ) : (
                <span className="forge-footer-gap" />
              )}
              <button type="button" className="secondary small" disabled={index >= steps.length - 1 || !canEnter(steps[index + 1]?.id)} onClick={() => go(1)}>
                {tr('forge.panel.next')}
                <ChevronRight size={14} />
              </button>
            </div>
          </>
        )}
        {view === 'jobs' && (
          <div className="panel-scroll forge-scroll">
            <ServerStrip server={server} onRetry={checkServer} />
            <ForgeJobs />
          </div>
        )}
        {view === 'batch' && (
          <div className="panel-scroll forge-scroll">
            <ServerStrip server={server} onRetry={checkServer} />
            <ForgeBatch />
          </div>
        )}
        {view === 'review' && <ForgeReview />}
      </div>
    </ForgeContext.Provider>
  );
}

function ServerStrip({ server, onRetry }) {
  const t = useT();
  if (server.state === 'checking') {
    return (
      <div className="forge-server">
        <Loader2 size={12} className="spin" />
        {t('forge.panel.checking')}
      </div>
    );
  }
  if (server.state !== 'ready') {
    return (
      <div className="forge-server-wrap">
        <Notice tone={server.state === 'missing' ? 'info' : 'warn'}>
          <p>{server.message}</p>
          <button type="button" className="forge-link" onClick={onRetry}>
            <RefreshCw size={12} />
            {t('forge.panel.retry')}
          </button>
        </Notice>
      </div>
    );
  }
  const s = server.status || {};
  const item = (label, ok, extra) => (
    <span className={ok ? 'ok' : 'bad'} title={extra || ''}>
      <span className={'status-dot ' + (ok ? 'online' : '')} />
      {label}
    </span>
  );
  return (
    <div className="forge-server">
      {item('Blender' + (s.blender?.version ? ' ' + s.blender.version : ''), !!s.blender?.ok, s.blender?.path)}
      {item('Godot' + (s.godot?.version ? ' ' + s.godot.version : ''), !!s.godot?.ok, s.godot?.path)}
      {typeof s.library?.clips === 'number' && <span>{t('forge.panel.clips', { n: s.library.clips })}</span>}
      {s.queue && <span>{t('forge.panel.queue', { running: s.queue.running || 0, queued: s.queue.queued || 0 })}</span>}
      {s.gpuLock?.held && <span className="busy" title={s.gpuLock.name || ''}>{t('forge.panel.gpuBusy')}</span>}
    </div>
  );
}

function AssetBar({ asset, kind, onNew, onRename }) {
  const t = useT();
  const [name, setName] = useState(asset.name || '');
  useEffect(() => setName(asset.name || ''), [asset.id, asset.name]);
  const version = (asset.pipeline || []).map((p) => p.version).filter(Boolean).pop();
  return (
    <div className="forge-assetbar">
      <input
        aria-label={t('forge.panel.assetName')}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={() => {
          const v = name.trim();
          if (v && v !== asset.name) onRename(v);
          else setName(asset.name || '');
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') e.currentTarget.blur();
        }}
      />
      <small>
        {kindInfo(kind)?.title || asset.kind}
        {version ? ' · ' + version : ''}
      </small>
      <button type="button" className="forge-link" onClick={onNew} title={t('forge.panel.newAssetTitle')}>
        <Plus size={12} />
        {t('forge.panel.newAsset')}
      </button>
    </div>
  );
}
