// Linux ROCm Pixal3D runtime hosted by the project-owned WSL distribution.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const APP_ROOT = fileURLToPath(new URL('..', import.meta.url));
const START_SCRIPT = path.join(APP_ROOT, 'scripts', 'wsl', 'start-pixal3d-amd.sh');

export function toWslPath(value) {
  const match = /^([A-Za-z]):[\\/](.*)$/.exec(value);
  if (!match) throw new Error('An absolute Windows path is required');
  return `/mnt/${match[1].toLowerCase()}/${match[2].replaceAll('\\', '/')}`;
}

export function pixal3dWslArgs(cfg, runtimeDir) {
  const tempDir = toWslPath(path.join(runtimeDir, 'pixal3d-temp'));
  const database = toWslPath(path.join(runtimeDir, 'pixal3d.db'));
  return [
    '-d', cfg.wslDistro || 'AstraForge-AMD', '--', 'bash', toWslPath(START_SCRIPT),
    '--port', String(cfg.port), '--temp-directory', tempDir,
    '--database-url', 'sqlite:///' + database,
  ];
}
