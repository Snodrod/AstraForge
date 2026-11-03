# Native Standard 1024 + UV responsiveness profiling design

**Goal:** capture a trustworthy baseline for system load and local API responsiveness during one native RX 9070 XT Standard 1024 + UV 1024 run, then change performance code only where the evidence identifies a bottleneck.

**Chosen approach:** a separate monitoring runner around the existing E2E. The monitor lives in the runner process, not in AstraForge or ComfyUI, so the production event loops and initial baseline are not modified.

**Sampling:** approximately once per second, without overlapping samples. Each record contains timestamp, job phase/progress, system CPU utilization derived from `os.cpus()` deltas, system RAM from `os.totalmem()`/`os.freemem()`, studio `/api/health` latency/status, and native `/system_stats` VRAM/queue data when available. Requests have bounded timeouts. No external network requests, browser automation, driver changes, or additional GPU jobs.

**Evidence:** append JSONL after every sample so a crash does not erase the history. Write a final JSON summary with sample coverage, CPU/RAM maxima, VRAM maxima, heartbeat latency percentiles/max, stalls over 250 ms and 1 s, request errors, and per-phase aggregates. Startup/unavailable states are labelled separately from outages after the studio became ready. Telemetry failure is recorded but does not cancel the model job.

**Files:** add one focused monitor module, one Standard UV profiling runner, and focused Node tests. Preserve the original `1024-single-uv1024.json`; write the profiling E2E report and monitor evidence under distinct `1024-single-uv1024-profile*` names.

**Acceptance:** the real run completes with an accepted GLB and no fallback; JSONL remains parseable and contains correlated phase/CPU/RAM/heartbeat records; summary counts and percentiles are covered by unit tests. A clean heartbeat result means only “no local API stall observed at this sampling resolution,” not proof of freeze-free graphics, drivers, or external networking.

**Next gate:** inspect the baseline before optimizing. Any subsequent fix must target a measured stall, memory spike, or phase regression and retain Standard 1024 geometry, UV 1024, single view, and fail-closed behavior.
