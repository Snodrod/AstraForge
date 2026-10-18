// Procedural textures for effects (VFX): glows, sparks, smoke, fire, noises, lightning, flipbooks.
// No DOM and no Math.random in generation: one seed + params = the same result down to the byte (own mulberry32 and own Perlin).
// By default the texture is white and the shape lives in alpha (the particle color_ramp sets the color, like make_fx_texture in _export_lib.py);
// "colored" mode tints brightness with a gradient of 2-3 colors.
import { t } from './i18n/index.js';

const TAU = Math.PI * 2;
const clamp = (v, a = 0, b = 1) => (v < a ? a : v > b ? b : v);
const lerp = (a, b, t) => a + (b - a) * t;
const smoothstep = (e0, e1, x) => { if (e1 - e0 < 1e-6) return x < e0 ? 0 : 1; const t = clamp((x - e0) / (e1 - e0)); return t * t * (3 - 2 * t); };

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
// Integer hash of two coordinates and a salt -> [0,1): Voronoi cells, dithering
function hash2(ix, iy, salt) {
  let h = Math.imul(ix | 0, 0x27d4eb2d) ^ Math.imul(iy | 0, 0x165667b1) ^ Math.imul((salt | 0) + 0x9e3779b9, 0x85ebca6b);
  h ^= h >>> 15; h = Math.imul(h, 0x2c1b3c6d); h ^= h >>> 12; h = Math.imul(h, 0x297a2d39); h ^= h >>> 15;
  return (h >>> 0) / 4294967296;
}

// ───────────────────────── Perlin noise (improved, 3D, periodic) ─────────────────────────
// n3(x,y,z,px,py,pz): value in [-1,1]; a period p>0 (in lattice units) makes the noise seamless along that axis, p<=0 means no period.
const NOISE_GAIN = 1.55;   // fbm in [-1,1] with headroom: typical spread ~0.15..0.85 after *0.5+0.5
function makeNoise(seed) {
  const rnd = mulberry32((seed ^ 0x5bd1e995) >>> 0);
  const p = new Uint8Array(256); for (let i = 0; i < 256; i++) p[i] = i;
  for (let i = 255; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); const t = p[i]; p[i] = p[j]; p[j] = t; }
  const perm = new Uint8Array(512); for (let i = 0; i < 512; i++) perm[i] = p[i & 255];
  const grad = (h, x, y, z) => { h &= 15; const u = h < 8 ? x : y; const v = h < 4 ? y : (h === 12 || h === 14 ? x : z); return ((h & 1) === 0 ? u : -u) + ((h & 2) === 0 ? v : -v); };
  function n3(x, y, z, px, py, pz) {
    const xf = Math.floor(x), yf = Math.floor(y), zf = Math.floor(z);
    const fx = x - xf, fy = y - yf, fz = z - zf;
    let xa, xb, ya, yb, za, zb;
    if (px > 0) { xa = ((xf % px) + px) % px; xb = xa + 1 >= px ? 0 : xa + 1; } else { xa = xf; xb = xf + 1; }
    if (py > 0) { ya = ((yf % py) + py) % py; yb = ya + 1 >= py ? 0 : ya + 1; } else { ya = yf; yb = yf + 1; }
    if (pz > 0) { za = ((zf % pz) + pz) % pz; zb = za + 1 >= pz ? 0 : za + 1; } else { za = zf; zb = zf + 1; }
    xa &= 255; xb &= 255; ya &= 255; yb &= 255; za &= 255; zb &= 255;
    const u = fx * fx * fx * (fx * (fx * 6 - 15) + 10), v = fy * fy * fy * (fy * (fy * 6 - 15) + 10), w = fz * fz * fz * (fz * (fz * 6 - 15) + 10);
    const A = perm[xa], B = perm[xb];
    const AA = perm[A + ya], AB = perm[A + yb], BA = perm[B + ya], BB = perm[B + yb];
    const x1 = fx - 1, y1 = fy - 1, z1 = fz - 1;
    const l00 = grad(perm[AA + za], fx, fy, fz), l10 = grad(perm[BA + za], x1, fy, fz);
    const l01 = grad(perm[AB + za], fx, y1, fz), l11 = grad(perm[BB + za], x1, y1, fz);
    const h00 = grad(perm[AA + zb], fx, fy, z1), h10 = grad(perm[BA + zb], x1, fy, z1);
    const h01 = grad(perm[AB + zb], fx, y1, z1), h11 = grad(perm[BB + zb], x1, y1, z1);
    const a0 = l00 + u * (l10 - l00), a1 = l01 + u * (l11 - l01), b0 = h00 + u * (h10 - h00), b1 = h01 + u * (h11 - h01);
    const c0 = a0 + v * (a1 - a0), c1 = b0 + v * (b1 - b0);
    return c0 + w * (c1 - c0);
  }
  // fbm: sum of octaves, normalized; periods double with the frequency, so seamlessness holds on every octave
  function fbm(x, y, z, oct, pers, px, py, pz) {
    let s = 0, a = 1, n = 0, f = 1;
    for (let o = 0; o < oct; o++) { s += a * n3(x * f + o * 0.5731 + 0.1237, y * f + o * 0.3191 + 0.4561, z * f + o * 0.7213 + 0.2719, px * f, py * f, pz * f); n += a; a *= pers; f *= 2; }
    return clamp((s / n) * NOISE_GAIN, -1, 1);
  }
  // ridged: 1-|n| squared on each octave, [0,1]
  function ridged(x, y, z, oct, pers, px, py, pz) {
    let s = 0, a = 1, n = 0, f = 1;
    for (let o = 0; o < oct; o++) { const r = 1 - Math.abs(n3(x * f + o * 0.5731 + 0.1237, y * f + o * 0.3191 + 0.4561, z * f + o * 0.7213 + 0.2719, px * f, py * f, pz * f)) * 1.25; s += a * r * r; n += a; a *= pers; f *= 2; }
    return clamp((s / n - 0.12) * 1.25, 0, 1);
  }
  // turbulence: |n| over octaves, [0,1]
  function turb(x, y, z, oct, pers, px, py, pz) {
    let s = 0, a = 1, n = 0, f = 1;
    for (let o = 0; o < oct; o++) { s += a * Math.abs(n3(x * f + o * 0.5731 + 0.1237, y * f + o * 0.3191 + 0.4561, z * f + o * 0.7213 + 0.2719, px * f, py * f, pz * f)); n += a; a *= pers; f *= 2; }
    return clamp((s / n) * 2.3, 0, 1);
  }
  return { n3, fbm, ridged, turb };
}

// ───────────────────────── Helpers: blur, segments ─────────────────────────
function boxPass(src, dst, W, H, r, horiz) {
  const inv = 1 / (2 * r + 1), len = horiz ? W : H, lines = horiz ? H : W, step = horiz ? 1 : W, ls = horiz ? W : 1;
  for (let l = 0; l < lines; l++) {
    const base = l * ls; let acc = 0;
    for (let k = 0; k <= r && k < len; k++) acc += src[base + k * step];
    for (let k = 0; k < len; k++) {
      dst[base + k * step] = acc * inv;
      const add = k + r + 1 < len ? src[base + (k + r + 1) * step] : 0, rem = k - r >= 0 ? src[base + (k - r) * step] : 0;
      acc += add - rem;
    }
  }
}
// Gaussian blur via three box-filter passes, sigma in pixels; edges are treated as zero
function blurBuf(src, W, H, sigma) {
  const out = new Float32Array(src);
  if (sigma < 0.6) return out;
  const r = Math.max(1, Math.floor((Math.sqrt(4 * sigma * sigma + 1) - 1) / 2)), tmp = new Float32Array(src.length);
  for (let i = 0; i < 3; i++) { boxPass(out, tmp, W, H, r, true); boxPass(tmp, out, W, H, r, false); }
  return out;
}
// Gaussian segments (coordinates in texture fractions): segment [x0,y0,x1,y1,weight,thickness factor]; max compositing
function drawSegs(buf, W, H, segs, thick) {
  for (let s = 0; s < segs.length; s++) {
    const [x0, y0, x1, y1, wt, k] = segs[s];
    const th = Math.max(thick * k, 0.9 / Math.min(W, H)), R = th * 3.2, th2 = th * th;
    const xs = Math.max(0, Math.floor((Math.min(x0, x1) - R) * W)), xe = Math.min(W - 1, Math.ceil((Math.max(x0, x1) + R) * W));
    const ys = Math.max(0, Math.floor((Math.min(y0, y1) - R) * H)), ye = Math.min(H - 1, Math.ceil((Math.max(y0, y1) + R) * H));
    const dx = x1 - x0, dy = y1 - y0, l2 = dx * dx + dy * dy, R2 = R * R;
    for (let y = ys; y <= ye; y++) {
      const py = (y + 0.5) / H;
      for (let x = xs; x <= xe; x++) {
        const px = (x + 0.5) / W;
        const t = l2 > 0 ? clamp(((px - x0) * dx + (py - y0) * dy) / l2) : 0;
        const ex = px - (x0 + dx * t), ey = py - (y0 + dy * t), d2 = ex * ex + ey * ey;
        if (d2 > R2) continue;
        const v = wt * Math.exp(-d2 / th2), i = y * W + x;
        if (v > buf[i]) buf[i] = v;
      }
    }
  }
}
// Core + halo: the halo is a blurred copy of the core, boosted in proportion to the blur (a thin line would barely glow otherwise)
function addGlow(core, W, H, thick, glow, out) {
  if (glow <= 0) { out.set(core); return; }
  const sigma = (thick * 3.5 + glow * 0.035) * Math.min(W, H), blur = blurBuf(core, W, H, sigma), gain = glow * clamp(sigma / (thick * Math.min(W, H) + 0.5) * 0.9, 1, 9);
  for (let i = 0; i < core.length; i++) out[i] = Math.min(1, core[i] + blur[i] * gain);
}

// ───────────────────────── Frame generators ─────────────────────────
// gen(p, W, H, c, out): p are params, c = {nz, frames, t (loop phase 0..1), prog (progress 0..1 inclusive)}, out is a Float32Array of brightness 0..1.
// Coordinates dx,dy: -1..1 from the center (1 is half the texture), y down.
const GEN = {
  soft(p, W, H, c, out) {
    const R = p.radius, aa = 2 / (W * R), e0 = clamp(Math.min(1 - p.softness, 1 - aa)), nz = c.nz;
    for (let y = 0; y < H; y++) {
      const dy = ((y + 0.5) / H - 0.5) * 2;
      for (let x = 0; x < W; x++) {
        const dx = ((x + 0.5) / W - 0.5) * 2, r = Math.sqrt(dx * dx + dy * dy) / R;
        if (r >= 1) { out[y * W + x] = 0; continue; }
        let a = 1 - smoothstep(e0, 1, r);
        if (p.wobble > 0) a *= 1 - p.wobble * 0.55 * (1 - clamp(nz.fbm(dx * 2.2 + 5, dy * 2.2 + 5, 0.5, 3, 0.5, 0, 0, 0) * 0.5 + 0.5)) * smoothstep(0, 0.5, r + 0.1);
        a += p.core * Math.exp(-(r * r) / 0.035) * (1 - r);
        out[y * W + x] = a;
      }
    }
  },
  spark(p, W, H, c, out) {
    const nz = c.nz, ang = (p.angle * Math.PI) / 180, ca = Math.cos(ang), sa = Math.sin(ang), L = p.length, th = p.thickness, gl = p.glow, fl = p.flicker, wmin = 1.8 / W, hw = th * 3.5 + 0.03;
    for (let y = 0; y < H; y++) {
      const dy = ((y + 0.5) / H - 0.5) * 2;
      for (let x = 0; x < W; x++) {
        const dx = ((x + 0.5) / W - 0.5) * 2, xr = dx * ca + dy * sa, yr = -dx * sa + dy * ca, xa = Math.abs(xr) / L;
        let a = 0;
        if (xa < 1) {
          let w = th * Math.pow(1 - xa, p.taper);
          if (fl > 0) w *= 1 + fl * 0.7 * nz.n3(xr * 6 + 3.3, 0.5, 0.5, 0, 0, 0);
          w = Math.max(w, wmin);
          const d = Math.abs(yr) / w;
          if (d < 1) a = Math.pow(1 - d, 1.35) * (1 - 0.25 * xa);
        }
        if (gl > 0) a += gl * 0.7 * Math.exp(-(xa * xa * 2.4 + (yr / hw) * (yr / hw))) + gl * 0.9 * Math.exp(-((dx * dx + dy * dy) / ((0.05 + th * 2) * (0.05 + th * 2))));
        out[y * W + x] = a;
      }
    }
  },
  star(p, W, H, c, out) {
    const N = p.rays, step = TAU / N, rot = (p.rotation * Math.PI) / 180, th = p.thickness, minor = p.minor, rng = mulberry32(p.seed * 31 + 77);
    const lens = []; for (let k = 0; k < N; k++) lens.push(p.length * (1 - p.variation * 0.7 * rng()));
    const mlen = p.length * minor, core = p.core, ci = 1 / (0.05 + 0.09 * core);
    for (let y = 0; y < H; y++) {
      const dy = ((y + 0.5) / H - 0.5) * 2;
      for (let x = 0; x < W; x++) {
        const dx = ((x + 0.5) / W - 0.5) * 2, r = Math.sqrt(dx * dx + dy * dy);
        const ang = Math.atan2(dy, dx) - rot, kf = ang / step, k = Math.round(kf), d = (kf - k) * step;
        const across = r * Math.abs(Math.sin(d)), along = r * Math.cos(d), len = lens[((k % N) + N) % N], fr = along / len;
        let a = 0;
        if (fr < 1) { const wid = th * Math.pow(1 - fr, 0.85) + 0.004; a = Math.exp(-(across / wid) * (across / wid)) * Math.pow(1 - fr, 1.3); }
        if (minor > 0) {
          const kf2 = ang / step - 0.5, k2 = Math.round(kf2), d2 = (kf2 - k2) * step, ac2 = r * Math.abs(Math.sin(d2)), al2 = r * Math.cos(d2), fr2 = al2 / mlen;
          if (fr2 < 1) { const wid = th * 0.8 * Math.pow(1 - fr2, 0.85) + 0.003; a += 0.85 * Math.exp(-(ac2 / wid) * (ac2 / wid)) * Math.pow(1 - fr2, 1.3); }
        }
        a += core * Math.exp(-(r * ci) * (r * ci));
        out[y * W + x] = a;
      }
    }
  },
  ring(p, W, H, c, out) {
    const nz = c.nz, fl = c.frames > 1, tt = fl ? c.prog : 0;
    const R = fl ? p.radius * (0.1 + 0.9 * (1 - (1 - tt) * (1 - tt))) : p.radius, th = fl ? p.thickness * (0.6 + 0.9 * tt) : p.thickness;
    const fade = fl ? 1 - 0.94 * smoothstep(0.25, 1, tt) : 1, z = tt * 2.5, ns = Math.max(1, Math.round(p.noiseScale)), en = p.edgeNoise;
    const NA = 720, tab = new Float32Array(NA + 1);
    for (let i = 0; i <= NA; i++) { const th2 = (i / NA) * TAU; tab[i] = en > 0 ? nz.fbm(Math.cos(th2) * ns * 0.5 + 9.1, Math.sin(th2) * ns * 0.5 + 4.3, z, 3, 0.55, 0, 0, 0) : 0; }
    const trail = 1 + p.trail * 4, soft = p.softness;
    for (let y = 0; y < H; y++) {
      const dy = ((y + 0.5) / H - 0.5) * 2;
      for (let x = 0; x < W; x++) {
        const dx = ((x + 0.5) / W - 0.5) * 2, r = Math.sqrt(dx * dx + dy * dy);
        let n = 0;
        if (en > 0) { const f = ((Math.atan2(dy, dx) + Math.PI) / TAU) * NA, i = Math.min(NA - 1, Math.floor(f)); n = tab[i] + (tab[i + 1] - tab[i]) * (f - i); }
        const Rt = R + n * en * (0.5 * th + 0.03), tht = th * (1 + 0.55 * en * n), dd = r - Rt, wth = Math.max(dd < 0 ? tht * trail : tht, 1e-4);
        const d = Math.abs(dd) / wth, aa = 2 / (W * wth), e0 = clamp(Math.min(1 - soft, 1 - aa));
        out[y * W + x] = (1 - smoothstep(e0, 1, d)) * fade * (1 - smoothstep(0.9, 1, r));
      }
    }
  },
  smoke(p, W, H, c, out) {
    const nz = c.nz, fl = c.frames > 1, pr = fl ? c.prog : 0, sc = p.scale, z = pr * p.evolve * 4, R = p.radius * (1 - (fl ? p.grow * 0.4 * (1 - pr) : 0));
    const thr = 0.5 + (fl ? p.dissolve * pr * 0.42 : 0), sf = 0.04 + p.softness * 0.42, warp = p.warp * 1.7, dens = p.density, pers = p.rough, oct = p.octaves;
    const drift = fl ? -pr * 0.9 : 0;
    for (let y = 0; y < H; y++) {
      const v = (y + 0.5) / H, dy = (v - 0.5) * 2;
      for (let x = 0; x < W; x++) {
        const u = (x + 0.5) / W, dx = (u - 0.5) * 2, r = Math.sqrt(dx * dx + dy * dy) / R;
        if (r >= 1) { out[y * W + x] = 0; continue; }
        const nx = u * sc + 17.3, ny = (v + drift * 0.5) * sc + 3.7;
        let qx = nx, qy = ny;
        if (warp > 0) { qx += warp * nz.fbm(nx * 0.8 + 3.7, ny * 0.8 + 1.1, z, 3, 0.5, 0, 0, 0); qy += warp * nz.fbm(nx * 0.8 + 8.3, ny * 0.8 + 4.9, z + 5, 3, 0.5, 0, 0, 0); }
        const n = nz.fbm(qx, qy, z, oct, pers, 0, 0, 0) * 0.5 + 0.5;
        const s = n * 0.95 + dens * 0.6 - r * r * 0.8;
        out[y * W + x] = smoothstep(thr - sf, thr + sf, s) * (1 - smoothstep(0.8, 1, r));
      }
    }
  },
  // Flame tongue: a body narrowing upwards, noise sway and edge erosion (vertically stretched veins make the tongues);
  // brightness falls off from the axis to the edges and to the tip. The noise moves up by exactly one lattice period, so the flipbook loops.
  flame(p, W, H, c, out) {
    const nz = c.nz, fl = c.frames > 1, t = fl ? c.t : 0, sc = p.scale, ky = sc * 0.55, Py = Math.max(2, Math.ceil(ky) + 1), Pz = 3, z = t * Pz;
    const y0 = 0.9, hgt = p.height * 0.86, Wd = p.width, turb = p.turbulence, sx = sc * 0.8, gw = 0.12 + 0.55 * p.softness;
    for (let y = 0; y < H; y++) {
      const v = (y + 0.5) / H, hn = (y0 - v) / hgt;
      for (let x = 0; x < W; x++) {
        const dx = ((x + 0.5) / W - 0.5) * 2;
        if (hn < -0.1 || hn > 1.02) { out[y * W + x] = 0; continue; }
        const hc = clamp(hn), ny = hn * ky - t * Py;
        const n1 = nz.fbm(dx * sx, ny, z, 4, 0.5, 0, Py, Pz);
        const n2 = nz.fbm(dx * sx * 2.6 + 11, ny, z + 9, 3, 0.55, 0, Py, Pz) * 0.5 + 0.5;
        const sway = n1 * turb * 0.5 * Math.pow(hc, 1.1);
        const wd = Math.max(1e-3, Wd * (1 + 0.35 * turb * n1) * Math.pow(1 - hc, p.taper) * (0.62 + 0.38 * smoothstep(0, 0.22, hc)) * (hn < 0 ? Math.sqrt(Math.max(0, 1 + hn * 10)) : 1));
        const e = 1 - Math.abs(dx - sway) / wd + (n2 - 0.5) * turb * 2.1 * (0.15 + 1.2 * hc);
        if (e <= 0) { out[y * W + x] = 0; continue; }
        let a = smoothstep(0, gw, e) * (0.6 + 0.4 * smoothstep(0, 0.85, e)) * (1 - 0.3 * hc) * (1 - smoothstep(0.8, 1.02, hn));
        a += p.core * Math.pow(Math.min(1, e), 2.2) * Math.pow(1 - hc, 1.2) * 0.45;
        out[y * W + x] = a;
      }
    }
  },
  noise(p, W, H, c, out) {
    const nz = c.nz, fl = c.frames > 1, t = fl ? c.t : 0, tile = p.tileable === 1, S = tile ? Math.max(1, Math.round(p.scale)) : p.scale, per = tile ? S : 0, Pz = 4, z = t * Pz + 0.37, pz = fl ? Pz : 0;
    const mode = p.mode, oct = p.octaves, pers = p.rough, jit = p.jitter, seed = p.seed;
    const vor = mode === 'cells' || mode === 'cracks' || mode === 'mosaic';
    // Voronoi: table of points per cell (modulo the period in seamless mode); in a flipbook the points move along closed orbits
    let P = 0, off = 0, tx, ty, tid;
    if (vor) {
      P = tile ? per : Math.ceil(S) + 5; off = tile ? 0 : 2;
      tx = new Float32Array(P * P); ty = new Float32Array(P * P); tid = new Float32Array(P * P);
      for (let j = 0; j < P; j++) for (let i = 0; i < P; i++) {
        const hx = i - off, hy = j - off, k = j * P + i;
        let fx = 0.5 + jit * (hash2(hx, hy, seed) - 0.5), fy = 0.5 + jit * (hash2(hx, hy, seed + 101) - 0.5);
        if (fl) { const ph1 = hash2(hx, hy, seed + 202), ph2 = hash2(hx, hy, seed + 303), am = 0.3 * jit; fx += am * (Math.sin(TAU * (t + ph1)) - Math.sin(TAU * ph1)); fy += am * (Math.cos(TAU * (t + ph2)) - Math.cos(TAU * ph2)); }
        tx[k] = fx; ty[k] = fy; tid[k] = hash2(hx, hy, seed + 404);
      }
    }
    const at = (gx, gy) => (tile ? (((gy % P) + P) % P) * P + (((gx % P) + P) % P) : (gy + off) * P + gx + off);
    for (let y = 0; y < H; y++) {
      const v = (y + 0.5) / H;
      for (let x = 0; x < W; x++) {
        const u = (x + 0.5) / W, X = u * S, Y = v * S;
        let a;
        if (!vor) {
          if (mode === 'ridged') a = nz.ridged(X, Y, z, oct, pers, per, per, pz);
          else if (mode === 'turbulence') a = nz.turb(X, Y, z, oct, pers, per, per, pz);
          else a = nz.fbm(X, Y, z, oct, pers, per, per, pz) * 0.5 + 0.5;
        } else {
          const cx = Math.floor(X), cy = Math.floor(Y);
          let f1 = 9, id = 0, mx = 0, my = 0;
          for (let j = -1; j <= 1; j++) for (let i = -1; i <= 1; i++) {
            const gx = cx + i, gy = cy + j, k = at(gx, gy), ddx = gx + tx[k] - X, ddy = gy + ty[k] - Y, d = ddx * ddx + ddy * ddy;
            if (d < f1) { f1 = d; id = tid[k]; mx = ddx; my = ddy; }
          }
          if (mode === 'cells') a = 1 - clamp(Math.sqrt(f1) * 1.25);
          else if (mode === 'mosaic') a = id;
          else {   // exact distance to the cell border (via perpendicular bisectors to the neighbours)
            let md = 9;
            for (let j = -2; j <= 2; j++) for (let i = -2; i <= 2; i++) {
              const gx = cx + i, gy = cy + j, k = at(gx, gy), rx = gx + tx[k] - X, ry = gy + ty[k] - Y, ex = rx - mx, ey = ry - my, el = Math.sqrt(ex * ex + ey * ey);
              if (el < 1e-5) continue;
              const dd = ((mx + rx) * 0.5 * ex + (my + ry) * 0.5 * ey) / el;
              if (dd < md) md = dd;
            }
            a = 1 - smoothstep(0, p.edge, md);
          }
        }
        out[y * W + x] = a;
      }
    }
  },
  lightning(p, W, H, c, out) {
    const rng = mulberry32(p.seed * 7919 + 13), M = 0.1;
    const dir = p.direction, j1 = (rng() - 0.5) * 0.2, j2 = (rng() - 0.5) * 0.2;
    const a0 = dir === 'v' ? [0.5 + j1, M] : dir === 'h' ? [M, 0.5 + j1] : [M + 0.02 + j1 * 0.3, M], b0 = dir === 'v' ? [0.5 + j2, 1 - M] : dir === 'h' ? [1 - M, 0.5 + j2] : [1 - M, 1 - M - 0.02 + j2 * 0.3];
    const segs = [], lim = (q) => [clamp(q[0], 0.03, 0.97), clamp(q[1], 0.03, 0.97)];
    const bolt = (a, b, levels, jag, wt, k, depth) => {
      let pts = [a, b];
      for (let l = 0; l < levels; l++) {
        const nx = [pts[0]];
        for (let i = 0; i < pts.length - 1; i++) {
          const [ax, ay] = pts[i], [bx, by] = pts[i + 1], dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy), off = (rng() * 2 - 1) * len * 0.5 * jag;
          nx.push(lim([(ax + bx) / 2 - (dy / (len || 1)) * off, (ay + by) / 2 + (dx / (len || 1)) * off]), pts[i + 1]);
        }
        pts = nx;
      }
      for (let i = 0; i < pts.length - 1; i++) segs.push([pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], wt, k]);
      if (depth > 1) return pts;
      const nb = depth === 0 ? Math.round(p.branches) : Math.round(p.branches * 0.35);
      const mainLen = Math.hypot(b[0] - a[0], b[1] - a[1]);
      for (let b2 = 0; b2 < nb; b2++) {
        const i = 1 + Math.floor(rng() * (pts.length - 3)), pt = pts[i], dd = [pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]], dl = Math.hypot(dd[0], dd[1]) || 1;
        const side = rng() < 0.5 ? -1 : 1, an = side * (0.3 + rng() * 0.6), ca = Math.cos(an), sa = Math.sin(an);
        const ux = (dd[0] * ca - dd[1] * sa) / dl, uy = (dd[0] * sa + dd[1] * ca) / dl, bl = mainLen * (0.12 + rng() * 0.22) * (depth === 0 ? 1 : 0.55);
        bolt(pt, lim([pt[0] + ux * bl, pt[1] + uy * bl]), Math.max(2, levels - 2), jag * 0.9, wt * 0.75, k * 0.7, depth + 1);
      }
      return pts;
    };
    bolt(a0, b0, p.detail, p.jag, 1, 1, 0);
    const core = new Float32Array(W * H); drawSegs(core, W, H, segs, p.thickness); addGlow(core, W, H, p.thickness, p.glow, out);
  },
  trail(p, W, H, c, out) {
    const nz = c.nz, vert = p.orientation === 'v', L = p.length, Wd = p.width, tp = p.taper, st = p.streaks, ss = p.streakScale, head = p.head;
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        // s: 0 tail … 1 head; cr: across, -1..1
        const u = (x + 0.5) / W, v = (y + 0.5) / H, s = vert ? 1 - v : u, cr = (vert ? u : v) * 2 - 1;
        const xs = (s - (1 - L)) / L;
        if (xs <= 0) { out[y * W + x] = 0; continue; }
        const w = Math.max(0.01, Wd * lerp(1, Math.pow(xs, 0.8) * 0.95 + 0.05, tp));
        const ac = Math.abs(cr) / w;
        if (ac >= 1.6) { out[y * W + x] = 0; continue; }
        let a = Math.exp(-ac * ac * (1.6 + 2.4 * (1 - p.softness)));
        a *= Math.pow(xs, 1.5);
        if (st > 0) { const n = nz.fbm(s * 3.2 + 4, cr * ss * 0.5 + 9, 0.5, 4, 0.55, 0, 0, 0) * 0.5 + 0.5; a *= 1 - st * (1 - clamp((n - 0.2) * 1.8)); }
        if (head > 0) { const hx = (s - (1 - 0.06 - 0.02)) / (0.05 + Wd * 0.25), hy = cr / (0.08 + Wd * 0.7); a += head * Math.exp(-(hx * hx + hy * hy)); }
        out[y * W + x] = a * (1 - smoothstep(0.95, 1, s));
      }
    }
  },
  magic(p, W, H, c, out) {
    const rng = mulberry32(p.seed * 2654 + 5), rot = (p.rotation * Math.PI) / 180, th = p.thickness * 0.5, segs = [];
    const P = (rad, a) => [0.5 + 0.5 * rad * Math.cos(a + rot), 0.5 + 0.5 * rad * Math.sin(a + rot)];
    const seg = (a, b, wt, k) => segs.push([a[0], a[1], b[0], b[1], wt, k]);
    const radii = [0.9, 0.84, 0.62, 0.31].slice(0, p.rings), Rs = 0.62;
    for (let i = 0; i < p.points; i++) {   // star {points/skip} inscribed in ring Rs
      const a = (i / p.points) * TAU - Math.PI / 2, b = ((i + p.skip) / p.points) * TAU - Math.PI / 2;
      if (p.skip % p.points !== 0) seg(P(Rs, a), P(Rs, b), 0.95, 1);
    }
    for (let i = 0; i < p.ticks; i++) { const a = (i / p.ticks) * TAU, long = i % 4 === 0; seg(P(0.9, a), P(long ? 0.84 : 0.87, a), 0.8, long ? 0.9 : 0.6); }
    const glyphs = p.runes, gw = 0.045, gh = 0.085;
    for (let g = 0; g < glyphs; g++) {   // rune: a polyline over a 3×4 grid, base turned towards the center
      const a = ((g + 0.5) / glyphs) * TAU, rc = 0.73;
      let gx = Math.floor(rng() * 3), gy = Math.floor(rng() * 4);
      const n = 3 + Math.floor(rng() * 3), pts = [[gx, gy]];
      for (let s = 0; s < n; s++) { let nx = gx, ny = gy; for (let tries = 0; tries < 6 && nx === gx && ny === gy; tries++) { nx = clamp(gx + Math.floor(rng() * 3) - 1, 0, 2); ny = clamp(gy + Math.floor(rng() * 5) - 2, 0, 3); } gx = nx; gy = ny; pts.push([gx, gy]); }
      pts.push([1, 0]); if (rng() < 0.7) pts.unshift([1, 3]);
      const ca = Math.cos(a + rot), sa = Math.sin(a + rot), cx = 0.5 + 0.5 * rc * ca, cy = 0.5 + 0.5 * rc * sa;
      const world = pts.map(([qx, qy]) => { const lx = (qx / 2 - 0.5) * gw, ly = -(qy / 3 - 0.5) * gh; return [cx + lx * -sa + ly * ca, cy + lx * ca + ly * sa]; });
      for (let i = 0; i < world.length - 1; i++) seg(world[i], world[i + 1], 0.9, 0.55);
    }
    const core = new Float32Array(W * H); drawSegs(core, W, H, segs, th);
    const wr = th * 1.0, wr2 = wr * wr, cr = [...radii, 0.035];
    for (let y = 0; y < H; y++) {
      const dy = (y + 0.5) / H - 0.5;
      for (let x = 0; x < W; x++) {
        const dx = (x + 0.5) / W - 0.5, r = Math.sqrt(dx * dx + dy * dy) * 2, i = y * W + x;
        let m = core[i];
        for (let k = 0; k < cr.length; k++) { const q = (r - cr[k]) * 0.5, e = Math.exp(-(q * q) / wr2 * (k === 0 ? 1 : 1.6)); if (e > m) m = e; }
        if (r < 0.035) m = Math.max(m, 1 - r / 0.035);
        core[i] = m;
      }
    }
    addGlow(core, W, H, th, p.glow, out);
  },
};

// ───────────────────────── Types and parameters ─────────────────────────
// Labels are getters: t() runs when the UI reads them, so a language switch applies on the next render.
// N/O/C(key, labelKey, ...): labelKey is a suffix of 'fxtex.param.'; option labels are suffixes of 'fxtex.opt.'.
const withLabel = (o, labelKey) => Object.defineProperty(o, 'label', { get: () => t('fxtex.param.' + labelKey), enumerable: true, configurable: true });
const N = (key, lk, min, max, step, def, extra) => withLabel({ key, min, max, step, default: def, ...extra }, lk);
const O = (key, lk, options, def, extra) => withLabel({ key, options, default: def, ...extra }, lk);
const C = (key, lk, def, extra) => withLabel({ key, type: 'color', default: def, ...extra }, lk);
// Option [value, label] with a translated label (index 1 is a getter, destructuring works as before)
const opt = (value, k) => { const o = [value, '']; Object.defineProperty(o, 1, { get: () => t('fxtex.opt.' + k), enumerable: true }); return o; };
const NO = () => opt(0, 'no'), YES = () => opt(1, 'yes');
const FLIP_OPTIONS = [opt('1x1', 'no'), ['2x2', '2×2'], ['4x4', '4×4'], ['8x8', '8×8']];
const COLOR_ONLY = { key: 'colorMode', value: 'color' };
export const FX_SIZES = [64, 128, 256, 512, 1024];
// Common params: appended to the end of every type's params (group:'common'); when is a visibility condition (all conditions at once)
const COMMON = [
  O('size', 'size', FX_SIZES.map((s) => [s, String(s)]), 512, { group: 'common' }),
  N('seed', 'seed', 0, 99999, 1, 1, { group: 'common', free: true }),
  N('contrast', 'contrast', 0.3, 3, 0.05, 1, { group: 'common' }),
  N('gamma', 'gamma', 0.3, 3, 0.05, 1, { group: 'common' }),
  O('invert', 'invert', [NO(), YES()], 0, { group: 'common' }),
  O('colorMode', 'colorMode', [opt('white', 'white'), opt('color', 'color')], 'white', { group: 'common' }),
  O('colorStops', 'colorStops', [[2, '2'], [3, '3']], 3, { group: 'common', when: [COLOR_ONLY] }),
  C('color1', 'color1', '#a01808', { group: 'common', when: [COLOR_ONLY] }),
  C('color2', 'color2', '#ff7a1a', { group: 'common', when: [COLOR_ONLY] }),
  C('color3', 'color3', '#fff2b0', { group: 'common', when: [COLOR_ONLY, { key: 'colorStops', value: 3 }] }),
];
// title/group are translated getters; groupId is the language-independent group id
const def = (id, groupId, params, extra) => ({ id, get title() { return t('fxtex.type.' + id); }, get group() { return t('fxtex.group.' + groupId); }, groupId, params: [...params, ...COMMON], ...extra });
const FLIP = (lk = 'flipbook') => O('flipbook', lk, FLIP_OPTIONS, '1x1');
export const FX_TEXTURE_TYPES = [
  def('soft', 'particles', [
    N('radius', 'radius', 0.3, 1, 0.01, 0.95), N('softness', 'softness', 0, 1, 0.01, 0.7), N('core', 'brightCore', 0, 1, 0.01, 0.3), N('wobble', 'unevenness', 0, 1, 0.01, 0.2)]),
  def('spark', 'particles', [
    N('length', 'length', 0.2, 1, 0.01, 0.92), N('thickness', 'thickness', 0.004, 0.2, 0.002, 0.026), N('taper', 'taperEnds', 0.5, 3, 0.05, 1.4),
    N('glow', 'glow', 0, 1, 0.01, 0.35), N('angle', 'angle', 0, 180, 1, 45), N('flicker', 'unevenness', 0, 1, 0.01, 0.25)]),
  def('star', 'particles', [
    N('rays', 'rays', 3, 16, 1, 4), N('length', 'rayLength', 0.3, 1, 0.01, 0.95), N('thickness', 'thickness', 0.004, 0.15, 0.002, 0.03),
    N('minor', 'minorRays', 0, 1, 0.01, 0), N('core', 'brightCore', 0, 1, 0.01, 0.55), N('rotation', 'rotation', 0, 180, 1, 0), N('variation', 'rayVariation', 0, 1, 0.01, 0.15)]),
  def('trail', 'particles', [
    N('length', 'length', 0.3, 1, 0.01, 0.95), N('width', 'width', 0.05, 0.8, 0.01, 0.28), N('taper', 'tailTaper', 0, 1, 0.01, 0.75), N('softness', 'softness', 0, 1, 0.01, 0.6),
    N('streaks', 'streaks', 0, 1, 0.01, 0.55), N('streakScale', 'streakFreq', 2, 40, 1, 14), N('head', 'headGlow', 0, 1, 0.01, 0.4),
    O('orientation', 'direction', [opt('h', 'headRight'), opt('v', 'headTop')], 'h')]),
  def('ring', 'waves', [
    N('radius', 'radius', 0.15, 0.95, 0.01, 0.72), N('thickness', 'thickness', 0.01, 0.35, 0.005, 0.05), N('softness', 'softness', 0, 1, 0.01, 0.5), N('trail', 'innerTrail', 0, 1, 0.01, 0),
    N('edgeNoise', 'edgeNoise', 0, 1, 0.01, 0.1), N('noiseScale', 'noiseFreq', 2, 24, 1, 7), FLIP('flipbookExpand')]),
  def('magic', 'waves', [
    N('rings', 'rings', 1, 4, 1, 3), N('points', 'starPoints', 3, 12, 1, 6), N('skip', 'starStep', 1, 5, 1, 2), N('runes', 'runes', 0, 24, 1, 12), N('ticks', 'ticks', 0, 72, 1, 48),
    N('thickness', 'thickness', 0.004, 0.03, 0.001, 0.012), N('glow', 'glow', 0, 1, 0.01, 0.45), N('rotation', 'rotation', 0, 360, 1, 0)]),
  def('smoke', 'smokeFire', [
    N('scale', 'scale', 1, 8, 0.1, 3), N('octaves', 'octaves', 1, 7, 1, 5), N('rough', 'detail', 0.3, 0.8, 0.01, 0.52), N('warp', 'swirl', 0, 1, 0.01, 0.55),
    N('density', 'density', 0.2, 1, 0.01, 0.62), N('softness', 'edgeSoftness', 0, 1, 0.01, 0.55), N('radius', 'puffSize', 0.4, 1, 0.01, 0.92),
    FLIP('flipbookGrowFade'), N('evolve', 'evolve', 0, 1, 0.01, 0.22, { when: [{ key: 'flipbook', not: '1x1' }] }),
    N('grow', 'puffGrow', 0, 1, 0.01, 0.5, { when: [{ key: 'flipbook', not: '1x1' }] }), N('dissolve', 'dissolve', 0, 1, 0.01, 0.5, { when: [{ key: 'flipbook', not: '1x1' }] })]),
  def('flame', 'smokeFire', [
    N('height', 'height', 0.4, 1, 0.01, 0.92), N('width', 'baseWidth', 0.15, 0.8, 0.01, 0.5), N('taper', 'taperTip', 0.3, 1.5, 0.01, 1), N('scale', 'tongueScale', 1, 8, 0.1, 3),
    N('turbulence', 'raggedness', 0, 1, 0.01, 0.65), N('softness', 'softness', 0, 1, 0.01, 0.4), N('core', 'hotCore', 0, 1, 0.01, 0.5), FLIP('flipbookLoop')]),
  def('lightning', 'lightning', [
    O('direction', 'direction', [opt('v', 'vertical'), opt('h', 'horizontal'), opt('d', 'diagonal')], 'v'), N('detail', 'subdivision', 3, 8, 1, 7), N('jag', 'jaggedness', 0, 1, 0.01, 0.55),
    N('branches', 'branches', 0, 10, 1, 5), N('thickness', 'thickness', 0.002, 0.03, 0.001, 0.007), N('glow', 'glow', 0, 1, 0.01, 0.55)]),
  def('noise', 'noise', [
    O('mode', 'kind', [opt('perlin', 'perlin'), opt('ridged', 'ridged'), opt('turbulence', 'turbulence'), opt('cells', 'cells'), opt('cracks', 'cracks'), opt('mosaic', 'mosaic')], 'perlin'),
    N('scale', 'scale', 1, 32, 1, 4), N('octaves', 'octaves', 1, 8, 1, 5), N('rough', 'roughness', 0.2, 0.85, 0.01, 0.5), N('jitter', 'jitter', 0, 1, 0.01, 1), N('edge', 'crackWidth', 0.02, 0.5, 0.01, 0.12),
    O('tileable', 'seamless', [YES(), NO()], 1), FLIP('flipbookLoop')]),
];
const TYPE_BY_ID = new Map(FX_TEXTURE_TYPES.map((t) => [t.id, t]));

// Ready-made recipes: values on top of the type defaults; title is a getter (key fxtex.preset.<camelCase id>)
const camel = (id) => id.replace(/-(\w)/g, (m, c) => c.toUpperCase());
const R = (id, type, params) => ({ id, get title() { return t('fxtex.preset.' + camel(id)); }, type, params });
export const FX_TEXTURE_PRESETS = [
  R('soft-glow', 'soft', { size: 256, softness: 0.75, core: 0.35, seed: 3 }),
  R('spark', 'spark', { size: 256, seed: 12 }),
  R('star', 'star', { size: 256, rays: 4, minor: 0.45, core: 0.6, seed: 4 }),
  R('star-8', 'star', { size: 256, rays: 8, length: 0.9, thickness: 0.02, minor: 0, core: 0.7, rotation: 22, variation: 0.35, seed: 9 }),
  R('ring-clean', 'ring', { size: 512, radius: 0.74, thickness: 0.035, softness: 0.45, edgeNoise: 0, seed: 2 }),
  R('shockwave', 'ring', { size: 512, radius: 0.8, thickness: 0.03, softness: 0.5, trail: 1, edgeNoise: 0.3, seed: 5 }),
  R('shockwave-flip', 'ring', { size: 1024, flipbook: '4x4', radius: 0.82, thickness: 0.035, softness: 0.55, trail: 1, edgeNoise: 0.35, seed: 6 }),
  R('smoke-puff', 'smoke', { size: 512, seed: 7 }),
  R('smoke-flip', 'smoke', { size: 1024, flipbook: '4x4', density: 0.64, radius: 0.96, seed: 8 }),
  R('flame-tongue', 'flame', { size: 512, seed: 21 }),
  R('flame-flip', 'flame', { size: 1024, flipbook: '4x4', seed: 22 }),
  R('lightning', 'lightning', { size: 512, seed: 31 }),
  R('lightning-h', 'lightning', { size: 512, direction: 'h', branches: 7, jag: 0.7, thickness: 0.006, seed: 17 }),
  R('perlin-tile', 'noise', { size: 512, mode: 'perlin', scale: 4, octaves: 5, tileable: 1, seed: 40 }),
  R('ridged-tile', 'noise', { size: 512, mode: 'ridged', scale: 3, octaves: 5, tileable: 1, seed: 41 }),
  R('voronoi-cells', 'noise', { size: 512, mode: 'cells', scale: 6, tileable: 1, seed: 42 }),
  R('voronoi-cracks', 'noise', { size: 512, mode: 'cracks', scale: 5, edge: 0.1, tileable: 1, seed: 43 }),
  R('noise-flip', 'noise', { size: 1024, mode: 'perlin', scale: 3, octaves: 4, flipbook: '4x4', tileable: 1, seed: 44 }),
  R('trail', 'trail', { size: 512, seed: 50 }),
  R('magic-circle', 'magic', { size: 512, seed: 60 }),
];

// ───────────────────────── Parameter parsing ─────────────────────────
// Unknown type: the message is translated here; i18nKey/i18nVars let the page re-translate an error sent back by the worker
function unknownType(type) {
  const key = 'fxtex.error.unknownType', vars = { type: String(type) };
  return Object.assign(new Error(t(key, vars)), { i18nKey: key, i18nVars: vars });
}
const HEX = /^#?([0-9a-f]{6}|[0-9a-f]{3})$/i;
function normHex(v, fallback) {
  const m = typeof v === 'string' ? v.trim().match(HEX) : null;
  if (!m) return fallback;
  let h = m[1].toLowerCase();
  if (h.length === 3) h = h.split('').map((x) => x + x).join('');
  return '#' + h;
}
export function fxDefaultParams(type) {
  const t = TYPE_BY_ID.get(type);
  if (!t) throw unknownType(type);
  return Object.fromEntries(t.params.map((q) => [q.key, q.default]));
}
// Full parameter set: defaults + values coerced to the type and range (garbage is replaced by the default)
export function resolveFxParams(type, params = {}) {
  const t = TYPE_BY_ID.get(type);
  if (!t) throw unknownType(type);
  const out = {};
  for (const q of t.params) {
    const raw = params == null ? undefined : params[q.key];
    if (q.type === 'color') out[q.key] = normHex(raw, q.default);
    else if (q.options) { const hit = q.options.find(([v]) => String(v) === String(raw)); out[q.key] = hit ? hit[0] : q.default; }
    else {
      let v = Number(raw);
      if (raw === undefined || raw === null || raw === '' || !Number.isFinite(v)) v = q.default;
      if (q.free) v = Math.floor(Math.abs(v)) >>> 0; else { v = clamp(v, q.min, q.max); if (q.step >= 1) v = Math.round(v); }
      out[q.key] = v;
    }
  }
  return out;
}
// Parameter visibility in the UI: when is an array of conditions {key,value} or {key,not}
export function fxParamVisible(q, params) {
  return !q.when || q.when.every((c) => (c.not !== undefined ? String(params[c.key]) !== String(c.not) : String(params[c.key]) === String(c.value)));
}
export function fxFlipbookGrid(params) {
  const m = typeof params?.flipbook === 'string' ? params.flipbook.match(/^(\d+)x(\d+)$/) : null;
  return m && (+m[1] > 1 || +m[2] > 1) ? { h: +m[1], v: +m[2] } : null;
}
// File name: ASCII, built from the type id and params only (language-independent)
export function fxFileName(type, params) {
  const p = resolveFxParams(type, params), fb = fxFlipbookGrid(p);
  return `fx_${type}_${p.seed}${fb ? `_${fb.h}x${fb.v}` : ''}.png`;
}

// ───────────────────────── Building the result ─────────────────────────
// Window at the frame edges: the shape is guaranteed to reach zero, flipbook frames do not bleed into each other
function guardEdges(buf, W, H, g) {
  const gx = Math.max(1, Math.round(W * g)), gy = Math.max(1, Math.round(H * g));
  for (let y = 0; y < H; y++) {
    const fy = y < gy ? smoothstep(0, gy, y + 0.5) : y >= H - gy ? smoothstep(0, gy, H - y - 0.5) : 1;
    for (let x = 0; x < W; x++) {
      const fx = x < gx ? smoothstep(0, gx, x + 0.5) : x >= W - gx ? smoothstep(0, gx, W - x - 0.5) : 1;
      if (fx * fy < 1) buf[y * W + x] *= fx * fy;
    }
  }
}
const GUARDED = new Set(['soft', 'spark', 'star', 'ring', 'smoke', 'flame', 'magic', 'lightning', 'trail']);
const hexRgb = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];

function finish(field, size, p) {
  const n = size * size, data = new Uint8ClampedArray(n * 4), k = p.contrast, ig = 1 / p.gamma, inv = p.invert === 1, color = p.colorMode === 'color';
  const stops = color ? [hexRgb(p.color1), hexRgb(p.color2), hexRgb(p.color3)] : null, three = p.colorStops === 3;
  for (let i = 0; i < n; i++) {
    let v = field[i];
    if (!(v > 0)) v = 0; else if (v > 1) v = 1;
    if (k !== 1 && v > 0 && v < 1) { const a = Math.pow(v, k), b = Math.pow(1 - v, k); v = a / (a + b); }
    if (ig !== 1 && v > 0) v = Math.pow(v, ig);
    if (inv) v = 1 - v;
    // stochastic rounding (dithering): preserves the expectation, 0 and 255 stay exact
    const al = Math.min(255, Math.floor(v * 255 + hash2(i, size, p.seed)));
    const o = i * 4;
    if (!color) { data[o] = 255; data[o + 1] = 255; data[o + 2] = 255; }
    else {
      let c0, c1, f;
      if (three) { if (v < 0.5) { c0 = stops[0]; c1 = stops[1]; f = v * 2; } else { c0 = stops[1]; c1 = stops[2]; f = (v - 0.5) * 2; } } else { c0 = stops[0]; c1 = stops[1]; f = v; }
      data[o] = Math.round(c0[0] + (c1[0] - c0[0]) * f); data[o + 1] = Math.round(c0[1] + (c1[1] - c0[1]) * f); data[o + 2] = Math.round(c0[2] + (c1[2] - c0[2]) * f);
    }
    data[o + 3] = al;
  }
  return data;
}

// -> { width, height, data: Uint8ClampedArray RGBA, flipbook: {h,v}|null, tileable }
export function generateFxTexture(type, params = {}) {
  const t = TYPE_BY_ID.get(type);
  if (!t) throw unknownType(type);
  const p = resolveFxParams(type, params), size = p.size, gen = GEN[type], fb = fxFlipbookGrid(p);
  const c = { nz: makeNoise(p.seed), frames: 1, t: 0, prog: 1 };
  let field;
  if (!fb) {
    field = new Float32Array(size * size);
    gen(p, size, size, c, field);
    if (GUARDED.has(type)) guardEdges(field, size, size, 0.035);
  } else {
    const fw = Math.floor(size / fb.h), fh = Math.floor(size / fb.v), frames = fb.h * fb.v, tmp = new Float32Array(fw * fh);
    field = new Float32Array(size * size);
    for (let f = 0; f < frames; f++) {
      c.frames = frames; c.t = f / frames; c.prog = f / (frames - 1);
      tmp.fill(0);
      gen(p, fw, fh, c, tmp);
      if (GUARDED.has(type)) guardEdges(tmp, fw, fh, 0.035);
      const ox = (f % fb.h) * fw, oy = Math.floor(f / fb.h) * fh;
      for (let y = 0; y < fh; y++) field.set(tmp.subarray(y * fw, y * fw + fw), (oy + y) * size + ox);
    }
  }
  return { width: size, height: size, data: finish(field, size, p), flipbook: fb, tileable: type === 'noise' && p.tileable === 1 };
}

// ───────────────────────── Lossless PNG ─────────────────────────
// Exact RGBA bytes (canvas.toBlob premultiplies and spoils the color of nearly transparent pixels). Sub filter, zlib via CompressionStream.
const CRC_TABLE = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
function crc32(bytes) { let c = 0xffffffff; for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; }
function pngChunk(type, body) {
  const out = new Uint8Array(12 + body.length), dv = new DataView(out.buffer);
  dv.setUint32(0, body.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(body, 8);
  dv.setUint32(8 + body.length, crc32(out.subarray(4, 8 + body.length)));
  return out;
}
export async function encodeFxPng(tex) {
  const { width: w, height: h, data } = tex, row = w * 4, raw = new Uint8Array((row + 1) * h);
  for (let y = 0; y < h; y++) {
    const o = y * (row + 1), s = y * row;
    raw[o] = 1;   // Sub: byte minus the byte to the left (one pixel back)
    for (let i = 0; i < row; i++) raw[o + 1 + i] = (data[s + i] - (i >= 4 ? data[s + i - 4] : 0)) & 255;
  }
  const z = new Uint8Array(await new Response(new Blob([raw]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer());
  const ihdr = new Uint8Array(13), dv = new DataView(ihdr.buffer);
  dv.setUint32(0, w); dv.setUint32(4, h); ihdr[8] = 8; ihdr[9] = 6;   // 8 bit, RGBA
  const parts = [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), pngChunk('IHDR', ihdr), pngChunk('IDAT', z), pngChunk('IEND', new Uint8Array(0))];
  const png = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0; for (const p of parts) { png.set(p, at); at += p.length; }
  return png;
}
