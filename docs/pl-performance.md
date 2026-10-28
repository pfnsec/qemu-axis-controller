# PS/PL measurements

`linux/framefx/app/framefx-bench.c` measures the existing framefx UAPI, driver
and production RTL. It does not use `/dev/mem`, program PL, change clocks,
inject faults, reset the core or unload the driver. Physical execution still
requires the [hardware runbook](bringup-ultra96.md) and a supervised, identified,
healthy boot. A userspace timeout is **not** a hardware watchdog.

## What is measured

- **Control latency:** 128 individual `QUERY_CAPS` calls after 16 warmups and
  an initial identity check. Each call includes the syscall, driver mutex,
  three AXI-Lite reads, copyout and userspace clock/validation overhead. This
  is not the latency of one AXI transaction. No timer-overhead subtraction.
- **Job latency:** `CLOCK_MONOTONIC_RAW` immediately before `SUBMIT` until
  successful `WAIT` return; separate submit and wait intervals are retained.
  Includes driver/MMIO, execution, interrupt handling, wakeup and scheduling.
  Excludes allocation, CPU reference generation, buffer preparation,
  verification, JSON logging and snapshot/USB export.
- **Hardware execution:** the production busy-cycle counter, converted only
  when the nominal PL clock is explicitly supplied from the verified design.
  Userspace elapsed time minus hardware time is **not** an IRQ-latency probe:
  endpoint placement, MMIO and scheduling contribute too.
- **Payload MiB/s:** sum of completed output pixels divided by the sum of
  measured job intervals and 2²⁰. **Counted DMA MiB/s** uses read bytes plus
  enabled write bytes instead. Reads round each row to eight bytes; write
  counters exclude disabled tail strobes. Neither measures physical HP bus
  utilization, peak DDR bandwidth or all AXI protocol traffic.
- **Host preparation/verification:** separate intervals for copying the
  prepared source plus clearing the destination, and for validating both
  allocations plus their CRCs. These deliberately remain outside job timing.

The tool allocates and maps once per profile, then reuses the same buffers.
Every job changes the input seed, cycling through four reproducible images.
Unequal padded strides and a trailing page guard exercise PS writes, PL reads,
PL writes and PS reads on the non-coherent HP path. Warmup jobs undergo exactly
the same correctness checks; only their timing samples are excluded.

The optional second pthread repeatedly queries capabilities, requesting a
100-µs sleep between calls. It runs across preparation, execution and checking.
The current driver holds its ioctl mutex during `WAIT`, so these queries can
block until DMA completes. This tests concurrent userspace/control ownership,
not unrestricted simultaneous AXI-Lite access during an outstanding WAIT.
Control-query count, accumulated time and maximum latency are retained; do not
interpret the requested sleep as a measured traffic rate or claim CPU affinity.

The results are **active-job rates**, not sustained application/video frame
rates: CPU checking and inter-job gaps are intentional. No CPU speedup,
display latency, hard real-time bound, exact CMA balance or malicious-IP
containment is established by this benchmark.

## Correctness and evidence

The native C reference checks **every byte of both complete allocations on
every job**, including source preservation, row padding and trailing guards.
It also checks status, byte counters and sequential job/interrupt counters.
Any error stops the application; there is no retry or recovery ioctl.
Successful completion unmaps, closes, reopens, queries capabilities and closes
again. The physical supervisor separately checks exact delivered IRQ deltas,
zero module references, unchanged boot identity and kernel health per profile.

Each profile exports:

- `benchmark.jsonl`: bounded plan, all samples (including warmups), and final
  completion record. Raw nanosecond values are preserved.
- `source.bin`, `destination.bin`: the final job's complete allocations.
- Physical supervision adds before/after snapshots, stderr, payload hashes
  and its own completion report.

`scripts/pl_benchmark.py` is an **offline** verifier. Independent Python image
generation and filters check each job's full-buffer CRC32; the final snapshots
are additionally compared byte-for-byte. CRC32 is an accidental-corruption
check, not cryptographic proof; native per-byte checks cover every job, while
independent host byte comparisons cover the final job only. Artifact SHA256
digests bind saved files. Metadata bounds and types are validated before large
allocations; malformed evidence cannot turn into PASS by supplying timings.

Reported percentiles use nearest rank; median uses the ordinary middle-value
median. The report retains minimum/median/p95/p99/maximum/mean and sample count.
Small sample populations do not establish reliable extreme-tail bounds.

```sh
/usr/bin/python3 scripts/pl_benchmark.py '<profile-directory>' --clock-hz 100000000
make test JOBS=4
make benchmark-sim JOBS=4 BOOT_TIMEOUT=180
```

`benchmark-sim` boots disposable one- and four-vCPU Linux guests against the
real Verilated RTL, all four modes with and without control contention: 48 jobs
per guest. It checks kernel DMA debugging, lockdep, balanced CMA page counts,
IRQ continuity and independent results. **Its wall times are not board
performance.** `artifacts/latest-benchmark-sim.json` and `make report` track this
gate separately from physical `artifacts/latest-pl-benchmark.json` observations.

The native program's explicit interface is:

```text
framefx-bench WIDTH HEIGHT MODE SAMPLES WARMUP CONTEND OUTPUT_DIRECTORY
```

Dimensions are 3–1024; mode 0–3; measured samples 1–256; warmups 0–16; contention
0 or 1. Threshold is 127, seeds start at 12000. Supply a fresh private output
directory and capture stdout as `benchmark.jsonl`. Snapshot writes use normal
RAM staging because filesystem paths such as 9p cannot necessarily pin DMA
mappings directly. The argument interface is not a substitute for board
identity, payload, ownership and safety preflight. There is deliberately no
Make target that contacts hardware or silently reruns a failed measurement.

Buildroot includes the program in the simulation guest. A separate native
cross-build can be deployed to volatile board scratch without changing the
frozen PL bundle, driver, UAPI or boot image; retain compiler, flags, source and
binary hashes. The boot-bound scripts under a physical evidence directory
record that particular supervised session, not future operational permission.

## Physical baseline — 2026-10-22

UTC evidence: `artifacts/hardware/pl-benchmark-20261023T003037Z/`, indexed by
`artifacts/latest-pl-benchmark.json`. This is the mikro boot already running
the verified 100-MHz design, not a new PL load. The PS governor was `userspace`
with reported CPU frequency 1,199,999 kHz; no affinity or real-time scheduling
was imposed. Frequency conversion uses the **nominal** PL platform clock,
not an independent clock measurement; debugfs clock summary was unavailable.

**1,124 additional jobs / exactly 1,124 delivered interrupts passed**, across
25 profiles. All four modes ran at 3×3, 65×9, 513×17, 320×240 and 1024×1024;
four additional full-size profiles enabled control contention. Each used four
warmups and 32 measured jobs. A final copy repetition test used four warmups
and 256 measured jobs. All allocations passed native checks, host CRC checks
and final-snapshot byte comparisons. Every profile released ownership cleanly.

| Profile | Median submit→wait | p95 | Payload MiB/s |
| --- | ---: | ---: | ---: |
| 3×3 copy | 21.945 µs | 22.720 µs | 0.394 |
| 65×9 copy | 33.240 µs | 33.580 µs | 16.850 |
| 513×17 copy | 196.275 µs | 196.800 µs | 42.370 |
| 320×240 copy | 1.556 ms | 1.558 ms | 47.039 |
| 1024×1024 copy | 19.934 ms | 19.948 ms | 50.164 |
| 1024×1024 Sobel | 19.925 ms | 19.929 ms | 50.190 |
| 1024×1024 copy + control thread | 19.932 ms | 19.939 ms | 50.173 |
| 1024×1024 Sobel + control thread | 19.924 ms | 19.928 ms | 50.193 |
| 1024×1024 copy, 256 measured repeats | 19.937 ms | 19.967 ms | 50.147 |

Full-size counted read+write throughput was about **100.3–100.4 MiB/s**.
Nominal hardware execution was about 19.88 ms / 1.988 million PL cycles.
Uncontended capability queries had a **1.60-µs median**. The concurrent workers
completed **504,282 capability calls**; their worst calls approached one full
frame interval, consistent with the driver's WAIT mutex. These runs show
similar job throughput with control contention, not a universal contention
bound. A later idle 64-query check passed; GIC 121 remained at 1,273 cumulative
completions (including the earlier 149 jobs), module references were zero and
no fatal kernel/DMA diagnostics appeared. PL remained operating.

### Interpretation and next measurements

The full-size verification interval was approximately **0.52 seconds per job**:
the byte-wise CRC reads directly from non-coherent DMA mappings dominate this
debug workload. That cost is excluded from the ~20-ms job figure, but prevents
claiming 50-fps end-to-end operation or a continuous-duty thermal soak. The
next useful PS-side experiment is bulk-copying completed DMA data into cached
RAM before CRC/consumption, with a separately timed download and realistic
producer/consumer rate. Do not change DMA cache attributes ad hoc.

By inspection, `framefx_pipeline.sv` serializes row read, pixel scan and row
write. Similar copy/Sobel execution times fit that shared pipeline; they do
not establish that DDR or HP0 is saturated. After measuring PS staging, a
double-buffered row/DMA-overlap experiment is a reasonable RTL optimization,
but needs simulation and a fresh, deliberately loaded hardware bitstream.
Keep the current design as the measured baseline; do not hot-replace it.

See [current status](progress.md) for remaining deployment/display acceptance.
