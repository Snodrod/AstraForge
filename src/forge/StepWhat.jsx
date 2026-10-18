// Step 1 "What are we making": kind cards, source and the passport.
import React, { useEffect, useState } from 'react';
import { User, PawPrint, Sword, Rocket, Crosshair, Box, Sparkles, Upload, SlidersHorizontal, ArrowRight, Loader2, FolderOpen } from 'lucide-react';
import { GLTFExporter } from 'three/addons/exporters/GLTFExporter.js';
import { clone as cloneSkinned } from 'three/addons/utils/SkeletonUtils.js';
import { KINDS, kindInfo, sourcesFor, extOf, formatDate, statusLabel, kindFromAsset } from './forge-core.js';
import { listAssets, listGenerationJobs, uploadStudioFile, createAssetFromFile, outputUrl } from './forge-api.js';
import { Section, Dropzone, TrashButton, useForge } from './ForgeUi.jsx';
import { useT } from '../i18n/index.js';

/** Extensions the forge accepts as a multipart upload (server sniffMeshUpload). */
const FORGE_UPLOAD = ['.glb', '.fbx', '.obj'];

const KIND_ICONS = { humanoid: User, creature: PawPrint, item: Sword, ship: Rocket, turret: Crosshair };
const SOURCE_ICONS = { scene: Box, job: Sparkles, upload: Upload, params: SlidersHorizontal };

/** Top-level scene object to send: selection (lifted to its root) or the only model. */
function sceneTarget(studio) {
  const s = studio?.current;
  if (!s) return null;
  let o = s.selected;
  if (o) {
    while (o.parent && o.parent !== s.content) o = o.parent;
    return o;
  }
  const tops = s.content?.children || [];
  return tops.length === 1 ? tops[0] : null;
}

/** Imported models sit in a display wrapper (StudioScene.importAsset: scale 3/max, centred, moved aside). */
function displayWrapper(object) {
  return object?.isGroup && object.children.length === 1 && object.children[0].userData?.source ? object : null;
}

/**
 * GLB of a scene object in its own units. The viewport wrapper's display scale and offset are
 * dropped (they would bake 3/max into the metric size of items and ships); a scale the user
 * applied on top of the import and the rotation are kept. The scene itself is not touched:
 * a skeleton-aware clone is exported.
 */
async function exportSceneObject(studio, object) {
  let target = object;
  const wrapper = displayWrapper(object);
  if (wrapper) {
    const base = studio?.editor?.bases?.[wrapper.uuid];
    const baseScale = Array.isArray(base?.scale) ? Number(base.scale[0]) : NaN;
    const userScale = baseScale > 0 ? wrapper.scale.x / baseScale : 1;
    target = cloneSkinned(wrapper);
    target.position.set(0, 0, 0);
    target.scale.setScalar(Number.isFinite(userScale) && userScale > 0 ? userScale : 1);
    target.updateMatrixWorld(true);
  }
  const bytes = await new GLTFExporter().parseAsync(target, { binary: true, onlyVisible: true });
  const base = String(object.name || 'scene-model').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 80) || 'scene-model';
  return new File([bytes], base + '.glb', { type: 'model/gltf-binary' });
}

export default function StepWhat({ onCreated }) {
  const t = useT();
  const { offline, asset, notify, studio, snapshot, kind, setKind, source, setSource, createPassport, openAsset, newAsset, setDraftName, trashAsset } = useForge();
  const [name, setName] = useState(kindInfo(kind)?.defaultName || '');
  const [nameTouched, setNameTouched] = useState(false);
  const [genJobs, setGenJobs] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState('');
  const [existing, setExisting] = useState(null);

  const sources = sourcesFor(kind);

  useEffect(() => {
    if (!nameTouched) setName(kindInfo(kind)?.defaultName || '');
    if (!sources.some((s) => s.id === source)) setSource(sources[0]?.id || null);
  }, [kind]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (source !== 'job' || genJobs !== null) return;
    listGenerationJobs()
      .then(setGenJobs)
      .catch((e) => {
        setGenJobs([]);
        notify(e.message, true);
      });
  }, [source, genJobs, notify]);

  useEffect(() => {
    if (offline) return;
    listAssets()
      .then((list) => setExisting(Array.isArray(list) ? list : list?.assets || []))
      .catch(() => setExisting([]));
  }, [offline]);

  async function trash(a) {
    try {
      await trashAsset(a.id);
      setExisting((list) => (list || []).filter((x) => x.id !== a.id));
      notify(t('wizard.what.trashed', { name: a.name || t('wizard.what.assetFallback') }));
    } catch (e) {
      notify(e.message, true);
    }
  }

  const selectedName = snapshot?.selected?.name;
  const sceneCount = snapshot?.objects?.filter((o) => o.depth === 0).length || 0;

  async function create() {
    const info = kindInfo(kind);
    setBusy(t('wizard.what.busyPreparing'));
    try {
      if (source === 'params') {
        setDraftName(name.trim());
        onCreated(null);
        return;
      }
      let src;
      const passportName = name.trim() || info.defaultName;
      if (source === 'scene') {
        const obj = sceneTarget(studio);
        if (!obj) throw new Error(t('wizard.what.errPickScene'));
        setBusy(t('wizard.what.busyPacking'));
        const glb = await exportSceneObject(studio?.current, obj);
        setBusy(t('wizard.what.busySending'));
        // straight into the forge (multipart, up to 300 MB): no copy in the studio assets
        const a = await createAssetFromFile(glb, { name: passportName, kind: info.apiKind });
        onCreated(a);
        return;
      } else if (source === 'job') {
        if (!jobId) throw new Error(t('wizard.what.errPickJob'));
        src = { type: 'job', jobId };
      } else if (source === 'upload') {
        if (!file) throw new Error(t('wizard.what.errPickFile'));
        if (FORGE_UPLOAD.includes(extOf(file.name))) {
          // GLB, FBX and OBJ go straight into the forge (multipart POST /api/forge/assets)
          setBusy(t('wizard.what.busyUploadForge'));
          const a = await createAssetFromFile(file, { name: passportName, kind: info.apiKind });
          onCreated(a);
          return;
        }
        // STL is not accepted by the forge upload: it goes through the studio assets
        setBusy(t('wizard.what.busyUpload'));
        const up = await uploadStudioFile(file);
        src = { type: 'asset', assetId: up.id };
      }
      setBusy(t('wizard.what.busyCreating'));
      const a = await createPassport({ name: passportName, kind: info.apiKind, source: src });
      onCreated(a);
    } catch (e) {
      notify(e.message, true);
    } finally {
      setBusy('');
    }
  }

  if (asset) {
    return (
      <>
        <Section title={t('wizard.what.openAsset')}>
          <div className="forge-open-card">
            <strong>{asset.name}</strong>
            <small>
              {t('wizard.what.updated', { kind: kindInfo(kindFromAsset(asset))?.title || asset.kind, date: formatDate(asset.updated) })}
            </small>
            <div className="forge-row">
              <button type="button" className="primary small" onClick={() => onCreated(null)}>
                {t('wizard.what.continue')}
                <ArrowRight size={14} />
              </button>
              <button type="button" className="secondary small" onClick={newAsset}>
                {t('wizard.what.newAsset')}
              </button>
            </div>
          </div>
        </Section>
        <ExistingAssets list={existing} current={asset.id} onOpen={(a) => openAsset(a).catch((e) => notify(e.message, true))} onTrash={trash} />
      </>
    );
  }

  const canCreate = !busy && (source === 'params' || !offline) && (
    source === 'params' || (source === 'scene' && sceneCount > 0) || (source === 'job' && jobId) || (source === 'upload' && file)
  );

  return (
    <>
      <Section title={t('wizard.what.whatTitle')}>
        <div className="forge-kinds">
          {KINDS.map((k) => {
            const Icon = KIND_ICONS[k.id];
            return (
              <button
                key={k.id}
                type="button"
                className={'forge-kind ' + (kind === k.id ? 'active ' : '') + (k.id === 'turret' ? 'wide' : '')}
                aria-pressed={kind === k.id}
                onClick={() => setKind(k.id)}
              >
                <Icon size={k.id === 'turret' ? 18 : 26} />
                <strong>{k.id === 'turret' ? '+ ' + k.title : k.title}</strong>
                <small>{k.hint}</small>
              </button>
            );
          })}
        </div>
      </Section>

      <Section title={t('wizard.what.sourceTitle')}>
        <div className="forge-sources" role="radiogroup" aria-label={t('wizard.what.sourceAria')}>
          {sources.map((s) => {
            const Icon = SOURCE_ICONS[s.id];
            return (
              <button key={s.id} type="button" role="radio" aria-checked={source === s.id} className={source === s.id ? 'active' : ''} onClick={() => setSource(s.id)}>
                <Icon size={16} />
                <span>
                  {s.title}
                  <small>{s.hint}</small>
                </span>
              </button>
            );
          })}
        </div>

        {source === 'scene' && (
          <p className="field-note">
            {selectedName
              ? t('wizard.what.sceneSelected', { name: selectedName })
              : sceneCount === 1
                ? t('wizard.what.sceneSingle')
                : sceneCount
                  ? t('wizard.what.scenePick')
                  : t('wizard.what.sceneEmpty')}
          </p>
        )}

        {source === 'job' && (
          <div className="forge-list">
            {genJobs === null && <p className="field-note"><Loader2 size={12} className="spin" /> {t('wizard.what.jobsLoading')}</p>}
            {genJobs?.length === 0 && <p className="field-note">{t('wizard.what.jobsEmpty')}</p>}
            {genJobs?.map((j) => (
              <label key={j.id} className={'forge-list-row ' + (jobId === j.id ? 'active' : '')}>
                <input type="radio" name="forge-gen-job" checked={jobId === j.id} onChange={() => setJobId(j.id)} />
                {j.concept?.url || j.preparedConcept?.reference?.url ? <img src={j.concept?.url || j.preparedConcept?.reference?.url} alt="" /> : <span className="forge-thumb-empty"><Box size={14} /></span>}
                <span>
                  {j.prompt?.slice(0, 60) || t('wizard.what.jobFromImage')}
                  <small>{formatDate(j.createdAt)}</small>
                </span>
              </label>
            ))}
          </div>
        )}

        {source === 'upload' && (
          <>
            <Dropzone
              accept=".glb,.fbx,.obj,.stl"
              title={file ? file.name : t('wizard.what.dropTitle')}
              hint={file ? t('wizard.what.dropHintChange') : t('wizard.what.dropHint')}
              onFiles={([f]) => setFile(f)}
            />
            {file && extOf(file.name) === '.fbx' && (
              <p className="field-note">{t('wizard.what.fbxNote')}</p>
            )}
            {file && extOf(file.name) === '.stl' && (
              <p className="field-note">{t('wizard.what.stlNote')}</p>
            )}
          </>
        )}

        {source === 'params' && <p className="field-note">{t('wizard.what.paramsNote')}</p>}
      </Section>

      <Section title={t('wizard.what.nameTitle')}>
        <input
          className="forge-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setNameTouched(true);
          }}
          maxLength={120}
          aria-label={t('wizard.what.nameAria')}
        />
      </Section>

      <div className="generate-area">
        <button type="button" className="primary generate" disabled={!canCreate} onClick={create}>
          {busy ? <Loader2 size={16} className="spin" /> : <Sparkles size={16} />}
          {busy || (source === 'params' ? t('wizard.what.toGenerator') : t('wizard.what.createPassport'))}
          <ArrowRight size={16} />
        </button>
        {offline && source !== 'params' && <p>{t('wizard.what.offline')}</p>}
      </div>

      <ExistingAssets list={existing} onOpen={(a) => openAsset(a).catch((e) => notify(e.message, true))} onTrash={trash} />
    </>
  );
}

function ExistingAssets({ list, current, onOpen, onTrash }) {
  const t = useT();
  const items = (list || []).filter((a) => a.id !== current);
  if (!items.length) return null;
  return (
    <Section title={t('wizard.what.resumeTitle')} aside={<span className="counter">{items.length}</span>}>
      <div className="forge-list">
        {items.slice(0, 12).map((a) => {
          const thumb = typeof a.thumbnail === 'string' ? (a.thumbnail.startsWith('/api/') ? a.thumbnail : outputUrl(a.id, a.thumbnail)) : null;
          const Icon = KIND_ICONS[kindFromAsset(a)] || FolderOpen;
          return (
            <div key={a.id} className="forge-list-row forge-asset-row">
              <button type="button" className="forge-asset-open" onClick={() => onOpen(a)}>
                {thumb ? <img src={thumb} alt="" /> : <span className="forge-thumb-empty"><Icon size={14} /></span>}
                <span>
                  {a.name || t('wizard.what.untitled')}
                  <small>
                    {kindInfo(kindFromAsset(a))?.title || a.kind} · {formatDate(a.updated)}
                    {a.lastStatus ? ' · ' + statusLabel(a.lastStatus) : ''}
                  </small>
                </span>
              </button>
              {onTrash && <TrashButton label={t('wizard.what.trashLabel', { name: a.name || t('wizard.what.assetFallback') })} onConfirm={() => onTrash(a)} />}
            </div>
          );
        })}
      </div>
      {items.length > 12 && <p className="field-note">{t('wizard.what.shownLast', { n: items.length })}</p>}
    </Section>
  );
}

