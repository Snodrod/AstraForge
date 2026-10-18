// Shared building blocks of the Forge wizard.
import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { Check, X, Dices, Upload, Eye, ExternalLink, TriangleAlert, Info, Loader2, Trash2 } from 'lucide-react';
import {
  paramType, coerceParam, enumLabel, normalizeChecks, checksSummary, imageOutputs, imageLabel,
  findOutput, statusLabel, isActiveStatus, jobProgress, currentJobStep, jobError, stepTitle,
  formatCount, randomSeed, formatDate, humanizeError, draftNumber, commitNumber, niceStep, paramLabel, paramHint,
} from './forge-core.js';
import { outputUrl } from './forge-api.js';
import { useT } from '../i18n/index.js';

export const ForgeContext = createContext(null);
export const useForge = () => useContext(ForgeContext);

export function Section({ title, aside, children, className = '' }) {
  return (
    <section className={'section ' + className}>
      {title && (
        <div className="section-heading">
          <h3>{title}</h3>
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}

export function Notice({ tone = 'info', children }) {
  const Icon = tone === 'warn' || tone === 'error' ? TriangleAlert : Info;
  return (
    <div className={'forge-notice ' + tone} role={tone === 'error' ? 'alert' : undefined}>
      <Icon size={14} />
      <div>{children}</div>
    </div>
  );
}

export function ProgressBar({ value }) {
  return (
    <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value || 0)}>
      <i style={{ width: Math.max(0, Math.min(100, value || 0)) + '%' }} />
    </div>
  );
}

export function StatusPill({ status }) {
  useT();
  const tone = status === 'completed' || status === 'done' ? 'good' : status === 'failed' ? 'bad' : status === 'cancelled' ? 'muted' : 'busy';
  return <span className={'forge-pill ' + tone}>{statusLabel(status)}</span>;
}

export function Chips({ options, value, onChange, multiple = false, disabled, label }) {
  const isOn = (id) => (multiple ? (value || []).includes(id) : value === id);
  const toggle = (id) => {
    if (!multiple) return onChange(id);
    const set = new Set(value || []);
    if (set.has(id)) set.delete(id);
    else set.add(id);
    onChange([...set]);
  };
  return (
    <div className="forge-chips" role={multiple ? 'group' : 'radiogroup'} aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          className={isOn(o.id) ? 'active' : ''}
          role={multiple ? 'checkbox' : 'radio'}
          aria-checked={isOn(o.id)}
          disabled={disabled || o.disabled}
          title={o.hint}
          onClick={() => toggle(o.id)}
        >
          {o.title}
          {o.count !== undefined && <small>{o.count}</small>}
        </button>
      ))}
    </div>
  );
}

const textOf = (v) => (v === null || v === undefined || v === '' ? '' : String(v));

/**
 * Number field that keeps what the user types: the value is passed up unclamped on every
 * keystroke and rounded/clamped to def.min/max only on blur (or Enter).
 */
export function NumberInput({ def = {}, value, onChange, disabled, placeholder, integer, ariaLabel, className }) {
  const [text, setText] = useState(textOf(value));
  const editing = useRef(false);
  useEffect(() => {
    if (!editing.current) setText(textOf(value));
  }, [value]);
  const commit = () => {
    editing.current = false;
    const v = commitNumber(def, text);
    if (v === undefined && draftNumber(def, text).valid === false) {
      setText(textOf(value));
      return;
    }
    setText(textOf(v));
    if (v !== value) onChange(v);
  };
  return (
    <input
      type="text"
      inputMode={integer ? 'numeric' : 'decimal'}
      className={className}
      aria-label={ariaLabel}
      value={text}
      placeholder={placeholder}
      disabled={disabled}
      onFocus={() => {
        editing.current = true;
      }}
      onChange={(e) => {
        editing.current = true;
        setText(e.target.value);
        const draft = draftNumber(def, e.target.value);
        if (draft.valid) onChange(draft.value);
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur();
      }}
    />
  );
}

const SEED_DEF = { type: 'integer', min: 0, max: 2147483647 };

export function SeedInput({ value, onChange, label, disabled, def }) {
  const t = useT();
  return (
    <label className="forge-field">
      <span>{label || t('forge.ui.seed')}</span>
      <div className="forge-seed">
        <NumberInput def={{ ...SEED_DEF, ...(def || {}), nullable: false }} integer value={value} disabled={disabled} onChange={onChange} />
        <button type="button" className="icon-btn" title={t('forge.ui.randomSeed')} aria-label={t('forge.ui.randomSeed')} disabled={disabled} onClick={() => onChange(randomSeed())}>
          <Dices size={15} />
        </button>
      </div>
    </label>
  );
}

/**
 * Two-click trash action (DELETE moves a passport to data/library/trash, nothing is erased):
 * the first click arms the button for a few seconds, the second one confirms.
 */
export function TrashButton({ onConfirm, label: labelProp, disabled, small = true }) {
  const t = useT();
  const label = labelProp || t('forge.ui.trash');
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!armed) return undefined;
    const timer = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [armed]);
  return (
    <button
      type="button"
      className={'forge-trash ' + (armed ? 'armed ' : '') + (small ? 'icon-btn' : 'secondary small')}
      title={armed ? t('forge.ui.trashArmed') : t('forge.ui.trashTitle', { label })}
      aria-label={armed ? t('forge.ui.trashConfirm') : label}
      disabled={disabled || busy}
      onClick={async (e) => {
        e.stopPropagation();
        if (!armed) {
          setArmed(true);
          return;
        }
        setBusy(true);
        try {
          await onConfirm();
        } finally {
          setBusy(false);
          setArmed(false);
        }
      }}
    >
      {busy ? <Loader2 size={13} className="spin" /> : <Trash2 size={13} />}
      {armed && <span>{t('forge.ui.trashSure')}</span>}
      {!armed && !small && <span>{label}</span>}
    </button>
  );
}

/**
 * One parameter control generated from a registry description. `step` (optional) is the step id:
 * with it the label and help come from the dictionary (param.<step>.<name>) when there is an entry.
 */
export function ParamField({ step: stepId, name, def = {}, value, onChange, disabled }) {
  const t = useT();
  const type = paramType(def);
  const title = paramLabel(stepId, name, def);
  const help = paramHint(stepId, name, def) || undefined;
  const current = value === undefined ? def.default : value;
  if (/^seed$/i.test(name) && (type === 'integer' || type === 'number')) {
    return <SeedInput value={current} onChange={onChange} label={title} disabled={disabled} def={def} />;
  }
  if (type === 'boolean') {
    return (
      <label className="forge-check" title={help}>
        <input type="checkbox" checked={!!current} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
        <span>{title}</span>
      </label>
    );
  }
  if (type === 'enum') {
    return (
      <label className="forge-field" title={help}>
        <span>{title}</span>
        <select value={String(current)} disabled={disabled} onChange={(e) => onChange(coerceParam(def, e.target.value))}>
          {def.enum.map((v) => (
            <option key={String(v)} value={String(v)}>{enumLabel(v)}</option>
          ))}
        </select>
      </label>
    );
  }
  if (type === 'integer' || type === 'number') {
    // Nullable numbers ("empty = by class") stay empty until the user types a value.
    const nullable = def.nullable || def.default === null;
    const ranged = Number.isFinite(def.min) && Number.isFinite(def.max) && !nullable && def.max - def.min <= 1e6;
    const step = def.step || (type === 'integer' ? 1 : ranged ? niceStep((def.max - def.min) / 100) : 0.01);
    return (
      <label className="forge-field" title={help}>
        <span>
          {title}
          {ranged && current !== null && current !== undefined && <em>{type === 'integer' ? formatCount(current) : Number(current).toFixed(2)}</em>}
        </span>
        {ranged ? (
          <input type="range" min={def.min} max={def.max} step={step} value={current ?? def.min} disabled={disabled} onChange={(e) => onChange(coerceParam(def, e.target.value))} />
        ) : (
          <NumberInput
            def={def}
            integer={type === 'integer'}
            value={current}
            placeholder={nullable ? (help && help.length < 40 ? help : t('forge.ui.auto')) : ''}
            disabled={disabled}
            onChange={onChange}
          />
        )}
      </label>
    );
  }
  if (type === 'array' || type === 'object') {
    return (
      <label className="forge-field" title={help}>
        <span>{title}</span>
        <input
          value={Array.isArray(current) ? current.join(', ') : current ? JSON.stringify(current) : ''}
          disabled={disabled || type === 'object'}
          onChange={(e) => onChange(coerceParam(def, e.target.value))}
        />
      </label>
    );
  }
  return (
    <label className="forge-field" title={help}>
      <span>{title}</span>
      <input value={current ?? ''} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

/** Remaining registry parameters, folded away. `step` is forwarded to ParamField. */
export function MoreParams({ step, defs, values, onChange, skip = [], disabled }) {
  const t = useT();
  // server/auto params are filled by the server (prop_asset, library, asset_id).
  const names = Object.keys(defs || {}).filter((n) => !skip.includes(n) && !defs[n]?.hidden && !defs[n]?.server && !defs[n]?.auto);
  if (!names.length) return null;
  return (
    <details className="forge-more">
      <summary>{t('forge.ui.allParams', { n: names.length })}</summary>
      {names.map((n) => (
        <ParamField key={n} step={step} name={n} def={defs[n]} value={values?.[n]} disabled={disabled} onChange={(v) => onChange({ ...values, [n]: v })} />
      ))}
    </details>
  );
}

export function Dropzone({ accept, multiple = false, onFiles, title, hint, disabled }) {
  const input = useRef(null);
  const [over, setOver] = useState(false);
  const pick = (files) => {
    const list = [...(files || [])];
    if (list.length) onFiles(multiple ? list : [list[0]]);
  };
  return (
    <>
      <button
        type="button"
        className={'dropzone forge-drop ' + (over ? 'over' : '')}
        disabled={disabled}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (!disabled) pick(e.dataTransfer.files);
        }}
      >
        <Upload size={20} />
        <strong>{title}</strong>
        {hint && <span>{hint}</span>}
      </button>
      <input
        ref={input}
        type="file"
        hidden
        accept={accept}
        multiple={multiple}
        onChange={(e) => {
          pick(e.target.files);
          e.target.value = '';
        }}
      />
    </>
  );
}

const CHECKS_OPEN = 6;

function CheckRow({ r }) {
  const tone = r.ok ? 'ok' : r.severity === 'warning' || r.severity === 'info' ? 'warn' : 'fail';
  const fmt = (v) => (typeof v === 'object' ? JSON.stringify(v) : String(v));
  return (
    <div className={'forge-check-row ' + tone}>
      {r.ok ? <Check size={12} /> : <X size={12} />}
      <span title={r.name}>{r.name}</span>
      {r.value !== undefined && r.value !== null && (
        <small>
          {fmt(r.value)}
          {r.limit !== undefined && r.limit !== null ? ' / ' + fmt(r.limit) : ''}
        </small>
      )}
      {r.note && <em>{r.note}</em>}
    </div>
  );
}

/** Green/red rows; long lists show only the problems and fold the rest away. */
export function ChecksList({ checks, title }) {
  const t = useT();
  const rows = normalizeChecks(checks);
  if (!rows.length) return null;
  const sum = checksSummary(rows);
  const folded = rows.length > CHECKS_OPEN;
  const visible = folded ? rows.filter((r) => !r.ok) : rows;
  return (
    <div className="forge-checks">
      <div className="forge-checks-head">
        <span>{title || t('forge.ui.checks')}</span>
        <small className={sum.ok ? 'good' : 'bad'}>
          {sum.ok ? t('forge.ui.checksOk') : t('forge.ui.checksFailed', { n: sum.failed })}
          {sum.warn ? ' · ' + t('forge.ui.checksWarn', { n: sum.warn }) : ''}
        </small>
      </div>
      {visible.map((r, i) => (
        <CheckRow key={r.name + i} r={r} />
      ))}
      {folded && (
        <details className="forge-checks-more">
          <summary>{t('forge.ui.allChecks', { n: rows.length })}</summary>
          {rows.map((r, i) => (
            <CheckRow key={r.name + i} r={r} />
          ))}
        </details>
      )}
    </div>
  );
}

export function ResultImages({ assetId, outputs, filter, large }) {
  const t = useT();
  const images = imageOutputs(outputs).filter((e) => (filter ? filter(e) : true));
  const shown = images.map((e) => ({ ...e, url: outputUrl(assetId, e.path) })).filter((e) => e.url);
  if (!shown.length) return null;
  return (
    <div className={'forge-images ' + (large || shown.length === 1 ? 'single' : '')}>
      {shown.map((e) => (
        <figure key={e.key}>
          <a href={e.url} target="_blank" rel="noreferrer" title={t('forge.ui.fullSize')}>
            <img src={e.url} alt={imageLabel(e)} loading="lazy" />
          </a>
          <figcaption>{imageLabel(e)}</figcaption>
        </figure>
      ))}
    </div>
  );
}

/** Compact inline progress of the job that is working on a step right now. */
export function JobInline({ job }) {
  const t = useT();
  const { registry, cancel } = useForge();
  if (!job) return null;
  const step = currentJobStep(job);
  const active = isActiveStatus(job.status);
  return (
    <div className="forge-job-inline">
      <div>
        {active ? <Loader2 size={14} className="spin" /> : null}
        <strong>{step ? stepTitle(step.step, registry) : t('forge.ui.job')}</strong>
        <StatusPill status={job.status} />
        {active && (
          <button type="button" className="forge-link" onClick={() => cancel(job.id)}>
            {t('forge.ui.cancel')}
          </button>
        )}
      </div>
      {active && <ProgressBar value={jobProgress(job)} />}
      {job.status === 'failed' && <p className="forge-error">{jobError(job)}</p>}
    </div>
  );
}

/** Last result of a step: status, previews, numbers, checks and "Show in scene". */
export function StepResult({ steps, imagesFilter, children, hideImages }) {
  const t = useT();
  const { assetId, lastResult, loadGlb, activeJobFor, registry } = useForge();
  const job = activeJobFor(steps);
  const last = lastResult(steps);
  const result = last?.result;
  const glb = result ? findOutput(result.outputs, { ext: '.glb' }) : null;
  const glbUrl = glb ? outputUrl(assetId, glb) : null;
  return (
    <div className="forge-result">
      <JobInline job={job} />
      {last && !job && (
        <div className="forge-result-head">
          <span>
            {stepTitle(last.step, registry)}
            {last.version ? ' · ' + last.version : ''}
            {last.at ? ' · ' + formatDate(last.at) : ''}
          </span>
          <StatusPill status={last.status} />
        </div>
      )}
      {result && !result.ok && result.error && <p className="forge-error">{humanizeError(result.error)}</p>}
      {result && !hideImages && <ResultImages assetId={assetId} outputs={result.outputs} filter={imagesFilter} />}
      {children}
      {result?.stats && <StatsLine stats={result.stats} />}
      {result?.checks?.length > 0 && <ChecksList checks={result.checks} />}
      {result?.warnings?.length > 0 && (
        <ul className="forge-warnings">
          {result.warnings.slice(0, 6).map((w, i) => (
            <li key={i}>{String(w)}</li>
          ))}
        </ul>
      )}
      {glbUrl && (
        <div className="forge-result-actions">
          <button type="button" className="secondary small" onClick={() => loadGlb(glbUrl, last)}>
            <Eye size={13} />
            {t('forge.ui.showInScene')}
          </button>
          <a className="forge-link" href={glbUrl} download>
            <ExternalLink size={12} />
            GLB
          </a>
        </div>
      )}
    </div>
  );
}

const STAT_KEYS = ['tris', 'verts', 'bones', 'meshes', 'clips', 'sockets'];

export function StatsLine({ stats }) {
  const t = useT();
  const parts = [];
  const stat = (key, n) => t('forge.ui.stat.' + key, { n, count: formatCount(n) });
  const hasPair = typeof stats.tris_before === 'number' && typeof stats.tris_after === 'number';
  for (const key of STAT_KEYS) {
    if (key === 'tris' && hasPair) continue;
    const v = stats[key];
    if (typeof v === 'number') parts.push(stat(key, v));
    else if (Array.isArray(v) && key !== 'tris') parts.push(stat(key, v.length));
  }
  if (hasPair) {
    parts.unshift(t('forge.ui.stat.trisPair', { before: formatCount(stats.tris_before), n: stats.tris_after, count: formatCount(stats.tris_after) }));
  }
  if (Array.isArray(stats.size) && stats.size.length === 3) {
    parts.push(t('forge.ui.stat.size', { size: stats.size.map((n) => Number(n).toFixed(2)).join(' × ') }));
  }
  if (!parts.length) return null;
  return <p className="forge-stats">{parts.join(' · ')}</p>;
}
