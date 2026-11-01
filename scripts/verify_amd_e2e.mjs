import {writeFileSync, mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

const base = 'http://127.0.0.1:3210';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function request(path, options = {}) {
  const response = await fetch(base + path, {
    ...options,
    headers: {'x-astra-client': 'studio', ...options.headers}
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

for (let i = 0; i < 100; i += 1) {
  try { if ((await request('/api/health')).app === 'AstraForge') break; } catch {}
  if (i === 99) throw new Error('AstraForge did not start');
  await sleep(1000);
}
await request('/api/runtime/start', {method: 'POST'});
for (let i = 0; i < 200; i += 1) {
  const status = await request('/api/status');
  if (status.comfy.ready) break;
  if (i === 199) throw new Error('ComfyUI is not ready');
  await sleep(3000);
}
const requestedJobId = process.env.AMD_E2E_JOB_ID;
const job = requestedJobId
  ? (await request('/api/jobs')).find((item) => item.id === requestedJobId)
  : await request('/api/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: 'a single simple red cube on a plain white background',
        quality: 'draft', shapeModel: 'default', meshLevel: 0,
        backgroundMode: 'auto', hazeFilter: 'auto', seed: 42, engine: 'hunyuan'
      })
    });
if (!job) throw new Error(`Job not found: ${requestedJobId}`);
let result;
for (let i = 0; i < 1200; i += 1) {
  result = (await request('/api/jobs')).find((item) => item.id === job.id);
  if (result.status === 'failed' || result.status === 'cancelled') throw new Error(result.message);
  if (result.status === 'completed') break;
  if (i === 1199) throw new Error('Generation timed out');
  await sleep(3000);
}
const validation = await request(`/api/jobs/${job.id}/validate`, {method: 'POST'});
if (!validation.accepted) throw new Error('GLB validation failed');
const out = resolve('data/runtime');
mkdirSync(out, {recursive: true});
const png = await download(`/api/assets/${result.concept.id}`, resolve(out, 'amd-e2e-concept.png'));
const glb = await download(`/api/assets/${result.asset.id}`, resolve(out, 'amd-e2e-mesh.glb'));
if (png.length < 1000 || png.subarray(1, 4).toString() !== 'PNG') throw new Error('Invalid SDXL PNG');
if (glb.length < 1000 || glb.subarray(0, 4).toString() !== 'glTF') throw new Error('Invalid Hunyuan3D GLB');
console.log(JSON.stringify({job: job.id, pngBytes: png.length, glbBytes: glb.length, validation}));
