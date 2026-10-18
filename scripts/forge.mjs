#!/usr/bin/env node
// AstraForge Forge CLI (docs/forge/API.md, "Command line").
//
//   node scripts/forge.mjs <command> [options] [--direct] [--json] [--server URL]
//
// By default it talks to a running studio server (header x-astra-client: studio);
// --direct runs the same pipeline engine in-process without the server.
import path from 'node:path';
import { existsSync, openAsBlob, promises as fs } from 'node:fs';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

const HELP = `AstraForge Forge — command line

Usage: node scripts/forge.mjs <command> [options]

Commands:
  status                                   state: Blender, Godot, library, queue, GPU lock
  steps | presets                          step registry and ready-made pipelines
  assets                                   list of passports
  show <assetId>                           passport: versions, pipeline, exports
  create --kind <type> --name <name> [source] [--preset id | --steps file.json]
         source: --from-glb <path> (GLB/FBX/OBJ) | --from-job <jobId> | --from-asset <id> | --params <json|file>
  run <assetId> (--steps file.json | --preset id) [--from source|latest|v003]
  export <assetId> --profile godot|iso8 [--params <json|file>] [--out <folder>]
  batch <file.json>                        batch: {"items":[{name, kind, source, preset|steps}]}
  clips [list] | clips import <folder> | clips remove <clipId>
  jobs | job <jobId> [--wait] | cancel <jobId>

Common options:
  --direct        no server, in this process (same pipeline module)
  --json          JSON output
  --server <URL>  studio address (default http://127.0.0.1:$PORT or :3210)
  --no-wait       do not wait for the job to finish (server mode only; ignored with --direct)
`;

// ------------------------------------------------------------------ args

const BOOL_FLAGS = new Set(['direct', 'json', 'no-wait', 'wait', 'help', 'quiet', 'unzip']);

export function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '-h') { flags.help = true; continue; }
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      const key = eq > 0 ? a.slice(2, eq) : a.slice(2);
      if (eq > 0) flags[key] = a.slice(eq + 1);
      else if (BOOL_FLAGS.has(key)) flags[key] = true;
      else if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) flags[key] = argv[++i];
      else flags[key] = true;
    } else {
      positional.push(a);
    }
  }
  return { positional, flags };
}

class CliError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.exitCode = exitCode;
  }
}

async function readJsonArg(value, name) {
  if (value === undefined || value === true) throw new CliError(`Option --${name} needs a value.`, 2);
  const text = String(value).trim();
  if (text.startsWith('{') || text.startsWith('[')) {
    try { return JSON.parse(text); } catch { throw new CliError(`--${name}: invalid JSON.`, 2); }
  }
  const file = path.resolve(text);
  if (!existsSync(file)) throw new CliError(`--${name}: file not found: ${file}`, 2);
  try {
    return JSON.parse((await fs.readFile(file, 'utf8')).replace(/^﻿/, ''));
  } catch {
    throw new CliError(`--${name}: file ${file} is not JSON.`, 2);
  }
}

// ------------------------------------------------------------------ clients

class HttpClient {
  constructor(base) {
    this.base = base.replace(/\/+$/, '');
    this.mode = 'http';
  }

  async request(method, url, body) {
    const headers = { 'x-astra-client': 'studio' };
    let payload;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined) { headers['content-type'] = 'application/json'; payload = JSON.stringify(body); }
    let res;
    try {
      res = await fetch(this.base + url, { method, headers, body: payload });
    } catch (err) {
      throw new CliError(`Studio server is unreachable (${this.base}): ${err.cause?.code || err.message}. Start the studio (Start.cmd) or use --direct.`);
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!res.ok) throw new CliError(data?.error ? `${data.error}${data.code ? ` [${data.code}]` : ''}` : `HTTP ${res.status}: ${text.slice(0, 300)}`);
    return data;
  }

  status() { return this.request('GET', '/api/forge/status'); }
  steps() { return this.request('GET', '/api/forge/steps'); }
  presets() { return this.request('GET', '/api/forge/presets'); }
  listAssets() { return this.request('GET', '/api/forge/assets'); }
  getAsset(id) { return this.request('GET', `/api/forge/assets/${encodeURIComponent(id)}`); }
  listJobs() { return this.request('GET', '/api/forge/jobs'); }
  getJob(id) { return this.request('GET', `/api/forge/jobs/${encodeURIComponent(id)}`); }
  cancelJob(id) { return this.request('POST', `/api/forge/jobs/${encodeURIComponent(id)}/cancel`, {}); }
  run(id, body) { return this.request('POST', `/api/forge/assets/${encodeURIComponent(id)}/run`, body); }
  exportAsset(id, body) { return this.request('POST', `/api/forge/assets/${encodeURIComponent(id)}/export`, body); }
  clips() { return this.request('GET', '/api/forge/clips'); }
  removeClip(id) { return this.request('DELETE', `/api/forge/clips/${encodeURIComponent(id)}`); }
  getBatch(id) { return this.request('GET', `/api/forge/batch/${encodeURIComponent(id)}`); }

  async createAsset(meta, filePath) {
    if (!filePath) return this.request('POST', '/api/forge/assets', meta);
    const form = new FormData();
    form.append('name', meta.name || '');
    form.append('kind', meta.kind);
    form.append('source', JSON.stringify({ type: 'upload' }));
    if (meta.profile) form.append('profile', JSON.stringify(meta.profile));
    form.append('file', await openAsBlob(filePath), path.basename(filePath));
    return this.request('POST', '/api/forge/assets', form);
  }

  async importClips(folder) {
    const files = await fbxFiles(folder);
    const form = new FormData();
    for (const f of files) form.append('files[]', await openAsBlob(f), path.basename(f));
    return this.request('POST', '/api/forge/clips', form);
  }

  async batch(items, filePaths) {
    if (!filePaths.length) return this.request('POST', '/api/forge/batch', { items });
    const form = new FormData();
    form.append('items', JSON.stringify(items));
    for (const f of filePaths) form.append('files[]', await openAsBlob(f), path.basename(f));
    return this.request('POST', '/api/forge/batch', form);
  }

  async download(fileUrl, dest) {
    const res = await fetch(this.base + fileUrl, { headers: { 'x-astra-client': 'studio' } });
    if (!res.ok) throw new CliError(`Failed to download ${fileUrl}: HTTP ${res.status}`);
    await fs.writeFile(dest, Buffer.from(await res.arrayBuffer()));
    return dest;
  }
}

class DirectClient {
  constructor(engine) {
    this.engine = engine;
    this.mode = 'direct';
  }

  status() { return this.engine.status(); }
  async steps() { return (await import('../server/forge-steps.mjs')).listSteps(); }
  async presets() { return (await import('../server/forge-steps.mjs')).listPresets(); }
  listAssets() { return this.engine.listAssets(); }
  getAsset(id) { return this.engine.getAsset(id); }
  listJobs() { return this.engine.listJobs(); }
  async getJob(id) { return JSON.parse(JSON.stringify(await this.engine.getJob(id))); }
  cancelJob(id) { return this.engine.cancelJob(id); }
  run(id, body) { return this.engine.startRun(id, body); }
  exportAsset(id, body) { return this.engine.startExport(id, body); }
  clips() { return this.engine.clipIndex(); }
  removeClip(id) { return this.engine.removeClip(id); }
  getBatch(id) { return this.engine.getBatch(id); }
  createAsset(meta, filePath) {
    return this.engine.createAsset(meta, filePath ? { path: path.resolve(filePath), originalName: path.basename(filePath) } : null);
  }
  importClips(folder) { return this.engine.importClips({ folder: path.resolve(folder) }); }
  batch(items, filePaths) { return this.engine.startBatch({ items }, filePaths.map((p) => ({ path: p, originalName: path.basename(p) }))); }
  async download(absPath, dest) {
    await fs.copyFile(absPath, dest);
    return dest;
  }
}

async function fbxFiles(folder) {
  const dir = path.resolve(folder);
  const st = await fs.stat(dir).catch(() => null);
  if (!st?.isDirectory()) throw new CliError(`Folder not found: ${dir}`);
  const files = (await fs.readdir(dir)).filter((n) => n.toLowerCase().endsWith('.fbx')).sort().map((n) => path.join(dir, n));
  if (!files.length) throw new CliError(`No .fbx files in the folder: ${dir}`);
  return files;
}

// ------------------------------------------------------------------ output helpers

const STATUS_LABEL = {
  queued: 'queued', running: 'running', completed: 'done', failed: 'failed', cancelled: 'cancelled',
  skipped: 'skipped', done: 'done', pending: 'pending', new: 'new', partial: 'partial', created: 'created',
};
const statusLabel = (s) => STATUS_LABEL[s] || s;

function makeOut(flags) {
  const json = !!flags.json;
  return {
    json,
    log: (...a) => { if (!json && !flags.quiet) console.log(...a); },
    info: (...a) => { if (!json) console.log(...a); },
    result: (value, human) => {
      if (json) console.log(JSON.stringify(value, null, 2));
      else if (human) human(value);
    },
  };
}

function printJobSummary(job, out) {
  out.info(`Job ${job.id}: ${statusLabel(job.status)}${job.version ? ` · version ${job.version}` : ''}`);
  for (const s of job.steps || []) {
    out.info(`  [${String(s.index).padStart(2, '0')}] ${s.step}: ${statusLabel(s.status)}${s.status === 'failed' && s.result?.error ? ` — ${s.result.error}` : ''}`);
    const outputs = s.result?.outputs || {};
    for (const [k, v] of Object.entries(outputs)) out.info(`       ${k}: ${Array.isArray(v) ? v.join(', ') : v}`);
    for (const w of s.warnings || []) out.info(`       warning: ${w}`);
    if (s.status === 'failed' && s.logFile) out.info(`       log: ${s.logFile}`);
  }
  if (job.error && job.status !== 'completed') out.info(`Error: ${job.error}`);
}

/** Poll a job until it finishes, printing step transitions and progress. */
async function watchJob(client, jobId, out, { onInterrupt } = {}) {
  let lastKey = '';
  let lastPrint = 0;
  let interrupted = false;
  const sigint = () => {
    if (interrupted) process.exit(130);
    interrupted = true;
    void onInterrupt?.();
  };
  process.on('SIGINT', sigint);
  try {
    for (;;) {
      const job = await client.getJob(jobId);
      const current = (job.steps || []).find((s) => s.status === 'running') || null;
      const key = `${job.status}|${current?.index || ''}|${current?.status || ''}`;
      if (key !== lastKey) {
        lastKey = key;
        if (current) out.log(`> [${current.index}/${job.steps.length}] ${current.step}: running`);
        else if (job.status === 'queued') out.log('… job queued');
      } else if (current && Date.now() - lastPrint > 3000 && current.message) {
        lastPrint = Date.now();
        out.log(`  ${current.progress ? `${Math.round(current.progress)}% ` : ''}${current.message}`);
      }
      if (['completed', 'failed', 'cancelled'].includes(job.status)) return job;
      await new Promise((r) => setTimeout(r, 1000));
    }
  } finally {
    process.off('SIGINT', sigint);
  }
}

async function waitAndReport(client, jobId, out, flags) {
  if (flags['no-wait'] && client.mode !== 'direct') {
    const job = await client.getJob(jobId);
    out.result(job, () => out.info(`Job started: ${jobId}. Check with: node scripts/forge.mjs job ${jobId}`));
    return job;
  }
  const job = await watchJob(client, jobId, out, {
    onInterrupt: async () => {
      if (client.mode === 'direct') {
        out.info('Cancelling the job…');
        await client.cancelJob(jobId).catch(() => {});
      } else {
        out.info(`Stopped watching. The job continues on the server: ${jobId}`);
        process.exit(130);
      }
    },
  });
  out.result(job, (j) => printJobSummary(j, out));
  if (job.status !== 'completed') throw new CliError(job.status === 'cancelled' ? 'Job cancelled.' : `Job failed: ${job.error || ''}`, 1);
  return job;
}

// ------------------------------------------------------------------ commands

async function cmdStatus(client, out) {
  const s = await client.status();
  out.result(s, () => {
    out.info(`Blender: ${s.blender?.ok ? `${s.blender.version} (${s.blender.path})` : `not available — ${s.blender?.error || ''}`}`);
    out.info(`Godot:   ${s.godot?.ok ? `${s.godot.version} (${s.godot.path})` : `not available — ${s.godot?.error || ''}`}`);
    out.info(`Library: clips ${s.library?.clips ?? 0}, assets ${s.library?.assets ?? '?'}${s.library?.path ? ` (${s.library.path})` : ''}`);
    out.info(`Blender queue: running ${s.queue?.running ?? 0}, waiting ${s.queue?.queued ?? 0}`);
    out.info(`GPU lock: ${s.gpuLock?.name} — ${s.gpuLock?.held ? 'held by this process' : 'not held by this process'}`);
  });
}

async function cmdSteps(client, out) {
  const steps = await client.steps();
  out.result(steps, () => {
    for (const s of steps) {
      out.info(`${s.step.padEnd(18)} ${s.title}${s.gpu === true ? ' [GPU]' : s.gpu === 'conditional' ? ' [GPU conditional]' : ''} · ${s.kinds.join(', ') || 'library'} · ${Math.round(s.timeoutSec / 60)} min`);
    }
  });
}

async function cmdPresets(client, out) {
  const presets = await client.presets();
  out.result(presets, () => {
    for (const p of presets) out.info(`${p.id.padEnd(20)} [${p.kind}] ${p.title}: ${p.steps.map((s) => s.step).join(' → ')}`);
  });
}

async function cmdAssets(client, out) {
  const list = await client.listAssets();
  out.result(list, () => {
    if (!list.length) return out.info('No passports yet.');
    for (const a of list) out.info(`${a.id}  ${a.kind.padEnd(9)} ${statusLabel(a.lastStatus).padEnd(12)} ${a.name}`);
  });
}

async function cmdShow(client, out, id) {
  if (!id) throw new CliError('Specify the asset ID.', 2);
  const m = await client.getAsset(id);
  out.result(m, () => {
    out.info(`${m.name} [${m.kind}] ${m.id}`);
    out.info(`Source: ${m.source?.type}${m.source?.file ? ` (${m.source.file})` : ''}`);
    for (const v of m.versions || []) out.info(`  ${v.version}: ${statusLabel(v.status)} · from ${v.base || 'source'} · ${(v.steps || []).join(' → ')}`);
    if ((m.pipeline || []).length) out.info('Pipeline:');
    for (const e of m.pipeline || []) out.info(`  ${e.version}/${e.dir || ''} ${e.step}: ${statusLabel(e.status)}${e.result?.error ? ` — ${e.result.error}` : ''}`);
    for (const e of m.exports || []) out.info(`Export ${e.profile} (${e.version}): ${e.path}`);
  });
}

async function stepsFromFlags(flags) {
  if (flags.preset && flags.steps) throw new CliError('Specify either --preset or --steps.', 2);
  if (flags.preset) return { preset: String(flags.preset) };
  if (flags.steps) {
    const data = await readJsonArg(flags.steps, 'steps');
    return { steps: Array.isArray(data) ? data : data.steps };
  }
  return null;
}

async function cmdCreate(client, out, flags) {
  const kind = flags.kind;
  if (!kind || kind === true) throw new CliError('Specify --kind (humanoid, creature, item, ship, prop).', 2);
  const meta = { name: flags.name === true ? '' : flags.name, kind };
  if (flags.profile) meta.profile = await readJsonArg(flags.profile, 'profile');
  let filePath = null;
  const fileFlag = flags['from-glb'] || flags['from-file'];
  if (fileFlag) {
    filePath = path.resolve(String(fileFlag));
    if (!existsSync(filePath)) throw new CliError(`File not found: ${filePath}`, 2);
    meta.source = { type: 'upload' };
  } else if (flags['from-job']) {
    meta.source = { type: 'job', jobId: String(flags['from-job']) };
  } else if (flags['from-asset']) {
    meta.source = { type: 'asset', assetId: String(flags['from-asset']) };
  } else {
    meta.source = { type: 'params', params: flags.params ? await readJsonArg(flags.params, 'params') : {} };
  }
  const run = await stepsFromFlags(flags);
  const manifest = await client.createAsset(meta, filePath);
  if (!run) {
    out.result(manifest, () => out.info(`Created passport "${manifest.name}" [${manifest.kind}]: ${manifest.id}`));
    return;
  }
  out.log(`Created passport "${manifest.name}" [${manifest.kind}]: ${manifest.id}`);
  const started = await client.run(manifest.id, { ...run, from: 'source' });
  for (const w of started.warnings || []) out.log(`warning: ${w}`);
  out.log(`Started version ${started.version}, job ${started.jobId}`);
  const job = await waitAndReport(client, started.jobId, out, flags);
  return job;
}

async function cmdRun(client, out, id, flags) {
  if (!id) throw new CliError('Specify the asset ID.', 2);
  const run = await stepsFromFlags(flags);
  if (!run) throw new CliError('Specify --preset <id> or --steps <file.json>.', 2);
  if (flags.from) run.from = String(flags.from);
  const started = await client.run(id, run);
  for (const w of started.warnings || []) out.log(`warning: ${w}`);
  out.log(`Started version ${started.version}, job ${started.jobId}`);
  await waitAndReport(client, started.jobId, out, flags);
}

async function cmdExport(client, out, id, flags) {
  if (!id) throw new CliError('Specify the asset ID.', 2);
  if (!flags.profile || flags.profile === true) throw new CliError('Specify --profile (godot, iso8).', 2);
  const body = { profile: String(flags.profile) };
  if (flags.params) body.params = await readJsonArg(flags.params, 'params');
  const started = await client.exportAsset(id, body);
  out.log(`Export ${body.profile}: version ${started.version}, job ${started.jobId}`);
  const job = await waitAndReport(client, started.jobId, out, flags);
  if (!job || flags['no-wait']) return;
  const manifest = await client.getAsset(id);
  const exported = (manifest.exports || []).filter((e) => e.jobId === job.id);
  if (!exported.length) { out.info('The step finished, but no export archive was created (see the step warnings).'); return; }
  if (flags.out && flags.out !== true) {
    const dir = path.resolve(String(flags.out));
    await fs.mkdir(dir, { recursive: true });
    for (const e of exported) {
      const dest = path.join(dir, path.basename(e.path));
      await client.download(client.mode === 'direct' ? e.path : e.url, dest);
      out.info(`Archive saved: ${dest}`);
      const JSZip = (await import('jszip')).default;
      const zip = await JSZip.loadAsync(await fs.readFile(dest));
      const target = path.join(dir, path.basename(dest, '.zip'));
      let count = 0;
      for (const entry of Object.values(zip.files)) {
        const rel = entry.name.replace(/\\/g, '/');
        const abs = path.resolve(target, rel);
        if (rel.split('/').includes('..') || !abs.startsWith(target + path.sep)) continue;
        if (entry.dir) { await fs.mkdir(abs, { recursive: true }); continue; }
        await fs.mkdir(path.dirname(abs), { recursive: true });
        await fs.writeFile(abs, await entry.async('nodebuffer'));
        count += 1;
      }
      out.info(`Files extracted: ${count} → ${target}`);
    }
  } else {
    for (const e of exported) out.info(`Archive: ${e.path}`);
  }
}

async function cmdBatch(client, out, file, flags) {
  if (!file) throw new CliError('Specify the batch .json file.', 2);
  const abs = path.resolve(file);
  const data = await readJsonArg(abs, 'batch');
  const items = Array.isArray(data) ? data : data.items;
  if (!Array.isArray(items) || !items.length) throw new CliError('The batch file has no items.', 2);
  const filePaths = [];
  const prepared = items.map((item) => {
    const it = structuredClone(item);
    if (it.source?.type === 'upload') {
      const p = path.resolve(path.dirname(abs), String(it.source.path || ''));
      if (!it.source.path || !existsSync(p)) throw new CliError(`File of item "${it.name || '?'}" not found: ${p}`, 2);
      it.source = { type: 'upload', fileIndex: filePaths.length };
      filePaths.push(p);
    }
    return it;
  });
  const started = await client.batch(prepared, filePaths);
  for (const [i, it] of started.items.entries()) {
    out.log(`  ${i + 1}. ${it.name || ''} ${it.assetId || ''} ${it.error ? `— error: ${it.error}` : it.jobId ? `job ${it.jobId}` : '(no pipeline)'}`);
  }
  if (flags['no-wait']) return out.result(started, () => out.info(`Batch ${started.batchId} started.`));
  let last = '';
  for (;;) {
    const b = await client.getBatch(started.batchId);
    const line = `done ${b.counts.completed}/${b.counts.total}, failed ${b.counts.failed}, running ${b.counts.running}, waiting ${b.counts.queued}`;
    if (line !== last) { out.log(line); last = line; }
    if (b.status !== 'running') {
      out.result(b, () => {
        out.info(`Batch ${b.id}: ${statusLabel(b.status)}`);
        for (const it of b.items) out.info(`  ${it.name || it.assetId}: ${statusLabel(it.status)}${it.error ? ` — ${it.error}` : ''}`);
      });
      if (b.status !== 'completed') throw new CliError('The batch has errors.', 1);
      return;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

async function cmdClips(client, out, sub, arg, flags) {
  if (!sub || sub === 'list') {
    const index = await client.clips();
    return out.result(index, () => {
      if (!index.clips.length) return out.info('The clip library is empty. Import: node scripts/forge.mjs clips import <folder with FBX>');
      for (const c of index.clips) out.info(`${String(c.id).padEnd(32)} ${String(c.category || '').padEnd(8)} ${c.frames ?? '?'} frames ${c.name || ''}`);
    });
  }
  if (sub === 'import') {
    if (!arg) throw new CliError('Specify the folder with FBX files.', 2);
    const started = await client.importClips(arg);
    out.log(`Clip import: job ${started.jobId}`);
    await waitAndReport(client, started.jobId, out, flags);
    return;
  }
  if (sub === 'remove') {
    if (!arg) throw new CliError('Specify the clip ID.', 2);
    const r = await client.removeClip(arg);
    return out.result(r, () => out.info(`Clip ${arg} moved to the trash: ${r.trash}`));
  }
  throw new CliError(`Unknown clips subcommand "${sub}".`, 2);
}

async function cmdJobs(client, out) {
  const jobs = await client.listJobs();
  out.result(jobs, () => {
    if (!jobs.length) return out.info('No jobs yet.');
    for (const j of jobs) out.info(`${j.id}  ${statusLabel(j.status).padEnd(12)} ${j.type.padEnd(6)} ${j.assetId || ''} ${j.version || ''} ${j.steps.map((s) => s.step).join(' → ')}`);
  });
}

// ------------------------------------------------------------------ main

export async function main(argv = process.argv.slice(2)) {
  const { positional, flags } = parseArgs(argv);
  const out = makeOut(flags);
  const [command, a1, a2] = positional;
  if (!command || flags.help || command === 'help') {
    console.log(HELP);
    return 0;
  }
  let client;
  if (flags.direct && flags['no-wait'] && ['create', 'run', 'export', 'batch', 'clips', 'job'].includes(command)) {
    // Without a server the job lives only inside this process: exiting early would kill it.
    console.error('Note: --no-wait does not work with --direct (without a server the job only runs while this process is alive). Waiting for completion; to avoid waiting, start the studio and drop --direct.');
    delete flags['no-wait'];
  }
  if (flags.direct) {
    const { ForgeEngine } = await import('../server/forge-pipeline.mjs');
    const engine = new ForgeEngine({ root: ROOT, host: 'cli', allowLocalPaths: true });
    await engine.init();
    client = new DirectClient(engine);
  } else {
    const base = (flags.server && flags.server !== true ? String(flags.server) : null) || process.env.ASTRA_FORGE_URL || `http://127.0.0.1:${process.env.PORT || 3210}`;
    client = new HttpClient(base);
  }
  switch (command) {
    case 'status': await cmdStatus(client, out); break;
    case 'steps': await cmdSteps(client, out); break;
    case 'presets': await cmdPresets(client, out); break;
    case 'assets': await cmdAssets(client, out); break;
    case 'show': await cmdShow(client, out, a1); break;
    case 'create': await cmdCreate(client, out, flags); break;
    case 'run': await cmdRun(client, out, a1, flags); break;
    case 'export': await cmdExport(client, out, a1, flags); break;
    case 'batch': await cmdBatch(client, out, a1, flags); break;
    case 'clips': await cmdClips(client, out, a1, a2, flags); break;
    case 'jobs': await cmdJobs(client, out); break;
    case 'job': {
      if (!a1) throw new CliError('Specify the job ID.', 2);
      if (flags.wait) await waitAndReport(client, a1, out, flags);
      else { const j = await client.getJob(a1); out.result(j, () => printJobSummary(j, out)); }
      break;
    }
    case 'cancel': {
      if (!a1) throw new CliError('Specify the job ID.', 2);
      const r = await client.cancelJob(a1);
      out.result(r, () => out.info(`Cancel: ${statusLabel(r.status) || r.status}`));
      break;
    }
    default:
      throw new CliError(`Unknown command "${command}". Help: node scripts/forge.mjs --help`, 2);
  }
  return 0;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().then(
    (code) => process.exit(code || 0),
    (err) => {
      const json = process.argv.includes('--json');
      if (json) console.error(JSON.stringify({ error: err.message, code: err.code || null }, null, 2));
      else console.error(`Error: ${err.message}`);
      process.exit(err instanceof CliError ? err.exitCode : 1);
    },
  );
}
