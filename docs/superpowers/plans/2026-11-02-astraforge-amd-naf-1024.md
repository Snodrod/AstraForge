# AMD NAF and Pixal3D 1024 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ROCm NAF fallback measurably faster without changing its math, then conditionally verify the full single-view Pixal3D 1024/Heun profile.

**Architecture:** Keep the existing PyTorch fallback and make its row-block size configurable through a project-only environment variable. Prove parity and memory bounds with a standalone microbenchmark before any full generation; runtime code and generated models remain separate.

**Tech Stack:** Python 3.12, PyTorch ROCm, Node.js test runner, WSL2, RX 9070 XT 16 GB.

## Global Constraints

- Do not update the AMD driver, ROCm or system components; no reboot and no global `wsl --shutdown`.
- Keep `FLEX_GEMM_ALGO = 'implicit_gemm'` and existing precision patches unchanged.
- Do not start the full profile before microbenchmarks pass.
- Stop only `AstraForge-AMD` with `wsl.exe -t AstraForge-AMD` when needed.

---

### Task 1: Configurable bounded NAF rows

**Files:**
- Modify: `scripts/wsl/patch_trellis2_amd.py:152-201`
- Test: `tests/amd-wsl-scripts.test.mjs:107-144`

**Interfaces:**
- Produces: fallback `na2d(...)` reads `ASTRA_AMD_NAF_ROW_BLOCK`, clamps it to `[1, height]`, default `4`; numerical signature remains unchanged.

- [ ] **Step 1: Write the failing test**

Replace the fixed-row assertion with:

```js
assert.match(patched, /ASTRA_AMD_NAF_ROW_BLOCK/);
assert.match(patched, /row_block = max\(1, min\(height,/);
assert.doesNotMatch(patched, /row_block = 1\s/);
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test --test-name-pattern="NAF uses" tests/amd-wsl-scripts.test.mjs`
Expected: FAIL because the fallback still contains `row_block = 1`.

- [ ] **Step 3: Implement the minimal change**

Add `import os` and replace the fixed assignment inside the generated fallback:

```python
row_block = max(1, min(height, int(os.environ.get('ASTRA_AMD_NAF_ROW_BLOCK', '4'))))
```

- [ ] **Step 4: Run focused tests**

Run: `node --test tests/amd-wsl-scripts.test.mjs`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/wsl/patch_trellis2_amd.py tests/amd-wsl-scripts.test.mjs
git commit -m "Make AMD NAF row blocking configurable"
```

### Task 2: Parity and bounded benchmark

**Files:**
- Modify: `scripts/wsl/verify_naf_amd.py`
- Create: `scripts/wsl/benchmark_naf_amd.py`
- Test: `tests/amd-wsl-scripts.test.mjs`

**Interfaces:**
- Consumes: patched NAF root passed as argv[1].
- Produces: JSON benchmark records `{shape, rowBlock, seconds, peakBytes, maxAbs}`.

- [ ] **Step 1: Add failing parity and benchmark coverage**

Add odd-shape parity `(5, 7)` and static test assertions:

```js
const benchmark = read('scripts/wsl/benchmark_naf_amd.py');
assert.match(benchmark, /max_memory_allocated/);
assert.match(benchmark, /ASTRA_AMD_NAF_ROW_BLOCK/);
assert.match(benchmark, /\[1, 2, 4, 8\]/);
```

- [ ] **Step 2: Run tests to verify failure**

Run: `node --test tests/amd-wsl-scripts.test.mjs`
Expected: FAIL because the benchmark is absent.

- [ ] **Step 3: Implement benchmark**

Use shapes `(1,32,32,4,64)` and `(1,64,64,4,64)`, `value_dim=128`, kernel `9`, blocks `[1,2,4,8]`. For each block set the environment variable, clear cache, run one warmup and two measured iterations, synchronize, and emit:

```python
{
  'shape': list(q.shape),
  'rowBlock': block,
  'seconds': elapsed / 2,
  'peakBytes': torch.cuda.max_memory_allocated(),
  'maxAbs': (actual.cpu() - reference).abs().max().item(),
}
```

Stop on OOM, report the failed block, and keep the verified smaller result. The reference is block 1 on the same inputs; require `maxAbs < 2e-5`.

- [ ] **Step 4: Run static and syntax checks**

Run: `node --test tests/amd-wsl-scripts.test.mjs && python -m py_compile scripts/wsl/verify_naf_amd.py scripts/wsl/benchmark_naf_amd.py`
Expected: PASS and no Python syntax errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/wsl/verify_naf_amd.py scripts/wsl/benchmark_naf_amd.py tests/amd-wsl-scripts.test.mjs
git commit -m "Add bounded AMD NAF benchmark"
```

### Task 3: Hardware microbenchmark and conditional 1024 run

**Files:**
- Modify: `scripts/wsl/README.md`
- Modify: `docs/forge/pixal3d.md`
- Modify only if measurements justify it: `server/pixal3d-run.mjs`, `server/gpu-handover.mjs`

**Interfaces:**
- Consumes: project WSL `AstraForge-AMD`, patched NAF root and benchmark JSON.
- Produces: recorded selected row block; optionally a completed 1024 job and validated GLB.

- [ ] **Step 1: Reapply only project patches**

Run the existing project patch command for NAF; do not run the installer or update packages. Then run `verify_naf_amd.py` with the NAF root.
Expected: CPU/GPU parity PASS.

- [ ] **Step 2: Run bounded GPU benchmark**

Run `benchmark_naf_amd.py` with a 180-second process timeout.
Expected: JSON for blocks 1/2/4/8 or an explicit OOM stop; choose the fastest block whose peak leaves headroom below 16 GB and whose parity passes.

- [ ] **Step 3: Run one single-view 1024/Heun job**

Set `AMD_PIXAL3D_QUALITY=standard` after adding that E2E option in the vertex-color plan; do not enable coloring for this run. Monitor the existing job and VRAM probe. Stop if memory approaches the card limit or no stage progress is observed.
Expected: completed prompt and accepted GLB; otherwise record the exact unfinished stage and do not claim full-profile support.

- [ ] **Step 4: Adjust timeout only on demonstrated progress**

If the sole failure is the existing 60-minute deadline while progress continues, change both deadlines to a measured bounded value and add a Node test using fake timers. Otherwise leave timeouts unchanged.

- [ ] **Step 5: Record and commit**

Write actual shapes, times, peak bytes, selected block and job outcome to `docs/forge/pixal3d.md`; commit documentation and any tested timeout change:

```bash
git add scripts/wsl/README.md docs/forge/pixal3d.md server/pixal3d-run.mjs server/gpu-handover.mjs tests
 git commit -m "Verify AMD Pixal3D full profile"
```
