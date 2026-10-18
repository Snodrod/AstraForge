# Views: 3D from several views (ChatGPT or local MV-Adapter)

State as of 2026-10-13. Installation and memory — `docs/forge/views_install.md`, HTTP routes — `docs/forge/API.md` ("Views" section).

## Why

Hunyuan3D from a single image sees only the front: the back, sides and thickness are inferred. The Hunyuan3D-2mv model accepts
up to four views at once (front, left, back, right). The more views, the less guessing and the fewer "empty" areas on the back.
But usually you have **one** view. So the app can create the rest and build 3D from all of them.

```
front view (T-pose) ──► views: left / back / right ──► one frame (1536², long side 1280)
        │                  ChatGPT | MV-Adapter | your files        │
        └────────────────────────────────────────────────────────────┴──► Hunyuan3D-2mv ──► GLB
```

## What creates the views

| Source | How it works | Speed (RTX 4070) | Strengths | Weaknesses |
|---|---|---|---|---|
| **ChatGPT** | the Codex app on a ChatGPT subscription, no API key; each view is sent the source image (and the back, if already done) | 57-65 s per view, 3 views in ~3 min | keeps style, cut, details of clothing and gear; an honest back | the image goes to OpenAI; depends on the subscription and the ChatGPT queue; the view is not strictly orthographic |
| **Local (MV-Adapter)** | SDXL base + `mvadapter_i2mv_sdxl`, ComfyUI on port 8190, no internet | ~34 s for all views (640 px, 30 steps), 41 s with preparation | offline, strictly orthographic views at one scale, consistent silhouettes | invents the details at the back and sides (on the test character the back came out with a yoke instead of a solid blue), the style is a bit softer, resolution 640 px |
| **Your own files** | any slot can be uploaded by yourself (PNG/JPG/WEBP) | — | the most accurate if you have real views | — |

Your own views always stay: automatic generation fills only empty slots (the ↻ button on a slot recreates exactly that one).

Slots: **front, left, back, right**. The convention for their meaning:

- "left" — the camera is on the **character's left-hand** side, the character looks toward the **left edge** of the frame;
- "right" — the camera is to the character's right, the character looks toward the right edge;
- for MV-Adapter, azimuth 90° is "left", 270° is "right" (checked on a real run with an asymmetric belt pouch);
- in a side view of a T-pose the near arm points into the camera (the sleeve/hand is seen end-on), the far one is hidden behind the body: the ChatGPT prompt demands this.
- the model accepts exactly four views; two more (front-left 45°, front-right 315°) are supported only by the API and CLI (`frontLeft`, `frontRight`, they do not affect 3D).

All views are cut out of the background (rembg + a colour mask for the grey studio background), brought to one figure height and placed in a shared frame:
a white 1536 px square, long side 1280 px (the same as ordinary concept preparation does). The floor shadow is removed.

## How to use: the studio

The "Create" tab, the **"Views"** block under "Concept image":

1. Upload the front view (a character in a T-pose, preferably without a weapon: a weapon in the hands confuses the side views).
2. Choose the source: "ChatGPT" or "Local". The **"Generate views"** button fills the empty slots; progress is shown at the top, and you can cancel.
3. You can drag your own file into any slot or click the upload icon. Hovering over a finished slot offers: replace, recreate, remove.
4. Above the button is the **"3D engine"** switch: **Pixal3D** (more accurate and crisper, about 4 minutes, ~10 GB VRAM; the default if installed) or **Hunyuan3D-2mv**
   (faster, softer shape). Details and numbers — `docs/forge/pixal3d.md`.
5. **"Create 3D from views"**: an ordinary generation job (queue, cancel, "Results" history), quality and seed come from the "Geometry quality" block below
   (Pixal3D has no quality switch). If there is only one view (just the front), the button is inactive: for one view there is the ordinary button.

The result is loaded into the scene as after an ordinary generation. In "Results" such a model is labelled "Model from views (N views)" (for Pixal3D prefixed "Pixal3D ·"),
"Re-extract the surface" works for Hunyuan3D (the sample is taken from the engine cache: a repeat took 3 s); Pixal3D has no such button.

## How to use: the command line

```
# 1. views
node scripts/views/views_generate.mjs --input front.png --out views/codex     --provider codex
node scripts/views/views_generate.mjs --input front.png --out views/mvadapter --provider mvadapter
#    your own view + the missing ones from ChatGPT:
node scripts/views/views_generate.mjs --input front.png --out views/mix --provider codex --manual back=my_back.png
#    redo just one view:
node scripts/views/views_generate.mjs --out views/codex --provider codex --slots left --overwrite
#    what is ready to work:
node scripts/views/views_generate.mjs --status

# 2. 3D from any set of views (front is required)
node scripts/views/views_3d.mjs --views views/codex --out mesh_4v.glb --quality high --seed 703
node scripts/views/views_3d.mjs --views views/codex --out mesh_2v.glb --slots front,back
#    engine: --engine pixal3d | hunyuan | auto (default auto = Pixal3D if installed); Pixal3D has its own --seed (1000) and --faces (700000)
node scripts/views/views_3d.mjs --views views/codex --out mesh_px.glb --engine pixal3d

# 3. untextured comparison (Blender) and a summary sheet
blender -b --factory-startup --python scripts/views/compare_render.py -- <renders folder> label=mesh.glb ...
python scripts/views/compare_sheet.py <renders folder> spec.json views_3d_compare.png
python scripts/views/contact_sheet.py views_sheet.jpg "ChatGPT=views/codex" "MV-Adapter=views/mvadapter"
```

`views_generate` parameters: `--slots left,back,right,frontLeft,frontRight`, `--seed`, `--size 512|640|768` (the app lowers 768 to 640 by itself),
`--steps`, `--note "character details for ChatGPT"`, `--aspect 3:4`, `--comfy http://127.0.0.1:8190`. The Python scripts run from the ComfyUI environment (`<ComfyUI dir>/.venv`).
As a library: `import { views_generate } from './scripts/views/views_generate.mjs'`.

## The set folder

```
<folder>/views.json                  sources, seconds, prompts, seed, progress of the last run
<folder>/front.png left.png back.png right.png   the finished set in one frame (the same as norm/norm_<slot>.png)
<folder>/raw/<slot>.png              as received (ChatGPT, MV-Adapter, uploaded by you)
<folder>/norm/norm_<slot>.png        shape generator input: white 1536² frame, shared scale
<folder>/norm/cut_<slot>.png         the same with a transparent background; thumb_<slot>.jpg — thumbnail; frame.json — frame and scales
<folder>/work/                       intermediate (provider images, the MV-Adapter reference frame)
```

In the studio the sets live in `data/views/<id>/` (not in git). A set is bound to a concept: the same concept opens the same set.

## Result on the test character (T-pose, no weapon)

Input: `front_tpose.png` of the test character. Results in `views/`: `views_sheet.jpg` (views from the two sources),
`views_3d_compare.png` and `views_3d_compare_seed11.png` (untextured models: front, left, back, 3/4), `views_3d_compare*.json` (numbers),
`codex/` and `mvadapter/` (sets with all files and `views.json`). The best four-view model: `mv/mesh_mv_views4.glb`
(ChatGPT, seed 703; normalisation the same as the other `mesh_mv_*.glb`: raw ComfyUI output, height axis +-1).

View creation: ChatGPT 57 / 64 / 65 s (left / back / right, 3 views 191 s); MV-Adapter 33.8 s for three views together (41 s with preparation).
Hunyuan3D-2mv, "Detailed" quality (45 steps, 512³): 2 views 153-156 s, 4 views 176-180 s.

Silhouette IoU (render of the untextured model against the image; 1 — a match; two seeds each):

| Set | seed | front | left | back | right |
|---|---|---|---|---|---|
| ChatGPT, 2 views (front + back) | 703 | 0.905 | 0.790 | 0.875 | 0.789 |
| ChatGPT, 2 views | 11 | 0.925 | 0.790 | 0.902 | 0.789 |
| **ChatGPT, 4 views** | 703 | **0.936** | 0.847 | 0.924 | 0.843 |
| **ChatGPT, 4 views** | 11 | **0.932** | 0.849 | 0.906 | 0.846 |
| MV-Adapter, 4 views | 703 | 0.934 | 0.895 | 0.937 | 0.896 |
| MV-Adapter, 4 views | 11 | 0.926 | 0.876 | 0.929 | 0.876 |

How to read it. The only real image here is the front view (it is also the input in every variant): the mean over two seeds is 0.915 for two views, 0.934 for four
ChatGPT views and 0.930 for four MV-Adapter views. The gain on the front view is small (on seed 11 it is within the seed spread). The IoU for the left, back and right views shows
how closely the model sticks to the images given to it, not to the truth: with two views the model simply has no side images (0.79), with four the views match at 0.84-0.90.
The real gain is visible by eye (sheet `views_3d_compare.png`): with two views the sides and thickness are inferred (the helmet and tunic "float" in profile), with four the profile,
belt, pouch and boots read well, and the back repeats the supplied view.

An honest verdict on the sources for this character:

- **ChatGPT**: the views are very close to the source in cut, colour and detail; the side views came out as real profiles with the arm "into the camera", the back is plausible (belt, pouch on the character's right).
  The model from these views is the most detailed and the most faithful. Downside: a minute per view, a subscription is needed, the image goes to OpenAI.
- **MV-Adapter**: fast, offline, the views are strictly orthographic and consistent with each other, so the silhouettes in 3D match the images better (0.88-0.90).
  But the details are invented: on the back, instead of a solid blue tunic, a light "yoke" appeared, the figure in profile is fuller than the original, the resolution is low (640 px).
  Fine for a draft and when going online is not allowed; for a final character ChatGPT or your own views are better.
- Left/right check (views supplied with the sides swapped): the model barely reacts to the swap (silhouettes and pouch stay in place), so a convention error would be hard to notice here;
  the convention was chosen by the model's view order (front, left, back, right: the camera circles the character counter-clockwise seen from above) and matches the MV-Adapter azimuths.

## Limitations

- ChatGPT draws views rather than renders them: small discrepancies between views are possible (tunic length, fold positions). Hunyuan3D-2mv smooths them but does not fix them.
- All sources guess the hidden parts. On the test character MV-Adapter got the back wrong; ChatGPT got it mostly right, but that is one character. Check against your own intent.
- Hunyuan3D-2mv accepts four views; the 45° ones do not affect 3D. The left/right sides were checked by the model's view order and by geometry,
  but on a symmetric character swapping left/right barely changes the result (checked: the swap gave the same silhouettes).
- Local MV-Adapter: size <= 640 px with four views on 12 GB; six views only at 512. While it runs the GPU is busy, as in 3D generation (the shared GPU lock).
- Characters not in a T-pose, four-legged creatures and objects were not checked: the prompts and MV-Adapter are designed for upright figures.
- 3D from views uses `hunyuan3d-dit-v2-mv.safetensors`; without this model the button returns a clear error.
- Privacy: the "ChatGPT" source sends the source view (and the back) to OpenAI through the Codex app. "Local" sends nothing.

## Files

`scripts/views/` — `views_generate.mjs` (the `views_generate` entry), `views_3d.mjs`, `codex_image.mjs`, `views_mvadapter.mjs`, `views_prompts.mjs`,
`views_prep.py` (cut-out and shared frame), `make_sdxl_config.py`, `compare_render.py`, `compare_sheet.py`, `contact_sheet.py`, `comfy_client.mjs`.
Server: `server/views.mjs` (`/api/views` routes), `server/schema.mjs` (`hunyuanMultiViewWorkflow`, `viewSetId`), `server/jobs.mjs` (the multi-view job branch).
Interface: `src/ViewsPanel.jsx`, `src/views.css`. ComfyUI node: `data/runtime/custom_nodes/ComfyUI-MVAdapter/` (`astra_views.py`). Tests: `tests/views.test.mjs`.
