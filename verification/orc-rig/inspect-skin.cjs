const fs = require('fs')

const file = process.argv[2]
if (!file) throw new Error('Usage: node inspect-skin.cjs model.glb')
const data = fs.readFileSync(file)
if (data.toString('ascii', 0, 4) !== 'glTF') throw new Error('Not a GLB')
const jsonLength = data.readUInt32LE(12)
const gltf = JSON.parse(data.subarray(20, 20 + jsonLength).toString('utf8'))
const binHeader = 20 + jsonLength
const binLength = data.readUInt32LE(binHeader)
const bin = data.subarray(binHeader + 8, binHeader + 8 + binLength)
const sizes = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }
const formats = {
  5121: [Uint8Array, 1, false],
  5123: [Uint16Array, 2, false],
  5125: [Uint32Array, 4, false],
  5126: [Float32Array, 4, true],
}
function accessor(index) {
  const a = gltf.accessors[index]
  const view = gltf.bufferViews[a.bufferView]
  const [Type, bytes, floating] = formats[a.componentType]
  const components = sizes[a.type]
  const stride = view.byteStride || components * bytes
  const start = (view.byteOffset || 0) + (a.byteOffset || 0)
  const result = new Array(a.count)
  for (let i = 0; i < a.count; i++) {
    const row = new Array(components)
    for (let c = 0; c < components; c++) {
      const offset = start + i * stride + c * bytes
      row[c] = floating ? bin.readFloatLE(offset) : bytes === 1 ? bin.readUInt8(offset) : bin[`readUInt${bytes * 8}LE`](offset)
      if (a.normalized) row[c] /= (2 ** (bytes * 8)) - 1
    }
    result[i] = row
  }
  return result
}
const primitive = gltf.meshes[0].primitives[0]
const positions = accessor(primitive.attributes.POSITION)
const joints = accessor(primitive.attributes.JOINTS_0)
const weights = accessor(primitive.attributes.WEIGHTS_0)
const names = gltf.skins[0].joints.map(i => gltf.nodes[i].name.replace('mixamorig:', ''))
const rows = positions.map((p, i) => {
  const influences = joints[i].map((j, k) => ({ bone: names[j], weight: weights[i][k] }))
    .filter(x => x.weight > 0.0001).sort((a, b) => b.weight - a.weight)
  return { i, p, influences, dominant: influences[0]?.bone || null }
})
const target = process.argv.length >= 6 ? process.argv.slice(3, 6).map(Number) : [0.394, 0.391, -0.012]
if (target.length !== 3 || target.some(x => !Number.isFinite(x))) throw new Error('Target must be three numbers in glTF Y-up coordinates')
const nearest = rows.map(r => ({ ...r, distance: Math.hypot(...r.p.map((v, k) => v - target[k])) }))
  .sort((a, b) => a.distance - b.distance).slice(0, 12)
const suspects = {}
for (const bone of ['LeftArm', 'LeftForeArm', 'LeftHand', 'RightArm', 'RightForeArm', 'RightHand']) {
  const low = rows.filter(r => r.dominant === bone && r.p[1] < 0.82)
  suspects[bone] = {
    dominantBelow082: low.length,
    examples: low.slice(0, 8).map(r => ({ p: r.p, weight: r.influences[0]?.weight })),
  }
}
console.log(JSON.stringify({ file, vertices: rows.length, targetGltfYUp: target, nearest, suspects }, null, 2))
