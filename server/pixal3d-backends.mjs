// Narrow Pixal3D backend descriptors. This is backend selection, not a universal GPU driver abstraction.
import path from 'node:path';

export const PIXAL3D_BACKEND_IDS = Object.freeze(['windows', 'wsl', 'windows-rocm']);
export const NATIVE_PIXAL3D_DIRNAME = 'ComfyUI-Pixal3D-AMD-Native';

const freezeCapabilities = (values) => Object.freeze({
  single512: false,
  single1024: false,
  multi512: false,
  multi1024: false,
  uvTexture512: false,
  uvTexture1024: false,
  uvTextureStandard1024: false,
  ...values,
});

const DESCRIPTORS = {
  windows: {
    attention: 'flash_attn',
    capabilities: freezeCapabilities({ single512: true, single1024: true, multi512: true, multi1024: true }),
    qualityProfile: 'standard',
  },
  wsl: {
    attention: 'sdpa',
    capabilities: freezeCapabilities({ single512: true, single1024: true, multi512: true, uvTexture512: true }),
    qualityProfile: 'balanced-1024',
  },
  'windows-rocm': {
    attention: 'sdpa',
    capabilities: freezeCapabilities(),
    qualityProfile: 'balanced-1024',
  },
};

export function getPixal3DBackend(id, env = process.env, appRoot = process.cwd()) {
  const base = DESCRIPTORS[id];
  if (!base) throw new Error('Pixal3D backend must be windows, wsl or windows-rocm.');
  let runtimeRoot = null;
  let pythonPath = null;
  if (id === 'windows-rocm') {
    const override = String(env.ASTRA_PIXAL3D_NATIVE_DIR || '').trim();
    if (override && !path.isAbsolute(override)) throw new Error('ASTRA_PIXAL3D_NATIVE_DIR must be an absolute native Windows path.');
    runtimeRoot = override ? path.resolve(override) : path.resolve(appRoot, 'runtime', NATIVE_PIXAL3D_DIRNAME);
    if (/^\/mnt\/[a-z](?:\/|$)/i.test(runtimeRoot) || /wsl\.exe/i.test(runtimeRoot)) throw new Error('The native AMD runtime must use a native Windows path, not WSL.');
    pythonPath = path.join(runtimeRoot, 'python_embeded', 'python.exe');
  }
  return Object.freeze({
    id,
    runtimeRoot,
    pythonPath,
    capabilities: base.capabilities,
    memoryPolicy: Object.freeze({ minFree1024Bytes: 11 * 1024 ** 3 }),
    qualityProfile: base.qualityProfile,
    attention: base.attention,
  });
}

export function listPixal3DBackends() {
  return PIXAL3D_BACKEND_IDS.map((id) => getPixal3DBackend(id));
}
