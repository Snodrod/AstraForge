import test from 'node:test';
import assert from 'node:assert/strict';
import { buildUvAtlasWorkflow, parseAtlasOptions } from '../scripts/native/uv_atlas_workflow.mjs';

test('native UV experiment is texture-only and keeps the 1024 denoiser', () => {
  const graph = buildUvAtlasWorkflow({ imageName: 'baton.png', glbPath: 'C:/baton.glb', textureSize: 2048 });
  assert.equal(graph['40'].inputs.resolution, 1024);
  assert.equal(graph['40'].inputs.texture_size, 2048);
  assert.equal(graph['40'].inputs.texture_steps, 12);
  assert.equal(graph['40'].inputs.mesh_orientation, '90 degrees');
  assert.equal(graph['39'].inputs.glb_path, 'C:/baton.glb');
  assert.equal(graph['27'].inputs.trimesh[0], '40');
  assert.ok(!Object.values(graph).some((node) => /Shape|Sparse|DecodeLatents/.test(node.class_type)));
});

test('native UV experiment accepts only 2048 or 4096 and explicit paths', () => {
  const base = { AMD_UV_SIZE: '2048', AMD_UV_INPUT: 'C:/image.png', AMD_UV_GLB: 'C:/mesh.glb' };
  assert.deepEqual(parseAtlasOptions(base), {
    textureSize: 2048, inputImage: 'C:/image.png', inputGlb: 'C:/mesh.glb', outputDir: null, jsonFile: null,
  });
  assert.equal(parseAtlasOptions({ ...base, AMD_UV_SIZE: '4096' }).textureSize, 4096);
  assert.throws(() => parseAtlasOptions({ ...base, AMD_UV_SIZE: '1024' }), /2048 or 4096/);
  assert.throws(() => parseAtlasOptions({ ...base, AMD_UV_INPUT: '' }), /AMD_UV_INPUT/);
  assert.throws(() => parseAtlasOptions({ ...base, AMD_UV_GLB: '' }), /AMD_UV_GLB/);
});

test('native UV report records the denoiser and exact atlas independently', async () => {
  const { buildUvAtlasReport } = await import('../scripts/native/run_native_uv_atlas.mjs');
  const report = buildUvAtlasReport({
    textureSize: 2048, started: 1000, now: 2500, glbPath: 'C:/baton.glb',
    normalized: { buffer: Buffer.alloc(128) }, validation: { accepted: true, uvTexture: true }, vram: null,
  });
  assert.equal(report.backend, 'windows-rocm');
  assert.equal(report.denoiserResolution, 1024);
  assert.equal(report.textureSize, 2048);
  assert.equal(report.seconds, 1.5);
  assert.equal(report.glbBytes, 128);
  assert.deepEqual(report.fallbackEvents, []);
});

test('native UV launch uses the patched runtime NAF cache, not the user cache', async () => {
  const { buildUvAtlasEnvironment } = await import('../scripts/native/run_native_uv_atlas.mjs');
  const env = buildUvAtlasEnvironment({ dir: 'C:/native-runtime' }, { SystemRoot: 'C:/Windows', PATH: 'C:/Tools' });
  assert.equal(env.HF_HOME, 'C:\\native-runtime\\cache\\huggingface');
  assert.equal(env.TORCH_HOME, 'C:\\native-runtime\\cache\\torch');
});
