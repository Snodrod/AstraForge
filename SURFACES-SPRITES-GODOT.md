# Textures, baking and Godot

## Textures

Select a model → **Textures → Create UV atlas**. xatlas runs on the CPU and carries attributes, morphs and weights over to vertices duplicated along UV seams. Rebuilding the UVs requires re-placing the previous maps.

You can then create Color / Roughness / Metallic / Normal from a "Metal", "Stone", "Wood" or "Fabric" material at a resolution from 512 to 4096, load your own maps and paint on Base Color. These four bases are procedural, without neural generation. Normal uses the OpenGL convention.

**Bake visible colour** transfers the image of the selected side into a single UV atlas. The normal direction and the depth buffer are used to cull hidden surfaces. A reference that matches the pose and the selected view is required. Hidden sides keep their previous colour; add other views for them. The button does not reconstruct unknown details, does not create a full PBR layout from a photograph and does not replace AI texturing.

Brush: left mouse button — stroke, right mouse button / wheel — camera, Ctrl+Z — undo a stroke. PNG maps are downloaded as a separate ZIP. To keep materials and map orientation together with the model, use GLB.

Projects are saved as gzip JSON with textures and skeleton. Old JSON projects can still be opened. The limit for an unpacked project is 512 MB; 4K maps and the undo history need more RAM.

## All animations in GLB

In the **Sprites** tab set the FPS → **Bake all into GLB**. Each clip is evaluated through AnimationMixer and written as uniform position/quaternion/scale/morph weight keys, including the last pose. Bones and weights are preserved. Particles are rendered into sprites and video; arbitrary VFX are not carried into GLB.

## Isometric sprites

By default: **8 directions, 35° above the horizon, transparent background**. Set the size, FPS, clips, loops and effect inclusion → **Export PNG + Godot**.

The ZIP contains the `AstraForgeSprites` folder: PNG RGBA atlases up to 4096 px, `metadata.json` with rectangles and pivot, and `animations.tres`. Extract the folder into the root of the Godot project. For `AnimatedSprite2D`, assign `animations.tres` as SpriteFrames. For example, `Walk_dir0` is the first view and `Walk_dir1` is the next one, 45° later. The zero view is shot by the camera from the +Z side; match the direction correspondence to the orientation of your character.

A single frame and scale are used for all directions; Root is fixed for in-place movement. The bounding box is estimated from several poses, so extreme short motions need visual checking. The pivot of the feet is stored in the metadata; AnimatedSprite2D itself uses the texture centre, so set its offset / position from the pivot. For loops the trailing duplicate frame is not included; for one-shot actions the last pose is included.

The export is limited to 2048 frames and 180 million pixels per package. For long clips, lower the FPS or choose clips one at a time.

## Limitations

The base walks are procedural drafts without IK and without foot contact with the surface. They must be corrected for a game character. Neural geometry also needs checking of silhouette, anatomy and mesh budget.
