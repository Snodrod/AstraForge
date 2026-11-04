import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createAttempt, sha256File, setReview, writeManifest } from '../scripts/native/attempt_store.mjs';
import { resolveApprovedAposeInput, resolveApprovedViewsDir } from '../scripts/native/run_native_orc_multiview.mjs';

function approved(stage) {
  const root = mkdtempSync(path.join(tmpdir(), `af-${stage}-`));
  const attempt = createAttempt(root, stage);
  const files = stage === 'apose' ? ['apose-front.png'] : ['views/norm/cut_front.png', 'views/norm/cut_left.png', 'views/norm/cut_back.png', 'views/norm/cut_right.png'];
  const artifacts = {};
  for (const relative of files) {
    const file = path.join(attempt.dir, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, relative);
    artifacts[relative] = sha256File(file);
  }
  writeManifest(attempt.dir, { version: 1, id: attempt.id, stage, status: 'success', artifacts });
  setReview(attempt.dir, 'approved', 'Synthetic integration fixture.');
  return attempt.dir;
}

test('views accepts only an approved unchanged A-pose attempt', () => {
  const dir = approved('apose');
  assert.equal(resolveApprovedAposeInput(dir).image, path.join(dir, 'apose-front.png'));
  assert.throws(() => resolveApprovedAposeInput(path.join(dir, 'apose-front.png')), /manifest|approved/i);
});

test('shape resolves RGBA cutouts only from approved views', () => {
  const dir = approved('views');
  const resolved = resolveApprovedViewsDir(dir);
  assert.equal(resolved.viewsDir, path.join(dir, 'views'));
  assert.match(resolved.manifest.id, /^attempt-/);
});
