// Native Windows process construction for AMD ROCm. No WSL or Linux-path fallback is permitted here.
import path from 'node:path';

function assertNativePath(value, label) {
  if (!path.isAbsolute(value || '') || /^\/mnt\/[a-z](?:\/|$)/i.test(value || '') || /wsl\.exe/i.test(value || '')) {
    throw new Error(`${label} must be an absolute native Windows path.`);
  }
}

export function buildNativePixal3DLaunch({ config, descriptor, tempDir, dbFile, env = process.env }) {
  if (config?.backend !== 'windows-rocm' || descriptor?.id !== 'windows-rocm') {
    throw new Error('The native Pixal3D launcher requires the windows-rocm backend.');
  }
  const runtimeRoot = descriptor.runtimeRoot;
  const pythonPath = descriptor.pythonPath;
  assertNativePath(runtimeRoot, 'Native AMD runtime');
  assertNativePath(pythonPath, 'Native AMD Python');
  assertNativePath(config.dir, 'Configured Pixal3D directory');
  const args = [
    '-I', '-W', 'ignore::FutureWarning', path.join('ComfyUI', 'main.py'), '--windows-standalone-build',
    '--listen', '127.0.0.1', '--port', String(config.port), '--disable-auto-launch', '--disable-async-offload',
    '--temp-directory', tempDir,
  ];
  if (dbFile) args.push('--database-url', 'sqlite:///' + dbFile.replaceAll('\\', '/'));
  return {
    command: pythonPath,
    args,
    cwd: runtimeRoot,
    env: {
      ...env,
      ASTRA_PIXAL3D_NATIVE: '1',
      ASTRA_AMD_ROCM: '1',
      HF_HUB_OFFLINE: '1',
      TRANSFORMERS_OFFLINE: '1',
    },
  };
}
