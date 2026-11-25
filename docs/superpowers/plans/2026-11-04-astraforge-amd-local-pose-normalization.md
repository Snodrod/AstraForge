# Local A-pose Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a coherent orc A-pose locally, generate a same-batch four-view set, and verify the existing native AMD Draft 512 + light UV 1024 cycle with reusable prompts and evidence.

**Architecture:** A tracked asset manifest and verifier protect the large Q4_K_M download. A narrow Qwen workflow/runner produces one front image; the existing views and orc runners then handle coherent views, shape, decimation, and texturing. Existing runtimes and prior artifacts remain untouched except for the isolated runtime's new model files and ComfyUI-GGUF extension.

**Tech Stack:** Node.js ES modules, Python 3 stdlib, ComfyUI API, Qwen-Image-Edit-2509 GGUF, MV-Adapter/SDXL, native Windows PyTorch ROCm, Pixal3D/TRELLIS.2.

## Global Constraints

- Work only inside the current workspace and existing `AstraForge-amd` checkout.
- Use Qwen-Image-Edit-2509 `Q4_K_M`; never silently substitute Q3, FP8, CUDA, WSL, CPU inference, or an external image-edit service.
- No GPU-driver, network, VPN, WSL, global-Python, or reboot changes.
- GPU jobs and downloads run sequentially; no Standard 1024, UV 2048, UV 4096, or broad browser/server suite.
- Preserve `orc` and `orc-light`; new artifacts go to `data/runtime/orc-apose/` and separate diagnostics.
- Do not change `native-ready.json` or expose a persistent production capability.
- Do not commit or push without a separate user request; each task ends with tests and a diff review instead.
- Stop after a real OOM or failed artistic gate and report it. At most one bounded A-pose prompt/seed revision is allowed.

---

### Task 1: Verified Qwen assets and GGUF loader

**Files:**
- Create: `scripts/native/qwen-apose-assets.json`
- Create: `scripts/native/verify_qwen_apose_assets.py`
- Test: `scripts/native/test_qwen_apose_assets.py`
- Git-ignored runtime: `runtime/ComfyUI-Pixal3D-AMD-Native/ComfyUI/custom_nodes/ComfyUI-GGUF/`
- Git-ignored models under `runtime/ComfyUI-Pixal3D-AMD-Native/ComfyUI/models/`

**Interfaces:**
- Consumes: user-authorized Hugging Face artifacts and the existing isolated native runtime.
- Produces: `verify_assets(manifest_path, runtime_root) -> list[dict]`, returning per-asset path/bytes/SHA-256 records and raising on mismatch.

- [ ] **Step 1: Write the failing manifest-verification tests**

```python
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.native.verify_qwen_apose_assets import verify_assets


class QwenAssetsTest(unittest.TestCase):
    def test_manifest_has_exact_q4_assets(self):
        manifest = json.loads(Path('scripts/native/qwen-apose-assets.json').read_text())
        self.assertEqual([a['bytes'] for a in manifest['assets']], [13065746976, 9384670680, 253806246])
        self.assertTrue(all(a['sha256'] for a in manifest['assets']))

    def test_verifier_rejects_wrong_content_and_accepts_exact_file(self):
        asset = {'path': 'models/vae/test.bin', 'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / 'python_embeded').mkdir()
            (runtime / 'python_embeded/python.exe').write_bytes(b'')
            manifest = runtime / 'manifest.json'
            manifest.write_text(json.dumps({'assets': [asset]}))
            target = runtime / asset['path']
            target.parent.mkdir(parents=True)
            target.write_bytes(b'abc')
            self.assertEqual(verify_assets(manifest, runtime)[0]['bytes'], 3)
            target.write_bytes(b'abd')
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                verify_assets(manifest, runtime)


if __name__ == '__main__':
    unittest.main()
```

Run: `python -m unittest scripts/native/test_qwen_apose_assets.py -v`
Expected: FAIL because the manifest and verifier do not exist.

- [ ] **Step 2: Add the exact manifest**

```json
{
  "assets": [
    {
      "url": "https://huggingface.co/QuantStack/Qwen-Image-Edit-2509-GGUF/resolve/main/Qwen-Image-Edit-2509-Q4_K_M.gguf",
      "path": "ComfyUI/models/unet/Qwen-Image-Edit-2509-Q4_K_M.gguf",
      "bytes": 13065746976,
      "sha256": "08f27cdf3e760edef5136ab0afdb9d3ed7a2799bd730b8d5cd9ecb291d808425"
    },
    {
      "url": "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
      "path": "ComfyUI/models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
      "bytes": 9384670680,
      "sha256": "cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4"
    },
    {
      "url": "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors",
      "path": "ComfyUI/models/vae/qwen_image_vae.safetensors",
      "bytes": 253806246,
      "sha256": "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f"
    }
  ]
}
```

- [ ] **Step 3: Implement the minimal verifier**

```python
import hashlib
import json
from pathlib import Path


def verify_assets(manifest_path, runtime_root):
    manifest_path = Path(manifest_path)
    runtime_root = Path(runtime_root).resolve()
    if not runtime_root.is_absolute() or not (runtime_root / 'python_embeded/python.exe').is_file():
        raise RuntimeError('An absolute native runtime with python_embeded/python.exe is required.')
    records = []
    for asset in json.loads(manifest_path.read_text(encoding='utf8'))['assets']:
        target = (runtime_root / asset['path']).resolve()
        if runtime_root not in target.parents:
            raise RuntimeError(f'Asset escapes native runtime: {asset["path"]}')
        if not target.is_file() or target.stat().st_size != asset['bytes']:
            raise RuntimeError(f'Missing or wrong-size asset: {target}')
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != asset['sha256']:
            raise RuntimeError(f'SHA-256 mismatch: {target}')
        records.append({'path': str(target), 'bytes': asset['bytes'], 'sha256': asset['sha256']})
    return records


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--manifest', default=str(Path(__file__).with_name('qwen-apose-assets.json')))
    args = parser.parse_args()
    print(json.dumps({'assets': verify_assets(args.manifest, args.runtime_root)}, indent=2))
```

Run: `python -m unittest scripts/native/test_qwen_apose_assets.py -v`
Expected: 2 tests PASS.

- [ ] **Step 4: Install only the loader dependency and download assets sequentially**

```bash
git clone --depth 1 https://github.com/city96/ComfyUI-GGUF.git runtime/ComfyUI-Pixal3D-AMD-Native/ComfyUI/custom_nodes/ComfyUI-GGUF
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I -m pip install -r runtime/ComfyUI-Pixal3D-AMD-Native/ComfyUI/custom_nodes/ComfyUI-GGUF/requirements.txt
```

For each manifest entry in order, create its parent directory and run `curl.exe -L --fail --retry 3 -C - -o <target> <url>`. If the 300-second command window expires, resume the same file with `-C -`; do not start another download or GPU job. Record the GGUF loader commit with `git -C <loader-path> rev-parse HEAD`.

Run: `python scripts/native/verify_qwen_apose_assets.py --runtime-root runtime/ComfyUI-Pixal3D-AMD-Native`
Expected: all three exact sizes and SHA-256 hashes printed. Review `git status --short`; no installer executable or model weights may be staged.

---

### Task 2: Qwen A-pose workflow and isolated runner

**Files:**
- Create: `scripts/native/qwen_apose_workflow.mjs`
- Create: `scripts/native/run_native_qwen_apose.mjs`
- Test: `tests/native-qwen-apose.test.mjs`

**Interfaces:**
- Consumes: verified model filenames and an uploaded source image name.
- Produces: `buildQwenAposeWorkflow({ image, seed = 42, steps = 40, prompt = APOSE_PROMPT, negative = APOSE_NEGATIVE }) -> ComfyUI graph`; `parseAposeOptions(argv) -> { input, outputDir, jsonFile, seed, steps }`; `buildAposeReport(...) -> object`.

- [ ] **Step 1: Write failing workflow and option tests**

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';
import { APOSE_PROMPT, APOSE_NEGATIVE, buildQwenAposeWorkflow, parseAposeOptions } from '../scripts/native/qwen_apose_workflow.mjs';


test('Qwen A-pose uses Q4 GGUF, the 2509 edit-plus nodes and source latent', () => {
  const graph = buildQwenAposeWorkflow({ image: 'orc.png' });
  assert.equal(graph['1'].class_type, 'UnetLoaderGGUF');
  assert.equal(graph['1'].inputs.unet_name, 'Qwen-Image-Edit-2509-Q4_K_M.gguf');
  assert.equal(graph['2'].inputs.type, 'qwen_image');
  assert.equal(graph['5'].class_type, 'FluxKontextImageScale');
  assert.equal(graph['6'].class_type, 'VAEEncode');
  assert.equal(graph['7'].class_type, 'TextEncodeQwenImageEditPlus');
  assert.equal(graph['7'].inputs.prompt, APOSE_PROMPT);
  assert.equal(graph['8'].inputs.prompt, APOSE_NEGATIVE);
  assert.equal(graph['9'].inputs.shift, 3);
  assert.equal(graph['11'].inputs.steps, 40);
  assert.equal(graph['11'].inputs.cfg, 4);
  assert.equal(graph['11'].inputs.denoise, 1);
  assert.deepEqual(graph['11'].inputs.latent_image, ['6', 0]);
});


test('A-pose options are bounded and explicit', () => {
  assert.deepEqual(parseAposeOptions(['--input', 'C:/orc.png']), { input: 'C:/orc.png', outputDir: null, jsonFile: null, seed: 42, steps: 40 });
  assert.equal(parseAposeOptions(['--input', 'C:/orc.png', '--seed', '43', '--steps', '20']).seed, 43);
  assert.throws(() => parseAposeOptions([]), /--input/);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png', '--steps', '41']), /1..40/);
});
```

Run: `node --test tests/native-qwen-apose.test.mjs`
Expected: FAIL because the module does not exist.

- [ ] **Step 2: Implement prompts and graph**

Copy `APOSE_PROMPT` and `APOSE_NEGATIVE` verbatim from the approved spec. Implement the following graph without a Lightning LoRA:

```javascript
export function buildQwenAposeWorkflow({ image, seed = 42, steps = 40, prompt = APOSE_PROMPT, negative = APOSE_NEGATIVE }) {
  return {
    '1': { class_type: 'UnetLoaderGGUF', inputs: { unet_name: 'Qwen-Image-Edit-2509-Q4_K_M.gguf' } },
    '2': { class_type: 'CLIPLoader', inputs: { clip_name: 'qwen_2.5_vl_7b_fp8_scaled.safetensors', type: 'qwen_image', device: 'default' } },
    '3': { class_type: 'VAELoader', inputs: { vae_name: 'qwen_image_vae.safetensors' } },
    '4': { class_type: 'LoadImage', inputs: { image } },
    '5': { class_type: 'FluxKontextImageScale', inputs: { image: ['4', 0] } },
    '6': { class_type: 'VAEEncode', inputs: { pixels: ['5', 0], vae: ['3', 0] } },
    '7': { class_type: 'TextEncodeQwenImageEditPlus', inputs: { clip: ['2', 0], vae: ['3', 0], image1: ['5', 0], prompt } },
    '8': { class_type: 'TextEncodeQwenImageEditPlus', inputs: { clip: ['2', 0], vae: ['3', 0], image1: ['5', 0], prompt: negative } },
    '9': { class_type: 'ModelSamplingAuraFlow', inputs: { model: ['1', 0], shift: 3 } },
    '10': { class_type: 'CFGNorm', inputs: { model: ['9', 0], strength: 1 } },
    '11': { class_type: 'KSampler', inputs: { model: ['10', 0], positive: ['7', 0], negative: ['8', 0], latent_image: ['6', 0], seed, steps, cfg: 4, sampler_name: 'euler', scheduler: 'simple', denoise: 1 } },
    '12': { class_type: 'VAEDecode', inputs: { samples: ['11', 0], vae: ['3', 0] } },
    '13': { class_type: 'SaveImage', inputs: { images: ['12', 0], filename_prefix: 'astra/orc-apose-front' } },
  };
}
```

`parseAposeOptions` accepts `--input`, `--output`, `--json`, `--seed`, `--steps`, requires an input, and restricts steps to 1–40 and seed to a non-negative safe integer.

- [ ] **Step 3: Implement the runner using the established native lifecycle**

The runner must use port 8196, `buildNativePixal3DLaunch`, `pixal3dConfig/pixal3dEnv`, `ASTRA_PIXAL3D_BACKEND=windows-rocm`, `ASTRA_AMD_NAF_TILE=2`, the same bounded `/prompt` + `/history` polling pattern as `run_native_orc_multiview.mjs`, and a `finally` block that calls `/free` and kills only its own process tree. It uploads the input, runs the graph, downloads the single PNG to `data/runtime/orc-apose/apose-front.png`, and writes `qwen-apose.json` with backend, prompt, negative, seed, steps, seconds, source/output paths, model manifest hashes, GGUF loader commit, workflow, and error status. It refuses to reuse an already answering port.

Run: `node --test tests/native-qwen-apose.test.mjs tests/native-multiview-orc.test.mjs`
Expected: all tests PASS; no GPU process is started by unit tests. Review the runner diff against the existing cleanup pattern.

- [ ] **Step 4: Run one real A-pose generation and inspect it**

```bash
node scripts/native/run_native_qwen_apose.mjs --input data/runtime/orc/views/raw/front.png --output data/runtime/orc-apose
```

Expected: one PNG and `data/runtime/diagnostics/native-amd/qwen-apose.json`; no leftover process on port 8196. Check PNG size before reading it, then inspect the full image against every front-gate item in the spec. If it fails artistically, allow only one recorded prompt/seed revision. If it fails with OOM, stop and report without another model download.

---

### Task 3: Prompt propagation and same-batch front handoff

**Files:**
- Modify: `scripts/views/views_mvadapter.mjs`
- Modify: `scripts/views/views_generate.mjs`
- Modify: `scripts/native/multiview_orc_workflow.mjs`
- Modify: `scripts/native/run_native_orc_multiview.mjs`
- Test: `tests/views.test.mjs`
- Test: `tests/native-multiview-orc.test.mjs`

**Interfaces:**
- Consumes: `generateMvadapterViews({ ..., prompt, negative })` and `views_generate({ ..., useGeneratedFront, prompt, negative })`.
- Produces: `ORC_VIEW_PROMPT`, `ORC_VIEW_NEGATIVE`, `parseOrcOptions(...).coherentFront`, and `views.slots.front.source === 'mvadapter'` when the same-batch front is selected.

- [ ] **Step 1: Add failing tests for custom prompts and coherent-front parsing**

```javascript
import { ORC_VIEW_PROMPT, ORC_VIEW_NEGATIVE } from '../scripts/native/multiview_orc_workflow.mjs';

test('MV-Adapter forwards experiment prompts without changing the default', () => {
  const w = mvadapterWorkflow({ refName: 'r.png', azimuths: [0, 90, 180, 270], size: 512, steps: 30, cfg: 3, seed: 21, prefix: 'x', prompt: ORC_VIEW_PROMPT, negative: ORC_VIEW_NEGATIVE });
  assert.equal(w['2'].inputs.prompt, ORC_VIEW_PROMPT);
  assert.equal(w['2'].inputs.negative_prompt, ORC_VIEW_NEGATIVE);
  assert.match(ORC_VIEW_PROMPT, /exactly one oversized spiked metal pauldron/);
  assert.match(ORC_VIEW_NEGATIVE, /duplicate pauldron/);
});

test('orc views can explicitly select the same-batch front', () => {
  const options = parseOrcOptions(['--stage', 'views', '--input', 'C:/apose.png', '--coherent-front', 'true']);
  assert.equal(options.coherentFront, true);
  assert.throws(() => parseOrcOptions(['--stage', 'views', '--input', 'C:/apose.png', '--coherent-front', 'maybe']), /true or false/);
});
```

Run: `node --test tests/views.test.mjs tests/native-multiview-orc.test.mjs`
Expected: FAIL on missing exports/options.

- [ ] **Step 2: Implement narrow prompt/front changes**

In `generateMvadapterViews`, pass `o.prompt` and `o.negative` into `mvadapterWorkflow`; existing defaults remain unchanged. In `views_generate`, pass those fields through and, only when `o.useGeneratedFront === true` and `r.frontGenerated` exists, copy it to `raw/front.png` and mark the front slot as same-batch MV-Adapter output. Preserve `work/mvadapter_front.png` and the accepted A-pose input outside the views directory.

Add the two orc prompt constants verbatim from the approved spec to `multiview_orc_workflow.mjs`. Add `coherentFront: false` to the parser defaults and strict `true`/`false` parsing. In the views stage, call:

```javascript
const views = await views_generate({
  input: path.resolve(options.input), outDir: viewsDir, provider: 'mvadapter', comfyUrl: url,
  size: options.viewSize, steps: 30, seed: options.viewSeed,
  prompt: ORC_VIEW_PROMPT, negative: ORC_VIEW_NEGATIVE,
  useGeneratedFront: options.coherentFront,
  onProgress: (message) => console.error('·', message),
});
```

Run: `node --test tests/views.test.mjs tests/native-multiview-orc.test.mjs`
Expected: all tests PASS.

- [ ] **Step 3: Generate and inspect the four-view contact sheet**

```bash
node scripts/native/run_native_orc_multiview.mjs --stage views --input data/runtime/orc-apose/apose-front.png --output data/runtime/orc-apose --views data/runtime/orc-apose/views --coherent-front true
python scripts/views/contact_sheet.py data/runtime/orc-apose/views-sheet.jpg "A-pose=data/runtime/orc-apose/views"
```

Expected: front/left/back/right all derive from one batch and `views.json` records the handoff and prompts. Check image sizes, inspect the sheet, and stop before shape if there are duplicate limbs/pauldrons, weapons, cropping, wrong orientation, or large segmentation holes.

---

### Task 4: Sequential Draft 512 and light UV 1024 E2E

**Files:**
- No production-code changes unless a new failure has a separately demonstrated root cause.
- Create at runtime: `data/runtime/orc-apose/*.glb`
- Create at runtime: `data/runtime/diagnostics/native-amd/orc-apose-*.json` and logs.
- Create: `render_orc_apose_preview.py` only if the existing preview renderer cannot select the new GLB without editing prior artifacts.

**Interfaces:**
- Consumes: accepted `data/runtime/orc-apose/views/{front,left,back,right}.png`.
- Produces: accepted Draft 512 GLB, decimated light GLB with exact 1024 × 1024 atlas, reports, and visual previews.

- [ ] **Step 1: Run shape only**

```bash
node scripts/native/run_native_orc_multiview.mjs --stage shape --views data/runtime/orc-apose/views --output data/runtime/orc-apose --json data/runtime/diagnostics/native-amd/orc-apose-shape-light.json
```

Expected: accepted Draft 512 GLB, finite geometry, no fallback events, native process cleanup. Do not continue after a crash, timeout, invalid GLB, or fallback.

- [ ] **Step 2: Run the established light texture stage only**

```bash
node scripts/native/run_native_orc_multiview.mjs --stage texture --profile light --views data/runtime/orc-apose/views --glb data/runtime/orc-apose/amd-pixal3d-native-multiview-draft512.glb --output data/runtime/orc-apose --json data/runtime/diagnostics/native-amd/orc-apose-texture-light.json
```

Expected: at most 120,000 triangles after CPU decimation, four texture steps, exact 1024 × 1024 embedded PNG, finite in-range UVs, `accepted: true`, and `fallbackEvents: []`.

- [ ] **Step 3: Render and inspect**

Render the same front/back/side/three-quarter views used for `orc-light`, plus the atlas and a side-by-side comparison. Read every generated image after checking file size. Record actual seconds, vertices, triangles, GLB bytes, VRAM value or `null`, visual improvements, and remaining artifacts. Do not claim artistic improvement solely because the pipeline passed.

---

### Task 5: Durable prompts, documentation, and regression checks

**Files:**
- Modify: `docs/forge/views.md`
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `scripts/native/README.md`
- Modify only with actual results: `docs/superpowers/specs/2026-11-04-astraforge-amd-local-pose-normalization-design.md`

**Interfaces:**
- Consumes: real reports, logs, prompts, model hashes, generated images, and test output from Tasks 1–4.
- Produces: reproducible local instructions and an explicit verified/failed experimental status.

- [ ] **Step 1: Add the reusable prompt playbook**

In `docs/forge/views.md`, add the exact approved A-pose and MV-Adapter positive/negative prompts, the asymmetric-pauldron rule, same-batch front handoff, and this rejection checklist: cropped hands/feet, extra limbs, duplicate armor, weapon invention, wrong left/right, inconsistent scale, segmentation holes. State that a single dynamic concept should be normalized to A-pose before four-view generation and that a UV atlas cannot repair inconsistent geometry.

- [ ] **Step 2: Record native evidence without expanding capabilities**

In `docs/forge/pixal3d-native-amd.md` and `scripts/native/README.md`, document exact Q4_K_M asset names/hashes, sequential download/run commands, required cleanup, actual timings/counts, artifact paths, and every failed gate. Keep multi-view light experimental and leave production capability flags unchanged. If any stage failed, mark the full cycle unverified rather than writing planned success.

- [ ] **Step 3: Run focused regression tests locally**

```bash
python -m unittest scripts/native/test_qwen_apose_assets.py scripts/native/test_native_tools.py -v
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_lighten_glb.py
node --test tests/native-qwen-apose.test.mjs tests/views.test.mjs tests/native-multiview-orc.test.mjs tests/native-uv-atlas.test.mjs tests/pixal3d-native.test.mjs tests/pixal3d.test.mjs
```

Expected: zero failures; conditional skips must name their missing runtime/hardware condition. These tests use local fixtures only and must not open external websites or launch GPU generation.

- [ ] **Step 4: Final review without committing**

Run `git status --short` and `git diff --check`. Confirm that `AstraForge-Setup-0.5.1.exe`, model weights, runtime outputs, and logs are not included as source changes. Review the final GLB/atlas/report links and leave the working tree uncommitted until the user separately requests a commit.
