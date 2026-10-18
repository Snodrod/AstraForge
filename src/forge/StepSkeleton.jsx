// Step 3 "Skeleton": humanoid autorig with draggable markers, creature templates, poses.png.
import React, { useEffect, useRef, useState } from 'react';
import { Bone, Loader2, Download, ExternalLink } from 'lucide-react';
import {
  paramDefs, resolveParamName, buildParams, findOutput, enumLabel, findStepDef, latestPipelineEntry, planSteps,
  rigSeedValues, markersForHeight, roundMarkers, replayNote,
} from './forge-core.js';
import { fetchJsonFile, outputUrl } from './forge-api.js';
import { Section, Notice, ParamField, MoreParams, StepResult, Chips, NumberInput, useForge } from './ForgeUi.jsx';
import MarkerEditor from './MarkerEditor.jsx';
import { t, useT, tOr } from '../i18n/index.js';

export default function StepSkeleton() {
  const { kind, assetId } = useForge();
  // keyed by the passport: the form is seeded from that asset's previous runs
  return kind === 'creature' ? <CreatureRig key={assetId || 'new'} /> : <HumanoidRig key={assetId || 'new'} />;
}

function HumanoidRig() {
  const t = useT();
  const { registry, offline, assetId, asset, runPipeline, notify, lastResult, activeJobFor } = useForge();
  const defs = paramDefs(registry, 'rig_humanoid');
  const markersName = resolveParamName(defs, 'markers');
  const fingersName = resolveParamName(defs, 'fingers');
  const heightName = resolveParamName(defs, 'height');
  // Previous rig params (fingers, robe, …) and the height chosen on step 2.
  const [values, setValuesRaw] = useState(() => rigSeedValues(asset, { heightName, markersName }));
  // Until the user edits the form it follows the passport (it may load after the step opens).
  const touched = useRef(false);
  const setValues = (next) => {
    touched.current = true;
    setValuesRaw(next);
  };
  useEffect(() => {
    if (!touched.current) setValuesRaw(rigSeedValues(asset, { heightName, markersName }));
  }, [asset, heightName, markersName]);
  const [doc, setDoc] = useState(null);
  const [docError, setDocError] = useState('');
  const running = activeJobFor(['rig_humanoid', 'import_mixamo_rig']);
  const last = lastResult(['rig_humanoid']);
  const outputs = last?.result?.outputs;
  const markersPng = findOutput(outputs, { ext: ['.png', '.jpg', '.webp'], match: /marker/ });
  const markersJson = findOutput(outputs, { ext: '.json', match: /marker/ });
  const markersPngUrl = markersPng ? outputUrl(assetId, markersPng) : null;
  const markersJsonUrl = markersJson ? outputUrl(assetId, markersJson) : null;

  useEffect(() => {
    setDoc(null);
    setDocError('');
    if (!markersJsonUrl) return;
    let alive = true;
    fetchJsonFile(markersJsonUrl)
      .then((d) => alive && setDoc(d))
      .catch((e) => alive && setDocError(e.message));
    return () => {
      alive = false;
    };
  }, [markersJsonUrl]);

  async function build(extra = {}) {
    try {
      const params = buildParams(defs, values, extra);
      const plan = planSteps(asset, 'skeleton', [{ step: 'rig_humanoid', params }]);
      await runPipeline(plan.steps, { from: plan.from });
      notify((extra[markersName] ? t('wizard.skeleton.rebuildStarted') : t('wizard.skeleton.autorigStarted')) + replayNote(plan, registry));
    } catch (e) {
      notify(e.message, true);
    }
  }

  /**
   * Rebuild by the dragged markers. markers.json is in the frame of its own height_m, so the
   * height is always sent with the markers; if the form height differs, the dots are scaled
   * to it (rig_humanoid divides params.markers by params.height_m).
   */
  function rebuild(markers) {
    const docHeight = Number(doc?.height_m) > 0 ? Number(doc.height_m) : null;
    const formHeight = Number(values[heightName]) > 0 ? Number(values[heightName]) : null;
    const height = formHeight || docHeight;
    const scaled = docHeight && height ? roundMarkers(markersForHeight(markers, docHeight, height)) : markers;
    const extra = { [markersName]: scaled };
    if (height) extra[heightName] = height;
    return build(extra);
  }

  // FBX uploads keep their name in source.originalName and land as source/source.fbx.
  const fromFbx = /\.fbx$/i.test(asset?.source?.originalName || asset?.source?.file || '');

  return (
    <>
      <Section title={t('wizard.skeleton.mixamoTitle')}>
        {defs[heightName] && (
          <>
            {/* typed, not a slider: a 1.2 m dwarf must be exactly 1.2 */}
            <label className="forge-field" title={tOr('param.rig_humanoid.' + heightName + '.help', defs[heightName].help)}>
              <span>{tOr('param.rig_humanoid.' + heightName, defs[heightName].title || t('wizard.skeleton.height'))}</span>
              <NumberInput def={defs[heightName]} value={values[heightName] ?? defs[heightName].default} onChange={(v) => setValues((s) => ({ ...s, [heightName]: v }))} />
            </label>
            <p className="field-note">{t('wizard.skeleton.heightNote')}</p>
          </>
        )}
        {defs[fingersName] && <ParamField step="rig_humanoid" name={fingersName} def={defs[fingersName]} value={values[fingersName]} onChange={(v) => setValues((s) => ({ ...s, [fingersName]: v }))} />}
        <MoreParams step="rig_humanoid" defs={defs} values={values} onChange={setValues} skip={[markersName, fingersName, heightName]} />
        <button type="button" className="primary full rig-gap" disabled={offline || !!running} onClick={() => build()}>
          {running ? <Loader2 size={15} className="spin" /> : <Bone size={15} />}
          {running ? t('wizard.skeleton.building') : last ? t('wizard.skeleton.rebuildAuto') : t('wizard.skeleton.build')}
        </button>
        <p className="field-note">{t('wizard.skeleton.mixamoNote')}</p>
        {fromFbx && findStepDef(registry, 'import_mixamo_rig') !== null && (
          <button type="button" className="secondary full rig-gap" disabled={offline || !!running} onClick={() => runPipeline([{ step: 'import_mixamo_rig', params: {} }], { from: 'source' }).catch((e) => notify(e.message, true))}>
            {t('wizard.skeleton.importMixamo')}
          </button>
        )}
      </Section>

      {markersPngUrl && (
        <Section title={t('wizard.skeleton.markersTitle')}>
          {docError && <Notice tone="warn">{t('wizard.skeleton.markersError', { error: docError })}</Notice>}
          <MarkerEditor
            imageUrl={markersPngUrl}
            doc={doc}
            busy={!!running}
            disabled={offline}
            onRebuild={rebuild}
          />
        </Section>
      )}

      <Section title={t('wizard.skeleton.resultTitle')}>
        <StepResult steps={['rig_humanoid', 'import_mixamo_rig']} imagesFilter={(e) => !/marker/i.test(e.key + e.path)} />
        {!last && !running && <p className="field-note">{t('wizard.skeleton.resultHint')}</p>}
      </Section>

      <MixamoManual />
    </>
  );
}

/** Optional manual path: FBX for mixamo.com auto-rigger, done by the owner by hand. */
function MixamoManual() {
  const t = useT();
  const { registry, offline, asset, assetId, runPipeline, notify, lastResult, activeJobFor } = useForge();
  if (!findStepDef(registry, 'prepare_mixamo')) return null;
  const running = activeJobFor(['prepare_mixamo']);
  const last = lastResult(['prepare_mixamo']);
  const fbx = last?.result ? findOutput(last.result.outputs, { ext: '.fbx' }) : null;
  const url = fbx ? outputUrl(assetId, fbx) : null;
  return (
    <Section title={t('wizard.skeleton.manualTitle')}>
      <details className="forge-more">
        <summary>{t('wizard.skeleton.manualSummary')}</summary>
        <ol className="forge-steps-list">
          <li>{t('wizard.skeleton.manual1')}</li>
          <li>{t('wizard.skeleton.manual2')}</li>
          <li>{t('wizard.skeleton.manual3')}</li>
        </ol>
        <p className="field-note">{t('wizard.skeleton.manualNote')}</p>
        <button type="button" className="secondary full" disabled={offline || !!running} onClick={() => {
            const plan = planSteps(asset, 'skeleton', [{ step: 'prepare_mixamo', params: {} }]);
            runPipeline(plan.steps, { from: plan.from }).catch((e) => notify(e.message, true));
          }}>
          {running ? <Loader2 size={14} className="spin" /> : <ExternalLink size={14} />}
          {t('wizard.skeleton.prepareFbx')}
        </button>
        {url && (
          <a className="secondary full forge-download" href={url} download>
            <Download size={14} />
            {t('wizard.skeleton.downloadFbx')}
          </a>
        )}
      </details>
    </Section>
  );
}

/** Hint per creature template; the template ids come from the rig_creature enum. */
const TEMPLATE_HINTS = ['auto', 'quadruped', 'winged_quadruped', 'bird', 'hexapod', 'octopod', 'serpent'];
const templateHint = (id) => (TEMPLATE_HINTS.includes(id) ? t('wizard.skeleton.template.' + id) : undefined);

function CreatureRig() {
  const t = useT();
  const { registry, offline, asset, runPipeline, notify, lastResult, activeJobFor } = useForge();
  const defs = paramDefs(registry, 'rig_creature');
  const templateName = resolveParamName(defs, 'template');
  const templates = defs[templateName]?.enum || ['auto'];
  const previous = latestPipelineEntry(asset, ['rig_creature'])?.params?.[templateName];
  const [values, setValues] = useState(() => ({ [templateName]: previous || asset?.profile?.template || defs[templateName]?.default || 'auto' }));
  const running = activeJobFor(['rig_creature']);
  const last = lastResult(['rig_creature']);

  async function build() {
    try {
      const plan = planSteps(asset, 'skeleton', [{ step: 'rig_creature', params: buildParams(defs, values) }]);
      await runPipeline(plan.steps, { from: plan.from });
      notify(t('wizard.skeleton.creatureStarted') + replayNote(plan, registry));
    } catch (e) {
      notify(e.message, true);
    }
  }

  return (
    <>
      <Section title={t('wizard.skeleton.templateTitle')}>
        <Chips
          label={t('wizard.skeleton.templateAria')}
          options={templates.map((id) => ({ id, title: enumLabel(id), hint: templateHint(id) }))}
          value={values[templateName]}
          onChange={(id) => setValues((s) => ({ ...s, [templateName]: id }))}
        />
        <p className="field-note">{templateHint(values[templateName]) || t('wizard.skeleton.templateDefault')} {t('wizard.skeleton.boneNames')}</p>
        <MoreParams step="rig_creature" defs={defs} values={values} onChange={setValues} skip={[templateName]} />
        <button type="button" className="primary full rig-gap" disabled={offline || !!running} onClick={build}>
          {running ? <Loader2 size={15} className="spin" /> : <Bone size={15} />}
          {running ? t('wizard.skeleton.building') : last ? t('wizard.skeleton.rebuild') : t('wizard.skeleton.build')}
        </button>
      </Section>
      <Section title={t('wizard.skeleton.resultTitle')}>
        <StepResult steps={['rig_creature']} />
        {!last && !running && <p className="field-note">{t('wizard.skeleton.creatureResultHint')}</p>}
      </Section>
    </>
  );
}
