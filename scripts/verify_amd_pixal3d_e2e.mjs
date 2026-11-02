import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { deflateSync } from 'node:zlib';
import { validateGeneratedMesh } from '../server/mesh-validation.mjs';

const base = process.env.ASTRA_URL || 'http://127.0.0.1:3210';
const sleep = (ms) => new Promise((resolveSleep) => setTimeout(resolveSleep, ms));

export function parseOptions(env = process.env) {
  const views = env.AMD_PIXAL3D_VIEWS || 'single';
  const quality = env.AMD_PIXAL3D_QUALITY || 'draft';
  const vertexColors = env.AMD_PIXAL3D_VERTEX_COLORS === '1';
  if (!['single', 'multi'].includes(views)) throw new Error('AMD_PIXAL3D_VIEWS must be single or multi.');
  if (!['draft', 'standard'].includes(quality)) throw new Error('AMD_PIXAL3D_QUALITY must be draft or standard.');
  if (vertexColors && views !== 'single') throw new Error('AMD Pixal3D vertex colors require the single-view workflow.');
  return { views, jobId: env.AMD_PIXAL3D_JOB_ID || null, quality, vertexColors };
}

async function request(path, options = {}) {
  const response = await fetch(base + path, {
    ...options,
    headers: { 'x-astra-client': 'studio', ...options.headers },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${path}: ${response.status}`);
  return body;
}

async function download(path, destination) {
  const response = await fetch(base + path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  writeFileSync(destination, bytes);
  return bytes;
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const name = Buffer.from(type);
  const chunk = Buffer.alloc(12 + data.length);
  chunk.writeUInt32BE(data.length, 0);
  name.copy(chunk, 4);
  data.copy(chunk, 8);
  chunk.writeUInt32BE(crc32(Buffer.concat([name, data])), 8 + data.length);
  return chunk;
}

/** Deterministic RGBA test object; no external image model or network asset is involved. */
function conceptPng(slot = 'front') {
  const size = 256;
  const raw = Buffer.alloc((size * 4 + 1) * size);
  const slotShift = { front: 0, left: 45, back: 100, right: 155 }[slot] || 0;
  for (let y = 0; y < size; y += 1) {
    const row = y * (size * 4 + 1);
    raw[row] = 0;
    for (let x = 0; x < size; x += 1) {
      const dx = (x - 128) / 82;
      const dy = (y - 132) / 96;
      const radius = dx * dx + dy * dy;
      const offset = row + 1 + x * 4;
      if (radius <= 1) {
        const light = Math.sqrt(1 - radius);
        raw[offset] = Math.round(45 + 150 * light + slotShift * 0.25);
        raw[offset + 1] = Math.round(65 + 125 * light);
        raw[offset + 2] = Math.round(95 + 130 * light - slotShift * 0.2);
        raw[offset + 3] = 255;
      }
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(raw, { level: 9 })),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

async function uploadAsset(bytes, name) {
  const form = new FormData();
  form.append('file', new Blob([bytes], { type: 'image/png' }), name);
  return request('/api/assets', { method: 'POST', body: form });
}

async function createInput(views) {
  const front = await uploadAsset(conceptPng('front'), `amd-pixal3d-${views}-front.png`);
  let viewSetId;
  if (views === 'multi') {
    const set = await request('/api/views', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ imageId: front.id }),
    });
    viewSetId = set.id;
    for (const slot of ['left', 'back', 'right']) {
      const form = new FormData();
      form.append('file', new Blob([conceptPng(slot)], { type: 'image/png' }), `amd-pixal3d-${slot}.png`);
      const updated = await request(`/api/views/${viewSetId}/${slot}`, { method: 'PUT', body: form });
      if (!updated.canBuild) throw new Error(`View set is not ready after uploading ${slot}.`);
    }
  }
  return { imageId: front.id, viewSetId };
}

async function waitForStudio() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { if ((await request('/api/health')).app === 'AstraForge') return; } catch { /* starting */ }
    await sleep(1000);
  }
  throw new Error('AstraForge did not start.');
}

async function main() {
  const options = parseOptions();
  await waitForStudio();
  await request('/api/runtime/pixal3d/start', { method: 'POST' });
  let job;
  if (options.jobId) {
    job = (await request('/api/jobs')).find((item) => item.id === options.jobId);
    if (!job) throw new Error(`Job not found: ${options.jobId}`);
    if (job.engine !== 'pixal3d') throw new Error(`Job ${job.id} is not a Pixal3D job.`);
  } else {
    const input = await createInput(options.views);
    job = await request('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...input, engine: 'pixal3d', quality: options.quality, vertexColors: options.vertexColors, seed: 42, backgroundMode: 'alpha', hazeFilter: 'auto',
      }),
    });
  }
  console.error(`Pixal3D job: ${job.id}`);
  let result = job;
  let previousMessage = '';
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    result = (await request('/api/jobs')).find((item) => item.id === job.id);
    if (!result) throw new Error(`Job disappeared: ${job.id}`);
    if (result.message !== previousMessage) {
      console.error(`[${result.status} ${result.progress ?? 0}%] ${result.message || ''}`);
      previousMessage = result.message;
    }
    if (['failed', 'cancelled'].includes(result.status)) throw new Error(result.message || `Job ${result.status}.`);
    if (result.status === 'completed') break;
    if (attempt === 1199) throw new Error(`Generation timed out; resume with AMD_PIXAL3D_JOB_ID=${job.id}`);
    await sleep(3000);
  }
  if (!result.asset?.id) throw new Error('Completed Pixal3D job has no GLB asset.');
  const validation = await request(`/api/jobs/${job.id}/validate`, { method: 'POST' });
  if (!validation.accepted) throw new Error('GLB validation failed.');
  const out = resolve('data/runtime');
  mkdirSync(out, { recursive: true });
  const png = await download(`/api/assets/${result.imageId}`, resolve(out, `amd-pixal3d-${options.views}-input.png`));
  const suffix = options.vertexColors ? '-colors' : options.quality === 'standard' ? '-standard' : '';
  const glbPath = resolve(out, `amd-pixal3d-${options.views}${suffix}.glb`);
  const glb = await download(`/api/assets/${result.asset.id}`, glbPath);
  if (png.length < 100 || png.subarray(1, 4).toString() !== 'PNG') throw new Error('Invalid input PNG.');
  const geometry = validateGeneratedMesh(glb, { requireVertexColors: options.vertexColors });
  console.log(JSON.stringify({
    job: job.id,
    engine: result.engine,
    views: options.views,
    quality: options.quality,
    vertexColors: options.vertexColors,
    pngBytes: png.length,
    glbBytes: glb.length,
    glbPath,
    geometry,
    pixal3d: result.pixal3d,
    validation,
  }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
