# M2: shared Verilator / AXI fabric

M2 is a bus-model and DMA-skeleton milestone, **not framefx acceptance**.
The same clocked RTL and pin-level models now run against local RAM or guest
RAM through QEMU. No host model substitutes for RTL data transformation.

## Run and replay

```sh
make test
make rtl-lint
make rtl-test JOBS=8
make spike-test JOBS=8 BOOT_TIMEOUT=60
make report
```

The standalone target needs C++17 and Verilator, not SystemC, QEMU or a guest.
Since M3, `make rtl-test` runs this suite followed by the framefx image suite;
`make framefx-test` runs only the latter.
It lints, builds with assertions and VCD support, runs four seeds, repeats seed
1, and checks a traced replay. Runtime runs have a 60-second host deadline;
individual bus operations/jobs also have cycle deadlines. Failures are nonzero.
To replay a single case with a waveform after building, choose a new directory:

```sh
artifacts/build/rtl-test/rtl-test 1 artifacts/manual-rtl-replay 323
```

Arguments are seed, output directory and optional case ID. Omitting the case ID
runs the complete suite without tracing. The Python suite independently checks
snapshots and automatically reruns a failing case into `failure-trace/`; the
original failure remains a failure even if replay succeeds. C++ failures print
the case, seed, addresses and last 32 bus transitions. A process abort during an
assertion may require this replay to obtain a complete waveform.

## Components and supported subset

| Component | Responsibility |
|---|---|
| `sim/axi/fabric.hpp` | Parameterized AXI-Lite master, memory slave, deterministic stalls, stability/legality checks, RAM backend |
| `sim/axi/socket_memory.hpp` | Wire RPC backend; no RTL timing or image processing |
| `sim/axi/spike_model.hpp` | Shared drive/sample/clock sequencing, reset, CSR helpers, optional VCD |
| `rtl/core/axi_burst_copy.sv` | Synthesizable burst-copy skeleton, optional XOR constant, boundary planning and error handling |
| `rtl/spike/axi_spike.sv` | Experimental CSR, deferred START and IRQ fixture, using the core |
| `sim/rtl_test.cpp`, `scripts/rtl.py` | Standalone cases, snapshots, independent Python scoreboard and replay |

The AXI-Lite master has a 2–32-bit address-width parameter, fixed 32-bit data
and four write strobes. The current RTL aperture is 12 bits. AW and W are driven
independently; B and R acceptance can stall. It permits one outstanding CSR
operation. Response errors are returned to the caller rather than discarded.

The memory slave accepts 32- or 64-bit data and a maximum-burst parameter from
1 through 16. Tests cover widths 32/64 and limits 1/4/16. Addresses are carried
in 64 bits. Only aligned, full-width INCR bursts are supported, with one read
and one write outstanding independently and a single implicit ID. There are
no ID ports, narrow/unaligned bursts, FIXED/WRAP bursts, exclusives or reordering.
This is deliberately not a full AXI4 VIP implementation.

AW and W can arrive in either order. W-before-AW data is bounded by the maximum
burst buffer. Exactly one backend call is made for each accepted read burst or
completed write burst. BVALID waits for AW, all W beats with correct WLAST, and
the backend result. Byte enables are preserved, including an all-zero mask.
Reads hold data/response/LAST stable until accepted. Legal error responses still
emit the entire requested read burst, including RLAST on its final beat.

Independent channel PRNGs add deterministic bounded stalls. Explicit holds and
initial delays exercise long stalls and partial acceptance. Stability checkers
and SV assertions catch payload changes under backpressure; illegal lengths,
sizes, burst types, strobes, LAST and 4-KiB crossings fail instead of silently
being normalized. The RAM backend checks the entire requested range before
mutation. Injected RAM write failures do not mutate RAM; that is not a promise
of atomic rollback for arbitrary external memory failures.

The RTL skeleton is fixed at 64-bit data/address, with `MAX_BEATS` 1–16 and an
optional `XOR_MASK`. The M1 wrapper selects the original XOR constant. It reads
a complete chunk, writes it, waits for B, then advances. It does not overlap
read and write bursts for throughput. The next chunk is bounded by remaining
words, the configured burst limit and **both** 4-KiB address boundaries. Invalid
zero/unaligned/overlapping/overflowing jobs fail before DMA. A failed read chunk
is drained but not written; earlier completed chunks remain visible. A malformed
multi-beat RLAST is a protocol failure requiring reset, not a recoverable memory
error. The injected RLAST test is deliberately a single-beat case.

These boundary/handshake choices follow the
[Arm AXI specification](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/IHI0022H_amba_axi_protocol_spec.pdf).
The trace wrapper uses the [Verilator C++ tracing interface](https://verilator.org/guide/latest/faq.html#how-do-i-generate-waveforms-traces-in-c).
The modeled clock is 100 MHz (10 ns/cycle); host elapsed time is not simulated
time, and QEMU/Linux scheduling is not deterministic cycle synchronization.

## Regression coverage

Each seed runs 346 case entries:

- 324 combinations of lengths 1/2/15/16/17/31/32/33/257 words, three source
  placements, three destination placements and masks `00/55/aa/ff`.
- 11 configuration, first/second-burst response, unmapped and malformed-LAST cases.
- 10 reset phases: AR/R/AW/W/B, AXI-Lite partial AW/W, held B/R and pending IRQ.
- One 100-consecutive-job sequence without reset, checking data after every job.

Seeds are 1, 42, 2026 and `0xdeadbeef`: **1,384 unique seeded case entries**.
The complete seed-1 rerun and traced case add 347 executions. Full 64-KiB RAM
snapshots include source, destination, guards and untouched bytes. RAM begins at
`0x100000000`, so standalone tests exercise addresses above 4 GiB. Python builds
its expected cases, byte results and burst-address sequence independently of
the C++ planner and RTL; it rejects missing/duplicate cases or changed metadata.
Seed replay compares all cycle counts and the complete bus history byte-for-byte.
Tracing must preserve the selected case's result and cycle count.

`make test` additionally runs synthetic pin-level C++ fabric tests: 80 concurrent
read/write cases across widths, burst limits, address/data orderings and seeds,
plus 12 negative groups for malformed traffic, stability, timeouts and reset.
AXI-Lite parameter checks cover 2/12/32-bit apertures, independent W acceptance,
responses, invalid requests and timeout/reset. These are explicitly **not** RTL
tests. Python tests corrupt snapshots, metadata
and bus evidence, test failure replay, and reject stale milestone evidence.

The Linux extension retains the original 36 jobs and adds four multi-burst jobs
per seed. It independently compares every byte of a 16-KiB coherent buffer.
The suite requires exactly 61 read and 59 write RPCs per seed, with actual
128-byte messages in both directions. Three seeds produce 120 jobs/IRQs.
Existing fragmented-transport, concurrency, terminal-fault and bridge-safety
checks remain enabled; see [M1](m1-spike.md).

## Reset and evidence boundaries

Local reset clocks four synchronous reset edges, clears pending pin-level
transactions, CSR state and IRQ, and permits a fresh job. Partially collected
writes must not commit later. A write already committed before a held B response
**remains in RAM**. Reset does not roll back memory or reseed every PRNG;
deterministic replay creates a fresh model. Reset during a blocking socket RPC
is still unsupported: use a fresh complete session after transport failure.

`artifacts/latest-rtl.json` points to the result directory, containing hashes,
per-seed `cases.jsonl`, `events.log`, `console.log`, `case-N.bin` snapshots,
`repeat-1/`, and `trace/wave.vcd`. `make report` requires current source/binary
hashes for both standalone and Linux suites before marking M2 PASS. Local PASS
does not imply GitHub CI or physical FPGA validation. Framefx synthesis is
documented in [M3](m3-framefx.md); routed implementation and board acceptance
are separate [hardware gates](bringup-ultra96.md#headless-pl-integration).

M3 now supplies framefx CSR semantics, image geometry/stride handling,
copy/invert/threshold/Sobel datapaths and independent image scoreboards;
see [M3](m3-framefx.md). The production driver, UAPI and end-to-end image demo
are documented in [M4](m4-linux.md). `sim/axi/model.hpp` owns the common clock/reset/trace wrapper;
`spike_model.hpp` retains only the experimental fixture's CSR helpers.
