import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolveIdentityConfig } from '../scripts/native/identity_config.mjs';

const identityRoot = path.resolve('scripts/native/identity');
const orc = path.join(identityRoot, 'preset-orc.json');
const building = path.join(identityRoot, 'preset-building.example.json');

test('identity-lock is shared while object and negative profiles stay independent', () => {
  const apose = resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'character', purpose: 'apose' });
  const views = resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'character', purpose: 'views' });
  assert.match(apose.prompt, /Change only the body pose, camera view and framing/);
  assert.equal(views.prompt, views.preset.viewsPrompt);
  assert.ok(views.prompt.length < 500);
  assert.match(views.prompt, /short broad nose/);
  assert.match(apose.prompt, /Do not redesign the nose, chin, jaw, eyes, ears/);
  assert.match(apose.prompt, /character's own RIGHT shoulder/);
  assert.match(apose.negative, /extra arms/);
  assert.notEqual(apose.prompt, views.prompt);
});

test('building preset never inherits orc or character rules', () => {
  const result = resolveIdentityConfig({ identityRoot, presetFile: building, negativeProfile: 'building', purpose: 'views' });
  assert.match(result.prompt, /two-storey wooden house/);
  assert.doesNotMatch(result.prompt, /\borc\b|pauldron|tusks/);
  assert.match(result.negative, /extra floors/);
  assert.doesNotMatch(result.negative, /extra arms|duplicate pauldron/);
});

test('generic, none and custom negative files are explicit', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'af-identity-'));
  const custom = path.join(dir, 'custom.json');
  writeFileSync(custom, JSON.stringify({ version: '1.0.0', kind: 'custom', terms: ['oversaturated'] }));
  assert.equal(resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'none', purpose: 'views' }).negative, '');
  assert.equal(resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'generic', purpose: 'views' }).negative, 'watermark, text, collage, blurry, noisy, low contrast');
  assert.equal(resolveIdentityConfig({ identityRoot, presetFile: orc, negativeFile: custom, purpose: 'views' }).negative, 'oversaturated');
  assert.throws(() => resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'none', negativeFile: custom, purpose: 'views' }), /mutually exclusive/);
});

test('invalid inputs fail before any ComfyUI process is started', () => {
  assert.throws(() => resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'unknown', purpose: 'views' }), /Unknown negative profile/);
  assert.throws(() => resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'building', purpose: 'views' }), /does not match preset kind/);
  assert.throws(() => resolveIdentityConfig({ identityRoot, presetFile: orc, negativeProfile: 'character', purpose: 'shape' }), /purpose/);
});
