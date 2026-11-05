# Edit3D AMD Result Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both AMD GLB scenes initialize reliably in embedded Edit3D and complete an actual visual/material review before enabling editor mode.

**Architecture:** Keep the existing project and stable scene/asset IDs. Treat startup timeout separately from scene-code failure, use the plugin SDK and real scene protocol, and avoid rewriting a scene that already loads in the desktop content preview.

**Tech Stack:** Edit3D scene protocol 1, Three.js 0.186, Node 22, InAppBrowser offscreen and desktop-container surfaces.

## Global Constraints

- Project: `pixal3d-amd-preview`, artifact `961e562a-1032-4bb6-a70c-147da3865cd9`.
- Do not change `review.status` to `passed` without a real visual review.
- Do not delete `data/edit3d.state.json` or overwrite user edits.
- Stop only verification resources created in this turn; keep the user's existing tabs and services.

---

### Task 1: Reproduce and classify startup behavior

**Files:**
- Inspect: `pixal3d-amd-preview/package.json`, `src/main.js`, `src/project.js`, `src/scene.js`
- Create if a code regression is confirmed: `pixal3d-amd-preview/src/scene.test.js`

**Interfaces:**
- Consumes: logical scene URL with the real artifact ID and absolute `projectPath`.
- Produces: distinct evidence for service startup timeout, model loading failure or scene exception.

- [ ] **Step 1: Check compilation and plugin attachment**

Run: `npm run check && npm run build` in `pixal3d-amd-preview`.
Expected: PASS; scripts remain `node .edit3d/run.mjs ...` and `sceneProtocol` remains 1.

- [ ] **Step 2: Record desktop and offscreen observations**

Use `list_tabs`, then the real scene URL. Confirm `document.getElementById('app').edit3dPreview` and inspect `getScene()` for mesh/material counts. For offscreen use `offscreen:true`, `projectPath`, 1280×720 and explicit tab IDs.
Current baseline to retest: desktop content preview initializes; offscreen returns `SCENE_HANDSHAKE_TIMEOUT`.

- [ ] **Step 3: Capture the actual failure category**

Read `document.documentElement.dataset.edit3dStartupError`, page text, model network responses and console errors in one pass. A handshake timeout without a scene exception is not fixed by changing geometry or review status.

- [ ] **Step 4: Add a regression test only for a confirmed code defect**

For example, if both scenes are being initialized instead of the default, assert factory call counts:

```js
const project = createProject();
assert.equal(project.defaultSceneId, 'single');
assert.deepEqual(project.scenes.map(s => s.id), ['single', 'multi']);
```

Run `node --test src/scene.test.js`; preserve the failing result before a fix. If no code defect is shown, do not create a cosmetic patch.

### Task 2: Apply the minimal confirmed fix

**Files:**
- Modify only the demonstrated source of failure: `src/main.js`, `src/project.js` or `src/scene.js`
- Test: `src/scene.test.js` when Task 1 identifies a code defect

**Interfaces:**
- Preserves: `single`, `multi`, `model_single`, `model_multi`, `ground`, `camera_main` and all model URLs.

- [ ] **Step 1: Implement the confirmed minimal correction**

For a service/startup timeout, first warm the project with `npm run check` and retry one offscreen session; do not alter scene content. For a concrete scene exception, change only the implicated statement and add its regression assertion. Keep `createAmdResultScene` returning `{scene,camera,dispose}` and awaiting `asset.loadModel`.

- [ ] **Step 2: Re-run focused checks**

Run: `node --test src/scene.test.js` when present, then `npm run check`.
Expected: PASS and no `SCENE_INITIALIZATION_FAILED` or handshake timeout in the new offscreen session.

- [ ] **Step 3: Verify both scene factories**

Use the mounted preview handle:

```js
const p = document.getElementById('app').edit3dPreview;
await p.switchScene('single');
const single = p.getScene();
await p.switchScene('multi');
const multi = p.getScene();
```

Expected: each scene contains its model and ground, named materials and a valid camera; no load/decode errors.

### Task 3: Visual review and editor gate

**Files:**
- Modify after evidence: `src/review.js`
- Create outside the project during verification: screenshot returned by the browser tool

- [ ] **Step 1: Capture actual canvas images**

For each scene call `contactSheet({subject:'model_single'})` or `model_multi`, then screenshot with `target:'canvas'`. Do not substitute `check/build` or scene object counts for pixels.

- [ ] **Step 2: Perform one find-fix-reverify round**

Inspect framing, grounding, materials and clipping. If a defect appears, adjust only the responsible scene value, rerun `contactSheet` and compare the new canvas. Do not simplify geometry or remove materials to make review easier.

- [ ] **Step 3: Check material cards in the visible desktop container**

Select each model and ground, verify displayed names match `asset.getMaterials()`, and confirm no unnamed placeholder remains. Hidden-tab screenshots are not evidence; use offscreen for content and a visible container for desktop controls.

- [ ] **Step 4: Update review only after checks pass**

Set `status:'passed'` with concrete screenshot/material evidence and retain any real limitations. Run `npm run check && npm run build`, reopen the editor and verify initialization, scene switching and no console errors.

### Task 4: Sync and cleanup

**Files:**
- No intentional source changes.

- [ ] **Step 1: Sync the user's existing preview**

On the matching desktop tab, save and dispatch `window.dispatchEvent(new Event('edit3d:reload'))`; verify `performance.timeOrigin` changes, artifact ID remains and `edit3dProject` initializes after the passed review.

- [ ] **Step 2: Close only this turn's verification tabs**

Close explicit offscreen tab IDs and confirm they disappear. Do not close the user's GitHub or pre-existing scene tabs.

- [ ] **Step 3: Deliver**

Give the absolute project path, one scene-protocol preview link and actual GLB links. State visual verification, preview sync and cleanup separately; if a visible material-card check remains blocked, leave `review.status:'pending'` and report that limitation.
