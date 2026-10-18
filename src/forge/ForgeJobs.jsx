// Jobs: forge jobs with step progress, log tail, plain-language errors and cancel.
import React, { useEffect, useState } from 'react';
import { RefreshCw, CircleStop, Check, X, Loader2, Circle, FolderOpen, Eye } from 'lucide-react';
import {
  isActiveStatus, jobProgress, jobError, stepTitle, statusLabel, tailLines, formatDate, humanizeError, EXPORT_PROFILES,
  findOutput,
} from './forge-core.js';
import { listJobs, listAssets, getJob, outputUrl } from './forge-api.js';
import { Section, Notice, ProgressBar, StatusPill, useForge } from './ForgeUi.jsx';
import { useT } from '../i18n/index.js';

function StepIcon({ status }) {
  if (status === 'completed' || status === 'done') return <Check size={12} className="good" />;
  if (status === 'failed') return <X size={12} className="bad" />;
  if (status === 'running') return <Loader2 size={12} className="spin" />;
  return <Circle size={10} className="muted" />;
}

export function JobCard({ job, assetName }) {
  const t = useT();
  const { registry, cancel, openAsset, notify, assetId, mergeJobs, loadGlb } = useForge();
  const [loadingLog, setLoadingLog] = useState(false);
  const [wantLog, setWantLog] = useState(false);
  const active = isActiveStatus(job.status);
  const steps = job.steps || [];
  const running = steps.find((s) => s.status === 'running');
  const logStep = running || steps.filter((s) => tailLines(s.logTail, 1)).pop();
  const log = logStep ? tailLines(logStep.logTail, 14) : '';
  // Jobs known only from the GET /jobs list (finished before a reload, or batch items) carry no
  // log tails: the full record is loaded when the log is opened.
  const brief = !active && !log && steps.length > 0 && !steps.some((s) => s.result);
  const loadFull = async () => {
    if (loadingLog) return;
    setLoadingLog(true);
    try {
      const full = await getJob(job.id);
      if (full) mergeJobs([full]);
      setWantLog(true);
    } catch (e) {
      notify(e.message, true);
    } finally {
      setLoadingLog(false);
    }
  };
  // Any completed job with a model result can be opened in the 3D view (CLI and batch results too).
  const lastGlbStep = (list) => [...(list || [])].reverse()
    .find((s) => s.result && findOutput(s.result.outputs, { ext: '.glb' }));
  const showInScene = async () => {
    try {
      let step = lastGlbStep(steps);
      if (!step) {
        const full = await getJob(job.id);
        if (full) mergeJobs([full]);
        step = lastGlbStep(full?.steps);
      }
      const url = step ? outputUrl(job.assetId, findOutput(step.result.outputs, { ext: '.glb' })) : null;
      if (!url) {
        notify(t('forge.jobs.noGlb'), true);
        return;
      }
      await loadGlb(url, { version: job.version, name: assetName || job.assetName });
    } catch (e) {
      notify(humanizeError(e.message), true);
    }
  };
  const profile = job.exportProfile || job._meta?.profile;
  const title = job.type === 'export' || job._meta?.export
    ? t('forge.jobs.export') + (profile ? ' · ' + (EXPORT_PROFILES.find((p) => p.id === profile)?.title || profile) : '')
    : job.type === 'clips'
      ? t('forge.jobs.clipImport')
      : steps.map((s) => stepTitle(s.step, registry)).join(' → ') || t('forge.jobs.forgeJob');
  const error = jobError(job);
  return (
    <div className={'forge-job ' + job.status}>
      <div className="forge-job-head">
        <strong>{title}</strong>
        <StatusPill status={job.status} />
      </div>
      <small className="forge-job-meta">
        {assetName || job.assetName || (job.assetId ? t('forge.jobs.assetShort', { id: String(job.assetId).slice(0, 8) }) : t('forge.jobs.clipLibrary'))}
        {job.version ? ' · ' + job.version : ''}
        {job.createdAt ? ' · ' + formatDate(job.createdAt) : ''}
      </small>
      {active && <ProgressBar value={jobProgress(job)} />}
      {steps.length > 1 && (
        <ol className="forge-job-steps">
          {steps.map((s, i) => (
            <li key={i} className={s.status}>
              <StepIcon status={s.status} />
              <span>{stepTitle(s.step, registry)}</span>
              <small>{s.status === 'running' && Number.isFinite(Number(s.progress)) ? Math.round(s.progress) + '%' : statusLabel(s.status)}</small>
            </li>
          ))}
        </ol>
      )}
      {error && <p className="forge-error">{error}</p>}
      {(job.warnings?.length > 0 || job._meta?.warnings?.length > 0) && (
        <ul className="forge-warnings">
          {(job.warnings?.length ? job.warnings : job._meta.warnings).slice(0, 4).map((w, i) => (
            <li key={i}>{String(w)}</li>
          ))}
        </ul>
      )}
      {log && (
        <details className="forge-log" open={active || wantLog}>
          <summary>{t('forge.jobs.log')}{logStep ? ' · ' + stepTitle(logStep.step, registry) : ''}</summary>
          <pre>{log}</pre>
        </details>
      )}
      {brief && (
        <details className="forge-log" onToggle={(e) => e.currentTarget.open && loadFull()}>
          <summary>{t('forge.jobs.log')}</summary>
          <pre>{loadingLog ? t('forge.jobs.logLoading') : t('forge.jobs.logEmpty')}</pre>
        </details>
      )}
      <div className="forge-row">
        {active && (
          <button type="button" className="secondary small" onClick={() => cancel(job.id)}>
            <CircleStop size={13} />
            {t('forge.jobs.cancel')}
          </button>
        )}
        {job.assetId && job.status === 'completed' && job.type !== 'clips' && (
          <button type="button" className="secondary small" onClick={showInScene}>
            <Eye size={13} />
            {t('forge.jobs.showInScene')}
          </button>
        )}
        {job.assetId && job.assetId !== assetId && (
          <button type="button" className="forge-link" onClick={() => openAsset({ id: job.assetId }).catch((e) => notify(humanizeError(e.message), true))}>
            <FolderOpen size={12} />
            {t('forge.jobs.openAsset')}
          </button>
        )}
      </div>
    </div>
  );
}

export default function ForgeJobs() {
  const t = useT();
  const { offline, sortedJobs, notify, mergeJobs } = useForge();
  const [onlyActive, setOnlyActive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [names, setNames] = useState({});

  const refresh = async () => {
    if (offline) return;
    setLoading(true);
    try {
      const [list, assets] = await Promise.all([listJobs(), listAssets().catch(() => [])]);
      mergeJobs(Array.isArray(list) ? list : list?.jobs || []);
      setNames(Object.fromEntries((Array.isArray(assets) ? assets : []).map((a) => [a.id, a.name])));
    } catch (e) {
      notify(e.message, true);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [offline]); // eslint-disable-line react-hooks/exhaustive-deps

  const list = sortedJobs.filter((j) => !onlyActive || isActiveStatus(j.status));
  return (
    <Section
      title={t('forge.jobs.title')}
      aside={
        <button type="button" className="icon-btn" title={t('forge.jobs.refresh')} aria-label={t('forge.jobs.refreshAria')} disabled={offline || loading} onClick={refresh}>
          <RefreshCw size={14} className={loading ? 'spin' : ''} />
        </button>
      }
    >
      <label className="forge-check">
        <input type="checkbox" checked={onlyActive} onChange={(e) => setOnlyActive(e.target.checked)} />
        <span>{t('forge.jobs.onlyActive')}</span>
      </label>
      {offline && <Notice>{t('forge.jobs.offline')}</Notice>}
      {!offline && !list.length && <p className="field-note">{t('forge.jobs.empty')}</p>}
      <div className="forge-jobs">
        {list.slice(0, 60).map((j) => (
          <JobCard key={j.id} job={j} assetName={names[j.assetId]} />
        ))}
      </div>
    </Section>
  );
}
