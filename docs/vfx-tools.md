# "VFX" tab: texture generators for effects

The catalog comes from the "Texture Generators" section of Simon Schreibt's
[Textures for VFX](https://simonschreibt.notion.site/Textures-for-VFX-Database-2c72eccccfa84a0eae927d778ad746cc) database
(GDC talk "How (not) to create Textures for VFX"): 31 tools. The list with licenses is in
`server/vfx-tools-catalog.json`, which is also the source for the UI and for the install script.

## What is where

| What | Where |
|---|---|
| Catalog | `server/vfx-tools-catalog.json` |
| Server (`/api/vfx-tools`) | `server/vfx-tools.mjs` |
| Web generators | `http://127.0.0.1:3211/vfx-web/<id>/` (`ASTRA_VFX_WEB_PORT`): a separate port, so that third-party JS does not get the studio's origin and its API |
| UI | `src/VfxPanel.jsx`, `src/vfx.css`, the "VFX" tab in `src/App.jsx` |
| Install | `scripts/vfx/Install-VfxTools.ps1`; in the wizard - the item "VFX texture generators" (id `vfx`) |
| Tools folder | `ASTRA_VFX_TOOLS` (key `vfxTools` in `config/paths.json`), default `C:\AstraForge\tools\vfx`; the wizard installs into `runtime\vfx` |

Inside the tools folder: `apps/` - portable programs, `downloads/` - installers, `web/` - web generators,
`textures/` - finished textures. The tab shows this folder and can apply textures from it to the selected object.

## Kinds of tools

- **Programs** (launched from the tab): Material Maker 1.7 (MIT), FlowMap Painter 0.9.2, Pseudo Volume Generator 2.7.
- **With an installer** ("Install" button, then "Launch"): PixaFlux, KumoWorks 1.0.1 (BSD-3), Slate.
- **Web generators inside the studio**, working offline: Texture Generator Online and NormalMap Online (MIT),
  Greeble/DisplacementX (GPL-3.0, built from source with `base: '/vfx-web/displacementx/'`), Neon Flames.
- **A template**, Impact Decal (CC0), opens in Material Maker.
- **Online**, paid tools, Substance Designer and tutorials - links only.

## Installation

```powershell
powershell -ExecutionPolicy Bypass -File scripts\vfx\Install-VfxTools.ps1            # everything
powershell -ExecutionPolicy Bypass -File scripts\vfx\Install-VfxTools.ps1 -DryRun    # plan only
powershell -ExecutionPolicy Bypass -File scripts\vfx\Install-VfxTools.ps1 -Only material-maker,neonflames
```

The script downloads from official sites (GitHub, pixaflux.com, facedownfx, teckartist, materialmaker.org) and skips
what is already downloaded. It only downloads installers: the user runs them. DisplacementX is built with npm
(Node.js required). Pseudo Volume Generator is served by itch.io only through a button on the site: put
`3D Volume Generator 2.7 Win.zip` into `downloads` and run the script again.

## Limitations

- The programs are not part of the AstraForge archive, each has its own license. Neon Flames is published without a
  license: for personal use only, do not include it in a build for sale.
- 2026-10-16: Fateloom does not open, the Flipped Normals links (Martin Schmitter's generators) return 404 -
  they are marked `unavailable` in the catalog.
- The free Smoke Trail Generator (Gumroad, $0+) and Free Seamless Textures Generator (the orange box) require
  placing a free order with an e-mail address, so they are downloaded manually.

# Effects editor ("Effects" tab)

Effects are made right in the studio, without external programs:

1. Select an object or a bone of the model -> the "Effects" tab -> a preset (12 from `scripts/blender/data/fx_presets.json`:
   engine trail, aura, fire, smoke, sparks, ice, poison, healing, muzzle flash and others) or "Empty".
2. The effect is placed as an emitter in the object tree under the selected part (the lightning-bolt icon). Its marker is
   moved with the gizmo (W/E/R), the emitter scale changes the particle size. Particles run live when paused and along
   the timeline during playback and WebM recording.
3. Parameters are edited right in the viewport: emission, shape, motion, appearance (size, curve, color gradient,
   blending), texture, flipbook, light. Ctrl+Z undoes edits.
4. Texture: 5 built-in, "From the VFX folder", "From disk" or "Create a texture in the generator" - the built-in generator
   (10 types, 20 recipes: glow, spark, star, ring/shockwave, smoke, flame, lightning, noises, trail, magic circle;
   flipbooks 2x2/4x4/8x8, seamless noises). "Use in the effect" saves the PNG into the assets and assigns it to the
   selected effect together with the flipbook layout.
5. Effects are saved in the project. "ZIP for Godot 4.5" exports `fx/*.tscn` (GPUParticles3D, as in the forge), the
   textures, `fx_bindings.json`, `af_fx_mount.gd` and a README. In the GLB the emitters appear as empty nodes with the
   parameters in extras.

Code: `src/fx-core.js` (format `astraforge-fx/1`, particles, .tscn - byte-for-byte as `_export_lib.py`),
`src/fx-runtime.js` (rendering), `src/FxPanel.jsx`, `src/fx-textures.js` + `src/FxTextureLab.jsx` (generator),
a copy of the presets for the browser is `src/fx-data.js` (a test compares it with `scripts/blender/data`; to update:
`FX_SYNC=1 node --test tests/fx-core.test.mjs`).
