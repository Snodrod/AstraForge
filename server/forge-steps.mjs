// Forge step registry and pipeline presets (data only + small validators).
//
// Every entry mirrors one scripts/blender/steps/<step>.py module. Reconciled with the real run(req)
// of every module on 10.10.2026 (names, types, defaults, enums, input slots, output keys); keep this
// file declarative: edit the tables, not the code below them.
//
// Param schema: {type, default, min, max, enum, nullable, items, minItems, maxItems, alt, title, help,
//                server, auto, path}
//   type    'number' | 'integer' | 'boolean' | 'string' | 'array' | 'object' | 'any'
//   alt     alternative schemas: a value that fails the main schema is accepted when it satisfies one
//           of them (e.g. head_dir 'auto' | '-Y' … or an [x, y] vector; item_scale number or 'auto').
//           The UI renders the main schema.
//   server  true  -> consumed by the server, never sent to Blender
//   auto    'asset_id' (UUID) | 'asset_slug' (snake_case game id) | 'animations' (clip library
//           folder) -> filled by the server when the request leaves it empty ('animations' is
//           always the server's library for HTTP clients)
//   path    true  -> the value is a local file/folder path: only the CLI (--direct) may set it
// Only explicitly given params are sent to Blender; `default` documents the step module's own
// default for the UI and is used for GPU/timeout decisions. The wizard writes out the scalar
// defaults of generators (item_kit, ship_gen, turret_gen), so their defaults must equal the module's.
// Inputs:  {slots, required, formats, manifest}
//   slots    which chain inputs the step consumes, in preference order:
//            'blend' (current .blend), 'mesh' (current GLB or source mesh), 'glb' (current GLB),
//            'character' + 'item' (attach_prop); [] = generator / library step
//   manifest true -> the server adds inputs.manifest (snapshot of asset.json at the start of the run)

export const KINDS = ['humanoid', 'creature', 'item', 'ship', 'prop'];
export const ALL_KINDS = [...KINDS];
export const CLIP_CATEGORIES = ['idle', 'walk', 'run', 'attack', 'cast', 'hit', 'death', 'jump', 'emote', 'fly', 'other'];
export const LOOP_CATEGORIES = ['idle', 'walk', 'run'];
const MESH_FORMATS = ['glb', 'gltf', 'fbx', 'obj', 'stl'];
const VIEW_NAMES = ['front', 'back', 'left', 'right', 'top', 'three_quarter'];
const FACING = ['auto', '-Y', '+Y', '-X', '+X'];
const CREATURE_CLIPS = ['idle-loop', 'walk-loop', 'run-loop', 'fly-loop', 'glide-loop', 'attack', 'hit', 'death'];
const ITEM_TYPES = ['sword', 'greatsword', 'dagger', 'axe', 'battle_axe', 'mace', 'warhammer', 'spear', 'halberd',
  'staff', 'wand', 'bow', 'crossbow', 'shield_round', 'shield_kite', 'shield_tower', 'buckler', 'torch', 'potion',
  'book', 'blaster_pistol', 'blaster_rifle'];
const ITEM_KINDS = ['auto', 'blade', 'axe', 'blunt', 'polearm', 'staff', 'wand', 'torch', 'shield'];
const SHIP_CLASSES = ['fighter', 'interceptor', 'gunship', 'corvette', 'freighter', 'miner', 'frigate', 'carrier', 'transport'];
const SHIP_STYLES = ['industrial', 'military', 'sleek', 'alien'];
const WING_STYLES = ['auto', 'none', 'delta', 'swept', 'forward', 'straight', 'x', 'stub', 'crescent'];
const THRUSTER_FRAMES = ['thrust', 'exhaust'];
const SOURCES = ['procedural', 'geometry_nodes', 'hunyuan3d', 'trellis', 'instantmesh', 'triposr', 'manual', 'mixed', 'external'];
const SHIP_LAYOUTS = 'fighter/interceptor: arrow, twin_boom, dart; gunship: brick, hammer, catamaran; corvette: destroyer, spear, ' +
  'arrowhead; freighter: truss, flatbed, bulk; miner: pods, crane, scoop; frigate: wedge, hammer, fork; carrier: flattop, ' +
  'hangar_pods, stepdeck; transport: pods, drums, barge';

// alternative schemas (see `alt` above)
const AUTO = { type: 'string', enum: ['auto'] };
const VEC2 = { type: 'array', items: { type: 'number' }, minItems: 2, maxItems: 3 };
const VEC3 = { type: 'array', items: { type: 'number' }, minItems: 3, maxItems: 3 };
const PER_CLIP = { type: 'object' };

const num = (def, min, max, title, help, extra = {}) => ({ type: 'number', default: def, min, max, title, help, ...extra });
const int = (def, min, max, title, help, extra = {}) => ({ type: 'integer', default: def, min, max, title, help, ...extra });
const bool = (def, title, help, extra = {}) => ({ type: 'boolean', default: def, title, help, ...(def === null ? { nullable: true } : {}), ...extra });
const oneOf = (values, def, title, help, extra = {}) => ({ type: 'any', enum: values, default: def, title, help, ...extra });
const str = (def, title, help, extra = {}) => ({ type: 'string', default: def, title, help, nullable: def === null, ...extra });
const list = (def, title, help, extra = {}) => ({ type: 'array', default: def, title, help, ...(def === null ? { nullable: true } : {}), ...extra });
const obj = (def, title, help, extra = {}) => ({ type: 'object', default: def, title, help, nullable: def === null, ...extra });
const opt = { nullable: true };
const preview = (size = 384) => ({
  preview: bool(true, 'Preview', 'Preview image of the result.'),
  preview_size: int(size, 64, 2048, 'Preview size, px', 'Side of one view.'),
});

const STEP_LIST = [
  // ---------------------------------------------------------------- core
  {
    step: 'inspect', module: 'core', title: 'Model inspection',
    help: 'Import and statistics: triangles, size, skeleton, clips; preview image.',
    kinds: ALL_KINDS, inputs: { slots: ['mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['preview'],
    params: {
      preview: bool(true, 'Preview', 'Render the view sheet.'),
      views: list(['front', 'left', 'back', 'three_quarter'], 'Views', 'Angles of the preview sheet.', { items: { type: 'string', enum: VIEW_NAMES } }),
      size: int(384, 64, 2048, 'Frame size, px', 'Side of one view.'),
    },
  },
  {
    step: 'cleanup', module: 'core', title: 'Mesh cleanup',
    help: 'Scale, floor, vertex merge, junk removal, simplification, normals, optionally a voxel remesh with color baking. The input rig is discarded.',
    kinds: ALL_KINDS, inputs: { slots: ['mesh', 'blend'], required: true, formats: MESH_FORMATS },
    outputs: ['glb', 'blend', 'preview'], timeoutSec: 30 * 60,
    gpuWhen: { all: [{ param: 'voxel_remesh_m', op: '>', value: 0 }, { param: 'bake_device', op: '==', value: 'GPU' }] },
    params: {
      height_m: num(null, 0.01, 1000, 'Height, m', 'Empty - keep the scale.', opt),
      rotate_z_deg: num(0, -360, 360, 'Rotation around Z, deg', 'Before everything else: turn to face -Y (facing +X means -90).'),
      merge_distance: num(null, 0, 0.1, 'Vertex merge, m', 'Weld threshold; empty - 1e-5 x height, 0 - no welding.', opt),
      remove_islands_below: num(0, 0, 1, 'Remove islands smaller than', 'Fraction of vertices (0 - do not remove).'),
      target_tris: int(null, 500, 2000000, 'Triangles', 'Target budget; empty - no simplification.', opt),
      symmetry: oneOf(['X', 'Y', 'Z', null], null, 'Symmetry', 'Symmetric simplification along an axis.'),
      planar_angle_deg: num(0.5, 0, 10, 'Flat pre-pass, deg', 'Angle of the flat pass before collapse (budget only); 0 - off.'),
      decimate_stage_ratio: num(0.3, 0.05, 0.95, 'Fraction per step', 'Minimum fraction of triangles that one collapse step keeps.'),
      seam_weight: num(null, 0, 1, 'UV seam protection', 'Extra seam weight during simplification; empty - off (measured to be worse).', opt),
      voxel_remesh_m: num(null, 0, 1, 'Voxel remesh, m', 'Voxel size (about 0.0033 x height); empty or 0 - no remesh.', opt),
      bake_size: oneOf([512, 1024, 2048, 4096], 2048, 'Bake size', 'Texture side when transferring color after the remesh.'),
      bake_device: oneOf(['CPU', 'GPU'], 'CPU', 'Bake device', 'GPU takes the shared GPU lock.'),
      bake_samples: int(4, 1, 256, 'Bake samples', 'Cycles samples per texel.'),
      bake_normal: bool(false, 'Bake normals', 'Normal map from the original; the mesh after the remesh is shaded fully smooth.'),
      bake_emission: bool(false, 'Bake emission', 'Emission map from the original.'),
      bake_extrusion_m: num(null, 0, 1, 'Bake cage, m', 'Empty - max(2 x voxel, 0.005 x height).', opt),
      bake_ray_m: num(null, 0, 2, 'Ray length, m', 'Empty - 2 x cage.', opt),
      smart_uv_angle_deg: num(66, 1, 89, 'Smart UV angle, deg', 'Unwrap of the mesh after the remesh.'),
      shade_auto_smooth_deg: num(40, 0, 180, 'Auto smooth, deg', 'Hard-edge angle; 0 - flat, empty - as on import.', opt),
      recalc_normals: bool(true, 'Recalculate normals', 'Flip normals outward.'),
      join: bool(true, 'Join objects', 'Merge all meshes into one.'),
      name: str('Body', 'Object name', 'Name of the resulting mesh.'),
      ground: bool(true, 'Put on the floor', 'Bottom at z = 0.'),
      center: bool(true, 'Center', 'Center X/Y at zero.'),
      ...preview(512),
    },
  },
  {
    step: 'convert', module: 'core', title: 'Format conversion',
    help: 'GLB <-> FBX <-> OBJ, optionally FBX for upload to Mixamo. Rig and animation are kept (except for OBJ and Mixamo).',
    kinds: ALL_KINDS, inputs: { slots: ['blend', 'mesh'], required: true, formats: MESH_FORMATS },
    outputs: ['glb', 'fbx', 'obj', 'blend', 'textures'],
    params: {
      format: oneOf(['glb', 'fbx', 'obj'], 'glb', 'Format', 'Target format (mixamo is always fbx).'),
      name: str('model', 'File name', 'Output file name without extension.'),
      height_m: num(null, 0.01, 1000, 'Height, m', 'Empty - keep (for Mixamo - 1.8).', opt),
      rotate_z_deg: num(0, -360, 360, 'Rotation around Z, deg', ''),
      ground: bool(null, 'Put on the floor', 'Empty - yes if a height is set or Mixamo is on, otherwise keep the position as is.'),
      center: bool(null, 'Center', 'Empty - yes if a height is set or Mixamo is on, otherwise keep the position as is.'),
      mixamo: bool(false, 'For Mixamo', 'One welded mesh without a rig, facing -Y, feet at 0, centimeters, textures embedded, up to 10 MB.'),
      mixamo_max_tris: int(50000, 1000, 500000, 'Triangles for Mixamo', 'Mesh simplification budget for Mixamo.'),
      mixamo_weld_m: num(0.0001, 0, 0.01, 'Weld for Mixamo, m', 'Mixamo auto-skinning fails on meshes cut along UV seams.'),
      mixamo_center: oneOf(['bbox', 'head'], 'bbox', 'Center for Mixamo', 'head - X by the head column (prop at the side).'),
      texture_max: int(2048, 256, 8192, 'Textures up to, px', 'Side of the FBX textures for Mixamo (reduced if the file is over 10 MB).'),
    },
  },
  {
    step: 'separate', module: 'core', title: 'Split into parts',
    help: 'Split the mesh by islands, materials or box zones: part, delete, or an open slit (slit - a gap or a slab along a plane or curve); cuts are closed with caps unless caps is false.',
    kinds: ALL_KINDS, inputs: { slots: ['mesh', 'blend'], required: true, formats: MESH_FORMATS },
    outputs: ['glb', 'blend', 'preview'],
    params: {
      mode: oneOf(['loose', 'material', 'regions', 'slit'], 'loose', 'Mode', 'Islands, materials, zones or slits (slit - the same zones, default kind is slit).'),
      height_m: num(null, 0.01, 1000, 'Height, m', 'Normalization before the split; empty - keep the scale.', opt),
      rotate_z_deg: num(0, -360, 360, 'Rotation around Z, deg', 'Before the split.'),
      ground: bool(true, 'Put on the floor', 'Bottom at z = 0 before the split.'),
      center: bool(true, 'Center', 'Center X/Y at zero before the split.'),
      merge_distance: num(null, 0, 0.1, 'Vertex merge, m', 'Weld before the split; empty - 1e-5 x height, 0 - off.', opt),
      shade_auto_smooth_deg: num(40, 0, 180, 'Auto smooth, deg', 'After welding.'),
      min_fraction: num(0.01, 0, 1, 'Min. part fraction', 'loose: parts smaller than this fraction of vertices are collected into "small".'),
      drop_small: bool(false, 'Drop small parts', 'loose: delete small parts instead of "small".'),
      names: list([], 'Part names', 'loose: names in order of decreasing size.', { items: { type: 'string' } }),
      regions: list([], 'Zones', 'regions / slit: [{name, min: [x, y, z], max: [x, y, z], kind, ...}] - boxes in meters after normalization (Z up, bottom at z = 0, center X/Y at zero); the same name and kind make one zone. kind: part | delete (faces are removed) | slit (open cut). part / delete: caps, facing [x, y, z] + facing_min (0.5) - only faces with normal . facing >= facing_min inside. slit: plane {axis, value} | {point, normal} (default x = box center) or curve {axis, along, points: [[along, value], ...]} - a piecewise-linear surface; width_m 0 - a gap (gap_m spreads the edges), > 0 - a slab of that width, each side with its own cap.', { items: { type: 'object' } }),
      caps: bool(true, 'Caps', 'regions: close the cut edges (part / delete and slit slabs); a zone has its own caps. false - open hem.'),
      slit_gap_m: num(null, 0, 0.1, 'Gap spread, m', 'slit with width_m 0: edges are spread so far that welding in later steps does not close the gap; empty - 0.001 x height.', opt),
      rest_name: str('body', 'Remainder name', 'regions: name of what is left outside the zones.'),
      origin: oneOf(['world', 'center', 'bottom'], 'world', 'Part origin', 'world - parts stay in place.'),
      explode: num(0.35, 0, 3, 'Preview spacing', 'Fraction of the bounding size.'),
      ...preview(512),
    },
  },
  {
    step: 'replace_part', module: 'core', title: 'Part replacement',
    help: 'Remove an area of a finished mesh (smoothed skull, hand, pommel) and put a separately generated part in its place, generated from a crop: ICP alignment, bridge seam, crop texture projection, glowing eyes. The part is a GLB of another asset (part_asset; an upload is also an asset) or a file (part_file, CLI only).',
    kinds: ALL_KINDS, inputs: { slots: ['mesh', 'blend', 'part'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'part_glb', 'compare', 'compare_views'], timeoutSec: 30 * 60,
    params: {
      part_asset: str(null, 'Part asset', 'UUID of the asset with the new part (forward -Y, like the body); the server substitutes its latest GLB into inputs.part.', { server: true }),
      part_file: str(null, 'Part file', 'Path to the part\'s GLB/FBX/OBJ (CLI --direct only); takes precedence over part_asset.', { server: true, path: true }),
      region: obj(null, 'Region', '{min: [x, y, z], max: [x, y, z]} - box around the part being replaced, in meters after normalization (required).'),
      cut_plane: obj(null, 'Cut plane', '{axis: "z", value: 1.5} or {point: [x, y, z], normal: [x, y, z]}; the part is on the +normal side, side: "-" flips the side. Empty - a z plane through region.min.'),
      height_m: num(null, 0.01, 1000, 'Body height, m', 'Body normalization before everything; region and cut_plane are in these meters. Empty - keep the scale.', opt),
      rotate_z_deg: num(0, -360, 360, 'Body rotation around Z, deg', 'Before everything else.'),
      ground: bool(true, 'Put on the floor', 'Body bottom at z = 0 before everything.'),
      center: bool(true, 'Center', 'Body center X/Y at zero before everything.'),
      merge_distance: num(null, 0, 0.1, 'Vertex merge, m', 'Body weld; empty - 1e-5 x height, 0 - off.', opt),
      shade_auto_smooth_deg: num(40, 0, 180, 'Auto smooth, deg', ''),
      min_inside: num(0.5, 0, 1, 'Fraction inside the region', 'A body piece is removed if this fraction of its area is inside the box.'),
      hang_m: num(null, 0, 10, 'Overhang below the cut, m', 'Old pieces below the plane down to this depth are removed along with the region; empty - 0.25 of the box height.', opt),
      align: oneOf(['icp', 'bbox', 'none'], 'icp', 'Alignment', 'icp - by surface; bbox - only the initial similarity by bounding size; none - the part stays in its own pose (after part_transform), with no fit to the removed region: a procedural part such as a bone_kit hand is placed by pose.'),
      part_transform: obj(null, 'Part pose', '{location: [x, y, z], rot_deg: [x, y, z] (Euler XYZ) | matrix: 3x3 by rows, scale: s} - applied to the part before everything (a bone_kit hand with its wrist at zero -> to the wrist).'),
      keep_materials: bool(false, 'Own part materials', 'false - without a texture the part gets one flat part_fill material; true - the part\'s materials are kept (gold rings, bone with vertex colors), UVs and color attributes are matched with the body.'),
      cut_part: bool(true, 'Cut the part', 'true - the part is cut by the plane + seam_gap and stitched to the body with a bridge; false - the whole part, the body hole at the plane is closed with a cap (a hand made of separate bones has no neck for a bridge).'),
      icp_rotation: bool(false, 'ICP with rotation', 'The crop and the body share one camera - no rotation needed.'),
      icp_iters: int(60, 1, 500, 'ICP iterations', ''),
      icp_trim: num(0.85, 0.3, 1, 'Fraction of ICP pairs', ''),
      samples: int(6000, 500, 100000, 'Points per surface', ''),
      max_rotation_deg: num(30, 0, 180, 'ICP rotation limit, deg', 'More - fall back to bbox.'),
      max_rms_rel: num(0.04, 0, 1, 'RMS limit', 'Fraction of the box diagonal (warning).'),
      part_tris: int(20000, 0, 2000000, 'Part triangles', 'Part budget after the cut; 0 - no simplification.'),
      part_min_island: num(0.0005, 0, 1, 'Part debris', 'Fraction of vertices: small bits are removed before simplification.'),
      crumb_fraction: num(0.002, 0, 1, 'Debris after the cut', 'Fraction of vertices.'),
      part_rotate_z_deg: num(0, -360, 360, 'Part rotation around Z, deg', ''),
      seam_gap: num(null, 0, 10, 'Seam gap, m', 'How far above the plane the part is cut (bridge length); empty - 0.02 of the box height.', opt),
      keep_hang: num(null, 0, 10, 'Part overhang, m', 'Depth of the part piece that stays (jaw); empty - = hang_m.', opt),
      texture: obj(null, 'Crop texture', '{image, crop_px: [l, t, r, b], mode: "front_projection", facing_min: 0.12, fade: 0.05, occlusion: false}; image is the path to the crop (CLI --direct only).'),
      eyes: obj(null, 'Eyes', '{auto: true, color: [0.25, 1.0, 0.35] (linear), strength: 6, radius_factor: 0.22, remove_inner_islands: true, snap_to_glow: true}: glowing spheres and sockets socket_fx_eye_l/r.'),
      name: str(null, 'Object name', 'Empty - replaced.'),
      ...preview(384),
    },
  },
  {
    step: 'place_parts', module: 'core', title: 'Parts placement',
    help: 'Copies of a part on the target surface (clasps, buttons, rivets, stones, spikes) or in an explicit pose (a hand at the wrist): placement by the part\'s footprint, normal from the contact patch, the part\'s sockets travel with each copy. The part is a GLB of another asset (part_asset, per point - points[].asset) or a file (part_file / points[].part, CLI only).',
    kinds: ALL_KINDS, inputs: { slots: ['mesh', 'blend', 'part'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'preview'],
    params: {
      part_asset: str(null, 'Part asset', 'UUID of the default part asset (bone_toggle frame: origin at the center of the back face, -Y outward, +Z up); the server substitutes its latest GLB into inputs.part.', { server: true }),
      part_file: str(null, 'Part file', 'Path to the default part\'s GLB/FBX/OBJ/.blend (CLI --direct only); takes precedence over part_asset.', { server: true, path: true }),
      points: list(null, 'Points', 'Required: [x, z] (front view, m), [x, y, z] or {at, asset (UUID of this point\'s part; the server substitutes its GLB into part), part (path, CLI only), name, scale (1), roll_deg (0), snap: surface | none, rot_deg [x, y, z] / matrix 3x3 (snap none: explicit pose at point at [x, y, z]), standoff_m (0), sockets (true), rest_percentile, normal_patch, max_tilt_deg}.', { items: { type: 'any' }, minItems: 1 }),
      target_objects: list(null, 'Target objects', 'Names of target meshes to keep (for example body from a separate file); the rest are dropped. Empty - all meshes.', { items: { type: 'string' } }),
      ray: oneOf(['+Y', '-Y', '+X', '-X', '+Z', '-Z'], '+Y', 'Ray', 'Where the rays go: +Y - parts on surfaces facing -Y (the front of the character). A point [x, z] shoots from behind the bounds, [x, y, z] takes the nearest hit on the line through it.'),
      contact: oneOf(['footprint', 'center'], 'footprint', 'Seating', 'footprint - rays over the back-face footprint, the part rests on the most protruding contacts (bridges a gap or fold); center - the back-face center goes to the hit of the central ray.'),
      normal: oneOf(['fit', 'hit', 'axis'], 'fit', 'Normal', 'fit - a plane over the contact patch the width of the part without deep points; hit - the face normal at the center; axis - strictly against the ray. With contact center the normal is the ray axis unless hit.'),
      footprint_grid: list([5, 3], 'Footprint ray grid', '[nx, nz] rays over the part\'s footprint (each >= 1).', { items: { type: 'integer' }, minItems: 2, maxItems: 2 }),
      footprint_fill: num(0.8, 0.01, 2, 'Footprint fill', 'Fraction of the part size along X and Z covered by the ray grid.'),
      rest_percentile: num(100, 0, 100, 'Support percentile', '100 - the part rests on the most protruding contact (does not sink); less - a single bump enters the part instead of lifting it. Can also be set per point.'),
      normal_patch: num(1, 0.05, 20, 'Normal patch', 'Side of the square patch for fit, as a fraction of the part\'s long side; more - averages over folds. Can also be set per point.'),
      max_tilt_deg: num(45, 0, 90, 'Normal tilt up to, deg', 'The fit normal is no further than this angle from the ray axis. Can also be set per point.'),
      up: list([0, 0, 1], 'Part up', 'World "up" that the part\'s +Z follows; for a ray along Z the fallback up is +Y.', { items: { type: 'number' }, minItems: 3, maxItems: 3 }),
      search_m: num(null, 0.001, 100, 'Search range, m', 'Range of the footprint rays and of the search for a 3D point; empty - max(0.1, 3 x part size).', opt),
      join: bool(true, 'Merge', 'true - target and parts as one mesh; false - each copy a separate object (origin at the back-face center) next to the target.'),
      name: str(null, 'Object name', 'Name of the merged object; empty - the name of the first target mesh.'),
      part_sockets: bool(true, 'Part sockets', 'The part\'s sockets (socket_* / game_socket, for example socket_mount_hand_* of a bone_kit hand) are placed with each copy; a repeated name gets _2, _3. The target\'s sockets are always kept.'),
      height_m: num(null, 0.01, 1000, 'Target height, m', 'Target normalization before placement (off by default: points are in the file frame).', opt),
      rotate_z_deg: num(0, -360, 360, 'Target rotation around Z, deg', 'Before placement.'),
      ground: bool(false, 'Put the target on the floor', 'Target bottom at z = 0 before placement (off by default).'),
      center: bool(false, 'Center the target', 'Target center X/Y at zero before placement (off by default).'),
      ...preview(512),
    },
  },
  {
    step: 'paint_from_concept', module: 'paint', title: 'Paint from concept',
    help: 'Color for an untextured mesh (Hunyuan3D 2.1 gives geometry only): an ortho camera is fitted to the concept silhouette, visible texels take the image color, hidden ones take a fill (the concept "through" for the back, a mirror about the body axis for what is covered by a shield, the nearest visible point of the same part, the back of the head without the face), Smart UV if needed, an emission map from bright colored spots. The concept is the reference image of the generation the asset was made from (default), concept_asset or concept_file (CLI).',
    kinds: ALL_KINDS, inputs: { slots: ['blend', 'mesh', 'concept'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'basecolor', 'emission', 'compare', 'camera', 'camera_back', 'qa_sheet', 'qa'],
    params: {
      concept_asset: str(null, 'Concept (studio image)', 'UUID of a studio image (preferably PNG with alpha); empty - the reference image of the generation the asset was made from.', { server: true }),
      concept_file: str(null, 'Concept file', 'Path to the image (CLI --direct only); takes precedence over concept_asset.', { server: true, path: true }),
      size: oneOf([512, 1024, 2048, 4096], 2048, 'Texture size', 'Side of the color map.'),
      uv: oneOf(['auto', 'keep', 'smart'], 'auto', 'Unwrap', 'auto - keep a usable one (single, no overlaps, within 0..1), otherwise Smart UV; keep - only the existing one; smart - always anew.'),
      uv_pack: oneOf(['aabb', 'concave', 'none'], 'aabb', 'Island packing', 'After Smart UV: aabb - fast (~37 % of the square), concave - denser (~45 %), but ~10 s.'),
      uv_angle_deg: num(66, 1, 89, 'Smart UV angle, deg', ''),
      uv_margin: num(0.003, 0, 0.05, 'Island margin', 'Fraction of the texture side.'),
      camera: { type: 'any', default: 'fit', title: 'Concept camera', help: '"fit" - fit to the silhouette; or {yaw_deg, pitch_deg, px_per_m, offset_px: [x, y]} in concept pixels (from paint_camera.json).', alt: [{ type: 'object' }] },
      yaw_range_deg: num(null, 0, 180, 'Rotation search, +/- deg', 'Around the model front (-Y); empty - 60, 180 for creatures.', opt),
      yaw_step_deg: num(5, 1, 30, 'Rotation search step, deg', ''),
      pitch_range_deg: num(15, 0, 30, 'Pitch search, +/- deg', '0 - the camera is strictly horizontal.'),
      perspective: bool(true, 'Perspective', 'The last fitting stage adds the camera distance (concepts are perspective renders: a shield and sword closer to the camera look bigger); kept only if IoU grows by 0.005 or more.'),
      facing_min: num(0.1, 0, 0.9, 'Grazing faces up to', 'Below - fill only.'),
      facing_full: num(0.4, 0.05, 1, 'Full concept color from', 'Between facing_min and this value the concept blends smoothly into the fill.'),
      depth_tolerance: num(0.004, 0, 0.1, 'Depth tolerance', 'Fraction of the model height (+ a correction for the face tilt) in the z-buffer visibility check.'),
      edge_fade_px: int(null, 0, 64, 'Edge fade, px', 'A band at the edge of the concept silhouette (background-removal halo); empty - 0.4 % of the figure height, at least 2.', opt),
      delight: num(0.25, 0, 1, 'Remove lighting', 'Strength of flattening a large brightness gradient (0 - off; the multiplier stays within 0.85-1.2).'),
      fill_blur: num(0.015, 0, 0.2, 'Fill blur', 'Fraction of the figure height: the fill of the back and hidden spots takes a blurred concept color.'),
      fill_smooth: int(10, 0, 200, 'Fill smoothing', 'Passes over the mesh edges.'),
      back_fill: oneOf(['through', 'none'], 'through', 'Back', 'through - the concept color "through" (the same mesh piece from the front); none - only the nearest visible point.'),
      head_guard: bool(null, 'Back of head without face', 'The back of the head (top of the body column) takes the nearest visible color rather than the face through; empty - yes, except creatures.'),
      head_fraction: num(0.13, 0, 0.5, 'Head height', 'Fraction of the height from the top of the body column.'),
      mirror: oneOf(['auto', 'x', 'none'], 'auto', 'Mirror', 'Places covered at the front (the body behind a shield) take the color of the mirrored visible point: auto - symmetry plane by the shoulders, x - the X plane.'),
      mirror_tolerance: num(0.03, 0, 0.5, 'Mirror tolerance', 'Fraction of the height: the mirrored point must lie this close to the visible surface.'),
      emission: oneOf(['auto', 'none'], 'auto', 'Emission', 'auto - bright saturated cool spots of the concept (eyes, crystals, runes) that stand out from their surroundings; gold, skin and red do not glow.'),
      emission_strength: num(3, 0, 100, 'Emission strength', 'Emission Strength (glTF KHR_materials_emissive_strength).'),
      roughness: num(0.75, 0, 1, 'Roughness', ''),
      metallic: num(0, 0, 1, 'Metallic', ''),
      objects: list([], 'Objects', 'Names of meshes to paint; empty - all except the glowing ones (replace_part eyes).', { items: { type: 'string' } }),
      material_name: str(null, 'Material name', 'Empty - <object>_painted.'),
      concept_max_px: int(2048, 256, 8192, 'Concept up to, px', 'A larger concept is first reduced (the reference image can be up to 4096 px).'),
      concept_back_asset: str(null, 'Back view (studio image)', 'UUID of a studio image with the same character from behind in the same pose: the back is painted from it instead of being guessed from the front.', { server: true }),
      concept_back_file: str(null, 'Back view file', 'Path to the back view image (CLI --direct only); takes precedence over concept_back_asset.', { server: true, path: true }),
      camera_back: { type: 'any', default: 'fit', title: 'Back view camera', help: '"fit" - fit to the back view silhouette; or a dictionary from paint_camera_back.json.' },
      back_yaw_deg: num(180, 0, 360, 'Back view rotation, deg', 'Center of the back view camera search.'),
      back_yaw_range_deg: num(15, 0, 90, 'Back rotation search, +/- deg', ''),
      back_pitch_range_deg: num(6, 0, 30, 'Back pitch search, +/- deg', ''),
      align: oneOf(['none', 'warp'], 'none', 'Fitting the image to the model', 'warp - the image is bent to the model silhouette (arm, helmet and sleeve fall into place, color does not drift); none - camera only.'),
      align_max: num(0.06, 0, 0.3, 'Warp limit', 'Largest displacement, fraction of the height.'),
      align_smooth: num(2, 0.05, 100, 'Warp stiffness', 'More - smoother; 0.3 follows every bump of the outline.'),
      align_iters: int(4, 1, 10, 'Warp iterations', ''),
      align_points: int(450, 50, 2000, 'Outline points', ''),
      flat: bool(false, 'Flat faces', 'Each triangle is one color (low-poly style): no smeared streaks; details smaller than a face (eyes) are averaged.'),
      flat_palette: int(0, 0, 64, 'Face palette', 'With flat faces: reduce colors to this many concept colors; 0 - no palette.'),
      qa_vlm: oneOf(['auto', 'off'], 'auto', 'Check with a local model', 'auto - the view sheet (front, back, 3/4, side) is rated by a vision model in Ollama, if installed; off - numbers only.'),
      qa_model: str(null, 'Check model', 'Empty - ASTRA_QA_VLM or qwen3-vl:8b.'),
      qa_url: str(null, 'Ollama address', 'Empty - ASTRA_OLLAMA_URL or http://127.0.0.1:11434.'),
      qa_min_score: num(6, 0, 10, 'Score threshold', 'Below - a texture_qa warning.'),
      qa_free_comfy: bool(true, 'Free the GPU', 'Before the check, unload ComfyUI models (if its queue is empty).'),
      qa_wait_comfy_s: num(0, 0, 3600, 'Wait for ComfyUI, s', 'How long to wait for an empty ComfyUI queue before skipping the check.'),
      qa_size: int(384, 128, 1024, 'Sheet tiles, px', ''),
      qa_timeout_s: num(300, 10, 3600, 'Check timeout, s', ''),
      ...preview(384),
    },
  },
  {
    step: 'assemble_parts', module: 'parts', title: 'Assembly from parts',
    help: 'Generation by parts: head, torso, arms and legs separately (scripts/parts: each part comes from a reference crop that fills the generator frame, i.e. with many times more voxels); here they are placed on the whole mesh and fused into one model. Scale and X/Z shift follow the silhouette of the front reference (it is the truth for the front view; the whole mesh is less accurate there), Y depth follows the whole mesh (front and back depth maps, thickness from it), joints are trimmed in the middle of the overlap, parts overlap around them by +/-joint_overlap_m, then merged by voxels; a part without a mesh is replaced by pieces of the whole mesh in its frame. Input: the whole mesh (default), the front reference (concept_asset / concept_file) and parts.json (parts_file, CLI --direct only).',
    kinds: ['humanoid'], inputs: { slots: ['blend', 'mesh', 'concept', 'parts'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'preview', 'parts_fit', 'glb_mv'],
    params: {
      parts_file: str(null, 'Parts file (parts.json)', 'Path to parts.json written by scripts/parts/parts_prep.py and parts_generate.py: part frames in reference pixels and paths to their meshes (CLI --direct only).', { server: true, path: true }),
      concept_asset: str(null, 'Front reference (studio image)', 'UUID of a studio image - the front reference the parts were cut from (preferably PNG with alpha); empty - the reference image of the generation the asset was made from.', { server: true }),
      concept_file: str(null, 'Front reference file', 'Path to the image (CLI --direct only); takes precedence over concept_asset.', { server: true, path: true }),
      height_m: num(1.8, 0.1, 100, 'Height, m', 'Height of the whole mesh and of the result; feet at z = 0, face to -Y.'),
      voxel_m: num(null, 0.0005, 0.05, 'Fusion voxel, m', 'Voxel size of the merge; empty - 0.0016 x height. A part is generated with a step down to ~0.5 mm, the whole body ~3.5 mm: a coarse voxel washes out the gain of the parts.', opt),
      target_tris: int(null, 1000, 5000000, 'Triangles', 'Simplify the result to no more than this many triangles; empty - no simplification.', opt),
      refine: oneOf(['silhouette', 'icp', 'none'], 'silhouette', 'Part fit', 'silhouette - scale and X/Z shift by the reference silhouette, depth by the mesh; icp - after that, a symmetric ICP (rigid + uniform scale) against the surface of the whole mesh: it pulls the part toward the errors of the whole mesh; none - by bounding size only.'),
      depth_scale: { type: 'any', enum: ['base', 'part'], default: 'base', title: 'Y thickness', help: 'base - the front-to-back thickness of a part is taken from the whole mesh (clamped to 0.8-1.25), neighboring parts meet without steps; part - the part\'s own proportions; a number - a multiplier on its own thickness.', alt: [{ type: 'number', min: 0.3, max: 3 }] },
      trim_overlap: bool(true, 'Trim joints', 'Each part is cut in the middle of the overlap with its neighbor; otherwise the overlap stays in full (a flat "patch" sticks out where the torso stump is thicker than the arm).'),
      joint_overlap_m: num(null, 0.0005, 0.1, 'Overlap at the joint, m', 'How far parts go past the middle of the joint into each other; empty - 2 voxels.', opt),
      island_min: num(0.002, 0, 0.5, 'Small islands', 'After trimming: pieces of a part smaller than this fraction of vertices (floating generator debris) are removed; 0 - do not remove.'),
      box_expand: num(0.012, 0, 0.2, 'Frame expansion', 'Fraction of the height: the whole mesh in the expanded frame is the target for comparison and ICP.'),
      icp_iters: int(40, 1, 500, 'ICP iterations', 'Only with refine icp.'),
      icp_trim: num(0.8, 0.3, 1, 'Fraction of ICP pairs', 'The worst (1 - fraction) pairs in each direction are discarded.'),
      icp_samples: int(15000, 500, 200000, 'ICP points', 'Surface points of a part (the whole mesh gets twice as many).'),
      icp_max_rot_deg: num(6, 0, 45, 'ICP rotation up to, deg', 'A larger rotation is discarded: only scale and shift are fitted.'),
      icp_scale_range: list([0.8, 1.25], 'ICP scale', '[min, max] how many times ICP may change the scale.', { items: { type: 'number' }, minItems: 2, maxItems: 2 }),
      icp_warn_m: num(0.012, 0.001, 0.5, 'Residual threshold, m', 'part_residual warning: the truncated RMS distance between the part and the whole mesh is above the threshold.'),
      try_flip: bool(true, 'Check flip', 'Each part is also tried rotated by 180 deg around Z (the generator may have turned it face-back); the rotation is taken if the residual drops by at least 40 %.'),
      flip_rms_m: num(0, 0, 1, 'Flip threshold, m', 'A flip is tried only if the residual is not below this value; 0 - always.'),
      fill_gaps: bool(true, 'Fill gaps with the whole mesh', 'A part without a mesh is replaced by a piece of the whole mesh in its frame (flat cuts at the frame faces, neighbors extend into it).'),
      gap_min_tris: int(1500, 0, 10000000, 'Min. gap piece', 'Pieces of the whole mesh in a gap smaller than this number of triangles are dropped.'),
      camera: { type: 'any', default: 'fit', title: 'Reference camera', help: '"fit" - fit scale and shift to the silhouette; or {px_per_m, offset_px: [x, y], centre: [x, y, z]} (parts_fit.json -> camera).', alt: [{ type: 'object' }] },
      pick: obj({}, 'Mesh choice', '{part: seed | path}: which mesh from parts.json goes into the assembly (default is the part\'s mesh).'),
      shade_auto_smooth_deg: num(40, 0, 180, 'Auto smooth, deg', 'Hard-edge angle; 0 - flat, empty - as is.', opt),
      export_mv: bool(false, 'Mesh in generator normalization', 'Also assembled_mv.glb: bbox centered on the origin, long side 2 (like Hunyuan\'s mesh_mv_<seed>.glb): used as the assembly seed instead of the whole mesh.'),
      ...preview(384),
      head_closeup: bool(true, 'Head close-up', 'In the preview: a large head (front and three-quarter), the whole mesh on top, the assembly below.'),
    },
  },
  {
    step: 'extract_props', module: 'items', title: 'Props from the character',
    help: 'Weapons, shields, banners, bows, staves and books in the hands of a generated character are cut out of the mesh (the rig detector in find-only mode + roles.json hints): the cut is at the fist (the fist stays with the body), caps on both sides, texture on both parts. Each prop is a separate GLB with socket_grip_main (shared grip frame), socket_grip_off, socket_fx_tip; a shield with a socket_mount_shield_* frame. A prop that fails the quality check is replaced by a kit prop of the same kind and size in the colors of the cut-out one. The body without props goes to the rig, the props are returned by attach_prop (extracted).',
    kinds: ['humanoid'], inputs: { slots: ['blend', 'mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'props_json', 'items', 'preview', 'detect', 'grips'],
    params: {
      height_m: num(1.8, 0.3, 10, 'Height, m', 'Normalization like rig_humanoid (up to the top of the head).'),
      front: oneOf(FACING, 'auto', 'Facing', 'Face direction in the input; auto - detect.'),
      front_prior: oneOf(['-Y', '+Y', '-X', '+X', null], null, 'Default facing', '-Y for generations from a front concept: a detector answer of "back" or an unsure one yields to it.'),
      fix_yaw: bool(true, 'Extra turn about Z', 'Same as rig_humanoid.'),
      markers: obj(null, 'Joint markers', 'Same as rig_humanoid.'),
      hints: obj(null, 'Hints', 'A roles.json entry: hand_weapon ("sword", "banner + staff", "two swords"), hand (r | l | both), shield, faction, build, notes ("... in the left hand").'),
      use_hints: bool(true, 'Use hints', 'Hints name the kinds of props and enable a search for the shaft that the hand trace did not find, and for banner cloth and shield.'),
      replace: oneOf(['auto', 'never', 'always'], 'auto', 'Kit replacement', 'auto - a prop that fails the check is rebuilt by item_kit of the same kind and size in its colors; never - always the cut-out one; always - always the kit (banner, skull, lantern stay cut out: there are none in the kit).'),
      gate: obj(null, 'Check thresholds', '{open_edge_frac: 0.02, main_part_share: 0.75, parts_over_1pct: 6, min_tris: 120}.'),
      bridge: bool(true, 'Handle through the fist', 'A handle cylinder in place of the cut-out fist (hidden by the body\'s fist).'),
      kit_style: oneOf(['auto', 'medieval', 'dark', 'elven'], 'auto', 'Kit style', 'auto - undead dark, others medieval.'),
      kit_ornament: int(1, 0, 3, 'Kit ornament', '0-3, as in item_kit.'),
      seed: { type: 'any', default: null, title: 'Kit seed', help: 'A number or a string (concept file name).', nullable: true },
      min_item_vertices: int(60, 1, 100000, 'Min. prop vertices', 'Smaller finds stay on the body.'),
      ...preview(320),
    },
  },
  // ---------------------------------------------------------------- humanoid
  {
    step: 'rig_humanoid', module: 'humanoid', title: 'Humanoid auto-rig',
    help: 'A skeleton with Mixamo names, weights, hand/shield/back/belt sockets, pose check. With a gross landmark error the rig is not built (markers are still written).',
    kinds: ['humanoid'], inputs: { slots: ['blend', 'mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'markers_json', 'markers_png', 'poses_png'], timeoutSec: 30 * 60,
    params: {
      height_m: num(1.8, 0.3, 10, 'Height, m', 'Height to the top of the head (a prop above the head does not count).'),
      height_ref: oneOf(['head', 'bbox'], 'head', 'Height by', 'head - to the top of the head, bbox - by bounding size.'),
      front: oneOf(FACING, 'auto', 'Facing', 'Face direction in the input; auto - detect.'),
      markers: obj(null, 'Joint markers', 'Manual coordinates (m, normalized frame): chin, groin, l_wrist... as [x, z] or [x, y, z]; joints: {Bone: [x, y, z]}.'),
      rigid: list([], 'Rigid parts', 'Rules {bone, axis, r | x_min..., merge}: these vertices go entirely to one bone.', { items: { type: 'object' } }),
      auto_rigid: bool(true, 'Find rigid parts', 'A staff, spear, sword, shield in the hand - rigid to the hand.'),
      robe: oneOf(['auto', true, false], 'auto', 'Robe/skirt', 'Cloth below the groin follows the pelvis and thighs without tearing.'),
      robe_feet: bool(false, 'Feet under the robe', 'Toes sticking out from under the robe keep the foot weights.'),
      max_influences: int(4, 1, 8, 'Influences per vertex', 'Godot and glTF - up to 4.'),
      proxy_tris: int(60000, 5000, 500000, 'Proxy triangles', 'Mesh for the weight computation.'),
      prune_limbs: bool(true, 'Trim arm weights', 'Arm weights fade outside the arm tube; a vertex does not follow the arm and the leg at once.'),
      smooth_iterations: int(0, 0, 20, 'Weight smoothing', 'Smoothing iterations on the proxy.'),
      objects: list([], '.blend objects', 'Object names for the rig if the input is a .blend.', { items: { type: 'string' } }),
      fingers: bool(false, 'Fingers', 'Add finger bones (if the fingers are separable).'),
      sockets: bool(true, 'Sockets', 'socket_mount_hand_r etc.'),
      shield_mounts: bool(true, 'Shield sockets', 'socket_mount_shield_l/r - fitting of the canonical attach_prop shield in the rest pose.'),
      grip_dirs: obj(null, 'Grip directions', '{"l"|"r": [x, y, z] | up | down | forward | back | out | in} - the +Z of socket socket_mount_hand_<side> strictly along this direction (rig frame: the character faces -Y, out is away from the body). A vertical staff: {"l": "up"}; a prop attached later by aligning socket_grip_main points where it should.'),
      keep_input_sockets: bool(true, 'Input sockets', 'Input sockets (socket_fx_eye_l/r from replace_part, socket_mount_hand_* of the hand from place_parts) move into the rig frame and are kept: a name that the rig builds itself takes the input\'s frame and extras, the rest become children of the bone with the largest weight at the nearest vertices.'),
      socket_bones: obj(null, 'Input socket bones', '{socket name: bone} - instead of the bone by weights (for example {"socket_fx_eye_l": "Head"}).'),
      replace_sockets: list([], 'Rebuild sockets', 'Names of input sockets that the rig builds anew instead of taking from the input.', { items: { type: 'string' } }),
      island_max_frac: num(0.01, 0, 1, 'Small islands up to fraction', 'Pieces with a smaller fraction of vertices (bone_kit finger bones, clasps, eyes) do not go into the heat map and attach rigidly to the nearest bone (pieces closer than 1 cm form one group).'),
      surface_aware: bool(true, 'Surface weight cleanup', 'Trimming the arm tube and splitting arm/leg use geodesic distances: a sleeve cut off from the robe keeps the arm weights, the side of the robe by a hidden arm loses them.'),
      surface_margin_h: num(0.05, 0, 1, 'Surface margin, H', 'Fraction of the height for the surface weight cleanup.'),
      arm_bleed: list([0.1, 0.35], 'Arm leak suppression', '[low, high]: the residual arm weight a on torso vertices x smoothstep(low, high, a), for hidden arms only; empty - off.', { nullable: true, items: { type: 'number' }, minItems: 2, maxItems: 2, alt: [{ type: 'boolean', enum: [false] }] }),
      deform_test: bool(true, 'Pose check', 'Arms forward and up, squat, step: tears and stretches.'),
      deform_gate: oneOf(['error', 'warning'], 'error', 'Pose check threshold', 'error - tears in abstract poses stop the step; warning - report only (a batch in --qa clips mode: the check of the real clips in motion_style decides).'),
      intersections: bool(true, 'Self-intersections', 'Look for new self-intersections in the pose check.'),
      min_confidence: num(0.35, 0, 1, 'Min. confidence', 'Below - the rig is not built.'),
      fix_yaw: bool(true, 'Extra turn about Z', 'Turn an input that is not axis-aligned to face -Y.'),
      max_yaw_deg: num(5, 0, 45, 'Rotation tolerance, deg', 'Residual rotation above which the input is turned.'),
      front_prior: oneOf(['-Y', '+Y', '-X', '+X', null], null, 'Default facing', '-Y for generations from a front concept: a detector answer of "back" or an unsure one yields to it.'),
      arm_clamp_h: num(null, 0.3, 0.6, 'Arm length limit, H', 'An arm chain that is longer (went into a cloak, hem or merged prop) - the wrist is placed at the typical arm length along it, confidence 0.3; empty - off (a batch after extract_props: 0.47).', opt),
      use_placement_grips: bool(true, 'Grip by prop sockets', 'socket_prop_* sockets from extract_props: the arm is solved on the grip of the cut-out prop (a fist without a sword is not traced to a book on the belt); the shield only if the landmarks without it did not agree.'),
      hard_surface: oneOf(['auto', 'off', 'on'], 'auto', 'Rigid plate armor', 'auto - for the armored/heavy build and for metal painting: each plate (gray metal in the color map) goes entirely to its bone (helmet - Head, pauldron - shoulder, cuirass - Spine2, bracers - forearm, greaves - shin), smooth weights are cloth, skin and joints.'),
      build: oneOf(['heavy', 'armored', 'robed', 'light', 'skeleton', 'zombie', null], null, 'Build', 'From roles.json; armored/heavy enable rigid plate armor with auto.'),
    },
  },
  {
    step: 'prepare_mixamo', module: 'humanoid', title: 'Mixamo preparation',
    help: 'An FBX for manual upload to the Mixamo site (the Forge does not open the site itself): one welded mesh, facing -Y, feet at 0, centimeters.',
    kinds: ['humanoid'], inputs: { slots: ['mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['fbx', 'fbx_prerigged', 'preview'],
    params: {
      height_m: num(1.8, 0.3, 10, 'Height, m', ''),
      height_ref: oneOf(['head', 'bbox'], 'head', 'Height by', ''),
      front: oneOf(FACING, 'auto', 'Facing', 'Face direction in the input; auto - detect.'),
      lite: bool(false, 'Lightweight', 'Up to 12,000 triangles and a 256 px texture (Mixamo does not accept large uploads).'),
      tris: int(null, 1000, 200000, 'Triangles', 'Empty - 18,000 (lightweight - 12,000).', opt),
      tex: int(null, 64, 4096, 'Texture, px', 'Empty - 1024 (lightweight - 256).', opt),
      weld: num(0.0001, 0, 0.01, 'Weld, m', 'Weld along UV seams; 0 - off.'),
      center: oneOf(['trunk', 'bbox', 'head'], 'trunk', 'Symmetry axis', 'Where x = 0 (the Mixamo marker line).'),
      prerigged: bool(false, 'With a ready rig', 'Also mixamo_prerigged.fbx with a mixamorig skeleton and the rig_humanoid weights.'),
      rig_params: obj(null, 'Rig parameters', 'rig_humanoid parameters for the ready rig.'),
      force_prerigged: bool(false, 'Rig without checks', 'Write the ready rig even if it failed the checks.'),
    },
  },
  {
    step: 'import_mixamo_rig', module: 'humanoid', title: 'Mixamo rig import',
    help: 'Accept an FBX downloaded from Mixamo ("With Skin"), restore the scale, sockets and optionally the original textures.',
    kinds: ['humanoid'], inputs: { slots: ['mesh'], required: true, formats: ['fbx'] },
    outputs: ['glb', 'blend', 'preview'],
    params: {
      height_m: num(1.8, 0.3, 10, 'Height, m', 'Height after normalization.'),
      texture_source: str(null, 'Texture source', 'Path to a GLB with the original materials (CLI --direct only).', { path: true }),
      retex: oneOf(['auto', 'material', 'image'], 'auto', 'Texture replacement', 'material - the whole material, image - images only.'),
      keep_animation: bool(true, 'Keep clip', 'The clip from the FBX as a glTF animation.'),
      clip: str(null, 'Clip name', 'Empty - the file name.'),
      loop: oneOf(['auto', true, false], 'auto', 'Loop', 'auto - by name (idle/walk/run...); loops get the suffix -loop.'),
      sockets: bool(true, 'Sockets', 'Hand sockets etc.'),
      max_influences: int(4, 1, 8, 'Influences per vertex', ''),
    },
  },
  // ---------------------------------------------------------------- mixamo / clips
  {
    step: 'retarget', module: 'mixamo', title: 'Clip retargeting',
    help: 'Library clips onto the character skeleton: rest correction, leg IK, loop seams.',
    kinds: ['humanoid'], inputs: { slots: ['blend', 'mesh'], required: true, formats: MESH_FORMATS },
    outputs: ['glb', 'blend', 'clips', 'preview'], timeoutSec: 30 * 60,
    params: {
      clips: list([], 'Clips', 'A list of {source, name, trim: [first, last], loop, in_place, locomotion, fps, speed, mirror, facing, facing_bone, hips_drop, hand_floor, ground}; source = library:<id>, category:<category>[:<tag>] or a clip id (an FBX path - CLI --direct only). in_place: true - remove the straight travel start->end (the sway stays), "strict" - remove the whole hip travel in XY, false - keep; without the key root_motion decides. locomotion - treat the clip as locomotion (default walk/run). facing, facing_bone, hips_drop, hand_floor, ground on a clip take precedence over the global ones.', { items: { type: 'any' } }),
      library: str(null, 'Library', 'Clip library folder; filled in by the server.', { auto: 'animations', path: true }),
      root_motion: oneOf(['lock', 'keep', 'extract'], 'lock', 'Root motion', 'lock - only walk/run play in place (the game moves the character at ground_speed_mps), other clips keep the hip travel so the feet do not slide; keep - everything as in the clip; extract - horizontal travel onto a non-deforming root bone (root motion in Godot).'),
      leg_ik: bool(true, 'Leg IK', 'Feet along the source trajectory.'),
      foot_pin: oneOf(['ball', 'ankle'], 'ball', 'Pin', 'ball - the ball of the foot at contact, ankle - ankle only.'),
      loop_blend: int(8, 0, 60, 'Loop seam, frames', 'Frames of the smooth loop closure.'),
      facing: oneOf(['keep', 'rest'], 'keep', 'Torso turn', 'rest - remove the mean rotation about the vertical at facing_bone (an archer\'s side stance of ~45 deg looks like the rest pose); keep - as in the clip. Can also be set per clip.'),
      facing_bone: str(null, 'Turn bone', 'The bone that measures the turn for facing rest (for example Spine2); empty - Hips. Can also be set per clip.'),
      hips_drop: num(1, 0, 1, 'Pelvis drop', 'Fraction of the pelvis drop below rest that is kept: 0 - the pelvis no lower than rest, the legs are straightened by IK (a robe hem on the floor in a crouching idle); death needs 1. Can also be set per clip.'),
      hips_height_m: num(null, 0.05, 10, 'Target pelvis height, m', 'Explicit height of the hip joints for root scale; empty - measured (under a robe it is a guess).', opt),
      floor: bool(true, 'Floor for leg IK', 'The ankle and ball-of-foot IK targets are no lower than their height in the target\'s rest pose.'),
      hand_floor: obj(null, 'Floor for hands', '{"l"|"r": dz} m - arm IK keeps the wrist no lower than rest - dz, the hand keeps its world orientation (a staff does not go into the floor). Can also be set per clip.'),
      ground: oneOf(['feet', 'mesh'], 'feet', 'Ground', 'feet - leg IK floor only; mesh - a frame whose lowest body point is below z = 0 lifts the whole body (a robe lying in death lies on the floor). Can also be set per clip.'),
      ground_tolerance_m: num(0, 0, 1, 'Ground tolerance, m', 'ground mesh: lift only if the body is below z = 0 by more than this.'),
      ground_samples: int(2000, 10, 200000, 'Vertices for ground', 'ground mesh: random sample of body vertices (linear skinning).'),
      floor_tolerance_m: num(0.02, 0, 1, 'Floor tolerance in the report, m', 'clips.json floor: floor_body_* / floor_props_* warnings if the body or props are below the floor by more than this.'),
      floor_samples: int(16, 1, 1000, 'Frames for the floor report', 'clips.json floor: how many frames of a clip are checked.'),
      apply_transforms: bool(false, 'Apply rig transform', 'Bake the rotation/scale of the rig object and its meshes (otherwise only a warning).'),
      keep_existing_actions: bool(false, 'Keep old clips', 'Otherwise this rig\'s actions are removed: the GLB has exactly the requested clips.'),
      preview: bool(true, 'Preview', 'One row of frames per clip.'),
      preview_cols: int(5, 1, 12, 'Frames per row', ''),
      preview_tile: int(256, 64, 1024, 'Frame size, px', ''),
    },
  },
  {
    step: 'motion_style', module: 'mixamo', title: 'Motion style',
    help: 'Each character gets its own walk and death: tempo, stride, arm swing, lean, slouch, bob, limp, swagger, stiffness (from a seed or explicit) on top of the retarget clips; feet stay planted, loops have no seam, the body does not sink into the floor. Procedural deaths: fall back/forward/left/right, to the knees, with a turn, slump, crumble (undead).',
    kinds: ['humanoid'], inputs: { slots: ['blend'], required: true, formats: ['blend'] },
    outputs: ['glb', 'blend', 'clips', 'preview'], timeoutSec: 30 * 60,
    params: {
      seed: { type: 'any', default: null, nullable: true, title: 'Seed', help: 'A number or a string (concept file name): knobs not set explicitly are taken from it deterministically; empty - a neutral style.', alt: [{ type: 'string' }, { type: 'number' }] },
      build: oneOf(['heavy', 'armored', 'robed', 'light', 'skeleton', 'zombie', null], null, 'Build', 'Seed shift by body: heavy - slower and stiffer, light - faster, skeleton - stiff and slouched, zombie - limps.'),
      tempo: num(null, 0.8, 1.25, 'Tempo', 'Clip speed; for walking the ground speed grows too.', opt),
      stride: num(null, 0.7, 1.35, 'Stride length', 'Leg swing in walking; planted feet do not slide.', opt),
      arm_swing: num(null, 0.2, 2, 'Arm swing', 'Amplitude of the arm swing around the clip\'s mean position.', opt),
      lean: num(null, -1, 1, 'Torso lean', '+1 - 12 deg forward, -1 - 12 deg back.', opt),
      hunch: num(null, 0, 1, 'Slouch', 'Round back, shoulders forward, neck forward.', opt),
      head_bob: num(null, 0.3, 2, 'Vertical bob', 'Vertical bob of the pelvis and head.', opt),
      sway: num(null, 0.2, 2.5, 'Side sway', 'Side sway of the pelvis.', opt),
      limp: num(null, 0, 1, 'Limp', 'Sagging and tilt toward the bad leg, the leg drags; the gait has no sliding.', opt),
      limp_side: oneOf(['l', 'r', null], null, 'Bad leg', 'l or r; empty - from the seed.'),
      swagger: num(null, 0, 1, 'Swagger', 'Torso back, chest forward, elbows out, more pelvis turn.', opt),
      stiffness: num(null, 0.5, 1.8, 'Stiffness', '>1 damps, <1 amplifies the motion of the torso, head and arms.', opt),
      clips: obj(null, 'Per clip', '{clip | category: {knob: value} | false} - a clip has its own values (without the category weights); false - leave the clip alone.'),
      apply: list(null, 'Categories', 'Which clips get the style: idle, walk, run, attack, cast, hit, other; empty - all.', { items: { type: 'string', enum: ['idle', 'walk', 'run', 'attack', 'cast', 'hit', 'other'] } }),
      death: oneOf(['library', 'auto', 'fall_back', 'fall_forward', 'fall_left', 'fall_right', 'kneel_collapse', 'spin_fall', 'slump_sit', 'crumble'], 'library', 'Death', 'library - the death from the library as is; auto - a variant from the seed (crumble only for undead); or an explicit variant.'),
      death_blend: num(1, 0, 1, 'Procedural death fraction', '1 - replaces the library one, less - a mix (the body is then put on the floor again).'),
      undead: bool(false, 'Undead', 'Allow crumble in death auto.'),
      hold_props: bool(true, 'Hold props', 'A staff, banner, spear, bow stay vertical in idle/walk/hit; prop ends do not go into the floor.'),
      floor: bool(true, 'Floor for feet', 'Leg IK targets no lower than the ankle height at rest.'),
      floor_tolerance_m: num(0.02, 0, 1, 'Floor tolerance, m', 'floor_body_* and death_on_floor_* checks.'),
      floor_samples: int(16, 1, 1000, 'Frames for the floor report', ''),
      ground_samples: int(2000, 100, 200000, 'Vertices for floor', 'Sample of body vertices for placing the death on the floor.'),
      preview: bool(true, 'Preview', 'One row of frames per clip.'),
      preview_cols: int(6, 1, 12, 'Frames per row', ''),
      preview_tile: int(220, 64, 1024, 'Frame size, px', ''),
      robe_settle: bool(true, 'Robe settles after death', 'The af_death_settle corrective shape lowers the robe/skirt cloth onto the legs and floor at the end of a procedural death (without a rigid "bell"); driven by a Hips bone property, in the GLB - a morph weight animation.'),
      deform_qa: bool(true, 'Tear check on clips', 'Fraction of torn area (triangles grew > 3x) on every N-th frame of every final clip, pose check thresholds: 1 %, 8 % with a raised arm, 4 % robe.'),
      deform_every: int(2, 1, 30, 'Every N-th frame', 'Frame step of the tear check on clips.'),
      deform_gate: oneOf(['error', 'warning'], 'error', 'Clip check threshold', 'error - a character whose real clips tear does not pass the step (with numbers); warning - report only.'),
      deform_limits: obj(null, 'Tear thresholds', '{main, arms_up, robe} - fractions of the area instead of 0.01 / 0.08 / 0.04.'),
    },
  },
  {
    step: 'cloth_sim', module: 'mixamo', title: 'Cloth (garment simulation)',
    help: 'Robes, cloaks, tabards, hoods and banner panels are separated from the body and computed with cloth physics in every clip (cache for sprites), for the GLB - cloth bones. Details: docs/forge/cloth_sim.md.',
    kinds: ['humanoid'], inputs: { slots: ['blend'], required: true, formats: ['blend'] },
    outputs: ['glb', 'blend', 'clips', 'report', 'preview', 'segmentation'], timeoutSec: 30 * 60,
    params: {
      cloth: { type: 'any', default: 'auto', title: 'Cloth', help: "'auto' - find it yourself; false - no cloth; or a list of boxes {name, min, max}." },
      build: str(null, 'Build', 'robed, armored, heavy, light, skeleton, zombie - a segmentation hint.'),
      presets: obj(null, 'Cloth presets', '{robe|cape|tabard|banner: wool_robe|heavy_cape|tabard|light_silk}.'),
      clips: { type: 'any', default: 'all', title: 'Clips', help: "'all' or a list of clip names." },
      qa_gate: oneOf(['warning', 'error'], 'warning', 'Cloth check', 'warning - report only; error - the step fails on penetration or stretch above the threshold.'),
      cloth_bones: bool(true, 'Cloth bones for GLB', 'Bone chains with baked cloth motion for Godot.'),
      ...preview(512),
    },
  },
  {
    step: 'clip_library', module: 'mixamo', title: 'Clip library',
    help: 'Scanning, import, removal and preview of clips (Mixamo FBX). One library job at a time.',
    kinds: [], scope: 'library', inputs: { slots: [], required: false, formats: [] },
    outputs: ['index', 'scan'],
    params: {
      action: oneOf(['scan', 'import', 'remove', 'preview', 'list'], 'scan', 'Action', ''),
      folder: str(null, 'Folder', 'Folder with FBX files for import or scanning.', { path: true }),
      pattern: str('**/*.fbx', 'Pattern', 'File pattern in the folder.'),
      files: list([], 'Files', 'Absolute FBX paths or {path, name, category, tags, source}.', { items: { type: 'any' }, path: true }),
      library: str(null, 'Library', 'Library folder; filled in by the server.', { auto: 'animations', path: true }),
      ids: list([], 'Clips', 'Clip ids for remove/preview.', { items: { type: 'string' } }),
      preview: bool(true, 'Poses for the preview', 'import: write <id>.poses.json.'),
    },
  },
  // ---------------------------------------------------------------- creatures
  {
    step: 'rig_creature', module: 'creatures', title: 'Creature rig',
    help: 'A template skeleton: quadrupeds, birds, winged, six- and eight-legged, snakes; weights, sockets. The creature is turned head to -Y.',
    kinds: ['creature'], inputs: { slots: ['blend', 'mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'preview', 'rig_json'], timeoutSec: 30 * 60,
    params: {
      template: oneOf(['auto', 'quadruped', 'winged_quadruped', 'bird', 'hexapod', 'octopod', 'serpent', 'custom'], 'auto', 'Pattern', 'auto - detect from the silhouette.'),
      length_m: num(null, 0.05, 200, 'Length, m', 'Scale by body length; empty - keep.', opt),
      height_m: num(null, 0.05, 100, 'Height, m', 'Scale by height (takes precedence over length); empty - keep.', opt),
      head_dir: oneOf(FACING, 'auto', 'Head direction', 'Where the head points in the input: auto - detect, -Y/+Y/-X/+X or a vector [x, y].', { alt: [VEC2] }),
      facing: oneOf(FACING, 'auto', 'Head direction (facing)', 'Same as head_dir; head_dir takes precedence.', { alt: [VEC2] }),
      tail: oneOf(['auto', true, false], 'auto', 'Tail', 'auto, none or a number of bones.', { alt: [{ type: 'integer', min: 0, max: 12 }] }),
      jaw: oneOf(['auto', true, false], 'auto', 'Jaw', 'auto - only with an open mouth.'),
      weights: oneOf(['auto', 'heat', 'distance'], 'auto', 'Weights', 'auto - bone heat, by distance on failure.'),
      custom_chains: list([], 'Custom chains', 'For template=custom: [{name, parent, points: [[x, y, z], ...], bones?, side?}] in the output frame (m, head at -Y).', { items: { type: 'object' } }),
      leg_ratios: obj(null, 'Leg proportions', '{"front": {"mid": 0.5, "paw": 0.16}, "back": {...}} - fractions of the belly height.'),
      voxel_res: int(112, 32, 256, 'Voxels along the length', 'Silhouette analysis resolution.'),
      symmetrize: bool(true, 'Leg symmetry', 'Average mirrored joints of a nearly symmetric mesh.'),
      snap_facing: bool(true, 'Snap axis to 90 deg', 'Snap the body axis to the axes if the deviation is under 20 deg.'),
      spine_segments: int(6, 2, 16, 'Vertebrae (snake)', ''),
      tail_segments: int(6, 1, 16, 'Tail bones (snake)', ''),
      preview: bool(true, 'Preview', ''),
      size: int(384, 64, 2048, 'Preview size, px', ''),
    },
  },
  {
    step: 'anim_creature', module: 'creatures', title: 'Procedural creature clips',
    help: 'Rest, step, run, flight, attack, hit, death - from the rig_creature skeleton template, feet pinned to the ground.',
    kinds: ['creature'], inputs: { slots: ['blend'], required: true, formats: ['blend'] },
    outputs: ['glb', 'blend', 'poses', 'clips_json'],
    params: {
      clips: list(null, 'Clips', 'Names of procedural clips; empty - all that fit the template.', { items: { type: 'string', enum: CREATURE_CLIPS } }),
      fps: int(30, 12, 120, 'Frames per second', ''),
      in_place: bool(true, 'In place', 'false - the root bone moves forward (root motion), the feet stand in the world.'),
      tempo: num(1, 0.25, 4, 'Tempo', 'Tempo multiplier for all clips (2 - twice as fast); cycle_frames and speed take precedence.'),
      speed: num(null, 0.01, 50, 'Speed, m/s', 'Meters per second (not a multiplier) for walk-loop or {clip: m/s}; empty - the step decides from the leg length.', { nullable: true, alt: [PER_CLIP] }),
      stride_m: num(null, 0.01, 50, 'Stride, m', 'Meters per cycle for walk-loop or {clip: m}; empty - from the leg length.', { nullable: true, alt: [PER_CLIP] }),
      cycle_frames: int(null, 8, 480, 'Frames per cycle', 'For walk-loop or {clip: frames}; empty - from the speed.', { nullable: true, alt: [PER_CLIP] }),
      run_gait: oneOf(['trot', 'gallop'], 'trot', 'Run', 'Quadrupeds: trot or gallop.'),
      bird_gait: oneOf(['walk', 'hop'], 'walk', 'Bird gait', ''),
      attack: oneOf(['auto', 'bite', 'claw', 'lunge', 'strike'], 'auto', 'Attack', 'auto - from the template.'),
      death_side: oneOf(['r', 'l'], 'r', 'Falls on its side', ''),
      hover_m: num(null, 0, 100, 'Flight height, m', 'Empty - 0.3 x height.', opt),
      poses: bool(true, 'Pose sheet', 'poses.png: a row per clip.'),
      pose_view: oneOf([...VIEW_NAMES, null], null, 'Pose sheet view', 'Empty - from the template.'),
      tile: int(240, 64, 1024, 'Pose frame size, px', ''),
    },
  },
  // ---------------------------------------------------------------- items
  {
    step: 'item_kit', module: 'items', title: 'Kit item',
    help: 'Procedural weapons, shields, staves, potions, blasters with grip sockets; a variant pack with a contact sheet.',
    kinds: ['item', 'prop'], inputs: { slots: [], required: false, formats: [] },
    outputs: ['glb', 'blend', 'json', 'preview'],
    params: {
      type: oneOf(ITEM_TYPES, 'sword', 'Type', ''),
      style: oneOf(['medieval', 'dark', 'elven', 'scifi', 'random', null], null, 'Style', 'Empty - by type (blasters - scifi, the rest - medieval).'),
      seed: int(0, 0, 2147483647, 'Seed', 'The same seed - the same item.'),
      ornament: int(null, 0, 3, 'Ornament', '0 - plain, 3 - rich; empty - 1 or 2 from the seed.', opt),
      palette: obj(null, 'Palette', '{metal: "#rrggbb", trim: ...} - replace colors by role.'),
      origin: oneOf(['bbox', 'axis'], 'bbox', 'Origin', 'bbox - the bounding box center in X/Y, axis - the main axis at zero.'),
      forward: oneOf(['-Y', '+Y'], '-Y', 'Facing', '-Y - the item frame (blade/muzzle toward -Y); +Y - a prop with its nose toward +Y, the item is rotated by 180 deg.'),
      size: num(null, 0.2, 5, 'Scale (crossbow, blasters)', 'This is the size of the item, not the preview; empty - 1 with a spread from the seed.', opt),
      dims: obj(null, 'Sizes', 'Sizes by type (blade length, handle, etc.), m; the module expands the object into top-level parameters.'),
      budget_tris: int(null, 100, 200000, 'Triangle budget', 'For the check; empty - 2500 (one-handed) or 4000 (two-handed).', opt),
      preview: bool(true, 'Preview', 'preview.png (3 views).'),
      preview_size: int(null, 64, 2048, 'Preview size, px', 'Empty - 384 (in a pack - 320).', opt),
      batch: { type: 'any', default: 1, min: 1, max: 64, title: 'Variants', help: 'A number of variants with neighboring seeds or a list of parameter dictionaries (each on top of the common ones); the glb/blend outputs are lists, the pipeline takes the first.' },
      sheet_cols: int(5, 1, 16, 'Sheet columns', 'Pack: contact sheet columns.'),
      sheet_tile: int(256, 64, 1024, 'Sheet cell, px', 'Pack: cell size.'),
      sheet_scale: oneOf(['common', 'fit', null], null, 'Sheet scale', 'common - lengths are comparable, fit - each fitted to its cell; empty - common for one type, otherwise fit.'),
      sheet_title: str(null, 'Sheet title', 'Empty - ASTRAFORGE ITEM KIT.'),
    },
  },
  {
    step: 'item_from_mesh', module: 'items', title: 'Item from a model',
    help: 'A generated mesh -> a game item: orientation by the main axis, grip point, budget, sockets.',
    kinds: ['item', 'prop'], inputs: { slots: ['mesh'], required: true, formats: [...MESH_FORMATS, 'blend'] },
    outputs: ['glb', 'blend', 'json', 'preview', 'profile'],
    params: {
      kind: oneOf(ITEM_KINDS, 'auto', 'Kind', 'Blade, axe, blunt, polearm, staff, wand, torch, shield; auto - by type or proportions.'),
      type: oneOf([...ITEM_TYPES, null], null, 'Kit type', 'A hint for kind and budget (sword, staff, shield_kite...); empty - by proportions.'),
      length_m: num(null, 0.01, 20, 'Length, m', 'Length of the main axis; empty - by the item kind.', opt),
      budget_tris: int(null, 100, 200000, 'Triangles', 'Mesh budget; empty - 2500 (one-handed) or 4000 (two-handed, staff, shield); a larger value is cut down to the module budget.', opt),
      allow_over_budget: bool(false, 'Allow over budget', 'Do not cut budget_tris down to the module budget (the check becomes a warning).'),
      crop: obj(null, 'Cut out', '{min: [x, y, z], max: [x, y, z]} or {segment: [[x, y, z], [x, y, z]], radius} in input coordinates - cut the item out of a merged generation.'),
      min_island: num(0.005, 0, 0.5, 'Min. island fraction', 'Pieces smaller than this fraction of vertices are removed.'),
      grip: { type: 'any', default: 'auto', title: 'Grip point', help: "'auto', {point: [x, y, z]} in input coordinates or {t: 0.55} - a fraction of the length from the handle end." },
      tip_end: oneOf(['auto', 'min', 'max'], 'auto', 'Working end', 'Which end of the main axis is the tip/pommel.'),
      edge: oneOf(['auto'], 'auto', 'Blade/face', "'auto' or a vector [x, y, z] in input coordinates.", { alt: [VEC3] }),
      texture_max: int(1024, 128, 8192, 'Textures up to, px', 'Large textures are reduced.'),
      origin: oneOf(['bbox', 'axis'], 'bbox', 'Origin', ''),
      sharp_deg: num(50, 0, 180, 'Sharp edges, deg', ''),
      style: oneOf(['medieval', 'dark', 'elven', 'scifi'], 'medieval', 'Default material', 'If the mesh has no material.'),
      preview: bool(true, 'Preview', ''),
      size: int(384, 64, 2048, 'Preview size, px', ''),
    },
  },
  {
    step: 'attach_prop', module: 'items', title: 'Item in hand',
    help: 'Align the item\'s socket_grip_main with the hand socket (shields - with the fitted socket_mount_shield_*), check intersections in the clips.',
    kinds: ['humanoid', 'creature'], inputs: { slots: ['character', 'item'], required: true, formats: ['glb', 'blend', 'fbx'] },
    outputs: ['glb', 'blend', 'json', 'preview', 'grip_closeup'],
    params: {
      hand: oneOf(['r', 'l'], 'r', 'Hand', ''),
      mode: oneOf(['bone_child', 'skin_rigid'], 'bone_child', 'Mode', 'A child object of the socket (swappable) or a rigid skin on the hand (attachments also have skin_separate - a separate mesh skinned 100 % to the hand).'),
      extracted: bool(false, 'extract_props items', 'Place all items cut out by extract_props in this run in their places (socket_prop_* sockets: weapon - move of the hand socket, shield - the shield socket).', { server: true }),
      extracted_mode: str('bone_child', 'Mode for extracted items', 'bone_child | skin_separate | skin_rigid; skin_separate - a separate rigid mesh skinned 100 % to the hand, visible to motion_style (a staff vertical in the walk, the item does not go into the floor, lies down in death).'),
      extracted_shield_fit: str('auto', 'Shield from extract_props', 'auto | placement | clips | rest. placement - the shield where it was in the concept (in clips the hand turns it edge-on and presses it into the arm); clips - the canonical shield socket fitted over the frames of the character\'s clips (the shield stands, faces outward and forward, does not cut the body). auto - clips if the character already has clips (attach_prop after retarget), otherwise placement.'),
      shield_front_bias: num(1, 0, 3, 'Shield facing forward', 'Shield fit over clips: how far "outward" it leans toward the front of the character (0 - strictly away from the chest: with a lowered arm the shield is seen edge-on from the front; 1 - diagonally forward-outward).'),
      offset: obj(null, 'Offset', '{loc: [x, y, z] m, rot_deg: [x, y, z]} in the hand socket frame.'),
      fit: oneOf(['auto', 'frame', 'rest', 'clips'], 'auto', 'Fit', 'auto - shields are fitted (shield socket), the rest by the frame.'),
      standoff: num(null, 0, 0.3, 'Shield standoff, m', 'Empty - auto (from the open hand, up to 12 cm).', { nullable: true, alt: [AUTO] }),
      item_scale: num(1, 0.05, 20, 'Item scale', "A number or 'auto' (character height / 1.8, if it differs by more than 20 %).", { alt: [AUTO] }),
      kind: str(null, 'Item kind', 'Empty - from the item\'s extras (af_item_type).'),
      replace: bool(false, 'Replace previous', 'Remove the items that a previous attach_prop placed in the same hand.'),
      refit_shield_mount: bool(false, 'Refit the shield socket', 'Even if the rig already has socket_mount_shield_*.'),
      refit_socket: bool(false, 'Bake offset into the socket', 'socket_mount_hand_<hand> is moved onto the item\'s socket_grip_main (in the rest pose, stays a child of the hand), the item hangs under it with no offset - a replacement by aligning sockets will repeat this grip. Weapons and props only; can also be set on an item in attachments.'),
      grip_dir: str(null, 'Grip direction', 'up | down | forward | back | out | in or [x, y, z]: the +Z of the hand socket that the step has to create (the rig has none); character frame (faces -Y, out is away from the body). Can also be set on an item in attachments.', { alt: [VEC3] }),
      thumb_hint: list(null, 'Thumb', 'World vector [x, y, z] of the thumb side; empty - the character\'s forward.', { items: { type: 'number' }, minItems: 3, maxItems: 3 }),
      socket_along: num(0.42, 0, 1, 'Socket along the hand', 'Fraction of the hand length for the new hand socket.'),
      clips_check: bool(true, 'Check clips', 'Look for intersections of the item with the body in the clips.'),
      frames_per_clip: int(12, 1, 120, 'Frames per clip', 'For the clip check.'),
      preview_clip: str(null, 'Preview clip', 'Empty - the first.'),
      attachments: list([], 'Several items', 'A list of {asset, hand, mode, offset, fit, kind, item_scale, standoff, refit_socket, grip_dir}; asset is the UUID of an item asset (the server substitutes its latest GLB), item is a path to a GLB (CLI --direct only). Takes precedence over prop_asset and hand.', { items: { type: 'object' } }),
      clips: list([], 'Clips for the check', 'library:<id> | category:<category>[:<tag>] | {source, name, loop}; the server passes the files to the step in inputs.clips (a character after retarget already has clips).', { items: { type: 'any' }, server: true }),
      prop_asset: str(null, 'Item asset', 'UUID from the passport\'s props; empty - the first.', { server: true }),
    },
  },
  {
    step: 'bone_kit', module: 'items', title: 'Bone kit',
    help: 'Undead parts without generation: a skeleton hand (27 bones, Mixamo finger bones, poses, rings), a bone staff with a crystal and claws, a bone clasp. A variant pack with a contact sheet.',
    kinds: ['item', 'prop'], inputs: { slots: [], required: false, formats: [] },
    outputs: ['glb', 'blend', 'json', 'preview', 'contact_sheet', 'batch_json', 'variants_dir'], timeoutSec: 30 * 60,
    params: {
      part: oneOf(['skeleton_hand', 'bone_staff', 'bone_toggle'], 'skeleton_hand', 'Part', 'Skeleton hand, bone staff or bone clasp (required).'),
      style: oneOf(['clean', 'aged'], 'clean', 'Style', 'aged - vertex colors of old bone.'),
      seed: int(0, 0, 2147483647, 'Seed', 'The same seed - the same part.'),
      length_m: num(null, 0.02, 4, 'Length, m', 'Empty - by part: hand 0.19 (wrist crease -> middle fingertip; 0.02-2), staff 2.05 (0.6-4), clasp 0.10 (0.02-0.5).', opt),
      origin: oneOf(['wrist', 'ground', 'bbox', 'axis', null], null, 'Origin', 'Hand: wrist (default) or ground; staff: bbox (default) or axis.'),
      palette: obj(null, 'Palette', '{bone, gold, crystal, crystal_glow: "#rrggbb", crystal_emission: number}.'),
      budget_tris: int(null, 50, 200000, 'Triangle budget', 'Empty - by part: hand 3000, staff 4500, clasp 600.', opt),
      side: oneOf(['r', 'l'], 'r', 'Hand (skeleton hand)', 'skeleton_hand: right or left.'),
      pose: oneOf(['open', 'relaxed', 'claw', 'fist', 'grip'], 'open', 'Pose (hand)', 'grip wraps a cylinder of grip_radius_m.'),
      finger_spread_deg: num(null, -30, 60, 'Finger spread, deg', 'skeleton_hand; empty - from the pose.', opt),
      curl: { type: 'any', default: null, nullable: true, title: 'Finger curl (hand)', help: '0..1: a number, a list of 5 (thumb..pinky) or {finger: c}; empty - from the pose.' },
      grip_radius_m: num(0.018, 0.003, 0.1, 'Grip radius, m', 'skeleton_hand: the socket_mount_hand_* socket on the axis of this cylinder in all poses.'),
      grip_tilt_deg: num(0, -45, 45, 'Grip tilt, deg', 'skeleton_hand: the grip axis relative to the knuckle line.'),
      rings: list([], 'Rings (hand)', 'Fingers with a gold ring: thumb, index, middle, ring, pinky.', { items: { type: 'string', enum: ['thumb', 'index', 'middle', 'ring', 'pinky'] } }),
      ring_at: num(0.66, 0, 1, 'Ring position', 'Fraction of the proximal phalanx.'),
      grip_fraction: num(0.62, 0.05, 0.95, 'Grip (staff)', 'bone_staff: socket_grip_main at this fraction of the length from the bottom.'),
      grip_off_fraction: num(0.45, 0.05, 0.95, 'Second grip (staff)', 'bone_staff: socket_grip_off.'),
      segments: oneOf([3, 4], 3, 'Shaft bones (staff)', ''),
      shaft_radius_m: num(0.018, 0.005, 0.1, 'Shaft radius, m', 'bone_staff: at the grip (= the hand\'s grip radius).'),
      crystal_height_m: num(0.21, 0.18, 0.24, 'Crystal height, m', 'bone_staff.'),
      crystal_radius_m: num(0.038, 0.01, 0.15, 'Crystal radius, m', 'bone_staff.'),
      talons: oneOf([3, 4], 4, 'Head claws', 'bone_staff.'),
      talon_length_m: num(null, 0.02, 0.5, 'Claw length, m', 'bone_staff; empty - 0.82 of the crystal height.', opt),
      bend: num(1, 0, 2, 'Bone bend', 'bone_staff: multiplier.'),
      gold: oneOf(['rings', 'caps', 'none'], 'rings', 'Gold (clasp)', 'bone_toggle: rings on the shaft, sleeves on the knobs, or no gold.'),
      loop: bool(true, 'Loop (clasp)', 'bone_toggle: the gold loop the clasp is sewn on with.'),
      bone_radius_m: num(null, 0.001, 0.05, 'Bone radius, m', 'bone_toggle; empty - 0.056 of the length.', opt),
      batch: { type: 'any', default: 1, min: 1, max: 64, title: 'Variants', help: 'A number of variants with neighboring seeds or a list of parameter dictionaries (each on top of the common ones, parts can be mixed); the glb/blend outputs are lists, the pipeline takes the first.' },
      sheet_cols: int(4, 1, 16, 'Sheet columns', 'Pack: contact sheet columns.'),
      sheet_tile: int(256, 64, 1024, 'Sheet cell, px', 'Pack: cell size.'),
      sheet_title: str(null, 'Sheet title', 'Empty - ASTRAFORGE BONE KIT.'),
      ...preview(384),
    },
  },
  // ---------------------------------------------------------------- ships
  {
    step: 'ship_gen', module: 'ships', title: 'Procedural ship',
    help: 'Hull, wings, engines, turrets, LODs and sockets (nose +Y in Blender).',
    kinds: ['ship'], inputs: { slots: [], required: false, formats: [] },
    outputs: ['glb', 'blend', 'preview', 'manifest', 'definition', 'lod1', 'lod2'],
    params: {
      class: oneOf(SHIP_CLASSES, 'fighter', 'Class', ''),
      seed: int(1, 0, 2147483647, 'Seed', 'Neighboring seeds give different layout families.'),
      layout: str('auto', 'Layout', `auto, a family name or a number. ${SHIP_LAYOUTS}.`),
      length_m: num(null, 2, 2000, 'Length, m', 'With nozzles; empty - by class. Outside the class range the budget is taken by length.', opt),
      style: oneOf(SHIP_STYLES, 'military', 'Faction', 'Silhouette language and palette (the faction step parameter).'),
      turret_count: int(null, 0, 32, 'Turrets', 'Empty - by class (no more than the layout points).', opt),
      engine_count: int(null, 1, 6, 'Engines', 'Empty - from the seed within the class range.', opt),
      wing_style: oneOf(WING_STYLES, 'auto', 'Wings', 'Fighter/interceptor; for gunship only auto | none | stub, not applicable to other classes.'),
      greeble_density: num(0.6, 0, 1, 'Detail', 'Density of small details.'),
      livery: { type: 'any', default: null, nullable: true, title: 'Paint', help: '{armor|graphite|trim|panel|glass|status|drive|marking: "#rrggbb"}.' },
      palette_variant: int(null, 0, 2, 'Palette variant', 'Empty - from the seed.', opt),
      thruster_frame: oneOf(THRUSTER_FRAMES, 'thrust', 'Nozzle frame', 'thrust - glTF -Z along the thrust (forward of the ship), exhaust - along the exhaust.'),
      tier: int(1, 1, 10, 'Tier', 'Into the manifest.'),
      lods: bool(true, 'LOD', 'ship_lod1.glb, ship_lod2.glb.'),
      preview: bool(true, 'Preview', ''),
      preview_turrets: bool(true, 'Turrets in the preview', 'In the image only, not in the GLB.'),
      preview_size: int(300, 120, 800, 'Preview size, px', ''),
      uv: oneOf(['smart', 'box'], 'smart', 'Unwrap', ''),
      asset_id: str(null, 'Asset ID', 'snake_case; filled in by the server from the passport name (as in godot_package).', { auto: 'asset_slug' }),
    },
  },
  {
    step: 'turret_gen', module: 'ships', title: 'Turret',
    help: 'Turret rig: socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_*, short clips, LODs.',
    kinds: ['ship', 'prop'], inputs: { slots: [], required: false, formats: [] },
    outputs: ['glb', 'blend', 'preview', 'manifest', 'definition', 'lod1', 'lod2'],
    params: {
      size: oneOf(['S', 'M', 'L'], 'M', 'Size', 'Ring 1.2 / 2.4 / 4.8 m.'),
      ring_m: num(null, 0.3, 10, 'Ring, m', 'Ring diameter within the class; empty - by size.', opt),
      barrels: int(2, 1, 4, 'Barrels', ''),
      barrel_style: oneOf(['plain', 'rotary'], 'plain', 'Barrels', 'plain - ordinary, rotary - each barrel a six-barrel block (Gatling).'),
      seed: int(1, 0, 2147483647, 'Seed', ''),
      style: oneOf(SHIP_STYLES, 'military', 'Faction', 'The faction step parameter.'),
      clips: bool(true, 'Clips', 'yaw_sweep-loop, pitch_cycle-loop, fire_recoil.'),
      lods: bool(true, 'LOD', 'turret_lod1.glb, turret_lod2.glb.'),
      livery: { type: 'any', default: null, nullable: true, title: 'Paint', help: 'As in ship_gen.' },
      palette_variant: int(null, 0, 2, 'Palette variant', 'Empty - from the seed.', opt),
      tier: int(1, 1, 10, 'Tier', ''),
      preview: bool(true, 'Preview', ''),
      preview_size: int(300, 120, 800, 'Preview size, px', ''),
      asset_id: str(null, 'Asset ID', 'snake_case; filled in by the server from the passport name.', { auto: 'asset_slug' }),
    },
  },
  {
    step: 'ship_from_mesh', module: 'ships', title: 'Ship from a model',
    help: 'A generated mesh -> a ship: welding, nose +Y, class budget, AF_* materials, sockets with confidence, LODs (heuristics, not a rebuild).',
    kinds: ['ship'], inputs: { slots: ['mesh'], required: true, formats: MESH_FORMATS },
    outputs: ['glb', 'blend', 'preview', 'lod_strip', 'manifest', 'definition', 'lod1', 'lod2'],
    params: {
      class: oneOf([...SHIP_CLASSES, 'ship_s', 'ship_m', 'ship_l'], 'ship_m', 'Class', 'Role (fighter, corvette...) or the budget class ship_s / ship_m / ship_l.'),
      length_m: num(null, 2, 2000, 'Length, m', 'Empty - by class.', opt),
      nose: oneOf(['auto', '+x', '-x', '+y', '-y', '+X', '-X', '+Y', '-Y'], 'auto', 'Nose', 'Nose axis in the input mesh (the Blender frame after import).'),
      up: oneOf(['auto', '+z', '-z', '+x', '-x', '+y', '-y'], 'auto', 'Up', 'Up axis in the input; auto - +Z.'),
      materials: oneOf(['classify', 'keep_texture', 'single', 'paint'], 'classify', 'Materials', 'classify - AF_* materials by color, keep_texture - the original texture, single - one material, paint - concept-based marking (needs paint; the default with paint).'),
      emissive: bool(true, 'Emission', 'Separate the glowing areas.'),
      palette: oneOf(['input', 'faction'], 'input', 'Palette', 'input - colors of the input or concept (engine and light glow too), faction - the faction palette.'),
      livery: { type: 'any', default: null, nullable: true, title: 'Paint', help: '{armor|graphite|trim|panel|glass|status|drive|marking: "#rrggbb" | [r, g, b] | {rgb, metal, rough, strength}} - on top of the palette.' },
      paint: obj(null, 'Concept marking', '{image, mask?, camera: "fit" | {location, rotation_matrix, lens_mm, resolution} | a json path, mirror: x | none, regions: [{box: [u0, v0, u1, v1] (fractions of the image), label, only?}], thresholds, markings, glow_gamma, smooth, smooth_lod0, min_dark_island}. image is a concept with alpha (the prepare_concept reference image); a path - CLI --direct only, over HTTP - asset: the UUID of a studio image.'),
      armor_texture: { type: 'any', default: null, nullable: true, title: 'Armor texture', help: 'A kit name (scripts/blender/data/kits/kits.json; no kits are shipped) or {texture, tile_m, normal_strength, regions: [{texture, name, box: {min, max}}]} - seamless armor with a box unwrap in meters.' },
      decals: list([], 'Inscriptions', 'A list of {text, side: port | starboard | both | top, position: [y, z] | [x, y], size: [w, h] m, font (DejaVuSans-Bold), bold, align, color, wear, line_scales, slashed_zero}: text into an RGBA atlas, a decal mesh over the hull, AF_Marking (BLEND).', { items: { type: 'object' } }),
      turret_count: int(null, 0, 32, 'Turrets', 'Empty - by class.', opt),
      turret_sockets: oneOf(['flats', 'protrusions', 'auto'], 'flats', 'Turret sockets', 'flats - flat pads, protrusions - at the base of the input\'s barrel protrusions (axis outward), auto - protrusions if there are at least 2.'),
      sockets: bool(true, 'Sockets', ''),
      target_tris: int(null, 200, 500000, 'LOD0 triangles', 'Empty - 90 % of the class budget (no more than 97 %).', opt),
      lod_targets: obj(null, 'LOD targets', '{lod1: tris, lod2: tris} - ceilings below the class budget (room for kitbash).'),
      thruster_frame: oneOf(THRUSTER_FRAMES, 'thrust', 'Nozzle frame', 'As in ship_gen.'),
      thruster_cue: oneOf(['auto', 'glow', 'geometry'], 'auto', 'Nozzle search', 'auto - by glow (with paint - by the geometry of the cut, glow only confirms).'),
      source: oneOf([...SOURCES, null], null, 'Source', 'Empty - from the file path, otherwise external.'),
      generator: str(null, 'Generator', 'Empty - from the file path (meshy, tripo, hunyuan3d...).'),
      faction: str(null, 'Faction', 'Empty - neutral.'),
      tier: int(1, 1, 10, 'Tier', ''),
      uv: oneOf(['keep', 'smart', 'box'], 'keep', 'Unwrap', ''),
      lods: bool(true, 'LOD', ''),
      preview: bool(true, 'Preview', ''),
      preview_size: int(300, 120, 800, 'Preview size, px', ''),
      preview_sockets: bool(true, 'Sockets in the preview', ''),
      preview_lods: bool(true, 'LOD strip', 'lod_strip.png - LOD0 | LOD1 | LOD2 with one camera.'),
      asset_id: str(null, 'Asset ID', 'snake_case; filled in by the server from the passport name.', { auto: 'asset_slug' }),
    },
  },
  {
    step: 'kitbash', module: 'ships', title: 'Parts on a model (kitbash)',
    help: 'Procedural parts on a finished (AI) model at sockets or points: six-barrel Gatlings with a shroud, ribbed nozzles with a glowing throat and drives, piping, antennas and masts, lights, grilles, blocks; the old AI geometry in the part\'s zone is cut out. Parts join the objects by material (the surface count does not grow), are built for LOD0/1/2, muzzle sockets are added.',
    kinds: ['ship', 'prop'], inputs: { slots: ['blend', 'mesh'], required: true, formats: ['glb', 'gltf', 'blend'] },
    outputs: ['glb', 'blend', 'preview', 'lod_strip', 'manifest', 'lod1', 'lod2'],
    params: {
      parts: list([], 'Parts', 'A list of {kind: gatling | nozzle | piping | antenna | mast | nav_light | vent_grille | block | remove, at: a socket or mask (socket_turret_*) | [x, y, z] | [[x, y, z], ...] | [x, y] (antennas and lights - attached to the hull), ...}; sizes in meters of the ship frame (nose +Y), remove - a cut zone {cylinder | sphere | box} or {radius}. Full list of fields: docs/forge/kitbash.md.', { items: { type: 'object' } }),
      drop_sockets: list([], 'Remove sockets', 'Names of sockets that are no longer needed (for example socket_utility_antenna).', { items: { type: 'string' } }),
      budget_class: oneOf(['ship_s', 'ship_m', 'ship_l', null], null, 'Budget class', 'Empty - from the input metadata (ship_from_mesh), otherwise ship_m.'),
      lods: bool(true, 'LOD', 'ship_lod1.glb, ship_lod2.glb (simpler details).'),
      preview: bool(true, 'Preview', ''),
      preview_size: int(300, 120, 800, 'Preview size, px', ''),
    },
  },
  // ---------------------------------------------------------------- export
  {
    step: 'render_sprites', module: 'export', title: 'Sprites',
    help: 'Sprite sheets by direction and clip (isometric iso8 or custom directions). Wrong model units are an error before rendering, with a hint (params.height_m).',
    kinds: ALL_KINDS, inputs: { slots: ['blend', 'mesh'], required: true, formats: MESH_FORMATS, manifest: true },
    outputs: ['sheets', 'sprites_json', 'spriteframes', 'preview', 'preview_frames', 'godot_check'],
    gpu: true, timeoutSec: 60 * 60, export: true,
    params: {
      asset_id: str(null, 'ID for files', 'snake_case; filled in by the server.', { auto: 'asset_slug' }),
      profile: oneOf(['iso8', 'custom'], 'iso8', 'Profile', ''),
      forward: oneOf(['-Y', '+Y', null], null, 'Facing', 'Empty - by asset kind: ships, props, turrets +Y, the rest -Y.'),
      height_m: num(null, 0.05, 100, 'Height, m', 'Empty - no scale (a model outside 0.3-15 m is probably in the wrong units).', opt),
      clips: { type: 'any', default: 'all', title: 'Clips', help: "'all' or a list of clip names." },
      clip_names: obj(null, 'Clip names', '{action: clip name}.'),
      loop: obj(null, 'Loops', '{clip: yes/no}; default -loop/idle/walk/run.'),
      frames: { type: 'any', default: null, nullable: true, title: 'Frames per clip', help: 'A number or {clip: number}; empty - by profile.' },
      fps: int(null, 1, 60, 'Frames per second', 'Empty - 12.', opt),
      ranges: obj(null, 'Ranges', '{clip: [start, end]} in scene frames.'),
      in_place: oneOf(['auto', 'lock', 'drift', 'none'], 'auto', 'In place', 'auto - loops without root drift, one-shots as is; lock - hips XY from the first frame.'),
      pixels_per_meter: num(null, 8, 1024, 'Pixels per meter', 'Empty - 100 (the profile\'s single scale).', opt),
      elevation_deg: num(null, 0, 90, 'Camera tilt, deg', 'Empty - 30 deg (isometric 2:1).', opt),
      directions: list(null, 'Directions', 'iso8: a subset of S, SW, W, NW, N, NE, E, SE; custom: [[name, model angle deg], ...].', { items: { type: 'any' } }),
      padding_px: int(6, 0, 256, 'Cell padding, px', ''),
      cell: list(null, 'Cell, px', '[width, height] - a fixed cell (needs anchor).', { items: { type: 'integer' }, minItems: 2, maxItems: 2 }),
      anchor: list(null, 'Anchor point, px', '[x, y] in the cell.', { items: { type: 'number' }, minItems: 2, maxItems: 2 }),
      center: oneOf(['feet', 'none'], 'feet', 'Centering', ''),
      normalize_pose: oneOf(['rest', 'first_frame', null], null, 'Pose for scale', 'Empty - rest.'),
      grade_materials: bool(null, 'Tweak materials', 'Empty - none.'),
      engine: oneOf(['EEVEE', 'CYCLES', 'WORKBENCH', null], null, 'Render engine', 'Empty - EEVEE.'),
      samples: int(null, 1, 4096, 'Samples', 'Empty - by profile.', opt),
      shadow: bool(null, 'Shadow', 'Cycles shadow catcher; empty - none.'),
      grade: bool(null, 'Color correction', 'Empty - none.'),
      cols: int(null, 1, 64, 'Frames per sheet row', 'Empty - by directions.', opt),
      max_sheet_px: int(8192, 512, 16384, 'Sheet up to, px', 'Soft limit of the sheet side.'),
      max_cell_px: int(2048, 64, 8192, 'Cell up to, px', 'More - an error before rendering (wrong units?).'),
      min_body_px: num(24, 1, 1024, 'Model from, px', 'Less - an error before rendering (wrong units?).'),
      validate_godot: bool(null, 'Check in Godot', 'SpriteFrames in Godot 4.5.1 headless; empty - yes if Godot is found.'),
      keep_frames: bool(false, 'Keep frames', 'Keep the individual render frames.'),
    },
  },
  {
    step: 'godot_package', module: 'export', title: 'Godot package',
    help: 'GLB + a .tscn scene + a script + effects at sockets + a passport; checked in Godot 4.5.1 headless.',
    kinds: ALL_KINDS, inputs: { slots: ['glb'], required: true, formats: ['glb'], manifest: true },
    outputs: ['package', 'glb', 'tscn', 'script', 'readme', 'manifest', 'godot_check', 'godot_log', 'fx_preview'], export: true,
    params: {
      asset_id: str(null, 'Asset ID', 'snake_case ASCII; filled in by the server from the passport name.', { auto: 'asset_slug' }),
      profile: oneOf(['godot'], 'godot', 'Profile', ''),
      forward_axis: oneOf(['+Z', '-Z', null], null, 'Forward', '+Z - as in the GLB, -Z - for Godot look_at; empty - as in the GLB.'),
      source_forward: oneOf(['+Z', '-Z', null], null, 'Forward in GLB', 'Empty - by asset kind (ships, props -Z; characters, creatures, items +Z).'),
      kind: str(null, 'Asset kind', 'Needed only if the passport has no kind: ship, prop, turret, station... - forward -Z, other kinds - +Z.'),
      fx: list([], 'Effects', 'A list of {preset, socket, params}; socket may be a mask (* and ?, for example socket_thruster_*): the server expands it over the sockets of the input GLB, a mask with no matches is an error. Empty - the passport\'s fx.', { items: { type: 'object' } }),
      fx_auto_scale: bool(true, 'Effect scale', 'Fit the effects to the socket or asset size (if fx has no scale).'),
      autoplay: str(null, 'Autoplay clip', 'The clip the scene plays at start; empty - none.'),
      validate: bool(true, 'Check in Godot', 'Godot 4.5.1 headless.'),
      fx_preview: bool(false, 'Effect frames', 'Three Godot Movie Maker frames (opens a Godot window for ~3 s).'),
      fx_preview_speed: num(null, 0, 100, 'Preview speed', 'Asset sizes per second along -Z.', opt),
      fx_preview_frames: list(null, 'Preview frames', 'Frame numbers at 30 fps; empty - [14, 30, 46].', { items: { type: 'integer' } }),
    },
  },
];

const STEPS = new Map(STEP_LIST.map((s) => [s.step, s]));

const humanoidClips = [
  { source: 'category:idle', name: 'idle', loop: true },
  { source: 'category:walk', name: 'walk', loop: true, in_place: true },
  { source: 'category:attack', name: 'attack', loop: false },
  { source: 'category:hit', name: 'hit', loop: false },
  { source: 'category:death', name: 'death', loop: false },
];
const engineTrails = [{ preset: 'engine_trail', socket: 'socket_thruster_*', params: {} }];

const PRESET_LIST = [
  {
    id: 'humanoid_from_mesh', kind: 'humanoid', title: 'Humanoid from a model -> Godot',
    help: 'Cleanup to 40,000 triangles and 1.8 m, auto-rig, library clips (rest, step, attack, hit, death), a Godot package. The step plays in place, the travel of standing clips (lunges, deaths) is moved to the root bone - root motion in Godot.',
    steps: [
      { step: 'cleanup', params: { target_tris: 40000, height_m: 1.8 } },
      { step: 'rig_humanoid', params: { height_m: 1.8 } },
      { step: 'retarget', params: { clips: humanoidClips, root_motion: 'extract' } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
  {
    id: 'humanoid_sprites', kind: 'humanoid', title: 'Humanoid -> Godot + iso8 sprites',
    help: 'Like "Humanoid from a model", but the clips are in lock mode (step in place, standing clips with hip travel, no root bone), plus in-place iso8 sprite sheets (in_place: drift - root drift removed from all clips). The sprites are rendered before the Godot package so that it includes them (the sprites/ folder).',
    steps: [
      { step: 'cleanup', params: { target_tris: 40000, height_m: 1.8 } },
      { step: 'rig_humanoid', params: { height_m: 1.8 } },
      { step: 'retarget', params: { clips: humanoidClips, root_motion: 'lock' } },
      // render_sprites before godot_package: the package takes inputs.sprites only from an earlier render
      { step: 'render_sprites', params: { profile: 'iso8', in_place: 'drift' } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
  {
    id: 'creature_auto', kind: 'creature', title: 'Creature: auto-asset',
    help: 'Cleanup to 30,000 triangles, a rig from the auto template, procedural clips (tempo and speed decided by the step), a Godot package.',
    steps: [
      { step: 'cleanup', params: { target_tris: 30000 } },
      { step: 'rig_creature', params: { template: 'auto' } },
      { step: 'anim_creature', params: { clips: ['idle-loop', 'walk-loop', 'run-loop', 'attack', 'hit', 'death'] } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
  {
    id: 'item_kit_sword', kind: 'item', title: 'Sword from the kit',
    help: 'A procedural medieval-style sword and a Godot package.',
    steps: [
      { step: 'item_kit', params: { type: 'sword', style: 'medieval', seed: 1 } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
  {
    id: 'item_from_mesh', kind: 'item', title: 'Item from a model',
    help: 'A generated mesh -> an item with a grip point and a budget by item kind, a Godot package.',
    steps: [
      { step: 'item_from_mesh', params: { grip: 'auto' } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
  {
    id: 'ship_procedural', kind: 'ship', title: 'Procedural fighter -> Godot',
    help: 'ship_gen of class fighter and a Godot package with engine trails at the thruster sockets.',
    steps: [
      { step: 'ship_gen', params: { class: 'fighter', seed: 1 } },
      { step: 'godot_package', params: { profile: 'godot', fx: engineTrails } },
    ],
  },
  {
    id: 'ship_from_mesh', kind: 'ship', title: 'Ship from a model -> Godot',
    help: 'A generated mesh -> a ship with sockets and a Godot package with engine trails.',
    steps: [
      { step: 'ship_from_mesh', params: { class: 'fighter', nose: 'auto' } },
      { step: 'godot_package', params: { profile: 'godot', fx: engineTrails } },
    ],
  },
  {
    id: 'turret', kind: 'ship', title: 'Turret -> Godot',
    help: 'A medium procedural turret with two barrels and a Godot package.',
    steps: [
      { step: 'turret_gen', params: { size: 'M', barrels: 2 } },
      { step: 'godot_package', params: { profile: 'godot' } },
    ],
  },
];

const PRESETS = new Map(PRESET_LIST.map((p) => [p.id, p]));

// Export profiles for POST /assets/:id/export.
export const EXPORT_PROFILES = {
  godot: { step: 'godot_package', params: { profile: 'godot' } },
  iso8: { step: 'render_sprites', params: { profile: 'iso8' } },
};

// Structured params the per-param schema cannot describe: checked before the run, so a typo fails
// in a second instead of after minutes of Blender work. Each hook throws paramError (Russian).
const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isVec = (v, n) => Array.isArray(v) && v.length === n && v.every((x) => typeof x === 'number' && Number.isFinite(x));
const UUID_LIKE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function checkClipList(step, clips, { strictKeys = true } = {}) {
  if (!Array.isArray(clips)) return;
  clips.forEach((c, i) => {
    const where = `Step "${step}", clip #${i + 1}`;
    if (typeof c === 'string') {
      if (!c.trim()) throw paramError(`${where}: empty source.`);
      return;
    }
    if (!isPlainObject(c)) throw paramError(`${where}: expected an object {source, name, ...} or a source string.`);
    // retarget reads only `source`; attach_prop (inputs.clips) also takes {file} / {path} from the CLI
    const src = strictKeys ? c.source : (c.source ?? c.file ?? c.path);
    if (typeof src !== 'string' || !src.trim()) throw paramError(`${where}: no source (library:<id>, category:<category>[:<tag>] or a clip id).`);
    if (c.name !== undefined && c.name !== null && typeof c.name !== 'string') throw paramError(`${where}: name must be a string.`);
    if (c.loop !== undefined && typeof c.loop !== 'boolean') throw paramError(`${where}: loop must be true/false.`);
    if (!strictKeys) return;
    if ('in_place' in c && !(c.in_place === true || c.in_place === false || (typeof c.in_place === 'string' && c.in_place.trim().toLowerCase() === 'strict'))) {
      throw paramError(`${where}: in_place must be true, false or "strict" (omit the key to let root_motion decide).`);
    }
    for (const k of ['locomotion', 'mirror']) if (k in c && typeof c[k] !== 'boolean') throw paramError(`${where}: ${k} must be true/false.`);
    if ('trim' in c && c.trim !== null && !(Array.isArray(c.trim) && c.trim.length === 2 && c.trim.every((x) => typeof x === 'number' && Number.isFinite(x)) && c.trim[1] > c.trim[0])) {
      throw paramError(`${where}: trim must be [first, last] source frame, last greater than first.`);
    }
    for (const k of ['fps', 'speed']) if (k in c && !(typeof c[k] === 'number' && Number.isFinite(c[k]) && c[k] > 0)) throw paramError(`${where}: ${k} must be a positive number.`);
    checkRetargetOptions(c, where);
  });
}

const SIDE_KEYS = ['l', 'r', 'left', 'right'];
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const lower = (v) => (typeof v === 'string' ? v.trim().toLowerCase() : v);

/** {"l"|"r": number} (retarget hand_floor).*/
function checkSideNumbers(v, where, what) {
  if (!isPlainObject(v) || !Object.entries(v).every(([k, x]) => SIDE_KEYS.includes(String(k).toLowerCase()) && isNum(x))) {
    throw paramError(`${where}: ${what} must be {"l"|"r": number}.`);
  }
}

/** retarget options that are both global and per clip (the module reads clip value ?? global value). */
function checkRetargetOptions(o, where) {
  if (o.facing !== undefined && !['keep', 'rest'].includes(lower(o.facing))) throw paramError(`${where}: facing must be keep or rest.`);
  if (o.facing_bone !== undefined && o.facing_bone !== null && !(typeof o.facing_bone === 'string' && o.facing_bone.trim())) throw paramError(`${where}: facing_bone must be a bone name (Hips, Spine2...).`);
  if (o.hips_drop !== undefined && !(isNum(o.hips_drop) && o.hips_drop >= 0 && o.hips_drop <= 1)) throw paramError(`${where}: hips_drop must be a number from 0 to 1.`);
  if (o.ground !== undefined && !['feet', 'mesh'].includes(lower(o.ground))) throw paramError(`${where}: ground must be feet or mesh.`);
  if (o.hand_floor !== undefined && o.hand_floor !== null) checkSideNumbers(o.hand_floor, where, 'hand_floor');
}

const STYLE_KNOBS = ['tempo', 'stride', 'arm_swing', 'lean', 'hunch', 'head_bob', 'sway', 'limp', 'limp_side', 'swagger', 'stiffness'];

const GRIP_DIR_NAMES = ['up', 'down', 'forward', 'back', 'out', 'in'];
const isGripDir = (v) => (typeof v === 'string' && GRIP_DIR_NAMES.includes(v.toLowerCase()))
  || (isVec(v, 3) && v.some((x) => x !== 0));
const GRIP_DIR_HINT = `[x, y, z] (non-zero) or ${GRIP_DIR_NAMES.join(' | ')}`;

const isMatrix3 = (m) => Array.isArray(m) && m.length === 3 && m.every((row) => isVec(row, 3));
const det3 = (m) => m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
  + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);

const PLACE_POINT_KEYS = ['at', 'asset', 'part', 'name', 'scale', 'roll_deg', 'snap', 'rot_deg', 'matrix', 'standoff_m', 'sockets',
  'rest_percentile', 'normal_patch', 'max_tilt_deg'];

/** One points[] entry of place_parts: [x, z] | [x, y, z] | {at, …} (place_parts._norm_point and run). */
function checkPlacePoint(raw, i) {
  const where = `Step "place_parts", point #${i + 1}`;
  const pt = isPlainObject(raw) ? raw : { at: raw };
  if (!isPlainObject(raw) && !Array.isArray(raw)) throw paramError(`${where}: expected [x, z], [x, y, z] or {at, ...}.`);
  const unknown = Object.keys(pt).filter((k) => !PLACE_POINT_KEYS.includes(k));
  if (unknown.length) throw paramError(`${where}: unknown keys ${unknown.join(', ')} (allowed: ${PLACE_POINT_KEYS.join(', ')}).`);
  if (!isPoint(pt.at)) throw paramError(`${where}: at must be [x, z] (front view, m) or [x, y, z].`);
  const snap = pt.snap === undefined ? 'surface' : lower(pt.snap);
  if (!['surface', 'none'].includes(snap)) throw paramError(`${where}: snap must be surface or none.`);
  if (snap === 'none' && pt.at.length !== 3) throw paramError(`${where}: snap none is an explicit pose, a point [x, y, z] is required.`);
  if (pt.scale !== undefined && !(isNum(pt.scale) && pt.scale > 0)) throw paramError(`${where}: scale must be a positive number.`);
  for (const k of ['roll_deg', 'standoff_m']) if (pt[k] !== undefined && !isNum(pt[k])) throw paramError(`${where}: ${k} must be a number.`);
  if (pt.rot_deg !== undefined && pt.rot_deg !== null && !isVec(pt.rot_deg, 3)) throw paramError(`${where}: rot_deg must be [x, y, z] in degrees.`);
  if (pt.matrix !== undefined && pt.matrix !== null && !(isMatrix3(pt.matrix) && det3(pt.matrix) > 0)) {
    throw paramError(`${where}: matrix must be a 3x3 rotation given by rows (determinant > 0).`);
  }
  if (pt.asset !== undefined && !(typeof pt.asset === 'string' && UUID_LIKE.test(pt.asset))) throw paramError(`${where}: asset must be the UUID of an asset with the part.`);
  if (pt.part !== undefined && !(typeof pt.part === 'string' && pt.part.trim())) throw paramError(`${where}: part must be a path to the part file (CLI --direct only).`);
  if (pt.asset !== undefined && pt.part !== undefined) throw paramError(`${where}: specify either asset (the part UUID) or part (a path, CLI only).`);
  if (pt.name !== undefined && pt.name !== null && typeof pt.name !== 'string') throw paramError(`${where}: name must be a string.`);
  if (pt.sockets !== undefined && typeof pt.sockets !== 'boolean') throw paramError(`${where}: sockets must be true/false.`);
  if (pt.rest_percentile !== undefined && !(isNum(pt.rest_percentile) && pt.rest_percentile >= 0 && pt.rest_percentile <= 100)) throw paramError(`${where}: rest_percentile must be from 0 to 100.`);
  if (pt.normal_patch !== undefined && !(isNum(pt.normal_patch) && pt.normal_patch > 0)) throw paramError(`${where}: normal_patch must be a positive number.`);
  if (pt.max_tilt_deg !== undefined && !(isNum(pt.max_tilt_deg) && pt.max_tilt_deg >= 0 && pt.max_tilt_deg <= 90)) throw paramError(`${where}: max_tilt_deg must be from 0 to 90 deg.`);
}

/** Cut surface of a separate slit region: {axis, value} | {point, normal} | curve {axis, along, points} (_mesh_lib.SlitSurface). */
function checkSlitSurface(s, where) {
  if (!isPlainObject(s)) throw paramError(`${where}: plane must be {axis: "x", value: v} or {point: [x, y, z], normal: [x, y, z]}; curve must be {axis: "x", along: "z", points: [[along, value], ...]}.`);
  if ('points' in s) {
    const ax = lower(s.axis ?? 'x');
    const al = lower(s.along ?? 'z');
    if (!['x', 'y', 'z'].includes(ax) || !['x', 'y', 'z'].includes(al) || ax === al) throw paramError(`${where}: curve - axis and along must be two different axes of x, y, z.`);
    if (!Array.isArray(s.points) || s.points.length < 2 || !s.points.every((q) => isVec(q, 2))) throw paramError(`${where}: curve.points needs at least two [along, value] pairs in meters.`);
    const along = s.points.map((q) => q[0]).sort((a, b) => a - b);
    if (along.some((a, k) => k > 0 && a - along[k - 1] < 1e-9)) throw paramError(`${where}: curve.points - the along values must not repeat.`);
    return;
  }
  if ('axis' in s && 'value' in s) {
    if (!['x', 'y', 'z'].includes(lower(s.axis)) || !isNum(s.value)) throw paramError(`${where}: plane must be {axis: "x" | "y" | "z", value: number}.`);
    return;
  }
  if ('point' in s && 'normal' in s) {
    if (!isVec(s.point, 3) || !isVec(s.normal, 3) || s.normal.every((x) => x === 0)) throw paramError(`${where}: plane must be {point: [x, y, z], normal: [x, y, z]} (non-zero normal).`);
    return;
  }
  throw paramError(`${where}: plane must be {axis, value} or {point, normal}; curve must be {axis, along, points}.`);
}

const BONE_ORIGINS = { skeleton_hand: ['wrist', 'ground'], bone_staff: ['bbox', 'axis'], bone_toggle: [] };
const BONE_LENGTH = { skeleton_hand: [0.02, 2], bone_staff: [0.6, 4], bone_toggle: [0.02, 0.5] };

function checkBoneVariant(p, where) {
  const part = p.part;
  if (!(part in BONE_ORIGINS)) throw paramError(`${where}: part is required - skeleton_hand, bone_staff or bone_toggle.`);
  if (p.origin !== undefined && p.origin !== null && !BONE_ORIGINS[part].includes(p.origin)) {
    throw paramError(`${where}: origin for ${part} must be ${BONE_ORIGINS[part].join(' or ') || 'not settable'}.`);
  }
  if (typeof p.length_m === 'number') {
    const [lo, hi] = BONE_LENGTH[part];
    if (!(p.length_m > lo && p.length_m < hi)) throw paramError(`${where}: length_m for ${part} must be from ${lo} to ${hi} m.`);
  }
}

const KITBASH_KINDS = ['gatling', 'nozzle', 'piping', 'antenna', 'mast', 'nav_light', 'vent_grille', 'block', 'remove'];
const isPoint = (v) => isVec(v, 2) || isVec(v, 3);
const isRegion = (r) => isPlainObject(r) && (
  (isPlainObject(r.cylinder) && isVec(r.cylinder.p0, 3) && isVec(r.cylinder.p1, 3) && typeof r.cylinder.radius === 'number')
  || (isPlainObject(r.sphere) && isVec(r.sphere.center, 3) && typeof r.sphere.radius === 'number')
  || (isPlainObject(r.box) && isVec(r.box.min, 3) && isVec(r.box.max, 3)));

const STEP_CHECKS = {
  kitbash(p) {
    if (p.parts === undefined) return;
    if (!Array.isArray(p.parts)) throw paramError('Step "kitbash": parts must be a list of parts {kind, at, ...}.');
    p.parts.forEach((part, i) => {
      const where = `Step "kitbash", part #${i + 1}`;
      if (!isPlainObject(part)) throw paramError(`${where}: expected an object {kind, at, ...}.`);
      if (!KITBASH_KINDS.includes(part.kind)) throw paramError(`${where}: kind — ${KITBASH_KINDS.join(', ')}.`);
      if (part.kind === 'piping') {
        if (!Array.isArray(part.points) || part.points.length < 2 || !part.points.every((q) => isVec(q, 3))) {
          throw paramError(`${where}: points needs at least two points [x, y, z] (m, nose +Y).`);
        }
      } else if (part.kind === 'remove') {
        if (!Array.isArray(part.regions) || !part.regions.length || !part.regions.every(isRegion)) {
          throw paramError(`${where}: regions must be zones {cylinder: {p0, p1, radius}} | {sphere: {center, radius}} | {box: {min, max}}.`);
        }
      } else {
        const at = part.at;
        const ok = (typeof at === 'string' && at.trim()) || isPoint(at) || (Array.isArray(at) && at.length && at.every(isPoint));
        if (!ok) throw paramError(`${where}: at must be a socket name or mask (socket_turret_*), a point [x, y, z] / [x, y] or a list of points.`);
      }
      if (part.remove !== undefined && part.remove !== false && !isRegion(part.remove)
          && !(isPlainObject(part.remove) && Object.keys(part.remove).every((k) => ['radius', 'depth', 'inset', 'above', 'cap'].includes(k)))) {
        throw paramError(`${where}: remove must be false, a zone {cylinder | sphere | box} or {radius, depth, inset, above, cap}.`);
      }
    });
  },
  ship_from_mesh(p) {
    if (p.paint !== undefined && p.paint !== null) {
      const pt = p.paint;
      if (!isPlainObject(pt) || !(typeof pt.image === 'string' || typeof pt.asset === 'string')) {
        throw paramError('Step "ship_from_mesh": paint must be {image (a path, CLI only) | asset (the UUID of a studio image), camera, mirror, regions...}.');
      }
      if (pt.asset !== undefined && !(typeof pt.asset === 'string' && UUID_LIKE.test(pt.asset))) throw paramError('Step "ship_from_mesh": paint.asset must be the UUID of a studio image.');
      (Array.isArray(pt.regions) ? pt.regions : []).forEach((r, i) => {
        if (!isPlainObject(r) || !isVec(r.box, 4) || typeof r.label !== 'string') {
          throw paramError(`Step "ship_from_mesh": paint.regions[${i}] must be {box: [u0, v0, u1, v1] (fractions of the image), label}.`);
        }
      });
    }
    (Array.isArray(p.decals) ? p.decals : []).forEach((d, i) => {
      const where = `Step "ship_from_mesh", inscription #${i + 1}`;
      if (!isPlainObject(d) || !(typeof d.text === 'string' || Array.isArray(d.lines))) throw paramError(`${where}: text is required.`);
      if (!isPoint(d.position || d.center) || !isVec(d.size, 2)) throw paramError(`${where}: position [y, z] (side) / [x, y] (top) and size [w, h] in meters are required.`);
    });
  },
  bone_kit(p) {
    const batch = Array.isArray(p.batch) ? p.batch : null;
    if (!batch) {
      checkBoneVariant(p, 'Step "bone_kit"');
      if (p.batch !== undefined && !(Number.isInteger(p.batch) && p.batch >= 1 && p.batch <= 64)) throw paramError('Step "bone_kit": batch must be a number of variants 1-64 or a list of dictionaries.');
      return;
    }
    if (!batch.length || batch.length > 64) throw paramError('Step "bone_kit": batch must be from 1 to 64 variants.');
    batch.forEach((v, i) => {
      if (!isPlainObject(v)) throw paramError(`Step "bone_kit", variant #${i + 1}: expected a parameter dictionary.`);
      checkBoneVariant({ ...p, batch: undefined, ...v }, `Step "bone_kit", variant #${i + 1}`);
    });
  },
  paint_from_concept(p) {
    const where = 'Step "paint_from_concept"';
    const c = p.camera;
    if (c !== undefined && c !== null && c !== 'fit') {
      const hint = 'camera must be "fit" or {yaw_deg, pitch_deg, px_per_m > 0, offset_px: [x, y]} (as in paint_camera.json).';
      if (!isPlainObject(c) || !(isNum(c.px_per_m) && c.px_per_m > 0)) throw paramError(`${where}: ${hint}`);
      for (const k of ['yaw_deg', 'pitch_deg']) if (c[k] !== undefined && !isNum(c[k])) throw paramError(`${where}: camera.${k} must be a number (degrees).`);
      if (c.offset_px !== undefined && !isVec(c.offset_px, 2)) throw paramError(`${where}: camera.offset_px must be [x, y] in concept pixels.`);
    }
    const fMin = isNum(p.facing_min) ? p.facing_min : 0.1;
    const fFull = isNum(p.facing_full) ? p.facing_full : 0.4;
    if (fFull <= fMin) throw paramError(`${where}: facing_full (${fFull}) must be greater than facing_min (${fMin}).`);
  },
  assemble_parts(p) {
    const where = 'Step "assemble_parts"';
    const c = p.camera;
    if (c !== undefined && c !== null && c !== 'fit') {
      if (!isPlainObject(c) || !(isNum(c.px_per_m) && c.px_per_m > 0)) throw paramError(`${where}: camera must be "fit" or {px_per_m > 0, offset_px: [x, y], centre: [x, y, z]} (as in parts_fit.json).`);
      if (c.offset_px !== undefined && !isVec(c.offset_px, 2)) throw paramError(`${where}: camera.offset_px must be [x, y] in reference pixels.`);
      if (c.centre !== undefined && !isVec(c.centre, 3)) throw paramError(`${where}: camera.centre must be [x, y, z] in meters.`);
    }
    if (Array.isArray(p.icp_scale_range) && !(p.icp_scale_range[0] > 0 && p.icp_scale_range[0] < p.icp_scale_range[1])) {
      throw paramError(`${where}: icp_scale_range must be [min, max], 0 < min < max.`);
    }
    if (p.pick !== undefined && p.pick !== null) {
      if (!isPlainObject(p.pick)) throw paramError(`${where}: pick must be {part: seed | path}.`);
      for (const [k, v] of Object.entries(p.pick)) {
        if (!/^[a-z][a-z0-9_]*$/.test(k)) throw paramError(`${where}: pick - the part name "${k}" is not valid (head, torso, arm_l, arm_r, legs).`);
        if (!(Number.isInteger(v) || (typeof v === 'string' && v.trim()))) throw paramError(`${where}: pick.${k} must be a seed (integer) or a path to a mesh.`);
      }
    }
  },
  replace_part(p) {
    if (p.region !== undefined && p.region !== null) {
      const r = p.region;
      if (!isPlainObject(r) || !isVec(r.min, 3) || !isVec(r.max, 3)) throw paramError('Step "replace_part": region must be {min: [x, y, z], max: [x, y, z]} in meters.');
      if (!r.max.every((v, k) => v > r.min[k])) throw paramError('Step "replace_part": region.max must be greater than region.min on every axis.');
    }
    if (p.cut_plane !== undefined && p.cut_plane !== null) {
      const c = p.cut_plane;
      const axisForm = isPlainObject(c) && ['x', 'y', 'z'].includes(String(c.axis || '').toLowerCase()) && typeof c.value === 'number';
      const pointForm = isPlainObject(c) && isVec(c.point, 3) && isVec(c.normal, 3);
      if (!axisForm && !pointForm) throw paramError('Step "replace_part": cut_plane must be {axis: "z", value: 1.5} or {point: [x, y, z], normal: [x, y, z]}.');
    }
    if (p.part_asset !== undefined && p.part_asset !== null && !(typeof p.part_asset === 'string' && UUID_LIKE.test(p.part_asset))) {
      throw paramError('Step "replace_part": part_asset must be the UUID of an asset with the part.');
    }
    if (p.part_transform !== undefined && p.part_transform !== null) {
      const t = p.part_transform;
      const form = 'part_transform must be {location: [x, y, z], rot_deg: [x, y, z] | matrix: 3x3 by rows, scale: s}';
      if (!isPlainObject(t) || !Object.keys(t).every((k) => ['location', 'rot_deg', 'matrix', 'scale'].includes(k))) throw paramError(`Step "replace_part": ${form}.`);
      if (t.location !== undefined && !isVec(t.location, 3)) throw paramError('Step "replace_part": part_transform.location must be [x, y, z] in meters.');
      if (t.rot_deg !== undefined && !isVec(t.rot_deg, 3)) throw paramError('Step "replace_part": part_transform.rot_deg must be [x, y, z] in degrees.');
      if (t.matrix !== undefined && t.matrix !== null && !(isMatrix3(t.matrix) && det3(t.matrix) > 1e-6)) throw paramError('Step "replace_part": part_transform.matrix must be a 3x3 rotation given by rows (determinant > 0).');
      if (t.scale !== undefined && !(isNum(t.scale) && t.scale > 0)) throw paramError('Step "replace_part": part_transform.scale must be a positive number.');
    }
  },
  place_parts(p) {
    if (!Array.isArray(p.points) || !p.points.length) {
      throw paramError('Step "place_parts": points is required - a non-empty list of [x, z] (front view, m), [x, y, z] or {at, ...}.');
    }
    p.points.forEach(checkPlacePoint);
    if (p.part_asset !== undefined && p.part_asset !== null && !(typeof p.part_asset === 'string' && UUID_LIKE.test(p.part_asset))) {
      throw paramError('Step "place_parts": part_asset must be the UUID of an asset with the part.');
    }
    if (Array.isArray(p.footprint_grid) && !p.footprint_grid.every((n) => n >= 1)) throw paramError('Step "place_parts": footprint_grid must be [nx, nz], each at least 1.');
    if (Array.isArray(p.up) && p.up.every((x) => x === 0)) throw paramError('Step "place_parts": up must not be a zero vector.');
  },
  rig_humanoid(p) {
    if (p.grip_dirs !== undefined && p.grip_dirs !== null) {
      if (!isPlainObject(p.grip_dirs)) throw paramError(`Step "rig_humanoid": grip_dirs must be {"l"|"r": ${GRIP_DIR_HINT}}.`);
      for (const [k, v] of Object.entries(p.grip_dirs)) {
        if (!SIDE_KEYS.includes(String(k).toLowerCase())) throw paramError('Step "rig_humanoid": the keys of grip_dirs must be l or r.');
        if (!isGripDir(v)) throw paramError(`Step "rig_humanoid": grip_dirs.${k} must be ${GRIP_DIR_HINT}.`);
      }
    }
    if (p.socket_bones !== undefined && p.socket_bones !== null
        && !(isPlainObject(p.socket_bones) && Object.values(p.socket_bones).every((b) => typeof b === 'string' && b.trim()))) {
      throw paramError('Step "rig_humanoid": socket_bones must be {socket name: bone}, for example {"socket_fx_eye_l": "Head"}.');
    }
    if (Array.isArray(p.arm_bleed) && !(p.arm_bleed[0] >= 0 && p.arm_bleed[0] < p.arm_bleed[1] && p.arm_bleed[1] <= 1)) {
      throw paramError('Step "rig_humanoid": arm_bleed must be [low, high], 0 <= low < high <= 1 (empty - off).');
    }
  },
  retarget(p) {
    checkClipList('retarget', p.clips);
    checkRetargetOptions(p, 'Step "retarget"');
  },
  motion_style(p) {
    // per clip: {clip | category: {knob: value} | false}; knobs and ranges as the global params
    if (p.clips === undefined || p.clips === null) return;
    if (!isPlainObject(p.clips)) throw paramError('Step "motion_style": clips must be {clip or category: {knob: value} | false}.');
    const params = STEPS.get('motion_style').params;
    for (const [clip, o] of Object.entries(p.clips)) {
      const where = `Step "motion_style", clip "${clip}"`;
      if (o === false) continue;
      if (!isPlainObject(o)) throw paramError(`${where}: expected {knob: value} or false.`);
      for (const [k, v] of Object.entries(o)) {
        if (!STYLE_KNOBS.includes(k)) throw paramError(`${where}: unknown knob "${k}" (allowed: ${STYLE_KNOBS.join(', ')}).`);
        if (k === 'limp_side') {
          if (!['l', 'r'].includes(v)) throw paramError(`${where}: limp_side must be l or r.`);
          continue;
        }
        const spec = params[k];
        if (!isNum(v) || v < spec.min || v > spec.max) throw paramError(`${where}: ${k} must be a number from ${spec.min} to ${spec.max}.`);
      }
    }
  },
  attach_prop(p) {
    checkClipList('attach_prop', p.clips, { strictKeys: false });
    if (p.extracted_mode !== undefined && p.extracted_mode !== null && !['bone_child', 'skin_separate', 'skin_rigid'].includes(p.extracted_mode)) {
      throw paramError('Step "attach_prop": extracted_mode must be bone_child, skin_separate or skin_rigid.');
    }
    if (p.extracted_shield_fit !== undefined && p.extracted_shield_fit !== null && !['auto', 'placement', 'clips', 'rest'].includes(p.extracted_shield_fit)) {
      throw paramError('Step "attach_prop": extracted_shield_fit must be auto, placement, clips or rest.');
    }
    if (p.extracted && Array.isArray(p.attachments) && p.attachments.length) {
      throw paramError('Step "attach_prop": extracted (extract_props items) and attachments cannot be set together - use one or the other.');
    }
    if (p.grip_dir !== undefined && p.grip_dir !== null && !isGripDir(p.grip_dir)) throw paramError(`Step "attach_prop": grip_dir must be ${GRIP_DIR_HINT}.`);
    (Array.isArray(p.attachments) ? p.attachments : []).forEach((a, i) => {
      const where = `Step "attach_prop", item #${i + 1}`;
      if (!isPlainObject(a)) throw paramError(`${where}: expected an object {asset, hand, ...}.`);
      const hasAsset = typeof a.asset === 'string' && UUID_LIKE.test(a.asset);
      if (a.asset !== undefined && !hasAsset) throw paramError(`${where}: asset must be the UUID of an item asset.`);
      if (!hasAsset && !(typeof a.item === 'string' && a.item.trim())) throw paramError(`${where}: asset (the UUID of an item asset) or item (a path, CLI --direct only) is required.`);
      if (a.hand !== undefined && !['r', 'l'].includes(a.hand)) throw paramError(`${where}: hand must be "r" or "l".`);
      if (a.mode !== undefined && !['bone_child', 'skin_rigid', 'skin_separate'].includes(a.mode)) throw paramError(`${where}: mode must be bone_child, skin_rigid or skin_separate.`);
      if (a.fit !== undefined && !['auto', 'frame', 'rest', 'clips'].includes(a.fit)) throw paramError(`${where}: fit must be auto, frame, rest or clips.`);
      if (a.refit_socket !== undefined && typeof a.refit_socket !== 'boolean') throw paramError(`${where}: refit_socket must be true/false.`);
      if (a.grip_dir !== undefined && a.grip_dir !== null && !isGripDir(a.grip_dir)) throw paramError(`${where}: grip_dir — ${GRIP_DIR_HINT}.`);
    });
  },
  extract_props(p) {
    const where = 'Step "extract_props"';
    if (p.hints !== undefined && p.hints !== null) {
      if (!isPlainObject(p.hints)) throw paramError(`${where}: hints must be a roles.json entry object {hand_weapon, hand, shield, ...}.`);
      const h = p.hints;
      if (h.hand !== undefined && h.hand !== null && !['r', 'l', 'both', 'none', ''].includes(lower(h.hand))) throw paramError(`${where}: hints.hand must be r, l or both.`);
      if (h.hand_weapon !== undefined && h.hand_weapon !== null && typeof h.hand_weapon !== 'string') throw paramError(`${where}: hints.hand_weapon must be a string ("sword", "banner + staff").`);
      if (h.shield !== undefined && typeof h.shield !== 'boolean') throw paramError(`${where}: hints.shield must be true/false.`);
    }
    if (p.gate !== undefined && p.gate !== null) {
      if (!isPlainObject(p.gate)) throw paramError(`${where}: gate must be an object of thresholds.`);
      for (const [k, v] of Object.entries(p.gate)) {
        if (!['open_edge_frac', 'main_part_share', 'parts_over_1pct', 'min_tris'].includes(k)) throw paramError(`${where}: gate.${k} is an unknown threshold (open_edge_frac, main_part_share, parts_over_1pct, min_tris).`);
        if (!(isNum(v) && v >= 0)) throw paramError(`${where}: gate.${k} must be a number, at least 0.`);
      }
    }
  },
  separate(p) {
    if ((p.mode === 'regions' || p.mode === 'slit') && (!Array.isArray(p.regions) || !p.regions.length)) {
      throw paramError(`Step "separate": mode = ${p.mode} needs zones regions: [{name, min: [x, y, z], max: [x, y, z]${p.mode === 'slit' ? ', plane | curve' : ''}}].`);
    }
    const defaultKind = p.mode === 'slit' ? 'slit' : 'part';
    (Array.isArray(p.regions) ? p.regions : []).forEach((r, i) => {
      const where = `Step "separate", zone #${i + 1}`;
      if (!isPlainObject(r)) throw paramError(`${where}: expected {name, min: [x, y, z], max: [x, y, z]}.`);
      const kind = r.kind === undefined ? defaultKind : lower(r.kind);
      if (!['part', 'delete', 'slit'].includes(kind)) throw paramError(`${where}: kind must be part, delete or slit.`);
      // a part is named after its region; delete / slit zones are named delete_<i> / slit_<i> when unnamed
      if (r.name !== undefined || kind === 'part') {
        if (typeof r.name !== 'string' || !r.name.trim()) throw paramError(`${where}: no name.`);
      }
      if (!isVec(r.min, 3) || !isVec(r.max, 3)) throw paramError(`${where}: min and max must be [x, y, z] in meters (after normalization, not fractions of the height).`);
      if (!r.max.every((v, k) => v > r.min[k])) throw paramError(`${where}: max must be greater than min on every axis.`);
      if (r.caps !== undefined && typeof r.caps !== 'boolean') throw paramError(`${where}: caps must be true/false.`);
      if (r.facing !== undefined && r.facing !== null && !(isVec(r.facing, 3) && r.facing.some((x) => x !== 0))) throw paramError(`${where}: facing must be a direction [x, y, z] (non-zero), for example [0, 0, -1] for the bottom of a robe.`);
      if (r.facing_min !== undefined && !(isNum(r.facing_min) && r.facing_min >= -1 && r.facing_min <= 1)) throw paramError(`${where}: facing_min must be a number from -1 to 1.`);
      const slitOnly = ['plane', 'curve', 'width_m', 'gap_m'].filter((k) => r[k] !== undefined && r[k] !== null);
      if (slitOnly.length && kind !== 'slit') throw paramError(`${where}: ${slitOnly.join(', ')} only applies to a zone of kind slit (here kind ${kind}).`);
      for (const k of ['width_m', 'gap_m']) if (r[k] !== undefined && !(isNum(r[k]) && r[k] >= 0)) throw paramError(`${where}: ${k} must be a number, at least 0 (meters).`);
      const surface = r.curve || r.plane;
      if (surface !== undefined && surface !== null) checkSlitSurface(surface, where);
    });
  },
};

const clone = (v) => (v === undefined ? undefined : structuredClone(v));

// ------------------------------------------------------------------ public API

export function stepNames() {
  return [...STEPS.keys()];
}

export function getStep(name) {
  return STEPS.get(name) || null;
}

/** Registry as served by GET /api/forge/steps (JSON-safe, server-only params hidden). */
export function listSteps() {
  return STEP_LIST.map((s) => ({
    step: s.step,
    title: s.title,
    help: s.help,
    module: s.module,
    kinds: [...s.kinds],
    scope: s.scope || 'asset',
    inputs: clone(s.inputs),
    outputs: [...(s.outputs || [])],
    params: Object.fromEntries(Object.entries(s.params).map(([k, v]) => [k, clone(v)])),
    gpu: s.gpu ? true : (s.gpuWhen ? 'conditional' : false),
    gpuWhen: clone(s.gpuWhen) || null,
    timeoutSec: timeoutFor(s.step),
    export: !!s.export,
  }));
}

export function listPresets() {
  return PRESET_LIST.map((p) => clone(p));
}

export function getPreset(id) {
  const p = PRESETS.get(id);
  return p ? clone(p) : null;
}

export function timeoutFor(name, params) {
  const s = STEPS.get(name);
  if (!s) return 15 * 60;
  if (s.timeoutSec) return s.timeoutSec;
  return isGpuStep(name, params) ? 60 * 60 : 15 * 60;
}

function compare(a, op, b) {
  switch (op) {
    case '==': return a === b;
    case '!=': return a !== b;
    case '>': return typeof a === 'number' && a > b;
    case '>=': return typeof a === 'number' && a >= b;
    case '<': return typeof a === 'number' && a < b;
    case '<=': return typeof a === 'number' && a <= b;
    case 'truthy': return !!a;
    default: return false;
  }
}

/** True when the step must hold the shared GPU mutex for these params. */
export function isGpuStep(name, params = {}) {
  const s = STEPS.get(name);
  if (!s) return false;
  if (s.gpu) return true;
  if (!s.gpuWhen) return false;
  const p = { ...defaultParams(name), ...(params || {}) };
  if (s.gpuWhen.all) return s.gpuWhen.all.every((c) => compare(p[c.param], c.op, c.value));
  if (s.gpuWhen.any) return s.gpuWhen.any.some((c) => compare(p[c.param], c.op, c.value));
  return false;
}

export function defaultParams(name) {
  const s = STEPS.get(name);
  if (!s) return {};
  return Object.fromEntries(Object.entries(s.params).map(([k, v]) => [k, clone(v.default)]));
}

function typeOk(spec, v) {
  switch (spec.type) {
    case 'number': return typeof v === 'number' && Number.isFinite(v);
    case 'integer': return Number.isInteger(v);
    case 'boolean': return typeof v === 'boolean';
    case 'string': return typeof v === 'string';
    case 'array': return Array.isArray(v);
    case 'object': return v !== null && typeof v === 'object' && !Array.isArray(v);
    default: return true;
  }
}

const TYPE_NAMES = { number: 'a number', integer: 'an integer', boolean: 'true/false', string: 'a string', array: 'a list', object: 'an object' };

/** Russian description of why v does not fit spec (null = fits). */
function valueProblem(spec, v, where) {
  if (!typeOk(spec, v)) return `${where}: expected ${TYPE_NAMES[spec.type] || spec.type}.`;
  if (spec.enum && !spec.enum.some((e) => e === v)) {
    return `${where}: allowed values: ${spec.enum.map((e) => JSON.stringify(e)).join(', ')}.`;
  }
  if (typeof v === 'number') {
    if (spec.min !== undefined && spec.min !== null && v < spec.min) return `${where}: must be at least ${spec.min}.`;
    if (spec.max !== undefined && spec.max !== null && v > spec.max) return `${where}: must be at most ${spec.max}.`;
  }
  if (Array.isArray(v)) {
    if (Number.isInteger(spec.minItems) && v.length < spec.minItems) return `${where}: needs at least ${spec.minItems} items.`;
    if (Number.isInteger(spec.maxItems) && v.length > spec.maxItems) return `${where}: needs at most ${spec.maxItems} items.`;
    if (spec.items) {
      for (let i = 0; i < v.length; i += 1) {
        const item = v[i];
        if (spec.items.type && !typeOk(spec.items, item)) return `${where}[${i}]: expected ${TYPE_NAMES[spec.items.type] || spec.items.type}.`;
        if (spec.items.enum && !spec.items.enum.includes(item)) return `${where}[${i}]: allowed values: ${spec.items.enum.join(', ')}.`;
      }
    }
  }
  return null;
}

function checkValue(stepName, key, spec, v) {
  const where = `Step "${stepName}", parameter "${key}"`;
  if (v === null) {
    if (spec.nullable || spec.default === null || (spec.enum && spec.enum.includes(null))) return;
    throw paramError(`${where}: the value must not be empty.`);
  }
  const problem = valueProblem(spec, v, where);
  if (!problem) return;
  if (Array.isArray(spec.alt) && spec.alt.some((alt) => !valueProblem(alt, v, where))) return;
  throw paramError(problem);
}

function paramError(message) {
  return Object.assign(new Error(message), { status: 400, code: 'bad_params' });
}

/**
 * Validate params. Unknown keys are kept (real step modules may accept more) and reported in
 * `warnings`.
 * @returns {{params: object, explicit: object, warnings: string[]}}
 *   params   — registry defaults + given values (for display / GPU and timeout decisions)
 *   explicit — only the given values; this is what goes to Blender and into the recipe, so the
 *              step module's own defaults stay authoritative (null for a param whose default is
 *              null is dropped: "empty" = module default)
 */
export function normalizeParams(name, params = {}) {
  const s = STEPS.get(name);
  if (!s) throw Object.assign(new Error(`Unknown step "${name}".`), { status: 400, code: 'unknown_step' });
  if (params === null || params === undefined) params = {};
  if (typeof params !== 'object' || Array.isArray(params)) throw paramError(`Step "${name}": params must be an object.`);
  const out = defaultParams(name);
  const explicit = {};
  const warnings = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined) continue;
    const spec = s.params[k];
    if (!spec) {
      warnings.push(`Step "${name}": parameter "${k}" is not in the registry and is passed through as is.`);
    } else {
      checkValue(name, k, spec, v);
    }
    out[k] = clone(v);
    // null for a param whose registry default is null means "empty — the module decides": it is
    // not sent, so a module reading p.get(key, default) never receives None.
    if (v === null && spec && spec.default === null) continue;
    explicit[k] = clone(v);
  }
  if (STEP_CHECKS[name]) STEP_CHECKS[name](explicit);
  return { params: out, explicit, warnings };
}

/** Names of params that are local paths (path:true, or library folders filled by the server). */
export function pathParams(name) {
  const s = STEPS.get(name);
  if (!s) return [];
  return Object.entries(s.params).filter(([, spec]) => spec.path || spec.auto === 'animations').map(([k]) => k);
}

/** Params actually sent to Blender (server-only keys removed). */
export function blenderParams(name, params) {
  const s = STEPS.get(name);
  const out = { ...params };
  if (s) for (const [k, spec] of Object.entries(s.params)) if (spec.server) delete out[k];
  return out;
}

/** Validate a step list ([{step, params}]) and return copies with the explicit params only. */
export function normalizeSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) {
    throw Object.assign(new Error('A non-empty list of steps is required.'), { status: 400, code: 'no_steps' });
  }
  if (steps.length > 40) throw Object.assign(new Error('Too many steps (maximum 40).'), { status: 400, code: 'too_many_steps' });
  const warnings = [];
  const out = steps.map((entry, i) => {
    if (!entry || typeof entry.step !== 'string') {
      throw Object.assign(new Error(`Step #${i + 1}: the step name is not specified.`), { status: 400, code: 'bad_steps' });
    }
    const n = normalizeParams(entry.step, entry.params || {});
    warnings.push(...n.warnings);
    return { step: entry.step, params: n.explicit };
  });
  return { steps: out, warnings };
}

// ---------------------------------------------------------------- creature size (smoke 10.10)
// creature_auto has no size of its own: without profile.height_m / length_m the warhorse kept the
// file's native 0.87 m. The passport size goes to rig_creature (and to cleanup of a creature), and a
// native size that is implausible for the template is reported after rig_creature.

/** Plausible sizes per creature template, metres: height = bbox Z after rig_creature, length = Y. */
export const CREATURE_SIZE_RANGES = {
  quadruped: { dim: 'height', min: 0.3, max: 6 },
  winged_quadruped: { dim: 'height', min: 0.3, max: 12 },
  bird: { dim: 'height', min: 0.08, max: 5 },
  hexapod: { dim: 'height', min: 0.05, max: 6 },
  octopod: { dim: 'height', min: 0.05, max: 6 },
  serpent: { dim: 'length', min: 0.3, max: 40 },
};

const positive = (v) => (Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : null);

/**
 * Fill rig_creature height_m / length_m (and cleanup height_m for a creature asset) from the asset
 * profile when the step params leave them empty. Mutates and returns `steps` ([{step, params}],
 * normalized); returns notes for the job warnings.
 */
export function applyProfileSize(steps, asset) {
  const notes = [];
  const prof = asset?.profile || {};
  const h = positive(prof.height_m);
  const l = positive(prof.length_m);
  if (!h && !l) return notes;
  for (const s of steps) {
    const p = s.params || (s.params = {});
    const unset = (k) => p[k] === undefined || p[k] === null;
    if (s.step === 'rig_creature' && unset('height_m') && unset('length_m')) {
      const spec = getStep('rig_creature').params;
      if (h && h >= spec.height_m.min && h <= spec.height_m.max) p.height_m = h;
      else if (l && l >= spec.length_m.min && l <= spec.length_m.max) p.length_m = l;
      else notes.push(`The passport size is outside the allowed range for rig_creature (height ${h ?? '—'} m, length ${l ?? '—'} m) - not passed.`);
    }
    if (s.step === 'cleanup' && asset?.kind === 'creature' && unset('height_m') && h) {
      const spec = getStep('cleanup').params.height_m;
      if (h >= spec.min && h <= spec.max) p.height_m = h;
    }
  }
  return notes;
}

/**
 * Warning text when rig_creature kept the native size (no height_m / length_m) and that size is
 * implausible for the template, else null. size = stats.size [x, y, z] (Blender, head to -Y).
 */
export function creatureSizeWarning(template, size, params = {}) {
  if (positive(params.height_m) || positive(params.length_m)) return null;
  const r = CREATURE_SIZE_RANGES[template];
  if (!r || !Array.isArray(size) || size.length < 3) return null;
  const v = Number(r.dim === 'height' ? size[2] : size[1]);
  if (!Number.isFinite(v) || (v >= r.min && v <= r.max)) return null;
  const fmt = (x) => String(Math.round(x * 1000) / 1000);
  return `Creature size is implausible for template ${template}: ${r.dim === 'height' ? 'height' : 'length'} ${fmt(v)} m ` +
    `(expected ${fmt(r.min)}–${fmt(r.max)} m). Height and length are not set (profile.height_m / length_m, rig_creature parameters) - ` +
    'the file\'s native scale was kept; set the size in the asset passport.';
}
