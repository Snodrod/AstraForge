# Native Standard UV Responsiveness Profiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This session uses inline execution because subagents are disabled.

**Goal:** Capture correlated CPU, RAM, VRAM, job-phase, and local API responsiveness evidence during one native Standard 1024 + UV 1024 run.

**Architecture:** A separate Node monitor samples the existing studio and native engine without modifying either process. It appends JSONL and writes a pure-function summary; a dedicated runner owns startup, monitoring, E2E, restoration, and cleanup.

**Tech Stack:** Node.js ESM, built-in `os`, `fetch`, `node:test`; existing AstraForge E2E.

## Global Constraints

- No production performance changes before baseline analysis.
- No external requests, browser automation, drivers, network changes, or concurrent GPU jobs.
- Preserve Standard 1024, UV 1024, single view, and fail-closed behavior.
- Do not overwrite the original `1024-single-uv1024.json` report.
- No commit or push unless separately requested.

---

### Task 1: Monitor math and evidence summary

**Files:**
- Create: `scripts/native/native_monitor.mjs`
- Test: `tests/native-monitor.test.mjs`

**Interfaces:**
- Produces: `cpuPercentBetween(previous, current): number|null`, `percentile(values, fraction): number|null`, `summarizeNativeSamples(samples): object`, and `createNativeMonitor(options): { start(), stop() }`.
- Consumes: sample records with `at`, `postReady`, `phase`, `system`, `studio`, and `engine` fields.

- [ ] **Step 1: Write failing pure-function tests**

Test 50% CPU from known idle/total deltas; empty percentile input; heartbeat percentiles; 250 ms/1 s stall counts; exclusion of pre-ready startup errors; per-phase maxima; and RAM/VRAM maxima. Example:

```js
assert.equal(cpuPercentBetween(
  { idle: 100, total: 200 }, { idle: 150, total: 300 },
), 50);
assert.equal(percentile([], 0.95), null);
assert.equal(percentile([100, 300, 1200], 0.95), 1200);
```

- [ ] **Step 2: Verify RED**

Run: `node --test tests/native-monitor.test.mjs`
Expected: FAIL because `native_monitor.mjs` does not exist.

- [ ] **Step 3: Implement the monitor**

Use non-overlapping asynchronous sampling. Each sample concurrently requests studio health, studio jobs, and engine stats with 2.5 s timeouts; compute OS CPU/RAM locally; append one JSON object per line immediately. Mark records before the first successful health check as `postReady: false`. Normalize the phase by removing the timer suffix after ` · `.

`start()` begins an immediate asynchronous loop; `stop()` stops scheduling, awaits the active sample, and writes summary JSON. Summary fields: `samples`, `postReadySamples`, `heartbeatMs` (p50/p95/max), `heartbeatStalls250Ms`, `heartbeatStalls1000Ms`, `postReadyErrors`, `maxCpuPercent`, `maxRamUsedMb`, `maxDeviceUsedMb`, and `phases` with per-phase sample counts and maxima.

- [ ] **Step 4: Verify GREEN**

Run: `node --test tests/native-monitor.test.mjs`
Expected: all tests pass with no warnings.

### Task 2: Dedicated profiling runner

**Files:**
- Create: `scripts/native/run_native_standard_uv1024_profile.mjs`

**Interfaces:**
- Consumes: `createNativeMonitor`, existing native readiness, and `scripts/verify_amd_pixal3d_e2e.mjs`.
- Produces: `1024-single-uv1024-profile.jsonl`, `1024-single-uv1024-profile.json`, `1024-single-uv1024-profile-e2e.json`, and an isolated server log.

- [ ] **Step 1: Add the runner**

Reuse the verified Standard UV runner lifecycle on ports `3214` and `8193`. Start the monitor after spawning the isolated studio and before spawning E2E. In `finally`, stop monitoring before stopping the studio, restore the original readiness byte-for-byte, and stop only the owned native runtime.

- [ ] **Step 2: Static and focused verification**

Run: `node --check scripts/native/run_native_standard_uv1024_profile.mjs && node --test tests/native-monitor.test.mjs tests/pixal3d-native.test.mjs tests/amd-pixal3d-e2e.test.mjs`
Expected: exit 0; all applicable tests pass.

### Task 3: Real baseline and analysis gate

**Files:**
- Create at runtime: `data/runtime/diagnostics/native-amd/1024-single-uv1024-profile*`
- Modify after result: `docs/forge/pixal3d-native-amd.md`
- Modify after result: `scripts/native/README.md`

**Interfaces:**
- Consumes: one real Standard 1024 + UV 1024 job and monitor JSONL.
- Produces: measured bottleneck evidence or an explicitly negative local-heartbeat result.

- [ ] **Step 1: Run one baseline**

Confirm no active native GPU job, then run `node scripts/native/run_native_standard_uv1024_profile.mjs`. Do not run browsers, external speed tests, or another GPU job during sampling.

- [ ] **Step 2: Validate evidence**

Require an accepted GLB, empty fallback events, parseable JSONL, matching summary counts, and no telemetry-induced job cancellation. Independently calculate the largest heartbeat stalls and phase maxima from JSONL.

- [ ] **Step 3: Review before optimization**

Identify a specific measured stall, memory spike, or phase regression. Revisit the plan before changing production code. If no local API stall is observed, report that bounded result without claiming freeze-free graphics, drivers, or external networking.

- [ ] **Step 4: Document measured facts and cleanup**

Update existing native documentation with sampling coverage, observed maxima/stalls, and remaining uncertainty. Stop owned studio/native processes and rerun the focused tests.
