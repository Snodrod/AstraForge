// Small canvas preview of a library clip drawn as a stick figure (GET /clips/:id/poses).
import React, { useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { normalizePoses, poseBounds, projectJoint, poseAt } from './forge-core.js';
import { getClipPoses } from './forge-api.js';
import { useT, t as translate } from '../i18n/index.js';

const VIEWS = [
  ['front', 'forge.stick.front', 0],
  ['three', 'forge.stick.threeQuarter', Math.PI / 4],
  ['side', 'forge.stick.side', Math.PI / 2],
];

export default function StickFigure({ clipId, title }) {
  const t = useT();
  const canvas = useRef(null);
  const [poses, setPoses] = useState(null);
  const [error, setError] = useState('');
  const [view, setView] = useState('three');

  useEffect(() => {
    let alive = true;
    setPoses(null);
    setError('');
    getClipPoses(clipId)
      .then((json) => {
        if (!alive) return;
        const norm = normalizePoses(json);
        if (!norm) setError(translate('forge.stick.badFormat'));
        setPoses(norm);
      })
      .catch((e) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [clipId]);

  useEffect(() => {
    const el = canvas.current;
    if (!el || !poses) return undefined;
    const yaw = VIEWS.find((v) => v[0] === view)?.[2] || 0;
    const bounds = poseBounds(poses, { yaw });
    if (!bounds) return undefined;
    const ctx = el.getContext('2d');
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const css = el.getBoundingClientRect();
    el.width = Math.max(1, Math.round(css.width * dpr));
    el.height = Math.max(1, Math.round(css.height * dpr));
    const w = el.width;
    const h = el.height;
    const pad = 14 * dpr;
    const spanX = Math.max(bounds.maxX - bounds.minX, 1e-3);
    const spanY = Math.max(bounds.maxY - bounds.minY, 1e-3);
    const scale = Math.min((w - pad * 2) / spanX, (h - pad * 2) / spanY);
    const cx = (bounds.minX + bounds.maxX) / 2;
    const toScreen = ([sx, sy]) => [w / 2 + (sx - cx) * scale, h - pad - (sy - bounds.minY) * scale];
    const styles = getComputedStyle(el);
    const accent = styles.getPropertyValue('--accent').trim() || '#d5df9b';
    let raf = 0;
    const start = performance.now();
    const draw = (now) => {
      raf = requestAnimationFrame(draw);
      const pts = poseAt(poses, (now - start) / 1000).map((p) => (p && p.every(Number.isFinite) ? toScreen(projectJoint(p, { up: poses.up, yaw })) : null));
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = '#3d4c44';
      ctx.lineWidth = dpr;
      ctx.beginPath();
      ctx.moveTo(pad / 2, h - pad);
      ctx.lineTo(w - pad / 2, h - pad);
      ctx.stroke();
      ctx.strokeStyle = accent;
      ctx.lineWidth = 2.2 * dpr;
      ctx.lineCap = 'round';
      for (const [a, b] of poses.edges) {
        if (!pts[a] || !pts[b]) continue;
        ctx.beginPath();
        ctx.moveTo(pts[a][0], pts[a][1]);
        ctx.lineTo(pts[b][0], pts[b][1]);
        ctx.stroke();
      }
      ctx.fillStyle = '#e2e5dc';
      for (const p of pts) {
        if (!p) continue;
        ctx.beginPath();
        ctx.arc(p[0], p[1], 2 * dpr, 0, Math.PI * 2);
        ctx.fill();
      }
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [poses, view]);

  return (
    <div className="forge-stick">
      <div className="forge-stick-head">
        <strong>{title}</strong>
        <div className="forge-stick-views">
          {VIEWS.map(([id, label]) => (
            <button key={id} type="button" className={view === id ? 'active' : ''} onClick={() => setView(id)}>
              {t(label)}
            </button>
          ))}
        </div>
      </div>
      {!poses && !error && (
        <div className="forge-stick-empty">
          <Loader2 size={14} className="spin" />
          {t('forge.stick.loading')}
        </div>
      )}
      {error && <div className="forge-stick-empty">{error}</div>}
      <canvas ref={canvas} className={poses ? '' : 'hidden'} aria-label={t('forge.stick.aria', { title })} />
    </div>
  );
}
