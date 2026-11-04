import test from 'node:test';
import assert from 'node:assert/strict';
import { buildQwenAposeWorkflow, parseAposeOptions } from '../scripts/native/qwen_apose_workflow.mjs';

test('Qwen workflow preserves Q4 nodes and uses only resolved prompts', () => {
  const graph = buildQwenAposeWorkflow({ image: 'orc.png', prompt: 'LOCK POSITIVE', negative: 'LOCK NEGATIVE' });
  assert.equal(graph['1'].inputs.unet_name, 'Qwen-Image-Edit-2509-Q4_K_M.gguf');
  assert.equal(graph['2'].inputs.type, 'qwen_image');
  assert.equal(graph['5'].class_type, 'FluxKontextImageScale');
  assert.equal(graph['6'].class_type, 'VAEEncode');
  assert.equal(graph['7'].inputs.prompt, 'LOCK POSITIVE');
  assert.equal(graph['8'].inputs.prompt, 'LOCK NEGATIVE');
  assert.equal(graph['11'].inputs.denoise, 1);
  assert.deepEqual(graph['11'].inputs.latent_image, ['6', 0]);
  assert.throws(() => buildQwenAposeWorkflow({ image: 'orc.png' }), /prompt and negative/);
});

test('A-pose CLI requires preset and separates negative selectors', () => {
  const options = parseAposeOptions(['--input', 'C:/orc.png', '--preset', 'C:/orc.json', '--negative-profile', 'character']);
  assert.equal(options.presetFile, 'C:/orc.json');
  assert.equal(options.negativeProfile, 'character');
  assert.equal(options.negativeFile, null);
  assert.equal(options.seed, 42);
  assert.equal(options.steps, 40);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png']), /--preset/);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png', '--preset', 'C:/orc.json', '--negative-profile', 'none', '--negative-file', 'C:/n.json']), /mutually exclusive/);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png', '--preset', 'C:/orc.json', '--steps', '41']), /1..40/);
});
