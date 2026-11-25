import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const REVIEWS = new Set(['pending', 'approved', 'rejected']);
const STAGES = new Set(['apose', 'views']);

export function sha256File(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex');
}

function writeJson(file, value) {
  const temp = `${file}.${process.pid}.tmp`;
  writeFileSync(temp, JSON.stringify(value, null, 2) + '\n');
  renameSync(temp, file);
}

export function createAttempt(outputRoot, stage) {
  if (!STAGES.has(stage)) throw new Error('Attempt stage must be apose or views.');
  const attempts = path.join(path.resolve(outputRoot), 'attempts');
  mkdirSync(attempts, { recursive: true });
  const numbers = readdirSync(attempts, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && /^attempt-\d{3,}$/.test(entry.name))
    .map((entry) => Number(entry.name.slice(8)));
  const id = `attempt-${String(Math.max(0, ...numbers) + 1).padStart(3, '0')}`;
  const dir = path.join(attempts, id);
  mkdirSync(dir, { recursive: false });
  writeJson(path.join(dir, 'review.json'), { status: 'pending', comment: '', reviewedAt: null });
  return { id, dir };
}

export function writeManifest(attemptDir, value) {
  writeJson(path.join(attemptDir, 'manifest.json'), value);
}

export function readManifest(attemptDir) {
  const file = path.join(attemptDir, 'manifest.json');
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ENOTDIR') throw new Error(`Approved attempt manifest not found: ${file}`);
    throw error;
  }
}

function verifyArtifacts(attemptDir, manifest) {
  for (const [relative, expected] of Object.entries(manifest.artifacts || {})) {
    const file = path.resolve(attemptDir, relative);
    if (path.relative(path.resolve(attemptDir), file).startsWith('..')) throw new Error(`Artifact escapes attempt: ${relative}`);
    if (!existsSync(file) || sha256File(file) !== expected) throw new Error(`Artifact SHA-256 mismatch: ${relative}`);
  }
}

export function setReview(attemptDir, status, comment) {
  if (!REVIEWS.has(status)) throw new Error('Review must be pending, approved or rejected.');
  const manifest = readManifest(attemptDir);
  if (manifest.status !== 'success') throw new Error('Only a successful attempt can be reviewed.');
  verifyArtifacts(attemptDir, manifest);
  writeJson(path.join(attemptDir, 'review.json'), { status, comment: String(comment || ''), reviewedAt: new Date().toISOString() });
}

export function assertApprovedAttempt(attemptDir, stage) {
  const manifest = readManifest(attemptDir);
  if (manifest.stage !== stage) throw new Error(`Attempt stage must be ${stage}.`);
  if (manifest.status !== 'success') throw new Error('Attempt did not complete successfully.');
  const review = JSON.parse(readFileSync(path.join(attemptDir, 'review.json'), 'utf8'));
  if (review.status !== 'approved') throw new Error(`Attempt is not approved: ${review.status}.`);
  verifyArtifacts(attemptDir, manifest);
  return manifest;
}
