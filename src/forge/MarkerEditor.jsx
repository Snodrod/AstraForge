// Mixamo-style joint markers over the front view (markers.png). Dots are draggable with the
// mouse/touch and nudgeable with arrow keys; coordinates are kept in metres (Blender X, Z).
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { RotateCcw, Hammer, Loader2 } from 'lucide-react';
import {
  normalizeMarkers, markerProjection, metresToPx, pxToMetres, clampPx, moveMarker, roundMarkers, markerLabel, mirrorName,
  detectColorBlobs, calibrateProjection, markersSymmetric, frontTileWidth,
} from './forge-core.js';
import { Notice } from './ForgeUi.jsx';
import { useT } from '../i18n/index.js';

export default function MarkerEditor({ imageUrl, doc, busy, disabled, onRebuild }) {
  const t = useT();
  const [natural, setNatural] = useState(null);
  const [markers, setMarkers] = useState({});
  const [initial, setInitial] = useState({});
  const [symmetry, setSymmetry] = useState(true);
  const [active, setActive] = useState(null);
  const [focus, setFocus] = useState(null);
  const img = useRef(null);
  const stage = useRef(null);
  const [showSide, setShowSide] = useState(false);

  useEffect(() => {
    const m = normalizeMarkers(doc);
    setMarkers(m);
    setInitial(m);
    // Symmetric dragging only makes sense when the detected pose is symmetric (no staff in one hand).
    setSymmetry(markersSymmetric(m));
  }, [doc]);

  const baseProj = useMemo(() => (doc ? markerProjection(doc, natural) : null), [doc, natural]);
  const [calibrated, setCalibrated] = useState(null);

  // Without a projection block, fit the projection to the joint spheres drawn in markers.png.
  useEffect(() => {
    setCalibrated(null);
    const el = img.current;
    if (!doc || !natural || !baseProj || baseProj.source === 'projection' || !el) return;
    try {
      const canvas = document.createElement('canvas');
      canvas.width = el.naturalWidth;
      canvas.height = el.naturalHeight;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(el, 0, 0);
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const tile = frontTileWidth(canvas.width, canvas.height);
      setCalibrated(calibrateProjection(baseProj, doc, detectColorBlobs(data, canvas.width, canvas.height, { maxX: tile })));
    } catch {
      /* no pixel access: keep the approximation */
    }
  }, [doc, natural, baseProj]);
  const proj = calibrated || baseProj;
  const names = Object.keys(markers);
  const dirty = JSON.stringify(roundMarkers(markers)) !== JSON.stringify(roundMarkers(initial));

  // The sheet is front + side; only the front tile is shown (larger dots), side view on demand.
  // The stage follows the loaded PNG; markerProjection already maps its pixels to that size.
  const sheetW = natural?.width || proj?.width || 1;
  const sheetH = natural?.height || proj?.height || 1;
  const tileW = frontTileWidth(sheetW, sheetH);
  const viewW = showSide ? sheetW : tileW;

  const toImagePx = (clientX, clientY) => {
    const r = stage.current.getBoundingClientRect();
    return [((clientX - r.left) / r.width) * viewW, ((clientY - r.top) / r.height) * sheetH];
  };

  const place = (name, px) => {
    const metres = pxToMetres(proj, clampPx({ width: tileW, height: sheetH }, px));
    setMarkers((m) => moveMarker(m, name, metres, { symmetry, centerX: 0 }));
  };

  const onKey = (name, e) => {
    const step = (e.shiftKey ? 10 : 1);
    const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!d || !proj) return;
    e.preventDefault();
    e.stopPropagation();
    const [u, v] = metresToPx(proj, markers[name]);
    place(name, [u + d[0], v + d[1]]);
  };

  const highlighted = active || focus;
  const partner = highlighted ? mirrorName(highlighted) : null;

  return (
    <div className="forge-markers">
      <div className="forge-marker-stage" ref={stage} style={natural ? { aspectRatio: viewW + ' / ' + sheetH } : undefined}>
        <img
          ref={img}
          style={natural ? { width: (sheetW / viewW) * 100 + '%' } : undefined}
          src={imageUrl}
          alt={t('forge.marker.alt')}
          draggable={false}
          onLoad={(e) => setNatural({ width: e.currentTarget.naturalWidth, height: e.currentTarget.naturalHeight })}
        />
        {proj && names.map((name) => {
          const [u, v] = metresToPx(proj, markers[name]);
          const cls = ['forge-marker', name === highlighted ? 'active' : '', name === partner && symmetry ? 'partner' : ''].join(' ');
          return (
            <button
              key={name}
              type="button"
              className={cls}
              style={{ left: (u / viewW) * 100 + '%', top: (v / sheetH) * 100 + '%' }}
              aria-label={t('forge.marker.aria', { name: markerLabel(name), x: markers[name][0].toFixed(3), y: markers[name][1].toFixed(3) })}
              title={markerLabel(name)}
              disabled={disabled}
              onPointerDown={(e) => {
                e.preventDefault();
                e.currentTarget.setPointerCapture(e.pointerId);
                setActive(name);
              }}
              onPointerMove={(e) => {
                if (active === name) place(name, toImagePx(e.clientX, e.clientY));
              }}
              onPointerUp={(e) => {
                e.currentTarget.releasePointerCapture?.(e.pointerId);
                setActive(null);
              }}
              onPointerCancel={() => setActive(null)}
              onFocus={() => setFocus(name)}
              onBlur={() => setFocus(null)}
              onKeyDown={(e) => onKey(name, e)}
            >
              <span>{markerLabel(name)}</span>
            </button>
          );
        })}
      </div>

      {!proj && natural && <Notice tone="warn">{t('forge.marker.noProjection')}</Notice>}
      {proj?.source === 'bbox' && <p className="field-note">{t('forge.marker.bbox')}</p>}
      {proj?.source === 'spheres' && (
        <p className="field-note">{t('forge.marker.spheres', { n: proj.points, rms: proj.rms.toFixed(1) })}</p>
      )}
      {proj?.source === 'joints' && (
        <Notice tone="warn">{t('forge.marker.joints')}</Notice>
      )}
      {!names.length && doc && <p className="field-note">{t('forge.marker.none')}</p>}

      {names.length > 0 && (
        <div className="forge-marker-legend">
          {names.map((name) => (
            <span key={name} className={name === highlighted ? 'active' : ''}>
              {markerLabel(name)}
              <small>
                {t('forge.marker.coords', { x: markers[name][0].toFixed(2), y: markers[name][1].toFixed(2) })}
              </small>
            </span>
          ))}
        </div>
      )}

      <div className="forge-row">
        <label className="forge-check">
          <input type="checkbox" checked={symmetry} onChange={(e) => setSymmetry(e.target.checked)} />
          <span>{t('forge.marker.symmetry')}</span>
        </label>
        {sheetW > tileW && (
          <button type="button" className="forge-link" onClick={() => setShowSide((v) => !v)}>
            {showSide ? t('forge.marker.frontOnly') : t('forge.marker.showSide')}
          </button>
        )}
      </div>
      <div className="forge-row">
        <button type="button" className="secondary small" disabled={!dirty || busy} onClick={() => setMarkers(initial)}>
          <RotateCcw size={13} />
          {t('forge.marker.reset')}
        </button>
        <button type="button" className="primary small" disabled={disabled || busy || !names.length} onClick={() => onRebuild(roundMarkers(markers))}>
          {busy ? <Loader2 size={13} className="spin" /> : <Hammer size={13} />}
          {t('forge.marker.rebuild')}
        </button>
      </div>
      <p className="field-note">{t('forge.marker.help')}</p>
    </div>
  );
}
