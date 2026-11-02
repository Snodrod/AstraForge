import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOptions } from '../scripts/verify_amd_pixal3d_e2e.mjs';

test('Pixal3D E2E defaults to a resumable single-view run', () => {
  assert.deepEqual(parseOptions({}), { views: 'single', jobId: null, quality: 'draft', vertexColors: false, uvTexture: false });
});

test('Pixal3D E2E enables vertex colors only for the verified single-view draft profile', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1' }), {
    views: 'single', jobId: null, quality: 'draft', vertexColors: true, uvTexture: false,
  });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /single-view/);
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1', AMD_PIXAL3D_QUALITY: 'standard' }), /draft 512/);
});

test('Pixal3D E2E enables UV textures only for AMD single-view runs', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1' }), {
    views: 'single', jobId: null, quality: 'draft', vertexColors: false, uvTexture: true,
  });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /cannot be combined/);
  assert.throws(() => parseOptions({ AMD_PIXAL3D_UV_TEXTURE: '1', AMD_PIXAL3D_VIEWS: 'multi' }), /single-view/);
});

test('Pixal3D E2E selects the full 1024 profile explicitly', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_QUALITY: 'standard' }).quality, 'standard');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_QUALITY: 'unknown' }), /AMD_PIXAL3D_QUALITY/);
});

test('Pixal3D E2E accepts multi-view draft 512 and blocks unverified multi-view 1024', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_VIEWS: 'multi' }).views, 'multi');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_QUALITY: 'standard' }), /draft 512/);
});

test('Pixal3D E2E rejects an unknown view mode', () => {
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'unknown' }), /AMD_PIXAL3D_VIEWS/);
});

test('Pixal3D E2E resumes the requested job without creating a duplicate', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_JOB_ID: 'abc' }).jobId, 'abc');
});
