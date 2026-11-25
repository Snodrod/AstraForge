import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOptions } from '../scripts/verify_amd_pixal3d_e2e.mjs';

const defaults = { views: 'single', jobId: null, quality: 'draft', vertexColors: false, uvTexture: false, backend: 'wsl', outputDir: null, jsonFile: null, inputFile: null };

test('Pixal3D E2E defaults to a resumable single-view WSL run', () => {
  assert.deepEqual(parseOptions({}), defaults);
});

test('Pixal3D E2E enables vertex colors only for the verified single-view draft profile', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1' }), { ...defaults, vertexColors: true });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /single-view/);
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1', AMD_PIXAL3D_QUALITY: 'standard' }), /draft 512/);
});

test('Pixal3D E2E enables draft UV and explicitly gated native Standard UV only for single view', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1' }), { ...defaults, uvTexture: true });
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_BACKEND: 'windows-rocm' }), { ...defaults, uvTexture: true, backend: 'windows-rocm' });
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_BACKEND: 'windows-rocm', AMD_PIXAL3D_QUALITY: 'standard' }), { ...defaults, uvTexture: true, backend: 'windows-rocm', quality: 'standard' });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /cannot be combined/);
  assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VIEWS: 'multi' }), /single-view/);
  assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_QUALITY: 'standard' }), /draft 512/);
});

test('Pixal3D E2E selects the full 1024 profile explicitly', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_QUALITY: 'standard' }).quality, 'standard');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_QUALITY: 'unknown' }), /AMD_PIXAL3D_QUALITY/);
});

test('Pixal3D E2E accepts multi-view draft 512 and blocks unverified multi-view 1024', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_VIEWS: 'multi' }).views, 'multi');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_QUALITY: 'standard' }), /draft 512/);
});

test('Pixal3D E2E selects native AMD explicitly and accepts diagnostic destinations', () => {
  const options = parseOptions({ AMD_PIXAL3D_BACKEND: 'windows-rocm', AMD_PIXAL3D_OUTPUT_DIR: 'out/native', AMD_PIXAL3D_JSON: 'out/native/report.json' });
  assert.equal(options.backend, 'windows-rocm');
  assert.equal(options.outputDir, 'out/native');
  assert.equal(options.jsonFile, 'out/native/report.json');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_BACKEND: 'directml' }), /AMD_PIXAL3D_BACKEND/);
});

test('Pixal3D E2E accepts a custom input image only for single view', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_INPUT: 'assets/baton.png' }), { ...defaults, inputFile: 'assets/baton.png' });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_INPUT: 'assets/baton.png' }), /single-view/);
});

test('Pixal3D E2E rejects an unknown view mode', () => {
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'unknown' }), /AMD_PIXAL3D_VIEWS/);
});

test('Pixal3D E2E resumes the requested job without creating a duplicate', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_JOB_ID: 'abc' }).jobId, 'abc');
});
