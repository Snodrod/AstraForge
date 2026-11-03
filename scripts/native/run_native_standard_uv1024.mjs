import { spawn, spawnSync } from 'node:child_process';
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const diagnostics = path.join(root, 'data', 'runtime', 'diagnostics', 'native-amd');
mkdirSync(diagnostics, { recursive: true });
const logFile = path.join(diagnostics, '1024-single-uv1024-server.log');
const readyPath = path.join(root, 'runtime', 'ComfyUI-Pixal3D-AMD-Native', 'native-ready.json');
const originalReady = readFileSync(readyPath, 'utf8');
const ready = JSON.parse(originalReady);
ready.capabilities = { ...ready.capabilities, uvTextureStandard1024: true };
writeFileSync(readyPath, JSON.stringify(ready, null, 2) + '\n');
const env = {
  ...process.env,
  PORT: '3213',
  ASTRA_URL: 'http://127.0.0.1:3213',
  ASTRA_PIXAL3D_BACKEND: 'windows-rocm',
  ASTRA_PIXAL3D_URL: 'http://127.0.0.1:8192',
  ASTRA_COMFY_URL: 'http://127.0.0.1:8192',
  AMD_PIXAL3D_BACKEND: 'windows-rocm',
  AMD_PIXAL3D_QUALITY: 'standard',
  AMD_PIXAL3D_VIEWS: 'single',
  AMD_PIXAL3D_UV_TEXTURE: '1',
  AMD_PIXAL3D_VERTEX_COLORS: '0',
  AMD_PIXAL3D_JSON: path.join(diagnostics, '1024-single-uv1024.json'),
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let studio;
let exitCode = 1;

async function stopStudio() {
  if (!studio || studio.exitCode !== null) return;
  try {
    await fetch('http://127.0.0.1:3213/api/shutdown', {
      method: 'POST', headers: { 'x-astra-client': 'studio' }, signal: AbortSignal.timeout(3000),
    });
  } catch { /* the process may already have exited */ }
  for (let attempt = 0; attempt < 100 && studio.exitCode === null; attempt += 1) await sleep(100);
  if (studio.exitCode === null && process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(studio.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
  }
}

try {
  const fd = openSync(logFile, 'a');
  try {
    studio = spawn(process.execPath, ['server/index.mjs'], {
      cwd: root, env, windowsHide: true, stdio: ['ignore', fd, fd],
    });
  } finally {
    closeSync(fd);
  }
  const runner = spawn(process.execPath, ['scripts/verify_amd_pixal3d_e2e.mjs'], {
    cwd: root, env, stdio: 'inherit',
  });
  exitCode = await new Promise((resolve, reject) => {
    runner.once('error', reject);
    runner.once('exit', (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
} finally {
  await stopStudio();
  writeFileSync(readyPath, originalReady);
  if (process.platform === 'win32') {
    try {
      const pid = Number(readFileSync(path.join(root, 'data', 'runtime', 'pixal3d.pid'), 'utf8'));
      if (Number.isInteger(pid) && pid > 0) {
        spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      }
    } catch { /* no runtime was started */ }
  }
}
process.exitCode = exitCode;
