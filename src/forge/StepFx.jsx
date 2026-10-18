// Step 6 "Effects": FX presets bound to sockets, stored in the passport (fx[]).
import React, { useEffect, useState } from 'react';
import { Trash2, Save, Plus } from 'lucide-react';
import { FX_PRESETS, fxPreset, socketsFor, suggestSocket, fxForPassport } from './forge-core.js';
import { Section, Notice, useForge } from './ForgeUi.jsx';
import { useT } from '../i18n/index.js';

export default function StepFx() {
  const t = useT();
  const { offline, kind, asset, patchAsset, notify } = useForge();
  const sockets = socketsFor(kind, asset);
  const [rows, setRows] = useState([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setRows((asset?.fx || []).map((f, i) => ({ key: i + ':' + f.preset, preset: f.preset, socket: f.socket, params: f.params || {} })));
  }, [asset?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const add = (preset) => setRows((r) => [...r, { key: Date.now() + ':' + preset, preset, socket: suggestSocket(preset, sockets), params: {} }]);
  const saved = JSON.stringify(fxForPassport((asset?.fx || []).map((f) => ({ ...f })))) === JSON.stringify(fxForPassport(rows));

  async function save() {
    setSaving(true);
    try {
      await patchAsset({ fx: fxForPassport(rows) });
      notify(t('wizard.fx.saved'));
    } catch (e) {
      notify(e.message, true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Section title={t('wizard.fx.addTitle')}>
        <div className="forge-chips" aria-label={t('wizard.fx.presetsAria')}>
          {FX_PRESETS.map((p) => (
            <button key={p.id} type="button" onClick={() => add(p.id)} title={p.id}>
              <Plus size={11} />
              {p.title}
            </button>
          ))}
        </div>
        <p className="field-note">{t('wizard.fx.presetsNote')}</p>
      </Section>

      <Section title={t('wizard.fx.assetFx')} aside={<span className="counter">{rows.length}</span>}>
        {!rows.length && <p className="field-note">{t('wizard.fx.empty')}</p>}
        <div className="forge-fx-rows">
          {rows.map((r) => (
            <div key={r.key} className="forge-fx-row">
              <strong>{fxPreset(r.preset).title}</strong>
              <select aria-label={t('wizard.fx.socketAria', { name: fxPreset(r.preset).title })} value={r.socket} onChange={(e) => setRows((list) => list.map((x) => (x.key === r.key ? { ...x, socket: e.target.value } : x)))}>
                {[...new Set([r.socket, ...sockets])].filter(Boolean).map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <button type="button" className="icon-btn" title={t('wizard.fx.remove')} aria-label={t('wizard.fx.removeAria', { name: fxPreset(r.preset).title })} onClick={() => setRows((list) => list.filter((x) => x.key !== r.key))}>
                <Trash2 size={13} />
              </button>
            </div>
          ))}
        </div>
        <button type="button" className="primary full rig-gap" disabled={offline || saving || saved} onClick={save}>
          <Save size={14} />
          {saved ? t('wizard.fx.savedShort') : t('wizard.fx.save')}
        </button>
        <p className="field-note">{t('wizard.fx.note')}</p>
        {offline && <Notice>{t('wizard.fx.offline')}</Notice>}
      </Section>
    </>
  );
}
