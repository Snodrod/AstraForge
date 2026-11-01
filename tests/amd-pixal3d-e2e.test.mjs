import test from 'node:test';
import assert from 'node:assert/strict';
import { parseOptions } from '../scripts/verify_amd_pixal3d_e2e.mjs';

test('Pixal3D E2E defaults to a resumable single-view run', () => {
  assert.deepEqual(parseOptions({}), { views: 'single', jobId: null });
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
