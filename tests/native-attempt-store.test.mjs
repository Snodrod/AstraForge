import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createAttempt, sha256File, writeManifest, setReview, assertApprovedAttempt } from '../scripts/native/attempt_store.mjs';

function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), 'af-attempt-'));
  const attempt = createAttempt(root, 'apose');
  const image = path.join(attempt.dir, 'apose-front.png');
  writeFileSync(image, Buffer.from('png-one'));
  const manifest = {
    version: 1,
    id: attempt.id,
    stage: 'apose',
    status: 'success',
    artifacts: { 'apose-front.png': sha256File(image) },
  };
  writeManifest(attempt.dir, manifest);
  return { root, attempt, image };
}

test('attempts never overwrite earlier output', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'af-attempts-'));
  assert.equal(createAttempt(root, 'apose').id, 'attempt-001');
  assert.equal(createAttempt(root, 'apose').id, 'attempt-002');
});

test('review is pending by default and approval verifies artifact hashes', () => {
  const { attempt, image } = fixture();
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /not approved/);
  setReview(attempt.dir, 'approved', 'Face and equipment match the source.');
  assert.equal(assertApprovedAttempt(attempt.dir, 'apose').stage, 'apose');
  writeFileSync(image, Buffer.from('changed-after-review'));
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /SHA-256 mismatch/);
});

test('wrong stage and rejected review block the next stage', () => {
  const { attempt } = fixture();
  setReview(attempt.dir, 'rejected', 'Invented profile nose.');
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /not approved/);
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'views'), /stage/);
});
