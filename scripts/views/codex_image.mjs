// Image generation through the locally installed OpenAI Codex app (ChatGPT subscription, no API key).
//
// AstraForge's own module (adapted from the codex-image skill's approach, no dependency on ~/.claude):
// `codex exec` runs headless with the reference images attached, the built-in image tool renders the
// picture and Codex stores it in ~/.codex/generated_images/<session>/; we pick it up from there.
import { spawn, execFile } from 'node:child_process';
import { promises as fs, existsSync, mkdtempSync, rmSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const run = (file, args, opts = {}) => new Promise((resolve) => {
  execFile(file, args, { encoding: 'utf8', timeout: opts.timeout || 20000, windowsHide: true }, (err, stdout, stderr) => {
    resolve({ ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''), error: err });
  });
});

let cachedExe;

/** Path of codex.exe: CODEX_EXE, PATH, or the Microsoft Store package OpenAI.Codex. null if not found. */
export async function findCodex() {
  if (cachedExe && existsSync(cachedExe)) return cachedExe;
  const candidates = [];
  if (process.env.CODEX_EXE) candidates.push(process.env.CODEX_EXE);
  const where = await run(process.platform === 'win32' ? 'where' : 'which', ['codex']);
  if (where.ok) {
    for (const line of where.stdout.split(/\r?\n/)) {
      const p = line.trim();
      if (p && /\.exe$|^[^.]+$/i.test(p)) candidates.push(p);
    }
  }
  for (const c of candidates) if (c && existsSync(c)) return (cachedExe = c);
  if (process.platform === 'win32') {
    const ps = await run('powershell', ['-NoProfile', '-Command',
      '(Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1).InstallLocation'], { timeout: 30000 });
    const loc = ps.stdout.trim();
    const exe = loc && path.join(loc, 'app', 'resources', 'codex.exe');
    if (exe && existsSync(exe)) return (cachedExe = exe);
  }
  return null;
}

/** {available, exe, version, loggedIn, imageGeneration, message} for the settings screen and for early errors. */
export async function codexStatus() {
  const exe = await findCodex();
  if (!exe) return { available: false, message: 'Codex app not found. Install Codex (ChatGPT subscription) or set CODEX_EXE.' };
  const [version, login, features] = await Promise.all([
    run(exe, ['--version']), run(exe, ['login', 'status']), run(exe, ['features', 'list']),
  ]);
  const loggedIn = /logged in/i.test(login.stdout + login.stderr);
  const feature = (features.stdout.split(/\r?\n/).find((l) => /^image_generation\s/.test(l)) || '').replace(/\s+/g, ' ');
  const imageGeneration = /true/.test(feature);
  return {
    available: loggedIn && imageGeneration,
    exe,
    version: version.stdout.trim(),
    loggedIn,
    imageGeneration,
    message: !loggedIn ? 'Codex found, but you are not signed in to ChatGPT.' : !imageGeneration ? 'Image generation is disabled in this Codex version.' : 'Ready',
  };
}

function killTree(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === 'win32') spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
  else child.kill();
}

// codex image sessions share ~/.codex; the rare "pick everything new" fallback is only safe one at a time.
const limit = Math.max(1, Number(process.env.ASTRA_CODEX_CONCURRENCY) || 1);
let active = 0;
const waiters = [];
async function slot() {
  if (active >= limit) await new Promise((r) => waiters.push(r));
  active++;
  return () => { active--; waiters.shift()?.(); };
}

/**
 * Generate one image.
 * @param {{prompt:string, refs?:string[], aspect?:string, outPath:string, timeoutSec?:number, signal?:AbortSignal, onLog?:(s:string)=>void}} o
 * @returns {Promise<{file:string, seconds:number, sessionId:string|null, exe:string}>}
 */
export async function generateImage(o) {
  const exe = await findCodex();
  if (!exe) throw new Error('Codex app not found. Install Codex (ChatGPT subscription) or set CODEX_EXE.');
  const refs = (o.refs || []).map((r) => path.resolve(r));
  for (const r of refs) if (!existsSync(r)) throw new Error('Reference file not found: ' + r);
  const outPath = path.resolve(o.outPath);
  await fs.mkdir(path.dirname(outPath), { recursive: true });
  const instr = [
    'Use your built-in image generation tool to create exactly 1 image.',
    refs.length ? 'Use the attached image(s) as the visual reference / base, keeping the subject consistent.' : '',
    o.aspect ? `Aspect ratio: ${o.aspect}.` : '',
    'Do not run shell commands, do not write files, do not ask questions. After generating, reply only DONE.',
    '',
    'Image prompt:',
    o.prompt,
  ].filter(Boolean).join('\n');
  const release = await slot();
  const workdir = mkdtempSync(path.join(os.tmpdir(), 'astra-codex-'));
  const started = Date.now();
  try {
    const args = ['exec', '--skip-git-repo-check', '-s', 'read-only', '-C', workdir];
    for (const r of refs) args.push('-i', r);
    args.push('-');
    const child = spawn(exe, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    let log = '';
    const add = (d) => { log += d; o.onLog?.(String(d)); };
    child.stdout.on('data', add);
    child.stderr.on('data', add);
    child.stdin.end(instr);
    const timer = setTimeout(() => killTree(child), (o.timeoutSec || 600) * 1000);
    const abort = () => killTree(child);
    o.signal?.addEventListener('abort', abort, { once: true });
    const code = await new Promise((resolve) => child.on('close', resolve));
    clearTimeout(timer);
    o.signal?.removeEventListener('abort', abort);
    if (o.signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
    const root = path.join(process.env.CODEX_HOME || path.join(os.homedir(), '.codex'), 'generated_images');
    const sessionId = (log.match(/session id:\s*([0-9a-f-]{20,})/i) || [])[1] || null;
    const list = async (dir) => (existsSync(dir) ? (await fs.readdir(dir)).filter((f) => /\.(png|jpe?g|webp)$/i.test(f)).map((f) => path.join(dir, f)) : []);
    let files = sessionId ? await list(path.join(root, sessionId)) : [];
    if (!files.length && existsSync(root) && limit === 1) {
      for (const d of await fs.readdir(root)) files.push(...await list(path.join(root, d)));
      files = files.filter((f) => statSync(f).mtimeMs >= started - 2000);
    }
    files.sort((a, b) => statSync(a).mtimeMs - statSync(b).mtimeMs);
    if (!files.length) throw new Error(`Codex returned no image (code ${code}). ` + log.split(/\r?\n/).filter(Boolean).slice(-6).join(' | ').slice(0, 600));
    await fs.copyFile(files[files.length - 1], outPath);
    return { file: outPath, seconds: Math.round((Date.now() - started) / 100) / 10, sessionId, exe };
  } finally {
    release();
    try { rmSync(workdir, { recursive: true, force: true }); } catch { /* temp dir only */ }
  }
}
