# Reusable Identity-lock and Negative Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать подготовку A-pose и ракурсов воспроизводимой, независимой от конкретного персонажа и неспособной пропустить отклонённые изображения в shape.

**Architecture:** JSON identity-lock, preset объекта и negative-профиль собираются одним resolver. Каждый запуск создаёт неизменяемый attempt с manifest и хешами; отдельная команда фиксирует человеческую приёмку, а runners проверяют approved attempt до запуска GPU. Существующие Qwen, MV-Adapter и Pixal3D workflow сохраняются; меняются их конфигурация и границы этапов.

**Tech Stack:** Node.js ES modules, `node:test`, JSON, SHA-256 из `node:crypto`, существующие Python/Pillow contact sheets, ComfyUI API, Qwen-Image-Edit-2509 Q4_K_M, MV-Adapter, Pixal3D/TRELLIS.2.

## Global Constraints

- Работать только внутри текущего workspace и `AstraForge-amd`.
- Не скачивать новые модели; сохранить Qwen-Image-Edit-2509 `Q4_K_M` и существующий MV-Adapter.
- Не менять драйверы, сеть, VPN, WSL, глобальный Python и не перезагружать компьютер.
- Не использовать субагентов без отдельного явного запроса пользователя.
- Не запускать Standard 1024, UV 2048/4096, production-ready флаги или менять `native-ready.json`.
- GPU-задачи выполнять последовательно; реальные генерации не входят в модульные тесты.
- При OOM остановиться, не подменять веса или backend.
- Не перезаписывать существующие `orc`, `orc-light`, `orc-apose` и прежние диагностические отчёты.
- Не коммитить и не пушить без отдельной просьбы; каждая задача заканчивается тестом и проверкой diff.
- Текущие A-pose и ракурсы орка остаются отклонёнными; нельзя помечать их approved ради прохождения конвейера.

---

### Task 1: Независимые identity-lock, preset и negative-профили

**Files:**
- Create: `scripts/native/identity/identity-lock.json`
- Create: `scripts/native/identity/negative-character.json`
- Create: `scripts/native/identity/negative-building.json`
- Create: `scripts/native/identity/negative-generic.json`
- Create: `scripts/native/identity/negative-none.json`
- Create: `scripts/native/identity/preset-orc.json`
- Create: `scripts/native/identity/preset-building.example.json`
- Create: `scripts/native/identity_config.mjs`
- Test: `tests/native-identity-config.test.mjs`

**Interfaces:**
- Consumes: существующие пути runner-ов и JSON-файлы без новых npm-зависимостей.
- Produces: `resolveIdentityConfig({ identityRoot, presetFile, negativeProfile, negativeFile, purpose })`, синхронно возвращающий `{ prompt, negative, preset, negativeProfile: object, identityLock: object, sources: object }`; `purpose` принимает только `apose` или `views`.

- [ ] **Step 1: Написать failing-тесты независимой сборки**

```js
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
  assert.match(views.prompt, /Change only the body pose, camera view and framing/);
  assert.match(apose.prompt, /Do not redesign the nose, chin, jaw, eyes, ears/);
  assert.match(apose.prompt, /character's own RIGHT shoulder/);
  assert.match(apose.negative, /extra arms/);
  assert.notEqual(apose.prompt, views.prompt);
});

test('building preset never inherits orc or character rules', () => {
  const result = resolveIdentityConfig({ identityRoot, presetFile: building, negativeProfile: 'building', purpose: 'views' });
  assert.match(result.prompt, /two-storey wooden house/);
  assert.doesNotMatch(result.prompt, /orc|pauldron|tusks/);
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
```

Run: `node --test tests/native-identity-config.test.mjs`
Expected: FAIL, потому что `identity_config.mjs` ещё не существует.

- [ ] **Step 2: Добавить общий identity-lock и отдельные negative JSON**

`identity-lock.json`:

```json
{
  "version": "1.0.0",
  "base": "Edit the supplied reference into exactly the same object. Change only the body pose, camera view and framing. Preserve the source design, proportions, materials, colors, inventory and asymmetry. Do not redesign the nose, chin, jaw, eyes, ears, tusks, scars, hair or other defining features when present. Do not add, duplicate, mirror or symmetrically recreate any item. Do not improve, modernize or replace ambiguous details with genre stereotypes. Keep exactly one instance of every source item and no new objects.",
  "purposes": {
    "apose": "For a character, produce one full-body FRONT view in a relaxed neutral A-pose: upright head and torso, arms lowered slightly away from the torso, open empty hands, straight parallel legs, both feet flat and visible. Preserve the head design instead of substituting a generic face. Center the entire object on a plain neutral gray background.",
    "views": "Produce coherent orthographic front, side and back views of the same object in the same neutral pose and outfit. Preserve the exact profile silhouette and all left/right differences. Use a plain neutral gray background and keep the entire object visible."
  }
}
```

`negative-character.json`:

```json
{
  "version": "1.0.0",
  "kind": "character",
  "terms": ["extra arms", "extra legs", "extra fingers", "duplicate equipment", "duplicate pauldron", "second pauldron", "symmetrical replacement of asymmetric equipment", "changed face", "cropped figure", "multiple characters", "weapon", "held prop", "watermark", "text", "blurry", "noisy"]
}
```

`negative-building.json`:

```json
{
  "version": "1.0.0",
  "kind": "building",
  "terms": ["extra floors", "extra windows", "extra doors", "duplicate towers", "duplicated wings", "changed roof structure", "changed footprint", "cropped building", "multiple buildings", "watermark", "text", "blurry", "noisy"]
}
```

`negative-generic.json`:

```json
{
  "version": "1.0.0",
  "kind": "generic",
  "terms": ["watermark", "text", "collage", "blurry", "noisy", "low contrast"]
}
```

`negative-none.json`:

```json
{
  "version": "1.0.0",
  "kind": "any",
  "terms": []
}
```

- [ ] **Step 3: Добавить preset орка и независимый пример здания**

`preset-orc.json`:

```json
{
  "version": "1.0.0",
  "kind": "character",
  "name": "orc",
  "positive": "The same green-skinned fantasy orc with his exact source face, protruding lower tusks, black beard and topknot, muscular proportions, scars and painted game-concept-art style.",
  "identityFeatures": [
    "Preserve the original short broad nose, heavy brow, chin, jaw, beard and tusk placement; do not substitute a long nose or a generic orc face.",
    "Keep the original topknot silhouette and all visible scars."
  ],
  "inventory": [
    "Exactly one oversized spiked metal pauldron on the character's own RIGHT shoulder, seen on the LEFT side of a front image; the other shoulder is bare.",
    "Keep the source's different left and right wrist coverings; do not give both arms identical spiked bracers.",
    "Keep the source's two different boots and their original left/right placement; do not replace them with a matching fur-trimmed pair.",
    "Keep the original belts, chest strap, ring, pouches and loincloth without duplicates."
  ],
  "asymmetry": [
    "The pauldron remains on the character's own right shoulder in every view.",
    "Wrist coverings and boots remain intentionally different."
  ],
  "viewNotes": [
    "Side views must have a credible human-orc profile with the source nose, lips, chin and tusks, never a long triangular facial wedge."
  ]
}
```

`preset-building.example.json`:

```json
{
  "version": "1.0.0",
  "kind": "building",
  "name": "building-example",
  "positive": "The same two-storey wooden house with the exact source footprint, roof silhouette, window positions and materials.",
  "identityFeatures": ["Preserve the roof, chimney, porch and facade proportions."],
  "inventory": ["Keep exactly one chimney and one front porch when present in the source."],
  "asymmetry": ["Do not mirror an asymmetric extension onto the opposite side."],
  "viewNotes": ["Hidden walls must follow the visible roof and floor structure without added rooms or towers."]
}
```

- [ ] **Step 4: Реализовать resolver с валидацией до GPU**

```js
import { readFileSync } from 'node:fs';
import path from 'node:path';

const PURPOSES = new Set(['apose', 'views']);
const PROFILE_KINDS = new Set(['character', 'building', 'generic', 'none']);

function readJson(file, label) {
  let value;
  try { value = JSON.parse(readFileSync(file, 'utf8')); }
  catch (error) { throw new Error(`Unable to read ${label}: ${file}: ${error.message}`); }
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`${label} must be a JSON object.`);
  return value;
}

function strings(value, field) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !item.trim())) {
    throw new Error(`${field} must be an array of non-empty strings.`);
  }
  return value.map((item) => item.trim());
}

export function resolveIdentityConfig({ identityRoot, presetFile, negativeProfile, negativeFile, purpose }) {
  if (!PURPOSES.has(purpose)) throw new Error('Identity purpose must be apose or views.');
  if (negativeProfile && negativeFile) throw new Error('--negative-profile and --negative-file are mutually exclusive.');
  if (!presetFile) throw new Error('--preset is required.');
  const lock = readJson(path.join(identityRoot, 'identity-lock.json'), 'identity-lock');
  const preset = readJson(path.resolve(presetFile), 'object preset');
  if (!lock.version || typeof lock.base !== 'string' || typeof lock.purposes?.[purpose] !== 'string') throw new Error('Invalid identity-lock.');
  if (!['character', 'building', 'generic'].includes(preset.kind)) throw new Error('Preset kind must be character, building or generic.');
  const positive = [preset.positive, ...strings(preset.identityFeatures, 'identityFeatures'), ...strings(preset.inventory, 'inventory'), ...strings(preset.asymmetry, 'asymmetry'), ...strings(preset.viewNotes, 'viewNotes')];
  if (typeof preset.positive !== 'string' || !preset.positive.trim()) throw new Error('Preset positive is required.');
  let profileName = negativeProfile || (['character', 'building'].includes(preset.kind) ? preset.kind : 'generic');
  let profilePath = negativeFile ? path.resolve(negativeFile) : path.join(identityRoot, `negative-${profileName}.json`);
  if (!negativeFile && !PROFILE_KINDS.has(profileName)) throw new Error(`Unknown negative profile: ${profileName}`);
  const profile = readJson(profilePath, 'negative profile');
  if (!profile.version || !Array.isArray(profile.terms) || profile.terms.some((term) => typeof term !== 'string')) throw new Error('Invalid negative profile.');
  if (profile.kind !== 'any' && profile.kind !== 'generic' && profile.kind !== 'custom' && profile.kind !== preset.kind) {
    throw new Error(`Negative profile kind ${profile.kind} does not match preset kind ${preset.kind}.`);
  }
  const prompt = [lock.base, lock.purposes[purpose], ...positive].join(' ');
  const negative = profile.terms.map((term) => term.trim()).filter(Boolean).join(', ');
  return {
    prompt,
    negative,
    preset,
    negativeProfile: profile,
    identityLock: lock,
    sources: {
      identityLock: path.join(identityRoot, 'identity-lock.json'),
      preset: path.resolve(presetFile),
      negative: profilePath,
      purpose,
    },
  };
}
```

Run: `node --test tests/native-identity-config.test.mjs`
Expected: 4 PASS.

- [ ] **Step 5: Проверить diff без коммита**

Run: `git diff --check -- scripts/native/identity scripts/native/identity_config.mjs tests/native-identity-config.test.mjs`
Expected: нет whitespace-ошибок; новые untracked-файлы дополнительно перечислены через `git status --short`.

### Task 2: Неизменяемые attempts, SHA-256 manifest и человеческая приёмка

**Files:**
- Create: `scripts/native/attempt_store.mjs`
- Create: `scripts/native/review_native_attempt.mjs`
- Test: `tests/native-attempt-store.test.mjs`

**Interfaces:**
- Consumes: resolved config из Task 1 и обычные файлы изображений.
- Produces: `createAttempt(outputRoot, stage) -> { id, dir }`; `sha256File(file) -> string`; `writeManifest(attemptDir, value)`; `readManifest(attemptDir)`; `setReview(attemptDir, status, comment)`; `assertApprovedAttempt(attemptDir, stage) -> manifest`.

- [ ] **Step 1: Написать failing-тесты попыток и блокировок**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createAttempt, sha256File, writeManifest, setReview, assertApprovedAttempt } from '../scripts/native/attempt_store.mjs';

function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), 'af-attempt-'));
  const attempt = createAttempt(root, 'apose');
  const image = path.join(attempt.dir, 'apose-front.png');
  writeFileSync(image, Buffer.from('png-one'));
  const manifest = {
    version: 1,
    id: attempt.id,
    stage: 'apose',
    status: 'success',
    artifacts: { 'apose-front.png': sha256File(image) },
  };
  writeManifest(attempt.dir, manifest);
  return { root, attempt, image };
}

test('attempts never overwrite earlier output', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'af-attempts-'));
  assert.equal(createAttempt(root, 'apose').id, 'attempt-001');
  assert.equal(createAttempt(root, 'apose').id, 'attempt-002');
});

test('review is pending by default and approval verifies artifact hashes', () => {
  const { attempt, image } = fixture();
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /not approved/);
  setReview(attempt.dir, 'approved', 'Face and equipment match the source.');
  assert.equal(assertApprovedAttempt(attempt.dir, 'apose').stage, 'apose');
  writeFileSync(image, Buffer.from('changed-after-review'));
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /SHA-256 mismatch/);
});

test('wrong stage and rejected review block the next stage', () => {
  const { attempt } = fixture();
  setReview(attempt.dir, 'rejected', 'Invented profile nose.');
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'apose'), /not approved/);
  assert.throws(() => assertApprovedAttempt(attempt.dir, 'views'), /stage/);
});
```

Run: `node --test tests/native-attempt-store.test.mjs`
Expected: FAIL, потому что `attempt_store.mjs` отсутствует.

- [ ] **Step 2: Реализовать attempt store**

```js
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
    .filter((entry) => entry.isDirectory() && /^attempt-\d{3}$/.test(entry.name))
    .map((entry) => Number(entry.name.slice(-3)));
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
  return JSON.parse(readFileSync(path.join(attemptDir, 'manifest.json'), 'utf8'));
}

export function setReview(attemptDir, status, comment) {
  if (!REVIEWS.has(status)) throw new Error('Review must be pending, approved or rejected.');
  const manifest = readManifest(attemptDir);
  if (manifest.status !== 'success') throw new Error('Only a successful attempt can be reviewed.');
  for (const [relative, expected] of Object.entries(manifest.artifacts || {})) {
    const file = path.join(attemptDir, relative);
    if (!existsSync(file) || sha256File(file) !== expected) throw new Error(`Artifact SHA-256 mismatch: ${relative}`);
  }
  writeJson(path.join(attemptDir, 'review.json'), { status, comment: String(comment || ''), reviewedAt: new Date().toISOString() });
}

export function assertApprovedAttempt(attemptDir, stage) {
  const manifest = readManifest(attemptDir);
  if (manifest.stage !== stage) throw new Error(`Attempt stage must be ${stage}.`);
  if (manifest.status !== 'success') throw new Error('Attempt did not complete successfully.');
  const review = JSON.parse(readFileSync(path.join(attemptDir, 'review.json'), 'utf8'));
  if (review.status !== 'approved') throw new Error(`Attempt is not approved: ${review.status}.`);
  for (const [relative, expected] of Object.entries(manifest.artifacts || {})) {
    const file = path.join(attemptDir, relative);
    if (!existsSync(file) || sha256File(file) !== expected) throw new Error(`Artifact SHA-256 mismatch: ${relative}`);
  }
  return manifest;
}
```

- [ ] **Step 3: Добавить отдельную команду приёмки**

`review_native_attempt.mjs`:

```js
#!/usr/bin/env node
import path from 'node:path';
import { setReview } from './attempt_store.mjs';

function parse(argv) {
  const result = { attempt: null, status: null, comment: '' };
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!value) throw new Error(`Missing value for ${flag}`);
    if (flag === '--attempt') result.attempt = value;
    else if (flag === '--status') result.status = value;
    else if (flag === '--comment') result.comment = value;
    else throw new Error(`Unknown option: ${flag}`);
  }
  if (!result.attempt || !result.status) throw new Error('--attempt and --status are required.');
  return result;
}

const options = parse(process.argv.slice(2));
const attemptDir = path.resolve(options.attempt);
setReview(attemptDir, options.status, options.comment);
console.log(JSON.stringify({ attempt: attemptDir, status: options.status, comment: options.comment }, null, 2));
```

Run: `node --test tests/native-attempt-store.test.mjs`
Expected: 3 PASS.

- [ ] **Step 4: Проверить CLI на копии, не на текущем орке**

Run: временную fixture создать только тестом Task 2; не вызывать approve для `data/runtime/orc-apose`.
Expected: rejected/pending блокируются; тестовый approved проходит; изменение одобренного PNG снова блокирует этап.

### Task 3: A-pose использует resolver и сохраняет отдельную попытку

**Files:**
- Modify: `scripts/native/qwen_apose_workflow.mjs`
- Modify: `scripts/native/run_native_qwen_apose.mjs`
- Modify: `tests/native-qwen-apose.test.mjs`
- Create: `scripts/native/apose_comparison.py`

**Interfaces:**
- Consumes: `resolveIdentityConfig`, `createAttempt`, `writeManifest`, `sha256File`.
- Produces: `buildQwenAposeWorkflow({ image, seed, steps, prompt, negative })` с обязательными строками prompt/negative; `parseAposeOptions()` с `presetFile`, `negativeProfile`, `negativeFile`; успешный attempt с `apose-front.png`, `comparison.jpg`, `manifest.json`, `review.json`.

- [ ] **Step 1: Заменить тесты глобальных орк-промтов тестами явной конфигурации**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildQwenAposeWorkflow, parseAposeOptions } from '../scripts/native/qwen_apose_workflow.mjs';

test('Qwen workflow preserves Q4 nodes and uses only resolved prompts', () => {
  const graph = buildQwenAposeWorkflow({ image: 'orc.png', prompt: 'LOCK POSITIVE', negative: 'LOCK NEGATIVE' });
  assert.equal(graph['1'].inputs.unet_name, 'Qwen-Image-Edit-2509-Q4_K_M.gguf');
  assert.equal(graph['7'].inputs.prompt, 'LOCK POSITIVE');
  assert.equal(graph['8'].inputs.prompt, 'LOCK NEGATIVE');
  assert.equal(graph['11'].inputs.denoise, 1);
  assert.throws(() => buildQwenAposeWorkflow({ image: 'orc.png' }), /prompt and negative/);
});

test('A-pose CLI requires preset and separates negative selectors', () => {
  const options = parseAposeOptions(['--input', 'C:/orc.png', '--preset', 'C:/orc.json', '--negative-profile', 'character']);
  assert.equal(options.presetFile, 'C:/orc.json');
  assert.equal(options.negativeProfile, 'character');
  assert.equal(options.negativeFile, null);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png']), /--preset/);
  assert.throws(() => parseAposeOptions(['--input', 'C:/orc.png', '--preset', 'C:/orc.json', '--negative-profile', 'none', '--negative-file', 'C:/n.json']), /mutually exclusive/);
});
```

Run: `node --test tests/native-qwen-apose.test.mjs`
Expected: FAIL на отсутствующих обязательных prompt/negative и preset-опциях.

- [ ] **Step 2: Удалить глобальные APOSE_PROMPT/APOSE_NEGATIVE и принимать resolved значения**

В `qwen_apose_workflow.mjs` оставить workflow и заменить начало функции:

```js
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
    '9': { class_type: 'ModelSamplingAuraFlow', inputs: { model: ['1', 0], shift: 3 } },
    '10': { class_type: 'CFGNorm', inputs: { model: ['9', 0], strength: 1 } },
    '11': { class_type: 'KSampler', inputs: { model: ['10', 0], positive: ['7', 0], negative: ['8', 0], latent_image: ['6', 0], seed, steps, cfg: 4, sampler_name: 'euler', scheduler: 'simple', denoise: 1 } },
    '12': { class_type: 'VAEDecode', inputs: { samples: ['11', 0], vae: ['3', 0] } },
    '13': { class_type: 'SaveImage', inputs: { images: ['12', 0], filename_prefix: 'astra/identity-apose-front' } },
  };
}
```

В parser добавить поля и конфликт:

```js
const options = {
  input: null, outputDir: null, jsonFile: null, seed: 42, steps: 40,
  presetFile: null, negativeProfile: null, negativeFile: null,
};
```

Обрабатывать `--preset`, `--negative-profile`, `--negative-file`; после цикла:

```js
if (!options.presetFile) throw new Error('--preset is required');
if (options.negativeProfile && options.negativeFile) throw new Error('--negative-profile and --negative-file are mutually exclusive');
```

- [ ] **Step 3: Создать comparison sheet и подключить attempts к runner**

Создать `apose_comparison.py` целиком:

```python
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

CELL = 768
HEADER = 42


def font():
    for name in ('arial.ttf', 'segoeui.ttf'):
        try:
            return ImageFont.truetype('C:/Windows/Fonts/' + name, 22)
        except OSError:
            continue
    return ImageFont.load_default()


def contain(image, size):
    image = image.convert('RGB')
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    panel = Image.new('RGB', (size, size), (128, 128, 128))
    panel.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    return panel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--generated', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.source)
    generated = Path(args.generated)
    output = Path(args.output)
    if not source.is_file() or not generated.is_file():
        raise SystemExit('Both source and generated images are required.')
    sheet = Image.new('RGB', (CELL * 2, HEADER + CELL), (28, 30, 34))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 9), 'SOURCE', fill=(240, 242, 246), font=font())
    draw.text((CELL + 12, 9), 'A-POSE', fill=(240, 242, 246), font=font())
    with Image.open(source) as image:
        sheet.paste(contain(image, CELL), (0, HEADER))
    with Image.open(generated) as image:
        sheet.paste(contain(image, CELL), (CELL, HEADER))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)
    print(f'RESULT {output.resolve()}')


if __name__ == '__main__':
    main()
```

В runner до проверки порта:

```js
const resolved = resolveIdentityConfig({
  identityRoot: path.join(root, 'scripts', 'native', 'identity'),
  presetFile: options.presetFile,
  negativeProfile: options.negativeProfile,
  negativeFile: options.negativeFile,
  purpose: 'apose',
});
if (resolved.preset.kind !== 'character') throw new Error('A-pose requires a character preset.');
const attempt = createAttempt(options.outputDir || path.join(root, 'data', 'runtime', 'identity-apose'), 'apose');
```

`output` должен быть `path.join(attempt.dir, 'apose-front.png')`; workflow вызывается с `prompt: resolved.prompt, negative: resolved.negative`. После скачивания PNG вызвать изолированный native Python для comparison sheet и записать manifest:

```js
const manifest = {
  version: 1,
  id: attempt.id,
  stage: 'apose',
  backend: 'windows-rocm',
  status: 'success',
  seed: options.seed,
  steps: options.steps,
  source: { path: source, sha256: sha256File(source) },
  identity: resolved,
  workflow,
  artifacts: {
    'apose-front.png': sha256File(output),
    'comparison.jpg': sha256File(path.join(attempt.dir, 'comparison.jpg')),
  },
  error: null,
};
writeManifest(attempt.dir, manifest);
```

При ошибке записать manifest в тот же attempt со `status: 'error'`, `error: error.message`, сохранённым resolved config и без выдуманных artifact hashes. Старый общий `qwen-apose.json` больше не перезаписывать; `--json` может создать дополнительную копию отчёта только по явному пути.

Run: `node --test tests/native-qwen-apose.test.mjs tests/native-identity-config.test.mjs tests/native-attempt-store.test.mjs`
Expected: все PASS; ComfyUI и GPU не запускаются.

- [ ] **Step 4: Проверить отсутствие старых глобальных промтов**

Run: `git diff --check -- scripts/native/qwen_apose_workflow.mjs scripts/native/run_native_qwen_apose.mjs scripts/native/apose_comparison.py tests/native-qwen-apose.test.mjs`
Expected: нет ошибок; импортов `APOSE_PROMPT`, `APOSE_NEGATIVE` не осталось.

### Task 4: Views требуют approved A-pose; shape требует approved views

**Files:**
- Modify: `scripts/native/multiview_orc_workflow.mjs`
- Modify: `scripts/native/run_native_orc_multiview.mjs`
- Modify: `tests/native-multiview-orc.test.mjs`
- Create: `tests/native-attempt-integration.test.mjs`

**Interfaces:**
- Consumes: `resolveIdentityConfig`, `assertApprovedAttempt`, `createAttempt`, `writeManifest`, `sha256File`.
- Produces: `parseOrcOptions()` с preset/negative selectors; `resolveApprovedAposeInput(input) -> { image, attemptDir, manifest }`; `resolveApprovedViewsDir(input) -> { viewsDir, attemptDir, manifest }`; approved views attempt с `views/`, `views-sheet.jpg`, `manifest.json`, `review.json`.

- [ ] **Step 1: Написать failing integration-тесты границ этапов**

```js
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createAttempt, sha256File, setReview, writeManifest } from '../scripts/native/attempt_store.mjs';
import { resolveApprovedAposeInput, resolveApprovedViewsDir } from '../scripts/native/run_native_orc_multiview.mjs';

function approved(stage) {
  const root = mkdtempSync(path.join(tmpdir(), `af-${stage}-`));
  const attempt = createAttempt(root, stage);
  const files = stage === 'apose' ? ['apose-front.png'] : ['views/norm/cut_front.png', 'views/norm/cut_left.png', 'views/norm/cut_back.png', 'views/norm/cut_right.png'];
  const artifacts = {};
  for (const relative of files) {
    const file = path.join(attempt.dir, relative);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, relative);
    artifacts[relative] = sha256File(file);
  }
  writeManifest(attempt.dir, { version: 1, id: attempt.id, stage, status: 'success', artifacts });
  setReview(attempt.dir, 'approved', 'Synthetic integration fixture.');
  return attempt.dir;
}

test('views accepts only an approved unchanged A-pose attempt', () => {
  const dir = approved('apose');
  assert.equal(resolveApprovedAposeInput(dir).image, path.join(dir, 'apose-front.png'));
  assert.throws(() => resolveApprovedAposeInput(path.join(dir, 'apose-front.png')), /manifest|approved/i);
});

test('shape resolves RGBA cutouts only from approved views', () => {
  const dir = approved('views');
  const resolved = resolveApprovedViewsDir(dir);
  assert.equal(resolved.viewsDir, path.join(dir, 'views'));
  assert.match(resolved.manifest.id, /^attempt-/);
});
```

Run: `node --test tests/native-attempt-integration.test.mjs`
Expected: FAIL, потому что resolver-функции ещё не экспортированы.

- [ ] **Step 2: Реализовать явные approved-boundaries**

```js
export function resolveApprovedAposeInput(input) {
  const attemptDir = path.resolve(input);
  const manifest = assertApprovedAttempt(attemptDir, 'apose');
  return { image: path.join(attemptDir, 'apose-front.png'), attemptDir, manifest };
}

export function resolveApprovedViewsDir(input) {
  const attemptDir = path.resolve(input);
  const manifest = assertApprovedAttempt(attemptDir, 'views');
  return { viewsDir: path.join(attemptDir, 'views'), attemptDir, manifest };
}
```

До создания ComfyUI child process:

- для `views`: вызвать `resolveApprovedAposeInput(options.input)` и передать возвращённый `image` в `views_generate`;
- для `shape` и `texture`: вызвать `resolveApprovedViewsDir(options.viewsDir)`;
- прямой PNG, прежний `data/runtime/orc-apose/views`, pending/rejected и изменённые после приёмки файлы должны завершаться ошибкой до старта сервера.

- [ ] **Step 3: Заменить ORC prompt constants на resolver и изолировать views attempt**

Удалить `ORC_VIEW_PROMPT` и `ORC_VIEW_NEGATIVE`. В `parseOrcOptions` добавить `presetFile`, `negativeProfile`, `negativeFile`, обрабатывать те же флаги, что и Task 3. Для stage `views` требовать `--preset`; конфликт selectors отклонять. Для shape/texture preset не нужен, поскольку approved manifest уже содержит конфигурацию.

До старта GPU для views:

```js
const resolved = resolveIdentityConfig({
  identityRoot: path.join(root, 'scripts', 'native', 'identity'),
  presetFile: options.presetFile,
  negativeProfile: options.negativeProfile,
  negativeFile: options.negativeFile,
  purpose: 'views',
});
const attempt = createAttempt(options.outputDir || path.join(root, 'data', 'runtime', 'identity-views'), 'views');
```

`views_generate` получает `outDir: path.join(attempt.dir, 'views')`, `prompt: resolved.prompt`, `negative: resolved.negative`, `useGeneratedFront: true`. После normalize создать `views-sheet.jpg` существующей командой:

```powershell
python scripts/views/contact_sheet.py "<attempt>\views-sheet.jpg" "A-pose=<attempt>\views"
```

Manifest views должен включать:

```js
const artifacts = { 'views-sheet.jpg': sha256File(path.join(attempt.dir, 'views-sheet.jpg')) };
for (const slot of ['front', 'left', 'back', 'right']) {
  const relative = `views/norm/cut_${slot}.png`;
  artifacts[relative] = sha256File(path.join(attempt.dir, relative));
}
const manifest = {
  version: 1,
  id: attempt.id,
  stage: 'views',
  backend: 'windows-rocm',
  status: 'success',
  seed: options.viewSeed,
  sourceApose: { attemptDir: approvedApose.attemptDir, manifestSha256: sha256File(path.join(approvedApose.attemptDir, 'manifest.json')) },
  identity: resolved,
  views,
  artifacts,
  error: null,
};
writeManifest(attempt.dir, manifest);
```

Ошибка записывается в attempt со `status: 'error'`; прежние views не перезаписываются.

- [ ] **Step 4: Обновить старые workflow-тесты и проверить RGBA contract**

Убрать тест, ожидающий `ORC_VIEW_PROMPT/ORC_VIEW_NEGATIVE`; проверить новые CLI-поля и обязательный preset. Сохранить существующие тесты Draft 512, light UV 1024 и `norm/cut_*.png`.

Run: `node --test tests/native-identity-config.test.mjs tests/native-attempt-store.test.mjs tests/native-qwen-apose.test.mjs tests/native-multiview-orc.test.mjs tests/native-attempt-integration.test.mjs`
Expected: все PASS без GPU.

- [ ] **Step 5: Проверить integration rejected-path на текущих изображениях без изменения review**

Run:

```powershell
node scripts/native/run_native_orc_multiview.mjs --stage shape --views data/runtime/orc-apose/views --output data/runtime/orc-apose-new-check
```

Expected: немедленная ошибка об отсутствующем approved manifest; порт 8195 не запускается, GLB не создаётся. Не создавать approved review для текущих ракурсов.

### Task 5: Документация, regression-набор и окончательная проверка

**Files:**
- Modify: `scripts/native/README.md`
- Modify: `docs/forge/views.md`
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `docs/superpowers/specs/2026-11-04-astraforge-amd-local-pose-normalization-design.md`
- Test: все `tests/*.test.mjs` и существующие Python unittest

**Interfaces:**
- Consumes: фактические CLI и отчёты Tasks 1–4.
- Produces: документированные команды для preset/negative, review и approved-boundaries; честный статус визуальной приёмки.

- [ ] **Step 1: Записать точные команды без реального GPU-запуска**

Документировать генерацию A-pose:

```powershell
node scripts/native/run_native_qwen_apose.mjs --input data/runtime/orc/views/raw/front.png --preset scripts/native/identity/preset-orc.json --negative-profile character --output data/runtime/orc-identity-v2
```

После просмотра `<attempt>\comparison.jpg` документировать reject:

```powershell
node scripts/native/review_native_attempt.mjs --attempt "<attempt>" --status rejected --comment "Changed nose, chin, boots or asymmetric equipment"
```

И approve только после реального визуального совпадения:

```powershell
node scripts/native/review_native_attempt.mjs --attempt "<attempt>" --status approved --comment "Checked face, tusks, hair, equipment sides, boots and duplicates against source"
```

Для approved A-pose документировать views:

```powershell
node scripts/native/run_native_orc_multiview.mjs --stage views --input "<approved-apose-attempt>" --preset scripts/native/identity/preset-orc.json --negative-profile character --output data/runtime/orc-views-v2 --coherent-front true
```

Для здания показать независимость negative-профиля на resolver-уровне, не выдумывать поддержку A-pose здания:

```powershell
node --input-type=module -e "import path from 'node:path'; import { resolveIdentityConfig } from './scripts/native/identity_config.mjs'; const root = path.resolve('scripts/native/identity'); const result = resolveIdentityConfig({ identityRoot: root, presetFile: path.join(root, 'preset-building.example.json'), negativeProfile: 'building', purpose: 'views' }); console.log(JSON.stringify({ prompt: result.prompt, negative: result.negative }, null, 2));"
```

Expected: prompt описывает двухэтажный деревянный дом, negative содержит `extra floors` и не содержит `extra arms`, `pauldron` или `orc`. Рабочая поддержка дома в этом плане ограничена resolver и JSON contract, поскольку approved A-pose предназначен только для персонажей.

- [ ] **Step 2: Обновить фактический статус**

В документации указать:

- старые орк A-pose/views отклонены из-за лица, профиля и симметризации;
- prompt/negative больше не являются глобальными константами;
- negative-профили независимы от presets;
- нет автоматической гарантии идентичности и автоматической визуальной приёмки;
- shape без approved views блокируется;
- реальный новый A-pose/views ещё не считается успешным, пока не создан и не просмотрен;
- никаких production-ready заявлений и изменений `native-ready.json`.

- [ ] **Step 3: Запустить полную регрессию**

```powershell
npm test
python -m unittest discover -s scripts/native -p "test_*.py" -v
git diff --check
```

Expected: все Node/Python тесты PASS; допустимы только ранее существующие conditional skips с их прежними причинами; нет whitespace-ошибок. Если полный набор обнаружит прежнее unrelated падение, записать точный тест и не объявлять его исправленным без доказательства.

- [ ] **Step 4: Проверить покрытие спецификации и остановиться перед GPU**

Проверить вручную:

- character/building/generic/none/custom negative выбираются независимо;
- орк-правила отсутствуют в building result;
- attempts не перезаписываются;
- prompt/negative/preset/versions/seed/SHA-256 сохраняются;
- pending/rejected/missing/changed-after-review блокируют следующий этап;
- current orc остаётся rejected;
- RGBA `norm/cut_*.png` остаются входом shape;
- GPU, драйверы, сеть и production capability не менялись.

После этого отдельно предложить пользователю одну новую A-pose попытку. Не запускать её автоматически: ожидаемое время на этом компьютере по предыдущему замеру — около 31 минуты, поэтому пользователь запускает команду в своём PowerShell. После результата сначала просмотреть comparison, затем только по явной приёмке перейти к views.

- [ ] **Step 5: Проверить diff без коммита**

```powershell
git status --short
git diff --stat
git diff --check
```

Expected: в списке только предусмотренные исходники, тесты, документация и существующие прежние изменения; случайные GLB, runtime-модели, логи, `AstraForge-Setup-0.5.1.exe` и `native-ready.json` не добавлены. Commit/push не выполнять без отдельной команды пользователя.
