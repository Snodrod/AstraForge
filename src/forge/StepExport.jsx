// Step 7 (Export): profiles, export job, result folder, download and validation rows.
import React, { useEffect, useMemo, useState } from 'react';
import { Package, Loader2, Download, Copy, FolderOpen } from 'lucide-react';
import {
  exportProfilesFor, EXPORT_PROFILES, findOutput, dirOf, outputEntries, isActiveStatus, formatDate, fileNameOf,
  exportPlan, stepTitle,
} from './forge-core.js';
import { exportAsset, outputUrl, fetchJsonFile, getJob } from './forge-api.js';
import { Section, Notice, ChecksList, JobInline, ResultImages, useForge } from './ForgeUi.jsx';
import { t, useT } from '../i18n/index.js';

const EXPORT_STEPS = ['godot_package', 'render_sprites'];

function isExportJob(job) {
  return !!(job?._meta?.export || job?.exportProfile || job?.type === 'export' || (job?.steps || []).some((s) => EXPORT_STEPS.includes(s.step)));
}

async function copyText(text, notify) {
  try {
    await navigator.clipboard.writeText(text);
    notify(t('export.copied'));
  } catch {
    notify(t('export.copyFailed'), true);
  }
}

/** "What goes into the package": the latest completed version, its steps and what it lacks. */
function RecipeBlock({ plan, registry, onGo }) {
  const t = useT();
  const skeleton = plan.missing.find((m) => m.what === 'skeleton');
  const clips = plan.missing.find((m) => m.what === 'clips');
  return (
    <Section title={t('export.recipe.title')}>
      {plan.version ? (
        <p className="forge-recipe">
          <strong>{plan.version}</strong>
          {plan.steps.length ? plan.steps.map((s) => stepTitle(s, registry)).join(' → ') : t('export.recipe.noSteps')}
        </p>
      ) : (
        <p className="field-note">{t('export.recipe.noVersions')}</p>
      )}
      {skeleton && (
        <Notice tone={skeleton.older ? 'warn' : 'info'}>
          {skeleton.older
            ? t('export.skeleton.older', { version: skeleton.older })
            : t('export.skeleton.none')}
          {' '}
          <button type="button" className="forge-link" onClick={() => onGo('skeleton')}>{t('export.goSkeleton')}</button>
        </Notice>
      )}
      {clips && !(skeleton && !skeleton.older) && (
        <Notice tone={clips.older ? 'warn' : 'info'}>
          {clips.older
            ? t('export.clips.older', { version: clips.older })
            : t('export.clips.none')}
          {' '}
          <button type="button" className="forge-link" onClick={() => onGo('motion')}>{t('export.goMotion')}</button>
        </Notice>
      )}
    </Section>
  );
}

export default function StepExport() {
  const t = useT();
  const { offline, kind, asset, assetId, notify, track, sortedJobs, mergeJobs, registry, setStepId } = useForge();
  const profiles = exportProfilesFor(kind);
  const [profile, setProfile] = useState(profiles[0]?.id || 'godot');
  const [busy, setBusy] = useState(false);
  const [validation, setValidation] = useState(null);

  const job = useMemo(() => sortedJobs.find((j) => j.assetId === assetId && isExportJob(j)) || null, [sortedJobs, assetId]);
  const active = job && isActiveStatus(job.status);
  const lastStep = job ? [...(job.steps || [])].reverse().find((s) => s.result) || null : null;
  const result = lastStep?.result || job?.result || null;
  const outputs = result?.outputs;
  // The server zips every export into exports/<profile>-<version>.zip and lists it in asset.exports.
  const record = job ? (asset?.exports || []).find((e) => e.jobId === job.id) || null : null;
  const archive = record?.path || findOutput(outputs, { ext: ['.zip'] }) || findOutput(outputs, { ext: ['.glb'] });
  const archiveUrl = record?.url || (archive ? outputUrl(assetId, archive) : null);
  const outFolder = [outputs?.package, outputs?.package_dir, outputs?.folder, outputs?.dir].find((v) => typeof v === 'string') || lastStep?.outDir;
  const folder = outFolder || (outputEntries(outputs)[0] ? dirOf(outputEntries(outputs)[0].path) : null);
  const validationFile = findOutput(outputs, { ext: '.json', match: /valid|check|report/ });
  const validationUrl = validationFile ? outputUrl(assetId, validationFile) : null;

  // GET /jobs lists jobs without step results; load the full export job once it is finished.
  useEffect(() => {
    if (!job || isActiveStatus(job.status) || (job.steps || []).some((s) => s.result)) return;
    getJob(job.id).then((full) => full && mergeJobs([full])).catch(() => {});
  }, [job?.id, job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setValidation(null);
    if (!validationUrl) return;
    let alive = true;
    fetchJsonFile(validationUrl)
      .then((v) => alive && setValidation(v))
      .catch(() => alive && setValidation(null));
    return () => {
      alive = false;
    };
  }, [validationUrl]);

  async function run() {
    setBusy(true);
    try {
      // godot_package takes the passport effects as params.fx (render_sprites has no fx).
      const withFx = profile === 'godot' && asset?.fx?.length;
      const r = await exportAsset(assetId, profile, withFx ? { fx: asset.fx } : {});
      track(r.jobId, { assetId, export: true, profile, steps: [] });
      notify(t('export.started'));
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  const exportsList = Array.isArray(asset?.exports) ? [...asset.exports].reverse() : [];
  // The server exports the latest completed version: show its recipe before exporting.
  const plan = exportPlan(asset, kind);

  return (
    <>
      <Section title={t('export.profile')}>
        <div className="forge-sources" role="radiogroup" aria-label={t('export.profile')}>
          {profiles.map((p) => (
            <button key={p.id} type="button" role="radio" aria-checked={profile === p.id} className={profile === p.id ? 'active' : ''} onClick={() => setProfile(p.id)}>
              <Package size={16} />
              <span>
                {p.title}
                <small>{p.hint}</small>
              </span>
            </button>
          ))}
        </div>
      </Section>

      {asset && <RecipeBlock plan={plan} registry={registry} onGo={setStepId} />}

      <div className="generate-area">
        <button type="button" className="primary generate" disabled={offline || busy || active || !assetId} onClick={run}>
          {busy || active ? <Loader2 size={16} className="spin" /> : <Package size={16} />}
          {active ? t('export.running') : t('export.run')}
        </button>
      </div>

      <Section title={t('export.result')}>
        {job && <JobInline job={job} />}
        {!job && <p className="field-note">{t('export.resultEmpty')}</p>}
        {folder && (
          <div className="forge-path">
            <FolderOpen size={13} />
            <code title={folder}>{folder}</code>
            <button type="button" className="icon-btn" title={t('export.copyPath')} aria-label={t('export.copyPath')} onClick={() => copyText(folder, notify)}>
              <Copy size={13} />
            </button>
          </div>
        )}
        {archiveUrl && (
          <a className="primary full forge-download" href={archiveUrl} download={fileNameOf(archive)}>
            <Download size={15} />
            {t('export.downloadFile', { file: fileNameOf(archive) })}
          </a>
        )}
        {archive && !archiveUrl && <p className="field-note">{t('export.outside')}</p>}
        {result && <ResultImages assetId={assetId} outputs={outputs} />}
        {result?.checks?.length > 0 && <ChecksList checks={result.checks} title={t('export.checks')} />}
        {validation && <ChecksList checks={validation} title={t('export.godotCheck')} />}
        {result?.warnings?.length > 0 && (
          <ul className="forge-warnings">
            {result.warnings.slice(0, 8).map((w, i) => (
              <li key={i}>{String(w)}</li>
            ))}
          </ul>
        )}
      </Section>

      {exportsList.length > 0 && (
        <Section title={t('export.past')} aside={<span className="counter">{exportsList.length}</span>}>
          <div className="forge-list">
            {exportsList.slice(0, 8).map((e, i) => (
              <div key={i} className="forge-list-row static">
                <span>
                  {EXPORT_PROFILES.find((p) => p.id === e.profile || p.id === String(e.profile).replace(/-/g, '_'))?.title || e.profile}
                  <small>
                    {formatDate(e.at)}
                    {e.version ? ' · ' + e.version : ''}
                    {Number(e.bytes) > 0 ? ' · ' + t('export.mb', { n: (Number(e.bytes) / 1048576).toFixed(1) }) : ''}
                  </small>
                  {e.path && <code title={e.path}>{e.path}</code>}
                </span>
                {e.url && (
                  <a className="icon-btn" href={e.url} download={fileNameOf(e.path || e.url)} title={t('export.download')} aria-label={t('export.downloadAria')}>
                    <Download size={13} />
                  </a>
                )}
                {e.path && (
                  <button type="button" className="icon-btn" title={t('export.copyPath')} aria-label={t('export.copyPath')} onClick={() => copyText(e.path, notify)}>
                    <Copy size={13} />
                  </button>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}
      {offline && <Notice>{t('export.offline')}</Notice>}
    </>
  );
}
