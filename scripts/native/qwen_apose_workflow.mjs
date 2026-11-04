export function buildQwenAposeWorkflow({ image, seed = 42, steps = 40, prompt, negative }) {
  if (typeof prompt !== 'string' || !prompt.trim() || typeof negative !== 'string') {
    throw new Error('Resolved prompt and negative are required.');
  }
  return {
    '1': { class_type: 'UnetLoaderGGUF', inputs: { unet_name: 'Qwen-Image-Edit-2509-Q4_K_M.gguf' } },
    '2': { class_type: 'CLIPLoader', inputs: { clip_name: 'qwen_2.5_vl_7b_fp8_scaled.safetensors', type: 'qwen_image', device: 'default' } },
    '3': { class_type: 'VAELoader', inputs: { vae_name: 'qwen_image_vae.safetensors' } },
    '4': { class_type: 'LoadImage', inputs: { image } },
    '5': { class_type: 'FluxKontextImageScale', inputs: { image: ['4', 0] } },
    '6': { class_type: 'VAEEncode', inputs: { pixels: ['5', 0], vae: ['3', 0] } },
    '7': { class_type: 'TextEncodeQwenImageEditPlus', inputs: { clip: ['2', 0], vae: ['3', 0], image1: ['5', 0], prompt } },
    '8': { class_type: 'TextEncodeQwenImageEditPlus', inputs: { clip: ['2', 0], vae: ['3', 0], image1: ['5', 0], prompt: negative } },
    '9': { class_type: 'ModelSamplingAuraFlow', inputs: { model: ['9', 0], shift: 3 } },
    '10': { class_type: 'CFGNorm', inputs: { model: ['9', 0], strength: 1 } },
    '11': { class_type: 'KSampler', inputs: { model: ['10', 0], positive: ['7', 0], negative: ['8', 0], latent_image: ['6', 0], seed, steps, cfg: 4, sampler_name: 'euler', scheduler: 'simple', denoise: 1 } },
    '12': { class_type: 'VAEDecode', inputs: { samples: ['11', 0], vae: ['3', 0] } },
    '13': { class_type: 'SaveImage', inputs: { images: ['12', 0], filename_prefix: 'astra/identity-apose-front' } },
  };
}

export function parseAposeOptions(argv) {
  const options = {
    input: null, outputDir: null, jsonFile: null, seed: 42, steps: 40,
    presetFile: null, negativeProfile: null, negativeFile: null,
  };
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (value === undefined) throw new Error(`Missing value for ${flag}`);
    if (flag === '--input') options.input = value;
    else if (flag === '--output') options.outputDir = value;
    else if (flag === '--json') options.jsonFile = value;
    else if (flag === '--seed') options.seed = Number(value);
    else if (flag === '--steps') options.steps = Number(value);
    else if (flag === '--preset') options.presetFile = value;
    else if (flag === '--negative-profile') options.negativeProfile = value;
    else if (flag === '--negative-file') options.negativeFile = value;
    else throw new Error(`Unknown option: ${flag}`);
  }
  if (!options.input) throw new Error('--input is required');
  if (!options.presetFile) throw new Error('--preset is required');
  if (options.negativeProfile && options.negativeFile) throw new Error('--negative-profile and --negative-file are mutually exclusive');
  if (!Number.isSafeInteger(options.seed) || options.seed < 0) throw new Error('--seed must be a non-negative safe integer');
  if (!Number.isInteger(options.steps) || options.steps < 1 || options.steps > 40) throw new Error('--steps must be in 1..40');
  return options;
}
