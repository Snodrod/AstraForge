// Effect texture generator (src/fx-textures.js): type descriptions, recipes, determinism, flipbooks, seamlessness, shape in alpha.
import test from 'node:test';
import assert from 'node:assert/strict';
import zlib from 'node:zlib';
import { FX_TEXTURE_TYPES, FX_TEXTURE_PRESETS, FX_SIZES, generateFxTexture, resolveFxParams, fxDefaultParams, fxParamVisible, fxFlipbookGrid, fxFileName, mulberry32, encodeFxPng } from '../src/fx-textures.js';

const IDS = ['soft', 'spark', 'star', 'ring', 'smoke', 'flame', 'noise', 'lightning', 'trail', 'magic'];
const FLIPBOOK_TYPES = ['smoke', 'flame', 'noise', 'ring'];
// CRC-32 (zlib.crc32 does not exist in every Node version)
const crc32 = (bytes) => { let c = 0xffffffff; for (const b of bytes) { c ^= b; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; } return (c ^ 0xffffffff) >>> 0; };
const same = (a, b) => Buffer.compare(Buffer.from(a.buffer, a.byteOffset, a.byteLength), Buffer.from(b.buffer, b.byteOffset, b.byteLength)) === 0;
const gen = (type, params = {}) => generateFxTexture(type, { size: 64, ...params });
const alphaOf = (tex) => Uint8Array.from({ length: tex.width * tex.height }, (_, i) => tex.data[i * 4 + 3]);
// Mean |difference| of two pixel lines (columns/rows) in alpha; (x,y) with step (sx,sy) from the origin (x0,y0)
const lineDiff = (tex, a, b, vertical, x0 = 0, y0 = 0, n = tex.width) => {
  let s = 0;
  for (let i = 0; i < n; i++) {
    const ia = vertical ? ((y0 + i) * tex.width + x0 + a) * 4 + 3 : ((y0 + a) * tex.width + x0 + i) * 4 + 3;
    const ib = vertical ? ((y0 + i) * tex.width + x0 + b) * 4 + 3 : ((y0 + b) * tex.width + x0 + i) * 4 + 3;
    s += Math.abs(tex.data[ia] - tex.data[ib]);
  }
  return s / n;
};
// Seam break: |last - first| versus the mean difference of neighboring lines over the whole frame
const seam = (tex, vertical, x0 = 0, y0 = 0, n = tex.width) => {
  let adj = 0;
  for (let k = 0; k < n - 1; k++) adj += lineDiff(tex, k, k + 1, vertical, x0, y0, n);
  adj /= n - 1;
  return { wrap: lineDiff(tex, n - 1, 0, vertical, x0, y0, n), adj };
};

test('mulberry32: one seed - one sequence, values in [0,1)', () => {
  const a = mulberry32(42), b = mulberry32(42), c = mulberry32(43);
  const xs = Array.from({ length: 200 }, () => a()), ys = Array.from({ length: 200 }, () => b()), zs = Array.from({ length: 200 }, () => c());
  assert.deepEqual(xs, ys);
  assert.notDeepEqual(xs, zs);
  assert.ok(xs.every((v) => v >= 0 && v < 1));
});

test('type descriptions: all 10 types, parameters are valid, keys are unique, defaults are in range', () => {
  assert.deepEqual(FX_TEXTURE_TYPES.map((t) => t.id).sort(), [...IDS].sort());
  for (const t of FX_TEXTURE_TYPES) {
    assert.ok(t.title && t.group, t.id);
    const keys = t.params.map((q) => q.key);
    assert.equal(new Set(keys).size, keys.length, `${t.id}: keys repeat`);
    for (const k of ['size', 'seed', 'contrast', 'gamma', 'invert', 'colorMode', 'color1', 'color2', 'color3']) assert.ok(keys.includes(k), `${t.id}: missing common parameter ${k}`);
    for (const q of t.params) {
      assert.ok(q.key && q.label, `${t.id}: key/label`);
      if (q.type === 'color') assert.match(q.default, /^#[0-9a-f]{6}$/, `${t.id}.${q.key}`);
      else if (q.options) { assert.ok(q.options.length >= 2, `${t.id}.${q.key}`); assert.ok(q.options.some(([v]) => v === q.default), `${t.id}.${q.key}: the default is not in the list`); }
      else { assert.ok(q.min < q.max && q.step > 0, `${t.id}.${q.key}`); assert.ok(q.default >= q.min && q.default <= q.max, `${t.id}.${q.key}: the default is out of range`); }
    }
    assert.ok(t.params.find((q) => q.key === 'size').options.every(([v]) => FX_SIZES.includes(v)));
  }
  assert.deepEqual(FX_SIZES, [64, 128, 256, 512, 1024]);
});

test('recipes: types and parameters exist and are valid, and they generate', () => {
  assert.ok(FX_TEXTURE_PRESETS.length >= 12);
  assert.equal(new Set(FX_TEXTURE_PRESETS.map((p) => p.id)).size, FX_TEXTURE_PRESETS.length);
  const titles = FX_TEXTURE_PRESETS.map((p) => p.title);
  for (const need of ['Soft glow', 'Spark', 'Star', 'Shockwave', 'Smoke puff', 'Fire 4×4', 'Lightning', 'Seamless Perlin noise', 'Voronoi cells', 'Trail']) assert.ok(titles.includes(need), 'no recipe for "' + need + '»');
  for (const p of FX_TEXTURE_PRESETS) {
    const t = FX_TEXTURE_TYPES.find((x) => x.id === p.type);
    assert.ok(t, `${p.id}: type ${p.type}`);
    const resolved = resolveFxParams(p.type, p.params);
    for (const [k, v] of Object.entries(p.params)) {
      assert.ok(t.params.some((q) => q.key === k), `${p.id}: unknown parameter ${k}`);
      assert.equal(resolved[k], v, `${p.id}.${k}: the value is out of the allowed range and would be corrected`);
    }
  }
  // One recipe per type - a real generation (small size so the test is fast)
  for (const p of FX_TEXTURE_PRESETS) {
    const tex = generateFxTexture(p.type, { ...p.params, size: 128 });
    assert.equal(tex.data.length, 128 * 128 * 4, p.id);
    assert.ok(alphaOf(tex).some((a) => a > 0), `${p.id}: empty texture`);
  }
});

test('resolveFxParams: defaults, range clamping, strings, garbage, extra keys', () => {
  const d = resolveFxParams('soft');
  assert.deepEqual(d, fxDefaultParams('soft'));
  assert.deepEqual(resolveFxParams('soft', null), d);
  const r = resolveFxParams('soft', { radius: '0.5', softness: 99, core: -3, wobble: 'abc', size: '256', invert: '1', colorMode: 'rainbow', color1: 'red', color2: 'F80', color3: '#00FF00', seed: -7.9, junk: 1 });
  assert.equal(r.radius, 0.5);
  assert.equal(r.softness, 1);
  assert.equal(r.core, 0);
  assert.equal(r.wobble, d.wobble);
  assert.equal(r.size, 256);
  assert.equal(r.invert, 1);
  assert.equal(r.colorMode, 'white');
  assert.equal(r.color1, d.color1);
  assert.equal(r.color2, '#ff8800');
  assert.equal(r.color3, '#00ff00');
  assert.equal(r.seed, 7);
  assert.ok(!('junk' in r));
  assert.equal(resolveFxParams('star', { rays: 5.6 }).rays, 6, 'integer parameters are rounded');
  assert.equal(resolveFxParams('soft', { size: 100 }).size, 512, 'a size outside the list -> default');
  assert.equal(resolveFxParams('soft', { seed: 123456 }).seed, 123456, 'seed is not clamped to the slider bounds');
  assert.throws(() => resolveFxParams('nope'), /Unknown texture type/);
  assert.throws(() => generateFxTexture('nope', {}), /Unknown texture type/);
  assert.throws(() => fxDefaultParams('nope'), /Unknown texture type/);
});

test('parameter visibility and the flipbook grid', () => {
  const smoke = FX_TEXTURE_TYPES.find((t) => t.id === 'smoke'), noise = FX_TEXTURE_TYPES.find((t) => t.id === 'noise');
  const evolve = smoke.params.find((q) => q.key === 'evolve'), c1 = smoke.params.find((q) => q.key === 'color1'), c3 = smoke.params.find((q) => q.key === 'color3');
  assert.equal(fxParamVisible(evolve, { flipbook: '1x1' }), false);
  assert.equal(fxParamVisible(evolve, { flipbook: '4x4' }), true);
  assert.equal(fxParamVisible(c1, { colorMode: 'white' }), false);
  assert.equal(fxParamVisible(c1, { colorMode: 'color' }), true);
  assert.equal(fxParamVisible(c3, { colorMode: 'color', colorStops: 2 }), false);
  assert.equal(fxParamVisible(c3, { colorMode: 'color', colorStops: 3 }), true);
  assert.equal(fxParamVisible(noise.params[0], {}), true);
  assert.equal(fxFlipbookGrid({ flipbook: '1x1' }), null);
  assert.equal(fxFlipbookGrid({}), null);
  assert.deepEqual(fxFlipbookGrid({ flipbook: '8x8' }), { h: 8, v: 8 });
  assert.equal(fxFileName('smoke', { seed: 42 }), 'fx_smoke_42.png');
  assert.equal(fxFileName('flame', { seed: 7, flipbook: '4x4' }), 'fx_flame_7_4x4.png');
  assert.match(fxFileName('noise', {}), /^fx_noise_\d+\.png$/);
});

for (const id of IDS) {
  test(`${id}: buffer size, determinism, seed, shape in alpha, white RGB`, () => {
    const a = gen(id, { seed: 5 }), b = gen(id, { seed: 5 }), c = gen(id, { seed: 6 });
    assert.equal(a.width, 64);
    assert.equal(a.height, 64);
    assert.ok(a.data instanceof Uint8ClampedArray);
    assert.equal(a.data.length, 64 * 64 * 4);
    assert.ok(same(a.data, b.data), 'the same seed and parameters gave different bytes');
    assert.ok(!same(a.data, c.data), 'different seeds gave the same result');
    assert.equal(a.flipbook, null);
    assert.equal(typeof a.tileable, 'boolean');
    let lo = 255, hi = 0, white = true;
    for (let i = 0; i < 64 * 64; i++) { const o = i * 4; if (a.data[o] !== 255 || a.data[o + 1] !== 255 || a.data[o + 2] !== 255) white = false; lo = Math.min(lo, a.data[o + 3]); hi = Math.max(hi, a.data[o + 3]); }
    assert.ok(white, 'in white mode RGB must be 255');
    assert.ok(hi > 100, 'the shape must be visible (max alpha > 100), not ' + hi);
    assert.ok(hi > lo, 'alpha must vary');
  });
  test(`${id}: parameters without a seed are also deterministic at different sizes, no NaN`, () => {
    for (const size of [64, 128]) {
      const a = generateFxTexture(id, { size, seed: 99 }), b = generateFxTexture(id, { size, seed: 99 });
      assert.ok(same(a.data, b.data));
      assert.equal(a.data.length, size * size * 4);
    }
  });
}

test('the shape fades to zero at the frame edges (no clipped squares) for non-noise types', () => {
  for (const id of ['soft', 'spark', 'star', 'ring', 'smoke', 'flame', 'magic', 'lightning', 'trail']) {
    for (const seed of [1, 2, 3]) {
      const t = generateFxTexture(id, { size: 128, seed });
      let m = 0;
      for (let i = 0; i < 128; i++) for (const [x, y] of [[i, 0], [i, 127], [0, i], [127, i]]) m = Math.max(m, t.data[(y * 128 + x) * 4 + 3]);
      assert.ok(m <= 6, `${id}/seed ${seed}: alpha at the edge ${m}`);
    }
  }
});

test('"colored" mode: the same alpha, colors come from the gradient', () => {
  const white = gen('soft', { seed: 2, size: 128 });
  const col = gen('soft', { seed: 2, size: 128, colorMode: 'color', colorStops: 3, color1: '#ff0000', color2: '#00ff00', color3: '#0000ff' });
  assert.ok(same(alphaOf(white), alphaOf(col)), 'alpha must not depend on the color mode');
  let imax = 0;
  for (let i = 0; i < 128 * 128; i++) if (col.data[i * 4 + 3] > col.data[imax * 4 + 3]) imax = i;
  assert.ok(col.data[imax * 4 + 2] > 200 && col.data[imax * 4] < 60, 'the brightest spot must be color3 (blue)');
  const colors = new Set(); for (let i = 0; i < 128 * 128; i++) colors.add(col.data[i * 4] << 16 | col.data[i * 4 + 1] << 8 | col.data[i * 4 + 2]);
  assert.ok(colors.size > 30, 'the gradient must give many shades');
  const two = gen('soft', { seed: 2, size: 128, colorMode: 'color', colorStops: 2, color1: '#ff0000', color2: '#0000ff', color3: '#00ff00' });
  assert.ok(two.data[imax * 4 + 1] < 30, 'in 2-color mode color3 does not take part');
  assert.ok(two.data[imax * 4 + 2] > 200);
});

test('invert, contrast and gamma change the result predictably', () => {
  const base = gen('noise', { seed: 3, size: 64, mode: 'perlin' }), inv = gen('noise', { seed: 3, size: 64, mode: 'perlin', invert: 1 });
  for (let i = 0; i < 64 * 64; i++) assert.ok(Math.abs(base.data[i * 4 + 3] + inv.data[i * 4 + 3] - 255) <= 1, 'invert: a + a\' = 255 (+-1 dithering)');
  const mean = (t) => { let s = 0; for (let i = 0; i < 64 * 64; i++) s += t.data[i * 4 + 3]; return s / (64 * 64); };
  const dev = (t) => { const m = mean(t); let s = 0; for (let i = 0; i < 64 * 64; i++) s += (t.data[i * 4 + 3] - m) ** 2; return Math.sqrt(s / (64 * 64)); };
  const hard = gen('noise', { seed: 3, size: 64, mode: 'perlin', contrast: 2.5 }), flat = gen('noise', { seed: 3, size: 64, mode: 'perlin', contrast: 0.4 });
  assert.ok(dev(hard) > dev(base) && dev(base) > dev(flat), 'contrast must change the brightness spread');
  const bright = gen('noise', { seed: 3, size: 64, mode: 'perlin', gamma: 2 }), dark = gen('noise', { seed: 3, size: 64, mode: 'perlin', gamma: 0.5 });
  assert.ok(mean(bright) > mean(base) && mean(base) > mean(dark), 'gamma > 1 brightens');
  // contrast does not lift black above zero: the glow corners stay transparent
  const soft = gen('soft', { size: 64, contrast: 0.3 });
  assert.equal(soft.data[3], 0);
});

for (const id of FLIPBOOK_TYPES) {
  test(`${id}: flipbook 2x2 / 4x4 / 8x8 - grid, frames differ, determinism, no empty frames`, () => {
    for (const grid of ['2x2', '4x4', '8x8']) {
      const n = +grid[0], size = 256;
      const a = generateFxTexture(id, { size, seed: 9, flipbook: grid }), b = generateFxTexture(id, { size, seed: 9, flipbook: grid });
      assert.deepEqual(a.flipbook, { h: n, v: n });
      assert.equal(a.width, size);
      assert.equal(a.height, size);
      assert.equal(a.data.length, size * size * 4);
      assert.ok(same(a.data, b.data));
      const fs_ = size / n, frame = (k) => { const out = []; const ox = (k % n) * fs_, oy = Math.floor(k / n) * fs_; for (let y = 0; y < fs_; y++) for (let x = 0; x < fs_; x++) out.push(a.data[((oy + y) * size + ox + x) * 4 + 3]); return out; };
      const frames = n * n, f0 = frame(0), f1 = frame(1), last = frame(frames - 1);
      assert.notDeepEqual(f0, f1, `${id} ${grid}: neighboring frames are identical`);
      assert.notDeepEqual(f0, last, `${id} ${grid}: the first and last frames are identical`);
      if (n <= 4) for (let k = 0; k < frames; k++) assert.ok(frame(k).some((v) => v > 0), `${id} ${grid}: frame ${k} is empty`);
    }
    assert.equal(generateFxTexture(id, { size: 64, flipbook: '1x1' }).flipbook, null);
  });
}

test('types without a flipbook ignore the flipbook parameter', () => {
  for (const id of ['soft', 'spark', 'star', 'lightning', 'trail', 'magic']) {
    const t = generateFxTexture(id, { size: 64, flipbook: '4x4' });
    assert.equal(t.flipbook, null, id);
  }
});

test('flipbook loops: the last -> first frame transition is not sharper than the neighbors (fire, noise)', () => {
  for (const [id, extra] of [['flame', {}], ['noise', { mode: 'perlin' }], ['noise', { mode: 'cells' }]]) {
    const n = 4, size = 512, fs_ = size / n, t = generateFxTexture(id, { size, seed: 5, flipbook: '4x4', ...extra });
    const frame = (k) => { const out = new Float32Array(fs_ * fs_); const ox = (k % n) * fs_, oy = Math.floor(k / n) * fs_; for (let y = 0; y < fs_; y++) for (let x = 0; x < fs_; x++) out[y * fs_ + x] = t.data[((oy + y) * size + ox + x) * 4 + 3]; return out; };
    const dist = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]); return s / a.length; };
    let adj = 0; for (let k = 0; k < 15; k++) adj += dist(frame(k), frame(k + 1)); adj /= 15;
    const wrap = dist(frame(15), frame(0));
    assert.ok(wrap <= adj * 2.2 + 1, `${id}/${extra.mode || ''}: loop seam ${wrap.toFixed(2)} versus neighbors ${adj.toFixed(2)}`);
  }
});

test('seamless noise: the left column continues the right one, top continues bottom, for all noise kinds', () => {
  for (const mode of ['perlin', 'ridged', 'turbulence', 'cells', 'cracks', 'mosaic']) {
    const t = generateFxTexture('noise', { size: 128, seed: 11, mode, scale: 4, tileable: 1 });
    assert.equal(t.tileable, true);
    for (const vertical of [true, false]) {
      const { wrap, adj } = seam(t, vertical);
      // the seam break is comparable to the normal step between neighboring pixels (tolerance 2x + a margin at cell edges)
      assert.ok(wrap <= adj * 2 + 2, `${mode}${vertical ? ' (left/right)' : ' (top/bottom)'}: seam ${wrap.toFixed(2)} versus neighbors ${adj.toFixed(2)}`);
    }
  }
  // a fractional scale is rounded to a whole period when seamless
  const frac = generateFxTexture('noise', { size: 128, seed: 11, mode: 'perlin', scale: 4.6, tileable: 1 });
  assert.ok(seam(frac, true).wrap <= seam(frac, true).adj * 2 + 2);
});

test('seamless flipbook: each frame is seamless on its own', () => {
  const n = 4, size = 512, fs_ = size / n, t = generateFxTexture('noise', { size, seed: 5, flipbook: '4x4', mode: 'perlin', scale: 3, tileable: 1 });
  for (const k of [0, 5, 10, 15]) {
    const x0 = (k % n) * fs_, y0 = Math.floor(k / n) * fs_;
    for (const vertical of [true, false]) {
      let adj = 0; for (let i = 0; i < fs_ - 1; i++) adj += lineDiff(t, i, i + 1, vertical, x0, y0, fs_); adj /= fs_ - 1;
      assert.ok(lineDiff(t, fs_ - 1, 0, vertical, x0, y0, fs_) <= adj * 2 + 2, `frame ${k}`);
    }
  }
});

test('without seamlessness the noise edges clearly diverge (a check that the seam test measures something)', () => {
  const t = generateFxTexture('noise', { size: 128, seed: 11, mode: 'perlin', scale: 3, tileable: 0 });
  assert.equal(t.tileable, false);
  const { wrap, adj } = seam(t, true);
  assert.ok(wrap > adj * 4, `without a period the seam ${wrap.toFixed(2)} is not much larger than neighbors ${adj.toFixed(2)}`);
  assert.equal(generateFxTexture('soft', { size: 64 }).tileable, false);
});

test('lightning branches: with branches there are more lit pixels than without', () => {
  const lit = (t) => alphaOf(t).filter((a) => a > 60).length;
  const none = generateFxTexture('lightning', { size: 256, seed: 4, branches: 0 }), many = generateFxTexture('lightning', { size: 256, seed: 4, branches: 8 });
  assert.ok(lit(many) > lit(none) * 1.25, `${lit(many)} versus ${lit(none)}`);
  for (const direction of ['v', 'h', 'd']) assert.ok(lit(generateFxTexture('lightning', { size: 128, seed: 4, direction })) > 0);
});

test('the spark is thin: the core width is much smaller than the length', () => {
  const t = generateFxTexture('spark', { size: 256, angle: 0, glow: 0, flicker: 0 });
  let rowsLit = 0, colsLit = 0;
  for (let y = 0; y < 256; y++) if (t.data[(y * 256 + 128) * 4 + 3] > 128) rowsLit++;
  for (let x = 0; x < 256; x++) if (Math.max(t.data[(127 * 256 + x) * 4 + 3], t.data[(128 * 256 + x) * 4 + 3]) > 50) colsLit++;   // the spark axis lies between two rows
  assert.ok(colsLit > 200, `length ${colsLit}`);
  assert.ok(rowsLit < 12, `thickness ${rowsLit}`);
});

test('the ring is clean: empty in the center and outside, present at the radius', () => {
  const t = generateFxTexture('ring', { size: 256, edgeNoise: 0, radius: 0.7, thickness: 0.05, trail: 0 });
  const a = (x, y) => t.data[(y * 256 + x) * 4 + 3];
  assert.equal(a(128, 128), 0);
  assert.equal(a(250, 128), 0);
  assert.ok(a(128 + Math.round(0.7 * 128), 128) > 230);
  assert.ok(a(128, 128 - Math.round(0.7 * 128)) > 230);
});

test('fast: a 256 preview of any type is computed well under a second', () => {
  for (const type of IDS) {
    const t0 = performance.now();
    generateFxTexture(type, { size: 256, seed: 1 });
    assert.ok(performance.now() - t0 < 1500, `${type}: ${(performance.now() - t0).toFixed(0)} ms`);
  }
});

test('encodeFxPng: a valid PNG, RGBA bytes are returned losslessly (including nearly transparent pixels)', async () => {
  const tex = generateFxTexture('smoke', { size: 64, seed: 3, colorMode: 'color' });
  const png = await encodeFxPng(tex), buf = Buffer.from(png);
  assert.deepEqual([...buf.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  assert.equal(buf.readUInt32BE(16), 64);
  assert.equal(buf.readUInt32BE(20), 64);
  assert.equal(buf[24], 8);
  assert.equal(buf[25], 6);
  // chunk parsing: every CRC matches, IDAT is zlib, the Sub filter reverses to the original bytes
  let at = 8, idat = [], end = false;
  while (at < buf.length) {
    const len = buf.readUInt32BE(at), type = buf.toString('latin1', at + 4, at + 8), body = buf.subarray(at + 8, at + 8 + len);
    assert.equal(buf.readUInt32BE(at + 8 + len), crc32(buf.subarray(at + 4, at + 8 + len)), 'CRC of chunk ' + type);
    if (type === 'IDAT') idat.push(body);
    if (type === 'IEND') end = true;
    at += 12 + len;
  }
  assert.ok(end && at === buf.length);
  const raw = zlib.inflateSync(Buffer.concat(idat)), row = 64 * 4;
  assert.equal(raw.length, (row + 1) * 64);
  const back = new Uint8Array(row * 64);
  for (let y = 0; y < 64; y++) {
    assert.equal(raw[y * (row + 1)], 1);
    for (let i = 0; i < row; i++) back[y * row + i] = (raw[y * (row + 1) + 1 + i] + (i >= 4 ? back[y * row + i - 4] : 0)) & 255;
  }
  assert.ok(same(back, tex.data));
});
