// Step 2: cleanup for mesh sources, the generator form (ships, turrets, items) for params.
import React, { useEffect, useMemo, useState } from 'react';
import { Scissors, Sparkles, Dices, Loader2, Check } from 'lucide-react';
import {
  paramDefs, resolveParamName, buildParams, generatorParams, shownParams, prepareSteps, kindInfo, generatorStep,
  findStepDef, sliderToBudget, budgetToSlider, formatCount, seedVariants, enumLabel, findOutput,
  isActiveStatus, jobProgress, jobError, stepTitle, humanizeError, generatorSeedValues, latestPipelineEntry,
} from './forge-core.js';
import { createBatch, outputUrl } from './forge-api.js';
import { Section, Notice, ParamField, MoreParams, StepResult, Chips, ProgressBar, NumberInput, TrashButton, useForge } from './ForgeUi.jsx';
import { ItemKitForm } from './StepItems.jsx';
import { useT, tOr } from '../i18n/index.js';

const BUDGET_CHIPS = [5000, 15000, 30000, 60000, 120000];
/** Suggested budgets (the humanoid one matches the server preset humanoid_from_mesh). */
const KIND_BUDGET = { humanoid: 40000, creature: 30000, item: 8000, ship: 40000, turret: 15000 };

export default function StepPrepare() {
  const { source, asset } = useForge();
  // The cleanup form is seeded from the passport, so it remounts once the passport is loaded.
  return source === 'params' ? <GeneratorForm /> : <CleanupForm key={asset?.id || 'loading'} />;
}

function CleanupForm() {
  const t = useT();
  const { registry, kind, source, offline, asset, runPipeline, notify, activeJobFor, patchAsset } = useForge();
  const defs = paramDefs(registry, 'cleanup');
  const triName = resolveParamName(defs, 'triangles');
  const heightName = resolveParamName(defs, 'height');
  const triDef = defs[triName];
  const heightDef = defs[heightName];
  const minTris = Number.isFinite(triDef?.min) ? triDef.min : 1000;
  const maxTris = Number.isFinite(triDef?.max) ? triDef.max : 300000;
  // Height default: the passport profile (kept by earlier cleanups), else the kind default.
  const kindHeight = Number(asset?.profile?.height_m) > 0 ? Number(asset.profile.height_m) : kindInfo(kind)?.height;
  // values: the previous cleanup run (so going back shows what was used), then the user's edits.
  const previous = latestPipelineEntry(asset, ['cleanup'])?.params || null;
  const [values, setValues] = useState(() => (previous ? { ...previous } : {}));
  const [simplify, setSimplify] = useState(() => !previous || (previous[triName] !== null && previous[triName] !== undefined));
  const [separate, setSeparate] = useState(false);
  const tris = Math.min(maxTris, Math.max(minTris, Number(values[triName]) || KIND_BUDGET[kind] || 30000));
  const params = buildParams(defs, {
    ...(kindHeight ? { [heightName]: kindHeight } : {}),
    ...values,
    [triName]: simplify ? tris : undefined,
  });
  const hasSeparate = !!findStepDef(registry, 'separate');
  const followUps = prepareSteps(kind, source).filter((s) => s !== 'cleanup');
  const running = activeJobFor(['cleanup', 'separate', ...followUps]);

  async function run() {
    const steps = [{ step: 'cleanup', params }];
    if (separate) steps.push({ step: 'separate', params: {} });
    for (const step of followUps) steps.push({ step, params: {} });
    try {
      await runPipeline(steps, { from: 'source' });
      notify(t('wizard.prepare.cleanupStarted'));
      // The passport profile keeps the chosen height: the rig falls back to it (rig_humanoid
      // rescales the body to its own height_m otherwise).
      const h = Number(params[heightName]);
      const rigged = kind === 'humanoid' || kind === 'creature';
      if (rigged && h > 0 && Number(asset?.profile?.height_m) !== h) patchAsset({ profile: { height_m: h } }).catch(() => {});
    } catch (e) {
      notify(e.message, true);
    }
  }

  return (
    <>
      {triDef && (
        <Section title={t('wizard.prepare.budgetTitle')} aside={<span className="forge-value">{simplify ? formatCount(tris) : t('wizard.prepare.noSimplify')}</span>}>
          <label className="forge-check">
            <input type="checkbox" checked={simplify} onChange={(e) => setSimplify(e.target.checked)} />
            <span>{t('wizard.prepare.simplify')}</span>
          </label>
          <input
            type="range"
            min="0"
            max="1000"
            step="1"
            aria-label={t('wizard.prepare.budgetTitle')}
            disabled={!simplify}
            value={budgetToSlider(tris, minTris, maxTris)}
            onChange={(e) => setValues((v) => ({ ...v, [triName]: sliderToBudget(Number(e.target.value), minTris, maxTris) }))}
          />
          <div className="range-caption">
            <span>{formatCount(minTris)}</span>
            <span>{formatCount(maxTris)}</span>
          </div>
          <Chips
            label={t('wizard.prepare.budgetQuick')}
            disabled={!simplify}
            options={BUDGET_CHIPS.filter((n) => n >= minTris && n <= maxTris).map((n) => ({ id: n, title: n >= 1000 ? n / 1000 + 'k' : String(n) }))}
            value={tris}
            onChange={(n) => setValues((v) => ({ ...v, [triName]: n }))}
          />
          <p className="field-note">{t('wizard.prepare.budgetNote')}</p>
        </Section>
      )}

      <Section title={t('wizard.prepare.sizeTitle')}>
        {heightDef && (
          <>
            <label className="forge-field">
              <span>{tOr('param.cleanup.' + heightName, defs[heightName]?.title || t('wizard.prepare.height'))}</span>
              <NumberInput
                def={heightDef}
                value={heightName in values ? values[heightName] : kindHeight ?? null}
                placeholder={t('wizard.prepare.heightPlaceholder')}
                onChange={(v) => setValues((cur) => ({ ...cur, [heightName]: v }))}
              />
            </label>
            <p className="field-note">{t('wizard.prepare.heightNote')}</p>
          </>
        )}
        {!heightDef && !triDef && <p className="field-note">{t('wizard.prepare.noParams')}</p>}
        {hasSeparate && (
          <label className="forge-check">
            <input type="checkbox" checked={separate} onChange={(e) => setSeparate(e.target.checked)} />
            <span>{t('wizard.prepare.separate')}</span>
          </label>
        )}
        <MoreParams step="cleanup" defs={defs} values={values} onChange={setValues} skip={[triName, heightName]} />
      </Section>

      <div className="generate-area">
        <button type="button" className="primary generate" disabled={offline || !!running} onClick={run}>
          {running ? <Loader2 size={16} className="spin" /> : <Scissors size={16} />}
          {running ? t('wizard.prepare.blenderBusy') : t('wizard.prepare.cleanup')}
        </button>
        {followUps.length > 0 && <p>{t('wizard.prepare.then', { steps: followUps.map((s) => stepTitle(s, registry)).join(', ') })}</p>}
      </div>

      <Section title={t('wizard.prepare.result')}>
        <StepResult steps={['cleanup', 'separate', ...followUps]} />
        {!running && <EmptyHint steps={['cleanup', 'separate', ...followUps]} text={t('wizard.prepare.resultHint')} />}
      </Section>
    </>
  );
}

function EmptyHint({ steps, text }) {
  const { lastResult } = useForge();
  if (lastResult(steps)) return null;
  return <p className="field-note">{text}</p>;
}

// --------------------------------------------------------------------------- generators

const SHIP_FEATURED = ['shipClass', 'style', 'length', 'engines', 'turrets', 'greebles'];

function GeneratorForm() {
  const t = useT();
  const { registry, kind, offline, asset, assetId, runPipeline, notify, createPassport, activeJobFor, draftName } = useForge();
  const step = generatorStep(kind);
  const defs = paramDefs(registry, step);
  const seedName = resolveParamName(defs, 'seed');
  // The latest generator run (re-runs with changed values), else the passport source params.
  const [values, setValues] = useState(() => generatorSeedValues(asset, step));
  const params = generatorParams(defs, values);
  const shown = shownParams(defs, params);
  const running = activeJobFor([step]);
  const [creating, setCreating] = useState(false);

  // A picked variant brings its own seed/params into the form.
  useEffect(() => {
    if (asset) setValues(generatorSeedValues(asset, step));
  }, [asset?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function generate() {
    setCreating(true);
    try {
      let id = assetId;
      if (!id) {
        const info = kindInfo(kind);
        const a = await createPassport({ name: asset?.name || draftName || info.defaultName, kind: info.apiKind, source: { type: 'params', params } });
        id = a.id;
      }
      await runPipeline([{ step, params }], { id, from: 'source' });
      notify(t('wizard.prepare.generatorStarted'));
    } catch (e) {
      notify(e.message, true);
    } finally {
      setCreating(false);
    }
  }

  const featured = useMemo(() => {
    if (kind === 'ship') return SHIP_FEATURED.map((alias) => resolveParamName(defs, alias)).filter((n) => defs[n]);
    return Object.keys(defs).filter((n) => n !== seedName && !defs[n]?.server && !defs[n]?.auto && defs[n]?.type !== 'object');
  }, [defs, kind, seedName]);

  const classChips = kind === 'ship' && featured[0] && defs[featured[0]]?.enum?.length <= 10 ? featured[0] : null;
  const setParam = (name, v) => setValues((cur) => ({ ...cur, [name]: v }));

  return (
    <>
      {kind === 'item' ? (
        <Section title={t('wizard.prepare.item')}>
          <ItemKitForm step={step} defs={defs} values={values} onChange={setValues} />
        </Section>
      ) : (
        <Section title={kind === 'ship' ? t('wizard.prepare.ship') : t('wizard.prepare.turret')}>
          {classChips && (
            <Chips
              label={t('wizard.prepare.shipClass')}
              options={defs[classChips].enum.map((v) => ({ id: v, title: enumLabel(v) }))}
              value={shown[classChips]}
              onChange={(v) => setParam(classChips, v)}
            />
          )}
          {featured
            .filter((n) => n !== classChips)
            .map((n) => (
              <ParamField key={n} step={step} name={n} def={defs[n]} value={values[n]} onChange={(v) => setParam(n, v)} />
            ))}
          <ParamField step={step} name={seedName} def={defs[seedName] || { type: 'integer', title: t('wizard.prepare.seed'), default: 1 }} value={values[seedName]} onChange={(v) => setParam(seedName, v)} />
          <MoreParams step={step} defs={defs} values={values} onChange={setValues} skip={[...featured, seedName]} />
          <p className="field-note">{t('wizard.prepare.seedNote')}</p>
        </Section>
      )}

      <div className="generate-area">
        <button type="button" className="primary generate" disabled={offline || creating || !!running} onClick={generate}>
          {creating || running ? <Loader2 size={16} className="spin" /> : <Sparkles size={16} />}
          {running ? t('wizard.prepare.blenderBusy') : assetId ? t('wizard.prepare.rebuild') : t('wizard.prepare.create')}
        </button>
      </div>

      <Section title={t('wizard.prepare.result')}>
        <StepResult steps={[step]} />
      </Section>

      <Variants step={step} params={params} seedName={seedName} />
    </>
  );
}

/** "6 more variants": a batch of new passports with neighbouring seeds; pick one. */
function Variants({ step, params, seedName }) {
  const t = useT();
  const { kind, offline, asset, notify, track, jobs, openAsset, registry, trashAsset } = useForge();
  const [batch, setBatch] = useState(null);
  const [busy, setBusy] = useState(false);

  async function more() {
    setBusy(true);
    try {
      const seeds = seedVariants(Number(params[seedName]) || 0, 6);
      const name = asset?.name || kindInfo(kind)?.defaultName;
      const items = seeds.map((seed) => {
        const p = { ...params, [seedName]: seed };
        return { name: t('wizard.prepare.variantName', { name, seed }), kind: kindInfo(kind).apiKind, source: { type: 'params', params: p }, steps: [{ step, params: p }] };
      });
      const r = await createBatch(items);
      const list = (r.items || []).map((it, i) => ({ ...it, seed: seeds[i] }));
      for (const it of list) if (it.jobId) track(it.jobId, { assetId: it.assetId, steps: [{ step }], batchId: r.batchId });
      setBatch({ id: r.batchId, items: list });
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  const settled = (it) => {
    const job = it.jobId ? jobs[it.jobId] : null;
    return !!it.error || (job && !isActiveStatus(job.status));
  };
  const spare = (batch?.items || []).filter((it) => it.assetId && it.assetId !== asset?.id && settled(it));

  async function drop(items) {
    const gone = new Set();
    for (const it of items) {
      try {
        await trashAsset(it.assetId);
        gone.add(it.assetId);
      } catch (e) {
        notify(e.message, true);
      }
    }
    if (gone.size) {
      setBatch((b) => (b ? { ...b, items: b.items.filter((it) => !gone.has(it.assetId)) } : b));
      notify(gone.size === 1 ? t('wizard.prepare.variantTrashed') : t('wizard.prepare.variantsTrashed', { n: gone.size }));
    }
  }

  return (
    <Section title={t('wizard.prepare.variants')}>
      <button type="button" className="secondary full" disabled={offline || busy} onClick={more}>
        {busy ? <Loader2 size={14} className="spin" /> : <Dices size={14} />}
        {t('wizard.prepare.moreVariants')}
      </button>
      <p className="field-note">{t('wizard.prepare.variantsNote')}</p>
      {batch && (
        <div className="forge-variants">
          {batch.items.map((it) => {
            const job = it.jobId ? jobs[it.jobId] : null;
            const result = (job?.steps || []).find((s) => s.step === step)?.result;
            const preview = result ? findOutput(result.outputs, { ext: ['.png', '.jpg', '.webp'], match: /preview|thumb/ }) || findOutput(result.outputs, { ext: ['.png', '.jpg', '.webp'] }) : null;
            const url = preview ? outputUrl(it.assetId, preview) : null;
            const active = !it.error && (!job || isActiveStatus(job.status));
            return (
              <div key={it.assetId || it.seed} className={'forge-variant ' + (asset?.id === it.assetId ? 'chosen' : '')}>
                {url ? <img src={url} alt={t('wizard.prepare.variantAlt', { seed: it.seed })} /> : <div className="forge-variant-empty">{active ? <Loader2 size={16} className="spin" /> : '—'}</div>}
                <small>{t('wizard.prepare.seedValue', { seed: it.seed })}</small>
                {active && <ProgressBar value={jobProgress(job)} />}
                {(it.error || job?.status === 'failed') && <em className="forge-error">{it.error ? humanizeError(it.error) : jobError(job)}</em>}
                <div className="forge-variant-actions">
                  {job?.status === 'completed' && (
                    <button type="button" className="secondary small" onClick={() => openAsset({ id: it.assetId }).catch((e) => notify(e.message, true))}>
                      <Check size={12} />
                      {asset?.id === it.assetId ? t('wizard.prepare.chosen') : t('wizard.prepare.choose')}
                    </button>
                  )}
                  {it.assetId && asset?.id !== it.assetId && settled(it) && <TrashButton label={t('wizard.prepare.variantTrash')} onConfirm={() => drop([it])} />}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {batch && asset && spare.length > 0 && batch.items.some((it) => it.assetId === asset.id) && (
        <TrashButton small={false} label={t('wizard.prepare.dropSpare', { n: spare.length })} onConfirm={() => drop(spare)} />
      )}
      {batch && <p className="field-note">{t('wizard.prepare.batch', { id: String(batch.id || '').slice(0, 8), step: stepTitle(step, registry) })}</p>}
      {!batch && offline && <Notice>{t('wizard.prepare.variantsOffline')}</Notice>}
    </Section>
  );
}
