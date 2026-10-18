// Step 5 "Items": item kit gallery and "Put in hand" (attach_prop).
import React, { useEffect, useState } from 'react';
import { Sword, Axe, Hammer, Shield, Wand, Crosshair, Gavel, Loader2, Hand, Sparkles, Flame, FlaskConical, BookOpen, Zap } from 'lucide-react';
import {
  paramDefs, resolveParamName, buildParams, generatorParams, shownParams, enumLabel, findOutput, isActiveStatus, jobProgress, jobError,
  kindFromAsset, planSteps, replayNote,
} from './forge-core.js';
import { listAssets, createAsset, runSteps, outputUrl } from './forge-api.js';
import { Section, ParamField, SeedInput, MoreParams, StepResult, ProgressBar, Notice, useForge } from './ForgeUi.jsx';
import { useT, tOr } from '../i18n/index.js';

const TYPE_ICONS = {
  sword: Sword, greatsword: Sword, dagger: Sword, axe: Axe, battle_axe: Axe, halberd: Axe, hammer: Hammer, warhammer: Hammer, mace: Gavel,
  shield: Shield, shield_round: Shield, shield_kite: Shield, shield_tower: Shield, buckler: Shield, staff: Wand, wand: Wand,
  bow: Crosshair, crossbow: Crosshair, spear: Crosshair, torch: Flame, potion: FlaskConical, book: BookOpen, blaster_pistol: Zap, blaster_rifle: Zap,
};

/** Gallery of item types + style / seed / ornament (shared with step 2 for kind=item). */
export function ItemKitForm({ defs, values, onChange, disabled, step = 'item_kit' }) {
  const t = useT();
  const typeName = resolveParamName(defs, 'itemType');
  const styleName = resolveParamName(defs, 'style');
  const seedName = resolveParamName(defs, 'seed');
  const ornamentName = resolveParamName(defs, 'ornament');
  const typeDef = defs[typeName];
  const types = typeDef?.enum || [];
  const params = shownParams(defs, values);
  const set = (name, v) => onChange({ ...values, [name]: v });
  return (
    <>
      {types.length > 0 && (
        <div className="forge-gallery" role="radiogroup" aria-label={t('wizard.items.typeAria')}>
          {types.map((type) => {
            const Icon = TYPE_ICONS[type] || Sparkles;
            return (
              <button key={type} type="button" role="radio" aria-checked={params[typeName] === type} className={params[typeName] === type ? 'active' : ''} disabled={disabled} onClick={() => set(typeName, type)}>
                <Icon size={20} />
                <span>{enumLabel(type)}</span>
              </button>
            );
          })}
        </div>
      )}
      {defs[styleName] && <ParamField step={step} name={styleName} def={defs[styleName]} value={values[styleName]} disabled={disabled} onChange={(v) => set(styleName, v)} />}
      {defs[ornamentName] && <ParamField step={step} name={ornamentName} def={defs[ornamentName]} value={values[ornamentName]} disabled={disabled} onChange={(v) => set(ornamentName, v)} />}
      <SeedInput value={params[seedName]} label={tOr('param.' + step + '.' + seedName, defs[seedName]?.title || t('wizard.items.seed'))} disabled={disabled} onChange={(v) => set(seedName, v)} />
      <MoreParams step={step} defs={defs} values={values} onChange={onChange} skip={[typeName, styleName, seedName, ornamentName]} disabled={disabled} />
    </>
  );
}

export default function StepItems() {
  const t = useT();
  const { registry, offline, asset, notify, track, jobs, patchAsset, runPipeline, activeJobFor } = useForge();
  const kitDefs = paramDefs(registry, 'item_kit');
  const attachDefs = paramDefs(registry, 'attach_prop');
  const handName = resolveParamName(attachDefs, 'hand');
  const modeName = resolveParamName(attachDefs, 'mode');
  const [kit, setKit] = useState({});
  const [items, setItems] = useState(null);
  const [itemId, setItemId] = useState(asset?.props?.[0]?.asset || null);
  const [created, setCreated] = useState(null); // {assetId, jobId}
  const [hand, setHand] = useState(attachDefs[handName]?.default ?? 'r');
  const [mode, setMode] = useState(attachDefs[modeName]?.default ?? 'bone_child');
  const [busy, setBusy] = useState(false);
  const attaching = activeJobFor(['attach_prop']);

  const loadItems = () => {
    if (offline) return;
    listAssets()
      .then((list) => setItems((Array.isArray(list) ? list : list?.assets || []).filter((a) => kindFromAsset(a) === 'item')))
      .catch(() => setItems([]));
  };
  useEffect(loadItems, [offline]); // eslint-disable-line react-hooks/exhaustive-deps

  async function makeItem() {
    setBusy(true);
    try {
      const params = generatorParams(kitDefs, kit);
      const typeName = resolveParamName(kitDefs, 'itemType');
      const title = t('wizard.items.newItemName', { type: enumLabel(params[typeName]) || t('wizard.items.itemFallback'), owner: asset?.name || t('wizard.items.ownerFallback') });
      const item = await createAsset({ name: title, kind: 'item', source: { type: 'params', params } });
      const r = await runSteps(item.id, [{ step: 'item_kit', params }]);
      track(r.jobId, { assetId: item.id, steps: [{ step: 'item_kit' }] }, (job) => {
        if (job.status === 'completed') loadItems();
      });
      setCreated({ assetId: item.id, jobId: r.jobId });
      setItemId(item.id);
      notify(t('wizard.items.itemStarted'));
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function attach() {
    if (!itemId) return;
    const socket = hand === 'l' ? 'socket_mount_hand_l' : 'socket_mount_hand_r';
    try {
      const props = [...(asset?.props || []).filter((p) => p.socket !== socket), { asset: itemId, socket }];
      await patchAsset({ props });
      // hand (contract §6) and the chosen item are always sent; mode only when the step declares it.
      const params = buildParams(attachDefs, { [modeName]: mode }, { [handName]: hand, [resolveParamName(attachDefs, 'propAsset')]: itemId });
      const plan = planSteps(asset, 'items', [{ step: 'attach_prop', params }]);
      await runPipeline(plan.steps, { from: plan.from });
      notify(t('wizard.items.attachStarted') + replayNote(plan, registry));
    } catch (e) {
      notify(e.message, true);
    }
  }

  const createdJob = created ? jobs[created.jobId] : null;
  const createdResult = createdJob?.steps?.find((s) => s.step === 'item_kit')?.result;
  const createdPreview = createdResult ? findOutput(createdResult.outputs, { ext: ['.png', '.jpg', '.webp'] }) : null;
  const handOptions = attachDefs[handName]?.enum || ['r', 'l'];
  const modeOptions = attachDefs[modeName]?.enum || [];

  return (
    <>
      <Section title={t('wizard.items.newItem')}>
        <ItemKitForm defs={kitDefs} values={kit} onChange={setKit} disabled={busy} />
        <button type="button" className="secondary full rig-gap" disabled={offline || busy} onClick={makeItem}>
          {busy ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />}
          {t('wizard.items.createItem')}
        </button>
        {createdJob && (
          <div className="forge-created">
            {isActiveStatus(createdJob.status) && <ProgressBar value={jobProgress(createdJob)} />}
            {createdJob.status === 'failed' && <p className="forge-error">{jobError(createdJob)}</p>}
            {createdPreview && <img src={outputUrl(created.assetId, createdPreview)} alt={t('wizard.items.previewAlt')} />}
          </div>
        )}
      </Section>

      <Section title={t('wizard.items.pickTitle')}>
        {items === null && !offline && <p className="field-note"><Loader2 size={12} className="spin" /> {t('wizard.items.loading')}</p>}
        {items?.length === 0 && <p className="field-note">{t('wizard.items.empty')}</p>}
        {items?.length > 0 && (
          <select aria-label={t('wizard.items.pickAria')} value={itemId || ''} onChange={(e) => setItemId(e.target.value || null)}>
            <option value="">{t('wizard.items.pickPlaceholder')}</option>
            {items.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        )}
        {offline && <Notice>{t('wizard.items.offline')}</Notice>}
      </Section>

      <Section title={t('wizard.items.attachTitle')}>
        <div className="segmented" role="radiogroup" aria-label={t('wizard.items.hand')}>
          {handOptions.map((h) => (
            <button key={h} type="button" role="radio" aria-checked={hand === h} className={hand === h ? 'active' : ''} onClick={() => setHand(h)}>
              <Hand size={13} style={h === 'l' || h === 'left' ? { transform: 'scaleX(-1)' } : undefined} />
              {enumLabel(h)}
            </button>
          ))}
        </div>
        {modeOptions.length > 0 && (
          <label className="forge-field">
            <span>{tOr('param.attach_prop.' + modeName, attachDefs[modeName]?.title || t('wizard.items.mode'))}</span>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              {modeOptions.map((m) => (
                <option key={m} value={m}>{enumLabel(m)}</option>
              ))}
            </select>
          </label>
        )}
        <button type="button" className="primary full rig-gap" disabled={offline || !itemId || !!attaching} onClick={attach}>
          {attaching ? <Loader2 size={14} className="spin" /> : <Hand size={14} />}
          {t('wizard.items.attach')}
        </button>
        <p className="field-note">{t('wizard.items.attachNote')}</p>
        <StepResult steps={['attach_prop']} />
      </Section>
    </>
  );
}
