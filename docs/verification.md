# Verification and stress testing

## Choose the relevant gate

| Change or question | Check |
| --- | --- |
| Documentation only | Local links/anchors, command names and claims against source/evidence; no full rebuild |
| Python tooling, protocol or evidence validation | `make test`; run the affected test module first when useful |
| AXI models or RTL | `make rtl-test`; `make framefx-test` isolates the image core |
| QEMU/transport concurrency | `make spike-test` |
| Driver/UAPI/app or production end-to-end behavior | `make demo-sim`; add `make hardening-test` for control/compatibility changes |
| Ownership, concurrency or broad randomized coverage | `make stress-test` |
| V1 wrapper/design/DT | `make fpga-wrapper-test`, `make fpga-validate`, then `make fpga` as appropriate; none load hardware |
| Physical board | Follow the [hardware runbook](bringup-ultra96.md), not simulator fault-injection commands |
| PS/PL benchmark or its evidence | `make test benchmark-sim JOBS=4`; [measurement definitions](pl-performance.md); simulator timing is not physical performance |

`make report` re-evaluates saved evidence, not runtime behavior. Its printed
PASS summaries must be read with the per-record `inputs_current` flags in
`artifacts/report.json`. Source changes can invalidate earlier stress/image
results without implying a new test failure. `scripts/lab.py:project_hashes`
hashes all non-cache files under `scripts`, `linux`, `tests`, `fpga`, `qemu`,
`transport`, `sim` and `rtl`, plus `Makefile` and `versions.lock`. Thus even a
Markdown edit **inside those trees** invalidates core gate freshness; root
guidance and `docs/` edits do not. Refresh only the evidence needed for the
claim being made, rather than rebuilding every historical gate after any edit.

Hardware records marked `historical_observation_only` do not attest to the
current boot or current payload. See [status](progress.md) for the last observed
scope and remaining acceptance work.

## Commands and budgets

```
make test rtl-test spike-test demo-sim boot-smoke JOBS=8 BOOT_TIMEOUT=180
make stress-test JOBS=8
make stress-test JOBS=8 STRESS_CASES=1024 STRESS_JOBS=512 STRESS_SEED=2026
make report
```

The stress default is 512 standalone randomized cases and 256 image jobs in
each of two Linux boots (one CPU, then four TCG CPUs with three-byte socket
fragmentation). `STRESS_TIMEOUT=600` is the outer timeout per boot, not a hardware
watchdog. Case/job budgets accept 1–100000; seed accepts 1–`UINT32_MAX`. Larger
budgets require proportionally more disk and time; a 512-job boot initially
exports 64 MiB of full destination snapshots before lossless compression.
Never use concurrent `make -j` lab targets against shared build directories.

The guest kernel enables lockdep, atomic-sleep checking and DMA-API debugging
for **all** targets. It is a verification kernel, not a performance baseline.
The stress guest requires lockdep to remain active, DMA debugging enabled with
zero errors, equal positive CMA allocation/release deltas, and clean shutdown.
Warnings cannot silently become success merely because userspace printed PASS.

## Layers

- Host tests: wire framing/deadlines, evidence corruption rejection, C/Python
  references, ABI layouts, strict application compilation and fabric monitors.
- Sanitizers: ASan/UBSan execute the synthetic 32-/64-bit concurrent fabric and
  negative monitor cases. This is not a sanitized QEMU or guest-kernel run.
- Protocol stress: 21 synthetic-peer scenarios check bus-error survival, wrong
  IDs/addresses/kinds, lengths/statuses, invalid headers, disconnect/partial
  frames, slow headers, reverse IRQ flood/replay and malformed IRQs. A 150 ms
  bridge deadline must stop slow/flooding peers; each fault must latch once,
  reject further MMIO/reset traffic and leave QMP responsive.
- Real RTL MMIO: 128 illegal reads and 256 illegal writes interleaved with
  valid identity/configuration accesses and a healthy reset. These catch a
  bridge that mistakes valid SLVERR responses for transport failure.
- Randomized RTL: all four modes, seven patterns, arbitrary thresholds,
  edge/random widths, unequal strides, many 64-byte-aligned 4-KiB phases,
  addresses above 4 GiB, burst maxima 1/4/16 and random read/write error ordinals.
  Every case checks whole RAM, counters and burst history; error cases must
  recover without reset. Three cases repeat with tracing and identical history.
- Linux contention: four CPU-affined pthreads race two WAIT calls, capability
  reads, invalid SUBMIT, exclusive opens, duplicate descriptors, alias mappings,
  unmapping and unknown ioctls. Main-thread barriers protect the test's own
  image/descriptor state; ioctl arrival and scheduling remain nondeterministic.
- Lifetime: SUBMIT/WAIT copyout faults, retained results, monotonic job/IRQ
  accounting, new owners every 32 jobs, split VMAs, SIGKILL during a 1024² Sobel
  job, then unload/reload and the existing signal/timeout/active-close tests.

Both guest C and independent Python references check images. The host regenerates
the complete randomized geometry/seed manifest rather than trusting guest
metadata, and compares every destination byte including guards. The guest also
compares every source byte. Each boot starts from a fresh VM and simulator.

## Evidence and replay

`artifacts/latest-stress.json` points to a timestamped run. It records counts,
coverage bins, elapsed times, sanitizer status, protocol cases and Linux results.
`make report` separately marks `platform_stress.inputs_current`, checking every
project source hash and the actual QEMU, guest and RTL binaries. A small-budget
PASS means only the recorded budget; it does not imply the long soak ran.

The run retains `plan.json`, resolved kernel/Buildroot configs, exact launch
commands, source/binary SHA256 hashes, full standalone snapshots/bus logs,
sanitizer logs and Linux debug/CMA evidence. Successful Linux snapshots become
`destination.bin.gz`; the per-boot result keeps the uncompressed verification
digest and the compressed artifact digest. Failed runs retain uncompressed
snapshots and the failing case's automatic waveform replay.

Replay one standalone case using its saved configuration and binary:

```
artifacts/build/framefx-16/framefx-test /absolute/path/to/standalone/case-N SEED trace
```

Use that case's recorded `command.json` to select the correct burst-limit binary
and effective seed (`seed XOR case.id`); use a scratch copy of the directory to
avoid overwriting original evidence. Re-running the stress target with identical
budget and seed regenerates the same input plan, but Linux wall time, cycles,
IRQ timing and thread ordering are not promised bit-for-bit identical.

Host corruption tests deliberately damage pixels, padding, counters, seeded
metadata, lengths, CPU counts, lockdep status and CMA accounting. Each must fail
validation. CI includes a smaller 48-case/64-job-per-boot stress pass; checked-in
workflow configuration is **not** evidence that GitHub CI has executed.

See the [platform contract](architecture.md) for AXI, timing, memory, reset and
security limitations. Seeded stress and software sanitizers are not formal
verification, exhaustive AXI compliance, physical timing closure or board tests.

## Control and compatibility hardening

```
make hardening-test JOBS=8 BOOT_TIMEOUT=180
```

This separate gate requires host clang/lld and the debug kernel with
`CONFIG_COMPAT`. It runs 18 targeted fault scenarios on each of one and four
vCPUs, followed by an ARM32 runtime test on each configuration: 38 fresh boots.
Seventeen invalid simulator invocations must fail before creating any socket.
The host unit suite additionally tests strict numeric/fault parsing, including
overflow, trailing garbage, signed inputs, zero ordinals and unaligned MMIO.

Seven terminal scenarios interrupt capability reads, pre-SUBMIT idle checks,
configuration writes, START, pre-RESET idle checks, RESET and completion reads.
The selected AXI-Lite transfer has actually completed at the RTL pins before
its socket reply is lost. In particular, a failed START response cannot be
treated as a guarantee that DMA never began. Eleven one-shot corruption
scenarios cover version/capabilities, idle state, IRQ/status/error agreement,
reserved bits and completion byte counts. Neither mode substitutes a software
algorithm for RTL; corruption changes only the selected reply payload.

Each fault must produce an error, latch offline, reject reopening, and leave
lockdep/DMA-API debugging enabled with zero DMA-API errors. Every fixture
allocates two 8-KiB buffers: four CMA pages. Terminal loss must retain all four
pages, pin the module, latch the bridge fault exactly once and issue no later
DMA. One-shot corrupt telemetry must instead prove idle, release all four pages
and allow unload/reload. Single-page allocations bypass global CMA on this
kernel; they are deliberately not used as a reclamation measurement.

The simulator's test-only fault syntax is:

```
mmio-disconnect:KIND:ADDRESS:ORDINAL
mmio-corrupt:ADDRESS:ORDINAL:VALUE
```

KIND is 2 (read) or 3 (write). ADDRESS is an aligned offset within the 4-KiB
window. ORDINAL counts only accesses matching that kind/address, starting at
one. Integers accept decimal or `0x` hexadecimal and are range checked. The
injection marker must occur exactly once. These are targeted test controls,
not production recovery mechanisms or permission to retry uncertain writes.

The ARM32 fixture uses direct EABI syscalls, with compile-time UAPI layout and
ioctl-number assertions. Its eight jobs cover copy, invert, threshold and a
constant-image Sobel oracle, full source/destination allocations, 64-bit job-ID
input/output, mmap2's page-unit offset, invalid compat pointers, executable-map
rejection, RESET, and file/VMA lifetime. It needs no second guest libc. The
existing large native M4 suite remains the independent general Sobel oracle.

`artifacts/latest-hardening.json` records the run; the timestamped directory
retains launch commands, configurations, source/binary hashes, LLVM versions,
CLI rejection records, simulator and guest logs. `make report` marks freshness
separately as `control_compat_hardening.inputs_current`.

Export validators bound JSON to 4 KiB per record and validate numeric types,
hardware geometry, allocation extents and counters before allocating reference
images. Booleans are not accepted as integer fields. Binary comparisons read
at most the expected size plus one byte; a malformed multi-gigabyte artifact
cannot force an equally large comparison allocation. Stress metadata is read
one bounded line at a time with a bounded job count. Guest JSON cannot override
the host-derived evidence filename. These checks improve failure reporting and
resource bounds; a writable guest share is still not an adversarial sandbox.
