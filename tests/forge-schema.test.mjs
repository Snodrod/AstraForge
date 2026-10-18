// generationSchema: the exhaust-haze filter option (hazeFilter) travels to scripts/prepare_concept.py inside backgroundMode.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generationSchema } from '../server/schema.mjs';

const IMAGE = '3f2504e0-4f89-41d3-9a0c-0305e82c3301';

test('by default backgroundMode stays auto and hazeFilter is auto', () => {
  const job = generationSchema.parse({ imageId: IMAGE });
  assert.equal(job.backgroundMode, 'auto');
  assert.equal(job.hazeFilter, 'auto');
  assert.equal(job.imageId, IMAGE);
  assert.equal(job.quality, 'standard');
  assert.equal(job.seed, 42);
});

test('explicit hazeFilter auto does not change backgroundMode', () => {
  assert.equal(generationSchema.parse({ imageId: IMAGE, backgroundMode: 'alpha', hazeFilter: 'auto' }).backgroundMode, 'alpha');
});

test('hazeFilter off is folded into backgroundMode', () => {
  const job = generationSchema.parse({ imageId: IMAGE, hazeFilter: 'off' });
  assert.equal(job.backgroundMode, 'auto:haze=off');
  assert.equal(job.hazeFilter, 'off');
});

test('hazeFilter on with backgroundMode keep gives keep:haze=on', () => {
  assert.equal(generationSchema.parse({ imageId: IMAGE, backgroundMode: 'keep', hazeFilter: 'on' }).backgroundMode, 'keep:haze=on');
});

test('an unknown hazeFilter or backgroundMode is rejected', () => {
  assert.equal(generationSchema.safeParse({ imageId: IMAGE, hazeFilter: 'maybe' }).success, false);
  assert.equal(generationSchema.safeParse({ imageId: IMAGE, hazeFilter: true }).success, false);
  assert.equal(generationSchema.safeParse({ imageId: IMAGE, backgroundMode: 'auto:haze=on' }).success, false);
});

test('a job still needs a concept image or a prompt', () => {
  assert.equal(generationSchema.safeParse({}).success, false);
  assert.equal(generationSchema.safeParse({ prompt: '   ', hazeFilter: 'on' }).success, false);
  assert.ok(generationSchema.safeParse({ prompt: 'gunship', hazeFilter: 'on' }).success);
});
