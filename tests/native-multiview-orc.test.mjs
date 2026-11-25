import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildOrcShapeWorkflow,
  buildOrcTextureWorkflow,
  parseOrcOptions,
} from '../scripts/native/multiview_orc_workflow.mjs';

const images = { front: 'front.png', left: 'left.png', back: 'back.png', right: 'right.png' };

test('orc shape experiment uses all four views and Draft 512 only', () => {
  const graph = buildOrcShapeWorkflow(images);
  assert.equal(graph['1'].inputs.pixal3d_multiview, true);
  assert.equal(graph['1'].inputs.backend, 'sdpa');
  assert.equal(graph['38'].inputs.azimuths, '0,90,180,270');
  assert.equal(graph['6'].inputs.resolution, 512);
  assert.deepEqual(graph['6'].inputs.pixal3d_mv_views, ['38', 0]);
  assert.equal(graph['7'], undefined);
  assert.equal(graph['40'], undefined);
  assert.equal(graph['27'].inputs.trimesh[0], '26');
});

test('orc texture experiment loads multi-view weights and a 2048 atlas', () => {
  const graph = buildOrcTextureWorkflow({ images, glbPath: 'C:/orc.glb', textureSize: 2048 });
  assert.equal(graph['1'].inputs.pixal3d_multiview, true);
  assert.equal(graph['38'].inputs.azimuths, '0,90,180,270');
  assert.equal(graph['39'].inputs.glb_path, 'C:/orc.glb');
  assert.deepEqual(graph['40'].inputs.pixal3d_mv_views, ['38', 0]);
  assert.deepEqual(graph['40'].inputs.moge_camera_config, ['38', 1]);
  assert.equal(graph['40'].inputs.resolution, 1024);
  assert.equal(graph['40'].inputs.texture_size, 2048);
  assert.equal(graph['40'].inputs.texture_steps, 12);
  assert.equal(graph['40'].inputs.mesh_orientation, '90 degrees');
  assert.ok(!Object.values(graph).some((node) => /ShapeGenerator|SparseGenerator|DecodeLatents/.test(node.class_type)));
});

test('orc light profile reduces texture work and keeps the multi-view contract', () => {
  const graph = buildOrcTextureWorkflow({ images, glbPath: 'C:/orc.glb', textureSize: 1024, textureSteps: 4 });
  assert.equal(graph['1'].inputs.pixal3d_multiview, true);
  assert.deepEqual(graph['40'].inputs.pixal3d_mv_views, ['38', 0]);
  assert.equal(graph['40'].inputs.resolution, 1024);
  assert.equal(graph['40'].inputs.texture_size, 1024);
  assert.equal(graph['40'].inputs.texture_steps, 4);
});

test('orc balanced profile keeps the approved geometry while using a 1024 atlas', async () => {
  const options = parseOrcOptions(['--stage', 'texture', '--views', 'C:/views', '--glb', 'C:/orc.glb', '--profile', 'balanced']);
  assert.equal(options.profile, 'balanced');
  const { resolveOrcTextureProfile } = await import('../scripts/native/run_native_orc_multiview.mjs');
  assert.deepEqual(resolveOrcTextureProfile('balanced'), { textureSize: 1024, textureSteps: 4, lighten: false });
  assert.deepEqual(resolveOrcTextureProfile('light'), { textureSize: 1024, textureSteps: 4, lighten: true });
  assert.deepEqual(resolveOrcTextureProfile('quality'), { textureSize: 2048, textureSteps: 12, lighten: false });
});

test('orc experiment requires explicit stage inputs and offers quality and light profiles', () => {
  assert.deepEqual(parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json']), {
    stage: 'views', input: 'C:/apose-attempt', viewsDir: null, inputGlb: null, outputDir: null, jsonFile: null,
    viewSize: 512, viewSeed: 21, profile: 'quality', coherentFront: false, debug: false,
    presetFile: 'C:/orc.json', negativeProfile: null, negativeFile: null,
  });
  assert.equal(parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--seed', '42']).viewSeed, 42);
  assert.equal(parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--debug', 'true']).debug, true);
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--debug', 'maybe']), /--debug must be true or false/);
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt']), /--preset/);
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--view-size', '640']), /512/);
  assert.equal(parseOrcOptions(['--stage', 'shape', '--views', 'C:/views']).viewsDir, 'C:/views');
  assert.equal(parseOrcOptions(['--stage', 'texture', '--views', 'C:/views', '--glb', 'C:/orc.glb']).inputGlb, 'C:/orc.glb');
  assert.equal(parseOrcOptions(['--stage', 'texture', '--views', 'C:/views', '--glb', 'C:/orc.glb', '--profile', 'light']).profile, 'light');
  assert.throws(() => parseOrcOptions(['--stage', 'texture', '--views', 'C:/views', '--glb', 'C:/orc.glb', '--profile', 'ultra']), /profile/);
  assert.throws(() => parseOrcOptions(['--stage', 'shape']), /--views/);
  assert.throws(() => parseOrcOptions(['--stage', 'texture', '--views', 'C:/views']), /--glb/);
  assert.throws(() => buildOrcTextureWorkflow({ images, glbPath: 'C:/orc.glb', textureSize: 4096 }), /1024 or 2048/);
  assert.throws(() => buildOrcShapeWorkflow({ front: 'front.png' }), /four/);
});

test('orc views select same-batch front and independent negative configuration', () => {
  const options = parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--negative-profile', 'character', '--coherent-front', 'true']);
  assert.equal(options.coherentFront, true);
  assert.equal(options.negativeProfile, 'character');
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--coherent-front', 'maybe']), /true or false/);
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose-attempt', '--preset', 'C:/orc.json', '--negative-profile', 'none', '--negative-file', 'C:/negative.json']), /mutually exclusive/);
});

test('orc native inputs use normalized RGBA cutouts required by Trellis2 preprocessing', async () => {
  const { nativeOrcViewPath } = await import('../scripts/native/run_native_orc_multiview.mjs');
  const file = nativeOrcViewPath('C:/views', 'front');
  assert.match(file, /norm[\\/]cut_front\.png$/);
});

test('orc report keeps geometry, atlas and fallbacks explicit', async () => {
  const { buildOrcReport } = await import('../scripts/native/run_native_orc_multiview.mjs');
  const report = buildOrcReport({
    stage: 'texture', started: 1000, now: 61000, glbPath: 'C:/orc.glb',
    normalized: { buffer: Buffer.alloc(256) }, validation: { accepted: true }, vram: null,
  });
  assert.equal(report.backend, 'windows-rocm');
  assert.equal(report.geometryResolution, 512);
  assert.equal(report.denoiserResolution, 1024);
  assert.equal(report.textureSize, 2048);
  assert.equal(report.seconds, 60);
  assert.equal(report.glbBytes, 256);
  assert.deepEqual(report.fallbackEvents, []);
});
