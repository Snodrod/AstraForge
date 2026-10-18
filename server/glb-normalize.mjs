// Normalise a generated GLB like the Hunyuan seed meshes of the pipeline:
//   glTF convention (+Y up, the character looks toward +Z = -Y after Blender's glTF import),
//   bounding box centred on the origin, HEIGHT (Y extent) = 2.
// Pixal3D already exports in that orientation (checked against the 24-rotation silhouette search of
// tools/normalize_seed_mesh.py on the militia: identity rotation wins), so only translation and one uniform scale are
// applied. Only POSITION accessors are rewritten (NORMAL stays valid under a uniform scale), the rest of the file is kept.
const GLB_MAGIC = 0x46546c67, CHUNK_JSON = 0x4e4f534a, CHUNK_BIN = 0x004e4942, FLOAT = 5126;

export function parseGlb(buffer) {
  if (buffer.length < 28 || buffer.readUInt32LE(0) !== GLB_MAGIC || buffer.readUInt32LE(4) !== 2) throw new Error('Invalid GLB: missing glTF 2.0 header.');
  if (buffer.readUInt32LE(8) !== buffer.length) throw new Error('Invalid GLB: file length does not match the header.');
  let offset = 12, json = null, bin = null;
  while (offset + 8 <= buffer.length) {
    const length = buffer.readUInt32LE(offset), type = buffer.readUInt32LE(offset + 4);
    const start = offset + 8, end = start + length;
    if (end > buffer.length) throw new Error('Invalid GLB: chunk extends past the end of the file.');
    if (type === CHUNK_JSON && !json) json = JSON.parse(buffer.toString('utf8', start, end));
    else if (type === CHUNK_BIN && !bin) bin = Buffer.from(buffer.subarray(start, end));
    offset = end + ((4 - (length % 4)) % 4);
  }
  if (!json) throw new Error('Invalid GLB: missing JSON chunk.');
  return { json, bin };
}

export function buildGlb(json, bin) {
  const jsonBytes = Buffer.from(JSON.stringify(json), 'utf8');
  const jsonPad = (4 - (jsonBytes.length % 4)) % 4;
  const binPad = bin ? (4 - (bin.length % 4)) % 4 : 0;
  const total = 12 + 8 + jsonBytes.length + jsonPad + (bin ? 8 + bin.length + binPad : 0);
  const out = Buffer.alloc(total);
  out.writeUInt32LE(GLB_MAGIC, 0); out.writeUInt32LE(2, 4); out.writeUInt32LE(total, 8);
  out.writeUInt32LE(jsonBytes.length + jsonPad, 12); out.writeUInt32LE(CHUNK_JSON, 16);
  jsonBytes.copy(out, 20); out.fill(0x20, 20 + jsonBytes.length, 20 + jsonBytes.length + jsonPad);
  if (bin) {
    const at = 20 + jsonBytes.length + jsonPad;
    out.writeUInt32LE(bin.length + binPad, at); out.writeUInt32LE(CHUNK_BIN, at + 4);
    bin.copy(out, at + 8);
  }
  return out;
}

/** Distinct POSITION accessors of all mesh primitives, each with the byte layout it reads. */
function positionAccessors(json, bin) {
  const seen = new Map(), layouts = new Set();
  for (const mesh of json.meshes || []) for (const primitive of mesh.primitives || []) {
    const index = primitive.attributes?.POSITION;
    if (index === undefined || seen.has(index)) continue;
    const accessor = json.accessors?.[index];
    if (!accessor || accessor.type !== 'VEC3' || accessor.componentType !== FLOAT || accessor.sparse || accessor.bufferView === undefined) {
      throw new Error('Unsupported vertex format in GLB (float32 VEC3 without sparse is required).');
    }
    const view = json.bufferViews?.[accessor.bufferView];
    if (!view || (view.buffer ?? 0) !== 0) throw new Error('Invalid GLB: vertices are outside the main buffer.');
    const stride = view.byteStride || 12, base = (view.byteOffset || 0) + (accessor.byteOffset || 0);
    if (base + (accessor.count - 1) * stride + 12 > bin.length) throw new Error('Invalid GLB: vertices extend past the end of the buffer.');
    // two accessors reading the same bytes must be transformed once
    const key = base + ':' + accessor.count + ':' + stride;
    if (layouts.has(key)) continue;
    layouts.add(key);
    seen.set(index, { index, accessor, stride, base });
  }
  if (!seen.size) throw new Error('The GLB contains no geometry.');
  return [...seen.values()];
}

export function glbBounds(buffer) {
  const { json, bin } = parseGlb(buffer);
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  let vertices = 0;
  for (const { accessor, stride, base } of positionAccessors(json, bin)) {
    vertices += accessor.count;
    for (let i = 0; i < accessor.count; i++) for (let a = 0; a < 3; a++) {
      const v = bin.readFloatLE(base + i * stride + a * 4);
      if (!Number.isFinite(v)) throw new Error('The GLB contains non-numeric vertex coordinates.');
      if (v < min[a]) min[a] = v;
      if (v > max[a]) max[a] = v;
    }
  }
  return { min, max, vertices, extent: max.map((v, a) => v - min[a]) };
}

/**
 * Centre the bounding box on the origin and scale uniformly so the Y extent equals `height`.
 * @returns {{buffer: Buffer, before: object, after: object, scale: number, center: number[]}}
 */
export function normalizeGlb(buffer, { height = 2 } = {}) {
  const { json, bin } = parseGlb(buffer);
  if (!bin) throw new Error('Invalid GLB: missing binary geometry chunk.');
  const before = glbBounds(buffer);
  const sizeY = before.extent[1];
  if (!(sizeY > 1e-9)) throw new Error('The mesh has no height: nothing to normalize.');
  const center = before.min.map((v, a) => (v + before.max[a]) / 2);
  const scale = height / sizeY;
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const { accessor, stride, base } of positionAccessors(json, bin)) {
    for (let i = 0; i < accessor.count; i++) for (let a = 0; a < 3; a++) {
      const at = base + i * stride + a * 4;
      const v = Math.fround((bin.readFloatLE(at) - center[a]) * scale);
      bin.writeFloatLE(v, at);
      if (v < min[a]) min[a] = v;
      if (v > max[a]) max[a] = v;
    }
  }
  for (const { accessor } of positionAccessors(json, bin)) { accessor.min = [...min]; accessor.max = [...max]; }
  const out = buildGlb(json, bin);
  return { buffer: out, before, after: { min, max, vertices: before.vertices, extent: max.map((v, a) => v - min[a]) }, scale, center };
}
