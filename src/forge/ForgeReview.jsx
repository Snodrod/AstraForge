// Review: the owner reviews every generated character next to its concept, approves it or
// sends it back for rework (docs/forge/REVIEW.md). Rows come from GET /api/forge/review; the
// decision is stored in the asset manifest (PATCH /assets/:id, qa.decision).
import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  RefreshCw, Eye, Maximize2, Check, Wrench, Ban, Save, X, ChevronLeft, ChevronRight, RotateCcw, Search, ImageOff,
  ExternalLink, Loader2,
} from 'lucide-react';
import * as forgeApi from './forge-api.js';
import { Chips, Notice, ProgressBar, useForge } from './ForgeUi.jsx';
import {
  BUILD_FILTERS, REWORK_PRESETS, DECISION_TITLES, filterItems, reviewCounts, chipCounts, distinct, suggestUnit, hasReason,
  toggleReason, moveSelection, neighbourId, reviewKeyAction, percent, decisionPatch, withDecision, roleTitle, factionTitle, rowKey,
} from './review-core.js';
import { t, useT } from '../i18n/index.js';
import './forge-review.css';

const STORE_KEY = 'astraforge.forge.review.v1';
const DEFAULT_FILTERS = { build: 'all', decision: 'all', faction: 'all', role: 'all', query: '' };

function readFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {};
    return { ...DEFAULT_FILTERS, ...saved, query: '' };
  } catch {
    return DEFAULT_FILTERS;
  }
}

function writeFilters(filters) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ build: filters.build, decision: filters.decision, faction: filters.faction, role: filters.role }));
  } catch {
    /* per-viewer convenience only */
  }
}

const PILL_TONE = { good: 'good', bad: 'bad', warn: 'warn', busy: 'busy', muted: 'muted' };

function loadError(e) {
  if (e?.notReady || e?.code === 'not_found' || e?.status === 404) {
    return t('forge.review.noRoute');
  }
  return e?.message || t('forge.review.loadFailed');
}

// ---------------------------------------------------------------------------- small parts

function Tile({ url, label, className = '', onOpen }) {
  const t = useT();
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [url]);
  if (!url || broken) {
    return (
      <div className={'rv-tile empty ' + className} title={t('forge.review.noImageOf', { label })}>
        <ImageOff size={14} />
        <span>{label}</span>
      </div>
    );
  }
  return (
    <figure className={'rv-tile ' + className}>
      <button type="button" className="rv-tile-btn" onClick={onOpen} aria-label={t('forge.review.openLargeOf', { label })}>
        <img src={url} alt={label} loading="lazy" decoding="async" onError={() => setBroken(true)} />
      </button>
      <figcaption>{label}</figcaption>
    </figure>
  );
}

function TearLine({ tear }) {
  const t = useT();
  if (!tear) return null;
  const { worst } = tear;
  return (
    <details className={'rv-tear ' + (tear.ok ? 'ok' : 'bad')}>
      <summary>
        <span>{t('forge.review.tear.title')}</span>
        <b>{percent(worst.tornPct)}</b>
        <small>
          {t('forge.review.tear.clip', { clip: worst.clip })}{Number.isFinite(worst.limitPct) ? ' · ' + t('forge.review.tear.limit', { pct: percent(worst.limitPct) }) : ''}
          {Number.isFinite(tear.worstRobe?.robePct) && tear.worstRobe.robePct > 0 ? ' · ' + t('forge.review.tear.robeUpTo', { pct: percent(tear.worstRobe.robePct) }) : ''}
        </small>
      </summary>
      <table>
        <thead>
          <tr><th>{t('forge.review.tear.colClip')}</th><th>{t('forge.review.tear.colTorn')}</th><th>{t('forge.review.tear.colRobe')}</th><th>{t('forge.review.tear.colFrames')}</th></tr>
        </thead>
        <tbody>
          {tear.clips.map((c) => (
            <tr key={c.clip} className={c.ok ? 'ok' : 'bad'}>
              <td>{c.clip}</td>
              <td>
                <i style={{ width: Math.max(2, Math.min(100, (c.tornPct || 0) * 2)) + '%' }} />
                {percent(c.tornPct)}
              </td>
              <td>{percent(c.robePct)}</td>
              <td>{c.overFrames ?? '—'}/{c.frames ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function DecisionControls({ item, draft, reworkOpen, saving, onDraft, onDecide, onClear, onOpenRework, onCloseRework, onUnitCommit }) {
  const t = useT();
  const current = item.decision?.decision || null;
  const blocked = !item.canApprove;
  return (
    <div className="rv-decision" onClick={(e) => e.stopPropagation()}>
      {item.decision && (
        <p className={'rv-verdict ' + current}>
          <b>{DECISION_TITLES[current]}</b>
          {item.decision.game_unit ? <span> · {t('forge.review.unitShort', { unit: item.decision.game_unit })}</span> : null}
          {item.decision.comment ? <em>{item.decision.comment}</em> : null}
        </p>
      )}
      <div className="rv-buttons">
        <button type="button" className={'rv-btn approve ' + (current === 'approved' ? 'on' : '')} disabled={saving || blocked} onClick={() => onDecide(item, 'approved')}
          title={blocked ? t('forge.review.approveBlocked') : t('forge.review.approveKey')}>
          <Check size={13} />
          {t('forge.review.approve')}
        </button>
        <button type="button" className={'rv-btn rework ' + (current === 'rework' ? 'on' : '')} disabled={saving} onClick={() => onOpenRework(item)} title={t('forge.review.reworkKey')}>
          <Wrench size={13} />
          {t('forge.review.decision.rework')}
        </button>
        <button type="button" className={'rv-btn reject ' + (current === 'rejected' ? 'on' : '')} disabled={saving} onClick={() => onDecide(item, 'rejected')} title={t('forge.review.rejectTitle')}>
          <Ban size={13} />
          {t('forge.review.decision.rejected')}
        </button>
        {item.decision && (
          <button type="button" className="icon-btn rv-reset" disabled={saving} onClick={() => onClear(item)} title={t('forge.review.clear')} aria-label={t('forge.review.clear')}>
            <RotateCcw size={13} />
          </button>
        )}
        {saving && <Loader2 size={13} className="spin" />}
      </div>
      {reworkOpen && (
        <div className="rv-rework">
          <span className="rv-rework-title">{t('forge.review.whatsWrong')}</span>
          <div className="forge-chips rv-presets" role="group" aria-label={t('forge.review.whatsWrongAria')}>
            {REWORK_PRESETS.map((p) => (
              <button key={p} type="button" role="checkbox" aria-checked={hasReason(draft.comment, p)} className={hasReason(draft.comment, p) ? 'active' : ''}
                onClick={() => onDraft({ comment: toggleReason(draft.comment, p) })}>
                {p}
              </button>
            ))}
          </div>
          <textarea
            rows={2}
            autoFocus
            value={draft.comment}
            placeholder={t('forge.review.commentPlaceholder')}
            aria-label={t('forge.review.commentAria')}
            onChange={(e) => onDraft({ comment: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.stopPropagation();
                onCloseRework();
              } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                onDecide(item, 'rework');
              }
            }}
          />
          <div className="rv-buttons">
            <button type="button" className="rv-btn rework on" disabled={saving} onClick={() => onDecide(item, 'rework')}>
              <Wrench size={13} />
              {t('forge.review.sendRework')}
            </button>
            <button type="button" className="forge-link" onClick={onCloseRework}>{t('forge.review.cancel')}</button>
          </div>
        </div>
      )}
      <label className="rv-unit">
        <span>{t('forge.review.unit')}</span>
        <input
          value={draft.unit}
          placeholder={suggestUnit(item.name) || t('forge.review.unitPlaceholder')}
          aria-label={t('forge.review.unitAria')}
          maxLength={80}
          onChange={(e) => onDraft({ unit: e.target.value })}
          onBlur={() => onUnitCommit(item)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') e.currentTarget.blur();
          }}
        />
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------- card

const Card = memo(function Card({ item, selected, unit, comment, reworkOpen, saving, actions }) {
  const t = useT();
  const key = rowKey(item);
  const draft = { unit, comment };
  const urls = item.urls || {};
  const rigFailed = item.status === 'failed_forge' && /^rig_/.test(item.failedStep || '');
  const tone = PILL_TONE[item.tone] || 'muted';
  const verdict = item.decision?.decision || 'none';
  return (
    <article
      className={`rv-card tone-${tone} verdict-${verdict} ${selected ? 'selected' : ''}`}
      data-key={key}
      aria-current={selected ? 'true' : undefined}
      onClick={() => actions.select(key)}
    >
      <div className="rv-media">
        <Tile className="concept" url={urls.concept} label={t('forge.review.img.concept')} onOpen={() => actions.open(key)} />
        <Tile className="compare" url={urls.compare} label={t('forge.review.img.paint')} onOpen={() => actions.open(key)} />
        {rigFailed ? (
          <>
            <Tile className="a" url={urls.markers} label={t('forge.review.img.skeleton')} onOpen={() => actions.open(key)} />
            <Tile className="b" url={urls.poses} label={t('forge.review.img.pose')} onOpen={() => actions.open(key)} />
          </>
        ) : (
          <>
            <Tile className="a" url={urls.motion} label={t('forge.review.img.motion')} onOpen={() => actions.open(key)} />
            <Tile className="b" url={urls.sprites} label={t('forge.review.img.sprites')} onOpen={() => actions.open(key)} />
          </>
        )}
      </div>
      <header className="rv-head">
        <h4 title={item.name}>{item.name}</h4>
        <span className={'forge-pill ' + tone} title={item.reason}>{item.label}</span>
      </header>
      <div className="rv-meta">
        {roleTitle(item.role)} · {factionTitle(item.faction)}
        {item.version ? ` · ${item.version}` : ''}
      </div>
      <p className="rv-reason">{item.reason}</p>
      <TearLine tear={item.tear} />
      <div className="rv-actions" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="secondary small" disabled={!urls.glb} onClick={() => actions.show(item)}
          title={urls.glb ? (item.glbStep && item.glbStep !== 'motion_style' ? t('forge.review.modelAfter', { step: item.glbStep }) : t('forge.review.loadModel')) : t('forge.review.noModel')}>
          <Eye size={13} />
          {t('forge.review.showInScene')}
        </button>
        <button type="button" className="secondary small" onClick={() => actions.open(key)} title={t('forge.review.openLargeTitle')}>
          <Maximize2 size={13} />
          {t('forge.review.openLarge')}
        </button>
      </div>
      {item.reviewable && (
        <DecisionControls
          item={item}
          draft={draft}
          reworkOpen={reworkOpen}
          saving={saving}
          onDraft={(patch) => actions.draft(key, patch)}
          onDecide={actions.decide}
          onClear={actions.clear}
          onOpenRework={actions.openRework}
          onCloseRework={actions.closeRework}
          onUnitCommit={actions.unitCommit}
        />
      )}
    </article>
  );
});

// ---------------------------------------------------------------------------- large view

function ImagePanel({ url, label, className = '' }) {
  const t = useT();
  const [broken, setBroken] = useState(false);
  useEffect(() => setBroken(false), [url]);
  return (
    <figure className={'rv-big ' + className}>
      <figcaption>
        {label}
        {url && !broken && (
          <a href={url} target="_blank" rel="noreferrer" title={t('forge.review.fullSize')} aria-label={t('forge.review.newTabOf', { label })}>
            <ExternalLink size={12} />
          </a>
        )}
      </figcaption>
      {url && !broken ? <img src={url} alt={label} decoding="async" onError={() => setBroken(true)} /> : <div className="rv-big-empty"><ImageOff size={22} />{t('forge.review.noImage')}</div>}
    </figure>
  );
}

function ReviewModal({ item, position, total, draft, reworkOpen, saving, actions, onClose, onPrev, onNext }) {
  const t = useT();
  const closeRef = useRef(null);
  useEffect(() => closeRef.current?.focus(), []);
  const key = rowKey(item);
  const urls = item.urls || {};
  const rigFailed = item.status === 'failed_forge' && /^rig_/.test(item.failedStep || '');
  return createPortal(
    <div className="rv-modal-backdrop" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="rv-modal" role="dialog" aria-modal="true" aria-label={t('forge.review.dialogAria', { name: item.name })}>
        <header className="rv-modal-head">
          <div>
            <h3>{item.name}</h3>
            <span className="rv-meta">
              {roleTitle(item.role)} · {factionTitle(item.faction)}
              {item.version ? ` · ${item.version}` : ''}
            </span>
          </div>
          <span className={'forge-pill ' + (PILL_TONE[item.tone] || 'muted')}>{item.label}</span>
          <div className="rv-modal-nav">
            <button type="button" className="icon-btn" onClick={onPrev} disabled={position <= 0} aria-label={t('forge.review.prevAria')} title={t('forge.review.prevTitle')}><ChevronLeft size={16} /></button>
            <small>{t('forge.review.position', { pos: position + 1, total })}</small>
            <button type="button" className="icon-btn" onClick={onNext} disabled={position >= total - 1} aria-label={t('forge.review.nextAria')} title={t('forge.review.nextTitle')}><ChevronRight size={16} /></button>
            <button type="button" className="icon-btn" ref={closeRef} onClick={onClose} aria-label={t('forge.review.close')} title={t('forge.review.closeTitle')}><X size={16} /></button>
          </div>
        </header>
        <div className="rv-modal-body">
          <div className="rv-big-layout">
            {/* concept and paint compare first: that is the comparison; the rest scrolls below */}
            <div className="rv-big-main">
              <ImagePanel url={urls.concept} label={t('forge.review.img.concept')} />
              <ImagePanel url={urls.compare} label={t('forge.review.img.paintBig')} />
              {rigFailed && <ImagePanel url={urls.markers} label={t('forge.review.img.skeletonBig')} />}
              {rigFailed && urls.poses && <ImagePanel url={urls.poses} label={t('forge.review.img.deformTest')} />}
              {(!rigFailed || urls.motion) && <ImagePanel url={urls.motion} label={t('forge.review.img.motion')} />}
            </div>
            <ImagePanel className="rv-big-sprites" url={urls.sprites} label={t('forge.review.img.sprites')} />
          </div>
          <aside className="rv-side">
            <p className="rv-reason">{item.reason}</p>
            {item.notes && <p className="rv-notes"><b>{t('forge.review.conceptNote')}</b> {item.notes}</p>}
            <TearLine tear={item.tear} />
            <div className="rv-actions">
              <button type="button" className="secondary small" disabled={!urls.glb} onClick={() => actions.show(item)}>
                <Eye size={13} />
                {t('forge.review.showInScene')}
              </button>
            </div>
            {item.reviewable && (
              <DecisionControls
                item={item}
                draft={draft}
                reworkOpen={reworkOpen}
                saving={saving}
                onDraft={(patch) => actions.draft(key, patch)}
                onDecide={actions.decide}
                onClear={actions.clear}
                onOpenRework={actions.openRework}
                onCloseRework={actions.closeRework}
                onUnitCommit={actions.unitCommit}
              />
            )}
            <p className="rv-hint">{t('forge.review.modalHint')}</p>
          </aside>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------- the view

export default function ForgeReview() {
  const tr = useT();
  const { offline, server, notify, loadGlb } = useForge();
  // The studio re-creates notify / loadGlb on its renders: refs keep the callbacks below stable.
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const loadGlbRef = useRef(loadGlb);
  loadGlbRef.current = loadGlb;
  const [phase, setPhase] = useState('loading'); // loading | ready | error
  const [problem, setProblem] = useState('');
  const [data, setData] = useState(null);
  const [items, setItems] = useState([]);
  const [filters, setFilters] = useState(readFilters);
  const [selected, setSelected] = useState(null);
  const [reworkFor, setReworkFor] = useState(null);
  const [zoomed, setZoomed] = useState(null);
  const [drafts, setDrafts] = useState({});
  const [saving, setSaving] = useState({});
  const [exporting, setExporting] = useState(false);
  const [exported, setExported] = useState(null);
  const gridRef = useRef(null);

  const readyRef = useRef(false);
  const load = useCallback(async () => {
    if (!readyRef.current) setPhase('loading');
    try {
      const review = await forgeApi.getReview();
      readyRef.current = true;
      setData(review);
      setItems(Array.isArray(review.items) ? review.items : []);
      setPhase('ready');
      setProblem('');
    } catch (e) {
      if (readyRef.current) {
        notifyRef.current(loadError(e), true); // the old list stays on screen
      } else {
        setProblem(loadError(e));
        setPhase('error');
      }
    }
  }, []);

  useEffect(() => {
    if (!offline) load();
  }, [offline, load]);

  useEffect(() => writeFilters(filters), [filters]);

  const visible = useMemo(() => filterItems(items, filters), [items, filters]);
  const counts = useMemo(() => reviewCounts(items), [items]);
  const buildCounts = useMemo(() => chipCounts(items, filters, 'build'), [items, filters]);
  const factions = useMemo(() => distinct(items, 'faction'), [items]);
  const roles = useMemo(() => distinct(items, 'role').filter((r) => r !== 'skip'), [items]);

  // Refs let the one keyboard listener always see the current state.
  const live = useRef({});
  live.current = { items, visible, selected, reworkFor, zoomed, drafts, saving };

  const draftOf = useCallback((item) => {
    const d = live.current.drafts[rowKey(item)] || {};
    return {
      unit: d.unit ?? item.decision?.game_unit ?? suggestUnit(item.name),
      comment: d.comment ?? item.decision?.comment ?? '',
    };
  }, []);

  const select = useCallback((key) => setSelected(key), []);

  const setDraft = useCallback((key, patch) => setDrafts((all) => ({ ...all, [key]: { ...all[key], ...patch } })), []);

  const store = useCallback((assetId, qa) => setItems((list) => list.map((i) => (i.assetId === assetId ? withDecision(i, qa) : i))), []);

  const patchQa = useCallback(async (item, body) => {
    const key = rowKey(item);
    setSaving((s) => ({ ...s, [key]: true }));
    try {
      const manifest = await forgeApi.patchAsset(item.assetId, body);
      store(item.assetId, manifest?.qa);
      return true;
    } catch (e) {
      notifyRef.current(e.message || t('forge.review.saveFailed'), true);
      return false;
    } finally {
      setSaving((s) => ({ ...s, [key]: false }));
    }
  }, [store]);

  const decide = useCallback(async (item, decision) => {
    if (!item?.assetId) return;
    if (decision === 'approved' && !item.canApprove) {
      notifyRef.current(t('forge.review.cannotApprove', { label: item.label.toLowerCase() }), true);
      return;
    }
    const draft = draftOf(item);
    const ids = live.current.visible.map(rowKey);
    const key = rowKey(item);
    const ok = await patchQa(item, decisionPatch(decision, { comment: draft.comment, unit: draft.unit }));
    if (!ok) return;
    setReworkFor((r) => (r === key ? null : r));
    // Keep moving down the list: the next card is selected (the decided one may leave the filter),
    // and the large view follows.
    const next = neighbourId(ids, key);
    if (next) {
      setSelected(next);
      if (live.current.zoomed === key) setZoomed(next);
    }
  }, [draftOf, patchQa]);

  const clear = useCallback((item) => patchQa(item, decisionPatch(null)), [patchQa]);

  const unitCommit = useCallback((item) => {
    if (!item.decision) return;
    const unit = draftOf(item).unit.trim();
    if (unit !== (item.decision.game_unit || '')) patchQa(item, { qa: { game_unit: unit } });
  }, [draftOf, patchQa]);

  const openRework = useCallback((item) => {
    setSelected(rowKey(item));
    setReworkFor(rowKey(item));
  }, []);

  const show = useCallback((item) => {
    if (item.urls?.glb) loadGlbRef.current(item.urls.glb, { name: item.name, version: item.version });
  }, []);

  const actions = useMemo(() => ({
    select, decide, clear, unitCommit, openRework, show,
    draft: setDraft,
    open: (key) => {
      setSelected(key);
      setZoomed(key);
    },
    closeRework: () => setReworkFor(null),
  }), [select, decide, clear, unitCommit, openRework, show, setDraft]);

  // ---- keyboard: arrows move, A approves, R opens the rework panel, Enter opens the large view
  useEffect(() => {
    const onKey = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      const { visible: list, selected: current, zoomed: zoom } = live.current;
      if (zoom && e.code === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        setZoomed(null);
        return;
      }
      const action = reviewKeyAction(e.code);
      if (!action) return;
      if (t?.tagName === 'BUTTON' && (e.code === 'Enter' || e.code === 'NumpadEnter')) return; // a focused button keeps its own Enter
      // The studio has its own R / arrow shortcuts: this view takes them while it is open.
      e.preventDefault();
      e.stopPropagation();
      const keys = list.map(rowKey);
      const item = list.find((i) => rowKey(i) === current);
      if (action === 'move') {
        const columns = zoom ? 1 : (gridRef.current ? getComputedStyle(gridRef.current).gridTemplateColumns.split(' ').filter(Boolean).length : 1);
        const next = moveSelection(keys, current, e.code, columns);
        if (next) {
          setSelected(next);
          if (zoom) setZoomed(next);
        }
      } else if (!item) {
        if (keys.length) setSelected(keys[0]);
      } else if (action === 'approve') {
        decide(item, 'approved');
      } else if (action === 'rework') {
        if (item.reviewable) openRework(item);
      } else if (action === 'open') {
        setZoomed(rowKey(item));
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [decide, openRework]);

  // The selected card stays in view while moving with the arrows.
  useEffect(() => {
    if (!selected || !gridRef.current) return;
    const el = [...gridRef.current.querySelectorAll('[data-key]')].find((n) => n.dataset.key === selected);
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selected]);

  const setFilter = (patch) => setFilters((f) => ({ ...f, ...patch }));
  const toggleDecision = (id) => setFilter({ decision: filters.decision === id ? 'all' : id });

  const exportFile = async () => {
    setExporting(true);
    try {
      const r = await forgeApi.exportReview();
      setExported(r);
      notifyRef.current(t('forge.review.exportedToast', { n: r.count, path: r.path }));
    } catch (e) {
      notifyRef.current(e.message, true);
    } finally {
      setExporting(false);
    }
  };

  if (offline) {
    return (
      <div className="forge-review">
        <div className="rv-body"><Notice tone="info"><p>{server?.message || tr('forge.review.offline')}</p></Notice></div>
      </div>
    );
  }

  const zoomItem = zoomed ? visible.find((i) => rowKey(i) === zoomed) || items.find((i) => rowKey(i) === zoomed) : null;
  const zoomPos = zoomItem ? visible.findIndex((i) => rowKey(i) === zoomed) : -1;
  const done = counts.total - counts.pending;
  const counter = (id, label, value) => (
    <button type="button" className={`rv-count ${id} ${filters.decision === id ? 'active' : ''}`} onClick={() => toggleDecision(id)} aria-pressed={filters.decision === id}
      title={filters.decision === id ? tr('forge.review.showAll') : tr('forge.review.showOnly')}>
      <b>{value}</b>
      <span>{label}</span>
    </button>
  );

  return (
    <div className="forge-review">
      <div className="rv-top">
        <div className="rv-counter" role="group" aria-label={tr('forge.review.totalsAria')} aria-live="polite">
          {counter('approved', tr('forge.review.count.approved'), counts.approved)}
          {counter('rework', tr('forge.review.count.rework'), counts.rework)}
          {counter('rejected', tr('forge.review.count.rejected'), counts.rejected)}
          {counter('pending', tr('forge.review.count.pending'), counts.pending)}
        </div>
        <ProgressBar value={counts.total ? (done / counts.total) * 100 : 0} />
        <div className="rv-row">
          <Chips
            label={tr('forge.review.buildState')}
            value={filters.build}
            onChange={(build) => setFilter({ build })}
            options={BUILD_FILTERS.map((f) => ({ id: f.id, title: f.title, hint: f.hint, count: buildCounts[f.id] }))}
          />
          <button type="button" className="secondary small rv-save" onClick={exportFile} disabled={exporting || phase !== 'ready'}
            title={tr('forge.review.exportTitle')}>
            {exporting ? <Loader2 size={13} className="spin" /> : <Save size={13} />}
            {tr('forge.review.export')}
          </button>
        </div>
        <div className="rv-tools">
          <label className="forge-search">
            <Search size={12} />
            <input value={filters.query} onChange={(e) => setFilter({ query: e.target.value })} placeholder={tr('forge.review.search')} aria-label={tr('forge.review.search')} />
          </label>
          <select value={filters.faction} onChange={(e) => setFilter({ faction: e.target.value })} aria-label={tr('forge.review.faction')}>
            <option value="all">{tr('forge.review.allFactions')}</option>
            {factions.map((f) => <option key={f} value={f}>{factionTitle(f)}</option>)}
          </select>
          <select value={filters.role} onChange={(e) => setFilter({ role: e.target.value })} aria-label={tr('forge.review.role')}>
            <option value="all">{tr('forge.review.allRoles')}</option>
            {roles.map((r) => <option key={r} value={r}>{roleTitle(r)}</option>)}
          </select>
          <button type="button" className="icon-btn" onClick={load} disabled={phase === 'loading'} title={tr('forge.review.refresh')} aria-label={tr('forge.review.refresh')}>
            <RefreshCw size={14} className={phase === 'loading' ? 'spin' : ''} />
          </button>
        </div>
        {exported && <small className="rv-written">{tr('forge.review.written', { n: exported.count, path: exported.path })}</small>}
      </div>
      <div className="panel-scroll forge-scroll rv-body">
        {phase === 'loading' && <p className="field-note"><Loader2 size={12} className="spin" /> {tr('forge.review.loading')}</p>}
        {phase === 'error' && (
          <Notice tone="warn">
            <p>{problem}</p>
            <button type="button" className="forge-link" onClick={load}><RefreshCw size={12} />{tr('forge.review.retry')}</button>
          </Notice>
        )}
        {phase === 'ready' && data?.batch?.source === 'library' && (
          <Notice>{tr('forge.review.libraryFallback')}</Notice>
        )}
        {phase === 'ready' && !items.length && <p className="field-note">{tr('forge.review.empty')}</p>}
        {phase === 'ready' && items.length > 0 && !visible.length && <p className="field-note">{tr('forge.review.noMatch')}</p>}
        <div className="rv-grid" ref={gridRef} aria-label={tr('forge.review.gridAria')}>
          {visible.map((item) => {
            const key = rowKey(item);
            const draft = draftOf(item);
            return (
              <Card
                key={key}
                item={item}
                selected={selected === key}
                unit={draft.unit}
                comment={draft.comment}
                reworkOpen={reworkFor === key && zoomed !== key}
                saving={!!saving[key]}
                actions={actions}
              />
            );
          })}
        </div>
        {phase === 'ready' && visible.length > 0 && (
          <p className="rv-foot">
            {tr('forge.review.foot', { shown: visible.length, total: items.length })}
          </p>
        )}
      </div>
      {zoomItem && (
        <ReviewModal
          item={zoomItem}
          position={Math.max(0, zoomPos)}
          total={visible.length || 1}
          draft={draftOf(zoomItem)}
          reworkOpen={reworkFor === rowKey(zoomItem)}
          saving={!!saving[rowKey(zoomItem)]}
          actions={actions}
          onClose={() => setZoomed(null)}
          onPrev={() => {
            const next = moveSelection(visible.map(rowKey), zoomed, 'ArrowLeft');
            setSelected(next);
            setZoomed(next);
          }}
          onNext={() => {
            const next = moveSelection(visible.map(rowKey), zoomed, 'ArrowRight');
            setSelected(next);
            setZoomed(next);
          }}
        />
      )}
    </div>
  );
}
