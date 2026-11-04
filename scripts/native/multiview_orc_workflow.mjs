import { PIXAL3D_AZIMUTH, pixal3dWorkflow } from '../../server/schema.mjs';

const SLOTS = ['front', 'left', 'back', 'right'];

function requireImages(images) {
  if (!SLOTS.every((slot) => images?.[slot])) throw new Error('The orc experiment requires four views.');
}

export function buildOrcShapeWorkflow(images) {
  requireImages(images);
  return pixal3dWorkflow(images, {
    seed: 42,
    prefix: 'astra/orc-multiview-draft512',
    runtimeBackend: 'windows-rocm',
    quality: 'draft',
  });
}

export function buildOrcTextureWorkflow({ images, glbPath, textureSize = 2048, textureSteps = 12, seed = 42 }) {
  requireImages(images);
  if (![1024, 2048].includes(textureSize)) throw new Error('The orc experiment supports a 1024 or 2048 texture atlas.');
  const graph = {
    '1': { class_type: 'Trellis2LoadModel', inputs: {
      modelname: 'TencentARC/Pixal3D', backend: 'sdpa', device: 'cuda', low_vram: true,
      keep_models_loaded: false, conv_backend: 'flex_gemm', sparse_backend: 'sdpa',
      use_reconviagen: false, pixal3d_multiview: true,
    } },
    '2': { class_type: 'Trellis2LoadImageWithTransparency', inputs: { image: images.front } },
    '3': { class_type: 'Trellis2PreProcessImage', inputs: {
      image: ['2', 2], padding: 5, remove_background: false, max_size: 1024,
    } },
    '4': { class_type: 'Trellis2ImageCondGenerator', inputs: { pipeline: ['1', 0], image: ['3', 0], max_views: 1 } },
  };
  const loads = { front: '30', left: '31', back: '32', right: '33' };
  for (const slot of SLOTS) graph[loads[slot]] = { class_type: 'Trellis2LoadImageWithTransparency', inputs: { image: images[slot] } };
  let batch = ['30', 2];
  SLOTS.slice(1).forEach((slot, index) => {
    const id = String(34 + index);
    graph[id] = { class_type: 'ImageBatch', inputs: { image1: batch, image2: [loads[slot], 2] } };
    batch = [id, 0];
  });
  graph['37'] = { class_type: 'Trellis2FovMoGeCameraConfig', inputs: { fov: 0.1, unit: 'rad', extend_pixel: 0, mesh_scale: 1 } };
  graph['38'] = { class_type: 'Trellis2Pixal3DMultiViewConfig', inputs: {
    images: batch,
    azimuths: SLOTS.map((slot) => PIXAL3D_AZIMUTH[slot]).join(','),
    elevations: '0,0,0,0', fov: 20, fov_unit: 'deg', mesh_scale: 1, distance: 0,
    remove_background: false, invert_mask: false, framing: 'auto', moge_camera_config: ['37', 0],
  } };
  graph['39'] = { class_type: 'Trellis2LoadMesh', inputs: { glb_path: glbPath, only_vertices_and_faces: false } };
  graph['40'] = { class_type: 'Trellis2MeshTexturingPixal3DMultiView', inputs: {
    pipeline: ['4', 2], trimesh: ['39', 0], image: ['3', 0], moge_camera_config: ['38', 1],
    pixal3d_mv_views: ['38', 0], seed,
    texture_steps: textureSteps, texture_guidance_strength: 1, texture_guidance_rescale: 0, texture_rescale_t: 3,
    resolution: 1024, texture_size: textureSize, texture_alpha_mode: 'OPAQUE', double_side_material: false,
    texture_guidance_interval_start: 0.6, texture_guidance_interval_end: 0.9, bake_on_vertices: false,
    use_custom_normals: false, mesh_cluster_threshold_cone_half_angle_rad: 60, mesh_orientation: '90 degrees',
    sampler: 'euler', inpainting: 'telea', verbose: false, dino_lock: 0, dino_substeps: 4, dino_foundation_cap: 1,
  } };
  graph['27'] = { class_type: 'Trellis2ExportMesh', inputs: {
    trimesh: ['40', 0], filename_prefix: `astra/orc-multiview-uv${textureSize}`, file_format: 'glb',
  } };
  return graph;
}

export function parseOrcOptions(argv) {
  const options = {
    stage: null, input: null, viewsDir: null, inputGlb: null, outputDir: null, jsonFile: null,
    viewSize: 512, viewSeed: 21, profile: 'quality', coherentFront: false,
    presetFile: null, negativeProfile: null, negativeFile: null,
  };
  const names = {
    '--stage': 'stage', '--input': 'input', '--views': 'viewsDir', '--glb': 'inputGlb',
    '--output': 'outputDir', '--json': 'jsonFile', '--view-size': 'viewSize', '--seed': 'viewSeed',
    '--profile': 'profile', '--coherent-front': 'coherentFront', '--preset': 'presetFile',
    '--negative-profile': 'negativeProfile', '--negative-file': 'negativeFile',
  };
  for (let index = 0; index < argv.length; index += 2) {
    const key = names[argv[index]];
    if (!key || argv[index + 1] === undefined) throw new Error(`Unknown or incomplete option: ${argv[index]}`);
    options[key] = argv[index + 1];
  }
  if (typeof options.coherentFront !== 'boolean') {
    if (!['true', 'false'].includes(options.coherentFront)) throw new Error('--coherent-front must be true or false.');
    options.coherentFront = options.coherentFront === 'true';
  }
  options.viewSize = Number(options.viewSize);
  options.viewSeed = Number(options.viewSeed);
  if (options.viewSize !== 512) throw new Error('--view-size must be 512 for the native AMD experiment.');
  if (!['quality', 'light'].includes(options.profile)) throw new Error('--profile must be quality or light.');
  if (!['views', 'shape', 'texture'].includes(options.stage)) throw new Error('--stage must be views, shape or texture.');
  if (options.stage === 'views' && !options.input) throw new Error('--input is required for views.');
  if (options.stage === 'views' && !options.presetFile) throw new Error('--preset is required for views.');
  if (options.negativeProfile && options.negativeFile) throw new Error('--negative-profile and --negative-file are mutually exclusive.');
  if (options.stage !== 'views' && !options.viewsDir) throw new Error('--views is required for shape and texture.');
  if (options.stage === 'texture' && !options.inputGlb) throw new Error('--glb is required for texture.');
  return options;
}
