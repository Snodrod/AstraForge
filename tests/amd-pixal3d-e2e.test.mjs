import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOptions } from '../scripts/verify_amd_pixal3d_e2e.mjs';

test('Pixal3D E2E defaults to a resumable single-view run', () => {
  assert.deepEqual(parseOptions({}), { views: 'single', jobId: null, quality: 'draft', vertexColors: false });
});

test('Pixal3D E2E enables vertex colors only for explicit opt-in', () => {
  assert.deepEqual(parseOptions({ AMD_PIXAL3D_VERTEX_COLORS: '1', AMD_PIXAL3D_QUALITY: 'standard' }), {
    views: 'single', jobId: null, quality: 'standard', vertexColors: true,
  });
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'multi', AMD_PIXAL3D_VERTEX_COLORS: '1' }), /single-view/);
});

test('Pixal3D E2E selects the full 1024 profile explicitly', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_QUALITY: 'standard' }).quality, 'standard');
  assert.throws(() => parseOptions({ AMD_PIXAL3D_QUALITY: 'unknown' }), /AMD_PIXAL3D_QUALITY/);
});

test('Pixal3D E2E accepts the existing multi-view workflow', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_VIEWS: 'multi' }).views, 'multi');
});

test('Pixal3D E2E rejects an unknown view mode', () => {
  assert.throws(() => parseOptions({ AMD_PIXAL3D_VIEWS: 'unknown' }), /AMD_PIXAL3D_VIEWS/);
});

test('Pixal3D E2E resumes the requested job without creating a duplicate', () => {
  assert.equal(parseOptions({ AMD_PIXAL3D_JOB_ID: 'abc' }).jobId, 'abc');
});
