export function parseAtlasOptions(env = process.env) {
  const textureSize = Number(env.AMD_UV_SIZE || 2048);
  if (![2048, 4096].includes(textureSize)) throw new Error('AMD_UV_SIZE must be 2048 or 4096.');
  if (!env.AMD_UV_INPUT) throw new Error('AMD_UV_INPUT must name the prepared input PNG.');
  if (!env.AMD_UV_GLB) throw new Error('AMD_UV_GLB must name the accepted input GLB.');
  return {
    textureSize,
    inputImage: env.AMD_UV_INPUT,
    inputGlb: env.AMD_UV_GLB,
    outputDir: env.AMD_UV_OUTPUT_DIR || null,
    jsonFile: env.AMD_UV_JSON || null,
  };
}

export function buildUvAtlasWorkflow({ imageName, glbPath, textureSize, seed = 42 }) {
  if (![2048, 4096].includes(textureSize)) throw new Error('Texture atlas must be 2048 or 4096.');
  return {
    '1': { class_type: 'Trellis2LoadModel', inputs: {
      modelname: 'TencentARC/Pixal3D', backend: 'sdpa', device: 'cuda', low_vram: true,
      keep_models_loaded: false, conv_backend: 'flex_gemm', sparse_backend: 'sdpa',
      use_reconviagen: false, pixal3d_multiview: false,
    } },
    '2': { class_type: 'Trellis2LoadImageWithTransparency', inputs: { image: imageName } },
    '3': { class_type: 'Trellis2PreProcessImage', inputs: {
      image: ['2', 2], padding: 5, remove_background: false, max_size: 1024,
    } },
    '4': { class_type: 'Trellis2ImageCondGenerator', inputs: { pipeline: ['1', 0], image: ['3', 0], max_views: 1 } },
    '39': { class_type: 'Trellis2LoadMesh', inputs: { glb_path: glbPath, only_vertices_and_faces: false } },
    '40': { class_type: 'Trellis2MeshTexturingPixal3DMultiView', inputs: {
      pipeline: ['4', 2], trimesh: ['39', 0], image: ['3', 0], moge_camera_config: ['4', 3], seed,
      texture_steps: 12, texture_guidance_strength: 1, texture_guidance_rescale: 0, texture_rescale_t: 3,
      resolution: 1024, texture_size: textureSize, texture_alpha_mode: 'OPAQUE', double_side_material: false,
      texture_guidance_interval_start: 0.6, texture_guidance_interval_end: 0.9, bake_on_vertices: false,
      use_custom_normals: false, mesh_cluster_threshold_cone_half_angle_rad: 60, mesh_orientation: '90 degrees',
      sampler: 'euler', inpainting: 'telea', verbose: false, dino_lock: 0, dino_substeps: 4, dino_foundation_cap: 1,
    } },
    '27': { class_type: 'Trellis2ExportMesh', inputs: {
      trimesh: ['40', 0], filename_prefix: `astra/amd-uv-${textureSize}`, file_format: 'glb',
    } },
  };
}
